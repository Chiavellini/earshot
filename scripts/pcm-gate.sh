#!/usr/bin/env bash
# pcm-gate.sh — prove a webcast source actually yields DECODABLE AUDIO on THIS host.
#
# WHY THIS EXISTS: every readiness gate we had for the webcast lane checks the
# manifest (HTTP 200 + #EXTM3U). That check is not merely weak, it is
# AFFIRMATIVELY MISLEADING on at least one venue class. HOOD 2026-07-29, the
# first YouTube event we ever armed: the master manifest resolved and returned
# 200 from us-east-1, the arm started clean, and BOTH legs died inside 35 s with
# frames=0 — YouTube bot-walls the datacenter IP at the *segment* layer, so the
# manifest is a decoy that 200s while the media underneath is refused.
# scripts/youtube-resolve.sh:26-31 has said in a comment since that day that the
# only gate which can clear this venue is PCM BYTES. Nothing implemented it.
# This does. Run it per host × per event BEFORE arming.
#
#   scripts/pcm-gate.sh --kind youtube --url https://www.youtube.com/live/<id>
#   scripts/pcm-gate.sh --kind hls     --url-file $HOME/snap-stream.url
#   scripts/pcm-gate.sh --kind hls     --url "https://…/master.m3u8" --secs 8
#
# Contract: 16 kHz mono s16le ⇒ 32000 bytes/s. --secs 6 ⇒ 192000 bytes expected.
# PASS at >= 80% of expected (allows one short read at the join point).
# Exit 0 PASS · 1 FAIL (short/zero bytes) · 2 usage · 3 source unreachable.
#
# A FAIL means THIS VANTAGE IS DEAD for this event. Fall back to another host —
# do not arm hopefully. frames=0 at 35 s is the HOOD signature.
set -uo pipefail

KIND=""; URL=""; URLFILE=""; SECS=6; LABEL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kind)     KIND="${2:?}"; shift 2 ;;
    --url)      URL="${2:?}"; shift 2 ;;
    --url-file) URLFILE="${2:?}"; shift 2 ;;
    --secs)     SECS="${2:?}"; shift 2 ;;
    --label)    LABEL="${2:?}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$KIND" ]] || { echo "usage: pcm-gate.sh --kind youtube|hls (--url U | --url-file F) [--secs N] [--label L]" >&2; exit 2; }
[[ "$KIND" == "youtube" || "$KIND" == "hls" ]] || { echo "--kind must be youtube or hls" >&2; exit 2; }

if [[ -n "$URLFILE" ]]; then
  [[ -s "$URLFILE" ]] || { echo "PCM-GATE FAIL: url-file missing or empty: $URLFILE" >&2; exit 3; }
  URL="$(head -n1 "$URLFILE")"
fi
[[ -n "$URL" ]] || { echo "PCM-GATE FAIL: no URL (pass --url or --url-file)" >&2; exit 2; }

LABEL="${LABEL:-$KIND}"
EXPECT=$(( SECS * 32000 ))
FLOOR=$(( EXPECT * 80 / 100 ))
ERR="$(mktemp -t pcmgate.XXXXXX)"
trap 'rm -f "$ERR"' EXIT

echo "=== pcm-gate $(date -u +%FT%TZ) label=$LABEL kind=$KIND secs=$SECS ==="
echo "host   : $(hostname -s)  ip-egress-class=$( [[ -f /sys/hypervisor/uuid ]] && echo datacenter || echo local )"
echo "url    : $(printf '%s' "$URL" | cut -c1-120)"
echo "expect : $EXPECT bytes (floor $FLOOR)"

# yt-dlp owns YouTube's segment addressing; ffmpeg alone cannot fetch those
# segments even when handed a valid manifest (youtube-resolve.sh:32-34).
if [[ "$KIND" == "youtube" ]]; then
  command -v yt-dlp >/dev/null || { echo "PCM-GATE FAIL: yt-dlp not installed" >&2; exit 3; }
  BYTES=$(yt-dlp -f bestaudio --quiet --no-warnings -o - "$URL" 2>"$ERR" \
          | ffmpeg -loglevel error -i - -vn -f s16le -ac 1 -ar 16000 -t "$SECS" - 2>>"$ERR" \
          | wc -c | tr -d ' ')
else
  BYTES=$(ffmpeg -loglevel error -fflags nobuffer -flags low_delay \
            -probesize 200000 -analyzeduration 3000000 -live_start_index -1 \
            -i "$URL" -vn -f s16le -ac 1 -ar 16000 -t "$SECS" - 2>"$ERR" \
          | wc -c | tr -d ' ')
fi
BYTES="${BYTES:-0}"

echo "bytes  : $BYTES"
if [[ "$BYTES" -ge "$FLOOR" ]]; then
  echo "PCM-GATE PASS: $LABEL yielded $BYTES/$EXPECT bytes of decodable audio on this host"
  exit 0
fi

echo "PCM-GATE FAIL: $LABEL yielded $BYTES/$EXPECT bytes (floor $FLOOR) — THIS VANTAGE IS DEAD" >&2
echo "--- stderr tail ---" >&2
tail -n 15 "$ERR" >&2
# The bot-wall has a distinctive signature; name it so nobody re-diagnoses it.
if grep -qi "confirm you're not a bot\|Sign in to confirm\|HTTP Error 403\|429" "$ERR"; then
  echo ">>> BOT-WALL SIGNATURE DETECTED. This is the HOOD 2026-07-29 failure:" >&2
  echo ">>> the manifest is reachable but segments are refused for this egress IP." >&2
  echo ">>> Capture from a residential vantage, or supply cookies. Do NOT arm here." >&2
fi
exit 1
