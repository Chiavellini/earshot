#!/usr/bin/env bash
# Generic "arm as soon as the resolver lands a validated URL" watcher.
# Usage: arm-when-url.sh <slug> <deadline-epoch> [arm-max-secs]
# Mirrors wing-autorun.sh steps 2-3 without re-doing step 1 (a resolver is
# already polling and writes $HOME/<slug>-stream.url atomically).
set -uo pipefail
REPO="${EARSHOT_REPO:-$HOME/ear}"; cd "$REPO"
SLUG="${1:?slug}"; DEADLINE="${2:?deadline epoch}"; ARM_MAX="${3:-5400}"
URLF="$HOME/$SLUG-stream.url"
LOG="$HOME/$SLUG-autoarm-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG") 2>&1
echo "=== $SLUG auto-arm watcher $(date -u +%FT%TZ) deadline=$(date -u -d @$DEADLINE +%FT%TZ) ==="
page() { "$REPO/scripts/notify.sh" "$1" "$2" --quiet || true; }

while [[ ! -s "$URLF" ]]; do
  if (( $(date +%s) > DEADLINE )); then
    echo "TIMEOUT: no $URLF by deadline — NOT armed"
    page "$SLUG NOT ARMED" "resolver never produced $URLF before the deadline"
    exit 3
  fi
  sleep 5
done
U="$(cat "$URLF")"
echo "url landed: $U"
# Independent reachability check before we hand it to the arm (Ford PATH A/B).
CODE=$(curl -sS -o /tmp/$SLUG-probe.m3u8 -w "%{http_code}" -m 20 "$U" || echo 000)
echo "probe: HTTP $CODE  $(head -c 120 /tmp/$SLUG-probe.m3u8 2>/dev/null | tr '\n' ' ')"
if [[ "$CODE" != 200 ]]; then
  echo "REFUSING to arm: URL not fetchable from the box (HTTP $CODE)"
  page "$SLUG URL NOT FETCHABLE" "HTTP $CODE for $U — session-bound token? capture must run where the session lives"
  exit 4
fi
if ! head -c 64 /tmp/$SLUG-probe.m3u8 | grep -q '#EXTM3U'; then
  echo "REFUSING to arm: body is not an HLS playlist"
  page "$SLUG URL NOT HLS" "$U returned 200 but no #EXTM3U"
  exit 5
fi
echo "--- arming ---"
setsid nohup scripts/run-arm.sh "$SLUG" > "$HOME/$SLUG-arm-launch.log" 2>&1 < /dev/null &
sleep 20; tail -20 "$HOME/$SLUG-arm-launch.log"
