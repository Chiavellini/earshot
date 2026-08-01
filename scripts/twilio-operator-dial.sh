#!/usr/bin/env bash
# OPERATOR-GATED bridge dial (docs/OPERATOR-HANDLER.md v1, human-first).
#
# Dials a Class B (live-operator-screened) bridge and, instead of parking on a
# <Pause>, drops the bridge leg into a named Twilio <Conference> so the USER'S
# CELL — paged in FIRST via scripts/operator-page.sh — can answer the
# operator's questions by voice. The inbound media fork starts with the call
# and persists for its whole life, so after admission the human hangs up and
# the capture continues exactly like the listen-only path.
#
#   twilio-operator-dial.sh --config <event.toml> --operator-i-am-paged \
#                           --to +1... --stream-url wss://host/media \
#                           [--conference <name>] [--dtmf 1234#] \
#                           [--digit-delay 8] [--dtmf-repeat 1] \
#                           [--time-limit <secs>] [--sid-file <path>] [--dry-run]
#
# DOUBLE GATE (both required, fail closed):
#   1. the event config must carry `[operator_handler]` with `enabled = true`
#   2. the caller must pass --operator-i-am-paged, asserting the pager leg
#      (operator-page.sh) has been originated and answered FIRST — a bridge
#      must never meet an unmanned conference (the operator drops silent
#      callers in 21-73 s, VERIFICATIONS Q-ADMIT).
#
# TwiML posture (the operator invariant, tests/twilio_operator_gated.rs):
#   <Start><Stream track="inbound_track">  one-way fork of the BRIDGE's audio;
#                                          conference/user audio rides the
#                                          outbound track and never enters the
#                                          pipeline (hypothesis pinned by
#                                          scripts/probe-q-conf.sh before live use)
#   <Play digits>                          passcode only, charset [0-9#w],
#                                          '*' hard-rejected (verbatim guard
#                                          from scripts/twilio-dial.sh)
#   <Dial><Conference>                     startConferenceOnEnter="false": the
#                                          bridge hears silence until the human
#                                          is present; endConferenceOnExit="true":
#                                          the conference dies with the bridge leg
#   NEVER <Number>, <Sip>, <Connect>, <Say>, <Record> — self-checked below.
#
# The default listen-only dial (scripts/twilio-dial.sh) is untouched by this
# script's existence; tests/twilio_operator_gated.rs freezes both.
set -euo pipefail

TO="" DTMF="" STREAM_URL="" SID_FILE="" DRY_RUN=0
CONFIG="" CONFERENCE="" PAGED=0
DIGIT_DELAY=8 DTMF_REPEAT=1     # same defaults as twilio-dial.sh
TIME_LIMIT=""                   # optional <Dial timeLimit> cap (--pause-secs analogue)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --to)                  TO="$2"; shift 2 ;;
    --dtmf)                DTMF="$2"; shift 2 ;;
    --stream-url)          STREAM_URL="$2"; shift 2 ;;
    --sid-file)            SID_FILE="$2"; shift 2 ;;
    --digit-delay)         DIGIT_DELAY="$2"; shift 2 ;;   # n × 0.5 s before the code
    --dtmf-repeat)         DTMF_REPEAT="$2"; shift 2 ;;   # send the code n times, 2 s apart
    --time-limit)          TIME_LIMIT="$2"; shift 2 ;;    # cap the bridge leg (secs, 1..5400)
    --config)              CONFIG="$2"; shift 2 ;;
    --conference)          CONFERENCE="$2"; shift 2 ;;
    --operator-i-am-paged) PAGED=1; shift ;;
    --dry-run)             DRY_RUN=1; shift ;;   # build+print TwiML + redacted curl, place NO call
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$TO" && -n "$STREAM_URL" && -n "$CONFIG" ]] || {
  echo "usage: $0 --config <event.toml> --operator-i-am-paged --to +1... --stream-url wss://host/media [--conference <name>] [--dtmf 1234#] [--digit-delay 8] [--dtmf-repeat 1] [--time-limit <secs>] [--sid-file <path>] [--dry-run]" >&2
  exit 2
}
[[ -f "$CONFIG" ]] || { echo "config not found: $CONFIG" >&2; exit 2; }

# ── DOUBLE GATE ─────────────────────────────────────────────────────────────
if [[ "$PAGED" != "1" ]]; then
  echo "REFUSING: --operator-i-am-paged not passed." >&2
  echo "  The operator path requires the human cell leg to be paged into the conference FIRST" >&2
  echo "  (scripts/operator-page.sh --wait-joined ...). A bridge must never meet an unmanned conference." >&2
  exit 5
fi
# Same sed style launch-event.sh uses for max_reconnects: section-scoped,
# shape-stable lines emitted by `earshot prepare`, no tomllib dependency.
OP_ENABLED=$(sed -n '/^\[operator_handler\]/,/^\[/s/^[[:space:]]*enabled[[:space:]]*=[[:space:]]*\([a-z]*\).*/\1/p' "$CONFIG" | head -1)
if [[ "$OP_ENABLED" != "true" ]]; then
  echo "REFUSING: ${CONFIG} has no [operator_handler] enabled = true." >&2
  echo "  The operator-capable leg is double-gated: config opt-in AND --operator-i-am-paged." >&2
  exit 5
fi
OP_PREFIX=$(sed -n '/^\[operator_handler\]/,/^\[/s/^[[:space:]]*conference_prefix[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
[[ -n "$OP_PREFIX" ]] || OP_PREFIX="earshot"
EVENT_ID=$(sed -n '/^\[event\]/,/^\[/s/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)

# Conference name: default <prefix>-<event-id, lowercased/sanitized> so this
# script and operator-page.sh land in the SAME room from --config alone. An
# explicit --conference must keep the config's namespace prefix.
if [[ -z "$CONFERENCE" ]]; then
  [[ -n "$EVENT_ID" ]] || { echo "could not parse [event].id from ${CONFIG} (needed for the default conference name; pass --conference)" >&2; exit 2; }
  SAFE_EVENT=$(printf '%s' "$EVENT_ID" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-')
  CONFERENCE="${OP_PREFIX}-${SAFE_EVENT}"
fi
[[ "$CONFERENCE" =~ ^[a-z0-9-]{1,64}$ ]] || { echo "REFUSING: conference name ${CONFERENCE:0:80}... must match [a-z0-9-]{1,64}" >&2; exit 2; }
[[ "$CONFERENCE" == "$OP_PREFIX"-* || "$CONFERENCE" == "$OP_PREFIX" ]] || {
  echo "REFUSING: --conference \"$CONFERENCE\" is outside this event's namespace \"${OP_PREFIX}-*\" — two events must never collide on a conference room" >&2
  exit 2
}

# Destination sanity: strict E.164 (mirrors config::is_e164).
[[ "$TO" =~ ^\+[1-9][0-9]{7,14}$ ]] || { echo "REFUSING: --to \"$TO\" is not strict E.164 (+[1-9][0-9]{7,14})" >&2; exit 2; }

# Numeric + sane-bound the timing knobs (verbatim from scripts/twilio-dial.sh).
[[ "$DIGIT_DELAY" =~ ^[0-9]+$ && "$DIGIT_DELAY" -le 120 ]] || { echo "REFUSING: --digit-delay must be an integer 0..120 (0.5 s units)" >&2; exit 2; }
[[ "$DTMF_REPEAT" =~ ^[1-9][0-9]*$ && "$DTMF_REPEAT" -le 5 ]] || { echo "REFUSING: --dtmf-repeat must be an integer 1..5" >&2; exit 2; }
if [[ -n "$TIME_LIMIT" ]]; then
  [[ "$TIME_LIMIT" =~ ^[1-9][0-9]*$ && "$TIME_LIMIT" -le 5400 ]] || { echo "REFUSING: --time-limit must be an integer 1..5400" >&2; exit 2; }
fi

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

# Bridge-leg TwiML. startConferenceOnEnter="false" — the bridge sits in the
# room but hears nothing until the human (whose leg sets it true) is present;
# waitUrl="" suppresses Twilio's default hold music (silence, never audio, at
# the bridge). endConferenceOnExit="true" — the room dies with the bridge leg.
DIAL_OPEN="<Dial>"
[[ -n "$TIME_LIMIT" ]] && DIAL_OPEN="<Dial timeLimit=\"${TIME_LIMIT}\">"
TWIML="<Response><Start><Stream url=\"${STREAM_URL}\" track=\"inbound_track\"/></Start>${PLAY_DIGITS}${DIAL_OPEN}<Conference beep=\"false\" startConferenceOnEnter=\"false\" endConferenceOnExit=\"true\" waitUrl=\"\">${CONFERENCE}</Conference></Dial></Response>"

# Fail-closed self-check: this leg dials a LIVE earnings bridge — audio
# injection verbs must be structurally impossible, not merely absent.
for forbidden in "<Number" "<Sip" "<Connect" "<Say" "<Record"; do
  if [[ "$TWIML" == *"$forbidden"* ]]; then
    echo "REFUSING: constructed TwiML contains forbidden verb ${forbidden} — operator invariant violated" >&2
    exit 6
  fi
done

# Dry run: emit the TwiML + a redacted curl for inspection/tests; place NO
# call (deliberately BEFORE credential resolution — needs no secrets).
if [[ "$DRY_RUN" == "1" ]]; then
  echo "$TWIML"
  echo "CONFERENCE=${CONFERENCE}"
  echo "# curl -s -X POST https://api.twilio.com/2010-04-01/Accounts/<SID>/Calls.json \\"
  echo "#   --data-urlencode \"To=${TO}\" --data-urlencode \"From=<FROM>\" \\"
  echo "#   --data-urlencode \"Twiml=<above>\" -u \"<SID>:<REDACTED>\""
  exit 0
fi

# Per-event credentials from the caller WIN; the global file is only a
# fallback for manual use (same rule as twilio-dial.sh, audit F5).
if [[ -z "${TWILIO_ACCOUNT_SID:-}" || -z "${TWILIO_AUTH_TOKEN:-}" || -z "${TWILIO_FROM_NUMBER:-}" ]]; then
  # shellcheck disable=SC1090
  source ~/.twilio/earshot_env
fi
: "${TWILIO_ACCOUNT_SID:?}" "${TWILIO_AUTH_TOKEN:?}" "${TWILIO_FROM_NUMBER:?}"

RESP=$(curl -s -X POST "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Calls.json" \
  --data-urlencode "To=${TO}" \
  --data-urlencode "From=${TWILIO_FROM_NUMBER}" \
  --data-urlencode "Twiml=${TWIML}" \
  -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}")

SID=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("sid","ERROR"))')
if [[ "$SID" == "ERROR" || -z "$SID" ]]; then
  echo "operator bridge-leg origination FAILED:" >&2
  echo "$RESP" >&2
  exit 1
fi
echo "operator bridge leg placed: CallSid=${SID} conference=${CONFERENCE}"
echo "hang up with: scripts/twilio-hangup.sh ${SID}"
[[ -n "$SID_FILE" ]] && echo "$SID" > "$SID_FILE"
exit 0
