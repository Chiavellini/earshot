#!/usr/bin/env bash
# Install or verify one schema-3 phone/webcast RunPlan as one systemd USER timer.
#
#   scripts/install-event-timer.sh \
#     --run-dir var/runs/<EVENT_ID>/<RUN_ID> \
#     --expected-host earshot-useast1 [--fire-at-utc UTC]
#     [--verify-only] [--repo <dir>]
#
# This is deliberately not an event-env/date/HHMM template. The immutable
# RunPlan owns identity and starts_at; phone fires at T-25 and webcast at
# T-75. A reviewed webcast recovery may name one exact future fire override.
# A successful return means all of the following are true:
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

RUN_DIR_RAW="" EXPECTED_HOST="" REPO="" VERIFY_ONLY=0 FIRE_AT_UTC="" FIRE_OVERRIDE_SET=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir)       RUN_DIR_RAW="${2:?}"; shift 2 ;;
    --expected-host) EXPECTED_HOST="${2:?}"; shift 2 ;;
    --fire-at-utc)   FIRE_AT_UTC="${2:?}"; FIRE_OVERRIDE_SET=1; shift 2 ;;
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
  echo "usage: $0 --run-dir var/runs/<EVENT_ID>/<RUN_ID> --expected-host <stable-id> [--fire-at-utc UTC] [--verify-only] [--repo <dir>]" >&2
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
PLAN_EXPORTS=$(python3 - "$REPO" "$RUN_DIR" "$EXPECTED_HOST" "$FIRE_AT_UTC" <<'PY'
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

repo, run, expected_host, fire_override = (
    Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
)
plan = json.loads((run / "control/run-plan.json").read_text())
if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
    raise SystemExit("schema-3 RunPlan required")
lane = plan.get("lane")
if lane not in {"phone", "webcast"}:
    raise SystemExit(f"systemd scheduler received unsupported lane={lane!r}")
if plan.get("execution_enabled") is not True:
    raise SystemExit("systemd scheduler requires an execution-enabled plan")
if lane == "webcast" and plan.get("capture_only") is not False:
    raise SystemExit("webcast scheduler requires an execution-enabled RunPlan")
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
if fire_override:
    if lane != "webcast":
        raise SystemExit("--fire-at-utc override is webcast-only")
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
    fire = start - dt.timedelta(minutes=25 if lane == "phone" else 75)

argv = [str(repo / "scripts" / "event-autorun.sh"), "--run-dir", str(run)]
environment = {
    "EARSHOT_EXECUTION_HOST": plan["execution_host_id"],
    "EARSHOT_REPO": str(repo),
    "EARSHOT_RUN_DIR": str(run),
    "EARSHOT_RUN_PLAN_SHA256": plan["plan_sha256"],
}
account = ""
posture = "n/a"
if lane == "webcast":
    # O6: posture comes from the plan's PINNED config through the one shared
    # derivation in schedule-proof.py, which re-verifies the config bytes
    # against plan.config_sha256. Derived ONCE here and exported, because this
    # script writes the unit from `argv` while the ExecStart= line below is
    # built separately in bash — deriving it twice is how those two drift.
    _posture = subprocess.run(
        [sys.executable, str(repo / "scripts/schedule-proof.py"), "posture",
         "--run-dir", str(run)],
        cwd=repo, text=True, capture_output=True,
        env={**os.environ, "EARSHOT_REPO": str(repo)},
    )
    if _posture.returncode != 0:
        raise SystemExit(
            "could not derive the scheduled posture: "
            + (_posture.stderr.strip() or "unknown error")
        )
    posture = _posture.stdout.strip()
    argv = [str(repo / "scripts" / "arm-on-url.sh"), "--run-dir", str(run)]
    if posture == "live":
        argv += ["--i-am-live", "--fillable"]
    elif posture != "rehearsal":
        raise SystemExit(f"unknown scheduled posture {posture!r}")
    env_rel = plan.get("event_env_path")
    if not env_rel:
        raise SystemExit("webcast RunPlan has no pinned event env/account locator")
    env_path = (repo / env_rel).resolve()
    if not env_path.is_file() or env_path.is_symlink():
        raise SystemExit(f"webcast event env is not a regular file: {env_path}")
    match = re.search(
        r'^\s*(?:export\s+)?(?:EARSHOT_ACCOUNT_PROFILE|ACCOUNT_ENV)=["\x27]([^"\x27]+)["\x27]',
        env_path.read_text(errors="replace"), re.M,
    )
    if not match:
        raise SystemExit("webcast event env has no literal account profile locator")
    raw_account = match.group(1)
    home = str(Path.home())
    raw_account = raw_account.replace("${HOME}", home).replace("$HOME", home)
    if raw_account.startswith("~/"):
        raw_account = home + raw_account[1:]
    account_path = Path(raw_account)
    if not account_path.is_absolute():
        raise SystemExit("webcast account profile locator must resolve to an absolute path")
    account_path = account_path.resolve()
    if not account_path.is_file() or account_path.is_symlink():
        raise SystemExit(f"webcast account profile is not a regular file: {account_path}")
    account = str(account_path)
    if unsafe.search(account):
        raise SystemExit("account profile path contains characters unsupported by the unit boundary")
    environment["EARSHOT_ACCOUNT_ENV"] = account
values = {
    "PLAN_LANE": lane,
    "PLAN_EVENT_ID": plan["event_id"],
    "PLAN_RUN_ID": plan["run_id"],
    "PLAN_SHA256": plan["plan_sha256"],
    "PLAN_HOST": plan["execution_host_id"],
    "LABEL": f"com.earshot.run.{plan['plan_sha256'][:20]}",
    "FIRE_AT_UTC": fire.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "ON_CALENDAR": fire.strftime("%Y-%m-%d %H:%M:%S UTC"),
    "ACCOUNT_ENV": account,
    "PLAN_POSTURE": posture,
    "PROGRAM_JSON": json.dumps(argv, separators=(",", ":")),
    "ENVIRONMENT_JSON": json.dumps(environment, separators=(",", ":"), sort_keys=True),
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
if [[ "$PLAN_LANE" == webcast ]]; then
  # O6: PLAN_POSTURE was derived ONCE in the block above, from the plan's
  # pinned config. Do not re-derive it here — this line and the argv the unit
  # is actually written from must never be able to disagree.
  case "$PLAN_POSTURE" in
    live)      EXEC_START="$REPO/scripts/arm-on-url.sh --run-dir $RUN_DIR --i-am-live --fillable" ;;
    rehearsal) EXEC_START="$REPO/scripts/arm-on-url.sh --run-dir $RUN_DIR" ;;
    *) echo "REFUSED: unknown scheduled posture '$PLAN_POSTURE'" >&2; exit 1 ;;
  esac
else
  EXEC_START="$REPO/scripts/event-autorun.sh --run-dir $RUN_DIR"
fi
SERVICE_ENVIRONMENT="Environment=EARSHOT_REPO=$REPO
Environment=EARSHOT_RUN_DIR=$RUN_DIR
Environment=EARSHOT_RUN_PLAN_SHA256=$PLAN_SHA256
Environment=EARSHOT_EXECUTION_HOST=$PLAN_HOST"
if [[ "$PLAN_LANE" == webcast ]]; then
  SERVICE_ENVIRONMENT="$SERVICE_ENVIRONMENT
Environment=EARSHOT_ACCOUNT_ENV=$ACCOUNT_ENV"
fi
SERVICE_BODY="[Unit]
Description=EARSHOT schema-3 $PLAN_LANE run $PLAN_EVENT_ID/$PLAN_RUN_ID

[Service]
Type=oneshot
WorkingDirectory=$REPO
$SERVICE_ENVIRONMENT
ExecStart=$EXEC_START
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
TIMER_LIST=$("$SYSTEMCTL" --user list-timers "$TIMER" --all --no-legend --no-pager 2>/dev/null) || {
  echo "REFUSED: $TIMER is absent from the loaded timer list" >&2
  rollback_loaded_job; exit 1
}
TIMER_LIST="$TIMER_LIST" python3 - "$TIMER" <<'PY' || {
import os
import sys
timer = sys.argv[1]
if timer not in {field for line in os.environ["TIMER_LIST"].splitlines() for field in line.split()}:
    raise SystemExit(1)
PY
  echo "REFUSED: loaded timer list does not name exact unit $TIMER" >&2
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
PROGRAM_JSON="$PROGRAM_JSON" ENVIRONMENT_JSON="$ENVIRONMENT_JSON" \
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
program = json.loads(os.environ["PROGRAM_JSON"])
environment = json.loads(os.environ["ENVIRONMENT_JSON"])
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
    "program_arguments": program,
    "environment": environment,
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
[[ "$FIRE_OVERRIDE_SET" == 1 ]] && PROOF_ARGS+=(--fire-at-utc "$FIRE_AT_UTC")
[[ "$VERIFY_ONLY" == 1 ]] && PROOF_ARGS+=(--verify-only)
if ! EARSHOT_REPO="$REPO" python3 scripts/schedule-proof.py "${PROOF_ARGS[@]}"; then
  echo "REFUSED: loaded job was not admitted as scheduled" >&2
  rollback_loaded_job
  exit 1
fi

echo "SCHEDULED: $PLAN_EVENT_ID/$PLAN_RUN_ID via $TIMER at $FIRE_AT_UTC (host=$PLAN_HOST)"
echo "  disarm: $SYSTEMCTL --user disable --now $TIMER"
