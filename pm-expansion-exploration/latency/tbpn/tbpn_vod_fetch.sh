#!/usr/bin/env bash
# Bank a TBPN (Technology Business Programming Network) episode VOD.
#
# PATTERN-COPIED from scripts/wnt-vod-fetch.sh (production, never edited or
# imported here) — same idea: free same-day YouTube captions as leg 1 for a
# pack-lag measurement, no local ASR, no live capture. Adapted for TBPN's
# structural differences from WNT, discovered empirically before writing this:
#
#   - WNT posts ONE ~20-minute "Full Broadcast" VOD per weekday, titled with
#     an explicit date ("...July 29, 2026"), so a title-regex date match plus
#     a tight duration window uniquely identifies it.
#   - TBPN's @TBPNLive channel posts several videos a day (segment clips,
#     guest interviews, "Diet TBPN" recap compilations) with NO date in the
#     title at all — titles are topic headlines ("Astra Ends Math, Gruber
#     Joins..."). The channel's /streams tab (archived livestreams, not
#     /videos or /search) is where the long ~2-3 hour full-episode archive
#     actually lives (confirmed empirically 2026-08-05: durations there
#     cluster 6500-11500s; the general /videos listing mixes in much shorter
#     clips down to ~1400s). Because there's no title date string, this
#     script identifies the right video by DURATION WINDOW + UPLOAD DATE
#     (via a full, non-flat yt-dlp metadata fetch per candidate — flat-
#     playlist mode does not populate upload_date), not by title regex.
#
# ⚠ UNVERIFIED, flag honestly: WNT's title-based method guarantees picking
# the FULL broadcast because the duration window is tight (900-1500s) and
# the title says "Full Broadcast". TBPN has no equivalent explicit label —
# this script's duration window (default 6000-14400s) is inferred from one
# day's sample of the /streams tab, not confirmed against a definitive list
# of "this is the whole show" vs "this is a long segment". Multiple
# same-day streams-tab entries inside the window are refused, not guessed,
# same discipline as WNT's AMBIGUOUS case — but if TBPN sometimes posts two
# long streams in one day (e.g. a special event), this window alone cannot
# tell them apart; that would show up as an AMBIGUOUS refusal, which is the
# safe failure mode.
#
# Usage:
#   tbpn_vod_fetch.sh 20260804 [20260803 ...]   # yyyymmdd, one or more
#   tbpn_vod_fetch.sh 20260804 --no-audio       # captions only, fast
#
# Output per session (pm-expansion-exploration/latency/tbpn/corpus/, inside
# the exploration tree — NEVER the real top-level corpus/, that's production):
#   corpus/<yyyymmdd>/audio.wav        16 kHz mono s16le (unless --no-audio)
#   corpus/<yyyymmdd>/meta.json        video id/title/duration/provenance
#   corpus/<yyyymmdd>/words.json       [{word,start,end,char_*}] programme time
#   corpus/<yyyymmdd>/transcript.txt   flat text
#
# Exit codes: 0 ok / 2 preconditions bad / 3 no VOD found or ambiguous /
#             4 audio download failed / 5 captions unavailable
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORPUS_ROOT="$HERE/corpus"
PARSER="$HERE/../../../scripts/wnt-captions-parse.py"   # reused UNMODIFIED, read-only:
                                                          # its json3->words.json shape is
                                                          # venue-agnostic (no WNT-specific
                                                          # logic in the file at all)

CHANNEL="https://www.youtube.com/@TBPNLive/streams"
MIN_SECS="${TBPN_VOD_MIN_SECS:-6000}"
MAX_SECS="${TBPN_VOD_MAX_SECS:-14400}"
SEARCH_DEPTH="${TBPN_VOD_SEARCH_DEPTH:-15}"

NO_AUDIO=0
FORCE=0
DATES=()
for a in "$@"; do
  case "$a" in
    --no-audio) NO_AUDIO=1 ;;
    --force)    FORCE=1 ;;
    -h|--help)  sed -n '2,45p' "$0"; exit 0 ;;
    --*)        echo "usage: tbpn_vod_fetch.sh [<yyyymmdd>...] [--no-audio] [--force]" >&2; exit 2 ;;
    *)          DATES+=("$a") ;;
  esac
done
(( ${#DATES[@]} )) || { echo "usage: tbpn_vod_fetch.sh <yyyymmdd> [...] [--no-audio]" >&2; exit 2; }

for t in curl python3 yt-dlp; do
  command -v "$t" >/dev/null || { echo "FATAL: missing $t" >&2; exit 2; }
done
[[ -f "$PARSER" ]] || { echo "FATAL: missing $PARSER (scripts/wnt-captions-parse.py)" >&2; exit 2; }
if (( NO_AUDIO == 0 )); then
  command -v ffmpeg >/dev/null || { echo "FATAL: missing ffmpeg (or pass --no-audio)" >&2; exit 2; }
fi

note() { echo "[$(date -u +%T)] $*"; }

rc=0
for TICK in "${DATES[@]}"; do
  OUT="$CORPUS_ROOT/$TICK"
  if [[ -s "$OUT/words.json" && $FORCE -eq 0 ]]; then
    note "$TICK: already banked ($OUT) — skip (--force to redo)"
    continue
  fi

  note "$TICK: listing @TBPNLive/streams (depth $SEARCH_DEPTH), matching upload_date=$TICK, duration $MIN_SECS-${MAX_SECS}s"
  LIST="$(mktemp)"
  yt-dlp --flat-playlist --playlist-end "$SEARCH_DEPTH" --print "%(id)s" \
      "$CHANNEL" 2>/dev/null >"$LIST"
  if [[ ! -s "$LIST" ]]; then
    note "$TICK: ⛔ could not list @TBPNLive/streams"; rm -f "$LIST"; rc=3; continue
  fi

  # Flat-playlist mode does not populate upload_date/duration reliably — one
  # full metadata fetch per candidate id is required. This is the real cost
  # of TBPN having no date in its titles: WNT's search query narrows to ~1
  # request; this narrows only by depth, then pays a full fetch per id.
  HIT=""
  MATCHES=0
  while IFS= read -r VID; do
    [[ -n "$VID" ]] || continue
    META="$(yt-dlp --print "%(id)s|%(duration)s|%(upload_date)s|%(title)s" \
        "https://www.youtube.com/watch?v=$VID" 2>/dev/null)"
    [[ -n "$META" ]] || continue
    IFS='|' read -r VID2 DUR UPDATE TITLE <<<"$META"
    [[ "$UPDATE" == "$TICK" ]] || continue
    if (( DUR >= MIN_SECS && DUR <= MAX_SECS )); then
      MATCHES=$(( MATCHES + 1 ))
      HIT="$VID2|$DUR|$TITLE"
    fi
  done < "$LIST"
  rm -f "$LIST"

  if (( MATCHES == 0 )); then
    note "$TICK: ⛔ no /streams entry with upload_date=$TICK in [$MIN_SECS,$MAX_SECS]s — try a wider --playlist-end depth or check the date is recent enough to still be listed"
    rc=3; continue
  fi
  if (( MATCHES > 1 )); then
    note "$TICK: ⛔ $MATCHES distinct candidates for this date inside the duration window — refusing to guess (same discipline as WNT's AMBIGUOUS case)"
    rc=3; continue
  fi

  VID="$(cut -d'|' -f1 <<<"$HIT")"; DUR="$(cut -d'|' -f2 <<<"$HIT")"; TITLE="$(cut -d'|' -f3- <<<"$HIT")"
  note "$TICK: $VID (${DUR}s) — $TITLE"
  mkdir -p "$OUT"

  # ── captions ────────────────────────────────────────────────────────────
  rm -f "$OUT"/subs.*.json3
  if ! yt-dlp --skip-download --write-auto-subs --write-subs \
        --sub-langs "en-orig,en" --sub-format json3 \
        -o "$OUT/subs.%(ext)s" --no-progress -q \
        "https://www.youtube.com/watch?v=$VID" 2>"$OUT/ytdlp-subs.err"; then
    note "$TICK: ⛔ caption fetch failed (see $OUT/ytdlp-subs.err)"; rc=5; continue
  fi
  CAP="$(ls "$OUT"/subs.en-orig.json3 "$OUT"/subs.en.json3 2>/dev/null | head -1)"
  if [[ ! -s "$CAP" ]]; then
    note "$TICK: ⛔ no en/en-orig json3 caption track"; rc=5; continue
  fi
  if ! NWORDS="$(python3 "$PARSER" "$CAP" "$OUT/words.json" "$OUT/transcript.txt")"; then
    note "$TICK: ⛔ caption track parsed to zero words"; rc=5; continue
  fi
  note "$TICK: captions -> $NWORDS words"

  python3 - "$OUT/meta.json" "$TICK" "$VID" "$DUR" "$TITLE" "$NWORDS" "$(basename "$CAP")" <<'PYMETA'
import json, sys, time
out, tick, vid, dur, title, nwords, cap = sys.argv[1:8]
json.dump({
    "session": tick,
    "video_id": vid,
    "title": title,
    "url": f"https://www.youtube.com/watch?v={vid}",
    "duration_secs": int(dur),
    "word_source": f"youtube-auto-captions ({cap})",
    "word_count": int(nwords),
    "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "programme": "TBPN (@TBPNLive) — technology/business daily livestream",
    "caveat": ("Identified by duration-window + upload_date, NOT a title-embedded "
               "date (TBPN titles carry no date string, unlike WNT). Whether this "
               "is the FULL daily show vs. a long segment is inferred from the "
               "/streams tab + duration window, not confirmed against an explicit "
               "'full episode' label — see script header. Captions carry ASR "
               "spellings; a market-word matcher must expect near-misses, same as "
               "the WNT pattern this was copied from."),
}, open(out, "w"), indent=1)
PYMETA

  # ── audio ───────────────────────────────────────────────────────────────
  if (( NO_AUDIO == 0 )); then
    if ! yt-dlp -f bestaudio -o "$OUT/source.%(ext)s" --no-progress -q \
          "https://www.youtube.com/watch?v=$VID" 2>"$OUT/ytdlp.err"; then
      note "$TICK: ⛔ audio download failed (see $OUT/ytdlp.err)"; rc=4; continue
    fi
    SRC="$(find "$OUT" -maxdepth 1 -name 'source.*' ! -name '*.err' | head -1)"
    [[ -s "$SRC" ]] || { note "$TICK: ⛔ no audio file after download"; rc=4; continue; }
    if ! ffmpeg -v error -y -i "$SRC" -vn -ac 1 -ar 16000 -c:a pcm_s16le "$OUT/audio.wav" 2>"$OUT/ffmpeg.err"; then
      note "$TICK: ⛔ decode failed (see $OUT/ffmpeg.err)"; rc=4; continue
    fi
    rm -f "$SRC"
    note "$TICK: audio banked ($(du -h "$OUT/audio.wav" | cut -f1))"
  fi

  note "$TICK: ✓ banked $OUT"
done

exit $rc
