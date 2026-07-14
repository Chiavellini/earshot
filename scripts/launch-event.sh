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
#     [--i-am-live] [--allow-mention]
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

CONFIG="" ACCOUNT="" BIND_PORT="" TO="" DTMF="" LIVE="" ALLOW_MENTION="" NGROK_CONFIG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)        CONFIG="$2"; shift 2 ;;
    --account)       ACCOUNT="$2"; shift 2 ;;
    --bind-port)     BIND_PORT="$2"; shift 2 ;;
    --to)            TO="$2"; shift 2 ;;
    --dtmf)          DTMF="$2"; shift 2 ;;
    --ngrok-config)  NGROK_CONFIG="$2"; shift 2 ;;
    --i-am-live)     LIVE="--i-am-live"; shift ;;
    --allow-mention) ALLOW_MENTION="--allow-mention"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$CONFIG" && -n "$ACCOUNT" && -n "$BIND_PORT" && -n "$TO" ]] || {
  echo "usage: $0 --config <cfg> --account <env-file> --bind-port <port> --to +1... [--dtmf 1234#] [--ngrok-config <yml>] [--i-am-live] [--allow-mention]" >&2
  exit 2
}
[[ -f "$CONFIG" ]]  || { echo "config not found: $CONFIG" >&2; exit 2; }
[[ -f "$ACCOUNT" ]] || { echo "account env file not found: $ACCOUNT" >&2; exit 2; }
# The per-event web-API port is derived from the bind port; keep it >= 4040 so
# two events (8080/8081/...) map to 4040/4041/... without clashing ngrok's default.
[[ "$BIND_PORT" =~ ^[0-9]+$ && "$BIND_PORT" -ge 8080 ]] || {
  echo "--bind-port must be an integer >= 8080 (got ${BIND_PORT})" >&2; exit 2
}

# This event's own secrets, into THIS process only.
# shellcheck disable=SC1090
source "$ACCOUNT"
: "${TWILIO_ACCOUNT_SID:?}" "${TWILIO_AUTH_TOKEN:?}" "${TWILIO_FROM_NUMBER:?}"

# Per-event state dir, keyed off the config's [event] id so ledgers/STOP never
# collide between concurrent events.
EVENT_ID=$(python3 -c 'import sys,tomllib;print(tomllib.load(open(sys.argv[1],"rb"))["event"]["id"])' "$CONFIG")
STATE_DIR="state/${EVENT_ID}"
mkdir -p "$STATE_DIR"
echo "event=${EVENT_ID}  account=${ACCOUNT}  port=${BIND_PORT}  state=${STATE_DIR}"

# This event's own ngrok tunnel, on its OWN local web-API port so two events'
# ngrok agents never share ngrok's default 127.0.0.1:4040. NOTE: the free tier
# allows only ONE agent per account, so two SIMULTANEOUS tunnels need two ngrok
# accounts — pass each event's auth file via --ngrok-config <acctX.yml>.
NGROK_WEB_PORT=$((4040 + BIND_PORT - 8080))
NGROK_CONFIG_ARG=()
[[ -n "$NGROK_CONFIG" ]] && NGROK_CONFIG_ARG=(--config "$NGROK_CONFIG")
ngrok http "$BIND_PORT" \
  --web-addr "127.0.0.1:${NGROK_WEB_PORT}" \
  "${NGROK_CONFIG_ARG[@]}" \
  --log stdout --log-level info > "${STATE_DIR}/ngrok.log" 2>&1 &
NGROK_PID=$!
trap 'kill "$NGROK_PID" 2>/dev/null || true' EXIT
sleep 3
# Select the tunnel that actually forwards to THIS event's bind port (never
# blindly tunnels[0]), so a stray/other tunnel on the same agent can't mislead us.
STREAM_HOST=$(curl -s "http://127.0.0.1:${NGROK_WEB_PORT}/api/tunnels" \
  | BIND_PORT="$BIND_PORT" python3 -c '
import sys, json, os
port = os.environ["BIND_PORT"]
tuns = json.load(sys.stdin).get("tunnels", [])
match = [t for t in tuns if t.get("config", {}).get("addr", "").endswith(":" + port)]
pick = (match or tuns)
print(pick[0]["public_url"].split("://")[1] if pick else "")
' 2>/dev/null || true)
[[ -n "$STREAM_HOST" ]] || { echo "could not read ngrok tunnel host from :${NGROK_WEB_PORT} for port ${BIND_PORT}" >&2; exit 1; }
STREAM_URL="wss://${STREAM_HOST}/media"
echo "ngrok tunnel: ${STREAM_URL} (web-api :${NGROK_WEB_PORT}, pid ${NGROK_PID})"

# Pin the signature base so the WS source rejects any stream not signed by THIS
# account's token (the config's [twilio] section should point stream_url here).
export TWILIO_STREAM_URL="$STREAM_URL"

# Arm the pipeline (blocks, waiting for the dial). Dial from a second shell, or
# background this and dial below.
REPORT="${STATE_DIR}/report.json"
echo "arming: earshot arm --config ${CONFIG} --state-dir ${STATE_DIR} --report ${REPORT} ${LIVE} ${ALLOW_MENTION}"
cargo run --release -- arm --config "$CONFIG" --state-dir "$STATE_DIR" --report "$REPORT" $LIVE $ALLOW_MENTION &
ARM_PID=$!
trap 'kill "$ARM_PID" "$NGROK_PID" 2>/dev/null || true' EXIT
sleep 8  # let the WS listener bind before Twilio connects

# Dial this event's call, forking its inbound audio to THIS event's stream URL.
scripts/twilio-dial.sh --to "$TO" ${DTMF:+--dtmf "$DTMF"} --stream-url "$STREAM_URL"

echo "event ${EVENT_ID} live. kill with: touch ${STATE_DIR}/STOP"
wait "$ARM_PID"
