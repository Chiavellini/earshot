#!/usr/bin/env bash
# youtube-resolve.sh <slug> <youtube-url> <deadline-epoch>
# Polls a YouTube Live URL until it is actually live, then writes the MASTER
# HLS manifest (hls_variant — NOT a media playlist) to $HOME/<slug>-stream.url.
#
# Why the master specifically: run-arm.sh guard 2 refuses a variant playlist,
# because a media playlist can 404 mid-call while the channel stays live and
# hls-edge-fetch.py can only re-resolve when handed a master (LATENCY-PROOF V6,
# and the WING 2026-07-29 failure: 2095 consecutive 404s, reresolve=0).
# `yt-dlp -g` returns a VARIANT, so we read manifest_url out of the JSON instead.
#
# ⚠⚠ THIS SCRIPT ALONE IS NOT ENOUGH TO CAPTURE A YOUTUBE EVENT FROM THE BOX.
# Live-proven 2026-07-29 (HOOD, the first YouTube-hosted event we ever armed):
# the master manifest resolves and fetches HTTP 200 from us-east-1, the arm
# starts clean, and then BOTH legs die inside 35 s with frames=0 —
#   pipe_tuned : ffmpeg "exit status: 183"
#   spec_edge  : segs=0 dropped=670 seg_err=71 poll_err=1625 DEAF-NOW
# because YouTube bot-walls the datacenter IP at the *segment* layer:
#   "Sign in to confirm you're not a bot. Use --cookies-from-browser…"
# The manifest URL is a decoy — it 200s while the media underneath is refused.
#
# MEASURED same day, same minute, same video: the CDMX laptop (residential IP)
# piped exactly 192000 bytes of PCM for 6 s @16 kHz mono — i.e. a clean feed.
# So the split is IP reputation, not tooling.
#
# Consequences for anyone wiring a YouTube event:
#   1. A URL-reachability gate (HTTP 200 + #EXTM3U) CANNOT clear this venue —
#      it passed here and the run still captured nothing. Gate on PCM BYTES:
#        yt-dlp … -o - | ffmpeg … -t 6 - | wc -c   # must be ≈192000, not 0
#   2. Prefer `yt-dlp -o -` piped straight into ffmpeg over hls-edge-fetch.py;
#      yt-dlp owns YouTube's segment addressing. See
#      configs/webcast-hood-ytdlp-arm.toml for that leg shape.
#   3. From the box you need cookies (operator action) or an egress with
#      residential reputation. Otherwise capture on the laptop and LABEL it
#      CDMX-vantage (~1 s worse feed position, 3.5x run variance) — its Δrace
#      must never be pooled with us-east HLS venues.
set -uo pipefail
SLUG="${1:?slug}"; YT="${2:?youtube url}"; DEADLINE="${3:?deadline epoch}"
REPO="${EARSHOT_REPO:-$HOME/ear}"
URLF="$HOME/$SLUG-stream.url"; TMP="$URLF.tmp.$$"
echo "=== $SLUG youtube-resolve $(date -u +%FT%TZ) -> $YT ==="
while :; do
  if (( $(date +%s) > DEADLINE )); then
    echo "TIMEOUT: $YT never went live"
    "$REPO/scripts/notify.sh" "$SLUG youtube NOT LIVE" "no manifest before deadline: $YT" --quiet || true
    exit 3
  fi
  M=$(yt-dlp -J --no-warnings "$YT" 2>/dev/null \
      | python3 -c "
import sys,json
try: d=json.load(sys.stdin)
except Exception: sys.exit(1)
if d.get('is_live') is not True: sys.exit(1)
for f in d.get('formats',[]):
    u=f.get('manifest_url') or ''
    if 'hls_variant' in u:
        print(u); break
" 2>/dev/null)
  if [[ -n "$M" ]]; then
    printf '%s\n' "$M" > "$TMP" && mv -f "$TMP" "$URLF"
    echo "LIVE. master manifest -> $URLF"
    echo "$M" | cut -c1-140
    exit 0
  fi
  echo "[$(date -u +%T)] not live yet…"
  sleep 10
done
