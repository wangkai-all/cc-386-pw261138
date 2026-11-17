"""Normalization-mapping service.

Responsibilities:
  * store rule *generations* (draft -> published -> superseded), immutable once
    published;
  * normalize a captured raw snapshot into a *run* pinned to one generation;
  * retain per-item, per-step evidence (original characters included);
  * expose collision evidence and cross-generation merge/split diffs.

A run is never rewritten.  Re-running the same rules creates a new run.  A
published generation cannot be edited; editing forks a new draft.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import secrets
from pathlib import Path
from typing import Any

from .common import (
    App, DB, HTTPError, Request, Response, csv_response, json_pretty, json_response,
    now_iso, sha256_text,
)
from . import normalization as N

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    status TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    steps_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    based_on INTEGER,
    note TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    generation_id INTEGER NOT NULL REFERENCES generations(id),
    generation_hash TEXT NOT NULL,
    generation_label TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    status TEXT NOT NULL,
    raw_fingerprint TEXT NOT NULL,
    raw_max_id INTEGER NOT NULL,
    row_count INTEGER NOT NULL,
    ok_count INTEGER NOT NULL DEFAULT 0,
    empty_count INTEGER NOT NULL DEFAULT 0,
    key_count INTEGER NOT NULL DEFAULT 0,
    collision_count INTEGER NOT NULL DEFAULT 0,
    item_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS run_items (
    run_id TEXT NOT NULL REFERENCES runs(id),
    raw_id INTEGER NOT NULL,
    source_system TEXT NOT NULL,
    source_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    raw_value TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    key TEXT NOT NULL,
    key_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    changed_steps INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY(run_id, raw_id)
);
CREATE INDEX IF NOT EXISTS idx_items_key ON run_items(run_id, key_sha256);
CREATE INDEX IF NOT EXISTS idx_items_raw ON run_items(run_id, raw_sha256);
CREATE TABLE IF NOT EXISTS run_keys (
    run_id TEXT NOT NULL REFERENCES runs(id),
    key TEXT NOT NULL,
    key_sha256 TEXT NOT NULL,
    member_count INTEGER NOT NULL,
    distinct_raw_count INTEGER NOT NULL,
    source_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(run_id, key_sha256)
);
CREATE INDEX IF NOT EXISTS idx_keys_collision ON run_keys(run_id, status, distinct_raw_count);
CREATE TABLE IF NOT EXISTS run_conflicts (
    run_id TEXT NOT NULL,
    key_sha256 TEXT NOT NULL,
    key TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    raw_value TEXT NOT NULL,
    member_count INTEGER NOT NULL,
    sources_json TEXT NOT NULL,
    PRIMARY KEY(run_id, key_sha256, raw_sha256)
);
"""


def _generation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "generation_id": row["id"],
        "label": row["label"],
        "status": row["status"],
        "steps": json.loads(row["steps_json"]),
        "steps_hash": row["steps_hash"],
        "created_at": row["created_at"],
        "published_at": row["published_at"],
        "based_on": row["based_on"],
        "note": row["note"],
    }


class NormalizerStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = DB(self.data_dir / "normalizer.db")
        self.db.executescript(SCHEMA)
        self._ensure_seed_generation()

    def _ensure_seed_generation(self):
        if self.db.scalar("SELECT COUNT(*) FROM generations", default=0):
            return
        steps = N.validate_steps([
            {"op": "unicode_nfkc", "note": "全角/兼容字符折叠"},
            {"op": "strip_format", "note": "去掉零宽字符与方向控制符"},
            {"op": "trim", "note": "去除首尾空白"},
            {"op": "collapse_ws", "note": "连续空白折叠为单个空格"},
            {"op": "casefold", "note": "Unicode 大小写折叠"},
        ])
        self._insert_generation("基线规则 v1", steps, None, "内置初始规则", publish=True)

    def _insert_generation(self, label: str, steps: list[dict[str, Any]], based_on: int | None, note: str | None,
                           publish: bool = False) -> dict[str, Any]:
        h = N.steps_hash(steps)
        now = now_iso()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO generations(label,status,steps_json,steps_hash,created_at,published_at,based_on,note)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (label, "published" if publish else "draft", json.dumps(steps, ensure_ascii=False, sort_keys=True),
                 h, now, now if publish else None, based_on, note),
            )
            gid = cur.lastrowid
            if publish:
                conn.execute("UPDATE generations SET status='superseded' WHERE status='published' AND id<>?", (gid,))
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES('current_generation',?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps({"generation_id": gid, "steps_hash": h, "published_at": now}),),
                )
        return _generation(self.db.q1("SELECT * FROM generations WHERE id=?", (gid,)))

    def create_generation(self, label: str, steps: Any, based_on: int | None, note: str | None) -> dict[str, Any]:
        if not isinstance(label, str) or not label.strip():
            raise HTTPError(400, "label 不能为空")
        clean = N.validate_steps(steps)
        probe = N.smoke_test(clean)
        gen = self._insert_generation(label.strip()[:120], clean, based_on, note, publish=False)
        gen["probe"] = probe
        return gen

    def update_generation(self, gid: int, label: str | None, steps: Any, note: str | None) -> dict[str, Any]:
        row = self.db.q1("SELECT * FROM generations WHERE id=?", (gid,))
        if not row:
            raise HTTPError(404, "规则世代不存在")
        if row["status"] != "draft":
            raise HTTPError(409, "只有草稿世代可以修改；已发布世代不可被悄悄改写，请基于它新建草稿")
        clean = N.validate_steps(steps) if steps is not None else json.loads(row["steps_json"])
        probe = N.smoke_test(clean)
        new_label = (label or row["label"]).strip()[:120]
        if not new_label:
            raise HTTPError(400, "label 不能为空")
        self.db.execute(
            "UPDATE generations SET label=?, steps_json=?, steps_hash=?, note=? WHERE id=?",
            (new_label, json.dumps(clean, ensure_ascii=False, sort_keys=True), N.steps_hash(clean), note if note is not None else row["note"], gid),
        )
        out = _generation(self.db.q1("SELECT * FROM generations WHERE id=?", (gid,)))
        out["probe"] = probe
        return out

    def publish_generation(self, gid: int) -> dict[str, Any]:
        row = self.db.q1("SELECT * FROM generations WHERE id=?", (gid,))
        if not row:
            raise HTTPError(404, "规则世代不存在")
        if row["status"] == "published":
            return {"published": _generation(row), "changed": False}
        if row["status"] == "superseded":
            raise HTTPError(409, "该世代已被取代，不能重新发布；请基于它新建草稿")
        steps = json.loads(row["steps_json"])
        N.smoke_test(steps)
        now = now_iso()
        with self.db.tx() as conn:
            conn.execute("UPDATE generations SET status='superseded' WHERE status='published'")
            conn.execute("UPDATE generations SET status='published', published_at=? WHERE id=?", (now, gid))
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('current_generation',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps({"generation_id": gid, "steps_hash": row["steps_hash"], "published_at": now}),),
            )
        return {"published": _generation(self.db.q1("SELECT * FROM generations WHERE id=?", (gid,))), "changed": True}

    def current_generation(self) -> dict[str, Any]:
        row = self.db.q1("SELECT * FROM generations WHERE status='published' ORDER BY id DESC LIMIT 1")
        if not row:
            raise HTTPError(409, "没有已发布的规则世代")
        return _generation(row)

    def generation(self, gid: int) -> dict[str, Any]:
        row = self.db.q1("SELECT * FROM generations WHERE id=?", (gid,))
        if not row:
            raise HTTPError(404, "规则世代不存在")
        return _generation(row)

    # ------------------------------------------------------------------ runs
    def run(self, generation_id: int | None, rows: list[dict[str, Any]], raw_snapshot: dict[str, Any]) -> dict[str, Any]:
        gen = self.generation(generation_id) if generation_id else self.current_generation()
        steps = gen["steps"]
        if not rows:
            raise HTTPError(400, "没有可规范化的记录")
        if len(rows) > 200_000:
            raise HTTPError(413, "单次运行最多 200000 条记录")

        seen: set[int] = set()
        prepared: list[tuple[dict[str, Any], dict[str, Any]]] = []
        fingerprint_parts: list[str] = []
        for idx, row in enumerate(rows):
            try:
                raw_id = int(row["raw_id"])
            except (KeyError, TypeError, ValueError):
                raise HTTPError(400, f"第 {idx + 1} 行 raw_id 无效")
            if raw_id in seen:
                raise HTTPError(400, f"raw_id {raw_id} 重复")
            seen.add(raw_id)
            raw = row.get("raw_value")
            if not isinstance(raw, str):
                raise HTTPError(400, f"raw_id {raw_id} 的 raw_value 必须是字符串")
            source = row.get("source_system")
            source_id = row.get("source_id")
            if not isinstance(source, str) or not isinstance(source_id, str):
                raise HTTPError(400, f"raw_id {raw_id} 缺少 source_system/source_id")
            raw_sha = sha256_text(raw)
            declared = row.get("raw_sha256")
            if declared and declared != raw_sha:
                raise HTTPError(409, f"raw_id {raw_id} 的 raw_value 与声明的 raw_sha256 不一致（数据在传输中被改动）")
            prepared.append((row, N.normalize(raw, steps)))
            fingerprint_parts.append(f"{raw_id}:{raw_sha}")

        fingerprint_parts.sort()
        item_fingerprint = hashlib.sha256("|".join(fingerprint_parts).encode("utf-8")).hexdigest()
        claimed = raw_snapshot.get("fingerprint") if isinstance(raw_snapshot, dict) else None
        if claimed and claimed != item_fingerprint:
            raise HTTPError(409, "原始快照指纹不一致：接收端数据在抓取过程中发生变化，请重新抓取")

        run_id = "r-" + secrets.token_hex(8)
        created = now_iso()
        import time
        t0 = time.monotonic()
        key_hashes: dict[str, str] = {}
        raw_by_key_hash: dict[str, set[str]] = {}
        sources_by_key_hash: dict[str, set[str]] = {}
        counts_by_key_hash: dict[str, int] = {}
        raw_values_by_key_hash: dict[str, dict[str, set[str]]] = {}
        empty_count = 0
        item_rows = []
        for row, result in prepared:
            raw = row["raw_value"]
            key = result["key"]
            key_sha = hashlib.sha256(key.encode("utf-8")).hexdigest()
            key_hashes[key_sha] = key
            status = result["status"]
            if status == "empty_key":
                empty_count += 1
            counts_by_key_hash[key_sha] = counts_by_key_hash.get(key_sha, 0) + 1
            raw_by_key_hash.setdefault(key_sha, set()).add(result["raw_sha256"])
            sources_by_key_hash.setdefault(key_sha, set()).add(row["source_system"])
            raw_values_by_key_hash.setdefault(key_sha, {}).setdefault(result["raw_sha256"], set()).add(raw)
            item_rows.append((
                run_id, int(row["raw_id"]), row["source_system"], row["source_id"], int(row.get("revision") or 1),
                raw, result["raw_sha256"], key, key_sha, status, result["changed_steps"],
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            ))
        collision_keys = [kh for kh, raws in raw_by_key_hash.items() if len(raws) > 1]
        now = now_iso()
        duration_ms = int((time.monotonic() - t0) * 1000)
        snapshot_info = {
            "fingerprint": item_fingerprint,
            "claimed_fingerprint": claimed,
            "count": len(prepared),
            "max_id": raw_snapshot.get("max_id") if isinstance(raw_snapshot, dict) else None,
        }
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO runs(id,generation_id,generation_hash,generation_label,steps_json,status,raw_fingerprint,"
                "raw_max_id,row_count,ok_count,empty_count,key_count,collision_count,item_fingerprint,created_at,completed_at,duration_ms)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, gen["generation_id"], gen["steps_hash"], gen["label"],
                 json.dumps(steps, ensure_ascii=False, sort_keys=True), "completed",
                 item_fingerprint, int(snapshot_info["max_id"] or max(seen)), len(prepared),
                 len(prepared) - empty_count, empty_count, len(key_hashes), len(collision_keys),
                 item_fingerprint, created, now, duration_ms),
            )
            conn.executemany(
                "INSERT INTO run_items(run_id,raw_id,source_system,source_id,revision,raw_value,raw_sha256,key,key_sha256,"
                "status,changed_steps,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", item_rows,
            )
            key_rows = []
            for key_sha, key in key_hashes.items():
                status = "collision" if key_sha in collision_keys else ("empty" if key == "" else "clean")
                key_rows.append((run_id, key, key_sha, counts_by_key_hash[key_sha], len(raw_by_key_hash[key_sha]),
                                 len(sources_by_key_hash[key_sha]), status))
            conn.executemany(
                "INSERT INTO run_keys(run_id,key,key_sha256,member_count,distinct_raw_count,source_count,status)"
                " VALUES(?,?,?,?,?,?,?)", key_rows,
            )
            conflict_rows = []
            for key_sha in collision_keys:
                for raw_sha, values in raw_values_by_key_hash[key_sha].items():
                    members = [
                        dict(r) for r in conn.execute(
                            "SELECT source_system, source_id, COUNT(*) AS n FROM run_items"
                            " WHERE run_id=? AND key_sha256=? AND raw_sha256=? GROUP BY source_system, source_id",
                            (run_id, key_sha, raw_sha),
                        ).fetchall()
                    ]
                    conflict_rows.append((
                        run_id, key_sha, key_hashes[key_sha], raw_sha, sorted(values)[0],
                        sum(m["n"] for m in members), json.dumps(members, ensure_ascii=False, sort_keys=True),
                    ))
            conn.executemany(
                "INSERT INTO run_conflicts(run_id,key_sha256,key,raw_sha256,raw_value,member_count,sources_json)"
                " VALUES(?,?,?,?,?,?,?)", conflict_rows,
            )
        return self.run_summary(run_id) | {"snapshot": snapshot_info}

    def run_summary(self, run_id: str) -> dict[str, Any]:
        row = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        if not row:
            raise HTTPError(404, "运行不存在")
        steps = json.loads(row.pop("steps_json"))
        return {
            "run_id": row["id"],
            "generation": {
                "generation_id": row["generation_id"],
                "label": row["generation_label"],
                "steps_hash": row["generation_hash"],
                "steps": steps,
            },
            "status": row["status"],
            "raw_fingerprint": row["raw_fingerprint"],
            "item_fingerprint": row["item_fingerprint"],
            "raw_max_id": row["raw_max_id"],
            "row_count": row["row_count"],
            "ok_count": row["ok_count"],
            "empty_count": row["empty_count"],
            "key_count": row["key_count"],
            "collision_count": row["collision_count"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
            "duration_ms": row["duration_ms"],
        }

    def run_exists(self, run_id: str) -> bool:
        return self.db.q1("SELECT 1 FROM runs WHERE id=?", (run_id,)) is not None

    def run_keys(self, run_id: str, status: str | None, search: str | None, page: int, page_size: int) -> dict[str, Any]:
        self.run_summary(run_id)
        clauses = ["run_id=?"]
        args: list[Any] = [run_id]
        if status:
            if status not in {"clean", "collision", "empty"}:
                raise HTTPError(400, "status 只能是 clean/collision/empty")
            clauses.append("status=?")
            args.append(status)
        if search:
            clauses.append("key LIKE ?")
            args.append(f"%{search}%")
        where = " AND ".join(clauses)
        total = self.db.scalar(f"SELECT COUNT(*) FROM run_keys WHERE {where}", args, default=0)
        rows = self.db.q(
            f"SELECT * FROM run_keys WHERE {where} ORDER BY status='collision' DESC, member_count DESC, key LIMIT ? OFFSET ?",
            [*args, page_size, (page - 1) * page_size],
        )
        return {"items": rows, "page": page, "page_size": page_size, "total": total, "pages": max(1, (total + page_size - 1) // page_size)}

    def run_items(self, run_id: str, key_sha: str | None, source: str | None, q: str | None,
                  raw_sha: str | None, page: int, page_size: int) -> dict[str, Any]:
        self.run_summary(run_id)
        clauses = ["run_id=?"]
        args: list[Any] = [run_id]
        if key_sha:
            clauses.append("key_sha256=?")
            args.append(key_sha)
        if raw_sha:
            clauses.append("raw_sha256=?")
            args.append(raw_sha)
        if source:
            clauses.append("source_system=?")
            args.append(source)
        if q:
            clauses.append("(raw_value LIKE ? OR key LIKE ? OR source_id LIKE ?)")
            args.extend([f"%{q}%"] * 3)
        where = " AND ".join(clauses)
        total = self.db.scalar(f"SELECT COUNT(*) FROM run_items WHERE {where}", args, default=0)
        rows = self.db.q(
            f"SELECT raw_id,source_system,source_id,revision,raw_value,raw_sha256,key,key_sha256,status,changed_steps"
            f" FROM run_items WHERE {where} ORDER BY raw_id LIMIT ? OFFSET ?",
            [*args, page_size, (page - 1) * page_size],
        )
        for row in rows:
            row["raw_escaped"] = row["raw_value"].encode("unicode_escape").decode("ascii")
            row["key_escaped"] = row["key"].encode("unicode_escape").decode("ascii")
        return {"items": rows, "page": page, "page_size": page_size, "total": total, "pages": max(1, (total + page_size - 1) // page_size)}

    def item_evidence(self, run_id: str, raw_id: int) -> dict[str, Any]:
        summary = self.run_summary(run_id)
        row = self.db.q1("SELECT * FROM run_items WHERE run_id=? AND raw_id=?", (run_id, raw_id))
        if not row:
            raise HTTPError(404, "该运行中没有此 raw_id")
        evidence = json.loads(row.pop("evidence_json"))
        return {"run": summary, "raw_id": raw_id, "source_system": row["source_system"],
                "source_id": row["source_id"], "raw_sha256": row["raw_sha256"], "key_sha256": row["key_sha256"],
                "evidence": evidence}

    def collisions(self, run_id: str, page: int, page_size: int) -> dict[str, Any]:
        summary = self.run_summary(run_id)
        total = self.db.scalar("SELECT COUNT(*) FROM run_keys WHERE run_id=? AND status='collision'", (run_id,), default=0)
        keys = self.db.q(
            "SELECT * FROM run_keys WHERE run_id=? AND status='collision' ORDER BY distinct_raw_count DESC, key LIMIT ? OFFSET ?",
            (run_id, page_size, (page - 1) * page_size),
        )
        for key in keys:
            conflicts = self.db.q(
                "SELECT * FROM run_conflicts WHERE run_id=? AND key_sha256=? ORDER BY member_count DESC, raw_value",
                (run_id, key["key_sha256"]),
            )
            for c in conflicts:
                c["sources"] = json.loads(c.pop("sources_json"))
                c["raw_escaped"] = c["raw_value"].encode("unicode_escape").decode("ascii")
                c["raw_codepoints"] = N.codepoint_report(c["raw_value"])
            key["conflicts"] = conflicts
            key["key_escaped"] = key["key"].encode("unicode_escape").decode("ascii")
        return {"run": summary, "items": keys, "page": page, "page_size": page_size, "total": total,
                "pages": max(1, (total + page_size - 1) // page_size)}

    # --------------------------------------------------------- cross-run diff
    def diff(self, from_run: str, to_run: str, examples: int = 3) -> dict[str, Any]:
        a = self.run_summary(from_run)
        b = self.run_summary(to_run)
        rows = self.db.q(
            "SELECT a.raw_id, a.key AS old_key, b.key AS new_key, a.raw_value FROM run_items a"
            " JOIN run_items b ON a.raw_id=b.raw_id AND b.run_id=? WHERE a.run_id=?",
            (to_run, from_run),
        )
        only_old = self.db.scalar(
            "SELECT COUNT(*) FROM run_items a WHERE a.run_id=? AND NOT EXISTS"
            " (SELECT 1 FROM run_items b WHERE b.run_id=? AND b.raw_id=a.raw_id)", (from_run, to_run), default=0,
        )
        only_new = self.db.scalar(
            "SELECT COUNT(*) FROM run_items b WHERE b.run_id=? AND NOT EXISTS"
            " (SELECT 1 FROM run_items a WHERE a.run_id=? AND a.raw_id=b.raw_id)", (to_run, from_run), default=0,
        )
        old_to_new: dict[str, set[str]] = {}
        new_to_old: dict[str, set[str]] = {}
        examples_by_old: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            old_to_new.setdefault(row["old_key"], set()).add(row["new_key"])
            new_to_old.setdefault(row["new_key"], set()).add(row["old_key"])
            bucket = examples_by_old.setdefault(row["old_key"], [])
            if len(bucket) < examples:
                bucket.append({"raw_id": row["raw_id"], "raw_value": row["raw_value"],
                               "old_key": row["old_key"], "new_key": row["new_key"]})
        merges = []
        for new_key, olds in new_to_old.items():
            if len(olds) > 1:
                merges.append({"new_key": new_key, "old_keys": sorted(olds),
                               "old_key_count": len(olds),
                               "sample": [e for old in olds for e in examples_by_old.get(old, [])][:examples * 2]})
        splits = []
        for old_key, news in old_to_new.items():
            if len(news) > 1:
                splits.append({"old_key": old_key, "new_keys": sorted(news),
                               "new_key_count": len(news), "sample": examples_by_old.get(old_key, [])})
        renames = [
            {"old_key": old, "new_key": next(iter(news)), "sample": examples_by_old.get(old, [])}
            for old, news in old_to_new.items() if len(news) == 1 and next(iter(news)) != old
        ]
        unchanged = sum(1 for old, news in old_to_new.items() if len(news) == 1 and next(iter(news)) == old)
        return {
            "from": {"run_id": from_run, "generation": a["generation"], "row_count": a["row_count"]},
            "to": {"run_id": to_run, "generation": b["generation"], "row_count": b["row_count"]},
            "compared_items": len(rows),
            "only_in_from": only_old,
            "only_in_to": only_new,
            "unchanged_key_count": unchanged,
            "merges": merges,
            "splits": splits,
            "renames": renames,
            "impact": {
                "merged_new_keys": len(merges),
                "split_old_keys": len(splits),
                "renamed_keys": len(renames),
                "affected_items": len(rows) - sum(
                    len(e) for e in [[1] for _ in []]
                ),
            },
        }

    def evidence_csv(self, run_id: str) -> Response:
        summary = self.run_summary(run_id)
        generator = self.db.conn().execute(
            "SELECT raw_id,source_system,source_id,revision,raw_value,raw_sha256,key,key_sha256,status,changed_steps,evidence_json"
            " FROM run_items WHERE run_id=? ORDER BY raw_id", (run_id,),
        )
        buf = io.StringIO(newline="")
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow([
            "run_id", "generation_id", "generation_label", "steps_hash", "raw_id", "source_system", "source_id",
            "revision", "raw_value", "raw_escaped", "raw_codepoints", "raw_sha256", "normalized_key", "key_escaped",
            "key_codepoints", "status", "changed_steps", "step_index", "op", "step_changed", "step_input",
            "step_input_escaped", "step_output", "step_output_escaped", "change_count", "changes_detail",
        ])
        for item in generator:
            ev = json.loads(item["evidence_json"])
            raw_cps = ";".join(f"{c['codepoint']} {c['name']}" for c in ev["raw_codepoints"]) or "(ascii)"
            key_cps = ";".join(f"{c['codepoint']} {c['name']}" for c in ev["key_codepoints"]) or "(ascii)"
            for step in ev["steps"]:
                writer.writerow([
                    run_id, summary["generation"]["generation_id"], summary["generation"]["label"],
                    summary["generation"]["steps_hash"], item["raw_id"], item["source_system"], item["source_id"],
                    item["revision"], item["raw_value"], ev["raw_escaped"], raw_cps, item["raw_sha256"],
                    item["key"], ev["key_escaped"], key_cps, item["status"], item["changed_steps"],
                    step["index"], step["op"], "yes" if step["changed"] else "no",
                    step.get("input", ""), step.get("input_escaped", ""), step.get("output", ""),
                    step.get("output_escaped", ""), step.get("change_count", 0),
                    json.dumps(step.get("changes", []), ensure_ascii=False, separators=(",", ":")),
                ])
        data = ("﻿" + buf.getvalue()).encode("utf-8")
        from .common import binary_response
        return binary_response(data, "text/csv; charset=utf-8", f"evidence_{run_id}.csv")

    def mapping_csv(self, run_id: str) -> Response:
        summary = self.run_summary(run_id)
        rows = self.db.q(
            "SELECT raw_id,source_system,source_id,revision,raw_value,raw_sha256,key,status FROM run_items WHERE run_id=? ORDER BY raw_id",
            (run_id,),
        )
        def gen():
            for r in rows:
                yield (r["raw_id"], r["source_system"], r["source_id"], r["revision"], r["raw_value"],
                       r["raw_value"].encode("unicode_escape").decode("ascii"), r["raw_sha256"], r["key"],
                       r["key"].encode("unicode_escape").decode("ascii"), r["status"])
        return csv_response(
            ["raw_id", "source_system", "source_id", "revision", "raw_value", "raw_escaped", "raw_sha256",
             "normalized_key", "key_escaped", "status"],
            gen(), f"mapping_{run_id}.csv",
        )


def build_app(data_dir: str) -> App:
    store = NormalizerStore(data_dir)
    app = App("normalization-mapper")

    @app.route("GET", "/health")
    def health(req: Request):
        return {"service": app.name, "status": "ok", "started_at": app.started_at}

    @app.route("GET", "/rules")
    def rules(req: Request):
        rows = store.db.q("SELECT * FROM generations ORDER BY id DESC")
        current = store.current_generation()
        return {"current": current, "generations": [_generation(r) for r in rows]}

    @app.route("GET", "/rules/current")
    def rules_current(req: Request):
        return store.current_generation()

    @app.route("GET", "/rules/generations/(?P<gid>[0-9]+)")
    def rules_get(req: Request, gid: str):
        return store.generation(int(gid))

    @app.route("POST", "/rules/generations")
    def rules_create(req: Request):
        body = req.json()
        if not isinstance(body, dict):
            raise HTTPError(400, "请求体必须是对象")
        try:
            return store.create_generation(body.get("label", ""), body.get("steps"), body.get("based_on"), body.get("note"))
        except N.RuleError as exc:
            raise HTTPError(400, str(exc))

    @app.route("PUT", "/rules/generations/(?P<gid>[0-9]+)")
    def rules_update(req: Request, gid: str):
        body = req.json()
        if not isinstance(body, dict):
            raise HTTPError(400, "请求体必须是对象")
        try:
            return store.update_generation(int(gid), body.get("label"), body.get("steps"), body.get("note"))
        except N.RuleError as exc:
            raise HTTPError(400, str(exc))

    @app.route("POST", "/rules/generations/(?P<gid>[0-9]+)/publish")
    def rules_publish(req: Request, gid: str):
        return store.publish_generation(int(gid))

    @app.route("POST", "/normalize/preview")
    def preview(req: Request):
        body = req.json()
        if not isinstance(body, dict):
            raise HTTPError(400, "请求体必须是对象")
        if body.get("generation_id"):
            steps = store.generation(int(body["generation_id"]))["steps"]
        elif body.get("steps") is not None:
            steps = N.validate_steps(body["steps"])
        else:
            steps = store.current_generation()["steps"]
        values = body.get("values")
        if not isinstance(values, list) or not values:
            raise HTTPError(400, "values 必须是非空数组")
        if len(values) > 500:
            raise HTTPError(413, "预览最多 500 个值")
        out = []
        for idx, value in enumerate(values):
            if not isinstance(value, str):
                raise HTTPError(400, f"values[{idx}] 必须是字符串")
            out.append(N.normalize(value, steps))
        return {"steps_hash": N.steps_hash(steps), "steps": steps, "results": out}

    @app.route("POST", "/runs")
    def create_run(req: Request):
        body = req.json()
        if not isinstance(body, dict):
            raise HTTPError(400, "请求体必须是对象")
        rows = body.get("rows")
        if not isinstance(rows, list):
            raise HTTPError(400, "rows 必须是数组")
        raw_snapshot = body.get("raw_snapshot") or {}
        if not isinstance(raw_snapshot, dict):
            raise HTTPError(400, "raw_snapshot 必须是对象")
        gid = body.get("generation_id")
        if gid is not None:
            try:
                gid = int(gid)
            except (TypeError, ValueError):
                raise HTTPError(400, "generation_id 必须是整数")
        return store.run(gid, rows, raw_snapshot)

    @app.route("GET", "/runs")
    def list_runs(req: Request):
        limit = req.q_int("limit", 20, minimum=1, maximum=100)
        rows = store.db.q("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
        out = []
        for row in rows:
            row = dict(row)
            row.pop("steps_json", None)
            out.append(row)
        return {"runs": out}

    @app.route("GET", "/runs/(?P<run_id>[A-Za-z0-9_.-]+)")
    def get_run(req: Request, run_id: str):
        return store.run_summary(run_id)

    @app.route("GET", "/runs/(?P<run_id>[A-Za-z0-9_.-]+)/keys")
    def get_keys(req: Request, run_id: str):
        return store.run_keys(run_id, req.q("status"), req.q("q"), req.q_int("page", 1, minimum=1) or 1,
                              req.q_int("page_size", 50, minimum=1, maximum=500) or 50)

    @app.route("GET", "/runs/(?P<run_id>[A-Za-z0-9_.-]+)/items")
    def get_items(req: Request, run_id: str):
        return store.run_items(run_id, req.q("key_sha256"), req.q("source_system"), req.q("q"), req.q("raw_sha256"),
                               req.q_int("page", 1, minimum=1) or 1,
                               req.q_int("page_size", 50, minimum=1, maximum=5000) or 50)

    @app.route("GET", "/runs/(?P<run_id>[A-Za-z0-9_.-]+)/items/(?P<raw_id>[0-9]+)")
    def get_item(req: Request, run_id: str, raw_id: str):
        return store.item_evidence(run_id, int(raw_id))

    @app.route("GET", "/runs/(?P<run_id>[A-Za-z0-9_.-]+)/collisions")
    def get_collisions(req: Request, run_id: str):
        return store.collisions(run_id, req.q_int("page", 1, minimum=1) or 1,
                                req.q_int("page_size", 50, minimum=1, maximum=500) or 50)

    @app.route("GET", "/runs/(?P<run_id>[A-Za-z0-9_.-]+)/evidence.csv")
    def evidence_csv(req: Request, run_id: str):
        return store.evidence_csv(run_id)

    @app.route("GET", "/runs/(?P<run_id>[A-Za-z0-9_.-]+)/mapping.csv")
    def mapping_csv(req: Request, run_id: str):
        return store.mapping_csv(run_id)

    @app.route("GET", "/runs/(?P<run_id>[A-Za-z0-9_.-]+)/rules.json")
    def rules_json(req: Request, run_id: str):
        summary = store.run_summary(run_id)
        payload = {
            "run_id": run_id,
            "generation_id": summary["generation"]["generation_id"],
            "generation_label": summary["generation"]["label"],
            "steps_hash": summary["generation"]["steps_hash"],
            "steps": summary["generation"]["steps"],
            "created_at": summary["created_at"],
            "raw_fingerprint": summary["raw_fingerprint"],
            "item_fingerprint": summary["item_fingerprint"],
        }
        return json_response(payload, headers={"Content-Disposition": f'attachment; filename="rules_{run_id}.json"'})

    @app.route("GET", "/diff")
    def diff(req: Request):
        from_run = req.q("from_run")
        to_run = req.q("to_run")
        if not from_run or not to_run:
            raise HTTPError(400, "需要 from_run 和 to_run")
        return store.diff(from_run, to_run, req.q_int("examples", 3, minimum=1, maximum=10) or 3)

    return app
