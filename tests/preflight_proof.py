#!/usr/bin/env python3
"""Offline lifecycle/admission tests for scripts/preflight-proof.py."""

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
SCRIPTS = ("eventctl.py", "access-plan.py", "preflight-proof.py")
CAPABILITIES = (
    "event-autorun.sh", "redial-supervisor.sh", "operator-redial.sh",
    "operator-page.sh", "twilio-operator-dial.sh",
    "held-admission-receipt.py",
)
CAPABILITY_MARKERS = {
    "event-autorun.sh": (
        "EARSHOT_FIRST_DROP_DIAL_SCRIPT",
        "first-drop operator fallback: ARMED",
    ),
    "redial-supervisor.sh": ("--first-drop-dial-script", "FIRST-DROP-OPERATOR"),
    "operator-redial.sh": ("--assert-admitted",),
    "operator-page.sh": ("--assert-admitted",),
    "twilio-operator-dial.sh": ("inbound_track",),
    "held-admission-receipt.py": ("held_admission_assertion",),
}
NOW = "2026-08-12T12:00:00Z"


PHONE = '''\
[event]
id = "{event}"
starts_at = "{starts}"

[operator_handler]
enabled = {operator_enabled}
cell_number = "+15550000000"
conference_prefix = "earshot"

[[sources]]
kind = "twilio_stream"
bind = "127.0.0.1:8080"

[[keywords]]
market_ticker = "KXEARNINGSMENTION{issuer}-26AUG20-FOO"
phrase = "foo"
'''

WEBCAST = '''\
[event]
id = "{event}"
starts_at = "2026-08-20T21:00:00Z"

[[sources]]
kind = "pipe"
label = "primary"
cmd = ["/bin/sh", "-c", "ffmpeg -i $(cat $HOME/{slug}-stream.url) -f s16le -"]

[[keywords]]
market_ticker = "KXEARNINGSMENTION{issuer}-26AUG20-FOO"
phrase = "foo"
'''


class ProofTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="preflight-proof-")
        self.repo = Path(self.tmp.name).resolve()
        for directory in ("scripts", "configs/.readiness", "state"):
            (self.repo / directory).mkdir(parents=True, exist_ok=True)
        for name in SCRIPTS:
            shutil.copy2(ROOT / "scripts" / name, self.repo / "scripts" / name)
        for name in CAPABILITIES:
            path = self.repo / "scripts" / name
            markers = "\n".join(f"# {marker}" for marker in CAPABILITY_MARKERS[name])
            path.write_text(f"#!/bin/sh\n{markers}\nexit 0\n")
            path.chmod(0o755)
        self.env = {
            **os.environ,
            "EARSHOT_REPO": str(self.repo),
            "EARSHOT_HOST_ID": "box-useast1",
            "EARSHOT_KALSHI_ACCOUNT_FINGERPRINT": "sha256:" + "a" * 64,
            "EARSHOT_PREFLIGHT_NOW_UTC": NOW,
        }
        self.issuer = "PROOF"
        self.event = "KXEARNINGSMENTIONPROOF-26AUG20-PHONE"

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, script: str, *args):
        return subprocess.run(
            [sys.executable, str(self.repo / "scripts" / script), *map(str, args)],
            cwd=self.repo, env=self.env, text=True, capture_output=True,
        )

    def phone(self, enabled=True, starts="2026-08-20T21:00:00Z") -> tuple[Path, Path]:
        config = self.repo / "configs" / "proof-phone.toml"
        config.write_text(PHONE.format(
            event=self.event, starts=starts, issuer=self.issuer,
            operator_enabled=str(enabled).lower(),
        ))
        self.last_config = config
        return config, self.init(config)

    def webcast(self) -> tuple[Path, Path]:
        event = "KXEARNINGSMENTIONPROOF-26AUG20-WEBCAST"
        config = self.repo / "configs" / "proof-webcast.toml"
        config.write_text(WEBCAST.format(event=event, issuer=self.issuer, slug="proof"))
        self.last_config = config
        return config, self.init(config)

    def init(self, config: Path) -> Path:
        ready = self.repo / "configs" / ".readiness" / f"{config.stem}.json"
        ready.write_text('{"schema":1,"keywords_sha256":"fixture","date":"2026-08-12"}\n')
        proc = self.cli("eventctl.py", "init", "--config", config)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        return Path(proc.stdout.split(": ", 1)[1].strip())

    def registry(self, admission=None, probed=None, reservation=False,
                 rehearsal=None):
        path = self.repo / "configs" / "dial-ins.toml"
        lines = [f"[{self.issuer}]", 'tier = "published"']
        if admission is not None:
            lines.append(f'admission = "{admission}"')
        if probed is not None:
            lines.append(f'admission_probed = "{probed}"')
        if reservation:
            lines.append("reservation_window = true")
        if rehearsal:
            lines.append(f'operator_handler = "{rehearsal}"')
        path.write_text("\n".join(lines) + "\n")
        return path

    def inspect(self, config: Path, run_dir: Path, *extra):
        return self.cli(
            "preflight-proof.py", "inspect", "--config", config,
            "--run-dir", run_dir, *extra,
        )

    def draft(self, passed=True) -> Path:
        config = self.last_config
        event_id = next(
            line.split('"')[1] for line in config.read_text().splitlines()
            if line.strip().startswith("id =")
        )
        path = self.repo / "state" / "draft.json"
        path.write_text(json.dumps({
            "schema": 1, "event_id": event_id,
            "config": config.relative_to(self.repo).as_posix(),
            "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            "generated_utc": NOW,
            "pass": passed, "fails": 0 if passed else 1, "warns": 0,
            "gates": [], "gates_skipped": [],
        }) + "\n")
        return path

    def finalize(self, run_dir: Path, draft: Path | None = None, result=None):
        binding = run_dir / "control" / "preflight-inputs.json"
        draft = draft or self.draft(True)
        result = result or (self.repo / "state" / "preflight-result.json")
        return self.cli(
            "preflight-proof.py", "finalize", "--binding", binding,
            "--report-draft", draft, "--result-json", result,
        )

    def transitions(self, run_dir: Path):
        path = run_dir / "control" / "receipts.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["transition"] for line in path.read_text().splitlines()
                if json.loads(line)["kind"] == "transition"]

    def test_fresh_held_strategy_and_full_receipt_chain(self):
        config, run_dir = self.phone()
        self.registry("held", "2026-08-01")
        inspected = self.inspect(config, run_dir)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        strategy = json.loads((run_dir / "control" / "admission-strategy.json").read_text())
        self.assertEqual(strategy["mode"], "fresh_probe_held")
        self.assertFalse(strategy["current_stream"]["admitted"])
        final = self.finalize(run_dir)
        self.assertEqual(final.returncode, 0, final.stderr)
        self.assertEqual(self.transitions(run_dir), [
            "plan_compiled", "prepared", "admission_resolved",
            "study_completed", "preflight_passed",
        ])
        report = json.loads((self.repo / "state" / "preflight-result.json").read_text())
        self.assertTrue(report["pass"])
        self.assertEqual(report["lane_strategy_mode"], "fresh_probe_held")
        self.assertFalse(report["current_stream_admitted"])
        verify = self.cli("eventctl.py", "verify", "--run-dir", run_dir)
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
        before = (run_dir / "control" / "receipts.jsonl").read_bytes()
        self.assertEqual(self.inspect(config, run_dir).returncode, 0)
        self.assertEqual(self.finalize(run_dir).returncode, 0)
        self.assertEqual((run_dir / "control" / "receipts.jsonl").read_bytes(), before)

    def test_stale_held_is_never_relabelled_held(self):
        config, run_dir = self.phone()
        self.registry("held", "2026-06-01")
        inspected = self.inspect(config, run_dir)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        strategy = json.loads((run_dir / "control" / "admission-strategy.json").read_text())
        self.assertEqual(strategy["mode"], "first_drop_operator_fallback")
        self.assertEqual(strategy["registry_facts"]["observed_admission"], "held")
        self.assertEqual(strategy["registry_facts"]["held_freshness"], "stale")
        self.assertNotIn("held-admission-receipt.py", json.dumps(strategy["operational_strategy"]))

    def test_rehearsed_operator_first_requires_both_typed_halves(self):
        config, run_dir = self.phone()
        self.registry("operator_screened", "2026-08-10",
                      rehearsal="rehearsed-2026-08-10")
        good = self.inspect(config, run_dir)
        self.assertEqual(good.returncode, 0, good.stderr)
        strategy = json.loads((run_dir / "control" / "admission-strategy.json").read_text())
        self.assertEqual(strategy["mode"], "rehearsed_operator_first")

        # A separate event/run avoids replacing evidence after inspection.
        self.event = "KXEARNINGSMENTIONNOCAP-26AUG20-PHONE"
        self.issuer = "NOCAP"
        bad_config, bad_run = self.phone(enabled=False)
        self.registry("operator_screened", "2026-08-10",
                      rehearsal="rehearsed-2026-08-10")
        bad = self.inspect(bad_config, bad_run)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("enabled=true", bad.stderr)
        self.assertNotIn("admission_resolved", self.transitions(bad_run))

    def test_reservation_pending_is_underwritten_only_by_fallback(self):
        config, run_dir = self.phone()
        self.registry("untested", "2026-08-01", reservation=True)
        inspected = self.inspect(config, run_dir)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        strategy = json.loads((run_dir / "control" / "admission-strategy.json").read_text())
        self.assertEqual(strategy["mode"], "reservation_window_first_drop_fallback")
        gate = strategy["probe_gate"]
        self.assertEqual(gate["deadline_utc"], "2026-08-20T20:25:00Z")
        self.assertEqual(gate["status"], "pending-until-t35")
        self.assertFalse(gate["underwrites_admission_resolved"])
        self.assertTrue(gate["fallback_underwrites_admission_resolved"])
        self.assertFalse(strategy["current_stream"]["admitted"])

    def test_unknown_without_fallback_capability_fails_closed(self):
        config, run_dir = self.phone(enabled=False)
        self.registry("unknown", "2026-08-01")
        inspected = self.inspect(config, run_dir)
        self.assertEqual(inspected.returncode, 1)
        self.assertIn("enabled=true", inspected.stderr)
        self.assertEqual(self.transitions(run_dir), ["plan_compiled"])

    def test_executable_without_operator_handoff_contract_fails_closed(self):
        config, run_dir = self.phone()
        self.registry("unknown", "2026-08-01")
        capability = self.repo / "scripts" / "redial-supervisor.sh"
        capability.write_text("#!/bin/sh\nexit 0\n")
        capability.chmod(0o755)
        inspected = self.inspect(config, run_dir)
        self.assertEqual(inspected.returncode, 1)
        self.assertIn("contract marker missing", inspected.stderr)
        self.assertIn("--first-drop-dial-script", inspected.stderr)
        self.assertEqual(self.transitions(run_dir), ["plan_compiled"])

    def test_webcast_requires_access_plan_and_never_claims_current_media(self):
        config, run_dir = self.webcast()
        missing = self.inspect(config, run_dir)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("AccessPlan invalid", missing.stderr)
        made = self.cli(
            "access-plan.py", "bind", "--run-dir", run_dir,
            "--access-mode", "public", "--resolver-kind", "q4-api",
            "--resolver-argv-json", '["scripts/q4-resolve.sh"]',
        )
        self.assertEqual(made.returncode, 0, made.stderr)
        inspected = self.inspect(config, run_dir)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        binding = json.loads((run_dir / "control" / "preflight-inputs.json").read_text())
        self.assertEqual(binding["lane_transition"], "access_acquired")
        self.assertFalse(binding["lane_proof"]["current_media_proven"])

        final = self.finalize(run_dir)
        self.assertEqual(final.returncode, 0, final.stderr)
        self.assertIn("access_acquired", self.transitions(run_dir))
        report = json.loads((self.repo / "state" / "preflight-result.json").read_text())
        self.assertFalse(report["current_media_proven"])

    def test_webcast_refuses_legacy_access_plan_without_explicit_access_class(self):
        config, run_dir = self.webcast()
        legacy = self.cli(
            "access-plan.py", "create", "--run-dir", run_dir,
            "--vantage", "us-east1/box", "--resolver-kind", "q4-api",
            "--resolver-argv-json", '["scripts/q4-resolve.sh"]',
            "--url-file", "$HOME/proof-stream.url",
            "--deadline-utc", "2026-08-20T21:05:00Z",
            "--expiry-mode", "refresh-at-launch",
        )
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        inspected = self.inspect(config, run_dir)
        self.assertEqual(inspected.returncode, 1)
        self.assertIn("canonical operation requires schema-2 AccessPlan", inspected.stderr)
        self.assertEqual(self.transitions(run_dir), ["plan_compiled"])

    def test_report_persistence_failure_never_records_preflight_passed(self):
        config, run_dir = self.phone()
        self.registry("held", "2026-08-01")
        self.assertEqual(self.inspect(config, run_dir).returncode, 0)
        blocked = self.repo / "state" / "not-a-directory"
        blocked.write_text("file\n")
        failed = self.finalize(run_dir, result=blocked / "result.json")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("could not durably publish", failed.stderr)
        reached = self.transitions(run_dir)
        self.assertIn("prepared", reached)
        self.assertNotIn("preflight_passed", reached)

    def test_red_draft_publishes_no_preparation_receipts(self):
        config, run_dir = self.phone()
        self.registry("held", "2026-08-01")
        self.assertEqual(self.inspect(config, run_dir).returncode, 0)
        final = self.finalize(run_dir, draft=self.draft(False))
        self.assertEqual(final.returncode, 0, final.stderr)
        self.assertEqual(self.transitions(run_dir), ["plan_compiled"])
        report = json.loads((self.repo / "state" / "preflight-result.json").read_text())
        self.assertFalse(report["pass"])

    def test_only_readiness_remains_side_effect_free(self):
        config = self.repo / "configs" / "only-readiness.toml"
        config.write_text('''\
[event]
id = "KXEARNINGSMENTIONONLY-26AUG20-WEBCAST"
starts_at = "2026-08-20T21:00:00Z"
[[keywords]]
market_ticker = "KXEARNINGSMENTIONONLY-26AUG20-FOO"
phrase = "foo"
variants = ["foo"]
''')
        hash_proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "keywords-extract.py"),
             str(config), "--sha256"],
            cwd=self.repo, text=True, capture_output=True,
        )
        self.assertEqual(hash_proc.returncode, 0, hash_proc.stderr)
        ready = self.repo / "configs" / ".readiness" / "only-readiness.json"
        ready.write_text(json.dumps({
            "schema": 1, "keywords_sha256": hash_proc.stdout.strip(),
            "date": dt.date.today().isoformat(), "engine": "fixture", "voices": ["fixture"],
            "keywords": [{"ticker": "FOO", "phrase": "foo", "confirmed": 1,
                          "clips": 1}],
        }) + "\n")
        before = sorted(p.relative_to(self.repo).as_posix() for p in self.repo.rglob("*"))
        proc = subprocess.run(
            ["bash", str(ROOT / "scripts" / "preflight-event.sh"),
             "--config", "configs/only-readiness.toml", "--only-readiness"],
            cwd=self.repo, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        after = sorted(p.relative_to(self.repo).as_posix() for p in self.repo.rglob("*"))
        self.assertEqual(after, before)


class NightlyRunPlanSelectionTest(unittest.TestCase):
    """The date sweep is keyed by immutable runs, never env-file presence."""

    DATE = "2026-08-12"
    FINGERPRINT = "sha256:" + "b" * 64

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="preflight-nightly-")
        self.repo = Path(self.tmp.name).resolve()
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "configs").mkdir()
        shutil.copy2(ROOT / "scripts" / "eventctl.py",
                     self.repo / "scripts" / "eventctl.py")
        shutil.copy2(ROOT / "scripts" / "preflight-nightly.sh",
                     self.repo / "scripts" / "preflight-nightly.sh")
        shutil.copy2(ROOT / "scripts" / "alertctl.py",
                     self.repo / "scripts" / "alertctl.py")
        self.calls = self.repo / "preflight-calls.txt"
        self.notify_calls = self.repo / "notify-calls.txt"
        self.phone_calls = self.repo / "phone-calls.txt"
        (self.repo / "scripts" / "preflight-event.sh").write_text(
            "#!/usr/bin/env bash\n"
            "# supports --result-json\n"
            "printf '%s\\n' \"$*\" >> \"$EARSHOT_TEST_PREFLIGHT_CALLS\"\n"
            "exit 0\n"
        )
        notify = self.repo / "scripts" / "notify.sh"
        notify.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$EARSHOT_TEST_NOTIFY_CALLS\"\n"
            "exit 0\n"
        )
        notify.chmod(0o755)
        phone = self.repo / "scripts" / "alert-phone.sh"
        phone.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$EARSHOT_TEST_PHONE_CALLS\"\n"
            "exit 0\n"
        )
        phone.chmod(0o755)
        self.env = {
            **os.environ,
            "EARSHOT_REPO": str(self.repo),
            "EARSHOT_TEST_PREFLIGHT_CALLS": str(self.calls),
            "EARSHOT_TEST_NOTIFY_CALLS": str(self.notify_calls),
            "EARSHOT_TEST_PHONE_CALLS": str(self.phone_calls),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, slug: str) -> Path:
        token = slug.upper().replace("-", "")
        path = self.repo / "configs" / f"{slug}.toml"
        path.write_text(f'''\
[event]
id = "KXEARNINGSMENTION{token}-26AUG12-WEBCAST"
starts_at = "2026-08-12T13:00:00Z"

[[sources]]
kind = "pipe"
cmd = ["ffmpeg", "-i", "$HOME/{slug}-stream.url"]

[[keywords]]
market_ticker = "KXEARNINGSMENTION{token}-26AUG12-FOO"
phrase = "foo"
''')
        return path

    def write_env(self, slug: str, config: Path) -> Path:
        path = self.repo / "configs" / f"{slug}-autorun.env"
        path.write_text(
            f'EVENT_NAME="{slug.upper()}"\n'
            f'CONFIG_PATH="{config.relative_to(self.repo).as_posix()}"\n'
            f'EVENT_DATE="{self.DATE}"\n'
            'ACCOUNT_ENV="$HOME/.earshot/test.env"\n'
            'FIRE_HHMM=1235\n'
        )
        return path

    def init_run(self, config: Path, env_path: Path | None = None) -> Path:
        argv = [
            sys.executable, str(self.repo / "scripts" / "eventctl.py"),
            "init", "--config", str(config.relative_to(self.repo)),
            "--host-id", "preflight-test-host",
            "--account-fingerprint", self.FINGERPRINT,
        ]
        if env_path is not None:
            argv.extend(["--event-env", str(env_path.relative_to(self.repo))])
        proc = subprocess.run(
            argv, cwd=self.repo, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        return Path(proc.stdout.split(": ", 1)[1].strip())

    def nightly(self, date: str | None = None, *, no_mail: bool = True,
                extra_env: dict[str, str] | None = None):
        argv = [
            "bash", str(self.repo / "scripts" / "preflight-nightly.sh"),
            "--date", date or self.DATE,
        ]
        if no_mail:
            argv.append("--no-mail")
        return subprocess.run(
            argv, cwd=self.repo, env={**self.env, **(extra_env or {})},
            text=True, capture_output=True,
        )

    @staticmethod
    def compat_rows(run_dir: Path) -> list[dict]:
        ledger = run_dir / "control" / "receipts.jsonl"
        return [
            row for row in map(json.loads, ledger.read_text().splitlines())
            if row.get("kind") == "compat"
        ]

    def test_missing_run_inputs_fail_but_ready_sibling_still_runs(self):
        no_env_cfg = self.write_config("a-noenv")
        no_env_run = self.init_run(no_env_cfg)

        no_access_cfg = self.write_config("b-noaccess")
        no_access_env = self.write_env("b-noaccess", no_access_cfg)
        no_access_run = self.init_run(no_access_cfg, no_access_env)

        deleted_env_cfg = self.write_config("c-deletedenv")
        deleted_env = self.write_env("c-deletedenv", deleted_env_cfg)
        deleted_env_run = self.init_run(deleted_env_cfg, deleted_env)
        deleted_env.unlink()

        good_cfg = self.write_config("z-good")
        good_env = self.write_env("z-good", good_cfg)
        good_run = self.init_run(good_cfg, good_env)
        (good_run / "control" / "access-plan.json").write_text("{}\n")

        # An env without a RunPlan is deliberately not part of the event set.
        (self.repo / "configs" / "orphan-autorun.env").write_text(
            f'EVENT_NAME="ORPHAN"\nEVENT_DATE="{self.DATE}"\n'
        )

        proc = self.nightly()
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn(f"{no_env_run.parent.name}/{no_env_run.name} FAIL", combined)
        self.assertIn("active RunPlan has no event_env_path", combined)
        self.assertIn(f"{no_access_run.parent.name}/{no_access_run.name} FAIL", combined)
        self.assertIn("required webcast AccessPlan missing", combined)
        self.assertIn(
            f"{deleted_env_run.parent.name}/{deleted_env_run.name} FAIL",
            combined,
        )
        self.assertIn("pinned event env configs/c-deletedenv-autorun.env is missing", combined)
        self.assertIn(f"{good_run.parent.name}/{good_run.name} PASS", combined)
        self.assertIn("1 PASS, 3 FAIL", combined)
        self.assertNotIn("ORPHAN", combined)

        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn(f"--run-dir {good_run}", calls[0])
        self.assertIn(
            f"--access-plan {good_run / 'control' / 'access-plan.json'}",
            calls[0],
        )
        self.assertIn("--config configs/z-good.toml", calls[0])

        # --no-mail is a no-notification mode, not a request-only mode. It
        # must leave no undelivered alert obligation in any failed run.
        for run_dir in (no_env_run, no_access_run, deleted_env_run):
            self.assertFalse(any(
                row.get("action", "").startswith("alert.")
                for row in self.compat_rows(run_dir)
            ))
        self.assertFalse(self.notify_calls.exists())
        self.assertFalse(self.phone_calls.exists())

    def test_verified_failures_raise_per_run_alerts_but_corrupt_plan_uses_aggregate(self):
        no_env_cfg = self.write_config("a-noenv-alert")
        no_env_run = self.init_run(no_env_cfg)

        no_access_cfg = self.write_config("b-noaccess-alert")
        no_access_env = self.write_env("b-noaccess-alert", no_access_cfg)
        no_access_run = self.init_run(no_access_cfg, no_access_env)

        corrupt_cfg = self.write_config("m-corrupt-alert")
        corrupt_env = self.write_env("m-corrupt-alert", corrupt_cfg)
        corrupt_run = self.init_run(corrupt_cfg, corrupt_env)
        corrupt_plan_path = corrupt_run / "control" / "run-plan.json"
        corrupt_plan = json.loads(corrupt_plan_path.read_text())
        corrupt_plan["execution_host_id"] = "tampered-host"
        corrupt_plan_path.write_text(json.dumps(corrupt_plan) + "\n")

        good_cfg = self.write_config("z-good-alert")
        good_env = self.write_env("z-good-alert", good_cfg)
        good_run = self.init_run(good_cfg, good_env)
        (good_run / "control" / "access-plan.json").write_text("{}\n")

        proc = self.nightly(no_mail=False)
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn(f"{no_env_run.parent.name}/{no_env_run.name} FAIL", combined)
        self.assertIn(f"{no_access_run.parent.name}/{no_access_run.name} FAIL", combined)
        self.assertIn(f"{corrupt_run.parent.name}/{corrupt_run.name} FAIL", combined)
        self.assertIn("RunPlan/ledger verification failed", combined)
        self.assertIn(f"{good_run.parent.name}/{good_run.name} PASS", combined)

        for run_dir in (no_env_run, no_access_run):
            rows = self.compat_rows(run_dir)
            required = [row for row in rows if row["action"].startswith("alert.required:")]
            self.assertEqual(len(required), 1, rows)
            request = json.loads(required[0]["detail"])
            self.assertEqual(request["severity"], "critical")
            self.assertEqual(request["code"], "preflight_failed")
            self.assertEqual(request["dedupe_key"], "nightly_preflight")
            self.assertIn(run_dir.parent.name, request["subject"])
            actions = [row["action"] for row in rows]
            self.assertTrue(any("delivery_started" in action and ":local:" in action
                                for action in actions), actions)
            self.assertTrue(any("delivery_started" in action and ":phone:" in action
                                for action in actions), actions)

        # The plan that could not be verified is not safe alert identity. Its
        # only notification is the existing aggregate summary.
        self.assertFalse(any(
            row.get("action", "").startswith("alert.")
            for row in self.compat_rows(corrupt_run)
        ))
        self.assertFalse(any(
            row.get("action", "").startswith("alert.")
            for row in self.compat_rows(good_run)
        ))
        self.assertEqual(len(self.phone_calls.read_text().splitlines()), 2)
        self.assertTrue(self.notify_calls.exists())

    def test_alert_delivery_failure_preserves_preflight_failure_and_siblings(self):
        failed_cfg = self.write_config("a-alertctl-fails")
        failed_env = self.write_env("a-alertctl-fails", failed_cfg)
        failed_run = self.init_run(failed_cfg, failed_env)

        good_cfg = self.write_config("z-still-runs")
        good_env = self.write_env("z-still-runs", good_cfg)
        good_run = self.init_run(good_cfg, good_env)
        (good_run / "control" / "access-plan.json").write_text("{}\n")

        failing_alertctl = self.repo / "scripts" / "alertctl-fails.py"
        failing_alertctl.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('fixture delivery failed', file=sys.stderr)\n"
            "raise SystemExit(9)\n"
        )
        proc = self.nightly(
            no_mail=False,
            extra_env={"EARSHOT_ALERTCTL": str(failing_alertctl)},
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn("required webcast AccessPlan missing", combined)
        self.assertIn("PREFLIGHT-NIGHTLY-RUN-ALERT-FAILED", combined)
        self.assertIn("original preflight failure preserved", combined)
        self.assertIn(f"{good_run.parent.name}/{good_run.name} PASS", combined)
        self.assertFalse(any(
            row.get("action", "").startswith("alert.")
            for row in self.compat_rows(failed_run)
        ))

    def test_zero_runplans_is_not_an_all_clear(self):
        proc = self.nightly()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(
            f"no schema-3 RunPlans exist for {self.DATE}",
            proc.stdout + proc.stderr,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
