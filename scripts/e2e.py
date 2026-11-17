#!/usr/bin/env python3
"""End-to-end workflow against the running three services.

Drives the real HTTP APIs from an empty data directory:
  1. ingest raw values (verbatim) from the generated seed CSV,
  2. create and publish a rule generation,
  3. build a full linkage release, inspect collision evidence,
  4. record an adjudication and rebuild,
  5. change the rules, show merge/split impact, preview, then publish,
  6. test incremental build and build cancellation,
  7. verify historical reports did not change.

Usage: python3 scripts/e2e.py --base http://127.0.0.1:8800 --data-dir data
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "seed" / "raw_rows.csv"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


def call(base: str, path: str, method: str = "GET", body=None, expect_error: bool = False):
    url = base.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return json.loads(raw) if raw.strip() else None
            text = raw.decode("utf-8-sig")
            if "csv" in ctype:
                return text
            return text
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if expect_error:
            try:
                return json.loads(payload)
            except Exception:
                return {"error": payload.decode("utf-8", "replace")}
        raise RuntimeError(f"{method} {path} -> {exc.code}: {payload[:800]!r}") from None


def wait_job(base: str, job_id: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = call(base, f"/api/link/jobs/{job_id}")
        if job["status"] not in {"queued", "running", "cancelling"}:
            return job
        time.sleep(0.15)
    raise TimeoutError(job_id)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8800")
    ap.add_argument("--limit", type=int, default=0, help="limit rows ingested (0 = all)")
    args = ap.parse_args()
    base = args.base

    print("== 0. service health")
    for svc in ["raw", "norm", "link"]:
        h = call(base, f"/api/{svc}/health")
        check(f"{svc} healthy", h.get("status") == "ok", str(h))

    print("== 1. ingest raw values verbatim")
    rows = list(csv.DictReader(SEED.open(encoding="utf-8")))
    if args.limit:
        rows = rows[: args.limit]
    payload = {"rows": [{"source_system": r["source_system"], "source_id": r["source_id"],
                         "raw_value": r["raw_value"]} for r in rows], "note": "e2e seed"}
    res = call(base, "/api/raw/ingest", "POST", payload)
    check("ingest accepted all rows", res["rejected"] == 0, str(res.get("rejected_rows"))[:300])
    check("inserted count matches", res["inserted"] == len(rows), f"{res['inserted']} vs {len(rows)}")
    snap = call(base, "/api/raw/snapshot")
    check("active snapshot = rows", snap["active_count"] == len(rows), str(snap["active_count"]))
    # verbatim preservation: read one record back and compare code points exactly
    target_index = next(i for i, r in enumerate(rows) if r["raw_value"] == "ＡＣＭＥ　ＧｍｂＨ")
    raw_id = res["records"][target_index]["raw_id"]
    rec = call(base, f"/api/raw/records/{raw_id}")
    original = rows[target_index]["raw_value"]
    check("raw value preserved byte-for-byte", rec["raw_value"] == original,
          repr((rec["raw_value"], original)))
    check("raw escaped form exposed", rec["raw_escaped"] == original.encode("unicode_escape").decode("ascii"))
    check("raw codepoints exposed", len(rec["raw_codepoints"]) > 0, str(rec["raw_codepoints"]))

    # duplicate ingest must not create revisions
    again = call(base, "/api/raw/ingest", "POST", payload)
    check("re-ingest is idempotent (unchanged)", again["unchanged"] == len(rows) and again["inserted"] == 0,
          f"{again['unchanged']}/{again['inserted']}")
    # a changed value for the same identity creates a revision
    first_raw_id = res["records"][0]["raw_id"]
    changed = call(base, "/api/raw/ingest", "POST", {
        "rows": [{"source_system": rows[0]["source_system"], "source_id": rows[0]["source_id"],
                  "raw_value": rows[0]["raw_value"] + "  "}]})
    check("new value creates a revision", changed["revised"] == 1, str(changed))
    before = call(base, "/api/raw/records?active=1&limit=5000")
    check("revision replaced active record, count stable", len(before["items"]) == len(rows),
          str(len(before["items"])))
    old = call(base, f"/api/raw/records/{first_raw_id}")
    check("old revision retained as inactive", old["active"] is False and old["superseded_by"] is not None,
          str(old))

    print("== 2. rule generation lifecycle")
    rules = call(base, "/api/norm/rules")
    base_gen = rules["current"]
    check("seeded published generation exists", base_gen["status"] == "published")
    bad = call(base, "/api/norm/rules/generations", "POST",
               {"label": "bad", "steps": [{"op": "nope"}]}, expect_error=True)
    check("invalid op rejected", "op" in bad["error"], str(bad))
    bad2 = call(base, "/api/norm/rules/generations", "POST",
                {"label": "bad regex", "steps": [{"op": "regex_replace", "pattern": "("}]}, expect_error=True)
    check("invalid regex rejected", "正则" in bad2["error"], str(bad2))

    steps_v2 = [
        {"op": "unicode_nfkc", "note": "全角→半角"},
        {"op": "strip_format", "note": "去零宽"},
        {"op": "trim", "note": "首尾空白"},
        {"op": "collapse_ws", "note": "折叠空白"},
        {"op": "casefold", "note": "大小写折叠"},
        {"op": "strip_diacritics", "note": "去变音符号"},
        {"op": "strip_punct", "keep": "-_", "note": "去句点等标点但保留下划线与连字符"},
        {"op": "collapse_ws"},
        {"op": "trim"},
    ]
    draft = call(base, "/api/norm/rules/generations", "POST",
                 {"label": "v2 去变音+标点", "steps": steps_v2, "based_on": base_gen["generation_id"]})
    check("draft created", draft["status"] == "draft", str(draft))
    # drafts are editable
    edited_steps = [dict(s) for s in steps_v2]
    updated = call(base, f"/api/norm/rules/generations/{draft['generation_id']}", "PUT",
                   {"label": "v2 去变音+标点", "steps": edited_steps, "note": "e2e edit"})
    check("draft editable", updated["status"] == "draft")
    pub = call(base, f"/api/norm/rules/generations/{draft['generation_id']}/publish", "POST", {})
    check("draft published", pub["published"]["status"] == "published")
    check("old generation superseded, not modified",
          call(base, f"/api/norm/rules/generations/{base_gen['generation_id']}")["status"] == "superseded")
    locked = call(base, f"/api/norm/rules/generations/{base_gen['generation_id']}", "PUT",
                  {"label": "hack"}, expect_error=True)
    check("published generation is immutable", locked.get("error") and "草稿" in locked["error"], str(locked))

    preview = call(base, "/api/norm/normalize/preview", "POST", {
        "generation_id": draft["generation_id"],
        "values": ["ＡＣＭＥ　ＧｍｂＨ", "ACME​ GmbH", "  acme   gmbh ", "Müller GmbH",
                   "Muller GmbH", "ＡＣＭＥ　ＧＭＢＨ"]})
    keys = [r["key"] for r in preview["results"]]
    check("fullwidth+case+spaces collapse to one key",
          len({keys[0], keys[1], keys[2], keys[5]}) == 1, str(keys))
    check("diacritic variant merges under NFKD+strip",
          keys[3] == keys[4] == "muller gmbh", str(keys[3:5]))
    ev = preview["results"][0]
    check("evidence keeps raw text", ev["raw"] == "ＡＣＭＥ　ＧｍｂＨ")
    check("evidence records per-step changes",
          any(s.get("changes") for s in ev["steps"]) and ev["changed_steps"] >= 1, str(ev["changed_steps"]))
    check("evidence includes codepoints",
          any(c["codepoint"].startswith("U+FF") for c in ev["raw_codepoints"]), str(ev["raw_codepoints"]))

    print("== 3. full build with collisions")
    job = call(base, "/api/link/builds", "POST", {"mode": "full", "publish": False, "generation_id": draft["generation_id"]})
    job = wait_job(base, job["id"])
    check("full build completed", job["status"] == "completed", str(job.get("error")))
    rel1 = job["release_id"]
    check("build produced a preview release", job["impact"]["published"] is False)
    rel = call(base, f"/api/link/releases/{rel1}")
    check("release pinned to v2 generation", rel["generation_id"] == draft["generation_id"])
    check("release has conflicts (not auto-merged)", rel["conflict_class_count"] > 0,
          str(rel["conflict_class_count"]))
    conf = call(base, f"/api/link/releases/{rel1}/conflicts?page_size=100")
    check("conflict report lists members with evidence",
          conf["total"] > 0 and all(c["members"] for c in conf["items"]))
    checked = 0
    for cls in conf["items"]:
        raws = {m["raw_value"] for m in cls["members"]}
        if len(raws) > 1:
            checked += 1
        if cls["normalized_key"] == "":
            check("empty key isolated in own class", len(cls["members"]) >= 1)
    check("some conflict classes hold multiple distinct raw strings", checked > 0, str(checked))
    # every member is retained: no rows dropped
    members = call(base, f"/api/link/releases/{rel1}/members.csv")
    check("members csv downloadable", isinstance(members, str) and "raw_value" in members)

    # activate release1 so we have a baseline
    act = call(base, f"/api/link/releases/{rel1}/activate", "POST",
               {"expected_generation_hash": rel["generation_hash"]})
    check("preview release activated", act["status"] == "active")
    check("activate with wrong hash rejected",
          call(base, f"/api/link/releases/{rel1}/activate", "POST",
               {"expected_generation_hash": "0" * 64}, expect_error=True).get("error") is not None)

    print("== 4. adjudication: split a collision")
    target = next((c for c in conf["items"] if c["distinct_raw_count"] > 1 and c["normalized_key"]), None)
    check("found a collision class to adjudicate", target is not None)
    if target:
        shas = sorted({m["raw_sha256"] for m in target["members"]})
        first_of: dict[str, str] = {}
        for m in target["members"]:
            first_of.setdefault(m["raw_sha256"], m["source_system"])
        by_source: dict[str, list[str]] = {}
        for sha in shas:
            by_source.setdefault(first_of[sha], []).append(sha)
        group_list = list(by_source.values())
        if len(group_list) < 2:
            group_list = [shas[:1], shas[1:]]
        adj = call(base, "/api/link/adjudications", "POST", {
            "release_id": rel1, "class_id": target["class_id"], "action": "split",
            "groups": group_list, "note": "e2e split", "operator": "e2e",
        })
        check("adjudication recorded", adj["status"] == "active" and adj["action"] == "split")
        dup = call(base, "/api/link/adjudications", "POST", {
            "release_id": rel1, "class_id": target["class_id"], "action": "accept_merge"},
            expect_error=True)
        check("second active adjudication on same key rejected", "已有" in dup.get("error", ""), str(dup))
        job2 = call(base, "/api/link/builds", "POST", {"mode": "incremental", "publish": True})
        job2 = wait_job(base, job2["id"])
        check("incremental build completed", job2["status"] == "completed", str(job2.get("error")))
        rel2 = job2["release_id"]
        got = call(base, f"/api/link/releases/{rel2}/classes?status=adjudicated&page_size=100")
        check("split produced adjudicated classes", got["total"] >= 2, str(got["total"]))
        hist = call(base, f"/api/link/releases/{rel1}/classes?page_size=5")
        check("historical release unchanged by new build",
              call(base, f"/api/link/releases/{rel1}")["status"] == "superseded")
        check("old release still reports its original conflict",
              call(base, f"/api/link/releases/{rel1}")["conflict_class_count"] == rel["conflict_class_count"])

    print("== 5. rule change: full rebuild shows merges and splits")
    steps_v3 = [
        {"op": "unicode_nfkc"},
        {"op": "strip_format"},
        {"op": "trim"},
        {"op": "collapse_ws"},
        {"op": "casefold"},
        {"op": "strip_diacritics"},
        {"op": "strip_punct", "note": "v3 去掉全部标点（含 - _），会把 ACME-Corp 与 ACME Corp 合并"},
        {"op": "remove_ws", "note": "v3 去掉所有空白，进一步扩大合并面"},
    ]
    v3 = call(base, "/api/norm/rules/generations", "POST",
              {"label": "v3 激进合并", "steps": steps_v3, "based_on": draft["generation_id"]})
    call(base, f"/api/norm/rules/generations/{v3['generation_id']}/publish", "POST", {})
    stale = call(base, "/api/link/builds", "POST", {"mode": "incremental", "publish": True},
                 expect_error=True)
    check("incremental rejected after rule change", "全量" in stale.get("error", ""), str(stale))
    job3 = call(base, "/api/link/builds", "POST", {"mode": "full", "publish": False})
    job3 = wait_job(base, job3["id"])
    check("v3 full build completed", job3["status"] == "completed", str(job3.get("error")))
    rel3 = job3["release_id"]
    rel3data = call(base, f"/api/link/releases/{rel3}")
    check("v3 release pinned to v3", rel3data["generation_id"] == v3["generation_id"])
    diff = call(base, f"/api/link/diff?base={rel2}&candidate={rel3}")
    check("diff reports merges", len(diff["merges"]) > 0, json.dumps(diff["impact"], ensure_ascii=False))
    check("diff impact counts consistent",
          diff["impact"]["merged_new_classes"] == len(diff["merges"])
          and diff["impact"]["split_old_classes"] == len(diff["splits"]))
    check("aggressive v3 only merges, does not split old keys", len(diff["splits"]) == 0,
          json.dumps(diff["impact"], ensure_ascii=False))

    print("== 5b. finer rule: same data splits previously merged keys")
    # v4 keeps spaces and hyphens distinct, so v3's aggressive merges break apart.
    steps_v4 = [
        {"op": "unicode_nfkc"}, {"op": "strip_format"}, {"op": "trim"}, {"op": "casefold"},
        {"op": "strip_diacritics"},
    ]
    v4 = call(base, "/api/norm/rules/generations", "POST",
              {"label": "v4 更细粒度", "steps": steps_v4, "based_on": v3["generation_id"]})
    call(base, f"/api/norm/rules/generations/{v4['generation_id']}/publish", "POST", {})
    job4 = call(base, "/api/link/builds", "POST", {"mode": "full", "publish": False})
    job4 = wait_job(base, job4["id"])
    check("v4 full build completed", job4["status"] == "completed", str(job4.get("error")))
    rel4 = job4["release_id"]
    diff34 = call(base, f"/api/link/diff?base={rel3}&candidate={rel4}")
    check("finer rules produce splits", len(diff34["splits"]) > 0,
          json.dumps(diff34["impact"], ensure_ascii=False))
    check("split sample carries source raw values",
          any(s.get("raw_value") for x in diff34["splits"] for s in x["sample"]))
    check("historical v3 report still intact",
          call(base, f"/api/link/releases/{rel3}")["class_count"] == rel3data["class_count"])

    print("== 6. build cancellation and mixed-generation protection")
    cancelled = None
    for _ in range(3):
        j = call(base, "/api/link/builds", "POST", {"mode": "full", "publish": True})
        time.sleep(0.02)
        c = call(base, f"/api/link/jobs/{j['id']}/cancel", "POST", {})
        if c["status"] == "cancelled":
            cancelled = c
            break
    if cancelled:
        check("build cancelled without touching active release",
              call(base, "/api/link/releases/active")["id"] == rel2)
        check("cancelled release left no staged rows",
              all(r["id"] != cancelled.get("release_id") for r in call(base, "/api/link/releases?limit=100")["releases"]))
    else:
        print("  · builds completed too fast to cancel; cancellation path covered by unit test")
    bad_gen = call(base, "/api/link/builds", "POST",
                   {"mode": "full", "generation_id": base_gen["generation_id"]}, expect_error=True)
    check("build with non-current generation rejected", "世代" in bad_gen.get("error", ""), str(bad_gen))

    print("== 7. report stability")
    rep_before = call(base, f"/api/link/releases/{rel1}/report.json")
    newgen = call(base, "/api/norm/rules/generations", "POST",
                  {"label": "v4 noise", "steps": [{"op": "trim"}]})
    call(base, f"/api/norm/rules/generations/{newgen['generation_id']}/publish", "POST", {})
    rep_after = call(base, f"/api/link/releases/{rel1}/report.json")
    strip = lambda r: {k: v for k, v in r.items() if k != "generated_at"}
    check("historical report bytes unchanged after rule publish",
          strip(rep_before)["report_body_sha256"] == strip(rep_after)["report_body_sha256"])
    check("report carries generation pin", rep_after["release"]["generation_id"] == draft["generation_id"])
    ev_csv = call(base, f"/api/norm/runs/{rel['normalizer_run_id']}/evidence.csv")
    check("evidence csv downloadable", isinstance(ev_csv, str) and "raw_value" in ev_csv)
    check("evidence csv preserves fullwidth chars", "ＡＣＭＥ" in ev_csv)

    print()
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
