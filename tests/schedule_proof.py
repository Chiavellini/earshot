#!/usr/bin/env python3
"""Offline contract tests for scripts/schedule-proof.py."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
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
        self.alert_log = self.repo / "alert-calls.jsonl"
        self.fake_alertctl = self.repo / "scripts" / "fake-alertctl.py"
        self.fake_alertctl.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['ALERT_CALL_LOG'], 'a') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "raise SystemExit(0)\n"
        )
        self.ssh_log = self.repo / "ssh-calls.jsonl"
        self.remote_bundle = self.repo / "remote-bundle.json"
        self.fake_ssh = self.repo / "scripts" / "fake-ssh.py"
        self.fake_ssh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "with open(os.environ['SSH_CALL_LOG'], 'a') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if os.environ.get('SSH_FAIL'):\n"
            "    print('injected ssh failure', file=sys.stderr)\n"
            "    raise SystemExit(23)\n"
            "print(pathlib.Path(os.environ['REMOTE_BUNDLE']).read_text(), end='')\n"
        )
        self.fake_ssh.chmod(0o755)
        self.home = self.repo / "home"
        self.home.mkdir()
        self.account = self.home / ".earshot" / "account.env"
        self.account.parent.mkdir()
        self.account.write_text("EARSHOT_ACCOUNT_FIXTURE=1\n")
        self.env = {
            **os.environ,
            "EARSHOT_REPO": str(self.repo),
            "HOME": str(self.home),
            "ALERT_CALL_LOG": str(self.alert_log),
            "SSH_CALL_LOG": str(self.ssh_log),
            "REMOTE_BUNDLE": str(self.remote_bundle),
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
            # Mirrors install-t0-agents.sh's pinned fire-time PATH (AMAT
            # 2026-08-13: unpinned launchd PATH killed the arm at fire).
            value["environment"]["PATH"] = (
                "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            )
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

    def launch_run(self, run: Path) -> None:
        for transition in ("scheduled", "capture_ready", "launched"):
            self.eventctl("record", "--run-dir", str(run), "--transition", transition)

    def write_progress(
        self, run: Path, relative: str, *, byte_count: int, cadence: float,
        append_age: float = 0.0,
    ) -> Path:
        path = run / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        plan = json.loads((run / "control" / "run-plan.json").read_text())
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "kind": "evidence-progress",
                    "run_id": plan["run_id"],
                    "plan_sha256": plan["plan_sha256"],
                    "bytes": byte_count,
                    "last_append_monotonic_ns": time.monotonic_ns()
                    - int(append_age * 1_000_000_000),
                    "cadence_seconds": cadence,
                },
                sort_keys=True,
            )
            + "\n"
        )
        return path

    def monitor(self, run: Path, *extra: str, timeout: float = 20) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(self.repo / "scripts" / "schedule-proof.py"),
                "monitor",
                "--run-dir",
                str(run),
                "--alert-dir",
                str(self.repo / "state" / "alerts"),
                "--alertctl",
                str(self.fake_alertctl),
                *extra,
            ],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def alert_calls(self) -> list[list[str]]:
        if not self.alert_log.exists():
            return []
        return [json.loads(line) for line in self.alert_log.read_text().splitlines()]

    def write_remote_bundle(self, run: Path, files: dict[str, str | None]) -> None:
        requested = {
            str((run / "control" / "receipts.jsonl").relative_to(self.repo)): None,
            str((run / "telemetry" / "live.jsonl").relative_to(self.repo)): None,
            str((self.repo / "state" / run.parent.name / "journal.jsonl").relative_to(self.repo)): None,
        }
        requested.update(files)
        self.remote_bundle.write_text(
            json.dumps(
                {"schema": 1, "kind": "remote-read-snapshot", "files": requested},
                sort_keys=True,
            )
            + "\n"
        )

    @staticmethod
    def tree_bytes(run: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(run)): path.read_bytes()
            for path in sorted(run.rglob("*"))
            if path.is_file()
        }

    def test_monitor_started_midrun_pages_no_frames_without_mutating_run(self):
        run, _plan = self.make_run("phone")
        self.launch_run(run)
        time.sleep(0.15)
        before = self.tree_bytes(run)
        proc = self.monitor(
            run,
            "--once",
            "--confirm-seconds",
            "0.1",
            "--progress",
            "audio=capture/audio.raw.progress.json:growth",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.alert_calls()
        flattened = "\n".join(" ".join(call) for call in calls)
        self.assertIn("progress_missing", flattened)
        self.assertIn("no_frames", flattened)
        self.assertEqual(self.tree_bytes(run), before, "monitor wrote into the live run tree")

    def test_monitor_consumes_growth_sidecar_and_pages_one_cadence_stall(self):
        run, _plan = self.make_run("phone")
        self.launch_run(run)
        self.eventctl("record", "--run-dir", str(run), "--transition", "source_attached")
        self.write_progress(
            run,
            "capture/audio.raw.progress.json",
            byte_count=4096,
            cadence=0.1,
            append_age=1.0,
        )
        proc = self.monitor(
            run,
            "--once",
            "--progress",
            "audio=capture/audio.raw.progress.json:growth",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        flattened = "\n".join(" ".join(call) for call in self.alert_calls())
        self.assertIn("tape_stalled", flattened)
        self.assertNotIn("no_frames", flattened)

    def test_monitor_pages_typed_all_legs_dead_drop_cadence_and_early_exit(self):
        run, plan = self.make_run("phone")
        self.launch_run(run)
        self.eventctl("record", "--run-dir", str(run), "--transition", "failed")
        progress = self.write_progress(
            run,
            "capture/audio.raw.progress.json",
            byte_count=4096,
            cadence=30,
        )
        telemetry = run / "telemetry" / "live.jsonl"
        telemetry.parent.mkdir()
        telemetry.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "kind": "live-telemetry",
                    "run_id": plan["run_id"],
                    "legs": [
                        {"label": "a", "died": "gap"},
                        {"label": "b", "alive": False},
                    ],
                }
            )
            + "\n"
        )
        journal = self.repo / "state" / plan["event_id"] / "journal.jsonl"
        journal.parent.mkdir(parents=True)
        base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
        journal.write_text(
            "".join(
                json.dumps(
                    {
                        "ts": (base + dt.timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "actor": "redial-supervisor.sh",
                        "action": "redial",
                        "outcome": "ok",
                    }
                )
                + "\n"
                for offset in (0, 75, 155)
            )
        )
        proc = self.monitor(
            run,
            "--once",
            "--progress",
            f"audio={progress.relative_to(run)}:heartbeat",
            "--telemetry",
            str(telemetry),
            "--journal",
            str(journal),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        flattened = "\n".join(" ".join(call) for call in self.alert_calls())
        self.assertIn("all_legs_dead", flattened)
        self.assertIn("drop_cadence", flattened)
        self.assertIn("arm_exited_early", flattened)

    def test_monitor_supervisor_restarts_a_killed_child(self):
        run, _plan = self.make_run("phone")
        progress = self.write_progress(
            run,
            "capture/audio.raw.progress.json",
            byte_count=0,
            cadence=30,
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                str(self.repo / "scripts" / "schedule-proof.py"),
                "monitor",
                "--run-dir",
                str(run),
                "--alert-dir",
                str(self.repo / "state" / "alerts"),
                "--alertctl",
                str(self.fake_alertctl),
                "--progress",
                f"audio={progress.relative_to(run)}:heartbeat",
                "--poll-seconds",
                "0.1",
                "--restart-seconds",
                "0.1",
                "--supervise",
            ],
            cwd=self.repo,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())

        def child_pid(after: int | None = None) -> int:
            deadline = time.time() + 10
            assert proc.stdout is not None
            while time.time() < deadline:
                line = proc.stdout.readline()
                if line.startswith("MONITOR_CHILD_PID="):
                    value = int(line.split("=", 1)[1])
                    if value != after:
                        return value
            self.fail("supervisor did not publish a child pid")

        first = child_pid()
        os.kill(first, signal.SIGKILL)
        second = child_pid(after=first)
        self.assertNotEqual(first, second)
        proc.terminate()
        self.assertEqual(proc.wait(timeout=10), 0)
        assert proc.stdout is not None and proc.stderr is not None
        proc.stdout.close()
        proc.stderr.close()

    def test_monitor_bounded_remote_read_uses_box_tail_without_local_mutation(self):
        run, plan = self.make_run("phone")
        self.launch_run(run)
        self.eventctl("record", "--run-dir", str(run), "--transition", "failed")
        ledger = run / "control" / "receipts.jsonl"
        remote_ledger = ledger.read_text()
        local_prefix = "\n".join(remote_ledger.splitlines()[:-1]) + "\n"
        ledger.write_text(local_prefix)
        progress_relative = "capture/audio.raw.progress.json"
        progress = json.dumps(
            {
                "schema": 1,
                "kind": "evidence-progress",
                "run_id": plan["run_id"],
                "plan_sha256": plan["plan_sha256"],
                "bytes": 8192,
                "last_append_monotonic_ns": time.monotonic_ns(),
                "cadence_seconds": 30,
            },
            sort_keys=True,
        ) + "\n"
        self.write_remote_bundle(
            run,
            {
                str(ledger.relative_to(self.repo)): remote_ledger,
                str((run / progress_relative).relative_to(self.repo)): progress,
            },
        )
        before = self.tree_bytes(run)
        proc = self.monitor(
            run,
            "--once",
            "--progress",
            f"audio={progress_relative}:heartbeat",
            "--remote-host",
            "earshot@box.test",
            "--remote-repo",
            "/srv/ear",
            "--ssh-command",
            str(self.fake_ssh),
            "--remote-timeout-seconds",
            "2",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        snapshots = [
            json.loads(line)
            for line in proc.stdout.splitlines()
            if line.startswith("{") and "live-monitor-snapshot" in line
        ]
        self.assertEqual(snapshots[-1]["terminal"], "failed")
        self.assertEqual(snapshots[-1]["authority"]["mode"], "bounded-remote-read")
        self.assertEqual(snapshots[-1]["progress"]["audio"]["bytes"], 8192)
        self.assertGreater(snapshots[-1]["authority"]["staleness_bound_seconds"], 0)
        flattened = "\n".join(" ".join(call) for call in self.alert_calls())
        self.assertIn("arm_exited_early", flattened)
        self.assertEqual(self.tree_bytes(run), before)
        ssh_args = json.loads(self.ssh_log.read_text().splitlines()[0])
        self.assertIn("BatchMode=yes", ssh_args)
        self.assertIn("control/receipts.jsonl", ssh_args[-1])

    def test_monitor_remote_refresh_failure_is_pageable_and_loud(self):
        run, _plan = self.make_run("phone")
        env = dict(self.env)
        env["SSH_FAIL"] = "1"
        previous = self.env
        self.env = env
        try:
            proc = self.monitor(
                run,
                "--once",
                "--progress",
                "audio=capture/audio.raw.progress.json:heartbeat",
                "--remote-host",
                "box.test",
                "--remote-repo",
                "/srv/ear",
                "--ssh-command",
                str(self.fake_ssh),
                "--remote-timeout-seconds",
                "2",
            )
        finally:
            self.env = previous
        self.assertEqual(proc.returncode, 1)
        self.assertIn('"state": "failed"', proc.stdout)
        flattened = "\n".join(" ".join(call) for call in self.alert_calls())
        self.assertIn("remote_read_failed", flattened)

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
