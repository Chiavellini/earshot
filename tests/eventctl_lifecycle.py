#!/usr/bin/env python3
"""Lifecycle-ledger tests for scripts/eventctl.py (node control.operate).

Every test runs in an isolated temporary repository — never the working
tree. Most cases drive the module in-process via main(argv); the
concurrency case and one end-to-end lifecycle case spawn the real CLI in
subprocesses, because cross-process flock semantics and the entrypoint
contract are themselves under test.

Run: python3 tests/eventctl_lifecycle.py
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "eventctl.py"

_spec = importlib.util.spec_from_file_location("eventctl", SCRIPT)
eventctl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eventctl)

DATE = "2026-08-12"
PHONE_EVENT = "KXEARNINGSMENTIONTEST-26AUG12-PHONE"
WC_EVENT = "KXEARNINGSMENTIONTEST-26AUG12-WEBCAST"

PHONE_CONFIG = """\
[event]
id        = "{event}"
starts_at = "{starts_at}"

[[sources]]
kind = "twilio_stream"
bind = "127.0.0.1:8080"
{keywords}
"""

WEBCAST_CONFIG = """\
[event]
id        = "{event}"
starts_at = "{starts_at}"

[arbiter]
mode = "fanout"

[[sources]]
kind = "pipe"
label = "streamlink"

[[sources]]
kind = "pipe"
label = "pipe_tuned"
{keywords}
"""

KEYWORDS = """
[[keywords]]
market_ticker = "KXEARNINGSMENTIONTEST-26AUG12-FOO"
phrase = "foo"
"""

ENV_TEMPLATE = """\
EVENT_NAME="TEST"
CONFIG_PATH="{config}"
EVENT_DATE="{date}"
ACCOUNT_ENV="{account}"
BIND_PORT=8080
"""


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="eventctl-test-")
        self.repo = Path(self._tmp.name).resolve()
        (self.repo / "configs").mkdir()
        (self.repo / "state").mkdir()
        self._old_env = os.environ.get("EARSHOT_REPO")
        self._old_cwd = os.getcwd()
        os.environ["EARSHOT_REPO"] = str(self.repo)
        self._old_host_id = os.environ.get("EARSHOT_HOST_ID")
        self._old_account_fingerprint = os.environ.get(
            "EARSHOT_KALSHI_ACCOUNT_FINGERPRINT")
        os.environ["EARSHOT_HOST_ID"] = "test-host"
        os.environ["EARSHOT_KALSHI_ACCOUNT_FINGERPRINT"] = (
            "sha256:" + "a" * 64)
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self._old_cwd)
        if self._old_env is None:
            os.environ.pop("EARSHOT_REPO", None)
        else:
            os.environ["EARSHOT_REPO"] = self._old_env
        if self._old_host_id is None:
            os.environ.pop("EARSHOT_HOST_ID", None)
        else:
            os.environ["EARSHOT_HOST_ID"] = self._old_host_id
        if self._old_account_fingerprint is None:
            os.environ.pop("EARSHOT_KALSHI_ACCOUNT_FINGERPRINT", None)
        else:
            os.environ["EARSHOT_KALSHI_ACCOUNT_FINGERPRINT"] = (
                self._old_account_fingerprint)
        self._tmp.cleanup()

    # -- fixtures -----------------------------------------------------------

    def write_phone_config(self, name="test-phone-arm.toml", event=PHONE_EVENT,
                           starts_at=f"{DATE}T21:00:00Z", keywords=True):
        p = self.repo / "configs" / name
        p.write_text(PHONE_CONFIG.format(
            event=event, starts_at=starts_at,
            keywords=KEYWORDS if keywords else ""))
        return p

    def write_webcast_config(self, name="webcast-test-arm-wc.toml",
                             event=WC_EVENT, starts_at=f"{DATE}T21:00:00Z",
                             keywords=True):
        p = self.repo / "configs" / name
        p.write_text(WEBCAST_CONFIG.format(
            event=event, starts_at=starts_at,
            keywords=KEYWORDS if keywords else ""))
        return p

    def write_env(self, config_rel, name="test-autorun.env", date=DATE,
                  account="$HOME/.earshot/ms.env"):
        p = self.repo / "configs" / name
        p.write_text(ENV_TEMPLATE.format(config=config_rel, date=date,
                                         account=account))
        return p

    # -- invocation ---------------------------------------------------------

    def cli(self, *argv):
        """In-process main(); returns (rc, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = eventctl.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def cli_sub(self, *argv):
        """Real-CLI subprocess; returns CompletedProcess."""
        env = os.environ.copy()
        env["EARSHOT_REPO"] = str(self.repo)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *argv],
            cwd=self.repo, env=env, capture_output=True, text=True,
        )

    def init_phone(self, **kw):
        cfg = self.write_phone_config(**kw)
        rc, out, err = self.cli("init", "--config", str(cfg))
        self.assertEqual(rc, 0, err or out)
        return Path(out.split(": ", 1)[1].strip())

    def run_dir_for(self, event):
        base = self.repo / "var" / "runs" / event
        runs = sorted(d for d in base.iterdir() if d.is_dir())
        self.assertEqual(len(runs), 1)
        return runs[0]

    def receipts_of(self, run_dir):
        return (run_dir / "control" / "receipts.jsonl").read_bytes()

    @contextlib.contextmanager
    def profile_identity_mode(self):
        """Remove global recovery overrides so an external profile is used."""
        keys = (
            "EARSHOT_EXECUTION_HOST",
            "EARSHOT_HOST_ID",
            "EARSHOT_KALSHI_ACCOUNT_FINGERPRINT",
            "EARSHOT_IDENTITY_HELPER",
            "EARSHOT_BIN",
        )
        previous = {key: os.environ.pop(key, None) for key in keys}
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is not None:
                    os.environ[key] = value

    def fake_identity_tools(self):
        helper = self.repo / "identity-helper"
        helper.write_text("""#!/usr/bin/env python3
import json, pathlib, sys
args = sys.argv[1:]
assert args[0] == "resolve"
account = args[args.index("--account") + 1]
config = pathlib.Path(args[args.index("--config") + 1])
binary = pathlib.Path(args[args.index("--bin") + 1])
assert config.is_file() and binary.is_file()
digit = "b" if "intc.env" in account else "a"
print(json.dumps({
    "schema": 1,
    "kind": "execution-identity",
    "execution_host_id": "box-useast1",
    "account_fingerprint": "sha256:" + digit * 64,
}, sort_keys=True, separators=(",", ":")))
""")
        binary = self.repo / "earshot-fake"
        binary.write_text("#!/bin/sh\nexit 0\n")
        helper.chmod(0o700)
        binary.chmod(0o700)
        return helper, binary

    def advance_to_preflight(self, event, lane="phone"):
        gate = "admission_resolved" if lane == "phone" else "access_acquired"
        for t in ("prepared", gate, "study_completed", "preflight_passed"):
            rc, out, err = self.cli("record", "--event", event,
                                    "--transition", t)
            self.assertEqual(rc, 0, f"{t}: {err or out}")

    def advance_to_launched(self, event, lane="phone"):
        self.advance_to_preflight(event, lane)
        for t in ("scheduled", "capture_ready", "launched"):
            rc, out, err = self.cli("record", "--event", event,
                                    "--transition", t)
            self.assertEqual(rc, 0, f"{t}: {err or out}")


class TestEndToEndReplay(Base):
    def test_full_lifecycle_and_replay_reconstructs_identical_state(self):
        cfg = self.write_phone_config()
        r = self.cli_sub("init", "--config", str(cfg))
        self.assertEqual(r.returncode, 0, r.stderr)
        chain = ["prepared", "admission_resolved", "study_completed",
                 "preflight_passed", "scheduled", "capture_ready", "launched",
                 "source_attached", "supervising", "draining", "completed",
                 "evidence_pulled", "graded", "archived"]
        for t in chain:
            r = self.cli_sub("record", "--event", PHONE_EVENT,
                             "--transition", t, "--actor", "e2e")
            self.assertEqual(r.returncode, 0, f"{t}: {r.stderr}")
        # archived closes the run: a NEW transition refuses; a repeat of an
        # already-reached one stays an idempotent no-op (writes nothing)
        r = self.cli_sub("record", "--event", PHONE_EVENT,
                         "--transition", "failed")
        self.assertEqual(r.returncode, 1)
        r = self.cli_sub("record", "--event", PHONE_EVENT,
                         "--transition", "prepared")
        self.assertEqual(r.returncode, 0)
        self.assertIn("idempotent", r.stdout)

        run_dir = self.run_dir_for(PHONE_EVENT)
        plan = json.loads((run_dir / "control" / "run-plan.json").read_text())
        receipts = [json.loads(l) for l in
                    self.receipts_of(run_dir).decode().splitlines()]
        independent = eventctl.reduce_state(plan, receipts)

        r = self.cli_sub("status", "--event", PHONE_EVENT, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        status_payload = json.loads(r.stdout)
        via_status = status_payload["state"]
        self.assertEqual(independent, via_status)
        self.assertEqual(status_payload["authority"]["location"], "execution_host")
        self.assertEqual(status_payload["authority"]["remote_status"], "not_required")
        self.assertEqual(
            status_payload["authority"]["as_of_receipt_sha256"],
            receipts[-1]["receipt_sha256"],
        )

        snapshot = json.loads((run_dir / "control" / "status.json").read_text())
        self.assertTrue(snapshot["derived"])
        self.assertEqual(snapshot["authority"], "control/receipts.jsonl")
        self.assertEqual(independent, snapshot["state"])
        self.assertEqual([x["transition"] for x in independent["reached"]],
                         ["plan_compiled"] + chain)


class TestIdempotency(Base):
    def test_local_consolidation_can_grade_without_inventing_a_pull(self):
        self.init_phone()
        self.advance_to_launched(PHONE_EVENT)
        for transition in ("source_attached", "supervising", "draining", "completed"):
            rc, out, err = self.cli(
                "record", "--event", PHONE_EVENT, "--transition", transition
            )
            self.assertEqual(rc, 0, err or out)
        for transition in ("evidence_consolidated", "graded"):
            rc, out, err = self.cli(
                "record", "--event", PHONE_EVENT, "--transition", transition
            )
            self.assertEqual(rc, 0, err or out)
        run_dir = self.run_dir_for(PHONE_EVENT)
        receipts = [
            json.loads(line)
            for line in self.receipts_of(run_dir).decode().splitlines()
        ]
        reached = {row.get("transition") for row in receipts}
        self.assertIn("evidence_consolidated", reached)
        self.assertIn("graded", reached)
        self.assertNotIn("evidence_pulled", reached)

    def test_repeated_transition_is_noop(self):
        self.init_phone()
        rc, _, _ = self.cli("record", "--event", PHONE_EVENT,
                            "--transition", "prepared")
        self.assertEqual(rc, 0)
        run_dir = self.run_dir_for(PHONE_EVENT)
        before = self.receipts_of(run_dir)
        rc, out, _ = self.cli("record", "--event", PHONE_EVENT,
                              "--transition", "prepared")
        self.assertEqual(rc, 0)
        self.assertIn("idempotent", out)
        self.assertEqual(before, self.receipts_of(run_dir))

    def test_repeated_init_is_idempotent(self):
        run_dir = self.init_phone()
        plan_before = (run_dir / "control" / "run-plan.json").read_bytes()
        receipts_before = self.receipts_of(run_dir)
        cfg = self.repo / "configs" / "test-phone-arm.toml"
        rc, out, err = self.cli("init", "--config", str(cfg))
        self.assertEqual(rc, 0, err)
        self.assertIn("already-initialized", out)
        self.assertEqual(plan_before,
                         (run_dir / "control" / "run-plan.json").read_bytes())
        receipts = [json.loads(l) for l in
                    self.receipts_of(run_dir).decode().splitlines()]
        self.assertEqual(receipts_before, self.receipts_of(run_dir))
        self.assertEqual(
            1, sum(1 for r in receipts if r["transition"] == "plan_compiled"))


class TestIllegalTransitions(Base):
    def test_illegal_transition_leaves_file_unchanged(self):
        self.init_phone()
        run_dir = self.run_dir_for(PHONE_EVENT)
        before = self.receipts_of(run_dir)
        rc, _, err = self.cli("record", "--event", PHONE_EVENT,
                              "--transition", "launched")
        self.assertEqual(rc, 1)
        self.assertIn("missing prerequisites", err)
        self.assertEqual(before, self.receipts_of(run_dir))
        rc, _, err = self.cli("record", "--event", PHONE_EVENT,
                              "--transition", "not_a_transition")
        self.assertEqual(rc, 1)
        self.assertEqual(before, self.receipts_of(run_dir))


class TestMutationDetection(Base):
    def test_config_mutation_blocks_typed_record(self):
        cfg = self.write_phone_config()
        self.cli("init", "--config", str(cfg))
        cfg.write_text(cfg.read_text() + "\n# drift\n")
        rc, _, err = self.cli("record", "--event", PHONE_EVENT,
                              "--transition", "prepared")
        self.assertEqual(rc, 1)
        self.assertIn("drifted", err)
        rc, out, _ = self.cli("verify", "--event", PHONE_EVENT)
        self.assertEqual(rc, 1)

    def test_env_mutation_blocks_typed_record(self):
        cfg = self.write_phone_config()
        env = self.write_env("configs/test-phone-arm.toml")
        rc, _, err = self.cli("init", "--config", str(cfg),
                              "--event-env", str(env))
        self.assertEqual(rc, 0, err)
        env.write_text(env.read_text() + "EXTRA=1\n")
        rc, _, err = self.cli("record", "--event", PHONE_EVENT,
                              "--transition", "prepared")
        self.assertEqual(rc, 1)
        self.assertIn("drifted", err)

    def test_plan_and_earlier_receipt_mutation_fail_verify(self):
        self.init_phone()
        run_dir = self.run_dir_for(PHONE_EVENT)
        self.cli("record", "--event", PHONE_EVENT, "--transition", "prepared")
        self.cli("record", "--event", PHONE_EVENT,
                 "--transition", "admission_resolved")

        plan_path = run_dir / "control" / "run-plan.json"
        plan = json.loads(plan_path.read_text())
        original = plan_path.read_text()
        plan["host"] = "box"
        plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True))
        rc, out, _ = self.cli("verify", "--run-dir", str(run_dir))
        self.assertEqual(rc, 1)
        self.assertTrue(
            "self-hash" in out or "fields do not match" in out,
            out,
        )
        plan_path.write_text(original)
        rc, _, _ = self.cli("verify", "--run-dir", str(run_dir))
        self.assertEqual(rc, 0)

        receipts_path = run_dir / "control" / "receipts.jsonl"
        lines = receipts_path.read_bytes().splitlines(keepends=True)
        self.assertGreaterEqual(len(lines), 3)
        lines[1] = lines[1].replace(b'"outcome":"ok"', b'"outcome":"fail"')
        receipts_path.write_bytes(b"".join(lines))
        rc, out, _ = self.cli("verify", "--run-dir", str(run_dir))
        self.assertEqual(rc, 1)
        self.assertIn("mid-chain", out)

    def test_rehashed_plan_with_wrong_schema_or_identity_is_rejected(self):
        self.init_phone()
        run_dir = self.run_dir_for(PHONE_EVENT)
        plan_path = run_dir / "control" / "run-plan.json"
        original = plan_path.read_bytes()
        for field, value, hint in (
            ("schema", 999, "schema/kind"),
            ("lane", "acoustic", "lane"),
            ("required_prep_receipts", ["prepared"], "lane/posture"),
        ):
            plan = json.loads(original)
            plan[field] = value
            plan["plan_sha256"] = eventctl.hash_of(plan, "plan_sha256")
            plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
            rc, out, _ = self.cli("verify", "--run-dir", str(run_dir))
            self.assertEqual(rc, 1, f"accepted mutated {field}")
            self.assertIn(hint, out)
        plan_path.write_bytes(original)

    def test_newline_terminated_corrupt_final_receipt_is_hard_failure(self):
        self.init_phone()
        run_dir = self.run_dir_for(PHONE_EVENT)
        receipts = run_dir / "control" / "receipts.jsonl"
        intact = receipts.read_bytes()
        receipts.write_bytes(intact + b'{"not":"a receipt"}\n')

        rc, out, _ = self.cli("verify", "--run-dir", str(run_dir))
        self.assertEqual(rc, 1)
        self.assertIn("invalid mid-chain", out)
        rc, _, err = self.cli("record", "--run-dir", str(run_dir),
                              "--transition", "prepared")
        self.assertEqual(rc, 1)
        self.assertIn("invalid mid-chain", err)
        self.assertEqual(intact + b'{"not":"a receipt"}\n',
                         receipts.read_bytes())

    def test_corrupt_sibling_run_is_not_silently_ignored(self):
        self.init_phone()
        valid = self.run_dir_for(PHONE_EVENT)
        corrupt = valid.parent / "SECONDATTEMPT"
        (corrupt / "control").mkdir(parents=True)
        (corrupt / "control" / "run-plan.json").write_text("{not json}\n")

        rc, _, err = self.cli("record", "--event", PHONE_EVENT,
                              "--transition", "prepared")
        self.assertEqual(rc, 1)
        self.assertIn("does not parse", err)
        rc, out, _ = self.cli("verify", "--event", PHONE_EVENT)
        self.assertEqual(rc, 1)
        self.assertIn("FAILED:", out)


class TestDurablePublication(Base):
    def test_init_fsyncs_plan_file_and_parent_directory(self):
        cfg = self.write_phone_config()
        calls = []
        real_fsync = eventctl.os.fsync

        def recording_fsync(fd):
            calls.append((fd, eventctl.os.fstat(fd).st_mode))
            return real_fsync(fd)

        with mock.patch.object(eventctl.os, "fsync",
                               side_effect=recording_fsync):
            rc, out, err = self.cli("init", "--config", str(cfg))
        self.assertEqual(rc, 0, err or out)
        # plan temp + control directory + receipts + status temp + control dir
        self.assertGreaterEqual(len(calls), 5)
        self.assertTrue(any(stat.S_ISDIR(mode) for _, mode in calls))


class TestPrepJoin(Base):
    def test_phone_needs_all_three_prep_receipts(self):
        self.init_phone()
        for t in ("prepared", "admission_resolved"):
            self.cli("record", "--event", PHONE_EVENT, "--transition", t)
            rc, _, err = self.cli("record", "--event", PHONE_EVENT,
                                  "--transition", "preflight_passed")
            self.assertEqual(rc, 1, f"passed too early after {t}")
        self.cli("record", "--event", PHONE_EVENT,
                 "--transition", "study_completed")
        rc, _, err = self.cli("record", "--event", PHONE_EVENT,
                              "--transition", "preflight_passed")
        self.assertEqual(rc, 0, err)

    def test_webcast_lane_asymmetry(self):
        cfg = self.write_webcast_config()
        rc, _, err = self.cli("init", "--config", str(cfg))
        self.assertEqual(rc, 0, err)
        rc, _, err = self.cli("record", "--event", WC_EVENT,
                              "--transition", "admission_resolved")
        self.assertEqual(rc, 1)
        self.assertIn("phone-lane only", err)
        for t in ("prepared", "access_acquired", "study_completed"):
            rc, _, err = self.cli("record", "--event", WC_EVENT,
                                  "--transition", t)
            self.assertEqual(rc, 0, err)
        rc, _, err = self.cli("record", "--event", WC_EVENT,
                              "--transition", "preflight_passed")
        self.assertEqual(rc, 0, err)

    def test_capture_only_waives_study(self):
        cfg = self.write_phone_config(name="capture-phone-arm.toml",
                                      keywords=False)
        rc, out, err = self.cli("init", "--config", str(cfg))
        self.assertEqual(rc, 0, err)
        run_dir = self.run_dir_for(PHONE_EVENT)
        plan = json.loads((run_dir / "control" / "run-plan.json").read_text())
        self.assertTrue(plan["capture_only"])
        self.assertNotIn("study_completed", plan["required_prep_receipts"])
        for t in ("prepared", "admission_resolved"):
            self.cli("record", "--event", PHONE_EVENT, "--transition", t)
        rc, _, err = self.cli("record", "--event", PHONE_EVENT,
                              "--transition", "preflight_passed")
        self.assertEqual(rc, 0, err)


class TestLaunchJoinAndTerminals(Base):
    def test_launch_needs_scheduled_and_capture_ready(self):
        self.init_phone()
        self.advance_to_preflight(PHONE_EVENT)
        rc, _, _ = self.cli("record", "--event", PHONE_EVENT,
                            "--transition", "launched")
        self.assertEqual(rc, 1)
        self.cli("record", "--event", PHONE_EVENT, "--transition", "scheduled")
        rc, _, err = self.cli("record", "--event", PHONE_EVENT,
                              "--transition", "launched")
        self.assertEqual(rc, 1)
        self.assertIn("capture_ready", err)
        self.cli("record", "--event", PHONE_EVENT,
                 "--transition", "capture_ready")
        rc, _, err = self.cli("record", "--event", PHONE_EVENT,
                              "--transition", "launched")
        self.assertEqual(rc, 0, err)

    def test_terminal_semantics(self):
        self.init_phone()
        self.advance_to_launched(PHONE_EVENT)
        # descope is a pre-launch terminal
        rc, _, err = self.cli("record", "--event", PHONE_EVENT,
                              "--transition", "descope")
        self.assertEqual(rc, 1)
        self.assertIn("pre-launch", err)
        # aborted is legal any time pre-terminal; the post-mortem tail stays open
        rc, _, err = self.cli("record", "--event", PHONE_EVENT,
                              "--transition", "aborted")
        self.assertEqual(rc, 0, err)
        rc, _, _ = self.cli("record", "--event", PHONE_EVENT,
                            "--transition", "failed")
        self.assertEqual(rc, 1)  # second different terminal
        for t in ("evidence_pulled", "graded", "archived"):
            rc, _, err = self.cli("record", "--event", PHONE_EVENT,
                                  "--transition", t)
            self.assertEqual(rc, 0, f"{t}: {err}")
        rc, _, _ = self.cli("record", "--event", PHONE_EVENT,
                            "--transition", "graded", "--outcome", "fail")
        self.assertEqual(rc, 1)  # archived closes even observations


class TestCompat(Base):
    def test_compat_never_changes_typed_state(self):
        self.init_phone()
        self.cli("record", "--event", PHONE_EVENT, "--transition", "prepared")
        run_dir = self.run_dir_for(PHONE_EVENT)
        rc, _, _ = self.cli("status", "--event", PHONE_EVENT, "--json")
        _, before_out, _ = self.cli("status", "--event", PHONE_EVENT, "--json")
        state_before = json.loads(before_out)["state"]
        for outcome in ("info", "ok", "fail", "refused", "warn"):
            rc, out, err = self.cli(
                "compat", "--event", PHONE_EVENT, "--actor", "launch-event.sh",
                "--action", "arm-exit", "--outcome", outcome,
                "--detail", "legacy line")
            self.assertEqual(rc, 0, err)
        _, after_out, _ = self.cli("status", "--event", PHONE_EVENT, "--json")
        state_after = json.loads(after_out)["state"]
        self.assertEqual(state_before, state_after)
        self.assertTrue(state_after["active"])
        rc, _, _ = self.cli("verify", "--run-dir", str(run_dir))
        self.assertEqual(rc, 0)

    def test_compat_resolution_zero_and_ambiguous(self):
        rc, out, err = self.cli("compat", "--event", PHONE_EVENT,
                                "--actor", "x", "--action", "note",
                                "--outcome", "info")
        self.assertEqual(rc, 0, err)
        self.assertIn("not bridged", out)
        cfg = self.write_phone_config()
        self.cli("init", "--config", str(cfg))
        self.cli("init", "--config", str(cfg), "--run-id", "SECONDATTEMPT")
        rc, _, err = self.cli("compat", "--event", PHONE_EVENT,
                              "--actor", "x", "--action", "note",
                              "--outcome", "info")
        self.assertEqual(rc, 1)
        self.assertIn("ambiguous", err)

    def test_run_dir_compat_can_append_after_terminal_and_dedupes_atomically(self):
        run_dir = self.init_phone()
        self.cli("record", "--run-dir", str(run_dir), "--transition", "failed")
        before = len(self.receipts_of(run_dir).splitlines())
        argv = (
            "compat", "--run-dir", str(run_dir), "--actor", "alertctl",
            "--action", "alert.required:abc", "--outcome", "info",
            "--detail", '{"schema":1}', "--idempotent",
        )
        rc, out, err = self.cli(*argv)
        self.assertEqual(rc, 0, err)
        self.assertIn("bridged", out)
        rc, out, err = self.cli(*argv)
        self.assertEqual(rc, 0, err)
        self.assertIn("idempotent", out)
        self.assertEqual(len(self.receipts_of(run_dir).splitlines()), before + 1)
        changed = list(argv)
        changed[changed.index('{"schema":1}')] = '{"schema":2}'
        rc, _, err = self.cli(*changed)
        self.assertEqual(rc, 1)
        self.assertIn("different detail", err)


class TestTruncatedTail(Base):
    def test_interrupted_final_write_is_recoverable(self):
        self.init_phone()
        self.cli("record", "--event", PHONE_EVENT, "--transition", "prepared")
        run_dir = self.run_dir_for(PHONE_EVENT)
        receipts_path = run_dir / "control" / "receipts.jsonl"
        intact = receipts_path.read_bytes()
        with open(receipts_path, "ab") as fh:
            fh.write(b'{"schema":1,"kind":"transition","truncat')

        rc, out, _ = self.cli("verify", "--run-dir", str(run_dir))
        self.assertEqual(rc, 3)
        m = re.search(r"truncate .* to (\d+) bytes", out)
        self.assertIsNotNone(m, out)
        self.assertEqual(int(m.group(1)), len(intact))

        torn = receipts_path.read_bytes()
        rc, _, err = self.cli("record", "--event", PHONE_EVENT,
                              "--transition", "admission_resolved")
        self.assertEqual(rc, 3)
        self.assertEqual(torn, receipts_path.read_bytes())

        # status still reports the valid prefix, loudly
        rc, out, _ = self.cli("status", "--event", PHONE_EVENT, "--json")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["integrity"]["recoverable_tail"])
        self.assertEqual(
            ["plan_compiled", "prepared"],
            [x["transition"] for x in payload["state"]["reached"]])

        with open(receipts_path, "r+b") as fh:
            fh.truncate(int(m.group(1)))
        rc, _, _ = self.cli("verify", "--run-dir", str(run_dir))
        self.assertEqual(rc, 0)
        rc, _, err = self.cli("record", "--event", PHONE_EVENT,
                              "--transition", "admission_resolved")
        self.assertEqual(rc, 0, err)


class TestConcurrency(Base):
    def test_concurrent_appends_keep_chain_valid(self):
        cfg = self.write_phone_config()
        self.cli("init", "--config", str(cfg))
        run_dir = self.run_dir_for(PHONE_EVENT)
        before = len(self.receipts_of(run_dir).splitlines())
        env = os.environ.copy()
        env["EARSHOT_REPO"] = str(self.repo)
        procs = [
            subprocess.Popen(
                [sys.executable, str(SCRIPT), "compat",
                 "--event", PHONE_EVENT, "--actor", f"racer-{i}",
                 "--action", "note", "--outcome", "info",
                 "--detail", f"concurrent append {i}"],
                cwd=self.repo, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for i in range(8)
        ]
        for p in procs:
            _, perr = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, perr.decode())
        lines = self.receipts_of(run_dir).decode().splitlines()
        self.assertEqual(len(lines), before + 8)
        receipts = [json.loads(l) for l in lines]
        self.assertEqual([r["sequence"] for r in receipts],
                         list(range(1, len(lines) + 1)))
        plan = json.loads((run_dir / "control" / "run-plan.json").read_text())
        prev = plan["plan_sha256"]
        for r in receipts:
            self.assertEqual(r["previous_sha256"], prev)
            prev = r["receipt_sha256"]
        rc, _, _ = self.cli("verify", "--run-dir", str(run_dir))
        self.assertEqual(rc, 0)


class TestRunSeparation(Base):
    def test_schema3_names_every_evidence_destination(self):
        run_dir = self.init_phone()
        plan = json.loads((run_dir / "control" / "run-plan.json").read_text())
        self.assertEqual(plan["schema"], 3)
        self.assertRegex(
            plan["run_id"], r"^20260812T210000Z-p3-[0-9a-f]{12}$"
        )
        self.assertEqual(
            plan["artifacts"],
            {
                "receipts": "control/receipts.jsonl",
                "status": "control/status.json",
                "audio_raw": "capture/audio.raw",
                "audio_frames": "capture/audio.frames.jsonl",
                "audio_manifest": "capture/audio.manifest.json",
                "audio_commit": "capture/audio.commit.json",
                "audio_lock": "capture/audio.lock",
                "audio_wav_dir": "capture/wav",
                "book_tape": "book/kalshi-tape.jsonl",
                "order_evidence": "orders/order-evidence.jsonl",
                "fix_observation_tape": "orders/fix-observations.jsonl",
                "runtime_report": "report/report.json",
                "grade": "grade/verdict.json",
            },
        )
        for relative in plan["artifacts"].values():
            self.assertTrue((run_dir / relative).parent.is_dir())
        self.assertFalse(
            (run_dir / plan["artifacts"]["audio_wav_dir"]).exists(),
            "the audio banker must create and exclusively own its WAV directory",
        )

    def test_two_run_ids_cannot_share_evidence_paths(self):
        cfg = self.write_phone_config()
        rc, out1, _ = self.cli("init", "--config", str(cfg))
        self.assertEqual(rc, 0)
        rc, out2, _ = self.cli("init", "--config", str(cfg),
                               "--run-id", "SECONDATTEMPT")
        self.assertEqual(rc, 0)
        d1 = Path(out1.split(": ", 1)[1].strip())
        d2 = Path(out2.split(": ", 1)[1].strip())
        self.assertNotEqual(d1, d2)
        p1 = json.loads((d1 / "control" / "run-plan.json").read_text())
        p2 = json.loads((d2 / "control" / "run-plan.json").read_text())
        abs1 = {str(d1 / v) for v in p1["artifacts"].values()}
        abs2 = {str(d2 / v) for v in p2["artifacts"].values()}
        self.assertFalse(abs1 & abs2,
                         "two runs may not share any evidence path")

    def test_divergent_reinit_at_same_path_refused(self):
        cfg = self.write_phone_config()
        env = self.write_env("configs/test-phone-arm.toml")
        rc, _, err = self.cli("init", "--config", str(cfg),
                              "--event-env", str(env))
        self.assertEqual(rc, 0, err)
        run_dir = self.run_dir_for(PHONE_EVENT)
        plan_before = (run_dir / "control" / "run-plan.json").read_bytes()
        env.write_text(env.read_text() + "STREAM_CONFIRM_SECS=150\n")
        rc, _, err = self.cli("init", "--config", str(cfg),
                              "--event-env", str(env))
        self.assertEqual(rc, 1)
        self.assertIn("DIFFERENT plan", err)
        self.assertEqual(plan_before,
                         (run_dir / "control" / "run-plan.json").read_bytes())


class TestSchemaIsolation(Base):
    @staticmethod
    def snapshot_tree(root):
        """Include directories as well as file bytes so mkdir is observable."""
        snapshot = []
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            snapshot.append(
                (rel, "dir", None) if path.is_dir()
                else (rel, "file", path.read_bytes())
            )
        return snapshot

    def install_legacy_run(self, schema, cfg, run_id):
        info = eventctl.load_event_config(cfg)
        plan = eventctl.build_plan(
            self.repo,
            PHONE_EVENT,
            run_id,
            info,
            cfg.relative_to(self.repo).as_posix(),
            eventctl.file_sha256(cfg),
            None,
            None,
            "legacy-host",
            "sha256:" + "a" * 64,
        )
        plan["schema"] = schema
        plan["artifacts"] = {
            "receipts": "control/receipts.jsonl",
            "status": "control/status.json",
            "audio_manifest": "capture/capture-anchor.json",
            "audio_wav": "capture/call.wav",
            "book_tape": "book/kalshi-tape.jsonl",
            "order_evidence": "orders/order-evidence.jsonl",
            "runtime_report": "report/report.json",
            "grade": "grade/verdict.json",
        }
        plan["plan_sha256"] = eventctl.hash_of(plan, "plan_sha256")
        run_dir = self.repo / "var" / "runs" / PHONE_EVENT / run_id
        (run_dir / "control").mkdir(parents=True)
        (run_dir / "control" / "run-plan.json").write_text(
            json.dumps(plan, indent=2, sort_keys=True) + "\n"
        )
        (run_dir / "control" / "receipts.jsonl").write_bytes(
            b"legacy-receipts-must-not-change\n"
        )
        (run_dir / "legacy-only").mkdir()
        (run_dir / "legacy-only" / "sentinel.bin").write_bytes(
            b"legacy-evidence\x00\xff"
        )
        return run_dir

    def test_explicit_schema1_and_schema2_collisions_are_read_only_refusals(self):
        cfg = self.write_phone_config()
        for schema in (1, 2):
            with self.subTest(schema=schema):
                run_id = f"LEGACYSCHEMA{schema}"
                run_dir = self.install_legacy_run(schema, cfg, run_id)
                before = self.snapshot_tree(run_dir)
                rc, _, err = self.cli(
                    "init", "--config", str(cfg), "--run-id", run_id
                )
                self.assertEqual(rc, 1)
                self.assertIn("unsupported run plan schema/kind", err)
                self.assertEqual(before, self.snapshot_tree(run_dir))

    def test_default_schema3_id_does_not_collide_with_old_default_id(self):
        cfg = self.write_phone_config()
        old_id = (
            "20260812T210000Z-" + eventctl.file_sha256(cfg)[:12]
        )
        legacy = self.install_legacy_run(2, cfg, old_id)
        legacy_before = self.snapshot_tree(legacy)

        rc, out, err = self.cli("init", "--config", str(cfg))
        self.assertEqual(rc, 0, err or out)
        current = Path(out.split(": ", 1)[1].strip())
        self.assertEqual(
            current.name,
            "20260812T210000Z-p3-" + eventctl.file_sha256(cfg)[:12],
        )
        self.assertNotEqual(current, legacy)
        self.assertEqual(legacy_before, self.snapshot_tree(legacy))

    def test_event_selection_ignores_coexisting_legacy_attempt(self):
        cfg = self.write_phone_config()
        legacy = self.install_legacy_run(
            1, cfg, "20260812T210000Z-legacyhistory"
        )
        legacy_before = self.snapshot_tree(legacy)

        rc, out, err = self.cli("init", "--config", str(cfg))
        self.assertEqual(rc, 0, err or out)
        current = Path(out.split(": ", 1)[1].strip())

        rc, out, err = self.cli("status", "--event", PHONE_EVENT, "--json")
        self.assertEqual(rc, 0, err or out)
        payload = json.loads(out)
        self.assertEqual(payload["run"], current.name)
        self.assertEqual(legacy_before, self.snapshot_tree(legacy))


class TestRefusals(Base):
    def assert_nothing_created(self):
        self.assertFalse((self.repo / "var" / "runs").exists())

    def test_unsafe_ids_refused(self):
        cfg = self.write_phone_config()
        for bad in ("a/../b", "UP/DOWN", "dot.dot", "-LEAD", "TRAIL-", ""):
            rc, _, err = self.cli("init", "--config", str(cfg),
                                  f"--run-id={bad}")
            self.assertEqual(rc, 1, f"run-id {bad!r} accepted")
        cfg2 = self.write_phone_config(name="bad-id-arm.toml",
                                       event="lower-case-id")
        rc, _, err = self.cli("init", "--config", str(cfg2))
        self.assertEqual(rc, 1)
        self.assertIn("unsafe EVENT_ID", err)
        self.assert_nothing_created()

    def test_bad_timestamps_refused(self):
        for starts_at, hint in (
            ("2026-07-15T00:00:00Z", "placeholder"),
            ("2026-08-12T21:00:00", "timezone-naive"),
            ("not-a-time", "unparseable"),
        ):
            cfg = self.write_phone_config(name="ts-arm.toml",
                                          starts_at=starts_at)
            rc, _, err = self.cli("init", "--config", str(cfg))
            self.assertEqual(rc, 1, f"{starts_at} accepted")
            self.assertIn(hint, err)
        cfg = self.repo / "configs" / "todo-arm.toml"
        cfg.write_text(PHONE_CONFIG.format(
            event=PHONE_EVENT,
            starts_at="2026-08-12T21:00:00Z",
            keywords=KEYWORDS).replace(
                'starts_at = "2026-08-12T21:00:00Z"',
                'starts_at = "2026-08-12T21:00:00Z"  # TODO: confirm'))
        rc, _, err = self.cli("init", "--config", str(cfg))
        self.assertEqual(rc, 1)
        self.assertIn("TODO", err)
        self.assert_nothing_created()

    def test_host_absolute_env_refused(self):
        cfg = self.write_phone_config()
        env = self.write_env("configs/test-phone-arm.toml",
                             account="/Users/bernardodelrio/.earshot/ms.env")
        rc, _, err = self.cli("init", "--config", str(cfg),
                              "--event-env", str(env))
        self.assertEqual(rc, 1)
        self.assertIn("host-absolute", err)
        self.assertIn("ACCOUNT_ENV", err)
        self.assert_nothing_created()

    def test_out_of_repo_paths_refused(self):
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".toml", delete=False) as fh:
            fh.write(PHONE_CONFIG.format(event=PHONE_EVENT,
                                         starts_at=f"{DATE}T21:00:00Z",
                                         keywords=""))
            outside = fh.name
        try:
            rc, _, err = self.cli("init", "--config", outside)
            self.assertEqual(rc, 1)
            self.assertIn("outside the repository", err)
        finally:
            os.unlink(outside)
        rc, _, err = self.cli("init", "--config",
                              "../" + Path(self.repo).name + "-nope.toml")
        self.assertEqual(rc, 1)
        self.assert_nothing_created()

    def test_missing_event_section_refused(self):
        cfg = self.repo / "configs" / "noevent-arm.toml"
        cfg.write_text('[pipeline]\nchunk_ms = 80\n')
        rc, _, err = self.cli("init", "--config", str(cfg))
        self.assertEqual(rc, 1)
        self.assertIn("[event]", err)
        self.assert_nothing_created()


class TestExecutionProfileIdentity(Base):
    def test_profile_resolver_supplies_runplan_identity_without_global_patch(self):
        cfg = self.write_phone_config()
        env = self.write_env("configs/test-phone-arm.toml")
        helper, binary = self.fake_identity_tools()
        with self.profile_identity_mode():
            rc, out, err = self.cli(
                "init", "--config", str(cfg), "--event-env", str(env),
                "--identity-helper", str(helper), "--earshot-bin", str(binary),
            )
        self.assertEqual(rc, 0, err or out)
        plan = json.loads(
            (self.run_dir_for(PHONE_EVENT) / "control" / "run-plan.json").read_text()
        )
        self.assertEqual(plan["execution_host_id"], "box-useast1")
        self.assertEqual(plan["account_fingerprint"], "sha256:" + "a" * 64)

    def test_explicit_profile_locator_wins_over_computed_runtime_account_env(self):
        cfg = self.write_phone_config()
        env = self.repo / "configs" / "test-autorun.env"
        env.write_text(
            ENV_TEMPLATE.format(
                config="configs/test-phone-arm.toml",
                date=DATE,
                account='${ACCOUNT_ENV:-$HOME/.earshot/wrong.env}',
            )
            + 'EARSHOT_ACCOUNT_PROFILE="$HOME/.earshot/intc.env"\n'
        )
        helper, binary = self.fake_identity_tools()
        with self.profile_identity_mode():
            rc, out, err = self.cli(
                "init", "--config", str(cfg), "--event-env", str(env),
                "--identity-helper", str(helper), "--earshot-bin", str(binary),
            )
        self.assertEqual(rc, 0, err or out)
        plan = json.loads(
            (self.run_dir_for(PHONE_EVENT) / "control" / "run-plan.json").read_text()
        )
        self.assertEqual(plan["account_fingerprint"], "sha256:" + "b" * 64)

    def test_computed_account_env_without_literal_profile_is_refused_read_only(self):
        cfg = self.write_phone_config()
        env = self.write_env(
            "configs/test-phone-arm.toml",
            account='${ACCOUNT_ENV:-$HOME/.earshot/ms.env}',
        )
        helper, binary = self.fake_identity_tools()
        with self.profile_identity_mode():
            rc, _, err = self.cli(
                "init", "--config", str(cfg), "--event-env", str(env),
                "--identity-helper", str(helper), "--earshot-bin", str(binary),
            )
        self.assertEqual(rc, 1)
        self.assertIn("must be portable", err)
        self.assertFalse((self.repo / "var" / "runs").exists())

    def test_one_sided_explicit_identity_never_mixes_with_profile(self):
        cfg = self.write_phone_config()
        env = self.write_env("configs/test-phone-arm.toml")
        helper, binary = self.fake_identity_tools()
        with self.profile_identity_mode():
            rc, _, err = self.cli(
                "init", "--config", str(cfg), "--event-env", str(env),
                "--host-id", "partial-host",
                "--identity-helper", str(helper), "--earshot-bin", str(binary),
            )
        self.assertEqual(rc, 1)
        self.assertIn("all-or-nothing", err)
        self.assertFalse((self.repo / "var" / "runs").exists())

    def test_init_date_resolves_each_accounts_fingerprint_independently(self):
        self.write_phone_config()
        self.write_env("configs/test-phone-arm.toml", account="$HOME/.earshot/ms.env")
        self.write_webcast_config()
        self.write_env(
            "configs/webcast-test-arm-wc.toml",
            name="webcast-autorun.env",
            account="$HOME/.earshot/intc.env",
        )
        helper, binary = self.fake_identity_tools()
        with self.profile_identity_mode():
            rc, out, err = self.cli(
                "init-date", "--date", DATE,
                "--identity-helper", str(helper), "--earshot-bin", str(binary),
            )
        self.assertEqual(rc, 0, err or out)
        phone = json.loads(
            (self.run_dir_for(PHONE_EVENT) / "control" / "run-plan.json").read_text()
        )
        webcast = json.loads(
            (self.run_dir_for(WC_EVENT) / "control" / "run-plan.json").read_text()
        )
        self.assertEqual(phone["account_fingerprint"], "sha256:" + "a" * 64)
        self.assertEqual(webcast["account_fingerprint"], "sha256:" + "b" * 64)


class TestInitDate(Base):
    def test_selection_skips_and_ambiguity(self):
        # the two live lanes for the date
        self.write_phone_config()
        self.write_env("configs/test-phone-arm.toml")
        self.write_webcast_config()
        # skipped shapes: raw copy (placeholder ts), rehearsal, bakeoff
        (self.repo / "configs" / "test-26aug12-raw.toml").write_text(
            PHONE_CONFIG.format(event="KXEARNINGSMENTIONTEST-26AUG12",
                                starts_at="2026-07-15T00:00:00Z",
                                keywords=""))
        self.write_phone_config(name="rehearsal-test-arm.toml",
                                event="KXEARNINGSMENTIONREH-26AUG12-PHONE")
        self.write_phone_config(name="bakeoff-test.toml",
                                event="KXEARNINGSMENTIONBAKE-26AUG12-PHONE")
        # stale event from another day: silently out of scope
        self.write_phone_config(name="old-phone-arm.toml",
                                event="KXEARNINGSMENTIONOLD-26AUG01-PHONE",
                                starts_at="2026-08-01T21:00:00Z")
        # sed template (__EVENT_TICKER__ convention): silently skipped, never
        # a nightly refusal
        (self.repo / "configs" / "webcast-fanout-arm.toml").write_text(
            PHONE_CONFIG.format(event="__EVENT_TICKER__",
                                starts_at="__START_ISO_UTC__", keywords=""))

        rc, out, _ = self.cli("init-date", "--date", DATE)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"{PHONE_EVENT}: initialized", out)
        self.assertIn(f"{WC_EVENT}: initialized", out)
        self.assertNotIn("REH", out)
        self.assertNotIn("BAKE", out)
        self.assertNotIn("OLD", out)
        events = sorted(p.name for p in (self.repo / "var" / "runs").iterdir())
        self.assertEqual(events, [PHONE_EVENT, WC_EVENT])
        # idempotent nightly re-run
        rc, out, _ = self.cli("init-date", "--date", DATE)
        self.assertEqual(rc, 0, out)
        self.assertIn("already-initialized", out)

    def test_refusals_are_per_event_and_nonzero(self):
        self.write_webcast_config()  # the good event still initializes
        # duplicate EVENT_ID from two config files
        self.write_phone_config(name="dup-a-phone-arm.toml")
        self.write_phone_config(name="dup-b-phone-arm.toml")
        # env whose CONFIG_PATH does not exist
        self.write_env("configs/ghost-arm.toml", name="ghost-autorun.env")
        # host-absolute env on its own event
        self.write_phone_config(
            name="abs-phone-arm.toml",
            event="KXEARNINGSMENTIONABS-26AUG12-PHONE")
        self.write_env("configs/abs-phone-arm.toml",
                       name="abs-autorun.env",
                       account="/home/ubuntu/.earshot/ms.env")

        rc, out, _ = self.cli("init-date", "--date", DATE)
        self.assertEqual(rc, 1)
        self.assertIn(f"{WC_EVENT}: initialized", out)
        self.assertIn("ambiguous", out)
        self.assertIn("ghost-arm.toml", out)
        self.assertIn("host-absolute", out)
        run_events = sorted(p.name for p in
                            (self.repo / "var" / "runs").iterdir())
        self.assertEqual(run_events, [WC_EVENT])

    def test_duplicate_env_for_one_config_refused(self):
        self.write_phone_config()
        self.write_env("configs/test-phone-arm.toml", name="a-autorun.env")
        self.write_env("configs/test-phone-arm.toml", name="b-autorun.env")
        rc, out, _ = self.cli("init-date", "--date", DATE)
        self.assertEqual(rc, 1)
        self.assertIn("multiple env files", out)
        self.assertFalse(
            (self.repo / "var" / "runs" / PHONE_EVENT).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
