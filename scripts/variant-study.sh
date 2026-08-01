#!/usr/bin/env bash
# variant-study.sh <config.toml> [outdir]
#
# Keyword-readiness helper (born 2026-07-29 after WING lost CITR to a Spanish
# pronunciation and AI to a dropped short form — both flagged in the config,
# neither enforced). For every [[keywords]] block in the config:
#   1. synthesize each variant with three macOS voices (say: Samantha + Daniel
#      + a Spanish voice — Paulina/Mónica — because Spanish-accented
#      pronunciations of menu items like "Citrus Mojo" are exactly the class
#      WING missed live; falls back to a third English voice when no Spanish
#      voice is installed, and says so in the artifact),
#   2. splice into one WAV per keyword (2.6 s gaps > refractory_ms),
#   3. replay through the REAL detector (config's own [detector] block,
#      confirmed_only policy) via `earshot replay`,
#   4. report detections vs expected clips per keyword, and
#   5. write the readiness artifact configs/.readiness/<basename>.json that
#      scripts/preflight-event.sh's BLOCKING keyword-readiness gate consumes
#      (keywords_sha256 via scripts/keywords-extract.py — the shared
#      extraction, so the gate recomputes the identical hash).
# Exit 1 if any keyword produced ZERO Confirmed detections. The artifact is
# written either way — a confirmed=0 entry is exactly what the gate must see.
#
# TTS proves the path is WIRED, not that live recall is 100% (LATENCY-PROOF
# D1g caveat). A keyword failing on clean studio TTS will not fire live.
# macOS only (uses `say`). Run from repo root.
set -uo pipefail
CFG="${1:?usage: variant-study.sh <config.toml> [outdir]}"
OUT="${2:-/tmp/variant-study-$(basename "$CFG" .toml)}"
BIN="${EARSHOT_BIN:-target/release/earshot}"
VOICES=(Samantha Daniel)
# Third voice: prefer an installed Spanish voice for pronunciation robustness
# (applied to ALL variants). Voice names can contain spaces, so match on the
# name+locale columns of `say -v ?` rather than awk field games for the
# preferred names.
VOICE_TABLE="$(say -v '?' 2>/dev/null || true)"
THIRD_VOICE="" THIRD_VOICE_NOTE=""
for CAND in Paulina Mónica Monica; do
  if grep -qE "^${CAND}[[:space:]]+[a-z]{2}_" <<<"$VOICE_TABLE"; then THIRD_VOICE="$CAND"; break; fi
done
if [[ -z "$THIRD_VOICE" ]]; then
  THIRD_VOICE="$(awk '$2 ~ /^es_/ {print $1; exit}' <<<"$VOICE_TABLE")"
fi
if [[ -z "$THIRD_VOICE" ]]; then
  THIRD_VOICE="$(awk '$2 ~ /^en_/ && $1 != "Samantha" && $1 != "Daniel" {print $1; exit}' <<<"$VOICE_TABLE")"
  [[ -n "$THIRD_VOICE" ]] && THIRD_VOICE_NOTE="no Spanish voice installed — third voice is English ($THIRD_VOICE); Spanish-accent coverage NOT exercised"
fi
if [[ -n "$THIRD_VOICE" ]]; then
  VOICES+=("$THIRD_VOICE")
  echo "variant-study: voices = ${VOICES[*]}${THIRD_VOICE_NOTE:+ (⚠ $THIRD_VOICE_NOTE)}"
else
  THIRD_VOICE_NOTE="only two voices available (Samantha + Daniel) — no third voice found"
  echo "⚠ variant-study: $THIRD_VOICE_NOTE" >&2
fi
mkdir -p "$OUT/clips" "$OUT/cfg" "$OUT/logs"

[[ -x "$BIN" ]] || { echo "REFUSING: no binary at $BIN (cargo build --release)" >&2; exit 2; }

# streaming_ctc loads onnxruntime dynamically; with ORT_DYLIB_PATH unset the
# process futex-hangs silently in dlopen (deploy-box.sh incident class).
if [[ -z "${ORT_DYLIB_PATH:-}" ]]; then
  ORT_DYLIB_PATH="$(find "$PWD/vendor/onnxruntime" -name "libonnxruntime.*.dylib" -not -path "*dSYM*" 2>/dev/null | head -1)"
  [[ -n "$ORT_DYLIB_PATH" ]] || { echo "REFUSING: ORT_DYLIB_PATH unset and no dylib under vendor/" >&2; exit 2; }
  export ORT_DYLIB_PATH
fi
echo "ORT_DYLIB_PATH=$ORT_DYLIB_PATH"

# 1) keyword table: TICKER<TAB>PHRASE<TAB>V1|V2|... — the SHARED extraction
# (scripts/keywords-extract.py). preflight-event.sh recomputes keywords_sha256
# through the same script, so the readiness artifact and the gate can never
# disagree about what "the keywords" are.
python3 "$(dirname "${BASH_SOURCE[0]}")/keywords-extract.py" "$CFG" > "$OUT/keywords.tsv"
KW_SHA=$(python3 "$(dirname "${BASH_SOURCE[0]}")/keywords-extract.py" "$CFG" --sha256)
N_KW=$(wc -l < "$OUT/keywords.tsv" | tr -d ' ')
echo "variant-study: $N_KW keywords from $CFG → $OUT (keywords sha256 ${KW_SHA:0:12}…)"

# detector block mirrored verbatim from the source config.
# 2026-07-30: was `sed -n '/^\[detector\]/,/^\[arbiter\]/p' | sed '$d'`, which
# assumed every config has an [arbiter] section. `earshot prepare` output does
# NOT (it goes [detector] -> [[sources]] -> [kalshi] -> [risk]), so the range
# ran to EOF and the mini-config inherited [[sources]]/[kalshi]/[[keywords]]
# too — invalid under deny_unknown_fields. The readiness gate could therefore
# not be run on a freshly prepared config, which is exactly when it is needed.
# Stop instead at the first top-level section that is not [detector...].
awk '/^\[detector\]/{f=1} f && /^\[/ && !/^\[detector/{exit} f' "$CFG" > "$OUT/cfg/detector.block"
[[ -s "$OUT/cfg/detector.block" ]] || { echo "REFUSING: no [detector] block in $CFG" >&2; exit 2; }
ENGINE=$(sed -n '/^\[detector\]/,/^\[/s/^engine[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CFG" | head -1)

SIL="$OUT/clips/_sil.wav"
ffmpeg -v error -y -f lavfi -i anullsrc=r=16000:cl=mono -t 2.6 -c:a pcm_s16le "$SIL"

FAILS=0
REPORT="$OUT/report.tsv"
: > "$REPORT"
while IFS=$'\t' read -r TICKER PHRASE VARIANTS; do
  # ⚠ STDIN DISCIPLINE (fixed 2026-07-30): every subprocess in this loop gets
  # </dev/null, and ffmpeg additionally gets -nostdin. This loop reads its
  # keyword list from `done < "$OUT/keywords.tsv"`, so ANY child that touches
  # stdin eats bytes out of the NEXT keyword's line. Measured live today on the
  # RDDT study under a loaded laptop: ffmpeg swallowed the leading bytes of the
  # last two tickers ("KXEARN…"→"INGSMENTION…-TIKT", "K…"→"XEARNINGS…-TRAF"),
  # those rows then failed to match the config in the artifact builder below,
  # and TIKT/TRAF were written confirmed=0 — two phantom DEAD words that would
  # have FAILED the blocking preflight gate on an event whose detector had in
  # fact fired 3/9 and 6/6. It fails closed, but a gate that invents dead words
  # under load is a gate people learn to override. Do not remove these.
  SLUG="${TICKER##*-}"
  LIST="$OUT/clips/$SLUG.list"; : > "$LIST"
  EXPECTED=0
  IFS='|' read -ra VARR <<< "$VARIANTS"
  for V in "${VARR[@]}"; do
    for VOICE in "${VOICES[@]}"; do
      C="$OUT/clips/$SLUG-$(echo "$V" | tr -c 'a-zA-Z0-9' '_')-$VOICE"
      say -v "$VOICE" -o "$C.aiff" "$V" </dev/null 2>/dev/null || continue
      ffmpeg -nostdin -v error -y -i "$C.aiff" -ac 1 -ar 16000 -c:a pcm_s16le "$C.wav" </dev/null && rm -f "$C.aiff"
      printf "file '%s'\nfile '%s'\n" "$C.wav" "$SIL" >> "$LIST"
      EXPECTED=$((EXPECTED+1))
    done
  done
  WAV="$OUT/clips/$SLUG.wav"
  ffmpeg -nostdin -v error -y -f concat -safe 0 -i "$LIST" -c:a pcm_s16le "$WAV" </dev/null

  # Per-keyword operating point (2026-07-30): mirror this ticker's
  # confirmed_threshold from the source config into the mini config. Without
  # it the study replays every entry at the engine default and a NEAR-MISS
  # word FIXED via the dead-word-autopsy's threshold prescription would still
  # read confirmed=0 here — the gate could never see the fix land. The
  # keywords_sha256 deliberately still excludes thresholds (retuning the
  # operating point does not change what the detector must HEAR).
  CT=$(CFG="$CFG" TICKER="$TICKER" python3 - <<'PY'
import os, re
text = open(os.environ["CFG"]).read()
for b in re.findall(r"\[\[keywords\]\]\s*(.*?)(?=\n\[\[|\n\[[a-z]|\Z)", text, re.S):
    if re.search(r'market_ticker\s*=\s*"%s"' % re.escape(os.environ["TICKER"]), b):
        m = re.search(r"^confirmed_threshold\s*=\s*([0-9.]+)", b, re.M)
        if m:
            print(m.group(1))
        break
PY
)
  MINI="$OUT/cfg/$SLUG.toml"
  {
    printf '[event]\nid        = "DRYRUN-VARSTUDY-%s"\nstarts_at = "2027-01-01T00:00:00Z"\n\n' "$SLUG"
    printf '[pipeline]\nchunk_ms       = 80\nresample_to_hz = 16000\n\n'
    cat "$OUT/cfg/detector.block"
    printf '\n[[sources]]\nkind  = "replay"\npath  = "%s"\nspeed = 0.0\n\n' "$WAV"
    printf '[kalshi]\nseries = "VARSTUDY"\n\n'
    printf '[[keywords]]\nmarket_ticker    = "%s"\nphrase           = "%s"\nvariants         = [' "$TICKER" "$PHRASE"
    for i in "${!VARR[@]}"; do [[ $i -gt 0 ]] && printf ', '; printf '"%s"' "${VARR[$i]}"; done
    printf ']\nmax_price_cents  = 1\nbudget_contracts = 1\npolicy           = "confirmed_only"\n'
    [[ -n "$CT" ]] && printf 'confirmed_threshold = %s\n' "$CT"
  } > "$MINI"

  LOG="$OUT/logs/$SLUG.log"
  TIMEOUT=(); command -v timeout >/dev/null && TIMEOUT=(timeout 180)
  command -v gtimeout >/dev/null && TIMEOUT=(gtimeout 180)
  "${TIMEOUT[@]+"${TIMEOUT[@]}"}" "$BIN" replay --config "$MINI" > "$LOG" 2>&1 </dev/null
  HITS=$(grep -acE "decision intent|detection ignored" "$LOG" || true)
  VERDICT=OK; (( HITS == 0 )) && { VERDICT=FAIL; FAILS=$((FAILS+1)); }
  printf "%s\t%s\t%d/%d\t%s\n" "$TICKER" "$PHRASE" "$HITS" "$EXPECTED" "$VERDICT" | tee -a "$REPORT"
done < "$OUT/keywords.tsv"

# Readiness artifact — what preflight-event.sh's blocking gate reads. Written
# even on failure: a keyword with confirmed=0 must be VISIBLE to the gate, not
# absent from it. Lives under configs/.readiness/ (generated, git-ignored from
# governance via architecture/nodes.toml excluded_globs — never claim it).
READY_DIR="configs/.readiness"
mkdir -p "$READY_DIR"
READY="$READY_DIR/$(basename "$CFG" .toml).json"
CFG_BASE="$(basename "$CFG" .toml)" KW_SHA="$KW_SHA" ENGINE="$ENGINE" OUT="$OUT" \
READY="$READY" VOICES_CSV="$(IFS=,; echo "${VOICES[*]}")" VOICE_NOTE="$THIRD_VOICE_NOTE" \
python3 - <<'PY'
import json, os
from datetime import date

out = os.environ["OUT"]
kw = {}   # ticker -> (phrase, variants)
order = []
for line in open(f"{out}/keywords.tsv"):
    t, p, v = line.rstrip("\n").split("\t")
    kw[t] = (p, v.split("|") if v else [])
    order.append(t)
results = {}  # ticker -> (hits, expected)
for line in open(f"{out}/report.tsv"):
    t, _p, frac, _v = line.rstrip("\n").split("\t")
    hits, expected = frac.split("/")
    results[t] = (int(hits), int(expected))

artifact = {
    "config": os.environ["CFG_BASE"],
    "keywords_sha256": os.environ["KW_SHA"],
    "date": date.today().isoformat(),
    "engine": os.environ["ENGINE"] or "unknown",
    "voices": os.environ["VOICES_CSV"].split(","),
    "keywords": [
        {
            "ticker": t,
            "phrase": kw[t][0],
            "variants": kw[t][1],
            "clips": results.get(t, (0, 0))[1],
            "confirmed": results.get(t, (0, 0))[0],
        }
        for t in order
    ],
}
if os.environ.get("VOICE_NOTE"):
    artifact["voice_note"] = os.environ["VOICE_NOTE"]
path = os.environ["READY"]
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(artifact, f, indent=1)
    f.write("\n")
os.replace(tmp, path)
print(f"readiness artifact: {path}")
PY

echo "── variant-study done: $((N_KW-FAILS))/$N_KW keywords detected ≥1 clip; report: $REPORT"
(( FAILS == 0 )) || exit 1
