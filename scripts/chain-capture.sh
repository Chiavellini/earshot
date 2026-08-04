#!/usr/bin/env bash
# Continuous bridge capture: chain 600 s legs back-to-back until a deadline.
#
# Why this exists: scripts/bridge-probe.sh transcribes with faster-whisper after
# each leg (measured 6 min on SPOT 2026-08-04), so chaining IT leaves a 6-minute
# hole between legs. This drives `earshot bridge-probe` + scripts/twilio-dial.sh
# directly, holds ONE ngrok tunnel for the whole run (free tier = 1 tunnel, and a
# restart would change the public URL that Twilio signs), and defers every
# transcription to after the last leg.
#
#   chain-capture.sh --to +1... --dtmf 1234# --label pgr-2026-08-04 \
#                    --until-utc 15:05 [--secs 600] [--bind-port 8099]
#                    [--account ~/.earshot/ms.env]
#
# Gap between legs is one dial (~3-5 s), not one transcription.
set -uo pipefail
cd "$(dirname "$0")/.."

TO="" DTMF="" LABEL="" UNTIL_UTC="" SECS=600 BIND_PORT=8099
ACCOUNT="$HOME/.earshot/ms.env"
CORPUS_ONLY=0 ISSUER="" MIN_LEG_FRAC=10 MAX_SHORT=3
while [[ $# -gt 0 ]]; do
  case "$1" in
    --to)          TO="$2"; shift 2 ;;
    --dtmf)        DTMF="$2"; shift 2 ;;
    --label)       LABEL="$2"; shift 2 ;;
    --until-utc)   UNTIL_UTC="$2"; shift 2 ;;
    --secs)        SECS="$2"; shift 2 ;;
    --bind-port)   BIND_PORT="$2"; shift 2 ;;
    --account)     ACCOUNT="$2"; shift 2 ;;
    --corpus-only) CORPUS_ONLY=1; shift ;;
    --issuer)      ISSUER="$2"; shift 2 ;;
    --max-short)   MAX_SHORT="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$TO" && -n "$LABEL" && -n "$UNTIL_UTC" ]] || {
  echo "usage: $0 --to +1... --label <l> --until-utc HH:MM [--dtmf 1234#] [--secs 600] [--bind-port N] [--account <env>] [--corpus-only] [--issuer X] [--max-short N]" >&2
  exit 2
}

# ── THE REFUSE-TO-RECORD GUARD (2026-08-04) ─────────────────────────────────
# This script records to WAV. It has no detector, no keyword table, no Kalshi
# connection, and it cannot place an order. On 2026-08-04 it was pointed at
# Progressive's earnings call while a 12-market board traded ~170,000 contracts
# through that same call, and nothing objected — because a recorder has no idea
# whether what it is recording is tradeable. Spotify went the same way an hour
# earlier via bridge-probe.sh: 18 markets, 157,217 contracts, zero orders.
#
# So: if the issuer has an OPEN mention board, this is not corpus work, and the
# arm path (scripts/launch-event.sh) is what should be running. Say so and stop.
# `--corpus-only` is the deliberate override for genuine corpus capture on a
# live board — it makes the choice explicit and greppable instead of silent.
#
# Fail-CLOSED: an UNDETERMINED board check (exit 2) also refuses. A guard that
# fails open on a network hiccup is how this class comes back.
[[ -n "$ISSUER" ]] || ISSUER="${LABEL%%-*}"
ISSUER_UC=$(echo "$ISSUER" | tr '[:lower:]' '[:upper:]')
if [[ "$CORPUS_ONLY" != "1" ]]; then
  BOARD_OUT=$(scripts/board-open.py --issuer "$ISSUER_UC" 2>&1) && BOARD_RC=0 || BOARD_RC=$?
  case "$BOARD_RC" in
    0) cat >&2 <<EOF
REFUSING: $ISSUER_UC has an OPEN Kalshi mention board.
  $BOARD_OUT

  This script RECORDS. It runs no detector and can place no order, so running
  it against a live board is how SPOT and PGR were lost on 2026-08-04 —
  ~327,000 contracts traded across the two calls we were listening to, and we
  placed nothing.

  Arm it instead:
      scripts/registry-admit.py --issuer $ISSUER_UC --admission held ...   # if not yet registered
      scripts/slate-stage.sh --issuer $ISSUER_UC
      scripts/preflight-event.sh --config <cfg>
      scripts/launch-event.sh --config <cfg> --to $TO --tls-host <box>

  If this really is corpus work on a live board, say so: --corpus-only
EOF
       exit 6 ;;
    1) : ;;   # no open board — plainly corpus work, proceed
    *) echo "REFUSING: could not determine whether $ISSUER_UC has an open board:" >&2
       echo "  $BOARD_OUT" >&2
       echo "  Fail-closed on purpose. Re-run with --corpus-only if you are sure." >&2
       exit 7 ;;
  esac
fi

# shellcheck disable=SC1090
source "$ACCOUNT"
: "${TWILIO_ACCOUNT_SID:?}" "${TWILIO_AUTH_TOKEN:?}" "${TWILIO_FROM_NUMBER:?}"
export TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN TWILIO_FROM_NUMBER

BIN="target/release/earshot"
OUT_DIR="corpus/bridge-probes"
mkdir -p "$OUT_DIR" state/chain

DEADLINE_EPOCH=$(python3 -c "
import datetime
now = datetime.datetime.now(datetime.timezone.utc)
h, m = '$UNTIL_UTC'.split(':')
d = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
if d < now: d += datetime.timedelta(days=1)
print(int(d.timestamp()))
")
echo "[chain] deadline $UNTIL_UTC UTC (epoch $DEADLINE_EPOCH)"

# Free out the port and any tunnel a previous single-shot probe left behind.
pkill -f "ngrok http ${BIND_PORT}" 2>/dev/null || true
sleep 2

nohup ngrok http "$BIND_PORT" --log stdout --log-level info > state/chain/ngrok.log 2>&1 &
NGROK_PID=$!
trap 'kill $NGROK_PID 2>/dev/null || true' EXIT

PUBLIC=""
for _ in $(seq 1 30); do
  PUBLIC=$(curl -s --max-time 3 http://127.0.0.1:4040/api/tunnels 2>/dev/null \
    | python3 -c "import sys,json
try:
    t=json.load(sys.stdin)['tunnels']
    print(next(x['public_url'] for x in t if x['public_url'].startswith('https')))
except Exception: pass" 2>/dev/null)
  [[ -n "$PUBLIC" ]] && break
  sleep 1
done
[[ -n "$PUBLIC" ]] || { echo "[chain] FATAL: no ngrok tunnel" >&2; exit 3; }

STREAM_URL="wss://${PUBLIC#https://}/media"
export TWILIO_STREAM_URL="$STREAM_URL"
echo "[chain] tunnel $STREAM_URL"

LEG=0
SHORT=0
while [[ $(date +%s) -lt $DEADLINE_EPOCH ]]; do
  LEG=$((LEG + 1))
  REMAIN=$((DEADLINE_EPOCH - $(date +%s)))
  THIS=$SECS
  [[ $REMAIN -lt $SECS ]] && THIS=$REMAIN
  [[ $THIS -lt 20 ]] && break

  LEG_LABEL="${LABEL}-leg$(printf '%02d' $LEG)"
  echo "[chain] leg $LEG ($THIS s) $(date -u '+%H:%M:%SZ') -> $LEG_LABEL"

  "$BIN" bridge-probe --bind "127.0.0.1:${BIND_PORT}" --secs "$THIS" \
      --label "$LEG_LABEL" --out-dir "$OUT_DIR" >> state/chain/${LABEL}.log 2>&1 &
  PROBE_PID=$!

  # The listener binds synchronously at construction, but give it a moment
  # before handing Twilio a URL that would 403 on an unbound port.
  for _ in $(seq 1 20); do
    nc -z 127.0.0.1 "$BIND_PORT" 2>/dev/null && break
    sleep 0.5
  done

  SID_FILE="state/chain/${LEG_LABEL}.sid"
  DIAL_ARGS=(--to "$TO" --stream-url "$STREAM_URL" --sid-file "$SID_FILE"
             --pause-secs $((THIS + 30)))
  [[ -n "$DTMF" ]] && DIAL_ARGS+=(--dtmf "$DTMF")
  scripts/twilio-dial.sh "${DIAL_ARGS[@]}" >> state/chain/${LABEL}.log 2>&1 \
    || echo "[chain] leg $LEG dial FAILED" >&2

  wait $PROBE_PID
  echo "[chain] leg $LEG done $(date -u '+%H:%M:%SZ')"

  # ── "a dial is not a capture" (docs/nodes/control-supervise.md) ───────────
  # Measured 2026-08-04: Progressive's call ended during leg 6, and this loop
  # then re-dialled 17 more times — legs 07..23 each held ~12.3 s of "the
  # conference has ended" before the bridge hung up. Wall-clock alone cannot
  # tell "still capturing" from "dialling a corpse", so count the corpses.
  LEG_JSON="${OUT_DIR}/$(echo "$LEG_LABEL" | tr -c 'a-zA-Z0-9' '_' | sed 's/_*$//').json"
  LEG_HELD=0
  [[ -f "$LEG_JSON" ]] && LEG_HELD=$(python3 -c "
import json
try: print(int(json.load(open('$LEG_JSON'))['held_ms']/1000))
except Exception: print(0)" 2>/dev/null || echo 0)
  if [[ "$LEG_HELD" -lt $((THIS * MIN_LEG_FRAC / 100)) ]]; then
    SHORT=$((SHORT + 1))
    echo "[chain] leg $LEG held ${LEG_HELD}s of ${THIS}s — SHORT ($SHORT/$MAX_SHORT)"
    if [[ "$SHORT" -ge "$MAX_SHORT" ]]; then
      echo "[chain] STOPPING: $SHORT consecutive short legs — the bridge is done." >&2
      echo "[chain] Re-dialling a hung-up conference costs money and captures nothing." >&2
      break
    fi
  else
    SHORT=0
  fi

  # Hang up before the next dial: the PIN admits one caller at a time, so an
  # overlapping leg would be refused or would evict the one that works.
  if [[ -f "$SID_FILE" ]]; then
    scripts/twilio-hangup.sh "$(cat "$SID_FILE")" >> state/chain/${LABEL}.log 2>&1 || true
  fi
done

echo "[chain] COMPLETE — $LEG leg(s), artifacts in $OUT_DIR/${LABEL}-leg*.wav"
