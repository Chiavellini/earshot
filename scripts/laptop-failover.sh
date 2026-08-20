#!/usr/bin/env bash
# laptop-failover.sh — box-primary / laptop-failover. Operator ruling 2026-08-19:
# "priority is capturing in the box with orders sent from the box; if the box
# cannot ingest, capture and work from the laptop — one or the other sends actual
# trades, never both."
#
# The laptop leg's T-75 arm job is WRAPPED so the laptop arms live ONLY if the box
# leg is not capturing. It watches the box the whole window, not once:
#   - box run reaches supervising with live unit -> laptop stands down (keeps watching)
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
#   scripts/laptop-failover.sh --verify-install --run-dir <laptop run-dir>
#   scripts/laptop-failover.sh --check
#
# --install reads the LOADED com.earshot.run.<plan_sha[:20]> plist that
# install-t0-agents.sh wrote for the laptop run, wraps its ProgramArguments with
# this script, bootout/bootstraps, reads the loaded job back, and records
# state/<E_LAPTOP>/failover-install.json. Run it AFTER `opsctl schedule`.
# A later schedule pass detects that proof and uses --verify-install to bind the
# wrapper prefix and its original scheduled launcher tail without rewrapping.
# Decision log: state/<E_LAPTOP>/failover.log. Pages on every outcome. Node: control.launch.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; REPO=$(cd "$REPO" && pwd -P); cd "$REPO"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
BOX="${EARSHOT_BOX:-ubuntu@3.212.174.94}"; KEY="${EARSHOT_BOX_KEY:-$HOME/.ssh/earshot-useast1.pem}"
SSH_BIN="${EARSHOT_SSH:-ssh}"
LAUNCHCTL_BIN="${EARSHOT_LAUNCHCTL:-launchctl}"
MODE=live E="" EB="" BR="" START="" GRACE=2 DEADLINE="" RUNDIR="" CHECK=0
while [[ $# -gt 0 ]]; do case "$1" in
  --install) MODE=install; shift;;
  --verify-install) MODE=verify-install; shift;;
  --run-dir) RUNDIR="$2"; shift 2;;
  --event) E="$2"; shift 2;; --box-event) EB="$2"; shift 2;; --box-run) BR="$2"; shift 2;;
  --start-utc) START="$2"; shift 2;; --grace-min) GRACE="$2"; shift 2;;
  --deadline-utc) DEADLINE="$2"; shift 2;; --box) BOX="$2"; shift 2;;
  --check) CHECK=1; shift;; --) shift; break;; *) echo "unknown arg $1" >&2; exit 2;; esac; done

decide_hhmm() { python3 -c 'import sys;h,m=map(int,sys.argv[1].split(":"));m+=int(sys.argv[2]);print(f"{(h+m//60)%24:02d}{m%60:02d}")' "$1" "$2"; }
classify_box_observation() { # capture_ready_count supervising_count terminal_count unit_state
  local ready="$1" supervising="$2" terminal="$3" unit="$4"
  if [[ "$terminal" =~ ^[1-9][0-9]*$ || "$unit" == failed ]]; then
    printf 'DEAD'
  elif [[ "$supervising" =~ ^[1-9][0-9]*$ ]]; then
    if [[ "$unit" == active || "$unit" == activating ]]; then printf 'READY'; else printf 'DEAD'; fi
  elif [[ "$ready" =~ ^[1-9][0-9]*$ ]]; then
    # URL-level PCM was proven, but the exact runtime source has not attached.
    # Keep the grace clock running; this state must never stand the laptop down.
    printf 'LAUNCHED'
  else
    printf 'NOTYET'
  fi
}
exec_takeover_argv() { # reason, exact launcher argv...
  local reason="$1"
  shift
  [[ $# -gt 0 ]] || {
    echo "REFUSED: failover takeover has no launcher argv (reason=$reason)" >&2
    return 2
  }
  exec "$@"
}

if (( CHECK )); then
  ok=1
  [[ "$(decide_hhmm 12:00 2)" == "1202" ]] || ok=0
  [[ "$(decide_hhmm 12:59 2)" == "1301" ]] || ok=0
  [[ "$(decide_hhmm 23:50 15)" == "0005" ]] || ok=0   # deadline wraps past midnight UTC
  [[ "$(classify_box_observation 1 1 0 activating)" == READY ]] || ok=0
  [[ "$(classify_box_observation 1 0 0 activating)" == LAUNCHED ]] || ok=0
  [[ "$(classify_box_observation 1 1 0 inactive)" == DEAD ]] || ok=0
  [[ "$(classify_box_observation 1 1 1 active)" == DEAD ]] || ok=0
  marker=$(mktemp "${TMPDIR:-/tmp}/earshot-failover-argv.XXXXXX") || exit 1
  rm -f -- "$marker"
  (exec_takeover_argv "fixture reason" /bin/sh -c 'printf forwarded > "$1"' _ "$marker") || ok=0
  [[ "$(cat "$marker" 2>/dev/null)" == forwarded ]] || ok=0
  rm -f -- "$marker"
  if (exec_takeover_argv "missing command" >/dev/null 2>&1); then ok=0; fi
  # Exercise the real decision loop, both SSH boundaries, and final exec with
  # an inert primary observation. This is the regression the pure argv split
  # alone cannot catch: callers once passed only the English reason string.
  check_root=$(mktemp -d "${TMPDIR:-/tmp}/earshot-failover-check.XXXXXX") || exit 1
  cleanup_check() { rm -rf -- "$check_root"; }
  trap cleanup_check EXIT
  mkdir -p "$check_root/repo/scripts" "$check_root/bin"
  cp "$0" "$check_root/repo/scripts/laptop-failover.sh"
  cat > "$check_root/repo/scripts/notify.sh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  cat > "$check_root/bin/ssh" <<'SH'
#!/usr/bin/env bash
count=$(cat "$EARSHOT_FAILOVER_STUB_STATE" 2>/dev/null || echo 0)
count=${count:-0}
if [[ "$count" == 0 ]]; then
  printf '1\n' > "$EARSHOT_FAILOVER_STUB_STATE"
  echo '1 0 0 inactive'
else
  echo box-stopped
fi
SH
  cat > "$check_root/bin/launchctl" <<'SH'
#!/usr/bin/env bash
if [[ "$1" == print ]]; then
  python3 - "$EARSHOT_FAILOVER_CHECK_PLIST" <<'PY'
import plistlib, sys
from pathlib import Path
path = sys.argv[1]
with open(path, "rb") as handle:
    value = plistlib.load(handle)
print(f"path = {Path(path).resolve()}")
print(f"working directory = {value['WorkingDirectory']}")
print("arguments = {")
for token in value["ProgramArguments"]:
    print(f"    {token}")
print("}")
for key, item in value["EnvironmentVariables"].items():
    print(f'"{key}" => "{item}"')
for key, item in value["StartCalendarInterval"].items():
    print(f'"{key}" => {item}')
PY
fi
exit 0
SH
  chmod +x "$check_root/repo/scripts/"*.sh "$check_root/bin/"*
  now_hm=$(date -u +%H:%M)
  : > "$check_root/ssh-count"
  EARSHOT_REPO="$check_root/repo" EARSHOT_SSH="$check_root/bin/ssh" \
    EARSHOT_FAILOVER_STUB_STATE="$check_root/ssh-count" \
    "$check_root/repo/scripts/laptop-failover.sh" \
      --event CHECK-LAPTOP --box-event CHECK-BOX --box-run run-check \
      --start-utc "$now_hm" --grace-min 0 --deadline-utc "$now_hm" \
      -- /bin/sh -c 'printf takeover-ok > "$1"' _ "$check_root/takeover.marker" \
      || ok=0
  [[ "$(cat "$check_root/takeover.marker" 2>/dev/null)" == takeover-ok ]] || ok=0
  grep -q 'LAPTOP TAKES OVER' "$check_root/repo/state/CHECK-LAPTOP/failover.log" 2>/dev/null || ok=0
  # The support stage is retryable: the same exact wrapper verifies cleanly
  # instead of nesting another wrapper, while any different identity refuses.
  install_run="$check_root/repo/var/runs/CHECK-INSTALL/run-1"
  install_home="$check_root/home"
  install_repo="$(cd "$check_root/repo" && pwd -P)"
  install_script="$install_repo/scripts/laptop-failover.sh"
  mkdir -p "$install_run/control" "$install_home/Library/LaunchAgents"
  python3 - "$install_run/control/run-plan.json" "$install_run/control/schedule-proof.json" "$install_home/Library/LaunchAgents/com.earshot.run.bbbbbbbbbbbbbbbbbbbb.plist" "$install_repo" "$install_run" <<'PY'
import json, plistlib, sys
plan_path, proof_path, plist_path, repo, run = sys.argv[1:]
plan = {"event_id":"CHECK-INSTALL","run_id":"run-1","plan_sha256":"b"*64,
        "starts_at_utc":"2026-08-20T12:00:00Z"}
json.dump(plan, open(plan_path,"w"))
argv = ["/bin/echo","launch"]
environment = {"EARSHOT_REPO":repo,"EARSHOT_RUN_DIR":run}
calendar = {"Month":8,"Day":20,"Hour":11,"Minute":58}
plistlib.dump({"Label":"com.earshot.run."+"b"*20,"ProgramArguments":argv,
               "WorkingDirectory":repo,"EnvironmentVariables":environment,
               "StartCalendarInterval":calendar,"RunAtLoad":False,
               "ProcessType":"Background"}, open(plist_path,"wb"))
json.dump({"readback":{"program_arguments":argv,
                        "environment":environment,
                        "fire_at_utc":"2026-08-20T11:58:00Z"}}, open(proof_path,"w"))
PY
  for attempt in 1 2; do
    HOME="$install_home" EARSHOT_REPO="$check_root/repo" \
      EARSHOT_LAUNCHCTL="$check_root/bin/launchctl" \
      EARSHOT_FAILOVER_CHECK_PLIST="$install_home/Library/LaunchAgents/com.earshot.run.bbbbbbbbbbbbbbbbbbbb.plist" \
      TZ=UTC \
      "$check_root/repo/scripts/laptop-failover.sh" --install \
        --run-dir "$install_run" --box-event CHECK-BOX --box-run run-box \
        --start-utc 12:00 --grace-min 2 --deadline-utc 12:15 --box fixture-box \
        >/dev/null || ok=0
  done
  if HOME="$install_home" EARSHOT_REPO="$check_root/repo" \
    EARSHOT_LAUNCHCTL="$check_root/bin/launchctl" \
    EARSHOT_FAILOVER_CHECK_PLIST="$install_home/Library/LaunchAgents/com.earshot.run.bbbbbbbbbbbbbbbbbbbb.plist" \
    TZ=UTC \
    "$check_root/repo/scripts/laptop-failover.sh" --install \
      --run-dir "$install_run" --box-event CHECK-BOX --box-run wrong-run \
      --start-utc 12:00 --grace-min 2 --deadline-utc 12:15 --box fixture-box \
      >/dev/null 2>&1; then ok=0; fi
  HOME="$install_home" EARSHOT_REPO="$check_root/repo" \
    EARSHOT_LAUNCHCTL="$check_root/bin/launchctl" \
    EARSHOT_FAILOVER_CHECK_PLIST="$install_home/Library/LaunchAgents/com.earshot.run.bbbbbbbbbbbbbbbbbbbb.plist" \
    TZ=UTC \
    "$check_root/repo/scripts/laptop-failover.sh" --verify-install \
      --run-dir "$install_run" >/dev/null || ok=0
  actual_count=$(python3 - "$install_home/Library/LaunchAgents/com.earshot.run.bbbbbbbbbbbbbbbbbbbb.plist" "$install_script" <<'PY'
import plistlib, sys
print(plistlib.load(open(sys.argv[1],"rb"))["ProgramArguments"].count(sys.argv[2]))
PY
  )
  [[ "$actual_count" == 1 ]] || ok=0
  cleanup_check
  trap - EXIT
  (( ok )) && { echo "SELFTEST PASS (14 checks, including takeover exec + idempotent verified install)"; exit 0; } || { echo "SELFTEST FAIL"; exit 1; }
fi

# box_state -> "STATE unit-active"  (STATE: READY|LAUNCHED|NOTYET|DEAD|UNREACHABLE)
box_state() {
  local out rc supervising terminal unit state
  out=$("$SSH_BIN" -n -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 "$BOX" '
    R=~/ear/var/runs/'"$EB"'/'"$BR"'/control/receipts.jsonl
    P=~/ear/var/runs/'"$EB"'/'"$BR"'/control/run-plan.json
    sha=$(python3 -c "import json;print(json.load(open(\"$P\"))[\"plan_sha256\"][:20])" 2>/dev/null)
    rc=$(grep -c "\"transition\":\"capture_ready\"" "$R" 2>/dev/null); rc=${rc:-0}
    sp=$(grep -c "\"transition\":\"supervising\"" "$R" 2>/dev/null); sp=${sp:-0}
    tm=$(grep -cE "\"transition\":\"(failed|aborted)\"" "$R" 2>/dev/null); tm=${tm:-0}
    ua=$(systemctl --user is-active "com.earshot.run.$sha.service" 2>/dev/null || true); ua=${ua:-unknown}
    printf "%s %s %s %s\n" "$rc" "$sp" "$tm" "$ua"' 2>/dev/null) || { echo "UNREACHABLE unreachable"; return; }
  read -r rc supervising terminal unit <<<"$out"
  [[ -n "$rc" && -n "$supervising" && -n "$terminal" && -n "$unit" ]] || { echo "UNREACHABLE unreachable"; return; }
  state=$(classify_box_observation "$rc" "$supervising" "$terminal" "$unit")
  echo "$state $unit"
}
stop_box() {
  "$SSH_BIN" -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 "$BOX" '
    P=~/ear/var/runs/'"$EB"'/'"$BR"'/control/run-plan.json
    sha=$(python3 -c "import json;print(json.load(open(\"$P\"))[\"plan_sha256\"][:20])" 2>/dev/null)
    mkdir -p ~/ear/state/'"$EB"' && touch ~/ear/state/'"$EB"'/STOP
    systemctl --user stop "com.earshot.run.$sha.timer" 2>/dev/null
    systemctl --user stop "com.earshot.run.$sha.service" 2>/dev/null
    echo box-stopped' 2>&1 | tail -1
}

if [[ "$MODE" == verify-install ]]; then
  [[ -n "$RUNDIR" ]] || { echo "USAGE-ERROR: --verify-install needs --run-dir" >&2; exit 2; }
  [[ -f "$RUNDIR/control/run-plan.json" ]] || { echo "REFUSED: $RUNDIR has no run-plan.json" >&2; exit 1; }
  read -r ELAP SHA20 < <(python3 -c 'import json,sys;p=json.load(open(sys.argv[1]+"/control/run-plan.json"));print(p["event_id"],p["plan_sha256"][:20])' "$RUNDIR")
  LABEL="com.earshot.run.$SHA20"; PL="$HOME/Library/LaunchAgents/$LABEL.plist"
  PROOF="state/$ELAP/failover-install.json"; SCHEDULE="$RUNDIR/control/schedule-proof.json"
  [[ -f "$PL" && -f "$PROOF" && -f "$SCHEDULE" ]] || { echo "REFUSED: failover plist/proof/schedule proof is incomplete for $ELAP" >&2; exit 1; }
  PRINTED=$($LAUNCHCTL_BIN print "gui/$(id -u)/$LABEL" 2>/dev/null) || { echo "REFUSED: launchctl does not report $LABEL loaded" >&2; exit 1; }
  PRINTED="$PRINTED" python3 - "$PL" "$PROOF" "$SCHEDULE" "$RUNDIR" "$REPO" "$LABEL" <<'PY' || exit 1
import datetime as dt
import json, os, plistlib, re, sys
from pathlib import Path

plist_path, proof_path, schedule_path, run_raw, repo_raw, label = sys.argv[1:]
repo, run = Path(repo_raw).resolve(), Path(run_raw).resolve()
for path in (Path(plist_path), Path(proof_path), Path(schedule_path)):
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"REFUSED: expected regular proof/descriptor: {path}")
plan = json.load(open(run / "control" / "run-plan.json"))
proof = json.load(open(proof_path))
schedule = json.load(open(schedule_path))
expected_keys = {"schema","kind","label","laptop_run","box_event","box_run",
                 "start_utc","grace_min","deadline_utc","box"}
if set(proof) != expected_keys or proof.get("schema") != 1 or proof.get("kind") != "laptop-failover-install-proof":
    raise SystemExit("REFUSED: failover proof has unsupported schema or fields")
if Path(str(proof["laptop_run"])).resolve() != run:
    raise SystemExit("REFUSED: failover proof names a different laptop RunPlan")
start = dt.datetime.fromisoformat(str(plan["starts_at_utc"]).replace("Z", "+00:00"))
if proof["label"] != label or proof["start_utc"] != start.strftime("%H:%M"):
    raise SystemExit("REFUSED: failover proof label/start differs from the RunPlan")
if not isinstance(proof["grace_min"], int) or proof["grace_min"] < 0:
    raise SystemExit("REFUSED: failover proof grace is invalid")
if not all(isinstance(proof[key], str) and proof[key] for key in ("box_event","box_run","deadline_utc","box")):
    raise SystemExit("REFUSED: failover proof host-pair fields are incomplete")
readback = schedule.get("readback", {})
tail = readback.get("program_arguments")
if not isinstance(tail, list) or not tail or not all(isinstance(token, str) for token in tail):
    raise SystemExit("REFUSED: schedule proof has no exact launcher argv")
prefix = ["/bin/bash", str(repo / "scripts" / "laptop-failover.sh"),
          "--event", str(plan["event_id"]), "--box-event", proof["box_event"],
          "--box-run", proof["box_run"], "--start-utc", proof["start_utc"],
          "--grace-min", str(proof["grace_min"]), "--deadline-utc", proof["deadline_utc"],
          "--box", proof["box"], "--"]
expected_argv = prefix + tail
with open(plist_path, "rb") as handle:
    plist = plistlib.load(handle)
fire = dt.datetime.fromisoformat(str(readback["fire_at_utc"]).replace("Z", "+00:00"))
calendar = {"Month":fire.month,"Day":fire.day,"Hour":fire.astimezone().hour,"Minute":fire.astimezone().minute}
if plist.get("Label") != label or plist.get("ProgramArguments") != expected_argv:
    raise SystemExit("REFUSED: failover plist does not contain the exact wrapper and scheduled launcher")
if plist.get("WorkingDirectory") != str(repo) or plist.get("EnvironmentVariables") != readback.get("environment"):
    raise SystemExit("REFUSED: failover plist changed the scheduled working directory/environment")
if plist.get("StartCalendarInterval") != calendar:
    raise SystemExit("REFUSED: failover plist calendar differs from schedule proof")
printed = os.environ["PRINTED"]
if not re.search(rf"(?m)^\s*path\s*=\s*{re.escape(str(Path(plist_path).resolve()))}\s*$", printed):
    raise SystemExit("REFUSED: loaded failover job points at a different plist")
if not re.search(rf"(?m)^\s*working directory\s*=\s*{re.escape(str(repo))}\s*$", printed):
    raise SystemExit("REFUSED: loaded failover working directory differs")
arg_match = re.search(r"(?ms)^\s*arguments\s*=\s*\{\s*(.*?)^\s*\}", printed)
loaded_argv = [line.strip() for line in arg_match.group(1).splitlines() if line.strip()] if arg_match else []
if loaded_argv != expected_argv:
    raise SystemExit("REFUSED: loaded failover argv differs from the exact plist")
for key, value in readback.get("environment", {}).items():
    if not re.search(rf'(?m)^\s*"?{re.escape(key)}"?\s*=>\s*"?{re.escape(str(value))}"?\s*$', printed):
        raise SystemExit(f"REFUSED: loaded failover environment differs for {key}")
for key, value in calendar.items():
    if not re.search(rf'(?m)^\s*"?{key}"?\s*(?:=>|=)\s*"?{value}"?\s*$', printed):
        raise SystemExit(f"REFUSED: loaded failover calendar differs for {key}")
print(f"VERIFIED failover wrapper on {label}")
PY
  exit 0
fi

if [[ "$MODE" == install ]]; then
  [[ -n "$RUNDIR" && -n "$EB" && -n "$BR" && -n "$START" ]] || { echo "USAGE-ERROR: --install needs --run-dir --box-event --box-run --start-utc" >&2; exit 2; }
  [[ -f "$RUNDIR/control/run-plan.json" ]] || { echo "REFUSED: $RUNDIR has no run-plan.json" >&2; exit 1; }
  [[ -z "$DEADLINE" ]] && DEADLINE=$(python3 -c 'import sys;h,m=map(int,sys.argv[1].split(":"));m+=15;print(f"{(h+m//60)%24:02d}:{m%60:02d}")' "$START")
  read -r ELAP SHA20 < <(python3 -c 'import json,sys;p=json.load(open(sys.argv[1]+"/control/run-plan.json"));print(p["event_id"],p["plan_sha256"][:20])' "$RUNDIR")
  E="$ELAP"; LABEL="com.earshot.run.$SHA20"; PL="$HOME/Library/LaunchAgents/$LABEL.plist"
  [[ -f "$PL" ]] || { echo "REFUSED: no loaded plist $PL — run opsctl schedule first" >&2; exit 1; }
  # Re-render the plist with ProgramArguments wrapped by this script. Refuse if already wrapped.
  python3 - "$PL" "$REPO/scripts/laptop-failover.sh" "$E" "$EB" "$BR" "$START" "$GRACE" "$DEADLINE" "$BOX" <<'PY' || exit 1
import plistlib,sys
pl_path,self_,E,EB,BR,START,GRACE,DEADLINE,BOX=sys.argv[1:]
with open(pl_path,'rb') as f: d=plistlib.load(f)
argv=d.get("ProgramArguments",[])
prefix=["/bin/bash",self_,"--event",E,"--box-event",EB,"--box-run",BR,"--start-utc",START,"--grace-min",str(GRACE)]
if DEADLINE: prefix+=["--deadline-utc",DEADLINE]
prefix+=["--box",BOX,"--"]
if self_ in argv:
    if argv[:len(prefix)] != prefix or len(argv) == len(prefix):
        sys.exit("REFUSED: existing failover wrapper differs from the requested exact host pair")
    print("verified existing failover ProgramArguments:", " ".join(prefix[:12]),"… + original",len(argv)-len(prefix),"args")
else:
    wrap=prefix+argv
    d["ProgramArguments"]=wrap
    with open(pl_path,'wb') as f: plistlib.dump(d,f)
    print("wrapped ProgramArguments:", " ".join(wrap[:12]),"… + original",len(argv),"args")
PY
  "$LAUNCHCTL_BIN" bootout "gui/$(id -u)/$LABEL" 2>/dev/null
  "$LAUNCHCTL_BIN" bootstrap "gui/$(id -u)" "$PL" || { echo "REFUSED: bootstrap failed for $LABEL" >&2; exit 1; }
  RB=$("$LAUNCHCTL_BIN" print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -o 'laptop-failover.sh\|"Hour" => [0-9]*\|"Minute" => [0-9]*\|"Day" => [0-9]*' | sort -u | tr '\n' ' ')
  echo "$RB" | grep -q laptop-failover.sh || { echo "REFUSED: readback does not show the wrapper in $LABEL" >&2; exit 1; }
  mkdir -p "state/$E"
  python3 -c 'import json,sys;json.dump({"schema":1,"kind":"laptop-failover-install-proof","label":sys.argv[1],"laptop_run":sys.argv[2],"box_event":sys.argv[3],"box_run":sys.argv[4],"start_utc":sys.argv[5],"grace_min":int(sys.argv[6]),"deadline_utc":sys.argv[7],"box":sys.argv[8]},open(sys.argv[9],"w"),indent=2)' "$LABEL" "$RUNDIR" "$EB" "$BR" "$START" "$GRACE" "$DEADLINE" "$BOX" "state/$E/failover-install.json"
  echo "INSTALLED failover wrapper on $LABEL — readback: $RB"
  echo "  rerun verification: $REPO/scripts/laptop-failover.sh --verify-install --run-dir $RUNDIR"
  echo "  disarm: launchctl bootout gui/$(id -u)/$LABEL then re-run opsctl schedule --date"
  exit 0
fi

[[ -n "$E" && -n "$EB" && -n "$BR" && -n "$START" && $# -gt 0 ]] || { echo "USAGE-ERROR: need --event --box-event --box-run --start-utc and -- <argv>" >&2; exit 2; }
mkdir -p "state/$E"; LOG="state/$E/failover.log"; exec >>"$LOG" 2>&1
[[ -z "$DEADLINE" ]] && DEADLINE=$(python3 -c 'import sys;h,m=map(int,sys.argv[1].split(":"));m+=15;print(f"{(h+m//60)%24:02d}:{m%60:02d}")' "$START")
echo "=== failover $(date -u +%FT%TZ) laptop=$E box=$EB/$BR start=${START}Z grace=${GRACE}m deadline=${DEADLINE}Z argv=[$*]"
DECIDE=$(decide_hhmm "$START" "$GRACE"); DLINE="${DEADLINE/:/}"; FIRE="${START/:/}"; LAST=""
takeover() {
  local reason="$1"
  shift
  echo "$(date -u +%T) DECISION: $reason — LAPTOP TAKES OVER"
  stop_box
  scripts/notify.sh "$E: BOX FAILED — laptop arming live" "$reason at $(date -u +%T)Z" >/dev/null 2>&1 || true
  echo "$(date -u +%T) exec: $*"
  exec_takeover_argv "$reason" "$@"
}
while :; do
  NOW=$(date -u +%H%M); read -r ST UA < <(box_state); [[ -z "$ST" ]] && { ST=UNREACHABLE; UA=unreachable; }
  [[ "$ST/$UA" != "$LAST" ]] && { echo "$(date -u +%T) box=$ST unit=$UA"; LAST="$ST/$UA"; }
  if [[ "$ST" == READY ]]; then
    # Keep watching until the AccessPlan deadline; a box that dies after going ready still fails over.
    if [[ "$NOW" > "$DLINE" || "$NOW" == "$DLINE" ]]; then
      echo "$(date -u +%T) BOX SUPERVISED DETECTOR AUDIO THROUGH THE DEADLINE — laptop stands down (no arm, no orders)"
      scripts/notify.sh "$E: box captured, laptop stood down" "box $EB/$BR held supervising detector-path audio to ${DEADLINE}Z" --quiet >/dev/null 2>&1 || true
      exit 0
    fi
    sleep 20; continue
  fi
  if [[ "$ST" == DEAD ]]; then takeover "box run/unit DEAD (unit=$UA)" "$@"; fi
  # box unit gone/failed after the timer should have fired = dead primary
  if [[ ( "$NOW" > "$FIRE" || "$NOW" == "$FIRE" ) && ( "$UA" == failed || "$UA" == inactive ) && "$ST" != LAUNCHED ]]; then
    takeover "box unit $UA past fire time, not capturing" "$@"
  fi
  if [[ "$NOW" > "$DECIDE" || "$NOW" == "$DECIDE" ]]; then takeover "box=$ST at start+${GRACE}m (UNREACHABLE counts as failed — capture beats the one-trader rule)" "$@"; fi
  sleep 20
done
