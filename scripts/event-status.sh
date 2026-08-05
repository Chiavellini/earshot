#!/usr/bin/env bash
# One command that answers "why did event X fail" — or "where does the day
# stand" — from state/<EVENT_ID>/ alone.
#
#   scripts/event-status.sh KXEARNINGSMENTIONDASH-26AUG05[-WEBCAST]
#   scripts/event-status.sh 2026-08-05
#   scripts/event-status.sh today
#
# Event mode: merge-sorts state/<E>/journal.jsonl with state/<E>/box/journal.jsonl
# (box copies rsync down post-event), prints the lifecycle, a "stopped at" line
# (last non-info entry), and the artifact checklist. Date mode: resolves the
# day's slate and renders one block per event. Read-only, exit 0.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

ARG="${1:?usage: event-status.sh <EVENT_ID | YYYY-MM-DD | today>}"

render_event() { # <EVENT_ID or bare EVENT_TICKER>
  local ev="$1"
  # accept a bare ticker: pick every state dir that starts with it
  local dirs=()
  for d in state/"$ev" state/"$ev"-*; do [[ -d "$d" ]] && dirs+=("$d"); done
  if [[ ${#dirs[@]} -eq 0 ]]; then
    echo "── $ev: NO state dir — nothing ever touched this event (not staged? not preflighted? journal would exist otherwise)"
    return
  fi
  for d in "${dirs[@]}"; do
    echo "── ${d#state/}"
    D="$d" python3 - <<'PY'
import json, os
from pathlib import Path
d = Path(os.environ["D"])
lines = []
for j in [d / "journal.jsonl", d / "box" / "journal.jsonl"]:
    if j.exists():
        for ln in j.read_text().splitlines():
            try:
                lines.append(json.loads(ln))
            except Exception:
                pass
lines.sort(key=lambda x: x.get("ts", ""))
if not lines:
    print("   (no journal lines)")
for l in lines:
    mark = {"ok": "✓", "fail": "✗", "refused": "⛔", "warn": "⚠", "skipped": "~", "info": "·"}.get(l.get("outcome"), "?")
    host = f" [{l['host']}]" if l.get("host") else ""
    print(f"   {l.get('ts','?')} {mark} {l.get('actor','?'):22} {l.get('action','?'):15} {l.get('detail','')[:90]}{host}")
stop = next((l for l in reversed(lines) if l.get("outcome") not in ("info", "ok")), None)
if stop:
    print(f"   STOPPED AT → {stop.get('actor')}/{stop.get('action')}: {stop.get('detail','')[:120]}")
    if stop.get("log"):
        print(f"   full context → {stop['log']}")
elif lines:
    print("   no non-ok stop recorded")
arts = ["preflight-result.json", "listener.ready", "stream.connected", "call.sid",
        "report.json", "DESCOPED.json", "STOP", "launch.log", "arm.log", "redial.log"]
have = [a for a in arts if (d / a).exists()]
miss = [a for a in arts if not (d / a).exists()]
print(f"   artifacts: {' '.join(have) or '(none)'}")
print(f"   absent   : {' '.join(miss)}")
PY
  done
}

case "$ARG" in
  today) DATE=$(date -u +%F); MODE=date ;;
  [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) DATE="$ARG"; MODE=date ;;
  *) MODE=event ;;
esac

if [[ "$MODE" == event ]]; then
  render_event "$ARG"
else
  echo "═══ event day $DATE ═══"
  TICKERS=$(python3 -c "
import json
try:
    d = json.load(open('slate/latest.json'))
    print(' '.join(e['event_ticker'] for e in d.get('events', []) if e.get('call_date') == '$DATE'))
except Exception as e:
    print('')")
  if [[ -z "$TICKERS" ]]; then
    echo "(no slate events dated $DATE in slate/latest.json)"
  fi
  for t in $TICKERS; do
    render_event "$t"
    echo
  done
fi
