#!/usr/bin/env python3
"""Proof-bearing operational alert requests for one immutable run.

Alert facts are compatibility observations in the RunPlan's append-only,
hash-chained lifecycle ledger. They never advance or rewrite typed run state.
The event control plane supplies the exclusive append lock and action-level
deduplication; this client owns the AlertRequest/DeliveryReceipt/Ack record
shapes and bounded delivery policy.

Delivery is not acknowledgement. Critical alerts attempt the ordinary local
notifier and then a bounded phone fallback unless already acknowledged. A
human must explicitly run ``ack`` with the displayed alert id. Offline tests
inject adapters; this tool never auto-acknowledges a successful send.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SCHEMA = 1
SEVERITIES = ("warning", "critical")
OUTCOMES = ("delivered", "failed", "ambiguous")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")
ACTION_RE = re.compile(
    r"^alert\.(required|delivery_started|delivery|acknowledged):([0-9a-f]{64})(?::([a-z0-9._-]+):([1-9][0-9]*))?$"
)


class AlertError(Exception):
    pass


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AlertError(f"{what} must be non-empty and <= {limit} characters")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise AlertError(f"{what} contains control characters")
    return value.strip()


def safe_id(value: str, what: str) -> str:
    if not ID_RE.fullmatch(value):
        raise AlertError(f"{what} must match {ID_RE.pattern}")
    return value


def regular_file(path: Path, what: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise AlertError(f"{what} is missing: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise AlertError(f"{what} must be a regular non-symlink file: {path}")


def strict_json(path: Path, what: str) -> dict[str, Any]:
    regular_file(path, what)
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AlertError(f"{what} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AlertError(f"{what} must be an object")
    return value


def run_identity(run_dir: Path) -> dict[str, str]:
    run_dir = run_dir.resolve(strict=True)
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise AlertError(f"run dir is unsafe: {run_dir}")
    plan = strict_json(run_dir / "control" / "run-plan.json", "RunPlan")
    if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
        raise AlertError("alerts require one immutable schema-3 RunPlan")
    if plan.get("event_id") != run_dir.parent.name or plan.get("run_id") != run_dir.name:
        raise AlertError("RunPlan identity differs from run directory")
    for field in ("event_id", "run_id", "plan_sha256", "execution_host_id"):
        if not isinstance(plan.get(field), str) or not plan[field]:
            raise AlertError(f"RunPlan {field} is invalid")
    return {field: plan[field] for field in ("event_id", "run_id", "plan_sha256", "execution_host_id")}


def eventctl_path() -> Path:
    raw = os.environ.get("EARSHOT_EVENTCTL")
    path = Path(raw).resolve() if raw else Path(__file__).with_name("eventctl.py")
    regular_file(path, "eventctl adapter")
    return path


def eventctl(run_dir: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.setdefault("EARSHOT_REPO", str(Path(__file__).resolve().parent.parent))
    return subprocess.run(
        [sys.executable, str(eventctl_path()), *argv, "--run-dir", str(run_dir)],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )


def command_detail(result: subprocess.CompletedProcess[str]) -> str:
    lines = (result.stdout + result.stderr).strip().splitlines()
    return lines[-1] if lines else f"exit {result.returncode}"


def lifecycle_rows(run_dir: Path) -> list[dict[str, Any]]:
    result = eventctl(run_dir, "verify")
    if result.returncode:
        raise AlertError(f"eventctl verification failed: {command_detail(result)}")
    path = run_dir / "control" / "receipts.jsonl"
    regular_file(path, "lifecycle ledger")
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise AlertError("lifecycle ledger has a torn tail")
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(raw.splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AlertError(f"lifecycle line {index} is invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise AlertError(f"lifecycle line {index} is not an object")
        rows.append(value)
    return rows


def alert_records(rows: list[dict[str, Any]], alert_id: str | None = None) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for receipt in rows:
        if receipt.get("kind") != "compat" or not isinstance(receipt.get("action"), str):
            continue
        match = ACTION_RE.fullmatch(receipt["action"])
        if not match or (alert_id is not None and match.group(2) != alert_id):
            continue
        try:
            detail = json.loads(receipt.get("detail", ""))
        except json.JSONDecodeError as exc:
            raise AlertError(f"alert receipt detail is invalid JSON: {exc}") from exc
        if not isinstance(detail, dict):
            raise AlertError("alert receipt detail is not an object")
        out.append((match.group(1), detail, receipt))
    return out


def append_fact(
    run_dir: Path,
    *,
    action: str,
    outcome: str,
    detail: dict[str, Any],
    artifact: str | None = None,
) -> bool:
    argv = [
        "compat",
        "--actor",
        "alertctl",
        "--action",
        action,
        "--outcome",
        outcome,
        "--detail",
        canonical(detail),
        "--idempotent",
    ]
    if artifact:
        argv.extend(["--log", artifact])
    result = eventctl(run_dir, *argv)
    if result.returncode:
        raise AlertError(f"eventctl refused alert fact: {command_detail(result)}")
    return not result.stdout.startswith("idempotent:")


def request_id(identity: dict[str, str], severity: str, code: str, dedupe_key: str) -> str:
    value = [
        SCHEMA,
        identity["plan_sha256"],
        severity,
        code,
        dedupe_key,
    ]
    return hashlib.sha256(canonical(value).encode("ascii")).hexdigest()


def required_record(
    identity: dict[str, str],
    *,
    alert_id: str,
    severity: str,
    code: str,
    dedupe_key: str,
    subject: str,
    body: str,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "kind": "alert-request",
        "alert_id": alert_id,
        "event_id": identity["event_id"],
        "run_id": identity["run_id"],
        "plan_sha256": identity["plan_sha256"],
        "execution_host_id": identity["execution_host_id"],
        "severity": severity,
        "code": code,
        "dedupe_key": dedupe_key,
        "subject": subject,
        "body": body,
    }


def request_for(records: list[tuple[str, dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    requests = [detail for kind, detail, _ in records if kind == "required"]
    if len(requests) != 1:
        raise AlertError(f"alert must have exactly one request, found {len(requests)}")
    request = requests[0]
    expected = {
        "schema",
        "kind",
        "alert_id",
        "event_id",
        "run_id",
        "plan_sha256",
        "execution_host_id",
        "severity",
        "code",
        "dedupe_key",
        "subject",
        "body",
    }
    if set(request) != expected or request.get("schema") != SCHEMA or request.get("kind") != "alert-request":
        raise AlertError("alert request has an unsupported shape")
    return request


def is_acknowledged(records: list[tuple[str, dict[str, Any], dict[str, Any]]]) -> bool:
    return any(kind == "acknowledged" for kind, _, _ in records)


def next_attempt(records: list[tuple[str, dict[str, Any], dict[str, Any]]], channel: str) -> int:
    attempts = [
        detail.get("attempt")
        for kind, detail, _ in records
        if kind == "delivery_started" and detail.get("channel") == channel
    ]
    if any(type(value) is not int or value <= 0 for value in attempts):
        raise AlertError("stored delivery attempt number is invalid")
    return max(attempts, default=0) + 1


def pending_attempts(
    records: list[tuple[str, dict[str, Any], dict[str, Any]]], channel: str
) -> list[int]:
    started = {
        detail.get("attempt")
        for kind, detail, _ in records
        if kind == "delivery_started" and detail.get("channel") == channel
    }
    completed = {
        detail.get("attempt")
        for kind, detail, _ in records
        if kind == "delivery" and detail.get("channel") == channel
    }
    if any(type(value) is not int or value <= 0 for value in started | completed):
        raise AlertError("stored delivery attempt number is invalid")
    if not completed <= started:
        raise AlertError("delivery completion exists without a durable start fact")
    return sorted(started - completed)


def delivery_outcomes(
    records: list[tuple[str, dict[str, Any], dict[str, Any]]], channel: str
) -> list[str]:
    outcomes = [
        detail.get("outcome")
        for kind, detail, _ in records
        if kind == "delivery" and detail.get("channel") == channel
    ]
    if any(value not in OUTCOMES for value in outcomes):
        raise AlertError("stored delivery outcome is invalid")
    return outcomes


def attempts_started(
    records: list[tuple[str, dict[str, Any], dict[str, Any]]], channel: str
) -> int:
    attempts = [
        detail.get("attempt")
        for kind, detail, _ in records
        if kind == "delivery_started" and detail.get("channel") == channel
    ]
    if any(type(value) is not int or value <= 0 for value in attempts):
        raise AlertError("stored delivery attempt number is invalid")
    if len(attempts) != len(set(attempts)):
        raise AlertError("stored delivery attempt number is duplicated")
    return len(attempts)


def required_delivery_met(
    request: dict[str, Any],
    records: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> bool:
    if is_acknowledged(records):
        return True
    required_channel = "phone" if request["severity"] == "critical" else "local"
    return "delivered" in delivery_outcomes(records, required_channel)


def reserve_delivery(
    run_dir: Path,
    request: dict[str, Any],
    records: list[tuple[str, dict[str, Any], dict[str, Any]]],
    channel: str,
) -> int | None:
    if pending_attempts(records, channel):
        return None
    attempt = next_attempt(records, channel)
    started = {
        "schema": SCHEMA,
        "kind": "alert-delivery-started",
        "alert_id": request["alert_id"],
        "channel": channel,
        "attempt": attempt,
    }
    created = append_fact(
        run_dir,
        action=f"alert.delivery_started:{request['alert_id']}:{channel}:{attempt}",
        outcome="info",
        detail=started,
    )
    return attempt if created else None


def run_adapter(path: Path, argv: list[str]) -> tuple[str, str]:
    regular_file(path, "alert delivery adapter")
    try:
        result = subprocess.run(
            [str(path), *argv],
            text=True,
            capture_output=True,
            check=False,
            timeout=90,
        )
    except subprocess.TimeoutExpired as exc:
        return "ambiguous", f"adapter timeout after {exc.timeout}s"
    detail = command_detail(result)
    if result.returncode == 0:
        return "delivered", detail
    if result.returncode == 75:
        return "ambiguous", detail
    return "failed", detail


def record_delivery(
    run_dir: Path,
    request: dict[str, Any],
    *,
    channel: str,
    attempt: int,
    outcome: str,
    detail: str,
) -> None:
    if outcome not in OUTCOMES:
        raise AlertError(f"unsupported delivery outcome: {outcome}")
    receipt = {
        "schema": SCHEMA,
        "kind": "alert-delivery-receipt",
        "alert_id": request["alert_id"],
        "channel": channel,
        "attempt": attempt,
        "outcome": outcome,
        "detail": detail[:1024],
    }
    append_fact(
        run_dir,
        action=f"alert.delivery:{request['alert_id']}:{channel}:{attempt}",
        outcome={"delivered": "ok", "failed": "fail", "ambiguous": "warn"}[outcome],
        detail=receipt,
    )


def deliver(
    run_dir: Path,
    request: dict[str, Any],
    records: list[tuple[str, dict[str, Any], dict[str, Any]]],
    *,
    notify: Path,
    phone: Path,
    phone_attempts: int,
    retry_delay: float,
) -> bool:
    if is_acknowledged(records):
        print(f"alert {request['alert_id']} already acknowledged; no delivery attempted")
        return True
    # The request's dedupe identity covers delivery as well as persistence.
    # Local notification gets one lifetime attempt. A repeated producer call
    # may not turn the same alert into an unbounded mail/speech loop.
    local_attempt = (
        reserve_delivery(run_dir, request, records, "local")
        if attempts_started(records, "local") == 0
        else None
    )
    if local_attempt is not None:
        outcome, detail = run_adapter(
            notify,
            [request["subject"], f"{request['body']}\n\nACK ID: {request['alert_id']}", "--quiet"],
        )
        record_delivery(
            run_dir,
            request,
            channel="local",
            attempt=local_attempt,
            outcome=outcome,
            detail=detail,
        )
    if request["severity"] == "critical":
        for _ in range(phone_attempts):
            current = alert_records(lifecycle_rows(run_dir), request["alert_id"])
            if is_acknowledged(current):
                print(f"alert {request['alert_id']} acknowledged before phone attempt")
                return True
            prior = delivery_outcomes(current, "phone")
            # An answered phone page now needs a human acknowledgement, not a
            # duplicate call. An ambiguous outcome may already be ringing and
            # must likewise never be blindly repeated.
            if "delivered" in prior or "ambiguous" in prior:
                break
            # phone_attempts is a lifetime bound for this deterministic alert,
            # not a fresh allowance every time a producer repeats `raise`.
            if attempts_started(current, "phone") >= phone_attempts:
                break
            attempt = reserve_delivery(run_dir, request, current, "phone")
            if attempt is None:
                # A durable start without a completion is ambiguous: the call
                # may already be on the wire. Never blind-redial it.
                break
            outcome, detail = run_adapter(
                phone,
                [
                    "--alert-id",
                    request["alert_id"],
                    "--subject",
                    request["subject"],
                    "--body",
                    request["body"],
                ],
            )
            record_delivery(
                run_dir,
                request,
                channel="phone",
                attempt=attempt,
                outcome=outcome,
                detail=detail,
            )
            # A positive delivery needs acknowledgement, not another nuisance
            # call. An ambiguous call may already exist and is never blindly
            # repeated. Only a definite failure is eligible for the bounded
            # next attempt.
            if outcome in ("delivered", "ambiguous"):
                break
            latest = alert_records(lifecycle_rows(run_dir), request["alert_id"])
            if attempts_started(latest, "phone") < phone_attempts and retry_delay:
                time.sleep(retry_delay)
    final = alert_records(lifecycle_rows(run_dir), request["alert_id"])
    return required_delivery_met(request, final)


def cmd_raise(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    identity = run_identity(run_dir)
    severity = args.severity
    code = safe_id(args.code, "code")
    dedupe = safe_id(args.dedupe_key, "dedupe key")
    subject = safe_text(args.subject, "subject", 200)
    body = safe_text(args.body, "body", 4000)
    alert_id = request_id(identity, severity, code, dedupe)
    request = required_record(
        identity,
        alert_id=alert_id,
        severity=severity,
        code=code,
        dedupe_key=dedupe,
        subject=subject,
        body=body,
    )
    append_fact(
        run_dir,
        action=f"alert.required:{alert_id}",
        outcome="info",
        detail=request,
    )
    rows = alert_records(lifecycle_rows(run_dir), alert_id)
    stored = request_for(rows)
    if stored != request:
        raise AlertError("existing alert request differs from deterministic request")
    if args.no_deliver:
        print(alert_id)
        return 0
    notify = Path(args.notify_adapter).resolve()
    phone = Path(args.phone_adapter).resolve()
    delivered = deliver(
        run_dir,
        request,
        rows,
        notify=notify,
        phone=phone,
        phone_attempts=args.phone_attempts,
        retry_delay=args.retry_delay,
    )
    print(
        f"alert {alert_id}: {'required delivery recorded' if delivered else 'required delivery unresolved'}; "
        f"explicit acknowledgement still required"
    )
    return 0 if delivered else 1


def cmd_ack(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    identity = run_identity(run_dir)
    alert_id = args.alert_id
    if not re.fullmatch(r"[0-9a-f]{64}", alert_id):
        raise AlertError("alert id must be 64 lowercase hexadecimal characters")
    records = alert_records(lifecycle_rows(run_dir), alert_id)
    request = request_for(records)
    if request["plan_sha256"] != identity["plan_sha256"]:
        raise AlertError("alert request is not bound to this RunPlan")
    actor = args.by
    if not ACTOR_RE.fullmatch(actor):
        raise AlertError("acknowledger identity is invalid")
    detail = {
        "schema": SCHEMA,
        "kind": "alert-acknowledgement",
        "alert_id": alert_id,
        "acknowledged_by": actor,
        "note": args.note,
    }
    append_fact(
        run_dir,
        action=f"alert.acknowledged:{alert_id}",
        outcome="ok",
        detail=detail,
    )
    print(f"acknowledged: {alert_id} by {actor}")
    return 0


def state_for(records: list[tuple[str, dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    request = request_for(records)
    starts = [detail for kind, detail, _ in records if kind == "delivery_started"]
    deliveries = [detail for kind, detail, _ in records if kind == "delivery"]
    acks = [detail for kind, detail, _ in records if kind == "acknowledged"]
    return {
        "request": request,
        "delivery_starts": starts,
        "deliveries": deliveries,
        "delivered": any(item.get("outcome") == "delivered" for item in deliveries),
        "required_delivery_met": required_delivery_met(request, records),
        "ambiguous": any(item.get("outcome") == "ambiguous" for item in deliveries)
        or any(pending_attempts(records, channel) for channel in ("local", "phone")),
        "acknowledged": bool(acks),
        "acknowledgement": acks[-1] if acks else None,
    }


def cmd_status(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    run_identity(run_dir)
    records = alert_records(lifecycle_rows(run_dir))
    ids = sorted({detail.get("alert_id") for _, detail, _ in records if isinstance(detail.get("alert_id"), str)})
    states = [state_for([row for row in records if row[1].get("alert_id") == alert_id]) for alert_id in ids]
    unresolved = [state for state in states if not state["acknowledged"]]
    payload = {
        "schema": SCHEMA,
        "kind": "alert-status",
        "alerts": states,
        "unacknowledged": len(unresolved),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if unresolved else 0


def parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("raise")
    sp.add_argument("--run-dir", required=True)
    sp.add_argument("--severity", choices=SEVERITIES, required=True)
    sp.add_argument("--code", required=True)
    sp.add_argument("--dedupe-key", required=True)
    sp.add_argument("--subject", required=True)
    sp.add_argument("--body", required=True)
    sp.add_argument("--notify-adapter", default=str(root / "notify.sh"))
    sp.add_argument("--phone-adapter", default=str(root / "alert-phone.sh"))
    sp.add_argument("--phone-attempts", type=int, default=2)
    sp.add_argument("--retry-delay", type=float, default=30.0)
    sp.add_argument("--no-deliver", action="store_true")
    sp.set_defaults(fn=cmd_raise)
    sp = sub.add_parser("ack")
    sp.add_argument("--run-dir", required=True)
    sp.add_argument("--alert-id", required=True)
    sp.add_argument("--by", required=True)
    sp.add_argument("--note", default="")
    sp.set_defaults(fn=cmd_ack)
    sp = sub.add_parser("status")
    sp.add_argument("--run-dir", required=True)
    sp.set_defaults(fn=cmd_status)
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if getattr(args, "phone_attempts", 1) not in (1, 2, 3):
            raise AlertError("phone attempts must be 1, 2, or 3")
        if not 0 <= getattr(args, "retry_delay", 0) <= 300:
            raise AlertError("retry delay must be between 0 and 300 seconds")
        return args.fn(args)
    except (AlertError, OSError, subprocess.SubprocessError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
