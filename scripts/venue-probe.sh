#!/usr/bin/env bash
# venue-probe.sh <stream-url | youtube-url> [--secs 6] [--json]
# venue-probe.sh --check                  # offline self-test
#
# Can this host actually DECODE AUDIO from this venue? Gate on PCM BYTES,
# never on an HTTP status.
#
# Why (2026-07-29, HOOD — the first YouTube-hosted event we ever armed):
#   scripts/arm-when-url.sh checked `HTTP 200` + `#EXTM3U` on the master
#   manifest. Both passed. The arm started clean. Then both legs died inside
#   35 s with frames=0 (ffmpeg "exit status: 183"; segs=0 seg_err=71
#   poll_err=1625) because YouTube bot-walls the datacenter IP at the SEGMENT
#   layer — "Sign in to confirm you're not a bot" — while the manifest keeps
#   returning 200. The manifest is a decoy. A reachability check cannot clear a
#   venue; only decoded audio can.
#   Measured the same minute, same video, from the CDMX laptop: exactly 192000
#   bytes of PCM. So the split was IP reputation, not tooling.
#
# ⚠ This is a VENUE-ONBOARDING gate, deliberately NOT a T-20 arm guard.
# At T-20 venues legitimately are not streaming yet (CMG served standby
# hold-music at its own start time), so a hard PCM gate there would refuse
# valid arms. Run this once, against a LIVE stream, when adopting a venue class
# — and read a failure as "this host cannot capture this venue", not "the URL
# is wrong".
#
# Expected bytes = secs * 16000 Hz * 2 bytes (s16 mono) = 192000 for 6 s.
# A partial read still proves decodability, so the bar is a floor, not equality.
set -uo pipefail

URL=""; SECS=6; JSON=0; CHECK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) CHECK=1; shift ;;
    --secs)  SECS="$2"; shift 2 ;;
    --json)  JSON=1; shift ;;
    -*) echo "unknown arg: $1" >&2; exit 2 ;;
    *)  URL="$1"; shift ;;
  esac
done

RATE=16000
want=$(( SECS * RATE * 2 ))
floor=$(( want / 2 ))          # ≥50% of the window = the stream decodes

# yt-dlp owns YouTube's segment addressing; ffmpeg cannot chew a watch page,
# and hls-edge-fetch.py speaks manifests only. Route by URL shape.
is_youtube() { [[ "$1" == *youtube.com/* || "$1" == *youtu.be/* ]]; }

probe() {  # probe <url> <secs> -> prints byte count; stderr saved to $ERRF
  local url="$1" secs="$2"
  if is_youtube "$url"; then
    yt-dlp -q --no-warnings -f bestaudio/best --no-part -o - "$url" 2>>"$ERRF" \
      | ffmpeg -loglevel error -i - -vn -f s16le -ac 1 -ar "$RATE" -t "$secs" - 2>>"$ERRF" \
      | wc -c | tr -d ' '
  else
    ffmpeg -loglevel error -i "$url" -vn -f s16le -ac 1 -ar "$RATE" -t "$secs" - 2>>"$ERRF" \
      | wc -c | tr -d ' '
  fi
}

if (( CHECK )); then
  P=0; F=0
  chk() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; P=$((P+1));
          else echo "  FAIL  $1 (expected $2, got $3)"; F=$((F+1)); fi; }
  echo "venue-probe self-test:"
  chk "youtube.com detected"     yes "$(is_youtube https://www.youtube.com/live/x && echo yes)"
  chk "youtu.be detected"        yes "$(is_youtube https://youtu.be/x && echo yes)"
  chk "m3u8 NOT youtube"         no  "$(is_youtube https://a.net/b.m3u8 && echo yes || echo no)"
  chk "6s window = 192000 bytes" 192000 "$want"
  chk "floor is half the window" 96000  "$floor"
  for t in yt-dlp ffmpeg; do
    if command -v "$t" >/dev/null 2>&1; then echo "  PASS  tool present: $t"; P=$((P+1));
    else echo "  FAIL  tool MISSING: $t"; F=$((F+1)); fi
  done
  echo
  (( F )) && { echo "SELFTEST FAIL ($F failed, $P passed)"; exit 1; }
  echo "SELFTEST PASS ($P checks)"; exit 0
fi

[[ -n "$URL" ]] || { echo "usage: $0 <stream-url|youtube-url> [--secs N] [--json] | --check" >&2; exit 2; }

ERRF="$(mktemp)"; trap 'rm -f "$ERRF"' EXIT
HOSTNAME_S="$(hostname -s 2>/dev/null || echo unknown)"
echo "venue-probe: $HOSTNAME_S → ${URL:0:100}"
echo "  decoding ${SECS}s (want ${want} bytes, floor ${floor})…"

# `probe` prints wc's count AND exits nonzero when a stage of the pipe fails
# (pipefail), so `|| echo 0` used to APPEND a second line and leave GOT="0\n0",
# which then blew up `(( ))` with a syntax error. Take the last bare-integer
# line instead and never let a non-numeric reach the arithmetic.
RAW="$(probe "$URL" "$SECS" || true)"
GOT="$(printf '%s\n' "$RAW" | grep -oE '^[0-9]+$' | tail -1)"
GOT="${GOT:-0}"
ERRTAIL="$(tr -d '\r' < "$ERRF" | grep -viE '^\s*$' | tail -3 | tr '\n' ' ')"
# Diagnose against the WHOLE stderr, not the tail. The bot-wall line comes from
# yt-dlp and is then buried by three ffmpeg "Invalid data" lines, so a tail-only
# match reported a generic failure for the one case we most need named.
ERRALL="$(tr -d '\r' < "$ERRF" | tr '\n' ' ')"

verdict=FAIL
if (( GOT >= floor )); then verdict=PASS; elif (( GOT > 0 )); then verdict=PARTIAL; fi

if (( JSON )); then
  printf '{"host":"%s","url":"%s","secs":%s,"want_bytes":%s,"got_bytes":%s,"verdict":"%s"}\n' \
    "$HOSTNAME_S" "$URL" "$SECS" "$want" "$GOT" "$verdict"
fi

case "$verdict" in
  PASS)
    echo "✅ PASS — decoded $GOT bytes of PCM. This host CAN capture this venue."
    exit 0 ;;
  PARTIAL)
    echo "⚠️  PARTIAL — only $GOT/$want bytes. The stream decodes but starved;"
    echo "   could be a slow start or a flaky edge. Re-probe before trusting it."
    [[ -n "$ERRTAIL" ]] && echo "   stderr: $ERRTAIL"
    exit 1 ;;
  FAIL)
    echo "⛔ FAIL — ZERO bytes decoded. This host CANNOT capture this venue." >&2
    [[ -n "$ERRTAIL" ]] && echo "   stderr: $ERRTAIL" >&2
    case "$ERRALL" in
      *"not a bot"*|*"Sign in to confirm"*)
        echo "   ⇒ BOT-WALLED at the segment layer (the HOOD 2026-07-29 class)." >&2
        echo "     The manifest may still return 200 — that means nothing here." >&2
        echo "     Options: cookies, residential egress, or capture on the laptop" >&2
        echo "     and LABEL it CDMX-vantage (never pool its Δrace with us-east)." >&2 ;;
      *403*|*401*)
        echo "   ⇒ token/session-bound (the Ford PATH A/B class): mint the URL where" >&2
        echo "     the session lives, or capture there." >&2 ;;
    esac
    exit 3 ;;
esac
