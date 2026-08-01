#!/usr/bin/env bash
# dead-word-autopsy.sh [--study-dir DIR] [--out FILE]
#
# Turn "0 detections" into a DIAGNOSIS, for every keyword the recall gate killed.
#
# Background: scripts/variant-study.sh found 12 of 52 market words scoring ZERO
# on clean studio TTS (docs/BAKEOFF.md §RECALL GATE, 2026-07-29). Three
# hypotheses were already eliminated there — it is not the harness, not
# multi-word phrases, not rare-brand-only. What remained un-testable was the
# scorer itself, because `ctc_scorer` computed each entry's confidence and then
# dropped it with a bare `continue`: no log, no counter, nothing to read.
#
# `earshot detect-explain` now keeps that number. This script runs it over every
# keyword in a variant-study output directory and sorts the results into the two
# buckets that need OPPOSITE fixes:
#
#   NEAR-MISS    peak confidence just under threshold. The model heard the word;
#                the operating point rejected it. A threshold/scoring question.
#   NO-EVIDENCE  some BPE piece the model never emits. Because confidence is
#                exp(score/k) — a geometric mean over the k pieces — one absent
#                piece drags the entry to ~0 however strongly the others peak.
#                UNREACHABLE BY ANY THRESHOLD. A spelling/variant question.
#
# ⚠ CONTROLS ARE MANDATORY, not decoration. A diagnostic that reports every word
# as broken has proven nothing. This script always runs the known-GOOD words too
# (orion, hyperion, inflation, advertising, loyalty …) and asserts they come back
# FIRED. If a control does not fire, the instrument is wrong and every verdict
# below it is void — the script says so and exits nonzero.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO="$PWD"
BIN="${EARSHOT_BIN:-target/release/earshot}"
STUDY="${STUDY_DIR:-/Volumes/Vault/earshot-archive/variant-study-2026-07-29}"
OUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --study-dir) STUDY="$2"; shift 2 ;;
    --out)       OUT="$2"; shift 2 ;;
    --auto)      AUTO=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -x "$BIN" ]] || { echo "REFUSING: no binary at $BIN (cargo build --release)" >&2; exit 2; }
[[ -d "$STUDY" ]] || { echo "REFUSING: no study dir at $STUDY" >&2; exit 2; }

# streaming_ctc dlopens onnxruntime; unset, the process futex-hangs silently in
# dlopen rather than failing (deploy-box.sh incident class, variant-study.sh:26).
if [[ -z "${ORT_DYLIB_PATH:-}" ]]; then
  ORT_DYLIB_PATH="$(find "$REPO/vendor/onnxruntime" -name 'libonnxruntime.*.dylib' -not -path '*dSYM*' 2>/dev/null | head -1)"
  [[ -n "$ORT_DYLIB_PATH" ]] || { echo "REFUSING: ORT_DYLIB_PATH unset and no dylib under vendor/" >&2; exit 2; }
  export ORT_DYLIB_PATH
fi

# The 12 dead words from the 2026-07-29 study, and the controls that must fire.
# Keyed slug:SUFFIX, matching that run's per-slug layout ($STUDY/vs-<slug>/...).
DEAD=(
  "sbux:MATC" "sbux:PROT" "sbux:FRAP" "sbux:HORC"
  "cmg:TARI" "cmg:HEAD" "cmg:TAIL" "cmg:AUTO" "cmg:CHIP" "cmg:CHIPL"
  "meta:LLAM" "meta:RAY"
)
CONTROL=( "meta:ORIO" "meta:HYPE" "meta:ADVE" "cmg:INFL" "sbux:LOYA" )

# --auto (2026-07-30): derive both lists from the study dir's OWN report.tsv
# instead of the hardcoded 07-29 sets, so the autopsy works on any later study
# (e.g. the daily WNT gate). Controls become the words that DID fire in this
# same run, ranked by hit count — a stronger control than a remembered list,
# because it is measured on the same clips by the same binary.
# A single-config study (scripts/variant-study.sh <cfg> <out>) writes a FLAT
# layout, so slug is empty and run_one falls back to $STUDY/cfg|clips.
if [[ "${AUTO:-0}" == 1 ]]; then
  RT="$STUDY/report.tsv"
  [[ -f "$RT" ]] || { echo "REFUSING: --auto needs $RT" >&2; exit 2; }
  DEAD=(); CONTROL=()
  while IFS=$'\t' read -r ticker phrase hits verdict; do
    [[ -n "${ticker:-}" ]] || continue
    suf="${ticker##*-}"
    [[ "$verdict" == FAIL ]] && DEAD+=(":$suf")
  done < "$RT"
  # top-5 firing words by hit count as controls
  while read -r suf; do
    [[ -n "$suf" ]] && CONTROL+=(":$suf")
  done < <(awk -F'\t' '$4=="OK"{split($3,h,"/"); n=$1; sub(/.*-/,"",n); print h[1]"\t"n}' "$RT" \
             | sort -rn | head -5 | cut -f2)
  echo "# --auto: ${#DEAD[@]} dead, ${#CONTROL[@]} controls from $RT" >&2
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
run_one() {  # run_one <slug> <SUF> -> prints "verdict<TAB>detail"
  local slug="$1" suf="$2"
  local cfg="$STUDY/vs-$slug/cfg/$suf.toml" wav="$STUDY/vs-$slug/clips/$suf.wav"
  # Flat layout: a single-config study (variant-study.sh <cfg> <out>) writes
  # $STUDY/cfg/<SUF>.toml directly, with no per-slug subdir.
  if [[ ! -f "$cfg" || ! -f "$wav" ]]; then
    cfg="$STUDY/cfg/$suf.toml"; wav="$STUDY/clips/$suf.wav"
  fi
  [[ -f "$cfg" && -f "$wav" ]] || { printf 'MISSING\tno cfg/clip for %s:%s\n' "${slug:-flat}" "$suf"; return; }
  # The mini-config's [[sources]] path points at the original /tmp location and
  # is irrelevant here — detect-explain takes the wav explicitly.
  local log="$TMP/$slug-$suf.txt"
  if ! "$BIN" detect-explain --config "$cfg" --wav "$wav" > "$log" 2>&1; then
    printf 'ERROR\t%s\n' "$(tail -2 "$log" | tr '\n' ' ')"; return
  fi
  cp "$log" "$TMP/keep-$slug-$suf.txt"
  local line; line="$(grep -m1 -E '^\S.*(FIRED|NO-EVIDENCE|NEAR-MISS|WEAK|NO-TARGET)' "$log")"
  [[ -n "$line" ]] || { printf 'NO-OUTPUT\t%s\n' "$(tail -2 "$log" | tr '\n' ' ')"; return; }
  local v="NO-TARGET"
  for cand in FIRED NO-EVIDENCE NEAR-MISS WEAK; do
    grep -q "$cand" <<<"$line" && { v="$cand"; break; }
  done
  printf '%s\t%s\n' "$v" "$line"
}

emit() { [[ -n "$OUT" ]] && printf '%s\n' "$*" >> "$OUT"; printf '%s\n' "$*"; }
[[ -n "$OUT" ]] && : > "$OUT"

emit "# dead-word autopsy — $(date -u +%FT%TZ)"
emit "# binary: $BIN   study: $STUDY"
emit ""
emit "## CONTROLS (must all be FIRED — otherwise every verdict below is void)"
CTRL_BAD=0
for pair in "${CONTROL[@]}"; do
  slug="${pair%%:*}"; suf="${pair##*:}"
  IFS=$'\t' read -r v line <<<"$(run_one "$slug" "$suf")"
  emit "$(printf '  %-6s %-6s %-12s %s' "$slug" "$suf" "$v" "${line:0:96}")"
  [[ "$v" == FIRED ]] || CTRL_BAD=$((CTRL_BAD+1))
done
emit ""
if (( CTRL_BAD )); then
  emit "⛔ $CTRL_BAD/${#CONTROL[@]} CONTROLS DID NOT FIRE — the instrument is not trustworthy."
  emit "   Do NOT read the dead-word verdicts below as evidence about the words."
fi

emit "## DEAD WORDS"
declare -a NEARMISS=() NOEV=() OTHER=()
for pair in "${DEAD[@]}"; do
  slug="${pair%%:*}"; suf="${pair##*:}"
  IFS=$'\t' read -r v line <<<"$(run_one "$slug" "$suf")"
  emit "$(printf '  %-6s %-6s %-12s %s' "$slug" "$suf" "$v" "${line:0:96}")"
  case "$v" in
    NEAR-MISS)   NEARMISS+=("$slug:$suf") ;;
    NO-EVIDENCE) NOEV+=("$slug:$suf") ;;
    *)           OTHER+=("$slug:$suf ($v)") ;;
  esac
done

emit ""
emit "## BUCKETS — the fix differs per bucket"
emit "  NEAR-MISS   (${#NEARMISS[@]}): ${NEARMISS[*]:-none}"
emit "      -> operating point. Per-keyword threshold from the MEASURED margin."
emit "  NO-EVIDENCE (${#NOEV[@]}): ${NOEV[*]:-none}"
emit "      -> spelling. Add ONLY variants whose pieces the model demonstrably"
emit "         emits, then re-gate through variant-study.sh. No threshold helps."
emit "  OTHER       (${#OTHER[@]}): ${OTHER[*]:-none}"
emit ""
emit "## PER-PIECE DETAIL"
for f in "$TMP"/keep-*.txt; do
  [[ -f "$f" ]] || continue
  emit ""
  emit "### $(basename "$f" .txt | sed 's/^keep-//')"
  while IFS= read -r l; do emit "$l"; done < <(grep -E '^\s+piece |FIRED|NO-EVIDENCE|NEAR-MISS|WEAK' "$f")
done

(( CTRL_BAD )) && exit 1
exit 0
