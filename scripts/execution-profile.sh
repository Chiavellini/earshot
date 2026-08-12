#!/usr/bin/env bash
# Provision or resolve the stable, non-secret execution identity carried by an
# external per-account env file.  No network call is made: the pinned earshot
# binary loads the same local signer `arm` would select and prints its public
# fingerprint.
#
# One-time provisioning (quote the portable locator so this shell does not
# expand it before the script can validate it):
#   scripts/execution-profile.sh configure \
#     --account '$HOME/.earshot/ms.env' \
#     --host-id earshot-useast1 \
#     --bin target/release/earshot
#
# Plan-time resolution/check:
#   scripts/execution-profile.sh resolve \
#     --account '$HOME/.earshot/ms.env' \
#     --config configs/event.toml \
#     --bin target/release/earshot
#
# `configure` is the only writing command and edits only the named external
# account env. `resolve` prints one JSON object and writes nothing.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd -P)
REPO=$(cd "$SCRIPT_DIR/.." && pwd -P)

usage() {
  cat >&2 <<'EOF'
usage:
  execution-profile.sh configure --account <env> --host-id <stable-target-id> [--config <cfg>] [--bin <earshot>]
  execution-profile.sh resolve   --account <env> [--config <cfg>] [--bin <earshot>]
EOF
  exit 2
}

die() {
  echo "REFUSED: $*" >&2
  exit 1
}

[[ $# -gt 0 ]] || usage
COMMAND=$1
shift
ACCOUNT_REF=""
HOST_ID=""
CONFIG=""
BIN="${EARSHOT_BIN:-$REPO/target/release/earshot}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) ACCOUNT_REF=${2:-}; shift 2 ;;
    --host-id) HOST_ID=${2:-}; shift 2 ;;
    --config) CONFIG=${2:-}; shift 2 ;;
    --bin) BIN=${2:-}; shift 2 ;;
    -h|--help) usage ;;
    *) die "unknown argument $1" ;;
  esac
done
[[ "$COMMAND" == configure || "$COMMAND" == resolve ]] || usage
[[ -n "$ACCOUNT_REF" ]] || die "--account is required"
[[ "$COMMAND" != configure || -n "$HOST_ID" ]] || die "configure requires --host-id"
[[ "$COMMAND" != resolve || -z "$HOST_ID" ]] || die "resolve does not accept --host-id"
[[ -z "$CONFIG" || -f "$CONFIG" ]] || die "config not found: $CONFIG"
[[ -x "$BIN" ]] || die "pinned earshot binary is not executable: $BIN"

# Expand only the portable spellings event envs are allowed to carry.  Do not
# use eval: ACCOUNT_REF is data, never shell input.
case "$ACCOUNT_REF" in
  '$HOME/'*) ACCOUNT_PATH="$HOME/${ACCOUNT_REF#\$HOME/}" ;;
  '${HOME}/'*) ACCOUNT_PATH="$HOME/${ACCOUNT_REF#\$\{HOME\}/}" ;;
  '~/'*) ACCOUNT_PATH="$HOME/${ACCOUNT_REF#\~/}" ;;
  /*) ACCOUNT_PATH="$ACCOUNT_REF" ;;
  *) die "--account must be absolute or start with literal \$HOME/, \${HOME}/, or ~/" ;;
esac
[[ -f "$ACCOUNT_PATH" && ! -L "$ACCOUNT_PATH" ]] || {
  die "account env must be a regular non-symlink file: $ACCOUNT_PATH"
}

# Credential env files contain private paths/tokens.  Profile provisioning must
# not bless a file other users can read, and atomic replacement preserves this
# exact mode.
MODE=$(python3 - "$ACCOUNT_PATH" <<'PY'
import os, stat, sys
mode = stat.S_IMODE(os.stat(sys.argv[1], follow_symlinks=False).st_mode)
print(f"{mode:03o}")
PY
)
(( (8#$MODE & 077) == 0 )) || die "account env permissions must not grant group/other access (mode $MODE)"

fingerprint_in_profile_env() {
  # Account envs are already the launchers' credential boundary. Source only
  # this explicit file, with xtrace disabled and all source output suppressed;
  # never source the generated per-event env during plan compilation.
  (
    set +x
    set +u
    set -a
    if ! source "$ACCOUNT_PATH" >/dev/null 2>/dev/null; then
      die "could not load account env"
    fi
    set +a
    args=("$BIN" account-fingerprint)
    [[ -z "$CONFIG" ]] || args+=(--config "$CONFIG")
    "${args[@]}"
  )
}

validate_fingerprint() {
  local value=$1
  [[ "$value" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    die "earshot account-fingerprint did not emit exactly sha256:<64 lowercase hex>"
  }
}

if [[ "$COMMAND" == configure ]]; then
  [[ "$HOST_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || {
    die "--host-id must be an explicit stable ID (1-64 letters, digits, dot, underscore, hyphen)"
  }
  # A shared account profile describes the ambient account credential.  When a
  # representative config is supplied, prove its arm-time selector resolves to
  # that same key; otherwise one profile would lie for one of its consumers.
  SELECTED=$(fingerprint_in_profile_env) || die "could not fingerprint selected credential"
  validate_fingerprint "$SELECTED"
  if [[ -n "$CONFIG" ]]; then
    SAVED_CONFIG=$CONFIG
    CONFIG=""
    AMBIENT=$(fingerprint_in_profile_env) || die "could not fingerprint ambient credential"
    CONFIG=$SAVED_CONFIG
    validate_fingerprint "$AMBIENT"
    [[ "$SELECTED" == "$AMBIENT" ]] || {
      die "config selects a different credential than this shared account env"
    }
  fi

  python3 - "$ACCOUNT_PATH" "$HOST_ID" "$SELECTED" <<'PY'
import os
import stat
import sys
import tempfile

path, host_id, fingerprint = sys.argv[1:]
begin = "# EARSHOT EXECUTION PROFILE BEGIN (managed by scripts/execution-profile.sh)"
end = "# EARSHOT EXECUTION PROFILE END"
with open(path, "r", encoding="utf-8") as fh:
    original = fh.read()
mode = stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
if original.count(begin) != original.count(end):
    raise SystemExit("REFUSED: malformed managed execution-profile block")
if original.count(begin) > 1:
    raise SystemExit("REFUSED: duplicate managed execution-profile blocks")
block = (
    f'{begin}\n'
    f'export EARSHOT_EXECUTION_HOST="{host_id}"\n'
    f'export EARSHOT_KALSHI_ACCOUNT_FINGERPRINT="{fingerprint}"\n'
    f'{end}\n'
)
if begin in original:
    start = original.index(begin)
    stop = original.index(end, start) + len(end)
    if stop < len(original) and original[stop] == "\n":
        stop += 1
    updated = original[:start] + block + original[stop:]
else:
    separator = "" if not original or original.endswith("\n") else "\n"
    updated = original + separator + block

directory = os.path.dirname(path) or "."
fd, temporary = tempfile.mkstemp(prefix=".execution-profile.", dir=directory)
try:
    os.fchmod(fd, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fd = -1
        fh.write(updated)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, path)
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
except BaseException:
    if fd >= 0:
        os.close(fd)
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
  echo "configured execution profile for $ACCOUNT_REF (host=$HOST_ID)"
  exit 0
fi

# `resolve` re-derives the exact selected signer and compares it with the
# provisioned value. A rotated/mismatched key therefore fails during nightly
# plan compilation, not minutes before a call.
ACTUAL=$(fingerprint_in_profile_env) || die "could not fingerprint selected credential"
validate_fingerprint "$ACTUAL"
(
  set +x
  set +u
  if ! source "$ACCOUNT_PATH" >/dev/null 2>/dev/null; then
    die "could not load account env"
  fi
  PROFILE_HOST=${EARSHOT_EXECUTION_HOST:-}
  PROFILE_FINGERPRINT=${EARSHOT_KALSHI_ACCOUNT_FINGERPRINT:-}
  [[ "$PROFILE_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || {
    die "account env has no valid EARSHOT_EXECUTION_HOST; run configure"
  }
  validate_fingerprint "$PROFILE_FINGERPRINT"
  [[ "$ACTUAL" == "$PROFILE_FINGERPRINT" ]] || {
    die "account credential no longer matches its configured fingerprint"
  }
  python3 - "$PROFILE_HOST" "$PROFILE_FINGERPRINT" <<'PY'
import json, sys
print(json.dumps({
    "schema": 1,
    "kind": "execution-identity",
    "execution_host_id": sys.argv[1],
    "account_fingerprint": sys.argv[2],
}, sort_keys=True, separators=(",", ":")))
PY
)
