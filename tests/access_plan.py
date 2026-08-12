#!/usr/bin/env python3
"""Offline contract tests for scripts/access-plan.py."""

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
ACCESS = ROOT / "scripts" / "access-plan.py"
EVENTCTL = ROOT / "scripts" / "eventctl.py"
EVENT = "KXEARNINGSMENTIONTEST-26AUG20-WEBCAST"


WEBCAST = '''\
[event]
id = "{event}"
starts_at = "2026-08-20T21:00:00Z"

[[sources]]
kind = "pipe"
label = "primary"
cmd = ["/bin/sh", "-c", "q4-player $(cat $HOME/test-stream.url) | ffmpeg -f s16le -"]

[[sources]]
kind = "pipe"
label = "backstop"
cmd = ["/bin/sh", "-c", "ffmpeg -i $(cat ${{HOME}}/test-stream.url) -f s16le -"]

[[keywords]]
market_ticker = "KXEARNINGSMENTIONTEST-26AUG20-FOO"
phrase = "foo"
'''

PHONE = '''\
[event]
id = "KXEARNINGSMENTIONTEST-26AUG20-PHONE"
starts_at = "2026-08-20T21:00:00Z"
[[sources]]
kind = "twilio_stream"
bind = "127.0.0.1:8080"
[[keywords]]
market_ticker = "KXEARNINGSMENTIONTEST-26AUG20-FOO"
phrase = "foo"
'''


class AccessPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="access-plan-")
        self.repo = Path(self.tmp.name).resolve()
        (self.repo / "scripts").mkdir()
        (self.repo / "configs").mkdir()
        shutil.copy2(EVENTCTL, self.repo / "scripts" / "eventctl.py")
        shutil.copy2(ACCESS, self.repo / "scripts" / "access-plan.py")
        self.env = {
            **os.environ,
            "EARSHOT_REPO": str(self.repo),
            "EARSHOT_HOST_ID": "box-useast1",
            "EARSHOT_KALSHI_ACCOUNT_FINGERPRINT": "sha256:" + "a" * 64,
        }
        self.config = self.repo / "configs" / "webcast-test.toml"
        self.config.write_text(WEBCAST.format(event=EVENT))
        self.run_dir = self.init(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, script: str, *args):
        return subprocess.run(
            [sys.executable, str(self.repo / "scripts" / script), *map(str, args)],
            cwd=self.repo, env=self.env, text=True, capture_output=True,
        )

    def init(self, config: Path) -> Path:
        proc = self.cli("eventctl.py", "init", "--config", config)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        return Path(proc.stdout.split(": ", 1)[1].strip())

    def create(self, *extra):
        return self.cli(
            "access-plan.py", "create", "--run-dir", self.run_dir,
            "--vantage", "us-east1/box-primary",
            "--resolver-kind", "q4-api",
            "--resolver-argv-json",
            '["scripts/q4-resolve.sh","--event-id","123","--slug","test"]',
            "--url-file", "$HOME/test-stream.url",
            "--deadline-utc", "2026-08-20T21:05:00Z",
            "--expiry-mode", "refresh-at-launch", *extra,
        )

    def bind(self, mode="public", *extra):
        return self.cli(
            "access-plan.py", "bind", "--run-dir", self.run_dir,
            "--access-mode", mode,
            "--resolver-kind", "q4-api",
            "--resolver-argv-json",
            '["scripts/q4-resolve.sh","--event-id","123","--slug","test"]',
            *extra,
        )

    def test_create_is_non_media_proof_and_validate_round_trips(self):
        proc = self.create()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = self.run_dir / "control" / "access-plan.json"
        plan = json.loads(path.read_text())
        self.assertEqual(plan["schema"], 1)
        self.assertEqual(plan["event_id"], EVENT)
        self.assertEqual(plan["capture"]["host_id"], "box-useast1")
        self.assertEqual(plan["capture"]["vantage"], "us-east1/box-primary")
        self.assertFalse(plan["resolver"]["network_performed_at_compile"])
        self.assertFalse(plan["resolver"]["secret_material_embedded"])
        self.assertEqual(plan["endpoint"]["url_files"], ["$HOME/test-stream.url"])
        self.assertEqual(plan["current_media"], {
            "belongs_to_transition": "capture_ready", "pcm_proof": None,
            "proven": False,
        })
        self.assertEqual(len(plan["sources"]), 2)
        validated = self.cli("access-plan.py", "validate", "--run-dir", self.run_dir)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        payload = json.loads(validated.stdout)
        self.assertFalse(payload["current_media_proven"])
        self.assertEqual(payload["access_plan_sha256"], plan["access_plan_sha256"])

    def test_same_create_is_idempotent_but_semantic_collision_refuses(self):
        self.assertEqual(self.create().returncode, 0)
        second = self.create()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already-valid", second.stdout)
        collision = self.cli(
            "access-plan.py", "create", "--run-dir", self.run_dir,
            "--vantage", "different-vantage", "--resolver-kind", "q4-api",
            "--resolver-argv-json", '["scripts/q4-resolve.sh"]',
            "--url-file", "$HOME/test-stream.url",
            "--deadline-utc", "2026-08-20T21:05:00Z",
            "--expiry-mode", "refresh-at-launch",
        )
        self.assertEqual(collision.returncode, 1)
        self.assertIn("collision", collision.stderr)

    def test_url_file_must_exactly_match_source_argv(self):
        bad = self.cli(
            "access-plan.py", "create", "--run-dir", self.run_dir,
            "--vantage", "us-east1", "--resolver-kind", "q4-api",
            "--resolver-argv-json", '["scripts/q4-resolve.sh"]',
            "--url-file", "$HOME/wrong-stream.url",
            "--deadline-utc", "2026-08-20T21:05:00Z",
            "--expiry-mode", "refresh-at-launch",
        )
        self.assertEqual(bad.returncode, 1)
        self.assertIn("exactly match", bad.stderr)

    def test_tamper_and_config_drift_fail_validation(self):
        self.assertEqual(self.create().returncode, 0)
        path = self.run_dir / "control" / "access-plan.json"
        value = json.loads(path.read_text())
        value["capture"]["vantage"] = "tampered"
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        bad = self.cli("access-plan.py", "validate", "--run-dir", self.run_dir)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("self-hash", bad.stderr)

        # Recreate in a new isolated run is unnecessary: eventctl itself must
        # reject changed pinned config bytes before AccessPlan can be trusted.
        self.config.write_text(self.config.read_text() + "\n# drift\n")
        drift = self.cli("access-plan.py", "validate", "--run-dir", self.run_dir)
        self.assertEqual(drift.returncode, 1)
        self.assertIn("eventctl verify refused", drift.stderr)

    def test_phone_run_refuses_and_static_public_needs_no_url_file(self):
        phone = self.repo / "configs" / "phone.toml"
        phone.write_text(PHONE)
        phone_run = self.init(phone)
        refused = self.cli(
            "access-plan.py", "create", "--run-dir", phone_run,
            "--vantage", "us-east1", "--resolver-kind", "static-public",
            "--deadline-utc", "2026-08-20T21:00:00Z",
            "--expiry-mode", "stable-public",
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("webcast-only", refused.stderr)

    def test_compile_never_executes_resolver_argv(self):
        sentinel = self.repo / "resolver-ran"
        proc = self.cli(
            "access-plan.py", "create", "--run-dir", self.run_dir,
            "--vantage", "us-east1", "--resolver-kind", "external-command",
            "--resolver-argv-json", json.dumps(["/bin/sh", "-c", f"touch {sentinel}"]),
            "--url-file", "$HOME/test-stream.url",
            "--deadline-utc", "2026-08-20T21:00:00Z",
            "--expiry-mode", "refresh-at-launch",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(sentinel.exists())

    def test_literal_secret_material_is_refused(self):
        proc = self.cli(
            "access-plan.py", "create", "--run-dir", self.run_dir,
            "--vantage", "us-east1", "--resolver-kind", "external-command",
            "--resolver-argv-json", '["resolver","--auth-token","literal-secret"]',
            "--url-file", "$HOME/test-stream.url",
            "--deadline-utc", "2026-08-20T21:00:00Z",
            "--expiry-mode", "refresh-at-launch",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("sensitive option", proc.stderr)

    def test_deadline_and_declared_expiry_are_enforced_on_create_and_validate(self):
        late = self.cli(
            "access-plan.py", "create", "--run-dir", self.run_dir,
            "--vantage", "us-east1", "--resolver-kind", "q4-api",
            "--resolver-argv-json", '["scripts/q4-resolve.sh"]',
            "--url-file", "$HOME/test-stream.url",
            "--deadline-utc", "2026-08-20T22:00:00Z",
            "--expiry-mode", "refresh-at-launch",
        )
        self.assertEqual(late.returncode, 1)
        self.assertIn("30 minutes", late.stderr)

        expired = self.cli(
            "access-plan.py", "create", "--run-dir", self.run_dir,
            "--vantage", "us-east1", "--resolver-kind", "q4-api",
            "--resolver-argv-json", '["scripts/q4-resolve.sh"]',
            "--url-file", "$HOME/test-stream.url",
            "--deadline-utc", "2026-08-20T21:05:00Z",
            "--expiry-mode", "resolver-declared",
            "--expires-at-utc", "2026-08-20T21:04:59Z",
        )
        self.assertEqual(expired.returncode, 1)
        self.assertIn("after its resolver deadline", expired.stderr)

        self.assertEqual(self.create().returncode, 0)
        path = self.run_dir / "control" / "access-plan.json"
        value = json.loads(path.read_text())
        value["resolver"]["deadline_utc"] = "2026-08-20T22:00:00Z"
        hashed = {k: v for k, v in value.items() if k != "access_plan_sha256"}
        value["access_plan_sha256"] = hashlib.sha256(json.dumps(
            hashed, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        validated = self.cli("access-plan.py", "validate", "--run-dir", self.run_dir)
        self.assertEqual(validated.returncode, 1)
        self.assertIn("30 minutes", validated.stderr)

    def test_canonical_public_bind_is_explicit_schema_2_and_idempotent(self):
        bound = self.bind()
        self.assertEqual(bound.returncode, 0, bound.stderr)
        path = self.run_dir / "control" / "access-plan.json"
        plan = json.loads(path.read_text())
        self.assertEqual(plan["schema"], 2)
        self.assertEqual(plan["capture"]["vantage"], "box-useast1")
        self.assertEqual(plan["endpoint"]["url_files"], ["$HOME/test-stream.url"])
        self.assertEqual(plan["resolver"]["deadline_utc"], "2026-08-20T21:15:00Z")
        self.assertEqual(plan["access_prerequisite"], {
            "attested_by": None,
            "mode": "public",
            "requirement": "none",
            "satisfied": True,
            "secret_material_embedded": False,
            "session_expires_at_utc": None,
            "session_reference": None,
        })
        second = self.bind()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already-valid", second.stdout)
        validated = self.cli("access-plan.py", "validate", "--run-dir", self.run_dir)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        value = json.loads(validated.stdout)
        self.assertEqual(value["access_plan_schema"], 2)
        self.assertEqual(value["access_prerequisite_mode"], "public")
        self.assertTrue(value["access_prerequisite_satisfied"])
        canonical = self.cli(
            "access-plan.py", "validate", "--run-dir", self.run_dir,
            "--canonical",
        )
        self.assertEqual(canonical.returncode, 0, canonical.stderr)

    def test_human_session_bind_requires_explicit_non_secret_attestation(self):
        missing = self.bind("registered-session")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("explicit completed-session attestation", missing.stderr)
        self.assertFalse((self.run_dir / "control" / "access-plan.json").exists())

        unsafe = self.bind(
            "registered-session",
            "--attested-by", "operator-1",
            "--session-ref", "https://example.test/?token=secret",
            "--session-expires-at-utc", "2026-08-20T22:00:00Z",
        )
        self.assertEqual(unsafe.returncode, 1)
        self.assertIn("never a cookie/token/URL", unsafe.stderr)

        expires_early = self.bind(
            "registered-session",
            "--attested-by", "operator-1",
            "--session-ref", "browser-profile:earnings-listen-only",
            "--session-expires-at-utc", "2026-08-20T21:14:59Z",
        )
        self.assertEqual(expires_early.returncode, 1)
        self.assertIn("beyond the resolver deadline", expires_early.stderr)

        good = self.bind(
            "registered-session",
            "--attested-by", "operator-1",
            "--session-ref", "browser-profile:earnings-listen-only",
            "--session-expires-at-utc", "2026-08-20T22:00:00Z",
        )
        self.assertEqual(good.returncode, 0, good.stderr)
        plan = json.loads((self.run_dir / "control" / "access-plan.json").read_text())
        self.assertEqual(
            plan["access_prerequisite"]["requirement"],
            "registration-complete",
        )
        self.assertEqual(
            plan["access_prerequisite"]["session_reference"],
            "browser-profile:earnings-listen-only",
        )

    def test_date_gate_continues_phone_sibling_and_requires_schema_2(self):
        missing = self.cli("access-plan.py", "check-day", "--date", "2026-08-20")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("missing canonical schema-2", missing.stderr)
        self.assertIn("bind-access", missing.stdout)

        self.assertEqual(self.bind().returncode, 0)
        phone = self.repo / "configs" / "phone.toml"
        phone.write_text(PHONE)
        self.init(phone)
        checked = self.cli("access-plan.py", "check-day", "--date", "2026-08-20")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("ACCESS-DAY PASS", checked.stdout)
        self.assertIn("phone lane uses admission strategy", checked.stdout)
        self.assertIn("selected=2 pass=1 phone=1", checked.stdout)

    def test_date_gate_rejects_legacy_plan_and_zero_plan_date(self):
        self.assertEqual(self.create().returncode, 0)
        canonical = self.cli(
            "access-plan.py", "validate", "--run-dir", self.run_dir,
            "--canonical",
        )
        self.assertEqual(canonical.returncode, 1)
        self.assertIn("requires schema-2", canonical.stderr)
        legacy = self.cli("access-plan.py", "check-day", "--date", "2026-08-20")
        self.assertEqual(legacy.returncode, 1)
        self.assertIn("legacy schema-1", legacy.stderr)
        self.assertIn("new immutable RunPlan attempt", legacy.stdout)

        empty = self.cli("access-plan.py", "check-day", "--date", "2026-08-19")
        self.assertEqual(empty.returncode, 1)
        self.assertIn("no schema-3 RunPlans", empty.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
