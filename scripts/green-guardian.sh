#!/usr/bin/env bash
# green-guardian.sh — guarantee an event is CLEARED-TO-ARM at fire time, or say
# loudly that it is not. Runs shortly BEFORE the watcher, unattended.
#
#   scripts/green-guardian.sh --event KXEARNINGSMENTION<T>-<DATE>-WEBCAST \
#                             --config configs/webcast-<t>-<date>-arm-wc.toml
#
# WHY (2026-08-06, during the CELH prep itself): improving a config the night
# before is normal and valuable -- widening weak keyword variants, retuning
# posture. But every such edit invalidates the readiness hash AND the preflight
# verdict, so between the edit and the finished re-study the event is RED. If
# the process doing that work dies in that window (it did: the background jobs
# were killed mid-study), the config is left red and assert-ready refuses at
# T-75. The event is then MISSED -- not by a bug in the runtime, but by an
# interrupted improvement.
#
# The fix is to make the safe state the DEFAULT state rather than something a
# live session has to remember to restore. This job re-checks the gate just
# before the watcher fires and, if it is red, restores the last known-green
# snapshot (state/<EVENT>/green-snapshot/) as an atomic SET.
#
# An improvement that completed and passed is never touched -- the snapshot is
# only consulted when the CURRENT state fails the gate.
#
# Always mails its verdict. Exit 0 = green (either already, or restored).
# Exit 1 = red and unrecoverable, operator must act.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

EVENT="" CONFIG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --event)  EVENT="${2:?}"; shift 2 ;;
    --config) CONFIG="${2:?}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$EVENT" && -n "$CONFIG" ]] || { echo "usage: green-guardian.sh --event <ID> --config <cfg>" >&2; exit 2; }

SNAP="state/$EVENT/green-snapshot"
BASE=$(basename "$CONFIG" .toml)

if OUT=$(scripts/assert-ready.sh "$CONFIG" 2>&1); then
  echo "GUARDIAN: already green — $OUT"
  scripts/notify.sh "$EVENT guardian OK" "Gate green at T-90, no action.
$OUT" --quiet || true
  exit 0
fi

echo "GUARDIAN: gate RED — $OUT" >&2

if [[ ! -f "$SNAP/config.toml" || ! -f "$SNAP/readiness.json" || ! -f "$SNAP/preflight-result.json" ]]; then
  scripts/notify.sh "$EVENT GUARDIAN CANNOT RECOVER" \
"Gate is RED and there is no complete green snapshot at $SNAP.
Gate said: $OUT
The watcher will refuse and this event will be MISSED unless you act." || true
  exit 1
fi

# Restore as a SET — a partial restore is its own hash mismatch.
cp "$SNAP/config.toml"           "$CONFIG"
cp "$SNAP/readiness.json"        "configs/.readiness/$BASE.json"
cp "$SNAP/preflight-result.json" "state/$EVENT/preflight-result.json"

if OUT2=$(scripts/assert-ready.sh "$CONFIG" 2>&1); then
  echo "GUARDIAN: RESTORED to green snapshot — $OUT2"
  scripts/notify.sh "$EVENT GUARDIAN RESTORED green" \
"The config was RED at T-90 and has been rolled back to the last verified-green
snapshot, so the event will arm.

was: $OUT
now: $OUT2

NOTE: any night-before keyword/posture improvement that was mid-flight has been
DISCARDED. The event runs the previously verified configuration." || true
  exit 0
fi

scripts/notify.sh "$EVENT GUARDIAN RESTORE FAILED" \
"Gate RED, snapshot restored, and it is STILL red:
$OUT2
This event will be MISSED unless you act." || true
exit 1
