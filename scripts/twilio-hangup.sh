#!/usr/bin/env bash
# Audio-side kill switch: terminate a live Twilio call leg.
#   twilio-hangup.sh <CallSid>
set -euo pipefail
SID="${1:?usage: twilio-hangup.sh <CallSid>}"
# shellcheck disable=SC1090
source ~/.twilio/earshot_env
: "${TWILIO_ACCOUNT_SID:?}" "${TWILIO_AUTH_TOKEN:?}"

curl -s -X POST "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Calls/${SID}.json" \
  --data-urlencode "Status=completed" \
  -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("status:", d.get("status", d))'
