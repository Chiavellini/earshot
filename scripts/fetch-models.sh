#!/usr/bin/env bash
# Fetch the M1 detector models into the gitignored models/ directory.
# Idempotent: skips anything already unpacked with a good checksum.
#
# Model facts (URL, sha256, chunk config) are pinned in VERIFICATIONS.md —
# update both together.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p models
cd models

fetch() {
  local name="$1" url="$2" sha="$3"
  local tar="${name}.tar.bz2"
  if [ -d "$name" ]; then
    echo "models/$name already present — skipping"
    return
  fi
  echo "fetching $name …"
  curl -fL --retry 3 -o "$tar" "$url"
  echo "$sha  $tar" | shasum -a 256 -c -
  tar xjf "$tar"
  rm -f "$tar"
  echo "unpacked models/$name"
}

# sherpa-onnx streaming KWS: Zipformer transducer (chunk-16-left-64),
# GigaSpeech English, BPE-500 vocab, 16 kHz. fp32 + int8 in one tarball.
fetch \
  "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01" \
  "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2" \
  "f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a"

echo "done."
