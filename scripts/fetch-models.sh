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
  dylib="$(find "$PWD/$dir/lib" -maxdepth 1 \( -name 'libonnxruntime.*.dylib' -o -name 'libonnxruntime.so.*' \) 2>/dev/null | head -1)"
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
# ENGLISH model (the M4 detector-latency unlock) — FOUND 2026-07-17 (Tier-0
# hunt, docs/CTC-MODEL-SOURCING.md): csukuangfj's LibriSpeech streaming
# zipformer SMALL (23.8 M), trained `--causal 1 --use-ctc 1` (evidence: the
# HF repo's run-streaming-zipformer-ctc.sh + training log; icefall PR #1557),
# already exported in the streaming-CTC ONNX format (fp32 + int8, tokens.txt,
# bpe.model). Caveat: the CTC head was an auxiliary loss (scale 0.2) with no
# published WER — corpus bake-off is the quality gate (docs/BAKEOFF.md).
# The tarball also ships a 350 MB HLG.fst (unused by earshot — FST decoding
# only; safe to delete after unpack).
fetch \
  "sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18" \
  "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18.tar.bz2" \
  "cd8ed7371b1a0c49e919549a606235c8092f9006425aa06a9e8e6395054126bd"
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

# --- online_asr candidates + the fill-path verifier (detection-accuracy program, 2026-08-18) ---
# `online_asr` engine: any sherpa streaming transducer/CTC. First candidate
# benched (D1, docs/BAKEOFF.md): LibriSpeech zipformer-en 2023-06-26 — FAILED
# the bars vs the armed CTC; kept as the engine's integration-test model.
fetch \
  "sherpa-onnx-streaming-zipformer-en-2023-06-26" \
  "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2" \
  "639e25b578e9e997131402199419c13a941f8e4e198e2da1ce57dbf5cf401282"
# Verifier (D2): OFFLINE Parakeet-TDT-0.6b-v2 int8 — the unbiased second decode
# of the last 3.5 s before a fill; p95 < 400 ms on the c6a box (4 threads).
fetch \
  "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8" \
  "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2" \
  "157c157bc51155e03e37d2466522a3a737dd9c72bb25f36eb18912964161e1ad"


echo "done."
