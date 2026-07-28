#!/usr/bin/env bash
# Daily slate sweep — the scheduled half of discover-events.py.
#
# The 07-25 sweep that produced the "only F and PYPL" Tuesday slate was a
# manual invocation of a blind sweep; Boeing was added by hand two days later
# only because a human went looking. This job makes the sweep a schedule, its
# output a committed artifact, and its silence impossible: every failure path
# alerts (a silent dead cron is the failure mode being replaced).
#
# Fired by launchd (configs/launchd/com.earshot.slate-daily.plist, 20:00
# local; missed-while-asleep firings run on wake). Manual: --dry-run prints
# every decision, commits nothing, alerts nothing.
#
# Steps:
#   1. discover-events.py --self-test (offline frozen BA fixture — the durable
#      form of --assert-ba, which died when BA's event closed post-call), then
#      the live sweep → slate/latest.json.
#   2. Diff vs the last committed slate BY CONTENT (events/dates/sweep-delta —
#      generated_utc alone never counts as a change).
#   3. recon-worklist.py → slate/worklist.md (+ ESCALATE lines).
#   4. Alert (scripts/notify.sh) on: events added/removed in horizon,
#      call_date changes, Boeing-class sweep delta, events ≤7d with no staged
#      webcast config, NO-ENTRY recon ≤4d, or ANY step failing.
#   5. Commit slate/ (scoped add, "slate:" prefix) when content changed.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
TODAY="$(date +%F)"

alert() {
  if [ "$DRY" = 1 ]; then echo "[dry-run] ALERT: $1"; echo "$2" | sed 's/^/[dry-run]   /'
  else scripts/notify.sh "$1" "$2"; fi
}
fail() { alert "slate-daily FAILED" "$1"; echo "FAIL: $1" >&2; exit 1; }

mkdir -p slate/changes
PREV="$(mktemp)"; trap 'rm -f "$PREV"' EXIT
git show HEAD:slate/latest.json > "$PREV" 2>/dev/null || : > "$PREV"

python3 scripts/discover-events.py --self-test \
  || fail "discover-events.py --self-test FAILED (BA fixture regression)"
python3 scripts/discover-events.py --json slate/latest.json --days 21 \
  || fail "discover-events.py sweep exited $? — check the launchd log"

REPORT="$(SLATE_PREV_FILE="$PREV" python3 - <<'PY'
import datetime, json, os, sys
from pathlib import Path

new = json.load(open("slate/latest.json"))
prev_raw = Path(os.environ["SLATE_PREV_FILE"]).read_text().strip()
prev = json.loads(prev_raw) if prev_raw else None
today = datetime.date.today()
week = today + datetime.timedelta(days=7)

def evmap(s):
    return {e["event_ticker"]: e for e in s.get("events", [])}

nm = evmap(new)
pm = evmap(prev) if prev else {}
if prev is None:
    print(f"INITIAL: first committed slate — {len(nm)} events")
else:
    for tk in sorted(set(nm) - set(pm)):
        e = nm[tk]
        print(f"CHANGE: ADDED {tk} ({e.get('issuer')}) call {e.get('call_date')}")
    for tk in sorted(set(pm) - set(nm)):
        print(f"CHANGE: REMOVED {tk}")
    for tk in sorted(set(nm) & set(pm)):
        a, b = pm[tk].get("call_date"), nm[tk].get("call_date")
        if a != b:
            print(f"CHANGE: DATE {tk} {a} -> {b}")

for tk in new.get("sweep_delta", {}).get("per_series_only", []):
    print(f"BOEING-CLASS: {tk} found by per-series sweep ONLY")

for e in new.get("events", []):
    cd = e.get("call_date")
    tk = e["event_ticker"]
    if not cd:
        if tk not in pm:  # alert once, when it first appears — not every day
            print(f"UNRESOLVED-DATE: {tk} — sub_title did not parse; pin the date by hand")
        continue
    d = datetime.date.fromisoformat(cd)
    if today <= d <= week:
        lc = (e.get("issuer") or "").lower()
        if lc and not list(Path("configs").glob(f"webcast-{lc}-*-arm.toml")):
            print(f"UNSTAGED: {e['issuer']} calls {cd} — no configs/webcast-{lc}-*-arm.toml")
PY
)" || fail "slate diff analysis crashed"

WORKLIST_OUT="$(python3 scripts/recon-worklist.py \
  --slate slate/latest.json --registry configs/dial-ins.toml \
  --days 10 --stale-days 45 --out slate/worklist.md)" \
  || fail "recon-worklist.py crashed"

CHANGES="$(grep -E '^(CHANGE|INITIAL|BOEING-CLASS|UNRESOLVED-DATE):' <<<"$REPORT" || true)"
UNSTAGED="$(grep '^UNSTAGED:' <<<"$REPORT" || true)"
ESCALATE="$(grep '^ESCALATE:' <<<"$WORKLIST_OUT" || true)"

if [ -n "$CHANGES" ]; then
  echo "$CHANGES" > "slate/changes/$TODAY.md"
fi

ALERT_BODY=""
[ -n "$CHANGES" ]  && ALERT_BODY+="$CHANGES"$'\n'
[ -n "$UNSTAGED" ] && ALERT_BODY+="$UNSTAGED"$'\n'
[ -n "$ESCALATE" ] && ALERT_BODY+="$ESCALATE"$'\n'
if [ -n "$ALERT_BODY" ]; then
  alert "slate-daily $TODAY: action needed" "$ALERT_BODY"
else
  echo "slate-daily $TODAY: no changes, nothing unstaged, no recon escalations"
fi

if [ "$DRY" = 1 ]; then
  echo "[dry-run] skipping commit; slate/ left as-written"
  exit 0
fi
if [ -n "$CHANGES" ] || ! git diff --quiet -- slate/worklist.md 2>/dev/null \
   || [ -n "$(git status --porcelain -- slate/worklist.md slate/changes 2>/dev/null)" ]; then
  if grep -q '^INITIAL:' <<<"$CHANGES"; then
    MSG="slate: initial import $TODAY — $(python3 -c "import json;print(len(json.load(open('slate/latest.json'))['events']))") events"
  else
    n_add="$(grep -c 'ADDED' <<<"$CHANGES" || true)"
    n_rem="$(grep -c 'REMOVED' <<<"$CHANGES" || true)"
    MSG="slate: auto $TODAY — ${n_add:-0} added, ${n_rem:-0} removed"
  fi
  git add slate/ \
    && git commit -q -m "$MSG" -- slate/ \
    || fail "git commit of slate/ failed"
  echo "committed slate/ ($TODAY)"
else
  git checkout -q -- slate/latest.json 2>/dev/null || true  # timestamp-only churn: keep tree clean
  echo "content unchanged — no commit"
fi
