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

CONFIG="" ACCOUNT="" FIRE_HHMM=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)    CONFIG="$2"; shift 2 ;;
    --account)   ACCOUNT="$2"; shift 2 ;;
    --fire-hhmm) FIRE_HHMM="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$CONFIG" && -n "$ACCOUNT" ]] || {
  echo "usage: $0 --config <cfg.toml> --account <env-file> [--fire-hhmm 0605]" >&2; exit 2
}

FAILS=0
REPORT_LINES=()
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

# ── Tools the launch chain shells out to ───────────────────────────────────
for tool in ngrok curl nc lsof python3 git shasum; do
  if command -v "$tool" >/dev/null 2>&1; then
    check PASS "tool: $tool ($(command -v "$tool"))"
  else
    check FAIL "tool MISSING: $tool"
  fi
done

# ── Disk ───────────────────────────────────────────────────────────────────
avail_gb=$(df -g / | awk 'NR==2 {print $4}')
if [[ "${avail_gb:-0}" -ge 5 ]]; then
  check PASS "disk: ${avail_gb} GiB free (>= 5)"
else
  check FAIL "disk: only ${avail_gb:-?} GiB free (< 5) — free space before event day (target/debug is safe to delete; NEVER target/release)"
fi

# ── Build + pin ────────────────────────────────────────────────────────────
dirty=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
[[ "$dirty" == 0 ]] && check PASS "git tree clean at $(git rev-parse --short HEAD)" \
                    || check WARN "git tree has $dirty dirty paths — the pin records what is BUILT, review before trusting"
if cargo build --release 2>&1 | tail -1 | grep -q "Finished"; then
  cp target/release/earshot "$STATE_DIR/earshot-pinned"
  {
    echo "commit=$(git rev-parse HEAD 2>/dev/null)"
    echo "dirty_paths=$dirty"
    echo "sha256=$(shasum -a 256 "$STATE_DIR/earshot-pinned" | awk '{print $1}')"
    echo "built=$(date "+%Y-%m-%d %H:%M:%S %z")"
  } > "$STATE_DIR/pinned.info"
  check PASS "pinned binary: $STATE_DIR/earshot-pinned ($(grep sha256 "$STATE_DIR/pinned.info" | cut -c1-23)…)"
  # Follow-up 6 gate: arm refuses the WHOLE config over one detector-
  # unencodable variant. Exercise the exact encode path with the pin (offline).
  if "$STATE_DIR/earshot-pinned" compile-keywords --config "$CONFIG" >/dev/null 2>&1; then
    check PASS "keywords compile (BPE-encodable) against the pinned binary"
  else
    check FAIL "compile-keywords FAILED — config has detector-unencodable phrases; arm will refuse at fire time"
  fi
else
  check FAIL "cargo build --release FAILED — fix before event day"
fi

# ── Credentials (live probes, read-only) ───────────────────────────────────
if [[ -f "$ACCOUNT" ]]; then
  check PASS "account env exists: $ACCOUNT ($(stat -f '%Lp' "$ACCOUNT") perms)"
  # shellcheck disable=SC1090
  source "$ACCOUNT"
  if [[ -n "${TWILIO_ACCOUNT_SID:-}" && -n "${TWILIO_AUTH_TOKEN:-}" ]]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' \
      "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}.json" \
      -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}")
    [[ "$code" == 200 ]] && check PASS "Twilio auth probe: 200" \
                         || check FAIL "Twilio auth probe: HTTP $code"
  else
    check FAIL "account env lacks TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN"
  fi
else
  check FAIL "account env missing: $ACCOUNT"
fi
kalshi_code=$(curl -s -o /dev/null -w '%{http_code}' "https://api.elections.kalshi.com/trade-api/v2/exchange/status")
[[ "$kalshi_code" == 200 ]] && check PASS "Kalshi API reachable (exchange/status 200)" \
                            || check FAIL "Kalshi API probe: HTTP $kalshi_code"

# ── ngrok agent ────────────────────────────────────────────────────────────
if ngrok config check >/dev/null 2>&1; then
  check PASS "ngrok agent config valid"
else
  check FAIL "ngrok config check failed"
fi

# ── Timezone + scheduled wake ──────────────────────────────────────────────
tz=$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')
[[ "$tz" == "America/Mexico_City" ]] && check PASS "timezone: $tz" \
  || check WARN "timezone is '$tz', not America/Mexico_City — ALL window times (launchd fire, END_HHMM) shift with local time; re-verify the schedule"
if [[ -n "$FIRE_HHMM" ]]; then
  # Only USER-scheduled wakes count — macOS system alarms (by 'com.apple…')
  # come and go and must not satisfy this check.
  user_wake=$(pmset -g sched 2>/dev/null | grep -i "wake at" | grep -v "by 'com.apple" | head -1 | tr -s ' ')
  if [[ -n "$user_wake" ]]; then
    check PASS "pmset user-scheduled wake: $user_wake — VERIFY it is before fire time $FIRE_HHMM"
  else
    check FAIL "NO user-scheduled pmset wake — a closed lid at fire time defers launchd past the window. Set one: sudo pmset schedule wake \"MM/DD/YY HH:MM:SS\""
  fi
fi
check WARN "power: $(pmset -g batt | tr '\n' ' ' | tr -s ' ') — must be AC + lid open at fire time"

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
