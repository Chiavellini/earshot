#!/usr/bin/env bash
# variant-study.sh <config.toml> [outdir]
#
# Keyword-readiness helper (born 2026-07-29 after WING lost CITR to a Spanish
# pronunciation and AI to a dropped short form — both flagged in the config,
# neither enforced). For every [[keywords]] block in the config:
#   1. synthesize each variant with two macOS voices (say: Samantha + Daniel),
#   2. splice into one WAV per keyword (2.6 s gaps > refractory_ms),
#   3. replay through the REAL detector (config's own [detector] block,
#      confirmed_only policy) via `earshot replay`,
#   4. report detections vs expected clips per keyword.
# Exit 1 if any keyword produced ZERO Confirmed detections.
#
# TTS proves the path is WIRED, not that live recall is 100% (LATENCY-PROOF
# D1g caveat). A keyword failing on clean studio TTS will not fire live.
# macOS only (uses `say`). Run from repo root.
set -uo pipefail
CFG="${1:?usage: variant-study.sh <config.toml> [outdir]}"
OUT="${2:-/tmp/variant-study-$(basename "$CFG" .toml)}"
BIN="${EARSHOT_BIN:-target/release/earshot}"
VOICES=(Samantha Daniel)
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

# 1) keyword table: TICKER<TAB>PHRASE<TAB>V1|V2|...
python3 - "$CFG" > "$OUT/keywords.tsv" <<'PY'
import re, sys
t = open(sys.argv[1]).read()
blocks = re.findall(
    r'\[\[keywords\]\]\s*(.*?)(?=\n\[\[|\n\[[a-z]|\Z)', t, re.S)
for b in blocks:
    tk = re.search(r'market_ticker\s*=\s*"([^"]+)"', b)
    ph = re.search(r'phrase\s*=\s*"([^"]+)"', b)
    vs = re.search(r'variants\s*=\s*\[(.*?)\]', b, re.S)
    if not (tk and ph):
        continue
    variants = re.findall(r'"([^"]+)"', vs.group(1)) if vs else [ph.group(1)]
    print(f"{tk.group(1)}\t{ph.group(1)}\t" + "|".join(variants))
PY
N_KW=$(wc -l < "$OUT/keywords.tsv" | tr -d ' ')
echo "variant-study: $N_KW keywords from $CFG → $OUT"

# detector block mirrored verbatim from the source config
sed -n '/^\[detector\]/,/^\[arbiter\]/p' "$CFG" | sed '$d' > "$OUT/cfg/detector.block"

SIL="$OUT/clips/_sil.wav"
ffmpeg -v error -y -f lavfi -i anullsrc=r=16000:cl=mono -t 2.6 -c:a pcm_s16le "$SIL"

FAILS=0
REPORT="$OUT/report.tsv"
: > "$REPORT"
while IFS=$'\t' read -r TICKER PHRASE VARIANTS; do
  SLUG="${TICKER##*-}"
  LIST="$OUT/clips/$SLUG.list"; : > "$LIST"
  EXPECTED=0
  IFS='|' read -ra VARR <<< "$VARIANTS"
  for V in "${VARR[@]}"; do
    for VOICE in "${VOICES[@]}"; do
      C="$OUT/clips/$SLUG-$(echo "$V" | tr -c 'a-zA-Z0-9' '_')-$VOICE"
      say -v "$VOICE" -o "$C.aiff" "$V" 2>/dev/null || continue
      ffmpeg -v error -y -i "$C.aiff" -ac 1 -ar 16000 -c:a pcm_s16le "$C.wav" && rm -f "$C.aiff"
      printf "file '%s'\nfile '%s'\n" "$C.wav" "$SIL" >> "$LIST"
      EXPECTED=$((EXPECTED+1))
    done
  done
  WAV="$OUT/clips/$SLUG.wav"
  ffmpeg -v error -y -f concat -safe 0 -i "$LIST" -c:a pcm_s16le "$WAV"

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
  } > "$MINI"

  LOG="$OUT/logs/$SLUG.log"
  TIMEOUT=(); command -v timeout >/dev/null && TIMEOUT=(timeout 180)
  command -v gtimeout >/dev/null && TIMEOUT=(gtimeout 180)
  "${TIMEOUT[@]+"${TIMEOUT[@]}"}" "$BIN" replay --config "$MINI" > "$LOG" 2>&1
  HITS=$(grep -acE "decision intent|detection ignored" "$LOG" || true)
  VERDICT=OK; (( HITS == 0 )) && { VERDICT=FAIL; FAILS=$((FAILS+1)); }
  printf "%s\t%s\t%d/%d\t%s\n" "$TICKER" "$PHRASE" "$HITS" "$EXPECTED" "$VERDICT" | tee -a "$REPORT"
done < "$OUT/keywords.tsv"

echo "── variant-study done: $((N_KW-FAILS))/$N_KW keywords detected ≥1 clip; report: $REPORT"
(( FAILS == 0 )) || exit 1
