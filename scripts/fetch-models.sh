#!/usr/bin/env bash
# Fetch the M1 detector models into the gitignored models/ directory.
# Idempotent: skips anything already unpacked with a good checksum.
#
# Model facts (URL, sha256, chunk config) are pinned in VERIFICATIONS.md —
# update both together.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# --- onnxruntime dylib for the streaming_ctc engine (ort load-dynamic) ---------
# ort dlopens onnxruntime at runtime (it must NOT static-link it — sherpa-onnx
# already static-links onnxruntime 1.24.2 and two copies clash; VERIFICATIONS.md
# "streaming_ctc engine pins"). Fetch the matching dylib into vendor/ (gitignored)
# and print the ORT_DYLIB_PATH export the streaming_ctc engine needs.
fetch_onnxruntime_dylib() {
  local os arch ver="1.27.1" dir tgz sha
  os="$(uname -s)"; arch="$(uname -m)"
  case "$os/$arch" in
    Darwin/arm64)  dir="onnxruntime-osx-arm64-$ver"
                   sha="e42b77a7281cc6e55141bf44fcfbac2c782b823a491bbb6ac33c781dd991f8a6" ;;
    Linux/x86_64)  dir="onnxruntime-linux-x64-$ver"; sha="" ;;   # TODO: pin sha on first Linux/CI use
    Linux/aarch64) dir="onnxruntime-linux-aarch64-$ver"; sha="" ;;
    *) echo "unknown platform $os/$arch — download an onnxruntime $ver dylib manually" >&2; return 1 ;;
  esac
  mkdir -p "$REPO_ROOT/vendor/onnxruntime"
  cd "$REPO_ROOT/vendor/onnxruntime"
  if [ ! -d "$dir" ]; then
    tgz="$dir.tgz"
    echo "fetching onnxruntime dylib $dir …"
    curl -fL --retry 3 -o "$tgz" "https://github.com/microsoft/onnxruntime/releases/download/v$ver/$dir.tgz"
    [ -n "$sha" ] && echo "$sha  $tgz" | shasum -a 256 -c -
    tar xzf "$tgz"; rm -f "$tgz"
    echo "unpacked vendor/onnxruntime/$dir"
  else
    echo "vendor/onnxruntime/$dir already present — skipping"
  fi
  local dylib
  dylib="$(find "$PWD/$dir/lib" -maxdepth 1 -name 'libonnxruntime.*.dylib' -o -name 'libonnxruntime.so.*' 2>/dev/null | head -1)"
  echo ""
  echo "  streaming_ctc engine needs:  export ORT_DYLIB_PATH=\"$dylib\""
  echo ""
}
fetch_onnxruntime_dylib

cd "$REPO_ROOT"
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
