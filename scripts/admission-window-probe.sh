#!/usr/bin/env bash
# Probe a bridge that only EXISTS in its event window (VERIFICATIONS Q-ADMIT
# addendum, 2026-07-30).
#
# Most venues answer whenever you call. A reservation-window venue does not:
# AAPL's published toll-free rang 55 s, returned Twilio status=no-answer and
# zero media at 03:40 UTC, with numbers and code byte-identical in Wayback
# since 2024. The bridge was not dead — it was not yet provisioned. For this
# class an off-hours probe cannot classify admission at ALL, and a "no-answer"
# is not evidence of anything.
#
# The only probe that decides them runs T-90..T-30 before the call. That was
# prose in a pin and a to-do in a plan doc; this is the executable form.
#
#   admission-window-probe.sh --config configs/twilio-aapl-...toml
#       plan only (DEFAULT): print the window and the exact command. NO CALL.
#   admission-window-probe.sh --config <cfg> --run
#       place the probe — REFUSED outside the window, because a call outside it
#       produces a no-answer that means nothing and costs a real dial
#   admission-window-probe.sh --config <cfg> --install-timer
#       PRINT (never execute) the scheduler line, per slate-stage.sh convention
#
# Numbers come from control.bridge.numbers (scripts/hunt-dial-in.py
# --emit-numbers), never from the registry: digits rotate and are gitignored.
set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG="" NUMBERS_FILE="state/hunt/numbers.env" RUN=0 INSTALL_TIMER=0
SECS=120 LEAD_OPEN_MIN=90 LEAD_CLOSE_MIN=30
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)        CONFIG="$2"; shift 2 ;;
    --numbers)       NUMBERS_FILE="$2"; shift 2 ;;
    --run)           RUN=1; shift ;;
    --install-timer) INSTALL_TIMER=1; shift ;;
    --secs)          SECS="$2"; shift 2 ;;
    --open-min)      LEAD_OPEN_MIN="$2"; shift 2 ;;
    --close-min)     LEAD_CLOSE_MIN="$2"; shift 2 ;;
    -h|--help)       sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$CONFIG" && -f "$CONFIG" ]] || { echo "need --config <event toml>" >&2; exit 2; }

# Derive the venue key and call time from the event config itself, so the probe
# can never drift from the arm it is meant to clear.
# Command substitution, NOT `read < <(...)`: bash 3.2 cannot parse a heredoc
# inside a process substitution, and this may be run under /bin/bash.
VENUE_LINE=$(CONFIG="$CONFIG" python3 - <<'PY'
import os, re, sys, tomllib
try:
    with open(os.environ["CONFIG"], "rb") as f:
        cfg = tomllib.load(f)
except Exception as e:
    print(f"ERR unreadable-config:{e}", file=sys.stderr); sys.exit(2)
ev = cfg.get("event", {})
m = re.match(r"KXEARNINGSMENTION([A-Z0-9]+)", str(ev.get("id", "")))
if not m:
    print(f"ERR no-series-key-in:{ev.get('id')!r}", file=sys.stderr); sys.exit(2)
starts = ev.get("starts_at")
if not starts:
    print("ERR no-starts_at", file=sys.stderr); sys.exit(2)
print(m.group(1), starts)
PY
) || { echo "cannot derive venue/time from $CONFIG" >&2; exit 2; }
KEY=$(echo "$VENUE_LINE" | awk '{print $1}')
STARTS_AT=$(echo "$VENUE_LINE" | awk '{print $2}')

# Window arithmetic in Python: `date -d` is GNU-only and this script has to run
# on the macOS laptop and the Linux box alike.
WINDOW_LINE=$(
  STARTS_AT="$STARTS_AT" OPEN_MIN="$LEAD_OPEN_MIN" CLOSE_MIN="$LEAD_CLOSE_MIN" python3 - <<'PY'
import datetime, os, sys
try:
    call = datetime.datetime.fromisoformat(os.environ["STARTS_AT"].replace("Z", "+00:00"))
except ValueError as e:
    print(f"ERR bad-starts_at:{e}", file=sys.stderr); sys.exit(2)
if call.tzinfo is None:
    call = call.replace(tzinfo=datetime.timezone.utc)
opens = call - datetime.timedelta(minutes=int(os.environ["OPEN_MIN"]))
closes = call - datetime.timedelta(minutes=int(os.environ["CLOSE_MIN"]))
now = datetime.datetime.now(datetime.timezone.utc)
state = "BEFORE" if now < opens else ("OPEN" if now <= closes else "AFTER")
fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
print(fmt(opens), fmt(closes), fmt(now), state)
PY
) || exit 2
OPEN_UTC=$(echo "$WINDOW_LINE"  | awk '{print $1}')
CLOSE_UTC=$(echo "$WINDOW_LINE" | awk '{print $2}')
NOW_UTC=$(echo "$WINDOW_LINE"   | awk '{print $3}')
STATE=$(echo "$WINDOW_LINE"     | awk '{print $4}')

echo "venue:  $KEY"
echo "call:   $STARTS_AT"
echo "window: $OPEN_UTC .. $CLOSE_UTC  (T-${LEAD_OPEN_MIN}m .. T-${LEAD_CLOSE_MIN}m)"
echo "now:    $NOW_UTC  → $STATE"

# Resolve the number. Absent is a first-class outcome, not an error: it means
# the hunt has not found this venue's bridge, which is its own finding.
TO="" DTMF=""
if [[ -f "$NUMBERS_FILE" ]]; then
  LINE=$(grep -E "^${KEY}=" "$NUMBERS_FILE" | head -1 || true)
  if [[ -n "$LINE" ]]; then
    VALUE="${LINE#*=}"
    TO="${VALUE%%,*}"
    [[ "$VALUE" == *,* ]] && DTMF="${VALUE#*,}"
  fi
fi
if [[ -z "$TO" ]]; then
  echo "number: NONE for $KEY in $NUMBERS_FILE"
  echo
  echo "NO-NUMBER: nothing to probe. Find one first:"
  echo "  python3 scripts/hunt-dial-in.py --key $KEY && python3 scripts/hunt-dial-in.py --emit-numbers"
  echo "If the hunt reports tier=unknown or bridge_unpublished, this venue has"
  echo "no ISSUER-PUBLISHED path and needs the browser/IR route, not a dial."
  exit 3
fi
# Never echo the digits themselves: this output lands in launchd logs and mail.
if [[ -n "$DTMF" ]]; then HAS_CODE="passcode present"; else HAS_CODE="no passcode"; fi
echo "number: on file (${#TO} chars, $HAS_CODE)"

LABEL="$(echo "$KEY" | tr 'A-Z' 'a-z')-$(echo "$STARTS_AT" | cut -c1-10)"
CMD=(scripts/bridge-probe.sh --to "\$TO" --label "$LABEL" --secs "$SECS")
[[ -n "$DTMF" ]] && CMD+=(--dtmf "\$DTMF")

if [[ "$INSTALL_TIMER" == "1" ]]; then
  # Printed, never executed — same posture as slate-stage.sh's TIMER-INSTALL.
  FIRE_DATE="${OPEN_UTC%%T*}"
  FIRE_HHMM="$(echo "$OPEN_UTC" | cut -c12-16 | tr -d ':')"
  echo
  echo "ADMIT-TIMER: fire at $OPEN_UTC (UTC $FIRE_DATE $FIRE_HHMM) —"
  echo "  cd ~/ear && scripts/admission-window-probe.sh --config $CONFIG --run"
  echo "(printed, not installed: a scheduled real phone call is a human decision)"
fi

if [[ "$RUN" != "1" ]]; then
  echo
  echo "plan only — no call placed. To probe inside the window:"
  echo "  scripts/admission-window-probe.sh --config $CONFIG --run"
  echo "Equivalent direct call (number read from $NUMBERS_FILE):"
  echo "  ${CMD[*]}"
  exit 0
fi

if [[ "$STATE" != "OPEN" ]]; then
  # Refusing is the whole point. A no-answer outside the window is not evidence
  # the bridge is dead, and recording it as `dropped` would poison the registry.
  echo
  echo "REFUSED: window is $STATE, not OPEN." >&2
  if [[ "$STATE" == "BEFORE" ]]; then
    echo "The reservation is not provisioned yet — a call now returns a" >&2
    echo "no-answer that classifies nothing. Wait for $OPEN_UTC." >&2
  else
    echo "The window has closed ($CLOSE_UTC); at T-${LEAD_CLOSE_MIN}m the bridge" >&2
    echo "is filling with real participants and a probe risks the live arm." >&2
  fi
  exit 4
fi

echo
echo "probing inside the window — one call, ~\$0.02, listen-only"
exec scripts/bridge-probe.sh --to "$TO" --label "$LABEL" --secs "$SECS" \
  ${DTMF:+--dtmf "$DTMF"}
