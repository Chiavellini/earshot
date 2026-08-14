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
            # BOTH identity variables are pinned. eventctl and access-plan read
            # EARSHOT_EXECUTION_HOST first, so leaving it to the ambient shell
            # would make the cross-host vantage rules pass or fail by whatever
            # the developer happens to export.
            "EARSHOT_EXECUTION_HOST": "box-useast1",
            "EARSHOT_HOST_ID": "box-useast1",
            "EARSHOT_KALSHI_ACCOUNT_FINGERPRINT": "sha256:" + "a" * 64,
        }
        self.config = self.repo / "configs" / "webcast-test.toml"
        self.config.write_text(WEBCAST.format(event=EVENT))
        self.run_dir = self.init(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, script: str, *args, env_overrides=None):
        return subprocess.run(
            [sys.executable, str(self.repo / "scripts" / script), *map(str, args)],
            cwd=self.repo, env={**self.env, **(env_overrides or {})},
            text=True, capture_output=True,
        )

    PRERESOLVE_ARGV = json.dumps([
        "python3", "scripts/browser-resolve.py", "--slug", "test",
        "--url", "https://venue.invalid/player",
        "--out", "$HOME/test-stream.url",
    ])

    def bind_preresolved(self, *extra, wait_argv=None, vantage="us-east1/box-primary"):
        argv = wait_argv or ["scripts/urlfile-wait.sh", "$HOME/test-stream.url"]
        base = [
            "access-plan.py", "bind", "--run-dir", self.run_dir,
            "--access-mode", "public",
            "--resolver-kind", "preresolved",
            "--resolver-argv-json", json.dumps(argv),
            "--preresolve-kind", "browser-network",
            "--preresolve-argv-json", self.PRERESOLVE_ARGV,
        ]
        if vantage:
            base += ["--vantage", vantage]
        return self.cli(*base, *extra)

    def plan_sha(self) -> str:
        return json.loads(
            (self.run_dir / "control" / "run-plan.json").read_text()
        )["plan_sha256"]

    def rehash_artifact(self, value: dict):
        """Rewrite the plan with a recomputed self-hash, canonical bytes."""
        path = self.run_dir / "control" / "access-plan.json"
        hashed = {k: v for k, v in value.items() if k != "access_plan_sha256"}
        value["access_plan_sha256"] = hashlib.sha256(json.dumps(
            hashed, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")

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

    def bind(self, mode="public", *extra, env_overrides=None):
        return self.cli(
            "access-plan.py", "bind", "--run-dir", self.run_dir,
            "--access-mode", mode,
            "--resolver-kind", "q4-api",
            "--resolver-argv-json",
            '["scripts/q4-resolve.sh","--event-id","123","--slug","test"]',
            *extra, env_overrides=env_overrides,
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

    def test_preresolved_bind_records_both_legs_and_bakes_attempt_identity(self):
        bound = self.bind_preresolved()
        self.assertEqual(bound.returncode, 0, bound.stderr)
        plan = json.loads(
            (self.run_dir / "control" / "access-plan.json").read_text()
        )
        resolver = plan["resolver"]
        self.assertEqual(resolver["kind"], "preresolved")
        self.assertEqual(plan["capture"]["vantage"], "us-east1/box-primary")
        # bind DERIVES the wait's bounds; the operator never retypes them.
        argv = resolver["argv"]
        self.assertEqual(argv[0], "scripts/urlfile-wait.sh")
        self.assertEqual(argv[1], "$HOME/test-stream.url")
        pairs = dict(zip(argv[2::2], argv[3::2]))
        self.assertEqual(pairs["--deadline-utc"], resolver["deadline_utc"])
        self.assertEqual(pairs["--expect-plan-sha256"], self.plan_sha())
        self.assertEqual(pairs["--fresh-secs"], "5400")
        # …and records WHICH resolver mints the URL on the other host.
        self.assertEqual(resolver["preresolve"], {
            "kind": "browser-network", "argv": json.loads(self.PRERESOLVE_ARGV),
        })
        validated = self.cli(
            "access-plan.py", "validate", "--run-dir", self.run_dir, "--canonical",
        )
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertEqual(
            json.loads(validated.stdout)["resolver_kind"], "preresolved"
        )

    def test_preresolved_refuses_a_defaulted_vantage(self):
        # Cross-host by construction: one host mints, another captures. A
        # defaulted vantage would silently pool us-east capture with laptop
        # Δrace history.
        refused = self.bind_preresolved(vantage=None)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("cross-host by construction", refused.stderr)

    def test_bind_refuses_a_defaulted_vantage_for_the_capture_box(self):
        # The plan is compiled for box-useast1 (setUp's host id); bind runs on
        # the laptop and box-useast1 IS the declared box, so the vantage must be
        # deliberate.
        laptop = {
            "EARSHOT_EXECUTION_HOST": "laptop-cdmx",
            "EARSHOT_BOX_EXECUTION_HOST": "box-useast1",
        }
        refused = self.bind(env_overrides=laptop)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("--vantage must be named explicitly", refused.stderr)
        named = self.bind("public", "--vantage", "cdmx-residential", env_overrides=laptop)
        self.assertEqual(named.returncode, 0, named.stderr)

    def test_other_cross_host_defaults_warn_but_never_refuse(self):
        # A blanket refusal here would redden sibling suites that bind plans for
        # hosts this node knows nothing about; the WARN carries the same fact.
        warned = self.bind(env_overrides={
            "EARSHOT_EXECUTION_HOST": "laptop-cdmx",
            "EARSHOT_BOX_EXECUTION_HOST": "some-other-box",
        })
        self.assertEqual(warned.returncode, 0, warned.stderr)
        self.assertIn("WARN: --vantage defaulted", warned.stderr)
        plan = json.loads(
            (self.run_dir / "control" / "access-plan.json").read_text()
        )
        self.assertEqual(plan["capture"]["vantage"], "box-useast1")

    def test_unresolvable_local_identity_keeps_the_default(self):
        blank = self.bind(env_overrides={
            "EARSHOT_EXECUTION_HOST": "", "EARSHOT_HOST_ID": "",
        })
        self.assertEqual(blank.returncode, 0, blank.stderr)
        self.assertNotIn("WARN: --vantage defaulted", blank.stderr)

    def test_preresolved_requires_the_other_host_resolve_leg(self):
        missing = self.cli(
            "access-plan.py", "bind", "--run-dir", self.run_dir,
            "--access-mode", "public", "--vantage", "us-east1",
            "--resolver-kind", "preresolved",
            "--resolver-argv-json",
            '["scripts/urlfile-wait.sh","$HOME/test-stream.url"]',
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("--preresolve-kind", missing.stderr)

    def test_preresolve_fields_are_refused_on_every_other_kind(self):
        refused = self.bind(
            "public",
            "--preresolve-kind", "browser-network",
            "--preresolve-argv-json", self.PRERESOLVE_ARGV,
        )
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn("only valid for resolver kind preresolved", refused.stderr)

    def test_preresolved_argv_must_use_the_expandable_home_spelling(self):
        braced = self.bind_preresolved(
            wait_argv=["scripts/urlfile-wait.sh", "${HOME}/test-stream.url"],
        )
        self.assertEqual(braced.returncode, 1)
        # arm-on-url expands only a literal $HOME/ prefix; ${HOME}/ would make
        # the wait address ./${HOME}/... under the repo.
        self.assertIn("spell URL files as $HOME/", braced.stderr)

    def test_preresolved_must_wait_on_the_file_its_sources_read(self):
        wrong = self.bind_preresolved(
            wait_argv=["scripts/urlfile-wait.sh", "$HOME/other-stream.url"],
        )
        self.assertEqual(wrong.returncode, 1)
        self.assertIn("wait on exactly the URL files", wrong.stderr)

    def test_preresolved_argv_bounds_may_not_contradict_the_plan(self):
        lying = self.bind_preresolved(wait_argv=[
            "scripts/urlfile-wait.sh", "$HOME/test-stream.url",
            "--expect-plan-sha256", "f" * 64,
        ])
        self.assertEqual(lying.returncode, 1)
        self.assertIn("contradicts the AccessPlan", lying.stderr)

    def test_a_smuggled_preresolve_block_is_refused_on_other_kinds(self):
        self.assertEqual(self.bind().returncode, 0)
        path = self.run_dir / "control" / "access-plan.json"
        value = json.loads(path.read_text())
        self.assertEqual(value["resolver"]["kind"], "q4-api")
        self.assertNotIn("preresolve", value["resolver"])
        value["resolver"]["preresolve"] = {
            "kind": "browser-network", "argv": json.loads(self.PRERESOLVE_ARGV),
        }
        self.rehash_artifact(value)
        validated = self.cli("access-plan.py", "validate", "--run-dir", self.run_dir)
        self.assertEqual(validated.returncode, 1)
        self.assertIn("resolver shape is invalid", validated.stderr)

    def test_preresolved_plan_survives_a_prior_attempts_wait_argv(self):
        # The AMAT 2026-08-13 shape: two attempts, one url-file path, one
        # deadline. What separates them is the plan hash baked into argv.
        self.assertEqual(self.bind_preresolved().returncode, 0)
        first = json.loads(
            (self.run_dir / "control" / "access-plan.json").read_text()
        )
        first_expect = dict(
            zip(first["resolver"]["argv"][2::2], first["resolver"]["argv"][3::2])
        )["--expect-plan-sha256"]
        # A second attempt is what config drift produces — the AMAT r1/r2
        # shape. The [[sources]] argv is untouched, so the url-file is identical.
        self.config.write_text(
            WEBCAST.format(event=EVENT)
            + '\n[[keywords]]\n'
            'market_ticker = "KXEARNINGSMENTIONTEST-26AUG20-BAR"\n'
            'phrase = "bar"\n'
        )
        second_run = self.init(self.config)
        self.assertNotEqual(second_run, self.run_dir)
        self.run_dir = second_run
        self.assertEqual(self.bind_preresolved().returncode, 0)
        second = json.loads((second_run / "control" / "access-plan.json").read_text())
        second_expect = dict(
            zip(second["resolver"]["argv"][2::2], second["resolver"]["argv"][3::2])
        )["--expect-plan-sha256"]
        self.assertEqual(
            first["endpoint"]["url_files"], second["endpoint"]["url_files"]
        )
        self.assertEqual(
            first["resolver"]["deadline_utc"], second["resolver"]["deadline_utc"]
        )
        self.assertNotEqual(first_expect, second_expect)

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
