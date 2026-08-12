#!/usr/bin/env python3
"""Offline contract tests for scripts/schedule-proof.py."""

from __future__ import annotations

import datetime as dt
import hashlib
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
SCHEDULE_PROOF = ROOT / "scripts" / "schedule-proof.py"
HOST = "earshot-test-host"
FP = "sha256:" + "a" * 64


class ScheduleProofTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="schedule-proof.")
        self.repo = Path(self.temp.name).resolve()
        (self.repo / "scripts").mkdir()
        (self.repo / "configs").mkdir()
        shutil.copy2(EVENTCTL, self.repo / "scripts" / "eventctl.py")
        shutil.copy2(SCHEDULE_PROOF, self.repo / "scripts" / "schedule-proof.py")
        (self.repo / "scripts" / "event-autorun.sh").write_text("#!/bin/sh\n")
        (self.repo / "scripts" / "arm-on-url.sh").write_text("#!/bin/sh\n")
        self.home = self.repo / "home"
        self.home.mkdir()
        self.account = self.home / ".earshot" / "account.env"
        self.account.parent.mkdir()
        self.account.write_text("EARSHOT_ACCOUNT_FIXTURE=1\n")
        self.env = {
            **os.environ,
            "EARSHOT_REPO": str(self.repo),
            "HOME": str(self.home),
        }

    def tearDown(self):
        self.temp.cleanup()

    def eventctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, str(self.repo / "scripts" / "eventctl.py"), *args],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
        )
        if check and proc.returncode:
            self.fail(f"eventctl failed: {proc.stderr}{proc.stdout}")
        return proc

    def make_run(self, lane: str = "phone") -> tuple[Path, dict]:
        event = f"KXEARNINGSMENTIONSCHEDULE-26AUG20-{lane.upper()}"
        start = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=8)
        source = (
            'kind = "twilio_stream"\nbind = "127.0.0.1:8080"'
            if lane == "phone"
            else 'kind = "pipe"\ncmd = ["ffmpeg", "-i", "$HOME/test-stream.url"]'
        )
        cfg = self.repo / "configs" / f"{lane}.toml"
        cfg.write_text(
            f'[event]\nid = "{event}"\nstarts_at = "{start:%Y-%m-%dT%H:%M:%SZ}"\n\n'
            f"[[sources]]\n{source}\n\n"
            f'[[keywords]]\nmarket_ticker = "{event}-FOO"\nphrase = "foo"\n'
        )
        env = self.repo / "configs" / f"{lane}.env"
        account = 'ACCOUNT_ENV="$HOME/.earshot/account.env"\n' if lane == "webcast" else ""
        env.write_text(
            f'CONFIG_PATH="configs/{lane}.toml"\nEVENT_DATE="{start:%Y-%m-%d}"\n{account}'
        )
        initialized = self.eventctl(
            "init",
            "--config",
            str(cfg),
            "--event-env",
            str(env),
            "--host-id",
            HOST,
            "--account-fingerprint",
            FP,
        )
        run = Path(initialized.stdout.split(": ", 1)[1].strip())
        prep = ["prepared", "admission_resolved" if lane == "phone" else "access_acquired", "study_completed", "preflight_passed"]
        for transition in prep:
            self.eventctl(
                "record", "--run-dir", str(run), "--transition", transition
            )
        plan = json.loads((run / "control" / "run-plan.json").read_text())
        return run, plan

    def readback(self, run: Path, plan: dict, scheduler: str,
                 fire_at: dt.datetime | None = None) -> Path:
        start = dt.datetime.fromisoformat(plan["starts_at_utc"].replace("Z", "+00:00"))
        lead = 25 if plan["lane"] == "phone" else 75
        program = (
            [str(self.repo / "scripts" / "event-autorun.sh"), "--run-dir", str(run)]
            if plan["lane"] == "phone"
            else [
                str(self.repo / "scripts" / "arm-on-url.sh"),
                "--run-dir",
                str(run),
                "--i-am-live",
                "--fillable",
            ]
        )
        value = {
            "schema": 1,
            "kind": "loaded-schedule-readback",
            "scheduler": scheduler,
            "loaded": True,
            "event_id": plan["event_id"],
            "run_id": plan["run_id"],
            "run_plan_sha256": plan["plan_sha256"],
            "execution_host_id": plan["execution_host_id"],
            "label": f"com.earshot.run.{plan['plan_sha256'][:20]}",
            "fire_at_utc": (fire_at or (start - dt.timedelta(minutes=lead))).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "program_arguments": program,
            "environment": {
                "EARSHOT_EXECUTION_HOST": plan["execution_host_id"],
                "EARSHOT_REPO": str(self.repo),
                "EARSHOT_RUN_DIR": str(run),
                "EARSHOT_RUN_PLAN_SHA256": plan["plan_sha256"],
            },
            "descriptor_sha256": hashlib.sha256(b"loaded descriptor").hexdigest(),
            "stop_timeout_seconds": 150 if scheduler == "systemd-user" else None,
            "kill_mode": "mixed" if scheduler == "systemd-user" else None,
        }
        if plan["lane"] == "webcast":
            value["environment"]["EARSHOT_ACCOUNT_ENV"] = str(self.account)
        path = self.repo / f"{plan['lane']}-readback.json"
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        return path

    def reconcile(self, run: Path, readback: Path, *extra: str, env=None):
        return subprocess.run(
            [
                sys.executable,
                str(self.repo / "scripts" / "schedule-proof.py"),
                "--run-dir",
                str(run),
                "--readback",
                str(readback),
                *extra,
            ],
            cwd=self.repo,
            env=env or self.env,
            text=True,
            capture_output=True,
        )

    def transitions(self, run: Path) -> list[str]:
        return [
            json.loads(line)["transition"]
            for line in (run / "control" / "receipts.jsonl").read_text().splitlines()
            if json.loads(line)["kind"] == "transition"
        ]

    def test_systemd_proof_is_durable_before_single_receipt(self):
        run, plan = self.make_run("phone")
        loaded = self.readback(run, plan, "systemd-user")
        proc = self.reconcile(run, loaded)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proof = json.loads((run / "control" / "schedule-proof.json").read_text())
        self.assertEqual(proof["readback"]["stop_timeout_seconds"], 150)
        self.assertEqual(self.transitions(run).count("scheduled"), 1)
        retry = self.reconcile(run, loaded, "--verify-only")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(self.transitions(run).count("scheduled"), 1)

    def test_launchd_exact_program_and_null_stop_policy(self):
        run, plan = self.make_run("webcast")
        loaded = self.readback(run, plan, "launchd")
        proc = self.reconcile(run, loaded)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proof = json.loads((run / "control" / "schedule-proof.json").read_text())
        self.assertEqual(proof["readback"]["program_arguments"][-2:], ["--i-am-live", "--fillable"])
        self.assertIsNone(proof["readback"]["stop_timeout_seconds"])

    def test_webcast_late_fire_override_is_bound_exactly(self):
        run, plan = self.make_run("webcast")
        fire = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
        loaded = self.readback(run, plan, "launchd", fire)
        override = fire.strftime("%Y-%m-%dT%H:%M:%SZ")
        proc = self.reconcile(run, loaded, "--fire-at-utc", override)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proof = json.loads((run / "control" / "schedule-proof.json").read_text())
        self.assertEqual(proof["readback"]["fire_at_utc"], override)

    def test_webcast_late_fire_override_refuses_past_and_post_start(self):
        for label, fire in (
            ("past", dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)),
            ("post-start", None),
        ):
            with self.subTest(label=label):
                run, plan = self.make_run("webcast")
                start = dt.datetime.fromisoformat(
                    plan["starts_at_utc"].replace("Z", "+00:00")
                )
                actual = fire or (start + dt.timedelta(seconds=1))
                loaded = self.readback(run, plan, "launchd", actual)
                override = actual.strftime("%Y-%m-%dT%H:%M:%SZ")
                before = (run / "control" / "receipts.jsonl").read_bytes()
                proc = self.reconcile(run, loaded, "--fire-at-utc", override)
                self.assertEqual(proc.returncode, 1)
                self.assertFalse((run / "control" / "schedule-proof.json").exists())
                self.assertEqual((run / "control" / "receipts.jsonl").read_bytes(), before)

    def test_invalid_readbacks_do_not_write_proof_or_receipt(self):
        cases = {
            "wrong plan": ("run_plan_sha256", "b" * 64),
            "wrong host": ("execution_host_id", "another-host"),
            "wrong argv": ("program_arguments", ["/bin/false"]),
            "too short": ("stop_timeout_seconds", 149),
            "not loaded": ("loaded", False),
        }
        for name, (field, value) in cases.items():
            with self.subTest(name=name):
                run, plan = self.make_run("phone")
                loaded = self.readback(run, plan, "systemd-user")
                body = json.loads(loaded.read_text())
                body[field] = value
                loaded.write_text(json.dumps(body))
                before = (run / "control" / "receipts.jsonl").read_bytes()
                proc = self.reconcile(run, loaded)
                self.assertEqual(proc.returncode, 1)
                self.assertFalse((run / "control" / "schedule-proof.json").exists())
                self.assertEqual((run / "control" / "receipts.jsonl").read_bytes(), before)

    def test_crash_gap_recovers_from_existing_proof(self):
        run, plan = self.make_run("phone")
        loaded = self.readback(run, plan, "systemd-user")
        injected = {**self.env, "_EARSHOT_SCHEDULE_PROOF_FAIL_AFTER_PUBLISH": "1"}
        failed = self.reconcile(run, loaded, env=injected)
        self.assertEqual(failed.returncode, 1)
        self.assertTrue((run / "control" / "schedule-proof.json").is_file())
        self.assertNotIn("scheduled", self.transitions(run))
        recovered = self.reconcile(run, loaded)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(self.transitions(run).count("scheduled"), 1)

    def test_existing_proof_cannot_be_rebound(self):
        run, plan = self.make_run("phone")
        loaded = self.readback(run, plan, "systemd-user")
        self.assertEqual(self.reconcile(run, loaded).returncode, 0)
        body = json.loads(loaded.read_text())
        body["descriptor_sha256"] = hashlib.sha256(b"different descriptor").hexdigest()
        loaded.write_text(json.dumps(body))
        before = (run / "control" / "receipts.jsonl").read_bytes()
        proc = self.reconcile(run, loaded, "--verify-only")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("cannot be rebound", proc.stderr)
        self.assertEqual((run / "control" / "receipts.jsonl").read_bytes(), before)

    def test_verify_only_never_repairs_missing_state(self):
        run, plan = self.make_run("phone")
        loaded = self.readback(run, plan, "systemd-user")
        before = (run / "control" / "receipts.jsonl").read_bytes()
        proc = self.reconcile(run, loaded, "--verify-only")
        self.assertEqual(proc.returncode, 1)
        self.assertFalse((run / "control" / "schedule-proof.json").exists())
        self.assertEqual((run / "control" / "receipts.jsonl").read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
