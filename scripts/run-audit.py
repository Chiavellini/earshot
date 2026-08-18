#!/usr/bin/env python3
"""Deterministic, offline audit for one immutable schema-3 EARSHOT run.

Order evidence is the accounting authority.  The book tape can prove only an
observed cap-depth transition; this tool deliberately never calls that event a
competitor trade or a sweep.  Audio truth/precision remains unresolved until a
separate transcript-scoring job consumes the committed WAV segments.

The tool performs no network, dial, or trading operation.  On validation
failure it writes nothing.  On success it publishes the exact RunPlan grade
destination without overwrite, then asks eventctl's public door to append the
``graded`` transition when (and only when) consolidated evidence is present.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
import wave
from pathlib import Path
from typing import Any


PLAN_SCHEMA = 3
GRADE_SCHEMA = 1
ORDER_SCHEMA = 4
BOOK_PROTOCOL = 2
AUDIO_SCHEMA = 1
FIX_OBSERVATION_PROTOCOL = 1
FIX_RAW_MEDIA_TYPE = "application/fix; version=FIXT.1.1"
BOOK_CAPABILITIES = [
    "sha256_bound_commit",
    "pending_close_commit",
    "per_ticker_snapshot_readiness",
    "bounded_producer_quiescence",
    "bounded_backlog_drain",
]

PLAN_ARTIFACT_KEYS = {
    "receipts",
    "status",
    "audio_raw",
    "audio_frames",
    "audio_manifest",
    "audio_commit",
    "audio_lock",
    "audio_wav_dir",
    "book_tape",
    "order_evidence",
    "fix_observation_tape",
    "runtime_report",
    "grade",
}

EVENT_FIELDS = {
    "schema_version",
    "journal_seq",
    "order_seq",
    "wall_unix_ns",
    "monotonic_ns",
    "process_instance_id",
    "run_id",
    "event_id",
    "kalshi_env",
    "account_fingerprint",
    "plan_sha256",
    "client_order_id",
    "market_ticker",
    "previous_process_instance_id",
    "previous_event_sha256",
    "transition",
    "event_sha256",
}

RECEIPT_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    "intent_evaluated": (
        {
            "client_order_id", "market_ticker", "side", "action",
            "price_cents", "contract_count", "time_in_force", "tier",
            "keyword_id", "keyword", "source_id", "audio_ts",
            "decision_ts", "exec_ts", "book_as_of_ts", "request_sha256",
            "decision_plan_sha1",
        },
        {"best_ask_cents"},
    ),
    "send_requested": (
        {"client_order_id", "market_ticker", "requested_ts"}, set()
    ),
    "not_attempted": (
        {"client_order_id", "market_ticker", "refusal_ts", "reason"}, set()
    ),
    "invocation_prepared": (
        {
            "client_order_id", "market_ticker", "protocol", "endpoint_id",
            "prepared_ts", "raw",
        },
        set(),
    ),
    "wire_attempted": (
        {
            "client_order_id", "market_ticker", "protocol", "endpoint_id",
            "send_ts",
        },
        set(),
    ),
    "exchange_observed": (
        {
            "client_order_id", "market_ticker", "protocol", "endpoint_id",
            "observed_ts", "protocol_status", "exchange_accepted",
            "order_terminal", "raw",
        },
        {
            "echoed_client_order_id", "order_id", "order_status",
            "exchange_event_ts", "fill", "parsed", "detail",
        },
    ),
    "transport_failed": (
        {
            "client_order_id", "market_ticker", "protocol", "endpoint_id",
            "failure_ts", "detail",
        },
        set(),
    ),
    "reconciled": (
        {
            "client_order_id", "market_ticker", "protocol", "endpoint_id",
            "observed_ts", "queried_client_order_id",
            "echoed_client_order_id", "order_status", "exchange_accepted",
            "order_terminal", "raw",
        },
        {"order_id", "exchange_event_ts", "fill", "parsed"},
    ),
    "reconcile_unresolved": (
        {
            "client_order_id", "market_ticker", "protocol", "endpoint_id",
            "observed_ts", "queried_client_order_id", "detail",
        },
        {"raw", "parsed"},
    ),
}

HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
UNSIGNED_FP = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
PROCESS_INSTANCE_RE = re.compile(r"^[A-Za-z0-9._-]+$")

FIX_EVENT_FIELDS = {
    "schema_version",
    "tape_seq",
    "wall_unix_ns",
    "monotonic_ns",
    "process_instance_id",
    "run_id",
    "event_id",
    "kalshi_env",
    "account_fingerprint",
    "plan_sha256",
    "endpoint_id",
    "previous_process_instance_id",
    "previous_record_sha256",
    "record",
    "record_sha256",
}
FIX_OBSERVATION_REQUIRED_FIELDS = {
    "source_sequence",
    "generation",
    "receipt_id",
    "frame_key",
    "kind",
    "fix_msg_type",
    "fix_msg_seq_num",
    "recv_monotonic_ns",
    "ack_monotonic_ns",
    "raw",
}
FIX_OBSERVATION_OPTIONAL_FIELDS = {
    "duplicate_of_tape_seq",
    "fix_sending_time",
    "execution",
    "reject",
}
FIX_CLOSE_FIELDS = {
    "type",
    "reason",
    "close_requested",
    "producer_quiesced",
    "terminal_accounting_complete",
    "session_complete",
    "tape_complete",
    "accepted_deliveries",
    "durably_accounted_deliveries",
    "observation_rows",
    "idempotent_retries",
    "execution_rows",
    "reject_rows",
    "correlated_rows",
    "orphan_rows",
    "duplicate_frames",
    "dropped_observations",
    "unwritten_observations",
    "prior_incomplete_sessions",
}
FIX_COMMIT_FIELDS = {
    "type",
    "schema_version",
    "process_instance_id",
    "close_tape_seq",
    "close_record_sha256",
    "tape_bytes",
    "tape_sha256",
    "session_complete",
    "tape_complete",
    "durable",
}
FIX_REPORT_SUMMARY_FIELDS = {
    "complete",
    "committed",
    "reason",
    "process_instance_id",
    "accepted_deliveries",
    "durably_accounted_deliveries",
    "observation_rows",
    "idempotent_retries",
    "execution_rows",
    "reject_rows",
    "correlated_rows",
    "orphan_rows",
    "duplicate_frames",
    "dropped_observations",
    "unwritten_observations",
    "prior_incomplete_sessions",
    "producer_quiesced",
    "terminal_accounting_complete",
    "close_record_sha256",
    "commit_path",
    "writer_error",
}


class AuditError(Exception):
    pass


def fail(message: str) -> None:
    raise AuditError(message)


def canonical_bytes(value: Any, *, ascii_only: bool = True) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=ascii_only,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fingerprint(path: Path) -> dict[str, Any]:
    regular_file(path, "evidence artifact")
    raw = path.read_bytes()
    return {"bytes": len(raw), "sha256": sha256_bytes(raw)}


def strict_json(raw: bytes, what: str) -> Any:
    def bad_constant(value: str) -> None:
        fail(f"{what}: invalid JSON numeric constant {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, member in pairs:
            if key in value:
                fail(f"{what}: duplicate JSON field {key!r}")
            value[key] = member
        return value

    try:
        return json.loads(
            raw,
            parse_constant=bad_constant,
            object_pairs_hook=unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"{what}: invalid JSON: {exc}")


def exact_fields(
    value: Any, required: set[str], optional: set[str], what: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"{what}: expected an object")
    fields = set(value)
    if not required <= fields or fields - required - optional:
        fail(
            f"{what}: fields differ (missing={sorted(required-fields)}, "
            f"unknown={sorted(fields-required-optional)})"
        )
    return value


def integer(value: Any, what: str, *, positive: bool = False) -> int:
    if (
        type(value) is not int
        or value < 0
        or value > 2**64 - 1
        or (positive and value == 0)
    ):
        fail(f"{what}: expected {'positive ' if positive else ''}u64")
    return value


def boolean(value: Any, what: str) -> bool:
    if type(value) is not bool:
        fail(f"{what}: expected boolean")
    return value


def nonempty(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(
        ord(char) < 32 for char in value
    ):
        fail(f"{what}: expected a non-empty string without control characters")
    return value


def lower_hex(value: Any, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        fail(f"{what}: invalid lowercase digest")
    return value


def regular_file(path: Path, what: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        fail(f"{what} is missing: {path}")
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        fail(f"{what} must be a regular non-symlink file: {path}")


def safe_artifact(run_dir: Path, plan: dict[str, Any], name: str) -> Path:
    raw = plan["artifacts"][name]
    if not isinstance(raw, str):
        fail(f"RunPlan artifacts.{name} is not a string")
    relative = Path(raw)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        fail(f"RunPlan artifacts.{name} is not a safe run-relative path")
    candidate = run_dir.joinpath(relative)
    # Existing evidence must not escape through a symlinked parent.  The grade
    # itself may not exist yet, so validate its existing parent instead.
    check = candidate if candidate.exists() else candidate.parent
    try:
        resolved = check.resolve(strict=True)
        resolved.relative_to(run_dir)
    except (FileNotFoundError, ValueError):
        fail(f"RunPlan artifacts.{name} escapes or lacks its run directory")
    return candidate


def one_json_line(path: Path, what: str) -> dict[str, Any]:
    regular_file(path, what)
    raw = path.read_bytes()
    if not raw or not raw.endswith(b"\n") or b"\n" in raw[:-1]:
        fail(f"{what} must be exactly one newline-terminated JSON row")
    value = strict_json(raw[:-1], what)
    if not isinstance(value, dict):
        fail(f"{what} is not an object")
    return value


def path_has_suffix(stored: Any, relative: str, what: str) -> Path:
    if not isinstance(stored, str) or not stored:
        fail(f"{what}: stored path is missing")
    path = Path(stored)
    suffix = Path(relative).parts
    if len(path.parts) < len(suffix) or path.parts[-len(suffix):] != suffix:
        fail(f"{what}: stored path is not bound to {relative}")
    return path


def verify_eventctl(run_dir: Path) -> None:
    eventctl = Path(__file__).with_name("eventctl.py")
    regular_file(eventctl, "public eventctl door")
    result = subprocess.run(
        [sys.executable, str(eventctl), "verify", "--run-dir", str(run_dir)],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()
        fail(f"eventctl verify refused the run ({result.returncode}): {detail}")


def load_plan(run_dir: Path) -> dict[str, Any]:
    plan_path = run_dir / "control" / "run-plan.json"
    regular_file(plan_path, "RunPlan")
    plan = strict_json(plan_path.read_bytes(), "RunPlan")
    if not isinstance(plan, dict):
        fail("RunPlan is not an object")
    if plan.get("schema") != PLAN_SCHEMA or plan.get("kind") != "run-plan":
        fail("run-audit supports only exact schema-3 RunPlans")
    if set(plan.get("artifacts", {})) != PLAN_ARTIFACT_KEYS:
        fail("RunPlan artifact keys are not exact schema 3")
    if run_dir.name != plan.get("run_id") or run_dir.parent.name != plan.get("event_id"):
        fail("RunPlan event/run identity differs from its directory")
    return plan


def raw_evidence(value: Any, what: str) -> bytes:
    row = exact_fields(
        value,
        {"media_type", "bytes_base64", "bytes_len", "bytes_sha256"},
        set(),
        what,
    )
    nonempty(row["media_type"], f"{what}.media_type")
    if not isinstance(row["bytes_base64"], str):
        fail(f"{what}.bytes_base64 is not a string")
    try:
        decoded = base64.b64decode(row["bytes_base64"], validate=True)
    except Exception as exc:
        fail(f"{what}.bytes_base64 is invalid: {exc}")
    if base64.b64encode(decoded).decode("ascii") != row["bytes_base64"]:
        fail(f"{what}.bytes_base64 is not canonical standard base64")
    if integer(row["bytes_len"], f"{what}.bytes_len") != len(decoded):
        fail(f"{what}: raw byte count differs")
    if lower_hex(row["bytes_sha256"], HEX64, f"{what}.bytes_sha256") != sha256_bytes(decoded):
        fail(f"{what}: raw SHA-256 differs")
    return decoded


def execution_transport(run_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    repo = Path(
        os.environ.get("EARSHOT_REPO", Path(__file__).resolve().parent.parent)
    ).resolve()
    relative = Path(plan.get("config_path", ""))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        fail("RunPlan config_path is not a safe repository-relative path")
    config_path = (repo / relative).resolve()
    try:
        config_path.relative_to(repo)
    except ValueError:
        fail("RunPlan config_path escapes the repository")
    regular_file(config_path, "pinned event config")
    if sha256_bytes(config_path.read_bytes()) != plan.get("config_sha256"):
        fail("pinned event config SHA-256 differs from RunPlan")
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        fail(f"pinned event config is invalid TOML: {exc}")
    kalshi = config.get("kalshi", {})
    if not isinstance(kalshi, dict):
        fail("pinned event config [kalshi] is not a table")
    transport = kalshi.get("transport", "rest")
    if transport not in ("rest", "fix"):
        fail(f"pinned event config has unsupported Kalshi transport {transport!r}")
    value: dict[str, Any] = {"kind": transport}
    if transport == "fix":
        fix = kalshi.get("fix", {})
        if not isinstance(fix, dict):
            fail("pinned event config [kalshi.fix] is not a table")
        host = nonempty(
            fix.get("host", "mm.fix.elections.kalshi.com"),
            "pinned FIX host",
        )
        port = integer(fix.get("port", 8228), "pinned FIX port", positive=True)
        if port > 65535:
            fail("pinned FIX port is outside 1..=65535")
        target = nonempty(
            fix.get("target_comp_id", "KalshiNR"), "pinned FIX target CompID"
        )
        sender = fix.get("fix_key_id", kalshi.get("key_id"))
        if sender is not None:
            sender = nonempty(sender, "pinned FIX sender CompID")
        value.update({"host": host, "port": port, "target": target, "sender": sender})
    return value


def parse_fix_frame(raw: bytes, what: str) -> dict[int, bytes]:
    if not raw or not raw.endswith(b"\x01"):
        fail(f"{what}: FIX frame is empty or lacks terminal SOH")
    parts = raw.split(b"\x01")
    if parts[-1] != b"":
        fail(f"{what}: FIX frame is not exactly SOH terminated")
    fields = parts[:-1]
    if len(fields) < 3:
        fail(f"{what}: FIX frame is truncated")
    parsed: list[tuple[int, bytes]] = []
    for index, field in enumerate(fields):
        tag_raw, separator, value = field.partition(b"=")
        if (
            not separator
            or not tag_raw.isdigit()
            or len(tag_raw) > 10
            or int(tag_raw) > 2**32 - 1
        ):
            fail(f"{what}: malformed FIX field {index + 1}")
        parsed.append((int(tag_raw), value))
    if (
        fields[0] != b"8=FIXT.1.1"
        or not fields[1].startswith(b"9=")
        or not fields[-1].startswith(b"10=")
        or parsed[0][0] != 8
        or parsed[1][0] != 9
        or parsed[-1][0] != 10
    ):
        fail(f"{what}: FIX BeginString/BodyLength/CheckSum framing is invalid")
    body_length_raw = parsed[1][1]
    checksum_raw = parsed[-1][1]
    if not body_length_raw.isdigit() or not checksum_raw.isdigit():
        fail(f"{what}: FIX BodyLength/CheckSum is not numeric")
    if len(body_length_raw) > 20:
        fail(f"{what}: FIX BodyLength is outside u64")
    if len(checksum_raw) != 3:
        fail(f"{what}: FIX CheckSum is not exactly three decimal digits")
    declared_length = int(body_length_raw)
    declared_checksum = int(checksum_raw)
    if declared_length > 2**64 - 1:
        fail(f"{what}: FIX BodyLength is outside u64")
    if not 0 <= declared_checksum <= 255:
        fail(f"{what}: FIX CheckSum is not exactly three decimal digits")
    first_soh = raw.find(b"\x01")
    second_soh = raw.find(b"\x01", first_soh + 1)
    checksum_at = len(raw) - len(fields[-1]) - 1
    measured_length = checksum_at - (second_soh + 1)
    if first_soh < 0 or second_soh < 0 or declared_length != measured_length:
        fail(
            f"{what}: FIX BodyLength says {declared_length}, measured {measured_length}"
        )
    computed_checksum = sum(raw[:checksum_at]) % 256
    if declared_checksum != computed_checksum:
        fail(
            f"{what}: FIX CheckSum says {declared_checksum:03}, "
            f"computed {computed_checksum:03}"
        )
    first: dict[int, bytes] = {}
    for tag, value in parsed:
        first.setdefault(tag, value)
    if 35 not in first or 34 not in first:
        fail(f"{what}: FIX frame lacks MsgType or MsgSeqNum")
    if not first[34].isdigit() or len(first[34]) > 20:
        fail(f"{what}: FIX MsgSeqNum is not u64")
    if int(first[34]) > 2**64 - 1:
        fail(f"{what}: FIX MsgSeqNum is not u64")
    return first


def fix_text(tags: dict[int, bytes], tag: int, what: str) -> str | None:
    raw = tags.get(tag)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # FixMsg::get_str treats a present non-UTF-8 optional value as absent.
        # MsgType is separately required below, while projection fields must
        # reproduce this exact Rust parser behavior.
        return None


def fix_u64(tags: dict[int, bytes], tag: int, what: str) -> int | None:
    raw = tags.get(tag)
    if raw is None:
        return None
    if not raw.isdigit() or len(raw) > 20:
        fail(f"{what}: FIX tag {tag} is not u64")
    value = int(raw)
    if value > 2**64 - 1:
        fail(f"{what}: FIX tag {tag} is not u64")
    return value


def fix_hash_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value, ascii_only=False))


def validate_fix_observation(
    value: Any,
    *,
    where: str,
    process_instance_id: str,
    endpoint_id: str,
    expected_source_sequence: int,
    first_frames: dict[str, int],
) -> dict[str, Any]:
    observation = exact_fields(
        value,
        FIX_OBSERVATION_REQUIRED_FIELDS,
        FIX_OBSERVATION_OPTIONAL_FIELDS,
        where,
    )
    source_sequence = integer(
        observation["source_sequence"], f"{where}.source_sequence", positive=True
    )
    if source_sequence != expected_source_sequence:
        fail(
            f"{where}: source sequence {source_sequence} != "
            f"{expected_source_sequence}"
        )
    generation = integer(
        observation["generation"], f"{where}.generation", positive=True
    )
    recv = integer(
        observation["recv_monotonic_ns"],
        f"{where}.recv_monotonic_ns",
        positive=True,
    )
    ack = integer(
        observation["ack_monotonic_ns"],
        f"{where}.ack_monotonic_ns",
        positive=True,
    )
    if ack < recv:
        fail(f"{where}: acknowledgement monotonic time precedes receive")
    raw_object = exact_fields(
        observation["raw"],
        {"media_type", "bytes_base64", "bytes_len", "bytes_sha256"},
        set(),
        f"{where}.raw",
    )
    if raw_object["media_type"] != FIX_RAW_MEDIA_TYPE:
        fail(f"{where}: raw media type is not exact FIXT.1.1")
    raw = raw_evidence(raw_object, f"{where}.raw")
    tags = parse_fix_frame(raw, f"{where}.raw")
    msg_type = fix_text(tags, 35, where)
    msg_seq_num = fix_u64(tags, 34, where)
    sending_time = fix_text(tags, 52, where)
    if observation["fix_msg_type"] != msg_type:
        fail(f"{where}: FIX MsgType projection differs from raw frame")
    if integer(observation["fix_msg_seq_num"], f"{where}.fix_msg_seq_num") != msg_seq_num:
        fail(f"{where}: FIX MsgSeqNum projection differs from raw frame")
    if sending_time is None:
        if "fix_sending_time" in observation:
            fail(f"{where}: absent raw SendingTime was projected")
    elif observation.get("fix_sending_time") != sending_time:
        fail(f"{where}: FIX SendingTime projection differs from raw frame")

    raw_sha = lower_hex(raw_object["bytes_sha256"], HEX64, f"{where}.raw.bytes_sha256")
    receipt = fix_hash_json(
        [process_instance_id, source_sequence, generation, raw_sha]
    )
    frame_key = fix_hash_json([endpoint_id, raw_sha])
    if lower_hex(observation["receipt_id"], HEX64, f"{where}.receipt_id") != receipt:
        fail(f"{where}: FIX delivery receipt identity hash mismatch")
    if lower_hex(observation["frame_key"], HEX64, f"{where}.frame_key") != frame_key:
        fail(f"{where}: FIX frame identity hash mismatch")
    expected_duplicate = first_frames.get(frame_key)
    if expected_duplicate is None:
        if "duplicate_of_tape_seq" in observation:
            fail(f"{where}: first frame carries a duplicate pointer")
    elif integer(
        observation.get("duplicate_of_tape_seq"),
        f"{where}.duplicate_of_tape_seq",
    ) != expected_duplicate:
        fail(f"{where}: duplicate pointer does not identify first exact frame")

    kind = observation["kind"]
    client_order_id: str | None
    if kind == "execution_report":
        if msg_type != "8" or "reject" in observation or "execution" not in observation:
            fail(f"{where}: execution kind/projection/raw MsgType disagree")
        projection = exact_fields(
            observation["execution"],
            {"client_order_id", "order_id", "status", "terminal", "parsed_fields"},
            set(),
            f"{where}.execution",
        )
        boolean(projection["terminal"], f"{where}.execution.terminal")
        exec_type_raw = tags.get(150)
        ord_status_raw = tags.get(39)
        if not exec_type_raw or not ord_status_raw:
            fail(f"{where}: execution raw frame lacks one-byte ExecType/OrdStatus")
        exec_type = chr(exec_type_raw[0])
        ord_status = chr(ord_status_raw[0])
        status = {
            "0": "new",
            "1": "partial",
            "2": "filled",
            "4": "canceled",
            "8": "rejected",
            "C": "expired",
        }.get(ord_status, "unknown")
        terminal = ord_status in {"2", "4", "8", "C"}
        parsed_fields = {
            "exec_type": exec_type,
            "ord_status": ord_status,
            "order_id": fix_text(tags, 37, where),
            "client_order_id": fix_text(tags, 11, where),
            "cum_qty": fix_text(tags, 14, where),
            "leaves_qty": fix_text(tags, 151, where),
            "last_px": fix_text(tags, 31, where),
            "last_qty": fix_text(tags, 32, where),
            "avg_px": fix_text(tags, 6, where),
            "ord_rej_reason": fix_text(tags, 103, where),
            "text": fix_text(tags, 58, where),
        }
        expected_projection = {
            "client_order_id": parsed_fields["client_order_id"],
            "order_id": parsed_fields["order_id"],
            "status": status,
            "terminal": terminal,
            "parsed_fields": parsed_fields,
        }
        if projection != expected_projection:
            fail(f"{where}: execution projection differs from exact raw frame")
        client_order_id = projection["client_order_id"]
    elif kind in ("session_reject", "business_reject"):
        expected_type = "3" if kind == "session_reject" else "j"
        if msg_type != expected_type or "execution" in observation or "reject" not in observation:
            fail(f"{where}: reject kind/projection/raw MsgType disagree")
        projection = exact_fields(
            observation["reject"],
            {
                "client_order_id",
                "ref_seq",
                "ref_msg_type",
                "business_ref_id",
                "reason",
                "text",
            },
            set(),
            f"{where}.reject",
        )
        if projection["ref_seq"] is not None:
            integer(projection["ref_seq"], f"{where}.reject.ref_seq")
        expected_fields = {
            "ref_seq": fix_u64(tags, 45, where),
            "ref_msg_type": fix_text(tags, 372, where),
            "business_ref_id": fix_text(tags, 379, where),
            "reason": fix_text(tags, 373 if kind == "session_reject" else 380, where),
            "text": fix_text(tags, 58, where),
        }
        for field, expected in expected_fields.items():
            if projection[field] != expected:
                fail(f"{where}: reject {field} projection differs from exact raw frame")
        client_order_id = projection["client_order_id"]
        if client_order_id is not None:
            nonempty(client_order_id, f"{where}.reject.client_order_id")
    else:
        fail(f"{where}: unsupported FIX observation kind {kind!r}")
    return {
        "source_sequence": source_sequence,
        "kind": kind,
        "client_order_id": client_order_id,
        "receipt_id": receipt,
        "frame_key": frame_key,
        "duplicate": expected_duplicate is not None,
    }


def validate_fix_endpoint(endpoint_id: str, transport: dict[str, Any]) -> None:
    endpoint_id = nonempty(endpoint_id, "FIX tape endpoint_id")
    for field in ("host", "target"):
        if "|" in transport[field]:
            fail(f"pinned FIX {field} contains reserved endpoint delimiter")
    prefix = f"tls://{transport['host']}:{transport['port']}|sender="
    suffix = f"|target={transport['target']}"
    if not endpoint_id.startswith(prefix) or not endpoint_id.endswith(suffix):
        fail("FIX tape endpoint identity differs from pinned FIX configuration")
    sender = endpoint_id[len(prefix) : len(endpoint_id) - len(suffix)]
    nonempty(sender, "FIX tape sender CompID")
    if "|" in sender:
        fail("FIX tape sender CompID contains reserved endpoint delimiter")
    if transport.get("sender") is not None and sender != transport["sender"]:
        fail("FIX tape sender CompID differs from pinned FIX configuration")


def fix_commit_path(tape_path: Path, process_instance_id: str) -> Path:
    return Path(str(tape_path) + f".commit.{process_instance_id}.json")


def validate_fix_commit(
    tape_path: Path,
    tape_prefix: bytes,
    close: dict[str, Any],
    process_instance_id: str,
    close_tape_seq: int,
    close_record_sha256: str,
) -> tuple[Path, dict[str, Any]]:
    path = fix_commit_path(tape_path, process_instance_id)
    commit = exact_fields(
        one_json_line(path, f"FIX observation commit {process_instance_id}"),
        FIX_COMMIT_FIELDS,
        set(),
        f"FIX observation commit {process_instance_id}",
    )
    what = f"FIX observation commit {process_instance_id}"
    if commit["type"] != "fix_observation_tape_commit":
        fail(f"{what}: wrong receipt type")
    if integer(commit["schema_version"], f"{what}.schema_version") != FIX_OBSERVATION_PROTOCOL:
        fail(f"{what}: unsupported schema_version")
    commit_process = nonempty(commit["process_instance_id"], f"{what}.process_instance_id")
    if not PROCESS_INSTANCE_RE.fullmatch(commit_process):
        fail(f"{what}: process_instance_id contains unsafe characters")
    integer(commit["close_tape_seq"], f"{what}.close_tape_seq")
    lower_hex(commit["close_record_sha256"], HEX64, f"{what}.close_record_sha256")
    integer(commit["tape_bytes"], f"{what}.tape_bytes")
    lower_hex(commit["tape_sha256"], HEX64, f"{what}.tape_sha256")
    boolean(commit["session_complete"], f"{what}.session_complete")
    boolean(commit["tape_complete"], f"{what}.tape_complete")
    boolean(commit["durable"], f"{what}.durable")
    expected = {
        "type": "fix_observation_tape_commit",
        "schema_version": FIX_OBSERVATION_PROTOCOL,
        "process_instance_id": process_instance_id,
        "close_tape_seq": close_tape_seq,
        "close_record_sha256": close_record_sha256,
        "tape_bytes": len(tape_prefix),
        "tape_sha256": sha256_bytes(tape_prefix),
        "session_complete": close["session_complete"],
        "tape_complete": close["tape_complete"],
        "durable": True,
    }
    if commit != expected:
        fail(
            f"FIX observation commit {process_instance_id} does not bind "
            "the exact tape prefix and close record"
        )
    return path, commit


def verify_fix_observation_tape(
    tape_path: Path,
    plan: dict[str, Any],
    transport: dict[str, Any],
) -> dict[str, Any]:
    regular_file(tape_path, "FIX observation tape")
    tape_raw = tape_path.read_bytes()
    if not tape_raw or not tape_raw.endswith(b"\n"):
        fail("FIX observation tape is empty or has a truncated final line")
    lines = tape_raw[:-1].split(b"\n")
    if any(not line for line in lines):
        fail("FIX observation tape contains a blank row")

    previous_hash = plan["plan_sha256"]
    previous_process = ""
    active: dict[str, Any] | None = None
    seen_processes: set[str] = set()
    seen_receipts: set[str] = set()
    first_frames: dict[str, int] = {}
    prior_incomplete = 0
    session_count = 0
    observation_rows_total = 0
    endpoint_id: str | None = None
    offset = 0
    commits: list[dict[str, Any]] = []
    expected_commit_paths: set[Path] = set()
    last_report_summary: dict[str, Any] | None = None

    for index, line in enumerate(lines):
        line_number = index + 1
        chunk = line + b"\n"
        offset += len(chunk)
        event = exact_fields(
            strict_json(line, f"FIX observation tape line {line_number}"),
            FIX_EVENT_FIELDS,
            set(),
            f"FIX observation tape line {line_number}",
        )
        where = f"FIX observation tape line {line_number}"
        if integer(event["schema_version"], f"{where}.schema_version") != FIX_OBSERVATION_PROTOCOL:
            fail(f"{where}: unsupported schema_version")
        if integer(event["tape_seq"], f"{where}.tape_seq") != index:
            fail(f"{where}: tape sequence is not contiguous from zero")
        integer(event["wall_unix_ns"], f"{where}.wall_unix_ns", positive=True)
        integer(event["monotonic_ns"], f"{where}.monotonic_ns", positive=True)
        process = nonempty(event["process_instance_id"], f"{where}.process_instance_id")
        if not PROCESS_INSTANCE_RE.fullmatch(process):
            fail(f"{where}: process_instance_id contains unsafe characters")
        for field, expected in (
            ("run_id", plan["run_id"]),
            ("event_id", plan["event_id"]),
            ("kalshi_env", plan["kalshi_env"]),
            ("account_fingerprint", plan["account_fingerprint"]),
            ("plan_sha256", plan["plan_sha256"]),
        ):
            if event[field] != expected:
                fail(f"{where}: immutable {field} differs from RunPlan")
        current_endpoint = nonempty(event["endpoint_id"], f"{where}.endpoint_id")
        if endpoint_id is None:
            validate_fix_endpoint(current_endpoint, transport)
            endpoint_id = current_endpoint
        elif current_endpoint != endpoint_id:
            fail(f"{where}: immutable FIX endpoint identity changed")
        if event["previous_process_instance_id"] != previous_process:
            fail(f"{where}: previous process instance mismatch")
        if event["previous_record_sha256"] != previous_hash:
            fail(f"{where}: previous-record hash chain mismatch")
        claimed = lower_hex(event["record_sha256"], HEX64, f"{where}.record_sha256")
        unhashed = dict(event)
        del unhashed["record_sha256"]
        if claimed != sha256_bytes(canonical_bytes(unhashed, ascii_only=False)):
            fail(f"{where}: record SHA-256 mismatch")

        record = event["record"]
        if not isinstance(record, dict):
            fail(f"{where}.record is not an object")
        record_type = record.get("type")
        if record_type == "session_started":
            start = exact_fields(
                record,
                {"type"},
                {"recovered_unclean_process_instance_id"},
                f"{where}.record",
            )
            expected_recovery = None if active is None else active["process_instance_id"]
            if expected_recovery is None:
                if "recovered_unclean_process_instance_id" in start:
                    fail(f"{where}: clean session start carries a recovery claim")
            elif start.get("recovered_unclean_process_instance_id") != expected_recovery:
                fail(f"{where}: session start does not name the exact unclean predecessor")
            if active is not None:
                prior_incomplete += 1
            if process in seen_processes:
                fail(f"{where}: process_instance_id was reused")
            seen_processes.add(process)
            active = {
                "process_instance_id": process,
                "next_source_sequence": 1,
                "observation_rows": 0,
                "execution_rows": 0,
                "reject_rows": 0,
                "correlated_rows": 0,
                "orphan_rows": 0,
                "duplicate_frames": 0,
            }
            session_count += 1
        elif record_type == "observation":
            exact_fields(record, {"type", "observation"}, set(), f"{where}.record")
            if active is None:
                fail(f"{where}: observation appears outside a tape session")
            if process != active["process_instance_id"]:
                fail(f"{where}: observation process differs from active session")
            observation = validate_fix_observation(
                record["observation"],
                where=f"{where}.observation",
                process_instance_id=process,
                endpoint_id=endpoint_id,
                expected_source_sequence=active["next_source_sequence"],
                first_frames=first_frames,
            )
            if observation["receipt_id"] in seen_receipts:
                fail(f"{where}: duplicate delivery receipt was appended instead of deduplicated")
            seen_receipts.add(observation["receipt_id"])
            first_frames.setdefault(observation["frame_key"], index)
            active["next_source_sequence"] += 1
            active["observation_rows"] += 1
            observation_rows_total += 1
            if observation["kind"] == "execution_report":
                active["execution_rows"] += 1
            else:
                active["reject_rows"] += 1
            if observation["client_order_id"] is None:
                active["orphan_rows"] += 1
            else:
                active["correlated_rows"] += 1
            if observation["duplicate"]:
                active["duplicate_frames"] += 1
        elif record_type == "session_closed":
            close = exact_fields(record, FIX_CLOSE_FIELDS, set(), f"{where}.record")
            if active is None:
                fail(f"{where}: session close appears without an active session")
            if process != active["process_instance_id"]:
                fail(f"{where}: close process differs from active session")
            nonempty(close["reason"], f"{where}.reason")
            for field in (
                "close_requested",
                "producer_quiesced",
                "terminal_accounting_complete",
                "session_complete",
                "tape_complete",
            ):
                boolean(close[field], f"{where}.{field}")
            idempotent_retries = integer(
                close["idempotent_retries"], f"{where}.idempotent_retries"
            )
            expected_counts = {
                "observation_rows": active["observation_rows"],
                # Idempotent offers do not append rows and therefore cannot be
                # reconstructed from a persisted tape.  The Rust replay
                # reducer consequently admits only zero here; mirror that
                # fail-closed rule instead of trusting the close declaration.
                "idempotent_retries": 0,
                "execution_rows": active["execution_rows"],
                "reject_rows": active["reject_rows"],
                "correlated_rows": active["correlated_rows"],
                "orphan_rows": active["orphan_rows"],
                "duplicate_frames": active["duplicate_frames"],
                "durably_accounted_deliveries": active["observation_rows"],
                "prior_incomplete_sessions": prior_incomplete,
            }
            for field, expected in expected_counts.items():
                if integer(close[field], f"{where}.{field}") != expected:
                    fail(f"{where}: close {field} differs from replay ({expected})")
            accepted = integer(
                close["accepted_deliveries"], f"{where}.accepted_deliveries"
            )
            accounted = integer(
                close["durably_accounted_deliveries"],
                f"{where}.durably_accounted_deliveries",
            )
            dropped = integer(
                close["dropped_observations"], f"{where}.dropped_observations"
            )
            unwritten = integer(
                close["unwritten_observations"], f"{where}.unwritten_observations"
            )
            if accepted != active["observation_rows"] + idempotent_retries:
                fail(f"{where}: accepted deliveries do not equal observations plus retries")
            if accounted != accepted:
                fail(f"{where}: accepted FIX observations are not durably accounted")
            terminal_accounting = close["producer_quiesced"] and unwritten == 0
            if close["terminal_accounting_complete"] != terminal_accounting:
                fail(f"{where}: terminal accounting completeness is inconsistent")
            session_complete = (
                close["close_requested"] and terminal_accounting and dropped == 0
            )
            if close["session_complete"] != session_complete:
                fail(f"{where}: session completeness is inconsistent")
            tape_complete = session_complete and prior_incomplete == 0
            if close["tape_complete"] != tape_complete:
                fail(f"{where}: whole-tape completeness is inconsistent")

            commit_path, commit = validate_fix_commit(
                tape_path,
                tape_raw[:offset],
                close,
                process,
                index,
                claimed,
            )
            expected_commit_paths.add(commit_path)
            commits.append(
                {
                    "artifact": commit_path.name,
                    "process_instance_id": process,
                    "close_tape_seq": index,
                    "close_record_sha256": claimed,
                    "tape_bytes": commit["tape_bytes"],
                    "tape_sha256": commit["tape_sha256"],
                    "receipt_sha256": fingerprint(commit_path)["sha256"],
                }
            )
            last_report_summary = {
                "complete": True,
                "committed": True,
                "reason": close["reason"],
                "process_instance_id": process,
                "accepted_deliveries": accepted,
                "durably_accounted_deliveries": accounted,
                "observation_rows": active["observation_rows"],
                "idempotent_retries": idempotent_retries,
                "execution_rows": active["execution_rows"],
                "reject_rows": active["reject_rows"],
                "correlated_rows": active["correlated_rows"],
                "orphan_rows": active["orphan_rows"],
                "duplicate_frames": active["duplicate_frames"],
                "dropped_observations": dropped,
                "unwritten_observations": unwritten,
                "prior_incomplete_sessions": prior_incomplete,
                "producer_quiesced": close["producer_quiesced"],
                "terminal_accounting_complete": terminal_accounting,
                "close_record_sha256": claimed,
                "commit_path_relative": commit_path.name,
                "writer_error": None,
            }
            if not session_complete:
                prior_incomplete += 1
            active = None
        else:
            fail(f"{where}: unsupported record type {record_type!r}")

        previous_hash = claimed
        previous_process = process

    if session_count == 0:
        fail("FIX observation tape has no session-start record")
    if active is not None:
        fail("FIX observation tape ends with an unclosed process session")
    if prior_incomplete != 0 or last_report_summary is None:
        fail("FIX observation tape contains an incomplete process session")
    if not last_report_summary["complete"]:
        fail("FIX observation tape final session is incomplete")
    family_prefix = tape_path.name + ".commit."
    actual_commit_paths = {
        entry
        for entry in tape_path.parent.iterdir()
        if entry.name.startswith(family_prefix) and entry.name.endswith(".json")
    }
    if actual_commit_paths != expected_commit_paths:
        fail("FIX observation commit receipt set differs from closed tape sessions")
    latest_session = dict(last_report_summary)
    commit_relative = latest_session.pop("commit_path_relative")
    latest_session["commit_artifact"] = str(
        Path(plan["artifacts"]["fix_observation_tape"]).with_name(commit_relative)
    )
    return {
        "status": "committed",
        "artifact": plan["artifacts"]["fix_observation_tape"],
        "protocol": FIX_OBSERVATION_PROTOCOL,
        "endpoint_id": endpoint_id,
        "tape_bytes": len(tape_raw),
        "tape_sha256": sha256_bytes(tape_raw),
        "record_count": len(lines),
        "observation_rows": observation_rows_total,
        "session_count": session_count,
        "incomplete_sessions": 0,
        "final_record_sha256": previous_hash,
        "session_commits": commits,
        "latest_session": latest_session,
        "report_summary": last_report_summary,
    }


def verify_fix_evidence(
    run_dir: Path,
    plan: dict[str, Any],
    transport: dict[str, Any],
) -> dict[str, Any]:
    tape_path = safe_artifact(run_dir, plan, "fix_observation_tape")
    if transport["kind"] == "fix":
        return verify_fix_observation_tape(tape_path, plan, transport)
    family_prefix = tape_path.name + ".commit."
    stray = []
    if os.path.lexists(tape_path):
        stray.append(tape_path.name)
    stray.extend(
        entry.name
        for entry in tape_path.parent.iterdir()
        if entry.name.startswith(family_prefix)
    )
    if stray:
        fail(
            "REST run contains contradictory FIX observation tape/commit evidence: "
            + ", ".join(sorted(set(stray)))
        )
    return {
        "status": "not_applicable",
        "artifact": plan["artifacts"]["fix_observation_tape"],
        "protocol": None,
        "endpoint_id": None,
        "tape_bytes": None,
        "tape_sha256": None,
        "record_count": 0,
        "observation_rows": 0,
        "session_count": 0,
        "incomplete_sessions": 0,
        "final_record_sha256": None,
        "session_commits": [],
        "latest_session": None,
    }


def validate_fix_report(
    report: dict[str, Any],
    plan: dict[str, Any],
    transport: dict[str, Any],
    verification: dict[str, Any],
) -> None:
    path_has_suffix(
        report.get("fix_observation_tape_path"),
        plan["artifacts"]["fix_observation_tape"],
        "runtime report FIX observation tape path",
    )
    for field in (
        "fix_observation_evidence_healthy",
        "fix_observation_tape_healthy",
        "fix_observation_evidence",
    ):
        if field not in report:
            fail(f"runtime report lacks required {field}")
    health = report.get("fix_observation_evidence_healthy")
    tape_health = report.get("fix_observation_tape_healthy")
    evidence = report.get("fix_observation_evidence")
    if report.get("fix_observation_evidence_error") is not None:
        fail("runtime report carries a FIX observation evidence fault")
    if transport["kind"] == "rest":
        if health is not None or tape_health is not None:
            fail("REST runtime report must use null FIX evidence health")
        if evidence != {"status": "not_applicable"}:
            fail("REST runtime report must declare FIX evidence not_applicable")
        return
    if health is not True or tape_health is not True:
        fail("FIX runtime report does not claim healthy committed observation evidence")
    evidence = exact_fields(
        evidence,
        {"status", "summary"},
        set(),
        "runtime report FIX observation evidence",
    )
    if evidence["status"] != "committed":
        fail("FIX runtime report observation evidence status is not committed")
    summary = exact_fields(
        evidence["summary"],
        FIX_REPORT_SUMMARY_FIELDS,
        set(),
        "runtime report FIX observation summary",
    )
    summary_what = "runtime report FIX observation summary"
    boolean(summary["complete"], f"{summary_what}.complete")
    boolean(summary["committed"], f"{summary_what}.committed")
    nonempty(summary["reason"], f"{summary_what}.reason")
    summary_process = nonempty(
        summary["process_instance_id"], f"{summary_what}.process_instance_id"
    )
    if not PROCESS_INSTANCE_RE.fullmatch(summary_process):
        fail(f"{summary_what}.process_instance_id contains unsafe characters")
    for field in (
        "accepted_deliveries",
        "durably_accounted_deliveries",
        "observation_rows",
        "idempotent_retries",
        "execution_rows",
        "reject_rows",
        "correlated_rows",
        "orphan_rows",
        "duplicate_frames",
        "dropped_observations",
        "unwritten_observations",
        "prior_incomplete_sessions",
    ):
        integer(summary[field], f"{summary_what}.{field}")
    boolean(summary["producer_quiesced"], f"{summary_what}.producer_quiesced")
    boolean(
        summary["terminal_accounting_complete"],
        f"{summary_what}.terminal_accounting_complete",
    )
    lower_hex(
        summary["close_record_sha256"],
        HEX64,
        f"{summary_what}.close_record_sha256",
    )
    nonempty(summary["commit_path"], f"{summary_what}.commit_path")
    if summary["writer_error"] is not None:
        fail(f"{summary_what}.writer_error must be null for committed evidence")
    expected = dict(verification["report_summary"])
    commit_relative = expected.pop("commit_path_relative")
    for field, value in expected.items():
        if summary[field] != value:
            fail(f"runtime report FIX observation summary disagrees for {field}")
    path_has_suffix(
        summary["commit_path"],
        str(Path(plan["artifacts"]["fix_observation_tape"]).with_name(commit_relative)),
        "runtime report FIX observation commit path",
    )


def optional_string(row: dict[str, Any], field: str, what: str) -> str | None:
    value = row.get(field)
    if value is not None:
        nonempty(value, f"{what}.{field}")
    return value


def validate_fill(value: Any, what: str) -> dict[str, Any]:
    fields = {
        "filled_count_fp", "remaining_count_fp", "average_price_dollars",
        "latest_fill_count_fp", "latest_fill_price_dollars",
        "latest_fill_exchange_ts",
    }
    row = exact_fields(value, set(), fields, what)
    if not row:
        fail(f"{what}: fill has no exact exchange field")
    for field, value in row.items():
        if field == "latest_fill_exchange_ts":
            nonempty(value, f"{what}.{field}")
        elif not isinstance(value, str) or not UNSIGNED_FP.fullmatch(value):
            fail(f"{what}.{field}: expected unsigned fixed-point string")
    return row


def validate_receipt_shape(kind: str, value: Any, where: str) -> dict[str, Any]:
    if kind not in RECEIPT_FIELDS:
        fail(f"{where}: unknown order transition {kind!r}")
    required, optional = RECEIPT_FIELDS[kind]
    return exact_fields(value, required, optional, where)


def validate_intent(row: dict[str, Any], where: str) -> None:
    for field in ("client_order_id", "market_ticker", "keyword"):
        nonempty(row[field], f"{where}.{field}")
    if row["side"] not in ("yes", "no") or row["action"] not in ("buy", "sell"):
        fail(f"{where}: unsupported side/action")
    if row["time_in_force"] not in ("immediate_or_cancel", "good_till_canceled"):
        fail(f"{where}: unsupported time_in_force")
    if row["tier"] not in ("fast", "confirmed"):
        fail(f"{where}: unsupported tier")
    price = integer(row["price_cents"], f"{where}.price_cents", positive=True)
    if price > 99:
        fail(f"{where}.price_cents is outside 1..=99")
    integer(row["contract_count"], f"{where}.contract_count", positive=True)
    integer(row["keyword_id"], f"{where}.keyword_id")
    integer(row["source_id"], f"{where}.source_id")
    audio = integer(row["audio_ts"], f"{where}.audio_ts", positive=True)
    decision = integer(row["decision_ts"], f"{where}.decision_ts", positive=True)
    execute = integer(row["exec_ts"], f"{where}.exec_ts", positive=True)
    book = integer(row["book_as_of_ts"], f"{where}.book_as_of_ts")
    if not audio <= decision <= execute or book > execute:
        fail(f"{where}: intent timestamp ordering is illegal")
    if "best_ask_cents" in row:
        ask = integer(row["best_ask_cents"], f"{where}.best_ask_cents", positive=True)
        if not book or ask > 99:
            fail(f"{where}: best ask lacks a valid book timestamp/price")
    lower_hex(row["request_sha256"], HEX64, f"{where}.request_sha256")
    lower_hex(row["decision_plan_sha1"], HEX40, f"{where}.decision_plan_sha1")


def apply_observation(state: dict[str, Any], row: dict[str, Any], where: str) -> None:
    accepted = row["exchange_accepted"]
    if accepted is not None and type(accepted) is not bool:
        fail(f"{where}.exchange_accepted must be boolean or null")
    terminal = boolean(row["order_terminal"], f"{where}.order_terminal")
    order_id = optional_string(row, "order_id", where)
    if accepted is False and (not terminal or order_id is not None):
        fail(f"{where}: exchange rejection terminal semantics are invalid")
    if terminal and accepted is not False and order_id is None:
        fail(f"{where}: terminal non-rejection lacks an order id")
    known = state.get("order_id")
    if known is not None and order_id is not None and known != order_id:
        fail(f"{where}: exchange order id changed")
    if order_id is not None:
        state["order_id"] = order_id
    status = optional_string(row, "order_status", where)
    if status is not None:
        state["order_status"] = status
    if accepted is not None:
        state["exchange_accepted"] = accepted
    if row.get("fill") is not None:
        state["fills"].append(validate_fill(row["fill"], f"{where}.fill"))
    state["terminal"] = terminal
    state["last_observed_ts"] = row["observed_ts"]


def validate_order_evidence(
    path: Path, plan: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    regular_file(path, "order evidence")
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        fail("order evidence has a torn final line")
    lines = raw[:-1].split(b"\n") if raw else []
    if any(not line for line in lines):
        fail("order evidence contains a blank row")

    states: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    previous_hash = plan["plan_sha256"]
    previous_process = ""

    for journal_seq, line in enumerate(lines):
        event = exact_fields(
            strict_json(line, f"order evidence line {journal_seq + 1}"),
            EVENT_FIELDS,
            set(),
            f"order evidence line {journal_seq + 1}",
        )
        where = f"order evidence line {journal_seq + 1}"
        if event["schema_version"] != ORDER_SCHEMA:
            fail(f"{where}: unsupported schema_version")
        if integer(event["journal_seq"], f"{where}.journal_seq") != journal_seq:
            fail(f"{where}: journal sequence is not contiguous from zero")
        integer(event["wall_unix_ns"], f"{where}.wall_unix_ns", positive=True)
        integer(event["monotonic_ns"], f"{where}.monotonic_ns", positive=True)
        for field, expected in (
            ("run_id", plan["run_id"]),
            ("event_id", plan["event_id"]),
            ("kalshi_env", plan["kalshi_env"]),
            ("account_fingerprint", plan["account_fingerprint"]),
            ("plan_sha256", plan["plan_sha256"]),
        ):
            if event[field] != expected:
                fail(f"{where}: {field} differs from RunPlan")
        process = nonempty(event["process_instance_id"], f"{where}.process_instance_id")
        client = nonempty(event["client_order_id"], f"{where}.client_order_id")
        ticker = nonempty(event["market_ticker"], f"{where}.market_ticker")
        if event["previous_event_sha256"] != previous_hash:
            fail(f"{where}: previous event hash mismatch")
        if event["previous_process_instance_id"] != previous_process:
            fail(f"{where}: previous process instance mismatch")
        claimed = lower_hex(event["event_sha256"], HEX64, f"{where}.event_sha256")
        unhashed = dict(event)
        del unhashed["event_sha256"]
        # serde_json::Value maps are lexically ordered without preserve_order.
        # ensure_ascii=False matches serde_json's compact UTF-8 serialization.
        actual = sha256_bytes(canonical_bytes(unhashed, ascii_only=False))
        if claimed != actual:
            fail(f"{where}: event SHA-256 mismatch")

        transition = exact_fields(
            event["transition"], {"kind", "receipt"}, set(), f"{where}.transition"
        )
        kind = nonempty(transition["kind"], f"{where}.transition.kind")
        receipt = validate_receipt_shape(kind, transition["receipt"], f"{where}.{kind}")
        if receipt["client_order_id"] != client or receipt["market_ticker"] != ticker:
            fail(f"{where}: transition identity differs from envelope")

        state = states.get(client)
        expected_order_seq = 0 if state is None else state["order_seq"] + 1
        if integer(event["order_seq"], f"{where}.order_seq") != expected_order_seq:
            fail(f"{where}: per-order sequence is not contiguous from zero")
        if state is not None:
            if state["market_ticker"] != ticker:
                fail(f"{where}: market ticker changed for client id")
            if state["terminal"]:
                fail(f"{where}: transition appears after terminal order evidence")

        if kind == "intent_evaluated":
            if state is not None:
                fail(f"{where}: duplicate intent_evaluated")
            validate_intent(receipt, f"{where}.intent_evaluated")
            state = {
                "client_order_id": client,
                "market_ticker": ticker,
                "evaluation": receipt,
                "send_request": None,
                "invocation_prepared": None,
                "wire_attempt": None,
                "order_id": None,
                "order_status": None,
                "exchange_accepted": None,
                "terminal": False,
                "last_kind": kind,
                "last_observed_ts": None,
                "order_seq": event["order_seq"],
                "transport_failed": False,
                "fills": [],
                "event_hashes": [claimed],
            }
            states[client] = state
        else:
            if state is None:
                fail(f"{where}: first transition is not intent_evaluated")
            assert state is not None
            last = state["last_kind"]
            if kind == "send_requested":
                if last != "intent_evaluated" or state["send_request"] is not None:
                    fail(f"{where}: send_requested is not exactly after intent")
                requested = integer(receipt["requested_ts"], f"{where}.requested_ts", positive=True)
                if requested < state["evaluation"]["exec_ts"]:
                    fail(f"{where}: send request precedes evaluation")
                state["send_request"] = receipt
            elif kind == "not_attempted":
                if last not in {"intent_evaluated", "send_requested", "invocation_prepared"} or state["wire_attempt"] is not None:
                    fail(f"{where}: not_attempted appears after wire attempt")
                refused = integer(receipt["refusal_ts"], f"{where}.refusal_ts", positive=True)
                nonempty(receipt["reason"], f"{where}.reason")
                minimum = (
                    state["invocation_prepared"]["prepared_ts"]
                    if state["invocation_prepared"] is not None
                    else state["send_request"]["requested_ts"]
                    if state["send_request"] is not None
                    else state["evaluation"]["exec_ts"]
                )
                if refused < minimum:
                    fail(f"{where}: refusal precedes prior transition")
                state["terminal"] = True
            elif kind == "invocation_prepared":
                if last != "send_requested" or state["invocation_prepared"] is not None:
                    fail(f"{where}: invocation_prepared is not exactly after send request")
                if receipt["protocol"] not in ("rest", "fix"):
                    fail(f"{where}: unsupported transport protocol")
                nonempty(receipt["endpoint_id"], f"{where}.endpoint_id")
                prepared = integer(receipt["prepared_ts"], f"{where}.prepared_ts", positive=True)
                if prepared < state["send_request"]["requested_ts"]:
                    fail(f"{where}: invocation preparation precedes request")
                raw_evidence(receipt["raw"], f"{where}.raw")
                state["invocation_prepared"] = receipt
            elif kind == "wire_attempted":
                prepared = state["invocation_prepared"]
                if last != "invocation_prepared" or prepared is None or state["wire_attempt"] is not None:
                    fail(f"{where}: wire_attempted is not exactly after preparation")
                send = integer(receipt["send_ts"], f"{where}.send_ts", positive=True)
                if receipt["protocol"] != prepared["protocol"] or receipt["endpoint_id"] != prepared["endpoint_id"] or send < prepared["prepared_ts"]:
                    fail(f"{where}: wire attempt differs from prepared invocation")
                state["wire_attempt"] = receipt
            elif kind == "exchange_observed":
                attempt = state["wire_attempt"]
                if attempt is None:
                    fail(f"{where}: exchange observation has no wire attempt")
                observed = integer(receipt["observed_ts"], f"{where}.observed_ts", positive=True)
                if receipt["protocol"] != attempt["protocol"] or receipt["endpoint_id"] != attempt["endpoint_id"] or observed < attempt["send_ts"]:
                    fail(f"{where}: exchange observation transport/timestamp mismatch")
                nonempty(receipt["protocol_status"], f"{where}.protocol_status")
                raw_evidence(receipt["raw"], f"{where}.raw")
                echoed = optional_string(receipt, "echoed_client_order_id", where)
                if echoed is not None and echoed != client:
                    fail(f"{where}: exchange echoed a different client id")
                if receipt.get("order_id") is not None and echoed != client:
                    fail(f"{where}: exchange order id lacks exact client-id echo")
                apply_observation(state, receipt, where)
            elif kind == "transport_failed":
                attempt = state["wire_attempt"]
                if attempt is None or state["transport_failed"]:
                    fail(f"{where}: transport failure lacks one unique wire attempt")
                failed = integer(receipt["failure_ts"], f"{where}.failure_ts", positive=True)
                if receipt["protocol"] != attempt["protocol"] or receipt["endpoint_id"] != attempt["endpoint_id"] or failed < attempt["send_ts"]:
                    fail(f"{where}: transport failure mismatch")
                nonempty(receipt["detail"], f"{where}.detail")
                state["transport_failed"] = True
                state["last_observed_ts"] = failed
            elif kind == "reconciled":
                if state["invocation_prepared"] is None:
                    fail(f"{where}: reconciliation lacks prepared ambiguity")
                integer(receipt["observed_ts"], f"{where}.observed_ts", positive=True)
                nonempty(receipt["endpoint_id"], f"{where}.endpoint_id")
                if receipt["protocol"] not in ("rest", "fix"):
                    fail(f"{where}: unsupported reconciliation protocol")
                if receipt["queried_client_order_id"] != client or receipt["echoed_client_order_id"] != client:
                    fail(f"{where}: reconciliation client id is not exact")
                nonempty(receipt["order_status"], f"{where}.order_status")
                raw_evidence(receipt["raw"], f"{where}.raw")
                apply_observation(state, receipt, where)
            elif kind == "reconcile_unresolved":
                if state["invocation_prepared"] is None:
                    fail(f"{where}: unresolved reconciliation lacks prepared ambiguity")
                observed = integer(receipt["observed_ts"], f"{where}.observed_ts", positive=True)
                nonempty(receipt["endpoint_id"], f"{where}.endpoint_id")
                nonempty(receipt["detail"], f"{where}.detail")
                if receipt["protocol"] not in ("rest", "fix") or receipt["queried_client_order_id"] != client:
                    fail(f"{where}: unresolved reconciliation identity mismatch")
                if receipt.get("raw") is not None:
                    raw_evidence(receipt["raw"], f"{where}.raw")
                state["last_observed_ts"] = observed
            state["last_kind"] = kind
            state["order_seq"] = event["order_seq"]
            state["event_hashes"].append(claimed)

        receipt_summary = {
            key: value
            for key, value in receipt.items()
            if key not in {"raw", "parsed"}
        }
        if receipt.get("raw") is not None:
            receipt_summary["raw"] = {
                key: receipt["raw"][key]
                for key in ("media_type", "bytes_len", "bytes_sha256")
            }
        events.append(
            {
                "journal_seq": journal_seq,
                "order_seq": event["order_seq"],
                "wall_unix_ns": event["wall_unix_ns"],
                "monotonic_ns": event["monotonic_ns"],
                "process_instance_id": process,
                "client_order_id": client,
                "market_ticker": ticker,
                "kind": kind,
                "receipt": receipt_summary,
                "event_sha256": claimed,
            }
        )
        previous_hash = claimed
        previous_process = process

    return list(states.values()), events


def dollars_to_cents(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    integer_part, dot, fraction = value.partition(".")
    if not integer_part.isdigit() or (dot and not fraction.isdigit()):
        return None
    if any(char != "0" for char in fraction[2:]):
        return None
    cents_text = (fraction[:2] + "00")[:2]
    cents = int(integer_part) * 100 + int(cents_text or "0")
    return cents if cents <= 100 else None


def fp_to_centi(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    negative = value.startswith("-")
    raw = value[1:] if negative else value
    integer_part, dot, fraction = raw.partition(".")
    if not integer_part.isdigit() or (dot and not fraction.isdigit()):
        return None
    centi = int(integer_part) * 100 + int(((fraction[:2] + "00")[:2]) or "0")
    return -centi if negative else centi


def parse_snapshot(message: Any) -> tuple[str, dict[int, int], dict[int, int]] | None:
    if not isinstance(message, dict) or message.get("type") != "orderbook_snapshot":
        return None
    body = message.get("msg")
    if not isinstance(body, dict) or not isinstance(body.get("market_ticker"), str):
        return None
    # Kalshi OMITS a side key entirely when that side holds zero levels; an
    # absent key is an empty ladder, not a malformed snapshot. A key that is
    # PRESENT but not an array (including an explicit null) is still refused.
    # Mirrors src/kalshi/book.rs::side_levels, pinned by Q-XTAPE addendum
    # (2026-08-18: AS PICK/WIMB, 2 of 12 tickers, voided the whole run).
    yes = body.get("yes_dollars_fp", [])
    no = body.get("no_dollars_fp", [])
    if not isinstance(yes, list) or not isinstance(no, list) or type(message.get("seq")) is not int:
        return None
    bids: dict[int, int] = {}
    asks: dict[int, int] = {}
    for side, levels in (("yes", yes), ("no", no)):
        for level in levels:
            if not isinstance(level, list) or len(level) != 2:
                return None
            price = dollars_to_cents(level[0])
            quantity = fp_to_centi(level[1])
            if price is None or quantity is None or quantity < 0 or quantity > 2**32 - 1:
                return None
            key = price if side == "yes" else 100 - min(price, 100)
            ladder = bids if side == "yes" else asks
            if quantity:
                ladder[key] = quantity
    return body["market_ticker"], bids, asks


def apply_book_message(
    books: dict[str, dict[str, Any]], message: Any
) -> tuple[str, dict[str, Any]] | None:
    snapshot = parse_snapshot(message)
    if snapshot is not None:
        ticker, bids, asks = snapshot
        state = {"bids": bids, "asks": asks, "snapshot": True}
        books[ticker] = state
        return ticker, state
    if not isinstance(message, dict) or message.get("type") != "orderbook_delta":
        return None
    body = message.get("msg")
    if not isinstance(body, dict):
        return None
    ticker = body.get("market_ticker")
    side = body.get("side")
    price = dollars_to_cents(body.get("price_dollars"))
    delta = fp_to_centi(body.get("delta_fp"))
    if not isinstance(ticker, str) or side not in ("yes", "no") or price is None or delta is None:
        return None
    state = books.setdefault(ticker, {"bids": {}, "asks": {}, "snapshot": False})
    ladder = state["bids"] if side == "yes" else state["asks"]
    key = price if side == "yes" else 100 - min(price, 100)
    quantity = max(0, ladder.get(key, 0) + delta)
    if quantity:
        ladder[key] = quantity
    else:
        ladder.pop(key, None)
    return ticker, state


def string_list(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        fail(f"{what}: expected a string array")
    return value


def verify_book_tape(
    tape_path: Path, plan: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    regular_file(tape_path, "book tape")
    tape_raw = tape_path.read_bytes()
    if not tape_raw or not tape_raw.endswith(b"\n"):
        fail("book tape is empty or has a torn final row")
    raw_lines = tape_raw[:-1].split(b"\n")
    if any(not line for line in raw_lines):
        fail("book tape contains a blank row")
    rows = [strict_json(line, f"book tape line {index+1}") for index, line in enumerate(raw_lines)]
    if not rows or not isinstance(rows[0], dict) or rows[0].get("t") != "session":
        fail("book tape session row is not first")
    session = rows[0]
    if (
        session.get("schema_version") != 1
        or session.get("book_tape_protocol_version") != BOOK_PROTOCOL
        or session.get("capabilities") != BOOK_CAPABILITIES
        or session.get("venue") != "kalshi"
        or session.get("feed_kind") != "push"
        or session.get("channel") != "orderbook_delta"
        or session.get("event") != plan["market_event_ticker"]
        or session.get("label") != plan["run_id"]
        or session.get("host") != plan["execution_host_id"]
    ):
        fail("book tape session schema or RunPlan identity is invalid")
    tickers = string_list(session.get("tickers"), "book session tickers")
    if not tickers or len(tickers) != len(set(tickers)):
        fail("book session ticker set is empty or duplicated")
    path_has_suffix(
        session.get("commit_receipt"),
        plan["artifacts"]["book_tape"] + ".commit.json",
        "book session commit receipt",
    )
    integer(session.get("start_wall_ns"), "book session start_wall_ns", positive=True)
    integer(session.get("start_mono_ns"), "book session start_mono_ns", positive=True)

    books: dict[str, dict[str, Any]] = {}
    histories: dict[str, list[dict[str, Any]]] = {ticker: [] for ticker in tickers}
    snapshot_proofs: dict[str, int] = {}
    frame_rows = heartbeat_rows = 0
    last_heartbeat_drops = 0
    close: dict[str, Any] | None = None
    for index, row in enumerate(rows[1:], start=2):
        if not isinstance(row, dict) or not isinstance(row.get("t"), str):
            fail(f"book tape line {index}: invalid row envelope")
        kind = row["t"]
        if close is not None:
            fail("book tape contains bytes after close")
        if kind == "session":
            fail("book tape contains duplicate session")
        if kind == "frame":
            wall = integer(row.get("wall_ns"), f"book frame {index} wall_ns", positive=True)
            mono = integer(row.get("mono_ns"), f"book frame {index} mono_ns", positive=True)
            raw = row.get("raw")
            if not isinstance(raw, str):
                fail(f"book frame {index}: raw payload is not a string")
            frame_rows += 1
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            applied = apply_book_message(books, message)
            if applied is not None:
                ticker, state = applied
                if ticker in histories:
                    if parse_snapshot(message) is not None:
                        snapshot_proofs.setdefault(ticker, mono)
                    histories[ticker].append(
                        {
                            "wall_ns": wall,
                            "mono_ns": mono,
                            "snapshot": state["snapshot"],
                            "asks": dict(state["asks"]),
                        }
                    )
        elif kind == "heartbeat":
            heartbeat_rows += 1
            if row.get("heartbeat_rows") != heartbeat_rows or row.get("frame_rows") != frame_rows:
                fail("book heartbeat counters differ from tape prefix")
            integer(row.get("wall_ns"), "book heartbeat wall_ns", positive=True)
            integer(row.get("mono_ns"), "book heartbeat mono_ns", positive=True)
            drops = integer(row.get("tap_dropped"), "book heartbeat tap_dropped")
            if drops < last_heartbeat_drops:
                fail("book heartbeat drops moved backwards")
            last_heartbeat_drops = drops
            through = integer(
                row.get("readiness_through_mono_ns"),
                "book heartbeat readiness_through_mono_ns",
                positive=True,
            )
            requested = string_list(
                row.get("requested_tickers"), "book heartbeat requested_tickers"
            )
            ready_at_heartbeat = string_list(
                row.get("ready_tickers"), "book heartbeat ready_tickers"
            )
            missing_at_heartbeat = string_list(
                row.get("missing_tickers"), "book heartbeat missing_tickers"
            )
            expected_ready = [
                ticker
                for ticker in tickers
                if ticker in snapshot_proofs and snapshot_proofs[ticker] <= through
            ]
            expected_missing = [ticker for ticker in tickers if ticker not in expected_ready]
            if (
                requested != tickers
                or ready_at_heartbeat != expected_ready
                or missing_at_heartbeat != expected_missing
                or sorted(ready_at_heartbeat + missing_at_heartbeat) != sorted(tickers)
                or row.get("writer_status") != "open"
                or row.get("writer_error") is not None
                or row.get("feed_health") not in ("unknown", "healthy", "stale")
                or type(row.get("ever_observed_feed")) is not bool
            ):
                fail("book heartbeat health/readiness fields differ from tape prefix")
        elif kind == "close":
            close = row
        else:
            fail(f"book tape line {index}: unknown row type {kind!r}")
    if close is None or rows[-1] is not close:
        fail("book tape has no final close row")

    if (
        close.get("status") != "pending"
        or close.get("complete") is not False
        or close.get("commit_required") is not True
        or close.get("candidate_status") != "closed"
        or close.get("candidate_complete") is not True
        or close.get("frame_rows") != frame_rows
        or close.get("rows") != frame_rows
        or close.get("heartbeat_rows") != heartbeat_rows
        or close.get("unwritten_frames") != 0
        or close.get("tap_dropped") != 0
        or close.get("dropped") != 0
        or close.get("ever_observed_feed") is not True
        or close.get("producer_quiesced") is not True
        or close.get("terminal_accounting_complete") is not True
        or close.get("feed_health") != "healthy"
    ):
        fail("book tape close is not complete, healthy, and zero-loss")
    if string_list(close.get("requested_tickers"), "book close requested_tickers") != tickers:
        fail("book close requested tickers differ from session")
    ready = string_list(close.get("ready_tickers"), "book close ready_tickers")
    missing = string_list(close.get("missing_tickers"), "book close missing_tickers")
    close_mono = integer(close.get("close_mono_ns"), "book close close_mono_ns", positive=True)
    if (
        missing
        or ready != tickers
        or any(
            ticker not in snapshot_proofs or snapshot_proofs[ticker] > close_mono
            for ticker in tickers
        )
    ):
        fail("book close readiness is not proven by full snapshots")

    commit_path = Path(str(tape_path) + ".commit.json")
    commit = one_json_line(commit_path, "book tape commit")
    if (
        commit.get("t") != "commit"
        or commit.get("schema_version") != BOOK_PROTOCOL
        or commit.get("book_tape_protocol_version") != BOOK_PROTOCOL
        or commit.get("capabilities") != BOOK_CAPABILITIES
        or commit.get("durable") is not True
        or commit.get("complete") is not True
        or commit.get("status") != "closed"
        or commit.get("tape_bytes") != len(tape_raw)
        or commit.get("tape_sha256") != sha256_bytes(tape_raw)
    ):
        fail("book tape commit protocol or byte binding is invalid")
    mappings = {
        "status": "candidate_status",
        "complete": "candidate_complete",
        "terminal_feed_age_ns": "feed_age_ns",
        "terminal_feed_health": "feed_health",
    }
    for field in (
        "status", "complete", "reason", "frame_rows", "heartbeat_rows",
        "unwritten_frames", "tap_dropped", "ever_observed_feed",
        "producer_quiesced", "terminal_accounting_complete",
        "requested_tickers", "ready_tickers", "missing_tickers",
        "terminal_feed_age_ns", "terminal_feed_health", "close_wall_ns",
        "close_mono_ns", "start_wall_ns", "start_mono_ns", "end_wall_ns",
        "end_mono_ns",
    ):
        if commit.get(field) != close.get(mappings.get(field, field)):
            fail(f"book commit differs from close for {field}")
    return commit, histories


def verify_fp_object(value: Any, actual: dict[str, Any], what: str) -> None:
    row = exact_fields(value, {"bytes", "sha256"}, set(), what)
    if row != actual:
        fail(f"{what}: byte length or SHA-256 differs")


def verify_audio(
    run_dir: Path, plan: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    manifest_path = safe_artifact(run_dir, plan, "audio_manifest")
    commit_path = safe_artifact(run_dir, plan, "audio_commit")
    raw_path = safe_artifact(run_dir, plan, "audio_raw")
    frames_path = safe_artifact(run_dir, plan, "audio_frames")
    wav_dir = safe_artifact(run_dir, plan, "audio_wav_dir")
    manifest = one_json_line(manifest_path, "audio manifest")
    commit = one_json_line(commit_path, "audio commit")
    identity = {
        "run_id": plan["run_id"],
        "event_id": plan["event_id"],
        "plan_sha256": plan["plan_sha256"],
    }
    if (
        manifest.get("schema_version") != AUDIO_SCHEMA
        or commit.get("schema_version") != AUDIO_SCHEMA
        or manifest.get("identity") != identity
        or commit.get("identity") != identity
        or manifest.get("status") != "pending_commit"
        or manifest.get("complete") is not False
        or manifest.get("commit_required") is not True
        or commit.get("status") != "committed"
        or commit.get("complete") is not True
        or commit.get("durable") is not True
    ):
        fail("audio manifest/commit identity or terminal protocol is invalid")
    for field in ("frame_count", "sample_count"):
        count = integer(manifest.get(field), f"audio manifest {field}", positive=True)
        if commit.get(field) != count:
            fail(f"audio commit differs from manifest for {field}")
    expected_frames = integer(
        manifest.get("expected_published_frames"), "audio expected frames", positive=True
    )
    start_wall = integer(manifest.get("start_wall_ns"), "audio start_wall_ns", positive=True)
    start_mono = integer(manifest.get("start_mono_ns"), "audio start_mono_ns", positive=True)
    end_wall = integer(manifest.get("end_wall_ns"), "audio end_wall_ns", positive=True)
    end_mono = integer(manifest.get("end_mono_ns"), "audio end_mono_ns", positive=True)
    close_wall = integer(commit.get("close_wall_ns"), "audio close_wall_ns", positive=True)
    close_mono = integer(commit.get("close_mono_ns"), "audio close_mono_ns", positive=True)
    if (
        manifest.get("bank_accepted_frames") != expected_frames
        or manifest.get("writer_frames") != expected_frames
        or manifest.get("frame_count") != expected_frames
        or manifest.get("dropped_frames") != 0
        or manifest.get("closed_attempts") != 0
        or manifest.get("invalid_attempts") != 0
        or manifest.get("producer_quiesced") is not True
        or end_wall < start_wall
        or end_mono < start_mono
        or close_wall < end_wall
        or close_mono < end_mono
    ):
        fail("audio evidence terminal accounting is incomplete or lossy")

    path_has_suffix(manifest.get("raw_path"), plan["artifacts"]["audio_raw"], "audio raw path")
    path_has_suffix(commit.get("raw_path"), plan["artifacts"]["audio_raw"], "audio commit raw path")
    path_has_suffix(manifest.get("frames_path"), plan["artifacts"]["audio_frames"], "audio frames path")
    path_has_suffix(commit.get("frames_path"), plan["artifacts"]["audio_frames"], "audio commit frames path")
    path_has_suffix(commit.get("manifest_path"), plan["artifacts"]["audio_manifest"], "audio commit manifest path")
    stored_wav_dir = path_has_suffix(manifest.get("wav_dir"), plan["artifacts"]["audio_wav_dir"], "audio WAV directory")
    if commit.get("wav_dir") != manifest.get("wav_dir"):
        fail("audio commit WAV directory differs from manifest")

    actual_manifest = fingerprint(manifest_path)
    actual_raw = fingerprint(raw_path)
    actual_frames = fingerprint(frames_path)
    verify_fp_object(commit.get("manifest"), actual_manifest, "audio manifest fingerprint")
    verify_fp_object(manifest.get("raw"), actual_raw, "audio manifest raw fingerprint")
    verify_fp_object(commit.get("raw"), actual_raw, "audio commit raw fingerprint")
    verify_fp_object(manifest.get("frames"), actual_frames, "audio manifest frames fingerprint")
    verify_fp_object(commit.get("frames"), actual_frames, "audio commit frames fingerprint")

    segments = manifest.get("wav_segments")
    if not isinstance(segments, list) or not segments or commit.get("wav_segments") != segments:
        fail("audio WAV segment manifest is empty or differs from commit")
    wav_paths: list[str] = []
    sample_sum = 0
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            fail(f"audio WAV segment {index} is not an object")
        stored_path = Path(nonempty(segment.get("path"), f"audio WAV segment {index}.path"))
        try:
            relative = stored_path.relative_to(stored_wav_dir)
        except ValueError:
            fail(f"audio WAV segment {index} escapes stored WAV directory")
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            fail(f"audio WAV segment {index} has unsafe relative path")
        local_path = wav_dir / relative
        actual_wav = fingerprint(local_path)
        verify_fp_object(segment.get("wav"), actual_wav, f"audio WAV segment {index} fingerprint")
        sample_count = integer(segment.get("sample_count"), f"audio WAV segment {index}.sample_count", positive=True)
        sample_rate = integer(segment.get("sample_rate_hz"), f"audio WAV segment {index}.sample_rate_hz", positive=True)
        lower_hex(segment.get("pcm_sha256"), HEX64, f"audio WAV segment {index}.pcm_sha256")
        try:
            with wave.open(str(local_path), "rb") as reader:
                if reader.getnchannels() != 1 or reader.getsampwidth() != 2 or reader.getframerate() != sample_rate or reader.getnframes() != sample_count:
                    fail(f"audio WAV segment {index} format/count differs from manifest")
                pcm = reader.readframes(reader.getnframes())
        except (wave.Error, EOFError) as exc:
            fail(f"audio WAV segment {index} is invalid: {exc}")
        if sha256_bytes(pcm) != segment["pcm_sha256"]:
            fail(f"audio WAV segment {index} PCM SHA-256 differs")
        sample_sum += sample_count
        wav_paths.append((Path(plan["artifacts"]["audio_wav_dir"]) / relative).as_posix())
    if sample_sum != manifest["sample_count"]:
        fail("audio WAV segment samples do not sum to manifest sample_count")
    return commit, wav_paths


def normalize_tier(value: Any, what: str) -> str:
    if not isinstance(value, str) or value.lower() not in ("fast", "confirmed"):
        fail(f"{what}: invalid tier")
    return value.lower()


def fill_is_positive(fill: dict[str, Any]) -> bool:
    for field in ("filled_count_fp", "latest_fill_count_fp"):
        if field in fill and any(char != "0" for char in fill[field] if char != "."):
            return True
    return "average_price_dollars" in fill or "latest_fill_price_dollars" in fill


def report_fill_is_positive(value: Any) -> bool:
    if value is None:
        return False
    if not isinstance(value, dict):
        fail("runtime report fill is neither an object nor null")
    for field in ("cumulative_contracts", "last_fill_contracts"):
        count = value.get(field)
        if isinstance(count, str) and any(char != "0" for char in count if char != "."):
            return True
    return value.get("average_price") is not None or value.get("last_fill_price") is not None


def validate_report(
    path: Path,
    plan: dict[str, Any],
    states: list[dict[str, Any]],
    book_commit: dict[str, Any],
    audio_commit: dict[str, Any],
    transport: dict[str, Any],
    fix_verification: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]:
    regular_file(path, "runtime report")
    report = strict_json(path.read_bytes(), "runtime report")
    if not isinstance(report, dict):
        fail("runtime report is not an object")
    for field, expected in (
        ("run_plan_schema", PLAN_SCHEMA),
        ("run_id", plan["run_id"]),
        ("event_id", plan["event_id"]),
        ("plan_sha256", plan["plan_sha256"]),
        ("execution_host_id", plan["execution_host_id"]),
        ("account_fingerprint", plan["account_fingerprint"]),
    ):
        if report.get(field) != expected:
            fail(f"runtime report {field} differs from RunPlan")
    if report.get("finalized") is not True:
        fail("runtime report is not finalized")
    if (
        report.get("required_evidence_healthy") is not True
        or report.get("book_tape_healthy") is not True
        or report.get("order_evidence_healthy") is not True
        or report.get("audio_evidence_healthy") is not True
    ):
        fail("runtime report says required evidence is unhealthy")
    for field, artifact in (
        ("book_tape_path", "book_tape"),
        ("order_evidence_path", "order_evidence"),
        ("audio_raw_path", "audio_raw"),
        ("audio_frames_path", "audio_frames"),
        ("audio_manifest_path", "audio_manifest"),
        ("audio_commit_path", "audio_commit"),
        ("audio_wav_dir", "audio_wav_dir"),
    ):
        path_has_suffix(report.get(field), plan["artifacts"][artifact], f"runtime report {field}")
    book_summary = report.get("book_tape")
    if not isinstance(book_summary, dict) or book_summary.get("complete") is not True or book_summary.get("committed") is not True:
        fail("runtime report book summary is incomplete")
    for field in ("frame_rows", "heartbeat_rows", "unwritten_frames", "tap_dropped", "reason"):
        if book_summary.get(field) != book_commit.get(field):
            fail(f"runtime report book summary disagrees for {field}")
    audio_summary = report.get("audio_evidence")
    if not isinstance(audio_summary, dict) or audio_summary.get("status") != "committed" or audio_summary.get("commit") != audio_commit:
        fail("runtime report audio commit disagrees with committed artifact")
    validate_fix_report(report, plan, transport, fix_verification)

    detections = report.get("detections")
    if not isinstance(detections, list):
        fail("runtime report detections is not an array")
    links: dict[str, dict[str, Any]] = {}
    seen_link_keys: set[tuple[str, int]] = set()
    for index, record in enumerate(detections):
        if not isinstance(record, dict) or not isinstance(record.get("detection"), dict):
            fail(f"runtime detection {index} has invalid shape")
        detection = record["detection"]
        integer(detection.get("emit_ts"), f"runtime detection {index}.emit_ts", positive=True)
        integer(detection.get("audio_ts"), f"runtime detection {index}.audio_ts", positive=True)
        intent = record.get("intent")
        if intent is None:
            continue
        if not isinstance(intent, dict):
            fail(f"runtime detection {index}.intent is not an object/null")
        ticker = nonempty(intent.get("market_ticker"), f"runtime detection {index}.market_ticker")
        decision = integer(intent.get("decision_ts"), f"runtime detection {index}.decision_ts", positive=True)
        key = (ticker, decision)
        if key in seen_link_keys:
            fail("runtime report has ambiguous duplicate intent detection links")
        seen_link_keys.add(key)

    state_by_id = {state["client_order_id"]: state for state in states}
    report_orders = report.get("orders")
    if not isinstance(report_orders, list) or any(not isinstance(item, dict) for item in report_orders):
        fail("runtime report orders is not an object array")
    report_by_id: dict[str, dict[str, Any]] = {}
    for index, order in enumerate(report_orders):
        client = nonempty(order.get("client_order_id"), f"runtime order {index}.client_order_id")
        if client in report_by_id:
            fail("runtime report repeats a client order id")
        report_by_id[client] = order
    if set(report_by_id) != set(state_by_id):
        fail("runtime report/order evidence intent identities disagree")

    attempts = accepted = fills = acknowledgements = 0
    unresolved: list[str] = []
    for client, state in state_by_id.items():
        order = report_by_id[client]
        evaluation = state["evaluation"]
        for report_field, evidence_field in (
            ("ticker", "market_ticker"),
            ("price_cents", "price_cents"),
            ("count", "contract_count"),
            ("audio_ts", "audio_ts"),
            ("decision_ts", "decision_ts"),
            ("exec_ts", "exec_ts"),
        ):
            if order.get(report_field) != evaluation[evidence_field]:
                fail(f"runtime report/order evidence disagree for {client}.{report_field}")
        if normalize_tier(order.get("tier"), f"runtime order {client}.tier") != evaluation["tier"]:
            fail(f"runtime report/order evidence disagree for {client}.tier")
        wire = state["wire_attempt"]
        expected_send = None if wire is None else wire["send_ts"]
        if order.get("send_ts") != expected_send:
            fail(f"runtime report/order evidence disagree for {client}.send_ts")
        if order.get("order_id") != state["order_id"]:
            fail(f"runtime report/order evidence disagree for {client}.order_id")
        if order.get("order_status") != state["order_status"]:
            fail(f"runtime report/order evidence disagree for {client}.order_status")
        if order.get("exchange_accepted") != state["exchange_accepted"]:
            fail(f"runtime report/order evidence disagree for {client}.exchange_accepted")
        attempts += wire is not None
        accepted += state["exchange_accepted"] is True
        filled = any(fill_is_positive(fill) for fill in state["fills"])
        if report_fill_is_positive(order.get("fill")) != filled:
            fail(f"runtime report/order evidence disagree for {client}.fill")
        fills += filled
        acknowledgements += state["order_id"] is not None
        if not state["terminal"]:
            unresolved.append(client)
        link_key = (evaluation["market_ticker"], evaluation["decision_ts"])
        matches = [record for record in detections if isinstance(record.get("intent"), dict) and (record["intent"].get("market_ticker"), record["intent"].get("decision_ts")) == link_key]
        if len(matches) != 1:
            fail(f"order evidence intent {client} lacks one exact report detection link")
        links[client] = matches[0]

    expected_counts = {
        "intents": len(states),
        "send_attempted": attempts,
        "sent": attempts,
        "acked": acknowledgements,
    }
    for field, expected in expected_counts.items():
        if report.get(field) != expected:
            fail(f"runtime report {field}={report.get(field)!r} disagrees with order evidence {expected}")
    for field in ("unresolved_cancellation_ids", "kill_unresolved_ids"):
        actual = report.get(field)
        if not isinstance(actual, list) or sorted(actual) != sorted(unresolved):
            fail(f"runtime report {field} disagrees with unresolved order evidence")
    return report, links, sorted(unresolved)


def cap_depth(asks: dict[int, int], cap: int) -> int:
    return sum(quantity for price, quantity in asks.items() if price <= cap)


def depth_analysis(
    state: dict[str, Any],
    detection_record: dict[str, Any],
    histories: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    evaluation = state["evaluation"]
    ticker = evaluation["market_ticker"]
    cap = evaluation["price_cents"]
    anchor = evaluation["book_as_of_ts"] or evaluation["decision_ts"]
    history = histories.get(ticker, [])
    baseline_candidates = [point for point in history if point["mono_ns"] <= anchor]
    base = baseline_candidates[-1] if baseline_candidates else None
    common = {
        "kind": "cap_depth_exhaustion",
        "causality": "not_resolvable",
        "clock": "host_monotonic_ns",
        "cap_price_cents": cap,
        "baseline_anchor_mono_ns": anchor,
        "runtime_book_as_of_mono_ns": evaluation["book_as_of_ts"] or None,
    }
    if base is None or not base["snapshot"]:
        return {**common, "classification": "no_snapshot", "baseline_depth_centi_contracts": None, "exhaustion_receive_mono_ns": None, "detection_to_exhaustion_ms": None, "wire_to_exhaustion_ms": None}
    baseline = cap_depth(base["asks"], cap)
    if baseline == 0:
        return {**common, "classification": "empty_at_detection", "baseline_depth_centi_contracts": 0, "exhaustion_receive_mono_ns": None, "detection_to_exhaustion_ms": None, "wire_to_exhaustion_ms": None}
    previous = baseline
    exhaustion: dict[str, Any] | None = None
    for point in history:
        if point["mono_ns"] <= anchor:
            continue
        depth = cap_depth(point["asks"], cap)
        if previous > 0 and depth == 0:
            exhaustion = point
            break
        previous = depth
    if exhaustion is None:
        return {**common, "classification": "depth_available_no_exhaustion", "baseline_depth_centi_contracts": baseline, "exhaustion_receive_mono_ns": None, "detection_to_exhaustion_ms": None, "wire_to_exhaustion_ms": None}
    emit = detection_record["detection"]["emit_ts"]
    wire = state["wire_attempt"]
    exhaustion_ns = exhaustion["mono_ns"]
    detection_ms = (exhaustion_ns - emit) / 1_000_000 if exhaustion_ns >= emit else None
    send = None if wire is None else wire["send_ts"]
    wire_ms = (exhaustion_ns - send) / 1_000_000 if send is not None and exhaustion_ns >= send else None
    return {
        **common,
        "classification": "depth_exhausted_after_detection",
        "baseline_depth_centi_contracts": baseline,
        "exhaustion_receive_wall_ns": exhaustion["wall_ns"],
        "exhaustion_receive_mono_ns": exhaustion_ns,
        "detection_to_exhaustion_ms": detection_ms,
        "wire_to_exhaustion_ms": wire_ms,
    }


def order_grade(
    state: dict[str, Any],
    detection: dict[str, Any],
    histories: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    filled = any(fill_is_positive(fill) for fill in state["fills"])
    return {
        "client_order_id": state["client_order_id"],
        "market_ticker": state["market_ticker"],
        "intent": state["evaluation"],
        "attempted": state["wire_attempt"] is not None,
        "wire_send_mono_ns": None if state["wire_attempt"] is None else state["wire_attempt"]["send_ts"],
        "accepted": state["exchange_accepted"] is True,
        "exchange_order_id": state["order_id"],
        "order_status": state["order_status"],
        "filled": filled,
        "fill_observations": state["fills"],
        "terminal": state["terminal"],
        "unresolved": not state["terminal"],
        "last_transition": state["last_kind"],
        "transition_event_sha256": state["event_hashes"],
        "book_observation": depth_analysis(state, detection, histories),
    }


def lifecycle_has(run_dir: Path, transition: str) -> bool:
    path = run_dir / "control" / "receipts.jsonl"
    regular_file(path, "lifecycle receipts")
    for line in path.read_bytes().splitlines():
        receipt = strict_json(line, "lifecycle receipt")
        if receipt.get("kind") == "transition" and receipt.get("outcome") == "ok" and receipt.get("transition") == transition:
            return True
    return False


def publish_new_or_identical(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        regular_file(path, "existing grade")
        if path.read_bytes() != payload:
            fail(f"grade destination already contains different bytes: {path}")
        return "already-identical"
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = None
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                fail("short write while publishing grade")
            view = view[count:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                fail(f"concurrent different grade appeared at {path}")
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return "published" if path.exists() else "already-identical"
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def record_transition(
    run_dir: Path,
    transition: str,
    detail: str,
    artifacts: list[str],
) -> None:
    eventctl = Path(__file__).with_name("eventctl.py")
    argv = [
        sys.executable,
        str(eventctl),
        "record",
        "--run-dir",
        str(run_dir),
        "--transition",
        transition,
        "--actor",
        "run-audit",
        "--detail",
        detail,
    ]
    for artifact in artifacts:
        argv.extend(["--artifact", artifact])
    result = subprocess.run(
        argv,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()
        fail(f"eventctl could not record {transition}: {detail}")


def consolidate_local_evidence(run_dir: Path, plan: dict[str, Any]) -> bool:
    if plan["lane"] != "webcast":
        return False
    if lifecycle_has(run_dir, "evidence_consolidated") or lifecycle_has(
        run_dir, "evidence_pulled"
    ):
        return True
    required = [
        plan["artifacts"]["audio_commit"],
        plan["artifacts"]["book_tape"],
        plan["artifacts"]["order_evidence"],
        plan["artifacts"]["runtime_report"],
    ]
    record_transition(
        run_dir,
        "evidence_consolidated",
        "terminal laptop-hosted evidence independently validated in place",
        required,
    )
    return True


def audit(run_dir: Path) -> tuple[Path, dict[str, Any], str, bool]:
    run_dir = run_dir.resolve(strict=True)
    if not run_dir.is_dir():
        fail(f"run directory is not a directory: {run_dir}")
    verify_eventctl(run_dir)
    plan = load_plan(run_dir)
    transport = execution_transport(run_dir, plan)

    order_path = safe_artifact(run_dir, plan, "order_evidence")
    states, transitions = validate_order_evidence(order_path, plan)
    book_path = safe_artifact(run_dir, plan, "book_tape")
    book_commit, histories = verify_book_tape(book_path, plan)
    audio_commit, wav_paths = verify_audio(run_dir, plan)
    fix_verification = verify_fix_evidence(run_dir, plan, transport)
    report_path = safe_artifact(run_dir, plan, "runtime_report")
    report, detection_links, unresolved = validate_report(
        report_path,
        plan,
        states,
        book_commit,
        audio_commit,
        transport,
        fix_verification,
    )
    for state in states:
        if state["market_ticker"] not in histories:
            fail(
                "order evidence market ticker is absent from the committed "
                f"book session: {state['market_ticker']}"
            )

    orders = [
        order_grade(state, detection_links[state["client_order_id"]], histories)
        for state in sorted(states, key=lambda item: item["client_order_id"])
    ]
    counts = {
        "intents": len(states),
        "attempted": sum(order["attempted"] for order in orders),
        "accepted": sum(order["accepted"] for order in orders),
        "filled": sum(order["filled"] for order in orders),
        "terminal": sum(order["terminal"] for order in orders),
        "unresolved": len(unresolved),
    }
    operational = "pass" if report.get("runtime_success") is True and not unresolved else "fail"
    grade = {
        "schema": GRADE_SCHEMA,
        "kind": "run-grade",
        "event_id": plan["event_id"],
        "run_id": plan["run_id"],
        "plan_sha256": plan["plan_sha256"],
        "lane": plan["lane"],
        "execution_host_id": plan["execution_host_id"],
        "account_fingerprint": plan["account_fingerprint"],
        "validation": {
            "status": "pass",
            "eventctl_verified": True,
            "runtime_report_finalized": True,
            "required_evidence_healthy": True,
        },
        "operational_outcome": operational,
        "runtime_exit": report.get("exit"),
        "accounting_authority": plan["artifacts"]["order_evidence"],
        "accounting": counts,
        "unresolved_client_order_ids": unresolved,
        "detections": report["detections"],
        "order_transitions": transitions,
        "orders": orders,
        "book_evidence": {
            "artifact": plan["artifacts"]["book_tape"],
            "protocol": BOOK_PROTOCOL,
            "tape_bytes": book_commit["tape_bytes"],
            "tape_sha256": book_commit["tape_sha256"],
            "frame_rows": book_commit["frame_rows"],
            "observation_name": "cap_depth_exhaustion",
            "causality_limit": "A cap-depth transition does not prove a competitor trade or sweep.",
        },
        "audio_evidence": {
            "artifact": plan["artifacts"]["audio_commit"],
            "status": "committed",
            "frame_count": audio_commit["frame_count"],
            "sample_count": audio_commit["sample_count"],
            "wav_paths": wav_paths,
            "precision": "not_resolvable",
            "precision_requires": "transcript_scoring",
        },
        "fix_observation_evidence": {
            key: value
            for key, value in fix_verification.items()
            if key != "report_summary"
        },
        "evidence_fingerprints": {
            "order_evidence_sha256": fingerprint(order_path)["sha256"],
            "runtime_report_sha256": fingerprint(report_path)["sha256"],
            "book_tape_sha256": book_commit["tape_sha256"],
            "audio_manifest_sha256": audio_commit["manifest"]["sha256"],
            "audio_raw_sha256": audio_commit["raw"]["sha256"],
            "audio_frames_sha256": audio_commit["frames"]["sha256"],
            "fix_observation_tape_sha256": fix_verification["tape_sha256"],
        },
    }
    payload = (json.dumps(grade, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode()
    grade_path = safe_artifact(run_dir, plan, "grade")
    publication = publish_new_or_identical(grade_path, payload)
    consolidated = (
        lifecycle_has(run_dir, "evidence_consolidated")
        or lifecycle_has(run_dir, "evidence_pulled")
        or consolidate_local_evidence(run_dir, plan)
    )
    if consolidated:
        record_transition(
            run_dir,
            "graded",
            "deterministic schema-1 run grade published",
            [plan["artifacts"]["grade"]],
        )
    return grade_path, grade, publication, consolidated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="offline deterministic audit for one schema-3 run"
    )
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)
    try:
        grade_path, grade, publication, consolidated = audit(Path(args.run_dir))
    except (AuditError, OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    lifecycle = (
        "graded recorded"
        if consolidated
        else "graded not recorded (consolidated evidence absent)"
    )
    print(
        f"OK: {grade['event_id']}/{grade['run_id']} {grade['operational_outcome']} — "
        f"{publication} {grade_path}; {lifecycle}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
