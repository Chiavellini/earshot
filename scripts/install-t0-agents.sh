#!/usr/bin/env bash
# Install or verify one schema-3 webcast RunPlan as one launchd agent.
#
#   scripts/install-t0-agents.sh \
#     --run-dir var/runs/<EVENT_ID>/<RUN_ID> \
#     --expected-host <stable-laptop-id> [--fire-at-utc UTC] [--verify-only]
#
# The old implementation inferred events from filenames and installed a watch,
# escalation ladder, probe page, and two liveness jobs in one pass. That made
# "scheduled" mean five unrelated intentions and required per-market maps.
# This boundary installs exactly one plan-derived runtime handoff at T-75.
# A reviewed late recovery may set an exact future --fire-at-utc no later than
# the RunPlan start; the loaded readback and proof bind that override exactly.
# Access-plan owns resolver strategy; arm-on-url owns capture proof and the one
# launch. `scheduled` is appended only after launchctl readback is bound to the
# exact RunPlan by schedule-proof.py.
set -euo pipefail

RUN_DIR_RAW="" EXPECTED_HOST="" VERIFY_ONLY=0 FIRE_AT_UTC=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir)       RUN_DIR_RAW="${2:?}"; shift 2 ;;
    --expected-host) EXPECTED_HOST="${2:?}"; shift 2 ;;
    --fire-at-utc)   FIRE_AT_UTC="${2:?}"; shift 2 ;;
    --verify-only)   VERIFY_ONLY=1; shift ;;
    -h|--help)       sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPO=$(cd "$REPO" && pwd -P)
cd "$REPO"
[[ -n "$RUN_DIR_RAW" && -n "$EXPECTED_HOST" ]] || {
  echo "usage: $0 --run-dir var/runs/<EVENT_ID>/<RUN_ID> --expected-host <stable-id> [--verify-only]" >&2
  exit 2
}

LAUNCHCTL="${EARSHOT_LAUNCHCTL:-launchctl}"
PYTHON="${EARSHOT_PYTHON:-python3}"
PLIST_DIR="${EARSHOT_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"
UID_N="${EARSHOT_LAUNCH_UID:-$(id -u)}"

RUN_DIR=$("$PYTHON" - "$REPO" "$RUN_DIR_RAW" <<'PY'
from pathlib import Path
import sys
repo = Path(sys.argv[1]).resolve()
run = Path(sys.argv[2])
run = (run if run.is_absolute() else repo / run).resolve()
try:
    rel = run.relative_to(repo / "var" / "runs")
except ValueError:
    raise SystemExit("run directory is outside canonical var/runs")
if len(rel.parts) != 2:
    raise SystemExit("run directory must be var/runs/<EVENT_ID>/<RUN_ID>")
print(run)
PY
) || { echo "REFUSED: invalid --run-dir '$RUN_DIR_RAW'" >&2; exit 2; }

EARSHOT_REPO="$REPO" "$PYTHON" scripts/eventctl.py verify --run-dir "$RUN_DIR" >/dev/null || {
  echo "REFUSED: RunPlan/ledger/pinned-input verification failed" >&2
  exit 1
}
EARSHOT_REPO="$REPO" "$PYTHON" scripts/access-plan.py validate --run-dir "$RUN_DIR" --canonical >/dev/null || {
  echo "REFUSED: webcast RunPlan has no canonical schema-2 immutable AccessPlan" >&2
  exit 1
}

PLAN_EXPORTS=$("$PYTHON" - "$REPO" "$RUN_DIR" "$EXPECTED_HOST" "$FIRE_AT_UTC" <<'PY'
import datetime as dt
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

repo, run, expected_host, fire_override = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
plan = json.loads((run / "control/run-plan.json").read_text())
access = json.loads((run / "control/access-plan.json").read_text())
if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
    raise SystemExit("schema-3 RunPlan required")
if plan.get("lane") != "webcast":
    raise SystemExit(f"webcast scheduler received lane={plan.get('lane')!r}")
if plan.get("execution_enabled") is not True or plan.get("capture_only") is not False:
    raise SystemExit("webcast scheduler requires an execution-enabled RunPlan")
if access.get("resolver", {}).get("kind") == "static-public":
    raise SystemExit(
        "static-public AccessPlan cannot produce the exact endpoint-bound PCM proof yet"
    )
if plan.get("execution_host_id") != expected_host:
    raise SystemExit(
        f"RunPlan host {plan.get('execution_host_id')!r} != expected host {expected_host!r}"
    )
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", expected_host):
    raise SystemExit("expected host is not a stable host ID")
unsafe = re.compile(r"[\s'\"\\%]")
for label, value in (("repo", str(repo)), ("run", str(run)), ("host", expected_host)):
    if unsafe.search(value):
        raise SystemExit(f"{label} contains characters unsupported by launchd boundary")
status = subprocess.run(
    [sys.executable, str(repo / "scripts/eventctl.py"), "status", "--run-dir", str(run), "--json"],
    cwd=repo, text=True, capture_output=True, check=True,
    env={**__import__('os').environ, "EARSHOT_REPO": str(repo)},
)
state = json.loads(status.stdout)["state"]
reached = {r["transition"] for r in state["reached"]}
if "preflight_passed" not in reached:
    raise SystemExit("RunPlan has not reached preflight_passed")
if not state["active"]:
    raise SystemExit("RunPlan is terminal or archived")
start = dt.datetime.fromisoformat(plan["starts_at_utc"].replace("Z", "+00:00"))
if fire_override:
    try:
        fire = dt.datetime.fromisoformat(fire_override.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"--fire-at-utc is not ISO-8601: {exc}")
    if fire.utcoffset() != dt.timedelta(0) or not fire_override.endswith("Z"):
        raise SystemExit("--fire-at-utc must be an exact UTC timestamp ending in Z")
    fire = fire.astimezone(dt.timezone.utc)
    if fire <= dt.datetime.now(dt.timezone.utc):
        raise SystemExit("--fire-at-utc must be strictly future")
    if fire > start:
        raise SystemExit("--fire-at-utc may not be after RunPlan starts_at_utc")
else:
    fire = start - dt.timedelta(minutes=75)
local = fire.astimezone()
argv = [str(repo / "scripts" / "arm-on-url.sh"), "--run-dir", str(run)]
argv += ["--i-am-live", "--fillable"]
env_rel = plan.get("event_env_path")
if not env_rel:
    raise SystemExit("webcast RunPlan has no pinned event env/account locator")
env_path = (repo / env_rel).resolve()
if not env_path.is_file() or env_path.is_symlink():
    raise SystemExit(f"webcast event env is not a regular file: {env_path}")
match = re.search(
    r"^\s*(?:export\s+)?(?:EARSHOT_ACCOUNT_PROFILE|ACCOUNT_ENV)=[\"\x27]([^\"\x27]+)[\"\x27]",
    env_path.read_text(errors="replace"),
    re.M,
)
if not match:
    raise SystemExit("webcast event env has no literal account profile locator")
raw_account = match.group(1)
home = str(Path.home())
raw_account = raw_account.replace("${HOME}", home).replace("$HOME", home)
if raw_account.startswith("~/"):
    raw_account = home + raw_account[1:]
account = Path(raw_account)
if not account.is_absolute():
    raise SystemExit("webcast account profile locator must resolve to an absolute path")
account = account.resolve()
if not account.is_file() or account.is_symlink():
    raise SystemExit(f"webcast account profile is not a regular file: {account}")
values = {
    "PLAN_EVENT_ID": plan["event_id"],
    "PLAN_RUN_ID": plan["run_id"],
    "PLAN_SHA256": plan["plan_sha256"],
    "PLAN_HOST": plan["execution_host_id"],
    "ACCOUNT_ENV": str(account),
    "LABEL": f"com.earshot.run.{plan['plan_sha256'][:20]}",
    "FIRE_AT_UTC": fire.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "LOCAL_MONTH": str(local.month),
    "LOCAL_DAY": str(local.day),
    "LOCAL_HOUR": str(local.hour),
    "LOCAL_MINUTE": str(local.minute),
    "PROGRAM_JSON": json.dumps(argv, separators=(",", ":")),
}
for key, value in values.items():
    print(f"{key}={shlex.quote(value)}")
PY
) || { echo "REFUSED: RunPlan is not schedulable on this host" >&2; exit 1; }
eval "$PLAN_EXPORTS"

PLIST="$PLIST_DIR/$LABEL.plist"
DOMAIN="gui/$UID_N"
TARGET="$DOMAIN/$LABEL"

rollback_loaded_job() {
  [[ "$VERIFY_ONLY" == 1 ]] && return 0
  "$LAUNCHCTL" bootout "$TARGET" >/dev/null 2>&1 || true
}

if [[ "$VERIFY_ONLY" != 1 ]]; then
  mkdir -p "$PLIST_DIR"
  PLIST_TMP=$(mktemp "$PLIST_DIR/.${LABEL}.XXXXXX")
  cleanup_plist_tmp() { rm -f -- "$PLIST_TMP"; }
  trap cleanup_plist_tmp EXIT
  PROGRAM_JSON="$PROGRAM_JSON" "$PYTHON" - "$PLIST_TMP" "$LABEL" "$REPO" \
    "$RUN_DIR" "$PLAN_SHA256" "$PLAN_HOST" "$ACCOUNT_ENV" "$LOCAL_MONTH" "$LOCAL_DAY" \
    "$LOCAL_HOUR" "$LOCAL_MINUTE" <<'PY'
import os
import plistlib
import sys

path, label, repo, run, plan_sha, host, account, month, day, hour, minute = sys.argv[1:]
import json
argv = json.loads(os.environ["PROGRAM_JSON"])
value = {
    "Label": label,
    "ProgramArguments": argv,
    "WorkingDirectory": repo,
    "EnvironmentVariables": {
        "EARSHOT_EXECUTION_HOST": host,
        "EARSHOT_ACCOUNT_ENV": account,
        "EARSHOT_REPO": repo,
        "EARSHOT_RUN_DIR": run,
        "EARSHOT_RUN_PLAN_SHA256": plan_sha,
    },
    "StartCalendarInterval": {
        "Month": int(month),
        "Day": int(day),
        "Hour": int(hour),
        "Minute": int(minute),
    },
    "RunAtLoad": False,
    "ProcessType": "Background",
}
with open(path, "wb") as fh:
    plistlib.dump(value, fh, sort_keys=True)
    fh.flush()
    os.fsync(fh.fileno())
PY
  chmod 0644 "$PLIST_TMP"
  mv -f -- "$PLIST_TMP" "$PLIST"
  trap - EXIT
  "$LAUNCHCTL" bootout "$TARGET" >/dev/null 2>&1 || true
  "$LAUNCHCTL" bootstrap "$DOMAIN" "$PLIST" || { rollback_loaded_job; exit 1; }
fi

PLIST=$(cd "$(dirname "$PLIST")" && pwd -P)/$(basename "$PLIST")
PRINTED=$("$LAUNCHCTL" print "$TARGET" 2>/dev/null) || {
  echo "REFUSED: launchctl does not report $TARGET loaded" >&2
  rollback_loaded_job
  exit 1
}

READBACK=$(mktemp "${TMPDIR:-/tmp}/earshot-launchd-readback.XXXXXX")
cleanup_readback() { rm -f -- "$READBACK"; }
trap cleanup_readback EXIT
if ! PROGRAM_JSON="$PROGRAM_JSON" PRINTED="$PRINTED" "$PYTHON" - \
  "$PLIST" "$READBACK" "$PLAN_EVENT_ID" "$PLAN_RUN_ID" "$PLAN_SHA256" \
  "$PLAN_HOST" "$LABEL" "$FIRE_AT_UTC" "$REPO" "$RUN_DIR" "$ACCOUNT_ENV" \
  "$LOCAL_MONTH" "$LOCAL_DAY" "$LOCAL_HOUR" "$LOCAL_MINUTE" <<\PY
import hashlib
import json
import os
import plistlib
import re
import sys
from pathlib import Path

(
    plist_path, out, event, run_id, plan_sha, host, label, fire, repo, run,
    account, month, day, hour, minute,
) = sys.argv[1:]
path = Path(plist_path)
if path.is_symlink() or not path.is_file():
    raise SystemExit("loaded launchd descriptor path is not a regular plist")
with path.open("rb") as fh:
    actual = plistlib.load(fh)
argv = json.loads(os.environ["PROGRAM_JSON"])
environment = {
    "EARSHOT_EXECUTION_HOST": host,
    "EARSHOT_ACCOUNT_ENV": account,
    "EARSHOT_REPO": repo,
    "EARSHOT_RUN_DIR": run,
    "EARSHOT_RUN_PLAN_SHA256": plan_sha,
}
expected = {
    "Label": label,
    "ProgramArguments": argv,
    "WorkingDirectory": repo,
    "EnvironmentVariables": environment,
    "StartCalendarInterval": {
        "Month": int(month), "Day": int(day),
        "Hour": int(hour), "Minute": int(minute),
    },
    "RunAtLoad": False,
    "ProcessType": "Background",
}
if actual != expected:
    raise SystemExit("plist bytes do not describe the exact RunPlan job")

printed = os.environ["PRINTED"]
if not re.search(rf"(?m)^\s*path\s*=\s*{re.escape(str(path))}\s*$", printed):
    raise SystemExit("launchctl loaded job does not point at the exact plist")
if not re.search(rf"(?m)^\s*working directory\s*=\s*{re.escape(repo)}\s*$", printed):
    raise SystemExit("launchctl loaded job has the wrong working directory")

arg_match = re.search(r"(?ms)^\s*arguments\s*=\s*\{\s*(.*?)^\s*\}", printed)
if not arg_match:
    raise SystemExit("launchctl print has no arguments block")
loaded_argv = [line.strip() for line in arg_match.group(1).splitlines() if line.strip()]
if loaded_argv != argv:
    raise SystemExit(f"launchctl loaded argv differs: {loaded_argv!r}")
for key, value in environment.items():
    pattern = rf"(?m)^\s*{re.escape(key)}\s*=>\s*{re.escape(value)}\s*$"
    if not re.search(pattern, printed):
        raise SystemExit(f"launchctl loaded environment differs for {key}")
for key, value in expected["StartCalendarInterval"].items():
    pattern = rf"(?m)^\s*{key}\s*(?:=>|=)\s*{value}\s*$"
    if not re.search(pattern, printed):
        raise SystemExit(f"launchctl loaded calendar differs for {key}")

payload = {
    "schema": 1,
    "kind": "loaded-schedule-readback",
    "scheduler": "launchd",
    "loaded": True,
    "event_id": event,
    "run_id": run_id,
    "run_plan_sha256": plan_sha,
    "execution_host_id": host,
    "label": label,
    "fire_at_utc": fire,
    "program_arguments": argv,
    "environment": environment,
    "descriptor_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    "stop_timeout_seconds": None,
    "kill_mode": None,
}
with open(out, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2, sort_keys=True)
    fh.write("\n")
    fh.flush()
    os.fsync(fh.fileno())
PY
then
  echo "REFUSED: loaded launchd descriptor does not match the RunPlan" >&2
  rollback_loaded_job
  exit 1
fi

PROOF_ARGS=(--run-dir "$RUN_DIR" --readback "$READBACK")
[[ -n "$FIRE_AT_UTC" ]] && PROOF_ARGS+=(--fire-at-utc "$FIRE_AT_UTC")
[[ "$VERIFY_ONLY" == 1 ]] && PROOF_ARGS+=(--verify-only)
if ! EARSHOT_REPO="$REPO" "$PYTHON" scripts/schedule-proof.py "${PROOF_ARGS[@]}"; then
  echo "REFUSED: loaded launchd job was not admitted as scheduled" >&2
  rollback_loaded_job
  exit 1
fi

echo "SCHEDULED: $PLAN_EVENT_ID/$PLAN_RUN_ID via $LABEL at $FIRE_AT_UTC (host=$PLAN_HOST)"
echo "  disarm: $LAUNCHCTL bootout $TARGET"
