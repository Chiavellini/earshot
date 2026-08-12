#!/usr/bin/env bash
# Operator-venue redial wrapper (control.operator). The redial supervisor
# invokes its --dial-script with twilio-dial.sh-style args; for a Class-B
# (operator-screened) venue a bare redial would present an unmanned silent leg
# to a live operator — the exact 21-73 s drop class. So: page the configured
# human cell back into the conference FIRST, then place the bridge call with
# the gated operator TwiML. A failed/unanswered page exits nonzero, which the
# supervisor treats as a failed replacement. The wrapper itself retries only
# the pager leg, with a hard bound and a proven hangup between attempts; bridge
# origination happens at most once and never unmanned.
#
# Config comes from $EARSHOT_OPERATOR_CONFIG (exported by launch-event.sh's
# --operator-handler branch) because the supervisor's pass-through args are
# dial-shaped and deliberately not extended.
set -euo pipefail
cd "$(dirname "$0")/.."

PAGE_SCRIPT="scripts/operator-page.sh"
BRIDGE_SCRIPT="scripts/twilio-operator-dial.sh"
PAGE_HANGUP_SCRIPT="scripts/twilio-hangup.sh"
# Keep the old 90-second total answer budget, but split it into two bounded
# chances: one unanswered ring was the live 2026-08-11 failure. A failed page
# is positively hung up before another is originated, so retry cannot create
# two pager legs. The bridge leg itself is still attempted exactly once.
PAGE_ATTEMPTS=2
PAGE_WAIT_SECS=45

[[ -n "${EARSHOT_OPERATOR_CONFIG:-}" && -f "${EARSHOT_OPERATOR_CONFIG}" ]] || {
  echo "operator-redial: EARSHOT_OPERATOR_CONFIG unset or missing — refusing" >&2
  exit 2
}
for helper in "$PAGE_SCRIPT" "$BRIDGE_SCRIPT" "$PAGE_HANGUP_SCRIPT"; do
  [[ -x "$helper" ]] || { echo "operator-redial: required helper missing/not executable: $helper" >&2; exit 2; }
done

# Pass-through dial args (same vocabulary the supervisor emits). --sid-file is
# extracted so the page leg gets its own sid file next to it.
SID_FILE=""
ARGS=("$@")
for ((i = 0; i < ${#ARGS[@]}; i++)); do
  [[ "${ARGS[$i]}" == "--sid-file" ]] && SID_FILE="${ARGS[$((i + 1))]:-}"
done
PAGE_SID_FILE="${SID_FILE:+$(dirname "$SID_FILE")/page.sid}"
PAGE_SID_FILE="${PAGE_SID_FILE:-/tmp/operator-redial-page.sid}"
STATE_DIR="$(dirname "$PAGE_SID_FILE")"

PAGE_JOINED=0
for ((page_attempt = 1; page_attempt <= PAGE_ATTEMPTS; page_attempt++)); do
  rm -f "$PAGE_SID_FILE"
  echo "operator-redial: paging the human before the bridge (attempt ${page_attempt}/${PAGE_ATTEMPTS}, wait ${PAGE_WAIT_SECS}s)"
  PAGE_RC=0
  "$PAGE_SCRIPT" --config "$EARSHOT_OPERATOR_CONFIG" --operator-i-am-paged \
    --sid-file "$PAGE_SID_FILE" --wait-joined "$PAGE_WAIT_SECS" || PAGE_RC=$?
  if [[ "$PAGE_RC" == 0 ]]; then
    PAGE_JOINED=1
    break
  fi

  # A timeout can leave a ringing page alive. Retrying without first proving
  # that leg closed would create two conference participants. No sid is an
  # ambiguous origination, and a failed hangup is ambiguous teardown: both are
  # terminal refusals, not reasons to ring again.
  PAGE_SID=$(head -1 "$PAGE_SID_FILE" 2>/dev/null || true)
  if [[ ! "$PAGE_SID" =~ ^CA[0-9a-fA-F]{32}$ ]]; then
    echo "operator-redial: page attempt ${page_attempt} failed (exit ${PAGE_RC}) without a valid CallSid — NOT retrying an ambiguous origination and NOT dialing the bridge" >&2
    exit 1
  fi
  if ! "$PAGE_HANGUP_SCRIPT" "$PAGE_SID"; then
    echo "operator-redial: could not prove failed page ${PAGE_SID} was hung up — NOT retrying and NOT dialing the bridge" >&2
    exit 1
  fi
  echo "operator-redial: failed page ${PAGE_SID} closed before any retry"
  if (( page_attempt < PAGE_ATTEMPTS )); then
    echo "operator-redial: retrying the configured cell now"
  fi
done
[[ "$PAGE_JOINED" == 1 ]] || {
  echo "operator-redial: configured cell was not answered after ${PAGE_ATTEMPTS} bounded attempts — NOT dialing the bridge unmanned" >&2
  exit 1
}

echo "operator-redial: after the screener admits this replacement leg and you hear the live issuer call, assert:"
echo "  scripts/operator-page.sh --config '$EARSHOT_OPERATOR_CONFIG' --operator-i-am-paged --assert-admitted --state-dir '$STATE_DIR'"

exec "$BRIDGE_SCRIPT" --config "$EARSHOT_OPERATOR_CONFIG" \
  --operator-i-am-paged "$@"
