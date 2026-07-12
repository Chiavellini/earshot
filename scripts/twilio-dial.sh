#!/usr/bin/env bash
# Originate the dial-in call and fork its inbound audio to our WS server.
#
#   twilio-dial.sh --to +18883243618 --dtmf "1364784#" --stream-url wss://<ngrok-host>/media
#
# Requires ~/.twilio/earshot_env exporting TWILIO_ACCOUNT_SID,
# TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER (a voice-capable Twilio number).
#
# TwiML notes (docs/TWILIO.md):
#   <Start><Stream>  forks inbound audio asynchronously — listen-only, the
#                    call keeps running while media flows to us.
#   <Play digits>    sends the passcode via DTMF. Each 'w' waits 0.5 s; the
#                    padding below assumes the bridge prompts within ~4 s of
#                    answer — tune during the loopback rehearsal.
#   <Pause>          holds the line for the call (90 min).
#
# Listen-only + no recording anywhere. Kill the audio leg with
# scripts/twilio-hangup.sh <CallSid> (printed below) or state/STOP for orders.
set -euo pipefail

TO="" DTMF="" STREAM_URL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --to)         TO="$2"; shift 2 ;;
    --dtmf)       DTMF="$2"; shift 2 ;;
    --stream-url) STREAM_URL="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$TO" && -n "$STREAM_URL" ]] || { echo "usage: $0 --to +1... [--dtmf 1234#] --stream-url wss://host/media" >&2; exit 2; }

# shellcheck disable=SC1090
source ~/.twilio/earshot_env
: "${TWILIO_ACCOUNT_SID:?}" "${TWILIO_AUTH_TOKEN:?}" "${TWILIO_FROM_NUMBER:?}"

PLAY_DIGITS=""
[[ -n "$DTMF" ]] && PLAY_DIGITS="<Play digits=\"wwwwwwww${DTMF}\"/>"

TWIML="<Response><Start><Stream url=\"${STREAM_URL}\" track=\"inbound_track\"/></Start>${PLAY_DIGITS}<Pause length=\"5400\"/></Response>"

RESP=$(curl -s -X POST "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Calls.json" \
  --data-urlencode "To=${TO}" \
  --data-urlencode "From=${TWILIO_FROM_NUMBER}" \
  --data-urlencode "Twiml=${TWIML}" \
  -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}")

SID=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("sid","ERROR"))')
if [[ "$SID" == "ERROR" || -z "$SID" ]]; then
  echo "call origination FAILED:" >&2
  echo "$RESP" >&2
  exit 1
fi
echo "call placed: CallSid=${SID}"
echo "hang up with: scripts/twilio-hangup.sh ${SID}"
