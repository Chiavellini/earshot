#!/usr/bin/env bash
# Offline contract test for the launchd/systemd schema-3 scheduler boundary.
# Every OS command is a stateful local stub: no launchctl, systemctl, loginctl,
# network, dial, deploy, or runtime command reaches the host.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/typed-scheduler.XXXXXX")
trap 'rm -rf -- "$TMP"' EXIT
REPO="$TMP/repo"
STUB="$TMP/stub"
mkdir -p "$REPO/scripts" "$REPO/configs" "$STUB/bin" "$STUB/systemd" \
  "$STUB/launchd" "$TMP/home/Library/LaunchAgents"
export HOME="$TMP/home"
mkdir -p "$HOME/.earshot"
printf 'EARSHOT_ACCOUNT_FIXTURE=1\n' > "$HOME/.earshot/account.env"

for path in eventctl.py access-plan.py schedule-proof.py capture-ready.py \
  pcm-gate.sh arm-on-url.sh install-event-timer.sh install-t0-agents.sh; do
  cp "$ROOT/scripts/$path" "$REPO/scripts/$path"
done
printf '#!/usr/bin/env bash\nexit 0\n' > "$REPO/scripts/event-autorun.sh"
chmod +x "$REPO/scripts/"*

START=$(python3 - <<'PY'
import datetime as dt
print((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
)
DATE=${START%%T*}
cat > "$REPO/configs/phone.toml" <<EOF
[event]
id = "KXEARNINGSMENTIONSCHEDULER-TEST-PHONE"
starts_at = "$START"
[[sources]]
kind = "twilio_stream"
bind = "127.0.0.1:8080"
[[keywords]]
market_ticker = "KXEARNINGSMENTIONSCHEDULER-TEST-FOO"
phrase = "foo"
EOF
cat > "$REPO/configs/phone.env" <<EOF
CONFIG_PATH="configs/phone.toml"
EVENT_DATE="$DATE"
EOF
cat > "$REPO/configs/webcast.toml" <<EOF
[event]
id = "KXEARNINGSMENTIONSCHEDULER-TEST-WEBCAST"
starts_at = "$START"
[[sources]]
kind = "pipe"
cmd = ["ffmpeg", "-i", "\$HOME/web-stream.url"]
[[keywords]]
market_ticker = "KXEARNINGSMENTIONSCHEDULER-TEST-FOO"
phrase = "foo"
EOF
cat > "$REPO/configs/webcast.env" <<EOF
CONFIG_PATH="configs/webcast.toml"
EVENT_DATE="$DATE"
ACCOUNT_ENV="\$HOME/.earshot/account.env"
EOF
cat > "$REPO/configs/static-webcast.toml" <<EOF
[event]
id = "KXEARNINGSMENTIONSCHEDULER-TEST-STATIC"
starts_at = "$START"
[[sources]]
kind = "pipe"
cmd = ["ffmpeg", "-i", "https://example.invalid/live.m3u8"]
[[keywords]]
market_ticker = "KXEARNINGSMENTIONSCHEDULER-TEST-FOO"
phrase = "foo"
EOF
cat > "$REPO/configs/static-webcast.env" <<EOF
CONFIG_PATH="configs/static-webcast.toml"
EVENT_DATE="$DATE"
ACCOUNT_ENV="\$HOME/.earshot/account.env"
EOF

HOST_ID=earshot-test-host
FP="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
eventctl() {
  EARSHOT_REPO="$REPO" python3 "$REPO/scripts/eventctl.py" "$@" >/dev/null
}
eventctl init --config configs/phone.toml --event-env configs/phone.env \
  --host-id "$HOST_ID" --account-fingerprint "$FP"
eventctl init --config configs/webcast.toml --event-env configs/webcast.env \
  --host-id "$HOST_ID" --account-fingerprint "$FP"
eventctl init --config configs/static-webcast.toml \
  --event-env configs/static-webcast.env --host-id "$HOST_ID" \
  --account-fingerprint "$FP"
PHONE_RUN=$(find "$REPO/var/runs/KXEARNINGSMENTIONSCHEDULER-TEST-PHONE" \
  -mindepth 1 -maxdepth 1 -type d -print -quit)
WEB_RUN=$(find "$REPO/var/runs/KXEARNINGSMENTIONSCHEDULER-TEST-WEBCAST" \
  -mindepth 1 -maxdepth 1 -type d -print -quit)
STATIC_RUN=$(find "$REPO/var/runs/KXEARNINGSMENTIONSCHEDULER-TEST-STATIC" \
  -mindepth 1 -maxdepth 1 -type d -print -quit)
for transition in prepared admission_resolved study_completed preflight_passed; do
  eventctl record --run-dir "$PHONE_RUN" --transition "$transition"
done
for transition in prepared access_acquired study_completed preflight_passed; do
  eventctl record --run-dir "$WEB_RUN" --transition "$transition"
  eventctl record --run-dir "$STATIC_RUN" --transition "$transition"
done
EARSHOT_REPO="$REPO" python3 "$REPO/scripts/access-plan.py" bind \
  --run-dir "$WEB_RUN" --access-mode public --resolver-kind external-command \
  --resolver-argv-json '["scripts/test-resolver.sh"]' >/dev/null
EARSHOT_REPO="$REPO" python3 "$REPO/scripts/access-plan.py" bind \
  --run-dir "$STATIC_RUN" --access-mode public --resolver-kind static-public \
  --resolver-argv-json '[]' --expiry-mode stable-public >/dev/null

cat > "$STUB/bin/loginctl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%q ' "$@" >> "$STUB_LOG"
printf '\n' >> "$STUB_LOG"
[[ "$*" == *"--property=Linger --value"* ]] || exit 9
echo yes
SH

cat > "$STUB/bin/systemctl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'systemctl ' >> "$STUB_LOG"
printf '%q ' "$@" >> "$STUB_LOG"
printf '\n' >> "$STUB_LOG"
[[ "${1:-}" == --user ]] && shift
cmd=${1:-}; shift || true
case "$cmd" in
  daemon-reload)
    mkdir -p "$STUB_SYSTEMD_STATE/loaded"
    cp "$EARSHOT_SYSTEMD_USER_DIR"/*.service "$STUB_SYSTEMD_STATE/loaded/"
    cp "$EARSHOT_SYSTEMD_USER_DIR"/*.timer "$STUB_SYSTEMD_STATE/loaded/"
    ;;
  enable)
    [[ "${1:-}" == --now ]] && shift
    unit=${1:?}
    : > "$STUB_SYSTEMD_STATE/$unit.enabled"
    : > "$STUB_SYSTEMD_STATE/$unit.active"
    ;;
  disable)
    [[ "${1:-}" == --now ]] && shift
    unit=${1:?}
    rm -f "$STUB_SYSTEMD_STATE/$unit.enabled" "$STUB_SYSTEMD_STATE/$unit.active"
    ;;
  stop)
    rm -f "$STUB_SYSTEMD_STATE/${1:?}.active"
    ;;
  show)
    unit=${1:?}; shift
    property="" value=0
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --property=*) property=${1#--property=}; shift ;;
        --value) value=1; shift ;;
        *) shift ;;
      esac
    done
    loaded="$STUB_SYSTEMD_STATE/loaded/$unit"
    source="$EARSHOT_SYSTEMD_USER_DIR/$unit"
    case "$property" in
      LoadState) [[ -f "$loaded" ]] && echo loaded || echo not-found ;;
      NeedDaemonReload)
        if [[ -f "$loaded" && -f "$source" ]] && cmp -s "$loaded" "$source"; then
          echo no
        else
          echo yes
        fi
        ;;
      ActiveState) [[ -f "$STUB_SYSTEMD_STATE/$unit.active" ]] && echo active || echo inactive ;;
      UnitFileState) [[ -f "$STUB_SYSTEMD_STATE/$unit.enabled" ]] && echo enabled || echo disabled ;;
      *) exit 8 ;;
    esac
    ;;
  cat)
    set +e
    unit=${1:?}; shift || true
    echo "# $STUB_SYSTEMD_STATE/loaded/$unit"
    /bin/cat "$STUB_SYSTEMD_STATE/loaded/$unit"
    if [[ "${STUB_CORRUPT_CAT:-0}" == 1 ]]; then
      echo 'Unexpected=corrupt'
    fi
    ;;
  *) exit 7 ;;
esac
SH

cat > "$STUB/bin/launchctl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'launchctl ' >> "$STUB_LOG"
printf '%q ' "$@" >> "$STUB_LOG"
printf '\n' >> "$STUB_LOG"
cmd=${1:-}; shift || true
case "$cmd" in
  bootout)
    target=${1:?}
    label=${target##*/}
    rm -rf "$STUB_LAUNCHD_STATE/$label"
    ;;
  bootstrap)
    domain=${1:?}; plist=${2:?}
    label=$("${EARSHOT_PYTHON:-python3}" - "$plist" <<'PY'
import plistlib,sys
print(plistlib.load(open(sys.argv[1], "rb"))["Label"])
PY
)
    mkdir -p "$STUB_LAUNCHD_STATE/$label"
    cp "$plist" "$STUB_LAUNCHD_STATE/$label/loaded.plist"
    (cd "$(dirname "$plist")" && pwd -P; basename "$plist") | paste -sd/ - \
      > "$STUB_LAUNCHD_STATE/$label/path"
    ;;
  print)
    target=${1:?}; label=${target##*/}
    state="$STUB_LAUNCHD_STATE/$label"
    [[ -f "$state/loaded.plist" ]] || exit 3
    "${EARSHOT_PYTHON:-python3}" - "$state/loaded.plist" "$state/path" <<'PY'
import plistlib,sys
p=plistlib.load(open(sys.argv[1], "rb")); path=open(sys.argv[2]).read().strip()
print(f"path = {path}")
print(f"working directory = {p['WorkingDirectory']}")
print("arguments = {")
for x in p["ProgramArguments"]: print(f"  {x}")
print("}")
print("environment = {")
for k,v in p["EnvironmentVariables"].items(): print(f"  {k} => {v}")
print("}")
print("event triggers = {")
for k,v in p["StartCalendarInterval"].items(): print(f"  {k} => {v}")
print("}")
PY
    ;;
  *) exit 7 ;;
esac
SH
cat > "$REPO/scripts/test-resolver.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
tmp="$HOME/.web-stream.url.$$"
printf 'https://media.example.invalid/live/master.m3u8\n' > "$tmp"
mv -f -- "$tmp" "$HOME/web-stream.url"
SH
cat > "$REPO/scripts/launch-webcast.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
run="" live=0 fillable=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir) run=${2:?}; shift 2 ;;
    --i-am-live) live=1; shift ;;
    --fillable) fillable=1; shift ;;
    --config|--account|--bin) [[ -e "${2:?}" ]]; shift 2 ;;
    *) echo "unexpected launch argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$run" && "$live" == 1 && "$fillable" == 1 ]]
EARSHOT_REPO="$EARSHOT_REPO" python3 "$EARSHOT_REPO/scripts/eventctl.py" \
  record --run-dir "$run" --transition launched --actor launch-webcast-test \
  --detail "one typed fire-time launch" >/dev/null
printf 'launch\n' >> "$LAUNCH_COUNT"
SH
cat > "$REPO/scripts/notify.sh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat > "$STUB/bin/ffmpeg" <<'SH'
#!/usr/bin/env bash
head -c 192000 /dev/zero
SH
cat > "$STUB/pdt-stub.py" <<'PY'
print('{"media_sequence": 101}')
print('{"media_sequence": 102}')
PY
chmod +x "$STUB/bin/loginctl" "$STUB/bin/systemctl" \
  "$STUB/bin/launchctl" "$STUB/bin/ffmpeg" "$REPO/scripts/test-resolver.sh" \
  "$REPO/scripts/launch-webcast.sh" "$REPO/scripts/notify.sh"

export USER=stub-user
export EARSHOT_REPO="$REPO"
export EARSHOT_SYSTEMCTL="$STUB/bin/systemctl"
export EARSHOT_LOGINCTL="$STUB/bin/loginctl"
export EARSHOT_SYSTEMD_USER_DIR="$TMP/home/systemd-user"
export EARSHOT_LAUNCHCTL="$STUB/bin/launchctl"
export EARSHOT_LAUNCH_AGENTS="$TMP/home/Library/LaunchAgents"
export EARSHOT_LAUNCH_UID=501
export STUB_SYSTEMD_STATE="$STUB/systemd"
export STUB_LAUNCHD_STATE="$STUB/launchd"
export STUB_LOG="$STUB/calls.log"
export EARSHOT_PYTHON=/opt/homebrew/opt/python@3.13/bin/python3.13
: > "$STUB_LOG"

scheduled_count() {
  python3 - "$1/control/receipts.jsonl" <<'PY'
import json,sys
print(sum(json.loads(x).get("transition") == "scheduled" for x in open(sys.argv[1])))
PY
}

# A loaded-descriptor mismatch rolls the timer back before any proof/receipt.
export STUB_CORRUPT_CAT=1
if "$REPO/scripts/install-event-timer.sh" --run-dir "$PHONE_RUN" \
    --expected-host "$HOST_ID" >/dev/null 2>&1; then
  echo "FAIL: corrupt loaded systemd descriptor was admitted" >&2
  exit 1
fi
unset STUB_CORRUPT_CAT
[[ "$(scheduled_count "$PHONE_RUN")" == 0 ]]
[[ ! -e "$PHONE_RUN/control/schedule-proof.json" ]]

# Good systemd install: one launch descriptor, 150 s drain, one receipt.
"$REPO/scripts/install-event-timer.sh" --run-dir "$PHONE_RUN" \
  --expected-host "$HOST_ID" >/dev/null
PHONE_SHA=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["plan_sha256"])' \
  "$PHONE_RUN/control/run-plan.json")
PHONE_LABEL="com.earshot.run.${PHONE_SHA:0:20}"
SERVICE="$EARSHOT_SYSTEMD_USER_DIR/$PHONE_LABEL.service"
[[ "$(grep -c '^ExecStart=' "$SERVICE")" == 1 ]]
grep -q '^TimeoutStopSec=150$' "$SERVICE"
grep -q '^KillMode=mixed$' "$SERVICE"
grep -qF -- "--run-dir $(cd "$PHONE_RUN" && pwd -P)" "$SERVICE"
[[ "$(scheduled_count "$PHONE_RUN")" == 1 ]]
ENABLES_BEFORE=$(grep -c 'systemctl --user enable --now' "$STUB_LOG" || true)
"$REPO/scripts/install-event-timer.sh" --run-dir "$PHONE_RUN" \
  --expected-host "$HOST_ID" --verify-only >/dev/null
[[ "$(scheduled_count "$PHONE_RUN")" == 1 ]]
[[ "$(grep -c 'systemctl --user enable --now' "$STUB_LOG" || true)" == "$ENABLES_BEFORE" ]]

# Good launchd install and verify-only: exactly one bootstrap and one receipt.
# A valid static-public AccessPlan is not schedulable until this boundary can
# bind its embedded endpoint to a durable PCM proof. Refuse before launchctl
# mutation or a misleading scheduled receipt.
BOOTSTRAPS_BEFORE=$(grep -c 'launchctl bootstrap' "$STUB_LOG" || true)
if "$REPO/scripts/install-t0-agents.sh" --run-dir "$STATIC_RUN" \
    --expected-host "$HOST_ID" >/dev/null 2>&1; then
  echo "FAIL: static-public AccessPlan reached the scheduler" >&2
  exit 1
fi
[[ "$(scheduled_count "$STATIC_RUN")" == 0 ]]
[[ ! -e "$STATIC_RUN/control/schedule-proof.json" ]]
[[ "$(grep -c 'launchctl bootstrap' "$STUB_LOG" || true)" == "$BOOTSTRAPS_BEFORE" ]]

"$REPO/scripts/install-t0-agents.sh" --run-dir "$WEB_RUN" \
  --expected-host "$HOST_ID" >/dev/null
WEB_SHA=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["plan_sha256"])' \
  "$WEB_RUN/control/run-plan.json")
WEB_LABEL="com.earshot.run.${WEB_SHA:0:20}"
PLIST="$EARSHOT_LAUNCH_AGENTS/$WEB_LABEL.plist"
"${EARSHOT_PYTHON:-python3}" - "$PLIST" "$WEB_RUN" <<'PY'
import plistlib,sys
p=plistlib.load(open(sys.argv[1], "rb"))
assert p["ProgramArguments"][1:] == ["--run-dir", str(__import__('pathlib').Path(sys.argv[2]).resolve()), "--i-am-live", "--fillable"]
assert len([x for x in p["ProgramArguments"] if x.endswith("arm-on-url.sh")]) == 1
assert set(p["EnvironmentVariables"]) == {
  "EARSHOT_ACCOUNT_ENV", "EARSHOT_EXECUTION_HOST", "EARSHOT_REPO",
  "EARSHOT_RUN_DIR", "EARSHOT_RUN_PLAN_SHA256"
}
assert p["EnvironmentVariables"]["EARSHOT_ACCOUNT_ENV"] == str(
  (__import__('pathlib').Path.home() / ".earshot" / "account.env").resolve()
)
PY
[[ "$(scheduled_count "$WEB_RUN")" == 1 ]]
BOOTSTRAPS_BEFORE=$(grep -c 'launchctl bootstrap' "$STUB_LOG")
"$REPO/scripts/install-t0-agents.sh" --run-dir "$WEB_RUN" \
  --expected-host "$HOST_ID" --verify-only >/dev/null
[[ "$(scheduled_count "$WEB_RUN")" == 1 ]]
[[ "$(grep -c 'launchctl bootstrap' "$STUB_LOG")" == "$BOOTSTRAPS_BEFORE" ]]

# Host identity is an install gate, not a free-form environment annotation.
BEFORE=$(cp "$WEB_RUN/control/receipts.jsonl" "$TMP/receipts.before"; \
  shasum -a 256 "$TMP/receipts.before" | awk '{print $1}')
if "$REPO/scripts/install-t0-agents.sh" --run-dir "$WEB_RUN" \
    --expected-host wrong-host --verify-only >/dev/null 2>&1; then
  echo "FAIL: wrong execution host verified" >&2
  exit 1
fi
AFTER=$(shasum -a 256 "$WEB_RUN/control/receipts.jsonl" | awk '{print $1}')
[[ "$BEFORE" == "$AFTER" ]]

# Fire-time execution is crash-safe across the PCM-proof/capture-receipt gap
# and reaches the launcher exactly once. Every media/network/runtime boundary
# below is a local stub.
WEB_EVENT=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["event_id"])' \
  "$WEB_RUN/control/run-plan.json")
mkdir -p "$REPO/state/$WEB_EVENT"
printf 'fixture binary\n' > "$REPO/state/$WEB_EVENT/earshot-pinned"
chmod +x "$REPO/state/$WEB_EVENT/earshot-pinned"
export EARSHOT_EXECUTION_HOST="$HOST_ID"
export EARSHOT_RUN_PLAN_SHA256="$WEB_SHA"
export EARSHOT_ACCOUNT_ENV="$HOME/.earshot/account.env"
export EARSHOT_FFMPEG="$STUB/bin/ffmpeg"
export EARSHOT_PDT_LOG="$STUB/pdt-stub.py"
export PCM_GATE_SEQ_SECS=1
export LAUNCH_COUNT="$STUB/launch-count"
if _EARSHOT_CAPTURE_READY_FAIL_AFTER_PUBLISH=1 \
    "$REPO/scripts/arm-on-url.sh" --run-dir "$WEB_RUN" \
      --i-am-live --fillable --secs 6 >/dev/null 2>&1; then
  echo "FAIL: injected capture-proof/receipt crash gap reported success" >&2
  exit 1
fi
[[ -f "$WEB_RUN/control/pcm-proof.json" ]]
[[ -f "$WEB_RUN/control/capture-ready.json" ]]
[[ ! -e "$LAUNCH_COUNT" ]]
[[ "$(python3 - "$WEB_RUN/control/receipts.jsonl" <<'PY'
import json,sys
print(sum(json.loads(x).get("transition") == "capture_ready" for x in open(sys.argv[1])))
PY
)" == 0 ]]
"$REPO/scripts/arm-on-url.sh" --run-dir "$WEB_RUN" \
  --i-am-live --fillable --secs 6 >/dev/null
[[ "$(wc -l < "$LAUNCH_COUNT" | tr -d ' ')" == 1 ]]
[[ "$(python3 - "$WEB_RUN/control/receipts.jsonl" <<'PY'
import json,sys
reached=[json.loads(x).get("transition") for x in open(sys.argv[1])]
print(f"{reached.count('capture_ready')}:{reached.count('launched')}")
PY
)" == "1:1" ]]
if "$REPO/scripts/arm-on-url.sh" --run-dir "$WEB_RUN" \
    --i-am-live --fillable --secs 6 >/dev/null 2>&1; then
  echo "FAIL: already-launched RunPlan was launched again" >&2
  exit 1
fi
[[ "$(wc -l < "$LAUNCH_COUNT" | tr -d ' ')" == 1 ]]

# Neither fire-time supervisor owns a process-group KILL/re-arm loop.
! rg -q 'kill[[:space:]]+-KILL|kill[[:space:]]+--[[:space:]]+-|kill[[:space:]]+-TERM[[:space:]]+-' \
  "$REPO/scripts/arm-on-url.sh"
[[ "$(rg -c 'exec .*arm-on-url\.sh' "$ROOT/scripts/webcast-autorun.sh")" == 1 ]]

echo "typed_scheduler_boundary: PASS"
