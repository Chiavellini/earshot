#!/usr/bin/env bash
# Daily EVIDENCE job for ABC World News Tonight — runs AFTER the broadcast.
#
# Market: KXWORLDNEWSMENTION-<yyMONdd> — "any host or reporter - ABC World News
# Tonight with David Muir". Airs 22:30Z = 16:30 CDMX, ~22 min.
# Confirmed daily (VERIFICATIONS Q-WNT): 16 settled events 26JUL06..26JUL29 =
# 15 of 18 weekdays (gaps 07-13/17/20) plus one Saturday. The series' own
# `frequency: one_off` is stale metadata — do not trust it.
#
# ⛔ WHY THIS NO LONGER CAPTURES A LIVE STREAM (VERIFICATIONS Q-WNT-SRC).
# Until 2026-07-30 this job raced the 22:30Z window on
# youtube.com/@ABCNews/live. That stream is ABC News **Live**, a different 24/7
# channel, and it does NOT carry World News Tonight at 22:30Z — our own whisper
# transcript found "world news tonight"/"david muir" only inside an ADVERT at
# 22:24:34Z, and the four words that SETTLED YES (blanche, maintain, unchanged,
# midterm) appear ZERO times in 8 337 transcribed words. Everything derived
# from that capture, including a "+12.9 s race win", was retracted.
#
# The market resolves on the LINEAR ABC TELEVISION NETWORK feed, which is not
# reachable from here: no US hardware for an antenna, and every live-TV
# streaming route needs a US IP + US billing while running 30-90 s behind
# broadcast — against a book that is swept in UNDER ONE SECOND (Q-WNT-CLOSE).
# There is no live capture worth racing, so this job stopped pretending to.
#
# WHAT IT DOES INSTEAD. ABC publishes the real broadcast free, same night, as
# a YouTube VOD (Q-WNT-VOD). This job banks that, plus the settled tape, plus
# the pack-lag measurement:
#
#     prepare -> readiness gate -> VOD fetch+ASR -> identity gate -> tape
#             -> scripts/wnt-pack-lag.py
#
# Consequences of the change, both deliberate:
#   * IT IS NO LONGER LONG-LIVED. The old design was one ~7 h process because
#     the clock anchor (CLOCK_UPTIME_RAW <-> UTC) had to be taken in the same
#     awake session as the capture. There is no capture and no capture_ns now —
#     VOD offsets are PROGRAMME time — so the anchor is gone and the job is a
#     few minutes long, fired after air.
#   * IT IS NO LONGER A RACE. Nothing here is time-critical; a run that slips
#     an hour loses nothing.
#
# RATE LIMITS ARE REAL. discover-events.py's own header: the events endpoint
# 429s under rapid repeat queries and its dual sweep is ~135 requests. This job
# uses ONE per-series query, never the sweep.
#
# NO ORDERS. There is no --i-am-live, no --fillable, no order path anywhere in
# this script, by design. Its job is to bank ONE labelled dataset per session —
# real broadcast audio + the settlement-truthed tape — so the accuracy work has
# material it did not have. Arming is a separate, later decision, and on the
# current evidence the live race is not ours to enter.
#
# Deliberately NOT calling preflight-event.sh: it runs a full
# `cargo build --release`, three live probes, and SENDS AN EMAIL on every run.
# Unconditional daily use would mean a daily rebuild and a daily email. It
# belongs downstream of arming.
#
# Exit codes (distinct so the log says which stage failed):
#   0 ok (including "no event today" — a legitimate no-op)
#   2 preconditions bad (tools/creds/repo)
#   3 no event listed for today
#   4 prepare failed
#   5 VOD unavailable (not posted yet, or no match)
#   6 wrong programme — the banked audio failed the identity gate
set -uo pipefail

REPO="${EARSHOT_REPO:-$HOME/Code/ear}"
cd "$REPO" || { echo "FATAL: no repo at $REPO"; exit 2; }

DRY=0
NO_CAPTURE=0
for a in "$@"; do
  case "$a" in
    --dry-run)    DRY=1 ;;
    --no-capture) NO_CAPTURE=1 ;;   # prepare + readiness gate only; skips the
                                    # VOD fetch, ASR, tape and lag measurement
    *) echo "usage: wnt-daily.sh [--dry-run] [--no-capture]" >&2; exit 2 ;;
  esac
done

SERIES=KXWORLDNEWSMENTION
API=https://api.elections.kalshi.com/trade-api/v2
# The event is listed SAME-DAY and the listing time moves (measured open_time
# spans 16:10Z..21:55Z). This job now runs AFTER air, by which time the listing
# has long since happened — so a short confirmation poll replaces the old
# all-day one. A miss here means there was genuinely no session.
POLL_DEADLINE_MINS="${WNT_POLL_MINS:-20}"
POLL_SECS="${WNT_POLL_SECS:-120}"
# The VOD is posted after the broadcast, not instantly. Retry rather than
# declaring a session unavailable on the first look.
VOD_TRIES="${WNT_VOD_TRIES:-6}"
VOD_RETRY_SECS="${WNT_VOD_RETRY_SECS:-600}"
DATE_UTC="$(date -u +%Y-%m-%d)"
TICK_DATE="$(date -u +%y%b%d | tr '[:lower:]' '[:upper:]')"
# WNT_EVENT overrides the event for replays of a missed/past day (e.g.
# WNT_EVENT=KXWORLDNEWSMENTION-26JUL29 with --no-capture). The date-derived
# default is the normal path.
EV="${WNT_EVENT:-$SERIES-$TICK_DATE}"
TICK_DATE="${EV##*-}"
SLUG="$(tr '[:upper:]' '[:lower:]' <<<"$TICK_DATE")"
# Working dir for per-run reports. The broadcast audio itself lands in the
# repo's gitignored corpus/wnt/<slug>/, where bench-detector and
# wnt-pack-lag.py look for it.
WNT_DIR="$HOME/abcwnt/$DATE_UTC"
VOD_DIR="$REPO/corpus/wnt/$SLUG"
LOG="$HOME/wnt-daily-$DATE_UTC.log"

exec > >(tee -a "$LOG") 2>&1
echo "=== wnt-daily $(date -u +%FT%TZ)  event=$EV  dry_run=$DRY ==="

note() { echo "[$(date -u +%T)] $*"; }
page() { [[ $DRY -eq 1 ]] && { note "DRY: would page: $1"; return 0; }
         "$REPO/scripts/notify.sh" "$1" "$2" --quiet || true; }

# ── preconditions ───────────────────────────────────────────────────────────
for t in curl python3 yt-dlp ffmpeg; do
  command -v "$t" >/dev/null || { note "FATAL: missing $t"; page "WNT daily: missing $t" "Cannot run."; exit 2; }
done
BIN="${EARSHOT_BIN:-$REPO/target/release/earshot}"
[[ -x "$BIN" ]] || { note "FATAL: no binary at $BIN"; page "WNT daily: no earshot binary" "Expected $BIN"; exit 2; }
# `prepare` hits the live API and needs creds; a missing env is a hard stop, not
# a silent skip.
if [[ -f "$HOME/.kalshi/earshot_env" ]]; then
  # shellcheck disable=SC1091
  source "$HOME/.kalshi/earshot_env"
else
  note "FATAL: no ~/.kalshi/earshot_env"; page "WNT daily: no credentials" "prepare needs live creds."; exit 2
fi
# streaming_ctc dlopens onnxruntime. Missing ORT hangs the detector build AFTER
# other setup, which cost the phone lane a whole event on 07-29 — resolve it up
# front and refuse loudly.
if [[ -z "${ORT_DYLIB_PATH:-}" ]]; then
  # -not -path "*dSYM*" is NOT optional: vendor/ ships a debug-symbols bundle
  # containing a file with the SAME NAME as the real library
  # (…​.dylib.dSYM/Contents/Resources/DWARF/libonnxruntime.1.27.1.dylib). A
  # plain find returns that first, and a wrong-platform / non-loadable lib
  # hangs dlopen identically to a missing one — the 07-29 phone-lane failure.
  # variant-study.sh already guards this the same way.
  case "$(uname -s)" in
    Darwin) ORT_DYLIB_PATH="$(find "$REPO/vendor" -name 'libonnxruntime*.dylib' -not -path '*dSYM*' 2>/dev/null | head -1)" ;;
    *)      ORT_DYLIB_PATH="$(find "$REPO/vendor" -name 'libonnxruntime*.so*'   -not -path '*dSYM*' 2>/dev/null | head -1)" ;;
  esac
  export ORT_DYLIB_PATH
fi
case "$ORT_DYLIB_PATH" in
  *dSYM*) note "FATAL: resolved ORT path is a dSYM bundle, not a library: $ORT_DYLIB_PATH"
          page "WNT daily: bad ORT path" "Resolved a dSYM debug bundle instead of the dylib."; exit 2 ;;
esac
[[ -n "${ORT_DYLIB_PATH:-}" ]] || { note "FATAL: no onnxruntime in vendor/"; page "WNT daily: no ORT dylib" "Run scripts/fetch-models.sh"; exit 2; }
note "ORT_DYLIB_PATH=$ORT_DYLIB_PATH"

# ── step 1: confirm today's event exists (ONE request per poll) ──────────────
# ⚠ NO status FILTER. The old version queried `status=open`, which was right
# for a job that ran BEFORE air. This one runs after, when the board has closed
# and settled, and `status=open` would report every session as "no event today".
deadline=$(( $(date -u +%s) + POLL_DEADLINE_MINS * 60 ))
note "confirming $EV is listed (up to ${POLL_DEADLINE_MINS} min)"
found=0
while :; do
  if curl -s --max-time 20 "$API/events?series_ticker=$SERIES&limit=200" \
     | python3 -c "
import sys,json
try: d=json.load(sys.stdin)
except Exception: sys.exit(1)
sys.exit(0 if any(e.get('event_ticker')=='$EV' for e in d.get('events',[])) else 1)"; then
    found=1; note "event listed: $EV"; break
  fi
  now=$(date -u +%s)
  if (( now >= deadline )); then note "deadline reached; $EV never listed"; break; fi
  [[ $DRY -eq 1 ]] && { note "DRY: would keep polling; stopping here"; break; }
  sleep "$POLL_SECS"
done
if (( found == 0 )); then
  # A no-show is NORMAL (3 of 18 weekdays had no event, and most weekends).
  # Report it without paging as a failure.
  note "no event today — no-op"
  page "WNT: no event listed $DATE_UTC" "KXWORLDNEWSMENTION had no event by ${POLL_DEADLINE_UTC}Z. 3 of 18 weekdays in July had none, so this is likely normal. Log: $LOG"
  [[ $DRY -eq 1 ]] && exit 0
  exit 3
fi

# ── step 2: prepare the config, on the PRODUCTION engine ────────────────────
CFG="configs/webcast-abcwnt-$(echo "$TICK_DATE" | tr '[:upper:]' '[:lower:]')-replay.toml"
if [[ $DRY -eq 1 ]]; then
  note "DRY: would run: $BIN prepare --event-ticker $EV --out $CFG"
else
  if ! "$BIN" prepare --event-ticker "$EV" --out "$CFG"; then
    note "FATAL: prepare failed"; page "WNT prepare FAILED" "earshot prepare failed for $EV. Log: $LOG"; exit 4
  fi
  # prepare emits engine = "sherpa_kws" and only that detector block, but
  # production runs streaming_ctc (19 of 30 arm configs, all the recent ones).
  # Benchmarking the wrong engine is exactly the error that made 2026-07-29's
  # zero-recall result unactionable, so pin the production engine here and keep
  # the kws block for a one-line revert.
  python3 - "$CFG" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p).read()
if 'engine = "streaming_ctc"' in s:
    sys.exit(0)
ctc = '''engine = "streaming_ctc"

[detector.streaming_ctc]
model_dir         = "models/sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18"
num_threads       = 1
default_threshold = 0.15
refractory_ms     = 2000

# kws block kept so the engine reverts with a one-line change above.
'''
s = s.replace('engine = "sherpa_kws"\n', ctc, 1)
open(p, "w").write(s)
print(f"pinned streaming_ctc in {p}")
PY
  note "prepared $CFG"
  # Record which words were SKIPPED — `prepare` refuses multi-count strikes
  # ("Trump (5+ times)"), which appear EVERY day and carry the most volume.
  # They are untradeable until DecisionTable gets an occurrence counter.
  grep -c "SKIPPED" "$CFG" >/dev/null 2>&1 && grep "SKIPPED" "$CFG" | sed 's/^/    /'
fi

# ── step 3: keyword readiness gate (report, do not block) ───────────────────
# variant-study.sh exits 1 if any keyword is DEAD on clean studio TTS. A dead
# word will not fire on a live compressed webcast either (BAKEOFF standing
# rule), so we want to know BEFORE the show — but a capture containing known-
# dead words is still a valid labelled dataset, so this never blocks.
if [[ $DRY -eq 1 ]]; then
  note "DRY: would run scripts/variant-study.sh on $CFG"
elif [[ -x scripts/variant-study.sh && "$(uname -s)" == "Darwin" ]]; then
  VS_OUT="$WNT_DIR/variant-study"
  mkdir -p "$VS_OUT"
  # positional args: variant-study.sh <config.toml> [outdir]
  if scripts/variant-study.sh "$CFG" "$VS_OUT" >"$VS_OUT/run.log" 2>&1; then
    note "variant-study: all keywords fired on TTS"
  else
    DEAD="$(awk -F'\t' '$4=="FAIL"{print $2}' "$VS_OUT/report.tsv" 2>/dev/null | paste -sd, -)"
    note "variant-study: DEAD words: ${DEAD:-<see $VS_OUT/run.log>}"
    page "WNT $DATE_UTC: dead keywords" "These market words scored 0 on clean studio TTS and will not fire live: ${DEAD:-see log}. Capture proceeds; these strikes are not armable. $VS_OUT"
  fi
else
  note "variant-study skipped (not Darwin, or script missing)"
fi

if (( NO_CAPTURE == 1 )); then
  note "--no-capture: stopping after prepare + readiness gate (config: $CFG)"
  exit 0
fi

# ── step 4: bank the REAL broadcast (VOD) ───────────────────────────────────
# Not a live capture. The linear ABC feed is unreachable (Q-WNT-SRC) and the
# free ABC News Live stream carries a different programme at 22:30Z. ABC posts
# the actual newscast the same night (Q-WNT-VOD); wnt-vod-fetch.sh resolves it
# by dated channel search, banks 16 kHz mono audio, and transcribes it.
#
# yt-dlp against googlevideo bakes the requesting IP into the URLs and
# bot-walls datacenter IPs, so this — like the old capture — runs on the
# laptop, not the us-east box.
if [[ $DRY -eq 1 ]]; then
  note "DRY: would run scripts/wnt-vod-fetch.sh $TICK_DATE"
else
  ok=0
  for (( try=1; try<=VOD_TRIES; try++ )); do
    if scripts/wnt-vod-fetch.sh "$TICK_DATE"; then ok=1; break; fi
    if (( try < VOD_TRIES )); then
      note "VOD not available yet (try $try/$VOD_TRIES); retrying in ${VOD_RETRY_SECS}s"
      sleep "$VOD_RETRY_SECS"
    fi
  done
  if (( ok == 0 )); then
    note "FATAL: no full-broadcast VOD for $TICK_DATE after $VOD_TRIES tries"
    page "WNT $DATE_UTC — no VOD" "ABC did not post a full-broadcast VOD for $TICK_DATE within $(( VOD_TRIES * VOD_RETRY_SECS / 60 )) min. Nothing banked. Log: $LOG"
    exit 5
  fi
  note "banked $VOD_DIR"
fi

# ── step 5: SOURCE-IDENTITY GATE (mandatory — VERIFICATIONS Q-WNT-SRC) ──────
# The 2026-07-29 failure was a wrong-programme failure that survived cross-leg
# corroboration, because every leg read the same URL and agreeing legs prove
# only that they heard the same audio, never that it is the RIGHT audio. The
# VOD is titled as the broadcast, but a title is a claim, not evidence — so the
# gate still runs, now against the transcript we already have rather than a
# fresh 90 s whisper pass.
#
# It should pass trivially. That is the point: the day it does not, something
# upstream changed and we find out at bank time, not at analysis time.
# ⚠ THE TIME WINDOW IS THE WHOLE POINT, and it nearly got lost in the retarget.
# The old live-capture gate sliced 22:31-22:33Z before matching, and that window
# is what made it work: in the 2026-07-29 wrong-source capture "world news
# tonight"/"david muir" DO appear — at 22:24:19-22:24:30Z, inside the advert —
# and "good evening" appears at 23:01:26Z in later programming. A whole-file
# match would have PASSED that capture. So the VOD gate keeps an equivalent
# constraint: the signature must land in the COLD OPEN. On the real 26JUL29 VOD
# the billboard is at 0.2-2.5 s ("David Mure, ABC's World News Tonight").
#
# Second, independent condition: ABC News Live's own show signatures must be
# ABSENT. Those are what the wrong capture was full of ("top searches" 22:41,
# "welcome back" 22:43). One test is presence of the right thing, the other
# absence of the known wrong thing; a single test would be a weaker guard.
if [[ $DRY -eq 1 ]]; then
  note "DRY: would run the source-identity gate over $VOD_DIR/words.json"
elif [[ -s "$VOD_DIR/words.json" ]]; then
  ID_OUT="$(python3 - "$VOD_DIR/words.json" "${WNT_COLDOPEN_SECS:-90}" <<'PYID'
import json, re, sys
words = json.load(open(sys.argv[1]))
head_s = float(sys.argv[2])
norm = lambda s: re.sub(r"[^a-z ]", "", s.lower())
head = norm(" ".join(w["word"] for w in words if w["start"] <= head_s))
allt = norm(" ".join(w["word"] for w in words))
# "Muir" is captioned "Mure"; match the surname loosely rather than exactly.
sig = [s for s in ("world news tonight", "david mu", "good evening") if s in head]
bad = [s for s in ("top searches", "the spiral", "welcome back to search") if s in allt]
print(("PASS" if sig and not bad else "FAIL")
      + "|" + ",".join(sig) + "|" + ",".join(bad))
PYID
)"
  case "$ID_OUT" in
    PASS*) note "SOURCE OK: cold-open signature [${ID_OUT#PASS|}] and no ABC-News-Live show markers"
           : > "$VOD_DIR/idcheck.PASS" ;;
    *)     note "⛔ WRONG PROGRAMME: $ID_OUT — $VOD_DIR is NOT the market's audio"
           : > "$VOD_DIR/idcheck.FAIL"
           page "WNT $DATE_UTC — WRONG PROGRAMME banked" \
"The VOD banked for $TICK_DATE has no World News Tonight signature in its cold open,
and/or carries ABC News Live's own show markers, so it is not the market's audio
(VERIFICATIONS Q-WNT-SRC). It must NOT be scored as a recall or race measurement.
Gate output: $ID_OUT
Dir: $VOD_DIR   Log: $LOG"
           exit 6 ;;
  esac
else
  note "source-identity gate SKIPPED — no words.json"
fi

# ── step 6: bank the settlement-truthed tape ────────────────────────────────
# No pre-sleep: the old one waited for "the early closes to land", and there
# are none — the board closes in a single ~23:05Z batch, YES and NO together
# (Q-WNT-CLOSE). This job already runs after that.
SAID="(not run)"
if [[ $DRY -eq 1 ]]; then
  note "DRY: would run scripts/fetch-tape.sh $EV"
else
  mkdir -p "$WNT_DIR"
  scripts/fetch-tape.sh "$EV" >/dev/null 2>&1 \
    && note "tape banked: state/$EV/" \
    || { note "WARN: fetch-tape failed"; page "WNT tape fetch failed" "Audio is banked but the tape is not; re-run scripts/fetch-tape.sh $EV. Log: $LOG"; }
  SAID="$(python3 - "$EV" <<'PY'
import json, sys, urllib.request
ev = sys.argv[1]
try:
    d = json.load(urllib.request.urlopen(
        f"https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker={ev}&limit=200", timeout=25))
except Exception:
    print("unknown"); raise SystemExit
ms = d["markets"]
# Prefer the SETTLED result. The previous version inferred "said" from
# last_price >= 0.90, which is a proxy for what the market believed; this job
# runs after settlement, where the real answer is available.
settled = [m for m in ms if m.get("result") in ("yes", "no")]
if settled:
    said = [m["ticker"].rsplit("-", 1)[1] for m in settled if m["result"] == "yes"]
    print(f"{len(said)}/{len(ms)} SETTLED yes: {','.join(sorted(said))}")
else:
    said = [m["ticker"].rsplit("-", 1)[1] for m in ms
            if float(m.get("last_price_dollars") or 0) >= 0.90]
    print(f"{len(said)}/{len(ms)} implied-by-price (NOT yet settled): {','.join(sorted(said))}")
PY
)"
  note "outcome: $SAID"
fi

# ── step 7: how far behind the broadcast is the pack? ───────────────────────
# The one number that decides whether any obtainable feed could ever compete
# here. See scripts/wnt-pack-lag.py for the method and the refusal conditions.
LAG="(not run)"
if [[ $DRY -eq 1 ]]; then
  note "DRY: would run scripts/wnt-pack-lag.py $TICK_DATE"
elif [[ -s "$VOD_DIR/words.json" ]]; then
  if scripts/wnt-pack-lag.py "$TICK_DATE" --json "$WNT_DIR/pack-lag.json" >"$WNT_DIR/pack-lag.txt" 2>&1; then
    LAG="$(grep -E 'lag <=|NOT-RESOLVABLE' "$WNT_DIR/pack-lag.txt" | head -1)"
  else
    LAG="NOT-RESOLVABLE (see $WNT_DIR/pack-lag.txt)"
  fi
  note "pack lag: $LAG"
fi

page "WNT $DATE_UTC banked" "Real broadcast audio + tape banked for $EV.
$SAID
Pack lag: $LAG
Audio: $VOD_DIR   Reports: $WNT_DIR
NOT a capture of a live feed and NOT tradeable — the linear ABC feed is
unreachable and the book is swept in under a second (Q-WNT-CLOSE/Q-WNT-SRC).
Next: bench recall on real audio (manifest-from-words.py -> bench-detector).
Log: $LOG"

note "done"
exit 0
