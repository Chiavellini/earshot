#!/usr/bin/env bash
# Probe an HLS manifest for liveness facts (docs/RUNBOOK.md webcast lane):
#   - segment cadence (EXT-X-TARGETDURATION / EXTINF) — sets the standard-HLS
#     latency floor (~1-1.5 segment durations even with a tuned client)
#   - LL-HLS capability tags — decides whether a partial-segment client
#     (streamlink / native) is worth building for this venue
#   - EXT-X-PROGRAM-DATE-TIME — when present, now_utc - last_segment_PDT is
#     the ABSOLUTE manifest-edge lag (bounded by the encoder's clock accuracy)
# Read-only (two curls), runs anywhere. Run from the SAME vantage as the
# capture box — segment URLs can be requester-IP-locked (RUNBOOK W1-pre).
#
# Usage: scripts/manifest-probe.sh "<m3u8-url>" [| tee probe.txt]
set -euo pipefail

url="${1:?usage: manifest-probe.sh <m3u8-url>}"

fetch() { curl -fsSL --max-time 15 "$1"; }

master="$(fetch "$url")"

# Master playlists carry EXT-X-STREAM-INF; follow the first variant so the
# tags below come from a media playlist.
media="$master"
media_url="$url"
if grep -q '^#EXT-X-STREAM-INF' <<<"$master"; then
  variant="$(grep -A1 '^#EXT-X-STREAM-INF' <<<"$master" | grep -v '^#' | grep -v '^--$' | head -1)"
  case "$variant" in
    http*) media_url="$variant" ;;
    /*)    media_url="$(sed -E 's|(https?://[^/]+).*|\1|' <<<"$url")$variant" ;;
    *)     media_url="${url%/*}/$variant" ;;
  esac
  echo "master playlist: $url"
  echo "variant probed:  $media_url"
  media="$(fetch "$media_url")"
else
  echo "media playlist:  $url"
fi
echo

echo "== cadence =="
grep '^#EXT-X-TARGETDURATION' <<<"$media" || echo "(no EXT-X-TARGETDURATION)"
echo "last EXTINF values:"
grep '^#EXTINF' <<<"$media" | tail -5 || echo "(no EXTINF — not a media playlist?)"
grep '^#EXT-X-MEDIA-SEQUENCE' <<<"$media" || true
echo

echo "== LL-HLS capability =="
ll="$(grep -E '^#EXT-X-(PART|PART-INF|SERVER-CONTROL|PRELOAD-HINT)' <<<"$media" || true)"
if [ -n "$ll" ]; then
  echo "$ll"
else
  echo "(none — standard-latency HLS; floor ≈ 1-1.5 segment durations)"
fi
echo

echo "== absolute edge lag (PDT) =="
pdt="$(grep '^#EXT-X-PROGRAM-DATE-TIME' <<<"$media" | tail -1 | cut -d: -f2- || true)"
if [ -n "$pdt" ]; then
  echo "last segment PDT: $pdt"
  now_s="$(date -u +%s)"
  # GNU date (the box) parses ISO-8601 via -d; macOS fallback trims to seconds.
  pdt_s="$(date -u -d "$pdt" +%s 2>/dev/null \
    || date -u -j -f '%Y-%m-%dT%H:%M:%S' "${pdt%%.*}" +%s 2>/dev/null \
    || echo '')"
  if [ -n "$pdt_s" ]; then
    echo "now_utc - last_segment_PDT = $((now_s - pdt_s)) s  (encoder-clock-bounded)"
  else
    echo "(could not parse PDT timestamp — compute by hand)"
  fi
else
  echo "(no EXT-X-PROGRAM-DATE-TIME — absolute lag not derivable from manifest)"
fi
