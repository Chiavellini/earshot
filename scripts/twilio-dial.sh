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
#   <Play digits>    sends the passcode via DTMF. Each 'w' waits 0.5 s.
#   <Pause>          holds the line for the call (90 min).
#
# ⚠ DTMF TIMING WAS A HARDCODED 4 s AND IT COST US TWO EVENTS (2026-07-29).
# The padding used to be a literal `wwwwwwww` (8 × 0.5 s) with the code played
# exactly ONCE, on the assumption that every bridge prompts within ~4 s. CMG's
# does — that call held for 4572 s. MSFT (+18774070666) and CVNA
# (+16463071963) do not: all four attempts were ANSWERED and then dropped by
# the far end at 23 / 42 / 42 / 73 s, `status=completed`, i.e. the digits went
# into dead air and the bridge hung up on an input timeout. A fixed constant
# cannot fit every venue, so it is now a knob:
#
#   --digit-delay <n>   n × 0.5 s of 'w' padding before the digits (default 8 = 4 s)
#   --dtmf-repeat <n>   send the code n times, 2 s apart (default 1)
#
# Defaults reproduce the old TwiML byte-for-byte, so every existing caller is
# unaffected. Tune per venue in `configs/dial-ins.toml` and pin what worked.
#
# Listen-only + no recording anywhere. Kill the audio leg with
# scripts/twilio-hangup.sh <CallSid> (printed below) or state/STOP for orders.
set -euo pipefail

TO="" DTMF="" STREAM_URL="" SID_FILE="" DRY_RUN=0
DIGIT_DELAY=8 DTMF_REPEAT=1     # defaults reproduce the pre-2026-07-29 TwiML exactly
PAUSE_SECS=5400                 # hold the line 90 min (a full call); probes pass less
while [[ $# -gt 0 ]]; do
  case "$1" in
    --to)          TO="$2"; shift 2 ;;
    --dtmf)        DTMF="$2"; shift 2 ;;
    --stream-url)  STREAM_URL="$2"; shift 2 ;;
    --sid-file)    SID_FILE="$2"; shift 2 ;;
    --digit-delay) DIGIT_DELAY="$2"; shift 2 ;;   # n × 0.5 s before the code
    --dtmf-repeat) DTMF_REPEAT="$2"; shift 2 ;;   # send the code n times, 2 s apart
    --pause-secs)  PAUSE_SECS="$2"; shift 2 ;;    # <Pause> hold time (bridge-probe uses ~150)
    --dry-run)     DRY_RUN=1; shift ;;   # build+print TwiML, place NO call (testing)
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$TO" && -n "$STREAM_URL" ]] || { echo "usage: $0 --to +1... [--dtmf 1234#] --stream-url wss://host/media [--sid-file <path>] [--digit-delay 8] [--dtmf-repeat 1] [--dry-run]" >&2; exit 2; }
# Numeric + sane-bound the timing knobs. An unbounded delay would silently hold
# a live bridge without ever sending the code; 120 (=60 s) is far past any
# observed prompt and still finite.
[[ "$DIGIT_DELAY" =~ ^[0-9]+$ && "$DIGIT_DELAY" -le 120 ]] || { echo "REFUSING: --digit-delay must be an integer 0..120 (0.5 s units)" >&2; exit 2; }
[[ "$DTMF_REPEAT" =~ ^[1-9][0-9]*$ && "$DTMF_REPEAT" -le 5 ]] || { echo "REFUSING: --dtmf-repeat must be an integer 1..5" >&2; exit 2; }
# 5400 (90 min) covers any earnings call; more would hold a line for no reason.
[[ "$PAUSE_SECS" =~ ^[1-9][0-9]*$ && "$PAUSE_SECS" -le 5400 ]] || { echo "REFUSING: --pause-secs must be an integer 1..5400" >&2; exit 2; }

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
if [[ -n "$DTMF" ]]; then
  # Build the 'w' padding from --digit-delay rather than a literal constant.
  PAD=""; for ((_i = 0; _i < DIGIT_DELAY; _i++)); do PAD+="w"; done
  # Repeats are separated by 4 × 'w' (2 s) so a bridge that re-prompts, or that
  # swallowed the first entry, gets another chance on the same call.
  SEQ="${PAD}${DTMF}"
  for ((_r = 1; _r < DTMF_REPEAT; _r++)); do SEQ+="wwww${DTMF}"; done
  PLAY_DIGITS="<Play digits=\"${SEQ}\"/>"
fi

# One-way capture by construction: <Start><Stream> forks inbound audio to us
# and has NO return-audio path (unlike <Connect><Stream>). Never emit <Connect>,
# <Dial>, or <Number> here — that would make audio-injection possible.
TWIML="<Response><Start><Stream url=\"${STREAM_URL}\" track=\"inbound_track\"/></Start>${PLAY_DIGITS}<Pause length=\"${PAUSE_SECS}\"/></Response>"

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
