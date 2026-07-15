#!/usr/bin/env bash
# UNATTENDED supervisor for the MS-26JUL15 live run (docs/RUNBOOK.md).
# Fired by launchd (com.earshot.ms-autorun) at 06:05 local on 2026-07-15.
# Nobody is watching: this script owns launch, auto-redial (the bridge IVR
# drops silent callers after ~60 s pre-conference), DTMF-timing variants,
# post-run reporting, and email notification. Test mode:
#   EARSHOT_AUTORUN_TEST=1 scripts/ms-autorun.sh   # dry-run flags, 2 attempts
#
# Abort before it fires:  launchctl bootout gui/$(id -u)/com.earshot.ms-autorun
# Abort mid-run:          touch state/KXEARNINGSMENTIONMS-26JUL15/STOP
#                         (orders cancel+disarm; the loop then stops redialing)
set -u  # NOT -e: unattended — every failure must reach the report, not die silently

# launchd runs with a minimal environment — set everything explicitly.
export PATH="/opt/homebrew/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="${HOME:-/Users/bernardodelrio}"
REPO="/Users/bernardodelrio/Code/ear"
EVENT_ID="KXEARNINGSMENTIONMS-26JUL15"
STATE_DIR="$REPO/state/$EVENT_ID"
LOG="$STATE_DIR/autorun.log"
SUMMARY="$STATE_DIR/autorun-summary.txt"
PIDFILE="$STATE_DIR/autorun.pid"
LABEL="com.earshot.ms-autorun"
TICKERS="BASE BUYB GALA HEAD INVE MIDD OIL PRIV PRIVC RECE TAIL TARI TOKE ZERO"

TEST="${EARSHOT_AUTORUN_TEST:-0}"
# Every email subject carries the mode — a rehearsal alert must be
# unmistakable from a live one (2026-07-15: an untagged TEST failure alert
# was read as a real event failure).
SUBJ_TAG=""; [[ "$TEST" == 1 ]] && SUBJ_TAG="[TEST] "
cd "$REPO" || exit 1
mkdir -p "$STATE_DIR"
exec >> "$LOG" 2>&1
ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(ts)] $*"; }

# Email via Gmail SMTP (app password) — best-effort, never fatal.
notify() {  # notify <subject> <body-file-or-string> — subject is mode-tagged here
  local subject="${SUBJ_TAG}$1" body="$2"
  [[ -f "$body" ]] && body=$(cat "$body")
  (
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
  ) || log "notify failed (non-fatal): $subject"
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
  notify "EARSHOT MS run: FAILED (script died)" "ms-autorun.sh exited unexpectedly at $(ts). Check $LOG"
}
trap on_exit EXIT

# ── Guards ────────────────────────────────────────────────────────────────
if [[ "$TEST" != 1 && "$(date +%Y-%m-%d)" != "2026-07-15" ]]; then
  log "date guard: today is $(date +%Y-%m-%d), not 2026-07-15 — exiting"
  # Every early-exit guard emails — a silent no-op on event day looks
  # identical to a healthy idle box (audit F6/F8).
  notify "EARSHOT MS run: NOT RUN (date guard)" "ms-autorun fired on $(date +%Y-%m-%d), not the event date — exiting without launching. If this WAS supposed to be event day, the schedule or date guard is wrong."
  finished=1; exit 0
fi
# Pidfile guard: PID-alive alone is not enough — after a crash the OS can
# recycle the PID onto an unrelated process, which would silently abort the
# real run (audit F13). Require the live PID to actually BE an ms-autorun.
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null \
   && ps -p "$(cat "$PIDFILE")" -o command= 2>/dev/null | grep -q "ms-autorun"; then
  log "already running (pid $(cat "$PIDFILE")) — exiting"
  notify "EARSHOT MS run: SKIPPED (already running)" "ms-autorun found a live instance (pid $(cat "$PIDFILE")) at $(ts) and exited. If no run is actually active, investigate $PIDFILE."
  finished=1; exit 0
fi
rm -f "$PIDFILE"  # stale (dead or recycled PID) — clear and proceed
echo $$ > "$PIDFILE"
rm -f "$STATE_DIR/STOP" "$REPO/state/STOP"

# ── Mode ──────────────────────────────────────────────────────────────────
if [[ "$TEST" == 1 ]]; then
  LIVE_FLAGS="--allow-mention"          # dry-run: orders computed, never sent
  MAX_ATTEMPTS=2
  END_HHMM=$((10#$(date -v+8M "+%H%M"))) # test window: ~8 minutes from now
  LONG_STREAM_S=99999                   # test: never "call complete" — forces the redial path
  MIN_FRAMES=1
  # Test mode places NO real call unless explicitly asked — the loop/report
  # mechanics don't need to ring a real bridge (EARSHOT_AUTORUN_TEST_DIAL=1
  # restores the full dial rehearsal).
  DIAL_FLAG="--no-dial"
  [[ "${EARSHOT_AUTORUN_TEST_DIAL:-0}" == 1 ]] && DIAL_FLAG=""
  MODE="TEST/dry-run"
else
  LIVE_FLAGS="--i-am-live --allow-mention --fillable"
  MAX_ATTEMPTS=25
  END_HHMM=740                          # 07:40 local — stop redialing (call ends ~7:30)
  LONG_STREAM_S=1200                    # a >20 min stream = the real call happened
  MIN_FRAMES=30000                      # ≥10 min of actual AUDIO (50 frames/s) — wall
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
  notify "EARSHOT MS run: WARNING (no pinned binary)" "Live run is falling back to 'cargo run' from the working tree — scripts/preflight-event.sh was not run. Launch continues, but this is the unpinned path the runbook forbids."
fi

log "=== ms-autorun start (mode: $MODE, end window: $END_HHMM, max attempts: $MAX_ATTEMPTS) ==="
log "power: $(pmset -g batt | tr '\n' ' ')"
notify "EARSHOT MS run: LAUNCHED ($MODE)" "ms-autorun started $(ts).
Mode: $MODE
Power: $(pmset -g batt | tr '\n' ' ')
Log: $LOG"

# ── Attempt loop ──────────────────────────────────────────────────────────
# DTMF variants cycle across attempts to cover unknown prompt timing
# (twilio-dial.sh prefixes 8x'w' = 4 s to every variant):
DTMF_1="400700#"                    # 4 s wait, passcode
DTMF_2="wwwwwwww400700#"            # 8 s wait
DTMF_3="400700#wwwwwwww400700#"     # send twice (early + re-prompt)
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
      notify "EARSHOT MS run: NOT RUN (window already closed)" "ms-autorun started at $(ts) but now=$now >= end=$END_HHMM — ZERO attempts were made. The launchd job likely fired late (machine asleep at schedule time?)."
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
  scripts/launch-event.sh \
    --config configs/twilio-live-ms.toml \
    --account "$HOME/.earshot/ms.env" \
    --bind-port 8080 \
    --to +18448862200 --dtmf "$DTMF" \
    ${BIN_ARGS[@]+"${BIN_ARGS[@]}"} $DIAL_FLAG \
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
          (source "$HOME/.earshot/ms.env" 2>/dev/null; scripts/twilio-hangup.sh "$(cat "$STATE_DIR/call.sid")") \
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
      notify "EARSHOT MS run: attempt $attempt FAILED (rc=$rc, ${consec_failures} consecutive)" \
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
      if [[ "$TEST" == 1 || "$now" -ge 710 ]]; then
        log "long stream (${dur}s, ${frames} frames) ended in the end-window — treating as call complete"
        break
      fi
      log "long stream ended early (${dur}s at $now, ${frames} frames) — call may have dropped; redialing"
    else
      log "long attempt (${dur}s) but only ${frames} frames of audio — dead tunnel/stream? NOT treating as call complete; redialing"
      notify "EARSHOT MS run: attempt $attempt had NO AUDIO (${dur}s, ${frames} frames)" \
        "Mode: $MODE
The call leg ran ${dur}s but the pipeline saw ${frames} frames (< ${MIN_FRAMES}). Likely a dead tunnel or stream. Redialing."
    fi
  fi
  sleep 20
done

# ── Post-run report ───────────────────────────────────────────────────────
log "building report"
STRIKES="$STATE_DIR/strike-close.json"
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

{
  echo "EARSHOT MS-26JUL15 autonomous run — $(ts)  (mode: $MODE)"
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

notify "EARSHOT MS run: SUMMARY ($MODE, $attempt attempts)" "$SUMMARY"
log "summary emailed; written to $SUMMARY"

# ── Done — mark success BEFORE self-disarm: booting out our own label makes
# launchd SIGTERM this script, so nothing may follow the bootout. ──────────
rm -f "$PIDFILE"
finished=1
log "=== ms-autorun done ==="

# Self-disarm (live only): never fire again. This kills us — final act.
if [[ "$TEST" != 1 ]]; then
  trap - EXIT
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
fi
