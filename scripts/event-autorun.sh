#!/usr/bin/env bash
# UNATTENDED per-event supervisor (docs/RUNBOOK.md). Generalized from the
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

# ── Per-event parameters (RUNBOOK follow-up 3) ────────────────────────────
EVENT_ENV="${EARSHOT_EVENT_ENV:-}"
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
for v in EVENT_NAME CONFIG_PATH TO_NUMBER PASSCODE LABEL EVENT_DATE \
         END_HHMM_LIVE COMPLETE_EARLIEST_HHMM ACCOUNT_ENV; do
  [[ -n "${!v:-}" ]] || missing="$missing $v"
done
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
STATE_DIR="$REPO/state/$EVENT_ID"
LOG="$STATE_DIR/autorun.log"
SUMMARY="$STATE_DIR/autorun-summary.txt"
PIDFILE="$STATE_DIR/autorun.pid"
mkdir -p "$STATE_DIR"
exec >> "$LOG" 2>&1
ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(ts)] $*"; }

# Email via Gmail SMTP (app password) — best-effort, never fatal.
# One retry with a 30 s backoff (RUNBOOK follow-up 7): the observed failure
# is Gmail's send-rate throttle, which an immediate retry would re-hit.
notify() {  # notify <subject> <body-file-or-string> — subject is mode-tagged here
  local subject="${SUBJ_TAG}$1" body="$2" attempt
  [[ -f "$body" ]] && body=$(cat "$body")
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

# kill_recorded <pidfile> <name>: TERM the recorded PID, wait up to 5 s,
# SIGKILL if it survives (a wedged process must never block the next attempt).
kill_recorded() {
  local pf="$1" name="$2" pid
  [[ -f "$pf" ]] || return 0
  pid=$(cat "$pf" 2>/dev/null)
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null
    for _ in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      log "$name (pid $pid) survived SIGTERM — sending SIGKILL"
      kill -9 "$pid" 2>/dev/null
    fi
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
if [[ "$TEST" != 1 && "$(date +%Y-%m-%d)" != "$EVENT_DATE" ]]; then
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
rm -f "$PIDFILE"  # stale (dead or recycled PID) — clear and proceed
echo $$ > "$PIDFILE"
rm -f "$STATE_DIR/STOP" "$REPO/state/STOP"

# ── Mode ──────────────────────────────────────────────────────────────────
if [[ "$TEST" == 1 ]]; then
  LIVE_FLAGS="--allow-mention"          # dry-run: orders computed, never sent
  MAX_ATTEMPTS=2
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
  MAX_ATTEMPTS="${MAX_ATTEMPTS:-25}"
  END_HHMM="$END_HHMM_LIVE"             # stop redialing (call end + margin; env file)
  LONG_STREAM_S="${LONG_STREAM_S:-1200}" # a >20 min stream = the real call happened
  MIN_FRAMES="${MIN_FRAMES:-30000}"     # ≥10 min of actual AUDIO (50 frames/s) — wall
                                        # time alone can be a dead tunnel (audit F11)
  DIAL_FLAG=""
  MODE="LIVE mention-fill"
fi

# Pinned binary from the night-before preflight (scripts/preflight-event.sh):
# run EXACTLY what was drilled, no fire-time compile (audit F7). Missing pin
# on a live run falls back to cargo — loudly, it should never happen.
PINNED_BIN="$STATE_DIR/earshot-pinned"
BIN_ARGS=()
if [[ -x "$PINNED_BIN" ]]; then
  BIN_ARGS=(--bin "$PINNED_BIN")
  log "using pinned binary: $PINNED_BIN ($(cat "$STATE_DIR/pinned.info" 2>/dev/null | tr '\n' ' '))"
elif [[ "$TEST" != 1 ]]; then
  log "NO pinned binary at $PINNED_BIN — falling back to cargo run (preflight was skipped?)"
  notify "EARSHOT $EVENT_NAME run: WARNING (no pinned binary)" "Live run is falling back to 'cargo run' from the working tree — scripts/preflight-event.sh was not run. Launch continues, but this is the unpinned path the runbook forbids."
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

log "=== event-autorun start ($EVENT_NAME/$EVENT_ID, mode: $MODE, end window: $END_HHMM, max attempts: $MAX_ATTEMPTS) ==="
log "power: $(power_status)"
notify "EARSHOT $EVENT_NAME run: LAUNCHED ($MODE)" "event-autorun started $(ts).
Event: $EVENT_ID
Mode: $MODE
Power: $(power_status)
Log: $LOG"

# ── Attempt loop ──────────────────────────────────────────────────────────
# DTMF variants cycle across attempts to cover unknown prompt timing
# (twilio-dial.sh prefixes 8x'w' = 4 s to every variant):
DTMF_1="$PASSCODE"                     # 4 s wait, passcode
DTMF_2="wwwwwwww$PASSCODE"             # 8 s wait
DTMF_3="${PASSCODE}wwwwwwww${PASSCODE}" # send twice (early + re-prompt)
attempt=0
long_stream_seen=0
consec_failures=0
while :; do
  now=$((10#$(date +%H%M)))
  if [[ "$now" -ge "$END_HHMM" ]]; then
    log "window closed ($now >= $END_HHMM) — stop"
    if [[ "$attempt" -eq 0 ]]; then
      # Fired late (box asleep at 06:05?) and the window is already gone —
      # without this email the day would end in total silence (audit F8).
      notify "EARSHOT $EVENT_NAME run: NOT RUN (window already closed)" "event-autorun started at $(ts) but now=$now >= end=$END_HHMM — ZERO attempts were made. The launchd job likely fired late (machine asleep at schedule time?)."
    fi
    break
  fi
  if [[ -f "$STATE_DIR/STOP" ]]; then log "STOP file present — stop redialing"; break; fi
  if [[ "$attempt" -ge "$MAX_ATTEMPTS" ]]; then log "max attempts reached — stop"; break; fi
  attempt=$((attempt+1))
  case $((attempt % 3)) in
    1) DTMF="$DTMF_1" ;;
    2) DTMF="$DTMF_2" ;;
    0) DTMF="$DTMF_3" ;;
  esac

  # Clean slate: no stale tunnel/arm from a previous attempt. Kill ONLY the
  # PIDs the launch chain recorded (a bare kill-whatever-holds-:8080 can hit
  # unrelated processes — audit F9), TERM first, KILL if it survives.
  kill_recorded "$STATE_DIR/arm.pid" "arm"
  kill_recorded "$STATE_DIR/ngrok.pid" "ngrok"
  # Last resort if something unrecorded still wedges the port (an attempt
  # that died before writing pidfiles): log WHAT we kill, then escalate.
  if lsof -ti :8080 >/dev/null 2>&1; then
    log "port 8080 still held by: $(lsof -ti :8080 | xargs ps -o pid=,command= -p 2>/dev/null | tr '\n' ';')"
    lsof -ti :8080 2>/dev/null | xargs kill 2>/dev/null
    sleep 3
    lsof -ti :8080 2>/dev/null | xargs kill -9 2>/dev/null
  fi
  rm -f "$STATE_DIR/call.sid"
  sleep 2

  log "── attempt $attempt (dtmf variant: $DTMF)"
  t0=$(date +%s)
  attempt_log_start=$(wc -l < "$LOG")
  # Tunnel-free hosts (Linux/VPS) set EARSHOT_STREAM_URL in the event env;
  # empty on Darwin/CDMX → launch-event runs its unchanged ngrok path.
  STREAM_ARGS=()
  [[ -n "${EARSHOT_STREAM_URL:-}" ]] && STREAM_ARGS=(--stream-url "$EARSHOT_STREAM_URL")
  scripts/launch-event.sh \
    --config "$CONFIG_PATH" \
    --account "$ACCOUNT_ENV" \
    --bind-port 8080 \
    --to "$TO_NUMBER" --dtmf "$DTMF" \
    ${BIN_ARGS[@]+"${BIN_ARGS[@]}"} $DIAL_FLAG \
    ${STREAM_ARGS[@]+"${STREAM_ARGS[@]}"} \
    $LIVE_FLAGS &
  LAUNCH_PID=$!

  # STOP watcher DURING the attempt: the loop-top check only ran between
  # attempts, so a mid-call STOP neither stopped the attempt nor hung up the
  # Twilio leg — the header's promise was half-true (audit F12). This
  # watcher hangs up the recorded CallSid (per-event creds) and SIGTERMs the
  # launcher; its trap SIGTERMs arm, whose kill watcher cancels orders,
  # flushes the ledger, and exits.
  (
    while kill -0 "$LAUNCH_PID" 2>/dev/null; do
      if [[ -f "$STATE_DIR/STOP" ]]; then
        log "STOP detected mid-attempt — hanging up call leg + stopping attempt"
        if [[ -s "$STATE_DIR/call.sid" ]]; then
          # shellcheck disable=SC1090
          (source "$ACCOUNT_ENV" 2>/dev/null; scripts/twilio-hangup.sh "$(cat "$STATE_DIR/call.sid")") \
            || log "hangup failed (non-fatal): call may already be down"
        fi
        kill "$LAUNCH_PID" 2>/dev/null
        break
      fi
      sleep 2
    done
  ) &
  STOP_WATCHER_PID=$!

  wait "$LAUNCH_PID"
  rc=$?
  kill "$STOP_WATCHER_PID" 2>/dev/null
  wait "$STOP_WATCHER_PID" 2>/dev/null
  dur=$(( $(date +%s) - t0 ))
  log "attempt $attempt ended: rc=$rc after ${dur}s"

  # Mid-window alerting (audit F6): the operator must hear about systemic
  # failure WHILE the window is open, not from the post-window summary.
  if [[ "$rc" -ne 0 && "$dur" -lt "$LONG_STREAM_S" ]]; then
    consec_failures=$((consec_failures+1))
    if [[ "$consec_failures" -eq 1 || $((consec_failures % 5)) -eq 0 ]]; then
      notify "EARSHOT $EVENT_NAME run: attempt $attempt FAILED (rc=$rc, ${consec_failures} consecutive)" \
        "Mode: $MODE
attempt $attempt exited rc=$rc after ${dur}s at $(ts). Redialing continues until $END_HHMM. Recent log:
$(tail -15 "$LOG")"
    fi
  else
    consec_failures=0
  fi

  if [[ "$dur" -ge "$LONG_STREAM_S" ]]; then
    # Wall time is NOT evidence of audio: a call leg parked on <Pause> with a
    # dead tunnel runs 90 min and captures nothing (audit F11). Require the
    # arm process to have reported real frames for this attempt.
    frames=$(tail -n +"$((attempt_log_start+1))" "$LOG" \
      | sed $'s/\x1b\[[0-9;]*m//g' \
      | grep -o 'stream stopped frames=[0-9]*' | tail -1 | grep -o '[0-9]*$' || true)
    frames="${frames:-0}"
    if [[ "$frames" -ge "$MIN_FRAMES" ]]; then
      long_stream_seen=1
      now=$((10#$(date +%H%M)))
      if [[ "$TEST" == 1 || "$now" -ge "$COMPLETE_EARLIEST_HHMM" ]]; then
        log "long stream (${dur}s, ${frames} frames) ended in the end-window — treating as call complete"
        break
      fi
      log "long stream ended early (${dur}s at $now, ${frames} frames) — call may have dropped; redialing"
    else
      log "long attempt (${dur}s) but only ${frames} frames of audio — dead tunnel/stream? NOT treating as call complete; redialing"
      notify "EARSHOT $EVENT_NAME run: attempt $attempt had NO AUDIO (${dur}s, ${frames} frames)" \
        "Mode: $MODE
The call leg ran ${dur}s but the pipeline saw ${frames} frames (< ${MIN_FRAMES}). Likely a dead tunnel or stream. Redialing."
    fi
  fi
  sleep 20
done

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
  echo "attempts: $attempt   long-stream seen: $long_stream_seen"
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

# ── Done — mark success BEFORE self-disarm: booting out our own label makes
# launchd SIGTERM this script, so nothing may follow the bootout. ──────────
rm -f "$PIDFILE"
finished=1
log "=== event-autorun done ==="

# Self-disarm (live only): never fire again. This kills us — final act.
# Darwin: launchd bootout (SIGTERMs us). Linux: disable+stop our systemd user
# timer AND service (scripts/install-event-timer.sh names both after $LABEL);
# stopping the service also SIGTERMs us, so it stays the final act on both.
if [[ "$TEST" != 1 ]]; then
  trap - EXIT
  if [[ "$(uname -s)" == Darwin ]]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  else
    systemctl --user disable --now "$LABEL.timer" 2>/dev/null || true
    systemctl --user stop "$LABEL.service" 2>/dev/null || true
  fi
fi
