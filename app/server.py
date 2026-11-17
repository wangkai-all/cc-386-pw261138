"""Process entrypoint for one of the three services."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from .common import App
from .link_service import build_app as build_link
from .normalizer_service import build_app as build_normalizer
from .raw_service import build_app as build_raw


def build(service: str, data_dir: str, raw_base: str, normalizer_base: str) -> App:
    if service == "raw":
        return build_raw(data_dir)
    if service == "normalizer":
        return build_normalizer(data_dir)
    if service == "linkage":
        return build_link(data_dir, raw_base, normalizer_base)
    raise SystemExit(f"unknown service {service!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NormLink service")
    parser.add_argument("service", choices=["raw", "normalizer", "linkage"])
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--raw-base", default=os.environ.get("RAW_BASE", "http://127.0.0.1:8801"))
    parser.add_argument("--normalizer-base", default=os.environ.get("NORMALIZER_BASE", "http://127.0.0.1:8802"))
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    Path(args.data_dir).mkdir(parents=True, exist_ok=True)
    app = build(args.service, args.data_dir, args.raw_base, args.normalizer_base)
    server = app.make_server(args.host, args.port)
    logging.getLogger("normlink").info("%s listening on http://%s:%d (data=%s)",
                                       args.service, args.host, args.port, args.data_dir)

    def stop(signum, frame):
        logging.getLogger("normlink").info("shutting down on signal %s", signum)
        # shutdown() waits for serve_forever() to return; calling it inline from a
        # signal handler (i.e. on the serve_forever thread) would deadlock.
        threading.Thread(target=server.shutdown, name="shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        logging.getLogger("normlink").info("%s stopped", args.service)
    return 0


if __name__ == "__main__":
    sys.exit(main())
