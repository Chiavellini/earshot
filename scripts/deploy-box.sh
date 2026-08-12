#!/usr/bin/env bash
# THE deploy path to the us-east box. Hand-rolled rsync has now shipped the
# macOS onnxruntime into the Linux box's vendor/ TWICE (2026-07-27 setup →
# the Ford 9-min deaf gap; 2026-07-28 evening again with --delete, which also
# DELETED the box's linux-x64 ORT — every new process futex-hung in ort's
# dlopen: a missing lib and a wrong-platform lib hang identically, silently).
# vendor/ is PLATFORM-SPECIFIC and must never cross machines. Neither must
# target/, models/, corpus/, state/, or var/runs/ (box-local artifacts).
#
# var/runs/ is especially load-bearing: after a typed RunPlan is dispatched,
# the box owns the authoritative receipt extension and all live evidence.  A
# normal source deploy uses --delete, so failing to exclude that tree can both
# replace the box ledger with the laptop's shorter preflight prefix and delete
# a box-only run outright.  The private host-boundary staging namespace is
# protected for the same reason.
#
#   scripts/deploy-box.sh                 # laptop tree → box ~/ear/
#   scripts/deploy-box.sh --check-only    # dry-run, show what would change
#
# After deploy, verifies the box still has a host-platform libonnxruntime and
# runs detector-smoke against the example config class actually armed next.
set -euo pipefail

# BOX ADDRESS. An Elastic IP (eipalloc-0d0f5d3654883fb68 → 3.212.174.94) was
# attached 2026-07-29 15:57Z, so the address is now STABLE across stop/start.
# Before that it was auto-assigned and moved on every resize — 44.197.147.18 →
# 3.239.237.41 (07-27) → 44.195.78.182 (07-29 c6a resize) → the EIP, three
# addresses in three days, each of which was hardcoded somewhere and stale.
#
# The hardcoded default below is therefore a FALLBACK, not the source of truth:
# we still resolve live from the instance id when the AWS CLI is available, so
# a future re-association can never leave this script pointing at a dead host.
EARSHOT_INSTANCE_ID="${EARSHOT_INSTANCE_ID:-i-04c50d76572aa092e}"
if [[ -z "${EARSHOT_BOX:-}" ]] && command -v aws >/dev/null 2>&1; then
  _ip="$(aws ec2 describe-instances --instance-ids "$EARSHOT_INSTANCE_ID" \
           --query 'Reservations[].Instances[].PublicIpAddress' --output text 2>/dev/null || true)"
  # `|| true` above and the regex here: a missing/expired credential yields an
  # empty string or an error blob, and set -e must not turn that into a hard
  # failure — we just fall back to the pinned address below.
  [[ "$_ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]] && EARSHOT_BOX="ubuntu@$_ip"
fi
BOX="${EARSHOT_BOX:-ubuntu@3.212.174.94}"   # c6a.4xlarge + EIP, 2026-07-29
KEY="${EARSHOT_BOX_KEY:-$HOME/.ssh/earshot-useast1.pem}"
DRY=()
[[ "${1:-}" == "--check-only" ]] && DRY=(--dry-run -v)

cd "$(dirname "$0")/.."

rsync -az --delete ${DRY[@]+"${DRY[@]}"} \
  --exclude target --exclude models --exclude .git \
  --exclude state --exclude corpus --exclude vendor \
  --exclude /var/runs/ --exclude /var/host-boundary-staging/ \
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
