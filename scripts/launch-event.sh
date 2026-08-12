#!/usr/bin/env bash
# Launch ONE isolated live event: source that event's account env, start its own
# ngrok tunnel, arm the pipeline with a per-event state dir, then dial. Run once
# per event (each with its own config + account env file + bind port) to trade
# two simultaneous earnings calls on two separate Kalshi + Twilio accounts
# without any cross-wiring (docs/MULTI-EVENT.md).
#
#   scripts/launch-event.sh \
#     --config configs/eventA.toml \
#     --account ~/.earshot/acctA.env \
#     --bind-port 8080 \
#     --to +18883243618 --dtmf "1364784#" \
#     [--i-am-live] [--allow-mention] [--fillable]
#
# The account env file is the single source of truth for THIS event's secrets:
# it must export the Kalshi pair (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH,
# used only if the config has no [kalshi] key_id) and the Twilio account
# (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER). Because each
# invocation sources a DIFFERENT account file into its own process environment,
# and the config binds the Kalshi key + Twilio auth token to this account,
# event A can never sign with event B's credentials.
#
# Isolation this script enforces per event:
#   - distinct --account env file  → distinct Kalshi + Twilio credentials
#   - distinct --bind-port         → distinct WS listener + ngrok tunnel
#   - distinct state dir (state/<event-id>) → distinct $1 ledger + STOP kill file
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
SCRIPT_REPO=$(cd "${SCRIPT_DIR}/.." && pwd)

CONFIG="" ACCOUNT="" BIND_PORT="" TO="" DTMF="" LIVE="" ALLOW_MENTION="" FILLABLE="" NGROK_CONFIG=""
DIGIT_DELAY="" DTMF_REPEAT=""   # empty => twilio-dial.sh defaults (4 s, play once)
BIN="${EARSHOT_BIN:-}" NO_DIAL="" STREAM_URL_OVERRIDE="" TLS_HOST="" OPERATOR_HANDLER=""
MAX_MINUTES_OVERRIDE="" CALL_END_GRACE_OVERRIDE=""
RUN_DIR_OVERRIDE="" REPORT=""
SHUTDOWN_GRACE_SECS="${EARSHOT_LAUNCH_SHUTDOWN_GRACE_SECS:-90}"
ARM_PID="" REDIAL_PID="" WATCHDOG_PID="" NGROK_PID="" SHUTDOWN_STARTED=""
ARM_RC="" ARM_REAPED="" FINALIZERS_UNRESOLVED=0
PLAN_RUN_ID="" PLAN_SHA256="" LIFECYCLE_LAUNCHED=0 LIFECYCLE_SUPERVISING=0
ADMISSION_WATCHER_PID="" ADMISSION_WATCHER_FAILED=0 ADMISSION_WATCHER_FAULT_FILE=""
ADMISSION_WATCHER_RESULT_FILE=""
ADMISSION_RECEIPT_HELPER="${SCRIPT_REPO}/scripts/held-admission-receipt.py"
DIAL_SCRIPT="scripts/twilio-dial.sh" DIAL_ATTEMPTS=3 ONE_SHOT_OK="" HANGUP_SCRIPT=""
# 90 s covers slow PSTN origination + ring with margin: the stream forks at
# ANSWER (<Start><Stream> precedes <Play digits>), so real time-to-first-media
# is ~4-30 s (Q-REDIAL timeline; MSFT bridge-entry transcript hears audio from
# 0.00 s of the call).
STREAM_CONFIRM_SECS=90
while [[ $# -gt 0 ]]; do
  case "$1" in
    # Operator-screened (Class B) venues: page the configured human cell into a
    # conference FIRST, then place the bridge call with the gated operator TwiML
    # (scripts/twilio-operator-dial.sh). Requires [operator_handler] enabled=true
    # in the config; the default listen-only dial path is untouched otherwise.
    --operator-handler) OPERATOR_HANDLER=1; shift ;;
    --config)        CONFIG="$2"; shift 2 ;;
    --account)       ACCOUNT="$2"; shift 2 ;;
    --bind-port)     BIND_PORT="$2"; shift 2 ;;
    --to)            TO="$2"; shift 2 ;;
    --dtmf)          DTMF="$2"; shift 2 ;;
    # Per-venue DTMF timing, passed straight through to twilio-dial.sh. Added
    # 2026-07-29: the 4 s / play-once default fits CMG (held 4572 s) but not
    # MSFT or CVNA, whose bridges answered and hung up at 21-73 s because the
    # code landed while their IVR was still prompting. Omit both to keep the
    # historical TwiML byte-for-byte.
    --digit-delay)   DIGIT_DELAY="$2"; shift 2 ;;
    --dtmf-repeat)   DTMF_REPEAT="$2"; shift 2 ;;
    --ngrok-config)  NGROK_CONFIG="$2"; shift 2 ;;
    --stream-url)    STREAM_URL_OVERRIDE="$2"; shift 2 ;;  # tunnel-free: skip ngrok, endpoint terminates on the box
    # TLS-on-box shorthand (docs/INGRESS-BAKEOFF.md, deploy/caddy/Caddyfile):
    # composes wss://<host>/e/<bind-port> — the per-event path caddy proxies to
    # this bind port. Q-TLS must be pinned before an event rides this.
    --tls-host)      TLS_HOST="$2"; shift 2 ;;
    # Redial-supervisor time budgets. Both were previously unreachable: the
    # launcher passed only --max-redials, so the supervisor kept its own
    # defaults regardless of when this event's call actually starts or ends.
    --max-minutes)     MAX_MINUTES_OVERRIDE="$2"; shift 2 ;;
    --call-end-grace)  CALL_END_GRACE_OVERRIDE="$2"; shift 2 ;;
    --bin)           BIN="$2"; shift 2 ;;
    # Immutable lifecycle run selected by eventctl. Live launches require a
    # RunPlan; --run-dir is an exact override for rehearsals/recovery tooling.
    # Without it, the sole active run for [event].id is resolved by eventctl.
    --run-dir)       RUN_DIR_OVERRIDE="$2"; shift 2 ;;
    # First-dial resilience (2026-08-04, Gap A): the dial script's classified
    # exits (4 permanent / 5 transient / 6 ambiguous) drive a bounded retry.
    # --dial-script exists for stub tests and is symmetric with the
    # supervisor's knob; production keeps the default.
    --dial-script)   DIAL_SCRIPT="$2"; shift 2 ;;
    --hangup-script) HANGUP_SCRIPT="$2"; shift 2 ;;   # forwarded to the supervisor; stub tests only
    --shutdown-grace-secs) SHUTDOWN_GRACE_SECS="$2"; shift 2 ;; # bounded evidence/report drain
    --dial-attempts) DIAL_ATTEMPTS="$2"; shift 2 ;;
    # A config without mid-call persistence (max_reconnects unset/0) is
    # refused unless this flag names the one-shot as deliberate.
    --one-shot-ok)   ONE_SHOT_OK=1; shift ;;
    # No-stream watchdog window (a placed call is not a stream — 2026-07-29).
    --stream-confirm-secs) STREAM_CONFIRM_SECS="$2"; shift 2 ;;
    --no-dial)       NO_DIAL=1; shift ;;
    --i-am-live)     LIVE="--i-am-live"; shift ;;
    --allow-mention) ALLOW_MENTION="--allow-mention"; shift ;;
    --fillable)      FILLABLE="--fillable"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$CONFIG" && -n "$ACCOUNT" && -n "$BIND_PORT" && -n "$TO" ]] || {
  echo "usage: $0 --config <cfg> --account <env-file> --bind-port <port> --to +1... [--dtmf 1234#] [--ngrok-config <yml> | --stream-url wss://host/media] [--run-dir <immutable-run>] [--i-am-live] [--allow-mention] [--fillable]" >&2
  exit 2
}
[[ -f "$CONFIG" ]]  || { echo "config not found: $CONFIG" >&2; exit 2; }
[[ -f "$ACCOUNT" ]] || { echo "account env file not found: $ACCOUNT" >&2; exit 2; }
[[ "$SHUTDOWN_GRACE_SECS" =~ ^[0-9]+$ && "$SHUTDOWN_GRACE_SECS" -ge 75 ]] || {
  echo "--shutdown-grace-secs must be an integer >= 75 (got ${SHUTDOWN_GRACE_SECS})" >&2; exit 2
}
# Listen-only invariant (docs/TWILIO.md): DTMF is passcode-entry only, charset
# [0-9#w]; '*' (raise-hand/unmute) hard-rejected so we can never enter a Q&A
# queue on a live call. Fail here too; twilio-dial.sh is the final chokepoint.
if [[ -n "$DTMF" && ! "$DTMF" =~ ^[0-9#w]+$ ]]; then
  echo "REFUSING: --dtmf \"$DTMF\" has a disallowed char (charset [0-9#w]; '*' never permitted)." >&2
  exit 3
fi
# The per-event web-API port is derived from the bind port; keep it >= 4040 so
# two events (8080/8081/...) map to 4040/4041/... without clashing ngrok's default.
[[ "$BIND_PORT" =~ ^[0-9]+$ && "$BIND_PORT" -ge 8080 ]] || {
  echo "--bind-port must be an integer >= 8080 (got ${BIND_PORT})" >&2; exit 2
}
if [[ -n "$TLS_HOST" ]]; then
  [[ -z "$STREAM_URL_OVERRIDE" ]] || { echo "--tls-host and --stream-url are mutually exclusive" >&2; exit 2; }
  STREAM_URL_OVERRIDE="wss://${TLS_HOST}/e/${BIND_PORT}"
fi

# This event's own secrets, into THIS process only.
# shellcheck disable=SC1090
source "$ACCOUNT"
: "${TWILIO_ACCOUNT_SID:?}" "${TWILIO_AUTH_TOKEN:?}" "${TWILIO_FROM_NUMBER:?}"

# streaming_ctc dlopens onnxruntime. With ORT_DYLIB_PATH unset the DETECTOR
# BUILD hangs — and because build_pipeline_input constructs the source (binding
# the WS listener) BEFORE the detector, the port is already open when the hang
# happens. Everything downstream then lies: `nc -z` below passes, ngrok logs
# "join connections", Twilio dials in and the media socket sits unaccepted in
# the kernel backlog (Recv-Q > 0) while the arm prints nothing after "twilio
# stream source bound". Cost 2026-07-29: the whole phone lane, diagnosed as a
# WS/signature bug for hours. Same silent-hang class as RUNBOOK bug 1 (Ford
# 07-28 ORT dylib), whose guards existed only in run-arm.sh/launch-webcast.sh —
# never here, on the TELEPHONY launcher. Resolve + REFUSE before the dial.
if grep -qE '^[[:space:]]*engine[[:space:]]*=[[:space:]]*"streaming_ctc"' "$CONFIG"; then
  if [[ -z "${ORT_DYLIB_PATH:-}" ]]; then
    # Same two fixes as launch-webcast.sh (2026-08-03): '*.so*'-only never
    # resolves on Darwin, and $HOME/ear is the box layout, not the laptop's.
    # dSYM exclusion because that bundle's same-named file HANGS dlopen.
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

# Per-event state dir, keyed off the config's [event] id so ledgers/STOP never
# collide between concurrent events. Parsed with sed, not python tomllib —
# tomllib needs python ≥3.11 and the launchd PATH may resolve an older one
# (audit F14); the id line is generated by `earshot prepare`, shape-stable.
EVENT_ID=$(sed -n '/^\[event\]/,/^\[/s/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
[[ -n "$EVENT_ID" ]] || { echo "could not parse [event].id from ${CONFIG}" >&2; exit 2; }

# Dry/rehearsal launches retain their isolated cwd-relative state contract.
# A live launch is different: it must consume exactly one verified immutable
# RunPlan, bind the runtime to the plan's host/account identity, write runtime
# state under the canonical repository state root, and send the report to the
# RunPlan artifact path. Without these bindings a shell invocation could arm
# from fresh config bytes while evidence lands in a legacy state/report path.
REPO="${EARSHOT_REPO:-$SCRIPT_REPO}"
[[ -d "$REPO" ]] || { echo "EARSHOT_REPO is not a directory: $REPO" >&2; exit 2; }
REPO=$(cd "$REPO" && pwd -P)
EVENTCTL="${EARSHOT_EVENTCTL:-${SCRIPT_REPO}/scripts/eventctl.py}"
RUN_DIR="" PLAN_PATH="" PLAN_REPORT=""
if [[ -n "$LIVE" ]]; then
  [[ -f "$EVENTCTL" ]] || { echo "REFUSING live launch: eventctl not found: $EVENTCTL" >&2; exit 2; }
  if [[ -n "$RUN_DIR_OVERRIDE" ]]; then
    [[ -d "$RUN_DIR_OVERRIDE" ]] || { echo "REFUSING live launch: --run-dir is not a directory: $RUN_DIR_OVERRIDE" >&2; exit 2; }
    RUN_DIR=$(cd "$RUN_DIR_OVERRIDE" && pwd -P)
  else
    STATUS_JSON=$(EARSHOT_REPO="$REPO" python3 "$EVENTCTL" status --event "$EVENT_ID" --json) || {
      echo "REFUSING live launch: eventctl could not resolve one active run for $EVENT_ID" >&2
      exit 7
    }
    RUN_ID=$(printf '%s' "$STATUS_JSON" | python3 -c 'import json,sys; v=json.load(sys.stdin); r=v.get("run"); print(r if isinstance(r,str) else "")') || {
      echo "REFUSING live launch: eventctl status returned invalid JSON for $EVENT_ID" >&2
      exit 7
    }
    [[ -n "$RUN_ID" ]] || { echo "REFUSING live launch: eventctl status omitted run id for $EVENT_ID" >&2; exit 7; }
    RUN_DIR="${REPO}/var/runs/${EVENT_ID}/${RUN_ID}"
    [[ -d "$RUN_DIR" ]] || { echo "REFUSING live launch: resolved run directory missing: $RUN_DIR" >&2; exit 7; }
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
  [[ -f "$PLAN_PATH" ]] || { echo "REFUSING live launch: immutable RunPlan missing: $PLAN_PATH" >&2; exit 7; }
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
    raise SystemExit("launch-event.sh requires a schema-3 RunPlan")
if plan.get("event_id") != event:
    raise SystemExit("RunPlan event_id does not match config")
if plan.get("lane") != "phone":
    raise SystemExit("launch-event.sh requires a phone RunPlan")
if plan.get("execution_enabled") is not True or plan.get("capture_only") is not False:
    raise SystemExit("--i-am-live requires an execution-enabled RunPlan")
if plan.get("canonical_state_root") != "state":
    raise SystemExit("phone live state must use RunPlan canonical_state_root=state")
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
    echo "REFUSING live launch: config '$CONFIG_REAL' is not immutable RunPlan config '$PLAN_CONFIG'" >&2; exit 7;
  }
  [[ -n "${EARSHOT_EXECUTION_HOST:-}" ]] || {
    echo "REFUSING live launch: EARSHOT_EXECUTION_HOST must be exported by the event/account environment" >&2; exit 7;
  }
  [[ -n "${EARSHOT_KALSHI_ACCOUNT_FINGERPRINT:-}" ]] || {
    echo "REFUSING live launch: EARSHOT_KALSHI_ACCOUNT_FINGERPRINT must be exported; it is never derived from a private key in shell" >&2; exit 7;
  }
  [[ "$EARSHOT_EXECUTION_HOST" == "$PLAN_HOST" ]] || {
    echo "REFUSING live launch: execution host '$EARSHOT_EXECUTION_HOST' != RunPlan '$PLAN_HOST'" >&2; exit 7;
  }
  [[ "$EARSHOT_KALSHI_ACCOUNT_FINGERPRINT" == "$PLAN_ACCOUNT_FINGERPRINT" ]] || {
    echo "REFUSING live launch: account fingerprint does not match immutable RunPlan" >&2; exit 7;
  }
  export EARSHOT_RUN_PLAN="$PLAN_PATH"
  export EARSHOT_EXECUTION_HOST EARSHOT_KALSHI_ACCOUNT_FINGERPRINT
  export EARSHOT_CANONICAL_STATE_ROOT="${REPO}/state"
  STATE_DIR="${REPO}/state/${EVENT_ID}"
else
  [[ -z "$RUN_DIR_OVERRIDE" ]] || { echo "--run-dir is valid only with --i-am-live" >&2; exit 2; }
  STATE_DIR="state/${EVENT_ID}"
fi
mkdir -p "$STATE_DIR"
ADMISSION_WATCHER_FAULT_FILE="${STATE_DIR}/admission-watcher.failed"
ADMISSION_WATCHER_RESULT_FILE="${STATE_DIR}/admission-watcher.last"
# These are process-control artifacts, not admission evidence. A new launcher
# owns a new watcher even though the runtime's nonce-bound admission receipt is
# deliberately left in place for the helper to validate or renew.
rm -f "$ADMISSION_WATCHER_FAULT_FILE" "$ADMISSION_WATCHER_RESULT_FILE" \
  "${STATE_DIR}/admission-watcher.pid"
# Same auto-pin as launch-webcast.sh (2026-08-05 rehearsal catch): a scheduled
# context has no cargo on PATH; when preflight pinned, run the pin.
if [[ -z "$BIN" && -x "${STATE_DIR}/earshot-pinned" ]]; then
  BIN="${STATE_DIR}/earshot-pinned"
  echo "pinned binary auto-selected: $BIN"
fi
# Everything from here lands in the per-event log too (2026-08-04: this
# launcher's stdout was terminal scrollback only — zero run evidence survived
# the day). Pattern proven live at arm-on-url.sh. Pre-EVENT_ID output above
# stays terminal-only by design: no event dir exists to file it under.
if [[ "${EARSHOT_TEST_DISABLE_TEE:-0}" != 1 ]]; then
  exec > >(tee -a "${STATE_DIR}/launch.log") 2>&1
fi
echo "event=${EVENT_ID}  account=${ACCOUNT}  port=${BIND_PORT}  state=${STATE_DIR}"
jnl() { scripts/journal.sh --event "$EVENT_ID" --actor launch-event.sh --action "$1" --outcome "$2" --detail "${3:-}" --log "${STATE_DIR}/launch.log" || true; }
page() { # $1 = subject, $2 = body; a dead notifier never kills a live event
  "${EARSHOT_NOTIFY:-scripts/notify.sh}" "$1" "$2" --quiet || true
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
    --transition "$1" --actor launch-event.sh --detail "$2"
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
  [[ -n "$RUN_DIR" && -f "${STATE_DIR}/source.attached" ]] || return 1
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
      && lifecycle_record supervising "phone detector-path media attached; launcher and redial supervisor own the live arm"; then
    LIFECYCLE_SUPERVISING=1
    echo "source attached and lifecycle supervising"
    return 0
  fi
  return 2
}
stream_marker_identity() {
  python3 - "${STATE_DIR}/stream.connected" <<'PY'
import json, os, re, stat, sys

path = sys.argv[1]
info = os.lstat(path)
if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
    raise SystemExit("stream.connected is not a regular non-symlink file")
with open(path, "r", encoding="utf-8") as fh:
    marker = json.load(fh)
sid = marker.get("call_sid")
attempt = marker.get("attempt")
if not isinstance(sid, str) or not re.fullmatch(r"CA[0-9a-fA-F]{32}", sid):
    raise SystemExit("stream.connected has no valid CallSid")
if type(attempt) is not int or not 0 <= attempt <= 0xFFFF_FFFF:
    raise SystemExit("stream.connected has no valid nonnegative attempt")
print(f"{sid}|{attempt}")
PY
}
record_admission_watcher_fault() { # $1 = helper rc, $2 = identity, $3 = detail
  local fault_rc="$1" fault_identity="$2" fault_detail="$3"
  local fault_tmp="${ADMISSION_WATCHER_FAULT_FILE}.tmp"
  # The decision gate must close before notification: a slow/dead notifier can
  # never delay the runtime's session-scoped STOP drain.
  : > "${STATE_DIR}/STOP"
  (
    umask 077
    printf 'rc=%s\nidentity=%s\ndetail=%s\n' \
      "$fault_rc" "$fault_identity" "$fault_detail" > "$fault_tmp"
  ) && mv "$fault_tmp" "$ADMISSION_WATCHER_FAULT_FILE" || true
  echo "ADMISSION WATCHER FAILURE: identity=${fault_identity} rc=${fault_rc}: ${fault_detail}" >&2
  jnl admission-watcher fail "identity=${fault_identity} rc=${fault_rc}: ${fault_detail}; STOP asserted"
  page "ADMISSION RECEIPT FAILED: ${EVENT_ID}" "The held-admission receipt helper failed for stream ${fault_identity} (exit ${fault_rc}). STOP was asserted before this page so the runtime can drain safely. Inspect ${ADMISSION_WATCHER_FAULT_FILE}, ${STATE_DIR}/admission.assertions.jsonl, and ${RUN_DIR}/control/receipts.jsonl. Detail: ${fault_detail}"
}
record_admission_watcher_result() { # non-authoritative watcher observability
  local result_rc="$1" result_identity="$2" result_tmp="${ADMISSION_WATCHER_RESULT_FILE}.tmp"
  (
    umask 077
    printf 'rc=%s\nidentity=%s\n' "$result_rc" "$result_identity" > "$result_tmp"
  ) && mv "$result_tmp" "$ADMISSION_WATCHER_RESULT_FILE" || true
}
start_admission_watcher() {
  [[ -n "$RUN_DIR" ]] || return 0
  [[ -z "$ADMISSION_WATCHER_PID" ]] || return 0
  (
    # Every command whose status matters is handled explicitly below. This
    # keeps an incidental shell status from silently killing the watcher under
    # the launcher's `set -e` posture.
    set +e
    trap - TERM INT EXIT
    # Bash 3.2 has no associative arrays. CallSid/attempt values cannot contain
    # semicolons, so this delimited string is a safe set and prevents a stale
    # A -> B -> A marker replay from invoking the helper twice for A.
    seen_identities=";"
    last_marker_fingerprint="<not-observed>"
    while kill -0 "$ARM_PID" 2>/dev/null; do
      if [[ ! -e "${STATE_DIR}/stream.connected" && ! -L "${STATE_DIR}/stream.connected" ]]; then
        sleep 0.1
        continue
      fi

      # Polling is cheap for a multi-hour arm: Bash reads the tiny marker and
      # Python parses it only after its bytes/type change. Atomic redial
      # replacement therefore wakes the semantic identity check without
      # spawning a Python interpreter ten times per second while it is stable.
      marker_read_rc=0
      marker_raw=""
      if [[ -L "${STATE_DIR}/stream.connected" || ! -f "${STATE_DIR}/stream.connected" ]]; then
        marker_fingerprint="<non-regular-stream-marker>"
      else
        marker_raw=$(< "${STATE_DIR}/stream.connected") || marker_read_rc=$?
        marker_fingerprint="${marker_read_rc}|${marker_raw}"
      fi
      if [[ "$marker_fingerprint" == "$last_marker_fingerprint" ]]; then
        sleep 0.1
        continue
      fi
      last_marker_fingerprint="$marker_fingerprint"

      identity=""
      identity_detail=""
      identity_rc=0
      identity=$(stream_marker_identity 2>&1) || identity_rc=$?
      if [[ "$identity_rc" -ne 0 ]]; then
        identity_detail="$identity"
        identity="<invalid-stream-marker>"
      elif [[ "$seen_identities" == *";${identity};"* ]]; then
        sleep 0.1
        continue
      fi

      helper_output=""
      helper_rc=0
      if [[ ! -x "$ADMISSION_RECEIPT_HELPER" ]]; then
        helper_rc=127
        helper_output="held-admission receipt helper is missing or not executable: ${ADMISSION_RECEIPT_HELPER}"
      else
        helper_output=$(EARSHOT_REPO="$REPO" "$ADMISSION_RECEIPT_HELPER" \
          --config "$CONFIG" --run-plan "$PLAN_PATH" --state-dir "$STATE_DIR" 2>&1) || helper_rc=$?
      fi
      case "$helper_rc" in
        0)
          seen_identities="${seen_identities}${identity};"
          record_admission_watcher_result 0 "$identity"
          echo "admission-watcher: current/open identity=${identity}: ${helper_output}"
          jnl admission-watcher ok "current/open identity=${identity}: ${helper_output}"
          ;;
        3)
          # Operator-screened, unknown, stale, and every other non-held
          # registry posture remain closed. They are expected policy, not an
          # operational page, and this watcher never manufactures an operator
          # assertion.
          seen_identities="${seen_identities}${identity};"
          record_admission_watcher_result 3 "$identity"
          echo "admission-watcher: expected closed/ineligible identity=${identity}: ${helper_output}"
          jnl admission-watcher info "expected closed/ineligible identity=${identity}: ${helper_output}"
          ;;
        *)
          [[ -n "$identity_detail" ]] && helper_output="${helper_output}; marker=${identity_detail}"
          record_admission_watcher_fault "$helper_rc" "$identity" "$helper_output"
          exit "$helper_rc"
          ;;
      esac
      sleep 0.1
    done
    exit 0
  ) &
  ADMISSION_WATCHER_PID=$!
  if ! printf '%s\n' "$ADMISSION_WATCHER_PID" > "${STATE_DIR}/admission-watcher.pid"; then
    kill "$ADMISSION_WATCHER_PID" 2>/dev/null || true
    wait "$ADMISSION_WATCHER_PID" 2>/dev/null || true
    ADMISSION_WATCHER_PID=""
    return 1
  fi
  echo "admission watcher up (pid ${ADMISSION_WATCHER_PID}; starts after durable source attachment)"
  jnl admission-watcher-start info "pid=${ADMISSION_WATCHER_PID} helper=${ADMISSION_RECEIPT_HELPER}"
  return 0
}
stop_admission_watcher() {
  local watcher_rc=0 watcher_pid="${ADMISSION_WATCHER_PID:-}"
  if [[ -n "$watcher_pid" ]]; then
    kill "$watcher_pid" 2>/dev/null || true
    wait "$watcher_pid" 2>/dev/null || watcher_rc=$?
    ADMISSION_WATCHER_PID=""
  fi
  if [[ -f "$ADMISSION_WATCHER_FAULT_FILE" ]]; then
    ADMISSION_WATCHER_FAILED=1
  elif [[ "$watcher_rc" -ne 0 && "$watcher_rc" -ne 143 ]]; then
    ADMISSION_WATCHER_FAILED=1
    record_admission_watcher_fault "$watcher_rc" "<watcher-process>" \
      "admission watcher exited unexpectedly without a fault marker"
  fi
  return 0
}
reap_arm() {
  [[ -n "$ARM_PID" && -z "$ARM_REAPED" ]] || return 0
  ARM_RC=0
  wait "$ARM_PID" || ARM_RC=$?
  ARM_REAPED=1
}

# TERM/INT is a coordinated shutdown request, not permission to discard the
# runtime's finalizers. Once an arm exists, assert the session STOP boundary
# first (risk disarms and cancels only this run's orders), hang up each current
# call leg, stop auxiliary processes, and give the arm a bounded evidence /
# report drain. Only after that budget expires do we escalate TERM and KILL.
shutdown_children() { # $1 = signal label (TERM/INT/EXIT)
  local why="${1:-EXIT}" sid="" waited=0 arm_done="" child_pid="" sid_file=""
  # Bash inherits EXIT traps into command-substitution/background subshells.
  # Cleanup belongs to the one launcher process only; otherwise the harmless
  # `$(...)` in an arm journal line can assert STOP as soon as the arm starts.
  [[ "${BASH_SUBSHELL:-0}" -eq 0 ]] || return 0
  [[ -z "$SHUTDOWN_STARTED" ]] || { reap_arm; return 0; }
  SHUTDOWN_STARTED=1
  if [[ -n "$ARM_PID" ]]; then
    : > "${STATE_DIR}/STOP"
    jnl shutdown info "${why}: STOP asserted; drain budget ${SHUTDOWN_GRACE_SECS}s"
    for sid_file in "${STATE_DIR}/call.sid" "${STATE_DIR}/page.sid"; do
      sid=$(head -1 "$sid_file" 2>/dev/null || true)
      [[ -n "$sid" ]] || continue
      local shutdown_hangup="${HANGUP_SCRIPT:-scripts/twilio-hangup.sh}"
      "$shutdown_hangup" "$sid" >>"${STATE_DIR}/launch.log" 2>&1 &
      local hangup_pid=$!
      for _ in $(seq 1 100); do
        kill -0 "$hangup_pid" 2>/dev/null || break
        sleep 0.1
      done
      if kill -0 "$hangup_pid" 2>/dev/null; then
        kill -TERM "$hangup_pid" 2>/dev/null || true
        jnl hangup warn "shutdown ${why}: timed out completing ${sid} (continuing drain)"
      elif wait "$hangup_pid"; then
        jnl hangup ok "shutdown ${why}: ${sid}"
      else
        jnl hangup warn "shutdown ${why}: unable to complete ${sid} (continuing drain)"
      fi
    done
  fi
  for child_pid in "$REDIAL_PID" "$WATCHDOG_PID" "$NGROK_PID"; do
    [[ -n "$child_pid" ]] && kill "$child_pid" 2>/dev/null || true
  done
  stop_admission_watcher
  if [[ -n "$ARM_PID" ]] && kill -0 "$ARM_PID" 2>/dev/null; then
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
  [[ "${LIFECYCLE_FINALIZED:-0}" == 0 ]] || return "${LIFECYCLE_FINALIZE_RC:-0}"
  LIFECYCLE_FINALIZED=1
  local report_check="" failure_detail="" report_ok=0
  if report_check=$(report_is_complete 2>&1); then report_ok=1; fi
  if [[ "$report_ok" == 1 ]] \
      && [[ "$LIFECYCLE_SUPERVISING" == 1 ]] \
      && [[ "$FINALIZERS_UNRESOLVED" == 0 ]] \
      && [[ "$ADMISSION_WATCHER_FAILED" == 0 ]]; then
    if lifecycle_record draining "arm exited rc=${ARM_RC:-unknown}; ${report_check}" \
        && lifecycle_record completed "runtime finalizers and all required evidence acknowledged; report=${REPORT}"; then
      jnl lifecycle ok "draining -> completed; ${report_check}"
      LIFECYCLE_FINALIZE_RC=0
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
    [[ "$ADMISSION_WATCHER_FAILED" == 0 ]] || failure_detail="admission watcher integrity failure; ${failure_detail}"
  fi
  echo "LIFECYCLE FAILED: ${failure_detail}" >&2
  jnl lifecycle fail "$failure_detail"
  lifecycle_record failed "${failure_detail}; arm_rc=${ARM_RC:-unknown}; report=${REPORT}" || true
  critical_alert runtime_failed lifecycle_terminal \
    "${EVENT_ID} runtime/evidence failure" \
    "Run ${EVENT_ID}/${PLAN_RUN_ID:-unknown} reached a failed terminal path. Required audio, book, order, report, admission, or cancellation evidence did not satisfy the completion predicate. Inspect the typed lifecycle and ${REPORT}."
  LIFECYCLE_FINALIZE_RC=1
  return 1
}
accept_completed_source_marker() {
  [[ "$LIFECYCLE_LAUNCHED" == 1 && "$LIFECYCLE_SUPERVISING" == 0 ]] || return 0
  source_marker_matches_run || return 0
  accept_source_marker || true
}
handle_shutdown_signal() {
  local why="$1" exit_code=143
  [[ "$why" == "INT" ]] && exit_code=130
  trap - TERM INT EXIT
  shutdown_children "$why"
  accept_completed_source_marker
  finalize_lifecycle || true
  exit "$exit_code"
}
handle_launcher_exit() {
  [[ "${BASH_SUBSHELL:-0}" -eq 0 ]] || return 0
  trap - TERM INT EXIT
  shutdown_children EXIT
  accept_completed_source_marker
  finalize_lifecycle || true
}
trap 'handle_shutdown_signal TERM' TERM
trap 'handle_shutdown_signal INT' INT
trap 'handle_launcher_exit' EXIT

# ── Green gate (2026-08-04, marker-activated) ───────────────────────────────
# Enforces ONLY when a preflight verdict exists for this event — legacy and
# rehearsal invocations are untouched. EARSHOT_GREEN_OVERRIDE="<reason>"
# proceeds but pages: an override that pages is a decision, not a bypass.
if [[ -f "${STATE_DIR}/preflight-result.json" ]]; then
  if GREEN_OUT=$(scripts/assert-ready.sh "$CONFIG" 2>&1); then
    echo "$GREEN_OUT"
  elif [[ -n "${EARSHOT_GREEN_OVERRIDE:-}" ]]; then
    echo "GREEN GATE OVERRIDDEN: $GREEN_OUT (reason: $EARSHOT_GREEN_OVERRIDE)"
    "${EARSHOT_NOTIFY:-scripts/notify.sh}" "GREEN GATE OVERRIDDEN: $EVENT_ID" "$GREEN_OUT — override reason: $EARSHOT_GREEN_OVERRIDE" --quiet || true
    jnl note warn "green gate overridden: $EARSHOT_GREEN_OVERRIDE"
  else
    echo "REFUSED by green gate: $GREEN_OUT" >&2
    "${EARSHOT_NOTIFY:-scripts/notify.sh}" "$EVENT_ID REFUSED by green gate" "$GREEN_OUT — fix and relaunch, or EARSHOT_GREEN_OVERRIDE=\"<reason>\" (pages)." --quiet || true
    jnl launch-start refused "$GREEN_OUT"
    exit 7
  fi
fi
jnl launch-start info "config=$CONFIG port=$BIND_PORT posture=${LIVE:-dry}${ALLOW_MENTION:+ mention}${FILLABLE:+ fillable}"

# --operator-handler double-gate half 1: the config must opt in explicitly.
# (Half 2 is --operator-i-am-paged, passed only after the page leg is live.)
if [[ -n "$OPERATOR_HANDLER" ]]; then
  # Character class, not `\(true\|false\)`: BSD sed has no alternation in a
  # basic regex, so the GNU-only form matches nothing on Darwin and this gate
  # refuses every config on the laptop. Also tolerate leading indentation.
  OP_ENABLED=$(sed -n '/^\[operator_handler\]/,/^\[/s/^[[:space:]]*enabled[[:space:]]*=[[:space:]]*\([a-zA-Z]*\).*/\1/p' "$CONFIG" | head -1)
  [[ "$OP_ENABLED" == "true" ]] || { echo "--operator-handler but [operator_handler] enabled=true not set in ${CONFIG}" >&2; exit 2; }
  [[ -x scripts/operator-page.sh && -x scripts/twilio-operator-dial.sh ]] || { echo "operator scripts missing/not executable" >&2; exit 2; }
fi

# Tunnel-free path (Linux/VPS production — docs/INGRESS-BAKEOFF.md §5 "No
# tunnels in production"): --stream-url wss://host/media skips ngrok entirely;
# the WS endpoint (TLS-terminated on the box) must route to 127.0.0.1:BIND_PORT.
# The Darwin/CDMX rehearsal path below is unchanged when the flag is absent.
if [[ -n "$STREAM_URL_OVERRIDE" ]]; then
  STREAM_URL="$STREAM_URL_OVERRIDE"
  NGROK_PID=""
  echo "tunnel-free: using provided stream URL ${STREAM_URL} (no ngrok)"
else
# This event's own ngrok tunnel, on its OWN local web-API port so two events'
# ngrok agents never share ngrok's default 127.0.0.1:4040. NOTE: the free tier
# allows only ONE agent per account, so two SIMULTANEOUS tunnels need two ngrok
# accounts — pass each event's auth file via --ngrok-config <acctX.yml>.
NGROK_WEB_PORT=$((4040 + BIND_PORT - 8080))
# ngrok v3 has no --web-addr CLI flag; web_addr is a config option. Compose the
# account config (default agent config, or --ngrok-config) with a tiny per-event
# override so each event gets its own web-API port. Later --config wins on merge.
BASE_NGROK_CONFIG="$NGROK_CONFIG"
if [[ -z "$BASE_NGROK_CONFIG" ]]; then
  BASE_NGROK_CONFIG=$(ngrok config check 2>/dev/null | sed -n 's/^Valid configuration file at //p')
fi
[[ -n "$BASE_NGROK_CONFIG" && -f "$BASE_NGROK_CONFIG" ]] || {
  echo "could not locate ngrok agent config (run 'ngrok config check' / pass --ngrok-config)" >&2; exit 1
}
EVENT_NGROK_CONFIG="${STATE_DIR}/ngrok-event.yml"
printf 'version: "3"\nagent:\n  web_addr: 127.0.0.1:%s\n' "$NGROK_WEB_PORT" > "$EVENT_NGROK_CONFIG"
ngrok http "$BIND_PORT" \
  --config "$BASE_NGROK_CONFIG" --config "$EVENT_NGROK_CONFIG" \
  --log stdout --log-level info > "${STATE_DIR}/ngrok.log" 2>&1 &
NGROK_PID=$!
echo "$NGROK_PID" > "${STATE_DIR}/ngrok.pid"
# Select the tunnel that actually forwards to THIS event's bind port — NEVER
# tunnels[0]: a stray tunnel from a half-dead prior agent would route Twilio
# audio to the wrong place (audit F10). No match = keep polling; the tunnel
# takes a few seconds to establish after the agent starts.
STREAM_HOST=""
for _ in $(seq 1 30); do
  STREAM_HOST=$(curl -s "http://127.0.0.1:${NGROK_WEB_PORT}/api/tunnels" \
    | BIND_PORT="$BIND_PORT" python3 -c '
import sys, json, os
port = os.environ["BIND_PORT"]
tuns = json.load(sys.stdin).get("tunnels", [])
match = [t for t in tuns if t.get("config", {}).get("addr", "").endswith(":" + port)]
print(match[0]["public_url"].split("://")[1] if match else "")
' 2>/dev/null || true)
  [[ -n "$STREAM_HOST" ]] && break
  kill -0 "$NGROK_PID" 2>/dev/null || { echo "ngrok exited — see ${STATE_DIR}/ngrok.log" >&2; exit 1; }
  sleep 1
done
[[ -n "$STREAM_HOST" ]] || { echo "could not read ngrok tunnel host from :${NGROK_WEB_PORT} for port ${BIND_PORT} within 30 s" >&2; exit 1; }
STREAM_URL="wss://${STREAM_HOST}/media"
echo "ngrok tunnel: ${STREAM_URL} (web-api :${NGROK_WEB_PORT}, pid ${NGROK_PID})"
fi  # end ngrok path (--stream-url skips it)

# Pin the signature base so the WS source rejects any stream not signed by THIS
# account's token (the config's [twilio] section should point stream_url here).
export TWILIO_STREAM_URL="$STREAM_URL"

# Mid-call persistence (docs/layers/source.md): with twilio_stream.max_reconnects
# > 0 the source survives a drop by re-accepting; it asks for the redial by
# rewriting ${STATE_DIR}/redial.request, which scripts/redial-supervisor.sh
# (started after the dial below) watches. The env var only names the marker
# path — with max_reconnects unset/0 the source never writes it.
export EARSHOT_REDIAL_DIR="$STATE_DIR"
MAX_RECONNECTS=$(sed -n 's/^[[:space:]]*max_reconnects[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$CONFIG" | head -1)

# No config gets armed one-shot by ACCIDENT (2026-08-04, Gap B/C): the Rust
# default is max_reconnects = 0, only slate-stage-derived configs carry the
# Q-REDIAL production values, and the hand-written 26jul29 configs went to
# real events where a first drop would have ended the run silently. Applies
# under --no-dial too — rehearsals must mirror production posture.
if [[ -z "$ONE_SHOT_OK" ]] && [[ -z "${MAX_RECONNECTS:-}" || "$MAX_RECONNECTS" -eq 0 ]]; then
  echo "REFUSING: config has no mid-call persistence (twilio_stream.max_reconnects unset/0)." >&2
  echo "  A first drop ends the run. Regenerate/stage the event config so it carries" >&2
  echo "  the generic phone policy (max_reconnects = 20, idle_timeout_secs = 8), or relaunch with" >&2
  echo "  --one-shot-ok to name the one-shot posture as deliberate." >&2
  exit 2
fi

# Supervisor time budgets, derived from THIS event's call time.
#
# Both of the supervisor's own defaults are anchored on when the SUPERVISOR
# started, which is launch time — not on when the call starts. Launch runs
# 10-20 min early, so its 2700 s "call has genuinely ended" grace actually
# expires ~25-35 min into the call, and its 120-minute lifetime ends 2 h after
# launch rather than 2 h after the call. Neither number was reachable from
# here: the launcher passed --max-redials and nothing else.
#
# Anchoring on [event].starts_at fixes both. CALL_END_GRACE becomes "40 min of
# real call have happened", so a clean far-end stop before that is treated as a
# drop worth redialing and after it as the end. MAX_MINUTES runs to 150 min
# past the call start — the same redial-window close slate-stage.sh already
# stages against (END_HHMM_LIVE). With no starts_at, both stay empty and the
# supervisor keeps its historical defaults.
CALL_GRACE_MIN=40
SUPERVISE_TO_MIN=150
# Command substitution, NOT `read < <(...)`: bash 3.2 cannot parse a heredoc
# inside a process substitution ("bad substitution: no closing `)'"), and this
# script is invoked as `bash launch-event.sh` by tests and by /bin/bash on the
# laptop even though the box runs bash 5.
DERIVED_BUDGETS=$(
  CONFIG="$CONFIG" GRACE_MIN="$CALL_GRACE_MIN" TO_MIN="$SUPERVISE_TO_MIN" python3 - <<'PY' 2>/dev/null || echo ""
import datetime, os, tomllib
starts = tomllib.load(open(os.environ["CONFIG"], "rb")).get("event", {}).get("starts_at")
call = datetime.datetime.fromisoformat(str(starts).replace("Z", "+00:00"))
if call.tzinfo is None:
    call = call.replace(tzinfo=datetime.timezone.utc)
now = datetime.datetime.now(datetime.timezone.utc)
lead = (call - now).total_seconds()
grace = int(lead + int(os.environ["GRACE_MIN"]) * 60)
maxmin = int((lead + int(os.environ["TO_MIN"]) * 60) // 60)
# A call already past these points leaves nothing sane to derive; fall through
# to the supervisor's defaults rather than passing a negative budget.
print(grace if grace > 0 else "", maxmin if maxmin > 0 else "")
PY
)
DERIVED_GRACE=$(echo "$DERIVED_BUDGETS" | awk '{print $1}')
DERIVED_MAXMIN=$(echo "$DERIVED_BUDGETS" | awk '{print $2}')
[[ -n "$MAX_MINUTES_OVERRIDE" ]] && DERIVED_MAXMIN="$MAX_MINUTES_OVERRIDE"
[[ -n "$CALL_END_GRACE_OVERRIDE" ]] && DERIVED_GRACE="$CALL_END_GRACE_OVERRIDE"
# A plain string, not an array: the box runs bash 5 but this laptop is bash 3.2,
# where "${arr[@]}" on an EMPTY array trips `set -u` and aborts the launcher.
# Both values are bare integers, so word-splitting them is safe and intended.
SUPERVISOR_BUDGETS=""
[[ -n "${DERIVED_MAXMIN:-}" ]] && SUPERVISOR_BUDGETS="--max-minutes $DERIVED_MAXMIN"
[[ -n "${DERIVED_GRACE:-}" ]] && SUPERVISOR_BUDGETS="$SUPERVISOR_BUDGETS --call-end-grace $DERIVED_GRACE"
# Unconditional echo: the 7942a41 clock-derivation is now posture-testable.
echo "supervisor budgets:${SUPERVISOR_BUDGETS:- (defaults)}"

# Arm the pipeline (blocks, waiting for the dial). Dial from a second shell, or
# background this and dial below.
REPORT="${PLAN_REPORT:-${STATE_DIR}/report.json}"
# Readiness token contract: `earshot arm` writes ${STATE_DIR}/listener.ready
# ({"port":…,"pid":…,"unix_ts":…}) only AFTER the detector is built and the
# pipeline is running. A stale token from a previous run would let the wait
# below pass instantly against a dead arm — delete it before starting.
READY_FILE="${STATE_DIR}/listener.ready"
rm -f "$READY_FILE"
# Same stale-token hazard for the no-stream watchdog's signal: a
# stream.connected left by a previous run would confirm a stream that never
# landed.
STREAM_CONNECTED_FILE="${STATE_DIR}/stream.connected"
rm -f "$STREAM_CONNECTED_FILE"
# A shutdown may only hang up legs created by THIS launcher invocation. A
# stale CallSid from an earlier attempt is not authority to touch that call.
rm -f "${STATE_DIR}/call.sid" "${STATE_DIR}/page.sid"
# Pinned binary (--bin / EARSHOT_BIN, produced by scripts/preflight-event.sh)
# runs EXACTLY what was drilled and skips the fire-time compile — `cargo run`
# from the live checkout can hit a dirty tree, a cold rebuild racing the
# 120 s bind timeout, or a full disk (audit F7). cargo stays as the dev path.
# Direct exec also means the recorded arm.pid IS the earshot process, so
# SIGTERM from the supervisor reaches its kill watcher, not a cargo wrapper.
if [[ -n "$BIN" ]]; then
  [[ -x "$BIN" ]] || { echo "pinned binary not executable: ${BIN}" >&2; exit 2; }
  echo "arming (pinned ${BIN}): arm --config ${CONFIG} --state-dir ${STATE_DIR} --report ${REPORT} ${LIVE} ${ALLOW_MENTION} ${FILLABLE}"
  "$BIN" arm --config "$CONFIG" --state-dir "$STATE_DIR" --report "$REPORT" $LIVE $ALLOW_MENTION $FILLABLE &
else
  echo "arming (cargo run): arm --config ${CONFIG} --state-dir ${STATE_DIR} --report ${REPORT} ${LIVE} ${ALLOW_MENTION} ${FILLABLE}"
  cargo run --release -- arm --config "$CONFIG" --state-dir "$STATE_DIR" --report "$REPORT" $LIVE $ALLOW_MENTION $FILLABLE &
fi
ARM_PID=$!
echo "$ARM_PID" > "${STATE_DIR}/arm.pid"
jnl arm-start info "$([[ -n "$BIN" ]] && echo "pinned $BIN" || echo "cargo run (UNPINNED)") pid=$ARM_PID"
# Wait for the arm's readiness token before dialing — arm validates every
# market and waits for book snapshots first, which takes ~30 s on a 14-market
# config. Dialing early hands Twilio a dead tunnel and the stream is lost.
# NOT `nc -z`: the port is bound BEFORE the detector build, so on 2026-07-29
# the port answered all day while an ORT-less detector build hung and the
# media socket sat unaccepted in the kernel backlog (header comment above).
# `earshot arm` writes ${STATE_DIR}/listener.ready only once the pipeline is
# actually running and accept()ing — that token is the only "safe to dial".
for _ in $(seq 1 240); do
  listener_marker_matches_arm && break
  kill -0 "$ARM_PID" 2>/dev/null || { echo "arm exited before writing ${READY_FILE} — see log above" >&2; exit 1; }
  sleep 0.5
done
listener_marker_matches_arm || { echo "arm never wrote a matching ${READY_FILE} within 120 s" >&2; jnl listener-ready fail "no PID-bound token within 120s"; exit 1; }
echo "listener ready ($(tr -d '\n' < "$READY_FILE" 2>/dev/null || echo '?')) — dialing"
jnl listener-ready ok "$(tr -d '\n' < "$READY_FILE" 2>/dev/null | head -c 120 || echo '?')"
if ! lifecycle_record launched "listener.ready accepted; arm_pid=${ARM_PID} state=${STATE_DIR}"; then
  echo "REFUSING to dial: failed to append durable launched receipt for ${RUN_DIR}" >&2
  jnl lifecycle fail "could not record launched; refusing dial"
  page "LIFECYCLE FAILED: ${EVENT_ID}" "listener.ready exists, but eventctl could not record launched. The arm is being stopped before any phone call is placed. Inspect ${RUN_DIR}/control/receipts.jsonl."
  shutdown_children "launched receipt failure"
  finalize_lifecycle || true
  exit 7
fi
[[ -n "$RUN_DIR" ]] && LIFECYCLE_LAUNCHED=1

# Classified first-dial retry (2026-08-04, Gap A). Retries ONLY when no call
# can exist (exit 5 transient / 1 unclassified — the operator dial script has
# no classification, so its generic failures retry too). Exit 6 (ambiguous:
# the POST may have created a call the recovery lookup could not prove) never
# blind-retries — a second leg into a live bridge is the 21-73 s operator-drop
# class. 2/3/4 are permanent. Backoff 2 s then 5 s: the dial fires minutes
# before the call; three fast attempts leave the operator time to act.
dial_with_retry() { # $@ = full dial command; sets DIAL_RC
  local attempt=1 rc=0
  while :; do
    rc=0
    "$@" || rc=$?
    case "$rc" in
      0) DIAL_RC=0; jnl dial ok "attempt ${attempt} sid=$(head -c 40 "${STATE_DIR}/call.sid" 2>/dev/null || echo '?')"; return 0 ;;
      5|1)
        if (( attempt >= DIAL_ATTEMPTS )); then
          echo "dial attempt ${attempt}/${DIAL_ATTEMPTS} failed (exit ${rc}) — retries exhausted" >&2
          jnl dial fail "attempt ${attempt}/${DIAL_ATTEMPTS} rc=${rc} — retries exhausted"
          break
        fi
        local delay=2; (( attempt >= 2 )) && delay=5
        echo "dial attempt ${attempt}/${DIAL_ATTEMPTS} failed transiently (exit ${rc}) — retrying in ${delay}s" >&2
        sleep "$delay"
        attempt=$((attempt + 1))
        ;;
      6)
        page "DIAL AMBIGUOUS: ${EVENT_ID}" "dial transport failed and the recovery lookup could not prove call state. NOT retrying blindly (double-leg risk). Check Twilio console, then dial by hand: ${DIAL_SCRIPT} --to ${TO} --stream-url ${STREAM_URL} --sid-file ${STATE_DIR}/call.sid"
        jnl dial fail "rc=6 ambiguous — NOT retrying (double-leg risk); paged"
        break
        ;;
      *) jnl dial fail "rc=${rc} permanent"; break ;;
    esac
  done
  DIAL_RC="$rc"
  return "$rc"
}

# Redial supervisor BEFORE the first dial (2026-08-04): it is purely
# marker-driven (baselines pre-existing redial.request content at startup,
# refuses without listener.ready — which now exists), so starting it here is
# safe, gives the post-dial window a redial consumer from second zero, and
# gives the no-stream watchdog someone to hand a synthetic request to.
REDIAL_PID=""
if [[ -z "$NO_DIAL" && -n "${MAX_RECONNECTS:-}" && "$MAX_RECONNECTS" -gt 0 ]]; then
  if [[ -n "$OPERATOR_HANDLER" ]]; then
    EARSHOT_OPERATOR_CONFIG="$CONFIG" scripts/redial-supervisor.sh --state-dir "$STATE_DIR" --to "$TO" --stream-url "$STREAM_URL" \
      ${DTMF:+--dtmf "$DTMF"} ${DIGIT_DELAY:+--digit-delay "$DIGIT_DELAY"} \
      ${DTMF_REPEAT:+--dtmf-repeat "$DTMF_REPEAT"} --max-redials "$MAX_RECONNECTS" \
      $SUPERVISOR_BUDGETS \
      ${HANGUP_SCRIPT:+--hangup-script "$HANGUP_SCRIPT"} \
      --dial-script scripts/operator-redial.sh &
  else
    scripts/redial-supervisor.sh --state-dir "$STATE_DIR" --to "$TO" --stream-url "$STREAM_URL" \
      ${DTMF:+--dtmf "$DTMF"} ${DIGIT_DELAY:+--digit-delay "$DIGIT_DELAY"} \
      ${DTMF_REPEAT:+--dtmf-repeat "$DTMF_REPEAT"} --max-redials "$MAX_RECONNECTS" \
      $SUPERVISOR_BUDGETS \
      ${HANGUP_SCRIPT:+--hangup-script "$HANGUP_SCRIPT"} \
      --dial-script "$DIAL_SCRIPT" &
  fi
  REDIAL_PID=$!
  echo "$REDIAL_PID" > "${STATE_DIR}/redial-supervisor.pid"
  echo "redial supervisor up (pid ${REDIAL_PID}${OPERATOR_HANDLER:+, operator redial wrapper}, max ${MAX_RECONNECTS} redials${SUPERVISOR_BUDGETS:+,$SUPERVISOR_BUDGETS})"
  jnl supervisor-start info "pid=${REDIAL_PID} max_redials=${MAX_RECONNECTS}${OPERATOR_HANDLER:+ operator-wrapper}"
fi

# No-stream watchdog (2026-08-04, Gap Q): a placed call is not a stream —
# 2026-07-29 calls ran to completion while nothing ever accept()ed, and the
# 900 s accept window is otherwise silent. The source writes
# ${STATE_DIR}/stream.connected on every accepted start; if it does not
# appear within the window, page and (with a supervisor running) hand it a
# synthetic no_stream redial request — the supervisor hangs up the deaf
# CallSid and redials with its own budget/backoff/media-confirm machinery.
start_stream_watchdog() {
  (
    DEADLINE=$(( $(date +%s) + STREAM_CONFIRM_SECS ))
    while [[ ! -f "$STREAM_CONNECTED_FILE" ]] && (( $(date +%s) < DEADLINE )); do sleep 1; done
    if [[ -f "$STREAM_CONNECTED_FILE" ]]; then
      echo "stream-watchdog: stream confirmed ($(tr -d '\n' < "$STREAM_CONNECTED_FILE" 2>/dev/null || echo '?'))"
      jnl stream ok "confirmed within ${STREAM_CONFIRM_SECS}s"
      exit 0
    fi
    DEAF_SID=$(head -1 "${STATE_DIR}/call.sid" 2>/dev/null || echo "")
    page "NO STREAM: ${EVENT_ID}" "call ${DEAF_SID:-<no sid>} placed but no stream landed within ${STREAM_CONFIRM_SECS}s. Manual redial: ${DIAL_SCRIPT} --to ${TO} --stream-url ${STREAM_URL} --sid-file ${STATE_DIR}/call.sid"
    jnl stream fail "no_stream within ${STREAM_CONFIRM_SECS}s (deaf sid ${DEAF_SID:-none}); paged"
    if [[ -n "${REDIAL_PID:-}" ]]; then
      # Synthetic marker — reason vocabulary gains no_stream (producer: this
      # watchdog, not the source). Consumes one redial-budget unit: a deaf
      # first call IS a real recovery attempt.
      TMP="${STATE_DIR}/.no_stream.$$"
      printf '{"unix_ts":%s,"old_call_sid":"%s","attempt":0,"reason":"no_stream"}\n' "$(date +%s)" "$DEAF_SID" > "$TMP"
      mv "$TMP" "${STATE_DIR}/redial.request"
      echo "stream-watchdog: wrote synthetic no_stream redial request (deaf sid: ${DEAF_SID:-none})"
      DEADLINE=$(( $(date +%s) + STREAM_CONFIRM_SECS ))
      while [[ ! -f "$STREAM_CONNECTED_FILE" ]] && (( $(date +%s) < DEADLINE )); do sleep 1; done
      if [[ -f "$STREAM_CONNECTED_FILE" ]]; then
        echo "stream-watchdog: stream confirmed after no_stream redial"
        jnl stream ok "confirmed after no_stream redial"
        exit 0
      fi
    fi
    page "NO STREAM FINAL: ${EVENT_ID}" "still no stream after the recovery window. The arm's accept window is open (900s from arm) — a manual dial can still save the event."
    jnl stream fail "FINAL — no stream after recovery window; arm accept window still open"
  ) &
  WATCHDOG_PID=$!
  echo "$WATCHDOG_PID" > "${STATE_DIR}/stream-watchdog.pid"
}

# Dial this event's call, forking its inbound audio to THIS event's stream URL.
# The CallSid lands in the state dir so a supervisor can hang up mid-call (STOP).
# On final dial failure the launcher pages and KEEPS THE ARM ALIVE: the
# listener.ready token is valid, the accept window is 900 s, and a manual dial
# can still save the event — tearing down would forfeit it.
WATCHDOG_PID=""
if [[ -n "$NO_DIAL" ]]; then
  echo "--no-dial: skipping twilio-dial (rehearsal without a real call)"
elif [[ -n "$OPERATOR_HANDLER" ]]; then
  # Operator flow: human FIRST, bridge second — never present an unmanned
  # silent leg to a live operator (that is exactly the 21-73 s drop class).
  echo "operator handler: paging the configured cell into the conference"
  scripts/operator-page.sh --config "$CONFIG" --operator-i-am-paged \
    --sid-file "${STATE_DIR}/page.sid" --wait-joined 60 || {
    echo "page leg not answered/joined within 60 s — REFUSING to dial the bridge unmanned" >&2
    exit 1
  }
  if dial_with_retry scripts/twilio-operator-dial.sh --to "$TO" --config "$CONFIG" --operator-i-am-paged \
    ${DTMF:+--dtmf "$DTMF"} --stream-url "$STREAM_URL" \
    ${DIGIT_DELAY:+--digit-delay "$DIGIT_DELAY"} ${DTMF_REPEAT:+--dtmf-repeat "$DTMF_REPEAT"} \
    --sid-file "${STATE_DIR}/call.sid"; then
    start_stream_watchdog
    echo "operator decision gate remains CLOSED until the human explicitly confirms admission."
    echo "AFTER hearing the live issuer call, assert this exact current stream:"
    echo "  scripts/operator-page.sh --config '$CONFIG' --operator-i-am-paged --assert-admitted --state-dir '$STATE_DIR'"
    page "OPERATOR PASS REQUIRED: ${EVENT_ID}" "The pager and bridge legs are connected, but no call state proves that the screener admitted us. AFTER you hear the live issuer call, run:

scripts/operator-page.sh --config '$CONFIG' --operator-i-am-paged --assert-admitted --state-dir '$STATE_DIR'

That explicit action binds admission to the current runtime nonce + CallSid + stream attempt. A redial invalidates the receipt and requires a fresh assertion."
  else
    page "FIRST DIAL FAILED: ${EVENT_ID}" "operator bridge dial failed (exit ${DIAL_RC}) after retries. Arm is warm and accepting for 900s. Dial by hand: scripts/twilio-operator-dial.sh --to ${TO} --config ${CONFIG} --operator-i-am-paged --stream-url ${STREAM_URL} --sid-file ${STATE_DIR}/call.sid"
    echo "first dial failed (exit ${DIAL_RC}) — arm stays warm; see the page for the manual dial command" >&2
  fi
else
  if dial_with_retry "$DIAL_SCRIPT" --to "$TO" ${DTMF:+--dtmf "$DTMF"} --stream-url "$STREAM_URL" \
    ${DIGIT_DELAY:+--digit-delay "$DIGIT_DELAY"} ${DTMF_REPEAT:+--dtmf-repeat "$DTMF_REPEAT"} \
    --sid-file "${STATE_DIR}/call.sid"; then
    start_stream_watchdog
  else
    page "FIRST DIAL FAILED: ${EVENT_ID}" "dial failed (exit ${DIAL_RC}) after retries. Arm is warm and accepting for 900s. Dial by hand: ${DIAL_SCRIPT} --to ${TO} --stream-url ${STREAM_URL} --sid-file ${STATE_DIR}/call.sid"
    echo "first dial failed (exit ${DIAL_RC}) — arm stays warm; see the page for the manual dial command" >&2
  fi
fi

echo "event ${EVENT_ID} live. kill with: touch ${STATE_DIR}/STOP"
jnl live info "kill with: touch ${STATE_DIR}/STOP"

# `stream.connected` remains the phone-session health/redial signal. Typed
# runtime attachment is stronger: the runtime publishes `source.attached`
# only after the in-process evidence bank durably accepts detector-path media,
# and the marker is bound to this RunPlan identity and arm PID.
SOURCE_LIFECYCLE_FAILED=0
if [[ -n "$RUN_DIR" ]]; then
  while kill -0 "$ARM_PID" 2>/dev/null; do
    ATTACH_RC=0
    accept_source_marker || ATTACH_RC=$?
    if [[ "$ATTACH_RC" -eq 0 ]]; then
      if ! start_admission_watcher; then
        SOURCE_LIFECYCLE_FAILED=1
        ADMISSION_WATCHER_FAILED=1
        record_admission_watcher_fault 8 "<watcher-start>" \
          "could not persist/start the admission watcher after durable source attachment"
        shutdown_children "admission watcher start failure"
      fi
      break
    elif [[ "$ATTACH_RC" -eq 2 ]]; then
      SOURCE_LIFECYCLE_FAILED=1
      echo "LIFECYCLE FAILURE: source attached but source_attached/supervising receipt failed" >&2
      page "LIFECYCLE FAILED: ${EVENT_ID}" "Detector-path audio attached, but eventctl could not durably record source_attached/supervising. STOP has been asserted. Inspect ${RUN_DIR}/control/receipts.jsonl."
      shutdown_children "source lifecycle receipt failure"
      break
    fi
    sleep 0.1
  done
  if [[ "$LIFECYCLE_SUPERVISING" == 0 && "$SOURCE_LIFECYCLE_FAILED" == 0 ]] \
      && source_marker_matches_run; then
    ATTACH_RC=0
    accept_source_marker || ATTACH_RC=$?
    [[ "$ATTACH_RC" -ne 2 ]] || SOURCE_LIFECYCLE_FAILED=1
  fi
fi

reap_arm
jnl arm-exit "$([[ "${ARM_RC:-1}" -eq 0 ]] && echo ok || echo fail)" "rc=${ARM_RC:-unknown} report=${REPORT}"
for child_pid in "${REDIAL_PID:-}" "${WATCHDOG_PID:-}" "${NGROK_PID:-}"; do
  [[ -n "$child_pid" ]] && kill "$child_pid" 2>/dev/null || true
done
stop_admission_watcher
SHUTDOWN_STARTED=1
LIFECYCLE_RC=0
finalize_lifecycle || LIFECYCLE_RC=$?
if [[ "$LIFECYCLE_RC" -ne 0 && "${ARM_RC:-0}" -eq 0 ]]; then
  exit 7
fi
exit "${ARM_RC:-1}"
