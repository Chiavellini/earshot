#!/usr/bin/env bash
# Install or verify one schema-3 phone RunPlan as one systemd USER timer.
#
#   scripts/install-event-timer.sh \
#     --run-dir var/runs/<EVENT_ID>/<RUN_ID> \
#     --expected-host earshot-useast1 [--verify-only] [--repo <dir>]
#
# This is deliberately not an event-env/date/HHMM template. The immutable
# RunPlan owns identity and starts_at; the phone schedule is always T-25. A
# successful return means all of the following are true:
#
#   * systemd reports both units loaded with no pending daemon reload;
#   * the one timer is active+enabled and its effective descriptor is exact;
#   * ExecStart names this canonical run once;
#   * TimeoutStopSec=150 and KillMode=mixed preserve launch-event's >=90 s
#     evidence/order drain before systemd's outer kill boundary;
#   * schedule-proof.py durably bound that readback to the RunPlan before the
#     lifecycle ledger reached `scheduled`.
#
# No dial or runtime launch occurs here. Verification may be exercised with
# EARSHOT_SYSTEMCTL/EARSHOT_LOGINCTL stubs; production defaults to the real
# commands.
set -euo pipefail

RUN_DIR_RAW="" EXPECTED_HOST="" REPO="" VERIFY_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir)       RUN_DIR_RAW="${2:?}"; shift 2 ;;
    --expected-host) EXPECTED_HOST="${2:?}"; shift 2 ;;
    --repo)          REPO="${2:?}"; shift 2 ;;
    --verify-only)   VERIFY_ONLY=1; shift ;;
    -h|--help)       sed -n '2,31p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

REPO="${REPO:-${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
REPO=$(cd "$REPO" && pwd -P)
cd "$REPO"
[[ -n "$RUN_DIR_RAW" && -n "$EXPECTED_HOST" ]] || {
  echo "usage: $0 --run-dir var/runs/<EVENT_ID>/<RUN_ID> --expected-host <stable-id> [--verify-only] [--repo <dir>]" >&2
  exit 2
}

SYSTEMCTL="${EARSHOT_SYSTEMCTL:-systemctl}"
LOGINCTL="${EARSHOT_LOGINCTL:-loginctl}"
UNIT_DIR="${EARSHOT_SYSTEMD_USER_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}"

resolve_run() {
  python3 - "$REPO" "$RUN_DIR_RAW" <<'PY'
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
}
RUN_DIR=$(resolve_run) || { echo "REFUSED: invalid --run-dir '$RUN_DIR_RAW'" >&2; exit 2; }

EARSHOT_REPO="$REPO" python3 scripts/eventctl.py verify --run-dir "$RUN_DIR" >/dev/null || {
  echo "REFUSED: RunPlan/ledger/pinned-input verification failed" >&2
  exit 1
}

# Export only shell-safe, plan-derived values. No event env is sourced here.
PLAN_EXPORTS=$(python3 - "$REPO" "$RUN_DIR" "$EXPECTED_HOST" <<'PY'
import datetime as dt
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

repo, run, expected_host = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
plan = json.loads((run / "control/run-plan.json").read_text())
if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
    raise SystemExit("schema-3 RunPlan required")
if plan.get("lane") != "phone":
    raise SystemExit(f"phone scheduler received lane={plan.get('lane')!r}")
if plan.get("execution_enabled") is not True:
    raise SystemExit("phone scheduler requires an execution-enabled plan")
if plan.get("execution_host_id") != expected_host:
    raise SystemExit(
        f"RunPlan host {plan.get('execution_host_id')!r} != expected host {expected_host!r}"
    )
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", expected_host):
    raise SystemExit("expected host is not a stable host ID")
unsafe = re.compile(r"[\s'\"\\%]")
for label, value in (("repo", str(repo)), ("run", str(run)), ("host", expected_host)):
    if unsafe.search(value):
        raise SystemExit(f"{label} contains characters unsupported by the unit boundary")
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
fire = start - dt.timedelta(minutes=25)
values = {
    "PLAN_EVENT_ID": plan["event_id"],
    "PLAN_RUN_ID": plan["run_id"],
    "PLAN_SHA256": plan["plan_sha256"],
    "PLAN_HOST": plan["execution_host_id"],
    "LABEL": f"com.earshot.run.{plan['plan_sha256'][:20]}",
    "FIRE_AT_UTC": fire.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "ON_CALENDAR": fire.strftime("%Y-%m-%d %H:%M:%S UTC"),
}
for key, value in values.items():
    print(f"{key}={shlex.quote(value)}")
PY
) || { echo "REFUSED: RunPlan is not schedulable on this host" >&2; exit 1; }
eval "$PLAN_EXPORTS"

SERVICE="$LABEL.service"
TIMER="$LABEL.timer"
SERVICE_PATH="$UNIT_DIR/$SERVICE"
TIMER_PATH="$UNIT_DIR/$TIMER"
SERVICE_BODY="[Unit]
Description=EARSHOT schema-3 phone run $PLAN_EVENT_ID/$PLAN_RUN_ID

[Service]
Type=oneshot
WorkingDirectory=$REPO
Environment=EARSHOT_REPO=$REPO
Environment=EARSHOT_RUN_DIR=$RUN_DIR
Environment=EARSHOT_RUN_PLAN_SHA256=$PLAN_SHA256
Environment=EARSHOT_EXECUTION_HOST=$PLAN_HOST
ExecStart=$REPO/scripts/event-autorun.sh --run-dir $RUN_DIR
TimeoutStopSec=150
KillMode=mixed"
TIMER_BODY="[Unit]
Description=EARSHOT schema-3 fire timer $PLAN_EVENT_ID/$PLAN_RUN_ID

[Timer]
OnCalendar=$ON_CALENDAR
AccuracySec=1s
Persistent=true
Unit=$SERVICE

[Install]
WantedBy=timers.target"

rollback_loaded_job() {
  [[ "$VERIFY_ONLY" == 1 ]] && return 0
  "$SYSTEMCTL" --user disable --now "$TIMER" >/dev/null 2>&1 || true
  "$SYSTEMCTL" --user stop "$SERVICE" >/dev/null 2>&1 || true
}

# User lingering is part of loaded-job readiness, not an installation hint.
LINGER=$("$LOGINCTL" show-user "${USER:?}" --property=Linger --value 2>/dev/null || true)
[[ "$LINGER" == yes ]] || {
  echo "REFUSED: lingering is OFF/unknown for $USER; user timers are not unattended" >&2
  exit 1
}

if [[ "$VERIFY_ONLY" != 1 ]]; then
  mkdir -p "$UNIT_DIR"
  SERVICE_TMP=$(mktemp "$UNIT_DIR/.${SERVICE}.XXXXXX")
  TIMER_TMP=$(mktemp "$UNIT_DIR/.${TIMER}.XXXXXX")
  cleanup_unit_temps() { rm -f -- "$SERVICE_TMP" "$TIMER_TMP"; }
  trap cleanup_unit_temps EXIT
  printf '%s\n' "$SERVICE_BODY" > "$SERVICE_TMP"
  printf '%s\n' "$TIMER_BODY" > "$TIMER_TMP"
  chmod 0644 "$SERVICE_TMP" "$TIMER_TMP"
  mv -f -- "$SERVICE_TMP" "$SERVICE_PATH"
  mv -f -- "$TIMER_TMP" "$TIMER_PATH"
  trap - EXIT
  "$SYSTEMCTL" --user daemon-reload || { rollback_loaded_job; exit 1; }
  "$SYSTEMCTL" --user enable --now "$TIMER" || { rollback_loaded_job; exit 1; }
fi

# Prove manager state, including that its cache is current. systemctl cat is
# compared to the exact descriptor while NeedDaemonReload=no binds those bytes
# to the manager's loaded view. Extra drop-ins or a stale/edited unit fail.
show_value() { "$SYSTEMCTL" --user show "$1" --property="$2" --value 2>/dev/null; }
for unit in "$SERVICE" "$TIMER"; do
  [[ "$(show_value "$unit" LoadState)" == loaded ]] || {
    echo "REFUSED: $unit is not loaded" >&2; rollback_loaded_job; exit 1;
  }
  [[ "$(show_value "$unit" NeedDaemonReload)" == no ]] || {
    echo "REFUSED: $unit has a pending daemon reload" >&2; rollback_loaded_job; exit 1;
  }
done
[[ "$(show_value "$TIMER" ActiveState)" == active ]] || {
  echo "REFUSED: $TIMER is not active" >&2; rollback_loaded_job; exit 1;
}
UNIT_FILE_STATE=$(show_value "$TIMER" UnitFileState)
[[ "$UNIT_FILE_STATE" == enabled || "$UNIT_FILE_STATE" == enabled-runtime ]] || {
  echo "REFUSED: $TIMER is not enabled (state=$UNIT_FILE_STATE)" >&2
  rollback_loaded_job; exit 1
}

SERVICE_LOADED=$("$SYSTEMCTL" --user cat "$SERVICE" --no-pager 2>/dev/null) || {
  echo "REFUSED: cannot read loaded $SERVICE" >&2; rollback_loaded_job; exit 1;
}
TIMER_LOADED=$("$SYSTEMCTL" --user cat "$TIMER" --no-pager 2>/dev/null) || {
  echo "REFUSED: cannot read loaded $TIMER" >&2; rollback_loaded_job; exit 1;
}

READBACK=$(mktemp "${TMPDIR:-/tmp}/earshot-systemd-readback.XXXXXX")
cleanup_readback() { rm -f -- "$READBACK"; }
trap cleanup_readback EXIT
if ! SERVICE_EXPECTED="$SERVICE_BODY" TIMER_EXPECTED="$TIMER_BODY" \
SERVICE_LOADED="$SERVICE_LOADED" TIMER_LOADED="$TIMER_LOADED" \
python3 - "$READBACK" "$PLAN_EVENT_ID" "$PLAN_RUN_ID" "$PLAN_SHA256" \
  "$PLAN_HOST" "$LABEL" "$FIRE_AT_UTC" "$REPO" "$RUN_DIR" <<'PY'
import hashlib
import json
import os
import sys

out, event, run_id, plan_sha, host, label, fire, repo, run = sys.argv[1:]

def normalize(value):
    # systemctl cat prefixes fragment/drop-in paths with comments. Descriptor
    # content itself contains no comments, so removing comment/blank lines is
    # deterministic and makes any non-comment drop-in visible as a mismatch.
    return "\n".join(
        line.rstrip()
        for line in value.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )

service = normalize(os.environ["SERVICE_LOADED"])
timer = normalize(os.environ["TIMER_LOADED"])
if service != normalize(os.environ["SERVICE_EXPECTED"]):
    raise SystemExit("loaded service descriptor differs from exact RunPlan service")
if timer != normalize(os.environ["TIMER_EXPECTED"]):
    raise SystemExit("loaded timer descriptor differs from exact RunPlan timer")
descriptor_hash = hashlib.sha256(
    (service + "\0" + timer).encode("utf-8")
).hexdigest()
payload = {
    "schema": 1,
    "kind": "loaded-schedule-readback",
    "scheduler": "systemd-user",
    "loaded": True,
    "event_id": event,
    "run_id": run_id,
    "run_plan_sha256": plan_sha,
    "execution_host_id": host,
    "label": label,
    "fire_at_utc": fire,
    "program_arguments": [f"{repo}/scripts/event-autorun.sh", "--run-dir", run],
    "environment": {
        "EARSHOT_EXECUTION_HOST": host,
        "EARSHOT_REPO": repo,
        "EARSHOT_RUN_DIR": run,
        "EARSHOT_RUN_PLAN_SHA256": plan_sha,
    },
    "descriptor_sha256": descriptor_hash,
    "stop_timeout_seconds": 150,
    "kill_mode": "mixed",
}
with open(out, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2, sort_keys=True)
    fh.write("\n")
    fh.flush()
    os.fsync(fh.fileno())
PY
then
  echo "REFUSED: loaded systemd descriptor mismatch" >&2
  rollback_loaded_job
  exit 1
fi

PROOF_ARGS=(--run-dir "$RUN_DIR" --readback "$READBACK")
[[ "$VERIFY_ONLY" == 1 ]] && PROOF_ARGS+=(--verify-only)
if ! EARSHOT_REPO="$REPO" python3 scripts/schedule-proof.py "${PROOF_ARGS[@]}"; then
  echo "REFUSED: loaded job was not admitted as scheduled" >&2
  rollback_loaded_job
  exit 1
fi

echo "SCHEDULED: $PLAN_EVENT_ID/$PLAN_RUN_ID via $TIMER at $FIRE_AT_UTC (host=$PLAN_HOST)"
echo "  disarm: $SYSTEMCTL --user disable --now $TIMER"
