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
  pcm-gate.sh arm-on-url.sh install-event-timer.sh install-t0-agents.sh \
  box-webcast-install.sh run-host-boundary.py; do
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
cat > "$REPO/configs/box-webcast.toml" <<EOF
[event]
id = "KXEARNINGSMENTIONSCHEDULER-TEST-BOX-WEBCAST"
starts_at = "$START"
[[sources]]
kind = "pipe"
cmd = ["ffmpeg", "-i", "\$HOME/box-web-stream.url"]
[[keywords]]
market_ticker = "KXEARNINGSMENTIONSCHEDULER-TEST-FOO"
phrase = "foo"
EOF
cat > "$REPO/configs/box-webcast.env" <<EOF
CONFIG_PATH="configs/box-webcast.toml"
EVENT_DATE="$DATE"
ACCOUNT_ENV="\$HOME/.earshot/account.env"
EOF
cat > "$REPO/configs/dispatch-webcast.toml" <<EOF
[event]
id = "KXEARNINGSMENTIONSCHEDULER-TEST-DISPATCH-WEBCAST"
starts_at = "$START"
[[sources]]
kind = "pipe"
cmd = ["ffmpeg", "-i", "\$HOME/dispatch-web-stream.url"]
[[keywords]]
market_ticker = "KXEARNINGSMENTIONSCHEDULER-TEST-FOO"
phrase = "foo"
EOF
cat > "$REPO/configs/dispatch-webcast.env" <<EOF
CONFIG_PATH="configs/dispatch-webcast.toml"
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
eventctl init --config configs/box-webcast.toml \
  --event-env configs/box-webcast.env --host-id "$HOST_ID" \
  --account-fingerprint "$FP"
eventctl init --config configs/dispatch-webcast.toml \
  --event-env configs/dispatch-webcast.env --host-id "$HOST_ID" \
  --account-fingerprint "$FP"
eventctl init --config configs/static-webcast.toml \
  --event-env configs/static-webcast.env --host-id "$HOST_ID" \
  --account-fingerprint "$FP"
PHONE_RUN=$(find "$REPO/var/runs/KXEARNINGSMENTIONSCHEDULER-TEST-PHONE" \
  -mindepth 1 -maxdepth 1 -type d -print -quit)
WEB_RUN=$(find "$REPO/var/runs/KXEARNINGSMENTIONSCHEDULER-TEST-WEBCAST" \
  -mindepth 1 -maxdepth 1 -type d -print -quit)
BOX_WEB_RUN=$(find "$REPO/var/runs/KXEARNINGSMENTIONSCHEDULER-TEST-BOX-WEBCAST" \
  -mindepth 1 -maxdepth 1 -type d -print -quit)
DISPATCH_WEB_RUN=$(find "$REPO/var/runs/KXEARNINGSMENTIONSCHEDULER-TEST-DISPATCH-WEBCAST" \
  -mindepth 1 -maxdepth 1 -type d -print -quit)
STATIC_RUN=$(find "$REPO/var/runs/KXEARNINGSMENTIONSCHEDULER-TEST-STATIC" \
  -mindepth 1 -maxdepth 1 -type d -print -quit)
for transition in prepared admission_resolved study_completed preflight_passed; do
  eventctl record --run-dir "$PHONE_RUN" --transition "$transition"
done
for transition in prepared access_acquired study_completed preflight_passed; do
  eventctl record --run-dir "$WEB_RUN" --transition "$transition"
  eventctl record --run-dir "$BOX_WEB_RUN" --transition "$transition"
  eventctl record --run-dir "$DISPATCH_WEB_RUN" --transition "$transition"
  eventctl record --run-dir "$STATIC_RUN" --transition "$transition"
done
EARSHOT_REPO="$REPO" python3 "$REPO/scripts/access-plan.py" bind \
  --run-dir "$WEB_RUN" --access-mode public --resolver-kind external-command \
  --resolver-argv-json '["scripts/test-resolver.sh"]' >/dev/null
EARSHOT_REPO="$REPO" python3 "$REPO/scripts/access-plan.py" bind \
  --run-dir "$BOX_WEB_RUN" --access-mode public --resolver-kind external-command \
  --resolver-argv-json '["scripts/test-resolver.sh"]' >/dev/null
EARSHOT_REPO="$REPO" python3 "$REPO/scripts/access-plan.py" bind \
  --run-dir "$DISPATCH_WEB_RUN" --access-mode public --resolver-kind external-command \
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
  list-timers)
    unit=""
    for arg in "$@"; do
      [[ "$arg" == *.timer ]] && unit="$arg"
    done
    [[ -n "$unit" && -f "$STUB_SYSTEMD_STATE/$unit.active" ]] || exit 1
    printf 'Thu 2026-08-20 20:45:00 UTC 1 week left - - %s %s\n' \
      "$unit" "${unit%.timer}.service"
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
cat > "$STUB/bin/ssh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|-i) shift 2 ;;
    *) break ;;
  esac
done
host=${1:?}; shift
command=${1:?}
printf 'ssh %s %s\n' "$host" "$command" >> "$STUB_LOG"
HOME="$REMOTE_HOME" USER=stub-user \
EARSHOT_SYSTEMD_USER_DIR="$REMOTE_SYSTEMD_USER_DIR" \
STUB_SYSTEMD_STATE="$REMOTE_SYSTEMD_STATE" \
EARSHOT_REPO="$REMOTE_REPO" \
/bin/bash -c "$command"
SH
cat > "$STUB/bin/rsync" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
dry=0
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -ani) dry=1; shift ;;
    -e) shift 2 ;;
    -*) shift ;;
    *) args+=("$1"); shift ;;
  esac
done
[[ ${#args[@]} == 2 ]]
src=${args[0]}
dest=${args[1]#*:}
printf 'rsync dry=%s %s %s\n' "$dry" "$src" "$dest" >> "$STUB_LOG"
[[ "$dry" == 1 ]] && exit 0
mkdir -p "$dest"
cp -a "$src"/. "$dest"/
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
  "$STUB/bin/launchctl" "$STUB/bin/ssh" "$STUB/bin/rsync" \
  "$STUB/bin/ffmpeg" "$REPO/scripts/test-resolver.sh" \
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
# The generalized timer must not perturb the existing phone descriptor bytes
# between its last environment line and ExecStart.
python3 - "$SERVICE" "$HOST_ID" <<'PY'
import pathlib, sys
service, host = pathlib.Path(sys.argv[1]), sys.argv[2]
assert f"Environment=EARSHOT_EXECUTION_HOST={host}\nExecStart=" in service.read_text()
PY
[[ "$(scheduled_count "$PHONE_RUN")" == 1 ]]
ENABLES_BEFORE=$(grep -c 'systemctl --user enable --now' "$STUB_LOG" || true)
"$REPO/scripts/install-event-timer.sh" --run-dir "$PHONE_RUN" \
  --expected-host "$HOST_ID" --verify-only >/dev/null
[[ "$(scheduled_count "$PHONE_RUN")" == 1 ]]
[[ "$(grep -c 'systemctl --user enable --now' "$STUB_LOG" || true)" == "$ENABLES_BEFORE" ]]

# Webcast can use the same systemd-user boundary on the box: T-75 (or an exact
# reviewed late-fire override), arm-on-url argv, account env, no launchd PATH.
BOX_FIRE=$(python3 - <<'PY'
import datetime as dt
print((dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
)
"$REPO/scripts/install-event-timer.sh" --run-dir "$BOX_WEB_RUN" \
  --expected-host "$HOST_ID" --fire-at-utc "$BOX_FIRE" >/dev/null
BOX_WEB_SHA=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["plan_sha256"])' \
  "$BOX_WEB_RUN/control/run-plan.json")
BOX_WEB_LABEL="com.earshot.run.${BOX_WEB_SHA:0:20}"
BOX_SERVICE="$EARSHOT_SYSTEMD_USER_DIR/$BOX_WEB_LABEL.service"
BOX_TIMER="$EARSHOT_SYSTEMD_USER_DIR/$BOX_WEB_LABEL.timer"
CANON_REPO=$(cd "$REPO" && pwd -P)
CANON_ACCOUNT=$(cd "$HOME/.earshot" && pwd -P)/account.env
grep -qF "ExecStart=$CANON_REPO/scripts/arm-on-url.sh --run-dir $(cd "$BOX_WEB_RUN" && pwd -P) --i-am-live --fillable" "$BOX_SERVICE"
grep -qF "Environment=EARSHOT_ACCOUNT_ENV=$CANON_ACCOUNT" "$BOX_SERVICE"
! grep -q '^Environment=PATH=' "$BOX_SERVICE"
BOX_ON_CALENDAR="${BOX_FIRE%Z}"
BOX_ON_CALENDAR="${BOX_ON_CALENDAR/T/ } UTC"
grep -qF "OnCalendar=$BOX_ON_CALENDAR" "$BOX_TIMER"
grep -q "systemctl --user list-timers .*${BOX_WEB_LABEL}.timer" "$STUB_LOG"
[[ "$(scheduled_count "$BOX_WEB_RUN")" == 1 ]]

# The remote webcast installer is a typed transport boundary: static-public
# refuses before SSH, while a non-static canonical plan is staged, reconciled,
# checked for an existing executable box pin, and admitted only after the
# remote loaded systemd readback writes its scheduled receipt.
REMOTE_REPO="$TMP/remote-repo"
REMOTE_HOME="$HOME"
REMOTE_SYSTEMD_USER_DIR="$TMP/remote-systemd-user"
REMOTE_SYSTEMD_STATE="$TMP/remote-systemd-state"
export REMOTE_REPO REMOTE_HOME REMOTE_SYSTEMD_USER_DIR REMOTE_SYSTEMD_STATE
mkdir -p "$REMOTE_REPO/scripts" "$REMOTE_REPO/configs" \
  "$REMOTE_REPO/state" "$REMOTE_SYSTEMD_STATE"
for path in eventctl.py access-plan.py schedule-proof.py run-host-boundary.py \
  install-event-timer.sh arm-on-url.sh event-autorun.sh; do
  cp "$REPO/scripts/$path" "$REMOTE_REPO/scripts/$path"
done
cp "$REPO/configs/dispatch-webcast.toml" \
  "$REPO/configs/dispatch-webcast.env" "$REMOTE_REPO/configs/"
DISPATCH_EVENT=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["event_id"])' \
  "$DISPATCH_WEB_RUN/control/run-plan.json")
mkdir -p "$REMOTE_REPO/state/$DISPATCH_EVENT"
printf 'fixture binary\n' > "$REMOTE_REPO/state/$DISPATCH_EVENT/earshot-pinned"
chmod +x "$REMOTE_REPO/scripts/"* "$REMOTE_REPO/state/$DISPATCH_EVENT/earshot-pinned"

SSH_BEFORE=$(grep -c '^ssh ' "$STUB_LOG" || true)
if EARSHOT_BOX_EXECUTION_HOST="$HOST_ID" EARSHOT_BOX=stub-box \
    EARSHOT_BOX_KEY="$TMP/no-key" EARSHOT_BOX_REPO="$REMOTE_REPO" \
    EARSHOT_SSH="$STUB/bin/ssh" EARSHOT_RSYNC="$STUB/bin/rsync" \
    "$REPO/scripts/box-webcast-install.sh" --run-dir "$STATIC_RUN" \
      > "$TMP/static-box-refusal.log" 2>&1; then
  echo "FAIL: static-public AccessPlan reached box dispatch" >&2
  exit 1
fi
grep -q 'static-public AccessPlan' "$TMP/static-box-refusal.log"
[[ "$(grep -c '^ssh ' "$STUB_LOG" || true)" == "$SSH_BEFORE" ]]

EARSHOT_BOX_EXECUTION_HOST="$HOST_ID" EARSHOT_BOX=stub-box \
  EARSHOT_BOX_KEY="$TMP/no-key" EARSHOT_BOX_REPO="$REMOTE_REPO" \
  EARSHOT_SSH="$STUB/bin/ssh" EARSHOT_RSYNC="$STUB/bin/rsync" \
  "$REPO/scripts/box-webcast-install.sh" --run-dir "$DISPATCH_WEB_RUN" \
    >/dev/null
REMOTE_DISPATCH_RUN="$REMOTE_REPO/var/runs/$DISPATCH_EVENT/$(basename "$DISPATCH_WEB_RUN")"
[[ "$(scheduled_count "$REMOTE_DISPATCH_RUN")" == 1 ]]
DISPATCH_SHA=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["plan_sha256"])' \
  "$REMOTE_DISPATCH_RUN/control/run-plan.json")
DISPATCH_SERVICE="$REMOTE_SYSTEMD_USER_DIR/com.earshot.run.${DISPATCH_SHA:0:20}.service"
CANON_REMOTE_REPO=$(cd "$REMOTE_REPO" && pwd -P)
CANON_REMOTE_DISPATCH_RUN=$(cd "$REMOTE_DISPATCH_RUN" && pwd -P)
grep -qF "ExecStart=$CANON_REMOTE_REPO/scripts/arm-on-url.sh --run-dir $CANON_REMOTE_DISPATCH_RUN --i-am-live --fillable" \
  "$DISPATCH_SERVICE"
grep -q '^TimeoutStopSec=150$' "$DISPATCH_SERVICE"
grep -q '^KillMode=mixed$' "$DISPATCH_SERVICE"
[[ ! -e "$REMOTE_REPO/state/hunt/numbers.env" ]]

# ── --dispatch-only: the pre-preflight transport door (E7) ──────────────────
# Box webcast preflight runs ON the box and refuses without the laptop-bound
# AccessPlan, while deploy-box.sh excludes var/runs/ and the full install above
# will not dispatch until preflight_passed. That circle was previously broken
# only by hand rsync. These cases pin the door that breaks it, and its bounds.
PRELAUNCH_RUN_ID="$(basename "$DISPATCH_WEB_RUN")-prelaunch"
eventctl init --config configs/dispatch-webcast.toml \
  --event-env configs/dispatch-webcast.env --host-id "$HOST_ID" \
  --account-fingerprint "$FP" --run-id "$PRELAUNCH_RUN_ID"
PRELAUNCH_RUN="$REPO/var/runs/$DISPATCH_EVENT/$PRELAUNCH_RUN_ID"
for transition in prepared access_acquired; do
  eventctl record --run-dir "$PRELAUNCH_RUN" --transition "$transition"
done
EARSHOT_REPO="$REPO" python3 "$REPO/scripts/access-plan.py" bind \
  --run-dir "$PRELAUNCH_RUN" --access-mode public --resolver-kind external-command \
  --resolver-argv-json '["scripts/test-resolver.sh"]' >/dev/null

box_install() {  # box_install <extra args...>
  EARSHOT_BOX_EXECUTION_HOST="$HOST_ID" EARSHOT_BOX=stub-box \
    EARSHOT_BOX_KEY="$TMP/no-key" EARSHOT_BOX_REPO="$REMOTE_REPO" \
    EARSHOT_SSH="$STUB/bin/ssh" EARSHOT_RSYNC="$STUB/bin/rsync" \
    "$REPO/scripts/box-webcast-install.sh" "$@"
}

# The circle itself: without the flag, a pre-preflight run cannot be dispatched.
SSH_BEFORE=$(grep -c '^ssh ' "$STUB_LOG" || true)
if box_install --run-dir "$PRELAUNCH_RUN" > "$TMP/prelaunch-full.log" 2>&1; then
  echo "FAIL: pre-preflight RunPlan reached the full box install" >&2
  exit 1
fi
grep -q 'has not reached preflight_passed' "$TMP/prelaunch-full.log"
[[ "$(grep -c '^ssh ' "$STUB_LOG" || true)" == "$SSH_BEFORE" ]]

# --dispatch-only breaks it, and installs nothing.
box_install --run-dir "$PRELAUNCH_RUN" --dispatch-only > "$TMP/prelaunch-dispatch.log" 2>&1
grep -q '^DISPATCHED: ' "$TMP/prelaunch-dispatch.log"
grep -q 'no timer installed, no receipt recorded' "$TMP/prelaunch-dispatch.log"
REMOTE_PRELAUNCH_RUN="$REMOTE_REPO/var/runs/$DISPATCH_EVENT/$PRELAUNCH_RUN_ID"
[[ -f "$REMOTE_PRELAUNCH_RUN/control/run-plan.json" ]]
[[ -f "$REMOTE_PRELAUNCH_RUN/control/access-plan.json" ]]
# The whole point: transport must not forge a lifecycle transition, and must not
# install a unit. Both are checked, because either alone would look like success.
[[ "$(scheduled_count "$PRELAUNCH_RUN")" == 0 ]]
[[ "$(scheduled_count "$REMOTE_PRELAUNCH_RUN")" == 0 ]]
PRELAUNCH_SHA=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["plan_sha256"])' \
  "$REMOTE_PRELAUNCH_RUN/control/run-plan.json")
[[ ! -e "$REMOTE_SYSTEMD_USER_DIR/com.earshot.run.${PRELAUNCH_SHA:0:20}.service" ]]
[[ ! -e "$REMOTE_SYSTEMD_USER_DIR/com.earshot.run.${PRELAUNCH_SHA:0:20}.timer" ]]

# Idempotent: a second dispatch of unchanged bytes is an equal-prefix no-op.
box_install --run-dir "$PRELAUNCH_RUN" --dispatch-only > "$TMP/prelaunch-dispatch2.log" 2>&1
grep -q '"relation":"equal"' "$TMP/prelaunch-dispatch2.log"

# Closed attempts DO travel — that is what clears "ambiguous: N active runs" on
# the box. The full install still refuses them.
eventctl record --run-dir "$PRELAUNCH_RUN" --transition blocked
eventctl record --run-dir "$PRELAUNCH_RUN" --transition archived
box_install --run-dir "$PRELAUNCH_RUN" --dispatch-only > "$TMP/prelaunch-super.log" 2>&1
grep -q 'propagating supersession only' "$TMP/prelaunch-super.log"
python3 - "$REMOTE_PRELAUNCH_RUN" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1] + "/control/receipts.jsonl") if l.strip()]
reached = [r.get("transition") for r in rows]
assert "archived" in reached, f"supersession did not reach the box: {reached}"
PY
if box_install --run-dir "$PRELAUNCH_RUN" > "$TMP/prelaunch-closed-full.log" 2>&1; then
  echo "FAIL: closed RunPlan reached the full box install" >&2
  exit 1
fi
grep -q 'terminal or archived' "$TMP/prelaunch-closed-full.log"

# Bound: once the box owns a launched run, the laptop may not push over it.
LAUNCHED_RUN_ID="$(basename "$DISPATCH_WEB_RUN")-launched"
eventctl init --config configs/dispatch-webcast.toml \
  --event-env configs/dispatch-webcast.env --host-id "$HOST_ID" \
  --account-fingerprint "$FP" --run-id "$LAUNCHED_RUN_ID"
LAUNCHED_RUN="$REPO/var/runs/$DISPATCH_EVENT/$LAUNCHED_RUN_ID"
for transition in prepared access_acquired study_completed preflight_passed \
    scheduled capture_ready launched; do
  eventctl record --run-dir "$LAUNCHED_RUN" --transition "$transition"
done
EARSHOT_REPO="$REPO" python3 "$REPO/scripts/access-plan.py" bind \
  --run-dir "$LAUNCHED_RUN" --access-mode public --resolver-kind external-command \
  --resolver-argv-json '["scripts/test-resolver.sh"]' >/dev/null
if box_install --run-dir "$LAUNCHED_RUN" --dispatch-only \
    > "$TMP/launched-dispatch.log" 2>&1; then
  echo "FAIL: --dispatch-only pushed over a launched run" >&2
  exit 1
fi
grep -q 'has reached launched' "$TMP/launched-dispatch.log"

# Scheduling arguments are refused, not silently ignored.
if box_install --run-dir "$PRELAUNCH_RUN" --dispatch-only --verify-only \
    > "$TMP/dispatch-mutex.log" 2>&1; then
  echo "FAIL: --dispatch-only --verify-only was accepted" >&2
  exit 1
fi
grep -q 'mutually exclusive' "$TMP/dispatch-mutex.log"
if box_install --run-dir "$PRELAUNCH_RUN" --dispatch-only \
    --fire-at-utc 2026-08-30T18:45:00Z > "$TMP/dispatch-mutex2.log" 2>&1; then
  echo "FAIL: --dispatch-only --fire-at-utc was accepted" >&2
  exit 1
fi
grep -q 'meaningless' "$TMP/dispatch-mutex2.log"

# ── rehearsal posture (O6) ──────────────────────────────────────────────────
# Both installers hardcoded --i-am-live --fillable into every webcast unit, so a
# drill that exercised the detector was scheduled fillable by construction and
# its inertness rested on per-word caps being right in config. A rehearsal plan
# must produce a unit with NO fill permission, and the readback must enforce it.
cat > "$REPO/configs/rehearsal-webcast.toml" <<EOF
[event]
id = "KXEARNINGSMENTIONSCHEDULER-TEST-REHEARSAL-WEBCAST"
starts_at = "$START"
rehearsal = true
[[sources]]
kind = "pipe"
cmd = ["ffmpeg", "-i", "\$HOME/rehearsal-stream.url"]
[[keywords]]
market_ticker = "KXEARNINGSMENTIONSCHEDULER-TEST-FOO"
phrase = "foo"
EOF
cat > "$REPO/configs/rehearsal-webcast.env" <<EOF
CONFIG_PATH="configs/rehearsal-webcast.toml"
EVENT_DATE="$DATE"
ACCOUNT_ENV="\$HOME/.earshot/account.env"
EOF
eventctl init --config configs/rehearsal-webcast.toml \
  --event-env configs/rehearsal-webcast.env --host-id "$HOST_ID" \
  --account-fingerprint "$FP"
REHEARSAL_RUN=$(find "$REPO/var/runs/KXEARNINGSMENTIONSCHEDULER-TEST-REHEARSAL-WEBCAST" \
  -mindepth 1 -maxdepth 1 -type d -print -quit)
for transition in prepared access_acquired study_completed preflight_passed; do
  eventctl record --run-dir "$REHEARSAL_RUN" --transition "$transition"
done
EARSHOT_REPO="$REPO" python3 "$REPO/scripts/access-plan.py" bind \
  --run-dir "$REHEARSAL_RUN" --access-mode public --resolver-kind external-command \
  --resolver-argv-json '["scripts/test-resolver.sh"]' >/dev/null

# A rehearsal plan is still execution_enabled (it has keywords) — that is the
# whole point: it exercises the detector. Only the fill permission is withheld.
[[ "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["execution_enabled"])' \
  "$REHEARSAL_RUN/control/run-plan.json")" == "True" ]]
[[ "$(EARSHOT_REPO="$REPO" python3 "$REPO/scripts/schedule-proof.py" posture \
  --run-dir "$REHEARSAL_RUN")" == "rehearsal" ]]
[[ "$(EARSHOT_REPO="$REPO" python3 "$REPO/scripts/schedule-proof.py" posture \
  --run-dir "$DISPATCH_WEB_RUN")" == "live" ]]

cp "$REPO/configs/rehearsal-webcast.toml" "$REPO/configs/rehearsal-webcast.env" \
  "$REMOTE_REPO/configs/"
REHEARSAL_EVENT=KXEARNINGSMENTIONSCHEDULER-TEST-REHEARSAL-WEBCAST
mkdir -p "$REMOTE_REPO/state/$REHEARSAL_EVENT"
printf 'fixture binary\n' > "$REMOTE_REPO/state/$REHEARSAL_EVENT/earshot-pinned"
chmod +x "$REMOTE_REPO/state/$REHEARSAL_EVENT/earshot-pinned"
box_install --run-dir "$REHEARSAL_RUN" >/dev/null
REHEARSAL_SHA=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["plan_sha256"])' \
  "$REHEARSAL_RUN/control/run-plan.json")
REHEARSAL_SERVICE="$REMOTE_SYSTEMD_USER_DIR/com.earshot.run.${REHEARSAL_SHA:0:20}.service"
CANON_REHEARSAL_RUN=$(cd "$REMOTE_REPO/var/runs/$REHEARSAL_EVENT/$(basename "$REHEARSAL_RUN")" && pwd -P)
grep -qF "ExecStart=$CANON_REMOTE_REPO/scripts/arm-on-url.sh --run-dir $CANON_REHEARSAL_RUN" \
  "$REHEARSAL_SERVICE"
# The bit that matters: no fill permission anywhere in the installed unit.
if grep -q -- '--i-am-live\|--fillable' "$REHEARSAL_SERVICE"; then
  echo "FAIL: rehearsal unit carries fill permission" >&2
  exit 1
fi
# ...and the live unit still does, so this is a posture switch and not a
# blanket removal that would silently disarm every real event.
grep -q -- '--i-am-live --fillable' "$DISPATCH_SERVICE"

# The readback rejects a rehearsal unit that was given fill flags by any means.
python3 - "$REHEARSAL_SERVICE" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p).read()
open(p, "w").write(re.sub(r"^(ExecStart=.*)$", r"\1 --i-am-live --fillable", s, count=1, flags=re.M))
PY
if EARSHOT_BOX_EXECUTION_HOST="$HOST_ID" EARSHOT_BOX=stub-box \
    EARSHOT_BOX_KEY="$TMP/no-key" EARSHOT_BOX_REPO="$REMOTE_REPO" \
    EARSHOT_SSH="$STUB/bin/ssh" EARSHOT_RSYNC="$STUB/bin/rsync" \
    "$REPO/scripts/box-webcast-install.sh" --run-dir "$REHEARSAL_RUN" \
      --verify-only > "$TMP/rehearsal-tamper.log" 2>&1; then
  echo "FAIL: readback accepted a rehearsal unit carrying fill permission" >&2
  exit 1
fi

# arm-on-url refuses the flags by hand, which is the last hole.
if "$REPO/scripts/arm-on-url.sh" --run-dir "$REHEARSAL_RUN" --i-am-live --fillable \
    > "$TMP/rehearsal-arm.log" 2>&1; then
  echo "FAIL: arm-on-url accepted --fillable against a rehearsal plan" >&2
  exit 1
fi
# Assert the REASON, not just the nonzero exit: arm-on-url would refuse this
# fixture for half a dozen unrelated reasons (no stream, no pin, no board), so
# a bare exit check here would pass even with the posture guard deleted.
if ! grep -q 'declares \[event\] rehearsal = true' "$TMP/rehearsal-arm.log"; then
  echo "FAIL: arm-on-url refused a rehearsal plan, but NOT for the posture —" >&2
  echo "      the guard may be gone and this assertion passing by accident:" >&2
  tail -3 "$TMP/rehearsal-arm.log" >&2
  exit 1
fi

# Editing the config after compile must not flip the posture silently.
printf '\n# drift\n' >> "$REPO/configs/rehearsal-webcast.toml"
if EARSHOT_REPO="$REPO" python3 "$REPO/scripts/schedule-proof.py" posture \
    --run-dir "$REHEARSAL_RUN" > "$TMP/posture-drift.log" 2>&1; then
  echo "FAIL: posture derived from drifted config bytes" >&2
  exit 1
fi
grep -q 'do not match the pinned plan digest' "$TMP/posture-drift.log"

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
  "EARSHOT_RUN_DIR", "EARSHOT_RUN_PLAN_SHA256", "PATH"
}
# AMAT 2026-08-13: without a pinned PATH, launchd resolves python3 to Apple's
# 3.9 (no tomllib/playwright) and the fire path dies before the resolver runs.
assert p["EnvironmentVariables"]["PATH"] == "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
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

# ── resolver retry-until-deadline (AMAT 2026-08-13) ─────────────────────────
# The resolver used to be single-shot: one nonzero exit at T-75 was a total
# miss even when the fix (register, or a manifest minted at call start) was
# minutes away. It must now retry inside the AccessPlan deadline, and the
# portable $HOME/ tokens in the bound argv must reach the resolver EXPANDED.
cat > "$REPO/configs/retry-webcast.toml" <<EOF
[event]
id = "KXEARNINGSMENTIONSCHEDULER-TEST-RETRY"
starts_at = "$START"
[[sources]]
kind = "pipe"
cmd = ["ffmpeg", "-i", "\$HOME/retry-stream.url"]
[[keywords]]
market_ticker = "KXEARNINGSMENTIONSCHEDULER-TEST-FOO"
phrase = "foo"
EOF
cat > "$REPO/configs/retry-webcast.env" <<EOF
CONFIG_PATH="configs/retry-webcast.toml"
EVENT_DATE="$DATE"
ACCOUNT_ENV="\$HOME/.earshot/account.env"
EOF
eventctl init --config configs/retry-webcast.toml \
  --event-env configs/retry-webcast.env --host-id "$HOST_ID" \
  --account-fingerprint "$FP"
RETRY_RUN=$(find "$REPO/var/runs/KXEARNINGSMENTIONSCHEDULER-TEST-RETRY" \
  -mindepth 1 -maxdepth 1 -type d -print -quit)
for transition in prepared access_acquired study_completed preflight_passed; do
  eventctl record --run-dir "$RETRY_RUN" --transition "$transition"
done
cat > "$REPO/scripts/flaky-resolver.sh" <<SH
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "\$@" > "$STUB/flaky-argv"
n=\$(cat "\$HOME/.flaky-count" 2>/dev/null || echo 0)
n=\$((n+1)); echo "\$n" > "\$HOME/.flaky-count"
(( n >= 3 )) || exit 1
tmp="\$HOME/.retry-stream.url.\$\$"
printf 'https://media.example.invalid/live/master.m3u8\n' > "\$tmp"
mv -f -- "\$tmp" "\$HOME/retry-stream.url"
SH
chmod +x "$REPO/scripts/flaky-resolver.sh"
EARSHOT_REPO="$REPO" python3 "$REPO/scripts/access-plan.py" bind \
  --run-dir "$RETRY_RUN" --access-mode public --resolver-kind external-command \
  --resolver-argv-json '["scripts/flaky-resolver.sh","--out","$HOME/retry-stream.url"]' \
  --url-file '$HOME/retry-stream.url' >/dev/null
eventctl record --run-dir "$RETRY_RUN" --transition scheduled
RETRY_EVENT=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["event_id"])' \
  "$RETRY_RUN/control/run-plan.json")
mkdir -p "$REPO/state/$RETRY_EVENT"
printf 'fixture binary\n' > "$REPO/state/$RETRY_EVENT/earshot-pinned"
chmod +x "$REPO/state/$RETRY_EVENT/earshot-pinned"
RETRY_SHA=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["plan_sha256"])' \
  "$RETRY_RUN/control/run-plan.json")
EARSHOT_RUN_PLAN_SHA256="$RETRY_SHA" EARSHOT_RESOLVER_RETRY_SECS=1 \
  "$REPO/scripts/arm-on-url.sh" --run-dir "$RETRY_RUN" \
    --i-am-live --fillable --secs 6 >/dev/null
[[ "$(cat "$TMP/home/.flaky-count")" == 3 ]]  # failed twice, succeeded third
[[ "$(wc -l < "$LAUNCH_COUNT" | tr -d ' ')" == 2 ]]
# The argv reached the resolver with $HOME expanded, not as a literal token.
# (normpath: $TMPDIR carries a trailing slash, Path.home() collapses it.)
EXPANDED_URL=$(python3 -c 'import os,sys;print(os.path.normpath(sys.argv[1]))' \
  "$TMP/home/retry-stream.url")
grep -qx "$EXPANDED_URL" "$STUB/flaky-argv"
! grep -q '\$HOME' "$STUB/flaky-argv"

# An always-failing resolver must exhaust the deadline and exit 3 — bounded,
# not infinite, and never launched.
cat > "$REPO/configs/retry2-webcast.toml" <<EOF
[event]
id = "KXEARNINGSMENTIONSCHEDULER-TEST-RETRYB"
starts_at = "$START"
[[sources]]
kind = "pipe"
cmd = ["ffmpeg", "-i", "\$HOME/retry2-stream.url"]
[[keywords]]
market_ticker = "KXEARNINGSMENTIONSCHEDULER-TEST-FOO"
phrase = "foo"
EOF
cat > "$REPO/configs/retry2-webcast.env" <<EOF
CONFIG_PATH="configs/retry2-webcast.toml"
EVENT_DATE="$DATE"
ACCOUNT_ENV="\$HOME/.earshot/account.env"
EOF
eventctl init --config configs/retry2-webcast.toml \
  --event-env configs/retry2-webcast.env --host-id "$HOST_ID" \
  --account-fingerprint "$FP"
RETRY2_RUN=$(find "$REPO/var/runs/KXEARNINGSMENTIONSCHEDULER-TEST-RETRYB" \
  -mindepth 1 -maxdepth 1 -type d -print -quit)
for transition in prepared access_acquired study_completed preflight_passed; do
  eventctl record --run-dir "$RETRY2_RUN" --transition "$transition"
done
cat > "$REPO/scripts/dead-resolver.sh" <<'SH'
#!/usr/bin/env bash
exit 1
SH
chmod +x "$REPO/scripts/dead-resolver.sh"
NEAR_DEADLINE=$(python3 -c 'import datetime as dt;print((dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=4)).strftime("%Y-%m-%dT%H:%M:%SZ"))')
EARSHOT_REPO="$REPO" python3 "$REPO/scripts/access-plan.py" bind \
  --run-dir "$RETRY2_RUN" --access-mode public --resolver-kind external-command \
  --resolver-argv-json '["scripts/dead-resolver.sh"]' \
  --url-file '$HOME/retry2-stream.url' --deadline-utc "$NEAR_DEADLINE" >/dev/null
eventctl record --run-dir "$RETRY2_RUN" --transition scheduled
RETRY2_EVENT=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["event_id"])' \
  "$RETRY2_RUN/control/run-plan.json")
mkdir -p "$REPO/state/$RETRY2_EVENT"
printf 'fixture binary\n' > "$REPO/state/$RETRY2_EVENT/earshot-pinned"
chmod +x "$REPO/state/$RETRY2_EVENT/earshot-pinned"
RETRY2_SHA=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["plan_sha256"])' \
  "$RETRY2_RUN/control/run-plan.json")
RC2=0
EARSHOT_RUN_PLAN_SHA256="$RETRY2_SHA" EARSHOT_RESOLVER_RETRY_SECS=1 \
  "$REPO/scripts/arm-on-url.sh" --run-dir "$RETRY2_RUN" \
    --i-am-live --fillable --secs 6 >/dev/null 2>&1 || RC2=$?
[[ "$RC2" == 3 ]]
[[ "$(wc -l < "$LAUNCH_COUNT" | tr -d ' ')" == 2 ]]  # no new launch

# Neither fire-time supervisor owns a process-group KILL/re-arm loop.
# (grep -E, not rg: ripgrep is absent on some hosts and a missing tool must
# never silently pass an assertion.)
! grep -Eq 'kill[[:space:]]+-KILL|kill[[:space:]]+--[[:space:]]+-|kill[[:space:]]+-TERM[[:space:]]+-' \
  "$REPO/scripts/arm-on-url.sh"
[[ "$(grep -Ec 'exec .*arm-on-url\.sh' "$ROOT/scripts/webcast-autorun.sh")" == 1 ]]

echo "typed_scheduler_boundary: PASS"
