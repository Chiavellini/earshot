#!/usr/bin/env bash
# Execute one immutable webcast AccessPlan, prove current PCM, and hand the
# exact schema-3 RunPlan to launch-webcast exactly once.
#
#   scripts/arm-on-url.sh --run-dir var/runs/<EVENT>/<RUN> \
#     [--i-am-live --fillable] [--secs 6]
#
# AccessPlan is the only resolver/source/url-file authority. An automatic
# re-arm under the same RunPlan is forbidden: launch-webcast durably records
# `launched`, and a new attempt requires a new RunPlan rather than overwriting
# evidence from the first process.
set -euo pipefail

REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPO=$(cd "$REPO" && pwd -P)
cd "$REPO"
RUN_DIR_RAW="${EARSHOT_RUN_DIR:-}" LIVE=0 FILLABLE=0 SECS=6
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir) RUN_DIR_RAW="${2:?}"; shift 2 ;;
    --i-am-live) LIVE=1; shift ;;
    --fillable) FILLABLE=1; shift ;;
    --secs) SECS="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$RUN_DIR_RAW" ]] || { echo "REFUSED: --run-dir is required" >&2; exit 2; }
[[ "$SECS" =~ ^[1-9][0-9]*$ ]] || { echo "--secs must be a positive integer" >&2; exit 2; }
[[ "$FILLABLE" == 0 || "$LIVE" == 1 ]] || { echo "--fillable requires --i-am-live" >&2; exit 2; }

RUN_DIR=$(python3 - "$REPO" "$RUN_DIR_RAW" <<'PY'
from pathlib import Path
import sys
repo = Path(sys.argv[1]).resolve()
run = Path(sys.argv[2]); run = (run if run.is_absolute() else repo / run).resolve()
try: rel = run.relative_to(repo / "var" / "runs")
except ValueError: raise SystemExit("run directory is outside canonical var/runs")
if len(rel.parts) != 2: raise SystemExit("run directory must be var/runs/<EVENT_ID>/<RUN_ID>")
print(run)
PY
) || { echo "REFUSED: invalid --run-dir '$RUN_DIR_RAW'" >&2; exit 2; }

EARSHOT_REPO="$REPO" python3 scripts/eventctl.py verify --run-dir "$RUN_DIR" >/dev/null || {
  echo "REFUSED: RunPlan/ledger/pinned-input verification failed" >&2; exit 1;
}
ACCESS_JSON=$(EARSHOT_REPO="$REPO" python3 scripts/access-plan.py validate \
  --run-dir "$RUN_DIR" --canonical) || { echo "REFUSED: canonical immutable AccessPlan invalid" >&2; exit 1; }

EXPORTS=$(ACCESS_JSON="$ACCESS_JSON" python3 - "$REPO" "$RUN_DIR" "$LIVE" <<'PY'
import datetime as dt
import json, os, shlex, subprocess, sys
from pathlib import Path
repo, run, live = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] == "1"
plan = json.loads((run / "control/run-plan.json").read_text())
access = json.loads((run / "control/access-plan.json").read_text())
if plan.get("schema") != 3 or plan.get("kind") != "run-plan" or plan.get("lane") != "webcast":
    raise SystemExit("arm-on-url requires a schema-3 webcast RunPlan")
if live != bool(plan.get("execution_enabled")):
    raise SystemExit(
        f"launch posture differs from RunPlan execution_enabled={plan.get('execution_enabled')!r}"
    )
if os.environ.get("EARSHOT_EXECUTION_HOST") != plan.get("execution_host_id"):
    raise SystemExit("current execution host differs from RunPlan")
if os.environ.get("EARSHOT_RUN_PLAN_SHA256") not in (None, "", plan["plan_sha256"]):
    raise SystemExit("scheduler RunPlan hash differs from selected run")
status = subprocess.run(
    [sys.executable, str(repo / "scripts/eventctl.py"), "status", "--run-dir", str(run), "--json"],
    cwd=repo, env={**os.environ, "EARSHOT_REPO": str(repo)}, text=True,
    capture_output=True, check=True,
)
state = json.loads(status.stdout)["state"]
reached = {x["transition"] for x in state["reached"]}
if "scheduled" not in reached: raise SystemExit("RunPlan has no scheduled receipt")
if "launched" in reached: raise SystemExit("RunPlan already launched")
if not state["active"]: raise SystemExit("RunPlan is terminal/archived")
resolver = access["resolver"]
deadline = dt.datetime.fromisoformat(resolver["deadline_utc"].replace("Z", "+00:00"))
if dt.datetime.now(dt.timezone.utc) > deadline:
    raise SystemExit("AccessPlan resolver deadline has passed")
url_files = access["endpoint"]["url_files"]
if len(url_files) > 1:
    raise SystemExit("runtime supports exactly zero or one AccessPlan URL file")
portable = url_files[0] if url_files else ""
url_path = str(Path.home() / portable[len("$HOME/"):]) if portable else ""
kind = "youtube" if resolver["kind"] == "youtube-live" else "hls"
source_argv = access["sources"][0]["argv"]
account = os.environ.get("EARSHOT_ACCOUNT_ENV", "")
if not account and plan.get("event_env_path"):
    env_path = repo / plan["event_env_path"]
    if env_path.is_file():
        import re
        match = re.search(r'^\s*ACCOUNT_ENV=["' + "']([^\"']+)[\"']", env_path.read_text(), re.M)
        if match:
            account = match.group(1).replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))
if not account:
    raise SystemExit("EARSHOT_ACCOUNT_ENV or pinned event env ACCOUNT_ENV must name the account profile")
values = {
    "PLAN_EVENT": plan["event_id"],
    "PLAN_RUN_ID": plan["run_id"],
    "PLAN_CONFIG": str((repo / plan["config_path"]).resolve()),
    "PLAN_HOST": plan["execution_host_id"],
    "RESOLVER_ARGV_JSON": json.dumps(resolver["argv"], separators=(",", ":")),
    "RESOLVER_KIND": resolver["kind"],
    "URL_FILE": url_path,
    "PCM_KIND": kind,
    "ACCOUNT_ENV": str(Path(account).expanduser().resolve()),
}
for key,value in values.items(): print(f"{key}={shlex.quote(value)}")
PY
) || { echo "REFUSED: AccessPlan/RunPlan cannot execute in this scheduler context" >&2; exit 1; }
eval "$EXPORTS"

STATE_DIR="$REPO/state/$PLAN_EVENT"
mkdir -p "$STATE_DIR"
LOG="$STATE_DIR/arm-on-url-$PLAN_RUN_ID.log"
# A scheduler-launched shell does not need an interactive tee, and restricted
# service contexts may reject /dev/fd process substitution before any proof is
# written. The run-scoped file is the durable fire-time log.
exec >> "$LOG" 2>&1
page() { "$REPO/scripts/notify.sh" "$1" "$2" --quiet || true; }

[[ -f "$ACCOUNT_ENV" ]] || { echo "REFUSED: account env is not readable: $ACCOUNT_ENV" >&2; exit 1; }
PINNED_BIN="$STATE_DIR/earshot-pinned"
[[ -x "$PINNED_BIN" ]] || {
  page "$PLAN_EVENT NOT ARMED — no pinned binary" \
    "The exact RunPlan has no executable $PINNED_BIN. Fire-time cargo fallback is disabled."
  exit 1
}

# Run exactly the AccessPlan resolver argv. No eval, filename map, resolver
# guess, or manual escalation loop exists here. static-public has empty argv.
if [[ "$RESOLVER_KIND" != static-public ]]; then
  RESOLVER_TMP=$(mktemp "${TMPDIR:-/tmp}/earshot-resolver-argv.XXXXXX")
  python3 -c '
import json,sys
for x in json.loads(sys.argv[1]): print(x)
' "$RESOLVER_ARGV_JSON" > "$RESOLVER_TMP"
  RESOLVER_ARGV=()
  while IFS= read -r resolver_arg; do
    RESOLVER_ARGV+=("$resolver_arg")
  done < "$RESOLVER_TMP"
  rm -f -- "$RESOLVER_TMP"
  [[ "${#RESOLVER_ARGV[@]}" -gt 0 ]] || { echo "REFUSED: resolver argv is empty" >&2; exit 1; }
  echo "RESOLVER: executing AccessPlan argv (${RESOLVER_ARGV[0]})"
  if ! "${RESOLVER_ARGV[@]}"; then
    page "$PLAN_EVENT NOT ARMED — resolver failed" \
      "AccessPlan resolver exited nonzero for $PLAN_RUN_ID. No capture-ready receipt or runtime was created."
    exit 3
  fi
fi

PCM_PROOF="$RUN_DIR/control/pcm-proof.json"
PCM_ARGS=(--kind "$PCM_KIND" --secs "$SECS" --label "$PLAN_EVENT"
  --run-dir "$RUN_DIR" --proof-json "$PCM_PROOF")
if [[ -n "$URL_FILE" ]]; then
  PCM_ARGS+=(--url-file "$URL_FILE")
else
  echo "REFUSED: static-public source has no single endpoint the current PCM gate can bind" >&2
  exit 4
fi
if [[ ! -e "$PCM_PROOF" ]]; then
  if ! "$REPO/scripts/pcm-gate.sh" "${PCM_ARGS[@]}"; then
    page "$PLAN_EVENT NOT ARMED — PCM proof failed" \
      "No fresh durable PCM+continuity proof was produced for $PLAN_RUN_ID on $PLAN_HOST."
    exit 4
  fi
fi
if ! EARSHOT_REPO="$REPO" EARSHOT_EXECUTION_HOST="$PLAN_HOST" \
  python3 "$REPO/scripts/capture-ready.py" webcast --run-dir "$RUN_DIR" \
    --pcm-proof "$PCM_PROOF"; then
  page "$PLAN_EVENT NOT ARMED — capture proof refused" \
    "capture-ready.py rejected PCM/AccessPlan evidence for $PLAN_RUN_ID."
  exit 4
fi

LAUNCH_ARGS=(--config "$PLAN_CONFIG" --account "$ACCOUNT_ENV"
  --bin "$PINNED_BIN" --run-dir "$RUN_DIR")
[[ "$LIVE" == 1 ]] && LAUNCH_ARGS+=(--i-am-live)
[[ "$FILLABLE" == 1 ]] && LAUNCH_ARGS+=(--fillable)

echo "LAUNCH: exact RunPlan $PLAN_EVENT/$PLAN_RUN_ID (one process, no re-arm)"
"$REPO/scripts/launch-webcast.sh" "${LAUNCH_ARGS[@]}" &
LAUNCH_PID=$!

# Signal only the launcher. It owns STOP plus its >=90 s runtime finalizer
# drain; this wrapper never addresses the process group and never sends KILL.
request_shutdown() {
  echo "TERM/INT: requesting ordered launcher drain"
  touch "$STATE_DIR/STOP"
  kill -TERM "$LAUNCH_PID" 2>/dev/null || true
}
trap request_shutdown TERM INT
while :; do
  wait "$LAUNCH_PID"; RC=$?
  if kill -0 "$LAUNCH_PID" 2>/dev/null; then
    continue
  fi
  break
done
trap - TERM INT
exit "$RC"
