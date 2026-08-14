#!/usr/bin/env python3
"""Prove a loaded schedule or observe one live run without mutating it.

The OS-specific installers own descriptor generation and readback.  This
helper owns the shared trust boundary: a scheduler job is not a lifecycle
fact until its *loaded* representation is bound to the exact schema-3
RunPlan, canonical run directory, execution host, fire instant, argv, and
identity environment.  The proof is durable before eventctl appends the
``scheduled`` receipt, so a crash in between is recovered by an idempotent
retry instead of producing an unexplainable receipt.

No scheduler command is executed here.  ``readback`` is a normalized JSON
observation produced from launchctl/systemctl by the relevant installer.

The ``monitor`` mode is the read-only live supervision door. It consumes the
RunPlan/lifecycle prefix, EVIDENCE-owned progress sidecars, typed telemetry,
and redial journal records. It writes only through platform.notify's
standalone alert ledger, which must be outside the run directory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


PROOF_SCHEMA = 1
READBACK_SCHEMA = 1
SYSTEMD_DRAIN_SECONDS = 150
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PROGRESS_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
REMOTE_HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._-]{0,31}@)?[A-Za-z0-9][A-Za-z0-9._-]{0,253}$"
)
TERMINALS = {"completed", "failed", "aborted", "descope"}

REMOTE_READ_SCRIPT = r'''import json, os, stat, sys
root = os.path.realpath(sys.argv[1])
cap = int(sys.argv[2])
files = {}
used = 0
for relative in sys.argv[3:]:
    parts = relative.split("/")
    if not relative or relative.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise SystemExit(f"refused relative path: {relative}")
    path = os.path.join(root, *parts)
    resolved = os.path.realpath(path)
    if os.path.commonpath((root, resolved)) != root:
        raise SystemExit(f"path escapes remote repository: {relative}")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        files[relative] = None
        continue
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"not a regular non-symlink file: {relative}")
    used += info.st_size
    if used > cap:
        raise SystemExit(f"remote read exceeds {cap} byte cap")
    with open(path, "rb") as handle:
        raw = handle.read(info.st_size + 1)
    if len(raw) != info.st_size:
        raise SystemExit(f"file changed during bounded read: {relative}")
    try:
        files[relative] = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SystemExit(f"file is not UTF-8 text: {relative}")
print(json.dumps({"schema": 1, "kind": "remote-read-snapshot", "files": files}, sort_keys=True))
'''


class Refusal(Exception):
    pass


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def object_hash(value: dict, omit: str) -> str:
    return hashlib.sha256(
        canonical_bytes({k: v for k, v in value.items() if k != omit})
    ).hexdigest()


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: object, field: str) -> dt.datetime:
    if not isinstance(value, str):
        raise Refusal(f"{field} must be a UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Refusal(f"{field} is not ISO-8601: {exc}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise Refusal(f"{field} must carry UTC")
    return parsed.astimezone(dt.timezone.utc)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, body: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        view = memoryview(body)
        while view:
            wrote = os.write(fd, view)
            if wrote <= 0:
                raise Refusal(f"short write while publishing {path}")
            view = view[wrote:]
        os.fsync(fd)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def load_regular_json(path: Path, what: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise Refusal(f"{what} must be a regular non-symlink file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Refusal(f"{what} does not parse as JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise Refusal(f"{what} must be a JSON object")
    return value


def load_json_text(raw: str, what: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal(f"{what} does not parse as JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise Refusal(f"{what} must be a JSON object")
    return value


def resolve_context(run_raw: str) -> tuple[Path, Path, dict]:
    repo = Path(os.environ.get("EARSHOT_REPO", Path(__file__).parent.parent)).resolve()
    run = Path(run_raw)
    run = (run if run.is_absolute() else repo / run).resolve()
    canonical_root = (repo / "var" / "runs").resolve()
    try:
        relative = run.relative_to(canonical_root)
    except ValueError as exc:
        raise Refusal(f"run directory is outside canonical {canonical_root}") from exc
    if len(relative.parts) != 2:
        raise Refusal("run directory must be exactly var/runs/<EVENT_ID>/<RUN_ID>")
    eventctl = repo / "scripts" / "eventctl.py"
    if not eventctl.is_file():
        raise Refusal(f"eventctl is missing: {eventctl}")
    verify = subprocess.run(
        [sys.executable, str(eventctl), "verify", "--run-dir", str(run)],
        cwd=repo,
        env={**os.environ, "EARSHOT_REPO": str(repo)},
        text=True,
        capture_output=True,
        check=False,
    )
    if verify.returncode != 0:
        detail = (verify.stderr or verify.stdout).strip()
        raise Refusal(f"eventctl verify refused the run: {detail}")
    plan = load_regular_json(run / "control" / "run-plan.json", "RunPlan")
    if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
        raise Refusal("schedule proof requires a schema-3 RunPlan")
    if plan.get("execution_enabled") is not True or plan.get("capture_only") is not False:
        raise Refusal("scheduler proof requires an execution-enabled RunPlan")
    if relative.parts != (plan.get("event_id"), plan.get("run_id")):
        raise Refusal("RunPlan identity does not match the canonical directory")
    return repo, run, plan


@dataclass(frozen=True)
class ProgressSpec:
    name: str
    path: Path
    mode: str


@dataclass
class ProgressState:
    bytes_seen: int | None = None
    changed_monotonic: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class RemoteSnapshot:
    files: dict[str, str | None]
    fetched_at_utc: str
    fetched_monotonic: float

    def content(self, repo: Path, path: Path) -> str | None:
        try:
            relative = path.resolve().relative_to(repo).as_posix()
        except ValueError as exc:
            raise Refusal(f"remote observation path escapes repository: {path}") from exc
        if relative not in self.files:
            raise Refusal(f"remote snapshot omitted requested path: {relative}")
        return self.files[relative]


class RemoteReader:
    def __init__(
        self,
        args: argparse.Namespace,
        repo: Path,
        paths: list[Path],
    ) -> None:
        self.args = args
        self.repo = repo
        self.paths = paths
        self.cached: RemoteSnapshot | None = None

    def read(self) -> RemoteSnapshot:
        now_mono = time.monotonic()
        if (
            self.cached is not None
            and now_mono - self.cached.fetched_monotonic < self.args.remote_refresh_seconds
        ):
            return self.cached
        relative: list[str] = []
        for path in self.paths:
            try:
                key = path.resolve().relative_to(self.repo).as_posix()
            except ValueError as exc:
                raise Refusal(f"remote read path escapes repository: {path}") from exc
            if key not in relative:
                relative.append(key)
        remote_argv = [
            "python3",
            "-c",
            REMOTE_READ_SCRIPT,
            self.args.remote_repo,
            str(self.args.remote_max_bytes),
            *relative,
        ]
        remote_command = " ".join(shlex.quote(value) for value in remote_argv)
        connect_timeout = max(1, int(self.args.remote_timeout_seconds + 0.999))
        try:
            proc = subprocess.run(
                [
                    self.args.ssh_command,
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    f"ConnectTimeout={connect_timeout}",
                    "--",
                    self.args.remote_host,
                    remote_command,
                ],
                cwd=self.repo,
                env={**os.environ, "EARSHOT_REPO": str(self.repo)},
                text=True,
                capture_output=True,
                check=False,
                timeout=self.args.remote_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise Refusal(f"bounded remote read failed: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise Refusal(f"bounded remote read returned {proc.returncode}: {detail}")
        value = load_json_text(proc.stdout, "bounded remote read response")
        if value.get("schema") != 1 or value.get("kind") != "remote-read-snapshot":
            raise Refusal("bounded remote read response has unsupported schema/kind")
        files = value.get("files")
        if not isinstance(files, dict) or set(files) != set(relative):
            raise Refusal("bounded remote read response path set differs from request")
        if any(item is not None and not isinstance(item, str) for item in files.values()):
            raise Refusal("bounded remote read response contains a non-text file")
        self.cached = RemoteSnapshot(files, now_utc(), time.monotonic())
        return self.cached


def resolve_monitor_context(run_raw: str) -> tuple[Path, Path, dict]:
    """Resolve identity without taking eventctl's shared ledger lock."""
    repo = Path(os.environ.get("EARSHOT_REPO", Path(__file__).parent.parent)).resolve()
    run = Path(run_raw)
    run = (run if run.is_absolute() else repo / run).resolve()
    canonical_root = (repo / "var" / "runs").resolve()
    try:
        relative = run.relative_to(canonical_root)
    except ValueError as exc:
        raise Refusal(f"run directory is outside canonical {canonical_root}") from exc
    if len(relative.parts) != 2 or run.is_symlink() or not run.is_dir():
        raise Refusal("monitor requires var/runs/<EVENT_ID>/<RUN_ID> as a real directory")
    plan = load_regular_json(run / "control" / "run-plan.json", "RunPlan")
    if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
        raise Refusal("monitor requires a schema-3 RunPlan")
    if relative.parts != (plan.get("event_id"), plan.get("run_id")):
        raise Refusal("RunPlan identity does not match the canonical directory")
    if not SHA_RE.fullmatch(str(plan.get("plan_sha256", ""))):
        raise Refusal("RunPlan plan_sha256 is invalid")
    return repo, run, plan


def read_jsonl_prefix(path: Path, what: str) -> list[dict]:
    """Read only newline-complete rows; never lock or repair a live writer."""
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise Refusal(f"{what} must be a regular non-symlink file: {path}")
    raw = path.read_bytes()
    return read_jsonl_bytes(raw, what)


def read_jsonl_text(raw: str | None, what: str) -> list[dict]:
    if raw is None:
        return []
    return read_jsonl_bytes(raw.encode("utf-8"), what)


def read_jsonl_bytes(raw: bytes, what: str) -> list[dict]:
    if raw and not raw.endswith(b"\n"):
        raw = raw[: raw.rfind(b"\n") + 1] if b"\n" in raw else b""
    rows: list[dict] = []
    for index, line in enumerate(raw.splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Refusal(f"{what} line {index} is invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise Refusal(f"{what} line {index} is not an object")
        rows.append(value)
    return rows


def lifecycle_view(
    repo: Path,
    run: Path,
    remote: RemoteSnapshot | None = None,
) -> tuple[list[dict], set[str], dict[str, dt.datetime]]:
    ledger = run / "control" / "receipts.jsonl"
    rows = (
        read_jsonl_text(remote.content(repo, ledger), "remote lifecycle ledger")
        if remote is not None
        else read_jsonl_prefix(ledger, "lifecycle ledger")
    )
    reached: set[str] = set()
    times: dict[str, dt.datetime] = {}
    for row in rows:
        if row.get("event") not in (None, run.parent.name) or row.get("run") not in (
            None,
            run.name,
        ):
            raise Refusal("lifecycle receipt identity differs from run directory")
        transition = row.get("transition") if row.get("kind") == "transition" else None
        if isinstance(transition, str):
            reached.add(transition)
            if transition not in times:
                times[transition] = parse_utc(row.get("ts"), f"{transition} receipt ts")
    return rows, reached, times


def parse_progress_spec(raw: str, run: Path) -> ProgressSpec:
    try:
        name, remainder = raw.split("=", 1)
        relative, mode = remainder.rsplit(":", 1)
    except ValueError as exc:
        raise Refusal("--progress must be NAME=RUN_RELATIVE_PATH:growth|heartbeat") from exc
    if not PROGRESS_NAME_RE.fullmatch(name) or mode not in ("growth", "heartbeat"):
        raise Refusal("progress name/mode is invalid")
    path = (run / relative).resolve()
    try:
        path.relative_to(run)
    except ValueError as exc:
        raise Refusal(f"progress path escapes run directory: {relative}") from exc
    return ProgressSpec(name, path, mode)


def default_progress_specs(repo: Path, run: Path, plan: dict) -> list[ProgressSpec]:
    artifacts = plan.get("artifacts") or {}
    mapping = [
        ("audio", "audio_raw", "growth"),
        ("book", "book_tape", "growth"),
        ("orders", "order_evidence", "heartbeat"),
    ]
    config_raw = plan.get("config_path")
    if isinstance(config_raw, str):
        config_path = (repo / config_raw).resolve()
        try:
            config_path.relative_to(repo)
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (ValueError, OSError, tomllib.TOMLDecodeError) as exc:
            raise Refusal(f"monitor cannot read pinned config transport: {exc}") from exc
        if (config.get("kalshi") or {}).get("transport", "rest") == "fix":
            mapping.append(("fix", "fix_observation_tape", "heartbeat"))
    specs: list[ProgressSpec] = []
    for name, key, mode in mapping:
        relative = artifacts.get(key)
        if isinstance(relative, str):
            path = (run / f"{relative}.progress.json").resolve()
            try:
                path.relative_to(run)
            except ValueError as exc:
                raise Refusal(f"RunPlan progress path for {name} escapes run") from exc
            specs.append(ProgressSpec(name, path, mode))
    return specs


def progress_record(
    repo: Path,
    spec: ProgressSpec,
    plan: dict,
    remote: RemoteSnapshot | None = None,
) -> dict:
    if remote is None:
        value = load_regular_json(spec.path, f"{spec.name} progress")
    else:
        raw = remote.content(repo, spec.path)
        if raw is None:
            raise Refusal(f"{spec.name} progress is absent from remote snapshot")
        value = load_json_text(raw, f"remote {spec.name} progress")
    if value.get("schema") != 1 or value.get("kind") not in (
        "evidence-progress",
        "tape-progress",
    ):
        raise Refusal(f"{spec.name} progress has unsupported schema/kind")
    byte_count = value.get("bytes")
    append_ns = value.get("last_append_monotonic_ns")
    cadence = value.get("cadence_seconds")
    if type(byte_count) is not int or byte_count < 0:
        raise Refusal(f"{spec.name} progress bytes is invalid")
    if type(append_ns) is not int or append_ns < 0:
        raise Refusal(f"{spec.name} progress last_append_monotonic_ns is invalid")
    if not isinstance(cadence, (int, float)) or isinstance(cadence, bool) or not 0 < cadence <= 300:
        raise Refusal(f"{spec.name} progress cadence_seconds is invalid")
    if value.get("run_id") not in (None, plan["run_id"]):
        raise Refusal(f"{spec.name} progress run_id differs from RunPlan")
    if value.get("plan_sha256") not in (None, plan["plan_sha256"]):
        raise Refusal(f"{spec.name} progress plan_sha256 differs from RunPlan")
    value["_bytes"] = byte_count
    value["_append_ns"] = append_ns
    value["_cadence"] = float(cadence)
    return value


def latest_telemetry(
    repo: Path,
    path: Path | None,
    plan: dict,
    remote: RemoteSnapshot | None = None,
) -> dict | None:
    if path is None:
        return None
    if remote is None:
        if not path.exists():
            return None
        rows = read_jsonl_prefix(path, "live telemetry")
    else:
        rows = read_jsonl_text(remote.content(repo, path), "remote live telemetry")
    if not rows:
        return None
    value = rows[-1]
    if value.get("schema") != 1 or value.get("kind") != "live-telemetry":
        raise Refusal("live telemetry has unsupported schema/kind")
    if value.get("run_id") not in (None, plan["run_id"]):
        raise Refusal("live telemetry run_id differs from RunPlan")
    return value


def redial_intervals(
    repo: Path,
    journal: Path | None,
    remote: RemoteSnapshot | None = None,
) -> list[float]:
    if journal is None:
        return []
    if remote is None:
        if not journal.exists():
            return []
        rows = read_jsonl_prefix(journal, "event journal")
    else:
        rows = read_jsonl_text(remote.content(repo, journal), "remote event journal")
    stamps: list[dt.datetime] = []
    for row in rows:
        if row.get("actor") == "redial-supervisor.sh" and row.get("action") == "redial":
            stamps.append(parse_utc(row.get("ts"), "redial journal ts"))
    return [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]


def status(repo: Path, run: Path) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "eventctl.py"),
            "status",
            "--run-dir",
            str(run),
            "--json",
        ],
        cwd=repo,
        env={**os.environ, "EARSHOT_REPO": str(repo)},
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise Refusal(f"eventctl status failed: {(proc.stderr or proc.stdout).strip()}")
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Refusal(f"eventctl status emitted invalid JSON: {exc}") from exc
    return value


def reached_from(value: dict) -> set[str]:
    try:
        return {r["transition"] for r in value["state"]["reached"]}
    except (KeyError, TypeError) as exc:
        raise Refusal("eventctl status has an unsupported shape") from exc


def expected_fire(plan: dict, override: str | None = None) -> str:
    start = parse_utc(plan.get("starts_at_utc"), "RunPlan starts_at_utc")
    if override is not None:
        if plan.get("lane") != "webcast":
            raise Refusal("--fire-at-utc override is webcast-only")
        fire = parse_utc(override, "--fire-at-utc")
        if fire <= dt.datetime.now(dt.timezone.utc):
            raise Refusal("--fire-at-utc must be strictly future")
        if fire > start:
            raise Refusal("--fire-at-utc may not be after RunPlan starts_at_utc")
        return fire.strftime("%Y-%m-%dT%H:%M:%SZ")
    lead = 25 if plan.get("lane") == "phone" else 75
    fire = start - dt.timedelta(minutes=lead)
    return fire.strftime("%Y-%m-%dT%H:%M:%SZ")


def expected_program(repo: Path, run: Path, plan: dict) -> list[str]:
    if plan.get("lane") == "phone":
        return [str(repo / "scripts" / "event-autorun.sh"), "--run-dir", str(run)]
    if plan.get("lane") == "webcast":
        argv = [str(repo / "scripts" / "arm-on-url.sh"), "--run-dir", str(run)]
        if plan.get("execution_enabled") is True:
            argv.extend(["--i-am-live", "--fillable"])
        return argv
    raise Refusal(f"unsupported RunPlan lane {plan.get('lane')!r}")


def expected_environment(
    repo: Path, run: Path, plan: dict, scheduler: str
) -> dict[str, str]:
    value = {
        "EARSHOT_EXECUTION_HOST": plan["execution_host_id"],
        "EARSHOT_REPO": str(repo),
        "EARSHOT_RUN_DIR": str(run),
        "EARSHOT_RUN_PLAN_SHA256": plan["plan_sha256"],
    }
    if plan.get("lane") == "webcast":
        value["EARSHOT_ACCOUNT_ENV"] = str(resolve_account_profile(repo, plan))
        if scheduler == "launchd":
            # Pinned fire-time PATH (AMAT 2026-08-13): launchd's default
            # resolves python3 to Apple's 3.9 (no tomllib/playwright). The
            # Linux systemd-user path is intentionally left at its healthy
            # host default; a Homebrew override would be invalid on the box.
            value["PATH"] = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    return value


def resolve_account_profile(repo: Path, plan: dict) -> Path:
    """Resolve the non-secret account-file locator without sourcing the env."""
    env_rel = plan.get("event_env_path")
    if not env_rel:
        raise Refusal("webcast RunPlan has no pinned event env/account locator")
    env_path = (repo / env_rel).resolve()
    if not env_path.is_file() or env_path.is_symlink():
        raise Refusal(f"webcast event env is not a regular file: {env_path}")
    match = re.search(
        r'^\s*(?:export\s+)?(?:EARSHOT_ACCOUNT_PROFILE|ACCOUNT_ENV)=["\']([^"\']+)["\']',
        env_path.read_text(errors="replace"),
        re.M,
    )
    if not match:
        raise Refusal("webcast event env has no literal account profile locator")
    raw = match.group(1)
    home = str(Path.home())
    raw = raw.replace("${HOME}", home).replace("$HOME", home)
    if raw.startswith("~/"):
        raw = home + raw[1:]
    profile = Path(raw)
    if not profile.is_absolute():
        raise Refusal("webcast account profile locator must resolve to an absolute path")
    profile = profile.resolve()
    if not profile.is_file() or profile.is_symlink():
        raise Refusal(f"webcast account profile is not a regular file: {profile}")
    return profile


def validate_readback(readback: dict, repo: Path, run: Path, plan: dict,
                      fire_override: str | None = None) -> None:
    expected_fields = {
        "schema",
        "kind",
        "scheduler",
        "loaded",
        "event_id",
        "run_id",
        "run_plan_sha256",
        "execution_host_id",
        "label",
        "fire_at_utc",
        "program_arguments",
        "environment",
        "descriptor_sha256",
        "stop_timeout_seconds",
        "kill_mode",
    }
    if set(readback) != expected_fields:
        raise Refusal(
            "loaded readback fields differ from schema 1 "
            f"(missing={sorted(expected_fields - set(readback))}, "
            f"unknown={sorted(set(readback) - expected_fields)})"
        )
    if readback["schema"] != READBACK_SCHEMA or readback["kind"] != "loaded-schedule-readback":
        raise Refusal("unsupported loaded readback schema/kind")
    if readback["scheduler"] not in ("launchd", "systemd-user"):
        raise Refusal(f"unsupported scheduler {readback['scheduler']!r}")
    if readback["loaded"] is not True:
        raise Refusal("scheduler readback does not prove the job is loaded")
    comparisons = {
        "event_id": plan["event_id"],
        "run_id": plan["run_id"],
        "run_plan_sha256": plan["plan_sha256"],
        "execution_host_id": plan["execution_host_id"],
        "label": f"com.earshot.run.{plan['plan_sha256'][:20]}",
        "fire_at_utc": expected_fire(plan, fire_override),
        "program_arguments": expected_program(repo, run, plan),
        "environment": expected_environment(repo, run, plan, readback["scheduler"]),
    }
    for field, wanted in comparisons.items():
        if readback[field] != wanted:
            raise Refusal(
                f"loaded readback {field} does not match the exact RunPlan job "
                f"(loaded={readback[field]!r}, expected={wanted!r})"
            )
    if not HOST_RE.fullmatch(str(readback["execution_host_id"])):
        raise Refusal("loaded readback execution_host_id is invalid")
    if not SHA_RE.fullmatch(str(readback["descriptor_sha256"])):
        raise Refusal("loaded readback descriptor_sha256 is invalid")
    fire = parse_utc(readback["fire_at_utc"], "loaded readback fire_at_utc")
    if fire <= dt.datetime.now(dt.timezone.utc):
        raise Refusal(
            f"loaded scheduler fire time {readback['fire_at_utc']} is not in the future"
        )
    if readback["scheduler"] == "systemd-user":
        timeout = readback["stop_timeout_seconds"]
        if type(timeout) is not int or timeout < SYSTEMD_DRAIN_SECONDS:
            raise Refusal(
                "systemd loaded readback must prove TimeoutStopSec >= "
                f"{SYSTEMD_DRAIN_SECONDS}s"
            )
        if readback["kill_mode"] != "mixed":
            raise Refusal("systemd loaded readback must prove KillMode=mixed")
    elif (
        readback["stop_timeout_seconds"] is not None
        or readback["kill_mode"] is not None
    ):
        raise Refusal("launchd readback must use null stop timeout/kill mode")


def build_proof(readback: dict, plan: dict) -> dict:
    proof = {
        "schema": PROOF_SCHEMA,
        "kind": "loaded-schedule-proof",
        "event_id": plan["event_id"],
        "run_id": plan["run_id"],
        "run_plan_sha256": plan["plan_sha256"],
        "created_utc": now_utc(),
        "readback": readback,
    }
    proof["schedule_proof_sha256"] = object_hash(proof, "schedule_proof_sha256")
    return proof


def validate_proof(proof: dict, readback: dict, plan: dict) -> None:
    expected = {
        "schema",
        "kind",
        "event_id",
        "run_id",
        "run_plan_sha256",
        "created_utc",
        "readback",
        "schedule_proof_sha256",
    }
    if set(proof) != expected:
        raise Refusal("existing schedule proof has unsupported fields")
    if proof["schema"] != PROOF_SCHEMA or proof["kind"] != "loaded-schedule-proof":
        raise Refusal("existing schedule proof has unsupported schema/kind")
    if proof.get("schedule_proof_sha256") != object_hash(proof, "schedule_proof_sha256"):
        raise Refusal("existing schedule proof self-hash mismatch")
    parse_utc(proof.get("created_utc"), "schedule proof created_utc")
    for field in ("event_id", "run_id", "run_plan_sha256"):
        expected_value = (
            plan["plan_sha256"] if field == "run_plan_sha256" else plan[field]
        )
        if proof.get(field) != expected_value:
            raise Refusal(f"existing schedule proof {field} differs from RunPlan")
    if proof.get("readback") != readback:
        raise Refusal(
            "existing schedule proof describes a different loaded job; "
            "a scheduled RunPlan cannot be rebound"
        )


def record_scheduled(repo: Path, run: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "eventctl.py"),
            "record",
            "--run-dir",
            str(run),
            "--transition",
            "scheduled",
            "--actor",
            "schedule-proof",
            "--detail",
            "loaded scheduler readback matches exact RunPlan job",
            "--artifact",
            "control/schedule-proof.json",
        ],
        cwd=repo,
        env={**os.environ, "EARSHOT_REPO": str(repo)},
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise Refusal(f"eventctl refused scheduled: {(proc.stderr or proc.stdout).strip()}")


def reconcile(args: argparse.Namespace) -> int:
    repo, run, plan = resolve_context(args.run_dir)
    readback = load_regular_json(Path(args.readback).resolve(), "loaded readback")
    validate_readback(readback, repo, run, plan, args.fire_at_utc)

    current = status(repo, run)
    reached = reached_from(current)
    state = current.get("state") or {}
    if "preflight_passed" not in reached:
        raise Refusal("RunPlan has not reached preflight_passed")
    if not state.get("active"):
        raise Refusal("RunPlan is terminal or archived")

    proof_path = run / "control" / "schedule-proof.json"
    if proof_path.exists():
        proof = load_regular_json(proof_path, "existing schedule proof")
        validate_proof(proof, readback, plan)
        proof_state = "already-valid"
    else:
        if args.verify_only:
            raise Refusal("scheduled verification requested but durable proof is missing")
        proof = build_proof(readback, plan)
        atomic_write(
            proof_path,
            (json.dumps(proof, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        proof_state = "published"

    if os.environ.get("_EARSHOT_SCHEDULE_PROOF_FAIL_AFTER_PUBLISH") == "1":
        raise Refusal("injected failure after proof publication")

    if "scheduled" not in reached:
        if args.verify_only:
            raise Refusal("durable proof exists but scheduled receipt is missing")
        record_scheduled(repo, run)
        receipt_state = "recorded"
    else:
        receipt_state = "already-recorded"

    # Final verification closes the proof/receipt gap in the success result.
    final_status = status(repo, run)
    if "scheduled" not in reached_from(final_status):
        raise Refusal("scheduled receipt was not visible after reconciliation")
    final_proof = load_regular_json(proof_path, "schedule proof")
    validate_proof(final_proof, readback, plan)
    print(
        json.dumps(
            {
                "schema": 1,
                "kind": "schedule-reconciliation-result",
                "event_id": plan["event_id"],
                "run_id": plan["run_id"],
                "proof": proof_state,
                "receipt": receipt_state,
                "verify_only": args.verify_only,
            },
            sort_keys=True,
        )
    )
    return 0


def delivery_options(args: argparse.Namespace) -> list[str]:
    options = ["--phone-attempts", str(args.phone_attempts), "--repage-after", str(args.repage_after)]
    if args.notify_adapter:
        options.extend(["--notify-adapter", str(Path(args.notify_adapter).resolve())])
    if args.phone_adapter:
        options.extend(["--phone-adapter", str(Path(args.phone_adapter).resolve())])
    return options


def raise_monitor_alert(
    args: argparse.Namespace,
    repo: Path,
    run: Path,
    plan: dict,
    *,
    code: str,
    dedupe: str,
    subject: str,
    body: str,
) -> None:
    alertctl = Path(args.alertctl).resolve() if args.alertctl else repo / "scripts" / "alertctl.py"
    alert_dir = Path(args.alert_dir).resolve()
    try:
        alert_dir.relative_to(run)
    except ValueError:
        pass
    else:
        raise Refusal("monitor alert directory must be outside the live run tree")
    proc = subprocess.run(
        [
            sys.executable,
            str(alertctl),
            "raise",
            "--alert-dir",
            str(alert_dir),
            "--scope",
            f"live-monitor:{plan['event_id']}:{plan['run_id']}",
            "--severity",
            "critical",
            "--code",
            code,
            "--dedupe-key",
            dedupe,
            "--subject",
            subject,
            "--body",
            body,
            *delivery_options(args),
        ],
        cwd=repo,
        env={**os.environ, "EARSHOT_REPO": str(repo)},
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    detail = (proc.stdout or proc.stderr).strip().splitlines()
    print(
        f"monitor alert {code}/{dedupe}: rc={proc.returncode} "
        f"{detail[-1] if detail else '<no detail>'}",
        flush=True,
    )


def repage_due(args: argparse.Namespace, repo: Path) -> None:
    alert_dir = Path(args.alert_dir).resolve()
    if not (alert_dir / "ledger.jsonl").is_file():
        return
    alertctl = Path(args.alertctl).resolve() if args.alertctl else repo / "scripts" / "alertctl.py"
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(alertctl),
                "repage-due",
                "--alert-dir",
                str(alert_dir),
                *delivery_options(args),
            ],
            cwd=repo,
            env={**os.environ, "EARSHOT_REPO": str(repo)},
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"monitor re-page failed: {exc}", file=sys.stderr, flush=True)
        return
    if proc.returncode not in (0, 1):
        print(
            f"monitor re-page refused: {(proc.stderr or proc.stdout).strip()}",
            file=sys.stderr,
            flush=True,
        )


def monitor_once(
    args: argparse.Namespace,
    repo: Path,
    run: Path,
    plan: dict,
    specs: list[ProgressSpec],
    states: dict[str, ProgressState],
    emitted: set[str],
    remote: RemoteSnapshot | None = None,
) -> tuple[dict, bool]:
    rows, reached, times = lifecycle_view(repo, run, remote)
    launched_at = times.get("launched")
    now_wall = dt.datetime.now(dt.timezone.utc)
    now_mono = time.monotonic()
    terminal = next(
        (
            row.get("transition")
            for row in rows
            if row.get("kind") == "transition" and row.get("transition") in TERMINALS
        ),
        None,
    )
    alerts: list[tuple[str, str, str, str]] = []
    progress_summary: dict[str, dict] = {}
    audio_bytes = 0
    for spec in specs:
        state = states.setdefault(spec.name, ProgressState())
        present = (
            remote.content(repo, spec.path) is not None
            if remote is not None
            else spec.path.exists()
        )
        if not present:
            progress_summary[spec.name] = {"state": "missing", "path": str(spec.path)}
            if launched_at and (now_wall - launched_at).total_seconds() >= args.confirm_seconds:
                alerts.append(
                    (
                        "progress_missing",
                        f"missing-{spec.name}",
                        f"{plan['event_id']}: {spec.name} evidence progress missing",
                        f"The EVIDENCE-owned {spec.name} progress record was still absent after the {args.confirm_seconds:g}s confirmation window.",
                    )
                )
            continue
        try:
            record = progress_record(repo, spec, plan, remote)
        except Refusal as exc:
            progress_summary[spec.name] = {"state": "invalid", "detail": str(exc)}
            alerts.append(
                (
                    "progress_invalid",
                    f"invalid-{spec.name}",
                    f"{plan['event_id']}: {spec.name} evidence progress invalid",
                    f"The read-only monitor refused the {spec.name} progress record: {exc}",
                )
            )
            continue
        byte_count = record["_bytes"]
        cadence = record["_cadence"]
        if spec.name == "audio":
            audio_bytes = byte_count
        if state.bytes_seen is None or state.bytes_seen != byte_count:
            state.bytes_seen = byte_count
            state.changed_monotonic = now_mono
        stalled_for = now_mono - state.changed_monotonic
        if spec.mode == "growth":
            append_ns = record["_append_ns"]
            if 0 <= time.monotonic_ns() - append_ns <= 7 * 24 * 3600 * 1_000_000_000:
                stalled_for = max(stalled_for, (time.monotonic_ns() - append_ns) / 1_000_000_000)
            if stalled_for >= cadence * args.stall_cadences:
                alerts.append(
                    (
                        "tape_stalled",
                        f"stalled-{spec.name}-{byte_count}",
                        f"{plan['event_id']}: {spec.name} tape growth stalled",
                        f"The {spec.name} tape remained at {byte_count} bytes for at least {args.stall_cadences:g} published cadence(s). Process existence was not used.",
                    )
                )
        progress_summary[spec.name] = {
            "state": "observed",
            "bytes": byte_count,
            "cadence_seconds": cadence,
            "mode": spec.mode,
        }

    if launched_at and "source_attached" not in reached and audio_bytes == 0:
        if (now_wall - launched_at).total_seconds() >= args.confirm_seconds:
            alerts.append(
                (
                    "no_frames",
                    "confirm-window",
                    f"{plan['event_id']}: no frames past confirmation window",
                    f"The run reached launched but had no source_attached receipt or audio growth within {args.confirm_seconds:g}s.",
                )
            )

    if args.watched_pid is not None and terminal is None:
        try:
            os.kill(args.watched_pid, 0)
        except ProcessLookupError:
            alerts.append(
                (
                    "arm_exited_early",
                    "process-before-terminal",
                    f"{plan['event_id']}: arm exited before scheduled end",
                    "The supervised launcher process exited before a terminal lifecycle receipt became readable.",
                )
            )
        except PermissionError:
            pass

    telemetry_path = Path(args.telemetry).resolve() if args.telemetry else run / "telemetry/live.jsonl"
    telemetry = None
    try:
        telemetry = latest_telemetry(repo, telemetry_path, plan, remote)
    except Refusal as exc:
        alerts.append(
            (
                "telemetry_invalid",
                "live-stream",
                f"{plan['event_id']}: live telemetry invalid",
                f"The read-only monitor refused the live telemetry stream: {exc}",
            )
        )
    if telemetry:
        legs = telemetry.get("legs")
        if isinstance(legs, list) and legs and all(
            isinstance(leg, dict) and (leg.get("died") or leg.get("alive") is False)
            for leg in legs
        ):
            alerts.append(
                (
                    "all_legs_dead",
                    "all-legs",
                    f"{plan['event_id']}: all capture legs dead",
                    "The latest typed live-telemetry record marks every configured capture leg dead.",
                )
            )

    journal = Path(args.journal).resolve() if args.journal else repo / "state" / plan["event_id"] / "journal.jsonl"
    try:
        intervals = redial_intervals(repo, journal, remote)
    except Refusal as exc:
        intervals = []
        alerts.append(
            (
                "journal_invalid",
                "redial-journal",
                f"{plan['event_id']}: redial journal invalid",
                f"The read-only monitor refused the redial journal: {exc}",
            )
        )
    recent = intervals[-args.drop_pattern_count :]
    if len(recent) >= args.drop_pattern_count and all(
        args.drop_min_seconds <= value <= args.drop_max_seconds for value in recent
    ):
        alerts.append(
            (
                "drop_cadence",
                f"operator-queue-{len(intervals)}",
                f"{plan['event_id']}: repeated operator-queue drop cadence",
                f"The last {args.drop_pattern_count} redial intervals all fell inside {args.drop_min_seconds:g}-{args.drop_max_seconds:g}s, the known operator-queue signature.",
            )
        )

    end_at = parse_utc(args.end_at_utc, "--end-at-utc") if args.end_at_utc else (
        parse_utc(plan.get("starts_at_utc"), "RunPlan starts_at_utc")
        + dt.timedelta(minutes=args.default_run_minutes)
    )
    if terminal in ("failed", "aborted") and times.get(terminal, now_wall) < end_at:
        alerts.append(
            (
                "arm_exited_early",
                terminal,
                f"{plan['event_id']}: arm exited before scheduled end",
                f"The lifecycle reached {terminal} before the monitor's scheduled end {end_at:%Y-%m-%dT%H:%M:%SZ}.",
            )
        )

    for code, dedupe, subject, body in alerts:
        key = f"{code}:{dedupe}"
        if key in emitted:
            continue
        raise_monitor_alert(
            args,
            repo,
            run,
            plan,
            code=code,
            dedupe=dedupe,
            subject=subject,
            body=body,
        )
        emitted.add(key)

    snapshot = {
        "schema": 1,
        "kind": "live-monitor-snapshot",
        "event_id": plan["event_id"],
        "run_id": plan["run_id"],
        "reached": sorted(reached),
        "terminal": terminal,
        "progress": progress_summary,
        "telemetry_observed": telemetry is not None,
        "redial_intervals_seconds": intervals,
        "alerts_seen": sorted(emitted),
    }
    if remote is not None:
        snapshot["authority"] = {
            "mode": "bounded-remote-read",
            "host": args.remote_host,
            "fetched_at_utc": remote.fetched_at_utc,
            "age_seconds": round(max(0.0, time.monotonic() - remote.fetched_monotonic), 3),
            "staleness_bound_seconds": (
                args.remote_refresh_seconds
                + args.poll_seconds
                + args.remote_timeout_seconds
            ),
        }
    else:
        snapshot["authority"] = {"mode": "local"}
    return snapshot, terminal is not None


def monitor(args: argparse.Namespace) -> int:
    repo, run, plan = resolve_monitor_context(args.run_dir)
    alert_dir = Path(args.alert_dir).resolve()
    try:
        alert_dir.relative_to(run)
    except ValueError:
        pass
    else:
        raise Refusal("monitor alert directory must be outside the live run tree")
    specs = (
        [parse_progress_spec(raw, run) for raw in args.progress]
        if args.progress
        else default_progress_specs(repo, run, plan)
    )
    telemetry_path = Path(args.telemetry).resolve() if args.telemetry else run / "telemetry/live.jsonl"
    journal_path = Path(args.journal).resolve() if args.journal else repo / "state" / plan["event_id"] / "journal.jsonl"
    remote_reader = None
    if args.remote_host:
        remote_reader = RemoteReader(
            args,
            repo,
            [
                run / "control" / "receipts.jsonl",
                *(spec.path for spec in specs),
                telemetry_path,
                journal_path,
            ],
        )
    states: dict[str, ProgressState] = {}
    emitted: set[str] = set()
    while True:
        remote = None
        if remote_reader is not None:
            try:
                remote = remote_reader.read()
            except Refusal as exc:
                key = "remote_read_failed:box-read-through"
                if key not in emitted:
                    raise_monitor_alert(
                        args,
                        repo,
                        run,
                        plan,
                        code="remote_read_failed",
                        dedupe="box-read-through",
                        subject=f"{plan['event_id']}: bounded box read-through failed",
                        body=f"The monitor could not refresh its read-only box snapshot: {exc}",
                    )
                    emitted.add(key)
                snapshot = {
                    "schema": 1,
                    "kind": "live-monitor-snapshot",
                    "event_id": plan["event_id"],
                    "run_id": plan["run_id"],
                    "authority": {
                        "mode": "bounded-remote-read",
                        "host": args.remote_host,
                        "state": "failed",
                        "detail": str(exc),
                        "staleness_bound_seconds": (
                            args.remote_refresh_seconds
                            + args.poll_seconds
                            + args.remote_timeout_seconds
                        ),
                    },
                    "alerts_seen": sorted(emitted),
                }
                print(json.dumps(snapshot, sort_keys=True), flush=True)
                repage_due(args, repo)
                if args.once:
                    return 1
                time.sleep(args.poll_seconds)
                continue
        snapshot, terminal = monitor_once(
            args, repo, run, plan, specs, states, emitted, remote
        )
        print(json.dumps(snapshot, sort_keys=True), flush=True)
        repage_due(args, repo)
        if args.once or terminal:
            return 0
        time.sleep(args.poll_seconds)


def supervise_monitor(args: argparse.Namespace) -> int:
    child_argv = [value for value in sys.argv[1:] if value != "--supervise"]
    stopped = False
    child: subprocess.Popen | None = None

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True
        if child is not None and child.poll() is None:
            child.terminate()

    old_term = signal.signal(signal.SIGTERM, stop)
    old_int = signal.signal(signal.SIGINT, stop)
    try:
        while not stopped:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *child_argv])
            print(f"MONITOR_CHILD_PID={child.pid}", flush=True)
            rc = child.wait()
            if stopped:
                return 0
            if rc == 0:
                return 0
            print(f"monitor child exited rc={rc}; restarting in {args.restart_seconds:g}s", flush=True)
            time.sleep(args.restart_seconds)
        return 0
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--run-dir", required=True)
    value.add_argument("--readback", required=True)
    value.add_argument("--fire-at-utc")
    value.add_argument("--verify-only", action="store_true")
    return value


def monitor_parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Supervised read-only live run monitor")
    value.add_argument("--run-dir", required=True)
    value.add_argument("--alert-dir", required=True)
    value.add_argument("--alertctl")
    value.add_argument("--notify-adapter")
    value.add_argument("--phone-adapter")
    value.add_argument("--progress", action="append", default=[])
    value.add_argument("--telemetry")
    value.add_argument("--journal")
    value.add_argument("--remote-host")
    value.add_argument("--remote-repo")
    value.add_argument("--ssh-command", default="ssh")
    value.add_argument("--remote-refresh-seconds", type=float, default=15.0)
    value.add_argument("--remote-timeout-seconds", type=float, default=5.0)
    value.add_argument("--remote-max-bytes", type=int, default=1024 * 1024)
    value.add_argument("--watched-pid", type=int)
    value.add_argument("--end-at-utc")
    value.add_argument("--default-run-minutes", type=float, default=90.0)
    value.add_argument("--confirm-seconds", type=float, default=90.0)
    value.add_argument("--poll-seconds", type=float, default=5.0)
    value.add_argument("--stall-cadences", type=float, default=1.0)
    value.add_argument("--drop-min-seconds", type=float, default=60.0)
    value.add_argument("--drop-max-seconds", type=float, default=105.0)
    value.add_argument("--drop-pattern-count", type=int, default=2)
    value.add_argument("--phone-attempts", type=int, default=3)
    value.add_argument("--repage-after", type=float, default=300.0)
    value.add_argument("--restart-seconds", type=float, default=2.0)
    value.add_argument("--once", action="store_true")
    value.add_argument("--supervise", action="store_true")
    return value


def validate_monitor_args(args: argparse.Namespace) -> None:
    if not 0 < args.confirm_seconds <= 1800:
        raise Refusal("--confirm-seconds must be in (0, 1800]")
    if not 0.1 <= args.poll_seconds <= 60:
        raise Refusal("--poll-seconds must be in [0.1, 60]")
    if not 0.5 <= args.stall_cadences <= 10:
        raise Refusal("--stall-cadences must be in [0.5, 10]")
    if not 0 < args.drop_min_seconds < args.drop_max_seconds <= 600:
        raise Refusal("drop cadence bounds are invalid")
    if not 1 <= args.drop_pattern_count <= 10:
        raise Refusal("--drop-pattern-count must be in [1, 10]")
    if args.phone_attempts not in (1, 2, 3):
        raise Refusal("--phone-attempts must be 1, 2, or 3")
    if not 0 <= args.repage_after <= 86400:
        raise Refusal("--repage-after must be in [0, 86400]")
    if not 0.1 <= args.restart_seconds <= 60:
        raise Refusal("--restart-seconds must be in [0.1, 60]")
    if not 1 <= args.default_run_minutes <= 360:
        raise Refusal("--default-run-minutes must be in [1, 360]")
    if args.once and args.supervise:
        raise Refusal("--once and --supervise are mutually exclusive")
    if args.watched_pid is not None and args.watched_pid <= 0:
        raise Refusal("--watched-pid must be positive")
    if bool(args.remote_host) != bool(args.remote_repo):
        raise Refusal("--remote-host and --remote-repo must be supplied together")
    if args.remote_host and not REMOTE_HOST_RE.fullmatch(args.remote_host):
        raise Refusal("--remote-host must be a plain host or user@host token")
    if args.remote_repo:
        if not args.remote_repo.startswith("/") or any(
            part in ("", ".", "..") for part in args.remote_repo.split("/")[1:]
        ):
            raise Refusal("--remote-repo must be a normalized absolute path")
    if not 0.1 <= args.remote_refresh_seconds <= 300:
        raise Refusal("--remote-refresh-seconds must be in [0.1, 300]")
    if not 0.1 <= args.remote_timeout_seconds <= 30:
        raise Refusal("--remote-timeout-seconds must be in [0.1, 30]")
    if not 4096 <= args.remote_max_bytes <= 16 * 1024 * 1024:
        raise Refusal("--remote-max-bytes must be in [4096, 16777216]")


def main() -> int:
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "monitor":
            args = monitor_parser().parse_args(sys.argv[2:])
            validate_monitor_args(args)
            return supervise_monitor(args) if args.supervise else monitor(args)
        return reconcile(parser().parse_args())
    except Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("REFUSED: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
