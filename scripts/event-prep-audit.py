#!/usr/bin/env python3
"""Fail-closed final readiness audit for one UTC event date.

This is the executable definition of READY used by ``/event-prep`` and
``opsctl prepare-day``. It joins the discovery slate to active schema-3
RunPlans and then checks the proof surfaces that used to live only in operator
memory: access, study, measured posture, preflight, loaded scheduling, URL
push, box/laptop failover, and two live watchdogs.

Read-only. Host jobs are inspected through launchctl; no job is installed or
modified here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import defaultdict
from pathlib import Path


TERMINALS = {"descope", "blocked", "aborted", "failed", "completed"}
PHASE_ORDER = (
    "prepared",
    "access_acquired",
    "study_completed",
    "preflight_passed",
    "scheduled",
)


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def reached(status: dict) -> set[str]:
    return {
        row.get("transition")
        for row in status.get("state", {}).get("reached", [])
        if isinstance(row, dict) and isinstance(row.get("transition"), str)
    }


def active(status: dict) -> bool:
    state = status.get("state", {})
    return state.get("active") is True and not state.get("terminal")


def issuer_slug(event_ticker: str) -> str:
    head = event_ticker.split("-", 1)[0]
    prefix = "KXEARNINGSMENTION"
    return (head[len(prefix) :] if head.startswith(prefix) else head).lower()


def postured(config: Path) -> tuple[bool, str]:
    try:
        text = config.read_text(encoding="utf-8")
        parsed = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return False, f"config unreadable: {exc}"
    keywords = parsed.get("keywords")
    if not isinstance(keywords, list) or not keywords:
        return True, "capture-only"
    blocks = re.findall(r"(?ms)^\[\[keywords\]\]\s*(.*?)(?=^\[\[|^\[[^[]|\Z)", text)
    if len(blocks) != len(keywords):
        return False, "keyword blocks could not be enumerated"
    missing = []
    for block, keyword in zip(blocks, keywords):
        ticker = keyword.get("market_ticker", "unknown") if isinstance(keyword, dict) else "unknown"
        line = re.search(r"(?m)^\s*max_price_cents\s*=.*$", block)
        if not line or "# posture:" not in line.group(0):
            missing.append(str(ticker))
    if missing:
        return False, "no measured posture marker: " + ",".join(missing)
    return True, "measured markers present"


def resilient_hls_sources(config: Path) -> tuple[bool, str]:
    """Require both production survivor transports for a resolved HLS URL."""
    try:
        parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return False, f"config unreadable: {exc}"
    sources = parsed.get("sources")
    if not isinstance(sources, list):
        return False, "config sources are missing"
    commands = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        cmd = source.get("cmd")
        if isinstance(cmd, list) and all(isinstance(part, str) for part in cmd):
            commands.append(" ".join(cmd))
    missing = []
    if not any("hls-edge-fetch.py" in command for command in commands):
        missing.append("edge_fetch")
    if not any("cdn-cachebust-fetch.sh" in command for command in commands):
        missing.append("cachebust")
    if missing:
        return False, "missing production HLS survivor source(s): " + ",".join(missing)
    return True, "edge re-resolution and cache-busting sources present"


def launchctl_snapshot() -> tuple[str, str | None]:
    command = os.environ.get("_EARSHOT_EVENT_PREP_LAUNCHCTL", "launchctl")
    try:
        proc = subprocess.run(
            [command, "print", f"gui/{os.getuid()}"],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"launchctl readback unavailable: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return "", f"launchctl readback failed: {detail[-1] if detail else proc.returncode}"
    return proc.stdout, None


def expected_events(repo: Path, date: str) -> tuple[set[str], list[str]]:
    path = repo / "slate" / "latest.json"
    try:
        slate = load_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return set(), [f"discovery slate unreadable: {exc}"]
    result = set()
    for event in slate.get("events", []):
        if not isinstance(event, dict) or event.get("call_date") != date:
            continue
        ticker = event.get("event_ticker")
        if isinstance(ticker, str) and ticker.startswith("KXEARNINGSMENTION"):
            result.add(ticker)
    if not result:
        return result, [f"discovery found zero earnings-mention events for {date}"]
    return result, []


def inventory(repo: Path, date: str) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    for plan_path in sorted((repo / "var" / "runs").glob("*/*/control/run-plan.json")):
        try:
            plan = load_json(plan_path)
            if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
                continue
            if str(plan.get("starts_at_utc", ""))[:10] != date:
                continue
            run = plan_path.parents[1]
            status = load_json(run / "control" / "status.json")
            if active(status):
                rows.append({"run": run, "plan": plan, "status": status})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{plan_path}: {exc}")
    return rows, errors


def classify_group(
    repo: Path, ticker: str, rows: list[dict], launchctl: str, launch_error: str | None
) -> tuple[str, list[str], str]:
    if not rows:
        return "NEEDS-STAGE", ["no active schema-3 RunPlan"], f"scripts/opsctl.sh stage --date DATE"

    issues: dict[str, list[str]] = defaultdict(list)
    by_event: dict[str, int] = defaultdict(int)
    preresolved = False
    for row in rows:
        run, plan, status = row["run"], row["plan"], row["status"]
        event_id = str(plan.get("event_id", run.parent.name))
        by_event[event_id] += 1
        got = reached(status)
        # prepared/study_completed are proof-bearing receipts emitted by
        # preflight-proof after it validates staging/readiness. Their absence
        # is therefore a preflight blocker, not evidence that staging or the
        # TTS measurement itself never ran.
        if "prepared" not in got:
            issues["NEEDS-PREFLIGHT"].append(f"{event_id}: missing prepared proof receipt")
        if plan.get("lane") == "webcast":
            access_path = run / "control" / "access-plan.json"
            resolver_kind = None
            if "access_acquired" not in got or not access_path.is_file():
                issues["NEEDS-ACCESS"].append(f"{event_id}: AccessPlan/access receipt missing")
            else:
                try:
                    access = load_json(access_path)
                    if access.get("schema") != 2:
                        issues["NEEDS-ACCESS"].append(f"{event_id}: AccessPlan is not schema 2")
                    resolver_kind = access.get("resolver", {}).get("kind")
                    preresolved |= resolver_kind == "preresolved"
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    issues["NEEDS-ACCESS"].append(f"{event_id}: AccessPlan unreadable: {exc}")
        config = repo / str(plan.get("config_path", ""))
        if plan.get("lane") == "webcast" and resolver_kind not in (None, "youtube-live", "static-public"):
            transport_ok, transport_detail = resilient_hls_sources(config)
            if not transport_ok:
                issues["NEEDS-TRANSPORT"].append(f"{event_id}: {transport_detail}")
        ready = repo / "configs" / ".readiness" / f"{config.stem}.json"
        try:
            artifact = load_json(ready)
            if not isinstance(artifact.get("keywords"), list):
                raise ValueError("keywords is not a list")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issues["NEEDS-STUDY"].append(f"{event_id}: readiness artifact invalid/missing: {exc}")
        if "study_completed" not in got:
            issues["NEEDS-PREFLIGHT"].append(f"{event_id}: missing study_completed proof receipt")
        posture_ok, posture_detail = postured(config)
        if not posture_ok:
            issues["NEEDS-POSTURE"].append(f"{event_id}: {posture_detail}")
        if "preflight_passed" not in got:
            issues["NEEDS-PREFLIGHT"].append(f"{event_id}: missing preflight_passed receipt")
        if "scheduled" not in got or not (run / "control" / "schedule-proof.json").is_file():
            issues["NEEDS-SCHEDULE"].append(f"{event_id}: schedule receipt/proof missing")

    for event_id, count in by_event.items():
        if count > 1:
            issues["NEEDS-STAGE"].append(f"{event_id}: {count} active attempts are ambiguous")

    # Support jobs are meaningful only after the underlying run is schedulable.
    if not any(issues[key] for key in (
        "NEEDS-STAGE", "NEEDS-ACCESS", "NEEDS-TRANSPORT", "NEEDS-STUDY", "NEEDS-POSTURE",
        "NEEDS-PREFLIGHT", "NEEDS-SCHEDULE",
    )):
        slug = issuer_slug(ticker)
        if launch_error:
            issues["NEEDS-SUPPORT"].append(launch_error)
        watchdogs = set(re.findall(rf"com\.earshot\.watchdog\.{re.escape(slug)}\.\d{{4}}", launchctl))
        if len(watchdogs) < 2:
            issues["NEEDS-SUPPORT"].append(f"{slug}: {len(watchdogs)}/2 watchdog jobs loaded")
        if preresolved and f"com.earshot.push.{slug}" not in launchctl:
            issues["NEEDS-SUPPORT"].append(f"{slug}: PCM-gated multi-host URL push job not loaded")
        hosts = {str(row["plan"].get("execution_host_id")) for row in rows}
        if len(hosts) > 1:
            local = next((row for row in rows if "-BOX-" not in row["plan"].get("event_id", "")), None)
            proof = repo / "state" / str(local["plan"].get("event_id")) / "failover-install.json" if local else None
            if proof is None or not proof.is_file():
                issues["NEEDS-SUPPORT"].append(f"{slug}: box-primary/laptop-failover wrapper not proven")

    priority = (
        "NEEDS-STAGE", "NEEDS-ACCESS", "NEEDS-TRANSPORT", "NEEDS-STUDY", "NEEDS-POSTURE",
        "NEEDS-PREFLIGHT", "NEEDS-SCHEDULE", "NEEDS-SUPPORT",
    )
    for status in priority:
        if issues[status]:
            actions = {
                "NEEDS-STAGE": "scripts/opsctl.sh stage --date DATE",
                "NEEDS-ACCESS": "bind the exact active RunPlan, then scripts/opsctl.sh access --date DATE",
                "NEEDS-TRANSPORT": "stage a fresh RunPlan from the resilient four-leg webcast template",
                "NEEDS-STUDY": "scripts/opsctl.sh study --date DATE",
                "NEEDS-POSTURE": "run posture-from-study on the active config, then publish a new RunPlan attempt",
                "NEEDS-PREFLIGHT": "scripts/opsctl.sh preflight --date DATE",
                "NEEDS-SCHEDULE": "scripts/opsctl.sh schedule --date DATE",
                "NEEDS-SUPPORT": "install/read back URL push, failover, and two watchdog jobs for this event",
            }
            return status, issues[status], actions[status]
    return "READY", ["all code-enforced preparation and support proofs present"], "none"


def audit(repo: Path, date: str) -> dict:
    expected, discovery_errors = expected_events(repo, date)
    rows, inventory_errors = inventory(repo, date)
    launchctl, launch_error = launchctl_snapshot()
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        ticker = row["plan"].get("market_event_ticker")
        if isinstance(ticker, str):
            grouped[ticker].append(row)
    all_tickers = sorted(expected | set(grouped))
    events = []
    for ticker in all_tickers:
        status, details, action = classify_group(repo, ticker, grouped[ticker], launchctl, launch_error)
        events.append({"event_ticker": ticker, "status": status, "details": details, "next_action": action.replace("DATE", date)})
    global_errors = discovery_errors + inventory_errors
    ok = bool(events) and not global_errors and all(row["status"] == "READY" for row in events)
    return {"schema": 1, "date": date, "ready": ok, "errors": global_errors, "events": events}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        dt.date.fromisoformat(args.date)
    except ValueError:
        parser.error("--date must be YYYY-MM-DD")
    repo = Path(os.environ.get("EARSHOT_REPO", Path(__file__).resolve().parent.parent)).resolve()
    result = audit(repo, args.date)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"EVENT-PREP READINESS {args.date}")
        for error in result["errors"]:
            print(f"NEEDS-DISCOVERY | {error}")
        for row in result["events"]:
            print(f"{row['status']:<16} | {row['event_ticker']} | {'; '.join(row['details'])}")
            if row["status"] != "READY":
                print(f"  NEXT: {row['next_action']}")
        print("EVENT-PREP READY" if result["ready"] else "EVENT-PREP NOT READY")
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
