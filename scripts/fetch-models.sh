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
    Linux/x86_64)  dir="onnxruntime-linux-x64-$ver"
                   sha="25b1ef1fea1acd210d63f8f24dc870ad6e077795ce1f54876252c6d3803c15af" ;;  # pinned 2026-07-17, first Linux use (us-east-1 box)
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

# --- streaming_ctc models -----------------------------------------------------
# The streaming_ctc engine is model-agnostic (shapes read off the ONNX
# signature), so it needs a streaming zipformer2 CTC export (model.onnx +
# tokens.txt [+ bpe.model], decode_chunk_len in ONNX metadata).
#
# ENGLISH model (the M4 detector-latency unlock) does NOT exist off-the-shelf:
# the sherpa "online-ctc" release page lists only Chinese CTC exports; English
# streaming zipformers on the shelf are transducers with no CTC head. Obtaining
# one is an icefall export (a checkpoint trained with --use-ctc 1, exported via
# export-onnx-ctc). Pin its URL/sha here + in VERIFICATIONS.md once produced:
#   fetch "sherpa-onnx-streaming-zipformer-ctc-en-<date>" "<url>" "<sha256>"
#
# CHINESE STAND-IN (opt-in): exercises fbank + ort + cache + scorer identically
# to the English model — used by the streaming_ctc integration tests
# (EARSHOT_CTC_MODEL_DIR), which build keyword targets from the model's OWN
# token ids (from_targets), so English text is not required. Not fetched by
# default (large; English-only boxes don't need it). Prints the sha256 for
# pinning rather than trusting an unverified one.
fetch_ctc_standin() {
  local name="sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01"
  local url="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$name.tar.bz2"
  if [ -d "$name" ]; then echo "models/$name already present — skipping"; return; fi
  echo "fetching CTC stand-in $name …"
  curl -fL --retry 3 -o "$name.tar.bz2" "$url"
  echo "  sha256 (pin this in VERIFICATIONS.md): $(shasum -a 256 "$name.tar.bz2" | awk '{print $1}')"
  tar xjf "$name.tar.bz2"; rm -f "$name.tar.bz2"
  echo "unpacked models/$name"
  echo "  streaming_ctc tests:  EARSHOT_CTC_MODEL_DIR=\"$PWD/$name\" (with ORT_DYLIB_PATH set)"
}
[ "${FETCH_CTC_STANDIN:-0}" = "1" ] && fetch_ctc_standin

echo "done."
