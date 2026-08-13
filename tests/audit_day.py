#!/usr/bin/env python3
"""Offline fixtures for scripts/audit-day.py."""

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
SCRIPT = REPO / "scripts" / "audit-day.py"
DAILY = REPO / "scripts" / "missed-arm-daily.sh"
DATE = "2026-08-12"
FINGERPRINT = "sha256:" + "a" * 64


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


class AuditDayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="audit-day-test."))
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "scripts").mkdir()
        self.eventctl = self.root / "scripts" / "fake-eventctl.py"
        self.auditor = self.root / "scripts" / "fake-auditor.py"
        self.alertctl = self.root / "scripts" / "fake-alertctl.py"
        self.eventctl.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json, sys
                from pathlib import Path
                command = sys.argv[1]
                run = Path(sys.argv[sys.argv.index("--run-dir") + 1])
                if (run / "control" / "verify-fail").exists():
                    print("fixture verification failure", file=sys.stderr)
                    raise SystemExit(1)
                if command == "verify":
                    print("OK")
                    raise SystemExit(0)
                reached = json.loads((run / "control" / "reached.json").read_text())
                ledger = (run / "control" / "receipts.jsonl").read_bytes()
                payload = {
                    "authority": {
                        "as_of_sequence": len(reached),
                        "as_of_receipt_sha256": __import__("hashlib").sha256(ledger).hexdigest(),
                    },
                    "state": {
                        "reached": [
                            {"transition": value, "sequence": index + 1, "ts": "2026-08-12T00:00:00Z"}
                            for index, value in enumerate(reached)
                        ],
                        "active": not any(value in reached for value in ("completed", "aborted", "failed", "blocked", "descope")),
                    },
                }
                print(json.dumps(payload))
                """
            )
        )
        self.auditor.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json, shutil, sys
                from pathlib import Path
                run = Path(sys.argv[sys.argv.index("--run-dir") + 1])
                marker = run / "control" / "auditor-called"
                marker.write_text("called\\n")
                if (run / "control" / "audit-fail").exists():
                    print("fixture audit failure", file=sys.stderr)
                    raise SystemExit(1)
                source = run / "grade" / "fixture.json"
                destination = run / "grade" / "verdict.json"
                shutil.copyfile(source, destination)
                reached_path = run / "control" / "reached.json"
                reached = json.loads(reached_path.read_text())
                if "graded" not in reached:
                    reached.append("graded")
                    reached_path.write_text(json.dumps(reached))
                    with (run / "control" / "receipts.jsonl").open("a") as stream:
                        stream.write("graded\\n")
                print("OK")
                """
            )
        )
        self.alertctl.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json, sys
                from pathlib import Path
                run = Path(sys.argv[sys.argv.index("--run-dir") + 1])
                marker = run / "control" / "unacknowledged-alerts"
                unacknowledged = int(marker.read_text()) if marker.exists() else 0
                alerts = [
                    {
                        "request": {
                            "alert_id": f"{index + 1:064x}",
                            "severity": "critical",
                            "code": "runtime_failed",
                        },
                        "delivered": True,
                        "ambiguous": False,
                        "acknowledged": False,
                    }
                    for index in range(unacknowledged)
                ]
                print(json.dumps({
                    "schema": 1,
                    "kind": "alert-status",
                    "alerts": alerts,
                    "unacknowledged": unacknowledged,
                }))
                raise SystemExit(1 if unacknowledged else 0)
                """
            )
        )

    def add_run(
        self,
        event: str,
        suffix: str,
        reached: list[str],
        *,
        outcome: str = "pass",
        audit_fail: bool = False,
        verify_fail: bool = False,
        date: str = DATE,
    ) -> Path:
        run_id = f"{date.replace('-', '')}T120000Z-p3-{suffix}"
        run = self.root / "var" / "runs" / event / run_id
        plan = {
            "schema": 3,
            "kind": "run-plan",
            "event_id": event,
            "run_id": run_id,
            "plan_sha256": hashlib.sha256(f"{event}/{run_id}".encode()).hexdigest(),
            "starts_at_utc": f"{date}T12:00:00Z",
            "lane": "webcast",
            "execution_host_id": "fixture-host",
            "artifacts": {"grade": "grade/verdict.json"},
        }
        write_json(run / "control" / "run-plan.json", plan)
        write_json(run / "control" / "reached.json", reached)
        (run / "control" / "receipts.jsonl").write_text(
            "".join(f"{transition}\n" for transition in reached)
        )
        grade = {
            "schema": 1,
            "kind": "run-grade",
            "event_id": event,
            "run_id": run_id,
            "plan_sha256": plan["plan_sha256"],
            "lane": "webcast",
            "execution_host_id": "fixture-host",
            "validation": {"status": "pass"},
            "operational_outcome": outcome,
            "accounting": {
                "intents": 2,
                "attempted": 1,
                "accepted": 1,
                "filled": 1,
                "terminal": 2,
                "unresolved": 0,
            },
            "detections": [{"keyword": "test"}, {"keyword": "test2"}],
            "orders": [
                {
                    "book_observation": {
                        "classification": "depth_exhausted_after_detection",
                        "detection_to_exhaustion_ms": 12.5,
                        "wire_to_exhaustion_ms": 8.0,
                    }
                },
                {"book_observation": {"classification": "empty_at_detection"}},
            ],
            "audio_evidence": {
                "frame_count": 25,
                "sample_count": 8000,
                "precision": "not_resolvable",
            },
            "fix_observation_evidence": {"status": "not_applicable_rest"},
        }
        write_json(run / "grade" / "fixture.json", grade)
        if audit_fail:
            (run / "control" / "audit-fail").touch()
        if verify_fail:
            (run / "control" / "verify-fail").touch()
        return run

    def write_slate(self, *events: str, date: str = DATE) -> Path:
        path = self.root / "slate" / "latest.json"
        write_json(
            path,
            {
                "generated_utc": f"{date}T00:00:00Z",
                "events": [
                    {
                        "event_ticker": event,
                        "issuer": event,
                        "call_date": date,
                        "call_time_utc": f"{date}T12:00:00Z",
                        "call_date_evidence": "fixture",
                    }
                    for event in events
                ],
            },
        )
        return path

    def run_day(self, date: str = DATE) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["EARSHOT_REPO"] = str(self.root)
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--date",
                date,
                "--auditor",
                str(self.auditor),
                "--eventctl",
                str(self.eventctl),
                "--alertctl",
                str(self.alertctl),
            ],
            text=True,
            capture_output=True,
            env=env,
        )

    def index(self, date: str = DATE) -> dict:
        return json.loads((self.root / "var" / "audits" / f"{date}.json").read_text())

    def test_continues_siblings_and_fails_active_or_unauditable(self) -> None:
        healthy = self.add_run("A", "aaaaaaaaaaaa", ["completed"])
        broken = self.add_run("B", "bbbbbbbbbbbb", ["failed"], audit_fail=True)
        active = self.add_run("C", "cccccccccccc", ["launched"])
        result = self.run_day()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        rows = {row["event_id"]: row for row in self.index()["runs"]}
        self.assertEqual(rows["A"]["status"], "audited")
        self.assertEqual(rows["B"]["status"], "audit_failed")
        self.assertEqual(rows["C"]["status"], "active")
        self.assertTrue((healthy / "control" / "auditor-called").exists())
        self.assertTrue((broken / "control" / "auditor-called").exists())
        self.assertFalse((active / "control" / "auditor-called").exists())

    def test_green_index_is_byte_stable_and_aggregates_performance(self) -> None:
        self.add_run("A", "aaaaaaaaaaaa", ["completed"])
        self.add_run("B", "bbbbbbbbbbbb", ["descope"])
        first = self.run_day()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        path = self.root / "var" / "audits" / f"{DATE}.json"
        before = path.read_bytes()
        second = self.run_day()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(before, path.read_bytes())
        payload = self.index()
        self.assertEqual(payload["overall"], "pass")
        self.assertEqual(payload["summary"]["attempts"], 2)
        self.assertEqual(payload["summary"]["accounting"]["filled"], 1)
        self.assertEqual(payload["summary"]["detection_count"], 2)
        self.assertEqual(payload["summary"]["audio_frame_count"], 25)
        classifications = {
            event["event_ticker"]: event["classification"]
            for event in payload["events"]
        }
        self.assertEqual(classifications, {"A": "COVERED", "B": "DESCOPED"})
        covered = next(event for event in payload["events"] if event["event_ticker"] == "A")
        self.assertIn("grade/verdict.json", "\n".join(covered["evidence_links"]))

    def test_slate_event_without_any_attempt_is_explicit_missed(self) -> None:
        self.write_slate("A", "MISSING")
        self.add_run("A-WEBCAST", "aaaaaaaaaaaa", ["completed"])
        result = self.run_day()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = self.index()
        events = {event["event_ticker"]: event for event in payload["events"]}
        self.assertEqual(events["A"]["classification"], "COVERED")
        self.assertEqual(events["MISSING"]["classification"], "MISSED")
        self.assertEqual(events["MISSING"]["attempts"], [])
        self.assertIn("slate/latest.json", events["MISSING"]["evidence_links"])
        self.assertEqual(payload["summary"]["classifications"], {
            "COVERED": 1,
            "MISSED": 1,
        })

    def test_daily_wrapper_runs_audit_after_missed_arm_and_pages_failure(self) -> None:
        wrapper = self.root / "scripts" / "missed-arm-daily.sh"
        shutil.copy2(DAILY, wrapper)
        order_log = self.root / "order.log"
        alert_log = self.root / "alerts.log"
        checker = self.root / "scripts" / "checker.py"
        day = self.root / "scripts" / "day.py"
        pager = self.root / "scripts" / "pager.py"
        checker.write_text(
            "import os,sys\nopen(os.environ['ORDER_LOG'],'a').write('missed\\n')\n"
            "raise SystemExit(int(os.environ.get('CHECK_CODE','0')))\n"
        )
        day.write_text(
            "import os,sys\nopen(os.environ['ORDER_LOG'],'a').write('audit\\n')\n"
            "raise SystemExit(int(os.environ.get('AUDIT_CODE','0')))\n"
        )
        pager.write_text(
            "#!/usr/bin/env python3\nimport os,sys\n"
            "open(os.environ['ALERT_LOG'],'a').write(' '.join(sys.argv[1:])+'\\n')\n"
        )
        pager.chmod(0o755)
        env = {
            **os.environ,
            "EARSHOT_REPO": str(self.root),
            "EARSHOT_PYTHON": sys.executable,
            "EARSHOT_MISSED_ARM_CHECK": str(checker),
            "EARSHOT_AUDIT_DAY": str(day),
            "EARSHOT_ALERTCTL": str(pager),
            "EARSHOT_ALERT_DIR": str(self.root / "alerts"),
            "EARSHOT_DATE_UTC": DATE,
            "ORDER_LOG": str(order_log),
            "ALERT_LOG": str(alert_log),
            "AUDIT_CODE": "1",
        }
        result = subprocess.run(
            ["bash", str(wrapper)], cwd=self.root, env=env,
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(order_log.read_text().splitlines(), ["missed", "audit"])
        self.assertIn("audit_day_failed", alert_log.read_text())

    def test_operational_failure_is_a_failed_day_with_valid_evidence(self) -> None:
        self.add_run("A", "aaaaaaaaaaaa", ["completed"], outcome="fail")
        result = self.run_day()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        row = self.index()["runs"][0]
        self.assertEqual(row["status"], "operational_fail")
        self.assertEqual(row["performance"]["operational_outcome"], "fail")

    def test_unacknowledged_proof_alert_keeps_an_audited_run_red(self) -> None:
        run = self.add_run("A", "aaaaaaaaaaaa", ["completed"])
        (run / "control" / "unacknowledged-alerts").write_text("1\n")
        result = self.run_day()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = self.index()
        row = payload["runs"][0]
        self.assertEqual(row["status"], "audited")
        self.assertTrue(row["problem"])
        self.assertEqual(row["alerts"]["unacknowledged"], 1)
        self.assertEqual(payload["summary"]["unacknowledged_alerts"], 1)

    def test_no_runs_is_loud_and_publishes_explicit_empty_denominator(self) -> None:
        result = self.run_day()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = self.index()
        self.assertTrue(payload["summary"]["no_runs"])
        self.assertEqual(payload["summary"]["attempts"], 0)

    def test_corrupt_default_runplan_stays_in_date_denominator(self) -> None:
        run = self.root / "var" / "runs" / "BROKEN" / "20260812T130000Z-p3-deadbeefdead"
        (run / "control").mkdir(parents=True)
        (run / "control" / "run-plan.json").write_text("{not-json\n")
        result = self.run_day()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        row = self.index()["runs"][0]
        self.assertEqual(row["status"], "invalid")
        self.assertIn("invalid JSON", row["detail"])

    def test_other_dates_are_excluded(self) -> None:
        self.add_run("OTHER", "aaaaaaaaaaaa", ["completed"], date="2026-08-13")
        result = self.run_day()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.index()["runs"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
