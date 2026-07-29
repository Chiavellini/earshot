#!/usr/bin/env bash
# allowlist-diff.sh <config.toml> [event_ticker]
#
# INDI-class gate (WING 2026-07-29: Kalshi listed 14 markets, the config armed
# 13 — the India market was never armed and its first-mention pump ran with no
# possible fire). Compares the config's allowlist_tickers against the LIVE
# market list for the event (public endpoint, no credentials) and prints both
# directions of drift. Exit 1 if the live board carries markets the config
# doesn't arm; exit 2 on fetch/parse errors; exit 0 clean.
set -uo pipefail
CFG="${1:?usage: allowlist-diff.sh <config.toml> [event_ticker]}"
[[ -f "$CFG" ]] || { echo "REFUSING: config not found: $CFG" >&2; exit 2; }

EV="${2:-$(python3 - "$CFG" <<'PY'
import re, sys
t = open(sys.argv[1]).read()
m = re.search(r'^\[event\]\s*$.*?^id\s*=\s*"([^"]+)"', t, re.M | re.S)
print(m.group(1) if m else "")
PY
)}"
[[ -n "$EV" ]] || { echo "REFUSING: no [event].id in $CFG and no ticker arg" >&2; exit 2; }

BODY="$(curl -sS -m 10 "https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=${EV}&limit=200")" \
  || { echo "REFUSING: market fetch failed for $EV" >&2; exit 2; }

BODY="$BODY" python3 - "$CFG" "$EV" <<'PY'
import json, os, re, sys
cfg_path, ev = sys.argv[1], sys.argv[2]
t = open(cfg_path).read()
m = re.search(r'allowlist_tickers\s*=\s*\[(.*?)\]', t, re.S)
allow = set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()
keywords = set(re.findall(r'market_ticker\s*=\s*"([^"]+)"', t))
d = json.loads(os.environ["BODY"])
live = {mk["ticker"]: mk.get("status", "?") for mk in d.get("markets", [])}
if not live:
    print(f"REFUSING: live board for {ev} is empty — wrong ticker or API change", file=sys.stderr)
    sys.exit(2)
missing_arm = sorted(set(live) - allow)          # live but we would not arm it
missing_kw  = sorted(allow - keywords)           # armed but no [[keywords]] entry
gone_live   = sorted(allow - set(live))          # armed but not on the board
print(f"event {ev}: live={len(live)} allowlisted={len(allow)} keyword-blocks={len(keywords)}")
for tk in missing_arm:
    print(f"  LIVE-NOT-ARMED: {tk}  (status={live[tk]})  <- INDI class")
for tk in missing_kw:
    print(f"  ARMED-NO-KEYWORD: {tk}")
for tk in gone_live:
    print(f"  ARMED-NOT-LIVE: {tk}")
sys.exit(1 if missing_arm else 0)
PY
