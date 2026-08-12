#!/usr/bin/env python3
"""Publish one durable loaded-scheduler proof, then record ``scheduled``.

The OS-specific installers own descriptor generation and readback.  This
helper owns the shared trust boundary: a scheduler job is not a lifecycle
fact until its *loaded* representation is bound to the exact schema-3
RunPlan, canonical run directory, execution host, fire instant, argv, and
identity environment.  The proof is durable before eventctl appends the
``scheduled`` receipt, so a crash in between is recovered by an idempotent
retry instead of producing an unexplainable receipt.

No scheduler command is executed here.  ``readback`` is a normalized JSON
observation produced from launchctl/systemctl by the relevant installer.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


PROOF_SCHEMA = 1
READBACK_SCHEMA = 1
SYSTEMD_DRAIN_SECONDS = 150
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


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


def expected_environment(repo: Path, run: Path, plan: dict) -> dict[str, str]:
    value = {
        "EARSHOT_EXECUTION_HOST": plan["execution_host_id"],
        "EARSHOT_REPO": str(repo),
        "EARSHOT_RUN_DIR": str(run),
        "EARSHOT_RUN_PLAN_SHA256": plan["plan_sha256"],
    }
    if plan.get("lane") == "webcast":
        value["EARSHOT_ACCOUNT_ENV"] = str(resolve_account_profile(repo, plan))
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
        "environment": expected_environment(repo, run, plan),
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


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--run-dir", required=True)
    value.add_argument("--readback", required=True)
    value.add_argument("--fire-at-utc")
    value.add_argument("--verify-only", action="store_true")
    return value


def main() -> int:
    try:
        return reconcile(parser().parse_args())
    except Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("REFUSED: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
