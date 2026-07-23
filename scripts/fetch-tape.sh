#!/usr/bin/env bash
# fetch-tape.sh <EVENT_TICKER> [out.json]
# Read-only, no auth: archives the full public Kalshi trades tape for every
# market in an event (VERIFICATIONS.md §Q-MSPM endpoint). Default output:
# state/<EVENT_TICKER>/tape-<UTC date>.json  — the raw-archive convention
# used for MS (tape-2026-07-17.json) and DPZ (tape-2026-07-20.json).
set -euo pipefail
EV="${1:?usage: fetch-tape.sh <EVENT_TICKER> [out.json]}"
OUT="${2:-state/$EV/tape-$(date -u +%F).json}"
mkdir -p "$(dirname "$OUT")"
python3 - "$EV" "$OUT" <<'EOF'
import json, sys, time, urllib.request
ev, out = sys.argv[1], sys.argv[2]
base = "https://api.elections.kalshi.com/trade-api/v2"
def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)
mkts = []
cursor = None
while True:
    u = f"{base}/markets?event_ticker={ev}&limit=200" + (f"&cursor={cursor}" if cursor else "")
    d = get(u)
    mkts += d.get("markets", [])
    cursor = d.get("cursor")
    if not cursor or not d.get("markets"):
        break
if not mkts:
    sys.exit(f"no markets for event {ev}")
tape = {"event": ev, "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "markets": {m["ticker"]: {"yes_sub_title": m.get("yes_sub_title"),
                                   "status": m.get("status"), "result": m.get("result")}
                    for m in mkts},
        "trades": {}}
for m in mkts:
    t = m["ticker"]
    trades, cursor = [], None
    while True:
        u = f"{base}/markets/trades?limit=1000&ticker={t}" + (f"&cursor={cursor}" if cursor else "")
        d = get(u)
        trades += d.get("trades", [])
        cursor = d.get("cursor")
        if not cursor or not d.get("trades"):
            break
        time.sleep(0.15)
    tape["trades"][t] = trades
    print(f"{t}: {len(trades)} trades", file=sys.stderr)
    time.sleep(0.15)
with open(out, "w") as f:
    json.dump(tape, f)
print(out)
EOF
