#!/bin/sh
# SHOP 2026-08-05 webcast — unattended master-m3u8 resolver.
# Venue: shopify.com proprietary page, media host = MUX (public playback policy).
# Writes the master m3u8 to $OUT once Mux reports the live stream active.
# Exit 0 = $OUT holds a 200-OK master playlist. Exit 1 = timed out.
#
# Usage: shop-resolve-stream.sh [OUT] [DEADLINE_EPOCH]
set -u
PAGE="https://www.shopify.com/investors/quarterly-results/webcast/q2-2026"
FALLBACK_PB="00qY00GWV6n02wds94RxvdlgK9WOhB00oMkPppJqPPr6znk"   # observed 2026-08-04, livestreamStatus=idle
OUT="${1:-$HOME/shop-stream.url}"
DEADLINE="${2:-$(( $(date +%s) + 2400 ))}"                       # default: 40 min

# 1. Re-extract the playback ID from the page (survives a re-provisioned stream).
PB=$(curl -sSL --max-time 20 "$PAGE.data" \
      | grep -oE 'playbackId","[A-Za-z0-9]{20,}' | cut -d'"' -f3 | head -1)
[ -n "${PB:-}" ] || PB=$(curl -sSL --max-time 20 "$PAGE" \
      | grep -oE 'playbackId\\?","?[A-Za-z0-9]{20,}' | grep -oE '[A-Za-z0-9]{30,}' | head -1)
[ -n "${PB:-}" ] || PB="$FALLBACK_PB"
URL="https://stream.mux.com/$PB.m3u8"
echo "[shop-resolve] playbackId=$PB" >&2

# 2. Poll until Mux flips 412 (idle) -> 200 (active).
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$URL")
  if [ "$CODE" = "200" ]; then
    printf '%s\n' "$URL" > "$OUT"
    echo "[shop-resolve] LIVE $URL" >&2
    exit 0
  fi
  echo "[shop-resolve] $(date -u +%H:%M:%S) http=$CODE waiting" >&2
  sleep 5
done
echo "[shop-resolve] TIMEOUT, never went 200" >&2
exit 1
