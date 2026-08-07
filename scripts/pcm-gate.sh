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
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

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
if [[ "$BYTES" -lt "$FLOOR" ]]; then
  echo "PCM-GATE FAIL: $LABEL yielded $BYTES/$EXPECT bytes (floor $FLOOR) — THIS VANTAGE IS DEAD" >&2
  echo "--- stderr tail ---" >&2
  tail -n 15 "$ERR" >&2
  if grep -qi "confirm you're not a bot\|Sign in to confirm\|HTTP Error 403\|429" "$ERR"; then
    echo ">>> BOT-WALL SIGNATURE DETECTED. This is the HOOD 2026-07-29 failure:" >&2
    echo ">>> the manifest is reachable but segments are refused for this egress IP." >&2
    echo ">>> Capture from a residential vantage, or supply cookies. Do NOT arm here." >&2
  fi
  exit 1
fi

# ── continuity ──────────────────────────────────────────────────────────────
# Bytes prove the URL yielded audio ONCE. They do not prove it will sustain,
# and that distinction cost AAOI-26AUG06: this gate passed twice on 541 KB of
# genuinely decoded audio, and the arm behind it died 32 s later, twice. One
# contiguous read cannot tell a live stream from a stalled one that still has
# a buffer to hand over.
#
# So also require the playlist to be MOVING: poll #EXT-X-MEDIA-SEQUENCE and
# demand it advance. scripts/pdt-log.py already does exactly this (resolves
# master -> variant, emits media_sequence + a computed new_segment per poll),
# so this is a reuse, not a second manifest parser.
#
# HLS only: yt-dlp owns YouTube's segment addressing and there is no playlist
# here to poll. Skipped rather than faked.
if [[ "$KIND" == "youtube" ]]; then
  echo "PCM-GATE PASS: $LABEL yielded $BYTES/$EXPECT bytes of decodable audio on this host"
  echo "  (continuity check skipped — youtube has no pollable media playlist)"
  exit 0
fi

SEQ_SECS="${PCM_GATE_SEQ_SECS:-12}"
SEQ_JSON=$(python3 "$REPO/scripts/pdt-log.py" "$URL" 1000 "$SEQ_SECS" 2>"$ERR" || true)
SEQS=$(printf '%s\n' "$SEQ_JSON" | sed -n 's/.*"media_sequence": *\([0-9]*\).*/\1/p' | sort -un | wc -l | tr -d ' ')
POLLS=$(printf '%s\n' "$SEQ_JSON" | grep -c media_sequence || true)

echo "seq    : ${SEQS:-0} distinct MEDIA-SEQUENCE value(s) across ${POLLS:-0} poll(s) in ${SEQ_SECS}s"
if [[ "${SEQS:-0}" -ge 2 ]]; then
  echo "PCM-GATE PASS: $LABEL decoded $BYTES/$EXPECT bytes AND its playlist is advancing"
  exit 0
fi

if [[ "${POLLS:-0}" -eq 0 ]]; then
  # Could not read a playlist at all. Do NOT fail the arm on this: a venue
  # whose manifest resists polling but whose audio decodes is still armable,
  # and refusing here would trade a real miss for a hypothetical one.
  echo "PCM-GATE PASS: $LABEL yielded $BYTES/$EXPECT bytes of decodable audio on this host"
  echo "  WARN: could not poll MEDIA-SEQUENCE (continuity UNVERIFIED) — stderr tail:" >&2
  tail -n 5 "$ERR" >&2
  exit 0
fi

echo "PCM-GATE FAIL: $LABEL decoded $BYTES bytes but MEDIA-SEQUENCE never advanced" >&2
echo ">>> over ${SEQ_SECS}s / ${POLLS} polls the playlist stood still. This is the" >&2
echo ">>> AAOI-26AUG06 shape: audio decodes from a buffer while the stream behind" >&2
echo ">>> it is already dead, so the arm fires and loses every leg seconds later." >&2
exit 1
