#!/usr/bin/env bash
# Q4 Inc attendee-platform resolver — headless, unauthenticated, no browser.
#
#   scripts/q4-resolve.sh --event-id 200002552 --slug dis [--deadline-utc HH:MM]
#
# WHY: most issuer IR front-ends (investors.<issuer>.com) sit behind a
# Cloudflare bot challenge that refuses fetchers AND real headed Chrome — on
# 2026-08-05 that walled DIS and UBER out of every automated path. But the Q4
# ATTENDEE platform that actually serves those webcasts is a different host and
# is NOT walled: https://attendees.events.q4inc.com/rest/v1/event/<id> returns
# public JSON with {status, msToStart, broadcastUrl} and no credential at all.
#
# Poll that until status leaves NOT_STARTED and broadcastUrl populates, then
# write the URL to $HOME/<slug>-stream.url — the file arm-on-url.sh waits on.
# The broadcast URL is handed to pcm-gate before any arm, so a wrong/dead value
# is caught there rather than producing a deaf leg.
#
# Exit 0 wrote a URL / 3 deadline with nothing / 2 usage. Never silent.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

EVENT_ID="" SLUG="" DEADLINE_HHMM="" POLL=20
while [[ $# -gt 0 ]]; do
  case "$1" in
    --event-id) EVENT_ID="${2:?}"; shift 2 ;;
    --slug)     SLUG="${2:?}"; shift 2 ;;
    --deadline-utc) DEADLINE_HHMM="${2:?}"; shift 2 ;;
    --poll)     POLL="${2:?}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$EVENT_ID" && -n "$SLUG" ]] || { echo "usage: q4-resolve.sh --event-id <id> --slug <slug> [--deadline-utc HH:MM]" >&2; exit 2; }

OUT="$HOME/${SLUG}-stream.url"
API="https://attendees.events.q4inc.com/rest/v1/event/${EVENT_ID}"

if [[ -n "$DEADLINE_HHMM" ]]; then
  DEADLINE=$(python3 -c "
import calendar,datetime,sys
h,m=sys.argv[1].split(':')
now=datetime.datetime.now(datetime.timezone.utc)
d=now.replace(hour=int(h),minute=int(m),second=0,microsecond=0)
e=calendar.timegm(d.timetuple())
if e < int(now.timestamp()): e += 86400
print(e)" "$DEADLINE_HHMM")
else
  DEADLINE=$(( $(date -u +%s) + 5400 ))
fi
echo "=== q4-resolve $SLUG event=$EVENT_ID $(date -u +%FT%TZ) deadline=$(python3 -c "import datetime;print(datetime.datetime.fromtimestamp($DEADLINE,datetime.timezone.utc).isoformat())")"

while [[ "$(date -u +%s)" -lt "$DEADLINE" ]]; do
  OUT_JSON=$(curl -s --max-time 15 "$API" 2>/dev/null || true)
  read -r STATUS URL MS <<<"$(printf '%s' "$OUT_JSON" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)['data']
    print(d.get('status','?'), (d.get('broadcastUrl') or '-'), d.get('msToStart','?'))
except Exception:
    print('PARSE-FAIL - -')" 2>/dev/null)"
  if [[ "$URL" != "-" && -n "$URL" ]]; then
    printf '%s' "$URL" > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"
    echo "URL-WRITTEN ($STATUS) → $OUT"
    echo "$URL"
    scripts/notify.sh "$SLUG URL RESOLVED (q4 api)" "$URL → $OUT (status=$STATUS)" --quiet || true
    exit 0
  fi
  echo "$(date -u +%H:%M:%S) status=$STATUS msToStart=$MS — waiting"
  sleep "$POLL"
done

echo "Q4-RESOLVE TIMEOUT: $SLUG never published a broadcastUrl" >&2
scripts/notify.sh "$SLUG q4-resolve TIMEOUT" \
  "Polled $API to deadline; broadcastUrl never populated. Manual: open https://events.q4inc.com/attendee/${EVENT_ID}, DevTools Network filter m3u8, then printf '%s' '<URL>' > $OUT" || true
exit 3
