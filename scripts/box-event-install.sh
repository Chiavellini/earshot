#!/usr/bin/env bash
# Schedule ONE phone event on the us-east-1 box, then prove the installed job is
# the job you meant.
#
#   scripts/box-event-install.sh --event-env configs/<ev>-phone-autorun.env
#       [--date YYYY-MM-DD] [--test] [--verify-only] [--disarm] [--no-digits]
#
# WHY THIS EXISTS. Three separate 2026-08-05 costs, all in the gap between
# "I edited the schedule" and "the schedule that will fire":
#
#   1. EDITING A UNIT FILE DOES NOT UPDATE THE LOADED JOB. On systemd that is
#      `daemon-reload` + `enable --now` (install-event-timer.sh does both); on
#      launchd it is bootout/bootstrap. A config carrying a raised cap plus a
#      stale job missing the matching posture flag makes EVERY order refuse, and
#      nothing says so until the call. The TLN go-live only caught it because it
#      re-read the loaded job and grepped for --fillable. That re-read is step 3
#      here, and it is not optional.
#   2. DIGITS NEVER REACH THE BOX BY DEPLOY. scripts/deploy-box.sh excludes
#      state/, so state/hunt/numbers.env — the gitignored store the tracked event
#      env reads at source time — simply is not there. An env that resolves to an
#      empty TO_NUMBER fails at fire time, not at prep time.
#   3. THE SCHEDULER'S PATH IS NOT YOURS. `systemd --user` on the box runs with
#      no ~/.cargo/bin (measured), so a fire-time compile is impossible and the
#      pinned binary from preflight is the only unattended-safe artifact.
#
# Digits are piped over stdin, never passed as arguments: an argv is visible in
# `ps` to every user on the host and lands in shell history.
#
# This script does NOT dial and does NOT arm. It schedules, and it verifies.
set -uo pipefail

REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO" || exit 2

EVENT_ENV="" DATE="" TEST=0 VERIFY_ONLY=0 DISARM=0 PUSH_DIGITS=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --event-env)  EVENT_ENV="$2"; shift 2 ;;
    --date)       DATE="$2"; shift 2 ;;
    --test)       TEST=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --disarm)     DISARM=1; shift ;;
    --no-digits)  PUSH_DIGITS=0; shift ;;
    -h|--help)    sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$EVENT_ENV" && -f "$EVENT_ENV" ]] || {
  echo "usage: $0 --event-env configs/<ev>-phone-autorun.env [--date YYYY-MM-DD] [--test] [--verify-only] [--disarm]" >&2
  exit 2
}

EARSHOT_INSTANCE_ID="${EARSHOT_INSTANCE_ID:-i-04c50d76572aa092e}"
if [[ -z "${EARSHOT_BOX:-}" ]] && command -v aws >/dev/null 2>&1; then
  _ip="$(aws ec2 describe-instances --instance-ids "$EARSHOT_INSTANCE_ID" \
           --query 'Reservations[].Instances[].PublicIpAddress' --output text 2>/dev/null || true)"
  [[ "$_ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]] && EARSHOT_BOX="ubuntu@$_ip"
fi
BOX="${EARSHOT_BOX:-ubuntu@3.212.174.94}"
KEY="${EARSHOT_BOX_KEY:-$HOME/.ssh/earshot-useast1.pem}"
BOX_REPO="${EARSHOT_BOX_REPO:-~/ear}"
SSH=(ssh -o ConnectTimeout=15 -o BatchMode=yes -i "$KEY" "$BOX")

# ── Read the event env WITHOUT letting it leak into this shell ─────────────
# It contains command substitutions that read the digit store; evaluate them in
# a subshell and export only the scalars this script needs.
eval "$(
  set -a
  # shellcheck disable=SC1090
  source "$EVENT_ENV" >/dev/null 2>&1
  set +a
  for v in EVENT_NAME LABEL EVENT_DATE FIRE_HHMM BIND_PORT CONFIG_PATH ACCOUNT_ENV \
           EARSHOT_STREAM_URL DIAL_ATTEMPTS STREAM_CONFIRM_SECS OPERATOR_HANDLER; do
    printf 'EV_%s=%q\n' "$v" "${!v:-}"
  done
  # Presence only. The digits themselves must not cross into this shell, be
  # printed, or reach a log — this is the one fact we need about them.
  printf 'EV_HAS_NUMBER=%q\n' "$([[ -n "${TO_NUMBER:-}" && "${TO_NUMBER}" != *TBD* ]] && echo 1 || echo 0)"
  printf 'EV_HAS_PASSCODE=%q\n' "$([[ -n "${PASSCODE:-}" && "${PASSCODE}" != *TBD* ]] && echo 1 || echo 0)"
)"

[[ -n "$EV_LABEL" ]]     || { echo "no LABEL in $EVENT_ENV — install-event-timer.sh names both units after it" >&2; exit 2; }
[[ -n "$EV_FIRE_HHMM" ]] || { echo "no FIRE_HHMM in $EVENT_ENV" >&2; exit 2; }
DATE="${DATE:-$EV_EVENT_DATE}"
[[ "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "need a real --date (or EVENT_DATE in the env), got '$DATE'" >&2; exit 2; }

echo "event ${EV_EVENT_NAME:-?}  label $EV_LABEL  fire ${EV_FIRE_HHMM}Z on $DATE"
echo "config $EV_CONFIG_PATH  port ${EV_BIND_PORT:-?}  ingress ${EV_STREAM_URL_UNSET:-$EV_EARSHOT_STREAM_URL}"

# ── Disarm ─────────────────────────────────────────────────────────────────
if [[ "$DISARM" == 1 ]]; then
  "${SSH[@]}" "systemctl --user disable --now '$EV_LABEL.timer' 2>/dev/null; systemctl --user stop '$EV_LABEL.service' 2>/dev/null; systemctl --user list-timers --all --no-legend | grep -F '$EV_LABEL' || echo 'no timer left'"
  echo "disarmed $EV_LABEL"
  exit 0
fi

# ── Coherence checks the scheduler cannot make for itself ──────────────────
FAILS=0
fail() { echo "FAIL: $*" >&2; FAILS=$((FAILS+1)); }

if [[ -n "$EV_CONFIG_PATH" && -f "$EV_CONFIG_PATH" ]]; then
  CFG_PORT=$(sed -n 's/^[[:space:]]*bind[[:space:]]*=[[:space:]]*"[^"]*:\([0-9]*\)".*/\1/p' "$EV_CONFIG_PATH" | head -1)
  if ! grep -qE '^[[:space:]]*kind[[:space:]]*=[[:space:]]*"twilio' "$EV_CONFIG_PATH"; then
    fail "$EV_CONFIG_PATH has no twilio_stream source — this installer is for PHONE events only (webcast lanes use scripts/install-t0-agents.sh)"
  fi
  # The three ports that must agree or media silently never arrives: the
  # listener's own bind, the env's BIND_PORT (now honored by event-autorun.sh),
  # and the /e/<port> path Twilio was told to connect to.
  URL_PORT="${EV_EARSHOT_STREAM_URL##*/e/}"
  [[ -n "$CFG_PORT" && -n "$EV_BIND_PORT" && "$CFG_PORT" != "$EV_BIND_PORT" ]] \
    && fail "port disagreement: config binds $CFG_PORT, env BIND_PORT=$EV_BIND_PORT"
  [[ -n "$EV_EARSHOT_STREAM_URL" && "$URL_PORT" != "$CFG_PORT" ]] \
    && fail "port disagreement: EARSHOT_STREAM_URL ends /e/$URL_PORT, config binds $CFG_PORT"
  [[ "$EV_EARSHOT_STREAM_URL" == wss://* ]] || fail "EARSHOT_STREAM_URL is not a wss:// URL ('$EV_EARSHOT_STREAM_URL') — the box is tunnel-free; without it launch-event.sh falls back to ngrok"
  [[ "$EV_EARSHOT_STREAM_URL" == *ngrok* ]] && fail "EARSHOT_STREAM_URL points at ngrok — production telephony terminates TLS on the box (Q-TLS: p99 226 ms -> 20 ms, 61 stalls -> 0)"
else
  fail "CONFIG_PATH '$EV_CONFIG_PATH' not found"
fi

if [[ "$TEST" != 1 && "$EV_HAS_NUMBER" != 1 ]]; then
  fail "the event env resolves TO_NUMBER to TBD/empty — the digit store has no entry for this venue yet. Add it: state/hunt/numbers.env (KEY=+E164[,passcode#], mode 0600), or run scripts/hunt-dial-in.py --emit-numbers"
fi

(( FAILS )) && { echo; echo "REFUSED: $FAILS coherence failure(s) — nothing installed"; exit 1; }

# ── Push the digit store (stdin, 0600, never argv) ─────────────────────────
if [[ "$PUSH_DIGITS" == 1 && "$VERIFY_ONLY" != 1 ]]; then
  if [[ -f state/hunt/numbers.env ]]; then
    if "${SSH[@]}" "umask 077; mkdir -p ${BOX_REPO}/state/hunt && cat > ${BOX_REPO}/state/hunt/numbers.env && chmod 600 ${BOX_REPO}/state/hunt/numbers.env" < state/hunt/numbers.env; then
      N=$(grep -c '^[A-Z0-9]*=' state/hunt/numbers.env)
      echo "pushed state/hunt/numbers.env → $BOX ($N venue entries, mode 0600, over stdin)"
    else
      echo "FAIL: could not push the digit store to the box" >&2; exit 1
    fi
  else
    echo "WARN: no local state/hunt/numbers.env to push — the box env will resolve TO_NUMBER to TBD" >&2
  fi
fi

# ── Install ────────────────────────────────────────────────────────────────
if [[ "$VERIFY_ONLY" != 1 ]]; then
  TEST_ENV=""
  TEST_FLAG=""
  if [[ "$TEST" == 1 ]]; then
    # Rehearsal posture. install-event-timer.sh --test bakes EARSHOT_AUTORUN_TEST=1
    # into the unit and forwards any EARSHOT_AUTORUN_TEST_* already exported, so
    # the drill runs through the REAL production mechanism (a timer-fired unit),
    # not a manual shell run — the distinction CLAUDE.md makes load-bearing.
    TEST_FLAG="--test"
    TEST_ENV="EARSHOT_AUTORUN_TEST_CONFIG=${EARSHOT_AUTORUN_TEST_CONFIG:-} EARSHOT_AUTORUN_TEST_DIAL=${EARSHOT_AUTORUN_TEST_DIAL:-} EARSHOT_AUTORUN_TEST_TO=${EARSHOT_AUTORUN_TEST_TO:-} EARSHOT_AUTORUN_TEST_LONG_S=${EARSHOT_AUTORUN_TEST_LONG_S:-}"
    if [[ "${EARSHOT_AUTORUN_TEST_DIAL:-0}" == 1 && -z "${EARSHOT_AUTORUN_TEST_TO:-}" ]]; then
      echo "REFUSED: EARSHOT_AUTORUN_TEST_DIAL=1 without EARSHOT_AUTORUN_TEST_TO — a drill must never ring a real bridge" >&2
      exit 1
    fi
  fi
  echo "installing $EV_LABEL on $BOX …"
  "${SSH[@]}" "cd ${BOX_REPO} && export $TEST_ENV && scripts/install-event-timer.sh --event-env '$EVENT_ENV' --fire-hhmm '$EV_FIRE_HHMM' --date '$DATE' $TEST_FLAG" || {
    echo "REFUSED: install-event-timer.sh failed on the box (lingering off? not Linux? env env missing?)" >&2
    exit 1
  }
fi

# ── VERIFY THE INSTALLED JOB — never the file you wrote ────────────────────
echo
echo "── re-reading the INSTALLED unit (not the file that was written) ──"
INSTALLED=$("${SSH[@]}" "systemctl --user cat '$EV_LABEL.service' '$EV_LABEL.timer' 2>/dev/null; echo '---TIMERS---'; systemctl --user list-timers --all --no-legend 2>/dev/null | grep -F '$EV_LABEL'")
# `systemctl --user cat` prints nothing for an unknown unit, so the marker echo
# is all that comes back. Say "nothing is loaded" plainly instead of listing six
# individual mismatches against an empty document.
if ! grep -q 'ExecStart=' <<<"$INSTALLED"; then
  echo "REFUSED: NO unit named $EV_LABEL is loaded on the box — nothing will fire." >&2
  echo "  Install it: $0 --event-env $EVENT_ENV --date $DATE" >&2
  exit 1
fi

VFAILS=0
vcheck() { # vcheck <needle> <description>
  if grep -qF -- "$1" <<<"$INSTALLED"; then
    echo "  OK   $2"
  else
    echo "  MISS $2  (looked for '$1')"
    VFAILS=$((VFAILS+1))
  fi
}
EVENT_ENV_ABS_BOX="${BOX_REPO/#\~/\/home\/ubuntu}/$EVENT_ENV"
vcheck "EARSHOT_EVENT_ENV=" "unit carries EARSHOT_EVENT_ENV"
grep -qF "$EVENT_ENV" <<<"$INSTALLED" \
  && echo "  OK   it points at $EVENT_ENV" \
  || { echo "  MISS the loaded unit points at a DIFFERENT event env than $EVENT_ENV"; VFAILS=$((VFAILS+1)); }
vcheck "scripts/event-autorun.sh" "ExecStart is event-autorun.sh"
vcheck "OnCalendar=$DATE ${EV_FIRE_HHMM:0:2}:${EV_FIRE_HHMM:2:2}:00" "fires at $DATE ${EV_FIRE_HHMM:0:2}:${EV_FIRE_HHMM:2:2}:00 box-local (=UTC)"
if [[ "$TEST" == 1 ]]; then
  vcheck "EARSHOT_AUTORUN_TEST=1" "REHEARSAL posture (dry-run flags, no live orders)"
else
  grep -qF "EARSHOT_AUTORUN_TEST=1" <<<"$INSTALLED" \
    && { echo "  MISS the loaded unit is in TEST posture but this is a LIVE install"; VFAILS=$((VFAILS+1)); } \
    || echo "  OK   LIVE posture (event-autorun applies --i-am-live --allow-mention --fillable)"
fi
if grep -q -- '---TIMERS---' <<<"$INSTALLED"; then
  NEXT=$(sed -n '/---TIMERS---/,$p' <<<"$INSTALLED" | tail -n +2 | head -1)
  [[ -n "$NEXT" ]] \
    && echo "  OK   timer listed: $NEXT" \
    || { echo "  MISS timer is not in systemctl --user list-timers — it will not fire"; VFAILS=$((VFAILS+1)); }
fi

echo
if (( VFAILS )); then
  echo "SUSPECT: the loaded job does not match what was asked for ($VFAILS mismatch(es))."
  echo "Inspect:  ssh -i $KEY $BOX \"systemctl --user cat $EV_LABEL.service\""
  "$REPO/scripts/notify.sh" "PHONE ARM SUSPECT: ${EV_EVENT_NAME:-?}" \
    "The installed systemd job for $EV_LABEL does not match the requested posture ($VFAILS mismatch). It will fire anyway — inspect before the call." --quiet >/dev/null 2>&1 || true
  exit 1
fi

echo "ARMED: $EV_LABEL fires ${EV_FIRE_HHMM}Z on $DATE, box $BOX"
echo "  ingress  $EV_EARSHOT_STREAM_URL"
echo "  dial     attempts=${EV_DIAL_ATTEMPTS:-launch-event default 3}, stream-confirm=${EV_STREAM_CONFIRM_SECS:-90}s"
echo "  disarm   scripts/box-event-install.sh --event-env $EVENT_ENV --disarm"
echo "  kill     ssh -i $KEY $BOX 'touch ${BOX_REPO}/state/<EVENT_ID>/STOP'"
"$REPO/scripts/notify.sh" "PHONE ARM SCHEDULED: ${EV_EVENT_NAME:-?}" \
  "$EV_LABEL fires ${EV_FIRE_HHMM}Z on $DATE on $BOX. Ingress $EV_EARSHOT_STREAM_URL. Verified against the loaded unit." --quiet >/dev/null 2>&1 || true
exit 0
