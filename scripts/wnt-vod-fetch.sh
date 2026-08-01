#!/usr/bin/env bash
# Bank the ACTUAL ABC World News Tonight broadcast, per session.
#
# WHY THIS EXISTS. The 2026-07-29 capture recorded ABC News Live — a different
# 24/7 streaming channel that does NOT carry WNT at 22:30Z (VERIFICATIONS
# Q-WNT-SRC). Every accuracy and race number derived from it was void. The
# linear ABC network feed is unreachable from here (no US hardware; the live-TV
# streamers need a US IP + US billing). But ABC publishes the broadcast itself,
# free, as a same-night YouTube VOD:
#
#   "ABC World News Tonight with David Muir Full Broadcast - July 29, 2026"
#
# That is the right programme. It is far too late to trade (posted after air,
# and the board is swept in under a second — Q-WNT-CLOSE), but it is exactly
# what the detector work needed and never had: real broadcast audio, with the
# settled Kalshi board as truth. It is also leg 1 of the pack-lag measurement
# (scripts/wnt-pack-lag.py), which needs word offsets inside the programme.
#
# ⚠ THE VOD HAS ADVERTISEMENTS REMOVED. Runtime is ~1170-1220 s against a
# 22-minute slot. So a word's offset here is PROGRAMME time, not
# wall-clock-minus-22:30Z. Anything mapping these offsets to UTC must model the
# ad breaks; wnt-pack-lag.py does, and refuses to report when it cannot.
#
# WORD TIMINGS COME FROM YOUTUBE'S OWN CAPTIONS, NOT FROM WHISPER. The json3
# caption track is word-level timed, downloads in about a second, and costs no
# CPU. Local faster-whisper needed 25+ min for one session on a contended box,
# which makes a 16-session backfill impossible to run alongside live event arms.
# The captions were checked against the settled 26JUL29 board and carry every
# word that settled YES.
#
# ⚠ CAPTIONS CARRY ASR SPELLINGS. On 26JUL29 "Todd Blanche" is captioned
# "Blanch" and "David Muir" as "David Mure". So a word the market settled YES
# can be genuinely present and still fail an exact string match — which is why
# wnt-pack-lag.py falls back to a bounded fuzzy match and LABELS every fuzzy hit
# rather than quietly counting or quietly dropping it.
#
# --asr re-derives timings locally with faster-whisper when caption quality is
# in question. It is off by default and it is slow; do not run it during an
# event window.
#
# Usage:
#   scripts/wnt-vod-fetch.sh 26JUL29 [26JUL28 ...]
#   scripts/wnt-vod-fetch.sh --settled             # every settled Kalshi session
#   scripts/wnt-vod-fetch.sh --settled --no-audio  # captions only (fast, no CPU)
#   scripts/wnt-vod-fetch.sh 26JUL29 --asr         # add a local whisper pass
#
# Output per session (corpus/wnt/ is gitignored — audio stays local):
#   corpus/wnt/<yyMONdd>/audio.wav        16 kHz mono s16le (unless --no-audio)
#   corpus/wnt/<yyMONdd>/meta.json        video id/title/duration/provenance
#   corpus/wnt/<yyMONdd>/words.json       [{word,start,end,char_*}] programme time
#   corpus/wnt/<yyMONdd>/transcript.txt   flat text, for the identity gate
#
# Exit codes:
#   0 ok   2 preconditions bad   3 no VOD found for a requested date
#   4 audio download failed      5 captions unavailable      6 ASR failed
set -uo pipefail

REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO" || { echo "FATAL: no repo at $REPO" >&2; exit 2; }

SERIES=KXWORLDNEWSMENTION
API=https://api.elections.kalshi.com/trade-api/v2
CHANNEL="https://www.youtube.com/@ABCNews/search"
# The full broadcast runs ~1170-1220 s with ads stripped. Clips from the same
# programme carry near-identical titles, so DURATION is the discriminator that
# actually separates them — a title match alone picks up 2-minute excerpts.
MIN_SECS="${WNT_VOD_MIN_SECS:-900}"
MAX_SECS="${WNT_VOD_MAX_SECS:-1500}"
SEARCH_DEPTH="${WNT_VOD_SEARCH_DEPTH:-12}"
ASR_MODEL="${WNT_ASR_MODEL:-small.en}"
PARSER="scripts/wnt-captions-parse.py"

DO_ASR=0
NO_AUDIO=0
FORCE=0
DATES=()
for a in "$@"; do
  case "$a" in
    --asr)      DO_ASR=1 ;;
    --no-audio) NO_AUDIO=1 ;;
    --force)    FORCE=1 ;;
    --settled)  DATES+=("__SETTLED__") ;;
    -h|--help)  sed -n '2,55p' "$0"; exit 0 ;;
    --*)        echo "usage: wnt-vod-fetch.sh [--settled] [<yyMONdd>...] [--no-audio] [--asr] [--force]" >&2; exit 2 ;;
    *)          DATES+=("$(tr '[:lower:]' '[:upper:]' <<<"$a")") ;;
  esac
done
(( ${#DATES[@]} )) || { echo "usage: wnt-vod-fetch.sh [--settled] [<yyMONdd>...]" >&2; exit 2; }

for t in curl python3 yt-dlp; do
  command -v "$t" >/dev/null || { echo "FATAL: missing $t" >&2; exit 2; }
done
[[ -f "$PARSER" ]] || { echo "FATAL: missing $PARSER" >&2; exit 2; }
if (( NO_AUDIO == 0 )); then
  command -v ffmpeg >/dev/null || { echo "FATAL: missing ffmpeg (or pass --no-audio)" >&2; exit 2; }
fi
if (( DO_ASR == 1 )) && [[ ! -x .venv-corpus/bin/python ]]; then
  echo "FATAL: --asr needs .venv-corpus (python3 -m venv .venv-corpus && .venv-corpus/bin/pip install faster-whisper)" >&2
  exit 2
fi

note() { echo "[$(date -u +%T)] $*"; }

# Expand --settled into the real settled event list rather than a date range:
# the series does not run every weekday (3 of 18 in July had no event), so a
# generated range would request VODs for sessions that never had a market.
expand_settled() {
  curl -s --max-time 30 "$API/events?series_ticker=$SERIES&limit=200" | python3 -c "
import sys, json
try: d = json.load(sys.stdin)
except Exception: sys.exit(1)
print('\n'.join(sorted({e['event_ticker'].rsplit('-', 1)[1] for e in d.get('events', [])})))"
}

# ⚠ bash 3.2 ONLY. macOS ships /bin/bash 3.2.57 and `#!/usr/bin/env bash`
# resolves to it, so `mapfile`/`readarray` do not exist and `${arr[-1]}` is a
# syntax error. Both were here and both failed only on the --settled path,
# which no earlier run had exercised. Keep this loop portable.
expanded=()
for d in "${DATES[@]}"; do
  if [[ "$d" == "__SETTLED__" ]]; then
    s=()
    while IFS= read -r line; do
      [[ -n "$line" ]] && s+=("$line")
    done < <(expand_settled)
    (( ${#s[@]} )) || { echo "FATAL: could not list settled $SERIES events" >&2; exit 2; }
    note "--settled: ${#s[@]} sessions (${s[0]}..${s[$(( ${#s[@]} - 1 ))]})"
    expanded+=("${s[@]}")
  else
    expanded+=("$d")
  fi
done
(( ${#expanded[@]} )) || { echo "FATAL: no sessions to fetch" >&2; exit 2; }

rc=0
for TICK in "${expanded[@]}"; do
  SLUG="$(tr '[:upper:]' '[:lower:]' <<<"$TICK")"
  OUT="corpus/wnt/$SLUG"
  if [[ -s "$OUT/words.json" && $FORCE -eq 0 ]]; then
    note "$TICK: already banked ($OUT) — skip (--force to redo)"
    continue
  fi
  # 26JUL29 -> "July 29, 2026" plus the query terms for the channel search.
  read -r MONTH DAY YEAR QUERY < <(python3 - "$TICK" <<'PYDATE'
import datetime as dt, sys
d = dt.datetime.strptime(sys.argv[1], "%y%b%d")
print(d.strftime("%B"), d.day, d.year, f"{d.strftime('%B')}+{d.day}")
PYDATE
)
  [[ -n "${YEAR:-}" ]] || { note "$TICK: unparseable session date"; rc=3; continue; }

  note "$TICK: searching @ABCNews for '$MONTH $DAY, $YEAR'"
  # Per-date query, not a channel crawl: ABC posts ~25 videos/day, so reaching
  # three weeks back by paging /videos would mean hundreds of entries. The
  # dated query puts the target at rank 1 and keeps this to one request.
  #
  # The listing goes to a FILE, not a pipe: `cmd | python3 - <<'X'` gives the
  # heredoc and the pipe the same fd, the heredoc wins, and the matcher reads
  # zero rows — a silent "not found" on every date.
  #
  # Space-separated, split from the left: yt-dlp's --print emits a LITERAL "\t"
  # for \t rather than a tab, so a tab template silently yields one field.
  # id and duration never contain spaces; the title takes the remainder.
  LIST="$(mktemp)"
  yt-dlp --flat-playlist --playlist-end "$SEARCH_DEPTH" \
          --print "%(id)s %(duration)s %(title)s" \
          "$CHANNEL?query=World+News+Tonight+Full+Broadcast+$QUERY" 2>/dev/null >"$LIST"
  HIT="$(python3 - "$LIST" "$MONTH" "$DAY" "$YEAR" "$MIN_SECS" "$MAX_SECS" <<'PYMATCH'
import re, sys
listing = sys.argv[1]
month, day, year, lo, hi = sys.argv[2], int(sys.argv[3]), sys.argv[4], int(sys.argv[5]), int(sys.argv[6])
# Optional ordinal suffix, optional comma: "July 19th, 2026" and "July 25 2026"
# both occur on this channel.
date_re = re.compile(rf"{month}\s+{day}(?:st|nd|rd|th)?,?\s+{year}", re.I)
hits = {}
for line in open(listing):
    parts = line.rstrip("\n").split(None, 2)
    if len(parts) != 3:
        continue
    vid, dur, title = parts
    if "world news tonight" not in title.lower():
        continue
    if not date_re.search(title):
        continue
    try:
        secs = int(float(dur))
    except ValueError:
        continue
    # Duration is what separates the broadcast from same-titled excerpts.
    if not (lo <= secs <= hi):
        continue
    hits.setdefault(vid, (secs, title))
if not hits:
    sys.exit(1)
# YouTube surfaces one video under several headline titles; identity is the id.
# Two DIFFERENT ids of broadcast length for one date means the assumption
# "one broadcast per session" broke — refuse rather than pick.
if len(hits) > 1:
    print("AMBIGUOUS " + " ".join(sorted(hits)), file=sys.stderr)
    sys.exit(2)
vid, (secs, title) = next(iter(hits.items()))
print(f"{vid}\t{secs}\t{title}")
PYMATCH
)"
  MATCH_RC=$?
  rm -f "$LIST"
  case $MATCH_RC in
    1) note "$TICK: ⛔ no full-broadcast VOD found for $MONTH $DAY, $YEAR"; rc=3; continue ;;
    2) note "$TICK: ⛔ multiple distinct broadcast-length VODs — refusing to guess"; rc=3; continue ;;
  esac
  VID="$(cut -f1 <<<"$HIT")"; DUR="$(cut -f2 <<<"$HIT")"; TITLE="$(cut -f3 <<<"$HIT")"
  note "$TICK: $VID (${DUR}s) — $TITLE"

  mkdir -p "$OUT"

  # ── captions: the word-timing source ──────────────────────────────────────
  # en-orig is the ORIGINAL-language auto-caption track. Asking for "en" alone
  # can resolve to a machine TRANSLATION on some videos, which would hand us
  # English words that are not the words that were said.
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
    "event_ticker": f"KXWORLDNEWSMENTION-{tick}",
    "video_id": vid,
    "title": title,
    "url": f"https://www.youtube.com/watch?v={vid}",
    "duration_secs": int(dur),
    "word_source": f"youtube-auto-captions ({cap})",
    "word_count": int(nwords),
    "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "programme": "ABC World News Tonight with David Muir (linear ABC network)",
    "caveat": ("VOD has advertisements removed; offsets are programme time, not wall clock. "
               "Captions carry ASR spellings (26JUL29: 'Blanche'->'Blanch', 'Muir'->'Mure')."),
}, open(out, "w"), indent=1)
PYMETA

  # ── audio: needed to BENCH the detector, not to measure lag ───────────────
  if (( NO_AUDIO == 0 )); then
    if ! yt-dlp -f bestaudio -o "$OUT/source.%(ext)s" --no-progress -q \
          "https://www.youtube.com/watch?v=$VID" 2>"$OUT/ytdlp.err"; then
      note "$TICK: ⛔ audio download failed (see $OUT/ytdlp.err)"; rc=4; continue
    fi
    SRC="$(find "$OUT" -maxdepth 1 -name 'source.*' ! -name '*.err' | head -1)"
    [[ -s "$SRC" ]] || { note "$TICK: ⛔ no audio file after download"; rc=4; continue; }
    # 16 kHz mono matches every detector config's resample_to_hz, so the same
    # wav feeds bench-detector and faster-whisper without a second conversion.
    if ! ffmpeg -v error -y -i "$SRC" -vn -ac 1 -ar 16000 -c:a pcm_s16le "$OUT/audio.wav" 2>"$OUT/ffmpeg.err"; then
      note "$TICK: ⛔ decode failed (see $OUT/ffmpeg.err)"; rc=4; continue
    fi
    rm -f "$SRC"
    note "$TICK: audio banked ($(du -h "$OUT/audio.wav" | cut -f1))"
  fi

  # ── optional local ASR, when caption quality is in question ───────────────
  if (( DO_ASR == 1 )); then
    if [[ ! -s "$OUT/audio.wav" ]]; then
      note "$TICK: --asr needs audio; skipping (was --no-audio given?)"; continue
    fi
    # Reuse the corpus node's transcriber rather than a second whisper call
    # site, so the word/char sidecar shape stays identical across the project.
    #
    # ⚠ THE ID IS "WNTVOD-", NOT "ABCWNT-", AND THAT IS LOAD-BEARING. macOS is
    # case-insensitive, so transcript-fetch.py writing ABCWNT-26JUL29.words.json
    # SILENTLY OVERWRITES corpus/transcripts/abcwnt-26jul29.words.json — the
    # 2026-07-29 ABC News Live transcript that is the evidence for the
    # Q-WNT-SRC retraction. Replacing the wrong-source proof with right-source
    # audio under the same name is exactly the kind of quiet substitution that
    # let the original error stand. Distinct prefix, distinct programme.
    #
    # ⚠ SLOW. Measured 25+ min for one 20-minute session on a contended box.
    # OMP_NUM_THREADS is capped below the core count because this machine also
    # runs live event arms, and a backfill must never starve one.
    if ! OMP_NUM_THREADS="${WNT_ASR_THREADS:-6}" .venv-corpus/bin/python scripts/transcript-fetch.py \
          --audio "$OUT/audio.wav" --id "WNTVOD-$TICK" --model "$ASR_MODEL" >"$OUT/asr.log" 2>&1; then
      note "$TICK: ⛔ transcription failed (see $OUT/asr.log)"; rc=6; continue
    fi
    cp "corpus/transcripts/WNTVOD-$TICK.words.json" "$OUT/words.asr.json"
    cp "corpus/transcripts/WNTVOD-$TICK.txt"        "$OUT/transcript.asr.txt"
    note "$TICK: ASR sidecar written (words.asr.json)"
  fi
  note "$TICK: ✓ banked $OUT"
done

exit $rc
