#!/usr/bin/env bash
# coverage-check.sh — daily mention market coverage verification (W1.4).
#
#   scripts/coverage-check.sh [--dry-run]
#
# WHY THIS EXISTS: The 40-day stale slate (2026-08-07 → 2026-09-17) was
# UNDETECTED — all downstream jobs trusted it. A daily comparison of "live
# Kalshi board" vs "committed slate" would have paged on 2026-08-08.
#
# WHAT IT DOES:
#   1. Queries live Kalshi Mentions board (board-open.py or direct API)
#   2. Compares event_ticker set vs committed slate/latest.json
#   3. Checks slate age (bar: 30h — slate-daily runs 20:00 local daily)
#   4. Alerts on: MISSING-FROM-SLATE, STALE-SLATE, or query failure
#
# SCHEDULE (intended, NOT installed here): Daily, 03:00Z (after slate-daily
# 02:00Z has committed). Or append to slate-daily.sh as a post-commit check.
# Exit 0 = all live markets in slate, slate fresh. Exit 1 = delta or stale.
set -euo pipefail

REPO="${EARSHOT_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$REPO"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

alert() {
  if [ "$DRY" = 1 ]; then echo "[dry-run] ALERT: $1"; echo "$2" | sed 's/^/[dry-run]   /'
  else scripts/notify.sh "$1" "$2" --quiet || true; fi
}

TMPD="$(mktemp -d)"; trap 'rm -rf "$TMPD"' EXIT

# ── 1. Live board snapshot ───────────────────────────────────────────────────
# Two paths: (A) board-open.py if it takes --category (not verified), or
# (B) direct API call (known to work, discover-events.py uses it).
LIVE="$TMPD/live-board.json"
if python3 scripts/board-open.py --category Mentions > "$LIVE" 2>&1; then
  : # board-open.py succeeded
elif python3 - > "$LIVE" 2>&1 <<'PY'
import json, urllib.request
API = "https://api.elections.kalshi.com/trade-api/v2"
req = urllib.request.Request(f"{API}/events?status=open&limit=200&category=Mentions")
with urllib.request.urlopen(req, timeout=40) as r:
    data = json.load(r)
    # Paginate if needed (simplified: assumes <200 open mention events)
    json.dump(data, open("/dev/stdout", "w"))
PY
then
  : # Direct API succeeded
else
  alert "coverage-check FAILED" "Could not query live Kalshi board (both board-open.py and direct API failed). Coverage unknown."
  exit 1
fi

# ── 2. Compare vs committed slate ────────────────────────────────────────────
RESULT="$(python3 - "$LIVE" slate/latest.json <<'PY'
import json, sys, datetime

try:
    live_raw = json.load(open(sys.argv[1]))
    live_events = live_raw.get("events", [])
except Exception as e:
    print(f"ERROR: Could not parse live board JSON: {e}", file=sys.stderr)
    sys.exit(2)

try:
    committed = json.load(open(sys.argv[2]))
except FileNotFoundError:
    print("ERROR: slate/latest.json missing", file=sys.stderr)
    sys.exit(2)
except Exception as e:
    print(f"ERROR: Could not parse slate/latest.json: {e}", file=sys.stderr)
    sys.exit(2)

# Live tickers: only events with status=active/open (board-open.py may return all)
live_tickers = {
    e.get("event_ticker") for e in live_events
    if e.get("event_ticker") and e.get("status") in ("active", "open", "initialized")
}
slate_tickers = {e.get("event_ticker") for e in committed.get("events", [])}

missing = live_tickers - slate_tickers
stale = slate_tickers - live_tickers  # In slate but not live (closed since sweep)

# Slate age check
try:
    gen_utc = committed.get("generated_utc", "")
    gen_dt = datetime.datetime.fromisoformat(gen_utc.replace("Z", "+00:00"))
    now = datetime.datetime.now(datetime.timezone.utc)
    slate_age_h = (now - gen_dt).total_seconds() / 3600
except Exception:
    slate_age_h = 9999  # Unparseable → treat as ancient

lines = []
if missing:
    lines.append(f"MISSING-FROM-SLATE: {len(missing)} live events not in committed slate:")
    for t in sorted(missing)[:20]:  # Cap at 20 for email brevity
        lines.append(f"  {t}")
    if len(missing) > 20:
        lines.append(f"  ...and {len(missing)-20} more")

if slate_age_h > 30:
    lines.append(f"STALE-SLATE: {slate_age_h:.1f}h old (bar 30h) — generated {gen_utc}")

if lines:
    print("\n".join(lines))
    sys.exit(1)

print(f"OK: All {len(live_tickers)} live markets in slate (age {slate_age_h:.1f}h, {len(stale)} closed since sweep)")
PY
)" || {
  RC=$?
  if [ "$RC" -eq 2 ]; then
    alert "coverage-check ERROR" "$RESULT"
    echo "$RESULT"
    exit 1
  elif [ "$RC" -eq 1 ]; then
    # Delta or staleness detected — print + alert
    echo "$RESULT"
    alert "Mention market coverage: DELTA or STALE" "$RESULT

Action: Run scripts/slate-daily.sh to refresh, then investigate missing events."
    exit 1
  fi
}

echo "$RESULT"
exit 0
