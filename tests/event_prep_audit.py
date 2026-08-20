#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import datetime as dt
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

SPEC = importlib.util.spec_from_file_location("event_prep_audit", AUDIT)
assert SPEC and SPEC.loader
AUDIT_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT_MODULE)


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
            f'''#!/usr/bin/env bash
target="${{2:-}}"
case "$target" in
  */com.earshot.watchdog.test.1205|*/com.earshot.watchdog.test.1235)
    cat <<'EOF'
arguments = {{
    /bin/bash
    {root.resolve()}/scripts/event-watchdog.sh
    --run
    --leg
    {EVENT}:{RUN}:local
}}
EOF
    case "$target" in
      *.1205) minute=5 ;;
      *) minute=35 ;;
    esac
    echo '"Month" => 8'
    echo '"Day" => 20'
    echo '"Hour" => 12'
    echo '"Minute" => '"$minute"
    ;;
  */com.earshot.push.test)
    cat <<'EOF'
arguments = {{
    /bin/bash
    {root.resolve()}/scripts/url-push-job.sh
    --run
    --slug
    test
    --until-utc
    12:15
    --pcm-secs
    6
    --run-dir
    {root.resolve()}/var/runs/{EVENT}/{RUN}
    --host
    local
}}
EOF
    echo '"Month" => 8'
    echo '"Day" => 20'
    echo '"Hour" => 11'
    echo '"Minute" => 45'
    ;;
  *)
    echo com.earshot.watchdog.test.1205
    echo com.earshot.watchdog.test.1235
    echo com.earshot.push.test
    ;;
esac
'''
        )
        launchctl.chmod(0o755)
        env = {
            **os.environ,
            "EARSHOT_REPO": str(root),
            "EARSHOT_LOCAL_EXECUTION_HOST": "laptop",
            "EARSHOT_BOX_EXECUTION_HOST": "box",
            "_EARSHOT_EVENT_PREP_LAUNCHCTL": str(launchctl),
            "TZ": "UTC",
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

    def test_loaded_watchdog_with_wrong_leg_identity_is_not_ready(self) -> None:
        root, env = self.fixture()
        self.write_slate(root)
        self.write_ready_run(root)
        launchctl = Path(env["_EARSHOT_EVENT_PREP_LAUNCHCTL"])
        launchctl.write_text(f'''#!/usr/bin/env bash
target="${{2:-}}"
case "$target" in
  */com.earshot.watchdog.test.1205|*/com.earshot.watchdog.test.1235)
    cat <<'EOF'
arguments = {{
    /bin/bash
    {root.resolve()}/scripts/event-watchdog.sh
    --run
    --leg
    {EVENT}:var/runs/{EVENT}/{RUN}:local
}}
EOF
    case "$target" in
      *.1205) minute=5 ;;
      *) minute=35 ;;
    esac
    echo '"Month" => 8'
    echo '"Day" => 20'
    echo '"Hour" => 12'
    echo '"Minute" => '"$minute"
    ;;
  *)
    echo com.earshot.watchdog.test.1205
    echo com.earshot.watchdog.test.1235
    echo com.earshot.push.test
    ;;
esac
''')
        result = self.run_audit(root, env)
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["events"][0]["status"], "NEEDS-SUPPORT")
        self.assertIn("loaded argv invalid", payload["events"][0]["details"][0])

    def test_two_watchdogs_at_arbitrary_times_are_not_ready(self) -> None:
        root, env = self.fixture()
        self.write_slate(root)
        self.write_ready_run(root)
        launchctl = Path(env["_EARSHOT_EVENT_PREP_LAUNCHCTL"])
        launchctl.write_text(
            "#!/usr/bin/env bash\n"
            "echo com.earshot.watchdog.test.1206\n"
            "echo com.earshot.watchdog.test.1236\n"
        )
        result = self.run_audit(root, env)
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["events"][0]["status"], "NEEDS-SUPPORT")
        self.assertIn("exact start+5/start+35 watchdogs missing", payload["events"][0]["details"][0])

    def test_exact_watchdog_labels_on_wrong_date_are_not_ready(self) -> None:
        root, env = self.fixture()
        self.write_slate(root)
        self.write_ready_run(root)
        launchctl = Path(env["_EARSHOT_EVENT_PREP_LAUNCHCTL"])
        launchctl.write_text(launchctl.read_text().replace('"Day" => 20', '"Day" => 19'))
        result = self.run_audit(root, env)
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["events"][0]["status"], "NEEDS-SUPPORT")
        self.assertIn("wrong calendar date/time", payload["events"][0]["details"][0])

    def test_missing_posture_is_a_distinct_blocker(self) -> None:
        root, env = self.fixture()
        self.write_slate(root)
        self.write_ready_run(root)
        config = root / "configs" / "event.toml"
        config.write_text(config.read_text().replace("  # posture: HIGH — measured", ""))
        result = self.run_audit(root, env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["events"][0]["status"], "NEEDS-POSTURE")

    def test_resolved_hls_without_survivor_sources_is_not_ready(self) -> None:
        root, env = self.fixture()
        self.write_slate(root)
        run = self.write_ready_run(root)
        (run / "control" / "access-plan.json").write_text(json.dumps({
            "schema": 2, "resolver": {
                "kind": "preresolved", "deadline_utc": f"{DATE}T12:15:00Z",
            },
        }))
        result = self.run_audit(root, env)
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["events"][0]["status"], "NEEDS-TRANSPORT")
        self.assertIn("config sources are missing", payload["events"][0]["details"][0])

    def test_resolved_hls_with_both_survivor_sources_is_ready(self) -> None:
        root, env = self.fixture()
        self.write_slate(root)
        run = self.write_ready_run(root)
        (run / "control" / "access-plan.json").write_text(json.dumps({
            "schema": 2, "resolver": {
                "kind": "preresolved", "deadline_utc": f"{DATE}T12:15:00Z",
            },
        }))
        config = root / "configs" / "event.toml"
        config.write_text(config.read_text() + '''
[[sources]]
kind = "pipe"
cmd = ["python3", "scripts/hls-edge-fetch.py"]

[[sources]]
kind = "pipe"
cmd = ["/bin/sh", "-c", "scripts/cdn-cachebust-fetch.sh $HOME/test-stream.url"]
''')
        result = self.run_audit(root, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["ready"])

    def test_url_push_job_with_wrong_run_target_is_not_ready(self) -> None:
        root, env = self.fixture()
        self.write_slate(root)
        run = self.write_ready_run(root)
        (run / "control" / "access-plan.json").write_text(json.dumps({
            "schema": 2, "resolver": {
                "kind": "preresolved", "deadline_utc": f"{DATE}T12:15:00Z",
            },
        }))
        config = root / "configs" / "event.toml"
        config.write_text(config.read_text() + '''
[[sources]]
kind = "pipe"
cmd = ["python3", "scripts/hls-edge-fetch.py"]

[[sources]]
kind = "pipe"
cmd = ["/bin/sh", "-c", "scripts/cdn-cachebust-fetch.sh $HOME/test-stream.url"]
''')
        launchctl = Path(env["_EARSHOT_EVENT_PREP_LAUNCHCTL"])
        exact = f"{root.resolve()}/var/runs/{EVENT}/{RUN}"
        launchctl.write_text(launchctl.read_text().replace(exact, exact + "-wrong"))
        result = self.run_audit(root, env)
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["events"][0]["status"], "NEEDS-SUPPORT")
        self.assertIn("targets do not match", payload["events"][0]["details"][0])

    def test_url_push_job_on_wrong_calendar_is_not_ready(self) -> None:
        root, env = self.fixture()
        self.write_slate(root)
        run = self.write_ready_run(root)
        (run / "control" / "access-plan.json").write_text(json.dumps({
            "schema": 2, "resolver": {
                "kind": "preresolved", "deadline_utc": f"{DATE}T12:15:00Z",
            },
        }))
        config = root / "configs" / "event.toml"
        config.write_text(config.read_text() + '''
[[sources]]
kind = "pipe"
cmd = ["python3", "scripts/hls-edge-fetch.py"]

[[sources]]
kind = "pipe"
cmd = ["/bin/sh", "-c", "scripts/cdn-cachebust-fetch.sh $HOME/test-stream.url"]
''')
        launchctl = Path(env["_EARSHOT_EVENT_PREP_LAUNCHCTL"])
        launchctl.write_text(launchctl.read_text().replace('"Hour" => 11', '"Hour" => 9'))
        result = self.run_audit(root, env)
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["events"][0]["status"], "NEEDS-SUPPORT")
        self.assertIn("T-60..T0", payload["events"][0]["details"][0])

    def test_failover_proof_and_loaded_argv_are_exactly_bound(self) -> None:
        root, env = self.fixture()
        local_run = root / "var" / "runs" / EVENT / RUN
        box_event = TICKER + "-BOX-WEBCAST"
        box_run_id = RUN + "-box"
        box_run = root / "var" / "runs" / box_event / box_run_id
        (local_run / "control").mkdir(parents=True)
        box_run.mkdir(parents=True)
        plan_sha = "a" * 64
        local = {"run": local_run, "plan": {
            "event_id": EVENT, "run_id": RUN, "plan_sha256": plan_sha,
            "starts_at_utc": f"{DATE}T12:00:00Z",
        }, "access": {"resolver": {"deadline_utc": f"{DATE}T12:15:00Z"}}}
        box = {"run": box_run, "plan": {
            "event_id": box_event, "run_id": box_run_id,
        }}
        proof_dir = root / "state" / EVENT
        proof_dir.mkdir(parents=True)
        proof = {
            "schema": 1,
            "kind": "laptop-failover-install-proof",
            "label": f"com.earshot.run.{plan_sha[:20]}",
            "laptop_run": str(local_run),
            "box_event": box_event,
            "box_run": box_run_id,
            "start_utc": "12:00",
            "grace_min": 2,
            "deadline_utc": "12:15",
            "box": "fixture-box",
        }
        proof_path = proof_dir / "failover-install.json"
        proof_path.write_text(json.dumps(proof))
        fire = dt.datetime.fromisoformat(f"{DATE}T10:45:00+00:00")
        local_fire = fire.astimezone()
        launcher = ["/bin/echo"]
        (local_run / "control" / "schedule-proof.json").write_text(json.dumps({
            "readback": {
                "fire_at_utc": f"{DATE}T10:45:00Z",
                "program_arguments": launcher,
            },
        }))
        launchctl = Path(env["_EARSHOT_EVENT_PREP_LAUNCHCTL"])
        launchctl.write_text(f'''#!/usr/bin/env bash
cat <<'EOF'
arguments = {{
    /bin/bash
    {root.resolve()}/scripts/laptop-failover.sh
    --event
    {EVENT}
    --box-event
    {box_event}
    --box-run
    {box_run_id}
    --start-utc
    12:00
    --grace-min
    2
    --deadline-utc
    12:15
    --box
    fixture-box
    --
    /bin/echo
}}
EOF
echo '"Month" => {local_fire.month}'
echo '"Day" => {local_fire.day}'
echo '"Hour" => {local_fire.hour}'
echo '"Minute" => {local_fire.minute}'
''')
        old = os.environ.get("_EARSHOT_EVENT_PREP_LAUNCHCTL")
        os.environ["_EARSHOT_EVENT_PREP_LAUNCHCTL"] = str(launchctl)
        self.addCleanup(
            lambda: os.environ.pop("_EARSHOT_EVENT_PREP_LAUNCHCTL", None)
            if old is None else os.environ.__setitem__("_EARSHOT_EVENT_PREP_LAUNCHCTL", old)
        )
        self.assertEqual(AUDIT_MODULE.failover_support_issues(root.resolve(), local, box), [])
        proof["box_run"] = "wrong-run"
        proof_path.write_text(json.dumps(proof))
        self.assertIn(
            "box_run does not match exact host-pair identity",
            AUDIT_MODULE.failover_support_issues(root.resolve(), local, box),
        )


if __name__ == "__main__":
    unittest.main()
