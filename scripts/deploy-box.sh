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
#   scripts/deploy-box.sh --check         # offline self-test (stub ssh/rsync)
#
# After deploy, verifies the box still has a host-platform libonnxruntime and
# runs detector-smoke against the example config class actually armed next.
#
# Every ssh and rsync leg rides ONE multiplexed master per invocation (E13):
# ControlMaster=auto, socket under /tmp, ControlPersist=120, torn down on exit.
set -euo pipefail

# ── Offline self-test ────────────────────────────────────────────────────────
# Stub ssh/rsync log their argv; the real script is re-invoked against them.
# Assertions go through must/must_not: bash 3.2 (macOS) does not trip `set -e`
# on a bare `[[ ]]` (SYSTEM-STATE E9/E14).
if [[ "${1:-}" == "--check" ]]; then
  T=$(mktemp -d /tmp/deploy-box-check.XXXXXX)
  trap 'rm -rf -- "$T"' EXIT
  ARGV_LOG="$T/argv.log"
  mkdir -p "$T/bin"
  cat > "$T/bin/ssh" <<'SH'
#!/usr/bin/env bash
printf 'ssh %s\n' "$*" >> "$EARSHOT_CHECK_ARGV_LOG"
cat >/dev/null   # swallow any heredoc payload (provenance)
exit 0
SH
  cat > "$T/bin/rsync" <<'SH'
#!/usr/bin/env bash
printf 'rsync %s\n' "$*" >> "$EARSHOT_CHECK_ARGV_LOG"
exit 0
SH
  chmod +x "$T/bin/ssh" "$T/bin/rsync"
  must()     { "$@" || { echo "deploy-box --check: FAIL: expected to succeed: $*" >&2; exit 1; }; }
  must_not() { if "$@"; then echo "deploy-box --check: FAIL: expected to fail: $*" >&2; exit 1; fi; }
  run_stubbed() {  # run_stubbed <args...>
    EARSHOT_CHECK_ARGV_LOG="$ARGV_LOG" EARSHOT_SSH="$T/bin/ssh" EARSHOT_RSYNC="$T/bin/rsync" \
      EARSHOT_BOX=stub-box EARSHOT_BOX_KEY="$T/no-key" "$0" "$@" </dev/null >"$T/out.log" 2>&1
  }
  : > "$ARGV_LOG"
  must run_stubbed
  # Exactly three transport legs: the rsync, the ORT invariant ssh, the
  # provenance ssh — every one of them on the same multiplexed master.
  must test "$(grep -c '^ssh ' "$ARGV_LOG")" = 2
  must test "$(grep -c '^rsync ' "$ARGV_LOG")" = 1
  must test "$(grep -c '^ssh .*-o ControlMaster=auto .*-o ControlPath=/tmp/ear-deploy-mux\.[^/ ]*/mux .*-o ControlPersist=120 ' "$ARGV_LOG")" = 2
  must grep -q '^rsync .*-e [^ ]*ssh -i [^ ]* -o ControlMaster=auto -o ControlPath=/tmp/ear-deploy-mux\.[^/ ]*/mux -o ControlPersist=120 ' "$ARGV_LOG"
  # No teardown may reach a stub (a stub never made a socket) …
  must_not grep -q -- ' -O exit' "$ARGV_LOG"
  # … and the mux dir is gone after the run.
  for d in $(sed -n 's/.*ControlPath=\(\/tmp\/ear-deploy-mux\.[^/ ]*\)\/mux.*/\1/p' "$ARGV_LOG" | sort -u); do
    must_not test -e "$d"
  done
  must grep -q '^deploy OK$' "$T/out.log"
  # --check-only is the dry-run: one rsync leg, no ssh at all.
  : > "$ARGV_LOG"
  must run_stubbed --check-only
  must test "$(grep -c '^rsync ' "$ARGV_LOG")" = 1
  must grep -q '^rsync .*--dry-run' "$ARGV_LOG"
  must_not grep -q '^ssh ' "$ARGV_LOG"
  echo "deploy-box --check: PASS"
  exit 0
fi

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
SSH_BIN="${EARSHOT_SSH:-ssh}"
RSYNC_BIN="${EARSHOT_RSYNC:-rsync}"
SSH_OPTS=(-i "$KEY")
DRY=()
[[ "${1:-}" == "--check-only" ]] && DRY=(--dry-run -v)

# One multiplexed master per invocation (E13). Mirror of
# scripts/pull-box-state.sh start_ssh_mux; socket under /tmp, never TMPDIR.
SSH_MUX_DIR=""
start_ssh_mux() {
  SSH_MUX_DIR=$(mktemp -d /tmp/ear-deploy-mux.XXXXXX) || return 1
  SSH_OPTS+=(-o ControlMaster=auto -o "ControlPath=$SSH_MUX_DIR/mux" -o ControlPersist=120)
}
stop_ssh_mux() {
  [[ -n "$SSH_MUX_DIR" ]] || return 0
  if [[ -S "$SSH_MUX_DIR/mux" ]]; then   # a stub or a pre-SSH failure leaves no socket
    "$SSH_BIN" "${SSH_OPTS[@]}" -O exit "$BOX" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$SSH_MUX_DIR"; SSH_MUX_DIR=""
}
start_ssh_mux || { echo "deploy REFUSED: could not allocate the SSH multiplex directory under /tmp" >&2; exit 1; }
trap stop_ssh_mux EXIT

cd "$(dirname "$0")/.."

SOURCE_COMMIT="$(git rev-parse HEAD 2>/dev/null || echo UNKNOWN-NOT-A-GIT-CHECKOUT)"
SOURCE_DIRTY="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
DEPLOYED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

"$RSYNC_BIN" -az --delete ${DRY[@]+"${DRY[@]}"} \
  --exclude target --exclude models --exclude .git \
  --exclude state --exclude corpus --exclude vendor \
  --exclude /var/runs/ --exclude /var/host-boundary-staging/ \
  -e "$SSH_BIN ${SSH_OPTS[*]}" ./ "$BOX":~/ear/

[[ "${1:-}" == "--check-only" ]] && exit 0

# Post-deploy invariant: the box must still hold a Linux ORT. This is the
# check that would have caught both incidents at deploy time.
"$SSH_BIN" "${SSH_OPTS[@]}" "$BOX" '
  set -e
  SO=$(find ~/ear/vendor -name "libonnxruntime*.so*" 2>/dev/null | head -1)
  MACH=$(find ~/ear/vendor -name "*.dylib" 2>/dev/null | head -1)
  [[ -n "$MACH" ]] && { echo "DEPLOY BROKEN: macOS dylib on the Linux box: $MACH" >&2; exit 1; }
  [[ -n "$SO" ]]   || { echo "DEPLOY BROKEN: no linux libonnxruntime under ~/ear/vendor" >&2; exit 1; }
  echo "vendor OK: $SO"
'

# The box is intentionally not a git checkout. Publish the source identity only
# after transfer and platform checks succeed, atomically, so on-box preflight
# can bind its freshly built pin to the real rsync source.
{
  printf 'commit=%s\n' "$SOURCE_COMMIT"
  printf 'dirty_paths=%s\n' "$SOURCE_DIRTY"
  printf 'deployed_utc=%s\n' "$DEPLOYED_UTC"
} | "$SSH_BIN" "${SSH_OPTS[@]}" "$BOX" '
  set -e
  umask 022
  tmp=~/ear/.DEPLOYED-FROM.tmp.$$
  cat > "$tmp"
  chmod 444 "$tmp"
  mv -f "$tmp" ~/ear/DEPLOYED-FROM
'
echo "provenance OK: DEPLOYED-FROM commit ${SOURCE_COMMIT:0:12} dirty_paths=$SOURCE_DIRTY deployed_utc=$DEPLOYED_UTC"
echo "deploy OK"
