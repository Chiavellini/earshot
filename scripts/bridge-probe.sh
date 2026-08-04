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
#                   [--ngrok-config <acct.yml>] [--promote [--issuer SPOT]] [--yes]
#
# --promote writes a `held` verdict straight into configs/dial-ins.toml via
# scripts/registry-admit.py (issuer defaults to the label's first token). This
# is THE MSFT RULE working as designed — admission still comes only from a
# probe dial — and it closes the 2026-08-04 SPOT gap where a proven-working
# bridge stayed unregistered because the snippet below was never pasted.
# Only `held` auto-writes; every other verdict needs the transcript read.
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
ACCOUNT="" NGROK_CONFIG="" YES=0 PROMOTE=0 ISSUER="" CORPUS_ONLY=0
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
    --promote)      PROMOTE=1; shift ;;
    --issuer)       ISSUER="$2"; shift 2 ;;
    --corpus-only)  CORPUS_ONLY=1; shift ;;
    --yes)          YES=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$TO" && -n "$LABEL" ]] || {
  echo "usage: $0 --to +1... --label <venue-YYYY-MM-DD> [--dtmf 1234#] [--secs 120] [--bind-port 8099] [--stream-url wss://…] [--account <env>] [--yes]" >&2
  exit 2
}
[[ "$SECS" =~ ^[1-9][0-9]*$ && "$SECS" -le 600 ]] || { echo "REFUSING: --secs must be 1..600 — a probe listens to the entry flow, not the call" >&2; exit 2; }

# ── Long-capture guard (2026-08-04, SPOT) ──────────────────────────────────
# A SHORT probe against an open board is the normal, correct thing: that is how
# admission gets established on event day (admission-window-probe.sh dials at
# T-90..T-30, when the board is necessarily open). A LONG one is not a probe at
# all — it is a call capture with no detector attached, which is exactly what
# took Spotify: --secs 600 against an 18-market board that traded 157,217
# contracts while we recorded it and armed nothing.
#
# So the guard keys on duration, matching this script's own stated purpose —
# "a probe listens to the entry flow, not the call". Entry flows are ~1-2 min.
LONG_PROBE_SECS=300
if [[ "$SECS" -gt "$LONG_PROBE_SECS" && "${CORPUS_ONLY:-0}" != "1" ]]; then
  GUARD_ISSUER=$(echo "${ISSUER:-${LABEL%%-*}}" | tr '[:lower:]' '[:upper:]')
  BOARD_OUT=$(scripts/board-open.py --issuer "$GUARD_ISSUER" 2>&1) && BOARD_RC=0 || BOARD_RC=$?
  if [[ "$BOARD_RC" == "0" ]]; then
    cat >&2 <<EOF
REFUSING: --secs $SECS is a CALL CAPTURE, not an entry probe, and
$GUARD_ISSUER has an OPEN Kalshi mention board:
  $BOARD_OUT

  This tool runs no detector and can place no order. Capturing a live board
  with it is the 2026-08-04 SPOT failure exactly.
  Probe admission with a short leg (--secs 120), then ARM the call:
      scripts/slate-stage.sh --issuer $GUARD_ISSUER && scripts/launch-event.sh …
  Genuine long corpus capture on a live board: --corpus-only
EOF
    exit 6
  fi
fi

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

# ── --promote: write the verdict down instead of hoping someone retypes it ──
# SPOT 2026-08-04 is why this exists. The snippet above was printed, correctly,
# for a bridge that had just admitted us to a live earnings call — and it died
# on the terminal. `configs/dial-ins.toml` kept saying no-entry, slate-stage.sh
# kept staging webcast-only, and an 18-market board traded without us.
#
# THE MSFT RULE HOLDS: this writes admission ONLY from this probe's own dial,
# and ONLY the one verdict that is mechanically inferable — the leg ran to
# --secs with frames flowing. A short hold means the far end hung up, and
# whether that was a screen or a drop is a TRANSCRIPT reading: printed for a
# human, never auto-written.
if [[ "$PROMOTE" == "1" ]]; then
  [[ -n "$ISSUER" ]] || ISSUER="${LABEL%%-*}"
  ISSUER_UC=$(echo "$ISSUER" | tr '[:lower:]' '[:upper:]')
  # "held to the cap" = within 5% of what we asked for. The probe stops itself
  # at --secs, so a full-length leg is the bridge never hanging up on us.
  HELD_OK=$(python3 -c "print(1 if $HELD_S >= $SECS * 0.95 else 0)")
  if [[ "$HELD_OK" == "1" ]]; then
    STALLS=$(python3 -c "import json;print(json.load(open('$JSON'))['stalls_over_100ms'])")
    scripts/registry-admit.py --auto \
      --issuer "$ISSUER_UC" --admission held \
      --detail "held ${HELD_S}s to the probe cap (${SECS}s), ${STALLS} stalls >100ms${DTMF:+, DTMF accepted}" \
      --probed "$TODAY" --artifact "$WAV" \
      --number "$TO" ${DTMF:+--passcode "$DTMF"} \
      || echo "⚠ registry-admit failed — pin [$ISSUER_UC] by hand from the snippet above" >&2
    echo
    echo "⇒ Next: stage the phone arm for this issuer BEFORE the call:"
    echo "     scripts/slate-stage.sh --issuer $ISSUER_UC"
  else
    echo
    echo "⇒ NOT auto-promoted: held ${HELD_S}s of ${SECS}s — the far end hung up." >&2
    echo "  Read the transcript, then record the verdict yourself:" >&2
    echo "     scripts/registry-admit.py --issuer $ISSUER_UC \\" >&2
    echo "         --admission <dropped|operator_screened|passcode_required> \\" >&2
    echo "         --detail '<what the bridge said>' --probed $TODAY --artifact $WAV" >&2
  fi
fi
