#!/usr/bin/env bash
# Batch bridge-admission audit (VERIFICATIONS Q-ADMIT): walk every venue in
# configs/dial-ins.toml that could carry a phone arm and list — or, with --run,
# probe — the ones with no fresh probe-observed `admission`.
#
#   bridge-probe-batch.sh                 # plan only: table of what needs probing
#   bridge-probe-batch.sh --run           # actually place the calls, spaced ≥2 min
#   bridge-probe-batch.sh --run --max 5   # cap this session's calls
#
# What it will NEVER do:
#   - edit configs/dial-ins.toml (the registry stays hand-audited; each probe
#     prints its proposed snippet — a human moves it in)
#   - probe a venue already observed operator_screened (a human answers those
#     calls; re-dialing them wastes an operator's time for a known answer)
#   - run while an armed event is live (delegated to bridge-probe.sh's guard)
#   - dial a venue with no number on file: it reads TO/DTMF from the venue's
#     env file if given, else expects --numbers <file> mapping KEY=+1...[,code]
#
# Numbers rotate per call and stay out of the repo, so this script cannot get
# them from the registry — provide them via --numbers <file>, lines like:
#     MSFT=+18774070666
#     CVNA=+18007159871,3609424#
set -euo pipefail
cd "$(dirname "$0")/.."

RUN=0 MAX=18 NUMBERS_FILE="" STALE_DAYS=30 SPACING_SECS=120
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run)     RUN=1; shift ;;
    --max)     MAX="$2"; shift 2 ;;
    --numbers) NUMBERS_FILE="$2"; shift 2 ;;
    --stale-days) STALE_DAYS="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

TODAY=$(date -u +%Y-%m-%d)

# Venues needing a probe: tier worth dialing, not operator_screened, and no
# fresh `held`. Emits "KEY<TAB>reason".
WORKLIST=$(STALE_DAYS="$STALE_DAYS" python3 - <<'PY'
import os, tomllib
from datetime import date, datetime

stale_days = int(os.environ["STALE_DAYS"])
with open("configs/dial-ins.toml", "rb") as f:
    reg = tomllib.load(f)
for key, e in sorted(reg.items()):
    if not isinstance(e, dict):
        continue
    if e.get("tier") not in ("published", "bridge_unpublished"):
        continue  # no number to dial
    admission = e.get("admission", "untested")
    if admission == "operator_screened":
        continue  # known answer; a human picks up — don't re-dial
    if admission == "held":
        try:
            age = (date.today() - datetime.strptime(e.get("admission_probed", ""), "%Y-%m-%d").date()).days
        except ValueError:
            age = 9999
        if age <= stale_days:
            continue  # fresh — nothing to do
        print(f"{key}\theld but stale ({age}d)")
    elif admission == "dropped":
        print(f"{key}\tdropped on last probe — re-test after a fix")
    else:
        print(f"{key}\tuntested")
PY
)

if [[ -z "$WORKLIST" ]]; then
  echo "nothing to probe: every dialable venue has a fresh probe-observed admission"
  exit 0
fi

echo "venues needing an admission probe:"
echo "$WORKLIST" | sed 's/^/  /'
N=$(echo "$WORKLIST" | wc -l | tr -d ' ')
echo "  ($N total)"

if [[ "$RUN" != "1" ]]; then
  echo
  echo "plan only — re-run with --run --numbers <file> to place the calls (≈\$0.02 each, spaced ${SPACING_SECS}s)"
  exit 0
fi

[[ -n "$NUMBERS_FILE" && -f "$NUMBERS_FILE" ]] || {
  echo "--run needs --numbers <file> (KEY=+1...[,code] lines; numbers rotate and are not stored in the registry)" >&2
  exit 2
}

DONE=0
while IFS=$'\t' read -r KEY REASON; do
  [[ "$DONE" -ge "$MAX" ]] && { echo "reached --max $MAX; stopping"; break; }
  LINE=$(grep -E "^${KEY}=" "$NUMBERS_FILE" | head -1 || true)
  if [[ -z "$LINE" ]]; then
    echo "── $KEY: no number in $NUMBERS_FILE — skipped ($REASON)"
    continue
  fi
  VALUE="${LINE#*=}"
  TO="${VALUE%%,*}"
  DTMF=""; [[ "$VALUE" == *,* ]] && DTMF="${VALUE#*,}"
  echo
  echo "── $KEY ($REASON): probing $TO${DTMF:+ code $DTMF}"
  ARGS=(--to "$TO" --label "$(echo "$KEY" | tr 'A-Z' 'a-z')-${TODAY}")
  [[ -n "$DTMF" ]] && ARGS+=(--dtmf "$DTMF")
  if ! scripts/bridge-probe.sh "${ARGS[@]}"; then
    echo "── $KEY: probe FAILED — fix before continuing the batch" >&2
    exit 1
  fi
  DONE=$((DONE + 1))
  # Space calls out: back-to-back dials from one number look like abuse to
  # carriers and conferencing providers.
  [[ "$DONE" -lt "$MAX" ]] && sleep "$SPACING_SECS"
done <<< "$WORKLIST"

echo
echo "$DONE probe(s) placed. Review each transcript above, then move the printed"
echo "snippets into configs/dial-ins.toml and pin excerpts in VERIFICATIONS.md Q-ADMIT."
