#!/usr/bin/env python3
"""Launch all three services plus the operator UI with one command.

    python3 scripts/dev.py [--data-dir DIR] [--clean]

Ports default to 8801 (raw), 8802 (normalizer), 8803 (linkage), 8800 (UI).
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

SERVICES = [
    ("raw", 8801, ["--data-dir", "{data}/raw"]),
    ("normalizer", 8802, ["--data-dir", "{data}/normalizer"]),
    ("linkage", 8803, ["--data-dir", "{data}/linkage", "--raw-base", "http://127.0.0.1:8801",
                       "--normalizer-base", "http://127.0.0.1:8802"]),
]


def wait_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--clean", action="store_true", help="delete existing data dir first")
    args = parser.parse_args()
    data = Path(args.data_dir).resolve()
    if args.clean and data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True, exist_ok=True)

    procs: list[subprocess.Popen] = []

    def on_signal(signum, frame):
        # Raise instead of dying outright so the finally block below can stop children.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    env = dict(os.environ, PYTHONPATH=str(ROOT))
    try:
        for name, port, extra in SERVICES:
            cmd = [PY, "-m", "app.server", name, "--port", str(port),
                   *[a.format(data=data) for a in extra]]
            proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env)
            procs.append(proc)
            if not wait_port(port):
                print(f"service {name} failed to start", file=sys.stderr)
                return 1
        ui_cmd = [PY, "scripts/ui_server.py", "--port", "8800",
                  "--raw-base", "http://127.0.0.1:8801",
                  "--normalizer-base", "http://127.0.0.1:8802",
                  "--linkage-base", "http://127.0.0.1:8803"]
        procs.append(subprocess.Popen(ui_cmd, cwd=str(ROOT), env=env))
        if not wait_port(8800):
            print("ui failed to start", file=sys.stderr)
            return 1
        print("NormLink is running:")
        print("  UI        http://127.0.0.1:8800/")
        print("  raw       http://127.0.0.1:8801/health")
        print("  normalize http://127.0.0.1:8802/health")
        print("  linkage   http://127.0.0.1:8803/health")
        print("Press Ctrl+C to stop.")
        while True:
            for proc in procs:
                if proc.poll() is not None:
                    print("a service exited; shutting down", file=sys.stderr)
                    return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
