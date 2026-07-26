#!/usr/bin/env bash
# corpus/raw/<id>.* → corpus/telephony/<id>.wav (8 kHz mono s16, telephony-
# degraded). The band-limit + G.711 µ-law round trip matches the M3 SIP
# dial-in path — engines are never benchmarked on clean speech
# (docs/BAKEOFF.md).
#
# --wideband: corpus/raw/<id>.* → corpus/wideband/<id>.wav (16 kHz mono s16,
# NO band-limit). This IS the channel the webcast HLS legs deliver
# (resample_to_hz = 16000), so applying the telephony degradation to it would
# benchmark the engine on a channel it never trades (docs/BAKEOFF.md §Corpus).
# Bench each engine on the channel it trades on.
#
# Usage: scripts/corpus-process.sh [--wideband] corpus/raw/aapl-q2-2026.m4a [more…]
set -euo pipefail

cd "$(dirname "$0")/.."

MODE=telephony
if [[ "${1:-}" == "--wideband" ]]; then MODE=wideband; shift; fi
[[ $# -gt 0 ]] || { echo "usage: $0 [--wideband] <raw-audio> [more…]" >&2; exit 2; }
mkdir -p "corpus/${MODE}"

if [[ "$MODE" == wideband ]]; then
  for src in "$@"; do
    id="$(basename "${src%.*}")"
    out="corpus/wideband/${id}.wav"
    ffmpeg -y -loglevel error -i "$src" -ac 1 -ar 16000 -acodec pcm_s16le "$out"
    dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$out" 2>/dev/null || echo "?")
    echo "wrote $out (${dur}s, 16 kHz mono s16, unbanded)"
  done
  exit 0
fi

for src in "$@"; do
  id="$(basename "${src%.*}")"
  out="corpus/telephony/${id}.wav"
  tmp="corpus/telephony/.${id}.ulaw.wav"
  # 1) mono, telephony band-limit, 8 kHz
  # 2) G.711 µ-law round trip (companding loss is part of the channel)
  ffmpeg -y -loglevel error -i "$src" \
    -ac 1 -af "highpass=f=300,lowpass=f=3400" -ar 8000 -acodec pcm_mulaw "$tmp"
  ffmpeg -y -loglevel error -i "$tmp" -acodec pcm_s16le "$out"
  rm -f "$tmp"
  echo "wrote $out ($(afinfo "$out" 2>/dev/null | awk '/estimated duration/{printf "%.0fs", $3}'))"
done
