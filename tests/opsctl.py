#!/usr/bin/env python3
"""Offline facade tests: generic sequencing, stop-on-failure, and delegation."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
OPSCTL = REPO / "scripts" / "opsctl.sh"
DESCOPE_TICKER = "KXEARNINGSMENTIONDESC-26AUG14"
DESCOPE_EVENT = f"{DESCOPE_TICKER}-WEBCAST"
DESCOPE_DATE = "2026-08-14"
FINGERPRINT = "sha256:" + "a" * 64


class OpsctlTest(unittest.TestCase):
    def fixture(self, fail_stage: str = "") -> tuple[Path, Path, dict[str, str]]:
        root = Path(tempfile.mkdtemp(prefix="opsctl-test."))
        self.addCleanup(shutil.rmtree, root)
        scripts = root / "scripts"
        scripts.mkdir()
        shutil.copy2(OPSCTL, scripts / "opsctl.sh")
        log = root / "calls"
        for name in (
            "discover-events.py",
            "stage-nightly.sh",
            "access-plan.py",
            "study-nightly.sh",
            "preflight-nightly.sh",
            "schedule-nightly.sh",
            "event-status.sh",
            "pull-box-state.sh",
            "run-audit.py",
            "descope-event.sh",
            "event-prep-audit.py",
            "support-nightly.py",
        ):
            path = scripts / name
            if name.endswith(".py"):
                extra = ""
                if name == "discover-events.py":
                    extra = (
                        "if '--json' in sys.argv:\n"
                        "    pathlib.Path(sys.argv[sys.argv.index('--json') + 1]).write_text('{\\\"events\\\": []}')\n"
                    )
                path.write_text(
                    "#!/usr/bin/env python3\n"
                    "import os, pathlib, sys\n"
                    "with pathlib.Path(os.environ['OPSCTL_TEST_LOG']).open('a') as f:\n"
                    "    f.write(pathlib.Path(sys.argv[0]).name + '\\t' + ' '.join(sys.argv[1:]) + '\\n')\n"
                    + extra
                    + ("raise SystemExit(23)\n" if name == fail_stage else "")
                )
            else:
                path.write_text(
                    "#!/usr/bin/env bash\n"
                    'printf "%s\\t%s\\n" "$(basename "$0")" "$*" >> "$OPSCTL_TEST_LOG"\n'
                    f'[[ "$(basename "$0")" == "{fail_stage}" ]] && exit 23\n'
                    "exit 0\n"
                )
            path.chmod(0o755)
        env = {**os.environ, "EARSHOT_REPO": str(root), "OPSCTL_TEST_LOG": str(log)}
        return root, log, env

    def run_ops(self, root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(root / "scripts" / "opsctl.sh"), *args],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
        )

    def study_fixture(self) -> tuple[Path, dict[str, str]]:
        root = Path(tempfile.mkdtemp(prefix="study-runplan-test."))
        self.addCleanup(shutil.rmtree, root)
        (root / "scripts").mkdir()
        (root / "configs").mkdir()
        (root / "target" / "release").mkdir(parents=True)
        for name in (
            "study-nightly.sh",
            "eventctl.py",
            "keywords-extract.py",
            "variant-widen.py",
        ):
            shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
        binary = root / "target" / "release" / "earshot"
        binary.write_text("#!/usr/bin/env bash\nexit 0\n")
        binary.chmod(0o755)
        notify = root / "scripts" / "notify.sh"
        notify.write_text(
            "#!/usr/bin/env bash\n"
            'printf "%s\\n" "$*" >> "$STUDY_NOTIFY_LOG"\n'
            "exit 0\n"
        )
        notify.chmod(0o755)
        alert = root / "scripts" / "alert-stub.sh"
        alert.write_text(
            "#!/usr/bin/env bash\n"
            'printf "%s\\n" "$*" >> "$STUDY_ALERT_LOG"\n'
            "exit 98\n"
        )
        alert.chmod(0o755)
        for name in ("journal.sh", "preflight-event.sh"):
            stub = root / "scripts" / name
            stub.write_text(
                "#!/usr/bin/env bash\n"
                f'printf "%s\\n" "$*" >> "$STUDY_{name.split(".")[0].upper().replace("-", "_")}_LOG"\n'
                "exit 99\n"
            )
            stub.chmod(0o755)
        env = {
            **os.environ,
            "EARSHOT_REPO": str(root),
            "EARSHOT_BIN": str(binary),
            "_EARSHOT_STUDY_PLATFORM": "Darwin",
            "_EARSHOT_STUDY_ALERTCTL": str(alert),
            "_EARSHOT_STUDY_NOTIFY": str(notify),
            "STUDY_ALERT_LOG": str(root / "alert-calls"),
            "STUDY_NOTIFY_LOG": str(root / "notify-calls"),
            "STUDY_JOURNAL_LOG": str(root / "journal-calls"),
            "STUDY_PREFLIGHT_EVENT_LOG": str(root / "preflight-calls"),
        }
        return root, env

    def add_capture_plan(
        self,
        root: Path,
        env: dict[str, str],
        filename: str,
        event_id: str,
        starts_at: str,
        *,
        compile_plan: bool = True,
        keyword: tuple[str, str] | None = None,
    ) -> Path:
        config = root / "configs" / filename
        keyword_block = ""
        if keyword is not None:
            ticker, phrase = keyword
            keyword_block = f'''\n[[keywords]]
market_ticker = "{ticker}"
phrase = "{phrase}"
variants = ["{phrase}"]
'''
        config.write_text(
            f'''[event]
id = "{event_id}"
starts_at = "{starts_at}"

[[sources]]
kind = "pipe"
cmd = ["ffmpeg", "-i", "$HOME/source.url"]
''' + keyword_block
        )
        if compile_plan:
            identity_args: list[str] = []
            if keyword is not None:
                identity_args = [
                    "--host-id",
                    "study-fixture-host",
                    "--account-fingerprint",
                    FINGERPRINT,
                ]
            result = subprocess.run(
                [
                    "python3",
                    str(root / "scripts" / "eventctl.py"),
                    "init",
                    "--config",
                    f"configs/{filename}",
                    *identity_args,
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return config

    def run_study(
        self, root: Path, env: dict[str, str], date: str, *, dry_run: bool = True
    ) -> subprocess.CompletedProcess[str]:
        argv = [
            "bash",
            str(root / "scripts" / "study-nightly.sh"),
            "--date",
            date,
        ]
        if dry_run:
            argv.append("--dry-run")
        return subprocess.run(
            argv,
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
        )

    def typed_descope_fixture(self) -> tuple[Path, Path, dict[str, str], Path]:
        root = Path(tempfile.mkdtemp(prefix="opsctl-descope-test."))
        self.addCleanup(shutil.rmtree, root)
        (root / "scripts").mkdir()
        (root / "configs").mkdir()
        for name in (
            "opsctl.sh",
            "descope-event.sh",
            "eventctl.py",
            "schedule-nightly.sh",
        ):
            shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
        for name in ("journal.sh", "notify.sh"):
            stub = root / "scripts" / name
            stub.write_text("#!/usr/bin/env bash\nexit 0\n")
            stub.chmod(0o755)

        config = root / "configs" / "event.toml"
        config.write_text(
            f'''[event]
id = "{DESCOPE_EVENT}"
starts_at = "{DESCOPE_DATE}T22:00:00Z"

[kalshi]
env = "demo"

[[sources]]
kind = "pipe"
cmd = ["ffmpeg", "-i", "$HOME/descope-stream.url"]

[[keywords]]
market_ticker = "{DESCOPE_TICKER}-FOO"
phrase = "foo"
'''
        )
        event_env = root / "configs" / "event.env"
        event_env.write_text(
            'CONFIG_PATH="configs/event.toml"\n'
            f'EVENT_DATE="{DESCOPE_DATE}"\n'
        )
        env = {**os.environ, "EARSHOT_REPO": str(root)}
        result = subprocess.run(
            [
                "python3",
                str(root / "scripts" / "eventctl.py"),
                "init",
                "--config",
                "configs/event.toml",
                "--event-env",
                "configs/event.env",
                "--host-id",
                "fixture-host",
                "--account-fingerprint",
                FINGERPRINT,
            ],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        runs = list((root / "var" / "runs" / DESCOPE_EVENT).iterdir())
        self.assertEqual(len(runs), 1)
        run = runs[0]
        for transition in (
            "prepared",
            "access_acquired",
            "study_completed",
            "preflight_passed",
        ):
            self.record_transition(root, env, run, transition)
        return root, run, env, event_env

    def record_transition(
        self,
        root: Path,
        env: dict[str, str],
        run: Path,
        transition: str,
    ) -> None:
        result = subprocess.run(
            [
                "python3",
                str(root / "scripts" / "eventctl.py"),
                "record",
                "--run-dir",
                str(run),
                "--transition",
                transition,
            ],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def reached(self, run: Path) -> set[str]:
        return {
            row["transition"]
            for row in (
                __import__("json").loads(line)
                for line in (run / "control" / "receipts.jsonl").read_text().splitlines()
            )
            if row.get("kind") == "transition" and row.get("outcome") == "ok"
        }

    def test_prepare_day_runs_exact_chain(self) -> None:
        root, log, env = self.fixture()
        result = self.run_ops(root, env, "prepare-day", "--date", "2026-08-14")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            log.read_text().splitlines(),
            [
                "discover-events.py\t--json " + next(
                    line.split("\t", 1)[1].split()[1]
                    for line in log.read_text().splitlines()
                    if line.startswith("discover-events.py\t")
                ) + " --days 21",
                "stage-nightly.sh\t--date 2026-08-14",
                "access-plan.py\tcheck-day --date 2026-08-14",
                "study-nightly.sh\t--date 2026-08-14",
                "preflight-nightly.sh\t--date 2026-08-14",
                "schedule-nightly.sh\t--date 2026-08-14",
                "support-nightly.py\t--date 2026-08-14",
                "event-prep-audit.py\t--date 2026-08-14",
            ],
        )

    def test_prepare_day_continues_siblings_but_returns_failure(self) -> None:
        root, log, env = self.fixture("preflight-nightly.sh")
        result = self.run_ops(root, env, "prepare-day", "--date", "2026-08-14")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(
            [line.split("\t", 1)[0] for line in log.read_text().splitlines()],
            [
                "discover-events.py",
                "stage-nightly.sh",
                "access-plan.py",
                "study-nightly.sh",
                "preflight-nightly.sh",
                "schedule-nightly.sh",
                "support-nightly.py",
                "event-prep-audit.py",
            ],
        )
        self.assertIn("preflight=23", result.stdout)
        self.assertIn("discovery=0", result.stdout)
        self.assertIn("failed runs were not marked scheduled", result.stderr)

    def test_prepare_day_no_mail_is_forwarded_only_to_preflight(self) -> None:
        root, log, env = self.fixture()
        result = self.run_ops(
            root, env, "prepare-day", "--date", "2026-08-14", "--no-mail"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = log.read_text().splitlines()
        self.assertEqual(calls[4], "preflight-nightly.sh\t--date 2026-08-14 --no-mail")
        self.assertNotIn("--no-mail", "".join(calls[:4] + calls[5:]))

    def test_access_delegates_one_utc_date_to_the_canonical_gate(self) -> None:
        root, log, env = self.fixture()
        result = self.run_ops(root, env, "access", "--date", "2026-08-14")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            log.read_text().splitlines(),
            ["access-plan.py\tcheck-day --date 2026-08-14"],
        )

    def test_support_delegates_one_utc_date_to_the_typed_installer(self) -> None:
        root, log, env = self.fixture()
        result = self.run_ops(root, env, "support", "--date", "2026-08-14", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            log.read_text().splitlines(),
            ["support-nightly.py\t--date 2026-08-14 --dry-run"],
        )

    def test_bind_access_delegates_exact_run_bound_arguments(self) -> None:
        root, log, env = self.fixture()
        result = self.run_ops(
            root,
            env,
            "bind-access",
            "--run-dir",
            "var/runs/E/R",
            "--access-mode",
            "public",
            "--resolver-kind",
            "static-public",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            log.read_text().splitlines(),
            [
                "access-plan.py\tbind --run-dir var/runs/E/R "
                "--access-mode public --resolver-kind static-public"
            ],
        )

    def test_invalid_date_runs_nothing(self) -> None:
        root, log, env = self.fixture()
        result = self.run_ops(root, env, "prepare-day", "--date", "2026-02-31")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(log.exists())

    def test_study_selects_schema3_runplans_by_utc_start_not_filename(self) -> None:
        root, env = self.study_fixture()
        selected = self.add_capture_plan(
            root,
            env,
            "webcast-nebius-26aug06-arm-wc.toml",
            "KXEARNINGSMENTIONNBIS-26AUG14-WEBCAST",
            "2026-08-14T12:00:00Z",
        )
        decoy = self.add_capture_plan(
            root,
            env,
            "webcast-decoy-26aug14-arm-wc.toml",
            "KXEARNINGSMENTIONDECOY-26AUG15-WEBCAST",
            "2026-08-15T12:00:00Z",
        )

        result = self.run_study(root, env, "2026-08-14")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"── {selected.stem}: no keywords", result.stdout)
        self.assertNotIn(decoy.stem, result.stdout)

    def test_date_status_does_not_duplicate_typed_lane_also_in_slate(self) -> None:
        root, env = self.study_fixture()
        status = root / "scripts" / "event-status.sh"
        shutil.copy2(REPO / "scripts" / "event-status.sh", status)
        status.chmod(0o755)
        event_ticker = "KXEARNINGSMENTIONUNION-26AUG14"
        event_id = event_ticker + "-WEBCAST"
        self.add_capture_plan(
            root,
            env,
            "union.toml",
            event_id,
            "2026-08-14T12:00:00Z",
        )
        (root / "slate").mkdir()
        (root / "slate" / "latest.json").write_text(
            __import__("json").dumps({
                "events": [{
                    "event_ticker": event_ticker,
                    "call_date": "2026-08-14",
                }]
            })
        )
        result = subprocess.run(
            ["bash", str(status), "2026-08-14"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count(f"── {event_id}\n"), 1)

    def test_study_refuses_partial_date_when_staged_config_has_no_runplan(self) -> None:
        root, env = self.study_fixture()
        selected = self.add_capture_plan(
            root,
            env,
            "arbitrary-name.toml",
            "KXEARNINGSMENTIONREADY-26AUG14-WEBCAST",
            "2026-08-14T12:00:00Z",
        )
        missing = self.add_capture_plan(
            root,
            env,
            "another-arbitrary-name.toml",
            "KXEARNINGSMENTIONMISSING-26AUG14-WEBCAST",
            "2026-08-14T13:00:00Z",
            compile_plan=False,
        )

        result = self.run_study(root, env, "2026-08-14")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("expected staged event", result.stderr)
        self.assertIn(missing.name, result.stderr)
        self.assertIn("has no immutable schema-3 RunPlan", result.stderr)
        self.assertNotIn(f"── {selected.stem}: no keywords", result.stdout)

    def test_study_widening_never_mutates_pinned_config_or_runs_preflight(self) -> None:
        root, env = self.study_fixture()
        event = "KXEARNINGSMENTIONIMMUTABLE-26AUG14-WEBCAST"
        ticker = "KXEARNINGSMENTIONIMMUTABLE-26AUG14-TAIL"
        config = self.add_capture_plan(
            root,
            env,
            "immutable-runplan.toml",
            event,
            "2026-08-14T12:00:00Z",
            keyword=(ticker, "tailwind"),
        )
        digest = subprocess.run(
            [
                "python3",
                str(root / "scripts" / "keywords-extract.py"),
                "configs/immutable-runplan.toml",
                "--sha256",
            ],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        readiness_dir = root / "configs" / ".readiness"
        readiness_dir.mkdir()
        (readiness_dir / "immutable-runplan.json").write_text(
            __import__("json").dumps(
                {
                    "date": date.today().isoformat(),
                    "keywords_sha256": digest,
                    "keywords": [{"ticker": ticker, "confirmed": 0}],
                }
            )
        )
        before = config.read_bytes()

        preview = self.run_study(root, env, "2026-08-14")

        self.assertEqual(preview.returncode, 1, preview.stdout + preview.stderr)
        self.assertEqual(config.read_bytes(), before)
        self.assertIn("WOULD REQUIRE CRITICAL ALERT", preview.stdout)
        for path in (
            root / "alert-calls",
            root / "notify-calls",
            root / "journal-calls",
            root / "preflight-calls",
        ):
            self.assertFalse(path.exists(), f"dry-run wrote {path}")

        result = self.run_study(root, env, "2026-08-14", dry_run=False)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse((root / "configs" / ".acks").exists())
        self.assertFalse(root.joinpath("preflight-calls").exists())
        self.assertFalse(root.joinpath("notify-calls").exists())
        alerts = (root / "alert-calls").read_text().splitlines()
        self.assertEqual(len(alerts), 1)
        self.assertIn("--code study_failed", alerts[0])
        self.assertIn("--dedupe-key nightly-study", alerts[0])
        self.assertIn("ALERT DELIVERY FAILED", result.stderr)

    def test_audit_delegates_exact_run_dir(self) -> None:
        root, log, env = self.fixture()
        result = self.run_ops(root, env, "audit", "--run-dir", "var/runs/E/R")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(log.read_text(), "run-audit.py\t--run-dir var/runs/E/R\n")

    def test_real_schedule_refuses_empty_date(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="schedule-empty-test."))
        self.addCleanup(shutil.rmtree, root)
        (root / "scripts").mkdir()
        (root / "var" / "runs").mkdir(parents=True)
        for name in ("schedule-nightly.sh", "eventctl.py"):
            shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
        notify = root / "scripts" / "notify.sh"
        notify.write_text("#!/bin/sh\nexit 0\n")
        notify.chmod(0o755)
        result = subprocess.run(
            ["bash", str(root / "scripts" / "schedule-nightly.sh"), "--date", "2026-08-14"],
            cwd=root,
            env={**os.environ, "EARSHOT_REPO": str(root)},
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("no schema-3 RunPlans", result.stderr)

    def test_typed_descope_closes_run_and_scheduler_cannot_install_it(self) -> None:
        root, run, env, _event_env = self.typed_descope_fixture()
        result = self.run_ops(
            root,
            env,
            "descope",
            DESCOPE_TICKER,
            "venue unavailable",
            "--fallbacks",
            "phone; webcast",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("descope", self.reached(run))

        projection = __import__("json").loads(
            (root / "state" / DESCOPE_TICKER / "DESCOPED.json").read_text()
        )
        self.assertEqual(projection["authority"], "typed-run-ledgers")
        self.assertEqual(
            [(row["event_id"], row["run_id"]) for row in projection["typed_runs"]],
            [(DESCOPE_EVENT, run.name)],
        )

        install_log = root / "install-calls"
        installer = root / "scripts" / "install-stub.sh"
        installer.write_text(
            "#!/usr/bin/env bash\n"
            'printf "%s\\n" "$*" >> "$EARSHOT_INSTALL_LOG"\n'
        )
        installer.chmod(0o755)
        schedule = subprocess.run(
            [
                "bash",
                str(root / "scripts" / "schedule-nightly.sh"),
                "--date",
                DESCOPE_DATE,
            ],
            cwd=root,
            env={
                **env,
                "EARSHOT_LOCAL_EXECUTION_HOST": "fixture-host",
                "EARSHOT_INSTALL_LOG": str(install_log),
                "_EARSHOT_SCHEDULE_INSTALL_T0": str(installer),
                "_EARSHOT_SCHEDULE_BOX_INSTALL": str(installer),
                "_EARSHOT_SCHEDULE_NOTIFY": str(root / "scripts" / "notify.sh"),
            },
            text=True,
            capture_output=True,
        )
        self.assertEqual(schedule.returncode, 0, schedule.stdout + schedule.stderr)
        self.assertIn(f"SKIP {DESCOPE_EVENT}/{run.name}: terminal", schedule.stdout)
        self.assertFalse(install_log.exists())

    def test_typed_descope_refuses_ambiguous_active_attempts_without_mutation(self) -> None:
        root, first, env, _event_env = self.typed_descope_fixture()
        second_init = subprocess.run(
            [
                "python3",
                str(root / "scripts" / "eventctl.py"),
                "init",
                "--config",
                "configs/event.toml",
                "--event-env",
                "configs/event.env",
                "--host-id",
                "fixture-host",
                "--account-fingerprint",
                FINGERPRINT,
                "--run-id",
                "second-attempt",
            ],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(
            second_init.returncode, 0, second_init.stdout + second_init.stderr
        )
        second = root / "var" / "runs" / DESCOPE_EVENT / "second-attempt"
        result = self.run_ops(
            root, env, "descope", DESCOPE_TICKER, "ambiguous attempts"
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("multiple active attempts", result.stderr)
        self.assertNotIn("descope", self.reached(first))
        self.assertNotIn("descope", self.reached(second))
        self.assertFalse(
            (root / "state" / DESCOPE_TICKER / "DESCOPED.json").exists()
        )

    def test_lane_event_id_cannot_mask_related_typed_run_as_legacy(self) -> None:
        root, run, env, _event_env = self.typed_descope_fixture()
        result = self.run_ops(root, env, "descope", DESCOPE_EVENT, "wrong key")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("outside the exact market-ticker selection", result.stderr)
        self.assertNotIn("descope", self.reached(run))
        self.assertFalse((root / "state" / DESCOPE_EVENT / "DESCOPED.json").exists())

    def test_genuinely_legacy_event_keeps_legacy_projection(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="opsctl-legacy-descope-test."))
        self.addCleanup(shutil.rmtree, root)
        (root / "scripts").mkdir()
        for name in ("opsctl.sh", "descope-event.sh"):
            shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
        for name in ("journal.sh", "notify.sh"):
            stub = root / "scripts" / name
            stub.write_text("#!/usr/bin/env bash\nexit 0\n")
            stub.chmod(0o755)
        env = {**os.environ, "EARSHOT_REPO": str(root)}
        ticker = "KXEARNINGSMENTIONLEGACY-26AUG14"
        result = self.run_ops(root, env, "descope", ticker, "historical event")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        projection = __import__("json").loads(
            (root / "state" / ticker / "DESCOPED.json").read_text()
        )
        self.assertEqual(projection["authority"], "legacy-only")
        self.assertEqual(projection["typed_runs"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
