#!/usr/bin/env bash
# Source (or eval) this to run the model-gated streaming_ctc tests, which
# silently SKIP without EARSHOT_CTC_MODEL_DIR + ORT_DYLIB_PATH:
#
#   source scripts/ctc-test-env.sh && EARSHOT_REQUIRE_MODELS=1 cargo test
#
# Locates the English streaming-CTC model under models/ and the onnxruntime
# dylib under vendor/ relative to the repo root, following symlinks (worktrees
# symlink models/ and vendor/ back to the main checkout). Read-only; prints
# what it exports. Both trees come from scripts/fetch-models.sh.

_ctc_root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"

# find -L: models/ and vendor/ may themselves be symlinks in a worktree.
_ctc_model="$(find -L "$_ctc_root/models" -maxdepth 1 -type d \
  -name '*streaming-zipformer-ctc*' 2>/dev/null | head -1)"
if [ -n "$_ctc_model" ]; then
  export EARSHOT_CTC_MODEL_DIR="$_ctc_model"
  echo "EARSHOT_CTC_MODEL_DIR=$EARSHOT_CTC_MODEL_DIR"
else
  echo "WARN: no models/*streaming-zipformer-ctc* dir — run scripts/fetch-models.sh" >&2
fi

_ctc_dylib="$(find -L "$_ctc_root/vendor/onnxruntime" \
  \( -name 'libonnxruntime.[0-9]*.dylib' -o -name 'libonnxruntime.so.[0-9]*' \) \
  -type f 2>/dev/null | sort | head -1)"
if [ -n "$_ctc_dylib" ]; then
  export ORT_DYLIB_PATH="$_ctc_dylib"
  echo "ORT_DYLIB_PATH=$ORT_DYLIB_PATH"
else
  echo "WARN: no vendor/onnxruntime dylib — run scripts/fetch-models.sh" >&2
fi

unset _ctc_root _ctc_model _ctc_dylib
