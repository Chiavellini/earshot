#!/usr/bin/env bash
# The nightly staging arrow (scheduled 20:50 local, after slate-daily 20:00 and
# hunt-daily 20:30): merge hand-verified call times → stage → compile immutable
# RunPlans. Every missing prerequisite PAGES TONIGHT — never silently at T-0.
# Scheduling deliberately happens later, after typed preflight, through
# scripts/schedule-nightly.sh. eventctl correctly forbids `scheduled` before
# `preflight_passed`; installing jobs here used to create an unprovable third
# state (loaded job, red/missing preflight, no lifecycle receipt).
#
#   scripts/stage-nightly.sh [--date YYYY-MM-DD]   (default: tomorrow UTC)
#
# Chain invariant (docs/runbooks/): every arrow is either a scheduled job or a
# FAIL-page in the next scheduled job. This is the arrow that was missing on
# 2026-08-04 (slate-stage.sh existed since 08-01, scheduled by nothing).
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"
page() { scripts/notify.sh "$1" "$2" --quiet || true; }

DATE="${2:-}"
[[ "${1:-}" == "--date" && -n "$DATE" ]] || DATE=$(python3 -c "
from datetime import date, timedelta
print(date.today() + timedelta(days=1))")
PROBLEMS=0

# 1. Merge verified call times; page each still-untimed event for tomorrow.
if ! MERGE_OUT=$(python3 scripts/slate-time-merge.py --require-date "$DATE" 2>&1); then
  page "NEEDS-TIME for $DATE" "$MERGE_OUT
Resolve times into slate/call-times.json (two sources, never Kalshi) or descope-event.sh — TONIGHT, not at T-0."
  echo "$MERGE_OUT"
  PROBLEMS=$((PROBLEMS + 1))
  # keep going: staging still proceeds for the timed events
  python3 scripts/slate-time-merge.py 2>/dev/null || true
fi

# 2. Stage everything the slate window covers (idempotent; ALREADY-STAGED skips).
if ! scripts/slate-stage.sh --slate slate/latest-timed.json; then
  page "STAGE ERRORS for $DATE" "slate-stage.sh exited nonzero — read its output; unstaged events will not arm."
  PROBLEMS=$((PROBLEMS + 1))
fi

# 2.5 Compile the immutable run plans for the staged events (typed lifecycle
#     ledger, control.run.plan). Parses configs/envs — never filename shapes —
#     and refuses placeholders, ambiguity, and host-absolute env paths. Never
#     fatal to staging: a refused event pages TONIGHT and stays visible.
IDENTITY_BIN="${EARSHOT_BIN:-$REPO/target/release/earshot}"
if ! python3 scripts/eventctl.py init-date --date "$DATE" --earshot-bin "$IDENTITY_BIN"; then
  page "RUN-PLAN INIT PROBLEMS for $DATE" "eventctl.py init-date reported refusals — read its output; an event without a run plan has no typed lifecycle ledger for tomorrow. Besides placeholder/ambiguity/path failures, provision each referenced account once with scripts/execution-profile.sh configure --account '\$HOME/.earshot/<account>.env' --host-id <stable-target-id> --bin '$IDENTITY_BIN'. The plan compiler re-derives that account's exact local signer and never guesses the laptop hostname for the box."
  PROBLEMS=$((PROBLEMS + 1))
fi

# 3. Unprobed phone venues on tomorrow's slate → page (the probe is a human
#    decision + a $0.02 call; the page carries the exact command).
DP=$(python3 -c "
from datetime import date
d = date.fromisoformat('$DATE')
print(f\"{d.strftime('%y')}{d.strftime('%b').lower()}{d.strftime('%d')}\")")
UNPROBED=""
for cfg in configs/twilio-*-"$DP"-arm.toml; do
  [[ -f "$cfg" ]] || continue
  slug=$(basename "$cfg" .toml); slug="${slug#twilio-}"; slug="${slug%-$DP-arm}"
  UNPROBED="${UNPROBED:+$UNPROBED }$slug"
done
# (a staged twilio config means admission was held at stage time — freshness
# is preflight's job; this page covers venues hunted AFTER staging)

# The loaded-job readback and future-fire proof now live in
# schedule-nightly.sh, after preflight. Running the legacy loaded-job sweep here
# would call every intentionally-not-yet-scheduled run a miss.
echo "stage-nightly done for $DATE; problems=$PROBLEMS; next typed arrow: study/preflight -> schedule-nightly.sh"
[[ "$PROBLEMS" -eq 0 ]] || exit 1
