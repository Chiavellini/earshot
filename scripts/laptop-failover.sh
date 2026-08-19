#!/usr/bin/env bash
# laptop-failover.sh — wrap the LAPTOP leg's T-75 arm job so the laptop arms live ONLY if the
# BOX leg (primary) has not reached capture_ready by start+<grace> min, and then stops the box
# leg first. Operator ruling 2026-08-19: "priority is capturing in the box with orders sent from
# the box; if the box cannot ingest, capture and work from the laptop — one or the other sends
# actual trades, never both."
#
#   scripts/laptop-failover.sh --event <E_LAPTOP> --box-event <E_BOX> --box-run <RUN_ID> \
#       --start-utc HH:MM [--grace-min 2] [--box ubuntu@host] -- <arm-on-url argv...>
#   scripts/laptop-failover.sh --check
#
# Installed by wrapping the laptop plan's launchd ProgramArguments
# (scripts/install-t0-agents.sh output): ['/bin/bash', this, ...flags, '--', <original argv>].
# Decision log: state/<E_LAPTOP>/failover.log. Pages on both outcomes.
# Exclusivity: before the laptop arms it touches state/<E_BOX>/STOP on the box (the per-event
# kill switch the arm honours) and stops the box unit; a box that comes up later finds STOP.
# A box that is UNREACHABLE at the decision counts as failed. Node: control.launch.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; REPO=$(cd "$REPO" && pwd -P); cd "$REPO"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
BOX="${EARSHOT_BOX:-ubuntu@3.212.174.94}"; KEY="${EARSHOT_BOX_KEY:-$HOME/.ssh/earshot-useast1.pem}"
E="" EB="" BR="" START="" GRACE=2 CHECK=0
while [[ $# -gt 0 ]]; do case "$1" in
  --event) E="$2"; shift 2;; --box-event) EB="$2"; shift 2;; --box-run) BR="$2"; shift 2;;
  --start-utc) START="$2"; shift 2;; --grace-min) GRACE="$2"; shift 2;; --box) BOX="$2"; shift 2;;
  --check) CHECK=1; shift;; --) shift; break;; *) echo "unknown arg $1" >&2; exit 2;; esac; done
decide_hhmm() { python3 -c 'import sys;h,m=map(int,sys.argv[1].split(":"));m+=int(sys.argv[2]);print(f"{(h+m//60)%24:02d}{m%60:02d}")' "$1" "$2"; }
if (( CHECK )); then
  [[ "$(decide_hhmm 12:00 2)" == "1202" && "$(decide_hhmm 12:59 2)" == "1301" ]] && { echo "SELFTEST PASS (2 checks)"; exit 0; } || { echo "SELFTEST FAIL"; exit 1; }
fi
[[ -n "$E" && -n "$EB" && -n "$BR" && -n "$START" && $# -gt 0 ]] || { echo "USAGE-ERROR: need --event --box-event --box-run --start-utc and -- <argv>" >&2; exit 2; }
mkdir -p "state/$E"; LOG="state/$E/failover.log"; exec >>"$LOG" 2>&1
echo "=== failover $(date -u +%FT%TZ) laptop=$E box=$EB/$BR start=${START}Z grace=${GRACE}m argv=[$*]"
box_state() { ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 "$BOX" \
  "R=~/ear/var/runs/$EB/$BR/control/receipts.jsonl; grep -q '\"transition\":\"capture_ready\"' \$R 2>/dev/null && echo READY || { grep -q '\"transition\":\"launched\"' \$R 2>/dev/null && echo LAUNCHED || echo NOTYET; }" 2>/dev/null; }
DECIDE=$(decide_hhmm "$START" "$GRACE"); LAST=""
while :; do
  NOW=$(date -u +%H%M); ST=$(box_state); [[ -z "$ST" ]] && ST=UNREACHABLE
  [[ "$ST" != "$LAST" ]] && { echo "$(date -u +%T) box=$ST"; LAST="$ST"; }
  if [[ "$ST" == READY ]]; then
    echo "$(date -u +%T) BOX IS CAPTURING — laptop stands down (no arm, no orders)"
    scripts/notify.sh "$E: box capturing, laptop standing down" "box $EB/$BR reached capture_ready" --quiet >/dev/null 2>&1 || true; exit 0
  fi
  if [[ "$NOW" > "$DECIDE" || "$NOW" == "$DECIDE" ]]; then echo "$(date -u +%T) DECISION: box=$ST at start+${GRACE} — LAPTOP TAKES OVER"; break; fi
  sleep 20
done
ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 "$BOX" "mkdir -p ~/ear/state/$EB && touch ~/ear/state/$EB/STOP; systemctl --user stop 'com.earshot.run.*.service' 2>/dev/null; echo box-stopped" 2>&1 | tail -1
scripts/notify.sh "$E: BOX FAILED ($ST) — laptop arming live" "laptop arm-on-url starting at $(date -u +%T)Z" >/dev/null 2>&1 || true
echo "$(date -u +%T) exec: $*"
exec "$@"
