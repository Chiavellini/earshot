#!/usr/bin/env bash
# Nightly keyword-readiness studies for a slate date — the scheduled
# generalization of the ad-hoc study-afternoon-26aug04.sh (which was re-written
# by hand per event day; this one takes the date).
#
#   scripts/study-nightly.sh --date 2026-08-05
#
# For every staged lane config of that date (webcast-<lc>-<dp>-arm-wc.toml AND
# twilio-<lc>-<dp>-arm.toml — readiness artifacts are keyed to the config
# BASENAME, so each lane needs its own), in CALL ORDER (earliest [event]
# .starts_at first, so a slow study can never leave the nearest event ungated):
#   * if the twilio twin's keywords_sha256 equals a fresh sibling artifact,
#     CLONE the artifact to this basename (recorded via "cloned_from" — honest:
#     the gate re-hashes the config itself);
#   * else run scripts/variant-study.sh (Darwin `say` — laptop only).
# Pages on: any study failure, any confirmed==0 keyword, missing binary/ORT.
# Journals per event. Exit 1 if anything needs attention.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

DATE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --date) DATE="${2:?}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$DATE" ]] || { echo "usage: study-nightly.sh --date YYYY-MM-DD" >&2; exit 2; }
DP=$(python3 -c "
from datetime import date
d = date.fromisoformat('$DATE')
print(f\"{d.strftime('%y')}{d.strftime('%b').lower()}{d.strftime('%d')}\")")
page() { scripts/notify.sh "$1" "$2" --quiet || true; }

[[ "$(uname -s)" == Darwin ]] || { echo "variant-study needs macOS 'say' — laptop only" >&2; exit 2; }
[[ -x target/release/earshot ]] || { page "study-nightly BLOCKED" "no target/release/earshot — build first"; exit 1; }
export EARSHOT_BIN=target/release/earshot

CFGS=$(ls configs/webcast-*-"$DP"-arm-wc.toml configs/twilio-*-"$DP"-arm.toml 2>/dev/null || true)
[[ -n "$CFGS" ]] || { page "study-nightly: NOTHING STAGED for $DATE" "no lane configs matching $DP — did slate-stage run?"; exit 1; }

# Call order: sort by [event].starts_at.
ORDERED=$(for c in $CFGS; do
  st=$(sed -n '/^\[event\]/,/^\[/s/^[[:space:]]*starts_at[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$c" | head -1)
  echo "${st:-9999}|$c"
done | sort | cut -d'|' -f2)

PROBLEMS=0
for cfg in $ORDERED; do
  base=$(basename "$cfg" .toml)
  ready="configs/.readiness/${base}.json"
  ev=$(sed -n '/^\[event\]/,/^\[/s/^[[:space:]]*id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$cfg" | head -1)
  kw_sha=$(python3 scripts/keywords-extract.py "$cfg" --sha256 2>/dev/null || true)
  n_kw=$(python3 scripts/keywords-extract.py "$cfg" 2>/dev/null | grep -c . || true)
  if [[ "${n_kw:-0}" -eq 0 ]]; then
    echo "── $base: no keywords (capture-only) — skipping"
    continue
  fi
  # Fresh + matching already? Don't re-burn 10-30 min of `say`.
  if [[ -f "$ready" ]] && python3 - "$ready" "$kw_sha" <<'PY'
import json, sys
from datetime import date, datetime
a = json.load(open(sys.argv[1]))
ok = (a.get("keywords_sha256") == sys.argv[2]
      and (date.today() - datetime.strptime(a.get("date",""), "%Y-%m-%d").date()).days <= 1)
sys.exit(0 if ok else 1)
PY
  then
    echo "── $base: readiness artifact already fresh + hash-matched — keeping"
  else
    # Clone from a same-event sibling with identical keyword extraction?
    CLONED=0
    for sib in configs/.readiness/*-"$DP"-*.json; do
      [[ -f "$sib" && "$sib" != "$ready" ]] || continue
      if python3 - "$sib" "$kw_sha" "$base" "$ready" <<'PY'
import json, sys
from datetime import date, datetime
a = json.load(open(sys.argv[1]))
if a.get("keywords_sha256") != sys.argv[2]:
    sys.exit(1)
if (date.today() - datetime.strptime(a.get("date",""), "%Y-%m-%d").date()).days > 1:
    sys.exit(1)
a["config"] = sys.argv[3]
a["cloned_from"] = sys.argv[1]
json.dump(a, open(sys.argv[4] + ".tmp", "w"), indent=1)
import os
os.replace(sys.argv[4] + ".tmp", sys.argv[4])
sys.exit(0)
PY
      then
        echo "── $base: CLONED readiness from $(basename "$sib") (identical keyword set)"
        CLONED=1
        break
      fi
    done
    if [[ "$CLONED" -eq 0 ]]; then
      echo "════ $(date -u '+%H:%M:%SZ') variant-study $base ════"
      if ! scripts/variant-study.sh "$cfg" "/tmp/variant-study-$base" 2>&1 | tail -25; then
        page "study FAILED: $base" "variant-study.sh exited nonzero for $cfg — the readiness gate will refuse this event. Diagnose tonight."
        PROBLEMS=$((PROBLEMS+1))
      fi
    fi
  fi
  # Read the result — an unread study is how ZETA armed with confirmed:0.
  if [[ -f "$ready" ]]; then
    DEAD=$(python3 -c "
import json
a = json.load(open('$ready'))
print(' '.join(k.get('ticker','?').split('-')[-1] for k in a.get('keywords',[]) if not k.get('confirmed')))" 2>/dev/null || true)
    if [[ -n "$DEAD" ]]; then
      page "DEAD WORDS: $base" "confirmed:0 in tonight's study: $DEAD — fix variants + restudy, or ack deliberately (configs/.acks/${base}-dead-word.ack). The green gate WILL refuse otherwise."
      PROBLEMS=$((PROBLEMS+1))
    fi
  else
    page "NO READINESS ARTIFACT: $base" "study did not produce $ready — event will be refused by the gate"
    PROBLEMS=$((PROBLEMS+1))
  fi
  [[ -n "$ev" ]] && scripts/journal.sh --event "$ev" --actor study-nightly.sh --action study \
    --outcome "$([[ -z "${DEAD:-}" && -f "$ready" ]] && echo ok || echo fail)" \
    --detail "artifact=$ready${DEAD:+ dead:$DEAD}" --log "$ready" || true
done

echo "ALL STUDIES DONE $(date -u '+%H:%M:%SZ') — problems: $PROBLEMS"
[[ "$PROBLEMS" -eq 0 ]] || exit 1
