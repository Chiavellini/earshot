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
BIN="${EARSHOT_BIN:-}" NO_DIAL="" STREAM_URL_OVERRIDE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)        CONFIG="$2"; shift 2 ;;
    --account)       ACCOUNT="$2"; shift 2 ;;
    --bind-port)     BIND_PORT="$2"; shift 2 ;;
    --to)            TO="$2"; shift 2 ;;
    --dtmf)          DTMF="$2"; shift 2 ;;
    --ngrok-config)  NGROK_CONFIG="$2"; shift 2 ;;
    --stream-url)    STREAM_URL_OVERRIDE="$2"; shift 2 ;;  # tunnel-free: skip ngrok, endpoint terminates on the box
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

# Arm the pipeline (blocks, waiting for the dial). Dial from a second shell, or
# background this and dial below.
REPORT="${STATE_DIR}/report.json"
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
# Wait for the WS listener to actually bind before dialing — arm validates every
# market and waits for book snapshots first, which takes ~30 s on a 14-market
# config. Dialing early hands Twilio a dead tunnel and the stream is lost.
for _ in $(seq 1 120); do
  nc -z 127.0.0.1 "$BIND_PORT" 2>/dev/null && break
  kill -0 "$ARM_PID" 2>/dev/null || { echo "arm exited before binding :${BIND_PORT} — see log above" >&2; exit 1; }
  sleep 1
done
nc -z 127.0.0.1 "$BIND_PORT" 2>/dev/null || { echo "arm never bound :${BIND_PORT} within 120 s" >&2; exit 1; }
echo "WS listener up on :${BIND_PORT} — dialing"

# Dial this event's call, forking its inbound audio to THIS event's stream URL.
# The CallSid lands in the state dir so a supervisor can hang up mid-call (STOP).
if [[ -n "$NO_DIAL" ]]; then
  echo "--no-dial: skipping twilio-dial (rehearsal without a real call)"
else
  scripts/twilio-dial.sh --to "$TO" ${DTMF:+--dtmf "$DTMF"} --stream-url "$STREAM_URL" \
    --sid-file "${STATE_DIR}/call.sid"
fi

echo "event ${EVENT_ID} live. kill with: touch ${STATE_DIR}/STOP"
wait "$ARM_PID"
