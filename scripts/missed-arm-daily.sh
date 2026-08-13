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

DATE_UTC="$(date -u +%F)"
OUT="$(python3 scripts/missed-arm-check.py --date "$DATE_UTC" 2>&1)"
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
exit "$CODE"
