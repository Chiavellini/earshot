#!/usr/bin/env bash
# UNATTENDED per-event supervisor (docs/history/RUNBOOK.md). Generalized from the
# MS-26JUL15 ms-autorun.sh (RUNBOOK follow-up 3): every event-specific value
# now comes from a per-event env file named by EARSHOT_EVENT_ENV (set in the
# event's launchd plist; see configs/*-autorun.env). Fired by launchd at
# fire time (≈ call − 25 min). Nobody is watching: this script owns launch,
# auto-redial (bridge IVRs drop silent callers after ~60 s pre-conference),
# DTMF-timing variants, post-run reporting, and email notification. Test mode:
#   EARSHOT_AUTORUN_TEST=1 EARSHOT_EVENT_ENV=configs/rehearsal-autorun.env \
#     scripts/event-autorun.sh                    # dry-run flags, 2 attempts
#
# Abort before it fires:  launchctl bootout gui/$(id -u)/<LABEL>
# Abort mid-run:          touch state/<EVENT_ID>/STOP
#                         (orders cancel+disarm; the loop then stops redialing)
set -u  # NOT -e: unattended — every failure must reach the report, not die silently

RUN_DIR_RAW="${EARSHOT_RUN_DIR:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir) RUN_DIR_RAW="${2:?}"; shift 2 ;;
    -h|--help) echo "usage: $0 --run-dir var/runs/<EVENT_ID>/<RUN_ID>"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# launchd runs with a minimal environment — set everything explicitly.
export PATH="/opt/homebrew/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="${HOME:-/Users/bernardodelrio}"
# Repo root: derive from this script's location; EARSHOT_REPO overrides.
# (Was hardcoded to the CDMX box's path — caught by the first systemd-fired
# VPS rehearsal, 2026-07-17: `cd /Users/...` on a Linux box, exit 1 in 6 ms.)
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Minimal mailer for failures BEFORE the env file is loaded (the full notify()
# below needs SUBJ_TAG/log; this one needs only ~/.kalshi/mail_env). A silent
# early exit on event morning is indistinguishable from a healthy idle box.
notify_min() {  # notify_min <subject> <body>
  (
    # shellcheck disable=SC1090
    source "$HOME/.kalshi/mail_env" 2>/dev/null || exit 1
    export MAIL_USER MAIL_APP_PASSWORD MAIL_TO
    MAIL_SUBJECT="$1" MAIL_BODY="$2" python3 - <<'PY'
import os, smtplib, ssl
from email.message import EmailMessage
msg = EmailMessage()
msg["From"] = os.environ["MAIL_USER"]
msg["To"] = os.environ["MAIL_TO"]
msg["Subject"] = os.environ["MAIL_SUBJECT"]
msg.set_content(os.environ["MAIL_BODY"])
with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
    s.login(os.environ["MAIL_USER"], os.environ["MAIL_APP_PASSWORD"])
    s.send_message(msg)
PY
  ) || true
}

# A fire-time production invocation is one exact immutable RunPlan. Keep the
# decision-only operator probe compatible with its isolated fixtures, but no
# scheduler/runtime path may select an event from mutable env or filename
# convention anymore.
TYPED_RUN=0
if [[ -n "$RUN_DIR_RAW" ]]; then
  RUN_DIR=$(python3 - "$REPO" "$RUN_DIR_RAW" <<'PY'
from pathlib import Path
import sys
repo = Path(sys.argv[1]).resolve()
run = Path(sys.argv[2]); run = (run if run.is_absolute() else repo / run).resolve()
try: rel = run.relative_to(repo / "var" / "runs")
except ValueError: raise SystemExit("run directory is outside canonical var/runs")
if len(rel.parts) != 2: raise SystemExit("run directory must be var/runs/<EVENT_ID>/<RUN_ID>")
print(run)
PY
  ) || { notify_min "EARSHOT autorun: NOT RUN (bad RunPlan path)" "Invalid --run-dir '$RUN_DIR_RAW'."; exit 2; }
  EARSHOT_REPO="$REPO" python3 "$REPO/scripts/eventctl.py" verify --run-dir "$RUN_DIR" >/dev/null || {
    notify_min "EARSHOT autorun: NOT RUN (RunPlan invalid)" "eventctl rejected $RUN_DIR. No launch attempted."
    exit 1
  }
  PLAN_EXPORTS=$(EARSHOT_REPO="$REPO" python3 - "$REPO" "$RUN_DIR" <<'PY'
import json, os, shlex, subprocess, sys
from pathlib import Path
repo, run = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
plan = json.loads((run / "control/run-plan.json").read_text())
if plan.get("schema") != 3 or plan.get("kind") != "run-plan" or plan.get("lane") != "phone":
    raise SystemExit("event-autorun requires a schema-3 phone RunPlan")
if plan.get("execution_enabled") is not True or not plan.get("event_env_path"):
    raise SystemExit("event-autorun requires an execution-enabled plan with event env")
status = subprocess.run(
    [sys.executable, str(repo / "scripts/eventctl.py"), "status", "--run-dir", str(run), "--json"],
    cwd=repo, env={**os.environ, "EARSHOT_REPO": str(repo)}, text=True,
    capture_output=True, check=True,
)
state = json.loads(status.stdout)["state"]
reached = {x["transition"] for x in state["reached"]}
if "scheduled" not in reached: raise SystemExit("RunPlan has no scheduled receipt")
if "launched" in reached: raise SystemExit("RunPlan already launched")
if not state["active"]: raise SystemExit("RunPlan is terminal/archived")
for key, value in {
    "PLAN_EVENT_ID": plan["event_id"],
    "PLAN_RUN_ID": plan["run_id"],
    "PLAN_SHA256": plan["plan_sha256"],
    "PLAN_HOST": plan["execution_host_id"],
    "PLAN_CONFIG": str((repo / plan["config_path"]).resolve()),
    "PLAN_EVENT_ENV": str((repo / plan["event_env_path"]).resolve()),
}.items(): print(f"{key}={shlex.quote(value)}")
PY
  ) || { notify_min "EARSHOT autorun: NOT RUN (RunPlan not schedulable)" "Typed run gate rejected $RUN_DIR. No launch attempted."; exit 1; }
  eval "$PLAN_EXPORTS"
  [[ -z "${EARSHOT_RUN_PLAN_SHA256:-}" || "$EARSHOT_RUN_PLAN_SHA256" == "$PLAN_SHA256" ]] || {
    echo "scheduler RunPlan hash does not match $RUN_DIR" >&2; exit 1;
  }
  [[ "${EARSHOT_EXECUTION_HOST:-}" == "$PLAN_HOST" ]] || {
    echo "scheduler execution host '${EARSHOT_EXECUTION_HOST:-unset}' != RunPlan '$PLAN_HOST'" >&2; exit 1;
  }
  TYPED_RUN=1
elif [[ "${EARSHOT_AUTORUN_DECIDE_ONLY:-0}" != 1 \
        && "${EARSHOT_AUTORUN_TEST:-0}" != 1 ]]; then
  notify_min "EARSHOT autorun: NOT RUN (no RunPlan)" \
    "event-autorun requires --run-dir var/runs/<EVENT>/<RUN>. Legacy mutable-env scheduling is disabled."
  echo "REFUSED: --run-dir is required" >&2
  exit 2
fi

# ── Per-event parameters (RUNBOOK follow-up 3) ────────────────────────────
if [[ "$TYPED_RUN" == 1 ]]; then
  EVENT_ENV="$PLAN_EVENT_ENV"
  if [[ -n "${EARSHOT_EVENT_ENV:-}" ]]; then
    SUPPLIED_EVENT_ENV=$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$EARSHOT_EVENT_ENV")
    [[ "$SUPPLIED_EVENT_ENV" == "$PLAN_EVENT_ENV" ]] || {
      echo "REFUSED: EARSHOT_EVENT_ENV differs from RunPlan event env" >&2; exit 1;
    }
  fi
else
  EVENT_ENV="${EARSHOT_EVENT_ENV:-}"
fi
if [[ -z "$EVENT_ENV" || ! -f "$REPO/$EVENT_ENV" && ! -f "$EVENT_ENV" ]]; then
  notify_min "EARSHOT autorun: NOT RUN (no event env)" \
    "event-autorun fired without a readable EARSHOT_EVENT_ENV ('$EVENT_ENV') at $(date). No launch attempted."
  echo "EARSHOT_EVENT_ENV missing or unreadable: '$EVENT_ENV'" >&2
  exit 1
fi
[[ -f "$EVENT_ENV" ]] || EVENT_ENV="$REPO/$EVENT_ENV"
# shellcheck disable=SC1090
source "$EVENT_ENV"
TEST="${EARSHOT_AUTORUN_TEST:-0}"
missing=""
for v in EVENT_NAME CONFIG_PATH TO_NUMBER EVENT_DATE \
         END_HHMM_LIVE COMPLETE_EARLIEST_HHMM ACCOUNT_ENV; do
  [[ -n "${!v:-}" ]] || missing="$missing $v"
done
# PASSCODE must be DEFINED, but may legitimately be EMPTY: an operator-screened
# bridge takes a reference spoken to a screener, not digits, so that venue class
# has no keyable credential at all. Requiring non-empty here refused to launch
# exactly the events the operator handler exists for. Unresolved is still caught
# — the env writes the literal TBD when the store has no line for the venue, and
# the placeholder guard below rejects it.
[[ -n "${PASSCODE+defined}" ]] || missing="$missing PASSCODE"
# TICKERS may be empty in TEST mode (strike-close fetch is skipped there).
[[ "$TEST" == 1 || -n "${TICKERS:-}" ]] || missing="$missing TICKERS"
TICKERS="${TICKERS:-}"
if [[ -n "$missing" ]]; then
  notify_min "EARSHOT autorun: NOT RUN (event env incomplete)" \
    "event-autorun: $EVENT_ENV lacks:$missing — exiting without launching."
  echo "event env $EVENT_ENV lacks:$missing" >&2
  exit 1
fi
# Placeholder guard: an env file staged before the dial-in was known must
# never reach a live dial (a 06:05 call to "TBD" would burn the window).
if [[ "$TEST" != 1 && ( "$TO_NUMBER" == *TBD* || "$PASSCODE" == *TBD* ) ]]; then
  notify_min "EARSHOT $EVENT_NAME run: NOT RUN (dial-in still TBD)" \
    "event-autorun: $EVENT_ENV has TO_NUMBER/PASSCODE placeholders — the number hunt never completed. Exiting without launching."
  echo "TO_NUMBER/PASSCODE still TBD in $EVENT_ENV" >&2
  exit 1
fi
# Listen-only invariant (docs/TWILIO.md): a passcode is digits/# only. Reject
# '*' (raise-hand/unmute) and any other char here — fail before dialing, so a
# bad env can never enter a Q&A queue on a live call. (twilio-dial.sh re-checks.)
if [[ -n "$PASSCODE" && ! "$PASSCODE" =~ ^[0-9#]+$ ]]; then
  notify_min "EARSHOT $EVENT_NAME run: NOT RUN (bad PASSCODE)" \
    "event-autorun: PASSCODE in $EVENT_ENV has a disallowed char (charset [0-9#]; '*' never permitted). Exiting without launching."
  echo "PASSCODE in $EVENT_ENV contains a disallowed character ('*' is never permitted)" >&2
  exit 1
fi
# Every email subject carries the mode — a rehearsal alert must be
# unmistakable from a live one (2026-07-15: an untagged TEST failure alert
# was read as a real event failure).
SUBJ_TAG=""; [[ "$TEST" == 1 ]] && SUBJ_TAG="[TEST] "
cd "$REPO" || exit 1

# Rehearsal knobs (TEST mode only — the live path is deliberately rigid):
# a dress rehearsal points at a different config (active markets, dry-run)
# and dials the TTS loopback, NEVER the real bridge. Its state (log, ledger,
# pids, STOP) then lives in that config's own state dir.
if [[ "$TEST" == 1 && -n "${EARSHOT_AUTORUN_TEST_CONFIG:-}" ]]; then
  CONFIG_PATH="$EARSHOT_AUTORUN_TEST_CONFIG"
fi
EVENT_ID=$(sed -n '/^\[event\]/,/^\[/s/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG_PATH" 2>/dev/null | head -1)
if [[ -z "$EVENT_ID" ]]; then
  notify_min "EARSHOT $EVENT_NAME run: NOT RUN (no event id)" \
    "event-autorun could not parse [event].id from $CONFIG_PATH — exiting without launching."
  echo "no parseable [event].id in $CONFIG_PATH" >&2
  exit 1
fi
if [[ "$TYPED_RUN" == 1 ]]; then
  CONFIG_REAL=$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$CONFIG_PATH")
  [[ "$EVENT_ID" == "$PLAN_EVENT_ID" && "$CONFIG_REAL" == "$PLAN_CONFIG" ]] || {
    notify_min "EARSHOT autorun: NOT RUN (env/RunPlan mismatch)" \
      "The pinned event env selected event/config '$EVENT_ID/$CONFIG_REAL', not '$PLAN_EVENT_ID/$PLAN_CONFIG'."
    echo "REFUSED: event env does not bind exact RunPlan config" >&2
    exit 1
  }
fi
STATE_DIR="$REPO/state/$EVENT_ID"
LOG="$STATE_DIR/autorun.log"
SUMMARY="$STATE_DIR/autorun-summary.txt"
PIDFILE="$STATE_DIR/autorun.pid"
mkdir -p "$STATE_DIR"
exec >> "$LOG" 2>&1
ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(ts)] $*"; }
"$REPO/scripts/journal.sh" --event "$EVENT_ID" --actor event-autorun.sh --action autorun-start \
  --outcome info --detail "mode=${MODE:-live} test=${TEST:-0}" --log "$LOG" || true

# Email via Gmail SMTP (app password) — best-effort, never fatal.
# One retry with a 30 s backoff (RUNBOOK follow-up 7): the observed failure
# is Gmail's send-rate throttle, which an immediate retry would re-hit.
notify() {  # notify <subject> <body-file-or-string> — subject is mode-tagged here
  local subject="${SUBJ_TAG}$1" body="$2" attempt
  [[ -f "$body" ]] && body=$(cat "$body")
  # The decision probe must not put mail on the wire: tests/operator_escalation.sh
  # exercises the escalate path, and that path pages the operator.
  if [[ "${EARSHOT_AUTORUN_DECIDE_ONLY:-0}" == 1 \
        || "$TEST" == 1 && "${EARSHOT_AUTORUN_TEST_NOTIFY:-0}" != 1 ]]; then
    log "notify SUPPRESSED (safe test mode): $subject"
    return 0
  fi
  for attempt in 1 2; do
    if (
      # shellcheck disable=SC1090
      source "$HOME/.kalshi/mail_env"
      export MAIL_USER MAIL_APP_PASSWORD MAIL_TO
      MAIL_SUBJECT="$subject" MAIL_BODY="$body" python3 - <<'PY'
import os, smtplib, ssl
from email.message import EmailMessage
msg = EmailMessage()
msg["From"] = os.environ["MAIL_USER"]
msg["To"] = os.environ["MAIL_TO"]
msg["Subject"] = os.environ["MAIL_SUBJECT"]
msg.set_content(os.environ["MAIL_BODY"])
with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
    s.login(os.environ["MAIL_USER"], os.environ["MAIL_APP_PASSWORD"])
    s.send_message(msg)
PY
    ); then
      return 0
    fi
    if [[ "$attempt" == 1 ]]; then
      log "notify attempt 1 failed — retrying in 30s: $subject"
      sleep 30
    fi
  done
  log "notify failed after 2 attempts (non-fatal): $subject"
}

# A recorded live child is evidence of an already-owned runtime, not debris.
# Never kill/restart it from the outer scheduler: that used to reset one-shot
# state, overwrite report.json, and cut off the executor's 15 s cancel/reconcile
# budget after only five seconds.  A dead pidfile is stale and may be removed;
# a live one is a loud collision that an operator must reconcile.
refuse_live_child() { # refuse_live_child <pidfile> <name>
  local pf="$1" name="$2" pid
  [[ -f "$pf" ]] || return 0
  pid=$(cat "$pf" 2>/dev/null || true)
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    log "REFUSING: recorded $name pid $pid is still alive; one event may own only one long-lived runtime"
    notify "EARSHOT $EVENT_NAME run: REFUSED (existing $name alive)" \
      "$name pid $pid from $pf is still alive. event-autorun will not kill or restart it: doing so loses finalizers and resets attempt-scoped evidence. Reconcile the existing run first."
    return 1
  fi
  rm -f "$pf"
}

# FAILED email if we die unexpectedly (best-effort).
finished=0
on_exit() {
  [[ "$finished" == 1 ]] && return
  log "EXIT trap: script terminated unexpectedly"
  notify "EARSHOT $EVENT_NAME run: FAILED (script died)" "event-autorun.sh exited unexpectedly at $(ts). Check $LOG"
}
trap on_exit EXIT

# ── Guards ────────────────────────────────────────────────────────────────
if [[ "$TEST" != 1 && "${EARSHOT_AUTORUN_DECIDE_ONLY:-0}" != 1 \
      && "$(date +%Y-%m-%d)" != "$EVENT_DATE" ]]; then
  log "date guard: today is $(date +%Y-%m-%d), not $EVENT_DATE — exiting"
  # Every early-exit guard emails — a silent no-op on event day looks
  # identical to a healthy idle box (audit F6/F8).
  notify "EARSHOT $EVENT_NAME run: NOT RUN (date guard)" "event-autorun fired on $(date +%Y-%m-%d), not the event date $EVENT_DATE — exiting without launching. If this WAS supposed to be event day, the schedule or date guard is wrong."
  finished=1; exit 0
fi
# Pidfile guard: PID-alive alone is not enough — after a crash the OS can
# recycle the PID onto an unrelated process, which would silently abort the
# real run (audit F13). Require the live PID to actually BE an event-autorun.
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null \
   && ps -p "$(cat "$PIDFILE")" -o command= 2>/dev/null | grep -q "event-autorun"; then
  log "already running (pid $(cat "$PIDFILE")) — exiting"
  notify "EARSHOT $EVENT_NAME run: SKIPPED (already running)" "event-autorun found a live instance (pid $(cat "$PIDFILE")) at $(ts) and exited. If no run is actually active, investigate $PIDFILE."
  finished=1; exit 0
fi
# The decision probe must never claim the pidfile or clear a STOP file — the
# global state/STOP is a kill switch for OTHER live arms on this host.
if [[ "${EARSHOT_AUTORUN_DECIDE_ONLY:-0}" != 1 ]]; then
  rm -f "$PIDFILE"  # stale (dead or recycled PID) — clear and proceed
  echo $$ > "$PIDFILE"
  rm -f "$STATE_DIR/STOP" "$REPO/state/STOP"
fi

# ── Mode ──────────────────────────────────────────────────────────────────
if [[ "$TEST" == 1 ]]; then
  LIVE_FLAGS="--allow-mention"          # dry-run: orders computed, never sent
  # test window: ~8 minutes from now (BSD vs GNU date syntax)
  if [[ "$(uname -s)" == Darwin ]]; then
    END_HHMM=$((10#$(date -v+8M "+%H%M")))
  else
    END_HHMM=$((10#$(date -d '+8 minutes' "+%H%M")))
  fi
  LONG_STREAM_S="${EARSHOT_AUTORUN_TEST_LONG_S:-99999}" # default: never "call complete" — forces the redial path
  MIN_FRAMES=1
  # Test mode places NO real call unless explicitly asked — the loop/report
  # mechanics don't need to ring anything (EARSHOT_AUTORUN_TEST_DIAL=1
  # restores the dial). A test dial additionally REQUIRES an explicit
  # EARSHOT_AUTORUN_TEST_TO target: a drill must never ring the real bridge.
  DIAL_FLAG="--no-dial"
  REQUIRE_AUDITABLE_AUDIO="${EARSHOT_AUTORUN_TEST_REQUIRE_AUDITABLE_AUDIO:-0}"
  if [[ ! "$REQUIRE_AUDITABLE_AUDIO" =~ ^[01]$ ]]; then
    log "EARSHOT_AUTORUN_TEST_REQUIRE_AUDITABLE_AUDIO must be 0 or 1"
    finished=1
    exit 2
  fi
  if [[ "${EARSHOT_AUTORUN_TEST_DIAL:-0}" == 1 ]]; then
    if [[ -n "${EARSHOT_AUTORUN_TEST_TO:-}" ]]; then
      DIAL_FLAG=""
      TO_NUMBER="$EARSHOT_AUTORUN_TEST_TO"
    else
      log "TEST_DIAL requested without EARSHOT_AUTORUN_TEST_TO — refusing to dial the real bridge from a drill; continuing with --no-dial"
    fi
  fi
  MODE="TEST/dry-run"
else
  LIVE_FLAGS="--i-am-live --allow-mention --fillable"
  END_HHMM="$END_HHMM_LIVE"             # stop redialing (call end + margin; env file)
  LONG_STREAM_S="${LONG_STREAM_S:-1200}" # a >20 min stream = the real call happened
  MIN_FRAMES="${MIN_FRAMES:-30000}"     # ≥10 min of actual AUDIO (50 frames/s) — wall
                                        # time alone can be a dead tunnel (audit F11)
  DIAL_FLAG=""
  REQUIRE_AUDITABLE_AUDIO=1
  MODE="LIVE mention-fill"
fi

# ── Per-event launch parameters (2026-08-05) ──────────────────────────────
# BIND_PORT was DECLARED in every configs/*-autorun.env that slate-stage.sh
# writes and then IGNORED here: the launch line hardcoded 8080. Two consequences,
# both silent. (1) An event whose config binds elsewhere got --bind-port 8080,
# so on the tunnel-free box the composed ingress path and the listener disagreed
# and no media arrived. (2) Two concurrent phone events both claimed 8080, and
# the loop's stale-port cleanup would kill the other event's listener. Honor the
# env; fall back to 8080 only when it is genuinely absent.
LAUNCH_PORT="${BIND_PORT:-8080}"
if [[ ! "$LAUNCH_PORT" =~ ^[0-9]+$ ]] || (( LAUNCH_PORT < 8080 )); then
  log "BIND_PORT='$LAUNCH_PORT' is not a port >= 8080 — falling back to 8080"
  LAUNCH_PORT=8080
fi
# A bridge that ANSWERS while unprovisioned and then hangs up looks SUCCESSFUL
# to Twilio (TLN 26AUG05: "the conference is not yet available", cut at 13.1 s),
# so a phone-only event wants more first-dial attempts and a wider stream-confirm
# window than launch-event.sh's defaults (3 / 90 s). Opt in from the event env;
# absent = unchanged behavior for every existing event.
LAUNCH_TUNING=()
[[ -n "${DIAL_ATTEMPTS:-}" ]]       && LAUNCH_TUNING+=(--dial-attempts "$DIAL_ATTEMPTS")
[[ -n "${STREAM_CONFIRM_SECS:-}" ]] && LAUNCH_TUNING+=(--stream-confirm-secs "$STREAM_CONFIRM_SECS")
# ── Operator escalation: known-first or unknown-on-first-drop ─────────────
# Until now OPERATOR_HANDLER was a static env var somebody had to remember to
# set, so the operator path could only ever be a REACTION to failure.
#
# HIMS 26AUG10 is what that costs. The venue was operator-screened; the loop
# dialled 17 legs across 31m47s cycling three DTMF variants of a credential that
# was never a passcode, each leg answered, streamed hold music, and got hung up
# by a screener waiting for a name. The operator handler — built, rehearsed, and
# already carrying this exact cell number for YUM and AMD — was engaged BY HAND
# at 21:16:50, 17 minutes into a live call. Admission then took 10 seconds.
#
# The verdict was sitting in configs/dial-ins.toml the whole time. Read it here,
# at fire time (T-15..T-25), so the page goes out BEFORE the call starts and
# admission completes pre-call. That is the entire point of the handler and it
# was never wired to the one fact that should trigger it.
#
# A screened venue is fail-closed: an env override cannot force the known-dead
# listen-only path. Both automatic and explicit FIRST-DIAL operator use require
# a usable config block plus the venue's durable `rehearsed-YYYY-MM-DD` registry
# pin. An admission-unknown venue cannot have that per-venue pin without first
# encountering its screener, so a separate run-scoped fallback is armed when
# the capability is usable: first dial stays plain, first early drop switches
# the replacement to the human-first wrapper. This never writes an admission
# verdict or receipt.
venue_field() {  # venue_field <key> <field> -> exact string value, or ""
  local key="$1" field="$2" reg="$REPO/configs/dial-ins.toml"
  [[ -n "$key" && -r "$reg" ]] || return 0
  sed -n "/^\[${key}\]/,/^\[/s/^${field}[[:space:]]*=[[:space:]]*\"\([^\"]*\)\".*/\1/p" \
    "$reg" | head -1
}
venue_bool() {  # venue_bool <key> <field> -> true/false, or ""
  local key="$1" field="$2" reg="$REPO/configs/dial-ins.toml"
  [[ -n "$key" && -r "$reg" ]] || return 0
  sed -n "/^\[${key}\]/,/^\[/s/^${field}[[:space:]]*=[[:space:]]*\([a-zA-Z]*\).*/\1/p" \
    "$reg" | head -1
}
# NOTE the character class instead of `\(true\|false\)`: BSD sed (the laptop)
# has no alternation in a basic regex, so the GNU-only form silently matches
# NOTHING on Darwin and this gate would read every config as "no handler".
config_has_operator_block() {
  local en cell prefix
  en=$(sed -n '/^\[operator_handler\]/,/^\[/s/^[[:space:]]*enabled[[:space:]]*=[[:space:]]*\([a-zA-Z]*\).*/\1/p' \
        "$CONFIG_PATH" 2>/dev/null | head -1)
  cell=$(sed -n '/^\[operator_handler\]/,/^\[/s/^[[:space:]]*cell_number[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$CONFIG_PATH" 2>/dev/null | head -1)
  prefix=$(sed -n '/^\[operator_handler\]/,/^\[/s/^[[:space:]]*conference_prefix[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$CONFIG_PATH" 2>/dev/null | head -1)
  [[ "$en" == "true" && "$cell" =~ ^\+[1-9][0-9]{7,14}$ \
     && ( -z "$prefix" || "$prefix" =~ ^[a-z0-9-]{1,24}$ ) \
     && -x "$REPO/scripts/operator-page.sh" \
     && -x "$REPO/scripts/twilio-operator-dial.sh" \
     && -x "$REPO/scripts/operator-redial.sh" ]]
}

# Never inherit a stale fallback from launchd/the invoking shell. This run's
# venue verdict and config capability below are the sole arming authority.
unset EARSHOT_FIRST_DROP_DIAL_SCRIPT EARSHOT_FIRST_DROP_OPERATOR_CONFIG
ADMISSION=$(venue_field "$EVENT_NAME" admission)
RESERVATION_WINDOW=$(venue_bool "$EVENT_NAME" reservation_window)
[[ "$RESERVATION_WINDOW" == "true" ]] || RESERVATION_WINDOW=""
HANDLER_REHEARSAL=$(venue_field "$EVENT_NAME" operator_handler)
HANDLER_REFUSAL=""
if [[ "$ADMISSION" == "operator_screened" ]]; then
  if [[ "${OPERATOR_HANDLER:-}" == "0" ]]; then
    HANDLER_REFUSAL="the event env tries to disable the handler for an operator_screened venue"
  elif ! config_has_operator_block; then
    HANDLER_REFUSAL="$CONFIG_PATH lacks a usable [operator_handler] enabled=true block with a valid cell"
  elif [[ ! "$HANDLER_REHEARSAL" =~ ^rehearsed-[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    HANDLER_REFUSAL="configs/dial-ins.toml lacks operator_handler=\"rehearsed-YYYY-MM-DD\" for $EVENT_NAME"
  else
    OPERATOR_HANDLER=1
    log "operator handler: AUTO-ESCALATED — $EVENT_NAME is admission=operator_screened and handler=$HANDLER_REHEARSAL; paging the cell BEFORE the first bridge dial"
    notify "EARSHOT $EVENT_NAME run: ANSWER YOUR PHONE (operator-screened venue)" \
      "$EVENT_NAME is recorded admission=operator_screened with $HANDLER_REHEARSAL, so event-autorun is taking the human-first path from the FIRST dial.

A pager leg is being placed to the cell in $CONFIG_PATH right now. ANSWER IT, handle the screener, then hang up — the bridge leg and the audio fork persist for the whole call.

After the operator admits the bridge, an explicit operator-pass assertion must mint admission.receipt.json from the current durable challenge + stream marker. Page answer, registry belief, and hangup do not open the decision gate.

After you hear the live issuer call, run:
  scripts/operator-page.sh --config '$CONFIG_PATH' --operator-i-am-paged --assert-admitted --state-dir '$STATE_DIR'

Event: $EVENT_ID
Log:   $LOG"
  fi
elif [[ "${OPERATOR_HANDLER:-0}" == "1" ]]; then
  if ! config_has_operator_block; then
    HANDLER_REFUSAL="OPERATOR_HANDLER=1 but $CONFIG_PATH has no usable handler block"
  elif [[ ! "$HANDLER_REHEARSAL" =~ ^rehearsed-[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    HANDLER_REFUSAL="OPERATOR_HANDLER=1 but the venue has no registry rehearsal receipt"
  else
    log "operator handler: explicitly enabled and verified by $HANDLER_REHEARSAL"
  fi
elif [[ -z "$ADMISSION" || "$ADMISSION" == "untested" || "$ADMISSION" == "unknown" ]]; then
  # Do not require a circular per-venue rehearsal here: the venue is unknown
  # precisely because no qualifying call has exposed its screen yet. The
  # handler topology/config double gate still applies, and a missing half is a
  # refusal before the initial leg rather than a silent screener redial later.
  if ! config_has_operator_block; then
    HANDLER_REFUSAL="admission is ${ADMISSION:-unknown}${RESERVATION_WINDOW:+ (reservation_window=$RESERVATION_WINDOW)} but $CONFIG_PATH lacks a usable [operator_handler] enabled=true capability"
  else
    FALLBACK_CONFIG="$CONFIG_PATH"
    [[ "$FALLBACK_CONFIG" == /* ]] || FALLBACK_CONFIG="$REPO/$FALLBACK_CONFIG"
    export EARSHOT_FIRST_DROP_DIAL_SCRIPT="$REPO/scripts/operator-redial.sh"
    export EARSHOT_FIRST_DROP_OPERATOR_CONFIG="$FALLBACK_CONFIG"
    log "first-drop operator fallback: ARMED — admission=${ADMISSION:-unknown}${RESERVATION_WINDOW:+ reservation_window=$RESERVATION_WINDOW}; initial dial remains plain, first qualifying early drop pages the configured cell and switches the replacement leg"
  fi
elif [[ -n "$ADMISSION" ]]; then
  log "operator handler: not engaged (venue $EVENT_NAME admission='$ADMISSION')"
fi
FIRST_DROP_FALLBACK_DECISION=0
[[ -n "${EARSHOT_FIRST_DROP_DIAL_SCRIPT:-}" ]] && FIRST_DROP_FALLBACK_DECISION=1
# Decision-only probe (tests/operator_escalation.sh): run the REAL lookups above
# against a real config + registry, print the verdict, and exit before anything
# dials, kills a process, or sends mail. Exists so the escalation rule is tested
# on the code that ships, not on a copy of its sed expressions.
if [[ "${EARSHOT_AUTORUN_DECIDE_ONLY:-0}" == 1 ]]; then
  echo "DECISION admission=${ADMISSION:-<none>} reservation_window=${RESERVATION_WINDOW:-false} handler_rehearsal=${HANDLER_REHEARSAL:-<none>} operator_handler=${OPERATOR_HANDLER:-0} first_drop_fallback=${FIRST_DROP_FALLBACK_DECISION} refusal=${HANDLER_REFUSAL:-<none>} test=${TEST}" >&2
  finished=1   # a clean probe exit is not a crash; keep the EXIT trap quiet
  [[ -z "$HANDLER_REFUSAL" ]] || exit 8
  exit 0
fi
if [[ -n "$HANDLER_REFUSAL" ]]; then
  log "REFUSING before dial: $HANDLER_REFUSAL"
  notify "EARSHOT $EVENT_NAME run: REFUSED (operator handler not ready)" \
    "$EVENT_NAME cannot be dialed unattended: $HANDLER_REFUSAL. No bridge or pager call was placed. Repair the configured handler capability and re-run preflight before launching."
  finished=1
  exit 8
fi
# Gated twice over — this flag AND [operator_handler] enabled=true in the config
# (launch-event.sh exits 2 without the second).
[[ "${OPERATOR_HANDLER:-0}" == 1 ]] && LAUNCH_TUNING+=(--operator-handler)

# Pinned binary from the night-before preflight (scripts/preflight-event.sh):
# run EXACTLY what was drilled, no fire-time compile (audit F7). Missing pin
# on a live run falls back to cargo — loudly, it should never happen.
PINNED_BIN="$STATE_DIR/earshot-pinned"
BIN_ARGS=()
if [[ -x "$PINNED_BIN" ]]; then
  BIN_ARGS=(--bin "$PINNED_BIN")
  log "using pinned binary: $PINNED_BIN ($(cat "$STATE_DIR/pinned.info" 2>/dev/null | tr '\n' ' '))"
elif [[ "$TEST" != 1 ]]; then
  log "REFUSING: no executable preflight pin at $PINNED_BIN"
  notify "EARSHOT $EVENT_NAME run: NOT RUN (no pinned binary)" \
    "The exact schema-3 run has no executable $PINNED_BIN. Fire-time compilation/fallback is disabled."
  finished=1
  exit 8
fi

# Platform seam (Darwin laptop vs Linux server — us-east-1 box, 2026-07-17).
# Darwin behavior is byte-identical to before; Linux fills the same slots.
power_status() {
  if [[ "$(uname -s)" == Darwin ]]; then
    pmset -g batt | tr '\n' ' '
  else
    echo "server (no battery); load: $(cat /proc/loadavg 2>/dev/null || echo '?')"
  fi
}

log "=== event-autorun start ($EVENT_NAME/$EVENT_ID, mode: $MODE, end window: $END_HHMM, one long-lived arm) ==="
log "power: $(power_status)"
notify "EARSHOT $EVENT_NAME run: LAUNCHED ($MODE)" "event-autorun started $(ts).
Event: $EVENT_ID
Mode: $MODE
Power: $(power_status)
Log: $LOG"

# ── One event, one runtime arm ──────────────────────────────────────────
# launch-event starts redial-supervisor behind the already-bound listener.
# Reconstructing launch-event after every phone drop used to reconstruct Rust,
# reset one-shot decisions, overwrite report.json, and kill evidence writers.
# Recovery is now exclusively a call-leg operation inside that one arm.
attempt=0
long_stream_seen=0
FINAL_RC=0
now=$((10#$(date +%H%M)))
if [[ "$now" -ge "$END_HHMM" ]]; then
  log "window already closed ($now >= $END_HHMM) — no runtime started"
  notify "EARSHOT $EVENT_NAME run: NOT RUN (window already closed)" \
    "event-autorun started at $(ts) but now=$now >= end=$END_HHMM. No runtime or phone leg was started."
else
  refuse_live_child "$STATE_DIR/arm.pid" "arm" || { finished=1; exit 9; }
  refuse_live_child "$STATE_DIR/ngrok.pid" "ngrok" || { finished=1; exit 9; }
  if lsof -ti :"$LAUNCH_PORT" >/dev/null 2>&1; then
    HOLDERS=$(lsof -ti :"$LAUNCH_PORT" | xargs ps -o pid=,command= -p 2>/dev/null | tr '\n' ';')
    log "REFUSING: port $LAUNCH_PORT is already held by $HOLDERS"
    notify "EARSHOT $EVENT_NAME run: REFUSED (port owned)" \
      "Port $LAUNCH_PORT is already held by: $HOLDERS. The scheduler will not kill an unknown/live owner."
    finished=1
    exit 9
  fi
  if [[ "$TYPED_RUN" == 1 ]]; then
    if ! EARSHOT_REPO="$REPO" EARSHOT_EXECUTION_HOST="$PLAN_HOST" \
      python3 "$REPO/scripts/capture-ready.py" phone \
        --run-dir "$RUN_DIR" --event-env "$EVENT_ENV" \
        --bind-port "$LAUNCH_PORT" --stream-url "${EARSHOT_STREAM_URL:-}" \
        --state-dir "$STATE_DIR" --binary "$PINNED_BIN"; then
      log "REFUSING: phone T0 capture proof failed; no listener/dial started"
      notify "EARSHOT $EVENT_NAME run: NOT RUN (capture not ready)" \
        "capture-ready.py refused exact run $PLAN_EVENT_ID/$PLAN_RUN_ID. No runtime or phone call was started."
      finished=1
      exit 8
    fi
  fi
  rm -f "$STATE_DIR/call.sid"

  attempt=1
  DTMF="$PASSCODE"
  log "── starting the event's single long-lived arm"
  t0=$(date +%s)
  attempt_log_start=$(wc -l < "$LOG")
  STREAM_ARGS=()
  [[ -n "${EARSHOT_STREAM_URL:-}" ]] && STREAM_ARGS=(--stream-url "$EARSHOT_STREAM_URL")
  LAUNCH_SCRIPT="$REPO/scripts/launch-event.sh"
  if [[ "$TEST" == 1 && -n "${EARSHOT_AUTORUN_TEST_LAUNCH_SCRIPT:-}" ]]; then
    LAUNCH_SCRIPT="$EARSHOT_AUTORUN_TEST_LAUNCH_SCRIPT"
  fi
  RUN_ARGS=()
  [[ "$TYPED_RUN" == 1 ]] && RUN_ARGS=(--run-dir "$RUN_DIR")
  "$LAUNCH_SCRIPT" \
    --config "$CONFIG_PATH" \
    --account "$ACCOUNT_ENV" \
    --bind-port "$LAUNCH_PORT" \
    --to "$TO_NUMBER" ${DTMF:+--dtmf "$DTMF"} \
    ${BIN_ARGS[@]+"${BIN_ARGS[@]}"} $DIAL_FLAG \
    ${STREAM_ARGS[@]+"${STREAM_ARGS[@]}"} \
    ${LAUNCH_TUNING[@]+"${LAUNCH_TUNING[@]}"} \
    ${RUN_ARGS[@]+"${RUN_ARGS[@]}"} \
    $LIVE_FLAGS &
  LAUNCH_PID=$!

  # systemd/launchd signal only this supervisor. It asserts STOP and signals
  # the launcher PID; launch-event owns its >=90 s cancellation/evidence/report
  # drain. Never signal the process group and never impose a shorter KILL here.
  SHUTDOWN_REQUESTED=0
  request_shutdown() {
    SHUTDOWN_REQUESTED=1
    log "TERM/INT received — requesting ordered launcher drain"
    touch "$STATE_DIR/STOP"
    kill -TERM "$LAUNCH_PID" 2>/dev/null || true
  }
  trap request_shutdown TERM INT

  # STOP is handed to launch-event, whose shutdown path hangs up the call and
  # gives the arm time to drain/cancel/finalize. This layer never SIGKILLs it.
  (
    while kill -0 "$LAUNCH_PID" 2>/dev/null; do
      if [[ -f "$STATE_DIR/STOP" ]]; then
        log "STOP detected — asking the one long-lived launch to drain"
        kill "$LAUNCH_PID" 2>/dev/null
        break
      fi
      sleep 2
    done
  ) &
  STOP_WATCHER_PID=$!

  rc=0
  while :; do
    wait "$LAUNCH_PID"; wait_rc=$?
    if kill -0 "$LAUNCH_PID" 2>/dev/null; then
      # wait was interrupted by TERM/INT; the launcher is still draining.
      continue
    fi
    rc=$wait_rc
    break
  done
  trap - TERM INT
  kill "$STOP_WATCHER_PID" 2>/dev/null || true
  wait "$STOP_WATCHER_PID" 2>/dev/null || true
  dur=$(( $(date +%s) - t0 ))
  frames=$(tail -n +"$((attempt_log_start+1))" "$LOG" \
    | sed $'s/\x1b\[[0-9;]*m//g' \
    | grep -o 'stream stopped frames=[0-9]*' | tail -1 | grep -o '[0-9]*$' || true)
  frames="${frames:-0}"
  [[ "$dur" -ge "$LONG_STREAM_S" && "$frames" -ge "$MIN_FRAMES" ]] && long_stream_seen=1
  log "long-lived arm ended: rc=$rc after ${dur}s, frames=${frames}"
  if [[ "$rc" -ne 0 ]]; then
    # launch-event owns lifecycle finalization and returns nonzero when the arm
    # or its required-evidence completion predicate fails. Preserve that exact
    # result for launchd/systemd after sending the human-readable summary.
    FINAL_RC="$rc"
    notify "EARSHOT $EVENT_NAME run: FAILED (single arm rc=$rc)" \
      "The event's only runtime exited rc=$rc after ${dur}s. It was not restarted, so durable decision/order/tape state was not overwritten. Recent log:
$(tail -20 "$LOG")"
  elif [[ "$frames" -lt "$MIN_FRAMES" && "$REQUIRE_AUDITABLE_AUDIO" == 1 ]]; then
    # A clean child status is insufficient when the detector-path capture is
    # below the explicit audit floor. Exit 10 is the supervisor's stable
    # "unauditable audio" terminal status (distinct from launcher failures).
    FINAL_RC=10
    notify "EARSHOT $EVENT_NAME run: UNAUDITABLE/NO AUDIO (${frames} frames)" \
      "The single arm exited cleanly but reported only ${frames} frames (< ${MIN_FRAMES}). No restart was attempted; inspect its evidence health before grading."
  fi
fi

# ── Post-run report ───────────────────────────────────────────────────────
log "building report"
STRIKES="$STATE_DIR/strike-close.json"
if [[ "$TEST" == 1 ]]; then
  log "test mode: skipping strike-close fetch (tickers are event-specific)"
else
python3 - "$STRIKES" <<PY || log "strike-close fetch failed (non-fatal)"
import json, sys, urllib.request
out = {}
for suf in "$TICKERS".split():
    t = f"$EVENT_ID-{suf}"
    try:
        with urllib.request.urlopen(f"https://api.elections.kalshi.com/trade-api/v2/markets/{t}", timeout=15) as r:
            m = json.load(r)["market"]
        out[t] = {"status": m.get("status"), "close_time": m.get("close_time"), "result": m.get("result", "")}
    except Exception as e:
        out[t] = {"error": str(e)}
json.dump(out, open(sys.argv[1], "w"), indent=1)
PY
fi

{
  echo "EARSHOT $EVENT_ID autonomous run — $(ts)  (mode: $MODE)"
  echo
  echo "attempts: $attempt   long-stream seen: $long_stream_seen   scheduler exit: $FINAL_RC"
  echo
  echo "── detections (decision intents) ──"
  grep "decision intent" "$LOG" | tail -30 || echo "(none)"
  echo
  echo "── orders / executor lines ──"
  grep -iE "order (acked|sent|placed)|LOST_RACE|refus" "$LOG" | tail -30 || echo "(none)"
  echo
  echo "── ledger ──"
  cat "$STATE_DIR/probe_ledger.json" 2>/dev/null || echo "(no ledger)"
  echo
  echo "── report.json (tail) ──"
  tail -40 "$STATE_DIR/report.json" 2>/dev/null || echo "(no report.json)"
  echo
  echo "── strike close times (resolver-race raw data) ──"
  cat "$STRIKES" 2>/dev/null || echo "(fetch failed)"
  echo
  echo "full log: $LOG"
} > "$SUMMARY"

notify "EARSHOT $EVENT_NAME run: SUMMARY ($MODE, $attempt attempts)" "$SUMMARY"
log "summary emailed; written to $SUMMARY"

# ── Done. The exact dated scheduler descriptor is already one-shot, and the
# immutable `launched` receipt refuses a duplicate even if an OS scheduler is
# manually prodded. Fire-time code never reloads/re-arms/stops its own unit. ──
rm -f "$PIDFILE"
finished=1
log "=== event-autorun done (scheduler exit $FINAL_RC) ==="
exit "$FINAL_RC"
