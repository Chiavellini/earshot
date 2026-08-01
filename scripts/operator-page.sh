#!/usr/bin/env bash
# Page the USER'S CELL into an event's operator conference (docs/OPERATOR-HANDLER.md
# v1, human-first). Originated BEFORE scripts/twilio-operator-dial.sh dials the
# bridge, so the operator's questions are answered by a live human — never by
# silence (Class B bridges drop silent callers in 21-73 s, VERIFICATIONS Q-ADMIT).
#
#   operator-page.sh --config <event.toml> --operator-i-am-paged \
#                    [--conference <name>] [--sid-file <path>] \
#                    [--wait-joined <secs>] [--dry-run]
#
# DESTINATION IS NOT A CHOICE: the pager dials EXACTLY the config's
# [operator_handler] cell_number. A --to arg is accepted only as a tripwire —
# any value other than the configured cell is refused.
#
# TwiML (the pager invariant, tests/twilio_operator_gated.rs):
#   <Say>   allowed HERE ONLY — it targets the user's own phone, never a bridge
#   <Dial>  contains ONLY <Conference> (startConferenceOnEnter="true": media
#           starts when the human answers; endConferenceOnExit="false": the
#           human hanging up after admission must NOT kill the bridge leg)
#   NEVER <Number>, <Sip>, <Connect>, <Record>, <Play digits>.
#
# --wait-joined N polls the Twilio Calls API until the page is answered
# (status=in-progress) and exits nonzero on timeout/busy/no-answer — the
# orchestrator uses this to never dial a bridge unmanned.
set -euo pipefail

CONFIG="" CONFERENCE="" SID_FILE="" DRY_RUN=0 PAGED=0 TO_OVERRIDE="" WAIT_JOINED=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)              CONFIG="$2"; shift 2 ;;
    --conference)          CONFERENCE="$2"; shift 2 ;;
    --sid-file)            SID_FILE="$2"; shift 2 ;;
    --to)                  TO_OVERRIDE="$2"; shift 2 ;;   # tripwire only — must equal the config cell
    --wait-joined)         WAIT_JOINED="$2"; shift 2 ;;   # poll until answered, else exit nonzero
    --operator-i-am-paged) PAGED=1; shift ;;
    --dry-run)             DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$CONFIG" ]] || {
  echo "usage: $0 --config <event.toml> --operator-i-am-paged [--conference <name>] [--sid-file <path>] [--wait-joined <secs>] [--dry-run]" >&2
  exit 2
}
[[ -f "$CONFIG" ]] || { echo "config not found: $CONFIG" >&2; exit 2; }
if [[ -n "$WAIT_JOINED" ]]; then
  [[ "$WAIT_JOINED" =~ ^[1-9][0-9]*$ && "$WAIT_JOINED" -le 600 ]] || { echo "REFUSING: --wait-joined must be an integer 1..600 (secs)" >&2; exit 2; }
fi

# ── DOUBLE GATE (same as twilio-operator-dial.sh) ───────────────────────────
if [[ "$PAGED" != "1" ]]; then
  echo "REFUSING: --operator-i-am-paged not passed (the operator path is explicitly opt-in per dial)." >&2
  exit 5
fi
OP_ENABLED=$(sed -n '/^\[operator_handler\]/,/^\[/s/^[[:space:]]*enabled[[:space:]]*=[[:space:]]*\([a-z]*\).*/\1/p' "$CONFIG" | head -1)
if [[ "$OP_ENABLED" != "true" ]]; then
  echo "REFUSING: ${CONFIG} has no [operator_handler] enabled = true." >&2
  exit 5
fi

# Config fields (same sed style as launch-event.sh; lines are prepare-shaped).
OP_CELL=$(sed -n '/^\[operator_handler\]/,/^\[/s/^[[:space:]]*cell_number[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
OP_PREFIX=$(sed -n '/^\[operator_handler\]/,/^\[/s/^[[:space:]]*conference_prefix[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
[[ -n "$OP_PREFIX" ]] || OP_PREFIX="earshot"
EVENT_ID=$(sed -n '/^\[event\]/,/^\[/s/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
[[ "$OP_CELL" =~ ^\+[1-9][0-9]{7,14}$ ]] || {
  echo "REFUSING: [operator_handler] cell_number ${OP_CELL:-<empty>} in ${CONFIG} is not strict E.164" >&2
  exit 2
}
# The pager's destination is the configured cell, full stop.
if [[ -n "$TO_OVERRIDE" && "$TO_OVERRIDE" != "$OP_CELL" ]]; then
  echo "REFUSING: --to \"$TO_OVERRIDE\" != configured [operator_handler] cell_number \"$OP_CELL\"." >&2
  echo "  The pager leg may dial ONLY the configured cell. Edit the config, not the flag." >&2
  exit 6
fi
TO="$OP_CELL"

# Conference name: identical derivation to twilio-operator-dial.sh so both
# legs land in the SAME room from --config alone.
if [[ -z "$CONFERENCE" ]]; then
  [[ -n "$EVENT_ID" ]] || { echo "could not parse [event].id from ${CONFIG} (needed for the default conference name; pass --conference)" >&2; exit 2; }
  SAFE_EVENT=$(printf '%s' "$EVENT_ID" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-')
  CONFERENCE="${OP_PREFIX}-${SAFE_EVENT}"
fi
[[ "$CONFERENCE" =~ ^[a-z0-9-]{1,64}$ ]] || { echo "REFUSING: conference name ${CONFERENCE:0:80}... must match [a-z0-9-]{1,64}" >&2; exit 2; }
[[ "$CONFERENCE" == "$OP_PREFIX"-* || "$CONFERENCE" == "$OP_PREFIX" ]] || {
  echo "REFUSING: --conference \"$CONFERENCE\" is outside this event's namespace \"${OP_PREFIX}-*\"" >&2
  exit 2
}

# <Say> body: EVENT_ID is prepare-shaped ([A-Z0-9-]); anything else is
# sanitized so the XML cannot be broken or repurposed.
SAY_EVENT=$(printf '%s' "${EVENT_ID:-unknown-event}" | tr -c 'a-zA-Z0-9 .-' ' ')
TWIML="<Response><Say>EARSHOT operator screen for ${SAY_EVENT}. Joining you to the bridge conference now. Handle the operator, then hang up.</Say><Dial><Conference beep=\"false\" startConferenceOnEnter=\"true\" endConferenceOnExit=\"false\">${CONFERENCE}</Conference></Dial></Response>"

# Fail-closed self-check: even on the user's own phone, never these verbs.
for forbidden in "<Number" "<Sip" "<Connect" "<Record" "<Play"; do
  if [[ "$TWIML" == *"$forbidden"* ]]; then
    echo "REFUSING: constructed pager TwiML contains forbidden verb ${forbidden}" >&2
    exit 6
  fi
done

# Dry run: TwiML + destination + redacted curl; no call, no creds needed.
if [[ "$DRY_RUN" == "1" ]]; then
  echo "$TWIML"
  echo "TO=${TO}"
  echo "CONFERENCE=${CONFERENCE}"
  echo "# curl -s -X POST https://api.twilio.com/2010-04-01/Accounts/<SID>/Calls.json \\"
  echo "#   --data-urlencode \"To=${TO}\" --data-urlencode \"From=<FROM>\" \\"
  echo "#   --data-urlencode \"Twiml=<above>\" -u \"<SID>:<REDACTED>\""
  exit 0
fi

# Per-event credentials from the caller WIN; global file is the manual fallback.
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
  echo "pager-leg origination FAILED:" >&2
  echo "$RESP" >&2
  exit 1
fi
echo "pager leg placed: CallSid=${SID} to=${TO} conference=${CONFERENCE}"
[[ -n "$SID_FILE" ]] && echo "$SID" > "$SID_FILE"

# Best-effort loud alert so the phone in a pocket gets a second channel.
# notify.sh may be absent on a stripped box — tolerate, never block the page.
if [[ -x "$(dirname "$0")/notify.sh" ]]; then
  "$(dirname "$0")/notify.sh" "OPERATOR PAGE: answer your cell NOW" \
    "Event ${EVENT_ID:-?}: operator screen. Answer ${TO}, handle the operator, then hang up. Conference ${CONFERENCE}, CallSid ${SID}." \
    || echo "warn: notify.sh failed (page still placed)" >&2
fi

# --wait-joined: the orchestrator's "never dial a bridge unmanned" primitive.
# in-progress = answered; queued/ringing = keep waiting; anything terminal
# (busy / no-answer / failed / canceled / completed) = the page did NOT land.
if [[ -n "$WAIT_JOINED" ]]; then
  DEADLINE=$(( $(date +%s) + WAIT_JOINED ))
  while :; do
    STATUS=$(curl -s "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Calls/${SID}.json" \
      -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}" \
      | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status",""))' || true)
    case "$STATUS" in
      in-progress)
        echo "pager ANSWERED (status=in-progress) — human is in the conference"
        exit 0 ;;
      busy|no-answer|failed|canceled|completed)
        echo "pager NOT joined: terminal status=${STATUS} — do NOT dial the bridge" >&2
        exit 7 ;;
    esac
    if (( $(date +%s) >= DEADLINE )); then
      echo "pager NOT answered within ${WAIT_JOINED}s (last status=${STATUS:-unknown}) — do NOT dial the bridge" >&2
      echo "  hang the page up with: scripts/twilio-hangup.sh ${SID}" >&2
      exit 7
    fi
    sleep 2
  done
fi
exit 0
