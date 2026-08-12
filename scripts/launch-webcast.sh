#!/usr/bin/env bash
# Webcast-lane launcher: arm the pipeline on a config whose [[sources]] is
# `hls` or `pipe` (source::pipe decodes via ffmpeg/yt-dlp/streamlink). No
# tunnel, no Twilio, no dial — the subprocess pulls the stream itself.
#
#   scripts/launch-webcast.sh --config configs/<event>.toml --account <env> \
#       [--bin <pinned>] [--run-dir <immutable-run>] \
#       [--wait-live <url-or-url-file> --wait-kind youtube|hls]
#
# POSTURE. Default is DRY RUN (no orders reach Kalshi). `--i-am-live` opts into a
# real arm; `--fillable` is ACCEPTED (see the W7 FILL-HALF note at the flag
# itself) and requires `--i-am-live`.
#
# CORRECTED 2026-08-11: this line used to read "`--fillable` is still refused
# here", which the flag parser below has contradicted since 2026-08-05. The
# stale text is worth calling out rather than just deleting, because it fails in
# the expensive direction — read literally it says the webcast lane cannot fill,
# so an operator pricing a FILL posture would conclude the whole config is inert
# and go looking for a bug that is not there. The precision caveat W7 was built
# for DOES still stand and is UNRESOLVED (docs/BAKEOFF.md G4 is not scored, and
# the .readiness studies measure RECALL, not precision).
#
# W7 LIFTED 2026-07-27 (operator ruling, for PayPal 07-28). The prior text read
# "this script deliberately accepts no --fillable/--i-am-live", which meant the
# RUNBOOK's own step-5 command produced a DRY RUN — so the 07-26 fix that makes
# `arm` refuse a partial allowlist (config::allowlist_covers_keywords) could never
# fire on the path the runbook told you to use, because dry-runs do not reach the
# risk gate. An event billed as "the live Δrace measurement" was going to place no
# orders at all.
#
# Live safety is enforced in `arm`, NOT duplicated here — one place, per CLAUDE.md:
#   - --i-am-live with an empty [risk].allowlist_tickers is REFUSED (main.rs)
#   - a non-empty allowlist that misses a keyword is REFUSED (allowlist_covers_keywords)
#   - --allow-mention alone is the mention-PROBE posture: cap must be ≤ 1¢ and
#     orders are non-crossing, so a live arm here still CANNOT fill.
#
# LIVE GATE (07-22 ops post-mortems #1/#2, docs/history/RUNBOOK.md): the wait for the
# stream to go live happens HERE, before arm ever starts:
#   #1 `yt-dlp --wait-for-video` stalls at the pre-live→live flip — so no
#      armed-leg cmd may carry it; this script polls `is_live` and launches
#      on the flip instead.
#   #2 a liveness gate INSIDE a pipe-source cmd emits no bytes and trips the
#      pipeline's 30 s idle guard — so the gate must run OUTSIDE the arm.
# Post-mortem #3 (sandboxed arm hard-killed, no signal handler ran): run this
# script from a PLAIN shell / nohup — never inside a sandboxed supervisor.
# arm's own SIGTERM path is the validated kill mechanism.
#
# Abort: Ctrl-C asks this wrapper to assert STOP, wait for the arm's session
# cancellation/evidence/report finalizers, and escalate only on timeout. A
# direct `touch state/<event>/STOP` reaches the same runtime boundary.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
SCRIPT_REPO=$(cd "${SCRIPT_DIR}/.." && pwd -P)

CONFIG="" ACCOUNT="" BIN="${EARSHOT_BIN:-}" WAIT_LIVE="" WAIT_KIND="" LIVE=0 FILLABLE=""
RUN_DIR_OVERRIDE=""
# Audio, book, FIX, cancellation, and report finalizers can legitimately use
# most of a minute after STOP.  The launcher owns a larger outer budget; a
# smaller value would turn a healthy coordinated drain into evidence loss.
SHUTDOWN_GRACE_SECS="${EARSHOT_LAUNCH_SHUTDOWN_GRACE_SECS:-90}"
ARM_PID="" SHUTDOWN_STARTED="" FINALIZERS_UNRESOLVED=0
RUN_DIR="" PLAN_PATH="" PLAN_REPORT="" PLAN_SHA256="" PLAN_RUN_ID=""
ACCOUNT_SOURCED=0 LIFECYCLE_LAUNCHED=0 LIFECYCLE_SUPERVISING=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)    CONFIG="$2"; shift 2 ;;
    --account)   ACCOUNT="$2"; shift 2 ;;
    --bin)       BIN="$2"; shift 2 ;;
    --run-dir)   RUN_DIR_OVERRIDE="$2"; shift 2 ;;
    --shutdown-grace-secs) SHUTDOWN_GRACE_SECS="$2"; shift 2 ;;
    --wait-live) WAIT_LIVE="$2"; shift 2 ;;
    --wait-kind) WAIT_KIND="$2"; shift 2 ;;
    --i-am-live) LIVE=1; shift ;;
    # W7 FILL-HALF LIFTED 2026-08-05 (operator ruling, for the 26AUG05 afternoon
    # slate). This arm previously refused --fillable outright, which — combined
    # with prepare writing max_price_cents=1 — meant the webcast lane could not
    # place an order at all: every intent short-circuited at executor.rs's
    # non-crossing branch and returned BEFORE send_order(). Four armed events on
    # 26AUG05 reported "23 orders" that never reached the wire.
    # The precision caveat W7 was built for still stands and is UNRESOLVED:
    # docs/BAKEOFF.md G4 is not scored, and the .readiness studies measure
    # RECALL, not precision. Money is bounded elsewhere — MENTION_FILL_CAP_CENTS
    # (a per-ORDER price bound), [risk].max_event_notional_cents, and the
    # per-event probe ledger.
    #
    # THE NUMBERS IN THIS NOTE WENT STALE TWICE; do not read them from here.
    # It said "50¢/order" (raised to 97 on 2026-08-05) and "the per-event $1
    # probe ledger — so a false fire costs at most that event's $1" (the ledger
    # went 100 → 2000 on 2026-08-10, then → 7000 on 2026-08-11 for tiered
    # sizing). "At most $1" understated the 26AUG11 exposure by ~70×. Read the
    # live values from src/risk.rs and the event's own [risk] block — a comment
    # is not a cap.
    --fillable)  FILLABLE="--fillable"; shift ;;
    # --fill-all stays refused: it is the tier override, a separate decision.
    --fill-all)
      echo "REFUSING $1: --fill-all overrides the vetted-word tier and is not part" >&2
      echo "of the 26AUG05 W7 ruling. Arm it deliberately by hand if you mean it." >&2
      exit 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
if [[ -n "$FILLABLE" && $LIVE -eq 0 ]]; then
  echo "--fillable requires --i-am-live (arm refuses it otherwise; fail here, not 8 min before a call)" >&2
  exit 2
fi
[[ -n "$CONFIG" && -n "$ACCOUNT" ]] || {
  echo "usage: $0 --config <cfg.toml> --account <env-file> [--bin <pinned>] [--run-dir <immutable-run>] [--shutdown-grace-secs N] [--i-am-live] [--wait-live <url-or-url-file> --wait-kind youtube|hls]" >&2; exit 2
}
[[ -f "$CONFIG" ]] || { echo "config not found: $CONFIG" >&2; exit 2; }
[[ -f "$ACCOUNT" ]] || { echo "account env not found: $ACCOUNT" >&2; exit 2; }
[[ "$SHUTDOWN_GRACE_SECS" =~ ^[0-9]+$ && "$SHUTDOWN_GRACE_SECS" -ge 75 ]] || {
  echo "--shutdown-grace-secs must be an integer >= 75 (got ${SHUTDOWN_GRACE_SECS})" >&2
  exit 2
}

for tool in ffmpeg; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing tool: $tool" >&2; exit 1; }
done
# yt-dlp/streamlink only matter for configs that use them; warn, don't fail.
command -v yt-dlp >/dev/null 2>&1 || echo "note: yt-dlp not found — YouTube pipe configs will fail" >&2
command -v streamlink >/dev/null 2>&1 || echo "note: streamlink not found — streamlink legs will fail" >&2

# streaming_ctc loads onnxruntime DYNAMICALLY. With ORT_DYLIB_PATH unset the
# detector fails to build and the arm dies at startup — and a launchd-fired run
# has a minimal env, which is exactly the class of failure CLAUDE.md warns about
# (an interactively-validated path is not a validated path). Resolve it HERE so
# the launcher works identically from a shell and from launchd, and REFUSE
# before the call rather than failing 0 s into it.
if grep -qE '^[[:space:]]*engine[[:space:]]*=[[:space:]]*"streaming_ctc"' "$CONFIG"; then
  if [[ -z "${ORT_DYLIB_PATH:-}" ]]; then
    # NOT `[[ -n "$_ort" ]] && export ...` — under `set -e` that compound
    # returns 1 when the find comes up empty and kills the script SILENTLY,
    # before the REFUSING message below can explain why. Same family as the
    # pipefail traps in docs/history/RUNBOOK.md. Use an if.
    # Two bugs fixed 2026-08-03, both of which made this resolver a no-op on a
    # Mac: it searched ONLY '*.so*' (there is no .so on Darwin, so the find
    # always came up empty and the REFUSE below fired on a healthy tree), and it
    # rooted at $HOME/ear, which is the BOX layout — the laptop repo is
    # $HOME/Code/ear. Resolve per-platform, and search the real repo root.
    # -not -path '*dSYM*': the debug bundle holds a same-named file that dlopen
    # HANGS on (see run-arm.sh) — that decoy hung preflight's detector-smoke.
    _vend="${EARSHOT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}/vendor"
    case "$(uname -s)" in
      Darwin) _pat='libonnxruntime*.dylib' ;;
      *)      _pat='libonnxruntime*.so*'   ;;
    esac
    _ort=$(find "$_vend" -name "$_pat" -not -path '*dSYM*' 2>/dev/null | head -1 || true)
    if [[ -n "$_ort" ]]; then
      export ORT_DYLIB_PATH="$_ort"
    fi
  fi
  case "${ORT_DYLIB_PATH:-}" in
    *dSYM*) echo "REFUSING: ORT_DYLIB_PATH is a dSYM debug bundle, not a loadable library: $ORT_DYLIB_PATH" >&2; exit 2 ;;
  esac
  [[ -n "${ORT_DYLIB_PATH:-}" && -f "${ORT_DYLIB_PATH}" ]] || {
    echo "REFUSING: config uses engine=streaming_ctc but ORT_DYLIB_PATH is unset/invalid." >&2
    echo "  run scripts/fetch-models.sh (it fetches the dylib and prints the export), or" >&2
    echo "  export ORT_DYLIB_PATH=/path/to/libonnxruntime.so before launching." >&2
    exit 2
  }
  echo "streaming_ctc: ORT_DYLIB_PATH=${ORT_DYLIB_PATH}"
fi

# Armed-leg cmds must not gate on liveness themselves (post-mortems #1/#2).
if grep -q "wait-for-video" "$CONFIG"; then
  echo "REFUSING: $CONFIG contains --wait-for-video in a source cmd — the live gate" >&2
  echo "belongs to this script (--wait-live), never inside the arm (RUNBOOK 07-22 #1/#2)" >&2
  exit 2
fi

EVENT_ID=$(sed -n '/^\[event\]/,/^\[/s/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
[[ -n "$EVENT_ID" ]] || { echo "no [event].id in $CONFIG" >&2; exit 2; }

# Dry/rehearsal launches keep their historical cwd-relative state/report
# contract.  LIVE is different: execution must consume exactly one immutable
# schema-3 RunPlan, and every identity/path handed to `arm` comes from it.
REPO="${EARSHOT_REPO:-$SCRIPT_REPO}"
[[ -d "$REPO" ]] || { echo "EARSHOT_REPO is not a directory: $REPO" >&2; exit 2; }
REPO=$(cd "$REPO" && pwd -P)
EVENTCTL="${EARSHOT_EVENTCTL:-${SCRIPT_REPO}/scripts/eventctl.py}"
if (( LIVE )); then
  if [[ -n "${EARSHOT_NO_SIDECAR+x}" ]]; then
    echo "REFUSING live launch: EARSHOT_NO_SIDECAR is obsolete; in-process audio evidence is mandatory" >&2
    exit 7
  fi
  # shellcheck disable=SC1090
  source "$ACCOUNT"
  ACCOUNT_SOURCED=1
  [[ -f "$EVENTCTL" ]] || { echo "REFUSING live launch: eventctl not found: $EVENTCTL" >&2; exit 2; }
  if [[ -n "$RUN_DIR_OVERRIDE" ]]; then
    [[ -d "$RUN_DIR_OVERRIDE" ]] || {
      echo "REFUSING live launch: --run-dir is not a directory: $RUN_DIR_OVERRIDE" >&2
      exit 2
    }
    RUN_DIR=$(cd "$RUN_DIR_OVERRIDE" && pwd -P)
  else
    STATUS_JSON=$(EARSHOT_REPO="$REPO" python3 "$EVENTCTL" status --event "$EVENT_ID" --json) || {
      echo "REFUSING live launch: eventctl could not resolve one active run for $EVENT_ID" >&2
      exit 7
    }
    PLAN_RUN_ID=$(printf '%s' "$STATUS_JSON" | python3 -c 'import json,sys; v=json.load(sys.stdin); r=v.get("run"); print(r if isinstance(r,str) else "")') || {
      echo "REFUSING live launch: eventctl status returned invalid JSON for $EVENT_ID" >&2
      exit 7
    }
    [[ -n "$PLAN_RUN_ID" ]] || {
      echo "REFUSING live launch: eventctl status omitted run id for $EVENT_ID" >&2
      exit 7
    }
    RUN_DIR="${REPO}/var/runs/${EVENT_ID}/${PLAN_RUN_ID}"
    [[ -d "$RUN_DIR" ]] || {
      echo "REFUSING live launch: resolved run directory missing: $RUN_DIR" >&2
      exit 7
    }
    RUN_DIR=$(cd "$RUN_DIR" && pwd -P)
  fi
  RUN_PARENT=$(dirname "$RUN_DIR")
  EXPECTED_RUN_PARENT="${REPO}/var/runs/${EVENT_ID}"
  [[ -d "$EXPECTED_RUN_PARENT" ]] || {
    echo "REFUSING live launch: canonical event run directory missing: $EXPECTED_RUN_PARENT" >&2
    exit 7
  }
  EXPECTED_RUN_PARENT=$(cd "$EXPECTED_RUN_PARENT" && pwd -P)
  [[ "$RUN_PARENT" == "$EXPECTED_RUN_PARENT" ]] || {
    echo "REFUSING live launch: run directory is outside ${EXPECTED_RUN_PARENT}: $RUN_DIR" >&2
    exit 7
  }
  PLAN_PATH="${RUN_DIR}/control/run-plan.json"
  [[ -f "$PLAN_PATH" ]] || {
    echo "REFUSING live launch: immutable RunPlan missing: $PLAN_PATH" >&2
    exit 7
  }
  EARSHOT_REPO="$REPO" python3 "$EVENTCTL" verify --run-dir "$RUN_DIR" || {
    echo "REFUSING live launch: RunPlan/receipt/input verification failed: $RUN_DIR" >&2
    exit 7
  }
  RUN_STATUS_JSON=$(EARSHOT_REPO="$REPO" python3 "$EVENTCTL" status --run-dir "$RUN_DIR" --json) || {
    echo "REFUSING live launch: could not derive verified lifecycle state for $RUN_DIR" >&2
    exit 7
  }
  printf '%s' "$RUN_STATUS_JSON" | python3 -c '
import json, sys
status = json.load(sys.stdin)
state = status.get("state") or {}
reached = {r.get("transition") for r in state.get("reached", [])}
missing = {"scheduled", "capture_ready"} - reached
if missing:
    raise SystemExit("RunPlan is not launch-ready; missing " + ", ".join(sorted(missing)))
if "launched" in reached:
    raise SystemExit("RunPlan already records launched; duplicate activation refused")
if state.get("terminal") or state.get("archived"):
    raise SystemExit("RunPlan is terminal/archived; activation refused")
' || { echo "REFUSING live launch: immutable lifecycle is not at the pre-launch gate" >&2; exit 7; }
  PLAN_FIELDS=$(python3 - "$PLAN_PATH" "$RUN_DIR" "$EVENT_ID" "$REPO" <<'PY'
import json, os, sys
plan_path, run_dir, event, repo = sys.argv[1:]
with open(plan_path, "r", encoding="utf-8") as fh:
    plan = json.load(fh)
if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
    raise SystemExit("launch-webcast.sh requires a schema-3 RunPlan")
if plan.get("event_id") != event:
    raise SystemExit("RunPlan event_id does not match config")
if plan.get("lane") != "webcast":
    raise SystemExit("launch-webcast.sh requires a webcast RunPlan")
if plan.get("execution_enabled") is not True or plan.get("capture_only") is not False:
    raise SystemExit("--i-am-live requires an execution-enabled RunPlan")
if plan.get("canonical_state_root") != "state":
    raise SystemExit("webcast live state must use RunPlan canonical_state_root=state")
host = plan.get("execution_host_id")
account = plan.get("account_fingerprint")
run_id = plan.get("run_id")
plan_sha = plan.get("plan_sha256")
artifact = (plan.get("artifacts") or {}).get("runtime_report")
config_rel = plan.get("config_path")
values = (host, account, run_id, plan_sha, artifact, config_rel)
if not all(isinstance(x, str) and x and "|" not in x for x in values):
    raise SystemExit("RunPlan identity/config/runtime_report fields are invalid")
root = os.path.realpath(run_dir)
report = os.path.realpath(os.path.join(root, artifact))
if os.path.commonpath((root, report)) != root:
    raise SystemExit("RunPlan runtime_report escapes run directory")
repo = os.path.realpath(repo)
config = os.path.realpath(os.path.join(repo, config_rel))
if os.path.commonpath((repo, config)) != repo:
    raise SystemExit("RunPlan config escapes repository")
print("|".join((host, account, run_id, plan_sha, report, config)))
PY
  ) || { echo "REFUSING live launch: could not bind immutable RunPlan fields" >&2; exit 7; }
  IFS='|' read -r PLAN_HOST PLAN_ACCOUNT_FINGERPRINT PLAN_RUN_ID PLAN_SHA256 PLAN_REPORT PLAN_CONFIG <<EOF
$PLAN_FIELDS
EOF
  CONFIG_REAL=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CONFIG")
  [[ "$CONFIG_REAL" == "$PLAN_CONFIG" ]] || {
    echo "REFUSING live launch: config '$CONFIG_REAL' is not immutable RunPlan config '$PLAN_CONFIG'" >&2
    exit 7
  }
  [[ -n "${EARSHOT_EXECUTION_HOST:-}" ]] || {
    echo "REFUSING live launch: EARSHOT_EXECUTION_HOST must be exported by the event/account environment" >&2
    exit 7
  }
  [[ -n "${EARSHOT_KALSHI_ACCOUNT_FINGERPRINT:-}" ]] || {
    echo "REFUSING live launch: EARSHOT_KALSHI_ACCOUNT_FINGERPRINT must be exported; it is never derived from private-key material in shell" >&2
    exit 7
  }
  [[ "$EARSHOT_EXECUTION_HOST" == "$PLAN_HOST" ]] || {
    echo "REFUSING live launch: execution host '$EARSHOT_EXECUTION_HOST' != RunPlan '$PLAN_HOST'" >&2
    exit 7
  }
  [[ "$EARSHOT_KALSHI_ACCOUNT_FINGERPRINT" == "$PLAN_ACCOUNT_FINGERPRINT" ]] || {
    echo "REFUSING live launch: account fingerprint does not match immutable RunPlan" >&2
    exit 7
  }
  export EARSHOT_RUN_PLAN="$PLAN_PATH"
  export EARSHOT_EXECUTION_HOST EARSHOT_KALSHI_ACCOUNT_FINGERPRINT
  export EARSHOT_CANONICAL_STATE_ROOT="${REPO}/state"
  STATE_DIR="${REPO}/state/${EVENT_ID}"
  REPORT="$PLAN_REPORT"
else
  [[ -z "$RUN_DIR_OVERRIDE" ]] || {
    echo "--run-dir is valid only with --i-am-live" >&2
    exit 2
  }
  STATE_DIR="state/${EVENT_ID}"
  REPORT="${STATE_DIR}/report.json"
fi
mkdir -p "$STATE_DIR"
# 2026-08-05 rehearsal catch: a launchd-fired watcher reached the exec with no
# --bin and DIED on `exec: cargo: not found` (launchd's PATH has no ~/.cargo).
# When preflight pinned a binary for this event, run the pin — which is also
# what the runbook demands. Explicit --bin still wins.
if [[ -z "$BIN" && -x "${STATE_DIR}/earshot-pinned" ]]; then
  BIN="${STATE_DIR}/earshot-pinned"
  echo "pinned binary auto-selected: $BIN (preflight wrote it; cargo fallback never fires under launchd)"
fi
# Per-event log (2026-08-04). Production shells use tee; restricted test/
# service sandboxes may refuse `/dev/fd` process substitution, so fall back to
# an ordinary append fd instead of aborting before the arm can report why.
if [[ "${EARSHOT_TEST_DISABLE_TEE:-0}" == 1 ]]; then
  : # focused-test/service-sandbox compatibility: leave inherited fds intact
else
  exec > >(tee -a "${STATE_DIR}/launch.log") 2>&1
fi
jnl() { "$SCRIPT_REPO/scripts/journal.sh" --event "$EVENT_ID" --actor launch-webcast.sh --action "$1" --outcome "$2" --detail "${3:-}" --log "${STATE_DIR}/launch.log" || true; }
page() {
  local notify="${EARSHOT_NOTIFY:-$REPO/scripts/notify.sh}"
  [[ -x "$notify" ]] && "$notify" "$1" "$2" --quiet || true
}
critical_alert() { # code, dedupe key, subject, body
  local code="$1" dedupe="$2" subject="$3" body="$4"
  local alertctl="${EARSHOT_ALERTCTL:-$REPO/scripts/alertctl.py}"
  if [[ -n "$RUN_DIR" && -x "$alertctl" ]]; then
    "$alertctl" raise --run-dir "$RUN_DIR" --severity critical \
      --code "$code" --dedupe-key "$dedupe" --subject "$subject" \
      --body "$body" \
      --retry-delay "${EARSHOT_ALERT_RETRY_DELAY:-30}" \
      || echo "CRITICAL ALERT DELIVERY UNRESOLVED: $subject" >&2
  else
    page "$subject" "$body"
  fi
}
lifecycle_record() { # $1 = typed transition, $2 = detail
  [[ -n "$RUN_DIR" ]] || return 0
  EARSHOT_REPO="$REPO" python3 "$EVENTCTL" record --run-dir "$RUN_DIR" \
    --transition "$1" --actor launch-webcast.sh --detail "$2"
}
listener_marker_matches_arm() {
  [[ -f "${STATE_DIR}/listener.ready" ]] || return 1
  python3 - "${STATE_DIR}/listener.ready" "$ARM_PID" <<'PY' >/dev/null 2>&1
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as fh:
    marker = json.load(fh)
raise SystemExit(0 if marker.get("pid") == int(sys.argv[2]) else 1)
PY
}
source_marker_matches_run() {
  [[ -f "${STATE_DIR}/source.attached" ]] || return 1
  python3 - "${STATE_DIR}/source.attached" "$ARM_PID" "$EVENT_ID" "$PLAN_RUN_ID" "$PLAN_SHA256" <<'PY' >/dev/null 2>&1
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as fh:
    marker = json.load(fh)
expected = {
    "pid": int(sys.argv[2]),
    "event_id": sys.argv[3],
    "run_id": sys.argv[4],
    "plan_sha256": sys.argv[5],
}
raise SystemExit(0 if all(marker.get(k) == v for k, v in expected.items())
                 and type(marker.get("accepted_frames")) is int
                 and marker["accepted_frames"] > 0 else 1)
PY
}
accept_source_marker() {
  source_marker_matches_run || return 1
  if lifecycle_record source_attached "source.attached accepted for arm_pid=${ARM_PID}" \
      && lifecycle_record supervising "webcast detector-path media attached; launcher owns the live arm"; then
    LIFECYCLE_SUPERVISING=1
    echo "source attached and lifecycle supervising"
    return 0
  fi
  return 2
}
reap_arm() {
  [[ -n "$ARM_PID" && -z "${ARM_REAPED:-}" ]] || return 0
  ARM_RC=0
  wait "$ARM_PID" || ARM_RC=$?
  ARM_REAPED=1
}
shutdown_arm() { # $1 = reason
  local why="$1" waited=0 arm_done=""
  [[ -n "$ARM_PID" ]] || return 0
  [[ -z "$SHUTDOWN_STARTED" ]] || { reap_arm; return 0; }
  SHUTDOWN_STARTED=1
  : > "${STATE_DIR}/STOP"
  jnl shutdown info "${why}: STOP asserted; drain budget ${SHUTDOWN_GRACE_SECS}s"
  while (( waited < SHUTDOWN_GRACE_SECS * 10 )); do
    if ! kill -0 "$ARM_PID" 2>/dev/null; then arm_done=1; break; fi
    sleep 0.1
    waited=$((waited + 1))
  done
  if [[ -z "$arm_done" ]] && kill -0 "$ARM_PID" 2>/dev/null; then
    echo "shutdown: arm did not finalize within ${SHUTDOWN_GRACE_SECS}s; escalating TERM" >&2
    jnl shutdown warn "arm drain timed out after ${SHUTDOWN_GRACE_SECS}s; TERM"
    kill -TERM "$ARM_PID" 2>/dev/null || true
    for _ in $(seq 1 100); do
      kill -0 "$ARM_PID" 2>/dev/null || { arm_done=1; break; }
      sleep 0.1
    done
  fi
  if [[ -z "$arm_done" ]] && kill -0 "$ARM_PID" 2>/dev/null; then
    echo "shutdown: arm ignored TERM for 10s; escalating KILL (finalizers unresolved)" >&2
    jnl shutdown fail "arm ignored TERM for 10s; KILL — finalizers unresolved"
    FINALIZERS_UNRESOLVED=1
    kill -KILL "$ARM_PID" 2>/dev/null || true
  fi
  reap_arm
}
report_is_complete() {
  python3 - "$REPORT" "$PLAN_RUN_ID" "$PLAN_SHA256" <<'PY'
import json, os, stat, sys
path, run_id, plan_sha = sys.argv[1:]
try:
    mode = os.lstat(path).st_mode
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        raise ValueError("runtime report is not a regular non-symlink file")
    with open(path, "r", encoding="utf-8") as fh:
        report = json.load(fh)
    checks = {
        "run_id": report.get("run_id") == run_id,
        "plan_sha256": report.get("plan_sha256") == plan_sha,
        "finalized": report.get("finalized") is True,
        "runtime_success": report.get("runtime_success") is True,
        "required_evidence_healthy": report.get("required_evidence_healthy") is True,
        "unresolved_cancellation_ids": report.get("unresolved_cancellation_ids") == [],
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError("runtime report failed completion predicate: " + ", ".join(failed))
except Exception as exc:
    print(str(exc))
    raise SystemExit(1)
print("finalized=true runtime_success=true required_evidence_healthy=true unresolved_cancellation_ids=[] identity=matched")
PY
}
finalize_lifecycle() {
  [[ -n "$RUN_DIR" ]] || return 0
  local report_check="" failure_detail="" report_ok=0
  if report_check=$(report_is_complete 2>&1); then report_ok=1; fi
  if [[ "$report_ok" == 1 ]] \
      && [[ "$LIFECYCLE_SUPERVISING" == 1 ]] \
      && [[ "$FINALIZERS_UNRESOLVED" == 0 ]]; then
    if lifecycle_record draining "arm exited rc=${ARM_RC:-unknown}; ${report_check}" \
        && lifecycle_record completed "runtime finalizers and all required evidence acknowledged; report=${REPORT}"; then
      jnl lifecycle ok "draining -> completed; ${report_check}"
      return 0
    fi
    failure_detail="could not append draining/completed lifecycle receipt"
  else
    if [[ "$report_ok" == 1 ]]; then
      failure_detail="runtime report passed but lifecycle/finalizer gate did not"
    else
      failure_detail="${report_check:-runtime report missing or malformed}"
    fi
    [[ "$LIFECYCLE_SUPERVISING" == 1 ]] || failure_detail="pre-attachment runtime exit; ${failure_detail}"
    [[ "$FINALIZERS_UNRESOLVED" == 0 ]] || failure_detail="forced KILL left finalizers unresolved; ${failure_detail}"
  fi
  echo "LIFECYCLE FAILED: ${failure_detail}" >&2
  jnl lifecycle fail "$failure_detail"
  lifecycle_record failed "${failure_detail}; arm_rc=${ARM_RC:-unknown}; report=${REPORT}" || true
  critical_alert runtime_failed lifecycle_terminal \
    "${EVENT_ID} runtime/evidence failure" \
    "Run ${EVENT_ID}/${PLAN_RUN_ID:-unknown} reached a failed terminal path. Required audio, book, order, report, or cancellation evidence did not satisfy the completion predicate. Inspect the typed lifecycle and ${REPORT}."
  return 1
}
handle_shutdown_signal() {
  local why="$1" exit_code=143
  [[ "$why" == "INT" ]] && exit_code=130
  trap - TERM INT
  shutdown_arm "$why"
  finalize_lifecycle || true
  exit "$exit_code"
}
# ── Green gate (2026-08-04, marker-activated) ───────────────────────────────
# Enforces ONLY when a preflight verdict exists for this event. Override
# EARSHOT_GREEN_OVERRIDE="<reason>" proceeds but pages.
if [[ -f "${STATE_DIR}/preflight-result.json" ]]; then
  if GREEN_OUT=$(scripts/assert-ready.sh "$CONFIG" 2>&1); then
    echo "$GREEN_OUT"
  elif [[ -n "${EARSHOT_GREEN_OVERRIDE:-}" ]]; then
    echo "GREEN GATE OVERRIDDEN: $GREEN_OUT (reason: $EARSHOT_GREEN_OVERRIDE)"
    page "GREEN GATE OVERRIDDEN: $EVENT_ID" "$GREEN_OUT — override reason: $EARSHOT_GREEN_OVERRIDE"
    jnl note warn "green gate overridden: $EARSHOT_GREEN_OVERRIDE"
  else
    echo "REFUSED by green gate: $GREEN_OUT" >&2
    page "$EVENT_ID REFUSED by green gate" "$GREEN_OUT — fix and relaunch, or EARSHOT_GREEN_OVERRIDE=\"<reason>\" (pages)."
    jnl launch-start refused "$GREEN_OUT"
    exit 7
  fi
fi
jnl launch-start info "config=$CONFIG"

# Self-check for post-mortem #3: prove this shell can trap SIGTERM and write
# the state dir — a sandboxed parent that swallows signals or blocks writes
# fails HERE, before the event, not 60 s into the call.
trap 'true' TERM
touch "${STATE_DIR}/.launch-selfcheck" 2>/dev/null || {
  echo "REFUSING: cannot write ${STATE_DIR} — launch from a plain shell (post-mortem #3)" >&2; exit 2
}
rm -f "${STATE_DIR}/.launch-selfcheck"
trap - TERM
trap 'handle_shutdown_signal TERM' TERM
trap 'handle_shutdown_signal INT' INT

# --wait-live: poll until the stream is actually live, THEN arm (launch on
# the flip; arm bring-up is ~8 s, cheap next to a pre-live stall).
if [[ -n "$WAIT_LIVE" ]]; then
  [[ "$WAIT_KIND" == "youtube" || "$WAIT_KIND" == "hls" ]] || {
    echo "--wait-live requires --wait-kind youtube|hls" >&2; exit 2
  }
  # Accept a URL file (the run-<ev> pattern) or a literal URL.
  URL="$WAIT_LIVE"
  [[ -f "$WAIT_LIVE" ]] && URL="$(cat "$WAIT_LIVE")"
  [[ -n "$URL" ]] || { echo "REFUSING: --wait-live target is empty" >&2; exit 2; }
  echo "live gate: polling ($WAIT_KIND) every 15 s — arm launches on the flip"
  while :; do
    case "$WAIT_KIND" in
      youtube)
        # RUNBOOK 07-22 fix #1: poll is_live; --wait-for-video is unvalidated
        # at the pre-live→live transition.
        IS_LIVE=$(yt-dlp --js-runtimes node --print "%(is_live)s" "$URL" 2>/dev/null || true)
        [[ "$IS_LIVE" == "True" ]] && break
        ;;
      hls)
        # Live = manifest resolves and lists at least one media segment.
        BODY=$(curl -fsS --max-time 10 "$URL" 2>/dev/null || true)
        if [[ -n "$BODY" ]]; then
          if grep -q "EXTINF" <<<"$BODY"; then
            break
          fi
          # Master playlist: follow the first variant. tr strips CRLF —
          # real-world manifests (DW/Akamai) end lines with \r\n and a
          # trailing \r makes curl reject the URL as malformed.
          VARIANT=$(grep -v '^#' <<<"$BODY" | head -1 | tr -d '\r')
          if [[ -n "$VARIANT" ]]; then
            [[ "$VARIANT" != http* ]] && VARIANT="${URL%/*}/$VARIANT"
            # Capture, then grep: `curl | grep -q` SIGPIPEs curl on first
            # match, and under pipefail the pipeline rc goes nonzero — the
            # break silently never fires (found in the 07-23 dress rehearsal).
            VBODY=$(curl -fsS --max-time 10 "$VARIANT" 2>/dev/null || true)
            if grep -q "EXTINF" <<<"$VBODY"; then
              break
            fi
          fi
        fi
        ;;
    esac
    sleep 15
  done
  echo "live gate: stream is LIVE ($(date -u +%FT%TZ)) — arming now"
fi

if (( ! ACCOUNT_SOURCED )); then
  # shellcheck disable=SC1090
  source "$ACCOUNT"
  ACCOUNT_SOURCED=1
fi

# The posture line must state what this run ACTUALLY is. It previously printed
# "(observe posture)" unconditionally, which was true only because the flag could
# not be passed — the same class of defect as V6's health line that went silent
# exactly when the leg broke (docs/LATENCY-PROOF.md §V6).
LIVE_ARGS=()
if (( LIVE )); then
  LIVE_ARGS+=(--i-am-live)
  if [[ -n "$FILLABLE" ]]; then
    LIVE_ARGS+=("$FILLABLE")
    # State the truth: this is the posture in which an order can actually FILL.
    # The old line claimed "orders WILL be sent" for the 1¢ probe, where in fact
    # nothing was ever sent — never describe a posture by its flag, only by what
    # it does at the wire.
    # Derive the bound from THIS config rather than repeating a literal that
    # can drift out of sync with risk.rs — a posture line that misstates the
    # cap is the exact defect class found on 26AUG05 (see the D2 note above).
    CFG_MAX_CAP=$(grep -oE '^max_price_cents[[:space:]]*=[[:space:]]*[0-9]+' "$CONFIG" \
                  | grep -oE '[0-9]+$' | sort -n | tail -1)
    CFG_MAX_SIZE=$(grep -oE '^budget_contracts[[:space:]]*=[[:space:]]*[0-9]+' "$CONFIG" \
                   | grep -oE '[0-9]+$' | sort -n | tail -1)
    POSTURE="LIVE mention-FILL (orders MAY cross the ask and FILL; max cap ${CFG_MAX_CAP:-?}¢ × ${CFG_MAX_SIZE:-?} contract(s) per order, spend bound by configured notional caps + the event ledger)"
  else
    POSTURE="LIVE mention-probe (cap ≤ 1¢ non-crossing — an order reaches the wire ONLY if the ask is ≤ 1¢; otherwise it short-circuits unsent)"
  fi
else
  POSTURE="DRY RUN (no orders are sent; the risk gate is NOT exercised)"
fi
echo "webcast lane: event=${EVENT_ID}  config=${CONFIG}  state=${STATE_DIR}"
echo "posture: ${POSTURE}"

# Audio evidence is now an in-process, fail-closed stage on the exact
# post-preprocessor frames sent toward detection.  A second pull can drift from
# what the detector heard and can never serve as the live run's audit record;
# launch-webcast therefore starts exactly one source graph and no sidecar.
if [[ -n "$BIN" ]]; then
  [[ -x "$BIN" ]] || { echo "pinned binary not executable: ${BIN}" >&2; exit 2; }
  jnl arm-start info "pinned $BIN — $POSTURE"
  echo "arming (pinned ${BIN}): arm --config ${CONFIG} --state-dir ${STATE_DIR} --report ${REPORT} --allow-mention ${LIVE_ARGS[*]:-}"
  "$BIN" arm --config "$CONFIG" --state-dir "$STATE_DIR" --report "$REPORT" --allow-mention ${LIVE_ARGS[@]+"${LIVE_ARGS[@]}"} &
else
  jnl arm-start warn "cargo run (UNPINNED — the runbook forbids this for real events) — $POSTURE"
  echo "arming (cargo run): arm --config ${CONFIG} --state-dir ${STATE_DIR} --report ${REPORT} --allow-mention ${LIVE_ARGS[*]:-}"
  cargo run --release -- arm --config "$CONFIG" --state-dir "$STATE_DIR" --report "$REPORT" --allow-mention ${LIVE_ARGS[@]+"${LIVE_ARGS[@]}"} &
fi
ARM_PID=$!
printf '%s\n' "$ARM_PID" > "${STATE_DIR}/arm.pid"

# Dry runs preserve the old untyped behavior: no RunPlan lookup, no lifecycle
# mutation, and the arm's own status remains the launcher status.
if (( ! LIVE )); then
  reap_arm
  exit "${ARM_RC:-1}"
fi

# `earshot arm` writes listener.ready only after the graph is constructed and
# released.  Require this process's PID so a stale/live sibling marker can
# never advance the selected RunPlan.
for _ in $(seq 1 240); do
  listener_marker_matches_arm && break
  kill -0 "$ARM_PID" 2>/dev/null || break
  sleep 0.5
done
if ! listener_marker_matches_arm; then
  echo "arm never wrote a matching ${STATE_DIR}/listener.ready within 120 s" >&2
  if kill -0 "$ARM_PID" 2>/dev/null; then shutdown_arm "listener readiness timeout"; else reap_arm; fi
  finalize_lifecycle || true
  [[ "${ARM_RC:-0}" -ne 0 ]] && exit "$ARM_RC"
  exit 7
fi
echo "listener ready for arm pid ${ARM_PID}"
jnl listener-ready ok "pid=${ARM_PID}"
if ! lifecycle_record launched "listener.ready accepted; arm_pid=${ARM_PID} state=${STATE_DIR}"; then
  echo "REFUSING to supervise: failed to append durable launched receipt for ${RUN_DIR}" >&2
  shutdown_arm "launched receipt failure"
  finalize_lifecycle || true
  exit 7
fi
LIFECYCLE_LAUNCHED=1

# Source attachment is stronger than process existence or a decodable URL: the
# runtime publishes this marker only after its in-process evidence bank has
# durably accepted a post-preprocessor frame that is eligible for detection.
while kill -0 "$ARM_PID" 2>/dev/null; do
  ATTACH_RC=0
  accept_source_marker || ATTACH_RC=$?
  if [[ "$ATTACH_RC" -eq 0 ]]; then
    break
  elif [[ "$ATTACH_RC" -eq 2 ]]; then
    echo "LIFECYCLE FAILURE: source attached but source_attached/supervising receipt failed" >&2
    shutdown_arm "source lifecycle receipt failure"
    break
  fi
  sleep 0.1
done
if [[ "$LIFECYCLE_SUPERVISING" == 0 ]] && source_marker_matches_run; then
  ATTACH_RC=0
  accept_source_marker || ATTACH_RC=$?
  if [[ "$ATTACH_RC" -eq 2 ]]; then
    echo "LIFECYCLE FAILURE: completed source attached but lifecycle receipt failed" >&2
  fi
fi

reap_arm
jnl arm-exit "$([[ "${ARM_RC:-1}" -eq 0 ]] && echo ok || echo fail)" "rc=${ARM_RC:-unknown} report=${REPORT}"
LIFECYCLE_RC=0
finalize_lifecycle || LIFECYCLE_RC=$?
if [[ "$LIFECYCLE_RC" -ne 0 && "${ARM_RC:-0}" -eq 0 ]]; then
  exit 7
fi
exit "${ARM_RC:-1}"
