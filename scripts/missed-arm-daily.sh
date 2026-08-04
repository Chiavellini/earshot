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

DATE_UTC="$(date -u +%F)"
OUT="$(python3 scripts/missed-arm-check.py --date "$DATE_UTC" 2>&1)"
CODE=$?
echo "missed-arm-daily: $DATE_UTC exit=$CODE"
echo "$OUT"

case "$CODE" in
  0) ;; # clean — log only
  1) scripts/notify.sh "MISSED-ARM on $DATE_UTC" "$OUT" --quiet \
       || echo "warn: notify.sh failed" >&2 ;;
  *) scripts/notify.sh "missed-arm UNDETERMINED on $DATE_UTC (exit $CODE)" "$OUT" --quiet \
       || echo "warn: notify.sh failed" >&2 ;;
esac
exit "$CODE"
