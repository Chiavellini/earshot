#!/usr/bin/env bash
# Bridge-entry admission probe (VERIFICATIONS Q-ADMIT; dial-ins.toml `admission`).
#
# Dials a conference bridge, records up to --secs of the bridge's OWN audio on a
# listen-only leg (earshot bridge-probe: one Twilio WS leg, no detector, no
# config), transcribes it with faster-whisper, and prints the observed hold
# time, the transcript, and pre-formatted VERIFICATIONS + dial-ins.toml
# snippets. This is how `admission` gets set — never from an IR page (the MSFT
# RULE in configs/dial-ins.toml).
#
#   bridge-probe.sh --to +18774070666 --label msft-2026-10-27 \
#                   [--dtmf 1234#] [--secs 120] [--bind-port 8099] \
#                   [--stream-url wss://host/media] [--account <env-file>] \
#                   [--ngrok-config <acct.yml>] [--yes]
#
# Safety:
#   - REFUSES while any armed event is live (unless --yes): a probe must never
#     dial a bridge we are also trading (docs/RUNBOOK.md).
#   - Listen-only: the call goes through scripts/twilio-dial.sh — same TwiML,
#     same DTMF charset guard, shortened <Pause>.
#   - Cost per probe ≈ $0.02 and ~1 min of an operator's time on screened
#     bridges; don't loop it against one venue.
set -euo pipefail
cd "$(dirname "$0")/.."

TO="" DTMF="" LABEL="" SECS=120 BIND_PORT=8099 STREAM_URL_OVERRIDE=""
ACCOUNT="" NGROK_CONFIG="" YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --to)           TO="$2"; shift 2 ;;
    --dtmf)         DTMF="$2"; shift 2 ;;
    --label)        LABEL="$2"; shift 2 ;;
    --secs)         SECS="$2"; shift 2 ;;
    --bind-port)    BIND_PORT="$2"; shift 2 ;;
    --stream-url)   STREAM_URL_OVERRIDE="$2"; shift 2 ;;
    --account)      ACCOUNT="$2"; shift 2 ;;
    --ngrok-config) NGROK_CONFIG="$2"; shift 2 ;;
    --yes)          YES=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$TO" && -n "$LABEL" ]] || {
  echo "usage: $0 --to +1... --label <venue-YYYY-MM-DD> [--dtmf 1234#] [--secs 120] [--bind-port 8099] [--stream-url wss://…] [--account <env>] [--yes]" >&2
  exit 2
}
[[ "$SECS" =~ ^[1-9][0-9]*$ && "$SECS" -le 600 ]] || { echo "REFUSING: --secs must be 1..600 — a probe listens to the entry flow, not the call" >&2; exit 2; }

# Guard: never probe while an event is armed (same process fingerprints as
# run-arm.sh guard 8).
LIVE=$( { pgrep -f -- "--state-dir state/" || true; pgrep -f "launch-event.sh" || true; } | sort -u | grep -v "^$$\$" || true)
if [[ -n "$LIVE" && "$YES" != "1" ]]; then
  echo "REFUSING: armed event process(es) live (pids: $(echo "$LIVE" | tr '\n' ' '))." >&2
  echo "  A probe must never dial a bridge during a live traded event. Re-run with --yes only if you are sure these are unrelated." >&2
  exit 4
fi

# Credentials: explicit --account wins; else caller env; else the global file.
if [[ -n "$ACCOUNT" ]]; then
  # shellcheck disable=SC1090
  source "$ACCOUNT"
fi
if [[ -z "${TWILIO_ACCOUNT_SID:-}" || -z "${TWILIO_AUTH_TOKEN:-}" || -z "${TWILIO_FROM_NUMBER:-}" ]]; then
  # shellcheck disable=SC1090
  source ~/.twilio/earshot_env
fi
: "${TWILIO_ACCOUNT_SID:?}" "${TWILIO_AUTH_TOKEN:?}" "${TWILIO_FROM_NUMBER:?}"

OUT_DIR="corpus/bridge-probes"
SAFE_LABEL=$(echo "$LABEL" | tr -c 'a-zA-Z0-9' '_' | sed 's/_*$//')
SID_FILE=$(mktemp)
PROBE_PID="" NGROK_PID=""
cleanup() {
  [[ -s "$SID_FILE" ]] && scripts/twilio-hangup.sh "$(cat "$SID_FILE")" >/dev/null 2>&1 || true
  [[ -n "$PROBE_PID" ]] && kill "$PROBE_PID" 2>/dev/null || true
  [[ -n "$NGROK_PID" ]] && kill "$NGROK_PID" 2>/dev/null || true
  rm -f "$SID_FILE"
}
trap cleanup EXIT

# Tunnel: an explicit --stream-url (TLS-on-box, or a tunnel you already run)
# skips ngrok; otherwise start one agent for the probe's port. Same
# select-by-addr rule as launch-event.sh (never tunnels[0], audit F10).
if [[ -n "$STREAM_URL_OVERRIDE" ]]; then
  STREAM_URL="$STREAM_URL_OVERRIDE"
  echo "tunnel-free: ${STREAM_URL}"
else
  NGROK_WEB_PORT=$((4040 + BIND_PORT - 8080))
  NGROK_ARGS=(http "$BIND_PORT" --log stdout --log-level info)
  [[ -n "$NGROK_CONFIG" ]] && NGROK_ARGS=(--config "$NGROK_CONFIG" "${NGROK_ARGS[@]}")
  NGROK_LOG=$(mktemp)
  ngrok "${NGROK_ARGS[@]}" > "$NGROK_LOG" 2>&1 &
  NGROK_PID=$!
  STREAM_HOST=""
  for _ in $(seq 1 30); do
    STREAM_HOST=$(curl -s "http://127.0.0.1:${NGROK_WEB_PORT}/api/tunnels" 2>/dev/null \
      | BIND_PORT="$BIND_PORT" python3 -c '
import sys, json, os
port = os.environ["BIND_PORT"]
try: tuns = json.load(sys.stdin).get("tunnels", [])
except Exception: tuns = []
match = [t for t in tuns if t.get("config", {}).get("addr", "").endswith(":" + port)]
print(match[0]["public_url"].split("://")[1] if match else "")
' || true)
    [[ -n "$STREAM_HOST" ]] && break
    kill -0 "$NGROK_PID" 2>/dev/null || { echo "ngrok exited — log: $NGROK_LOG" >&2; exit 1; }
    sleep 1
  done
  # ngrok's default web_addr is 4040; if the port math missed it, fall back once.
  if [[ -z "$STREAM_HOST" && "$NGROK_WEB_PORT" != 4040 ]]; then
    STREAM_HOST=$(curl -s "http://127.0.0.1:4040/api/tunnels" 2>/dev/null \
      | BIND_PORT="$BIND_PORT" python3 -c '
import sys, json, os
port = os.environ["BIND_PORT"]
try: tuns = json.load(sys.stdin).get("tunnels", [])
except Exception: tuns = []
match = [t for t in tuns if t.get("config", {}).get("addr", "").endswith(":" + port)]
print(match[0]["public_url"].split("://")[1] if match else "")
' || true)
  fi
  [[ -n "$STREAM_HOST" ]] || { echo "could not read ngrok tunnel for :${BIND_PORT} — log: $NGROK_LOG" >&2; exit 1; }
  STREAM_URL="wss://${STREAM_HOST}/media"
  echo "ngrok tunnel: ${STREAM_URL}"
fi

# Pin the signature base BEFORE the probe binds (bind() reads env).
export TWILIO_STREAM_URL="$STREAM_URL"
export TWILIO_AUTH_TOKEN

# Start the capture leg, THEN dial — same ordering rule as arm.
BIN="${EARSHOT_BIN:-}"
if [[ -n "$BIN" ]]; then
  "$BIN" bridge-probe --bind "127.0.0.1:${BIND_PORT}" --secs "$SECS" --label "$LABEL" --out-dir "$OUT_DIR" &
else
  cargo run --release --quiet -- bridge-probe --bind "127.0.0.1:${BIND_PORT}" --secs "$SECS" --label "$LABEL" --out-dir "$OUT_DIR" &
fi
PROBE_PID=$!
for _ in $(seq 1 120); do
  nc -z 127.0.0.1 "$BIND_PORT" 2>/dev/null && break
  kill -0 "$PROBE_PID" 2>/dev/null || { echo "probe process died before binding" >&2; exit 1; }
  sleep 0.5
done
nc -z 127.0.0.1 "$BIND_PORT" || { echo "probe never bound :${BIND_PORT}" >&2; exit 1; }

DIAL_ARGS=(--to "$TO" --stream-url "$STREAM_URL" --sid-file "$SID_FILE" --pause-secs $((SECS + 30)))
[[ -n "$DTMF" ]] && DIAL_ARGS+=(--dtmf "$DTMF")
scripts/twilio-dial.sh "${DIAL_ARGS[@]}"

# The probe exits on far-end hangup (stream stop) or at --secs.
set +e
wait "$PROBE_PID"
PROBE_RC=$?
set -e
PROBE_PID=""
[[ -s "$SID_FILE" ]] && scripts/twilio-hangup.sh "$(cat "$SID_FILE")" >/dev/null 2>&1 || true
[[ "$PROBE_RC" == 0 ]] || { echo "bridge-probe FAILED (rc=$PROBE_RC) — no artifact; nothing to pin" >&2; exit "$PROBE_RC"; }

WAV="${OUT_DIR}/${SAFE_LABEL}.wav"
JSON="${OUT_DIR}/${SAFE_LABEL}.json"
HELD_S=$(python3 -c "import json;print(round(json.load(open('$JSON'))['held_ms']/1000,1))")

# Transcribe next to the artifact (faster-whisper via the corpus venv).
TXT="${OUT_DIR}/${SAFE_LABEL}.txt"
if [[ -x .venv-corpus/bin/python ]]; then
  .venv-corpus/bin/python scripts/transcript-fetch.py --audio "$WAV" --id "bridge-${SAFE_LABEL}" >/dev/null
  {
    echo "# ${SAFE_LABEL}.wav — faster-whisper small.en, $(date -u +%Y-%m-%d)"
    echo "# probe: ${TO}${DTMF:+ dtmf=$DTMF}, held ${HELD_S}s"
    cat "corpus/transcripts/bridge-${SAFE_LABEL}.txt"
  } > "$TXT"
else
  echo "⚠ .venv-corpus missing — transcribe by hand: .venv-corpus/bin/python scripts/transcript-fetch.py --audio $WAV --id bridge-${SAFE_LABEL}" >&2
fi

TODAY=$(date -u +%Y-%m-%d)
echo
echo "════════ bridge probe: ${LABEL} ════════"
echo "to:        ${TO}${DTMF:+   dtmf: $DTMF}"
echo "held:      ${HELD_S}s"
echo "artifacts: ${WAV}  +.jsonl  +.json${TXT:+  +.txt}"
if [[ -f "$TXT" ]]; then
  echo "transcript:"
  sed 's/^/  | /' "$TXT"
fi
cat <<EOF

── read the transcript, then classify ──────────────────────────────────────
held to --secs + conference/hold audio        → admission = "held"
a HUMAN asked who we are, then hung up        → admission = "operator_screened"
connected then far end hung up on silence     → admission = "dropped"

── dial-ins.toml snippet (adjust admission per the transcript) ─────────────
admission        = "<held|dropped|operator_screened>"
admission_detail = "held ${HELD_S}s; <what the bridge said>"
admission_probed = "${TODAY}"
probe_artifact   = "${WAV}"

── then pin the transcript excerpt in VERIFICATIONS.md Q-ADMIT ─────────────
EOF
