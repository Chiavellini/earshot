#!/usr/bin/env bash
# THE deploy path to the us-east box. Hand-rolled rsync has now shipped the
# macOS onnxruntime into the Linux box's vendor/ TWICE (2026-07-27 setup →
# the Ford 9-min deaf gap; 2026-07-28 evening again with --delete, which also
# DELETED the box's linux-x64 ORT — every new process futex-hung in ort's
# dlopen: a missing lib and a wrong-platform lib hang identically, silently).
# vendor/ is PLATFORM-SPECIFIC and must never cross machines. Neither must
# target/, models/, corpus/, state/ (box-local artifacts).
#
#   scripts/deploy-box.sh                 # laptop tree → box ~/ear/
#   scripts/deploy-box.sh --check-only    # dry-run, show what would change
#
# After deploy, verifies the box still has a host-platform libonnxruntime and
# runs detector-smoke against the example config class actually armed next.
set -euo pipefail

BOX="${EARSHOT_BOX:-ubuntu@3.239.237.41}"
KEY="${EARSHOT_BOX_KEY:-$HOME/.ssh/earshot-useast1.pem}"
DRY=()
[[ "${1:-}" == "--check-only" ]] && DRY=(--dry-run -v)

cd "$(dirname "$0")/.."

rsync -az --delete ${DRY[@]+"${DRY[@]}"} \
  --exclude target --exclude models --exclude .git \
  --exclude state --exclude corpus --exclude vendor \
  -e "ssh -i $KEY" ./ "$BOX":~/ear/

[[ "${1:-}" == "--check-only" ]] && exit 0

# Post-deploy invariant: the box must still hold a Linux ORT. This is the
# check that would have caught both incidents at deploy time.
ssh -i "$KEY" "$BOX" '
  set -e
  SO=$(find ~/ear/vendor -name "libonnxruntime*.so*" 2>/dev/null | head -1)
  MACH=$(find ~/ear/vendor -name "*.dylib" 2>/dev/null | head -1)
  [[ -n "$MACH" ]] && { echo "DEPLOY BROKEN: macOS dylib on the Linux box: $MACH" >&2; exit 1; }
  [[ -n "$SO" ]]   || { echo "DEPLOY BROKEN: no linux libonnxruntime under ~/ear/vendor" >&2; exit 1; }
  echo "vendor OK: $SO"
'
echo "deploy OK"
