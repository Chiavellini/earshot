#!/usr/bin/env bash
# Originate the dial-in call and fork its inbound audio to our WS server.
#
#   twilio-dial.sh --to +18883243618 --dtmf "1364784#" --stream-url wss://<ngrok-host>/media \
#                  [--sid-file <path>]   # write the CallSid here for supervisors
#
# Credentials: uses TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER
# from the CALLER's environment when set (launch-event.sh sources the per-event
# --account file — that must win, or multi-event isolation breaks); falls back
# to ~/.twilio/earshot_env only when they are absent.
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

TO="" DTMF="" STREAM_URL="" SID_FILE="" DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --to)         TO="$2"; shift 2 ;;
    --dtmf)       DTMF="$2"; shift 2 ;;
    --stream-url) STREAM_URL="$2"; shift 2 ;;
    --sid-file)   SID_FILE="$2"; shift 2 ;;
    --dry-run)    DRY_RUN=1; shift ;;   # build+print TwiML, place NO call (testing)
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$TO" && -n "$STREAM_URL" ]] || { echo "usage: $0 --to +1... [--dtmf 1234#] --stream-url wss://host/media [--sid-file <path>] [--dry-run]" >&2; exit 2; }

# LISTEN-ONLY CAPTURE INVARIANT (docs/TWILIO.md): DTMF is passcode-entry ONLY.
# Allowed charset is digits, '#', and 'w' (0.5 s wait). '*' is HARD-REJECTED
# because '*1'/'*11' is the raise-hand/unmute key on operator bridges — the
# system must never be able to enter a Q&A queue or open its line onto a live
# call. Any other character is rejected too (fail closed).
if [[ -n "$DTMF" && ! "$DTMF" =~ ^[0-9#w]+$ ]]; then
  echo "REFUSING: --dtmf \"$DTMF\" contains a disallowed character." >&2
  echo "  DTMF is passcode-entry only: charset [0-9#w]. '*' (raise-hand/unmute) is never permitted." >&2
  echo "  A bridge whose ENTRY requires '*' is not usable under the listen-only invariant (docs/TWILIO.md)." >&2
  exit 3
fi

# Per-event credentials from the caller WIN; the global file is only a
# fallback for manual use. Sourcing it unconditionally overrode the
# --account isolation (audit F5, 2026-07-15).
if [[ -z "${TWILIO_ACCOUNT_SID:-}" || -z "${TWILIO_AUTH_TOKEN:-}" || -z "${TWILIO_FROM_NUMBER:-}" ]]; then
  # shellcheck disable=SC1090
  source ~/.twilio/earshot_env
fi
: "${TWILIO_ACCOUNT_SID:?}" "${TWILIO_AUTH_TOKEN:?}" "${TWILIO_FROM_NUMBER:?}"

PLAY_DIGITS=""
[[ -n "$DTMF" ]] && PLAY_DIGITS="<Play digits=\"wwwwwwww${DTMF}\"/>"

# One-way capture by construction: <Start><Stream> forks inbound audio to us
# and has NO return-audio path (unlike <Connect><Stream>). Never emit <Connect>,
# <Dial>, or <Number> here — that would make audio-injection possible.
TWIML="<Response><Start><Stream url=\"${STREAM_URL}\" track=\"inbound_track\"/></Start>${PLAY_DIGITS}<Pause length=\"5400\"/></Response>"

# Dry run: emit the TwiML for inspection/tests and place NO call (no creds needed).
if [[ "$DRY_RUN" == "1" ]]; then
  echo "$TWIML"
  exit 0
fi

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
[[ -n "$SID_FILE" ]] && echo "$SID" > "$SID_FILE"
exit 0
