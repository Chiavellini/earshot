#!/usr/bin/env bash
# Schedule ONE phone event on the us-east-1 box, then prove the installed job is
# the job you meant.
#
#   scripts/box-event-install.sh --event-env configs/<ev>-phone-autorun.env \
#       --run-dir var/runs/<EVENT_ID>/<RUN_ID>
#       [--date YYYY-MM-DD] [--test] [--verify-only] [--disarm] [--no-digits]
#
# WHY THIS EXISTS. Three separate 2026-08-05 costs, all in the gap between
# "I edited the schedule" and "the schedule that will fire":
#
#   1. EDITING A UNIT FILE DOES NOT UPDATE THE LOADED JOB. On systemd that is
#      `daemon-reload` + `enable --now` (install-event-timer.sh does both); on
#      launchd it is bootout/bootstrap. A config carrying a raised cap plus a
#      stale job missing the matching posture flag makes EVERY order refuse, and
#      nothing says so until the call. The TLN go-live only caught it because it
#      re-read the loaded job and grepped for --fillable. That re-read is step 3
#      here, and it is not optional.
#   2. DIGITS NEVER REACH THE BOX BY DEPLOY. scripts/deploy-box.sh excludes
#      state/, so state/hunt/numbers.env — the gitignored store the tracked event
#      env reads at source time — simply is not there. An env that resolves to an
#      empty TO_NUMBER fails at fire time, not at prep time.
#   3. THE SCHEDULER'S PATH IS NOT YOURS. `systemd --user` on the box runs with
#      no ~/.cargo/bin (measured), so a fire-time compile is impossible and the
#      pinned binary from preflight is the only unattended-safe artifact.
#
# Digits are piped over stdin, never passed as arguments: an argv is visible in
# `ps` to every user on the host and lands in shell history.
#
# This script does NOT dial and does NOT arm. Before scheduling, it transfers
# the exact verified RunPlan + receipt prefix into a private box staging path,
# verifies it again against the box's pinned config/env, and publishes it only
# through scripts/run-host-boundary.py. The box owns every later append.
set -uo pipefail

REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO" || exit 2

EVENT_ENV="" RUN_DIR_RAW="" DATE="" TEST=0 VERIFY_ONLY=0 DISARM=0 PUSH_DIGITS=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --event-env)  EVENT_ENV="$2"; shift 2 ;;
    --run-dir)    RUN_DIR_RAW="$2"; shift 2 ;;
    --date)       DATE="$2"; shift 2 ;;
    --test)       TEST=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --disarm)     DISARM=1; shift ;;
    --no-digits)  PUSH_DIGITS=0; shift ;;
    -h|--help)    sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$EVENT_ENV" && -f "$EVENT_ENV" ]] || {
  echo "usage: $0 --event-env configs/<ev>-phone-autorun.env --run-dir var/runs/<EVENT_ID>/<RUN_ID> [--date YYYY-MM-DD] [--test] [--verify-only] [--disarm]" >&2
  exit 2
}

EARSHOT_INSTANCE_ID="${EARSHOT_INSTANCE_ID:-i-04c50d76572aa092e}"
if [[ -z "${EARSHOT_BOX:-}" ]] && command -v aws >/dev/null 2>&1; then
  _ip="$(aws ec2 describe-instances --instance-ids "$EARSHOT_INSTANCE_ID" \
           --query 'Reservations[].Instances[].PublicIpAddress' --output text 2>/dev/null || true)"
  [[ "$_ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]] && EARSHOT_BOX="ubuntu@$_ip"
fi
BOX="${EARSHOT_BOX:-ubuntu@3.212.174.94}"
KEY="${EARSHOT_BOX_KEY:-$HOME/.ssh/earshot-useast1.pem}"
BOX_REPO="${EARSHOT_BOX_REPO:-~/ear}"
SSH=(ssh -o ConnectTimeout=15 -o BatchMode=yes -i "$KEY" "$BOX")

# ── Exact typed run selection (read-only locally) ─────────────────────────
# Disarm keeps its legacy label-only form so an operator can stop a stale unit
# even if the corresponding laptop run tree is unavailable. Every install or
# verification, however, is schema-3-only and must name one canonical run.
if [[ "$DISARM" != 1 ]]; then
  [[ -n "$RUN_DIR_RAW" ]] || {
    echo "REFUSED: --run-dir is required; scheduling by mutable event env alone is no longer supported" >&2
    exit 2
  }
  RUN_DIR=$(python3 - "$REPO" "$RUN_DIR_RAW" <<'PY'
from pathlib import Path
import sys
repo = Path(sys.argv[1]).resolve()
raw = Path(sys.argv[2])
run = (raw if raw.is_absolute() else repo / raw).resolve()
try:
    run.relative_to(repo / "var" / "runs")
except ValueError:
    raise SystemExit("run directory is outside canonical var/runs")
print(run)
PY
  ) || { echo "REFUSED: unsafe --run-dir '$RUN_DIR_RAW'" >&2; exit 2; }
  EARSHOT_REPO="$REPO" python3 scripts/eventctl.py verify --run-dir "$RUN_DIR" || {
    echo "REFUSED: local RunPlan/ledger verification failed" >&2; exit 1;
  }
  PLAN_EXPORTS=$(python3 - "$REPO" "$RUN_DIR" "$EVENT_ENV" <<'PY'
import hashlib, json, shlex, subprocess, sys
from pathlib import Path
repo, run, event_env = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
plan = json.loads((run / "control/run-plan.json").read_text())
if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
    raise SystemExit("not a schema-3 RunPlan")
if plan.get("lane") != "phone":
    raise SystemExit(f"box-event-install is phone-only; plan lane={plan.get('lane')!r}")
if not plan.get("execution_enabled"):
    raise SystemExit("box-event-install refuses a capture-only plan")
want = (repo / plan["event_env_path"]).resolve() if plan.get("event_env_path") else None
if want != event_env.resolve():
    raise SystemExit(f"event env mismatch: plan pins {plan.get('event_env_path')!r}")
status = subprocess.run(
    [sys.executable, str(repo / "scripts/eventctl.py"), "status", "--run-dir", str(run), "--json"],
    cwd=repo, text=True, capture_output=True, check=True,
    env={**__import__('os').environ, "EARSHOT_REPO": str(repo)},
)
payload = json.loads(status.stdout)
reached = {x["transition"] for x in payload["state"]["reached"]}
if "preflight_passed" not in reached:
    raise SystemExit("RunPlan has not reached preflight_passed")
if not payload["state"]["active"]:
    raise SystemExit("RunPlan is terminal/archived")
values = {
    "PLAN_EVENT_ID": plan["event_id"],
    "PLAN_RUN_ID": plan["run_id"],
    "PLAN_SHA256": plan["plan_sha256"],
    "PLAN_HOST": plan["execution_host_id"],
    "PLAN_CONFIG_PATH": plan["config_path"],
    "PLAN_ENV_PATH": plan["event_env_path"],
}
for key, value in values.items():
    print(f"{key}={shlex.quote(str(value))}")
PY
  ) || { echo "REFUSED: --run-dir does not match this phone event/env" >&2; exit 1; }
  eval "$PLAN_EXPORTS"
fi

# ── Read the event env WITHOUT letting it leak into this shell ─────────────
# It contains command substitutions that read the digit store; evaluate them in
# a subshell and export only the scalars this script needs.
eval "$(
  set -a
  # shellcheck disable=SC1090
  source "$EVENT_ENV" >/dev/null 2>&1
  set +a
  for v in EVENT_NAME LABEL EVENT_DATE FIRE_HHMM BIND_PORT CONFIG_PATH ACCOUNT_ENV \
           EARSHOT_STREAM_URL DIAL_ATTEMPTS STREAM_CONFIRM_SECS OPERATOR_HANDLER; do
    printf 'EV_%s=%q\n' "$v" "${!v:-}"
  done
  # Presence only. The digits themselves must not cross into this shell, be
  # printed, or reach a log — this is the one fact we need about them.
  printf 'EV_HAS_NUMBER=%q\n' "$([[ -n "${TO_NUMBER:-}" && "${TO_NUMBER}" != *TBD* ]] && echo 1 || echo 0)"
  printf 'EV_HAS_PASSCODE=%q\n' "$([[ -n "${PASSCODE:-}" && "${PASSCODE}" != *TBD* ]] && echo 1 || echo 0)"
)"

[[ -n "$EV_LABEL" ]]     || { echo "no LABEL in $EVENT_ENV — install-event-timer.sh names both units after it" >&2; exit 2; }
[[ -n "$EV_FIRE_HHMM" ]] || { echo "no FIRE_HHMM in $EVENT_ENV" >&2; exit 2; }
DATE="${DATE:-$EV_EVENT_DATE}"
[[ "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "need a real --date (or EVENT_DATE in the env), got '$DATE'" >&2; exit 2; }

echo "event ${EV_EVENT_NAME:-?}  label $EV_LABEL  fire ${EV_FIRE_HHMM}Z on $DATE"
echo "config $EV_CONFIG_PATH  port ${EV_BIND_PORT:-?}  ingress ${EV_STREAM_URL_UNSET:-$EV_EARSHOT_STREAM_URL}"

# ── Disarm ─────────────────────────────────────────────────────────────────
if [[ "$DISARM" == 1 ]]; then
  "${SSH[@]}" "systemctl --user disable --now '$EV_LABEL.timer' 2>/dev/null; systemctl --user stop '$EV_LABEL.service' 2>/dev/null; systemctl --user list-timers --all --no-legend | grep -F '$EV_LABEL' || echo 'no timer left'"
  echo "disarmed $EV_LABEL"
  exit 0
fi

# ── Coherence checks the scheduler cannot make for itself ──────────────────
FAILS=0
fail() { echo "FAIL: $*" >&2; FAILS=$((FAILS+1)); }

if [[ -n "$EV_CONFIG_PATH" && -f "$EV_CONFIG_PATH" ]]; then
  CFG_PORT=$(sed -n 's/^[[:space:]]*bind[[:space:]]*=[[:space:]]*"[^"]*:\([0-9]*\)".*/\1/p' "$EV_CONFIG_PATH" | head -1)
  if ! grep -qE '^[[:space:]]*kind[[:space:]]*=[[:space:]]*"twilio' "$EV_CONFIG_PATH"; then
    fail "$EV_CONFIG_PATH has no twilio_stream source — this installer is for PHONE events only (webcast lanes use scripts/install-t0-agents.sh)"
  fi
  # The three ports that must agree or media silently never arrives: the
  # listener's own bind, the env's BIND_PORT (now honored by event-autorun.sh),
  # and the /e/<port> path Twilio was told to connect to.
  URL_PORT="${EV_EARSHOT_STREAM_URL##*/e/}"
  [[ -n "$CFG_PORT" && -n "$EV_BIND_PORT" && "$CFG_PORT" != "$EV_BIND_PORT" ]] \
    && fail "port disagreement: config binds $CFG_PORT, env BIND_PORT=$EV_BIND_PORT"
  [[ -n "$EV_EARSHOT_STREAM_URL" && "$URL_PORT" != "$CFG_PORT" ]] \
    && fail "port disagreement: EARSHOT_STREAM_URL ends /e/$URL_PORT, config binds $CFG_PORT"
  [[ "$EV_EARSHOT_STREAM_URL" == wss://* ]] || fail "EARSHOT_STREAM_URL is not a wss:// URL ('$EV_EARSHOT_STREAM_URL') — the box is tunnel-free; without it launch-event.sh falls back to ngrok"
  [[ "$EV_EARSHOT_STREAM_URL" == *ngrok* ]] && fail "EARSHOT_STREAM_URL points at ngrok — production telephony terminates TLS on the box (Q-TLS: p99 226 ms -> 20 ms, 61 stalls -> 0)"
else
  fail "CONFIG_PATH '$EV_CONFIG_PATH' not found"
fi

if [[ "$TEST" != 1 && "$EV_HAS_NUMBER" != 1 ]]; then
  fail "the event env resolves TO_NUMBER to TBD/empty — the digit store has no entry for this venue yet. Add it: state/hunt/numbers.env (KEY=+E164[,passcode#], mode 0600), or run scripts/hunt-dial-in.py --emit-numbers"
fi

(( FAILS )) && { echo; echo "REFUSED: $FAILS coherence failure(s) — nothing installed"; exit 1; }

# ── Transfer the exact RunPlan + receipt prefix ───────────────────────────
# A normal source deploy explicitly excludes var/runs. This is the only path
# that may publish a run on the execution host. rsync lands in a private
# namespace first; a checksum dry-run proves the source did not change during
# transfer, then the box's eventctl and run-host-boundary helper verify/publish.
REMOTE_REPO=""
REMOTE_RUN=""
if [[ "$VERIFY_ONLY" != 1 ]]; then
  REMOTE_REPO=$("${SSH[@]}" "cd ${BOX_REPO} && pwd -P") || {
    echo "REFUSED: cannot resolve the box repository" >&2; exit 1;
  }
  [[ "$REMOTE_REPO" == /* && "$REMOTE_REPO" != *[$'\t\r\n ']* ]] || {
    echo "REFUSED: box returned an unsafe repository path" >&2; exit 1;
  }
  STAGING=$("${SSH[@]}" "mkdir -p '$REMOTE_REPO/var/host-boundary-staging' && mktemp -d '$REMOTE_REPO/var/host-boundary-staging/dispatch.XXXXXX'") || {
    echo "REFUSED: cannot allocate private box run staging" >&2; exit 1;
  }
  [[ "$STAGING" == "$REMOTE_REPO"/var/host-boundary-staging/dispatch.* ]] || {
    echo "REFUSED: box returned an unsafe staging path '$STAGING'" >&2; exit 1;
  }
  STAGED_RUN="$STAGING/$PLAN_EVENT_ID/$PLAN_RUN_ID"
  REMOTE_RUN="$REMOTE_REPO/var/runs/$PLAN_EVENT_ID/$PLAN_RUN_ID"
  cleanup_staging() {
    "${SSH[@]}" "case '$STAGING' in '$REMOTE_REPO'/var/host-boundary-staging/dispatch.*) rm -rf -- '$STAGING';; esac" >/dev/null 2>&1 || true
  }
  trap cleanup_staging EXIT
  "${SSH[@]}" "mkdir -p '$STAGING/$PLAN_EVENT_ID'" || {
    echo "REFUSED: cannot create staged event directory" >&2; exit 1;
  }
  RSYNC_RSH="ssh -o ConnectTimeout=15 -o BatchMode=yes -i $KEY"
  rsync -a --delete -e "$RSYNC_RSH" "$RUN_DIR/" "$BOX:$STAGED_RUN/" || {
    echo "REFUSED: RunPlan prefix transfer failed" >&2; exit 1;
  }
  STABILITY=$(rsync -ani --checksum --delete -e "$RSYNC_RSH" \
    "$RUN_DIR/" "$BOX:$STAGED_RUN/") || {
      echo "REFUSED: RunPlan prefix stability check failed" >&2; exit 1;
    }
  [[ -z "$STABILITY" ]] || {
    printf '%s\n' "$STABILITY" >&2
    echo "REFUSED: RunPlan prefix changed during transfer" >&2
    exit 1
  }
  "${SSH[@]}" "cd '$REMOTE_REPO' && EARSHOT_REPO='$REMOTE_REPO' python3 scripts/eventctl.py verify --run-dir '$STAGED_RUN' && EARSHOT_REPO='$REMOTE_REPO' python3 scripts/run-host-boundary.py reconcile --repo '$REMOTE_REPO' --incoming '$STAGED_RUN' --destination '$REMOTE_RUN' --expected-host '$PLAN_HOST'" || {
    echo "REFUSED: box rejected the staged RunPlan prefix" >&2; exit 1;
  }
  cleanup_staging
  trap - EXIT
else
  REMOTE_REPO=$("${SSH[@]}" "cd ${BOX_REPO} && pwd -P") || {
    echo "REFUSED: cannot resolve the box repository" >&2; exit 1;
  }
  REMOTE_RUN="$REMOTE_REPO/var/runs/$PLAN_EVENT_ID/$PLAN_RUN_ID"
  "${SSH[@]}" "cd '$REMOTE_REPO' && EARSHOT_REPO='$REMOTE_REPO' python3 scripts/eventctl.py verify --run-dir '$REMOTE_RUN'" || {
    echo "REFUSED: installed box RunPlan/ledger is missing or invalid" >&2; exit 1;
  }
fi

# ── Push the digit store (stdin, 0600, never argv) ─────────────────────────
if [[ "$PUSH_DIGITS" == 1 && "$VERIFY_ONLY" != 1 ]]; then
  if [[ -f state/hunt/numbers.env ]]; then
    if "${SSH[@]}" "umask 077; mkdir -p ${BOX_REPO}/state/hunt && cat > ${BOX_REPO}/state/hunt/numbers.env && chmod 600 ${BOX_REPO}/state/hunt/numbers.env" < state/hunt/numbers.env; then
      N=$(grep -c '^[A-Z0-9]*=' state/hunt/numbers.env)
      echo "pushed state/hunt/numbers.env → $BOX ($N venue entries, mode 0600, over stdin)"
    else
      echo "FAIL: could not push the digit store to the box" >&2; exit 1
    fi
  else
    echo "WARN: no local state/hunt/numbers.env to push — the box env will resolve TO_NUMBER to TBD" >&2
  fi
fi

# ── Install ────────────────────────────────────────────────────────────────
if [[ "$VERIFY_ONLY" != 1 ]]; then
  if [[ "$TEST" == 1 ]]; then
    echo "REFUSED: --test is not a schema-3 scheduling posture. Compile a capture-only rehearsal RunPlan instead." >&2
    exit 2
  fi
  echo "installing $EV_LABEL on $BOX …"
  "${SSH[@]}" "cd '$REMOTE_REPO' && EARSHOT_REPO='$REMOTE_REPO' scripts/install-event-timer.sh --run-dir '$REMOTE_RUN' --expected-host '$PLAN_HOST'" || {
    echo "REFUSED: install-event-timer.sh failed on the box (lingering off? not Linux? env env missing?)" >&2
    exit 1
  }
else
  # Verification is delegated to the same loaded-readback boundary used at
  # install time. It performs no descriptor write/reload/enable operation and
  # requires the durable proof + `scheduled` receipt to already exist.
  "${SSH[@]}" "cd '$REMOTE_REPO' && EARSHOT_REPO='$REMOTE_REPO' scripts/install-event-timer.sh --run-dir '$REMOTE_RUN' --expected-host '$PLAN_HOST' --verify-only" || {
    echo "REFUSED: installed box job/proof does not match the exact RunPlan" >&2
    exit 1
  }
fi

# Re-read the box-owned ledger after installation. A green scheduler command
# without a persisted scheduled receipt is not an installed event.
"${SSH[@]}" "cd '$REMOTE_REPO' && EARSHOT_REPO='$REMOTE_REPO' python3 scripts/eventctl.py verify --run-dir '$REMOTE_RUN' && EARSHOT_REPO='$REMOTE_REPO' python3 scripts/eventctl.py status --run-dir '$REMOTE_RUN' --json" >/dev/null || {
  echo "REFUSED: box ledger failed post-install verification" >&2
  exit 1
}

echo "ARMED: $PLAN_EVENT_ID/$PLAN_RUN_ID at T-25 on $DATE, box $BOX"
echo "  ingress  $EV_EARSHOT_STREAM_URL"
echo "  dial     attempts=${EV_DIAL_ATTEMPTS:-launch-event default 3}, stream-confirm=${EV_STREAM_CONFIRM_SECS:-90}s"
echo "  disarm   scripts/box-event-install.sh --event-env $EVENT_ENV --disarm"
echo "  kill     ssh -i $KEY $BOX 'touch ${BOX_REPO}/state/<EVENT_ID>/STOP'"
"$REPO/scripts/notify.sh" "PHONE ARM SCHEDULED: ${EV_EVENT_NAME:-?}" \
  "$PLAN_EVENT_ID/$PLAN_RUN_ID fires at RunPlan T-25 on $BOX. Ingress $EV_EARSHOT_STREAM_URL. Verified against loaded unit + durable schedule proof." --quiet >/dev/null 2>&1 || true
exit 0
