"""Linkage-result service.

Consumes a pinned normalization run and materializes equivalence classes.

Invariants
----------
* Every published release is pinned to exactly one rule generation and one
  normalizer run.  All class/member rows of a release belong to that release,
  so a release can never mix generations.
* Releases are immutable once published.  Reading a historical report always
  uses the release pointer, never the "current" rules.
* Collisions (one key, several distinct raw strings) are never silently merged
  into one identity.  They are published as ``conflict`` classes with every
  member retained and listed in a conflict report.
* Human adjudications are stored separately and carry a snapshot of the raw
  values they were made about.  If new raw values join the key, the
  adjudication is reported as ``needs_review`` instead of being applied.
* Incremental maintenance is only allowed when the rule generation is
  unchanged; a rule change forces a full rebuild because every key may move.
* Builds write into a staging release and publish by pointer swap in a single
  transaction.  Cancelling a build leaves the previous release untouched.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import threading
import time
import traceback
import unicodedata
from pathlib import Path
from typing import Any

from .common import (
    App, DB, HTTPError, RemoteError, Request, Response, binary_response, csv_response,
    http_request, json_dumps, json_pretty, json_response, now_iso, sha256_text,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS generations (
    generation_id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    steps_hash TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    published_at TEXT,
    mirrored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    generation_id INTEGER NOT NULL,
    generation_hash TEXT NOT NULL,
    generation_label TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    normalizer_run_id TEXT,
    parent_release TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT,
    item_count INTEGER NOT NULL DEFAULT 0,
    class_count INTEGER NOT NULL DEFAULT 0,
    conflict_class_count INTEGER NOT NULL DEFAULT 0,
    clean_class_count INTEGER NOT NULL DEFAULT 0,
    adjudicated_class_count INTEGER NOT NULL DEFAULT 0,
    empty_item_count INTEGER NOT NULL DEFAULT 0,
    member_conflict_count INTEGER NOT NULL DEFAULT 0,
    adjudication_applied INTEGER NOT NULL DEFAULT 0,
    adjudication_review INTEGER NOT NULL DEFAULT 0,
    report_sha256 TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_releases_status ON releases(status, created_at);
CREATE TABLE IF NOT EXISTS classes (
    release_id TEXT NOT NULL REFERENCES releases(id),
    class_id TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    key_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    member_count INTEGER NOT NULL,
    distinct_raw_count INTEGER NOT NULL,
    source_count INTEGER NOT NULL,
    conflict_member_count INTEGER NOT NULL DEFAULT 0,
    split_group INTEGER,
    adjudication_id INTEGER,
    PRIMARY KEY(release_id, class_id)
);
CREATE INDEX IF NOT EXISTS idx_classes_key ON classes(release_id, key_sha256);
CREATE INDEX IF NOT EXISTS idx_classes_status ON classes(release_id, status);
CREATE TABLE IF NOT EXISTS members (
    release_id TEXT NOT NULL,
    class_id TEXT NOT NULL,
    raw_id INTEGER NOT NULL,
    raw_sha256 TEXT NOT NULL,
    raw_value TEXT NOT NULL,
    source_system TEXT NOT NULL,
    source_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    normalized_key TEXT NOT NULL,
    key_sha256 TEXT NOT NULL,
    conflict INTEGER NOT NULL DEFAULT 0,
    empty_key INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(release_id, raw_id)
);
CREATE INDEX IF NOT EXISTS idx_members_class ON members(release_id, class_id);
CREATE INDEX IF NOT EXISTS idx_members_source ON members(release_id, source_system);
CREATE TABLE IF NOT EXISTS adjudications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_sha256 TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    generation_hash TEXT NOT NULL,
    generation_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    raw_shas_json TEXT NOT NULL,
    groups_json TEXT NOT NULL,
    note TEXT,
    operator TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    last_applied_release TEXT,
    last_applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_adj_key ON adjudications(key_sha256, status);
-- Frozen copy of the adjudications that were actually applied to a release.
CREATE TABLE IF NOT EXISTS release_adjudications (
    release_id TEXT NOT NULL,
    adjudication_id INTEGER NOT NULL,
    normalized_key TEXT NOT NULL,
    key_sha256 TEXT NOT NULL,
    action TEXT NOT NULL,
    raw_shas_json TEXT NOT NULL,
    groups_json TEXT NOT NULL,
    note TEXT,
    operator TEXT,
    created_at TEXT NOT NULL,
    revoked_later INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(release_id, adjudication_id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    publish INTEGER NOT NULL,
    generation_id INTEGER NOT NULL,
    generation_hash TEXT NOT NULL,
    generation_label TEXT NOT NULL,
    release_id TEXT,
    parent_release TEXT,
    stage TEXT,
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    raw_item_count INTEGER NOT NULL DEFAULT 0,
    affected_item_count INTEGER NOT NULL DEFAULT 0,
    affected_key_count INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    impact_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    note TEXT
);
"""

CANCEL_POLL_SECONDS = 0.2


def _release_row(row: dict[str, Any], include_steps: bool = False) -> dict[str, Any]:
    out = dict(row)
    steps = out.pop("steps_json", None)
    if include_steps and steps:
        out["steps"] = json.loads(steps)
    out["report_url"] = f"/releases/{row['id']}/report.json"
    out["classes_url"] = f"/releases/{row['id']}/classes"
    return out


def _class_id_for(key_sha: str, raw_shas: list[str], group: int | None = None) -> str:
    payload = key_sha + "|" + "|".join(sorted(raw_shas))
    if group is not None:
        payload += f"|g{group}"
    return "c-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _strip_invisible(text: str) -> str:
    """Drop zero-width / control characters, keeping real spaces for later stages."""
    return "".join(ch for ch in text if unicodedata.category(ch) not in {"Cf", "Cc"})


def _fold_case_width(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _strip_ws(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _strip_punct_all(text: str) -> str:
    return "".join(ch for ch in text if not unicodedata.category(ch).startswith(("P", "S")))


# Ordered from least to most consequential.  The stages are *cumulative*: stage
# n applies the transforms of every earlier stage plus one more equivalence, so
# a pair is graded by the mildest explanation that accounts for it.  An operator
# reading "punctuation" knows the strings differ only in punctuation;
# "characters" means nothing short of a rule change explains the merge, which
# deserves a look.
DIFFERENCE_TRANSFORMS = [
    ("invisible", _strip_invisible),          # zero-width / control characters
    ("case_or_width", _fold_case_width),      # NFKC width folding + casefold
    ("whitespace", _strip_ws),                # any remaining spaces
    ("diacritic", _strip_accents),            # accents / combining marks
    ("punctuation", _strip_punct_all),        # punctuation and symbols
]


def _stage_chain(text: str) -> list[str]:
    """Return [raw, after stage 1, after stage 2, …] for comparing two strings."""
    chain = [text]
    for _name, fn in DIFFERENCE_TRANSFORMS:
        chain.append(fn(chain[-1]))
    return chain


def difference_profile(raw_values: list[str], key: str | None = None) -> dict[str, Any]:
    """Explain *why* different raw strings landed on one key.

    Each distinct spelling is compared with the normalized key, not with each
    other: the key is the canonical result, and comparing against it avoids
    arbitrary "base" ordering.  Both sides are pushed through the cumulative
    stage chain and the first level where they agree names the difference.  The
    class severity is the worst spelling's stage; ``worst_member`` names it so
    the UI can point at the exact row that needs review.

    Stages are intentionally a *triage aid only*: they never change the class.
    A rule rewrite such as ``replace_map`` that merges genuinely different
    spellings still grades as ``substantive``, which is the point.
    """
    distinct = sorted(set(raw_values))
    if key == "":
        return {"severity": "empty_key", "types": ["blank"], "pairs": []}
    if len(distinct) < 2:
        return {"severity": "none", "types": [], "pairs": []}
    target = key if key is not None else distinct[0]
    target_chain = _stage_chain(target)
    types: list[str] = []
    pairs: list[dict[str, str]] = []
    worst = 0
    worst_raw = ""
    for raw in distinct:
        raw_chain = _stage_chain(raw)
        stage_name = None
        for level in range(1, len(target_chain)):
            if raw_chain[level] == target_chain[level]:
                stage_name = DIFFERENCE_TRANSFORMS[level - 1][0]
                if level > worst:
                    worst, worst_raw = level, raw
                break
        if stage_name is None:
            stage_name = "characters"
            worst, worst_raw = len(target_chain), raw
        if stage_name not in types:
            types.append(stage_name)
        pairs.append({"a": target, "b": raw, "explained_by": stage_name})
    severity = "substantive" if worst >= len(target_chain) else "variant"
    return {
        "severity": severity,
        "types": types,
        "pairs": pairs,
        "worst_member": worst_raw if severity == "substantive" else None,
    }


class LinkStore:
    def __init__(self, data_dir: str | Path, raw_base: str, normalizer_base: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.raw_base = raw_base.rstrip("/")
        self.normalizer_base = normalizer_base.rstrip("/")
        self.db = DB(self.data_dir / "linkage.db")
        self.db.executescript(SCHEMA)
        self._job_threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------- generations
    def sync_current_generation(self) -> dict[str, Any]:
        try:
            gen = http_request(f"{self.normalizer_base}/rules/current")
        except RemoteError as exc:
            raise HTTPError(502, f"无法从规范化服务读取当前规则世代: {exc.message}")
        self.mirror_generation(gen)
        return gen

    def mirror_generation(self, gen: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO generations(generation_id,label,steps_hash,steps_json,published_at,mirrored_at)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(generation_id) DO UPDATE SET"
            " label=excluded.label, steps_hash=excluded.steps_hash, steps_json=excluded.steps_json,"
            " published_at=excluded.published_at, mirrored_at=excluded.mirrored_at",
            (gen["generation_id"], gen["label"], gen["steps_hash"],
             json.dumps(gen["steps"], ensure_ascii=False, sort_keys=True),
             gen.get("published_at"), now_iso()),
        )

    def require_current_generation(self) -> dict[str, Any]:
        gen = self.sync_current_generation()
        return gen

    # --------------------------------------------------------------- remote
    def _raw_page(self, since_id: int, limit: int = 5000) -> dict[str, Any]:
        try:
            return http_request(f"{self.raw_base}/records?active=1&since_id={since_id}&limit={limit}")
        except RemoteError as exc:
            raise HTTPError(502, f"读取原始值服务失败: {exc.message}")

    def fetch_active_records(self, cancel_check=None, progress=None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        cursor = 0
        total = None
        try:
            snap = http_request(f"{self.raw_base}/snapshot")
            total = snap.get("active_count")
        except RemoteError as exc:
            raise HTTPError(502, f"读取原始值快照失败: {exc.message}")
        while True:
            if cancel_check and cancel_check():
                raise BuildCancelled()
            page = self._raw_page(cursor)
            items = page.get("items") or []
            records.extend(items)
            if progress:
                progress(len(records), total or len(records))
            if not page.get("has_more") or not items:
                break
            cursor = page["next_cursor"]
        return records

    @staticmethod
    def fingerprint(records: list[dict[str, Any]]) -> str:
        parts = sorted(f"{r['raw_id']}:{r['raw_sha256']}" for r in records)
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    def run_normalizer(self, generation_id: int, records: list[dict[str, Any]], claimed_fingerprint: str | None) -> dict[str, Any]:
        rows = [
            {
                "raw_id": r["raw_id"], "raw_value": r["raw_value"], "raw_sha256": r["raw_sha256"],
                "source_system": r["source_system"], "source_id": r["source_id"], "revision": r["revision"],
            }
            for r in records
        ]
        payload = {
            "generation_id": generation_id,
            "rows": rows,
            "raw_snapshot": {
                "fingerprint": claimed_fingerprint,
                "count": len(records),
                "max_id": max((r["raw_id"] for r in records), default=0),
            },
        }
        try:
            return http_request(f"{self.normalizer_base}/runs", "POST", payload, timeout=600)
        except RemoteError as exc:
            raise HTTPError(502, f"规范化服务运行失败: {exc.message}", exc.detail)

    def fetch_run_items(self, run_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            try:
                data = http_request(
                    f"{self.normalizer_base}/runs/{run_id}/items?page={page}&page_size=5000", timeout=600
                )
            except RemoteError as exc:
                raise HTTPError(502, f"读取规范化结果失败: {exc.message}")
            items.extend(data["items"])
            if page >= data["pages"]:
                break
            page += 1
        return items

    # ----------------------------------------------------------------- jobs
    def _cancel_checker(self, job_id: str):
        def check() -> bool:
            v = self.db.scalar("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,), default=0)
            return bool(v)
        return check

    def _job_update(self, job_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE jobs SET {cols} WHERE id=?", [*fields.values(), job_id])

    def start_build(self, mode: str, publish: bool, generation_id: int | None, note: str | None,
                    force: bool = False) -> dict[str, Any]:
        if mode not in {"full", "incremental"}:
            raise HTTPError(400, "mode 只能是 full 或 incremental")
        gen = self.require_current_generation()
        if generation_id is not None and generation_id != gen["generation_id"]:
            pinned = self.db.q1("SELECT * FROM generations WHERE generation_id=?", (generation_id,))
            if not pinned:
                try:
                    pinned = http_request(f"{self.normalizer_base}/rules/generations/{generation_id}")
                except RemoteError:
                    raise HTTPError(404, "指定的规则世代不存在")
                self.mirror_generation(pinned)
            if pinned["steps_hash"] != gen["steps_hash"]:
                raise HTTPError(409, "指定世代不是当前已发布世代；为避免发布混合世代，请先发布该规则或改用当前世代")
            gen = pinned
        active = self.active_release()
        if mode == "incremental":
            if not active:
                raise HTTPError(409, "没有可增量的已发布关联结果，请先做一次全量构建")
            if active["generation_hash"] != gen["steps_hash"]:
                raise HTTPError(409, "规则世代已变化，增量维护不再安全；请执行全量重建")
        self.mirror_generation(gen)
        job_id = "j-" + hashlib.sha256(f"{time.time_ns()}".encode()).hexdigest()[:16]
        now = now_iso()
        self.db.execute(
            "INSERT INTO jobs(id,mode,status,publish,generation_id,generation_hash,generation_label,"
            "parent_release,stage,created_at,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, mode, "queued", 1 if publish else 0, gen["generation_id"], gen["steps_hash"],
             gen["label"], active["id"] if active else None, "queued", now, note),
        )
        job = self.get_job(job_id)
        thread = threading.Thread(target=self._run_build, args=(job_id, mode, publish, gen, active, force),
                                  name=f"build-{job_id}", daemon=True)
        with self._lock:
            self._job_threads[job_id] = thread
        thread.start()
        return job

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job["status"] in {"completed", "cancelled", "failed", "stale"}:
            return job
        self._job_update(job_id, cancel_requested=1, stage="cancelling")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.get_job(job_id)
            if job["status"] in {"completed", "cancelled", "failed", "stale"}:
                return job
            time.sleep(0.1)
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self.db.q1("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not row:
            raise HTTPError(404, "构建任务不存在")
        row["publish"] = bool(row["publish"])
        row["cancel_requested"] = bool(row["cancel_requested"])
        if row.get("impact_json"):
            row["impact"] = json.loads(row["impact_json"])
            row.pop("impact_json", None)
        return row

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.q("SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self.get_job(r["id"]) for r in rows]

    def _run_build(self, job_id: str, mode: str, publish: bool, gen: dict[str, Any],
                   parent: dict[str, Any] | None, force: bool) -> None:
        started = time.monotonic()
        release_id = "rel-" + hashlib.sha256(f"{job_id}-{time.time_ns()}".encode()).hexdigest()[:16]
        try:
            self._job_update(job_id, status="running", stage="reading_raw", started_at=now_iso())
            cancel = self._cancel_checker(job_id)

            def progress(cur: int, total: int):
                self._job_update(job_id, progress_current=cur, progress_total=total)

            all_records = self.fetch_active_records(cancel_check=cancel, progress=progress)
            full_fingerprint = self.fingerprint(all_records)
            if cancel():
                raise BuildCancelled()

            if mode == "incremental":
                parent_members = {
                    r["raw_id"]: r for r in self.db.q(
                        "SELECT raw_id, raw_sha256, key_sha256, class_id FROM members WHERE release_id=?", (parent["id"],)
                    )
                }
                affected = [r for r in all_records if parent_members.get(r["raw_id"], {}).get("raw_sha256") != r["raw_sha256"]]
                stale = [rid for rid in parent_members if rid not in {r["raw_id"] for r in all_records}]
                if stale:
                    raise HTTPError(409, "原始服务中出现了被撤回的活动记录，增量无法处理；请全量重建")
                # Adjudications recorded/revoked since the parent release must also force
                # their keys' members through the build so the change can be applied.
                forced_keys = self._keys_requiring_rebuild(parent["id"])
                reopened_raws: list[dict[str, Any]] = []
                for key_sha in forced_keys:
                    for m in self.db.q(
                        "SELECT raw_id FROM members WHERE release_id=? AND key_sha256=?",
                        (parent["id"], key_sha),
                    ):
                        rec = next((r for r in all_records if r["raw_id"] == m["raw_id"]), None)
                        if rec is not None:
                            reopened_raws.append(rec)
                affected_ids = {r["raw_id"] for r in affected} | {r["raw_id"] for r in reopened_raws}
                affected = [r for r in all_records if r["raw_id"] in affected_ids]
                if not affected:
                    self._job_update(job_id, status="completed", stage="no_changes", finished_at=now_iso(),
                                     duration_ms=int((time.monotonic() - started) * 1000),
                                     raw_item_count=len(all_records), affected_item_count=0,
                                     impact_json=json_dumps({"no_changes": True, "active_release": parent["id"]}))
                    return
                subset = affected
                claimed = hashlib.sha256(
                    "|".join(sorted(f"{r['raw_id']}:{r['raw_sha256']}" for r in subset)).encode("utf-8")
                ).hexdigest()
            else:
                subset = all_records
                claimed = full_fingerprint
            self._job_update(job_id, stage="normalizing", raw_item_count=len(all_records),
                             affected_item_count=len(subset))
            run = self.run_normalizer(gen["generation_id"], subset, claimed)
            run_id = run["run_id"]
            if run["generation"]["steps_hash"] != gen["steps_hash"]:
                raise HTTPError(409, "规范化运行使用的世代与请求不一致")
            if cancel():
                raise BuildCancelled()
            self._job_update(job_id, stage="fetching_results", progress_current=0, progress_total=len(subset))
            items = self.fetch_run_items(run_id)
            if len(items) != len(subset):
                raise HTTPError(502, "规范化运行返回的条目数与会话不一致")
            if cancel():
                raise BuildCancelled()
            self._job_update(job_id, stage="building_classes")
            affected_hashes = {it["key_sha256"] for it in items}
            if mode == "incremental":
                parent_members = {m["raw_id"]: m for m in self.db.q("SELECT * FROM members WHERE release_id=?", (parent["id"],))}
                # Keep every unchanged member (same rules => same key), including unchanged
                # siblings of an affected key; only the re-normalized rows are replaced.
                changed_raw_ids = {it["raw_id"] for it in items}
                combined_items = items + [
                    {
                        "raw_id": m["raw_id"], "raw_sha256": m["raw_sha256"], "raw_value": m["raw_value"],
                        "source_system": m["source_system"], "source_id": m["source_id"], "revision": m["revision"],
                        "key": m["normalized_key"], "key_sha256": m["key_sha256"], "status": "ok",
                    }
                    for m in parent_members.values() if m["raw_id"] not in changed_raw_ids
                ]
            else:
                combined_items = items
            classes, members, applied, needs_review = self._materialize(combined_items, gen)
            if cancel():
                raise BuildCancelled()
            self._job_update(job_id, stage="checking_generation")
            current = http_request(f"{self.normalizer_base}/rules/current")
            if current["steps_hash"] != gen["steps_hash"] and not force:
                raise HTTPError(409, "构建期间已发布了新的规则世代，已放弃发布以避免混合世代；请重新构建")
            if cancel():
                raise BuildCancelled()
            self._job_update(job_id, stage="publishing", progress_current=0, progress_total=len(members))
            counts = self._publish_release(
                release_id, mode, gen, run_id, parent["id"] if parent else None,
                classes, list(members.values()), applied, needs_review,
                publish=publish, job_id=job_id, cancel=cancel, note=self.get_job(job_id).get("note"),
            )
            impact = {
                "class_count": counts["class_count"],
                "conflict_class_count": counts["conflict_class_count"],
                "member_conflict_count": counts["member_conflict_count"],
                "adjudication_applied": counts["adjudication_applied"],
                "adjudication_needs_review": counts["adjudication_review"],
                "affected_keys": len(affected_hashes),
                "published": publish,
            }
            if parent:
                diff = self.diff_releases(parent["id"], release_id, examples=2)
                impact["diff"] = {
                    "merged_classes": len(diff["merges"]),
                    "split_classes": len(diff["splits"]),
                    "added_classes": len(diff["added"]),
                    "removed_classes": len(diff["removed"]),
                    "moved_members": diff["moved_member_count"],
                }
            status = "completed"
            self._job_update(
                job_id, status=status, stage="done", release_id=release_id, finished_at=now_iso(),
                duration_ms=int((time.monotonic() - started) * 1000), impact_json=json_dumps(impact),
                progress_current=len(members), progress_total=len(members),
            )
        except BuildCancelled:
            self._cleanup_release(release_id)
            self._job_update(job_id, status="cancelled", stage="cancelled", finished_at=now_iso(),
                             duration_ms=int((time.monotonic() - started) * 1000))
        except (HTTPError, RemoteError) as exc:
            self._cleanup_release(release_id)
            detail = exc.detail if hasattr(exc, "detail") else None
            self._job_update(job_id, status="failed", stage="failed", error=f"{exc.message}" + (f" ({detail})" if detail else ""),
                             finished_at=now_iso(), duration_ms=int((time.monotonic() - started) * 1000))
        except Exception as exc:  # pragma: no cover - defensive
            self._cleanup_release(release_id)
            self._job_update(job_id, status="failed", stage="failed", error=f"{type(exc).__name__}: {exc}",
                             finished_at=now_iso(), duration_ms=int((time.monotonic() - started) * 1000),
                             impact_json=json_dumps({"traceback": traceback.format_exc()[-4000:]}))
        finally:
            with self._lock:
                self._job_threads.pop(job_id, None)

    # ----------------------------------------------------------- materialize
    def _keys_requiring_rebuild(self, parent_release_id: str) -> set[str]:
        """Keys whose class membership could change without any raw-value change.

        Covers adjudications recorded (or revoked) since the parent release, and
        adjudications whose raw membership no longer matches the parent class.
        """
        keys: set[str] = set()
        for adj in self.db.q(
            "SELECT * FROM adjudications WHERE status='active'"
            " AND (last_applied_release IS NULL OR last_applied_release<>?)",
            (parent_release_id,),
        ):
            keys.add(adj["key_sha256"])
        applied = self.db.q(
            "SELECT DISTINCT key_sha256, adjudication_id FROM classes"
            " WHERE release_id=? AND adjudication_id IS NOT NULL", (parent_release_id,),
        )
        active_ids = {
            r["id"] for r in self.db.q("SELECT id FROM adjudications WHERE status='active'")
        }
        for row in applied:
            if row["adjudication_id"] not in active_ids:
                keys.add(row["key_sha256"])
        for adj in self.db.q("SELECT * FROM adjudications WHERE status='active'"):
            present = {
                r["raw_sha256"] for r in self.db.q(
                    "SELECT DISTINCT raw_sha256 FROM members WHERE release_id=? AND key_sha256=?",
                    (parent_release_id, adj["key_sha256"]),
                )
            }
            if present and present != set(json.loads(adj["raw_shas_json"])):
                keys.add(adj["key_sha256"])
        return keys

    def _materialize(self, items: list[dict[str, Any]], gen: dict[str, Any]):
        """Group items into equivalence classes, applying matching adjudications."""
        by_key: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            by_key.setdefault(item["key_sha256"], []).append(item)
        adjudications = self.db.q("SELECT * FROM adjudications WHERE status='active' ORDER BY id DESC")
        adj_by_key: dict[str, list[dict[str, Any]]] = {}
        for adj in adjudications:
            adj_by_key.setdefault(adj["key_sha256"], []).append(adj)
        classes: list[dict[str, Any]] = []
        members: dict[int, dict[str, Any]] = {}
        applied = 0
        needs_review = 0
        for key_sha, group in by_key.items():
            distinct = sorted({it["raw_sha256"] for it in group})
            by_raw: dict[str, list[dict[str, Any]]] = {}
            for it in group:
                by_raw.setdefault(it["raw_sha256"], []).append(it)
            chosen = None
            for adj in adj_by_key.get(key_sha, []):
                if self._adj_matches(adj, distinct):
                    chosen = adj
                    break
            if chosen is not None:
                applied += 1
                action = chosen["action"]
                groups = json.loads(chosen["groups_json"])
                if action == "accept_merge":
                    self._emit_class(classes, members, group, sorted(distinct), "adjudicated_merge",
                                     chosen["id"], None, len(distinct) > 1)
                elif action == "split":
                    groups = [sorted(g) for g in groups]
                    for idx, part in enumerate(groups):
                        part_members = [it for raw in part for it in by_raw.get(raw, [])]
                        if part_members:
                            self._emit_class(classes, members, part_members, part, "adjudicated_split", chosen["id"], idx,
                                             len(part) > 1)
                elif action == "hold":
                    self._emit_class(classes, members, group, distinct, "hold", chosen["id"], None, len(distinct) > 1)
                else:
                    needs_review += 1
                    self._emit_class(classes, members, group, distinct, "conflict_needs_review", None, None, True)
            else:
                if adj_by_key.get(key_sha):
                    needs_review += 1
                    self._emit_class(classes, members, group, distinct, "conflict_needs_review", None, None, True)
                elif len(distinct) > 1:
                    self._emit_class(classes, members, group, distinct, "conflict", None, None, True)
                else:
                    is_empty_key = group[0]["key"] == ""
                    status = "empty" if is_empty_key else "clean"
                    self._emit_class(classes, members, group, distinct, status, None, None, False)
        return classes, members, applied, needs_review

    @staticmethod
    def _adj_matches(adj: dict[str, Any], distinct: list[str]) -> bool:
        raw_shas = json.loads(adj["raw_shas_json"])
        return sorted(raw_shas) == distinct

    def _emit_class(self, classes, members, group, raw_shas, status, adjudication_id, split_group, conflict):
        key = group[0]["key"]
        key_sha = group[0]["key_sha256"]
        class_id = _class_id_for(key_sha, raw_shas, split_group)
        source_count = len({it["source_system"] for it in group})
        members_in = [it for it in group if it["raw_sha256"] in set(raw_shas)]
        classes.append({
            "class_id": class_id,
            "normalized_key": key,
            "key_sha256": key_sha,
            "status": status,
            "member_count": len(members_in),
            "distinct_raw_count": len(set(raw_shas)),
            "source_count": source_count,
            "conflict_member_count": len(members_in) if conflict else 0,
            "split_group": split_group,
            "adjudication_id": adjudication_id,
        })
        is_empty = key == ""
        for it in members_in:
            members[it["raw_id"]] = {
                "class_id": class_id,
                "raw_id": it["raw_id"],
                "raw_sha256": it["raw_sha256"],
                "raw_value": it["raw_value"],
                "source_system": it["source_system"],
                "source_id": it["source_id"],
                "revision": it["revision"],
                "normalized_key": key,
                "key_sha256": key_sha,
                "conflict": 1 if conflict else 0,
                "empty_key": 1 if is_empty else 0,
            }

    # -------------------------------------------------------------- publish
    def _cleanup_release(self, release_id: str) -> None:
        try:
            with self.db.tx() as conn:
                conn.execute("DELETE FROM members WHERE release_id=?", (release_id,))
                conn.execute("DELETE FROM classes WHERE release_id=?", (release_id,))
                conn.execute("DELETE FROM releases WHERE id=? AND status!='active'", (release_id,))
        except Exception:
            pass

    def _publish_release(self, release_id: str, mode: str, gen: dict[str, Any], run_id: str,
                         parent_release: str | None, classes: list[dict[str, Any]], members: list[dict[str, Any]],
                         applied: int, needs_review: int, publish: bool, job_id: str,
                         cancel=None, note: str | None = None) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for c in classes:
            by_status[c["status"]] = by_status.get(c["status"], 0) + 1
        conflict_classes = sum(v for k, v in by_status.items() if k.startswith("conflict") or k in {"hold"})
        adjudicated = sum(v for k, v in by_status.items() if k.startswith("adjudicated"))
        clean = by_status.get("clean", 0)
        empty_items = sum(1 for m in members if m["empty_key"])
        member_conflicts = sum(1 for m in members if m["conflict"])
        applied_ids = sorted({c["adjudication_id"] for c in classes if c["adjudication_id"] is not None})
        applied_adjudications: list[dict[str, Any]] = []
        for adj_id in applied_ids:
            adj = self.db.q1("SELECT * FROM adjudications WHERE id=?", (adj_id,))
            if adj is None:
                continue
            applied_adjudications.append({
                "adjudication_id": adj["id"], "normalized_key": adj["normalized_key"],
                "key_sha256": adj["key_sha256"], "action": adj["action"],
                "raw_shas": json.loads(adj["raw_shas_json"]), "groups": json.loads(adj["groups_json"]),
                "note": adj["note"], "operator": adj["operator"], "created_at": adj["created_at"],
                "revoked_later": adj["status"] != "active",
            })
        # Carry forward adjudications inherited from the parent release that are no
        # longer in force, so a reader of this release can see they were withdrawn
        # rather than silently dropped.
        withdrawn_adjudications: list[dict[str, Any]] = []
        if parent_release:
            for row in self.db.q("SELECT * FROM release_adjudications WHERE release_id=?", (parent_release,)):
                if row["adjudication_id"] in applied_ids:
                    continue
                status = self.db.scalar("SELECT status FROM adjudications WHERE id=?", (row["adjudication_id"],))
                if status != "active":
                    withdrawn_adjudications.append({
                        "adjudication_id": row["adjudication_id"],
                        "normalized_key": row["normalized_key"],
                        "key_sha256": row["key_sha256"],
                        "action": row["action"],
                        "raw_shas": json.loads(row["raw_shas_json"]),
                        "groups": json.loads(row["groups_json"]),
                        "note": row["note"],
                        "operator": row["operator"],
                        "created_at": row["created_at"],
                        "revoked_later": True,
                    })
        report_payload = {
            "release_id": release_id,
            "generation_id": gen["generation_id"],
            "generation_label": gen["label"],
            "generation_hash": gen["steps_hash"],
            "normalizer_run_id": run_id,
            "parent_release": parent_release,
            "classes": sorted(classes, key=lambda c: c["class_id"]),
            "members": sorted(members, key=lambda m: m["raw_id"]),
            "adjudications_applied": sorted(applied_adjudications + withdrawn_adjudications,
                                            key=lambda a: a["adjudication_id"]),
        }
        report_sha = hashlib.sha256(json.dumps(report_payload, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")).encode("utf-8")).hexdigest()
        created = now_iso()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO releases(id,kind,status,generation_id,generation_hash,generation_label,steps_json,"
                "normalizer_run_id,parent_release,created_at,item_count,class_count,conflict_class_count,"
                "clean_class_count,adjudicated_class_count,empty_item_count,member_conflict_count,"
                "adjudication_applied,adjudication_review,report_sha256,note)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (release_id, mode, "staging", gen["generation_id"], gen["steps_hash"], gen["label"],
                 json.dumps(gen["steps"], ensure_ascii=False, sort_keys=True), run_id, parent_release, created,
                 len(members), len(classes), conflict_classes, clean, adjudicated, empty_items,
                 member_conflicts, applied, needs_review, report_sha, note),
            )
            conn.executemany(
                "INSERT INTO classes(release_id,class_id,normalized_key,key_sha256,status,member_count,"
                "distinct_raw_count,source_count,conflict_member_count,split_group,adjudication_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(release_id, c["class_id"], c["normalized_key"], c["key_sha256"], c["status"], c["member_count"],
                  c["distinct_raw_count"], c["source_count"], c["conflict_member_count"], c["split_group"],
                  c["adjudication_id"]) for c in classes],
            )
            conn.executemany(
                "INSERT INTO members(release_id,class_id,raw_id,raw_sha256,raw_value,source_system,source_id,"
                "revision,normalized_key,key_sha256,conflict,empty_key)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(release_id, m["class_id"], m["raw_id"], m["raw_sha256"], m["raw_value"], m["source_system"],
                  m["source_id"], m["revision"], m["normalized_key"], m["key_sha256"], m["conflict"], m["empty_key"])
                 for m in members],
            )
            applied_ids = sorted({c["adjudication_id"] for c in classes if c["adjudication_id"] is not None})
            for adj_id in applied_ids:
                adj = conn.execute("SELECT * FROM adjudications WHERE id=?", (adj_id,)).fetchone()
                if adj is None:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO release_adjudications(release_id,adjudication_id,normalized_key,"
                    "key_sha256,action,raw_shas_json,groups_json,note,operator,created_at,revoked_later)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (release_id, adj["id"], adj["normalized_key"], adj["key_sha256"], adj["action"],
                     adj["raw_shas_json"], adj["groups_json"], adj["note"], adj["operator"], adj["created_at"],
                     0 if adj["status"] == "active" else 1),
                )
            for adj in withdrawn_adjudications:
                conn.execute(
                    "INSERT OR REPLACE INTO release_adjudications(release_id,adjudication_id,normalized_key,"
                    "key_sha256,action,raw_shas_json,groups_json,note,operator,created_at,revoked_later)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                    (release_id, adj["adjudication_id"], adj["normalized_key"], adj["key_sha256"],
                     adj["action"], json.dumps(adj["raw_shas"], ensure_ascii=False),
                     json.dumps(adj["groups"], ensure_ascii=False), adj["note"], adj["operator"],
                     adj["created_at"]),
                )
            if publish:
                if cancel and cancel():
                    raise BuildCancelled()
                conn.execute("UPDATE releases SET status='superseded' WHERE status='active'")
                conn.execute("UPDATE releases SET status='active', published_at=? WHERE id=?", (now_iso(), release_id))
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES('active_release',?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(release_id),),
                )
            else:
                conn.execute("UPDATE releases SET status='preview' WHERE id=?", (release_id,))
        if applied:
            self.db.execute(
                "UPDATE adjudications SET last_applied_release=?, last_applied_at=?"
                " WHERE status='active' AND id IN (SELECT DISTINCT adjudication_id FROM classes WHERE release_id=? AND adjudication_id IS NOT NULL)",
                (release_id, now_iso(), release_id),
            )
        return {
            "class_count": len(classes),
            "conflict_class_count": conflict_classes,
            "member_conflict_count": member_conflicts,
            "adjudication_applied": applied,
            "adjudication_review": needs_review,
            "report_sha256": report_sha,
        }

    # ---------------------------------------------------------------- read
    def active_release(self) -> dict[str, Any] | None:
        rid = self.db.meta_get("active_release")
        if not rid:
            row = self.db.q1("SELECT id FROM releases WHERE status='active' ORDER BY published_at DESC LIMIT 1")
            rid = row["id"] if row else None
        if not rid:
            return None
        row = self.db.q1("SELECT * FROM releases WHERE id=?", (rid,))
        return _release_row(row) if row else None

    def require_active(self) -> dict[str, Any]:
        rel = self.active_release()
        if not rel:
            raise HTTPError(404, "尚无已发布的关联结果，请先构建")
        return rel

    def release(self, release_id: str) -> dict[str, Any]:
        row = self.db.q1("SELECT * FROM releases WHERE id=?", (release_id,))
        if not row:
            raise HTTPError(404, "关联发布不存在")
        return _release_row(row, include_steps=True)

    def list_releases(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.db.q("SELECT * FROM releases WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            rows = self.db.q("SELECT * FROM releases ORDER BY created_at DESC LIMIT ?", (limit,))
        return [_release_row(r) for r in rows]

    def activate_release(self, release_id: str, expected_generation_hash: str | None = None) -> dict[str, Any]:
        rel = self.db.q1("SELECT * FROM releases WHERE id=?", (release_id,))
        if not rel:
            raise HTTPError(404, "关联发布不存在")
        if not expected_generation_hash:
            raise HTTPError(400, "激活前必须提交 expected_generation_hash 以确认审阅的是同一世代")
        if expected_generation_hash != rel["generation_hash"]:
            raise HTTPError(409, "世代哈希不匹配，拒绝激活")
        current = self.sync_current_generation()
        if current["steps_hash"] != rel["generation_hash"]:
            raise HTTPError(409, "该发布使用的规则世代已不是当前发布世代；为避免混合世代，请用当前规则重新构建")
        if rel["status"] == "active":
            return self.release(release_id)
        with self.db.tx() as conn:
            conn.execute("UPDATE releases SET status='superseded' WHERE status='active'")
            conn.execute("UPDATE releases SET status='active', published_at=? WHERE id=?",
                         (now_iso(), release_id))
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('active_release',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(release_id),),
            )
        return self.release(release_id)

    def classes(self, release_id: str, status: str | None, q: str | None, page: int, page_size: int) -> dict[str, Any]:
        self.release(release_id)
        clauses = ["release_id=?"]
        args: list[Any] = [release_id]
        if status:
            if status == "conflict":
                clauses.append("(status LIKE 'conflict%' OR status='hold')")
            elif status == "adjudicated":
                clauses.append("status LIKE 'adjudicated%'")
            else:
                clauses.append("status=?")
                args.append(status)
        if q:
            clauses.append("(normalized_key LIKE ? OR class_id LIKE ?)")
            args.extend([f"%{q}%", f"%{q}%"])
        where = " AND ".join(clauses)
        total = self.db.scalar(f"SELECT COUNT(*) FROM classes WHERE {where}", args, default=0)
        rows = self.db.q(
            f"SELECT * FROM classes WHERE {where} ORDER BY status='conflict' DESC, member_count DESC, normalized_key"
            f" LIMIT ? OFFSET ?", [*args, page_size, (page - 1) * page_size],
        )
        for row in rows:
            row["key_escaped"] = row["normalized_key"].encode("unicode_escape").decode("ascii")
        return {"release_id": release_id, "items": rows, "page": page, "page_size": page_size,
                "total": total, "pages": max(1, (total + page_size - 1) // page_size)}

    def class_detail(self, release_id: str, class_id: str) -> dict[str, Any]:
        self.release(release_id)
        cls = self.db.q1("SELECT * FROM classes WHERE release_id=? AND class_id=?", (release_id, class_id))
        if not cls:
            raise HTTPError(404, "等价类不存在")
        cls["key_escaped"] = cls["normalized_key"].encode("unicode_escape").decode("ascii")
        members = self.db.q(
            "SELECT * FROM members WHERE release_id=? AND class_id=? ORDER BY source_system, source_id, raw_id",
            (release_id, class_id),
        )
        for m in members:
            m["raw_escaped"] = m["raw_value"].encode("unicode_escape").decode("ascii")
            m["key_escaped"] = m["normalized_key"].encode("unicode_escape").decode("ascii")
        return {"release_id": release_id, "class": cls, "members": members,
                "adjudication": self.db.q1("SELECT * FROM adjudications WHERE id=?", (cls["adjudication_id"],))
                if cls["adjudication_id"] else None}

    def conflicts(self, release_id: str, page: int, page_size: int, include_hold: bool = True) -> dict[str, Any]:
        self.release(release_id)
        cond = "(status LIKE 'conflict%' OR status='hold')" if include_hold else "status LIKE 'conflict%'"
        total = self.db.scalar(f"SELECT COUNT(*) FROM classes WHERE release_id=? AND {cond}", (release_id,), default=0)
        rows = self.db.q(
            f"SELECT * FROM classes WHERE release_id=? AND {cond} ORDER BY distinct_raw_count DESC, normalized_key"
            f" LIMIT ? OFFSET ?", (release_id, page_size, (page - 1) * page_size),
        )
        # Severity of every conflict class in this release, so the header counts are
        # about the whole release and not just the visible page.
        class_cond = "c.status LIKE 'conflict%' OR c.status='hold'" if include_hold else "c.status LIKE 'conflict%'"
        raw_by_class: dict[str, list[str]] = {}
        key_by_class: dict[str, str] = {}
        for rec in self.db.q(
            "SELECT m.class_id, m.raw_value, m.normalized_key FROM members m JOIN classes c"
            " ON c.release_id=m.release_id AND c.class_id=m.class_id"
            " WHERE m.release_id=? AND (" + class_cond + ")", (release_id,),
        ):
            raw_by_class.setdefault(rec["class_id"], []).append(rec["raw_value"])
            key_by_class[rec["class_id"]] = rec["normalized_key"]
        severity_by_class = {
            cid: difference_profile(vals, key_by_class.get(cid))["severity"]
            for cid, vals in raw_by_class.items()
        }
        for cls in rows:
            cls["key_escaped"] = cls["normalized_key"].encode("unicode_escape").decode("ascii")
            members = self.db.q(
                "SELECT raw_id,source_system,source_id,revision,raw_value,raw_sha256,conflict FROM members"
                " WHERE release_id=? AND class_id=? ORDER BY source_system, source_id, raw_id",
                (release_id, cls["class_id"]),
            )
            for m in members:
                m["raw_escaped"] = m["raw_value"].encode("unicode_escape").decode("ascii")
                m["raw_codepoints"] = [
                    {"char": ch, "codepoint": f"U+{ord(ch):04X}", "name": unicodedata.name(ch, "UNKNOWN")}
                    for ch in m["raw_value"] if ord(ch) >= 128 or not ch.isprintable()
                ]
            cls["members"] = members
            profile = difference_profile([m["raw_value"] for m in members], cls["normalized_key"])
            cls["difference"] = profile
            cls["severity"] = profile["severity"]
        summary = {
            "substantive": sum(1 for v in severity_by_class.values() if v == "substantive"),
            "variant": sum(1 for v in severity_by_class.values() if v == "variant"),
            "empty_key": sum(1 for v in severity_by_class.values() if v == "empty_key"),
            "total": total,
        }
        return {"release_id": release_id, "items": rows, "page": page, "page_size": page_size,
                "total": total, "pages": max(1, (total + page_size - 1) // page_size), "summary": summary}

    def member(self, release_id: str, raw_id: int) -> dict[str, Any]:
        rel = self.release(release_id)
        m = self.db.q1("SELECT * FROM members WHERE release_id=? AND raw_id=?", (release_id, raw_id))
        if not m:
            raise HTTPError(404, "该发布中没有此原始记录")
        cls = self.db.q1("SELECT * FROM classes WHERE release_id=? AND class_id=?", (release_id, m["class_id"]))
        m["raw_escaped"] = m["raw_value"].encode("unicode_escape").decode("ascii")
        try:
            evidence = http_request(f"{self.normalizer_base}/runs/{rel['normalizer_run_id']}/items/{raw_id}")
        except RemoteError as exc:
            evidence = {"error": exc.message}
        return {"release": {k: rel[k] for k in ("id", "generation_id", "generation_hash", "generation_label", "normalizer_run_id")},
                "member": m, "class": cls, "evidence": evidence}

    # ---------------------------------------------------------------- diff
    def diff_releases(self, base_id: str, candidate_id: str, examples: int = 3) -> dict[str, Any]:
        base = self.release(base_id)
        cand = self.release(candidate_id)
        bm = {r["raw_id"]: r for r in self.db.q(
            "SELECT raw_id,class_id,normalized_key,key_sha256,source_system,raw_value FROM members WHERE release_id=?",
            (base_id,))}
        cm = {r["raw_id"]: r for r in self.db.q(
            "SELECT raw_id,class_id,normalized_key,key_sha256,source_system,raw_value FROM members WHERE release_id=?",
            (candidate_id,))}
        common = sorted(set(bm) & set(cm))
        old_to_new: dict[str, set[str]] = {}
        new_to_old: dict[str, set[str]] = {}
        sample_by_old: dict[str, list[dict[str, Any]]] = {}
        for rid in common:
            old, new = bm[rid]["class_id"], cm[rid]["class_id"]
            old_to_new.setdefault(old, set()).add(new)
            new_to_old.setdefault(new, set()).add(old)
            bucket = sample_by_old.setdefault(old, [])
            if len(bucket) < examples:
                bucket.append({"raw_id": rid, "raw_value": bm[rid]["raw_value"],
                               "source_system": bm[rid]["source_system"],
                               "old_key": bm[rid]["normalized_key"], "new_key": cm[rid]["normalized_key"]})
        base_classes = {c["class_id"]: c for c in self.db.q("SELECT * FROM classes WHERE release_id=?", (base_id,))}
        cand_classes = {c["class_id"]: c for c in self.db.q("SELECT * FROM classes WHERE release_id=?", (candidate_id,))}
        merges = [
            {"new_class": cand_classes[new], "old_class_ids": sorted(olds),
             "old_key": cand_classes[new]["normalized_key"], "sample": [e for o in olds for e in sample_by_old.get(o, [])][:examples * 2]}
            for new, olds in new_to_old.items() if len(olds) > 1
        ]
        splits = [
            {"old_class": base_classes[old], "new_class_ids": sorted(news),
             "sample": sample_by_old.get(old, [])}
            for old, news in old_to_new.items() if len(news) > 1
        ]
        added = [cand_classes[c] for c in cand_classes if c not in base_classes]
        removed = [base_classes[c] for c in base_classes if c not in cand_classes]
        moved = [rid for rid in common if bm[rid]["class_id"] != cm[rid]["class_id"]]
        return {
            "base": {"release_id": base_id, "generation": {"generation_id": base["generation_id"],
                     "label": base["generation_label"], "steps_hash": base["generation_hash"]}},
            "candidate": {"release_id": candidate_id, "generation": {"generation_id": cand["generation_id"],
                          "label": cand["generation_label"], "steps_hash": cand["generation_hash"]}},
            "common_members": len(common),
            "only_in_base": len(set(bm) - set(cm)),
            "only_in_candidate": len(set(cm) - set(bm)),
            "moved_member_count": len(moved),
            "merges": merges,
            "splits": splits,
            "added": added,
            "removed": removed,
            "impact": {
                "merged_new_classes": len(merges),
                "split_old_classes": len(splits),
                "added_classes": len(added),
                "removed_classes": len(removed),
                "moved_members": len(moved),
                "base_class_count": len(base_classes),
                "candidate_class_count": len(cand_classes),
            },
        }

    # -------------------------------------------------------- adjudications
    def list_adjudications(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if status:
            rows = self.db.q("SELECT * FROM adjudications WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
        else:
            rows = self.db.q("SELECT * FROM adjudications ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["raw_shas"] = json.loads(r.pop("raw_shas_json"))
            r["groups"] = json.loads(r.pop("groups_json"))
            r["key_escaped"] = r["normalized_key"].encode("unicode_escape").decode("ascii")
        return rows

    def create_adjudication(self, body: dict[str, Any]) -> dict[str, Any]:
        release_id = body.get("release_id")
        class_id = body.get("class_id")
        action = body.get("action")
        if action not in {"accept_merge", "split", "hold"}:
            raise HTTPError(400, "action 只能是 accept_merge/split/hold")
        if release_id:
            rel = self.release(release_id)
            cls = self.db.q1("SELECT * FROM classes WHERE release_id=? AND class_id=?", (release_id, class_id))
            if not cls:
                raise HTTPError(404, "等价类不存在")
            key_sha = cls["key_sha256"]
            key = cls["normalized_key"]
            members = self.db.q(
                "SELECT raw_sha256, raw_value, source_system FROM members WHERE release_id=? AND class_id=?",
                (release_id, class_id),
            )
            raw_shas = sorted({m["raw_sha256"] for m in members})
            generation_hash = rel["generation_hash"]
            generation_id = rel["generation_id"]
            if action == "accept_merge":
                groups = [raw_shas]
            elif action == "hold":
                groups = [raw_shas]
            else:
                groups = body.get("groups")
                if not isinstance(groups, list) or len(groups) < 2:
                    raise HTTPError(400, "split 需要至少两个分组")
                groups = [sorted(g) for g in groups]
                flat = sorted(x for g in groups for x in g)
                if flat != raw_shas:
                    raise HTTPError(400, "split 分组必须无遗漏、无重复地覆盖该类的全部不同原值")
                if any(not g for g in groups):
                    raise HTTPError(400, "split 分组不能为空")
        else:
            raise HTTPError(400, "必须指定 release_id 与 class_id")
        # Validate every raw_sha actually occurs in that class.
        known = {m["raw_sha256"] for m in members}
        if not set(raw_shas) <= known:
            raise HTTPError(400, "存在不属于该等价类的原值")
        ids = self.db.q("SELECT * FROM adjudications WHERE status='active' AND key_sha256=?", (key_sha,))
        if ids:
            raise HTTPError(409, "该键已有生效中的人工裁决；请先撤销后再重新裁决")
        note = body.get("note")
        operator = body.get("operator") or "operator"
        now = now_iso()
        cur = self.db.execute(
            "INSERT INTO adjudications(key_sha256,normalized_key,generation_hash,generation_id,action,"
            "raw_shas_json,groups_json,note,operator,created_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (key_sha, key, generation_hash, generation_id, action, json.dumps(raw_shas, ensure_ascii=False),
             json.dumps(groups, ensure_ascii=False), note, operator, now, "active"),
        )
        return self.get_adjudication(cur.lastrowid)

    def get_adjudication(self, adj_id: int) -> dict[str, Any]:
        row = self.db.q1("SELECT * FROM adjudications WHERE id=?", (adj_id,))
        if not row:
            raise HTTPError(404, "裁决不存在")
        row["raw_shas"] = json.loads(row.pop("raw_shas_json"))
        row["groups"] = json.loads(row.pop("groups_json"))
        row["key_escaped"] = row["normalized_key"].encode("unicode_escape").decode("ascii")
        return row

    def revoke_adjudication(self, adj_id: int, note: str | None) -> dict[str, Any]:
        adj = self.get_adjudication(adj_id)
        if adj["status"] != "active":
            return adj
        self.db.execute("UPDATE adjudications SET status='revoked', note=COALESCE(?,note) WHERE id=?", (note, adj_id))
        return self.get_adjudication(adj_id)

    def adjudication_gaps(self, release_id: str) -> list[dict[str, Any]]:
        """Active adjudications whose raw membership no longer matches the release."""
        rel = self.release(release_id)
        out = []
        for adj in self.db.q("SELECT * FROM adjudications WHERE status='active'"):
            raws = json.loads(adj["raw_shas_json"])
            present = {
                r["raw_sha256"] for r in self.db.q(
                    "SELECT raw_sha256 FROM members WHERE release_id=? AND key_sha256=?", (release_id, adj["key_sha256"])
                )
            }
            if set(raws) != present:
                out.append({
                    "adjudication_id": adj["id"], "key_sha256": adj["key_sha256"],
                    "normalized_key": adj["normalized_key"], "action": adj["action"],
                    "reason": "键的原值集合已变化，裁决未应用，需要人工复核",
                    "adjudicated_raw_count": len(raws), "current_raw_count": len(present),
                })
        return out

    # -------------------------------------------------------------- reports
    def report(self, release_id: str) -> dict[str, Any]:
        rel = self.release(release_id)
        classes = self.db.q("SELECT * FROM classes WHERE release_id=? ORDER BY class_id", (release_id,))
        members = self.db.q(
            "SELECT m.*, c.status AS class_status FROM members m JOIN classes c"
            " ON c.release_id=m.release_id AND c.class_id=m.class_id WHERE m.release_id=? ORDER BY m.raw_id",
            (release_id,),
        )
        for m in members:
            m["raw_escaped"] = m["raw_value"].encode("unicode_escape").decode("ascii")
            m["key_escaped"] = m["normalized_key"].encode("unicode_escape").decode("ascii")
        payload = {
            "report_schema": 1,
            "release": rel,
            "generated_at": now_iso(),
            "release_report_sha256": rel["report_sha256"],
            "classes": classes,
            "members": members,
            "adjudications_applied": [
                {**{k: v for k, v in row.items() if k not in {"release_id", "revoked_later"}},
                 "revoked_later": bool(row["revoked_later"])}
                for row in self.db.q(
                    "SELECT * FROM release_adjudications WHERE release_id=? ORDER BY adjudication_id",
                    (release_id,),
                )
            ],
            "adjudication_gaps": self.adjudication_gaps(release_id),
            "conflicts": self.db.q(
                "SELECT class_id, normalized_key, key_sha256, member_count, distinct_raw_count, source_count, status"
                " FROM classes WHERE release_id=? AND (status LIKE 'conflict%' OR status='hold') ORDER BY distinct_raw_count DESC",
                (release_id,),
            ),
        }
        # Fingerprint the report *content*, not the moment it was rendered, so a
        # historical report has one stable hash that can be quoted in an audit.
        stable = {k: v for k, v in payload.items() if k != "generated_at"}
        payload["report_body_sha256"] = hashlib.sha256(
            json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return payload

    def classes_csv(self, release_id: str) -> Response:
        self.release(release_id)
        rows = self.db.q(
            "SELECT c.class_id, c.normalized_key, c.status, c.member_count, c.distinct_raw_count, c.source_count,"
            " c.split_group, c.adjudication_id, GROUP_CONCAT(m.source_system, '|') AS sources"
            " FROM classes c LEFT JOIN members m ON m.release_id=c.release_id AND m.class_id=c.class_id"
            " WHERE c.release_id=? GROUP BY c.class_id ORDER BY c.class_id",
            (release_id,),
        )
        header = ["release_id", "class_id", "normalized_key", "key_escaped", "status", "member_count",
                  "distinct_raw_count", "source_count", "sources", "split_group", "adjudication_id"]
        out = [(release_id, r["class_id"], r["normalized_key"],
                r["normalized_key"].encode("unicode_escape").decode("ascii"), r["status"], r["member_count"],
                r["distinct_raw_count"], r["source_count"], r["sources"], r["split_group"], r["adjudication_id"])
               for r in rows]
        return csv_response(header, out, f"classes_{release_id}.csv")

    def members_csv(self, release_id: str) -> Response:
        self.release(release_id)
        rows = self.db.q(
            "SELECT m.raw_id, m.raw_value, m.raw_sha256, m.source_system, m.source_id, m.revision,"
            " m.normalized_key, m.conflict, m.empty_key, m.class_id, c.status AS class_status"
            " FROM members m JOIN classes c ON c.release_id=m.release_id AND c.class_id=m.class_id"
            " WHERE m.release_id=? ORDER BY m.raw_id", (release_id,),
        )
        header = ["release_id", "class_id", "class_status", "raw_id", "source_system", "source_id", "revision",
                  "raw_value", "raw_escaped", "raw_sha256", "normalized_key", "key_escaped", "conflict", "empty_key"]
        out = [(release_id, r["class_id"], r["class_status"], r["raw_id"], r["source_system"], r["source_id"],
                r["revision"], r["raw_value"], r["raw_value"].encode("unicode_escape").decode("ascii"),
                r["raw_sha256"], r["normalized_key"], r["normalized_key"].encode("unicode_escape").decode("ascii"),
                r["conflict"], r["empty_key"]) for r in rows]
        return csv_response(header, out, f"members_{release_id}.csv")

    def overview(self) -> dict[str, Any]:
        raw = {}
        try:
            raw = http_request(f"{self.raw_base}/snapshot", timeout=10)
        except RemoteError as exc:
            raw = {"error": exc.message}
        gen = None
        try:
            gen = self.sync_current_generation()
        except HTTPError as exc:
            gen = {"error": exc.message}
        active = self.active_release()
        previews = self.db.q("SELECT id, created_at, generation_label, class_count, conflict_class_count"
                             " FROM releases WHERE status='preview' ORDER BY created_at DESC LIMIT 5")
        return {
            "raw": raw,
            "current_generation": gen,
            "active_release": active,
            "preview_releases": previews,
            "active_jobs": self.db.q(
                "SELECT id, mode, status, stage, progress_current, progress_total, publish FROM jobs"
                " WHERE status IN ('queued','running','cancelling') ORDER BY created_at DESC LIMIT 10"
            ),
            "recent_jobs": self.list_jobs(5),
            "pending_adjudications": self.db.scalar(
                "SELECT COUNT(*) FROM adjudications WHERE status='active'", default=0),
            "conflict_classes": active["conflict_class_count"] if active else 0,
            "at": now_iso(),
        }


class BuildCancelled(Exception):
    pass


def build_app(data_dir: str, raw_base: str, normalizer_base: str) -> App:
    store = LinkStore(data_dir, raw_base, normalizer_base)
    app = App("linkage")

    @app.route("GET", "/health")
    def health(req: Request):
        return {"service": app.name, "status": "ok", "started_at": app.started_at,
                "raw_base": store.raw_base, "normalizer_base": store.normalizer_base}

    @app.route("GET", "/overview")
    def overview(req: Request):
        return store.overview()

    @app.route("POST", "/builds")
    def builds(req: Request):
        body = req.json()
        if not isinstance(body, dict):
            raise HTTPError(400, "请求体必须是对象")
        mode = body.get("mode", "full")
        publish = body.get("publish", False)
        if not isinstance(publish, bool):
            raise HTTPError(400, "publish 必须是布尔值")
        gid = body.get("generation_id")
        if gid is not None:
            try:
                gid = int(gid)
            except (TypeError, ValueError):
                raise HTTPError(400, "generation_id 必须是整数")
        return store.start_build(mode, publish, gid, body.get("note"), bool(body.get("force", False)))

    @app.route("GET", "/jobs")
    def jobs(req: Request):
        return {"jobs": store.list_jobs(req.q_int("limit", 20, minimum=1, maximum=100) or 20)}

    @app.route("GET", "/jobs/(?P<job_id>[A-Za-z0-9_.-]+)")
    def job(req: Request, job_id: str):
        return store.get_job(job_id)

    @app.route("POST", "/jobs/(?P<job_id>[A-Za-z0-9_.-]+)/cancel")
    def cancel(req: Request, job_id: str):
        return store.cancel_job(job_id)

    @app.route("GET", "/releases")
    def releases(req: Request):
        return {"releases": store.list_releases(req.q_int("limit", 50, minimum=1, maximum=200) or 50, req.q("status"))}

    @app.route("GET", "/releases/active")
    def release_active(req: Request):
        return store.require_active()

    @app.route("GET", "/releases/(?P<release_id>[A-Za-z0-9_.-]+)")
    def release_get(req: Request, release_id: str):
        return store.release(release_id)

    @app.route("POST", "/releases/(?P<release_id>[A-Za-z0-9_.-]+)/activate")
    def release_activate(req: Request, release_id: str):
        body = req.json() if req.body else {}
        if not isinstance(body, dict):
            raise HTTPError(400, "请求体必须是对象")
        return store.activate_release(release_id, body.get("expected_generation_hash"))

    @app.route("GET", "/releases/(?P<release_id>[A-Za-z0-9_.-]+)/classes")
    def release_classes(req: Request, release_id: str):
        return store.classes(release_id, req.q("status"), req.q("q"),
                             req.q_int("page", 1, minimum=1) or 1,
                             req.q_int("page_size", 50, minimum=1, maximum=500) or 50)

    @app.route("GET", "/releases/(?P<release_id>[A-Za-z0-9_.-]+)/classes/(?P<class_id>[A-Za-z0-9_.-]+)")
    def release_class(req: Request, release_id: str, class_id: str):
        return store.class_detail(release_id, class_id)

    @app.route("GET", "/releases/(?P<release_id>[A-Za-z0-9_.-]+)/conflicts")
    def release_conflicts(req: Request, release_id: str):
        return store.conflicts(release_id, req.q_int("page", 1, minimum=1) or 1,
                               req.q_int("page_size", 50, minimum=1, maximum=200) or 50)

    @app.route("GET", "/releases/(?P<release_id>[A-Za-z0-9_.-]+)/members/(?P<raw_id>[0-9]+)")
    def release_member(req: Request, release_id: str, raw_id: str):
        return store.member(release_id, int(raw_id))

    @app.route("GET", "/releases/(?P<release_id>[A-Za-z0-9_.-]+)/adjudication-gaps")
    def release_gaps(req: Request, release_id: str):
        return {"release_id": release_id, "gaps": store.adjudication_gaps(release_id)}

    @app.route("GET", "/releases/(?P<release_id>[A-Za-z0-9_.-]+)/report.json")
    def release_report(req: Request, release_id: str):
        data = store.report(release_id)
        return json_response(data, headers={"Content-Disposition": f'attachment; filename="report_{release_id}.json"'})

    @app.route("GET", "/releases/(?P<release_id>[A-Za-z0-9_.-]+)/classes.csv")
    def release_classes_csv(req: Request, release_id: str):
        return store.classes_csv(release_id)

    @app.route("GET", "/releases/(?P<release_id>[A-Za-z0-9_.-]+)/members.csv")
    def release_members_csv(req: Request, release_id: str):
        return store.members_csv(release_id)

    @app.route("GET", "/diff")
    def diff(req: Request):
        base = req.q("base")
        candidate = req.q("candidate")
        if not base or not candidate:
            raise HTTPError(400, "需要 base 与 candidate 发布 id")
        return store.diff_releases(base, candidate, req.q_int("examples", 3, minimum=1, maximum=10) or 3)

    @app.route("GET", "/adjudications")
    def adjudications(req: Request):
        return {"items": store.list_adjudications(req.q("status"), req.q_int("limit", 200, minimum=1, maximum=1000) or 200)}

    @app.route("POST", "/adjudications")
    def adjudication_create(req: Request):
        body = req.json()
        if not isinstance(body, dict):
            raise HTTPError(400, "请求体必须是对象")
        return store.create_adjudication(body)

    @app.route("POST", "/adjudications/(?P<adj_id>[0-9]+)/revoke")
    def adjudication_revoke(req: Request, adj_id: str):
        body = req.json() if req.body else {}
        if not isinstance(body, dict):
            raise HTTPError(400, "请求体必须是对象")
        return store.revoke_adjudication(int(adj_id), body.get("note"))

    return app
