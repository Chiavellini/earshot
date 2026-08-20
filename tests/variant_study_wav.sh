#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/variant-study-wav.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# A container/header with no sample payload must never count as a spoken clip.
ffmpeg -nostdin -v error -y -f lavfi -i anullsrc=r=16000:cl=mono \
  -t 0 -c:a pcm_s16le "$TMP/header-only.wav"
if "$REPO/scripts/variant-study.sh" --check-wav "$TMP/header-only.wav"; then
  echo "FAIL: header-only WAV passed the PCM gate" >&2
  exit 1
fi

ffmpeg -nostdin -v error -y -f lavfi -i anullsrc=r=16000:cl=mono \
  -t 0.2 -c:a pcm_s16le "$TMP/real.wav"
"$REPO/scripts/variant-study.sh" --check-wav "$TMP/real.wav"

# Wrong sample rate is payload, but not the exact format replay consumes.
ffmpeg -nostdin -v error -y -f lavfi -i anullsrc=r=8000:cl=mono \
  -t 0.2 -c:a pcm_s16le "$TMP/wrong-rate.wav"
if "$REPO/scripts/variant-study.sh" --check-wav "$TMP/wrong-rate.wav"; then
  echo "FAIL: wrong-rate WAV passed the PCM gate" >&2
  exit 1
fi

echo "variant-study WAV gate: PASS"
