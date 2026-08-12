#!/usr/bin/env python3
"""Focused fail-closed tests for scripts/held-admission-receipt.py.

Every case builds an isolated temporary repository and a real schema-3
RunPlan/hash-chained lifecycle with eventctl.  No working-tree state, network,
call, page, or trading endpoint is touched.
"""

import contextlib
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "held-admission-receipt.py"
EVENTCTL_SCRIPT = REPO / "scripts" / "eventctl.py"
EVENT = "KXEARNINGSMENTIONTEST-26AUG12-PHONE"
ISSUER = "TEST"
CALL_0 = "CA" + "1" * 32
CALL_1 = "CA" + "2" * 32
FULL_LIFECYCLE = (
    "prepared",
    "admission_resolved",
    "study_completed",
    "preflight_passed",
    "scheduled",
    "capture_ready",
    "launched",
    "source_attached",
)

CONFIG = f"""\
[event]
id = "{EVENT}"
starts_at = "2026-08-12T21:00:00Z"

[kalshi]
env = "demo"

[[sources]]
kind = "twilio_stream"
bind = "127.0.0.1:8080"

[[keywords]]
market_ticker = "KXEARNINGSMENTIONTEST-26AUG12-FOO"
phrase = "foo"
"""

_spec = importlib.util.spec_from_file_location("held_receipt_eventctl", EVENTCTL_SCRIPT)
eventctl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eventctl)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def write_json(path: Path, value: dict, mode=None):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    if mode is not None:
        path.chmod(mode)


class HeldAdmissionReceiptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="held-admission-test-")
        self.repo = Path(self._tmp.name).resolve()
        (self.repo / "configs").mkdir()
        (self.repo / "state").mkdir()
        self.config = self.repo / "configs" / "test-phone-arm.toml"
        self.config.write_text(CONFIG)
        self.registry = self.repo / "configs" / "dial-ins.toml"

        self._old_cwd = Path.cwd()
        self._old_env = {
            key: os.environ.get(key)
            for key in (
                "EARSHOT_REPO",
                "EARSHOT_HOST_ID",
                "EARSHOT_KALSHI_ACCOUNT_FINGERPRINT",
            )
        }
        os.environ["EARSHOT_REPO"] = str(self.repo)
        os.environ["EARSHOT_HOST_ID"] = "held-receipt-test-host"
        os.environ["EARSHOT_KALSHI_ACCOUNT_FINGERPRINT"] = "sha256:" + "a" * 64
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self._old_cwd)
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def eventctl(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = eventctl.main(list(args))
        self.assertEqual(rc, 0, stderr.getvalue() or stdout.getvalue())
        return stdout.getvalue()

    def write_registry(self, admission="held", probed=None, include=True):
        probed = probed or dt.datetime.now(dt.timezone.utc).date().isoformat()
        if not include:
            self.registry.write_text('[OTHER]\nadmission = "held"\n'
                                     f'admission_probed = "{probed}"\n')
            return
        fields = [f"[{ISSUER}]"]
        if admission is not None:
            fields.append(f'admission = "{admission}"')
        fields.append(f'admission_probed = "{probed}"')
        self.registry.write_text("\n".join(fields) + "\n")

    def make_rig(self, transitions=FULL_LIFECYCLE):
        self.write_registry()
        self.eventctl("init", "--config", str(self.config))
        run_root = self.repo / "var" / "runs" / EVENT
        runs = list(run_root.iterdir())
        self.assertEqual(len(runs), 1)
        self.run_dir = runs[0]
        self.plan_path = self.run_dir / "control" / "run-plan.json"
        self.plan = json.loads(self.plan_path.read_text())
        for transition in transitions:
            self.eventctl(
                "record",
                "--run-dir",
                str(self.run_dir),
                "--transition",
                transition,
                "--actor",
                "held-admission-test",
            )

        self.state = self.repo / "state" / EVENT
        self.state.mkdir()
        self.challenge_path = self.state / "admission.challenge.json"
        self.source_path = self.state / "source.attached"
        self.stream_path = self.state / "stream.connected"
        self.receipt_path = self.state / "admission.receipt.json"
        self.audit_path = self.state / "admission.assertions.jsonl"
        self.pid = os.getpid()
        self.nonce = "b" * 64
        self.write_challenge()
        self.write_source()
        self.write_stream()

    def write_challenge(self, **updates):
        value = {
            "schema_version": 1,
            "event_id": EVENT,
            "nonce": self.nonce,
            "pid": self.pid,
            "receipt_path": str(self.receipt_path),
            "stream_connected_path": str(self.stream_path),
        }
        value.update(updates)
        write_json(self.challenge_path, value)

    def write_source(self, **updates):
        value = {
            "schema_version": 1,
            "run_id": self.plan["run_id"],
            "event_id": EVENT,
            "plan_sha256": self.plan["plan_sha256"],
            "pid": self.pid,
            "unix_ns": time.time_ns(),
            "accepted_frames": 1,
        }
        value.update(updates)
        write_json(self.source_path, value)

    def write_stream(self, **updates):
        value = {
            "unix_ts": int(time.time()),
            "call_sid": CALL_0,
            "attempt": 0,
        }
        value.update(updates)
        write_json(self.stream_path, value)

    def run_producer(
        self, *extra, config=None, run_plan=None, state_dir=None
    ):
        env = os.environ.copy()
        env["EARSHOT_REPO"] = str(self.repo)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--config",
                str(config or self.config),
                "--run-plan",
                str(run_plan or self.plan_path),
                "--state-dir",
                str(state_dir or self.state),
                *extra,
            ],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def assert_refused_without_output(self, result, contains=None):
        self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
        self.assertIn("REFUSING held admission receipt:", result.stderr)
        if contains:
            self.assertIn(contains, result.stderr)
        self.assertFalse(self.receipt_path.exists())
        self.assertFalse(self.audit_path.exists())

    def assert_ineligible_without_output(self, result, contains=None):
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("INELIGIBLE held admission receipt:", result.stderr)
        if contains:
            self.assertIn(contains, result.stderr)
        self.assertFalse(self.receipt_path.exists())
        self.assertFalse(self.audit_path.exists())

    def audit_rows(self):
        return [json.loads(line) for line in self.audit_path.read_text().splitlines()]

    def test_success_binds_probe_run_challenge_source_and_stream(self):
        self.make_rig()
        result = self.run_producer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("held admission ASSERTED", result.stdout)

        receipt = json.loads(self.receipt_path.read_text())
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["event_id"], EVENT)
        self.assertEqual(receipt["run_id"], self.plan["run_id"])
        self.assertEqual(receipt["plan_sha256"], self.plan["plan_sha256"])
        self.assertEqual(receipt["nonce"], self.nonce)
        self.assertEqual(receipt["call_sid"], CALL_0)
        self.assertEqual(receipt["stream_attempt"], 0)
        self.assertEqual(receipt["assertion_method"], "probe_held_current_run")
        self.assertNotEqual(receipt["assertion_method"], "explicit_operator_pass_cli")

        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        assertion = rows[0]
        self.assertEqual(assertion["kind"], "held_admission_assertion")
        self.assertEqual(assertion["issuer"], ISSUER)
        self.assertEqual(assertion["admission"], "held")
        self.assertEqual(assertion["config_sha256"], self.plan["config_sha256"])
        self.assertRegex(assertion["registry_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(assertion["preflight_receipt_sha256"], r"^[0-9a-f]{64}$")
        claimed = assertion.pop("assertion_sha256")
        self.assertEqual(claimed, hashlib.sha256(canonical(assertion)).hexdigest())
        self.assertEqual(receipt["assertion_sha256"], claimed)
        self.assertEqual(stat.S_IMODE(self.receipt_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.audit_path.stat().st_mode), 0o600)

    def test_idempotence_and_crash_recovery_do_not_duplicate_audit(self):
        self.make_rig()
        first = self.run_producer()
        self.assertEqual(first.returncode, 0, first.stderr)
        receipt = self.receipt_path.read_bytes()
        audit = self.audit_path.read_bytes()

        again = self.run_producer()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already asserted", again.stdout)
        self.assertEqual(self.receipt_path.read_bytes(), receipt)
        self.assertEqual(self.audit_path.read_bytes(), audit)

        self.receipt_path.unlink()  # model death after durable audit, before rename
        recovered = self.run_producer()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn("RECOVERED", recovered.stdout)
        self.assertEqual(self.receipt_path.read_bytes(), receipt)
        self.assertEqual(self.audit_path.read_bytes(), audit)

    def test_redial_replaces_only_same_challenge_stale_stream_receipt(self):
        self.make_rig()
        first = self.run_producer()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.write_stream(call_sid=CALL_1, attempt=1)
        second = self.run_producer()
        self.assertEqual(second.returncode, 0, second.stderr)
        receipt = json.loads(self.receipt_path.read_text())
        self.assertEqual(receipt["nonce"], self.nonce)
        self.assertEqual(receipt["call_sid"], CALL_1)
        self.assertEqual(receipt["stream_attempt"], 1)
        self.assertEqual(len(self.audit_rows()), 2)

    def test_operator_screened_dropped_untested_unknown_and_missing_refuse(self):
        self.make_rig()
        for admission in (
            "operator_screened",
            "dropped",
            "untested",
            "unknown",
            None,
        ):
            with self.subTest(admission=admission):
                self.write_registry(admission=admission)
                self.assert_ineligible_without_output(
                    self.run_producer(), "only a fresh probe-proven 'held'"
                )

    def test_stale_future_and_malformed_held_dates_refuse(self):
        self.make_rig()
        today = dt.datetime.now(dt.timezone.utc).date()
        cases = (
            ((today - dt.timedelta(days=31)).isoformat(), "stale"),
            ((today + dt.timedelta(days=1)).isoformat(), "future"),
            ("not-a-date", "YYYY-MM-DD"),
        )
        for probed, message in cases:
            with self.subTest(probed=probed):
                self.write_registry(probed=probed)
                self.assert_ineligible_without_output(self.run_producer(), message)

    def test_thirty_day_freshness_boundary_is_eligible(self):
        self.make_rig()
        boundary = (
            dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=30)
        ).isoformat()
        self.write_registry(probed=boundary)
        result = self.run_producer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.audit_rows()[0]["admission_age_days"], 30)

    def test_config_drift_and_wrong_registry_issuer_refuse(self):
        self.make_rig()
        original = self.config.read_text()
        self.config.write_text(original + "\n# drift\n")
        self.assert_refused_without_output(self.run_producer(), "eventctl verify")
        self.config.write_text(original)
        self.write_registry(include=False)
        self.assert_ineligible_without_output(self.run_producer(), "no exact [TEST]")

    def test_selected_config_and_run_plan_paths_are_exact(self):
        self.make_rig()
        config_copy = self.repo / "configs" / "same-bytes.toml"
        config_copy.write_bytes(self.config.read_bytes())
        self.assert_refused_without_output(
            self.run_producer(config=config_copy), "not RunPlan config"
        )

        alternate = self.repo / "alternate" / EVENT / self.plan["run_id"]
        alternate.parent.mkdir(parents=True)
        shutil.copytree(self.run_dir, alternate)
        self.assert_refused_without_output(
            self.run_producer(run_plan=alternate / "control" / "run-plan.json"),
            "canonical current-run path",
        )

    def test_registry_integrity_failure_is_loud_not_ineligible(self):
        self.make_rig()
        self.registry.write_text("[TEST\nnot toml")
        self.assert_refused_without_output(self.run_producer(), "not valid TOML")

        self.write_registry()
        real = self.repo / "configs" / "dial-ins.real.toml"
        self.registry.replace(real)
        self.registry.symlink_to(real)
        self.assert_refused_without_output(self.run_producer(), "symlink")

        # There is intentionally no evidence override. argparse usage failure
        # remains distinct from both registry ineligibility and integrity.
        result = self.run_producer("--registry", str(real))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("unrecognized arguments: --registry", result.stderr)

    def test_wrong_source_run_plan_pid_and_frames_refuse(self):
        self.make_rig()
        cases = (
            ({"run_id": "wrong-run"}, "source attachment run_id"),
            ({"plan_sha256": "0" * 64}, "source attachment plan_sha256"),
            ({"pid": self.pid + 1}, "source attachment pid"),
            ({"accepted_frames": 0}, "accepted detector-path frames"),
        )
        for update, message in cases:
            with self.subTest(update=update):
                self.write_source(**update)
                self.assert_refused_without_output(self.run_producer(), message)
        self.write_source()

    def test_dead_challenge_pid_and_wrong_challenge_paths_refuse(self):
        self.make_rig()
        dead_pid = 2_147_483_647
        self.write_challenge(pid=dead_pid)
        self.write_source(pid=dead_pid)
        self.assert_refused_without_output(self.run_producer(), "is not alive")

        self.write_challenge(receipt_path=str(self.state / "wrong.receipt"))
        self.write_source()
        self.assert_refused_without_output(self.run_producer(), "receipt_path")
        self.write_challenge(stream_connected_path=str(self.state / "wrong.stream"))
        self.assert_refused_without_output(self.run_producer(), "stream_connected_path")
        self.write_challenge(event_id="KXEARNINGSMENTIONOTHER-26AUG12-PHONE")
        self.assert_refused_without_output(self.run_producer(), "challenge event")

    def test_invalid_or_old_stream_marker_refuses(self):
        self.make_rig()
        cases = (
            ({"call_sid": "not-a-sid"}, "CallSid"),
            ({"attempt": -1}, "nonnegative attempt"),
            ({"attempt": True}, "nonnegative attempt"),
            ({"unix_ts": 1}, "predates"),
        )
        for update, message in cases:
            with self.subTest(update=update):
                self.write_stream(**update)
                self.assert_refused_without_output(self.run_producer(), message)

    def test_lifecycle_requires_preflight_launch_source_and_active_run(self):
        self.make_rig(
            transitions=("prepared", "admission_resolved", "study_completed")
        )
        self.assert_refused_without_output(self.run_producer(), "preflight_passed")

    def test_missing_source_attached_lifecycle_refuses_despite_markers(self):
        self.make_rig(transitions=FULL_LIFECYCLE[:-1])
        self.assert_refused_without_output(self.run_producer(), "source_attached")

    def test_terminal_run_refuses(self):
        self.make_rig()
        self.eventctl(
            "record",
            "--run-dir",
            str(self.run_dir),
            "--transition",
            "failed",
            "--actor",
            "held-admission-test",
        )
        self.assert_refused_without_output(self.run_producer(), "terminal")

    def test_wrong_state_and_symlink_marker_refuse(self):
        self.make_rig()
        wrong_state = self.repo / "state" / "OTHER"
        wrong_state.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--config",
                str(self.config),
                "--run-plan",
                str(self.plan_path),
                "--state-dir",
                str(wrong_state),
            ],
            cwd=self.repo,
            env={**os.environ, "EARSHOT_REPO": str(self.repo)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 8)
        self.assertIn("not current event state", result.stderr)
        self.assertFalse(self.receipt_path.exists())
        self.assertFalse(self.audit_path.exists())

        real = self.state / "challenge.real.json"
        self.challenge_path.replace(real)
        self.challenge_path.symlink_to(real)
        self.assert_refused_without_output(self.run_producer(), "symlink")

    def test_incompatible_existing_receipt_is_never_overwritten(self):
        self.make_rig()
        incompatible = {
            "schema_version": 1,
            "event_id": EVENT,
            "nonce": "c" * 64,
            "status": "admitted",
            "call_sid": CALL_0,
            "stream_attempt": 0,
            "assertion_method": "probe_held_current_run",
        }
        write_json(self.receipt_path, incompatible, 0o600)
        result = self.run_producer()
        self.assertEqual(result.returncode, 8)
        self.assertIn("incompatible", result.stderr)
        self.assertEqual(json.loads(self.receipt_path.read_text()), incompatible)
        # Validation gets as far as the shared audit lock, but it must not mint
        # any assertion row for an incompatible receipt.
        self.assertEqual(self.audit_path.read_bytes(), b"")

    def test_corrupt_assertion_journal_never_allows_a_receipt(self):
        self.make_rig()
        self.audit_path.write_bytes(b"{not-json}\n")
        self.audit_path.chmod(0o600)
        result = self.run_producer()
        self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
        self.assertIn("journal line 1 is invalid", result.stderr)
        self.assertFalse(self.receipt_path.exists())

        forged = {
            "schema_version": 1,
            "kind": "held_admission_assertion",
            "asserted_unix_ns": time.time_ns(),
            "assertion_sha256": "0" * 64,
        }
        self.audit_path.write_bytes(canonical(forged) + b"\n")
        result = self.run_producer()
        self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
        self.assertIn("hash mismatch", result.stderr)
        self.assertFalse(self.receipt_path.exists())

    def test_current_operator_assertion_is_preserved_not_relabelled(self):
        self.make_rig()
        asserted_ns = time.time_ns()
        operator = {
            "schema_version": 1,
            "event_id": EVENT,
            "nonce": self.nonce,
            "status": "admitted",
            "call_sid": CALL_0,
            "stream_attempt": 0,
            "assertion_method": "explicit_operator_pass_cli",
            "asserted_by": "human-cli",
            "asserted_unix_ns": asserted_ns,
            "challenge_pid": self.pid,
        }
        assertion = dict(operator)
        assertion["kind"] = "operator_admission_assertion"
        assertion["assertion_sha256"] = hashlib.sha256(canonical(assertion)).hexdigest()
        self.audit_path.write_bytes(canonical(assertion) + b"\n")
        self.audit_path.chmod(0o600)
        write_json(self.receipt_path, operator, 0o600)
        before_audit = self.audit_path.read_bytes()
        before_receipt = self.receipt_path.read_bytes()
        result = self.run_producer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("explicit operator assertion; preserved", result.stdout)
        self.assertEqual(self.audit_path.read_bytes(), before_audit)
        self.assertEqual(self.receipt_path.read_bytes(), before_receipt)
        self.assertNotIn(b"held_admission_assertion", before_audit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
