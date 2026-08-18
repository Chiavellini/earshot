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
import re
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
LANE_SUFFIX = re.compile(r"-(WEBCAST|PHONE|ACOUSTIC)$")


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


def declared_run_date(runs_root: Path, ticker: str) -> str | None:
    """The UTC date this market event's RunPlans actually declare.

    The slate dates an event from its Kalshi ticker suffix, which is minted
    once and never moves. A call CAN move: KXEARNINGSMENTIONTOL-26AUG18
    carries a typed date correction to 2026-08-19T12:30Z. Graded against the
    slate alone, 08-18 reported TOL MISSED for a call that had not happened.

    A RunPlan is immutable and authoritative about its own start, so it wins.
    Returns None when no plan declares a date, so a missing plan can never
    shrink the denominator — only a plan that positively says otherwise can.
    """
    if not runs_root.is_dir():
        return None
    declared: set[str] = set()
    for plan_path in sorted(runs_root.glob(f"{ticker}*/*/control/run-plan.json")):
        try:
            declared.add(run_date(strict_json(plan_path, "RunPlan")))
        except (OSError, DayAuditError):
            continue
    # Unanimity required: if any attempt claims the graded date, the event
    # belongs to it and nothing is deferred.
    return declared.pop() if len(declared) == 1 else None


def expected_events(
    slate_path: Path, date: str, runs_root: Path | None = None
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None, dict[str, dict[str, Any]]]:
    if not slate_path.exists():
        return {}, None, {}
    if slate_path.is_symlink() or not slate_path.is_file():
        raise DayAuditError(f"slate path is unsafe: {slate_path}")
    slate = strict_json(slate_path, "slate")
    events = slate.get("events")
    if not isinstance(events, list):
        raise DayAuditError("slate events is not a list")
    selected: dict[str, dict[str, Any]] = {}
    for item in events:
        if not isinstance(item, dict) or item.get("call_date") != date:
            continue
        ticker = item.get("event_ticker")
        if not isinstance(ticker, str) or not ticker:
            raise DayAuditError("dated slate event has no event_ticker")
        selected[ticker] = {
            "issuer": item.get("issuer"),
            "call_date": date,
            "call_time_utc": item.get("call_time_utc"),
            "call_date_evidence": item.get("call_date_evidence"),
        }
    deferred: dict[str, dict[str, Any]] = {}
    if runs_root is not None:
        for ticker in sorted(selected):
            declared = declared_run_date(runs_root, ticker)
            if declared is not None and declared != date:
                deferred[ticker] = {**selected.pop(ticker), "declared_date": declared}
    return selected, fingerprint(slate_path), deferred


def market_event(event_id: str) -> str:
    return LANE_SUFFIX.sub("", event_id)


def relative_link(repo: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo).as_posix()
    except ValueError:
        return str(resolved)


def classify_events(
    rows: list[dict[str, Any]],
    expected: dict[str, dict[str, Any]],
    *,
    repo: Path,
    slate_path: Path,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise DayAuditError("run row lacks event_id")
        grouped.setdefault(market_event(event_id), []).append(row)
    events: list[dict[str, Any]] = []
    for ticker in sorted(set(expected) | set(grouped)):
        attempts = grouped.get(ticker, [])
        covered = any(
            row.get("status") in ("audited", "operational_fail")
            and isinstance(row.get("performance"), dict)
            and row["performance"].get("audio_frame_count", 0) > 0
            for row in attempts
        )
        descoped = any(row.get("status") == "descoped" for row in attempts)
        classification = "COVERED" if covered else "DESCOPED" if descoped else "MISSED"
        links: set[str] = set()
        if ticker in expected:
            links.add(relative_link(repo, slate_path))
        attempt_rows = []
        for row in attempts:
            run_dir = Path(row["run_dir"])
            for path in (
                run_dir / "control" / "run-plan.json",
                run_dir / "control" / "receipts.jsonl",
            ):
                if path.is_file():
                    links.add(relative_link(repo, path))
            grade = row.get("grade")
            if isinstance(grade, str) and Path(grade).is_file():
                links.add(relative_link(repo, Path(grade)))
            attempt_rows.append({
                "event_id": row["event_id"],
                "run_id": row.get("run_id"),
                "status": row.get("status"),
                "problem": bool(row.get("problem")),
            })
        events.append({
            "event_ticker": ticker,
            "classification": classification,
            "problem": classification == "MISSED" or any(
                item["problem"] for item in attempt_rows),
            "slate": expected.get(ticker),
            "attempts": attempt_rows,
            "evidence_links": sorted(links),
        })
    return events


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
    # An ARCHIVED attempt is closed evidence: eventctl refuses any further
    # receipt on it ("run is archived — no further receipts"), so its alerts can
    # never be acknowledged by anyone. Counting them as a day problem therefore
    # fails that date PERMANENTLY, with no action available to the operator.
    # They stay visible in this row's `alerts` block and in the day's
    # `unacknowledged_alerts` total — they are excluded from the pass/fail
    # verdict only, never hidden.
    archived = "archived" in transitions
    open_alert_problem = alert_problem and not archived
    if "descope" in transitions:
        return {**base, "status": "descoped", "problem": open_alert_problem}
    if "blocked" in transitions:
        # M7: a blocked attempt that NEVER LAUNCHED is prep-time churn — a plan
        # superseded by a later attempt (config drift after dispatch, a preflight
        # that failed and was re-staged). It stays visible in its own row, but it
        # must not fail the whole date: on 2026-08-18 six such attempts outlived
        # four runs that captured and three that filled, and the date graded FAIL
        # while every board was covered. A blocked attempt that DID launch is a
        # real operational failure and still fails the date. Unacknowledged
        # alerts always count, so a superseded attempt cannot silence its own
        # warnings — it only stops them from being counted twice.
        return {
            **base,
            "status": "blocked",
            "problem": "launched" in transitions or open_alert_problem,
        }
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
    parser.add_argument("--slate", default=None)
    parser.add_argument("--auditor", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--eventctl", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--alertctl", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        date = valid_date(args.date)
        repo = Path(os.environ.get("EARSHOT_REPO", Path(__file__).resolve().parent.parent)).resolve()
        run_root = Path(args.run_root).resolve() if args.run_root else repo / "var" / "runs"
        output_root = Path(args.output_root).resolve() if args.output_root else repo / "var" / "audits"
        slate_path = Path(args.slate).resolve() if args.slate else repo / "slate" / "latest.json"
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
        expected, slate_fingerprint, deferred_events = expected_events(
            slate_path, date, runs_root=run_root)
        events = classify_events(rows, expected, repo=repo, slate_path=slate_path)
        classifications: dict[str, int] = {}
        for event in events:
            classification = event["classification"]
            classifications[classification] = classifications.get(classification, 0) + 1
        event_problems = sum(bool(event["problem"]) for event in events)
        summary["events"] = len(events)
        summary["event_problems"] = event_problems
        summary["classifications"] = dict(sorted(classifications.items()))
        # A deferred event is REPORTED, never silently dropped: a grader whose
        # denominator quietly shrinks cannot report a total failure.
        summary["deferred"] = len(deferred_events)
        no_runs = not rows
        no_events = not events
        payload = {
            "schema": SCHEMA,
            "kind": "run-day-index",
            "date_utc": date,
            "derived": True,
            "authority": (
                "dated slate events plus immutable RunPlans, lifecycle receipt tails, "
                "and schema-1 RunGrades"
            ),
            "slate": {
                "path": relative_link(repo, slate_path),
                "fingerprint": slate_fingerprint,
            },
            "overall": "fail" if no_events or event_problems or summary["problems"] else "pass",
            "events": events,
            "summary": {**summary, "no_runs": no_runs, "no_events": no_events},
            "runs": rows,
            "deferred_events": deferred_events,
        }
        destination = output_root / f"{date}.json"
        publish(destination, canonical_bytes(payload))
        print(
            f"AUDIT-DAY {payload['overall'].upper()}: {date} — "
            f"{summary['events']} event(s), {summary['event_problems']} missed/problem event(s), "
            + (f"{summary['deferred']} deferred, " if summary.get("deferred") else "")
            + f"{summary['attempts']} attempt(s), {summary['problems']} attempt problem(s); {destination}"
        )
        return 1 if payload["overall"] == "fail" else 0
    except (OSError, subprocess.SubprocessError, DayAuditError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
