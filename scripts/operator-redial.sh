#!/usr/bin/env bash
# Operator-venue redial wrapper (control.operator). The redial supervisor
# invokes its --dial-script with twilio-dial.sh-style args; for a Class-B
# (operator-screened) venue a bare redial would present an unmanned silent leg
# to a live operator — the exact 21-73 s drop class. So: page the configured
# human cell back into the conference FIRST, then place the bridge call with
# the gated operator TwiML. A failed/unanswered page exits nonzero, which the
# supervisor treats as a failed dial (backoff, retry next marker/poll) — the
# bridge is never dialed unmanned.
#
# Config comes from $EARSHOT_OPERATOR_CONFIG (exported by launch-event.sh's
# --operator-handler branch) because the supervisor's pass-through args are
# dial-shaped and deliberately not extended.
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -n "${EARSHOT_OPERATOR_CONFIG:-}" && -f "${EARSHOT_OPERATOR_CONFIG}" ]] || {
  echo "operator-redial: EARSHOT_OPERATOR_CONFIG unset or missing — refusing" >&2
  exit 2
}

# Pass-through dial args (same vocabulary the supervisor emits). --sid-file is
# extracted so the page leg gets its own sid file next to it.
SID_FILE=""
ARGS=("$@")
for ((i = 0; i < ${#ARGS[@]}; i++)); do
  [[ "${ARGS[$i]}" == "--sid-file" ]] && SID_FILE="${ARGS[$((i + 1))]:-}"
done
PAGE_SID_FILE="${SID_FILE:+$(dirname "$SID_FILE")/page.sid}"
PAGE_SID_FILE="${PAGE_SID_FILE:-/tmp/operator-redial-page.sid}"

echo "operator-redial: re-paging the human before re-dialing the bridge"
scripts/operator-page.sh --config "$EARSHOT_OPERATOR_CONFIG" --operator-i-am-paged \
  --sid-file "$PAGE_SID_FILE" --wait-joined 90 || {
  echo "operator-redial: page not answered — NOT dialing the bridge unmanned" >&2
  exit 1
}

exec scripts/twilio-operator-dial.sh --config "$EARSHOT_OPERATOR_CONFIG" \
  --operator-i-am-paged "$@"
