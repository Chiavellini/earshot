#!/usr/bin/env bash
# Night-before preflight for an unattended event run (docs/RUNBOOK.md —
# REQUIRED before every scheduled event; born from the 2026-07-15 audit).
#
#   scripts/preflight-event.sh --config configs/<event>.toml --account ~/.earshot/<acct>.env \
#       [--fire-hhmm 0605]   # scheduled fire time, for the pmset wake check
#
# What it does:
#   1. Builds --release from the CURRENT checkout and PINS the binary into
#      the event state dir (earshot-pinned + pinned.info with commit+sha256).
#      ms-autorun/launch-event run the pin — never a fire-time compile.
#   2. Verifies every environment assumption the 06:05 launchd context makes:
#      disk headroom, credentials (Kalshi + Twilio live probes), ngrok agent,
#      required tools, timezone, scheduled wake, mailer.
# Exit nonzero on any FAIL; a PREFLIGHT email reports the full checklist.
set -uo pipefail

CONFIG="" ACCOUNT="" FIRE_HHMM="" EVENT_ENV=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)    CONFIG="$2"; shift 2 ;;
    --account)   ACCOUNT="$2"; shift 2 ;;
    --fire-hhmm) FIRE_HHMM="$2"; shift 2 ;;
    --event-env) EVENT_ENV="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$CONFIG" && -n "$ACCOUNT" ]] || {
  echo "usage: $0 --config <cfg.toml> --account <env-file> [--fire-hhmm 0605]" >&2
  echo "          [--event-env configs/<event>-autorun.env]   # dial-in go/no-go gate" >&2
  exit 2
}

FAILS=0
REPORT_LINES=()

# GNU timeout exists on the box; the CDMX laptop has neither timeout nor
# gtimeout — fall back to a background-killer that reports 124 like timeout.
run_with_timeout() {  # <secs> <cmd…>
  if command -v timeout >/dev/null 2>&1; then timeout "$@"; return $?; fi
  if command -v gtimeout >/dev/null 2>&1; then gtimeout "$@"; return $?; fi
  local secs="$1"; shift
  "$@" & local pid=$!
  ( sleep "$secs" && kill -9 "$pid" 2>/dev/null ) & local watcher=$!
  local rc=0; wait "$pid" || rc=$?
  kill "$watcher" 2>/dev/null || true
  [[ $rc -ge 128 ]] && return 124
  return $rc
}
check() {  # check <PASS|FAIL|WARN> <message>
  local verdict="$1" msg="$2"
  REPORT_LINES+=("[$verdict] $msg")
  echo "[$verdict] $msg"
  [[ "$verdict" == FAIL ]] && FAILS=$((FAILS+1))
  return 0
}

# ── Event identity ─────────────────────────────────────────────────────────
if [[ -f "$CONFIG" ]]; then
  EVENT_ID=$(sed -n '/^\[event\]/,/^\[/s/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
  if [[ -n "$EVENT_ID" ]]; then
    check PASS "config $CONFIG → event $EVENT_ID"
  else
    check FAIL "config $CONFIG has no parseable [event].id"
  fi
else
  check FAIL "config not found: $CONFIG"
fi
STATE_DIR="state/${EVENT_ID:-preflight-unknown}"
mkdir -p "$STATE_DIR"

# ⚠ PORTABILITY (fixed 2026-07-27). This script was written for the CDMX macOS
# laptop. Run unchanged on the us-east Linux box it produced FIVE failures, of
# which FOUR were false — ngrok (not used on Linux, as this script itself says
# 130 lines below), `df -g` (a macOS flag), `cargo` (not on a non-interactive
# PATH), and Twilio creds (irrelevant to a webcast-only event). A gate that
# cries wolf four times out of five trains the operator to ignore the fifth.

# ── Tools the launch chain shells out to ───────────────────────────────────
TOOLS=(curl nc lsof python3 git shasum)
# ngrok is the CDMX tunnel path only; on Linux the WS endpoint is direct.
[[ "$(uname -s)" == "Darwin" ]] && TOOLS+=(ngrok)
for tool in "${TOOLS[@]}"; do
  if command -v "$tool" >/dev/null 2>&1; then
    check PASS "tool: $tool ($(command -v "$tool"))"
  else
    check FAIL "tool MISSING: $tool"
  fi
done

# ── Disk ───────────────────────────────────────────────────────────────────
# `df -g` is macOS. `df -k` is POSIX and works on both.
avail_gb=$(df -k / | awk 'NR==2 {printf "%d", $4/1048576}')
if [[ "${avail_gb:-0}" -ge 5 ]]; then
  check PASS "disk: ${avail_gb} GiB free (>= 5)"
else
  check FAIL "disk: only ${avail_gb:-?} GiB free (< 5) — free space before event day (target/debug is safe to delete; NEVER target/release)"
fi

# ── Build + pin ────────────────────────────────────────────────────────────
# The box is rsync-deployed, so it is NOT a git checkout. That must be a loud
# failure, not a silent pass: the whole point of the pin is "this binary is
# THAT commit", and the previous code printed `git tree clean at ` with an
# EMPTY commit and scored it PASS — a check passing vacuously, the same defect
# class as the placeholder allowlist caught earlier today.
if git rev-parse --git-dir >/dev/null 2>&1; then
  HEAD_SHA=$(git rev-parse HEAD)
  dirty=$(git status --porcelain | wc -l | tr -d ' ')
  [[ "$dirty" == 0 ]] && check PASS "git tree clean at $(git rev-parse --short HEAD)" \
                      || check WARN "git tree has $dirty dirty paths — the pin records what is BUILT, review before trusting"
else
  HEAD_SHA="UNKNOWN-NOT-A-GIT-CHECKOUT"
  dirty="n/a"
  check WARN "NOT a git checkout (rsync-deployed host) — the pin CANNOT record a commit. Provenance is the rsync source, so verify the laptop tree is committed and clean before trusting this pin."
fi
# cargo lives in ~/.cargo/bin, which a NON-INTERACTIVE shell does not get: the
# profile that adds it is skipped. This is the same minimal-env class as the
# ORT_DYLIB_PATH trap, and it made the build look broken when it was fine.
[[ -d "$HOME/.cargo/bin" ]] && export PATH="$HOME/.cargo/bin:$PATH"
if ! command -v cargo >/dev/null 2>&1; then
  check FAIL "cargo not on PATH (looked in ~/.cargo/bin) — cannot build or pin"
fi
if cargo build --release 2>&1 | tail -1 | grep -q "Finished"; then
  cp target/release/earshot "$STATE_DIR/earshot-pinned"
  {
    echo "commit=$HEAD_SHA"
    echo "dirty_paths=$dirty"
    echo "sha256=$(shasum -a 256 "$STATE_DIR/earshot-pinned" | awk '{print $1}')"
    echo "built=$(date "+%Y-%m-%d %H:%M:%S %z")"
  } > "$STATE_DIR/pinned.info"
  check PASS "pinned binary: $STATE_DIR/earshot-pinned ($(grep sha256 "$STATE_DIR/pinned.info" | cut -c1-23)…)"
  # Follow-up 6 gate: arm refuses the WHOLE config over one detector-
  # unencodable variant. Exercise the exact encode path with the pin (offline).
  #
  # ⚠ --model-dir is LOAD-BEARING, not tidiness. compile-keywords resolves the
  # FIRST detector block's model_dir, and the armed configs deliberately keep a
  # [detector.sherpa_kws] block so the engine can be reverted with a one-line
  # edit. Without this, a streaming_ctc config is validated against the
  # GIGASPEECH vocab — a keyword outside the CTC vocab would never fire and this
  # check would report PASS. Hit by hand on 2026-07-25; pinned as a RUNBOOK trap.
  ENGINE=$(sed -n '/^\[detector\]/,/^\[/s/^engine[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
  MODEL_DIR=$(sed -n "/^\[detector\.${ENGINE}\]/,/^\[/s/^model_dir[[:space:]]*=[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$CONFIG" | head -1)
  if [[ -n "$ENGINE" && -n "$MODEL_DIR" ]]; then
    check PASS "detector engine=$ENGINE → model_dir=$MODEL_DIR"
    CK_OUT=$("$STATE_DIR/earshot-pinned" compile-keywords --config "$CONFIG" --model-dir "$MODEL_DIR" 2>&1)
    CK_RC=$?
  else
    check FAIL "could not resolve [detector].engine / its model_dir from $CONFIG — cannot validate the RIGHT vocab"
    CK_OUT=$("$STATE_DIR/earshot-pinned" compile-keywords --config "$CONFIG" 2>&1)
    CK_RC=$?
  fi
  # Detector HEARTBEAT with the pin (Ford 2026-07-28 bug 1): compile-keywords
  # proves the vocab, not the runtime — a wrong-platform onnxruntime dylib
  # still hangs at arm. detector-smoke builds the engine and decodes 3 s of
  # audio under the SAME env the arm will use; run it the night before so a
  # dead runtime is a tonight problem, not a T-20 problem.
  if [[ "$ENGINE" == "streaming_ctc" && -z "${ORT_DYLIB_PATH:-}" ]]; then
    case "$(uname -s)" in
      Linux)  ORT_DYLIB_PATH="$(find vendor -name 'libonnxruntime*.so*' 2>/dev/null | head -1)" ;;
      Darwin) ORT_DYLIB_PATH="$(find vendor -name 'libonnxruntime*.dylib' 2>/dev/null | head -1)" ;;
    esac
    export ORT_DYLIB_PATH
  fi
  SMOKE_OUT=$(run_with_timeout 90 "$STATE_DIR/earshot-pinned" detector-smoke --config "$CONFIG" 2>&1 | tail -1)
  SMOKE_RC=$?
  if [[ $SMOKE_RC -eq 0 && "$SMOKE_OUT" == *"detector-smoke OK"* ]]; then
    check PASS "detector heartbeat: $SMOKE_OUT"
  elif [[ $SMOKE_RC -eq 124 ]]; then
    check FAIL "detector-smoke HUNG (>90 s) — dead/wrong-platform engine runtime (Ford deaf-gap class). ORT_DYLIB_PATH=${ORT_DYLIB_PATH:-unset}"
  else
    check FAIL "detector-smoke failed (exit $SMOKE_RC): $SMOKE_OUT"
  fi
  if [[ $CK_RC -eq 0 ]]; then
    check PASS "keywords compile (BPE-encodable) on the ARMED engine's vocab"
    # Short token targets are the acoustic analogue of the 2-character variant
    # problem that produced 38 false alarms in one event set. Warn, never fail —
    # dropping a keyword forfeits its market and that is a trading decision.
    SHORT=$(printf '%s\n' "$CK_OUT" | grep -oE "confirmed \[[^]]+\]: .* \(([12]) tokens\)" | sed -E 's/confirmed \[([^]]+)\].*\(([12]) tokens\)/\1 (\2 tokens)/' || true)
    if [[ -n "$SHORT" ]]; then
      check WARN "SHORT token targets — highest false-alarm risk, review before arming:"
      printf '%s\n' "$SHORT" | sed 's/^/        /'
    else
      check PASS "no 1-2 token keyword targets"
    fi
  else
    check FAIL "compile-keywords FAILED — config has detector-unencodable phrases; arm will refuse at fire time"
  fi
else
  check FAIL "cargo build --release FAILED — fix before event day"
fi

# ── Allowlist coverage ─────────────────────────────────────────────────────
# `arm` refuses a partial allowlist at start-up, but that fires at 8 AM on the
# call. Catch it the night before. The shape: `prepare`'s placeholder allowlist
# entry survives while [[keywords]] are filled in real — non-empty, so
# --i-am-live's emptiness check passes, the arm comes up clean, and then EVERY
# order is refused NotAllowlisted at fire time. All three configs staged for
# 2026-07-28/29 shipped exactly that way.
AL_OUT=$(python3 - "$CONFIG" <<'PY'
import re, sys
s = open(sys.argv[1]).read()
m = re.search(r'allowlist_tickers\s*=\s*\[(.*?)\]', s, re.S)
allow = set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()
kws = set(re.findall(r'market_ticker\s*=\s*"([^"]+)"', s))
if not kws:
    print("SKIP no keywords")
elif not allow:
    print(f"EMPTY {len(kws)}")
else:
    miss = sorted(kws - allow)
    print(f"MISS {len(miss)} {len(kws)} " + " ".join(miss[:5]) if miss else f"OK {len(kws)}")
PY
) || AL_OUT="ERR"
case "$AL_OUT" in
  OK*)    check PASS "[risk].allowlist_tickers covers all ${AL_OUT#OK } keyword markets" ;;
  EMPTY*) check FAIL "[risk].allowlist_tickers is EMPTY with ${AL_OUT#EMPTY } keywords — every order refused NotAllowlisted; paste prepare's [risk] block" ;;
  MISS*)  check FAIL "[risk].allowlist_tickers MISSES keyword markets: $AL_OUT — those orders are refused NotAllowlisted at fire time" ;;
  SKIP*)  check WARN "no [[keywords]] in config — allowlist coverage not checked" ;;
  *)      check FAIL "allowlist coverage check could not run ($AL_OUT)" ;;
esac

# ── Allowlist vs LIVE board (INDI class) ──────────────────────────────────
# The inverse direction of the check above: markets Kalshi lists for the event
# that the config never arms. WING 2026-07-29 shipped 13/14 (India missing);
# FED 2026-07-29 shipped 43/44 (FAMI opened after staging). Public endpoint,
# no creds; network failure is a WARN, a live-not-armed market is a FAIL.
if DIFF_OUT=$(bash "$(dirname "$0")/allowlist-diff.sh" "$CONFIG" 2>&1); then
  check PASS "allowlist matches the live Kalshi board ($(head -1 <<<"$DIFF_OUT"))"
else
  case $? in
    1) check FAIL "live board has markets this config does not arm: $(grep LIVE-NOT-ARMED <<<"$DIFF_OUT" | tr '\n' ' ')" ;;
    *) check WARN "allowlist-vs-live diff could not run: $(head -1 <<<"$DIFF_OUT")" ;;
  esac
fi

# ── Credentials (live probes, read-only) ───────────────────────────────────
if [[ -f "$ACCOUNT" ]]; then
  if [[ "$(uname -s)" == Darwin ]]; then
    perms=$(stat -f '%Lp' "$ACCOUNT")
  else
    perms=$(stat -c '%a' "$ACCOUNT")
  fi
  check PASS "account env exists: $ACCOUNT ($perms perms)"
  # shellcheck disable=SC1090
  source "$ACCOUNT"
  # Twilio is the PHONE lane. A webcast-only event has no phone leg, so absent
  # Twilio creds are correct there, not a failure — demand them only when the
  # config actually declares a twilio source.
  if grep -qE '^[[:space:]]*kind[[:space:]]*=[[:space:]]*"twilio' "$CONFIG" 2>/dev/null; then
    if [[ -n "${TWILIO_ACCOUNT_SID:-}" && -n "${TWILIO_AUTH_TOKEN:-}" ]]; then
      code=$(curl -s -o /dev/null -w '%{http_code}' \
        "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}.json" \
        -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}")
      [[ "$code" == 200 ]] && check PASS "Twilio auth probe: 200" \
                           || check FAIL "Twilio auth probe: HTTP $code"
    else
      check FAIL "config declares a twilio source but the account env lacks TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN"
    fi
  else
    check PASS "webcast-only config (no twilio source) — Twilio creds not required"
  fi
else
  check FAIL "account env missing: $ACCOUNT"
fi
kalshi_code=$(curl -s -o /dev/null -w '%{http_code}' "https://api.elections.kalshi.com/trade-api/v2/exchange/status")
[[ "$kalshi_code" == 200 ]] && check PASS "Kalshi API reachable (exchange/status 200)" \
                            || check FAIL "Kalshi API probe: HTTP $kalshi_code"

# ── ngrok agent (Darwin/CDMX only) ─────────────────────────────────────────
# Linux/VPS production is tunnel-free by design (docs/INGRESS-BAKEOFF.md §5:
# "No tunnels in production") — the WS endpoint terminates on the box.
if [[ "$(uname -s)" == Darwin ]]; then
  if ngrok config check >/dev/null 2>&1; then
    check PASS "ngrok agent config valid"
  else
    check FAIL "ngrok config check failed"
  fi
else
  check PASS "tunnel-free host (Linux) — direct WS endpoint; ngrok not used"
fi

# ── Timezone + scheduled wake ──────────────────────────────────────────────
tz=$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')
[[ "$tz" == "America/Mexico_City" ]] && check PASS "timezone: $tz" \
  || check WARN "timezone is '$tz', not America/Mexico_City — ALL window times (launchd fire, END_HHMM) shift with local time; re-verify the schedule"
if [[ -n "$FIRE_HHMM" ]]; then
  if [[ "$(uname -s)" == Darwin ]]; then
    # Only USER-scheduled wakes count — macOS system alarms (by 'com.apple…')
    # come and go and must not satisfy this check.
    user_wake=$(pmset -g sched 2>/dev/null | grep -i "wake at" | grep -v "by 'com.apple" | head -1 | tr -s ' ')
    if [[ -n "$user_wake" ]]; then
      check PASS "pmset user-scheduled wake: $user_wake — VERIFY it is before fire time $FIRE_HHMM"
    else
      check FAIL "NO user-scheduled pmset wake — a closed lid at fire time defers launchd past the window. Set one: sudo pmset schedule wake \"MM/DD/YY HH:MM:SS\""
    fi
  else
    # Linux server: no sleep/wake concept — the fire mechanism is a systemd
    # user timer (scripts/install-event-timer.sh). Verify it is armed.
    ev_label=""
    if [[ -n "$EVENT_ENV" && -f "$EVENT_ENV" ]]; then
      ev_label=$(source "$EVENT_ENV" 2>/dev/null; echo "${LABEL:-}")
    fi
    if [[ -n "$ev_label" ]]; then
      next=$(systemctl --user list-timers --all 2>/dev/null | grep -F "$ev_label.timer" | head -1 | tr -s ' ')
      if [[ -n "$next" ]]; then
        check PASS "systemd timer armed: $next — VERIFY NEXT is the event fire time ($FIRE_HHMM box-local)"
      else
        check FAIL "NO systemd user timer '$ev_label.timer' — nothing will fire. Arm it: scripts/install-event-timer.sh --event-env $EVENT_ENV --fire-hhmm $FIRE_HHMM --date <YYYY-MM-DD>"
      fi
    else
      check WARN "fire-time check skipped: no --event-env (or no LABEL in it) — cannot name the systemd timer to verify"
    fi
  fi
fi
if [[ "$(uname -s)" == Darwin ]]; then
  check WARN "power: $(pmset -g batt | tr '\n' ' ' | tr -s ' ') — must be AC + lid open at fire time"
fi

# ── CPU load ───────────────────────────────────────────────────────────────
# 2026-07-17: unrelated batch jobs pushed this box to load 82 — network egress
# failed outright and every process crawled. The pipeline's latency budgets
# (chunk→detect p90 ~25 ms) were measured on a quiet box; a saturated one is a
# different machine than the one that rehearsed.
if [[ "$(uname -s)" == Darwin ]]; then
  ncpu=$(sysctl -n hw.ncpu)
  load1=$(sysctl -n vm.loadavg | awk '{print $2}')
else
  ncpu=$(nproc)
  load1=$(awk '{print $1}' /proc/loadavg)
fi
if python3 -c "exit(0 if float('$load1') > $ncpu*2 else 1)"; then
  check FAIL "load average $load1 with $ncpu cores — box is saturated; whatever is running must be done or stopped before the event"
elif python3 -c "exit(0 if float('$load1') > $ncpu*0.75 else 1)"; then
  check WARN "load average $load1 with $ncpu cores — something heavy is running; confirm it finishes before fire time"
else
  check PASS "load average $load1 ($ncpu cores)"
fi

# ── Dial-in go/no-go gate ──────────────────────────────────────────────────
# The night-before FAIL-STOP promised by configs/<event>-autorun.env ("the
# preflight must FAIL-STOP if these are still TBD"). Until 2026-07-16 that gate
# did not exist: the ONLY placeholder check lived in event-autorun.sh, and it
# fires at 06:05 on call morning — hours after the go/no-go decision, with no
# time left to hunt a number. The promise read as covered until the morning it
# wasn't. This is that gate, at the hour it was meant to run.
if [[ -n "$EVENT_ENV" ]]; then
  if [[ -f "$EVENT_ENV" ]]; then
    # Subshell: never leak event vars into the rest of the preflight.
    DIAL_STATUS=$(
      # shellcheck disable=SC1090
      source "$EVENT_ENV" 2>/dev/null
      if [[ "${TO_NUMBER:-TBD}" == *TBD* || "${PASSCODE:-}" == *TBD* ]]; then
        echo "TBD:${TO_NUMBER:-unset}/${PASSCODE:-unset}"
      else
        echo "OK:${TO_NUMBER}"
      fi
    )
    if [[ "$DIAL_STATUS" == TBD:* ]]; then
      check FAIL "dial-in still TBD in $EVENT_ENV (${DIAL_STATUS#TBD:}) — NO-GO. The number hunt has not completed; event-autorun would abort at fire time. See docs/RUNBOOK.md Track A."
    else
      check PASS "dial-in resolved in $EVENT_ENV (${DIAL_STATUS#OK:})"
    fi
  else
    check FAIL "event env not found: $EVENT_ENV"
  fi
else
  # Not passing --event-env is itself a risk: it silently skips the gate.
  check WARN "no --event-env given — dial-in go/no-go gate SKIPPED. Pass --event-env configs/<event>-autorun.env to enforce it."
fi

# ── Mailer live test ───────────────────────────────────────────────────────
BODY=$(printf '%s\n' "${REPORT_LINES[@]}")
if [[ -f "$HOME/.kalshi/mail_env" ]]; then
  if (
    # shellcheck disable=SC1090
    source "$HOME/.kalshi/mail_env"
    export MAIL_USER MAIL_APP_PASSWORD MAIL_TO
    MAIL_SUBJECT="EARSHOT preflight ${EVENT_ID:-?}: $([[ $FAILS -eq 0 ]] && echo PASS || echo "$FAILS FAIL")" \
    MAIL_BODY="$BODY" python3 - <<'PY'
import os, smtplib, ssl
from email.message import EmailMessage
msg = EmailMessage()
msg["From"] = os.environ["MAIL_USER"]; msg["To"] = os.environ["MAIL_TO"]
msg["Subject"] = os.environ["MAIL_SUBJECT"]
msg.set_content(os.environ["MAIL_BODY"])
with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
    s.login(os.environ["MAIL_USER"], os.environ["MAIL_APP_PASSWORD"])
    s.send_message(msg)
PY
  ); then
    check PASS "mailer live test: preflight email sent"
  else
    check FAIL "mailer live test FAILED — the unattended run cannot alert"
  fi
else
  check FAIL "mail env missing: ~/.kalshi/mail_env"
fi

echo
if [[ "$FAILS" -eq 0 ]]; then
  echo "PREFLIGHT PASS — pinned binary + environment verified for ${EVENT_ID:-?}"
else
  echo "PREFLIGHT: $FAILS FAILURE(S) — fix before the event" >&2
  exit 1
fi
