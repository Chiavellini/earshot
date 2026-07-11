#!/usr/bin/env bash
# corpus/raw/<id>.* → corpus/telephony/<id>.wav (8 kHz mono s16, telephony-
# degraded). The band-limit + G.711 µ-law round trip matches the M3 SIP
# dial-in path — engines are never benchmarked on clean speech
# (docs/BAKEOFF.md).
#
# Usage: scripts/corpus-process.sh corpus/raw/aapl-q2-2026.m4a [more files…]
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p corpus/telephony

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
