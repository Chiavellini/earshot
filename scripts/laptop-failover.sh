#!/usr/bin/env bash
# laptop-failover.sh — box-primary / laptop-failover. Operator ruling 2026-08-19:
# "priority is capturing in the box with orders sent from the box; if the box
# cannot ingest, capture and work from the laptop — one or the other sends actual
# trades, never both."
#
# The laptop leg's T-75 arm job is WRAPPED so the laptop arms live ONLY if the box
# leg is not capturing. It watches the box the whole window, not once:
#   - box run reaches capture_ready              -> laptop stands down (keeps watching)
#   - box run/unit dies BEFORE the grace point   -> laptop takes over immediately
#   - box not capture_ready at start+grace       -> laptop takes over
#   - box was capturing then dies before deadline -> laptop takes over (late failover)
#   - box still capturing at the AccessPlan deadline -> laptop stands down for good
# Before the laptop arms it stops the box (STOP file + unit + timer) so only one host trades.
#
#   scripts/laptop-failover.sh --event E_LAPTOP --box-event E_BOX --box-run RUN_ID \
#       --start-utc HH:MM [--grace-min 2] [--deadline-utc HH:MM] [--box ubuntu@host] -- <argv...>
#   scripts/laptop-failover.sh --install --run-dir <laptop run-dir> --box-event E_BOX \
#       --box-run RUN_ID --start-utc HH:MM [--grace-min N] [--deadline-utc HH:MM] [--box ubuntu@host]
#   scripts/laptop-failover.sh --check
#
# --install reads the LOADED com.earshot.run.<plan_sha[:20]> plist that
# install-t0-agents.sh wrote for the laptop run, wraps its ProgramArguments with
# this script, bootout/bootstraps, reads the loaded job back, and records
# state/<E_LAPTOP>/failover-install.json. Run it AFTER `opsctl schedule`; do NOT
# re-run schedule / install-t0-agents --verify-only afterwards (the verify compares
# the loaded argv to the plan argv and would see the wrapper as a mismatch).
# Decision log: state/<E_LAPTOP>/failover.log. Pages on every outcome. Node: control.launch.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; REPO=$(cd "$REPO" && pwd -P); cd "$REPO"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
BOX="${EARSHOT_BOX:-ubuntu@3.212.174.94}"; KEY="${EARSHOT_BOX_KEY:-$HOME/.ssh/earshot-useast1.pem}"
MODE=live E="" EB="" BR="" START="" GRACE=2 DEADLINE="" RUNDIR="" CHECK=0
while [[ $# -gt 0 ]]; do case "$1" in
  --install) MODE=install; shift;;
  --run-dir) RUNDIR="$2"; shift 2;;
  --event) E="$2"; shift 2;; --box-event) EB="$2"; shift 2;; --box-run) BR="$2"; shift 2;;
  --start-utc) START="$2"; shift 2;; --grace-min) GRACE="$2"; shift 2;;
  --deadline-utc) DEADLINE="$2"; shift 2;; --box) BOX="$2"; shift 2;;
  --check) CHECK=1; shift;; --) shift; break;; *) echo "unknown arg $1" >&2; exit 2;; esac; done

decide_hhmm() { python3 -c 'import sys;h,m=map(int,sys.argv[1].split(":"));m+=int(sys.argv[2]);print(f"{(h+m//60)%24:02d}{m%60:02d}")' "$1" "$2"; }

if (( CHECK )); then
  ok=1
  [[ "$(decide_hhmm 12:00 2)" == "1202" ]] || ok=0
  [[ "$(decide_hhmm 12:59 2)" == "1301" ]] || ok=0
  [[ "$(decide_hhmm 23:50 15)" == "0005" ]] || ok=0   # deadline wraps past midnight UTC
  (( ok )) && { echo "SELFTEST PASS (3 checks)"; exit 0; } || { echo "SELFTEST FAIL"; exit 1; }
fi

# box_state -> "STATE unit-active"  (STATE: READY|LAUNCHED|NOTYET|DEAD|UNREACHABLE)
box_state() {
  ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 "$BOX" '
    R=~/ear/var/runs/'"$EB"'/'"$BR"'/control/receipts.jsonl
    P=~/ear/var/runs/'"$EB"'/'"$BR"'/control/run-plan.json
    sha=$(python3 -c "import json;print(json.load(open(\"$P\"))[\"plan_sha256\"][:20])" 2>/dev/null)
    ua=$(systemctl --user is-active "com.earshot.run.$sha.service" 2>/dev/null || echo unknown)
    if grep -q "\"transition\":\"capture_ready\"" "$R" 2>/dev/null; then echo "READY $ua"
    elif grep -qE "\"transition\":\"(failed|aborted)\"" "$R" 2>/dev/null; then echo "DEAD $ua"
    elif [ "$ua" = failed ]; then echo "DEAD $ua"
    elif grep -q "\"transition\":\"launched\"" "$R" 2>/dev/null; then echo "LAUNCHED $ua"
    else echo "NOTYET $ua"; fi' 2>/dev/null
}
stop_box() {
  ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 "$BOX" '
    P=~/ear/var/runs/'"$EB"'/'"$BR"'/control/run-plan.json
    sha=$(python3 -c "import json;print(json.load(open(\"$P\"))[\"plan_sha256\"][:20])" 2>/dev/null)
    mkdir -p ~/ear/state/'"$EB"' && touch ~/ear/state/'"$EB"'/STOP
    systemctl --user stop "com.earshot.run.$sha.timer" 2>/dev/null
    systemctl --user stop "com.earshot.run.$sha.service" 2>/dev/null
    echo box-stopped' 2>&1 | tail -1
}

if [[ "$MODE" == install ]]; then
  [[ -n "$RUNDIR" && -n "$EB" && -n "$BR" && -n "$START" ]] || { echo "USAGE-ERROR: --install needs --run-dir --box-event --box-run --start-utc" >&2; exit 2; }
  [[ -f "$RUNDIR/control/run-plan.json" ]] || { echo "REFUSED: $RUNDIR has no run-plan.json" >&2; exit 1; }
  read -r ELAP SHA20 < <(python3 -c 'import json,sys;p=json.load(open(sys.argv[1]+"/control/run-plan.json"));print(p["event_id"],p["plan_sha256"][:20])' "$RUNDIR")
  E="$ELAP"; LABEL="com.earshot.run.$SHA20"; PL="$HOME/Library/LaunchAgents/$LABEL.plist"
  [[ -f "$PL" ]] || { echo "REFUSED: no loaded plist $PL — run opsctl schedule first" >&2; exit 1; }
  # Re-render the plist with ProgramArguments wrapped by this script. Refuse if already wrapped.
  python3 - "$PL" "$REPO/scripts/laptop-failover.sh" "$E" "$EB" "$BR" "$START" "$GRACE" "$DEADLINE" "$BOX" <<'PY' || exit 1
import plistlib,sys
pl_path,self_,E,EB,BR,START,GRACE,DEADLINE,BOX=sys.argv[1:]
with open(pl_path,'rb') as f: d=plistlib.load(f)
argv=d.get("ProgramArguments",[])
if self_ in argv: sys.exit("REFUSED: plist ProgramArguments already wrapped by laptop-failover.sh")
wrap=["/bin/bash",self_,"--event",E,"--box-event",EB,"--box-run",BR,"--start-utc",START,"--grace-min",str(GRACE)]
if DEADLINE: wrap+=["--deadline-utc",DEADLINE]
wrap+=["--box",BOX,"--"]+argv
d["ProgramArguments"]=wrap
with open(pl_path,'wb') as f: plistlib.dump(d,f)
print("wrapped ProgramArguments:", " ".join(wrap[:12]),"… + original",len(argv),"args")
PY
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
  launchctl bootstrap "gui/$(id -u)" "$PL" || { echo "REFUSED: bootstrap failed for $LABEL" >&2; exit 1; }
  RB=$(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -o 'laptop-failover.sh\|"Hour" => [0-9]*\|"Minute" => [0-9]*\|"Day" => [0-9]*' | sort -u | tr '\n' ' ')
  echo "$RB" | grep -q laptop-failover.sh || { echo "REFUSED: readback does not show the wrapper in $LABEL" >&2; exit 1; }
  mkdir -p "state/$E"
  python3 -c 'import json,sys;json.dump({"label":sys.argv[1],"laptop_run":sys.argv[2],"box_event":sys.argv[3],"box_run":sys.argv[4],"start_utc":sys.argv[5],"grace_min":int(sys.argv[6]),"box":sys.argv[7]},open(sys.argv[8],"w"),indent=2)' "$LABEL" "$RUNDIR" "$EB" "$BR" "$START" "$GRACE" "$BOX" "state/$E/failover-install.json"
  echo "INSTALLED failover wrapper on $LABEL — readback: $RB"
  echo "  NOTE: do NOT re-run opsctl schedule / install-t0-agents --verify-only after this (the wrapper reads as an argv mismatch)."
  echo "  disarm: launchctl bootout gui/$(id -u)/$LABEL then re-run opsctl schedule --date"
  exit 0
fi

[[ -n "$E" && -n "$EB" && -n "$BR" && -n "$START" && $# -gt 0 ]] || { echo "USAGE-ERROR: need --event --box-event --box-run --start-utc and -- <argv>" >&2; exit 2; }
mkdir -p "state/$E"; LOG="state/$E/failover.log"; exec >>"$LOG" 2>&1
[[ -z "$DEADLINE" ]] && DEADLINE=$(python3 -c 'import sys;h,m=map(int,sys.argv[1].split(":"));m+=15;print(f"{(h+m//60)%24:02d}:{m%60:02d}")' "$START")
echo "=== failover $(date -u +%FT%TZ) laptop=$E box=$EB/$BR start=${START}Z grace=${GRACE}m deadline=${DEADLINE}Z argv=[$*]"
DECIDE=$(decide_hhmm "$START" "$GRACE"); DLINE="${DEADLINE/:/}"; FIRE="${START/:/}"; LAST=""
takeover() {
  echo "$(date -u +%T) DECISION: $1 — LAPTOP TAKES OVER"
  stop_box
  scripts/notify.sh "$E: BOX FAILED — laptop arming live" "$1 at $(date -u +%T)Z" >/dev/null 2>&1 || true
  echo "$(date -u +%T) exec: $*"
  exec "$@"
}
while :; do
  NOW=$(date -u +%H%M); read -r ST UA < <(box_state); [[ -z "$ST" ]] && { ST=UNREACHABLE; UA=unreachable; }
  [[ "$ST/$UA" != "$LAST" ]] && { echo "$(date -u +%T) box=$ST unit=$UA"; LAST="$ST/$UA"; }
  if [[ "$ST" == READY ]]; then
    # Keep watching until the AccessPlan deadline; a box that dies after going ready still fails over.
    if [[ "$NOW" > "$DLINE" || "$NOW" == "$DLINE" ]]; then
      echo "$(date -u +%T) BOX CAPTURED THROUGH THE DEADLINE — laptop stands down (no arm, no orders)"
      scripts/notify.sh "$E: box captured, laptop stood down" "box $EB/$BR held capture_ready to ${DEADLINE}Z" --quiet >/dev/null 2>&1 || true
      exit 0
    fi
    sleep 20; continue
  fi
  if [[ "$ST" == DEAD ]]; then takeover "box run/unit DEAD (unit=$UA)"; fi
  # box unit gone/failed after the timer should have fired = dead primary
  if [[ ( "$NOW" > "$FIRE" || "$NOW" == "$FIRE" ) && ( "$UA" == failed || "$UA" == inactive ) && "$ST" != LAUNCHED ]]; then
    takeover "box unit $UA past fire time, not capturing"
  fi
  if [[ "$NOW" > "$DECIDE" || "$NOW" == "$DECIDE" ]]; then takeover "box=$ST at start+${GRACE}m (UNREACHABLE counts as failed — capture beats the one-trader rule)"; fi
  sleep 20
done
