#!/usr/bin/env bash
# Is the new-account restriction (compliance/fraud review hold) still on?
#
#   twilio-status.sh                                   # read-only snapshot
#   twilio-status.sh --probe --probe-to +5255...       # one definitive probe
#   twilio-status.sh --watch --probe-to +5255... [--interval 30]
#
# A review hold does NOT reliably surface in the read-only Account API
# (status can read "active" while outbound calling is blocked). The only
# definitive signal is to attempt the blocked action — an outbound call — and
# read Twilio's response: a CallSid means the hold is gone; an error means it
# is still on (and the error message says which restriction remains).
#
# The probe places a real call with self-terminating TwiML
# (<Response><Hangup/></Response>) and force-hangs-up immediately, so the
# destination barely rings and no audio is ever streamed. Same posture and
# credential contract as twilio-dial.sh / twilio-hangup.sh — no Kalshi, no
# orders, no recording.
#
# Requires ~/.twilio/earshot_env exporting TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
# (and TWILIO_FROM_NUMBER for the probe, a voice-capable Twilio number).
#
# Exit codes: 0 = calling works (lifted), 10 = still blocked, 2 = usage/env.
set -euo pipefail

PROBE="" WATCH="" PROBE_TO="" INTERVAL=30
while [[ $# -gt 0 ]]; do
  case "$1" in
    --probe)     PROBE=1; shift ;;
    --watch)     WATCH=1; shift ;;
    --probe-to)  PROBE_TO="$2"; shift 2 ;;
    --interval)  INTERVAL="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# shellcheck disable=SC1090
source ~/.twilio/earshot_env
: "${TWILIO_ACCOUNT_SID:?}" "${TWILIO_AUTH_TOKEN:?}"

API="https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}"
AUTH=(-u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}")

# --- read-only snapshot (context only; NOT authoritative for a review hold) ---
snapshot() {
  echo "== Twilio account snapshot (read-only; NOT authoritative for a review hold) =="
  curl -s "${API}.json" "${AUTH[@]}" \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print("  account : type=%s status=%s name=%s" % (d.get("type"), d.get("status"), d.get("friendly_name")))' || true
  curl -s "${API}/Balance.json" "${AUTH[@]}" \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print("  balance : %s %s" % (d.get("balance"), d.get("currency")))' || true
  curl -s "${API}/OutgoingCallerIds.json" "${AUTH[@]}" \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); ids=[c.get("phone_number") for c in d.get("outgoing_caller_ids",[])]; print("  verified:", ", ".join(ids) if ids else "(none)")' || true
}

# --- live-call probe: the definitive check. 0 = lifted, 10 = still blocked ---
probe() {
  local to="$1"
  : "${TWILIO_FROM_NUMBER:?probe needs TWILIO_FROM_NUMBER in ~/.twilio/earshot_env}"
  local resp verdict
  resp=$(curl -s -X POST "${API}/Calls.json" \
    --data-urlencode "To=${to}" \
    --data-urlencode "From=${TWILIO_FROM_NUMBER}" \
    --data-urlencode "Twiml=<Response><Hangup/></Response>" \
    "${AUTH[@]}") || true

  verdict=$(printf '%s' "$resp" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("PARSE"); sys.exit(0)
sid = d.get("sid")
if sid:
    print("LIFTED", sid, d.get("status", ""))
else:
    print("BLOCKED", d.get("code", "?"), "::", d.get("message", d))
') || true

  case "$verdict" in
    LIFTED*)
      local _ sid status
      read -r _ sid status <<<"$verdict"
      echo "  -> ACCEPTED: outbound calling WORKS (CallSid=${sid} status=${status})"
      # hang up immediately so the destination barely rings
      curl -s -X POST "${API}/Calls/${sid}.json" \
        --data-urlencode "Status=completed" "${AUTH[@]}" >/dev/null 2>&1 || true
      echo "  -> test call terminated (hangup sent)."
      return 0 ;;
    PARSE*)
      echo "  -> could not parse Twilio response:" >&2
      printf '%s\n' "$resp" >&2
      return 10 ;;
    *)
      echo "  -> STILL BLOCKED ${verdict#BLOCKED }"
      return 10 ;;
  esac
}

# --- dispatch ---
if [[ -n "$WATCH" ]]; then
  [[ -n "$PROBE_TO" ]] || { echo "usage: --watch needs --probe-to +1... (the probe is the definitive check)" >&2; exit 2; }
  snapshot
  echo "== watching: probing ${PROBE_TO} every ${INTERVAL} min until the hold lifts (Ctrl-C to stop) =="
  while true; do
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    echo "[$ts] probing..."
    if probe "$PROBE_TO"; then
      echo "[$ts] LIFTED — outbound calling is available. Green light for the loopback rehearsal."
      exit 0
    fi
    echo "[$ts] still blocked; sleeping ${INTERVAL}m."
    sleep $(( INTERVAL * 60 ))
  done
fi

snapshot
if [[ -n "$PROBE" ]]; then
  [[ -n "$PROBE_TO" ]] || { echo "usage: --probe needs --probe-to +1... (e.g. your own phone, +525545436717)" >&2; exit 2; }
  echo "== live-call probe (definitive) =="
  if probe "$PROBE_TO"; then exit 0; else exit 10; fi
fi
