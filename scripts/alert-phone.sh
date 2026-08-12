#!/usr/bin/env bash
# Bounded critical-alert phone fallback. This calls only the operator number
# provisioned outside the repository in ~/.kalshi/alert_env; it never calls an
# event bridge, conferences another leg, plays DTMF, records, or auto-acks.
#
# Required external variables:
#   ALERT_TO_NUMBER, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER
#
# Exit 0: Twilio accepted one exact alert call and returned a CallSid.
# Exit 1: Twilio definitely refused or returned an invalid response.
# Exit 75: transport outcome is ambiguous; alertctl records it and may retry
#          only as a separately numbered delivery attempt.
set -euo pipefail

ALERT_ID="" SUBJECT="" BODY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --alert-id) ALERT_ID="$2"; shift 2 ;;
    --subject) SUBJECT="$2"; shift 2 ;;
    --body) BODY="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$ALERT_ID" =~ ^[0-9a-f]{64}$ && -n "$SUBJECT" && -n "$BODY" ]] || {
  echo "usage: alert-phone.sh --alert-id 64hex --subject text --body text" >&2
  exit 2
}

ALERT_ENV="${EARSHOT_ALERT_ENV:-$HOME/.kalshi/alert_env}"
[[ -f "$ALERT_ENV" && ! -L "$ALERT_ENV" ]] || {
  echo "alert phone configuration is missing or unsafe: $ALERT_ENV" >&2
  exit 1
}
# shellcheck disable=SC1090
source "$ALERT_ENV"
: "${ALERT_TO_NUMBER:?}" "${TWILIO_ACCOUNT_SID:?}" "${TWILIO_AUTH_TOKEN:?}" "${TWILIO_FROM_NUMBER:?}"
[[ "$ALERT_TO_NUMBER" =~ ^\+[1-9][0-9]{7,14}$ ]] || {
  echo "ALERT_TO_NUMBER is not strict E.164" >&2
  exit 1
}

# Text is escaped by Python; only <Say> + <Pause> are emitted. This is a page
# to the user's configured phone, never a two-way bridge control path.
TWIML=$(python3 - "$SUBJECT" "$BODY" "$ALERT_ID" <<'PY'
import html, sys
subject, body, alert_id = sys.argv[1:]
spoken = f"Earshot critical alert. {subject}. {body[:600]}. Acknowledge alert I D {alert_id[:12]} in ops control."
print(f'<Response><Say>{html.escape(spoken)}</Say><Pause length="2"/></Response>')
PY
)

set +e
RESPONSE=$(curl -sS --max-time 20 -w '\n%{http_code}' -X POST \
  "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Calls.json" \
  --data-urlencode "To=${ALERT_TO_NUMBER}" \
  --data-urlencode "From=${TWILIO_FROM_NUMBER}" \
  --data-urlencode "Twiml=${TWIML}" \
  -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}" 2>&1)
CURL_RC=$?
set -e
if [[ "$CURL_RC" -ne 0 ]]; then
  echo "Twilio alert-call transport ambiguous (curl rc=$CURL_RC)" >&2
  exit 75
fi
HTTP=${RESPONSE##*$'\n'}
JSON=${RESPONSE%$'\n'*}
if [[ "$HTTP" =~ ^2[0-9][0-9]$ ]]; then
  SID=$(python3 -c 'import json,sys; v=json.load(sys.stdin); print(v.get("sid", ""))' <<<"$JSON" 2>/dev/null || true)
  [[ "$SID" =~ ^CA[0-9a-fA-F]{32}$ ]] || {
    echo "Twilio accepted alert call but returned no valid CallSid" >&2
    exit 75
  }
  # Provider acceptance is not human delivery. Poll until the call was
  # answered/connected, definitely failed, or becomes ambiguous at the bound.
  for _ in $(seq 1 "${EARSHOT_ALERT_PHONE_POLLS:-15}"); do
    sleep "${EARSHOT_ALERT_PHONE_POLL_SECS:-2}"
    set +e
    STATUS_JSON=$(curl -sS --max-time 10 \
      "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Calls/${SID}.json" \
      -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}" 2>&1)
    STATUS_RC=$?
    set -e
    [[ "$STATUS_RC" -eq 0 ]] || {
      echo "Twilio accepted CallSid=$SID but status lookup is ambiguous" >&2
      exit 75
    }
    STATUS=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("status", ""))' <<<"$STATUS_JSON" 2>/dev/null || true)
    case "$STATUS" in
      in-progress|completed)
        echo "critical alert phone answered CallSid=$SID status=$STATUS"
        exit 0
        ;;
      busy|failed|no-answer|canceled)
        echo "critical alert phone not delivered CallSid=$SID status=$STATUS" >&2
        exit 1
        ;;
      queued|ringing) ;;
      *)
        echo "Twilio alert call status is unknown for CallSid=$SID" >&2
        exit 75
        ;;
    esac
  done
  echo "Twilio alert call remained queued/ringing at poll bound CallSid=$SID" >&2
  exit 75
fi
echo "Twilio alert call refused (HTTP ${HTTP:-unknown})" >&2
exit 1
