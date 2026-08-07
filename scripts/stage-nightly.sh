#!/usr/bin/env bash
# The nightly staging arrow (scheduled 20:50 local, after slate-daily 20:00 and
# hunt-daily 20:30): merge hand-verified call times → stage → install tomorrow's
# T-0 agents. Every missing prerequisite PAGES TONIGHT — never silently at T-0.
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

# 1. Merge verified call times; page each still-untimed event for tomorrow.
if ! MERGE_OUT=$(python3 scripts/slate-time-merge.py --require-date "$DATE" 2>&1); then
  page "NEEDS-TIME for $DATE" "$MERGE_OUT
Resolve times into slate/call-times.json (two sources, never Kalshi) or descope-event.sh — TONIGHT, not at T-0."
  echo "$MERGE_OUT"
  # keep going: staging still proceeds for the timed events
  python3 scripts/slate-time-merge.py 2>/dev/null || true
fi

# 2. Stage everything the slate window covers (idempotent; ALREADY-STAGED skips).
if ! scripts/slate-stage.sh --slate slate/latest-timed.json; then
  page "STAGE ERRORS for $DATE" "slate-stage.sh exited nonzero — read its output; unstaged events will not arm."
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

# 4. Install tomorrow's T-0 agents (watchers + escalate ladders + probe pages
#    + liveness). Refusal inside prints LOAD-FAILED per agent.
if ! scripts/install-t0-agents.sh --date "$DATE"; then
  page "T0-AGENT INSTALL PROBLEMS for $DATE" "install-t0-agents.sh reported problems — run 'launchctl print gui/$(id -u)' and re-install by hand. A missing watcher is a missed event."
fi

# 5. PROVE tomorrow will actually fire. Everything above reports on what it
#    INTENDED to do; this is the only step that reads back what launchd
#    actually loaded.
#
#    This is the check that both lost days needed and did not have.
#    install-t0-agents.sh happily wrote plists whose calendar time had already
#    passed — launchd accepts them, `launchctl list` shows the label, runs=0,
#    and they fire in August 2027. A green install log said nothing.
#    night-before-sweep.sh was written on 2026-08-05 for exactly this and was
#    then scheduled by NOTHING, so it never ran on 08-06 or 08-07.
#
#    Read-only, so it is safe to run unconditionally; it never repairs, only
#    reports. Non-fatal to the staging run — the events are staged either way
#    and the operator needs the detail, not an abort.
if ! scripts/night-before-sweep.sh "$DATE"; then
  page "SLATE WILL NOT FIRE for $DATE" "night-before-sweep.sh found problems: a loaded launchd descriptor in the past, a missing pinned binary, a stale stream URL, or a red green-gate. Read its output — every FAIL is an event that will silently not arm."
fi

echo "stage-nightly done for $DATE"
