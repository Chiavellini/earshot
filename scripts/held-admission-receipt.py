#!/usr/bin/env python3
"""Open the live phone decision interlock from a fresh, probe-proven hold.

This is the unattended counterpart to the explicit human assertion in
operator-page.sh.  It is intentionally narrower: only the canonical registry's
fresh ``admission = "held"`` verdict is eligible.  An operator-screened,
dropped, untested, missing, or unknown verdict can never be promoted here.

The launcher-facing invocation is deliberately explicit and local-only::

    scripts/held-admission-receipt.py \
      --config configs/<event>.toml \
      --run-plan var/runs/<EVENT>/<RUN>/control/run-plan.json \
      --state-dir state/<EVENT>

Call it only after the runtime has published the current ``source.attached``
and ``stream.connected`` markers and the launcher has recorded the matching
``source_attached`` lifecycle transition.  The program performs no network,
call, page, or trading action.  Its only writes are a durable assertion row and
the runtime-compatible schema-1 admission receipt.  Exit 0 means
published/recovered/already current, exit 3 means the canonical registry does
not authorize automatic admission (expected closed-gate posture), and exit 8
means current-run/current-stream integrity failed and callers must fail loud.
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import tomllib


METHOD = "probe_held_current_run"
ASSERTED_BY = "scripts/held-admission-receipt.py"
REGISTRY_REL = "configs/dial-ins.toml"
MAX_PROBE_AGE_DAYS = 30
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_AUDIT_BYTES = 32 * 1024 * 1024
CALL_SID_RE = re.compile(r"^CA[0-9a-fA-F]{32}$")
NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
EVENT_ISSUER_RE = re.compile(r"^KXEARNINGSMENTION([A-Z0-9]+)-")
EXIT_INELIGIBLE = 3
EXIT_INTEGRITY = 8


class Refusal(Exception):
    """A fail-closed eligibility or integrity refusal."""


class Ineligible(Refusal):
    """The registry deliberately does not authorize automatic admission."""


def canonical_bytes(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def safe_relative(raw) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    path = Path(raw)
    return not path.is_absolute() and all(
        part not in ("", ".", "..") for part in path.parts
    )


def repo_root() -> Path:
    raw = os.environ.get("EARSHOT_REPO")
    path = Path(raw) if raw else Path(__file__).resolve().parent.parent
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise Refusal(f"repository root is not an existing real path: {exc}")
    if not path.is_dir():
        raise Refusal(f"repository root is not a directory: {path}")
    return path


def input_path(raw: str, repo: Path, label: str) -> Path:
    """Resolve a CLI path like eventctl: CWD first, then repository root.

    Do not call Path.resolve here: the final-component symlink check belongs to
    read_regular_bytes()/validate_directory and must happen before resolution.
    """

    path = Path(raw)
    candidates = [path] if path.is_absolute() else [Path.cwd() / path, repo / path]
    for candidate in candidates:
        absolute = Path(os.path.abspath(candidate))
        if os.path.lexists(absolute):
            return absolute
    raise Refusal(f"{label} does not exist: {raw}")


def _open_regular(path: Path, flags: int, label: str, mode: int = 0o600) -> int:
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, mode)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise Refusal(f"{label} must not be a symlink")
        raise Refusal(f"cannot open {label} {path}: {exc}")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refusal(f"{label} must be a regular non-symlink file")
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_fd(fd: int, label: str, limit: int) -> tuple[bytes, os.stat_result]:
    before = os.fstat(fd)
    if before.st_size > limit:
        raise Refusal(f"{label} is unexpectedly large ({before.st_size} bytes)")
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    total = 0
    while True:
        chunk = os.read(fd, min(1 << 20, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise Refusal(f"{label} exceeds {limit} bytes")
    after = os.fstat(fd)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after:
        raise Refusal(f"{label} changed while it was being read")
    return b"".join(chunks), after


def require_fd_still_at_path(fd: int, path: Path, label: str) -> None:
    """Refuse if a locked authoritative file was replaced behind its fd."""

    held = os.fstat(fd)
    try:
        current = path.lstat()
    except OSError as exc:
        raise Refusal(f"{label} disappeared while locked: {exc}")
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
        raise Refusal(f"{label} path stopped naming a regular non-symlink file")
    if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
        raise Refusal(f"{label} was replaced while locked")


def read_regular_bytes(
    path: Path, label: str, limit: int = MAX_FILE_BYTES
) -> tuple[bytes, os.stat_result]:
    fd = _open_regular(path, os.O_RDONLY, label)
    try:
        return _read_fd(fd, label, limit)
    finally:
        os.close(fd)


def read_regular_json(path: Path, label: str) -> tuple[dict, bytes, os.stat_result]:
    raw, info = read_regular_bytes(path, label)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal(f"{label} is not valid JSON: {exc}")
    if not isinstance(value, dict):
        raise Refusal(f"{label} must be a JSON object")
    return value, raw, info


def read_optional_json(path: Path, label: str):
    if not os.path.lexists(path):
        return None
    return read_regular_json(path, label)


def validate_directory(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise Refusal(f"{label} is missing: {exc}")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Refusal(f"{label} must be a real non-symlink directory")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise Refusal(f"cannot resolve {label}: {exc}")


def require_directory_identity(path: Path, identity: tuple[int, int], label: str) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise Refusal(f"{label} disappeared: {exc}")
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
        raise Refusal(f"{label} stopped naming a real directory")
    if (current.st_dev, current.st_ino) != identity:
        raise Refusal(f"{label} was replaced during admission verification")


def resolve_declared_path(raw, repo: Path, label: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise Refusal(f"challenge {label} is absent or malformed")
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    try:
        return path.resolve(strict=False)
    except OSError as exc:
        raise Refusal(f"challenge {label} cannot be resolved: {exc}")


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


def run_eventctl(repo: Path, *args: str) -> str:
    eventctl = Path(__file__).resolve().with_name("eventctl.py")
    if not eventctl.is_file():
        raise Refusal(f"eventctl verifier is missing: {eventctl}")
    env = os.environ.copy()
    env["EARSHOT_REPO"] = str(repo)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run(
            [sys.executable, str(eventctl), *args],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal(f"eventctl {' '.join(args)} could not run: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or (
            f"exit {result.returncode}"
        )
        raise Refusal(f"eventctl {' '.join(args)} failed: {detail}")
    return result.stdout


def lifecycle_provenance(fd: int, status: dict) -> dict:
    raw, _ = _read_fd(fd, "lifecycle receipts", MAX_AUDIT_BYTES)
    if not raw or not raw.endswith(b"\n"):
        raise Refusal("lifecycle receipts are empty or have a torn tail")
    try:
        rows = [json.loads(line) for line in raw[:-1].split(b"\n")]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal(f"lifecycle receipts became unreadable: {exc}")
    if len(rows) != status.get("integrity", {}).get("receipts"):
        raise Refusal("eventctl status receipt count disagrees with the locked ledger")

    def transition_hash(name: str) -> str:
        matches = [
            row.get("receipt_sha256")
            for row in rows
            if isinstance(row, dict)
            and row.get("kind") == "transition"
            and row.get("outcome") == "ok"
            and row.get("transition") == name
        ]
        if len(matches) != 1 or not re.fullmatch(r"[0-9a-f]{64}", matches[0] or ""):
            raise Refusal(f"lifecycle lacks one exact successful {name} receipt")
        return matches[0]

    head = rows[-1].get("receipt_sha256") if isinstance(rows[-1], dict) else None
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{64}", head):
        raise Refusal("lifecycle head receipt hash is malformed")
    return {
        "lifecycle_receipts_sha256": sha256(raw),
        "lifecycle_head_sha256": head,
        "preflight_receipt_sha256": transition_hash("preflight_passed"),
        "source_attached_receipt_sha256": transition_hash("source_attached"),
        "lifecycle_receipt_count": len(rows),
    }


def validate_static(
    repo: Path,
    plan_path: Path,
    config_arg: str,
    state_arg: str,
    plan: dict,
    plan_raw: bytes,
    status: dict,
    lifecycle: dict,
) -> dict:
    if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
        raise Refusal("only an immutable schema-3 RunPlan is eligible")
    if plan.get("lane") != "phone":
        raise Refusal("held admission receipts are phone-lane only")
    if plan.get("execution_enabled") is not True or plan.get("capture_only") is not False:
        raise Refusal("RunPlan does not describe an execution-enabled live arm")
    event_id = plan.get("event_id")
    run_id = plan.get("run_id")
    plan_hash = plan.get("plan_sha256")
    if not all(isinstance(x, str) and x for x in (event_id, run_id, plan_hash)):
        raise Refusal("RunPlan event/run/hash identity is malformed")
    if not re.fullmatch(r"[0-9a-f]{64}", plan_hash):
        raise Refusal("RunPlan plan_sha256 is malformed")

    run_dir = plan_path.parent.parent
    if plan_path.name != "run-plan.json" or plan_path.parent.name != "control":
        raise Refusal("--run-plan must name control/run-plan.json exactly")
    if run_dir.name != run_id or run_dir.parent.name != event_id:
        raise Refusal("RunPlan path does not match its event/run identity")
    expected_run_dir = repo / "var" / "runs" / event_id / run_id
    if run_dir != expected_run_dir:
        raise Refusal(
            f"RunPlan is not at the canonical current-run path {expected_run_dir}"
        )

    config_rel = plan.get("config_path")
    if not safe_relative(config_rel):
        raise Refusal("RunPlan config_path is not a safe repository-relative path")
    pinned_config = repo / config_rel
    pinned_raw, _ = read_regular_bytes(pinned_config, "RunPlan-pinned config")
    config_path = input_path(config_arg, repo, "config")
    selected_raw, _ = read_regular_bytes(config_path, "selected config")
    try:
        pinned_real = pinned_config.resolve(strict=True)
        selected_real = config_path.resolve(strict=True)
    except OSError as exc:
        raise Refusal(f"cannot resolve selected/RunPlan config: {exc}")
    try:
        pinned_real.relative_to(repo)
    except ValueError:
        raise Refusal("RunPlan config escapes the repository")
    if selected_real != pinned_real:
        raise Refusal(
            f"selected config {selected_real} is not RunPlan config {pinned_real}"
        )
    if selected_raw != pinned_raw or sha256(pinned_raw) != plan.get("config_sha256"):
        raise Refusal("current config bytes do not match the immutable RunPlan hash")
    try:
        config = tomllib.loads(pinned_raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise Refusal(f"RunPlan config is not valid TOML: {exc}")
    if config.get("event", {}).get("id") != event_id:
        raise Refusal("config event id does not match the RunPlan")
    sources = config.get("sources")
    if not isinstance(sources, list) or not any(
        isinstance(source, dict) and source.get("kind") == "twilio_stream"
        for source in sources
    ):
        raise Refusal("config has no Twilio stream source")

    event_match = EVENT_ISSUER_RE.match(event_id)
    market_match = EVENT_ISSUER_RE.match(str(plan.get("market_event_ticker", "")))
    if not event_match or not market_match:
        raise Refusal("cannot derive an exact registry issuer from event/market identity")
    issuer = event_match.group(1)
    if market_match.group(1) != issuer:
        raise Refusal("RunPlan event and market identities name different issuers")

    if plan.get("canonical_state_root") != "state":
        raise Refusal("live phone RunPlan must use canonical_state_root=state")
    canonical_root = validate_directory(repo / "state", "canonical state root")
    expected_state = canonical_root / event_id
    state_path = input_path(state_arg, repo, "state directory")
    selected_state = validate_directory(state_path, "selected state directory")
    if selected_state != expected_state:
        raise Refusal(
            f"selected state directory {selected_state} is not current event state {expected_state}"
        )
    state_info = selected_state.lstat()

    if status.get("schema") != 1:
        raise Refusal("eventctl status schema is unsupported")
    if (
        status.get("event") != event_id
        or status.get("run") != run_id
        or status.get("lane") != "phone"
    ):
        raise Refusal("eventctl status identity does not match the RunPlan")
    integrity = status.get("integrity")
    state = status.get("state")
    if not isinstance(integrity, dict) or integrity.get("ok") is not True:
        raise Refusal("lifecycle integrity is not clean")
    if not isinstance(state, dict) or state.get("active") is not True:
        raise Refusal("RunPlan lifecycle is terminal, archived, or inactive")
    reached_rows = state.get("reached")
    if not isinstance(reached_rows, list):
        raise Refusal("eventctl status has no reached-transition list")
    reached = {
        row.get("transition")
        for row in reached_rows
        if isinstance(row, dict) and isinstance(row.get("transition"), str)
    }
    required = plan.get("required_prep_receipts")
    if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
        raise Refusal("RunPlan required_prep_receipts is malformed")
    needed = set(required) | {"preflight_passed", "launched", "source_attached"}
    missing = sorted(needed - reached)
    if missing:
        raise Refusal(f"current lifecycle is not admission-ready; missing {missing}")

    return {
        "repo": repo,
        "plan_path": plan_path,
        "plan_raw": plan_raw,
        "plan": plan,
        "run_dir": run_dir,
        "event_id": event_id,
        "run_id": run_id,
        "plan_sha256": plan_hash,
        "config_path": pinned_config,
        "config_rel": config_rel,
        "config_raw": pinned_raw,
        "config_sha256": plan["config_sha256"],
        "issuer": issuer,
        "state": selected_state,
        "state_identity": (state_info.st_dev, state_info.st_ino),
        "registry": repo / REGISTRY_REL,
        **lifecycle,
    }


def validate_dynamic(static: dict) -> dict:
    require_directory_identity(
        static["state"], static["state_identity"], "selected state directory"
    )
    config_raw, _ = read_regular_bytes(static["config_path"], "RunPlan-pinned config")
    if config_raw != static["config_raw"] or sha256(config_raw) != static["config_sha256"]:
        raise Refusal("config changed after RunPlan/lifecycle verification")

    registry_raw, _ = read_regular_bytes(static["registry"], "canonical admission registry")
    try:
        registry = tomllib.loads(registry_raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise Refusal(f"canonical admission registry is not valid TOML: {exc}")
    entry = registry.get(static["issuer"])
    if not isinstance(entry, dict):
        raise Ineligible(
            f"canonical registry has no exact [{static['issuer']}] issuer"
        )
    admission = entry.get("admission")
    if admission != "held":
        shown = "missing/unknown" if admission is None else repr(admission)
        raise Ineligible(
            f"issuer [{static['issuer']}] admission={shown} is ineligible; only "
            "a fresh probe-proven 'held' verdict may mint this receipt"
        )
    probed_raw = entry.get("admission_probed")
    if not isinstance(probed_raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", probed_raw):
        raise Ineligible("held registry verdict has no exact YYYY-MM-DD probe date")
    try:
        probed = dt.date.fromisoformat(probed_raw)
    except ValueError as exc:
        raise Ineligible(f"held registry probe date is invalid: {exc}")
    today = dt.datetime.now(dt.timezone.utc).date()
    age_days = (today - probed).days
    if age_days < 0:
        raise Ineligible(f"held registry probe date {probed_raw} is in the future")
    if age_days > MAX_PROBE_AGE_DAYS:
        raise Ineligible(
            f"held registry verdict is stale ({age_days}d > {MAX_PROBE_AGE_DAYS}d)"
        )

    state = static["state"]
    challenge_path = state / "admission.challenge.json"
    source_path = state / "source.attached"
    stream_path = state / "stream.connected"
    receipt_path = state / "admission.receipt.json"
    challenge, challenge_raw, challenge_stat = read_regular_json(
        challenge_path, "admission challenge"
    )
    source, source_raw, _ = read_regular_json(source_path, "source attachment")
    stream, stream_raw, _ = read_regular_json(stream_path, "stream marker")

    if challenge.get("schema_version") != 1 or isinstance(
        challenge.get("schema_version"), bool
    ):
        raise Refusal("admission challenge schema is unsupported")
    if challenge.get("event_id") != static["event_id"]:
        raise Refusal("admission challenge event does not match config/RunPlan")
    nonce = challenge.get("nonce")
    if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
        raise Refusal("admission challenge nonce is malformed")
    pid = challenge.get("pid")
    if not is_int(pid) or pid <= 0 or pid > 2_147_483_647:
        raise Refusal("admission challenge PID is malformed")
    if not process_alive(pid):
        raise Refusal(f"admission challenge owner PID {pid} is not alive")
    if resolve_declared_path(challenge.get("receipt_path"), static["repo"], "receipt_path") != receipt_path:
        raise Refusal("challenge receipt_path does not name this event state")
    if resolve_declared_path(
        challenge.get("stream_connected_path"), static["repo"], "stream_connected_path"
    ) != stream_path:
        raise Refusal("challenge stream_connected_path does not name this event state")

    if source.get("schema_version") != 1 or isinstance(source.get("schema_version"), bool):
        raise Refusal("source attachment schema is unsupported")
    expected_source = {
        "event_id": static["event_id"],
        "run_id": static["run_id"],
        "plan_sha256": static["plan_sha256"],
        "pid": pid,
    }
    for key, expected in expected_source.items():
        if source.get(key) != expected:
            raise Refusal(f"source attachment {key} does not match current RunPlan/challenge")
    accepted = source.get("accepted_frames")
    source_unix_ns = source.get("unix_ns")
    if not is_int(accepted) or accepted <= 0:
        raise Refusal("source attachment has no accepted detector-path frames")
    if not is_int(source_unix_ns) or source_unix_ns <= 0:
        raise Refusal("source attachment timestamp is malformed")

    call_sid = stream.get("call_sid")
    attempt = stream.get("attempt")
    stream_unix_ts = stream.get("unix_ts")
    if not isinstance(call_sid, str) or not CALL_SID_RE.fullmatch(call_sid):
        raise Refusal("current stream marker has no valid Twilio CallSid")
    if not is_int(attempt) or attempt < 0 or attempt > 0xFFFF_FFFF:
        raise Refusal("current stream marker has no valid nonnegative attempt")
    if not is_int(stream_unix_ts) or stream_unix_ts <= 0:
        raise Refusal("current stream marker timestamp is malformed")

    challenge_second = challenge_stat.st_mtime_ns // 1_000_000_000
    if stream_unix_ts < challenge_second:
        raise Refusal("stream marker predates the current admission challenge")
    if source_unix_ns < challenge_stat.st_mtime_ns:
        raise Refusal("source attachment predates the current admission challenge")
    now_ns = time.time_ns()
    if source_unix_ns > now_ns + 300_000_000_000 or stream_unix_ts > now_ns // 1_000_000_000 + 300:
        raise Refusal("runtime marker timestamp is implausibly in the future")

    return {
        "registry_raw": registry_raw,
        "registry_sha256": sha256(registry_raw),
        "admission_probed": probed_raw,
        "admission_age_days": age_days,
        "challenge_raw": challenge_raw,
        "source_raw": source_raw,
        "stream_raw": stream_raw,
        "nonce": nonce,
        "pid": pid,
        "call_sid": call_sid,
        "stream_attempt": attempt,
        "stream_unix_ts": stream_unix_ts,
        "source_attached_unix_ns": source_unix_ns,
        "receipt_path": receipt_path,
    }


def dynamic_fingerprint(dynamic: dict) -> tuple:
    return (
        dynamic["registry_raw"],
        dynamic["challenge_raw"],
        dynamic["source_raw"],
        dynamic["stream_raw"],
        dynamic["nonce"],
        dynamic["pid"],
        dynamic["call_sid"],
        dynamic["stream_attempt"],
    )


def parse_audit(fd: int) -> tuple[bytes, list[dict]]:
    raw, info = _read_fd(fd, "admission assertion journal", MAX_AUDIT_BYTES)
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise Refusal("admission assertion journal permissions must be 0600-style")
    if raw and not raw.endswith(b"\n"):
        raise Refusal("admission assertion journal has a torn final line")
    rows = []
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Refusal(f"admission assertion journal line {number} is invalid: {exc}")
        if not isinstance(row, dict):
            raise Refusal(f"admission assertion journal line {number} is not an object")
        claimed = row.get("assertion_sha256")
        if claimed is not None:
            unhashed = {k: v for k, v in row.items() if k != "assertion_sha256"}
            if not isinstance(claimed, str) or claimed != sha256(canonical_bytes(unhashed)):
                raise Refusal(f"admission assertion journal line {number} hash mismatch")
        if row.get("kind") in (
            "held_admission_assertion",
            "operator_admission_assertion",
        ) and claimed is None:
            raise Refusal(
                f"admission assertion journal line {number} lacks its assertion hash"
            )
        if row.get("kind") == "held_admission_assertion" and (
            row.get("schema_version") != 1
            or not is_int(row.get("asserted_unix_ns"))
            or row.get("asserted_unix_ns") <= 0
        ):
            raise Refusal(
                f"admission assertion journal line {number} has malformed held assertion metadata"
            )
        rows.append(row)
    return raw, rows


def assertion_core(static: dict, dynamic: dict) -> dict:
    return {
        "schema_version": 1,
        "kind": "held_admission_assertion",
        "event_id": static["event_id"],
        "run_id": static["run_id"],
        "plan_sha256": static["plan_sha256"],
        "config_path": static["config_rel"],
        "config_sha256": static["config_sha256"],
        "registry_path": REGISTRY_REL,
        "registry_sha256": dynamic["registry_sha256"],
        "issuer": static["issuer"],
        "admission": "held",
        "admission_probed": dynamic["admission_probed"],
        "admission_age_days": dynamic["admission_age_days"],
        "nonce": dynamic["nonce"],
        "status": "admitted",
        "call_sid": dynamic["call_sid"],
        "stream_attempt": dynamic["stream_attempt"],
        "stream_unix_ts": dynamic["stream_unix_ts"],
        "challenge_pid": dynamic["pid"],
        "source_attached_unix_ns": dynamic["source_attached_unix_ns"],
        "assertion_method": METHOD,
        "asserted_by": ASSERTED_BY,
        "lifecycle_receipts_sha256": static["lifecycle_receipts_sha256"],
        "lifecycle_head_sha256": static["lifecycle_head_sha256"],
        "preflight_receipt_sha256": static["preflight_receipt_sha256"],
        "source_attached_receipt_sha256": static[
            "source_attached_receipt_sha256"
        ],
        "lifecycle_receipt_count": static["lifecycle_receipt_count"],
    }


def assertion_matches(row: dict, core: dict) -> bool:
    return row.get("kind") == "held_admission_assertion" and all(
        row.get(key) == value for key, value in core.items()
    )


def receipt_from(assertion: dict) -> dict:
    return {
        "schema_version": 1,
        "event_id": assertion["event_id"],
        "run_id": assertion["run_id"],
        "plan_sha256": assertion["plan_sha256"],
        "nonce": assertion["nonce"],
        "status": "admitted",
        "call_sid": assertion["call_sid"],
        "stream_attempt": assertion["stream_attempt"],
        "assertion_method": METHOD,
        "asserted_by": ASSERTED_BY,
        "asserted_unix_ns": assertion["asserted_unix_ns"],
        "challenge_pid": assertion["challenge_pid"],
        "assertion_sha256": assertion["assertion_sha256"],
    }


def receipt_base_matches(receipt: dict, static: dict, dynamic: dict) -> bool:
    expected = {
        "schema_version": 1,
        "event_id": static["event_id"],
        "nonce": dynamic["nonce"],
        "status": "admitted",
        "call_sid": dynamic["call_sid"],
        "stream_attempt": dynamic["stream_attempt"],
    }
    return all(receipt.get(key) == value for key, value in expected.items())


def validate_existing_receipt(
    prior, rows: list[dict], static: dict, dynamic: dict
) -> str:
    """Return current|stale|absent, refusing an incompatible artifact."""

    if prior is None:
        return "absent"
    receipt, _, _ = prior
    if receipt_base_matches(receipt, static, dynamic):
        method = receipt.get("assertion_method")
        if method == "explicit_operator_pass_cli":
            fields = (
                "schema_version",
                "event_id",
                "nonce",
                "status",
                "call_sid",
                "stream_attempt",
                "assertion_method",
                "asserted_by",
                "asserted_unix_ns",
                "challenge_pid",
            )
            if not any(
                row.get("kind") == "operator_admission_assertion"
                and all(row.get(key) == receipt.get(key) for key in fields)
                for row in rows
            ):
                raise Refusal(
                    "operator receipt has no matching durable operator assertion"
                )
            return "current-operator"
        if method != METHOD:
            raise Refusal("current receipt has an unknown assertion method")
        if receipt.get("run_id") != static["run_id"] or receipt.get(
            "plan_sha256"
        ) != static["plan_sha256"]:
            raise Refusal("held receipt is not bound to the current RunPlan")
        assertion_hash = receipt.get("assertion_sha256")
        match = next(
            (
                row
                for row in rows
                if row.get("assertion_sha256") == assertion_hash
                and row.get("kind") == "held_admission_assertion"
            ),
            None,
        )
        if match is None:
            raise Refusal("held receipt has no matching durable audit assertion")
        for key in (
            "event_id",
            "run_id",
            "plan_sha256",
            "nonce",
            "status",
            "call_sid",
            "stream_attempt",
            "assertion_method",
            "asserted_by",
            "asserted_unix_ns",
            "challenge_pid",
        ):
            if receipt.get(key) != match.get(key):
                raise Refusal(f"held receipt disagrees with its audit assertion on {key}")
        return "current-held"

    # A redial deliberately leaves a now-stale receipt under the SAME live
    # challenge.  It is eligible for replacement only if it is recognizable as
    # a previously valid stream-bound receipt; wrong event/nonce/status is a
    # concurrent-arm or tampering signal and must never be overwritten.
    same_challenge = (
        receipt.get("schema_version") == 1
        and receipt.get("event_id") == static["event_id"]
        and receipt.get("nonce") == dynamic["nonce"]
        and receipt.get("status") == "admitted"
    )
    old_sid = receipt.get("call_sid")
    old_attempt = receipt.get("stream_attempt")
    known_method = receipt.get("assertion_method") in (
        METHOD,
        "explicit_operator_pass_cli",
    )
    if (
        same_challenge
        and known_method
        and isinstance(old_sid, str)
        and CALL_SID_RE.fullmatch(old_sid)
        and is_int(old_attempt)
        and old_attempt >= 0
    ):
        return "stale-stream"
    raise Refusal("existing admission receipt is incompatible; refusing to overwrite it")


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_receipt(path: Path, receipt: dict) -> None:
    encoded = json.dumps(receipt, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = -1
    try:
        fd = os.open(tmp, flags, 0o600)
        written = os.write(fd, encoded)
        if written != len(encoded):
            raise Refusal(
                f"short write while publishing receipt ({written}/{len(encoded)})"
            )
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
        fsync_directory(path.parent)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def produce(args) -> tuple[str, dict | None]:
    repo = repo_root()
    plan_path_arg = input_path(args.run_plan, repo, "RunPlan")
    plan, initial_plan_raw, _ = read_regular_json(plan_path_arg, "RunPlan")
    try:
        plan_path = plan_path_arg.resolve(strict=True)
    except OSError as exc:
        raise Refusal(f"cannot resolve RunPlan: {exc}")
    try:
        plan_path.relative_to(repo)
    except ValueError:
        raise Refusal("RunPlan escapes the repository")
    if plan_path.name != "run-plan.json" or plan_path.parent.name != "control":
        raise Refusal("--run-plan must name control/run-plan.json exactly")
    run_dir = plan_path.parent.parent
    receipts_path = run_dir / "control" / "receipts.jsonl"
    lifecycle_fd = _open_regular(
        receipts_path, os.O_RDONLY, "lifecycle receipts"
    )
    try:
        fcntl.flock(lifecycle_fd, fcntl.LOCK_SH)
        verify_out = run_eventctl(repo, "verify", "--run-dir", str(run_dir))
        if not verify_out.startswith("OK:"):
            raise Refusal("eventctl verify did not return an exact OK verdict")
        status_out = run_eventctl(
            repo, "status", "--run-dir", str(run_dir), "--json"
        )
        try:
            status = json.loads(status_out)
        except json.JSONDecodeError as exc:
            raise Refusal(f"eventctl status did not return valid JSON: {exc}")
        require_fd_still_at_path(
            lifecycle_fd, receipts_path, "lifecycle receipts"
        )

        current_plan, current_plan_raw, _ = read_regular_json(plan_path, "RunPlan")
        if current_plan_raw != initial_plan_raw or current_plan != plan:
            raise Refusal("RunPlan changed during verification")
        lifecycle = lifecycle_provenance(lifecycle_fd, status)
        static = validate_static(
            repo,
            plan_path,
            args.config,
            args.state_dir,
            current_plan,
            current_plan_raw,
            status,
            lifecycle,
        )

        # Validate before O_CREAT so ordinary refusals leave no assertion file.
        validate_dynamic(static)

        audit_path = static["state"] / "admission.assertions.jsonl"
        audit_fd = _open_regular(
            audit_path,
            os.O_RDWR | os.O_APPEND | os.O_CREAT,
            "admission assertion journal",
            0o600,
        )
        try:
            fcntl.flock(audit_fd, fcntl.LOCK_EX)
            require_fd_still_at_path(
                audit_fd, audit_path, "admission assertion journal"
            )
            _, rows = parse_audit(audit_fd)
            dynamic = validate_dynamic(static)
            prior = read_optional_json(
                dynamic["receipt_path"], "existing admission receipt"
            )
            prior_bytes = None if prior is None else prior[1]
            prior_state = validate_existing_receipt(prior, rows, static, dynamic)
            if prior_state in ("current-held", "current-operator"):
                return prior_state, None if prior is None else prior[0]

            core = assertion_core(static, dynamic)
            assertion = next(
                (row for row in rows if assertion_matches(row, core)), None
            )
            appended = False
            if assertion is None:
                assertion = dict(core)
                assertion["asserted_unix_ns"] = time.time_ns()
                assertion["assertion_sha256"] = sha256(canonical_bytes(assertion))
                line = canonical_bytes(assertion) + b"\n"
                written = os.write(audit_fd, line)
                if written != len(line):
                    try:
                        os.fsync(audit_fd)
                    finally:
                        raise Refusal(
                            "short write to admission assertion journal; receipt not published"
                        )
                os.fsync(audit_fd)
                # The first creation's directory entry must be durable before
                # the receipt can make the decision gate observable as open.
                fsync_directory(static["state"])
                appended = True

            # Re-check every mutable fact after the durable audit append.  A
            # process death, redial, marker replacement, or registry/config
            # edit leaves an audit trail but never publishes a stale receipt.
            final_dynamic = validate_dynamic(static)
            if dynamic_fingerprint(final_dynamic) != dynamic_fingerprint(dynamic):
                raise Refusal("admission inputs changed during assertion; receipt not published")
            current_prior = read_optional_json(
                dynamic["receipt_path"], "existing admission receipt"
            )
            current_prior_bytes = None if current_prior is None else current_prior[1]
            if current_prior_bytes != prior_bytes:
                raise Refusal("admission receipt changed concurrently; refusing to overwrite it")
            final_plan_raw, _ = read_regular_bytes(plan_path, "RunPlan")
            if final_plan_raw != static["plan_raw"]:
                raise Refusal("RunPlan changed before receipt publication")
            require_directory_identity(
                static["state"],
                static["state_identity"],
                "selected state directory",
            )
            require_fd_still_at_path(
                audit_fd, audit_path, "admission assertion journal"
            )
            require_fd_still_at_path(
                lifecycle_fd, receipts_path, "lifecycle receipts"
            )
            if not process_alive(dynamic["pid"]):
                raise Refusal("admission challenge owner exited before receipt publication")

            receipt = receipt_from(assertion)
            publish_receipt(dynamic["receipt_path"], receipt)
            return "published" if appended else "recovered", receipt
        finally:
            os.close(audit_fd)
    finally:
        os.close(lifecycle_fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Mint the current stream's admission receipt from the canonical "
            "registry's fresh probe-proven held verdict."
        )
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-plan", required=True)
    parser.add_argument("--state-dir", required=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outcome, receipt = produce(args)
    except Ineligible as exc:
        print(f"INELIGIBLE held admission receipt: {exc}", file=sys.stderr)
        return EXIT_INELIGIBLE
    except Refusal as exc:
        print(f"REFUSING held admission receipt: {exc}", file=sys.stderr)
        return EXIT_INTEGRITY
    except Exception as exc:
        print(
            f"REFUSING held admission receipt: unexpected local integrity error: {exc}",
            file=sys.stderr,
        )
        return EXIT_INTEGRITY

    if outcome == "current-operator":
        print(
            "admission already open for the current challenge/stream via an "
            "explicit operator assertion; preserved"
        )
    elif outcome == "current-held":
        print(
            f"held admission already asserted for event={receipt['event_id']} "
            f"call_sid={receipt['call_sid']} stream_attempt={receipt['stream_attempt']}"
        )
    else:
        verb = "RECOVERED" if outcome == "recovered" else "ASSERTED"
        print(
            f"held admission {verb} event={receipt['event_id']} "
            f"run={receipt['run_id']} call_sid={receipt['call_sid']} "
            f"stream_attempt={receipt['stream_attempt']}; "
            f"receipt={Path(args.state_dir) / 'admission.receipt.json'}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
