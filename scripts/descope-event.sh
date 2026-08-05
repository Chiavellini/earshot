#!/usr/bin/env bash
# Descope an event ON THE RECORD.
#   descope-event.sh <EVENT_TICKER> "<reason>" [--fallbacks "<tried1>; <tried2>"]
#
# 2026-08-04: MCD was deliberately not armed and the only trace was a commit
# message — missed-arm-check could not tell a decision from a miss. From now
# on a slate event ends the day in exactly ONE of three buckets:
#   COVERED  (state/<E>*/report.json)  |  DESCOPED (this record)  |  MISSED.
# There is no fourth state. A descope with no record is a miss.
#
# Writes state/<EVENT_TICKER>/DESCOPED.json and PAGES — a descope is a
# decision someone should see tonight, not discover tomorrow.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

EV="${1:?usage: descope-event.sh <EVENT_TICKER> \"<reason>\" [--fallbacks \"...\"]}"
REASON="${2:?a descope without a reason is a miss with paperwork — give the reason}"
FALLBACKS=""
[[ "${3:-}" == "--fallbacks" ]] && FALLBACKS="${4:-}"

DIR="state/${EV}"
mkdir -p "$DIR"
OUT="$DIR/DESCOPED.json"

EV="$EV" REASON="$REASON" FALLBACKS="$FALLBACKS" python3 - > "$OUT.tmp" <<'PY'
import json, os, socket
from datetime import datetime, timezone
print(json.dumps({
    "event_ticker": os.environ["EV"],
    "reason": os.environ["REASON"],
    "fallbacks_exhausted": [s.strip() for s in os.environ.get("FALLBACKS", "").split(";") if s.strip()],
    "decided_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "decided_by": os.environ.get("USER", "unknown"),
    "host": socket.gethostname().split(".")[0],
}, indent=1))
PY
mv "$OUT.tmp" "$OUT"
echo "DESCOPED: $EV — $REASON"
echo "record: $OUT"

scripts/journal.sh --event "$EV" --actor descope-event.sh --action descope --outcome info \
  --detail "$REASON" --log "$OUT" || true
scripts/notify.sh "DESCOPED $EV" "$REASON${FALLBACKS:+ — fallbacks tried: $FALLBACKS} (record: $OUT)" --quiet || true
