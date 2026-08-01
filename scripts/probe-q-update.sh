#!/usr/bin/env bash
# Q-UPDATE wire pin (docs/OPERATOR-HANDLER.md Phase 0): does POSTing
# replacement TwiML to a live call (`POST /Calls/<sid>.json` with `Twiml=`)
# KILL a running <Start><Stream> fork, or does the fork keep flowing across
# the TwiML swap? Decides whether mid-call REST re-steering (v2 clip play,
# conference redirect of an already-connected bridge) is even possible.
#
#   probe-q-update.sh [--after 20] [--post 30] [--label qupdate-YYYY-MM-DD]
#                     [--bind-port 8099] [--stream-url wss://host/media]
#                     [--account <env-file>] [--ngrok-config <acct.yml>] [--yes]
#
# Flow: dial the TTS rehearsal number with plain <Start><Stream> + <Pause>
# TwiML (inline — this script is self-contained), wait --after seconds, POST
# `Twiml=<Response><Pause length="120"/></Response>` to the live call, observe
# --post more seconds, then compare per-frame timestamps in the bridge-probe
# JSONL before/after the update instant. Keep --after <= 40: the TTS script is
# ~55 s long and the far end hangs up after it.
#
# HARD DESTINATION ALLOWLIST — no override flag exists: +17627212978 ONLY.
set -euo pipefail
cd "$(dirname "$0")/.."

TTS_NUMBER="+17627212978"

AFTER=20 POST=30 LABEL="" BIND_PORT=8099 STREAM_URL_OVERRIDE=""
ACCOUNT="" NGROK_CONFIG="" YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --after)        AFTER="$2"; shift 2 ;;
    --post)         POST="$2"; shift 2 ;;
    --label)        LABEL="$2"; shift 2 ;;
    --bind-port)    BIND_PORT="$2"; shift 2 ;;
    --stream-url)   STREAM_URL_OVERRIDE="$2"; shift 2 ;;
    --account)      ACCOUNT="$2"; shift 2 ;;
    --ngrok-config) NGROK_CONFIG="$2"; shift 2 ;;
    --yes)          YES=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ "$AFTER" =~ ^[1-9][0-9]*$ && "$AFTER" -le 60 ]] || { echo "REFUSING: --after must be 1..60 s" >&2; exit 2; }
[[ "$POST" =~ ^[1-9][0-9]*$ && "$POST" -le 120 ]] || { echo "REFUSING: --post must be 1..120 s" >&2; exit 2; }
[[ -n "$LABEL" ]] || LABEL="qupdate-$(date -u +%Y-%m-%d)"
SECS=$((AFTER + POST + 10))

# Guard: never probe while an event is armed (same process fingerprints as
# scripts/bridge-probe.sh / run-arm.sh guard 8).
LIVE=$( { pgrep -f -- "--state-dir state/" || true; pgrep -f "launch-event.sh" || true; } | sort -u | grep -v "^$$\$" || true)
if [[ -n "$LIVE" && "$YES" != "1" ]]; then
  echo "REFUSING: armed event process(es) live (pids: $(echo "$LIVE" | tr '\n' ' '))." >&2
  echo "  A probe must never share Twilio/tunnel resources with a live traded event. Re-run with --yes only if you are sure these are unrelated." >&2
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
API="https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}"

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

# Tunnel: --stream-url (TLS-on-box) skips ngrok, else one agent for this port.
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
    for PORT_TRY in "$NGROK_WEB_PORT" 4040; do
      STREAM_HOST=$(curl -s "http://127.0.0.1:${PORT_TRY}/api/tunnels" 2>/dev/null \
        | BIND_PORT="$BIND_PORT" python3 -c '
import sys, json, os
port = os.environ["BIND_PORT"]
try: tuns = json.load(sys.stdin).get("tunnels", [])
except Exception: tuns = []
match = [t for t in tuns if t.get("config", {}).get("addr", "").endswith(":" + port)]
print(match[0]["public_url"].split("://")[1] if match else "")
' || true)
      [[ -n "$STREAM_HOST" ]] && break
    done
    [[ -n "$STREAM_HOST" ]] && break
    kill -0 "$NGROK_PID" 2>/dev/null || { echo "ngrok exited — log: $NGROK_LOG" >&2; exit 1; }
    sleep 1
  done
  [[ -n "$STREAM_HOST" ]] || { echo "could not read ngrok tunnel for :${BIND_PORT} — log: $NGROK_LOG" >&2; exit 1; }
  STREAM_URL="wss://${STREAM_HOST}/media"
  echo "ngrok tunnel: ${STREAM_URL}"
fi

export TWILIO_STREAM_URL="$STREAM_URL"
export TWILIO_AUTH_TOKEN

# Capture leg first, THEN dial (same ordering rule as arm).
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

# Plain listen-only TwiML, inline (NOT via twilio-dial.sh — self-contained pin).
TWIML="<Response><Start><Stream url=\"${STREAM_URL}\" track=\"inbound_track\"/></Start><Pause length=\"$((SECS + 30))\"/></Response>"
echo "dialing TTS rehearsal number ${TTS_NUMBER}"
RESP=$(curl -s -X POST "${API}/Calls.json" \
  --data-urlencode "To=${TTS_NUMBER}" \
  --data-urlencode "From=${TWILIO_FROM_NUMBER}" \
  --data-urlencode "Twiml=${TWIML}" \
  -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}")
SID=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("sid","ERROR"))')
[[ "$SID" != "ERROR" && -n "$SID" ]] || { echo "call origination FAILED:" >&2; echo "$RESP" >&2; exit 1; }
echo "$SID" > "$SID_FILE"
T_DIAL=$(date +%s)
echo "call placed: CallSid=${SID}; updating TwiML in ${AFTER}s"

sleep "$AFTER"

# THE PIN: replace the live call's TwiML. If Twilio tears the <Start><Stream>
# fork down with the old document, frames stop here; if the fork is
# call-scoped, they keep flowing through the new <Pause>.
T_UPDATE=$(date +%s)
UPDATE_OFFSET=$((T_UPDATE - T_DIAL))
URESP=$(curl -s -X POST "${API}/Calls/${SID}.json" \
  --data-urlencode "Twiml=<Response><Pause length=\"120\"/></Response>" \
  -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}")
USTATUS=$(echo "$URESP" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("status") or d.get("code") or "ERROR")')
echo "TwiML update POSTed at t≈${UPDATE_OFFSET}s after dial (call status: ${USTATUS}); observing ${POST}s more"

sleep "$POST"
scripts/twilio-hangup.sh "$SID" >/dev/null 2>&1 || true

set +e
wait "$PROBE_PID"
PROBE_RC=$?
set -e
PROBE_PID=""
[[ "$PROBE_RC" == 0 ]] || { echo "bridge-probe FAILED (rc=$PROBE_RC) — no artifact; nothing to pin" >&2; exit "$PROBE_RC"; }

WAV="${OUT_DIR}/${SAFE_LABEL}.wav"
JSON="${OUT_DIR}/${SAFE_LABEL}.json"
JSONL="${OUT_DIR}/${SAFE_LABEL}.jsonl"

# Frame accounting around the update instant. capture_ns is monotonic; frame 0
# lands ~1-3 s after the dial POST (ring + answer + stream start), so rel-time
# comparisons vs UPDATE_OFFSET carry that ~±3 s skew — decisive for a
# kept-flowing/died verdict, which differs by tens of seconds.
VERDICT=$(OFFSET="$UPDATE_OFFSET" JSONL="$JSONL" python3 - <<'PY'
import json, os
offset = float(os.environ["OFFSET"])
frames = [json.loads(l) for l in open(os.environ["JSONL"]) if l.strip()]
if not frames:
    print("NO FRAMES captured — nothing to judge (stream never started)")
    raise SystemExit
t0 = frames[0]["capture_ns"]
rel = [(f["capture_ns"] - t0) / 1e9 for f in frames]
before = sum(1 for t in rel if t < offset)
after = len(rel) - before
tail = rel[-1]
print(f"frames_before_update={before}  frames_after_update={after}  "
      f"stream_tail={tail:.1f}s vs update_at≈{offset:.0f}s (±3s dial-to-media skew)")
if tail >= offset + 5:
    print("VERDICT: stream KEPT FLOWING across the TwiML replacement")
elif tail <= offset + 3:
    print("VERDICT: stream DIED at (or before) the TwiML replacement")
else:
    print("VERDICT: AMBIGUOUS — tail within the skew window; rerun with a larger --post")
PY
)
HELD_S=$(python3 -c "import json;print(round(json.load(open('$JSON'))['held_ms']/1000,1))")
FRAMES=$(python3 -c "import json;print(json.load(open('$JSON'))['frames'])")

TODAY=$(date -u +%Y-%m-%d)
cat <<EOF

════════ Q-UPDATE probe: ${LABEL} ════════
to:         ${TTS_NUMBER}   CallSid: ${SID}
timeline:   dial → +${UPDATE_OFFSET}s TwiML replaced → +${POST}s observed → hangup
frames:     ${FRAMES}   held: ${HELD_S}s
${VERDICT}
artifacts:  ${WAV}  +.jsonl  +.json

── then pin in VERIFICATIONS.md (new Q-UPDATE section) ──────────────────────
## Q-UPDATE — live TwiML replacement vs a running <Start><Stream> (${TODAY}, live)
**Claim:** POST /Calls/<sid>.json with replacement TwiML <kills|does not kill>
a running <Start><Stream> fork (fork is <document|call>-scoped).
**Evidence:** ${WAV} + ${JSONL} — ${VERDICT}
CallSid ${SID}, update at ≈${UPDATE_OFFSET}s, observed ${POST}s after.
**Implication:** mid-call REST re-steering (v2 clip play / conference
redirect of a connected bridge) <is|is not> compatible with an uninterrupted
capture fork. Confidence: <high|med>. Re-verify: on any Twilio Media Streams
behavior change.
EOF
