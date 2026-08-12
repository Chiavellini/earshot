#!/usr/bin/env bash
# cdn-cachebust-fetch.sh — HLS source leg for CDNs that serve a STALE CACHED
# media playlist. Emits raw MPEG-TS segment bytes on stdout; pipe into ffmpeg.
#
#   scripts/cdn-cachebust-fetch.sh "$HOME/<slug>-stream.url" [poll_secs]
#
# WHY THIS EXISTS (2026-08-12, CBRS/Cerebras on media-server.com + CloudFront):
# every normal HLS consumer we own went deaf on this venue while the call was
# audibly still running:
#   * streamlink  -> "Stream ended" after ~25 s, then nothing
#   * ffmpeg      -> blocks forever on the stalled input
#   * pcm-gate.sh -> 157492/256000 bytes, i.e. "THIS VANTAGE IS DEAD"
#   * hls-edge-fetch.py -> plain urlopen, so it freezes the same way
# None of that was true. The MEDIA playlist sits behind a CDN cache that keeps
# returning the SAME body (#EXT-X-MEDIA-SEQUENCE frozen at one value) even as
# the encoder keeps appending. A client that trusts the playlist concludes the
# stream ended; the stream had not ended.
#
# The fix is one line of cache-busting per reload: a unique query param plus
# Cache-Control: no-cache. With it the playlist advances normally and segments
# keep arriving at real time (32000 B/s of 16 kHz mono s16le downstream).
#
# THE TRAP THIS ALSO GUARDS: the same venue serves a FINISHED 15-minute VOD
# (#EXT-X-ENDLIST, fixed segment count) from a sibling host. launch-webcast.sh's
# --wait-live hls gate only greps for EXTINF, so it declares that VOD "LIVE" and
# would arm a detector against pre-recorded audio. Never treat "manifest has
# EXTINF" as live; require no ENDLIST *and* an advancing tail.
#
# This leg is deliberately crash-proof: any curl failure sleeps and retries
# rather than killing the leg (a dead leg is a silent half-deaf fanout).
set -uo pipefail

URL_FILE="${1:?usage: cdn-cachebust-fetch.sh <url-file> [poll_secs]}"
POLL="${2:-1}"

[[ -f "$URL_FILE" ]] || { echo "cdn-cachebust-fetch: no url file $URL_FILE" >&2; exit 2; }
URL="$(tr -d '[:space:]' < "$URL_FILE")"
[[ -n "$URL" ]] || { echo "cdn-cachebust-fetch: empty url file $URL_FILE" >&2; exit 2; }

bust() { printf '%s%s_cb=%s' "$1" "$([[ "$1" == *\?* ]] && echo '&' || echo '?')" "$(date +%s%N)"; }

# Follow a MASTER playlist down to its first variant. The master is what the
# operator pins (it survives rendition changes); segments live under the variant.
BODY="$(curl -fsSL --max-time 10 -H 'Cache-Control: no-cache' "$(bust "$URL")" 2>/dev/null || true)"
if grep -q 'EXT-X-STREAM-INF' <<<"$BODY"; then
  VARIANT="$(grep -v '^#' <<<"$BODY" | grep -v '^[[:space:]]*$' | head -1 | tr -d '\r')"
  [[ "$VARIANT" != http* ]] && VARIANT="${URL%/*}/$VARIANT"
  URL="${VARIANT%%\?*}"
fi
BASE="${URL%/*}"
echo "cdn-cachebust-fetch: media playlist $URL" >&2

last=""
while true; do
  PL="$(curl -fsSL --max-time 8 -H 'Cache-Control: no-cache' "$(bust "$URL")" 2>/dev/null)" || { sleep "$POLL"; continue; }
  # newest segment line = last non-comment, non-blank entry
  seg="$(grep -v '^#' <<<"$PL" | grep -v '^[[:space:]]*$' | tail -1 | tr -d '\r')"
  if [[ -n "$seg" && "$seg" != "$last" ]]; then
    [[ "$seg" == http* ]] && SEG_URL="$seg" || SEG_URL="$BASE/$seg"
    curl -fsSL --max-time 8 "$SEG_URL" 2>/dev/null || true
    last="$seg"
  fi
  sleep "$POLL"
done
