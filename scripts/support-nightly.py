#!/usr/bin/env python3
"""Install exact push, failover, and watchdog support for one prepared UTC day."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


TERMINALS = {"blocked", "descope", "aborted", "failed"}


def load(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"not a regular file: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"not a JSON object: {path}")
    return value


def reached(status: dict) -> set[str]:
    return {
        str(row.get("transition"))
        for row in status.get("state", {}).get("reached", [])
        if isinstance(row, dict)
    }


def inventory(repo: Path, date: dt.date) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    for path in sorted((repo / "var" / "runs").glob("*/*/control/run-plan.json")):
        try:
            plan = load(path)
            start = dt.datetime.fromisoformat(str(plan["starts_at_utc"]).replace("Z", "+00:00"))
            if start.astimezone(dt.timezone.utc).date() != date:
                continue
            status = load(path.with_name("status.json"))
            got = reached(status)
            state = status.get("state", {})
            if not state.get("active") or state.get("terminal") or got & TERMINALS:
                continue
            if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
                raise ValueError("not a current schema-3 RunPlan")
            run = path.parents[1]
            rows.append({"run": run, "plan": plan, "status": status, "reached": got})
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: {exc}")
    return rows, errors


def slug(ticker: str) -> str:
    head = ticker.split("-", 1)[0]
    prefix = "KXEARNINGSMENTION"
    return (head[len(prefix):] if head.startswith(prefix) else head).lower()


def minute(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%H:%M")


def run(command: list[str], repo: Path, dry_run: bool) -> bool:
    printable = " ".join(command)
    if dry_run:
        print(f"DRY-RUN {printable}")
        return True
    print(f"INSTALL {printable}")
    try:
        proc = subprocess.run(command, cwd=repo, env={**os.environ, "EARSHOT_REPO": str(repo)})
    except OSError as exc:
        print(f"REFUSED: adapter execution failed: {exc}", file=sys.stderr)
        return False
    return proc.returncode == 0


def support_group(repo: Path, ticker: str, rows: list[dict], now: dt.datetime,
                  dry_run: bool) -> list[str]:
    problems = []
    if not rows:
        return ["no active RunPlan"]
    event_ids = [str(row["plan"].get("event_id", "")) for row in rows]
    if len(event_ids) != len(set(event_ids)):
        return ["multiple active attempts for one event identity are ambiguous"]
    lanes = {str(row["plan"].get("lane", "")) for row in rows}
    if len(lanes) != 1:
        return ["one market event has mixed active lane types"]
    if any("scheduled" not in row["reached"] or not (row["run"] / "control" / "schedule-proof.json").is_file() for row in rows):
        return ["every active host RunPlan must be scheduled before support installation"]
    starts = {
        dt.datetime.fromisoformat(str(row["plan"]["starts_at_utc"]).replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        for row in rows
    }
    if len(starts) != 1:
        return ["host RunPlans disagree on start time"]
    start = next(iter(starts))
    if now >= start:
        return [f"event start {start:%Y-%m-%dT%H:%MZ} is not in the future"]

    local_id = os.environ.get("EARSHOT_LOCAL_EXECUTION_HOST", "")
    box_id = os.environ.get("EARSHOT_BOX_EXECUTION_HOST", "earshot-useast1")
    box_host = os.environ.get("EARSHOT_BOX", "ubuntu@3.212.174.94")
    if not local_id or local_id == box_id:
        return ["stable local/box execution-host identities are absent or ambiguous"]
    host_rows = []
    for row in rows:
        identity = str(row["plan"].get("execution_host_id", ""))
        if identity == local_id:
            host = "local"
        elif identity == box_id:
            host = box_host
        else:
            problems.append(f"unsupported execution host {identity!r}")
            continue
        host_rows.append((row, host))
    if problems:
        return problems

    accesses = []
    webcast_rows = [(row, host) for row, host in host_rows if row["plan"].get("lane") == "webcast"]
    for row, _ in webcast_rows:
        try:
            access = load(row["run"] / "control" / "access-plan.json")
            if access.get("schema") != 2:
                raise ValueError("AccessPlan is not canonical schema 2")
            accesses.append(access)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return [f"AccessPlan unavailable for {row['plan']['event_id']}: {exc}"]
    deadlines = set()
    resolver_kinds = set()
    for access in accesses:
        try:
            resolver = access["resolver"]
            kind = resolver["kind"]
            deadline_value = resolver["deadline_utc"]
            deadline_item = dt.datetime.fromisoformat(str(deadline_value).replace("Z", "+00:00"))
            if deadline_item.tzinfo is None:
                raise ValueError("deadline is timezone-naive")
        except (KeyError, TypeError, ValueError) as exc:
            return [f"AccessPlan resolver/deadline is invalid: {exc}"]
        resolver_kinds.add(str(kind))
        deadlines.add(deadline_item.astimezone(dt.timezone.utc))
    if len(resolver_kinds) > 1:
        return ["host AccessPlans disagree on resolver kind"]
    if accesses and len(deadlines) != 1:
        return ["host AccessPlans disagree on resolver deadline"]
    deadline = next(iter(deadlines)) if deadlines else start + dt.timedelta(minutes=45)
    if deadline <= start or deadline <= now:
        return ["resolver deadline does not extend beyond both now and event start"]
    event_slug = slug(ticker)
    watchdog = os.environ.get("_EARSHOT_SUPPORT_WATCHDOG", str(repo / "scripts" / "event-watchdog.sh"))
    failover = os.environ.get("_EARSHOT_SUPPORT_FAILOVER", str(repo / "scripts" / "laptop-failover.sh"))
    push = os.environ.get("_EARSHOT_SUPPORT_PUSH", str(repo / "scripts" / "url-push-job.sh"))

    if "preresolved" in resolver_kinds:
        fire = max(start - dt.timedelta(minutes=15), now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1))
        fire = min(fire, start)
        command = [push, "--install", "--slug", event_slug, "--at-utc", minute(fire), "--until-utc", minute(deadline)]
        for row, host in host_rows:
            command += ["--run-dir", str(row["run"].resolve()), "--host", host]
        if not run(command, repo, dry_run):
            problems.append("URL-push installation/readback failed")

    legs = [f"{row['plan']['event_id']}:{row['plan']['run_id']}:{host}" for row, host in host_rows]
    for offset in (5, 35):
        command = [watchdog, "--install", "--slug", event_slug,
                   "--at-utc", minute(start + dt.timedelta(minutes=offset))]
        for leg in legs:
            command += ["--leg", leg]
        if not run(command, repo, dry_run):
            problems.append(f"start+{offset} watchdog installation/readback failed")

    local = [(row, host) for row, host in host_rows if host == "local"]
    remote = [(row, host) for row, host in host_rows if host != "local"]
    if local and remote and webcast_rows:
        if len(local) != 1 or len(remote) != 1:
            problems.append("failover requires exactly one laptop and one box RunPlan")
        else:
            local_row, _ = local[0]
            box_row, _ = remote[0]
            command = [
                failover, "--install", "--run-dir", str(local_row["run"].resolve()),
                "--box-event", str(box_row["plan"]["event_id"]),
                "--box-run", str(box_row["plan"]["run_id"]),
                "--start-utc", minute(start), "--grace-min",
                os.environ.get("EARSHOT_FAILOVER_GRACE_MIN", "2"),
                "--deadline-utc", minute(deadline), "--box", box_host,
            ]
            if not run(command, repo, dry_run):
                problems.append("laptop failover installation/readback failed")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        wanted = dt.date.fromisoformat(args.date)
    except ValueError as exc:
        parser.error(str(exc))
    repo = Path(os.environ.get("EARSHOT_REPO", Path(__file__).resolve().parent.parent)).resolve()
    raw_now = os.environ.get("_EARSHOT_SUPPORT_NOW_UTC")
    now = (dt.datetime.fromisoformat(raw_now.replace("Z", "+00:00")) if raw_now else dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    if wanted != now.date():
        print("REFUSED: support installers can only encode the current UTC event date", file=sys.stderr)
        return 1
    rows, errors = inventory(repo, wanted)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        ticker = row["plan"].get("market_event_ticker")
        if isinstance(ticker, str):
            grouped[ticker].append(row)
    problems = list(errors)
    if not grouped:
        problems.append("zero active event groups")
    for ticker, group in sorted(grouped.items()):
        details = support_group(repo, ticker, group, now, args.dry_run)
        if details:
            problems.extend(f"{ticker}: {detail}" for detail in details)
        else:
            print(f"SUPPORTED {ticker}")
    for problem in problems:
        print(f"REFUSED: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
