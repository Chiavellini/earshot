#!/usr/bin/env bash
# Nothing watches the watchers (2026-08-04: missed-arm-check was blind to
# events that captured nothing, and no process notices a dead/never-started
# watcher). This is the page-only fix: for every installed per-event watch
# agent whose fire time has passed but whose event window has not, verify an
# arm-on-url.sh process exists for that slug — page the diff.
#
#   scripts/watcher-liveness.sh --date 2026-08-05
# Page-only; zero blast radius. Scheduled at 10:30Z + 19:00Z on event days.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

DATE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --date) DATE="${2:?}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$DATE" ]] || { echo "usage: watcher-liveness.sh --date YYYY-MM-DD" >&2; exit 2; }
DP=$(python3 -c "
from datetime import date
d = date.fromisoformat('$DATE')
print(f\"{d.strftime('%y')}{d.strftime('%b').lower()}{d.strftime('%d')}\")")

MISSING=""
for plist in "$HOME"/Library/LaunchAgents/com.earshot.*-"$DP"-watch.plist; do
  [[ -f "$plist" ]] || continue
  label=$(basename "$plist" .plist)
  slug="${label#com.earshot.}"; slug="${slug%-$DP-watch}"
  # Fire time (local) from the plist; skip watchers not yet due.
  fire=$(python3 - "$plist" <<'PY'
import plistlib, sys
from datetime import datetime
d = plistlib.load(open(sys.argv[1], "rb")).get("StartCalendarInterval", {})
now = datetime.now()
fire = now.replace(hour=int(d.get("Hour", 0)), minute=int(d.get("Minute", 0)), second=0)
print("due" if now >= fire else "not-due")
PY
) || fire="unknown"
  [[ "$fire" == "due" ]] || continue
  if ! pgrep -f "arm-on-url.sh --slug $slug" >/dev/null 2>&1 \
     && ! pgrep -f "state/KXEARNINGSMENTION.*${slug^^}" >/dev/null 2>&1; then
    MISSING="${MISSING:+$MISSING, }$slug"
  fi
done

if [[ -n "$MISSING" ]]; then
  scripts/notify.sh "WATCHER DEAD: $MISSING" \
    "watch agents for [$MISSING] are due but no arm-on-url/arm process exists. Restart NOW: launchctl kickstart gui/$(id -u)/com.earshot.<slug>-$DP-watch — or run the watcher by hand. $(date -u +%FT%TZ)" || true
  echo "WATCHER-DEAD: $MISSING"
  exit 1
fi
echo "watcher-liveness: all due watchers alive ($(date -u +%FT%TZ))"
