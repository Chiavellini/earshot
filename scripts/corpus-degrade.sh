#!/usr/bin/env bash
# Time-compress the telephony corpus to simulate fast / "speed-talking" speech.
# ffmpeg `atempo` changes TEMPO without pitch shift (the closest cheap proxy for
# a fast talker); the 300-3400 Hz band-limit is inherited from the source
# telephony WAV. Produces a self-contained corpus root per tempo:
#   corpus-t<TAG>/telephony/<id>.wav      (compressed audio, 8 kHz s16 mono)
#   corpus-t<TAG>/manifests/<id>.toml     (onsets rescaled by 1/T)
# so `bench-detector --corpus corpus-t<TAG>/` measures recall-vs-speed cleanly,
# one tempo per run (keeps per-speed numbers unmixed).
#
# Usage: scripts/corpus-degrade.sh [tempo ...]   (default: 1.25 1.5)
set -euo pipefail
cd "$(dirname "$0")/.."

tempos=("$@")
[ ${#tempos[@]} -eq 0 ] && tempos=(1.25 1.5)

for T in "${tempos[@]}"; do
  tag="t${T//./}"                       # 1.25 -> t125, 1.5 -> t15
  root="corpus-${tag}"
  mkdir -p "$root/telephony" "$root/manifests"
  for src in corpus/telephony/*.wav; do
    id="$(basename "${src%.wav}")"
    ffmpeg -y -loglevel error -i "$src" -af "atempo=${T}" -ar 8000 -acodec pcm_s16le \
      "$root/telephony/${id}.wav"
  done
  for m in corpus/manifests/*.toml; do
    python3 scripts/manifest-rescale.py "$m" "$T" "$root/manifests/$(basename "$m")"
  done
  echo "wrote $root (tempo ${T}x, $(ls "$root"/telephony/*.wav | wc -l | tr -d ' ') calls)"
done
