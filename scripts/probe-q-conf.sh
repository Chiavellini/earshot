#!/usr/bin/env bash
# Q-CONF wire pin (docs/OPERATOR-HANDLER.md Phase 0): does a call leg whose
# TwiML is <Start><Stream track="inbound_track"> + <Dial><Conference> keep its
# media fork alive and carrying the FAR END's audio while parked in the
# conference — and does the OTHER conference participant's voice stay OFF the
# inbound track? This is the load-bearing hypothesis of operator handler v1
# (the user's voice must never enter the pipeline). The pin decides, not memory.
#
#   probe-q-conf.sh [--secs 90] [--label qconf-YYYY-MM-DD] [--bind-port 8099]
#                   [--stream-url wss://host/media] [--account <env-file>]
#                   [--ngrok-config <acct.yml>] [--with-cell --config <event.toml>]
#                   [--yes]
#
# Step 1 (unattended): dials the TTS rehearsal number with conference TwiML
#   (startConferenceOnEnter="true" here so media starts immediately — the live
#   operator dial uses "false" and waits for the human) and captures the fork
#   via `earshot bridge-probe`. PASS = frames flow for the whole window and the
#   WAV contains the TTS script.
# Step 2 (--with-cell): additionally pages the configured cell into the SAME
#   conference. The human answers, says the marker phrase below, and stays on.
#   PASS = the marker phrase is ABSENT from the captured WAV/transcript while
#   the TTS audio is present (inbound_track excludes conference-injected audio).
#
# HARD DESTINATION ALLOWLIST — no override flag exists:
#   bridge-sim leg: +17627212978 (our TTS rehearsal number) ONLY
#   cell leg:       [operator_handler] cell_number from --config ONLY
set -euo pipefail
cd "$(dirname "$0")/.."

# The only number the far-end leg may dial. Constant, not a flag.
TTS_NUMBER="+17627212978"
MARKER_PHRASE="earshot marker check one two three"

SECS=90 LABEL="" BIND_PORT=8099 STREAM_URL_OVERRIDE=""
ACCOUNT="" NGROK_CONFIG="" WITH_CELL=0 CONFIG="" YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --secs)         SECS="$2"; shift 2 ;;
    --label)        LABEL="$2"; shift 2 ;;
    --bind-port)    BIND_PORT="$2"; shift 2 ;;
    --stream-url)   STREAM_URL_OVERRIDE="$2"; shift 2 ;;
    --account)      ACCOUNT="$2"; shift 2 ;;
    --ngrok-config) NGROK_CONFIG="$2"; shift 2 ;;
    --with-cell)    WITH_CELL=1; shift ;;
    --config)       CONFIG="$2"; shift 2 ;;
    --yes)          YES=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ "$SECS" =~ ^[1-9][0-9]*$ && "$SECS" -le 600 ]] || { echo "REFUSING: --secs must be 1..600" >&2; exit 2; }
[[ -n "$LABEL" ]] || LABEL="qconf-$(date -u +%Y-%m-%d)"

CELL=""
if [[ "$WITH_CELL" == "1" ]]; then
  [[ -n "$CONFIG" && -f "$CONFIG" ]] || { echo "--with-cell requires --config <event.toml> carrying [operator_handler] cell_number" >&2; exit 2; }
  CELL=$(sed -n '/^\[operator_handler\]/,/^\[/s/^[[:space:]]*cell_number[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
  [[ "$CELL" =~ ^\+[1-9][0-9]{7,14}$ ]] || { echo "REFUSING: [operator_handler] cell_number in ${CONFIG} is missing/not E.164 — the cell leg has no other permitted destination" >&2; exit 2; }
fi

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
CONFERENCE="qconf-$(date +%s)"
SID_FILE=$(mktemp) CELL_SID_FILE=$(mktemp)
PROBE_PID="" NGROK_PID=""
cleanup() {
  [[ -s "$SID_FILE" ]] && scripts/twilio-hangup.sh "$(cat "$SID_FILE")" >/dev/null 2>&1 || true
  [[ -s "$CELL_SID_FILE" ]] && scripts/twilio-hangup.sh "$(cat "$CELL_SID_FILE")" >/dev/null 2>&1 || true
  [[ -n "$PROBE_PID" ]] && kill "$PROBE_PID" 2>/dev/null || true
  [[ -n "$NGROK_PID" ]] && kill "$NGROK_PID" 2>/dev/null || true
  rm -f "$SID_FILE" "$CELL_SID_FILE"
}
trap cleanup EXIT

# Tunnel: an explicit --stream-url (TLS-on-box) skips ngrok; otherwise start
# one agent for this probe's port (same select-by-addr rule as bridge-probe.sh).
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

# Far-end leg: EXACTLY the operator bridge-leg TwiML shape, except
# startConferenceOnEnter="true" so the conference (and thus media) starts the
# moment the TTS number answers — no human required for step 1.
TWIML="<Response><Start><Stream url=\"${STREAM_URL}\" track=\"inbound_track\"/></Start><Dial timeLimit=\"$((SECS + 30))\"><Conference beep=\"false\" startConferenceOnEnter=\"true\" endConferenceOnExit=\"true\" waitUrl=\"\">${CONFERENCE}</Conference></Dial></Response>"
echo "dialing TTS rehearsal number ${TTS_NUMBER} into conference ${CONFERENCE}"
RESP=$(curl -s -X POST "${API}/Calls.json" \
  --data-urlencode "To=${TTS_NUMBER}" \
  --data-urlencode "From=${TWILIO_FROM_NUMBER}" \
  --data-urlencode "Twiml=${TWIML}" \
  -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}")
SID=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("sid","ERROR"))')
[[ "$SID" != "ERROR" && -n "$SID" ]] || { echo "far-end leg FAILED:" >&2; echo "$RESP" >&2; exit 1; }
echo "$SID" > "$SID_FILE"
echo "far-end leg: CallSid=${SID}"

# Step 2: page the configured cell into the SAME conference (inline TwiML —
# a wire probe must not depend on the gated operator scripts).
if [[ "$WITH_CELL" == "1" ]]; then
  CELL_TWIML="<Response><Say>EARSHOT Q-CONF probe. Say the marker phrase: ${MARKER_PHRASE}. Then stay on the line until the far end hangs up.</Say><Dial><Conference beep=\"false\" startConferenceOnEnter=\"true\" endConferenceOnExit=\"false\">${CONFERENCE}</Conference></Dial></Response>"
  RESP2=$(curl -s -X POST "${API}/Calls.json" \
    --data-urlencode "To=${CELL}" \
    --data-urlencode "From=${TWILIO_FROM_NUMBER}" \
    --data-urlencode "Twiml=${CELL_TWIML}" \
    -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}")
  CELL_SID=$(echo "$RESP2" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("sid","ERROR"))')
  [[ "$CELL_SID" != "ERROR" && -n "$CELL_SID" ]] || { echo "cell leg FAILED:" >&2; echo "$RESP2" >&2; exit 1; }
  echo "$CELL_SID" > "$CELL_SID_FILE"
  cat <<EOF

════ HUMAN STEP — answer ${CELL} now ════
1. Answer the call; you will hear the prompt, then join conference ${CONFERENCE}.
2. Say the marker phrase CLEARLY, twice:  "${MARKER_PHRASE}"
3. Stay on the line until the probe window (${SECS}s) ends.
PASS = the marker phrase is ABSENT from the captured WAV/transcript below
while the TTS script IS present (your voice never reaches inbound_track).
EOF
fi

# The probe exits on far-end hangup (stream stop) or at --secs.
set +e
wait "$PROBE_PID"
PROBE_RC=$?
set -e
PROBE_PID=""
[[ -s "$SID_FILE" ]] && scripts/twilio-hangup.sh "$(cat "$SID_FILE")" >/dev/null 2>&1 || true
[[ -s "$CELL_SID_FILE" ]] && scripts/twilio-hangup.sh "$(cat "$CELL_SID_FILE")" >/dev/null 2>&1 || true
[[ "$PROBE_RC" == 0 ]] || { echo "bridge-probe FAILED (rc=$PROBE_RC) — no artifact; nothing to pin" >&2; exit "$PROBE_RC"; }

WAV="${OUT_DIR}/${SAFE_LABEL}.wav"
JSON="${OUT_DIR}/${SAFE_LABEL}.json"
JSONL="${OUT_DIR}/${SAFE_LABEL}.jsonl"
HELD_S=$(python3 -c "import json;print(round(json.load(open('$JSON'))['held_ms']/1000,1))")
FRAMES=$(python3 -c "import json;print(json.load(open('$JSON'))['frames'])")

# Transcribe next to the artifact when the corpus venv is available.
TXT="${OUT_DIR}/${SAFE_LABEL}.txt"
MARKER_VERDICT="(no transcript — check the WAV by ear)"
if [[ -x .venv-corpus/bin/python ]]; then
  .venv-corpus/bin/python scripts/transcript-fetch.py --audio "$WAV" --id "bridge-${SAFE_LABEL}" >/dev/null
  {
    echo "# ${SAFE_LABEL}.wav — faster-whisper, $(date -u +%Y-%m-%d)"
    echo "# Q-CONF probe: ${TTS_NUMBER} in <Dial><Conference>, held ${HELD_S}s${CELL:+, cell leg ${CELL}}"
    cat "corpus/transcripts/bridge-${SAFE_LABEL}.txt"
  } > "$TXT"
  if grep -qi "marker" "$TXT"; then
    MARKER_VERDICT="MARKER PRESENT on inbound_track — HYPOTHESIS FAILS (conference audio leaks into the fork)"
  else
    MARKER_VERDICT="marker absent from inbound_track"
  fi
fi

TODAY=$(date -u +%Y-%m-%d)
cat <<EOF

════════ Q-CONF probe: ${LABEL} ════════
far end:    ${TTS_NUMBER} (TTS rehearsal)   conference: ${CONFERENCE}
frames:     ${FRAMES}   held: ${HELD_S}s of a ${SECS}s window
artifacts:  ${WAV}  +.jsonl  +.json$( [[ -f "$TXT" ]] && echo "  +.txt")
$( [[ -f "$TXT" ]] && sed 's/^/  | /' "$TXT")

── read, then judge ─────────────────────────────────────────────────────────
stream flowed for the whole window + TTS script audible  → fork SURVIVES <Dial><Conference>
${CELL:+marker check: ${MARKER_VERDICT}
}
── then pin in VERIFICATIONS.md (new Q-CONF section) ────────────────────────
## Q-CONF — <Start><Stream> fork vs <Dial><Conference> (${TODAY}, live)
**Claim:** a <Start><Stream track="inbound_track"> fork started before
<Dial><Conference> <keeps flowing|dies> once the call enters the conference,
and conference-injected audio (the cell participant) <does|does not> appear on
the inbound track.
**Evidence:** ${WAV} (${FRAMES} frames, held ${HELD_S}s), CallSid $(cat "$SID_FILE" 2>/dev/null || echo "<sid>")${CELL:+, cell leg marker test: ${MARKER_VERDICT}}.
**Implication:** operator handler v1 <may|may not> carry the capture fork on
the bridge leg itself. Confidence: <high|med>. Re-verify: on any Twilio
Media Streams behavior change.
EOF
