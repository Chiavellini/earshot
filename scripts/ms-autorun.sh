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
cd "$REPO" || exit 1
mkdir -p "$STATE_DIR"
exec >> "$LOG" 2>&1
ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(ts)] $*"; }

# Email via Gmail SMTP (app password) — best-effort, never fatal.
notify() {  # notify <subject> <body-file-or-string>
  local subject="$1" body="$2"
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
  finished=1; exit 0
fi
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  log "already running (pid $(cat "$PIDFILE")) — exiting"
  finished=1; exit 0
fi
echo $$ > "$PIDFILE"
rm -f "$STATE_DIR/STOP" "$REPO/state/STOP"

# ── Mode ──────────────────────────────────────────────────────────────────
if [[ "$TEST" == 1 ]]; then
  LIVE_FLAGS="--allow-mention"          # dry-run: orders computed, never sent
  MAX_ATTEMPTS=2
  END_HHMM=$((10#$(date -v+8M "+%H%M"))) # test window: ~8 minutes from now
  LONG_STREAM_S=99999                   # test: never "call complete" — forces the redial path
  MODE="TEST/dry-run"
else
  LIVE_FLAGS="--i-am-live --allow-mention --fillable"
  MAX_ATTEMPTS=25
  END_HHMM=740                          # 07:40 local — stop redialing (call ends ~7:30)
  LONG_STREAM_S=1200                    # a >20 min stream = the real call happened
  MODE="LIVE mention-fill"
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
while :; do
  now=$((10#$(date +%H%M)))
  if [[ "$now" -ge "$END_HHMM" ]]; then log "window closed ($now >= $END_HHMM) — stop"; break; fi
  if [[ -f "$STATE_DIR/STOP" ]]; then log "STOP file present — stop redialing"; break; fi
  if [[ "$attempt" -ge "$MAX_ATTEMPTS" ]]; then log "max attempts reached — stop"; break; fi
  attempt=$((attempt+1))
  case $((attempt % 3)) in
    1) DTMF="$DTMF_1" ;;
    2) DTMF="$DTMF_2" ;;
    0) DTMF="$DTMF_3" ;;
  esac

  # Clean slate: no stale tunnel/arm from a previous attempt.
  pkill -f "ngrok http 8080" 2>/dev/null
  lsof -ti :8080 2>/dev/null | xargs kill 2>/dev/null
  sleep 2

  log "── attempt $attempt (dtmf variant: $DTMF)"
  t0=$(date +%s)
  scripts/launch-event.sh \
    --config configs/twilio-live-ms.toml \
    --account "$HOME/.earshot/ms.env" \
    --bind-port 8080 \
    --to +18448862200 --dtmf "$DTMF" \
    $LIVE_FLAGS
  rc=$?
  dur=$(( $(date +%s) - t0 ))
  log "attempt $attempt ended: rc=$rc after ${dur}s"

  if [[ "$dur" -ge "$LONG_STREAM_S" ]]; then
    long_stream_seen=1
    now=$((10#$(date +%H%M)))
    if [[ "$TEST" == 1 || "$now" -ge 710 ]]; then
      log "long stream (${dur}s) ended in the end-window — treating as call complete"
      break
    fi
    log "long stream ended early (${dur}s at $now) — call may have dropped; redialing"
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

# ── Self-disarm (live only): never fire again ─────────────────────────────
if [[ "$TEST" != 1 ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  log "launchd job disarmed"
fi
rm -f "$PIDFILE"
finished=1
log "=== ms-autorun done ==="
