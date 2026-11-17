"""Shared plumbing: SQLite, HTTP server/client, CSV helpers.

The three services are intentionally thin and communicate only over HTTP.
No service reaches into another service's database.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable

LOG = logging.getLogger("normlink")

MAX_BODY = 128 * 1024 * 1024


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)


def sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class HTTPError(Exception):
    def __init__(self, status: int, message: str, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


class Response:
    def __init__(self, status: int = 200, body: bytes = b"", content_type: str = "application/json; charset=utf-8", headers: dict[str, str] | None = None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or {}


def json_response(obj: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(status, json_dumps(obj).encode("utf-8") + b"\n", "application/json; charset=utf-8", headers)


def text_response(text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> Response:
    return Response(status, text.encode("utf-8"), content_type)


def binary_response(data: bytes, content_type: str = "application/octet-stream", filename: str | None = None) -> Response:
    headers = {}
    if filename:
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return Response(200, data, content_type, headers)


def csv_response(header: Iterable[str], rows: Iterable[Iterable[Any]], filename: str) -> Response:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(list(header))
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    # BOM keeps Chinese/other scripts readable when opened in spreadsheet apps.
    data = ("﻿" + buf.getvalue()).encode("utf-8")
    return binary_response(data, "text/csv; charset=utf-8", filename)


class Request:
    def __init__(self, method: str, path: str, query: dict[str, list[str]], headers: dict[str, str], body: bytes):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body

    def q(self, name: str, default: str | None = None) -> str | None:
        vals = self.query.get(name)
        return vals[0] if vals else default

    def q_int(self, name: str, default: int | None = None, minimum: int | None = None, maximum: int | None = None) -> int | None:
        raw = self.q(name)
        if raw is None or raw == "":
            return default
        try:
            val = int(raw)
        except ValueError:
            raise HTTPError(400, f"参数 {name} 必须是整数")
        if minimum is not None and val < minimum:
            raise HTTPError(400, f"参数 {name} 不能小于 {minimum}")
        if maximum is not None and val > maximum:
            raise HTTPError(400, f"参数 {name} 不能大于 {maximum}")
        return val

    def json(self) -> Any:
        if not self.body:
            raise HTTPError(400, "请求体为空")
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPError(400, "请求体不是合法 JSON", str(exc))

    def text(self, encoding: str = "utf-8") -> str:
        try:
            return self.body.decode(encoding)
        except (LookupError, UnicodeDecodeError) as exc:
            raise HTTPError(400, f"无法以 {encoding} 解码请求体", str(exc))


class App:
    def __init__(self, name: str):
        self.name = name
        self.routes: list[tuple[str, re.Pattern[str], Callable[[Request], Response]]] = []
        self.started_at = now_iso()

    def route(self, method: str, pattern: str, fn: Callable[[Request], Any] | None = None):
        compiled = re.compile("^" + pattern + "$")

        def register(fn: Callable[[Request], Any]):
            self.routes.append((method.upper(), compiled, fn))
            return fn

        return register(fn) if fn is not None else register

    def dispatch(self, req: Request) -> Response:
        path_matched = False
        for method, pattern, fn in self.routes:
            match = pattern.match(req.path)
            if not match:
                continue
            path_matched = True
            if method != req.method and not (method == "GET" and req.method == "HEAD"):
                continue
            try:
                result = fn(req, **match.groupdict())
                if isinstance(result, Response):
                    return result
                return json_response(result)
            except HTTPError:
                raise
            except Exception as exc:  # pragma: no cover - defensive boundary
                LOG.exception("unhandled error in %s %s", req.method, req.path)
                raise HTTPError(500, "服务内部错误", f"{type(exc).__name__}: {exc}")
        if path_matched:
            raise HTTPError(405, "方法不允许")
        raise HTTPError(404, "未找到接口")

    def make_server(self, host: str, port: int) -> ThreadingHTTPServer:
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self):
                parsed = urllib.parse.urlsplit(self.path)
                path = urllib.parse.unquote(parsed.path)
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    self._send(HTTPError(413, "请求体过大"))
                    return
                body = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                req = Request(self.command, path, query, headers, body)
                try:
                    resp = app.dispatch(req)
                except HTTPError as exc:
                    payload = {"error": exc.message, "service": app.name}
                    if exc.detail is not None:
                        payload["detail"] = exc.detail
                    resp = json_response(payload, exc.status)
                except Exception as exc:  # pragma: no cover
                    LOG.exception("dispatch failure")
                    resp = json_response({"error": "服务内部错误", "detail": str(exc), "service": app.name}, 500)
                self._send(resp)

            def _send(self, resp: Response):
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.content_type)
                self.send_header("Content-Length", str(len(resp.body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                for key, value in resp.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(resp.body)

            def do_GET(self):  # noqa: N802
                self._handle()

            def do_HEAD(self):  # noqa: N802
                self._handle()

            def do_POST(self):  # noqa: N802
                self._handle()

            def do_PUT(self):  # noqa: N802
                self._handle()

            def do_DELETE(self):  # noqa: N802
                self._handle()

            def do_OPTIONS(self):  # noqa: N802
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, fmt, *args):  # quiet by default
                LOG.debug("%s - %s", self.address_string(), fmt % args)

        return ThreadingHTTPServer((host, port), Handler)


class DB:
    """Small thread-local SQLite wrapper (WAL, autocommit, explicit transactions)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn().execute(sql, tuple(args)).fetchall()]

    def q1(self, sql: str, args: Iterable[Any] = ()) -> dict[str, Any] | None:
        row = self.conn().execute(sql, tuple(args)).fetchone()
        return dict(row) if row else None

    def scalar(self, sql: str, args: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.conn().execute(sql, tuple(args)).fetchone()
        return row[0] if row is not None else default

    def execute(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn().execute(sql, tuple(args))

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        self.conn().executemany(sql, seq)

    def executescript(self, script: str) -> None:
        self.conn().executescript(script)

    @contextmanager
    def tx(self):
        conn = self.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def meta_get(self, key: str, default: Any = None) -> Any:
        try:
            row = self.q1("SELECT value FROM meta WHERE key=?", (key,))
        except sqlite3.OperationalError:
            return default
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def meta_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json_dumps(value)),
        )


class RemoteError(Exception):
    def __init__(self, status: int, message: str, detail: Any = None, url: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail
        self.url = url


def http_request(url: str, method: str = "GET", payload: Any = None, raw_body: bytes | None = None,
                 content_type: str = "application/json", timeout: float = 60.0) -> Any:
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json_dumps(payload).encode("utf-8")
        headers["Content-Type"] = content_type
    elif raw_body is not None:
        data = raw_body
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return json.loads(body.decode("utf-8"))
            return body
    except urllib.error.HTTPError as exc:
        body = exc.read()
        detail = None
        message = f"HTTP {exc.code}"
        try:
            parsed = json.loads(body.decode("utf-8"))
            message = parsed.get("error", message)
            detail = parsed.get("detail")
        except Exception:
            message = body.decode("utf-8", "replace")[:500] or message
        raise RemoteError(exc.code, message, detail, url)
    except urllib.error.URLError as exc:
        raise RemoteError(502, f"无法连接 {url}: {exc.reason}", None, url)


def paginate(items: list[Any], page: int, page_size: int) -> dict[str, Any]:
    return {"items": items, "page": page, "page_size": page_size, "total": len(items)}


def start_background(fn: Callable[[], None], name: str) -> threading.Thread:
    def runner():
        try:
            fn()
        except Exception:
            LOG.exception("background task %s failed", name)

    thread = threading.Thread(target=runner, name=name, daemon=True)
    thread.start()
    return thread
