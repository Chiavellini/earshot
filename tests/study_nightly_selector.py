#!/usr/bin/env python3
"""study-nightly RunPlan selection vs superseded attempts (AMAT 2026-08-13).

A terminal/archived predecessor attempt used to count as a fatal
"multiple RunPlans select <config>" collision, making the whole date
unstudyable after any supersession. The selector must skip closed attempts
(the typed reducer is the authority) and still refuse two genuinely ACTIVE
attempts on one config.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATE = "2026-08-12"
EVENT = "KXEARNINGSMENTIONSEL-26AUG12-WEBCAST"

CONFIG = f"""[event]
id = "{EVENT}"
starts_at = "{DATE}T21:00:00Z"

[[sources]]
kind = "pipe"
cmd = ["ffmpeg", "-i", "$HOME/sel-stream.url"]

[[keywords]]
market_ticker = "KXEARNINGSMENTIONSEL-26AUG12-FOO"
phrase = "foo"
"""

ENV = f"""CONFIG_PATH="configs/sel-webcast-arm-wc.toml"
EVENT_DATE="{DATE}"
ACCOUNT_ENV="$HOME/.earshot/account.env"
"""


class StudySelectorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="study-selector-")
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / "configs").mkdir(parents=True)
        (self.repo / "scripts").mkdir()
        (self.repo / "state").mkdir()
        for name in (
            "eventctl.py",
            "study-nightly.sh",
            "doc-lint.py",
        ):
            src = ROOT / "scripts" / name
            if src.exists():
                (self.repo / "scripts" / name).write_bytes(src.read_bytes())
        for stub in ("alertctl-stub", "notify-stub"):
            p = self.repo / "scripts" / stub
            p.write_text("#!/bin/sh\nexit 0\n")
            p.chmod(0o755)
        bin_dir = self.repo / "target" / "release"
        bin_dir.mkdir(parents=True)
        fake_bin = bin_dir / "earshot"
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        fake_bin.chmod(0o755)
        self.cfg = self.repo / "configs" / "sel-webcast-arm-wc.toml"
        self.cfg.write_text(CONFIG)
        (self.repo / "configs" / "sel-autorun.env").write_text(ENV)
        self.env = os.environ.copy()
        self.env.update(
            {
                "EARSHOT_REPO": str(self.repo),
                "EARSHOT_HOST_ID": "earshot-test-host",
                "EARSHOT_KALSHI_ACCOUNT_FINGERPRINT": "sha256:" + "a" * 64,
                "_EARSHOT_STUDY_ALERTCTL": str(self.repo / "scripts" / "alertctl-stub"),
                "_EARSHOT_STUDY_NOTIFY": str(self.repo / "scripts" / "notify-stub"),
            }
        )

    def tearDown(self):
        self.temp.cleanup()

    def eventctl(self, *argv):
        return subprocess.run(
            [sys.executable, str(self.repo / "scripts" / "eventctl.py"), *argv],
            cwd=self.repo, env=self.env, capture_output=True, text=True,
        )

    def study(self):
        return subprocess.run(
            ["bash", str(self.repo / "scripts" / "study-nightly.sh"),
             "--date", DATE, "--dry-run"],
            cwd=self.repo, env=self.env, capture_output=True, text=True,
        )

    def init(self, run_id=None):
        argv = ["init", "--config", "configs/sel-webcast-arm-wc.toml"]
        if run_id:
            argv += ["--run-id", run_id]
        r = self.eventctl(*argv)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return Path(r.stdout.split(": ", 1)[1].strip())

    def test_archived_predecessor_does_not_collide(self):
        first = self.init()
        # supersession: bytes change, predecessor closed, successor inited
        self.cfg.write_text(CONFIG + "\n# posture edit\n")
        for transition in ("blocked", "archived"):
            r = self.eventctl(
                "record", "--run-dir", str(first),
                "--transition", transition,
                "--detail", "superseded in test",
            )
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        second = self.init()
        self.assertNotEqual(first, second)
        r = self.study()
        combined = r.stdout + r.stderr
        # The old selector died here with "multiple schema-3 RunPlans select
        # <config>" and the date never studied. Closed predecessor + active
        # successor must select cleanly and run the date to completion.
        self.assertNotIn("multiple", combined)
        self.assertIn("ALL STUDIES DONE", combined)
        self.assertEqual(r.returncode, 0, combined)

    def test_two_active_attempts_still_collide(self):
        self.init()
        self.init(run_id="20260812T210000Z-p3-second-active")
        r = self.study()
        combined = r.stdout + r.stderr
        self.assertIn("multiple", combined)
        self.assertNotEqual(r.returncode, 0, combined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
