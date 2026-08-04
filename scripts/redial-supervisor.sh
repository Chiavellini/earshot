#!/usr/bin/env bash
# Mid-call redial supervisor (docs/layers/source.md, 2026-07-29).
#
# The armed pipeline's Twilio source survives a mid-call drop by re-entering
# its accept loop (twilio_stream.max_reconnects > 0) — but a source cannot
# place a call. On each drop it atomically rewrites
# ${STATE_DIR}/redial.request; this supervisor watches that marker's CONTENT
# (not its mtime — two drops in the same second must both fire) and re-dials
# the bridge so the re-opened accept window gets a fresh stream. The armed
# pipeline, its decision table, and the warm detector never restart — unlike
# event-autorun.sh's full-teardown retry loop, which re-pays ~30 s of warmup
# per attempt.
#
#   redial-supervisor.sh --state-dir state/<id> --to +1... --stream-url wss://… \
#                        [--dtmf 1234#] [--digit-delay 8] [--dtmf-repeat 1] \
#                        [--max-redials 3] [--max-minutes 120] \
#                        [--call-end-grace 2700] [--dial-script scripts/twilio-dial.sh]
#
# Marker contract (producer: src/source/twilio_ws.rs):
#   {"unix_ts":<s>,"old_call_sid":"<sid|''>","attempt":<n>,"reason":
#    "stop|idle|socket_closed|close_frame|ws_error"}
# reason=stop is a CLEAN Twilio stop event. After --call-end-grace seconds of
# supervisor uptime (default 2700 = 45 min) a stop means the call genuinely
# ended — redialing would place a paid call into a dead bridge, so we log the
# far-end call status and exit 0 instead. Early stops (IVR hangups, operator
# screens) and every abnormal reason still redial.
#
# Dialing requires ${STATE_DIR}/listener.ready (written by `earshot arm` only
# AFTER the detector is built and the pipeline is running). Without it a dial
# hands Twilio a socket nothing will ever accept() — the 2026-07-29 ORT hang.
#
# First redial is immediate; the backoff sleep (1→2→…→30 s) applies only after
# a FAILED dial attempt and resets on the next success.
#
# Hard stops (whichever comes first): --max-redials, --max-minutes from start,
# a STOP file in the state dir, or the launcher dying (we are its child).
# All decisions land in ${STATE_DIR}/redial.log.
set -euo pipefail
cd "$(dirname "$0")/.."

STATE_DIR="" TO="" DTMF="" STREAM_URL="" DIGIT_DELAY="" DTMF_REPEAT=""
MAX_REDIALS=3 MAX_MINUTES=120 CALL_END_GRACE=2700
# A redial that lands and produces no audio shows up as a fresh idle marker
# almost immediately. 30 s comfortably covers the production idle_timeout_secs=8
# plus dial setup, without flagging a genuine second drop minutes later.
MEDIA_CONFIRM_SECS=30
LAST_DIAL_TS=0
DIAL_SCRIPT="scripts/twilio-dial.sh"
HANGUP_SCRIPT="scripts/twilio-hangup.sh"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --state-dir)      STATE_DIR="$2"; shift 2 ;;
    --to)             TO="$2"; shift 2 ;;
    --dtmf)           DTMF="$2"; shift 2 ;;
    --stream-url)     STREAM_URL="$2"; shift 2 ;;
    --digit-delay)    DIGIT_DELAY="$2"; shift 2 ;;
    --dtmf-repeat)    DTMF_REPEAT="$2"; shift 2 ;;
    --max-redials)    MAX_REDIALS="$2"; shift 2 ;;
    --max-minutes)    MAX_MINUTES="$2"; shift 2 ;;
    --call-end-grace) CALL_END_GRACE="$2"; shift 2 ;;
    --media-confirm-secs) MEDIA_CONFIRM_SECS="$2"; shift 2 ;;
    # Future operator-venue redials pass a different (gated) dial script; the
    # pass-through args are identical, only the invoked path changes.
    --dial-script)    DIAL_SCRIPT="$2"; shift 2 ;;
    # Stub tests must never touch the Twilio API; production keeps the default.
    --hangup-script)  HANGUP_SCRIPT="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$STATE_DIR" && -n "$TO" && -n "$STREAM_URL" ]] || {
  echo "usage: $0 --state-dir state/<id> --to +1... --stream-url wss://… [--dtmf …] [--max-redials 3] [--max-minutes 120] [--call-end-grace 2700] [--dial-script <path>]" >&2
  exit 2
}
[[ "$MAX_REDIALS" =~ ^[0-9]+$ && "$MAX_MINUTES" =~ ^[1-9][0-9]*$ ]] || { echo "bad --max-redials/--max-minutes" >&2; exit 2; }
[[ "$CALL_END_GRACE" =~ ^[0-9]+$ ]] || { echo "bad --call-end-grace" >&2; exit 2; }

REQ="${STATE_DIR}/redial.request"
SID_FILE="${STATE_DIR}/call.sid"
READY="${STATE_DIR}/listener.ready"
LOG="${STATE_DIR}/redial.log"
log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; echo "redial-supervisor: $*" >&2; }

# Parse one string field out of the marker JSON; empty on any parse failure.
marker_field() { # $1 = marker content, $2 = field name
  printf '%s' "$1" | FIELD="$2" python3 -c '
import sys, json, os
try:
    print(json.load(sys.stdin).get(os.environ["FIELD"], ""))
except Exception:
    print("")
' 2>/dev/null || echo ""
}

# Best-effort far-end status for a CallSid, for the redial.log. twilio-status.sh
# is an account-hold prober (no per-call mode), so this is a one-shot curl with
# the same env vars twilio-dial.sh uses. Never fails the caller.
log_call_status() { # $1 = CallSid
  local sid="$1" line=""
  [[ -n "$sid" && -n "${TWILIO_ACCOUNT_SID:-}" && -n "${TWILIO_AUTH_TOKEN:-}" ]] || {
    log "far-end status for ${sid:-<none>}: unavailable (no sid or Twilio env)"
    return 0
  }
  line=$(curl -s -m 10 -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}" \
    "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Calls/${sid}.json" \
    | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print("status=%s duration=%s end_time=%s" % (d.get("status"), d.get("duration"), d.get("end_time")))
except Exception:
    print("unparseable response")
' 2>/dev/null) || line="fetch failed"
  log "far-end status for ${sid}: ${line:-fetch failed}"
}

START_TS=$(date +%s)
# A marker left over from a previous run must not trigger a dial at startup:
# whatever CONTENT exists now counts as already consumed.
LAST_CONTENT=$(cat "$REQ" 2>/dev/null || true)
LAST_NOT_READY_CONTENT=""
BACKOFF=0
REDIALS=0
log "up: watching $REQ content (max_redials=$MAX_REDIALS, max_minutes=$MAX_MINUTES, call_end_grace=${CALL_END_GRACE}s, dial=$DIAL_SCRIPT, to=$TO)"

while :; do
  sleep 0.25
  if [[ -f "${STATE_DIR}/STOP" ]]; then
    log "STOP file present — exiting, no more redials"
    exit 0
  fi
  NOW=$(date +%s)
  if (( NOW - START_TS > MAX_MINUTES * 60 )); then
    log "max-minutes ($MAX_MINUTES) reached — the call window is over, exiting"
    exit 0
  fi
  CONTENT=$(cat "$REQ" 2>/dev/null || true)
  [[ -n "$CONTENT" && "$CONTENT" != "$LAST_CONTENT" ]] || continue

  REASON=$(marker_field "$CONTENT" reason)
  OLD_SID=$(marker_field "$CONTENT" old_call_sid)
  [[ -n "$OLD_SID" ]] || OLD_SID=$(head -1 "$SID_FILE" 2>/dev/null || true)

  # Clean stop after the grace window = the call genuinely ended; a redial
  # would place a paid call into a dead bridge. Early stops (IVR kicked us
  # during joining) and all abnormal reasons still redial.
  if [[ "$REASON" == "stop" ]] && (( NOW - START_TS > CALL_END_GRACE )); then
    log "stop after grace window — treating as call end, not redialing (uptime $((NOW - START_TS))s > ${CALL_END_GRACE}s): $CONTENT"
    log_call_status "$OLD_SID"
    exit 0
  fi

  # Never dial into a pipeline that cannot accept: the ready token is written
  # only after the detector is built and the pipeline is running. Do NOT
  # consume the marker — retry it on the next poll in case readiness returns.
  if [[ ! -f "$READY" ]]; then
    if [[ "$CONTENT" != "$LAST_NOT_READY_CONTENT" ]]; then
      log "refusing to dial: $READY missing (pipeline not accepting) — will retry while the request stands"
      LAST_NOT_READY_CONTENT="$CONTENT"
    fi
    continue
  fi
  LAST_NOT_READY_CONTENT=""

  if (( REDIALS >= MAX_REDIALS )); then
    log "request seen but redial budget exhausted ($REDIALS/$MAX_REDIALS) — ignoring"
    LAST_CONTENT="$CONTENT"
    continue
  fi
  LAST_CONTENT="$CONTENT"

  # Did the PREVIOUS redial actually produce audio? The dial script's exit code
  # only says Twilio accepted the call-creation request; it cannot say the
  # bridge admitted us or that a single frame arrived. The in-band answer is
  # this very marker: the source rewrites it when it goes idle, so a fresh
  # request arriving within a hair of the last dial means that dial landed and
  # produced nothing.
  #
  # Worth naming because the budget is small — production runs max_reconnects=2
  # — and a bridge that admits and instantly re-drops would otherwise eat both
  # attempts with two cheerful "redialed OK" lines and no explanation.
  if (( LAST_DIAL_TS > 0 && NOW - LAST_DIAL_TS <= MEDIA_CONFIRM_SECS )); then
    log "REDIAL-NO-MEDIA: redial #${REDIALS} produced no audio — the source went idle again after $((NOW - LAST_DIAL_TS))s (bar ${MEDIA_CONFIRM_SECS}s). The call connected but the leg is silent or was dropped again; a further redial will likely do the same."
    # Page it: the budget is small and this class eats it silently otherwise
    # (2026-08-01 f67ac27 made it visible in the log; a log nobody reads
    # during a live call is the PYPL/BA lesson again).
    "${EARSHOT_NOTIFY:-scripts/notify.sh}" "REDIAL-NO-MEDIA: $(basename "$STATE_DIR")" \
      "redial #${REDIALS} connected but produced no audio within ${MEDIA_CONFIRM_SECS}s; budget $((MAX_REDIALS - REDIALS)) left" \
      --quiet >>"$LOG" 2>&1 || log "warn: notify failed for REDIAL-NO-MEDIA"
  fi

  REDIALS=$((REDIALS + 1))
  log "redial request #${REDIALS} (reason=${REASON:-?}): $CONTENT"

  # Hygiene: a half-open previous call may still hold the bridge slot. Hang it
  # up CONCURRENTLY with the new dial — the source rejects the zombie stream by
  # callSid, so ordering is safe and serializing it only delays the redial.
  if [[ -n "$OLD_SID" ]]; then
    {
      if "$HANGUP_SCRIPT" "$OLD_SID" >>"$LOG" 2>&1; then
        log "hung up previous call $OLD_SID"
      else
        log "previous call $OLD_SID hangup skipped (probably already dead)"
      fi
    } &
  fi

  DIAL_ARGS=(--to "$TO" --stream-url "$STREAM_URL" --sid-file "$SID_FILE")
  [[ -n "$DTMF" ]] && DIAL_ARGS+=(--dtmf "$DTMF")
  [[ -n "$DIGIT_DELAY" ]] && DIAL_ARGS+=(--digit-delay "$DIGIT_DELAY")
  [[ -n "$DTMF_REPEAT" ]] && DIAL_ARGS+=(--dtmf-repeat "$DTMF_REPEAT")
  if "$DIAL_SCRIPT" "${DIAL_ARGS[@]}" >>"$LOG" 2>&1; then
    # "dialed", not "capturing": media is confirmed only by the ABSENCE of a
    # fresh idle marker over the next MEDIA_CONFIRM_SECS, checked at the top of
    # the loop.
    LAST_DIAL_TS=$(date +%s)
    log "redialed OK (#${REDIALS}): new CallSid $(head -1 "$SID_FILE" 2>/dev/null || echo '?') — media unconfirmed for ${MEDIA_CONFIRM_SECS}s"
    BACKOFF=0
  else
    BACKOFF=$(( BACKOFF == 0 ? 1 : (BACKOFF * 2 > 30 ? 30 : BACKOFF * 2) ))
    log "REDIAL #${REDIALS} FAILED — backing off ${BACKOFF}s before honoring the next request"
    sleep "$BACKOFF"
  fi
done
