#!/usr/bin/env python3
"""Offline fixtures for generic control.prepare staging reconciliation."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MERGE = load_script("slate_time_merge", "scripts/slate-time-merge.py")
RECONCILE = load_script("stage_reconcile", "scripts/stage-reconcile.py")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def minimal_config(event: str, start: str, todo: bool = False) -> str:
    marker = "  # TODO: set the real call time (UTC) — slate had none" if todo else ""
    return f'''[event]
id        = "{event}-WEBCAST"
starts_at = "{start}"{marker}

[pipeline]
sample_rate_hz = 16000

[detector]
engine = "streaming_ctc"

[[sources]]
kind = "pipe"

[kalshi]
env = "demo"

[risk]
max_order_notional_cents = 100
allowlist_tickers = ["{event}-WORD"]

[[keywords]]
market_ticker = "{event}-WORD"
phrase = "alpha word"
variants = ["alpha word"]
max_price_cents = 1
budget_contracts = 1
'''


class DateCorrectionTests(unittest.TestCase):
    def test_cross_date_requires_exact_typed_source_bearing_correction(self):
        ticker = "KXEARNINGSMENTIONALFA-26AUG10"
        slate = {
            "events": [{
                "event_ticker": ticker,
                "issuer": "ALFA",
                "call_date": "2026-08-10",
                "call_time_utc": None,
            }]
        }
        untyped = {
            ticker: {
                "call_time_utc": "2026-08-12T12:00:00Z",
                "source": "Issuer event page",
            }
        }
        merged, errors, _ = MERGE.merge(json.loads(json.dumps(slate)), untyped, None)
        self.assertTrue(errors)
        self.assertEqual(merged["events"][0]["call_date"], "2026-08-10")

        typed = {
            ticker: {
                "call_time_utc": "2026-08-12T12:00:00Z",
                "source": "Issuer event page: noon UTC",
                "date_correction": {
                    "schema": 1,
                    "kind": "call-date-correction",
                    "from": "2026-08-10",
                    "to": "2026-08-12",
                    "source": "Issuer IR release and registration page agree on August 12",
                    "recorded_utc": "2026-08-11T20:00:00Z",
                },
            }
        }
        merged, errors, notes = MERGE.merge(json.loads(json.dumps(slate)), typed, None)
        self.assertFalse(errors)
        row = merged["events"][0]
        self.assertEqual(row["call_date"], "2026-08-12")
        self.assertEqual(row["call_date_source"], "authoritative_correction")
        self.assertEqual(row["call_date_correction"], typed[ticker]["date_correction"])
        self.assertTrue(any("CORRECTED-DATE" in note for note in notes))

        unsourced = json.loads(json.dumps(typed))
        unsourced[ticker]["date_correction"]["source"] = ""
        merged, errors, _ = MERGE.merge(json.loads(json.dumps(slate)), unsourced, None)
        self.assertTrue(errors)
        self.assertEqual(merged["events"][0]["call_date"], "2026-08-10")


class ReconcileBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "configs").mkdir()
        (self.repo / "var" / "runs").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_todo_is_incomplete_and_unpinned_repair_is_idempotent(self):
        event = "KXEARNINGSMENTIONALFA-26AUG12"
        cfg = self.repo / "configs" / "webcast-alfa-26aug12-arm-wc.toml"
        cfg.write_text(minimal_config(event, "2026-08-12T00:00:00Z", todo=True))
        self.assertEqual(
            RECONCILE.config_state(cfg, "2026-08-12T12:00:00Z")[0], "incomplete"
        )
        result = RECONCILE.reconcile_config(
            self.repo, self.repo / "var" / "runs", cfg.relative_to(self.repo).as_posix(),
            "2026-08-12T12:00:00Z",
        )
        self.assertEqual(result, "RECONCILED")
        first = digest(cfg)
        self.assertNotIn("TODO", cfg.read_text())
        self.assertEqual(
            RECONCILE.reconcile_config(
                self.repo, self.repo / "var" / "runs",
                cfg.relative_to(self.repo).as_posix(), "2026-08-12T12:00:00Z",
            ),
            "READY",
        )
        self.assertEqual(digest(cfg), first)

    def test_published_runplan_and_todo_config_are_never_rewritten(self):
        event = "KXEARNINGSMENTIONBETA-26AUG12"
        rel = "configs/webcast-beta-26aug12-arm-wc.toml"
        cfg = self.repo / rel
        cfg.write_text(minimal_config(event, "2026-08-12T00:00:00Z", todo=True))
        plan_dir = self.repo / "var" / "runs" / f"{event}-WEBCAST" / "attempt" / "control"
        plan_dir.mkdir(parents=True)
        plan = plan_dir / "run-plan.json"
        plan.write_text(json.dumps({
            "schema": 3,
            "kind": "run-plan",
            "config_path": rel,
            "config_sha256": digest(cfg),
            "event_env_path": None,
            "event_env_sha256": None,
        }, sort_keys=True))
        before_cfg, before_plan = digest(cfg), digest(plan)
        with self.assertRaisesRegex(RECONCILE.Refusal, "IMMUTABLE-INCOMPLETE"):
            RECONCILE.reconcile_config(
                self.repo, self.repo / "var" / "runs", rel,
                "2026-08-12T12:00:00Z",
            )
        self.assertEqual(digest(cfg), before_cfg)
        self.assertEqual(digest(plan), before_plan)


class SlateStageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.home = Path(self.tmp.name) / "home"
        for directory in (self.repo / "scripts", self.repo / "configs", self.repo / "slate",
                          self.home / ".kalshi"):
            directory.mkdir(parents=True, exist_ok=True)
        for name in ("slate-stage.sh", "stage-reconcile.py", "stage-webcast-arm.sh"):
            shutil.copy2(REPO / "scripts" / name, self.repo / "scripts" / name)
            (self.repo / "scripts" / name).chmod(0o755)
        notify = self.repo / "scripts" / "notify.sh"
        notify.write_text("#!/usr/bin/env bash\nexit 0\n")
        notify.chmod(0o755)
        (self.home / ".kalshi" / "earshot_env").write_text("# offline fixture\n")
        (self.repo / "configs" / "dial-ins.toml").write_text(
            '[ALFA]\nadmission = "no-entry"\n'
        )
        (self.repo / "configs" / "webcast-fanout-arm.toml").write_text('''[event]
id        = "__EVENT_TICKER__"
starts_at = "__START_ISO_UTC__"

[pipeline]
sample_rate_hz = 16000

[detector]
engine = "sherpa_kws"

[[sources]]
kind = "pipe"

[kalshi]
env = "demo"

[risk]
max_order_notional_cents = 100
allowlist_tickers = ["__EVENT_TICKER__-CAPT"]

[[keywords]]
market_ticker = "__EVENT_TICKER__-CAPT"
phrase          = "guidance"
variants = ["guidance"]
max_price_cents = 1
budget_contracts = 1
''')
        self.fake_bin = self.repo / "fake-earshot"
        self.fake_bin.write_text(r"""#!/usr/bin/env python3
import pathlib, sys
args = sys.argv[1:]
event = args[args.index("--event-ticker") + 1]
out = pathlib.Path(args[args.index("--out") + 1])
out.write_text(f'''[event]
id        = "{event}"
starts_at = "2026-07-15T00:00:00Z"  # TODO: set the real call time (UTC)

[pipeline]
sample_rate_hz = 16000

[detector]
engine = "sherpa_kws"

[[sources]]
kind = "twilio_stream"
bind = "127.0.0.1:8080"

[kalshi]
env = "demo"

[risk]
max_order_notional_cents = 100
allowlist_tickers = ["{event}-WORD"]

[[keywords]]
market_ticker = "{event}-WORD"
phrase = "alpha word"
variants = ["alpha word"]
max_price_cents = 1
budget_contracts = 1
''')
""")
        self.fake_bin.chmod(0o755)
        today = dt.datetime.now(dt.timezone.utc).date()
        self.call_date = today.isoformat()
        self.call_time = f"{self.call_date}T12:00:00Z"
        self.datepart = today.strftime("%y%b%d").lower()
        self.event = f"KXEARNINGSMENTIONALFA-{today.strftime('%y%b%d').upper()}"
        self.slate = self.repo / "slate" / "fixture.json"
        self.env = os.environ.copy()
        self.env.update({
            "EARSHOT_REPO": str(self.repo),
            "EARSHOT_BIN": str(self.fake_bin),
            "EARSHOT_RUN_ROOT": str(self.repo / "var" / "runs"),
            "HOME": str(self.home),
        })

    def tearDown(self):
        self.tmp.cleanup()

    def write_slate(self, call_time: str | None):
        self.slate.write_text(json.dumps({
            "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "events": [{
                "event_ticker": self.event,
                "issuer": "ALFA",
                "call_date": self.call_date,
                "call_time_utc": call_time,
                "call_date_disputed": False,
            }],
        }))

    def run_stage(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(self.repo / "scripts" / "slate-stage.sh"),
                *extra,
                "--slate",
                str(self.slate),
            ],
            cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=30,
        )

    def test_todo_then_time_reconciles_config_and_creates_missing_env(self):
        self.write_slate(None)
        first = self.run_stage()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        cfg = self.repo / "configs" / f"webcast-alfa-{self.datepart}-arm-wc.toml"
        env = self.repo / "configs" / f"alfa-{self.datepart}-autorun.env"
        self.assertIn("TODO", cfg.read_text())
        self.assertFalse(env.exists())
        self.assertIn("NEEDS-TIME", first.stdout)
        self.assertNotIn("ALREADY-STAGED", first.stdout)

        self.write_slate(self.call_time)
        todo_hash = digest(cfg)
        dry = self.run_stage("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("RECONCILABLE", dry.stdout)
        self.assertEqual(digest(cfg), todo_hash)
        self.assertFalse(env.exists())

        second = self.run_stage()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("RECONCILED", second.stdout)
        self.assertIn(f'starts_at = "{self.call_time}"', cfg.read_text())
        self.assertNotIn("TODO", cfg.read_text())
        self.assertTrue(env.is_file())
        env_text = env.read_text()
        self.assertIn(f'EVENT_DATE="{self.call_date}"', env_text)
        self.assertIn(f'CONFIG_PATH="configs/webcast-alfa-{self.datepart}-arm-wc.toml"', env_text)
        cfg_hash, env_hash = digest(cfg), digest(env)

        third = self.run_stage()
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        self.assertIn("ALREADY-STAGED", third.stdout)
        self.assertEqual(digest(cfg), cfg_hash)
        self.assertEqual(digest(env), env_hash)

    def test_missing_env_after_plan_is_loud_and_nothing_is_rewritten(self):
        self.write_slate(self.call_time)
        staged = self.run_stage()
        self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
        cfg = self.repo / "configs" / f"webcast-alfa-{self.datepart}-arm-wc.toml"
        env = self.repo / "configs" / f"alfa-{self.datepart}-autorun.env"
        env.unlink()
        compiled = subprocess.run(
            [
                "python3",
                str(REPO / "scripts" / "eventctl.py"),
                "init",
                "--config",
                cfg.relative_to(self.repo).as_posix(),
                "--host-id",
                "fixture-host",
                "--account-fingerprint",
                "sha256:" + "1" * 64,
                "--run-root",
                str(self.repo / "var" / "runs"),
            ],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
        plans = list((self.repo / "var" / "runs").glob("*/*/control/run-plan.json"))
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        cfg_before, plan_before = digest(cfg), digest(plan)
        rerun = self.run_stage()
        self.assertNotEqual(rerun.returncode, 0)
        self.assertIn("IMMUTABLE-INCOMPLETE", rerun.stdout + rerun.stderr)
        self.assertFalse(env.exists())
        self.assertEqual(digest(cfg), cfg_before)
        self.assertEqual(digest(plan), plan_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
