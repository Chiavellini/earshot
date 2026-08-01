#!/usr/bin/env bash
# Intraday listed-late watcher (W1.2) — the fast, alert-only half of the slate.
#
# The once-daily sweep (slate-daily.sh, 02:00Z-ish) leaves an all-day blind
# window between "staging finished" and "call time": anything Kalshi lists
# AFTER the sweep is invisible until tomorrow. That window has already cost
# two races — WING-INDI (2026-07-29: the India market was listed late and
# never armed; its first-mention pump ran with no possible fire) and FED-FAMI
# (event listed after staging; nobody was watching). This script closes the
# window by re-sweeping cheaply every couple of hours and screaming when the
# live board has grown past what we committed/armed.
#
# ALERT-ONLY, by design: this script NEVER commits, NEVER writes
# slate/latest.json, NEVER edits a config. The daily job owns the artifact;
# this one only tells a human (or the autorun lane) that the artifact is
# already stale. Fixing is one command per finding and is printed with it.
#
# Checks (all today/tomorrow bucketing is UTC, never host-local):
#   1. discover-events.py --days 3 --no-ir-check → TEMP json (mktemp). The
#      IR-page confirmation is skipped on purpose: this runs every 2h and the
#      issuer fetches are the slow, geo-blocked part — Kalshi metadata at
#      face value is good enough to raise a hand. Its stderr is scanned for
#      the "! <series>" swallowed per-series-failure marker; any hits →
#      SWEEP-DEGRADED-LOCAL: line in the same mail (a shrunken sweep must
#      not read as a clean board).
#   2. Any event in the temp sweep but NOT in the committed slate
#      (git show HEAD:slate/latest.json) with call_date <= 2 days out
#      → SAMEDAY-ADDED: line + one aggregate mail. A missing baseline is
#      itself an alert: NO-COMMITTED-SLATE: goes in the mail (exit 1),
#      with "path absent in HEAD" worded differently from other git-show
#      failures.
#   3. Every configs/{webcast,twilio}-*-arm*.toml whose [event].starts_at is
#      today or tomorrow gets scripts/allowlist-diff.sh, passing the REAL
#      event ticker (lane suffixes -WEBCAST/-CAPTURE/-CAPT stripped from the
#      synthetic [event].id of -wc configs); LIVE-NOT-ARMED
#      drift (the exact INDI class) → LIVE-NOT-ARMED: line + the same mail.
#      Fanout TEMPLATE configs (starts_at still a __PLACEHOLDER__) are
#      skipped with a stdout-only TEMPLATE-SKIPPED: line — never mailed.
#   4. Every failure path alerts (fail() → notify.sh) — a silently dead
#      watcher is the failure mode this replaces, same as slate-daily.
#
# Intended schedule (documented here, INSTALLED BY NOTHING in this draft —
# the systemd unit / launchd plist are a separate change):
#   box (Linux):  systemd user timer, every 2h across the listing day,
#                 OnCalendar=*-*-* 10/2:00:00 UTC   (10,12,…,22Z + a 23Z cap)
#   laptop (mac): launchd StartCalendarInterval ARRAY covering the same
#                 10–23Z band in local hours (CDMX = UTC-6 → 04..17 local),
#                 missed-while-asleep firings run on wake like slate-daily.
#
# Manual: scripts/slate-intraday.sh [--dry-run]
#   --dry-run prints every would-be alert, mails nothing. Any other
#   argument is rejected: usage on stderr + exit 2.
# Exit codes: 0 clean, 1 at least one alert fired, 2 crash (a step died)
#   or bad usage.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
usage() { echo "usage: scripts/slate-intraday.sh [--dry-run]" >&2; exit 2; }
[ $# -le 1 ] || usage
DRY=0
case "${1:-}" in
  "") ;;
  --dry-run) DRY=1 ;;
  *) usage ;;
esac
TODAY="$(date -u +%F)"

alert() {
  if [ "$DRY" = 1 ]; then echo "[dry-run] ALERT: $1"; echo "$2" | sed 's/^/[dry-run]   /'
  else scripts/notify.sh "$1" "$2"; fi
}
fail() { alert "slate-intraday FAILED" "$1"; echo "FAIL: $1" >&2; exit 2; }

TMPJSON="$(mktemp)"; PREV="$(mktemp)"; SWEEPERR="$(mktemp)"
trap 'rm -f "$TMPJSON" "$PREV" "$SWEEPERR"' EXIT

# ── 1. cheap 3-day sweep into a temp file (never slate/latest.json) ─────────
# stderr → temp file so the "  ! <series>: <err>" swallowed per-series-failure
# markers are countable; replayed to our stderr so the run log keeps them.
python3 scripts/discover-events.py --json "$TMPJSON" --days 3 --no-ir-check 2> "$SWEEPERR" \
  || { RC=$?; cat "$SWEEPERR" >&2
       fail "discover-events.py intraday sweep exited $RC — the watcher is blind"; }
cat "$SWEEPERR" >&2
NSWEEPFAIL="$(grep -c '^[[:space:]]*! ' "$SWEEPERR" || true)"
SWEEP_DEGRADED=""
if [ "${NSWEEPFAIL:-0}" -gt 0 ]; then
  SWEEP_DEGRADED="SWEEP-DEGRADED-LOCAL: $NSWEEPFAIL series queries failed inside the sweep — board may be under-counted"
  echo "$SWEEP_DEGRADED"
fi

# Committed slate = what staging worked from. A missing/unreadable baseline
# is itself an ALERT (not just stdout): every near event then reads as
# listed-late, which is exactly the truth of that situation — the run must
# not print OK / exit 0 on top of a missing baseline.
SLATE_BASELINE=""
GIT_ERR="$(git show HEAD:slate/latest.json 2>&1 > "$PREV")"; GIT_RC=$?
if [ "$GIT_RC" -ne 0 ]; then
  : > "$PREV"
  case "$GIT_ERR" in
    *"does not exist"*|*"exists on disk, but not in"*)
      SLATE_BASELINE="NO-COMMITTED-SLATE: HEAD has no slate/latest.json — treating every near event as listed-late" ;;
    *)
      SLATE_BASELINE="NO-COMMITTED-SLATE: git show HEAD:slate/latest.json failed (exit $GIT_RC): $(head -1 <<<"$GIT_ERR") — baseline unreadable, treating every near event as listed-late" ;;
  esac
  echo "$SLATE_BASELINE"
fi

# ── 2. temp sweep vs committed slate, call_date <= 2 days out ───────────────
SAMEDAY="$(TMPJSON="$TMPJSON" PREV="$PREV" python3 - <<'PY'
import datetime, json, os, sys
new = json.load(open(os.environ["TMPJSON"]))
prev_raw = open(os.environ["PREV"]).read().strip()
committed = set()
if prev_raw:
    committed = {e["event_ticker"] for e in json.loads(prev_raw).get("events", [])}
today = datetime.datetime.now(datetime.timezone.utc).date()
horizon = today + datetime.timedelta(days=2)
for e in new.get("events", []):
    tk = e["event_ticker"]
    if tk in committed:
        continue
    cd = e.get("call_date")
    if not cd:
        # Unparsed date on a listed-late event: could be in an hour. Loud, not silent.
        print(f"SAMEDAY-ADDED: {tk} {e.get('issuer')} UNRESOLVED-DATE (sub_title did not parse — assume near)")
        continue
    try:
        d = datetime.date.fromisoformat(cd)
    except ValueError:
        print(f"SAMEDAY-ADDED: {tk} {e.get('issuer')} BAD-DATE:{cd}")
        continue
    if d <= horizon:
        print(f"SAMEDAY-ADDED: {tk} {e.get('issuer')} {cd}")
PY
)" || fail "listed-late diff analysis crashed"

# ── 3. INDI-class drift on every config armed for today/tomorrow ────────────
# starts_at parsed the way preflight-event.sh parses [event].id (section-
# scoped sed); date math via python3 (UTC — `date -d` differs on macOS and
# host-local days drift off the listing day).
DRIFT=""
CHECK_FAILS=""
shopt -s nullglob
for CFG in configs/webcast-*-arm*.toml configs/twilio-*-arm*.toml; do
  STARTS_AT="$(sed -n '/^\[event\]/,/^\[/s/^starts_at[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CFG" | head -1)"
  case "$STARTS_AT" in *__*)
    # Fanout TEMPLATE config: starts_at is a __PLACEHOLDER__ until stamped.
    # stdout only — a template is not drift and must never reach the mail.
    echo "TEMPLATE-SKIPPED: $CFG (starts_at placeholder \"$STARTS_AT\")"
    continue ;;
  esac
  if [ -z "$STARTS_AT" ]; then
    CHECK_FAILS+="NO-STARTS-AT: $CFG — [event].starts_at missing/unparseable; cannot tell if it fires today"$'\n'
    continue
  fi
  REL="$(python3 -c 'import datetime,sys
d = datetime.date.fromisoformat(sys.argv[1][:10])
print((d - datetime.datetime.now(datetime.timezone.utc).date()).days)' "$STARTS_AT" 2>/dev/null)" || {
    CHECK_FAILS+="BAD-STARTS-AT: $CFG — starts_at \"$STARTS_AT\" not ISO; cannot tell if it fires today"$'\n'
    continue
  }
  { [ "$REL" = "0" ] || [ "$REL" = "1" ]; } || continue   # not today/tomorrow

  EV="$(sed -n '/^\[event\]/,/^\[/s/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CFG" | head -1)"
  # -wc lane configs carry a synthetic [event].id (…-WEBCAST/-CAPTURE/-CAPT);
  # Kalshi only knows the real ticker, so strip the lane suffix and pass it
  # explicitly (allowlist-diff.sh optional arg 2).
  EV_REAL="${EV%-WEBCAST}"; EV_REAL="${EV_REAL%-CAPTURE}"; EV_REAL="${EV_REAL%-CAPT}"
  AD_OUT="$(scripts/allowlist-diff.sh "$CFG" "$EV_REAL" 2>&1)"; AD_RC=$?
  case "$AD_RC" in
    0) echo "OK: $CFG allowlist matches live board (${EV_REAL:-?})" ;;
    1) DETAILS="$(grep 'LIVE-NOT-ARMED:' <<<"$AD_OUT" \
         | sed 's/^ *LIVE-NOT-ARMED: *//; s/ *<- INDI class//' | paste -sd ',' -)"
       DRIFT+="LIVE-NOT-ARMED: $CFG — $DETAILS; re-prepare: earshot prepare --event-ticker ${EV_REAL:-<EV>}"$'\n' ;;
    *) CHECK_FAILS+="ALLOWLIST-CHECK-FAILED: $CFG (exit $AD_RC) — $(tail -1 <<<"$AD_OUT")"$'\n' ;;
  esac
done

# ── 4. one mail aggregating everything ──────────────────────────────────────
ALERT_BODY=""
[ -n "$SWEEP_DEGRADED" ] && ALERT_BODY+="$SWEEP_DEGRADED"$'\n'
[ -n "$SLATE_BASELINE" ] && ALERT_BODY+="$SLATE_BASELINE"$'\n'
[ -n "$SAMEDAY" ]     && ALERT_BODY+="$SAMEDAY"$'\n'
[ -n "$DRIFT" ]       && ALERT_BODY+="$DRIFT"
[ -n "$CHECK_FAILS" ] && ALERT_BODY+="$CHECK_FAILS"
if [ -n "$ALERT_BODY" ]; then
  echo "$ALERT_BODY"
  alert "slate-intraday $TODAY: listed-late action needed" "$ALERT_BODY"
  exit 1
fi
echo "OK: slate-intraday $TODAY clean — no listed-late events, no allowlist drift"
exit 0
