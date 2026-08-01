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

CONFIG="" ACCOUNT="" BIND_PORT="" TO="" DTMF="" LIVE="" ALLOW_MENTION="" FILLABLE="" NGROK_CONFIG=""
DIGIT_DELAY="" DTMF_REPEAT=""   # empty => twilio-dial.sh defaults (4 s, play once)
BIN="${EARSHOT_BIN:-}" NO_DIAL="" STREAM_URL_OVERRIDE="" TLS_HOST="" OPERATOR_HANDLER=""
MAX_MINUTES_OVERRIDE="" CALL_END_GRACE_OVERRIDE=""
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
    --no-dial)       NO_DIAL=1; shift ;;
    --i-am-live)     LIVE="--i-am-live"; shift ;;
    --allow-mention) ALLOW_MENTION="--allow-mention"; shift ;;
    --fillable)      FILLABLE="--fillable"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$CONFIG" && -n "$ACCOUNT" && -n "$BIND_PORT" && -n "$TO" ]] || {
  echo "usage: $0 --config <cfg> --account <env-file> --bind-port <port> --to +1... [--dtmf 1234#] [--ngrok-config <yml> | --stream-url wss://host/media] [--i-am-live] [--allow-mention] [--fillable]" >&2
  exit 2
}
[[ -f "$CONFIG" ]]  || { echo "config not found: $CONFIG" >&2; exit 2; }
[[ -f "$ACCOUNT" ]] || { echo "account env file not found: $ACCOUNT" >&2; exit 2; }
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
    _ort=$(find "${EARSHOT_ROOT:-$HOME/ear}/vendor" -name 'libonnxruntime*.so*' 2>/dev/null | head -1 || true)
    if [[ -n "$_ort" ]]; then
      export ORT_DYLIB_PATH="$_ort"
    fi
  fi
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
STATE_DIR="state/${EVENT_ID}"
mkdir -p "$STATE_DIR"
echo "event=${EVENT_ID}  account=${ACCOUNT}  port=${BIND_PORT}  state=${STATE_DIR}"

# --operator-handler double-gate half 1: the config must opt in explicitly.
# (Half 2 is --operator-i-am-paged, passed only after the page leg is live.)
if [[ -n "$OPERATOR_HANDLER" ]]; then
  OP_ENABLED=$(sed -n '/^\[operator_handler\]/,/^\[/s/^enabled[[:space:]]*=[[:space:]]*\(true\|false\).*/\1/p' "$CONFIG" | head -1)
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
trap 'kill "$NGROK_PID" 2>/dev/null || true' EXIT
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

# Arm the pipeline (blocks, waiting for the dial). Dial from a second shell, or
# background this and dial below.
REPORT="${STATE_DIR}/report.json"
# Readiness token contract: `earshot arm` writes ${STATE_DIR}/listener.ready
# ({"port":…,"pid":…,"unix_ts":…}) only AFTER the detector is built and the
# pipeline is running. A stale token from a previous run would let the wait
# below pass instantly against a dead arm — delete it before starting.
READY_FILE="${STATE_DIR}/listener.ready"
rm -f "$READY_FILE"
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
trap 'kill "$ARM_PID" "$NGROK_PID" 2>/dev/null || true' EXIT
# Wait for the arm's readiness token before dialing — arm validates every
# market and waits for book snapshots first, which takes ~30 s on a 14-market
# config. Dialing early hands Twilio a dead tunnel and the stream is lost.
# NOT `nc -z`: the port is bound BEFORE the detector build, so on 2026-07-29
# the port answered all day while an ORT-less detector build hung and the
# media socket sat unaccepted in the kernel backlog (header comment above).
# `earshot arm` writes ${STATE_DIR}/listener.ready only once the pipeline is
# actually running and accept()ing — that token is the only "safe to dial".
for _ in $(seq 1 240); do
  [[ -f "$READY_FILE" ]] && break
  kill -0 "$ARM_PID" 2>/dev/null || { echo "arm exited before writing ${READY_FILE} — see log above" >&2; exit 1; }
  sleep 0.5
done
[[ -f "$READY_FILE" ]] || { echo "arm never wrote ${READY_FILE} within 120 s" >&2; exit 1; }
echo "listener ready ($(tr -d '\n' < "$READY_FILE" 2>/dev/null || echo '?')) — dialing"

# Dial this event's call, forking its inbound audio to THIS event's stream URL.
# The CallSid lands in the state dir so a supervisor can hang up mid-call (STOP).
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
  scripts/twilio-operator-dial.sh --to "$TO" --config "$CONFIG" --operator-i-am-paged \
    ${DTMF:+--dtmf "$DTMF"} --stream-url "$STREAM_URL" \
    ${DIGIT_DELAY:+--digit-delay "$DIGIT_DELAY"} ${DTMF_REPEAT:+--dtmf-repeat "$DTMF_REPEAT"} \
    --sid-file "${STATE_DIR}/call.sid"

  if [[ -n "${MAX_RECONNECTS:-}" && "$MAX_RECONNECTS" -gt 0 ]]; then
    EARSHOT_OPERATOR_CONFIG="$CONFIG" scripts/redial-supervisor.sh --state-dir "$STATE_DIR" --to "$TO" --stream-url "$STREAM_URL" \
      ${DTMF:+--dtmf "$DTMF"} ${DIGIT_DELAY:+--digit-delay "$DIGIT_DELAY"} \
      ${DTMF_REPEAT:+--dtmf-repeat "$DTMF_REPEAT"} --max-redials "$MAX_RECONNECTS" \
      $SUPERVISOR_BUDGETS \
      --dial-script scripts/operator-redial.sh &
    REDIAL_PID=$!
    echo "$REDIAL_PID" > "${STATE_DIR}/redial-supervisor.pid"
    trap 'kill "$ARM_PID" "$NGROK_PID" "$REDIAL_PID" 2>/dev/null || true' EXIT
    echo "redial supervisor up (pid ${REDIAL_PID}, operator redial wrapper, max ${MAX_RECONNECTS} redials${SUPERVISOR_BUDGETS:+,$SUPERVISOR_BUDGETS})"
  fi
else
  scripts/twilio-dial.sh --to "$TO" ${DTMF:+--dtmf "$DTMF"} --stream-url "$STREAM_URL" \
    ${DIGIT_DELAY:+--digit-delay "$DIGIT_DELAY"} ${DTMF_REPEAT:+--dtmf-repeat "$DTMF_REPEAT"} \
    --sid-file "${STATE_DIR}/call.sid"

  # Redial supervisor: only when the config opts into mid-call persistence.
  if [[ -n "${MAX_RECONNECTS:-}" && "$MAX_RECONNECTS" -gt 0 ]]; then
    scripts/redial-supervisor.sh --state-dir "$STATE_DIR" --to "$TO" --stream-url "$STREAM_URL" \
      ${DTMF:+--dtmf "$DTMF"} ${DIGIT_DELAY:+--digit-delay "$DIGIT_DELAY"} \
      ${DTMF_REPEAT:+--dtmf-repeat "$DTMF_REPEAT"} --max-redials "$MAX_RECONNECTS" \
      $SUPERVISOR_BUDGETS &
    REDIAL_PID=$!
    echo "$REDIAL_PID" > "${STATE_DIR}/redial-supervisor.pid"
    trap 'kill "$ARM_PID" "$NGROK_PID" "$REDIAL_PID" 2>/dev/null || true' EXIT
    echo "redial supervisor up (pid ${REDIAL_PID}, max ${MAX_RECONNECTS} redials${SUPERVISOR_BUDGETS:+,$SUPERVISOR_BUDGETS})"
  fi
fi

echo "event ${EVENT_ID} live. kill with: touch ${STATE_DIR}/STOP"
wait "$ARM_PID"
