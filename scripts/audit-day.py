#!/usr/bin/env python3
"""Reconcile one UTC date of immutable EARSHOT runs.

The per-run auditor remains the evidence authority.  This wrapper enumerates
the date's RunPlans, verifies every lifecycle, audits every completed/failed
attempt while continuing siblings, and publishes one derived index.  A date is
not green when it has no runs or when any attempt is active, blocked, invalid,
unauditable, or operationally failed.

No network, scheduler, dial, or trading operation is performed here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


SCHEMA = 1
PLAN_SCHEMA = 3
GRADE_SCHEMA = 1
TERMINAL_FOR_AUDIT = {"completed", "aborted", "failed"}
PROBLEM_STATUSES = {
    "active",
    "audit_failed",
    "blocked",
    "invalid",
    "operational_fail",
}


class DayAuditError(Exception):
    pass


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fingerprint(path: Path) -> dict[str, Any]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise DayAuditError(f"missing file: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise DayAuditError(f"not a regular non-symlink file: {path}")
    raw = path.read_bytes()
    return {"bytes": len(raw), "sha256": sha256(raw)}


def strict_json(path: Path, what: str) -> dict[str, Any]:
    raw = path.read_bytes()

    def bad_constant(value: str) -> None:
        raise ValueError(f"invalid numeric constant {value}")

    try:
        value = json.loads(raw, parse_constant=bad_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DayAuditError(f"{what} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise DayAuditError(f"{what} is not an object")
    return value


def valid_date(value: str) -> str:
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise DayAuditError(f"invalid UTC date: {value!r}") from exc


def run_date(plan: dict[str, Any]) -> str:
    raw = plan.get("starts_at_utc")
    if not isinstance(raw, str):
        raise DayAuditError("RunPlan starts_at_utc is missing")
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DayAuditError(f"RunPlan starts_at_utc is invalid: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise DayAuditError("RunPlan starts_at_utc is timezone-naive")
    return parsed.astimezone(dt.timezone.utc).date().isoformat()


def candidate_runs(root: Path, date: str) -> list[tuple[Path, dict[str, Any] | None, str | None]]:
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise DayAuditError(f"run root is unsafe: {root}")
    compact = date.replace("-", "")
    selected: list[tuple[Path, dict[str, Any] | None, str | None]] = []
    for event_dir in sorted(root.iterdir()):
        if event_dir.is_symlink() or not event_dir.is_dir():
            continue
        for run_dir in sorted(event_dir.iterdir()):
            if run_dir.is_symlink() or not run_dir.is_dir():
                continue
            plan_path = run_dir / "control" / "run-plan.json"
            try:
                plan = strict_json(plan_path, "RunPlan")
                date_from_plan = run_date(plan)
            except (OSError, DayAuditError) as exc:
                # Default RunIds begin with the UTC start.  This fallback makes
                # a corrupt plan visible on its intended day instead of
                # silently removing it from the denominator.
                if run_dir.name.startswith(compact + "T"):
                    selected.append((run_dir, None, str(exc)))
                continue
            if date_from_plan == date:
                selected.append((run_dir, plan, None))
    return selected


def run_command(argv: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
        env=env,
    )


def command_detail(result: subprocess.CompletedProcess[str]) -> str:
    lines = (result.stdout + result.stderr).strip().splitlines()
    return lines[-1] if lines else f"exit {result.returncode}"


def exact_identity(plan: dict[str, Any], run_dir: Path) -> None:
    if plan.get("schema") != PLAN_SCHEMA or plan.get("kind") != "run-plan":
        raise DayAuditError("unsupported RunPlan schema/kind")
    if plan.get("event_id") != run_dir.parent.name or plan.get("run_id") != run_dir.name:
        raise DayAuditError("RunPlan identity differs from directory")
    if not isinstance(plan.get("plan_sha256"), str):
        raise DayAuditError("RunPlan plan_sha256 is missing")


def status_for(eventctl: Path, run_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    result = run_command(
        [sys.executable, str(eventctl), "status", "--run-dir", str(run_dir), "--json"],
        env=env,
    )
    if result.returncode:
        raise DayAuditError(f"lifecycle status failed: {command_detail(result)}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DayAuditError(f"lifecycle status emitted invalid JSON: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("state"), dict):
        raise DayAuditError("lifecycle status emitted an unsupported shape")
    return value


def alert_status_for(alertctl: Path, run_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    result = run_command(
        [sys.executable, str(alertctl), "status", "--run-dir", str(run_dir)],
        env=env,
    )
    # alertctl uses one to mean a valid status with outstanding human
    # acknowledgement. Two (or any other exit) is an integrity/tool failure.
    if result.returncode not in (0, 1):
        raise DayAuditError(f"alert status failed: {command_detail(result)}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DayAuditError(f"alert status emitted invalid JSON: {exc}") from exc
    if value.get("schema") != 1 or value.get("kind") != "alert-status":
        raise DayAuditError("alert status emitted an unsupported shape")
    alerts = value.get("alerts")
    unacknowledged = value.get("unacknowledged")
    if not isinstance(alerts, list):
        raise DayAuditError("alert status alerts is invalid")
    if type(unacknowledged) is not int or not 0 <= unacknowledged <= len(alerts):
        raise DayAuditError("alert status unacknowledged count is invalid")
    if (result.returncode == 1) != (unacknowledged > 0):
        raise DayAuditError("alert status exit code disagrees with acknowledgement state")
    items: list[dict[str, Any]] = []
    for item in alerts:
        if not isinstance(item, dict) or not isinstance(item.get("request"), dict):
            raise DayAuditError("alert status item is invalid")
        request = item["request"]
        alert_id = request.get("alert_id")
        severity = request.get("severity")
        code = request.get("code")
        if not all(isinstance(value, str) and value for value in (alert_id, severity, code)):
            raise DayAuditError("alert status request identity is invalid")
        for field in ("delivered", "ambiguous", "acknowledged"):
            if type(item.get(field)) is not bool:
                raise DayAuditError(f"alert status {field} is invalid")
        items.append(
            {
                "alert_id": alert_id,
                "severity": severity,
                "code": code,
                "delivered": item["delivered"],
                "ambiguous": item["ambiguous"],
                "acknowledged": item["acknowledged"],
            }
        )
    return {
        "count": len(items),
        "unacknowledged": unacknowledged,
        "items": items,
    }


def reached(status: dict[str, Any]) -> set[str]:
    rows = status["state"].get("reached")
    if not isinstance(rows, list):
        raise DayAuditError("lifecycle reached set is invalid")
    out: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("transition"), str):
            raise DayAuditError("lifecycle reached row is invalid")
        out.add(row["transition"])
    return out


def nonnegative_int(value: Any, what: str) -> int:
    if type(value) is not int or value < 0:
        raise DayAuditError(f"{what} is not a non-negative integer")
    return value


def grade_summary(path: Path, plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    grade = strict_json(path, "RunGrade")
    if grade.get("schema") != GRADE_SCHEMA or grade.get("kind") != "run-grade":
        raise DayAuditError("unsupported RunGrade schema/kind")
    for field in ("event_id", "run_id", "plan_sha256", "lane", "execution_host_id"):
        if grade.get(field) != plan.get(field):
            raise DayAuditError(f"RunGrade {field} differs from RunPlan")
    validation = grade.get("validation")
    if not isinstance(validation, dict) or validation.get("status") != "pass":
        raise DayAuditError("RunGrade validation did not pass")
    outcome = grade.get("operational_outcome")
    if outcome not in ("pass", "fail"):
        raise DayAuditError("RunGrade operational_outcome is invalid")
    accounting = grade.get("accounting")
    if not isinstance(accounting, dict):
        raise DayAuditError("RunGrade accounting is invalid")
    required_counts = ("intents", "attempted", "accepted", "filled", "terminal", "unresolved")
    counts = {key: nonnegative_int(accounting.get(key), f"accounting.{key}") for key in required_counts}
    detections = grade.get("detections")
    orders = grade.get("orders")
    if not isinstance(detections, list) or not isinstance(orders, list):
        raise DayAuditError("RunGrade detections/orders is invalid")
    observations: dict[str, int] = {}
    detection_to_exhaustion: list[float] = []
    wire_to_exhaustion: list[float] = []
    for order in orders:
        if not isinstance(order, dict) or not isinstance(order.get("book_observation"), dict):
            raise DayAuditError("RunGrade order book observation is invalid")
        observation = order["book_observation"]
        classification = observation.get("classification")
        if not isinstance(classification, str):
            raise DayAuditError("RunGrade book classification is invalid")
        observations[classification] = observations.get(classification, 0) + 1
        for key, target in (
            ("detection_to_exhaustion_ms", detection_to_exhaustion),
            ("wire_to_exhaustion_ms", wire_to_exhaustion),
        ):
            value = observation.get(key)
            if value is not None:
                if type(value) not in (int, float) or value < 0:
                    raise DayAuditError(f"RunGrade {key} is invalid")
                target.append(value)
    audio = grade.get("audio_evidence")
    fix = grade.get("fix_observation_evidence")
    if not isinstance(audio, dict) or not isinstance(fix, dict):
        raise DayAuditError("RunGrade audio/FIX evidence summary is invalid")
    summary = {
        "operational_outcome": outcome,
        "accounting": counts,
        "detection_count": len(detections),
        "audio_frame_count": nonnegative_int(audio.get("frame_count"), "audio.frame_count"),
        "audio_sample_count": nonnegative_int(audio.get("sample_count"), "audio.sample_count"),
        "precision": audio.get("precision"),
        "fix_status": fix.get("status"),
        "book_observations": dict(sorted(observations.items())),
        "detection_to_exhaustion_ms": detection_to_exhaustion,
        "wire_to_exhaustion_ms": wire_to_exhaustion,
    }
    return grade, summary


def audit_one(
    run_dir: Path,
    plan: dict[str, Any],
    *,
    eventctl: Path,
    alertctl: Path,
    auditor: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    exact_identity(plan, run_dir)
    base = {
        "event_id": plan["event_id"],
        "run_id": plan["run_id"],
        "plan_sha256": plan["plan_sha256"],
        "lane": plan.get("lane"),
        "execution_host_id": plan.get("execution_host_id"),
        "run_dir": str(run_dir),
        "plan_fingerprint": fingerprint(run_dir / "control" / "run-plan.json"),
    }
    verify = run_command(
        [sys.executable, str(eventctl), "verify", "--run-dir", str(run_dir)],
        env=env,
    )
    if verify.returncode:
        raise DayAuditError(f"eventctl verification failed: {command_detail(verify)}")
    status = status_for(eventctl, run_dir, env)
    transitions = reached(status)
    alert_status = alert_status_for(alertctl, run_dir, env)
    alert_problem = alert_status["unacknowledged"] > 0
    base["alerts"] = alert_status
    base["lifecycle_tail"] = {
        "sequence": status.get("authority", {}).get("as_of_sequence"),
        "receipt_sha256": status.get("authority", {}).get("as_of_receipt_sha256"),
        "ledger_fingerprint": fingerprint(run_dir / "control" / "receipts.jsonl"),
    }
    base["reached"] = sorted(transitions)
    if "descope" in transitions:
        return {**base, "status": "descoped", "problem": alert_problem}
    if "blocked" in transitions:
        return {**base, "status": "blocked", "problem": True}
    if not (transitions & TERMINAL_FOR_AUDIT):
        return {**base, "status": "active", "problem": True}

    result = run_command(
        [sys.executable, str(auditor), "--run-dir", str(run_dir)], env=env
    )
    if result.returncode:
        return {
            **base,
            "status": "audit_failed",
            "problem": True,
            "detail": command_detail(result),
        }
    status = status_for(eventctl, run_dir, env)
    transitions = reached(status)
    if "graded" not in transitions:
        return {
            **base,
            "status": "audit_failed",
            "problem": True,
            "detail": "auditor returned zero without a graded lifecycle receipt",
        }
    artifacts = plan.get("artifacts")
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get("grade"), str):
        raise DayAuditError("RunPlan grade artifact is invalid")
    relative = Path(artifacts["grade"])
    if relative.is_absolute() or ".." in relative.parts:
        raise DayAuditError("RunPlan grade artifact is unsafe")
    grade_path = run_dir / relative
    _, summary = grade_summary(grade_path, plan)
    row_status = "audited" if summary["operational_outcome"] == "pass" else "operational_fail"
    return {
        **base,
        "status": row_status,
        "problem": row_status in PROBLEM_STATUSES or alert_problem,
        "grade": str(grade_path),
        "grade_fingerprint": fingerprint(grade_path),
        "performance": summary,
        "lifecycle_tail": {
            "sequence": status.get("authority", {}).get("as_of_sequence"),
            "receipt_sha256": status.get("authority", {}).get("as_of_receipt_sha256"),
            "ledger_fingerprint": fingerprint(run_dir / "control" / "receipts.jsonl"),
        },
        "reached": sorted(transitions),
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    accounting = {key: 0 for key in ("intents", "attempted", "accepted", "filled", "terminal", "unresolved")}
    detection_count = audio_frames = audio_samples = 0
    alert_count = unacknowledged_alerts = 0
    observation_counts: dict[str, int] = {}
    detection_timing: list[float] = []
    wire_timing: list[float] = []
    for row in rows:
        status = row["status"]
        statuses[status] = statuses.get(status, 0) + 1
        alerts = row.get("alerts")
        if isinstance(alerts, dict):
            alert_count += alerts.get("count", 0)
            unacknowledged_alerts += alerts.get("unacknowledged", 0)
        perf = row.get("performance")
        if not isinstance(perf, dict):
            continue
        for key in accounting:
            accounting[key] += perf["accounting"][key]
        detection_count += perf["detection_count"]
        audio_frames += perf["audio_frame_count"]
        audio_samples += perf["audio_sample_count"]
        for key, count in perf["book_observations"].items():
            observation_counts[key] = observation_counts.get(key, 0) + count
        detection_timing.extend(perf["detection_to_exhaustion_ms"])
        wire_timing.extend(perf["wire_to_exhaustion_ms"])
    problems = sum(bool(row.get("problem")) for row in rows)
    return {
        "attempts": len(rows),
        "problems": problems,
        "statuses": dict(sorted(statuses.items())),
        "accounting": accounting,
        "detection_count": detection_count,
        "audio_frame_count": audio_frames,
        "audio_sample_count": audio_samples,
        "alert_count": alert_count,
        "unacknowledged_alerts": unacknowledged_alerts,
        "book_observations": dict(sorted(observation_counts.items())),
        "detection_to_exhaustion_ms": detection_timing,
        "wire_to_exhaustion_ms": wire_timing,
    }


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise DayAuditError(f"derived index path is unsafe: {path}")
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            view = memoryview(payload)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise DayAuditError("short write while publishing day index")
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, path)
        fsync_dir(path.parent)
    finally:
        os.close(lock_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--run-root", default=None)
    parser.add_argument("--output-root", default=None)
    parser.add_argument("--auditor", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--eventctl", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--alertctl", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        date = valid_date(args.date)
        repo = Path(os.environ.get("EARSHOT_REPO", Path(__file__).resolve().parent.parent)).resolve()
        run_root = Path(args.run_root).resolve() if args.run_root else repo / "var" / "runs"
        output_root = Path(args.output_root).resolve() if args.output_root else repo / "var" / "audits"
        auditor = Path(args.auditor).resolve() if args.auditor else Path(__file__).with_name("run-audit.py")
        eventctl = Path(args.eventctl).resolve() if args.eventctl else Path(__file__).with_name("eventctl.py")
        alertctl = Path(args.alertctl).resolve() if args.alertctl else Path(__file__).with_name("alertctl.py")
        env = dict(os.environ)
        env["EARSHOT_REPO"] = str(repo)
        env["EARSHOT_EVENTCTL"] = str(eventctl)
        rows: list[dict[str, Any]] = []
        for run_dir, plan, discovery_error in candidate_runs(run_root, date):
            if plan is None:
                rows.append(
                    {
                        "event_id": run_dir.parent.name,
                        "run_id": run_dir.name,
                        "run_dir": str(run_dir),
                        "status": "invalid",
                        "problem": True,
                        "detail": discovery_error,
                    }
                )
                continue
            try:
                rows.append(
                    audit_one(
                        run_dir,
                        plan,
                        eventctl=eventctl,
                        alertctl=alertctl,
                        auditor=auditor,
                        env=env,
                    )
                )
            except (OSError, subprocess.SubprocessError, DayAuditError) as exc:
                rows.append(
                    {
                        "event_id": plan.get("event_id", run_dir.parent.name),
                        "run_id": plan.get("run_id", run_dir.name),
                        "run_dir": str(run_dir),
                        "status": "invalid",
                        "problem": True,
                        "detail": str(exc),
                    }
                )
        rows.sort(key=lambda row: (str(row.get("event_id")), str(row.get("run_id"))))
        summary = aggregate(rows)
        no_runs = not rows
        payload = {
            "schema": SCHEMA,
            "kind": "run-day-index",
            "date_utc": date,
            "derived": True,
            "authority": "immutable RunPlans, lifecycle receipt tails, and schema-1 RunGrades",
            "overall": "fail" if no_runs or summary["problems"] else "pass",
            "summary": {**summary, "no_runs": no_runs},
            "runs": rows,
        }
        destination = output_root / f"{date}.json"
        publish(destination, canonical_bytes(payload))
        print(
            f"AUDIT-DAY {payload['overall'].upper()}: {date} — "
            f"{summary['attempts']} attempt(s), {summary['problems']} problem(s); {destination}"
        )
        return 1 if payload["overall"] == "fail" else 0
    except (OSError, subprocess.SubprocessError, DayAuditError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
