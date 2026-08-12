#!/usr/bin/env bash
# One command that answers "why did event X fail" — or "where does the day
# stand" — from state/<EVENT_ID>/ alone.
#
#   scripts/event-status.sh KXEARNINGSMENTIONDASH-26AUG05[-WEBCAST]
#   scripts/event-status.sh 2026-08-05
#   scripts/event-status.sh today
#
# Event mode: verifies and renders every exact typed run first, then may
# merge-sort historical state journals as a compatibility view.
# (box copies rsync down post-event), prints the lifecycle, a "stopped at" line
# (last non-info entry), and the artifact checklist. Date mode: resolves the
# day's union of RunPlans and slate entries and renders one block per event.
# Read-only. Exit 1 means at least one typed run is corrupt or its remote-owned
# tail may be stale; the printed local prefix remains useful but not authoritative.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

ARG="${1:?usage: event-status.sh <EVENT_ID | YYYY-MM-DD | today>}"
PROBLEMS=0

render_event() { # <EVENT_ID or bare EVENT_TICKER>
  local ev="$1"
  # accept a bare ticker: pick every state dir AND typed-run dir that starts
  # with it (a run plan can exist before anything ever journals into state/).
  local ids=() d id x seen
  for d in state/"$ev" state/"$ev"-* var/runs/"$ev" var/runs/"$ev"-*; do
    [[ -d "$d" ]] || continue
    id="${d#state/}"; id="${id#var/runs/}"
    seen=0
    if [[ ${#ids[@]} -gt 0 ]]; then
      for x in "${ids[@]}"; do [[ "$x" == "$id" ]] && seen=1; done
    fi
    [[ $seen -eq 0 ]] && ids+=("$id")
  done
  if [[ ${#ids[@]} -eq 0 ]]; then
    echo "── $ev: NO state dir — nothing ever touched this event (not staged? not preflighted? journal would exist otherwise)"
    return
  fi
  for id in "${ids[@]}"; do
    echo "── $id"
    # Typed ledger first (control.run.receipts). Corruption is never hidden by
    # the legacy projection. A scheduled active run's laptop ledger may be only
    # a dispatch prefix, so status names that uncertainty and returns nonzero.
    local typed="" typed_json="" authority=""
    if [[ -d "var/runs/$id" ]]; then
      if ! typed_json=$(python3 scripts/eventctl.py status --event "$id" --json 2>&1); then
        echo "   TYPED STATUS INVALID → $typed_json"
        PROBLEMS=$((PROBLEMS + 1))
        continue
      fi
      typed=$(python3 scripts/eventctl.py status --event "$id" 2>&1) || {
        echo "   TYPED STATUS INVALID → $typed"
        PROBLEMS=$((PROBLEMS + 1))
        continue
      }
      authority=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["authority"]["remote_status"])' <<< "$typed_json") || {
        echo "   TYPED STATUS INVALID → missing authority metadata"
        PROBLEMS=$((PROBLEMS + 1))
        continue
      }
      if [[ "$authority" == stale_prefix_possible ]]; then
        PROBLEMS=$((PROBLEMS + 1))
      fi
    fi
    if [[ -n "$typed" && "$typed" == *"active: yes"* ]]; then
      printf '%s\n' "$typed"
      continue
    fi
    if [[ ! -d "state/$id" ]]; then
      echo "   (typed run present but not active; no legacy state dir)"
      [[ -n "$typed" ]] && printf '%s\n' "$typed"
      continue
    fi
    d="state/$id"
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
# A failure that was later recovered from is history, not the current state —
# reporting it as "STOPPED AT" sent a 06:00 operator chasing a preflight that
# had already been fixed (2026-08-05). Only the TAIL of the timeline describes
# where the event actually stands.
bad = [l for l in lines if l.get("outcome") not in ("info", "ok")]
last_bad = bad[-1] if bad else None
tail_is_bad = bool(lines) and lines[-1].get("outcome") not in ("info", "ok")
if tail_is_bad and last_bad:
    print(f"   STOPPED AT → {last_bad.get('actor')}/{last_bad.get('action')}: {last_bad.get('detail','')[:120]}")
    if last_bad.get("log"):
        print(f"   full context → {last_bad['log']}")
elif lines:
    cur = lines[-1]
    print(f"   CURRENT → {cur.get('actor')}/{cur.get('action')} ({cur.get('outcome')}): {cur.get('detail','')[:100]}")
    if last_bad:
        print(f"   earlier failure (RECOVERED) → {last_bad.get('actor')}/{last_bad.get('action')}: {last_bad.get('detail','')[:90]}")
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
  TICKERS=$(python3 - "$DATE" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

wanted = dt.date.fromisoformat(sys.argv[1])
typed_ids = set()
for path in sorted(Path("var/runs").glob("*/*/control/run-plan.json")):
    try:
        plan = json.loads(path.read_text())
        starts = dt.datetime.fromisoformat(plan["starts_at_utc"].replace("Z", "+00:00"))
    except Exception as exc:
        raise SystemExit(f"invalid RunPlan candidate {path}: {exc}")
    if starts.date() == wanted:
        typed_ids.add(plan["event_id"])
try:
    slate = json.load(open("slate/latest.json"))
except FileNotFoundError:
    slate = {"events": []}
except Exception as exc:
    raise SystemExit(f"invalid slate/latest.json: {exc}")
slate_ids = set()
for event in slate.get("events", []):
    if event.get("call_date") == str(wanted) and event.get("event_ticker"):
        slate_ids.add(event["event_ticker"])
# A slate market-event ticker deliberately expands to all of its lane IDs in
# render_event(). Do not also render the exact typed child: that duplicates the
# same run block whenever both authorities know the event.
for base in slate_ids:
    typed_ids = {
        event_id for event_id in typed_ids
        if event_id != base and not event_id.startswith(base + "-")
    }
tickers = typed_ids | slate_ids
print("\n".join(sorted(tickers)))
PY
  ) || { echo "REFUSED: could not enumerate date authority" >&2; exit 1; }
  if [[ -z "$TICKERS" ]]; then
    echo "(no slate events dated $DATE in slate/latest.json)"
  fi
  while IFS= read -r t; do
    [[ -n "$t" ]] || continue
    render_event "$t"
    echo
  done <<< "$TICKERS"
fi
exit "$([[ "$PROBLEMS" -eq 0 ]] && echo 0 || echo 1)"
