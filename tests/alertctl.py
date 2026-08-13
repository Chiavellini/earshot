#!/usr/bin/env python3
"""Offline tests for proof-bearing alert delivery and acknowledgement."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
ALERTCTL = REPO / "scripts" / "alertctl.py"
EVENTCTL = REPO / "scripts" / "eventctl.py"
EVENT = "ALERT-TEST"
RUN = "20260812T120000Z-p3-alertfixture"


def canon(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class AlertCtlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp(prefix="alertctl-test."))
        self.addCleanup(shutil.rmtree, self.repo)
        (self.repo / "configs").mkdir()
        config = self.repo / "configs" / "alert.toml"
        config.write_text(
            "[event]\n"
            f'id = "{EVENT}"\n'
            'starts_at = "2026-08-12T12:00:00Z"\n'
            "[[sources]]\n"
            'kind = "pipe"\n'
            "[kalshi]\n"
            'env = "prod"\n'
            '[[keywords]]\nmarket_ticker = "KXALERT-YES"\n'
        )
        run_root = self.repo / "var" / "runs"
        env = self.env()
        result = subprocess.run(
            [
                sys.executable,
                str(EVENTCTL),
                "init",
                "--config",
                str(config),
                "--run-root",
                str(run_root),
                "--run-id",
                RUN,
                "--host-id",
                "fixture-host",
                "--account-fingerprint",
                "sha256:" + "a" * 64,
            ],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.run_dir = run_root / EVENT / RUN
        # Alerting must work after a terminal failure because that is its most
        # important production use. `failed` is legal from any nonterminal.
        self.eventctl("record", "--run-dir", str(self.run_dir), "--transition", "failed")
        self.adapters = self.repo / "adapters"
        self.adapters.mkdir()
        self.notify_log = self.repo / "notify.log"
        self.phone_log = self.repo / "phone.log"
        self.notify = self.adapter("notify", "EARSHOT_TEST_NOTIFY_LOG", 0)
        self.phone = self.adapter("phone", "EARSHOT_TEST_PHONE_LOG", 0)

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["EARSHOT_REPO"] = str(self.repo)
        env["EARSHOT_EVENTCTL"] = str(EVENTCTL)
        env["EARSHOT_TEST_NOTIFY_LOG"] = str(getattr(self, "notify_log", self.repo / "notify.log"))
        env["EARSHOT_TEST_PHONE_LOG"] = str(getattr(self, "phone_log", self.repo / "phone.log"))
        return env

    def adapter(self, name: str, variable: str, status: int) -> Path:
        path = self.adapters / name
        path.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                printf 'called\\n' >> "${{{variable}}}"
                exit {status}
                """
            )
        )
        path.chmod(0o755)
        return path

    def eventctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(EVENTCTL), *args],
            text=True,
            capture_output=True,
            env=self.env(),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def alert(self, *args: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(ALERTCTL), *args],
            text=True,
            capture_output=True,
            env=self.env(),
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def raise_args(self, severity: str = "critical") -> list[str]:
        return [
            "raise",
            "--run-dir",
            str(self.run_dir),
            "--severity",
            severity,
            "--code",
            "runtime_failed",
            "--dedupe-key",
            "terminal",
            "--subject",
            "Runtime failed",
            "--body",
            "Evidence finalization failed",
            "--notify-adapter",
            str(self.notify),
            "--phone-adapter",
            str(self.phone),
            "--phone-attempts",
            "2",
            "--retry-delay",
            "0",
        ]

    def rows(self) -> list[dict]:
        path = self.run_dir / "control" / "receipts.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def alert_rows(self) -> list[dict]:
        return [row for row in self.rows() if str(row.get("action", "")).startswith("alert.")]

    def test_critical_request_is_durable_phone_retries_and_never_auto_acks(self) -> None:
        result = self.alert(*self.raise_args())
        alert_id = result.stdout.split()[1].rstrip(":")
        rows = self.alert_rows()
        self.assertEqual(
            [row["action"].split(":", 1)[0].split(".")[1] for row in rows],
            ["required", "delivery_started", "delivery", "delivery_started", "delivery"],
        )
        self.assertEqual(len(self.notify_log.read_text().splitlines()), 1)
        self.assertEqual(len(self.phone_log.read_text().splitlines()), 1)
        status = self.alert("status", "--run-dir", str(self.run_dir), expected=1)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["unacknowledged"], 1)
        self.assertFalse(payload["alerts"][0]["acknowledged"])
        self.assertEqual(payload["alerts"][0]["request"]["alert_id"], alert_id)

    def test_repeated_raise_dedupes_request_and_successful_delivery(self) -> None:
        self.alert(*self.raise_args())
        self.alert(*self.raise_args())
        actions = [row["action"] for row in self.alert_rows()]
        required = [action for action in actions if action.startswith("alert.required:")]
        local = [action for action in actions if action.startswith("alert.delivery:") and ":local:" in action]
        phone = [action for action in actions if action.startswith("alert.delivery:") and ":phone:" in action]
        self.assertEqual(len(required), 1)
        self.assertEqual([action.rsplit(":", 1)[1] for action in local], ["1"])
        self.assertEqual([action.rsplit(":", 1)[1] for action in phone], ["1"])
        self.assertEqual(len(self.notify_log.read_text().splitlines()), 1)
        self.assertEqual(len(self.phone_log.read_text().splitlines()), 1)
        verify = subprocess.run(
            [sys.executable, str(EVENTCTL), "verify", "--run-dir", str(self.run_dir)],
            text=True,
            capture_output=True,
            env=self.env(),
        )
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

    def test_unacknowledged_critical_alert_repages_when_due_and_ack_stops_it(self) -> None:
        result = self.alert(*self.raise_args())
        alert_id = result.stdout.split()[1].rstrip(":")
        self.assertEqual(len(self.phone_log.read_text().splitlines()), 1)
        self.alert(
            "repage-due",
            "--run-dir",
            str(self.run_dir),
            "--notify-adapter",
            str(self.notify),
            "--phone-adapter",
            str(self.phone),
            "--phone-attempts",
            "2",
            "--retry-delay",
            "0",
            "--repage-after",
            "0",
        )
        self.assertEqual(len(self.phone_log.read_text().splitlines()), 2)
        self.alert(
            "ack",
            "--run-dir",
            str(self.run_dir),
            "--alert-id",
            alert_id,
            "--by",
            "operator@example",
        )
        self.alert(
            "repage-due",
            "--run-dir",
            str(self.run_dir),
            "--notify-adapter",
            str(self.notify),
            "--phone-adapter",
            str(self.phone),
            "--phone-attempts",
            "3",
            "--retry-delay",
            "0",
            "--repage-after",
            "0",
        )
        self.assertEqual(len(self.phone_log.read_text().splitlines()), 2)

    def test_standalone_alert_is_hash_chained_and_does_not_touch_run_state(self) -> None:
        alert_dir = self.repo / "state" / "alerts"
        receipts = self.run_dir / "control" / "receipts.jsonl"
        before = receipts.read_bytes()
        result = self.alert(
            "raise",
            "--alert-dir",
            str(alert_dir),
            "--scope",
            "monitor:ALERT-TEST:fixture",
            "--severity",
            "critical",
            "--code",
            "tape_stalled",
            "--dedupe-key",
            "book-tape",
            "--subject",
            "Tape stalled",
            "--body",
            "No growth for one cadence",
            "--notify-adapter",
            str(self.notify),
            "--phone-adapter",
            str(self.phone),
            "--phone-attempts",
            "2",
            "--retry-delay",
            "0",
        )
        alert_id = result.stdout.split()[1].rstrip(":")
        self.assertEqual(receipts.read_bytes(), before)
        rows = [json.loads(line) for line in (alert_dir / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual([row["sequence"] for row in rows], [1, 2, 3, 4, 5])
        self.assertEqual(rows[0]["previous_sha256"], "0" * 64)
        self.assertEqual(rows[-1]["previous_sha256"], rows[-2]["receipt_sha256"])
        self.alert("status", "--alert-dir", str(alert_dir), expected=1)
        self.alert(
            "ack",
            "--alert-dir",
            str(alert_dir),
            "--alert-id",
            alert_id,
            "--by",
            "operator@example",
        )
        self.alert("status", "--alert-dir", str(alert_dir))

    def test_host_contention_is_first_class_and_restart_safe(self) -> None:
        alert_dir = self.repo / "state" / "alerts"
        args = [
            "host-contention",
            "--alert-dir",
            str(alert_dir),
            "--host-id",
            "capture-box-1",
            "--guard-id",
            "fixture-event.capture-ready",
            "--subject",
            "Capture host contention",
            "--body",
            "load15=7.2 budget=4.0",
            "--notify-adapter",
            str(self.notify),
            "--phone-adapter",
            str(self.phone),
            "--phone-attempts",
            "2",
            "--retry-delay",
            "0",
        ]
        first = self.alert(*args)
        alert_id = first.stdout.split()[1].rstrip(":")
        self.alert(*args)
        self.assertEqual(len(self.phone_log.read_text().splitlines()), 1)
        rows = [json.loads(line) for line in (alert_dir / "ledger.jsonl").read_text().splitlines()]
        required = json.loads(rows[0]["detail"])
        self.assertEqual(required["scope"], "host-contention:capture-box-1")
        self.assertEqual(required["severity"], "critical")
        self.assertEqual(required["code"], "host_contention")
        self.assertEqual(required["dedupe_key"], "fixture-event.capture-ready")
        self.alert(
            "ack",
            "--alert-dir",
            str(alert_dir),
            "--alert-id",
            alert_id,
            "--by",
            "operator@example",
        )
        self.alert(*args)
        self.assertEqual(len(self.phone_log.read_text().splitlines()), 1)

    def test_acknowledgement_is_explicit_idempotent_and_suppresses_redelivery(self) -> None:
        result = self.alert(*self.raise_args())
        alert_id = result.stdout.split()[1].rstrip(":")
        self.alert(
            "ack",
            "--run-dir",
            str(self.run_dir),
            "--alert-id",
            alert_id,
            "--by",
            "operator@example",
            "--note",
            "seen",
        )
        before_notify = self.notify_log.read_bytes()
        before_phone = self.phone_log.read_bytes()
        before_rows = len(self.alert_rows())
        self.alert(
            "ack",
            "--run-dir",
            str(self.run_dir),
            "--alert-id",
            alert_id,
            "--by",
            "operator@example",
            "--note",
            "seen",
        )
        self.alert(*self.raise_args())
        self.assertEqual(self.notify_log.read_bytes(), before_notify)
        self.assertEqual(self.phone_log.read_bytes(), before_phone)
        self.assertEqual(len(self.alert_rows()), before_rows)
        status = self.alert("status", "--run-dir", str(self.run_dir))
        self.assertEqual(json.loads(status.stdout)["unacknowledged"], 0)

    def test_warning_uses_local_delivery_only(self) -> None:
        self.alert(*self.raise_args("warning"))
        self.assertEqual(len(self.notify_log.read_text().splitlines()), 1)
        self.assertFalse(self.phone_log.exists())

    def test_all_delivery_failures_are_recorded_and_return_nonzero(self) -> None:
        self.notify = self.adapter("notify-fail", "EARSHOT_TEST_NOTIFY_LOG", 1)
        self.phone = self.adapter("phone-fail", "EARSHOT_TEST_PHONE_LOG", 1)
        self.alert(*self.raise_args(), expected=1)
        status = self.alert("status", "--run-dir", str(self.run_dir), expected=1)
        state = json.loads(status.stdout)["alerts"][0]
        self.assertFalse(state["delivered"])
        self.assertEqual([row["outcome"] for row in state["deliveries"]], ["failed", "failed", "failed"])

    def test_critical_local_success_cannot_mask_phone_failure_or_reset_retry_cap(self) -> None:
        self.phone = self.adapter("phone-fail", "EARSHOT_TEST_PHONE_LOG", 1)
        self.alert(*self.raise_args(), expected=1)
        before = self.phone_log.read_bytes()
        self.alert(*self.raise_args(), expected=1)
        self.assertEqual(self.phone_log.read_bytes(), before)
        self.assertEqual(len(before.splitlines()), 2)
        status = self.alert("status", "--run-dir", str(self.run_dir), expected=1)
        state = json.loads(status.stdout)["alerts"][0]
        self.assertTrue(state["delivered"], "local delivery remains visible")
        self.assertFalse(state["required_delivery_met"])

    def test_ambiguous_adapter_outcome_is_not_called_delivered(self) -> None:
        self.notify = self.adapter("notify-ambiguous", "EARSHOT_TEST_NOTIFY_LOG", 75)
        self.phone = self.adapter("phone-fail", "EARSHOT_TEST_PHONE_LOG", 1)
        self.alert(*self.raise_args(), expected=1)
        status = self.alert("status", "--run-dir", str(self.run_dir), expected=1)
        state = json.loads(status.stdout)["alerts"][0]
        self.assertTrue(state["ambiguous"])
        self.assertFalse(state["delivered"])

    def test_no_deliver_persists_request_without_running_adapters(self) -> None:
        result = self.alert(*self.raise_args(), "--no-deliver")
        self.assertRegex(result.stdout.strip(), r"^[0-9a-f]{64}$")
        self.assertFalse(self.notify_log.exists())
        self.assertFalse(self.phone_log.exists())
        self.assertEqual(len(self.alert_rows()), 1)

    def test_crash_ambiguous_phone_start_is_never_blind_redialed(self) -> None:
        result = self.alert(*self.raise_args(), "--no-deliver")
        alert_id = result.stdout.strip()
        started = {
            "schema": 1,
            "kind": "alert-delivery-started",
            "alert_id": alert_id,
            "channel": "phone",
            "attempt": 1,
        }
        self.eventctl(
            "compat",
            "--run-dir",
            str(self.run_dir),
            "--actor",
            "alertctl",
            "--action",
            f"alert.delivery_started:{alert_id}:phone:1",
            "--outcome",
            "info",
            "--detail",
            json.dumps(started, sort_keys=True, separators=(",", ":")),
            "--idempotent",
        )
        self.alert(*self.raise_args(), expected=1)
        self.assertFalse(self.phone_log.exists())
        status = self.alert("status", "--run-dir", str(self.run_dir), expected=1)
        state = json.loads(status.stdout)["alerts"][0]
        self.assertTrue(state["ambiguous"])
        self.assertTrue(state["delivered"], "the independent local adapter still delivered")


if __name__ == "__main__":
    unittest.main(verbosity=2)
