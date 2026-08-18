#!/usr/bin/env bash
# Daily missed-arm reconciliation (launchd: com.earshot.missed-arm, 17:30 local).
#
# Runs scripts/missed-arm-check.py for TODAY (UTC) and alerts on anything that
# is not a clean 0. Fires at 17:30 local (23:30Z) — after the last US afternoon
# calls end but before the UTC date rolls, so `date -u +%F` still names the
# event day being reconciled.
#
# The checker is read-only and writes nothing, so this wrapper owns the
# alerting (the 2026-07-28 PYPL/BA lesson: a check nobody hears is not a
# check). Exit contract of missed-arm-check.py: 0 clean, 1 at least one
# MISSED-ARM, 2 could not determine (fail-loud, never silently "no").
#
# KNOWN GAP (documented in docs/nodes/control-grade.md): events launched on
# the us-east-1 box leave their state/<event>/report.json on the box. Until
# that state is rsynced back, a laptop-side capture with box-side orders reads
# as a false MISSED-ARM, and box-side captures are invisible. After box event
# days: rsync the box's state/KXEARNINGSMENTION* down, then trust the check.
set -u
cd "$(dirname "$0")/.."

ALERTCTL="${EARSHOT_ALERTCTL:-scripts/alertctl.py}"
ALERT_DIR="${EARSHOT_ALERT_DIR:-state/alerts}"
PYTHON="${EARSHOT_PYTHON:-python3}"
CHECKER="${EARSHOT_MISSED_ARM_CHECK:-scripts/missed-arm-check.py}"
AUDIT_DAY="${EARSHOT_AUDIT_DAY:-scripts/audit-day.py}"
FILL_RECONCILE="${EARSHOT_FILL_RECONCILE:-scripts/fill-reconcile.py}"
AUDIT_DIR="${EARSHOT_AUDIT_DIR:-var/audits}"
DELIVERY_ARGS=(--phone-attempts 3)
[[ -n "${EARSHOT_NOTIFY:-}" ]] && DELIVERY_ARGS+=(--notify-adapter "$EARSHOT_NOTIFY")
[[ -n "${EARSHOT_ALERT_PHONE:-}" ]] && DELIVERY_ARGS+=(--phone-adapter "$EARSHOT_ALERT_PHONE")

critical_alert() { # critical_alert <code> <subject> <body>
  local code="$1" subject="$2" body="$3"
  if [[ ! -x "$ALERTCTL" ]]; then
    echo "ALERT REFUSED: alertctl unavailable at $ALERTCTL" >&2
    return 1
  fi
  "$ALERTCTL" raise --alert-dir "$ALERT_DIR" --scope "missed-arm:$DATE_UTC" \
    --severity critical --code "$code" --dedupe-key daily \
    --subject "$subject" --body "$body" "${DELIVERY_ARGS[@]}"
}

DATE_UTC="${EARSHOT_DATE_UTC:-$(date -u +%F)}"
OUT="$("$PYTHON" "$CHECKER" --date "$DATE_UTC" 2>&1)"
CODE=$?
echo "missed-arm-daily: $DATE_UTC exit=$CODE"
echo "$OUT"

case "$CODE" in
  0) ;; # clean — log only
  1) critical_alert missed_arm "MISSED-ARM on $DATE_UTC" "$OUT" \
       || echo "warn: proof-bearing MISSED-ARM page failed" >&2 ;;
  *) critical_alert missed_arm_undetermined \
       "missed-arm UNDETERMINED on $DATE_UTC (exit $CODE)" "$OUT" \
       || echo "warn: proof-bearing UNDETERMINED page failed" >&2 ;;
esac

# M6: the same launchd firing always advances from legacy missed-arm
# reconciliation to the typed date index. A failed first check does not hide
# the second result; each gets its own deterministic alert identity.
AUDIT_OUT="$("$PYTHON" "$AUDIT_DAY" --date "$DATE_UTC" 2>&1)"
AUDIT_CODE=$?
echo "audit-day-daily: $DATE_UTC exit=$AUDIT_CODE"
echo "$AUDIT_OUT"
if [[ "$AUDIT_CODE" -ne 0 ]]; then
  critical_alert audit_day_failed \
    "audit-day FAILED on $DATE_UTC (exit $AUDIT_CODE)" "$AUDIT_OUT" \
    || echo "warn: proof-bearing audit-day page failed" >&2
fi

# E15: both checks above read only what WE wrote about ourselves. This third
# leg asks the EXCHANGE what traded on our markets, because sweep-hft shares
# this Kalshi account and a fill it places on one of our boards is invisible to
# every earshot artifact (2026-08-18: HD-INFL 1×49¢, settled NO).
#
# Deliberately asymmetric alerting. Exit 1 means a fill is OURS and no run
# recorded it — a lost trade, and it pages. A foreign fill is expected on a
# shared account, so it is printed and banked in the JSON but never pages: a
# nightly alarm for a known-legitimate condition trains the operator to ignore
# the alarm that matters. Exit 2 (no credentials under launchd, or the account
# unreachable) warns and does not fail the job, but it also never reads as
# clean — the log line carries the exit code.
FILL_JSON="$AUDIT_DIR/fills-$DATE_UTC.json"
FILL_OUT="$("$PYTHON" "$FILL_RECONCILE" --date "$DATE_UTC" --json "$FILL_JSON" 2>&1)"
FILL_CODE=$?
echo "fill-reconcile-daily: $DATE_UTC exit=$FILL_CODE"
echo "$FILL_OUT"
case "$FILL_CODE" in
  0) ;;
  1) critical_alert ledger_unrecorded_fill \
       "UNRECORDED FILL on $DATE_UTC — the account traded and our ledger did not record it" \
       "$FILL_OUT" || echo "warn: proof-bearing unrecorded-fill page failed" >&2 ;;
  *) echo "warn: fill-reconcile could not determine the account's side on $DATE_UTC (exit $FILL_CODE)" >&2 ;;
esac

if [[ "$CODE" -ne 0 ]]; then
  exit "$CODE"
fi
if [[ "$AUDIT_CODE" -ne 0 ]]; then
  exit "$AUDIT_CODE"
fi
[[ "$FILL_CODE" -eq 1 ]] && exit 1
exit 0
