"""Integration tests that start the three real HTTP services on free ports.

Covers linkage invariants that are hard to prove through the UI:
generation pinning, collision isolation, adjudication matching, release
immutability and build cancellation.
"""
from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app import server as server_mod
from app.link_service import LinkStore
from app.common import http_request, RemoteError


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stack:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ports = {name: free_port() for name in ("raw", "norm", "link")}
        self.servers = []
        self.threads = []
        raw_base = f"http://127.0.0.1:{self.ports['raw']}"
        norm_base = f"http://127.0.0.1:{self.ports['norm']}"
        specs = {
            "raw": (raw_base, None, None),
            "normalizer": (norm_base, None, None),
            "linkage": (f"http://127.0.0.1:{self.ports['link']}", raw_base, norm_base),
        }
        port_by_name = {"raw": self.ports["raw"], "normalizer": self.ports["norm"], "linkage": self.ports["link"]}
        for name, (base, rb, nb) in specs.items():
            app = server_mod.build(name, str(self.root / name), rb or "", nb or "")
            srv = app.make_server("127.0.0.1", port_by_name[name])
            self.servers.append(srv)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            self.threads.append(t)
        self.raw = raw_base
        self.norm = norm_base
        self.link = f"http://127.0.0.1:{self.ports['link']}"

    def close(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()
        self.tmp.cleanup()


class LinkageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = Stack()

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()

    # helpers -----------------------------------------------------------
    def ingest(self, rows):
        return http_request(self.stack.raw + "/ingest", "POST", {"rows": rows})

    def build(self, mode="full", publish=True, generation_id=None):
        job = http_request(self.stack.link + "/builds", "POST",
                           {"mode": mode, "publish": publish, "generation_id": generation_id})
        deadline = time.time() + 60
        while time.time() < deadline:
            job = http_request(self.stack.link + f"/jobs/{job['id']}")
            if job["status"] not in {"queued", "running", "cancelling"}:
                return job
            time.sleep(0.05)
        self.fail("build did not finish")

    def publish_steps(self, label, steps):
        gen = http_request(self.stack.norm + "/rules/generations", "POST",
                           {"label": label, "steps": steps})
        http_request(self.stack.norm + f"/rules/generations/{gen['generation_id']}/publish", "POST", {})
        return gen

    # tests -------------------------------------------------------------
    def test_a_full_pipeline_and_collision_isolation(self):
        rows = [
            {"source_system": "ERP", "source_id": "1", "raw_value": "ACME GmbH"},
            {"source_system": "CRM", "source_id": "1", "raw_value": "ＡＣＭＥ　ＧｍｂＨ"},
            {"source_system": "CRM", "source_id": "2", "raw_value": "ACME​ GmbH"},
            {"source_system": "MDM", "source_id": "9", "raw_value": "Other Ltd"},
            {"source_system": "MDM", "source_id": "10", "raw_value": "   "},
        ]
        self.ingest(rows)
        job = self.build()
        self.assertEqual(job["status"], "completed", job.get("error"))
        rel = http_request(self.stack.link + f"/releases/{job['release_id']}")
        self.assertEqual(rel["item_count"], 5)
        # three raw spellings of the same key collide -> conflict class, not cleaned
        conflicts = http_request(self.stack.link + f"/releases/{job['release_id']}/conflicts")
        self.assertEqual(conflicts["total"], 1, conflicts)
        self.assertEqual(conflicts["items"][0]["distinct_raw_count"], 3)
        self.assertEqual(len(conflicts["items"][0]["members"]), 3)
        # every row is accounted for exactly once
        members = http_request(self.stack.link + f"/releases/{job['release_id']}/report.json")
        self.assertEqual(len(members["members"]), 5)
        self.assertEqual(len({m["raw_id"] for m in members["members"]}), 5)
        # whitespace-only value is its own empty class
        classes = http_request(self.stack.link + f"/releases/{job['release_id']}/classes?status=empty")
        self.assertEqual(classes["total"], 1)
        self.assertEqual(classes["items"][0]["normalized_key"], "")

    def test_b_historical_report_frozen_when_rules_change(self):
        active = http_request(self.stack.link + "/releases/active")
        before = http_request(self.stack.link + f"/releases/{active['id']}/report.json")
        self.publish_steps("v2 test", [
            {"op": "unicode_nfkc"}, {"op": "strip_format"}, {"op": "trim"},
            {"op": "collapse_ws"}, {"op": "casefold"}, {"op": "strip_punct"},
        ])
        after = http_request(self.stack.link + f"/releases/{active['id']}/report.json")
        self.assertEqual(before["report_body_sha256"], after["report_body_sha256"])
        self.assertEqual(before["release"]["generation_id"], after["release"]["generation_id"])

    def test_c_incremental_rejected_after_generation_change(self):
        self.publish_steps("v3 test", [{"op": "trim"}, {"op": "casefold"}])
        with self.assertRaises(RemoteError) as ctx:
            http_request(self.stack.link + "/builds", "POST", {"mode": "incremental", "publish": True})
        self.assertIn("全量", ctx.exception.message)

    def test_d_adjudication_requires_exact_raw_membership(self):
        # Publish a generation that deliberately merges the spellings used here,
        # so this test does not depend on whichever generation an earlier test left.
        self.publish_steps("d-merging", [
            {"op": "unicode_nfkc"}, {"op": "strip_format"}, {"op": "trim"},
            {"op": "collapse_ws"}, {"op": "casefold"},
        ])
        self.ingest([
            {"source_system": "D1", "source_id": "1", "raw_value": "Acme GmbH"},
            {"source_system": "D2", "source_id": "1", "raw_value": "ＡＣＭＥ　ＧｍｂＨ"},
        ])
        job = self.build(publish=True)
        self.assertEqual(job["status"], "completed", job.get("error"))
        rel = job["release_id"]
        conflicts = http_request(self.stack.link + f"/releases/{rel}/conflicts")
        target = next(c for c in conflicts["items"] if c["normalized_key"] == "acme gmbh")
        adj = http_request(self.stack.link + "/adjudications", "POST", {
            "release_id": rel, "class_id": target["class_id"], "action": "accept_merge", "operator": "test"})
        self.assertEqual(adj["status"], "active")
        # add a new distinct raw value that lands on the same key
        self.ingest([{"source_system": "NEW", "source_id": "x", "raw_value": "Acme  GmbH"}])
        job2 = self.build(mode="incremental", publish=True)
        self.assertEqual(job2["status"], "completed", job2.get("error"))
        gaps = http_request(self.stack.link + f"/releases/{job2['release_id']}/adjudication-gaps")
        self.assertTrue(any("复核" in g["reason"] for g in gaps["gaps"]))
        # the new value must not be silently folded into the old adjudicated class
        merged = http_request(self.stack.link + f"/releases/{job2['release_id']}/classes?status=adjudicated")
        for item in merged["items"]:
            self.assertNotEqual(item["distinct_raw_count"], 3)

    def test_e_cancellation_leaves_active_release_untouched(self):
        active_before = http_request(self.stack.link + "/releases/active")
        cancelled = False
        for _ in range(5):
            job = http_request(self.stack.link + "/builds", "POST", {"mode": "full", "publish": True})
            result = http_request(self.stack.link + f"/jobs/{job['id']}/cancel", "POST", {})
            if result["status"] == "cancelled":
                cancelled = True
                break
        if cancelled:
            active_after = http_request(self.stack.link + "/releases/active")
            self.assertEqual(active_before["id"], active_after["id"])
            releases = http_request(self.stack.link + "/releases?limit=100")["releases"]
            self.assertFalse(any(r["status"] == "staging" for r in releases))

    def test_f_regeneration_rejects_non_current_generation(self):
        gens = http_request(self.stack.norm + "/rules")["generations"]
        superseded = next(g for g in gens if g["status"] == "superseded")
        with self.assertRaises(RemoteError) as ctx:
            http_request(self.stack.link + "/builds", "POST", {"mode": "full", "generation_id": superseded["generation_id"]})
        self.assertIn("世代", ctx.exception.message)

    def test_g_linkage_rejects_unknown_activation_hash(self):
        rel = http_request(self.stack.link + "/releases/active")
        with self.assertRaises(RemoteError) as ctx:
            http_request(self.stack.link + f"/releases/{rel['id']}/activate", "POST",
                         {"expected_generation_hash": "f" * 64})
        self.assertEqual(ctx.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
