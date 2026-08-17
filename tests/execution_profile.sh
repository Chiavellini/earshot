#!/usr/bin/env bash
# Offline contract tests for scripts/execution-profile.sh. Every account env,
# key path, and binary is a temporary fake; this never reads or mutates the
# operator's real credential files and performs no network call.
set -euo pipefail

# bash 3.2 (macOS) does NOT trip `set -e` on a failing bare `[[ ]]`, so every
# assertion here is `[[ … ]] || fail $LINENO` (SYSTEM-STATE E14, the E9 class).
fail() {  # fail <lineno>
  echo "FAIL: execution_profile.sh:$1: $(sed -n "${1}p" "${BASH_SOURCE[0]}" | sed 's/^ *//')" >&2
  exit 1
}

REPO=$(cd "$(dirname "$0")/.." && pwd -P)
SCRIPT="$REPO/scripts/execution-profile.sh"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/execution-profile-test.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
TEST_HOME="$TMP/home"
mkdir -p "$TEST_HOME/.earshot"
ACCOUNT="$TEST_HOME/.earshot/ms.env"
CONFIG="$TMP/event.toml"
DIFFERENT_CONFIG="$TMP/different.toml"
FAKE_BIN="$TMP/earshot"

cat >"$FAKE_BIN" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == account-fingerprint ]] || exit 64
shift
different=0
if [[ "${1:-}" == --config ]]; then
  [[ -f "${2:-}" ]] || exit 65
  grep -q '^DIFFERENT=1$' "$2" && different=1
  shift 2
fi
[[ $# -eq 0 ]] || exit 67
if [[ $different -eq 1 ]]; then
  printf 'sha256:%064d\n' 2
elif [[ "${KALSHI_API_KEY_ID:-}" == rotated-key ]]; then
  printf 'sha256:%064d\n' 3
else
  printf 'sha256:%064d\n' 1
fi
SH
chmod 700 "$FAKE_BIN"
printf 'SAME=1\n' >"$CONFIG"
printf 'DIFFERENT=1\n' >"$DIFFERENT_CONFIG"

cat >"$ACCOUNT" <<'ENV'
# secret sentinel must survive byte-for-byte and must never reach output
export KALSHI_API_KEY_ID="test-key"
export KALSHI_PRIVATE_KEY_PATH="$HOME/private.pem"
export TWILIO_AUTH_TOKEN="DO-NOT-PRINT-THIS"
echo "SOURCE-OUTPUT-MUST-BE-SUPPRESSED"
ENV
chmod 600 "$ACCOUNT"

run_profile() {
  HOME="$TEST_HOME" "$SCRIPT" "$@"
}

OUT=$(run_profile configure \
  --account '$HOME/.earshot/ms.env' \
  --host-id earshot-useast1 \
  --config "$CONFIG" \
  --bin "$FAKE_BIN" 2>&1)
[[ "$OUT" == *"configured execution profile"* ]] || fail $LINENO
[[ "$OUT" != *"DO-NOT-PRINT-THIS"* ]] || fail $LINENO
[[ "$OUT" != *"SOURCE-OUTPUT-MUST-BE-SUPPRESSED"* ]] || fail $LINENO
[[ $(python3 - "$ACCOUNT" <<'PY'
import os, stat, sys
print(f"{stat.S_IMODE(os.stat(sys.argv[1]).st_mode):03o}")
PY
) == 600 ]] || fail $LINENO
grep -q '^export KALSHI_API_KEY_ID="test-key"$' "$ACCOUNT"
grep -q '^export TWILIO_AUTH_TOKEN="DO-NOT-PRINT-THIS"$' "$ACCOUNT"
[[ $(grep -c '^# EARSHOT EXECUTION PROFILE BEGIN' "$ACCOUNT") -eq 1 ]] || fail $LINENO

JSON=$(run_profile resolve \
  --account '$HOME/.earshot/ms.env' \
  --config "$CONFIG" \
  --bin "$FAKE_BIN")
python3 - "$JSON" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
assert value == {
    "schema": 1,
    "kind": "execution-identity",
    "execution_host_id": "earshot-useast1",
    "account_fingerprint": "sha256:" + "0" * 63 + "1",
}
PY

# Reconfiguration is content-idempotent and never duplicates its managed block.
BEFORE=$(shasum -a 256 "$ACCOUNT" | awk '{print $1}')
run_profile configure \
  --account '$HOME/.earshot/ms.env' \
  --host-id earshot-useast1 \
  --bin "$FAKE_BIN" >/dev/null
AFTER=$(shasum -a 256 "$ACCOUNT" | awk '{print $1}')
[[ "$BEFORE" == "$AFTER" ]] || fail $LINENO
[[ $(grep -c '^# EARSHOT EXECUTION PROFILE BEGIN' "$ACCOUNT") -eq 1 ]] || fail $LINENO

# A config-bound signer that differs from ambient cannot poison a shared
# account profile, and the refusal leaves the credential file untouched.
BEFORE=$(shasum -a 256 "$ACCOUNT" | awk '{print $1}')
if run_profile configure \
    --account '$HOME/.earshot/ms.env' \
    --host-id earshot-useast1 \
    --config "$DIFFERENT_CONFIG" \
    --bin "$FAKE_BIN" >"$TMP/mismatch.out" 2>&1; then
  echo "mismatched config credential was accepted" >&2
  exit 1
fi
AFTER=$(shasum -a 256 "$ACCOUNT" | awk '{print $1}')
[[ "$BEFORE" == "$AFTER" ]] || fail $LINENO
grep -q 'different credential' "$TMP/mismatch.out"

# Rotation is caught by resolve before a RunPlan can pin stale identity.
python3 - "$ACCOUNT" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
s = p.read_text().replace(
    'KALSHI_API_KEY_ID="test-key"',
    'KALSHI_API_KEY_ID="rotated-key"',
)
p.write_text(s)
PY
chmod 600 "$ACCOUNT"
if run_profile resolve \
    --account '$HOME/.earshot/ms.env' \
    --config "$CONFIG" \
    --bin "$FAKE_BIN" >"$TMP/rotation.out" 2>&1; then
  echo "stale account fingerprint was accepted" >&2
  exit 1
fi
grep -q 'no longer matches' "$TMP/rotation.out"

# Secret-bearing account envs with broad permissions are rejected before any
# fingerprint process or write.
chmod 644 "$ACCOUNT"
if run_profile configure \
    --account '$HOME/.earshot/ms.env' \
    --host-id earshot-useast1 \
    --bin "$FAKE_BIN" >"$TMP/mode.out" 2>&1; then
  echo "world-readable account env was accepted" >&2
  exit 1
fi
grep -q 'permissions' "$TMP/mode.out"

echo "execution-profile: 8/8 passed"
