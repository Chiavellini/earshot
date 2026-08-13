#!/usr/bin/env bash
# capture-sidecar.sh — bank the AUDIO of a live webcast call, alongside the arm.
#
#   scripts/capture-sidecar.sh --slug <t> \
#       --config configs/<t>-capture-sidecar.toml \
#       --record-dir state/<EVENT>/capture \
#       --event <EVENT> [--secs 5400]
#
# WHY: a normal `arm` run banks no audio and no transcript, so PRECISION — the
# false-positive rate, the thing gating larger position size — cannot be
# measured after the fact. The readiness study measures RECALL only, on clean
# studio TTS. This script keeps the one artifact that makes everything else
# scoreable offline: the call itself.
#
# Waits for $HOME/<slug>-stream.url on the SAME contract as arm-on-url.sh (the
# q4resolve timer writes it at go-live), then runs `earshot capture-bakeoff`,
# which has no detector, no risk gate and no Kalshi connection — it cannot
# influence an order.
#
# Brackets the capture with scripts/clock-anchor.py (start AND end): the anchor
# is a mono<->wall clock PAIR, and a single or late sample cannot correct
# oscillator drift, which every downstream latency join depends on.
#
# Exit 0 captured / 3 deadline with no URL / 2 usage. Never silent.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

SLUG="" CONFIG="" RECORD_DIR="" EVENT="" SECS=5400 DEADLINE_HHMM=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --slug)         SLUG="${2:?}"; shift 2 ;;
    --config)       CONFIG="${2:?}"; shift 2 ;;
    --record-dir)   RECORD_DIR="${2:?}"; shift 2 ;;
    --event)        EVENT="${2:?}"; shift 2 ;;
    --secs)         SECS="${2:?}"; shift 2 ;;
    --deadline-utc) DEADLINE_HHMM="${2:?}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$SLUG" && -n "$CONFIG" && -n "$RECORD_DIR" && -n "$EVENT" ]] || {
  echo "usage: capture-sidecar.sh --slug S --config C --record-dir D --event E [--secs N] [--deadline-utc HH:MM]" >&2
  exit 2; }
[[ -f "$CONFIG" ]] || { echo "REFUSING: config not found: $CONFIG" >&2; exit 2; }

URLFILE="$HOME/${SLUG}-stream.url"
mkdir -p "$RECORD_DIR"

jnl() { scripts/journal.sh --event "$EVENT" --actor capture-sidecar --action "$1" --outcome "$2" --detail "$3" || true; }

if [[ -n "$DEADLINE_HHMM" ]]; then
  DEADLINE=$(python3 -c "
import calendar,datetime,sys
h,m=sys.argv[1].split(':')
now=datetime.datetime.now(datetime.timezone.utc)
d=now.replace(hour=int(h),minute=int(m),second=0,microsecond=0)
e=calendar.timegm(d.timetuple())
if e < int(now.timestamp()): e += 86400
print(e)" "$DEADLINE_HHMM")
else
  DEADLINE=$(( $(date -u +%s) + 7200 ))
fi

echo "=== capture-sidecar $SLUG $(date -u +%FT%TZ) ==="
echo "url-file  : $URLFILE"
echo "record-dir: $RECORD_DIR"
jnl note info "waiting for $URLFILE (record-dir $RECORD_DIR)"

while [[ ! -s "$URLFILE" ]]; do
  if [[ "$(date -u +%s)" -ge "$DEADLINE" ]]; then
    echo "DEADLINE with no URL — nothing captured" >&2
    jnl note fail "deadline reached with no $URLFILE — no audio banked"
    scripts/notify.sh "$SLUG capture-sidecar MISSED" "No $URLFILE by deadline; the call audio was NOT banked." --quiet || true
    exit 3
  fi
  sleep 5
done

URL="$(cat "$URLFILE")"
echo "$(date -u +%H:%M:%SZ) URL present — starting capture: $URL"

# Clock anchor BEFORE the capture (half of the bracketing pair).
python3 scripts/clock-anchor.py "$RECORD_DIR" >> "$RECORD_DIR/clock-anchor.log" 2>&1 \
  && echo "clock-anchor: start sample written" \
  || echo "clock-anchor: start sample FAILED (latency joins will be degraded)" >&2

# WHY PLAIN ffmpeg AND NOT `earshot capture-bakeoff`: capture-bakeoff refuses a
# config with fewer than 2 [[sources]] legs ("capture-bakeoff needs at least 2
# [[sources]] legs") — it is a leg-COMPARISON tool by construction. Honouring
# that would mean two more full stream pulls on top of the arm's own two, on a
# box that is also running three sweep arms and a book-tape recorder, and CPU
# starvation (not the network) is the documented way a live arm loses audio
# frames. The arm is what makes money tonight; the recorder must not compete
# with it.
#
# What we give up is capture-bakeoff's per-frame <leg>.jsonl (seq/capture_ns),
# which feeds leg-lag.py / abs-lag.py / drace-wallclock.py. That is acceptable:
# the RACE question is answerable without it by joining the arm's own
# decision_ts (report.json) against the book tape's wall_ns, and the jsonl was
# only needed for capture-arrival forensics. The artifact that cannot be
# reconstructed later — the audio itself — is what this banks.
#
# Absolute-time anchor: ffmpeg gives no wall clock, so stamp the start instant
# next to the wav. Transcript alignment needs second-level accuracy, not ms.
python3 -c "
import json,time
json.dump({'wav':'call.wav','start_unix':time.time(),
           'start_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
           'url':'''$URL''','rate_hz':16000,'channels':1,'fmt':'s16le'},
          open('$RECORD_DIR/capture-anchor.json','w'), indent=1)"

jnl note ok "capture starting (ffmpeg->wav), secs=$SECS, url=$URL"
ffmpeg -loglevel warning -nostdin \
  -fflags nobuffer -flags low_delay \
  -probesize 2000000 -analyzeduration 5000000 \
  -live_start_index -1 -http_persistent 1 \
  -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 2 \
  -i "$URL" -vn -ac 1 -ar 16000 -c:a pcm_s16le \
  -t "$SECS" "$RECORD_DIR/call.wav" \
  >> "$RECORD_DIR/capture.log" 2>&1
RC=$?

# Clock anchor AFTER — a single sample cannot correct drift.
python3 scripts/clock-anchor.py "$RECORD_DIR" >> "$RECORD_DIR/clock-anchor.log" 2>&1 \
  && echo "clock-anchor: end sample written" \
  || echo "clock-anchor: end sample FAILED" >&2

BYTES=$(find "$RECORD_DIR" -name '*.wav' -exec ls -l {} \; 2>/dev/null | awk '{s+=$5} END {print s+0}')
echo "capture done rc=$RC, wav bytes=$BYTES"
if [[ "$BYTES" -lt 1000000 ]]; then
  jnl note fail "capture ended rc=$RC but only ${BYTES}B of wav — treat as NO usable audio"
  scripts/notify.sh "$SLUG capture-sidecar THIN" "Only ${BYTES}B of wav banked (rc=$RC) — precision scoring may be impossible." --quiet || true
  exit 1
fi
jnl note ok "audio banked: ${BYTES}B wav in $RECORD_DIR (rc=$RC)"
exit 0
