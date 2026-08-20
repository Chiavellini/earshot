#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "support-nightly.py"
DATE = "2026-08-21"
TICKER = "KXEARNINGSMENTIONAUTO-26AUG21"


class SupportNightlyTest(unittest.TestCase):
    def fixture(self) -> tuple[Path, dict[str, str], Path]:
        root = Path(tempfile.mkdtemp(prefix="support-nightly."))
        self.addCleanup(shutil.rmtree, root)
        (root / "scripts").mkdir()
        shutil.copy2(SCRIPT, root / "scripts" / SCRIPT.name)
        calls = root / "calls.jsonl"
        stub = root / "stub.py"
        stub.write_text('''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["SUPPORT_CALLS"], "a") as handle:
    handle.write(json.dumps(sys.argv) + "\\n")
''')
        stub.chmod(0o755)
        env = {
            **os.environ,
            "EARSHOT_REPO": str(root),
            "EARSHOT_LOCAL_EXECUTION_HOST": "laptop-test",
            "EARSHOT_BOX_EXECUTION_HOST": "box-test",
            "EARSHOT_BOX": "ubuntu@test-box",
            "_EARSHOT_SUPPORT_NOW_UTC": f"{DATE}T10:00:00Z",
            "_EARSHOT_SUPPORT_WATCHDOG": str(stub),
            "_EARSHOT_SUPPORT_FAILOVER": str(stub),
            "_EARSHOT_SUPPORT_PUSH": str(stub),
            "SUPPORT_CALLS": str(calls),
        }
        return root, env, calls

    def add_run(self, root: Path, event: str, run_id: str, host: str,
                scheduled: bool = True, lane: str = "webcast") -> Path:
        run = root / "var" / "runs" / event / run_id
        (run / "control").mkdir(parents=True)
        plan = {
            "schema": 3,
            "kind": "run-plan",
            "event_id": event,
            "run_id": run_id,
            "starts_at_utc": f"{DATE}T12:00:00Z",
            "market_event_ticker": TICKER,
            "execution_host_id": host,
            "lane": lane,
        }
        (run / "control" / "run-plan.json").write_text(json.dumps(plan))
        transitions = ["scheduled"] if scheduled else ["preflight_passed"]
        status = {"state": {"active": True, "terminal": None, "reached": [
            {"transition": transition} for transition in transitions
        ]}}
        (run / "control" / "status.json").write_text(json.dumps(status))
        if lane == "webcast":
            (run / "control" / "access-plan.json").write_text(json.dumps({
                "schema": 2,
                "resolver": {
                    "kind": "preresolved",
                    "deadline_utc": f"{DATE}T12:15:00Z",
                },
            }))
        if scheduled:
            (run / "control" / "schedule-proof.json").write_text("{}")
        return run

    def invoke(self, root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(root / "scripts" / SCRIPT.name), "--date", DATE],
            cwd=root, env=env, text=True, capture_output=True,
        )

    def test_two_host_preresolved_event_installs_complete_support(self) -> None:
        root, env, calls = self.fixture()
        local = self.add_run(root, TICKER + "-WEBCAST", "local-run", "laptop-test")
        box = self.add_run(root, TICKER + "-BOX-WEBCAST", "box-run", "box-test")
        result = self.invoke(root, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        invocations = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual(len(invocations), 4)
        push, first_watchdog, second_watchdog, failover = invocations
        self.assertIn("--at-utc", push)
        self.assertEqual(push[push.index("--at-utc") + 1], "11:45")
        self.assertIn(str(local.resolve()), push)
        self.assertIn(str(box.resolve()), push)
        self.assertEqual(first_watchdog[first_watchdog.index("--at-utc") + 1], "12:05")
        self.assertEqual(second_watchdog[second_watchdog.index("--at-utc") + 1], "12:35")
        self.assertEqual(failover[failover.index("--box-run") + 1], "box-run")
        self.assertEqual(failover[failover.index("--run-dir") + 1], str(local.resolve()))

    def test_incomplete_host_pair_installs_nothing(self) -> None:
        root, env, calls = self.fixture()
        self.add_run(root, TICKER + "-WEBCAST", "local-run", "laptop-test")
        self.add_run(root, TICKER + "-BOX-WEBCAST", "box-run", "box-test", scheduled=False)
        result = self.invoke(root, env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("every active host RunPlan must be scheduled", result.stderr)
        self.assertFalse(calls.exists())

    def test_unknown_execution_host_installs_nothing(self) -> None:
        root, env, calls = self.fixture()
        self.add_run(root, TICKER + "-WEBCAST", "local-run", "unknown-host")
        result = self.invoke(root, env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("unsupported execution host", result.stderr)
        self.assertFalse(calls.exists())

    def test_malformed_access_plan_refuses_without_partial_install(self) -> None:
        root, env, calls = self.fixture()
        run = self.add_run(root, TICKER + "-WEBCAST", "local-run", "laptop-test")
        (run / "control" / "access-plan.json").write_text(json.dumps({
            "schema": 2, "resolver": {"kind": "preresolved"},
        }))
        result = self.invoke(root, env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("AccessPlan resolver/deadline is invalid", result.stderr)
        self.assertFalse(calls.exists())

    def test_duplicate_active_attempt_refuses_without_partial_install(self) -> None:
        root, env, calls = self.fixture()
        event = TICKER + "-WEBCAST"
        self.add_run(root, event, "local-run-a", "laptop-test")
        self.add_run(root, event, "local-run-b", "laptop-test")
        result = self.invoke(root, env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("multiple active attempts", result.stderr)
        self.assertFalse(calls.exists())

    def test_single_box_phone_event_needs_only_two_watchdogs(self) -> None:
        root, env, calls = self.fixture()
        self.add_run(root, TICKER + "-PHONE", "phone-run", "box-test", lane="phone")
        result = self.invoke(root, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        invocations = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual(len(invocations), 2)
        self.assertTrue(all("--leg" in invocation for invocation in invocations))
        self.assertTrue(all(any("ubuntu@test-box" in token for token in invocation) for invocation in invocations))


if __name__ == "__main__":
    unittest.main()
