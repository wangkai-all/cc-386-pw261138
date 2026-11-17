#!/usr/bin/env python3
"""One-command demo: fresh empty data dir -> ingest -> rules -> release -> reports.

Leaves the stack running and prints the URLs plus downloaded evidence files.
    python3 scripts/demo.py [--data-dir DIR]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from e2e import call, wait_job  # noqa: E402


RULES_V1 = [
    {"op": "unicode_nfkc", "note": "全角与兼容字符折叠"},
    {"op": "strip_format", "note": "移除零宽/方向控制符"},
    {"op": "trim", "note": "去除首尾空白"},
    {"op": "collapse_ws", "note": "折叠连续空白"},
    {"op": "casefold", "note": "Unicode 大小写折叠"},
    {"op": "strip_diacritics", "note": "去除变音符号（Müller → Muller）"},
]


def stop_stack(proc: subprocess.Popen) -> None:
    """Stop dev.py and every service it spawned, even mid-interrupt."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)

    def on_signal(signum, frame):
        # SIGTERM should run the same cleanup path as Ctrl+C.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--base", default="http://127.0.0.1:8800")
    args = ap.parse_args()
    data_dir = Path(args.data_dir).resolve()
    base = args.base

    # Own session/process group: Ctrl+C reaches this script, and cleanup can take
    # the whole stack down with one signal instead of leaving orphaned services.
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "dev.py"), "--data-dir", str(data_dir), "--clean"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print(f"starting stack (data dir {data_dir})…")
    for _ in range(120):
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=1):
                break
        except Exception:
            time.sleep(0.25)
    else:
        stop_stack(proc)
        raise SystemExit("stack did not start")

    try:
        seed = ROOT / "seed" / "raw_rows.csv"
        if not seed.exists():
            subprocess.run([sys.executable, str(ROOT / "scripts" / "make_seed.py")], check=True)
        rows = list(csv.DictReader(seed.open(encoding="utf-8")))
        res = call(base, "/api/raw/ingest", "POST", {"rows": [
            {"source_system": r["source_system"], "source_id": r["source_id"], "raw_value": r["raw_value"]}
            for r in rows
        ], "note": "demo"})
        print(f"ingested {res['inserted']} raw values from {len({r['source_system'] for r in rows})} systems")

        gen = call(base, "/api/norm/rules/generations", "POST",
                   {"label": "演示规则 v1", "steps": RULES_V1, "note": "demo"})
        call(base, f"/api/norm/rules/generations/{gen['generation_id']}/publish", "POST", {})
        print(f"published rule generation #{gen['generation_id']}")

        job = call(base, "/api/link/builds", "POST", {"mode": "full", "publish": True})
        job = wait_job(base, job["id"])
        if job["status"] != "completed":
            raise SystemExit(f"build failed: {job.get('error')}")
        rel = call(base, f"/api/link/releases/{job['release_id']}")
        print(f"release {rel['id']}: {rel['class_count']} classes, "
              f"{rel['conflict_class_count']} conflict classes, {rel['item_count']} members")

        out = ROOT / "artifacts"
        out.mkdir(exist_ok=True)
        downloads = {
            "report.json": f"/api/link/releases/{rel['id']}/report.json",
            "classes.csv": f"/api/link/releases/{rel['id']}/classes.csv",
            "members.csv": f"/api/link/releases/{rel['id']}/members.csv",
            "evidence.csv": f"/api/norm/runs/{rel['normalizer_run_id']}/evidence.csv",
            "mapping.csv": f"/api/norm/runs/{rel['normalizer_run_id']}/mapping.csv",
            "rules.json": f"/api/norm/runs/{rel['normalizer_run_id']}/rules.json",
        }
        for name, path in downloads.items():
            with urllib.request.urlopen(base + path, timeout=60) as resp:
                (out / name).write_bytes(resp.read())
        (out / "release.json").write_text(json.dumps(rel, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"artifacts written to {out}")
        print(f"open {base}/ to explore")
        print("press Ctrl+C to stop the stack")
        while proc.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            stop_stack(proc)
        except KeyboardInterrupt:
            # A second Ctrl+C must not skip cleanup.
            stop_stack(proc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
