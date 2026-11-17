"""Raw-value receiver service.

This service owns the immutable-ish receipt log.  Raw strings are stored
verbatim (no trimming, no case folding, no Unicode normalization).  A record
is never overwritten: when a source sends a new value for the same external
identity, the old revision is deactivated and a new revision is appended.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import App, DB, HTTPError, Request, Response, csv_response, json_response, now_iso, sha256_text

MAX_ROWS_PER_BATCH = 50_000
MAX_RAW_LENGTH = 20_000
SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,63}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    source_system TEXT,
    received_at TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    inserted INTEGER NOT NULL,
    unchanged INTEGER NOT NULL,
    revised INTEGER NOT NULL,
    rejected INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    source_system TEXT NOT NULL,
    source_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    raw_value TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    received_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    superseded_by INTEGER,
    UNIQUE(source_system, source_id, revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_records_active_identity
    ON records(source_system, source_id) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_records_active_id ON records(active, id);
CREATE INDEX IF NOT EXISTS idx_records_raw_sha ON records(raw_sha256);
"""


class RawStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = DB(self.data_dir / "raw.db")
        self.db.executescript(SCHEMA)

    # ---------------------------------------------------------------- ingest
    def ingest(self, rows: list[dict[str, Any]], default_source: str | None, note: str | None,
               content_sha256: str | None = None) -> dict[str, Any]:
        if not rows:
            raise HTTPError(400, "没有可接收的行")
        if len(rows) > MAX_ROWS_PER_BATCH:
            raise HTTPError(413, f"单批最多 {MAX_ROWS_PER_BATCH} 行")
        batch_id = "b-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4)
        received = now_iso()
        prepared: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        canonical_for_hash: list[list[str]] = []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                rejected.append({"index": idx, "reason": "行必须是对象"})
                continue
            source = row.get("source_system") or default_source
            if not isinstance(source, str) or not SOURCE_RE.match(source.strip()):
                rejected.append({"index": idx, "reason": "source_system 缺失或格式不合法"})
                continue
            source = source.strip()
            source_id = row.get("source_id")
            if not isinstance(source_id, str) or not source_id.strip():
                rejected.append({"index": idx, "reason": "source_id 不能为空", "source_system": source})
                continue
            source_id = source_id.strip()
            raw = row.get("raw_value")
            if not isinstance(raw, str):
                rejected.append({"index": idx, "reason": "raw_value 必须是字符串", "source_system": source, "source_id": source_id})
                continue
            if len(raw) > MAX_RAW_LENGTH:
                rejected.append({"index": idx, "reason": f"raw_value 超过 {MAX_RAW_LENGTH} 字符", "source_system": source, "source_id": source_id})
                continue
            prepared.append({"index": idx, "source_system": source, "source_id": source_id, "raw_value": raw})
            canonical_for_hash.append([source, source_id, raw])
        if not prepared:
            raise HTTPError(400, "所有行都未通过校验", rejected[:50])
        batch_hash = content_sha256 or hashlib.sha256(
            json.dumps(canonical_for_hash, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        results: list[dict[str, Any]] = []
        inserted = unchanged = revised = 0
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO batches(id,source_system,received_at,row_count,inserted,unchanged,revised,rejected,content_sha256,note)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (batch_id, default_source, received, len(rows), 0, 0, 0, len(rejected), batch_hash, note),
            )
            for item in prepared:
                existing = conn.execute(
                    "SELECT id, revision, raw_sha256 FROM records WHERE source_system=? AND source_id=? AND active=1",
                    (item["source_system"], item["source_id"]),
                ).fetchone()
                raw_sha = sha256_text(item["raw_value"])
                if existing is not None and existing["raw_sha256"] == raw_sha:
                    unchanged += 1
                    results.append({
                        "index": item["index"], "raw_id": existing["id"], "revision": existing["revision"],
                        "source_system": item["source_system"], "source_id": item["source_id"], "status": "unchanged",
                    })
                    continue
                if existing is not None:
                    revision = int(existing["revision"]) + 1
                    conn.execute("UPDATE records SET active=0 WHERE id=?", (existing["id"],))
                else:
                    revision = 1
                cursor = conn.execute(
                    "INSERT INTO records(batch_id,source_system,source_id,revision,raw_value,raw_sha256,received_at,active)"
                    " VALUES(?,?,?,?,?,?,?,1)",
                    (batch_id, item["source_system"], item["source_id"], revision, item["raw_value"], raw_sha, received),
                )
                new_id = cursor.lastrowid
                if existing is not None:
                    conn.execute("UPDATE records SET superseded_by=? WHERE id=?", (new_id, existing["id"]))
                    conn.execute("UPDATE records SET superseded_by=NULL WHERE id=?", (new_id,))
                    revised += 1
                    status = "revised"
                else:
                    inserted += 1
                    status = "inserted"
                results.append({
                    "index": item["index"], "raw_id": new_id, "revision": revision,
                    "source_system": item["source_system"], "source_id": item["source_id"], "status": status,
                })
            conn.execute(
                "UPDATE batches SET inserted=?,unchanged=?,revised=?,rejected=? WHERE id=?",
                (inserted, unchanged, revised, len(rejected), batch_id),
            )
        return {
            "batch_id": batch_id,
            "received_at": received,
            "row_count": len(rows),
            "inserted": inserted,
            "unchanged": unchanged,
            "revised": revised,
            "rejected": len(rejected),
            "rejected_rows": rejected[:200],
            "content_sha256": batch_hash,
            "records": results,
            "note": note,
        }


def _record_row(row: dict[str, Any], detailed: bool = False) -> dict[str, Any]:
    raw = row["raw_value"]
    item = {
        "raw_id": row["id"],
        "source_system": row["source_system"],
        "source_id": row["source_id"],
        "revision": row["revision"],
        "raw_value": raw,
        "raw_sha256": row["raw_sha256"],
        "raw_length": len(raw),
        "raw_escaped": raw.encode("unicode_escape").decode("ascii"),
        "received_at": row["received_at"],
        "batch_id": row["batch_id"],
        "active": bool(row["active"]),
    }
    if detailed:
        item["raw_utf8_hex"] = raw.encode("utf-8").hex()
        item["raw_codepoints"] = [
            {"char": ch, "codepoint": f"U+{ord(ch):04X}", "name": __import__("unicodedata").name(ch, "UNKNOWN")}
            for ch in raw
            if ord(ch) >= 128 or not ch.isprintable()
        ]
    return item


def build_app(data_dir: str) -> App:
    store = RawStore(data_dir)
    app = App("raw-receiver")

    @app.route("GET", "/health")
    def health(req: Request):
        return {"service": app.name, "status": "ok", "started_at": app.started_at, "data_dir": store.data_dir.name}

    @app.route("POST", "/ingest")
    def ingest(req: Request):
        ctype = req.headers.get("content-type", "")
        note = req.q("note")
        default_source = req.q("source_system")
        if "text/csv" in ctype or "application/csv" in ctype:
            try:
                text = req.body.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise HTTPError(400, "CSV 必须使用 UTF-8 编码", str(exc))
            reader = csv.reader(io.StringIO(text, newline=""))
            try:
                header = [h.strip() for h in next(reader)]
            except StopIteration:
                raise HTTPError(400, "CSV 为空")
            lookup = {name.lower(): idx for idx, name in enumerate(header)}
            if "raw_value" not in lookup:
                raise HTTPError(400, "CSV 必须包含 raw_value 列")
            if "source_id" not in lookup and "external_id" not in lookup:
                raise HTTPError(400, "CSV 必须包含 source_id 列")
            rows = []
            for record in reader:
                if not any(cell.strip() for cell in record):
                    continue
                row = {
                    "source_system": default_source or (record[lookup["source_system"]] if "source_system" in lookup and lookup["source_system"] < len(record) else None),
                    "source_id": record[lookup["source_id"]] if "source_id" in lookup and lookup["source_id"] < len(record) else record[lookup["external_id"]],
                    "raw_value": record[lookup["raw_value"]] if lookup["raw_value"] < len(record) else None,
                }
                rows.append(row)
            return store.ingest(rows, default_source, note)
        if "application/json" not in ctype and req.body[:1] not in (b"{", b"["):
            raise HTTPError(415, "请使用 application/json 或 text/csv")
        payload = req.json()
        if isinstance(payload, list):
            payload = {"rows": payload}
        if not isinstance(payload, dict):
            raise HTTPError(400, "JSON 根必须是对象")
        rows = payload.get("rows") or payload.get("records")
        if not isinstance(rows, list):
            raise HTTPError(400, "rows 必须是数组")
        source = payload.get("source_system") or default_source
        if source is not None and not isinstance(source, str):
            raise HTTPError(400, "source_system 必须是字符串")
        return store.ingest(rows, source, payload.get("note") or note)

    @app.route("GET", "/active_ids")
    def active_ids(req: Request):
        rows = store.db.q("SELECT id FROM records WHERE active=1 ORDER BY id")
        return {
            "ids": [r["id"] for r in rows],
            "count": len(rows),
            "max_id": rows[-1]["id"] if rows else 0,
            "at": now_iso(),
        }

    @app.route("GET", "/records")
    def records(req: Request):
        ids_param = req.q("ids")
        limit = req.q_int("limit", 100, minimum=1, maximum=5000)
        if ids_param:
            try:
                ids = [int(x) for x in ids_param.split(",") if x]
            except ValueError:
                raise HTTPError(400, "ids 必须是逗号分隔的整数")
            if len(ids) > 5000:
                raise HTTPError(413, "单次最多按 id 读取 5000 条")
            placeholders = ",".join("?" for _ in ids)
            rows = store.db.q(f"SELECT * FROM records WHERE id IN ({placeholders}) ORDER BY id", ids)
            return {"items": [_record_row(r) for r in rows], "requested": len(ids), "found": len(rows)}
        since_id = req.q_int("since_id", 0, minimum=0)
        active = req.q("active", "1") != "0"
        source = req.q("source_system")
        search = req.q("q")
        clauses = ["id > ?"]
        args: list[Any] = [since_id]
        if active:
            clauses.append("active = 1")
        if source:
            clauses.append("source_system = ?")
            args.append(source)
        if search:
            clauses.append("raw_value LIKE ?")
            args.append(f"%{search}%")
        args.append(limit + 1)
        rows = store.db.q(
            "SELECT * FROM records WHERE " + " AND ".join(clauses) + " ORDER BY id LIMIT ?", args
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = rows[-1]["id"] if rows else since_id
        return {
            "items": [_record_row(r) for r in rows],
            "next_cursor": next_cursor,
            "has_more": has_more,
            "active": active,
        }

    @app.route("GET", "/records/(?P<raw_id>[0-9]+)")
    def record_detail(req: Request, raw_id: str):
        row = store.db.q1("SELECT * FROM records WHERE id=?", (int(raw_id),))
        if not row:
            raise HTTPError(404, "记录不存在")
        item = _record_row(row, detailed=True)
        item["superseded_by"] = row["superseded_by"]
        return item

    @app.route("GET", "/snapshot")
    def snapshot(req: Request):
        stats = store.db.q1(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS active,"
            " MAX(CASE WHEN active=1 THEN id ELSE NULL END) AS max_active_id,"
            " MIN(received_at) AS first_received, MAX(received_at) AS last_received FROM records"
        ) or {}
        sources = store.db.q(
            "SELECT source_system, COUNT(*) AS active FROM records WHERE active=1 GROUP BY source_system ORDER BY source_system"
        )
        batches = store.db.scalar("SELECT COUNT(*) FROM batches", default=0)
        return {
            "service": app.name,
            "total_count": stats.get("total") or 0,
            "active_count": stats.get("active") or 0,
            "max_active_id": stats.get("max_active_id") or 0,
            "first_received": stats.get("first_received"),
            "last_received": stats.get("last_received"),
            "sources": sources,
            "batch_count": batches,
            "at": now_iso(),
        }

    @app.route("GET", "/sources")
    def sources(req: Request):
        rows = store.db.q(
            "SELECT source_system, COUNT(*) AS active, MAX(received_at) AS last_received FROM records"
            " WHERE active=1 GROUP BY source_system ORDER BY source_system"
        )
        return {"sources": rows}

    @app.route("GET", "/batches")
    def batches(req: Request):
        limit = req.q_int("limit", 20, minimum=1, maximum=200)
        return {"batches": store.db.q("SELECT * FROM batches ORDER BY received_at DESC LIMIT ?", (limit,))}

    @app.route("GET", "/batches/(?P<batch_id>[A-Za-z0-9_.-]+)")
    def batch(req: Request, batch_id: str):
        row = store.db.q1("SELECT * FROM batches WHERE id=?", (batch_id,))
        if not row:
            raise HTTPError(404, "批次不存在")
        row["records"] = store.db.q(
            "SELECT id, source_system, source_id, revision, raw_sha256, active FROM records WHERE batch_id=? ORDER BY id",
            (batch_id,),
        )
        return row

    @app.route("GET", "/export.csv")
    def export(req: Request):
        rows = store.db.q(
            "SELECT id, source_system, source_id, revision, raw_value, raw_sha256, received_at, batch_id"
            " FROM records WHERE active=1 ORDER BY id"
        )
        header = ["raw_id", "source_system", "source_id", "revision", "raw_value", "raw_escaped", "raw_sha256", "received_at", "batch_id"]
        out_rows = [
            (r["id"], r["source_system"], r["source_id"], r["revision"], r["raw_value"],
             r["raw_value"].encode("unicode_escape").decode("ascii"), r["raw_sha256"], r["received_at"], r["batch_id"])
            for r in rows
        ]
        return csv_response(header, out_rows, "raw_active_records.csv")

    return app
