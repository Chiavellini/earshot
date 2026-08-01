#!/usr/bin/env bash
# url-escalate.sh — loud escalation ladder for a manual webcast-URL
# browser-read (W2.3).
#
# WHY THIS EXISTS: the 2026-07-30 RACE event was LOST because its required
# morning browser-read (a human opens the IR/webcast page and pastes the
# stream URL into a file) had no human awake and NO ESCALATION — nothing
# paged anyone before the deadline, and the miss surfaced only after the
# call. Same silent-miss class as the 2026-07-28 PYPL/BA morning (see the
# scripts/notify.sh header). This script makes that read impossible to miss
# quietly: it pages at T-60/T-30/T-15 before the URL deadline with the EXACT
# file to write and the one-line browser recipe, and pages a final
# "MISSED — event will not arm" if the deadline passes with no URL. Every
# outcome is a page and/or a grep-able UPPERCASE tag; there is no silent
# path out of this script.
#
# Usage:
#   scripts/url-escalate.sh --slug <slug> --url-file <path> \
#       --deadline-utc <HH:MM> [--ladder 60,30,15] \
#       [--instructions <one-line browser-read recipe>]
#
# Semantics:
#   * --deadline-utc HH:MM means the NEXT occurrence of that UTC wall time:
#     started the night before a morning deadline it resolves to tomorrow
#     (DEADLINE-ROLLED is printed when that happens). The resolved absolute
#     deadline always prints as DEADLINE-RESOLVED — verify it in the log.
#   * Ladder tiers whose fire time is already past at start are skipped, not
#     fired late (TIER-SKIPPED) — mirrors arm-when-url.sh's wait-then-alert.
#   * Idempotent / safe to re-run: sent tiers are recorded in
#     <url-file>.escalate.state, keyed by the resolved deadline epoch, and
#     never re-sent. A page that failed on EVERY notify channel is NOT
#     recorded, so it retries on the next poll and prints NOTIFY-FAILED
#     each attempt.
#   * READY  → "URL-READY: <slug> <url>", exit 0 (file exists with a
#              non-empty first line; URL-SUSPECT warns if it does not look
#              like http(s), but readiness is the file contract, not ours).
#   * MISSED → final page + "URL-MISSED: <slug>", exit 1.
#   * Pages are deliberately NOT --quiet: a human must act on them.
#   * Poll cadence 20s (env override URL_ESCALATE_POLL_SECS for tests).
#
#   * A url-file that exists but whose first line is empty/whitespace prints
#     URL-FILE-BLANK-LINE once per state change (not every poll) so the
#     paged human can see why READY never fires; it is still not READY.
#   * Any UNDOCUMENTED nonzero exit (crash, unhandled error) prints
#     WATCHER-DIED and best-effort pages before dying — a dead watcher must
#     never be silent. Documented exits (0 URL-READY, 1 URL-MISSED, 2 usage,
#     130/143 signals) do not trigger it. TIER-SENT prints ONLY when a page
#     was actually delivered; a dedup skip prints TIER-DEDUP, so
#     `grep -c TIER-SENT` equals real pages.
#
# Grep-able tags: DEADLINE-RESOLVED, DEADLINE-ROLLED, TIER-SENT, TIER-DEDUP,
#   TIER-SKIPPED, NOTIFY-FAILED, URL-READY, URL-SUSPECT, URL-MISSED,
#   URL-FILE-BLANK-LINE, PRIOR-MISS, WATCHER-KILLED, WATCHER-DIED,
#   USAGE-ERROR, NOTIFY-MISSING, STATE-UNWRITABLE.
#
# Portability: runs on both the laptop (macOS, bash 3.2) and the box
# (Linux). No mapfile, no associative arrays, no `date -d` — all date math
# goes through python3.
set -Eeuo pipefail

# No silent deaths — this is the same silent-miss class the whole script
# exists to kill. Any exit that is NOT one of the documented outcomes
# (0 URL-READY, 1 URL-MISSED, 2 usage, 130/143 signals — those set DONE=1
# or carry a signal rc) prints WATCHER-DIED and best-effort pages.
# Installed before arg parsing and before any python3 call on purpose.
DONE=0
DIED_LINE=""
on_unexpected_exit() {
  local rc=$?
  if [[ "$rc" -eq 0 || "$rc" -eq 130 || "$rc" -eq 143 || "${DONE:-0}" -eq 1 ]]; then
    return 0
  fi
  local slug="${SLUG:-}"
  [[ -n "$slug" ]] || slug="unparsed"
  echo "WATCHER-DIED: $slug rc=$rc line=${DIED_LINE:-?}" >&2
  local n="${NOTIFY:-}"
  if [[ -n "$n" && -x "$n" ]]; then
    "$n" "URL-ESCALATE WATCHER DIED ($slug)" \
"url-escalate.sh died unexpectedly (rc=$rc line=${DIED_LINE:-?}) while watching ${URLF:-<unset>}.
The escalation ladder for $slug is NO LONGER RUNNING — restart it or do the browser-read now." \
      || echo "NOTIFY-FAILED: watcher-died page failed on EVERY channel" >&2
  fi
}
trap 'DIED_LINE=$LINENO' ERR
trap on_unexpected_exit EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="$SCRIPT_DIR/notify.sh"
POLL_SECS="${URL_ESCALATE_POLL_SECS:-20}"

SLUG="" URLF="" DEADLINE_HHMM=""
LADDER="60,30,15"
INSTR="Open the event's webcast/IR page in a browser, start the player, copy the media URL (.m3u8) from DevTools > Network, and paste it as the first line of the url-file."

usage() {
  echo "usage: url-escalate.sh --slug <slug> --url-file <path> --deadline-utc <HH:MM> [--ladder 60,30,15] [--instructions <text>]"
}
die_usage() { DONE=1; echo "USAGE-ERROR: $*" >&2; usage >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slug)         [[ $# -ge 2 ]] || die_usage "--slug needs a value";         SLUG="$2"; shift 2 ;;
    --url-file)     [[ $# -ge 2 ]] || die_usage "--url-file needs a value";     URLF="$2"; shift 2 ;;
    --deadline-utc) [[ $# -ge 2 ]] || die_usage "--deadline-utc needs a value"; DEADLINE_HHMM="$2"; shift 2 ;;
    --ladder)       [[ $# -ge 2 ]] || die_usage "--ladder needs a value";       LADDER="$2"; shift 2 ;;
    --instructions) [[ $# -ge 2 ]] || die_usage "--instructions needs a value"; INSTR="$2"; shift 2 ;;
    -h|--help)      usage; exit 0 ;;
    *)              die_usage "unknown argument: $1" ;;
  esac
done

[[ -n "$SLUG" ]]          || die_usage "--slug is required"
[[ -n "$URLF" ]]          || die_usage "--url-file is required"
[[ -n "$DEADLINE_HHMM" ]] || die_usage "--deadline-utc is required"
re_slug='^[A-Za-z0-9_-]+$'
[[ "$SLUG" =~ $re_slug ]] || die_usage "slug must be [A-Za-z0-9_-]+: $SLUG"
re_hhmm='^([01][0-9]|2[0-3]):[0-5][0-9]$'
[[ "$DEADLINE_HHMM" =~ $re_hhmm ]] || die_usage "--deadline-utc must be HH:MM (24h UTC): $DEADLINE_HHMM"
re_ladder='^[0-9]+(,[0-9]+)*$'
[[ "$LADDER" =~ $re_ladder ]] || die_usage "--ladder must be comma-separated minutes: $LADDER"
re_poll='^[1-9][0-9]*$'
[[ "$POLL_SECS" =~ $re_poll ]] || die_usage "URL_ESCALATE_POLL_SECS must be a positive integer (seconds): $POLL_SECS"

# Installed here — before deadline resolution and tier planning — so even an
# early kill prints WATCHER-KILLED instead of dying silently.
trap 'DONE=1; echo "WATCHER-KILLED: url-escalate for $SLUG stopped before a URL landed"; exit 130' TERM INT

# An escalation script that cannot page is worse than none — it looks armed.
[[ -x "$NOTIFY" ]] || { DONE=1; echo "NOTIFY-MISSING: $NOTIFY not found/executable — refusing to run an escalation that cannot page" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { DONE=1; echo "USAGE-ERROR: python3 not on PATH (needed for portable date math)" >&2; exit 2; }

# Descending, de-duplicated tier list ("60 30 15").
TIERS="$(tr ',' '\n' <<<"$LADDER" | sort -rnu | tr '\n' ' ')"
for t in $TIERS; do
  [[ "$t" -ge 1 ]] || die_usage "ladder tiers must be >= 1 minute (got $t)"
done

# Resolve HH:MM UTC to the next absolute occurrence. python3, not `date -d`:
# BSD date on the laptop has no -d and GNU date has no -r — python3 is the
# one date engine both hosts share.
if ! DEADLINE_INFO="$(python3 - "$DEADLINE_HHMM" <<'PY'
import sys, datetime as dt
hh, mm = (int(x) for x in sys.argv[1].split(":"))
now = dt.datetime.now(dt.timezone.utc)
d = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
rolled = 0
if d <= now:
    d += dt.timedelta(days=1)
    rolled = 1
print(int(d.timestamp()), rolled)
PY
)"; then
  DONE=1
  echo "USAGE-ERROR: could not resolve --deadline-utc $DEADLINE_HHMM" >&2
  exit 2
fi
DEADLINE_EPOCH="${DEADLINE_INFO% *}"
ROLLED="${DEADLINE_INFO#* }"

iso_utc() {
  python3 -c 'import sys, datetime as dt; print(dt.datetime.fromtimestamp(int(sys.argv[1]), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$1"
}
DEADLINE_ISO="$(iso_utc "$DEADLINE_EPOCH")"
UP="$(printf '%s' "$SLUG" | tr '[:lower:]' '[:upper:]')"
START_EPOCH="$(date -u +%s)"

# State lives beside the url-file so a re-run (same watcher restarted, or a
# second copy launched by mistake) sees what was already paged. Keyed by the
# resolved deadline epoch: a new day's deadline never inherits stale tiers.
STATEF="${URLF}.escalate.state"
mkdir -p "$(dirname "$URLF")" || { DONE=1; echo "STATE-UNWRITABLE: cannot create directory for $URLF" >&2; exit 2; }
touch "$STATEF" 2>/dev/null   || { DONE=1; echo "STATE-UNWRITABLE: cannot write $STATEF — refusing to run a ladder whose idempotency cannot be tracked" >&2; exit 2; }

tier_sent() { grep -q "^$DEADLINE_EPOCH tier $1\$" "$STATEF"; }

echo "=== url-escalate $SLUG $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
if [[ "$ROLLED" == 1 ]]; then
  echo "DEADLINE-ROLLED: $DEADLINE_HHMM UTC already past today — resolved to tomorrow. If you meant TODAY, the event is already missed; stop this watcher."
fi
echo "DEADLINE-RESOLVED: $DEADLINE_ISO (in $(( (DEADLINE_EPOCH - START_EPOCH) / 60 ))m)"
echo "watching: $URLF  (poll ${POLL_SECS}s; state: $STATEF)"
if grep -q ' missed$' "$STATEF"; then
  echo "PRIOR-MISS: $STATEF records a missed deadline from an earlier run — verify this is a NEW deadline, not a re-run of a dead event"
fi

for t in $TIERS; do
  fire=$(( DEADLINE_EPOCH - t * 60 ))
  if (( fire <= START_EPOCH )); then
    echo "TIER-SKIPPED: T-${t}m (fire time $(iso_utc "$fire") already past at start)"
  elif tier_sent "$t"; then
    echo "TIER-DEDUP: T-${t}m already recorded by a prior run; will not resend"
  else
    echo "tier plan: T-${t}m fires at $(iso_utc "$fire")"
  fi
done

send_tier() {
  local t="$1"
  local subj="$UP URL NEEDED (T-${t}m)"
  local body
  body="Manual browser-read required for ${SLUG} (W2.3).

WRITE THIS FILE (stream URL as the single first line):
  ${URLF}

RECIPE: ${INSTR}

DEADLINE: ${DEADLINE_ISO} (this alert is T-${t}m).
If the file is not there by the deadline, ${SLUG} WILL NOT ARM.
Ladder: alerts at T-{${LADDER}}m, then a final MISSED page."
  if "$NOTIFY" "$subj" "$body"; then
    echo "$DEADLINE_EPOCH tier $t" >> "$STATEF"
    echo "TIER-SENT: T-${t}m at $(date -u +%H:%M:%SZ)"
  else
    # Not recorded on purpose: an undelivered tier must retry, and each
    # failed attempt must be visible in the log.
    echo "NOTIFY-FAILED: T-${t}m page failed on EVERY channel; retrying next poll"
  fi
}

url_first_line() {
  head -n 1 "$URLF" 2>/dev/null | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

re_http='^https?://'
BLANK_WARNED=0
while :; do
  if [[ -e "$URLF" ]]; then
    URL="$(url_first_line || true)"
    if [[ -n "$URL" ]]; then
      if ! [[ "$URL" =~ $re_http ]]; then
        echo "URL-SUSPECT: first line of $URLF does not look like an http(s) URL — arm-side guards (arm-when-url.sh) will re-validate"
      fi
      DONE=1
      echo "URL-READY: $SLUG $URL"
      exit 0
    fi
    # File exists but first line is empty/whitespace: say so ONCE per state
    # change so the paged human can see why READY never fires. Still not
    # READY — readiness requires a non-empty first line.
    if (( BLANK_WARNED == 0 )); then
      echo "URL-FILE-BLANK-LINE: $URLF exists but its first line is empty/whitespace — not READY; write the stream URL as the single first line"
      BLANK_WARNED=1
    fi
  else
    BLANK_WARNED=0
  fi

  now="$(date -u +%s)"
  if (( now >= DEADLINE_EPOCH )); then
    subj="$UP URL MISSED — event will not arm"
    body="No URL landed in ${URLF} by the ${DEADLINE_ISO} deadline. ${SLUG} will NOT arm.

If the call is still salvageable, do the read NOW and launch by hand:
RECIPE: ${INSTR}
then write the URL to ${URLF} and run the arm path manually."
    if ! "$NOTIFY" "$subj" "$body"; then
      echo "NOTIFY-FAILED: missed-deadline page failed on EVERY channel"
    fi
    echo "$DEADLINE_EPOCH missed" >> "$STATEF"
    DONE=1
    echo "URL-MISSED: $SLUG"
    exit 1
  fi

  for t in $TIERS; do
    fire=$(( DEADLINE_EPOCH - t * 60 ))
    if (( fire <= START_EPOCH )); then continue; fi  # skipped at start, stays skipped
    if (( now < fire ));          then continue; fi  # not due yet
    if tier_sent "$t";            then continue; fi  # idempotent re-run guard
    send_tier "$t"
  done

  sleep "$POLL_SECS"
done
