#!/usr/bin/env python3
"""Offline contract tests for scripts/run-host-boundary.py."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EVENTCTL = ROOT / "scripts" / "eventctl.py"
BOUNDARY = ROOT / "scripts" / "run-host-boundary.py"
EVENT = "KXEARNINGSMENTIONBOUNDARY-26AUG12-PHONE"
HOST = "earshot-useast1"
FP = "sha256:" + "a" * 64


class BoundaryFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="run-host-boundary-test."))
        self.local = self.tmp / "local"
        self.remote = self.tmp / "remote"
        for repo in (self.local, self.remote):
            (repo / "scripts").mkdir(parents=True)
            (repo / "configs").mkdir()
            shutil.copy2(EVENTCTL, repo / "scripts" / "eventctl.py")
            shutil.copy2(BOUNDARY, repo / "scripts" / "run-host-boundary.py")
            (repo / "configs" / "event.toml").write_text(
                f'''[event]\nid = "{EVENT}"\nstarts_at = "2026-08-12T21:00:00Z"\n\n'''
                '[[sources]]\nkind = "twilio_stream"\nbind = "127.0.0.1:8081"\n\n'
                '[[keywords]]\nmarket_ticker = "KXEARNINGSMENTIONBOUNDARY-26AUG12-FOO"\nphrase = "foo"\n'
            )
            (repo / "configs" / "event.env").write_text(
                'CONFIG_PATH="configs/event.toml"\nEVENT_DATE="2026-08-12"\n'
            )
        self.cli(
            self.local,
            "init", "--config", "configs/event.toml",
            "--event-env", "configs/event.env",
            "--host-id", HOST, "--account-fingerprint", FP,
        )
        self.local_run = next((self.local / "var" / "runs" / EVENT).iterdir())
        for transition in (
            "prepared", "admission_resolved", "study_completed", "preflight_passed"
        ):
            self.record(self.local, self.local_run, transition)
        self.run_id = self.local_run.name
        self.remote_run = self.remote / "var" / "runs" / EVENT / self.run_id

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def cli(self, repo: Path, *args: str, check: bool = True):
        proc = subprocess.run(
            [sys.executable, str(repo / "scripts" / "eventctl.py"), *args],
            cwd=repo,
            env={**os.environ, "EARSHOT_REPO": str(repo)},
            text=True,
            capture_output=True,
        )
        if check and proc.returncode:
            self.fail(f"eventctl {' '.join(args)} failed: {proc.stderr}{proc.stdout}")
        return proc

    def record(self, repo: Path, run: Path, transition: str):
        self.cli(repo, "record", "--run-dir", str(run), "--transition", transition)

    def boundary(self, repo: Path, incoming: Path, destination: Path, check=True, check_only=False):
        extra = ["--check-only"] if check_only else []
        proc = subprocess.run(
            [
                sys.executable, str(repo / "scripts" / "run-host-boundary.py"),
                "reconcile", "--repo", str(repo), "--incoming", str(incoming),
                "--destination", str(destination), "--expected-host", HOST,
                *extra,
            ],
            cwd=repo,
            env={**os.environ, "EARSHOT_REPO": str(repo)},
            text=True,
            capture_output=True,
        )
        if check and proc.returncode:
            self.fail(f"boundary failed: {proc.stderr}{proc.stdout}")
        return proc

    def test_check_only_proves_absent_publish_without_writing(self):
        result = json.loads(
            self.boundary(
                self.remote, self.local_run, self.remote_run, check_only=True
            ).stdout
        )
        self.assertTrue(result["check_only"])
        self.assertEqual(result["action"], "would-publish")
        self.assertFalse(self.remote_run.exists())

    def test_absent_publish_and_dispatch_retry_accept_remote_extension(self):
        result = json.loads(self.boundary(self.remote, self.local_run, self.remote_run).stdout)
        self.assertEqual(result["relation"], "absent")
        self.assertEqual(result["action"], "published")
        self.record(self.remote, self.remote_run, "scheduled")
        before = (self.remote_run / "control" / "receipts.jsonl").read_bytes()
        retry = json.loads(self.boundary(self.remote, self.local_run, self.remote_run).stdout)
        self.assertEqual(retry["relation"], "incoming_prefix_destination")
        self.assertEqual((self.remote_run / "control" / "receipts.jsonl").read_bytes(), before)

    def test_remote_extension_advances_local_and_local_tail_is_idempotent(self):
        self.boundary(self.remote, self.local_run, self.remote_run)
        for transition in (
            "scheduled", "capture_ready", "launched", "source_attached",
            "supervising", "draining", "completed",
        ):
            self.record(self.remote, self.remote_run, transition)
        (self.remote_run / "report" / "report.json").write_text('{"finalized":true}\n')
        (self.remote_run / "orders" / "order-evidence.jsonl").write_text('{"sent":true}\n')
        result = json.loads(self.boundary(self.local, self.remote_run, self.local_run).stdout)
        self.assertEqual(result["relation"], "destination_prefix_incoming")
        self.assertEqual(result["action"], "advanced")
        self.assertEqual(
            (self.local_run / "control" / "receipts.jsonl").read_bytes(),
            (self.remote_run / "control" / "receipts.jsonl").read_bytes(),
        )
        self.assertTrue((self.local_run / "report" / "report.json").is_file())
        self.record(self.local, self.local_run, "evidence_pulled")
        before = (self.local_run / "control" / "receipts.jsonl").read_bytes()
        repeat = json.loads(self.boundary(self.local, self.remote_run, self.local_run).stdout)
        self.assertEqual(repeat["relation"], "incoming_prefix_destination")
        self.assertEqual((self.local_run / "control" / "receipts.jsonl").read_bytes(), before)

    def test_append_only_partial_evidence_may_grow_with_ledger_extension(self):
        self.boundary(self.remote, self.local_run, self.remote_run)
        (self.local_run / "capture" / "audio.raw").write_bytes(b"abc")
        (self.remote_run / "capture" / "audio.raw").write_bytes(b"abcdef")
        self.record(self.remote, self.remote_run, "scheduled")
        self.boundary(self.local, self.remote_run, self.local_run)
        self.assertEqual((self.local_run / "capture" / "audio.raw").read_bytes(), b"abcdef")

    def test_divergent_evidence_refuses_before_receipt_mutation(self):
        self.boundary(self.remote, self.local_run, self.remote_run)
        (self.local_run / "control" / "access-plan.json").write_text("local\n")
        (self.remote_run / "control" / "access-plan.json").write_text("remote\n")
        self.record(self.remote, self.remote_run, "scheduled")
        before = (self.local_run / "control" / "receipts.jsonl").read_bytes()
        proc = self.boundary(self.local, self.remote_run, self.local_run, check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("file bytes differ", proc.stderr)
        self.assertEqual((self.local_run / "control" / "receipts.jsonl").read_bytes(), before)

    def test_authority_sidecar_never_crosses_the_boundary(self):
        """E11: control/authority.json is host-private bookkeeping. Publish
        must not carry it; a stray copy on the other side must neither be
        compared ("file bytes differ") nor counted as unexplained bytes."""
        sidecar = "control/authority.json"
        self.cli(self.local, "authority", "--run-dir", str(self.local_run),
                 "--transfer-to", HOST, "--by", "t")
        local_bytes = (self.local_run / sidecar).read_bytes()
        result = json.loads(self.boundary(self.remote, self.local_run, self.remote_run).stdout)
        self.assertEqual(result["action"], "published")
        self.assertFalse((self.remote_run / sidecar).exists())
        # the box extends its copy and grows a DIFFERENT stray sidecar
        self.record(self.remote, self.remote_run, "scheduled")
        (self.remote_run / sidecar).write_text('{"stray": true}\n')
        check = json.loads(self.boundary(
            self.local, self.remote_run, self.local_run, check_only=True).stdout)
        self.assertEqual(check["action"], "would-advance")
        result = json.loads(self.boundary(self.local, self.remote_run, self.local_run).stdout)
        self.assertEqual(result["relation"], "destination_prefix_incoming")
        self.assertEqual(result["action"], "advanced")
        self.assertEqual((self.local_run / sidecar).read_bytes(), local_bytes)
        self.assertEqual(
            (self.local_run / "control" / "receipts.jsonl").read_bytes(),
            (self.remote_run / "control" / "receipts.jsonl").read_bytes(),
        )
        # a local tail past the box's copy: the stray must not read as
        # "files absent from incoming" in the reverse direction either
        self.cli(self.local, "authority", "--run-dir", str(self.local_run),
                 "--returned-from", HOST, "--by", "t")
        self.record(self.local, self.local_run, "blocked")
        (self.remote_run / sidecar).unlink()
        back = json.loads(self.boundary(self.remote, self.local_run, self.remote_run).stdout)
        self.assertEqual(back["relation"], "destination_prefix_incoming")
        self.assertFalse((self.remote_run / sidecar).exists())

    def test_wrong_host_and_symlink_refuse(self):
        wrong = subprocess.run(
            [
                sys.executable, str(BOUNDARY), "reconcile", "--repo", str(self.remote),
                "--incoming", str(self.local_run), "--destination", str(self.remote_run),
                "--expected-host", "wrong-host",
            ],
            cwd=self.remote,
            env={**os.environ, "EARSHOT_REPO": str(self.remote)},
            text=True,
            capture_output=True,
        )
        self.assertEqual(wrong.returncode, 1)
        self.assertFalse(self.remote_run.exists())
        link = self.local_run / "report" / "bad-link"
        link.symlink_to("/tmp")
        proc = self.boundary(self.remote, self.local_run, self.remote_run, check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("symlink in run tree", proc.stderr)
        self.assertFalse(self.remote_run.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
