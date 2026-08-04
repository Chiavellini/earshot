#!/usr/bin/env bash
# arm-on-url.sh — wait for a resolver to land a stream URL, PROVE it decodes on
# THIS host, then launch the webcast arm. Fail-closed at every step.
#
# WHY NOT arm-when-url.sh: that script's readiness gate is HTTP 200 + #EXTM3U.
# On YouTube that gate is affirmatively misleading — HOOD 2026-07-29 passed it
# and still captured nothing, because the bot-wall refuses SEGMENTS while the
# manifest 200s (scripts/youtube-resolve.sh:26-31). This script gates on
# scripts/pcm-gate.sh — actual decoded PCM bytes — which is the only check that
# can distinguish "reachable" from "capturable".
#
#   scripts/arm-on-url.sh --slug pltr --kind youtube \
#       --config configs/webcast-pltr-26aug03-arm-wc.toml \
#       --account $HOME/.earshot/ms.env --deadline-utc 21:20 [--i-am-live]
#
# Chain: wait for $HOME/<slug>-stream.url  →  pcm-gate on this host  →
#        launch-webcast.sh (which runs its own --wait-live poll and guards).
# Any step failing pages via notify.sh and exits nonzero. There is no silent
# path out: a missed event is an acceptable outcome, a deaf arm is not.
set -uo pipefail

REPO="${EARSHOT_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$REPO"

SLUG="" KIND="" CONFIG="" ACCOUNT="" DEADLINE_HHMM="" LIVE="" SECS=6
while [[ $# -gt 0 ]]; do
  case "$1" in
    --slug)         SLUG="${2:?}"; shift 2 ;;
    --kind)         KIND="${2:?}"; shift 2 ;;
    --config)       CONFIG="${2:?}"; shift 2 ;;
    --account)      ACCOUNT="${2:?}"; shift 2 ;;
    --deadline-utc) DEADLINE_HHMM="${2:?}"; shift 2 ;;
    --secs)         SECS="${2:?}"; shift 2 ;;
    --i-am-live)    LIVE="--i-am-live"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$SLUG" && -n "$KIND" && -n "$CONFIG" && -n "$ACCOUNT" && -n "$DEADLINE_HHMM" ]] || {
  echo "usage: arm-on-url.sh --slug S --kind youtube|hls --config C --account A --deadline-utc HH:MM [--i-am-live]" >&2
  exit 2; }

URLF="$HOME/$SLUG-stream.url"
LOG="$HOME/$SLUG-armonurl-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG") 2>&1
page() { "$REPO/scripts/notify.sh" "$1" "$2" || true; }

# Resolve the deadline to an absolute epoch the same way url-escalate.sh does:
# the NEXT occurrence of that UTC wall time.
NOW=$(date -u +%s)
TODAY=$(date -u +%Y-%m-%d)
DEADLINE=$(python3 -c "
import calendar,datetime,sys
h,m=sys.argv[1].split(':')
d=datetime.datetime.strptime(sys.argv[2],'%Y-%m-%d').replace(hour=int(h),minute=int(m))
e=calendar.timegm(d.timetuple())
if e < int(sys.argv[3]): e += 86400
print(e)" "$DEADLINE_HHMM" "$TODAY" "$NOW")
echo "=== arm-on-url $SLUG kind=$KIND $(date -u +%FT%TZ) ==="
echo "DEADLINE-RESOLVED: $(python3 -c "import datetime;print(datetime.datetime.fromtimestamp($DEADLINE,datetime.timezone.utc).isoformat())")"
echo "url-file : $URLF"
echo "posture  : ${LIVE:---dry-run}"

# ── step 1: wait for the resolver ──────────────────────────────────────────
while [[ ! -s "$URLF" ]]; do
  if (( $(date -u +%s) > DEADLINE )); then
    echo "URL-MISSED: $SLUG — no $URLF by deadline. NOT ARMED."
    page "$SLUG NOT ARMED — no URL" "Resolver never produced $URLF before $DEADLINE_HHMM UTC. See $LOG"
    exit 3
  fi
  sleep 5
done
echo "URL-READY: $SLUG $(head -c 120 "$URLF")"

# ── step 2: PCM gate — the check that HTTP 200 cannot make ─────────────────
# Deliberately gates on the URL FILE, so we test exactly what the arm will read.
if ! "$REPO/scripts/pcm-gate.sh" --kind "$KIND" --url-file "$URLF" --secs "$SECS" --label "$SLUG"; then
  echo "PCM-GATE FAILED for $SLUG on $(hostname -s) — REFUSING to arm a deaf leg."
  page "$SLUG NOT ARMED — no decodable audio" \
       "pcm-gate got no PCM from $URLF on $(hostname -s). This vantage is dead (HOOD 07-29 class). See $LOG"
  exit 4
fi

# ── step 3: launch ─────────────────────────────────────────────────────────
# NO --wait-live here, deliberately. That gate polls the target for liveness,
# but the value in $URLF is a MASTER MANIFEST, not a watch page — with
# --wait-kind youtube it would run `yt-dlp --print is_live` against an .m3u8
# and never see True, hanging forever 20 s before the call. It is also
# redundant: step 2 just decoded real PCM off this exact URL, which is a
# strictly stronger liveness proof than an is_live poll.
echo "ARMING $SLUG at $(date -u +%FT%TZ)"
exec "$REPO/scripts/launch-webcast.sh" --config "$CONFIG" --account "$ACCOUNT" $LIVE
