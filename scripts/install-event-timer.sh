#!/usr/bin/env bash
# Linux counterpart of the per-event launchd plist: generate + arm a systemd
# USER timer/service pair that fires scripts/event-autorun.sh once, at the
# event's fire time, with the same env contract (EARSHOT_EVENT_ENV etc.).
#
#   scripts/install-event-timer.sh --event-env configs/<event>-autorun.env \
#       --fire-hhmm 0605 --date 2026-08-05 [--test] [--repo <dir>]
#
#   --test  adds EARSHOT_AUTORUN_TEST=1 (+ passes through any
#           EARSHOT_AUTORUN_TEST_* already exported) — rehearsal posture.
#
# Unit names come from the env file's LABEL (e.g. com.earshot.rehearsal →
# com.earshot.rehearsal.{service,timer}), so event-autorun.sh's Linux
# self-disarm (`systemctl --user disable --now "$LABEL.timer"`) matches.
#
# Requires lingering so user units run without a login session:
#   sudo loginctl enable-linger $USER        (checked below)
#
# Fire-time semantics match the plist: OnCalendar uses BOX-LOCAL time (UTC on
# the us-east box) — convert the event's local window yourself and re-verify
# with `systemctl --user list-timers` (preflight does this on Linux).
set -euo pipefail

EVENT_ENV="" FIRE_HHMM="" DATE="" TEST=0 REPO=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --event-env) EVENT_ENV="$2"; shift 2 ;;
    --fire-hhmm) FIRE_HHMM="$2"; shift 2 ;;
    --date)      DATE="$2"; shift 2 ;;
    --test)      TEST=1; shift ;;
    --repo)      REPO="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$EVENT_ENV" && -n "$FIRE_HHMM" && -n "$DATE" ]] || {
  echo "usage: $0 --event-env <env> --fire-hhmm HHMM --date YYYY-MM-DD [--test] [--repo <dir>]" >&2
  exit 2
}
[[ "$(uname -s)" == Linux ]] || { echo "Linux only — macOS uses the launchd plist" >&2; exit 2; }
[[ -f "$EVENT_ENV" ]] || { echo "event env not found: $EVENT_ENV" >&2; exit 2; }
REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
EVENT_ENV_ABS="$(cd "$(dirname "$EVENT_ENV")" && pwd)/$(basename "$EVENT_ENV")"

LABEL=$(source "$EVENT_ENV_ABS" 2>/dev/null; echo "${LABEL:-}")
[[ -n "$LABEL" ]] || { echo "no LABEL in $EVENT_ENV" >&2; exit 2; }

# Without lingering, user units die with the SSH session and timers never
# fire unattended — the exact silent failure this script exists to prevent.
if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
  echo "FAIL: lingering is OFF for $USER — user timers will not fire unattended." >&2
  echo "      run: sudo loginctl enable-linger $USER   and re-run this script." >&2
  exit 1
fi

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
ONCAL="$DATE ${FIRE_HHMM:0:2}:${FIRE_HHMM:2:2}:00"

TEST_LINES=""
if [[ "$TEST" == 1 ]]; then
  TEST_LINES="Environment=EARSHOT_AUTORUN_TEST=1"
  for v in EARSHOT_AUTORUN_TEST_CONFIG EARSHOT_AUTORUN_TEST_DIAL EARSHOT_AUTORUN_TEST_TO EARSHOT_AUTORUN_TEST_LONG_S; do
    [[ -n "${!v:-}" ]] && TEST_LINES+=$'\n'"Environment=$v=${!v}"
  done
fi

cat > "$UNIT_DIR/$LABEL.service" <<UNIT
[Unit]
Description=EARSHOT event-autorun ($LABEL)

[Service]
Type=oneshot
WorkingDirectory=$REPO
Environment=EARSHOT_EVENT_ENV=$EVENT_ENV_ABS
$TEST_LINES
ExecStart=$REPO/scripts/event-autorun.sh
# event-autorun handles SIGTERM itself (STOP semantics); give it room to
# cancel orders + write last words before systemd escalates to SIGKILL.
TimeoutStopSec=30
KillMode=mixed
UNIT

cat > "$UNIT_DIR/$LABEL.timer" <<UNIT
[Unit]
Description=EARSHOT fire timer ($LABEL)

[Timer]
OnCalendar=$ONCAL
AccuracySec=1s
# If the box was down/booting at fire time, fire as soon as possible after —
# same recovery posture as launchd's missed-job behavior.
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now "$LABEL.timer"
echo "armed: $LABEL.timer → $ONCAL (box-local)"
systemctl --user list-timers --all | grep -F "$LABEL" || true
echo "disarm: systemctl --user disable --now $LABEL.timer"
