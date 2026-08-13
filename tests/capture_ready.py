#!/usr/bin/env python3
"""Offline contract tests for capture-ready and durable PCM proof."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EVENTCTL = ROOT / "scripts" / "eventctl.py"
ACCESS = ROOT / "scripts" / "access-plan.py"
CAPTURE = ROOT / "scripts" / "capture-ready.py"
PCM_GATE = ROOT / "scripts" / "pcm-gate.sh"
HOST = "earshot-capture-host"
FP = "sha256:" + "a" * 64


class CaptureReadyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="capture-ready.")
        self.repo = Path(self.temp.name).resolve() / "repo"
        self.home = Path(self.temp.name).resolve() / "home"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "configs").mkdir()
        self.home.mkdir()
        for source in (EVENTCTL, ACCESS, CAPTURE, PCM_GATE):
            shutil.copy2(source, self.repo / "scripts" / source.name)
        self.env = {
            **os.environ,
            "EARSHOT_REPO": str(self.repo),
            "EARSHOT_EXECUTION_HOST": HOST,
            "HOME": str(self.home),
        }
        self.lsof = Path(self.temp.name) / "lsof-stub"
        self.lsof.write_text(
            "#!/bin/sh\n"
            "if [ \"${CAPTURE_TEST_OCCUPIED:-0}\" = 1 ]; then echo 4242; exit 0; fi\n"
            "exit 1\n"
        )
        self.lsof.chmod(0o755)
        self.env["EARSHOT_LSOF"] = str(self.lsof)
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, path: Path, *args: str, env=None) -> subprocess.CompletedProcess:
        command = [str(path), *map(str, args)]
        if path.suffix == ".py":
            command.insert(0, sys.executable)
        return subprocess.run(
            command,
            cwd=self.repo,
            env=env or self.env,
            text=True,
            capture_output=True,
        )

    def eventctl(self, *args: str) -> subprocess.CompletedProcess:
        proc = self.cli(self.repo / "scripts" / "eventctl.py", *args)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        return proc

    @staticmethod
    def free_port() -> int:
        return 42000

    def make_run(self, lane: str) -> tuple[Path, dict, Path, int | None]:
        self.counter += 1
        suffix = f"{lane.upper()}{self.counter}"
        event = f"KXEARNINGSMENTIONCAPTURE-TEST-{suffix}"
        start = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=8)
        name = f"{lane}{self.counter}"
        config = self.repo / "configs" / f"{name}.toml"
        event_env = self.repo / "configs" / f"{name}.env"
        port = self.free_port() + self.counter if lane == "phone" else None
        if lane == "phone":
            source = f'kind = "twilio_stream"\nbind = "127.0.0.1:{port}"'
        else:
            source = (
                'kind = "pipe"\n'
                f'cmd = ["ffmpeg", "-i", "$HOME/{name}-stream.url"]'
            )
        config.write_text(
            f'[event]\nid = "{event}"\nstarts_at = "{start:%Y-%m-%dT%H:%M:%SZ}"\n'
            f"[[sources]]\n{source}\n"
            f'[[keywords]]\nmarket_ticker = "{event}-FOO"\nphrase = "foo"\n'
        )
        event_env.write_text(
            f'CONFIG_PATH="configs/{name}.toml"\nEVENT_DATE="{start:%Y-%m-%d}"\n'
        )
        initialized = self.eventctl(
            "init",
            "--config",
            str(config),
            "--event-env",
            str(event_env),
            "--host-id",
            HOST,
            "--account-fingerprint",
            FP,
        )
        run = Path(initialized.stdout.split(": ", 1)[1].strip())
        transitions = [
            "prepared",
            "admission_resolved" if lane == "phone" else "access_acquired",
            "study_completed",
            "preflight_passed",
            "scheduled",
        ]
        for transition in transitions:
            self.eventctl(
                "record", "--run-dir", str(run), "--transition", transition
            )
        plan = json.loads((run / "control" / "run-plan.json").read_text())
        if lane == "webcast":
            created = self.cli(
                self.repo / "scripts" / "access-plan.py",
                "bind",
                "--run-dir",
                run,
                "--access-mode",
                "public",
                "--resolver-kind",
                "external-command",
                "--resolver-argv-json",
                '["scripts/test-resolver.sh"]',
            )
            self.assertEqual(created.returncode, 0, created.stderr)
        return run, plan, event_env, port

    def pin(self, plan: dict) -> tuple[Path, Path]:
        state = self.repo / "state" / plan["event_id"]
        state.mkdir(parents=True)
        binary = state / "earshot-pinned"
        binary.write_bytes(b"fixture pinned binary\n")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        return state, binary

    def phone_args(self, run, plan, event_env, port, state, binary):
        return [
            "phone",
            "--run-dir",
            run,
            "--event-env",
            event_env,
            "--bind-port",
            str(port),
            "--stream-url",
            f"wss://audio.example.test/e/{port}",
            "--state-dir",
            state,
            "--binary",
            binary,
        ]

    def transitions(self, run: Path) -> list[str]:
        return [
            item.get("transition")
            for item in map(
                json.loads,
                (run / "control" / "receipts.jsonl").read_text().splitlines(),
            )
            if item.get("kind") == "transition"
        ]

    def test_phone_proof_is_pre_listener_and_idempotent(self):
        run, plan, event_env, port = self.make_run("phone")
        state, binary = self.pin(plan)
        args = self.phone_args(run, plan, event_env, port, state, binary)
        proc = self.cli(self.repo / "scripts" / "capture-ready.py", *args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proof = json.loads((run / "control" / "capture-ready.json").read_text())
        evidence = proof["evidence"]
        self.assertFalse(evidence["listener_started"])
        self.assertFalse(evidence["current_stream_admitted"])
        self.assertTrue(evidence["bind"]["free"])
        self.assertEqual(evidence["stream_route"]["path"], f"/e/{port}")
        self.assertEqual(self.transitions(run).count("capture_ready"), 1)
        retry = self.cli(self.repo / "scripts" / "capture-ready.py", *args)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(self.transitions(run).count("capture_ready"), 1)

    def test_phone_refusals_do_not_claim_capture(self):
        cases = ("wrong-url", "wrong-host", "occupied-bind", "wrong-binary")
        for case in cases:
            with self.subTest(case=case):
                run, plan, event_env, port = self.make_run("phone")
                state, binary = self.pin(plan)
                args = self.phone_args(run, plan, event_env, port, state, binary)
                env = self.env
                if case == "wrong-url":
                    args[args.index("--stream-url") + 1] = "wss://audio.example.test/e/9"
                elif case == "wrong-host":
                    env = {**self.env, "EARSHOT_EXECUTION_HOST": "wrong-host"}
                elif case == "occupied-bind":
                    env = {**self.env, "CAPTURE_TEST_OCCUPIED": "1"}
                else:
                    other = state / "other-binary"
                    other.write_bytes(b"other")
                    other.chmod(0o755)
                    args[args.index("--binary") + 1] = other
                before = (run / "control" / "receipts.jsonl").read_bytes()
                proc = self.cli(
                    self.repo / "scripts" / "capture-ready.py", *args, env=env
                )
                self.assertEqual(proc.returncode, 1)
                self.assertFalse((run / "control" / "capture-ready.json").exists())
                self.assertEqual((run / "control" / "receipts.jsonl").read_bytes(), before)

    def test_phone_crash_gap_recovers_without_duplicate(self):
        run, plan, event_env, port = self.make_run("phone")
        state, binary = self.pin(plan)
        args = self.phone_args(run, plan, event_env, port, state, binary)
        failed = self.cli(
            self.repo / "scripts" / "capture-ready.py",
            *args,
            env={**self.env, "_EARSHOT_CAPTURE_READY_FAIL_AFTER_PUBLISH": "1"},
        )
        self.assertEqual(failed.returncode, 1)
        self.assertTrue((run / "control" / "capture-ready.json").is_file())
        self.assertNotIn("capture_ready", self.transitions(run))
        recovered = self.cli(self.repo / "scripts" / "capture-ready.py", *args)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(self.transitions(run).count("capture_ready"), 1)

    def webcast_url(self, plan: dict) -> tuple[str, Path]:
        access = json.loads(
            (
                self.repo
                / "var"
                / "runs"
                / plan["event_id"]
                / plan["run_id"]
                / "control"
                / "access-plan.json"
            ).read_text()
        )
        portable = access["endpoint"]["url_files"][0]
        path = self.home / portable[len("$HOME/") :]
        path.write_text("https://media.example.test/live/master.m3u8\n")
        return portable, path

    def curl_stub(self, name: str, body: str) -> Path:
        """A stub `curl` that prints one fixed manifest and ignores its argv.

        pcm-gate now fetches the playlist itself to refuse a finished VOD, so
        these tests would otherwise reach the network for their verdict.
        """
        stub = Path(self.temp.name) / name
        stub.write_text("#!/bin/sh\ncat <<'M3U8'\n" + body + "\nM3U8\n")
        stub.chmod(0o755)
        return stub

    LIVE_MANIFEST = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:4",
            "#EXT-X-MEDIA-SEQUENCE:807",
            "#EXTINF:4.017,",
            "seg807.ts",
        ]
    )
    # Identical but finished. Note every segment still carries #EXTINF — which
    # is exactly why grepping for EXTINF never proved anything.
    VOD_MANIFEST = LIVE_MANIFEST + "\n#EXT-X-ENDLIST"

    def test_pcm_gate_publishes_fresh_webcast_proof(self):
        run, plan, _event_env, _port = self.make_run("webcast")
        _portable, url_file = self.webcast_url(plan)
        ffmpeg = Path(self.temp.name) / "ffmpeg-stub"
        ffmpeg.write_text("#!/bin/sh\nhead -c 192000 /dev/zero\n")
        ffmpeg.chmod(0o755)
        pdt = Path(self.temp.name) / "pdt-stub.py"
        pdt.write_text(
            'print(\'{"media_sequence": 101}\')\n'
            'print(\'{"media_sequence": 102}\')\n'
        )
        proof = run / "control" / "pcm-proof.json"
        gate = self.cli(
            self.repo / "scripts" / "pcm-gate.sh",
            "--kind",
            "hls",
            "--url-file",
            url_file,
            "--run-dir",
            run,
            "--proof-json",
            proof,
            env={
                **self.env,
                "EARSHOT_FFMPEG": str(ffmpeg),
                "EARSHOT_PDT_LOG": str(pdt),
                "EARSHOT_CURL": str(self.curl_stub("curl-live", self.LIVE_MANIFEST)),
                "PCM_GATE_SEQ_SECS": "1",
            },
        )
        self.assertEqual(gate.returncode, 0, gate.stderr + gate.stdout)
        pcm = json.loads(proof.read_text())
        self.assertGreaterEqual(
            pcm["decoded_pcm"]["decoded_bytes"], pcm["decoded_pcm"]["floor_bytes"]
        )
        self.assertEqual(pcm["continuity"]["status"], "advanced")
        capture = self.cli(
            self.repo / "scripts" / "capture-ready.py",
            "webcast",
            "--run-dir",
            run,
            "--pcm-proof",
            proof,
        )
        self.assertEqual(capture.returncode, 0, capture.stderr)
        self.assertEqual(self.transitions(run).count("capture_ready"), 1)

    def test_hls_without_continuity_cannot_publish_proof(self):
        run, plan, _event_env, _port = self.make_run("webcast")
        _portable, url_file = self.webcast_url(plan)
        ffmpeg = Path(self.temp.name) / "ffmpeg-no-continuity"
        ffmpeg.write_text("#!/bin/sh\nhead -c 192000 /dev/zero\n")
        ffmpeg.chmod(0o755)
        pdt = Path(self.temp.name) / "pdt-empty.py"
        pdt.write_text("pass\n")
        proof = run / "control" / "pcm-proof.json"
        gate = self.cli(
            self.repo / "scripts" / "pcm-gate.sh",
            "--kind",
            "hls",
            "--url-file",
            url_file,
            "--run-dir",
            run,
            "--proof-json",
            proof,
            env={
                **self.env,
                "EARSHOT_FFMPEG": str(ffmpeg),
                "EARSHOT_PDT_LOG": str(pdt),
                "EARSHOT_CURL": str(self.curl_stub("curl-live2", self.LIVE_MANIFEST)),
                "PCM_GATE_SEQ_SECS": "1",
            },
        )
        self.assertEqual(gate.returncode, 1)
        self.assertFalse(proof.exists())
        self.assertNotIn("capture_ready", self.transitions(run))

    def test_finished_vod_is_refused_before_any_decode(self):
        """C1: #EXT-X-ENDLIST means pre-recorded, and pre-recorded can trade.

        The stubs here are deliberately GENEROUS — ffmpeg hands over a full
        192000 bytes and pdt-log reports an advancing sequence — so the only
        thing that can refuse this run is the ENDLIST check itself. Before
        2026-08-12 this exact fixture produced capture_ready.
        """
        run, plan, _event_env, _port = self.make_run("webcast")
        _portable, url_file = self.webcast_url(plan)
        ffmpeg = Path(self.temp.name) / "ffmpeg-generous"
        ffmpeg.write_text("#!/bin/sh\nhead -c 192000 /dev/zero\n")
        ffmpeg.chmod(0o755)
        pdt = Path(self.temp.name) / "pdt-advancing.py"
        pdt.write_text(
            'print(\'{"media_sequence": 101}\')\n'
            'print(\'{"media_sequence": 102}\')\n'
        )
        proof = run / "control" / "pcm-proof.json"
        gate = self.cli(
            self.repo / "scripts" / "pcm-gate.sh",
            "--kind",
            "hls",
            "--url-file",
            url_file,
            "--run-dir",
            run,
            "--proof-json",
            proof,
            env={
                **self.env,
                "EARSHOT_FFMPEG": str(ffmpeg),
                "EARSHOT_PDT_LOG": str(pdt),
                "EARSHOT_CURL": str(self.curl_stub("curl-vod", self.VOD_MANIFEST)),
                "PCM_GATE_SEQ_SECS": "1",
            },
        )
        self.assertEqual(gate.returncode, 1, gate.stdout + gate.stderr)
        self.assertIn("FINISHED VOD", gate.stderr)
        self.assertFalse(proof.exists())
        self.assertNotIn("capture_ready", self.transitions(run))


if __name__ == "__main__":
    unittest.main(verbosity=2)
