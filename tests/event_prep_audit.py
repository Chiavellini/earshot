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
AUDIT = REPO / "scripts" / "event-prep-audit.py"
DATE = "2026-08-20"
TICKER = "KXEARNINGSMENTIONTEST-26AUG20"
EVENT = TICKER + "-WEBCAST"
RUN = "20260820T120000Z-p3-" + "a" * 12


class EventPrepAuditTest(unittest.TestCase):
    def fixture(self) -> tuple[Path, dict[str, str]]:
        root = Path(tempfile.mkdtemp(prefix="event-prep-audit."))
        self.addCleanup(shutil.rmtree, root)
        (root / "slate").mkdir()
        (root / "configs").mkdir()
        (root / "configs" / ".readiness").mkdir()
        (root / "scripts").mkdir()
        shutil.copy2(AUDIT, root / "scripts" / AUDIT.name)
        launchctl = root / "launchctl"
        launchctl.write_text(
            "#!/usr/bin/env bash\n"
            "echo com.earshot.watchdog.test.1205\n"
            "echo com.earshot.watchdog.test.1235\n"
        )
        launchctl.chmod(0o755)
        env = {
            **os.environ,
            "EARSHOT_REPO": str(root),
            "_EARSHOT_EVENT_PREP_LAUNCHCTL": str(launchctl),
        }
        return root, env

    def write_slate(self, root: Path) -> None:
        (root / "slate" / "latest.json").write_text(json.dumps({"events": [{
            "event_ticker": TICKER, "issuer": "TEST", "call_date": DATE,
        }]}))

    def write_ready_run(self, root: Path) -> Path:
        run = root / "var" / "runs" / EVENT / RUN
        (run / "control").mkdir(parents=True)
        config = root / "configs" / "event.toml"
        config.write_text(
            f'''[event]\nid = "{EVENT}"\nstarts_at = "{DATE}T12:00:00Z"\n\n'''
            f'''[[keywords]]\nmarket_ticker = "{TICKER}-WORD"\nphrase = "word"\nvariants = ["word"]\nmax_price_cents = 97  # posture: HIGH — measured\nbudget_contracts = 1\n'''
        )
        plan = {
            "schema": 3, "kind": "run-plan", "event_id": EVENT, "run_id": RUN,
            "starts_at_utc": f"{DATE}T12:00:00Z", "market_event_ticker": TICKER,
            "lane": "webcast", "execution_host_id": "laptop", "config_path": "configs/event.toml",
        }
        (run / "control" / "run-plan.json").write_text(json.dumps(plan))
        transitions = ["prepared", "access_acquired", "study_completed", "preflight_passed", "scheduled"]
        status = {"state": {"active": True, "terminal": None, "reached": [
            {"transition": transition} for transition in transitions
        ]}}
        (run / "control" / "status.json").write_text(json.dumps(status))
        (run / "control" / "access-plan.json").write_text(json.dumps({
            "schema": 2, "resolver": {"kind": "static-public"},
        }))
        (run / "control" / "schedule-proof.json").write_text("{}")
        (root / "configs" / ".readiness" / "event.json").write_text(json.dumps({
            "keywords": [{"ticker": TICKER + "-WORD", "confirmed": 1, "clips": 1}],
        }))
        return run

    def run_audit(self, root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(root / "scripts" / AUDIT.name), "--date", DATE, "--json"],
            cwd=root, env=env, text=True, capture_output=True,
        )

    def test_discovered_event_without_run_is_not_ready(self) -> None:
        root, env = self.fixture()
        self.write_slate(root)
        result = self.run_audit(root, env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["events"][0]["status"], "NEEDS-STAGE")

    def test_complete_single_host_event_is_ready(self) -> None:
        root, env = self.fixture()
        self.write_slate(root)
        self.write_ready_run(root)
        result = self.run_audit(root, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["ready"])

    def test_missing_posture_is_a_distinct_blocker(self) -> None:
        root, env = self.fixture()
        self.write_slate(root)
        self.write_ready_run(root)
        config = root / "configs" / "event.toml"
        config.write_text(config.read_text().replace("  # posture: HIGH — measured", ""))
        result = self.run_audit(root, env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["events"][0]["status"], "NEEDS-POSTURE")


if __name__ == "__main__":
    unittest.main()
