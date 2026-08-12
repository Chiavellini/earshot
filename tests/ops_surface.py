#!/usr/bin/env python3
"""Offline tests for the canonical operations surface."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
CHECKER = REPO / "scripts" / "ops-surface-check.py"


class OpsSurfaceTest(unittest.TestCase):
    def run_checker(self, root: Path, *, ok: bool) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["python3", str(CHECKER), "--repo", str(root)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result

    def fixture(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="ops-surface-test."))
        self.addCleanup(shutil.rmtree, root)
        (root / "architecture").mkdir()
        (root / "scripts").mkdir()
        for name, body in {
            "stage.sh": "#!/bin/sh\nexit 0\n",
            "launch.sh": "#!/bin/sh\nexec scripts/launch-event.sh \"$@\"\n",
            "old.sh": "#!/bin/sh\n# EARSHOT_RETIRED_ENTRYPOINT\necho REFUSED >&2\nexit 64\n",
            "study-26aug04.sh": "#!/bin/sh\nexit 0\n",
        }.items():
            path = root / "scripts" / name
            path.write_text(body)
            path.chmod(0o755)
        (root / "architecture" / "operations.toml").write_text(textwrap.dedent("""
            schema_version = 2
            authority = "canonical-event-operations"
            [[operator_command]]
            name = "stage"
            argv = ["scripts/opsctl.sh", "stage"]
            arguments = "--date YYYY-MM-DD"
            host = "laptop"
            purpose = "stage"
            [[internal_adapter]]
            path = "scripts/launch.sh"
            owner = "control.launch"
            [[retired_entrypoint]]
            path = "scripts/old.sh"
            replacement = "scripts/stage.sh"
            reason = "old"
            [[historical_script]]
            path = "scripts/study-26aug04.sh"
            reason = "dated"
        """))
        (root / "scripts" / "opsctl.sh").write_text("#!/bin/sh\nexit 0\n")
        (root / "scripts" / "opsctl.sh").chmod(0o755)
        return root

    def test_repository_surface_is_valid(self) -> None:
        self.run_checker(REPO, ok=True)

    def test_unclassified_trade_capable_script_is_refused(self) -> None:
        root = self.fixture()
        path = root / "scripts" / "surprise.sh"
        path.write_text("#!/bin/sh\nexec scripts/launch-event.sh \"$@\"\n")
        path.chmod(0o755)
        result = self.run_checker(root, ok=False)
        self.assertIn("outside the operations surface", result.stderr)

    def test_unclassified_dated_script_is_refused(self) -> None:
        root = self.fixture()
        path = root / "scripts" / "patch-26aug12.sh"
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        result = self.run_checker(root, ok=False)
        self.assertIn("not classified historical", result.stderr)

    def test_retired_entrypoint_must_be_inert(self) -> None:
        root = self.fixture()
        path = root / "scripts" / "old.sh"
        path.write_text("#!/bin/sh\n# EARSHOT_RETIRED_ENTRYPOINT\nexec scripts/launch-event.sh\n")
        result = self.run_checker(root, ok=False)
        self.assertIn("still contains an executable live path", result.stderr)

    def test_duplicate_classification_is_refused(self) -> None:
        root = self.fixture()
        manifest = root / "architecture" / "operations.toml"
        manifest.write_text(manifest.read_text() + textwrap.dedent("""
            [[historical_script]]
            path = "scripts/launch.sh"
            reason = "collision"
        """))
        result = self.run_checker(root, ok=False)
        self.assertIn("classified twice", result.stderr)

    def test_public_facade_dry_run_is_one_generic_chain(self) -> None:
        result = subprocess.run(
            [
                "bash",
                str(REPO / "scripts" / "opsctl.sh"),
                "prepare-day",
                "--date",
                "2026-08-14",
                "--dry-run",
            ],
            cwd=REPO,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "DRY-RUN prepare-day 2026-08-14",
                "  scripts/stage-nightly.sh --date 2026-08-14",
                "  python3 scripts/access-plan.py check-day --date 2026-08-14",
                "  scripts/study-nightly.sh --date 2026-08-14 --dry-run",
                "  scripts/preflight-nightly.sh --date 2026-08-14 --no-mail",
                "  scripts/schedule-nightly.sh --date 2026-08-14 --dry-run",
            ],
        )

    def test_public_audit_day_rejects_non_date_arguments(self) -> None:
        result = subprocess.run(
            ["bash", str(REPO / "scripts" / "opsctl.sh"), "audit-day", "CAVA"],
            cwd=REPO,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("unknown argument", result.stderr)

    def test_retired_repository_entrypoints_refuse_without_side_effects(self) -> None:
        for name in ("arm-when-url.sh", "ms-morning.sh", "run-arm.sh", "wing-autorun.sh"):
            with self.subTest(name=name):
                result = subprocess.run(
                    ["bash", str(REPO / "scripts" / name)],
                    cwd=REPO,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
                self.assertIn("is retired", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
