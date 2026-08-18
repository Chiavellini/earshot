#!/usr/bin/env python3
"""Offline fixtures for scripts/run-audit.py."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
AUDIT = REPO / "scripts" / "run-audit.py"
EVENT = "AUDIT-TEST"
RUN = "20260812T120000Z-p3-aaaaaaaaaaaa"
MARKET = "KXTEST-YES"
FINGERPRINT = "sha256:" + "a" * 64
PROCESS = "fixture-process"
FIX_PROCESS = "fixture-fix-process"
FIX_ENDPOINT = "tls://fix.example.invalid:8228|sender=FIXTURE|target=KalshiNR"
SOH = b"\x01"
SNAPSHOT_MONO = 1_000_000_000
DETECTION_EMIT = 1_150_000_000
DECISION_TS = 1_200_000_000
EXEC_TS = 1_250_000_000
SEND_TS = 1_300_000_000
EXHAUST_MONO = 1_500_000_000

ARTIFACTS = {
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
}


def canon(value, *, ascii_only=True):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=ascii_only
    ).encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fp(path: Path):
    raw = path.read_bytes()
    return {"bytes": len(raw), "sha256": digest(raw)}


def write_line(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canon(value) + b"\n")


def raw_evidence(raw=b"{}"):
    return {
        "media_type": "application/json",
        "bytes_base64": base64.b64encode(raw).decode(),
        "bytes_len": len(raw),
        "bytes_sha256": digest(raw),
    }


def fix_frame(fields: list[tuple[str, str]]) -> bytes:
    body = b"".join(
        tag.encode() + b"=" + value.encode() + SOH for tag, value in fields
    )
    frame = b"8=FIXT.1.1" + SOH + f"9={len(body)}".encode() + SOH + body
    checksum = sum(frame) % 256
    return frame + f"10={checksum:03}".encode() + SOH


def fix_execution_frame() -> bytes:
    return fix_frame(
        [
            ("35", "8"),
            ("34", "41"),
            ("49", "KalshiNR"),
            ("56", "FIXTURE"),
            ("52", "20260812-17:00:00.000"),
            ("37", "order-1"),
            ("11", "client-1"),
            ("150", "F"),
            ("39", "2"),
            ("14", "1.00"),
            ("151", "0.00"),
            ("31", "0.97"),
            ("32", "1.00"),
            ("6", "0.97"),
        ]
    )


def dollars(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02}00"


class Fixture:
    def __init__(
        self,
        root: Path,
        *,
        order_mode: str = "fill",
        book_mode: str = "available",
        evidence_pulled: bool = True,
        transport: str = "rest",
    ):
        self.transport = transport
        if transport not in ("rest", "fix"):
            raise ValueError(transport)
        self.repo = root
        self.run_dir = root / "var" / "runs" / EVENT / RUN
        for relative in ARTIFACTS.values():
            (self.run_dir / relative).parent.mkdir(parents=True, exist_ok=True)
        self.config = root / "configs" / "audit.toml"
        self.config.parent.mkdir(parents=True)
        config = "[event]\nid = \"AUDIT-TEST\"\n"
        if transport == "fix":
            config += (
                "[kalshi]\n"
                'series = "KXTEST"\n'
                'transport = "fix"\n'
                'key_id = "FIXTURE"\n'
                "[kalshi.fix]\n"
                'host = "fix.example.invalid"\n'
                "port = 8228\n"
                'target_comp_id = "KalshiNR"\n'
                'fix_key_id = "FIXTURE"\n'
            )
        self.config.write_text(config)
        self.plan = {
            "schema": 3,
            "kind": "run-plan",
            "event_id": EVENT,
            "run_id": RUN,
            "created_utc": "2026-08-12T11:00:00Z",
            "starts_at_utc": "2026-08-12T12:00:00Z",
            "lane": "webcast",
            "execution_enabled": True,
            "kalshi_env": "prod",
            "market_event_ticker": "KXTEST",
            "execution_host_id": "fixture-box",
            "capture_only": False,
            "config_path": "configs/audit.toml",
            "config_sha256": digest(self.config.read_bytes()),
            "event_env_path": None,
            "event_env_sha256": None,
            "required_prep_receipts": [
                "prepared",
                "access_acquired",
                "study_completed",
            ],
            "artifacts": ARTIFACTS,
            "account_fingerprint": FINGERPRINT,
            "canonical_state_root": "state",
        }
        self.plan["plan_sha256"] = digest(canon(self.plan))
        (self.run_dir / "control" / "run-plan.json").write_text(
            json.dumps(self.plan, indent=2, sort_keys=True) + "\n"
        )
        self.write_lifecycle(evidence_pulled)
        self.order_events = self.make_orders(order_mode)
        self.write_orders()
        self.book_commit = self.write_book(book_mode)
        self.audio_commit = self.write_audio()
        self.fix_summary = self.write_fix_tape(order_mode) if transport == "fix" else None
        self.write_report(order_mode)

    def remote(self, relative: str) -> str:
        return f"/remote/ear/var/runs/{EVENT}/{RUN}/{relative}"

    def write_lifecycle(self, pulled: bool) -> None:
        transitions = [
            "plan_compiled",
            "prepared",
            "access_acquired",
            "study_completed",
            "preflight_passed",
            "scheduled",
            "capture_ready",
            "launched",
            "source_attached",
            "supervising",
            "draining",
            "completed",
        ]
        if pulled:
            transitions.append("evidence_pulled")
        previous = self.plan["plan_sha256"]
        rows = []
        for sequence, transition in enumerate(transitions, start=1):
            row = {
                "kind": "transition",
                "ts": f"2026-08-12T12:{sequence:02}:00Z",
                "actor": "fixture",
                "host": "fixture-box",
                "pid": 99,
                "transition": transition,
                "action": None,
                "outcome": "ok",
                "detail": transition,
                "log": None,
                "artifacts": [],
                "schema": 1,
                "event": EVENT,
                "run": RUN,
                "sequence": sequence,
                "previous_sha256": previous,
            }
            row["receipt_sha256"] = digest(canon(row))
            previous = row["receipt_sha256"]
            rows.append(canon(row))
        (self.run_dir / ARTIFACTS["receipts"]).write_bytes(
            b"\n".join(rows) + b"\n"
        )

    def intent(self):
        return {
            "client_order_id": "client-1",
            "market_ticker": MARKET,
            "side": "yes",
            "action": "buy",
            "price_cents": 97,
            "contract_count": 1,
            "time_in_force": "immediate_or_cancel",
            "tier": "fast",
            "keyword_id": 0,
            "keyword": "revenue",
            "source_id": 0,
            "audio_ts": 1_100_000_000,
            "decision_ts": DECISION_TS,
            "exec_ts": EXEC_TS,
            "book_as_of_ts": 1_100_000_000,
            "best_ask_cents": 97,
            "request_sha256": "b" * 64,
            "decision_plan_sha1": "c" * 40,
        }

    def transition(self, kind: str, receipt: dict, prior: list[dict]) -> dict:
        event = {
            "schema_version": 4,
            "journal_seq": len(prior),
            "order_seq": len(prior),
            "wall_unix_ns": 1_800_000_000_000_000_000 + len(prior),
            "monotonic_ns": EXEC_TS + len(prior) * 10_000_000,
            "process_instance_id": PROCESS,
            "run_id": RUN,
            "event_id": EVENT,
            "kalshi_env": "prod",
            "account_fingerprint": FINGERPRINT,
            "plan_sha256": self.plan["plan_sha256"],
            "client_order_id": "client-1",
            "market_ticker": MARKET,
            "previous_process_instance_id": "" if not prior else PROCESS,
            "previous_event_sha256": (
                self.plan["plan_sha256"] if not prior else prior[-1]["event_sha256"]
            ),
            "transition": {"kind": kind, "receipt": receipt},
            "event_sha256": "",
        }
        unhashed = dict(event)
        del unhashed["event_sha256"]
        event["event_sha256"] = digest(canon(unhashed, ascii_only=False))
        return event

    def make_orders(self, mode: str) -> list[dict]:
        events: list[dict] = []
        if mode == "none":
            return events
        intent = self.intent()
        events.append(self.transition("intent_evaluated", intent, events))
        if mode == "not_attempted":
            events.append(
                self.transition(
                    "not_attempted",
                    {
                        "client_order_id": "client-1",
                        "market_ticker": MARKET,
                        "refusal_ts": EXEC_TS + 10_000_000,
                        "reason": "no ask at or under cap",
                    },
                    events,
                )
            )
        elif mode == "illegal_wire":
            events.append(
                self.transition(
                    "wire_attempted",
                    {
                        "client_order_id": "client-1",
                        "market_ticker": MARKET,
                        "protocol": "rest",
                        "endpoint_id": "POST /orders",
                        "send_ts": SEND_TS,
                    },
                    events,
                )
            )
        else:
            protocol = self.transport
            endpoint = (
                FIX_ENDPOINT
                if self.transport == "fix"
                else "POST /trade-api/v2/portfolio/orders"
            )
            events.append(
                self.transition(
                    "send_requested",
                    {
                        "client_order_id": "client-1",
                        "market_ticker": MARKET,
                        "requested_ts": 1_270_000_000,
                    },
                    events,
                )
            )
            events.append(
                self.transition(
                    "invocation_prepared",
                    {
                        "client_order_id": "client-1",
                        "market_ticker": MARKET,
                        "protocol": protocol,
                        "endpoint_id": endpoint,
                        "prepared_ts": 1_280_000_000,
                        "raw": raw_evidence(b'{"order":"request"}'),
                    },
                    events,
                )
            )
            events.append(
                self.transition(
                    "wire_attempted",
                    {
                        "client_order_id": "client-1",
                        "market_ticker": MARKET,
                        "protocol": protocol,
                        "endpoint_id": endpoint,
                        "send_ts": SEND_TS,
                    },
                    events,
                )
            )
            events.append(
                self.transition(
                    "exchange_observed",
                    {
                        "client_order_id": "client-1",
                        "market_ticker": MARKET,
                        "protocol": protocol,
                        "endpoint_id": endpoint,
                        "observed_ts": 1_400_000_000,
                        "protocol_status": "201",
                        "exchange_accepted": True,
                        "order_terminal": True,
                        "echoed_client_order_id": "client-1",
                        "order_id": "order-1",
                        "order_status": "filled",
                        "fill": {
                            "filled_count_fp": "1.00",
                            "remaining_count_fp": "0.00",
                            "average_price_dollars": "0.9700",
                        },
                        "raw": raw_evidence(b'{"status":"filled"}'),
                        "parsed": {"status": "filled"},
                    },
                    events,
                )
            )
        return events

    def write_orders(self) -> None:
        path = self.run_dir / ARTIFACTS["order_evidence"]
        path.write_bytes(
            b"\n".join(canon(event) for event in self.order_events)
            + (b"\n" if self.order_events else b"")
        )

    def snapshot_raw(self, available: bool, omit=(), null_sides=()) -> str:
        """One orderbook_snapshot frame.

        `omit` DROPS a side key entirely, which is what Kalshi does when that
        side holds zero levels (Q-XTAPE addendum, 2026-08-18). `null_sides`
        sets it to JSON null, which is malformed and must still be refused.
        """
        no = [[dollars(3), "1.00"]] if available else []
        msg = {
            "market_ticker": MARKET,
            "yes_dollars_fp": [],
            "no_dollars_fp": no,
        }
        for key in omit:
            msg.pop(key, None)
        for key in null_sides:
            msg[key] = None
        return json.dumps(
            {"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": msg},
            separators=(",", ":"),
        )

    def delta_raw(self) -> str:
        return json.dumps(
            {
                "type": "orderbook_delta",
                "sid": 1,
                "seq": 2,
                "msg": {
                    "market_ticker": MARKET,
                    "side": "no",
                    "price_dollars": dollars(3),
                    "delta_fp": "-1.00",
                },
            },
            separators=(",", ":"),
        )

    def write_book(self, mode: str) -> dict:
        session = {
            "t": "session",
            "schema_version": 1,
            "book_tape_protocol_version": 2,
            "capabilities": [
                "sha256_bound_commit",
                "pending_close_commit",
                "per_ticker_snapshot_readiness",
                "bounded_producer_quiescence",
                "bounded_backlog_drain",
            ],
            "venue": "kalshi",
            "feed_kind": "push",
            "endpoint": "wss://example.invalid",
            "channel": "orderbook_delta",
            "event": "KXTEST",
            "label": RUN,
            "tickers": [MARKET],
            "host": "fixture-box",
            "git_sha": "fixture",
            "clock_offset_s": 0.0,
            "start_wall_ns": 1_800_000_000_000_000_000,
            "start_mono_ns": 900_000_000,
            "planned_secs": None,
            "commit_receipt": self.remote(ARTIFACTS["book_tape"] + ".commit.json"),
        }
        available = mode != "empty"
        omit = ()
        null_sides = ()
        if mode == "omit_yes_side":
            omit = ("yes_dollars_fp",)
        elif mode == "omit_both_sides":
            omit = ("yes_dollars_fp", "no_dollars_fp")
        elif mode == "null_yes_side":
            null_sides = ("yes_dollars_fp",)
        rows = [
            session,
            {
                "t": "frame",
                "wall_ns": 1_800_000_000_100_000_000,
                "mono_ns": SNAPSHOT_MONO,
                "raw": self.snapshot_raw(available, omit, null_sides),
            },
        ]
        if mode == "exhaustion":
            rows.append(
                {
                    "t": "frame",
                    "wall_ns": 1_800_000_000_500_000_000,
                    "mono_ns": EXHAUST_MONO,
                    "raw": self.delta_raw(),
                }
            )
        frame_count = len(rows) - 1
        close = {
            "t": "close",
            "status": "pending",
            "candidate_status": "closed",
            "complete": False,
            "candidate_complete": True,
            "commit_required": True,
            "reason": "requested",
            "rows": frame_count,
            "dropped": 0,
            "frame_rows": frame_count,
            "heartbeat_rows": 0,
            "unwritten_frames": 0,
            "tap_dropped": 0,
            "ever_observed_feed": True,
            "producer_quiesced": True,
            "terminal_accounting_complete": True,
            "requested_tickers": [MARKET],
            "ready_tickers": [MARKET],
            "missing_tickers": [],
            "start_wall_ns": session["start_wall_ns"],
            "start_mono_ns": session["start_mono_ns"],
            "end_wall_ns": 1_800_000_000_600_000_000,
            "end_mono_ns": 1_600_000_000,
            "close_wall_ns": 1_800_000_000_550_000_000,
            "close_mono_ns": 1_550_000_000,
            "feed_age_ns": 1_000_000,
            "feed_health": "healthy",
            "writer_status": "pending_commit",
            "writer_error": None,
        }
        rows.append(close)
        tape_path = self.run_dir / ARTIFACTS["book_tape"]
        tape_raw = b"\n".join(canon(row) for row in rows) + b"\n"
        tape_path.write_bytes(tape_raw)
        commit = {
            "t": "commit",
            "schema_version": 2,
            "book_tape_protocol_version": 2,
            "capabilities": session["capabilities"],
            "durable": True,
            "tape_bytes": len(tape_raw),
            "tape_sha256": digest(tape_raw),
            "status": "closed",
            "complete": True,
            "reason": close["reason"],
            "frame_rows": close["frame_rows"],
            "heartbeat_rows": close["heartbeat_rows"],
            "unwritten_frames": 0,
            "tap_dropped": 0,
            "ever_observed_feed": True,
            "producer_quiesced": True,
            "terminal_accounting_complete": True,
            "requested_tickers": [MARKET],
            "ready_tickers": [MARKET],
            "missing_tickers": [],
            "terminal_feed_age_ns": close["feed_age_ns"],
            "terminal_feed_health": "healthy",
            "close_wall_ns": close["close_wall_ns"],
            "close_mono_ns": close["close_mono_ns"],
            "start_wall_ns": close["start_wall_ns"],
            "start_mono_ns": close["start_mono_ns"],
            "end_wall_ns": close["end_wall_ns"],
            "end_mono_ns": close["end_mono_ns"],
        }
        write_line(Path(str(tape_path) + ".commit.json"), commit)
        return commit

    def write_audio(self) -> dict:
        raw_path = self.run_dir / ARTIFACTS["audio_raw"]
        frames_path = self.run_dir / ARTIFACTS["audio_frames"]
        manifest_path = self.run_dir / ARTIFACTS["audio_manifest"]
        commit_path = self.run_dir / ARTIFACTS["audio_commit"]
        wav_dir = self.run_dir / ARTIFACTS["audio_wav_dir"]
        wav_dir.mkdir()
        raw_path.write_bytes(b"fixture-audio-raw")
        frames_path.write_bytes(b'{"fixture":true}\n')
        pcm = struct.pack("<hh", 1, -1)
        wav_path = wav_dir / "source-00000-segment-0000-8000hz.wav"
        with wave.open(str(wav_path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(8000)
            writer.writeframes(pcm)
        segment = {
            "source": 0,
            "source_label": "primary",
            "segment": 0,
            "sample_rate_hz": 8000,
            "first_seq": 0,
            "last_seq": 0,
            "first_capture_ts_ns": 1,
            "last_capture_ts_ns": 1,
            "first_preproc_ts_ns": 2,
            "last_preproc_ts_ns": 2,
            "frame_count": 1,
            "sample_count": 2,
            "start_reasons": ["first_frame"],
            "pcm_sha256": digest(pcm),
            "path": self.remote(
                ARTIFACTS["audio_wav_dir"] + "/" + wav_path.name
            ),
            "wav": fp(wav_path),
        }
        identity = {
            "run_id": RUN,
            "event_id": EVENT,
            "plan_sha256": self.plan["plan_sha256"],
        }
        manifest = {
            "schema_version": 1,
            "identity": identity,
            "status": "pending_commit",
            "complete": False,
            "commit_required": True,
            "close_reason": "requested",
            "frame_count": 1,
            "sample_count": 2,
            "expected_published_frames": 1,
            "bank_accepted_frames": 1,
            "writer_frames": 1,
            "dropped_frames": 0,
            "closed_attempts": 0,
            "invalid_attempts": 0,
            "producer_quiesced": True,
            "raw_path": self.remote(ARTIFACTS["audio_raw"]),
            "raw": fp(raw_path),
            "frames_path": self.remote(ARTIFACTS["audio_frames"]),
            "frames": fp(frames_path),
            "source_labels": {"0": "primary"},
            "wav_dir": self.remote(ARTIFACTS["audio_wav_dir"]),
            "wav_segments": [segment],
            "start_wall_ns": 1,
            "start_mono_ns": 1,
            "end_wall_ns": 2,
            "end_mono_ns": 2,
        }
        write_line(manifest_path, manifest)
        commit = {
            "schema_version": 1,
            "identity": identity,
            "status": "committed",
            "complete": True,
            "durable": True,
            "manifest_path": self.remote(ARTIFACTS["audio_manifest"]),
            "manifest": fp(manifest_path),
            "raw_path": manifest["raw_path"],
            "raw": manifest["raw"],
            "frames_path": manifest["frames_path"],
            "frames": manifest["frames"],
            "wav_dir": manifest["wav_dir"],
            "wav_segments": [segment],
            "frame_count": 1,
            "sample_count": 2,
            "close_wall_ns": 3,
            "close_mono_ns": 3,
        }
        write_line(commit_path, commit)
        return commit

    def detection(self):
        return {
            "detection": {
                "keyword": 0,
                "confidence": 0.9,
                "tier": "Fast",
                "audio_ts": 1_100_000_000,
                "emit_ts": DETECTION_EMIT,
                "source": 0,
                "audio_pos_ms": 1000,
                "audio_consumed_ms": 1100,
            },
            "intent": {
                "keyword": 0,
                "source": 0,
                "market_ticker": MARKET,
                "max_price_cents": 97,
                "tranche_contracts": 1,
                "tier": "Fast",
                "audio_ts": 1_100_000_000,
                "decision_ts": DECISION_TS,
                "handoff_ts": 1_210_000_000,
            },
            "detect_latency_us": 50_000,
        }

    def write_fix_tape(self, mode: str) -> dict:
        tape_path = self.run_dir / ARTIFACTS["fix_observation_tape"]
        rows: list[dict] = []
        previous_hash = self.plan["plan_sha256"]
        previous_process = ""

        def append(record: dict) -> None:
            nonlocal previous_hash, previous_process
            event = {
                "schema_version": 1,
                "tape_seq": len(rows),
                "wall_unix_ns": 1_800_000_000_500_000_000 + len(rows),
                "monotonic_ns": 1_500_000_000 + len(rows),
                "process_instance_id": FIX_PROCESS,
                "run_id": RUN,
                "event_id": EVENT,
                "kalshi_env": "prod",
                "account_fingerprint": FINGERPRINT,
                "plan_sha256": self.plan["plan_sha256"],
                "endpoint_id": FIX_ENDPOINT,
                "previous_process_instance_id": previous_process,
                "previous_record_sha256": previous_hash,
                "record": record,
                "record_sha256": "",
            }
            unhashed = dict(event)
            del unhashed["record_sha256"]
            event["record_sha256"] = digest(canon(unhashed, ascii_only=False))
            rows.append(event)
            previous_hash = event["record_sha256"]
            previous_process = FIX_PROCESS

        append({"type": "session_started"})
        observation_count = int(mode not in ("none", "not_attempted", "illegal_wire"))
        if observation_count:
            raw = fix_execution_frame()
            raw_sha = digest(raw)
            observation = {
                "source_sequence": 1,
                "generation": 1,
                "receipt_id": digest(
                    canon([FIX_PROCESS, 1, 1, raw_sha], ascii_only=False)
                ),
                "frame_key": digest(
                    canon([FIX_ENDPOINT, raw_sha], ascii_only=False)
                ),
                "kind": "execution_report",
                "fix_msg_type": "8",
                "fix_msg_seq_num": 41,
                "fix_sending_time": "20260812-17:00:00.000",
                "recv_monotonic_ns": 1_400_000_000,
                "ack_monotonic_ns": 1_410_000_000,
                "execution": {
                    "client_order_id": "client-1",
                    "order_id": "order-1",
                    "status": "filled",
                    "terminal": True,
                    "parsed_fields": {
                        "exec_type": "F",
                        "ord_status": "2",
                        "order_id": "order-1",
                        "client_order_id": "client-1",
                        "cum_qty": "1.00",
                        "leaves_qty": "0.00",
                        "last_px": "0.97",
                        "last_qty": "1.00",
                        "avg_px": "0.97",
                        "ord_rej_reason": None,
                        "text": None,
                    },
                },
                "raw": {
                    "media_type": "application/fix; version=FIXT.1.1",
                    "bytes_base64": base64.b64encode(raw).decode(),
                    "bytes_len": len(raw),
                    "bytes_sha256": raw_sha,
                },
            }
            append({"type": "observation", "observation": observation})
        close = {
            "type": "session_closed",
            "reason": "requested",
            "close_requested": True,
            "producer_quiesced": True,
            "terminal_accounting_complete": True,
            "session_complete": True,
            "tape_complete": True,
            "accepted_deliveries": observation_count,
            "durably_accounted_deliveries": observation_count,
            "observation_rows": observation_count,
            "idempotent_retries": 0,
            "execution_rows": observation_count,
            "reject_rows": 0,
            "correlated_rows": observation_count,
            "orphan_rows": 0,
            "duplicate_frames": 0,
            "dropped_observations": 0,
            "unwritten_observations": 0,
            "prior_incomplete_sessions": 0,
        }
        append(close)
        tape_raw = b"".join(canon(row, ascii_only=False) + b"\n" for row in rows)
        tape_path.write_bytes(tape_raw)
        close_event = rows[-1]
        commit_path = Path(str(tape_path) + f".commit.{FIX_PROCESS}.json")
        commit = {
            "type": "fix_observation_tape_commit",
            "schema_version": 1,
            "process_instance_id": FIX_PROCESS,
            "close_tape_seq": close_event["tape_seq"],
            "close_record_sha256": close_event["record_sha256"],
            "tape_bytes": len(tape_raw),
            "tape_sha256": digest(tape_raw),
            "session_complete": True,
            "tape_complete": True,
            "durable": True,
        }
        write_line(commit_path, commit)
        return {
            "complete": True,
            "committed": True,
            "reason": "requested",
            "process_instance_id": FIX_PROCESS,
            "accepted_deliveries": observation_count,
            "durably_accounted_deliveries": observation_count,
            "observation_rows": observation_count,
            "idempotent_retries": 0,
            "execution_rows": observation_count,
            "reject_rows": 0,
            "correlated_rows": observation_count,
            "orphan_rows": 0,
            "duplicate_frames": 0,
            "dropped_observations": 0,
            "unwritten_observations": 0,
            "prior_incomplete_sessions": 0,
            "producer_quiesced": True,
            "terminal_accounting_complete": True,
            "close_record_sha256": close_event["record_sha256"],
            "commit_path": self.remote(
                ARTIFACTS["fix_observation_tape"]
                + f".commit.{FIX_PROCESS}.json"
            ),
            "writer_error": None,
        }

    def rewrite_fix_rows(self, rows: list[dict]) -> None:
        previous_hash = self.plan["plan_sha256"]
        previous_process = ""
        chunks: list[bytes] = []
        for tape_seq, row in enumerate(rows):
            row["tape_seq"] = tape_seq
            row["previous_record_sha256"] = previous_hash
            row["previous_process_instance_id"] = previous_process
            unhashed = dict(row)
            unhashed.pop("record_sha256", None)
            row["record_sha256"] = digest(canon(unhashed, ascii_only=False))
            previous_hash = row["record_sha256"]
            previous_process = row["process_instance_id"]
            chunks.append(canon(row, ascii_only=False) + b"\n")
        tape_path = self.run_dir / ARTIFACTS["fix_observation_tape"]
        tape_raw = b"".join(chunks)
        tape_path.write_bytes(tape_raw)
        family_prefix = tape_path.name + ".commit."
        for entry in tape_path.parent.iterdir():
            if entry.name.startswith(family_prefix) and entry.name.endswith(".json"):
                entry.unlink()
        offset = 0
        for row, chunk in zip(rows, chunks, strict=True):
            offset += len(chunk)
            if row["record"].get("type") != "session_closed":
                continue
            process = row["process_instance_id"]
            close = row["record"]
            commit_path = Path(str(tape_path) + f".commit.{process}.json")
            commit = {
                "type": "fix_observation_tape_commit",
                "schema_version": 1,
                "process_instance_id": process,
                "close_tape_seq": row["tape_seq"],
                "close_record_sha256": row["record_sha256"],
                "tape_bytes": offset,
                "tape_sha256": digest(tape_raw[:offset]),
                "session_complete": close["session_complete"],
                "tape_complete": close["tape_complete"],
                "durable": True,
            }
            write_line(commit_path, commit)

    def fix_rows(self) -> list[dict]:
        path = self.run_dir / ARTIFACTS["fix_observation_tape"]
        return [json.loads(line) for line in path.read_text().splitlines()]

    def write_report(self, mode: str) -> None:
        attempted = mode not in ("not_attempted", "illegal_wire", "none")
        order = {
            "client_order_id": "client-1",
            "ticker": MARKET,
            "price_cents": 97,
            "count": 1,
            "tier": "Fast",
            "dry_run": False,
            "lost_race": not attempted,
            "refusal": None if attempted else "no ask at or under cap",
            "order_status": "filled" if attempted else None,
            "order_id": "order-1" if attempted else None,
            "audio_ts": 1_100_000_000,
            "decision_ts": DECISION_TS,
            "exec_ts": EXEC_TS,
            "wire_state": "observed" if attempted else "not_attempted",
            "send_ts": SEND_TS if attempted else None,
            "hdrs_ts": 1_350_000_000 if attempted else None,
            "ack_ts": 1_400_000_000 if attempted else None,
            "transport_protocol": self.transport if attempted else None,
            "protocol_status": "201" if attempted else None,
            "exchange_accepted": True if attempted else None,
            "order_terminal": True if attempted else None,
            "exchange_observed_ts": 1_400_000_000 if attempted else None,
            "reconciliation_ts": None,
            "reconciliation_status": None,
            "transport_error": None,
            "parse_error": None,
            "raw_response": None,
            "raw_response_body_base64": None,
            "reconciliation_response": None,
            "fill": {"cumulative_contracts": "1.00"} if attempted else None,
            "best_ask": [97, 100] if attempted else None,
            "book_as_of_ts": 1_100_000_000,
            "book_age_ns": 150_000_000,
            "ev_at_ask_cents": 3 if attempted else None,
        }
        report = {
            "event_id": EVENT,
            "run_plan_schema": 3,
            "run_id": RUN,
            "plan_sha256": self.plan["plan_sha256"],
            "execution_host_id": "fixture-box",
            "account_fingerprint": FINGERPRINT,
            "finalized": True,
            "required_evidence_healthy": True,
            "runtime_success": True,
            "book_tape_healthy": True,
            "order_evidence_healthy": True,
            "audio_evidence_healthy": True,
            "fix_observation_evidence_healthy": (
                True if self.transport == "fix" else None
            ),
            "fix_observation_tape_healthy": (
                True if self.transport == "fix" else None
            ),
            "book_tape_path": self.remote(ARTIFACTS["book_tape"]),
            "order_evidence_path": self.remote(ARTIFACTS["order_evidence"]),
            "audio_raw_path": self.remote(ARTIFACTS["audio_raw"]),
            "audio_frames_path": self.remote(ARTIFACTS["audio_frames"]),
            "audio_manifest_path": self.remote(ARTIFACTS["audio_manifest"]),
            "audio_commit_path": self.remote(ARTIFACTS["audio_commit"]),
            "audio_wav_dir": self.remote(ARTIFACTS["audio_wav_dir"]),
            "fix_observation_tape_path": self.remote(
                ARTIFACTS["fix_observation_tape"]
            ),
            "book_tape": {
                "complete": True,
                "committed": True,
                "frame_rows": self.book_commit["frame_rows"],
                "heartbeat_rows": 0,
                "unwritten_frames": 0,
                "tap_dropped": 0,
                "reason": "requested",
            },
            "audio_evidence": {"status": "committed", "commit": self.audio_commit},
            "fix_observation_evidence": (
                {"status": "committed", "summary": self.fix_summary}
                if self.transport == "fix"
                else {"status": "not_applicable"}
            ),
            "exit": {"reason": "clean_drain"},
            "detections": [] if mode == "none" else [self.detection()],
            "intents": 0 if mode == "none" else 1,
            "send_attempted": 1 if attempted else 0,
            "sent": 1 if attempted else 0,
            "acked": 1 if attempted else 0,
            "orders": [] if mode == "none" else [order],
            "unresolved_cancellation_ids": [],
            "kill_unresolved_ids": [],
        }
        (self.run_dir / ARTIFACTS["runtime_report"]).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )

    def run(self):
        env = dict(os.environ)
        env["EARSHOT_REPO"] = str(self.repo)
        return subprocess.run(
            [sys.executable, str(AUDIT), "--run-dir", str(self.run_dir)],
            text=True,
            capture_output=True,
            env=env,
            # The grader's own eventctl and lifecycle subprocess bounds are
            # 30 seconds; keep the outer fixture bound above them so machine
            # load cannot report a false test failure first.
            timeout=45,
            check=False,
        )

    def grade(self):
        return json.loads((self.run_dir / ARTIFACTS["grade"]).read_text())

    def receipt_bytes(self):
        return (self.run_dir / ARTIFACTS["receipts"]).read_bytes()


class RunAuditTests(unittest.TestCase):
    def fixture(self, **kwargs):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return Fixture(Path(temp.name), **kwargs)

    def assert_ok(self, fixture: Fixture):
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return fixture.grade()

    def assert_failure_without_mutation(self, fixture: Fixture):
        before = fixture.receipt_bytes()
        result = fixture.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(fixture.receipt_bytes(), before)
        self.assertFalse((fixture.run_dir / ARTIFACTS["grade"]).exists())
        return result

    def test_happy_fill_uses_order_evidence_as_accounting_authority(self):
        fixture = self.fixture(order_mode="fill", book_mode="available")
        # Known serde_json::to_value -> compact object-map hash vector for the
        # fixture's first OrderEvidenceEvent.  This is deliberately literal:
        # the grader and fixture builder cannot silently agree on a different
        # Python serialization and still pass.
        self.assertEqual(
            fixture.order_events[0]["event_sha256"],
            "91079f07154848ae3ad8d2421f983a5615325f716c6fa74fb5285cd765c6677b",
        )
        grade = self.assert_ok(fixture)
        self.assertEqual(grade["accounting"], {
            "accepted": 1, "attempted": 1, "filled": 1,
            "intents": 1, "terminal": 1, "unresolved": 0,
        })
        self.assertTrue(grade["orders"][0]["filled"])
        self.assertEqual(grade["audio_evidence"]["precision"], "not_resolvable")

    def test_not_attempted_empty_book_is_not_a_lost_speed_claim(self):
        fixture = self.fixture(order_mode="not_attempted", book_mode="empty")
        grade = self.assert_ok(fixture)
        observation = grade["orders"][0]["book_observation"]
        self.assertEqual(observation["classification"], "empty_at_detection")
        self.assertEqual(observation["kind"], "cap_depth_exhaustion")
        self.assertEqual(observation["causality"], "not_resolvable")
        self.assertFalse(grade["orders"][0]["attempted"])

    def test_zero_intent_journal_is_valid_zero_trade_accounting(self):
        fixture = self.fixture(order_mode="none", book_mode="empty")
        grade = self.assert_ok(fixture)
        self.assertEqual(grade["accounting"], {
            "accepted": 0, "attempted": 0, "filled": 0,
            "intents": 0, "terminal": 0, "unresolved": 0,
        })
        self.assertEqual(grade["orders"], [])

    def test_cap_exhaustion_reports_detection_and_wire_timing(self):
        fixture = self.fixture(order_mode="fill", book_mode="exhaustion")
        grade = self.assert_ok(fixture)
        observation = grade["orders"][0]["book_observation"]
        self.assertEqual(observation["classification"], "depth_exhausted_after_detection")
        self.assertEqual(observation["detection_to_exhaustion_ms"], 350.0)
        self.assertEqual(observation["wire_to_exhaustion_ms"], 200.0)
        self.assertEqual(observation["exhaustion_receive_mono_ns"], EXHAUST_MONO)

    def test_omitted_snapshot_side_is_an_empty_ladder_not_a_broken_tape(self):
        """Kalshi DROPS a side key when that side holds zero levels.

        Live 2026-08-18: 2 of 12 tickers on the AS board (PICK, WIMB) had no
        YES resting orders, so their snapshots carried only `no_dollars_fp`.
        The auditor required both keys, so those tickers never earned a
        snapshot proof, the re-derived readiness set disagreed with the
        runtime's on all 4,021 heartbeats, and the WHOLE run was refused — the
        one run that day which filled at 96c. An omitted side must read exactly
        like an explicitly empty one.
        """
        omitted = self.assert_ok(
            self.fixture(order_mode="fill", book_mode="omit_yes_side"))
        explicit = self.assert_ok(
            self.fixture(order_mode="fill", book_mode="available"))
        self.assertEqual(
            omitted["orders"][0]["book_observation"],
            explicit["orders"][0]["book_observation"],
        )
        self.assertEqual(
            omitted["book_evidence"]["frame_rows"],
            explicit["book_evidence"]["frame_rows"],
        )

    def test_both_snapshot_sides_omitted_is_a_valid_empty_book(self):
        """A board with no resting orders at all serializes with neither key.

        Already pinned for the whole-board case (AMD 26AUG04 pre-call); this
        keeps the auditor agreeing with it rather than calling it malformed.
        """
        grade = self.assert_ok(
            self.fixture(order_mode="fill", book_mode="omit_both_sides"))
        self.assertEqual(
            grade["orders"][0]["book_observation"]["causality"], "not_resolvable")

    def test_null_snapshot_side_is_still_refused(self):
        """Absent means empty; PRESENT-but-not-an-array stays malformed.

        The fail-closed half must survive the fix — this is the exact pairing
        src/kalshi/book.rs::side_levels makes, pinned there by
        `omitted_snapshot_side_is_an_explicit_empty_ladder`. Without this test,
        widening `parse_snapshot` to accept an absent key could be quietly
        widened again to accept any junk.
        """
        result = self.assert_failure_without_mutation(
            self.fixture(order_mode="fill", book_mode="null_yes_side"))
        self.assertIn("readiness is not proven", result.stdout + result.stderr)

    def test_available_depth_without_exhaustion_stays_explicit(self):
        fixture = self.fixture(order_mode="fill", book_mode="available")
        grade = self.assert_ok(fixture)
        observation = grade["orders"][0]["book_observation"]
        self.assertEqual(observation["classification"], "depth_available_no_exhaustion")
        self.assertIsNone(observation["detection_to_exhaustion_ms"])

    def test_tampered_order_hash_is_refused_without_mutation(self):
        fixture = self.fixture()
        fixture.order_events[0]["event_sha256"] = "0" * 64
        fixture.write_orders()
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("event SHA-256 mismatch", result.stderr)

    def test_illegal_order_transition_is_refused(self):
        fixture = self.fixture(order_mode="illegal_wire")
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("wire_attempted", result.stderr)

    def test_corrupt_book_commit_is_refused(self):
        fixture = self.fixture()
        path = Path(str(fixture.run_dir / ARTIFACTS["book_tape"]) + ".commit.json")
        commit = json.loads(path.read_text())
        commit["tape_sha256"] = "0" * 64
        write_line(path, commit)
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("book tape commit", result.stderr)

    def test_report_order_disagreement_is_loud(self):
        fixture = self.fixture()
        path = fixture.run_dir / ARTIFACTS["runtime_report"]
        report = json.loads(path.read_text())
        report["send_attempted"] = 0
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("disagrees with order evidence", result.stderr)

    def test_audio_commit_tamper_is_refused(self):
        fixture = self.fixture()
        path = fixture.run_dir / ARTIFACTS["audio_commit"]
        commit = json.loads(path.read_text())
        commit["raw"]["sha256"] = "0" * 64
        write_line(path, commit)
        # Keep report equal to the tampered receipt: the independent bytes bind.
        report_path = fixture.run_dir / ARTIFACTS["runtime_report"]
        report = json.loads(report_path.read_text())
        report["audio_evidence"]["commit"] = commit
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("audio commit raw fingerprint", result.stderr)

    def test_fix_tape_is_independently_verified_and_fingerprinted(self):
        fixture = self.fixture(transport="fix")
        grade = self.assert_ok(fixture)
        evidence = grade["fix_observation_evidence"]
        tape_path = fixture.run_dir / ARTIFACTS["fix_observation_tape"]
        self.assertEqual(evidence["status"], "committed")
        self.assertEqual(evidence["protocol"], 1)
        self.assertEqual(evidence["endpoint_id"], FIX_ENDPOINT)
        self.assertEqual(evidence["record_count"], 3)
        self.assertEqual(evidence["observation_rows"], 1)
        self.assertEqual(evidence["session_count"], 1)
        self.assertEqual(evidence["incomplete_sessions"], 0)
        self.assertEqual(evidence["tape_sha256"], digest(tape_path.read_bytes()))
        self.assertEqual(evidence["latest_session"]["observation_rows"], 1)
        self.assertEqual(
            evidence["latest_session"]["commit_artifact"],
            ARTIFACTS["fix_observation_tape"]
            + f".commit.{FIX_PROCESS}.json",
        )
        self.assertEqual(len(evidence["session_commits"]), 1)
        self.assertEqual(
            evidence["session_commits"][0]["close_record_sha256"],
            fixture.fix_summary["close_record_sha256"],
        )
        self.assertEqual(
            grade["evidence_fingerprints"]["fix_observation_tape_sha256"],
            evidence["tape_sha256"],
        )

    def test_fix_zero_observation_session_is_still_committed_evidence(self):
        fixture = self.fixture(transport="fix", order_mode="none", book_mode="empty")
        grade = self.assert_ok(fixture)
        evidence = grade["fix_observation_evidence"]
        self.assertEqual(evidence["record_count"], 2)
        self.assertEqual(evidence["observation_rows"], 0)
        self.assertEqual(evidence["latest_session"]["accepted_deliveries"], 0)
        self.assertEqual(evidence["latest_session"]["observation_rows"], 0)

    def test_fix_multiple_sessions_require_prefix_bound_commit_per_close(self):
        fixture = self.fixture(transport="fix")
        rows = fixture.fix_rows()
        second_process = "fixture-fix-process-2"
        second_start = json.loads(json.dumps(rows[0]))
        second_start["process_instance_id"] = second_process
        second_start["wall_unix_ns"] += 10
        second_start["monotonic_ns"] += 10
        second_start["record"] = {"type": "session_started"}
        second_close = json.loads(json.dumps(rows[-1]))
        second_close["process_instance_id"] = second_process
        second_close["wall_unix_ns"] += 20
        second_close["monotonic_ns"] += 20
        for field in (
            "accepted_deliveries",
            "durably_accounted_deliveries",
            "observation_rows",
            "execution_rows",
            "correlated_rows",
        ):
            second_close["record"][field] = 0
        rows.extend([second_start, second_close])
        fixture.rewrite_fix_rows(rows)

        report_path = fixture.run_dir / ARTIFACTS["runtime_report"]
        report = json.loads(report_path.read_text())
        summary = report["fix_observation_evidence"]["summary"]
        summary["process_instance_id"] = second_process
        summary["accepted_deliveries"] = 0
        summary["durably_accounted_deliveries"] = 0
        summary["observation_rows"] = 0
        summary["execution_rows"] = 0
        summary["correlated_rows"] = 0
        summary["close_record_sha256"] = rows[-1]["record_sha256"]
        summary["commit_path"] = fixture.remote(
            ARTIFACTS["fix_observation_tape"]
            + f".commit.{second_process}.json"
        )
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

        grade = self.assert_ok(fixture)
        evidence = grade["fix_observation_evidence"]
        self.assertEqual(evidence["record_count"], 5)
        self.assertEqual(evidence["session_count"], 2)
        self.assertEqual(evidence["observation_rows"], 1)
        self.assertEqual(len(evidence["session_commits"]), 2)
        self.assertLess(
            evidence["session_commits"][0]["tape_bytes"],
            evidence["session_commits"][1]["tape_bytes"],
        )
        self.assertEqual(
            evidence["latest_session"]["process_instance_id"], second_process
        )

    def test_fix_repeated_wire_frame_is_retained_with_first_row_pointer(self):
        fixture = self.fixture(transport="fix")
        rows = fixture.fix_rows()
        repeated = json.loads(json.dumps(rows[1]))
        observation = repeated["record"]["observation"]
        observation["source_sequence"] = 2
        observation["receipt_id"] = digest(
            canon(
                [
                    FIX_PROCESS,
                    2,
                    observation["generation"],
                    observation["raw"]["bytes_sha256"],
                ],
                ascii_only=False,
            )
        )
        observation["duplicate_of_tape_seq"] = 1
        repeated["wall_unix_ns"] += 10
        repeated["monotonic_ns"] += 10
        rows.insert(-1, repeated)
        close = rows[-1]["record"]
        close["accepted_deliveries"] = 2
        close["durably_accounted_deliveries"] = 2
        close["observation_rows"] = 2
        close["execution_rows"] = 2
        close["correlated_rows"] = 2
        close["duplicate_frames"] = 1
        fixture.rewrite_fix_rows(rows)

        report_path = fixture.run_dir / ARTIFACTS["runtime_report"]
        report = json.loads(report_path.read_text())
        summary = report["fix_observation_evidence"]["summary"]
        for field in (
            "accepted_deliveries",
            "durably_accounted_deliveries",
            "observation_rows",
            "execution_rows",
            "correlated_rows",
        ):
            summary[field] = 2
        summary["duplicate_frames"] = 1
        summary["close_record_sha256"] = rows[-1]["record_sha256"]
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

        evidence = self.assert_ok(fixture)["fix_observation_evidence"]
        self.assertEqual(evidence["observation_rows"], 2)
        self.assertEqual(evidence["latest_session"]["duplicate_frames"], 1)

    def test_missing_fix_tape_is_refused(self):
        fixture = self.fixture(transport="fix")
        (fixture.run_dir / ARTIFACTS["fix_observation_tape"]).unlink()
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("FIX observation tape is missing", result.stderr)

    def test_truncated_fix_tape_is_refused(self):
        fixture = self.fixture(transport="fix")
        path = fixture.run_dir / ARTIFACTS["fix_observation_tape"]
        path.write_bytes(path.read_bytes()[:-1])
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("truncated final line", result.stderr)

    def test_mutated_fix_tape_hash_chain_is_refused(self):
        fixture = self.fixture(transport="fix")
        rows = fixture.fix_rows()
        rows[1]["record"]["observation"]["recv_monotonic_ns"] += 1
        path = fixture.run_dir / ARTIFACTS["fix_observation_tape"]
        path.write_bytes(
            b"".join(canon(row, ascii_only=False) + b"\n" for row in rows)
        )
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("record SHA-256 mismatch", result.stderr)

    def test_fix_tape_duplicate_json_field_is_refused(self):
        fixture = self.fixture(transport="fix")
        path = fixture.run_dir / ARTIFACTS["fix_observation_tape"]
        raw = path.read_bytes()
        needle = b'"event_id":"AUDIT-TEST",'
        self.assertIn(needle, raw)
        path.write_bytes(raw.replace(needle, needle + needle, 1))
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("duplicate JSON field 'event_id'", result.stderr)

    def test_fix_tape_unknown_json_field_is_refused_after_rehash(self):
        fixture = self.fixture(transport="fix")
        rows = fixture.fix_rows()
        rows[1]["record"]["observation"]["unexpected"] = True
        fixture.rewrite_fix_rows(rows)
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("fields differ", result.stderr)

    def test_fix_projection_mutation_is_refused_after_rehash(self):
        fixture = self.fixture(transport="fix")
        rows = fixture.fix_rows()
        rows[1]["record"]["observation"]["execution"]["status"] = "new"
        fixture.rewrite_fix_rows(rows)
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("projection differs from exact raw frame", result.stderr)

    def test_fix_tape_identity_mutation_is_refused_after_rehash(self):
        fixture = self.fixture(transport="fix")
        rows = fixture.fix_rows()
        rows[1]["event_id"] = "OTHER-EVENT"
        fixture.rewrite_fix_rows(rows)
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("immutable event_id differs", result.stderr)

    def test_fix_close_accounting_mutation_is_refused_after_rehash(self):
        fixture = self.fixture(transport="fix")
        rows = fixture.fix_rows()
        rows[-1]["record"]["accepted_deliveries"] = 2
        rows[-1]["record"]["durably_accounted_deliveries"] = 2
        fixture.rewrite_fix_rows(rows)
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("durably_accounted_deliveries differs from replay", result.stderr)

    def test_missing_fix_commit_receipt_is_refused(self):
        fixture = self.fixture(transport="fix")
        commit_path = Path(
            str(fixture.run_dir / ARTIFACTS["fix_observation_tape"])
            + f".commit.{FIX_PROCESS}.json"
        )
        commit_path.unlink()
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("FIX observation commit", result.stderr)
        self.assertIn("is missing", result.stderr)

    def test_fix_commit_prefix_mismatch_is_refused(self):
        fixture = self.fixture(transport="fix")
        commit_path = Path(
            str(fixture.run_dir / ARTIFACTS["fix_observation_tape"])
            + f".commit.{FIX_PROCESS}.json"
        )
        commit = json.loads(commit_path.read_text())
        commit["tape_sha256"] = "0" * 64
        write_line(commit_path, commit)
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("does not bind the exact tape prefix", result.stderr)

    def test_fix_report_summary_disagreement_is_refused(self):
        fixture = self.fixture(transport="fix")
        path = fixture.run_dir / ARTIFACTS["runtime_report"]
        report = json.loads(path.read_text())
        report["fix_observation_evidence"]["summary"]["observation_rows"] = 2
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("summary disagrees for observation_rows", result.stderr)

    def test_rest_run_refuses_stray_fix_tape(self):
        fixture = self.fixture()
        (fixture.run_dir / ARTIFACTS["fix_observation_tape"]).write_bytes(b"stray\n")
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("contradictory FIX observation tape", result.stderr)

    def test_rest_run_refuses_stray_fix_commit_receipt(self):
        fixture = self.fixture()
        tape_path = fixture.run_dir / ARTIFACTS["fix_observation_tape"]
        Path(str(tape_path) + ".commit.stray.json").write_bytes(b"{}\n")
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("contradictory FIX observation tape", result.stderr)

    def test_rest_run_refuses_contradictory_fix_report_claim(self):
        fixture = self.fixture()
        path = fixture.run_dir / ARTIFACTS["runtime_report"]
        report = json.loads(path.read_text())
        report["fix_observation_evidence_healthy"] = True
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("REST runtime report must use null FIX evidence health", result.stderr)

    def test_rest_run_requires_explicit_null_fix_health(self):
        fixture = self.fixture()
        path = fixture.run_dir / ARTIFACTS["runtime_report"]
        report = json.loads(path.read_text())
        del report["fix_observation_tape_healthy"]
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        result = self.assert_failure_without_mutation(fixture)
        self.assertIn("lacks required fix_observation_tape_healthy", result.stderr)

    def test_grade_and_lifecycle_record_are_idempotent(self):
        fixture = self.fixture()
        first = fixture.run()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        grade_bytes = (fixture.run_dir / ARTIFACTS["grade"]).read_bytes()
        second = fixture.run()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual((fixture.run_dir / ARTIFACTS["grade"]).read_bytes(), grade_bytes)
        receipts = [json.loads(line) for line in fixture.receipt_bytes().splitlines()]
        self.assertEqual(
            sum(r.get("transition") == "graded" and r.get("outcome") == "ok" for r in receipts),
            1,
        )

    def test_local_webcast_consolidates_evidence_before_grading(self):
        fixture = self.fixture(evidence_pulled=False)
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipts = [json.loads(line) for line in fixture.receipt_bytes().splitlines()]
        reached = [
            row.get("transition")
            for row in receipts
            if row.get("kind") == "transition" and row.get("outcome") == "ok"
        ]
        self.assertNotIn("evidence_pulled", reached)
        self.assertLess(reached.index("evidence_consolidated"), reached.index("graded"))

    def test_validation_failure_never_creates_grade_or_receipt(self):
        fixture = self.fixture()
        path = fixture.run_dir / ARTIFACTS["runtime_report"]
        report = json.loads(path.read_text())
        report["finalized"] = False
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        self.assert_failure_without_mutation(fixture)


if __name__ == "__main__":
    unittest.main(verbosity=2)
