#!/usr/bin/env python3
"""Operator UI: serves the single-page app and a same-origin proxy to the three services.

Proxying keeps the browser on one origin (no CORS surprises) while the services
themselves stay separate processes with separate databases.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "ui"

ROUTES = {
    "/api/raw/": ("raw", ""),
    "/api/norm/": ("normalizer", ""),
    "/api/link/": ("linkage", ""),
}

SERVICE_BASES: dict[str, str] = {}


def proxy(handler: BaseHTTPRequestHandler, service: str, path: str) -> None:
    base = urllib.parse.urlsplit(SERVICE_BASES[service])
    length = int(handler.headers.get("Content-Length") or 0)
    body = handler.rfile.read(length) if length else None
    headers = {k: v for k, v in handler.headers.items()
               if k.lower() in {"content-type", "accept"} and k.lower() != "host"}
    conn = http.client.HTTPConnection(base.hostname, base.port, timeout=900)
    try:
        conn.request(handler.command, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        handler.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() in {"connection", "transfer-encoding", "content-length", "access-control-allow-origin"}:
                continue
            handler.send_header(key, value)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(data)
    except OSError as exc:
        payload = json.dumps({"error": f"服务 {service} 不可用: {exc}"}, ensure_ascii=False).encode()
        handler.send_response(502)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        self._dispatch()

    def do_POST(self):  # noqa: N802
        self._dispatch()

    def do_PUT(self):  # noqa: N802
        self._dispatch()

    def do_DELETE(self):  # noqa: N802
        self._dispatch()

    def _dispatch(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        for prefix, (service, _) in ROUTES.items():
            if path.startswith(prefix):
                target = path[len(prefix) - 1:]  # keep leading slash
                if parsed.query:
                    target += "?" + parsed.query
                proxy(self, service, target)
                return
        if path == "/healthz":
            self._bytes(b'{"status":"ok"}', "application/json")
            return
        rel = path.lstrip("/") or "index.html"
        if ".." in rel.split("/"):
            self._bytes(b'{"error":"bad path"}', "application/json", 400)
            return
        file = (STATIC / rel).resolve()
        if not str(file).startswith(str(STATIC.resolve())) or not file.is_file():
            file = STATIC / "index.html"
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".json": "application/json; charset=utf-8",
        }.get(file.suffix, "application/octet-stream")
        self._bytes(file.read_bytes(), ctype)

    def _bytes(self, data: bytes, ctype: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self):  # noqa: N802
        self._dispatch()

    def log_message(self, fmt, *args):
        if os.environ.get("UI_ACCESS_LOG"):
            super().log_message(fmt, *args)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--raw-base", default="http://127.0.0.1:8801")
    parser.add_argument("--normalizer-base", default="http://127.0.0.1:8802")
    parser.add_argument("--linkage-base", default="http://127.0.0.1:8803")
    args = parser.parse_args()
    SERVICE_BASES.update(raw=args.raw_base, normalizer=args.normalizer_base, linkage=args.linkage_base)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"UI on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
