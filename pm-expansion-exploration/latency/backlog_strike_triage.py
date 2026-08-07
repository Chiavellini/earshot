#!/usr/bin/env python3
"""backlog_strike_triage.py — pull real, current strike text (rules_primary)
for each Kalshi backlog ticker listed in
mention-market-exploitability-scoreboard.md's Backlog section, so
classification (rare-discrete-event vs. predictable-word) can be judged from
the actual resolution text instead of the ticker name alone.

Fetch-only. No classification logic here -- the rare-event/predictable-word/
mixed judgment is editorial (same discipline as pack_lag_mlb.py's hand-
authored matchers, not a keyword-matching auto-classifier) and gets applied
by hand when the scoreboard's Backlog section is rewritten from this output.

Public, no-auth Kalshi endpoints only (GET /events, GET /markets) -- same
base used by every other script in this program.

Usage:
  backlog_strike_triage.py [--top N]   # N highest-volume strikes per ticker (default 6)
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

API = "https://api.elections.kalshi.com/trade-api/v2"
UA = "earshot-backlog-triage/1.0"

TICKERS = [
    "KXOBAMAMENTION", "KXVANCEMENTION", "KXRUBIOMENTION", "KXAOCMENTION",
    "KXWARSHMENTION", "KXNEWSOMMENTION", "KXHEGSETHMENTION", "KXBESSENTMTPMENTION",
    "KXGUTFELDMENTION", "KXMTPMENTION", "KXHOMANMENTION", "KXNEWSNATIONMENTION",
    "KXHOCHULMENTION", "KXFEDGOVMENTION", "KXFNSMENTION", "KXFOXNEWSMENTION",
    "KXGOVERNORMENTION", "KXHANNITYMENTION", "KXTHEBEAT", "KXLATENIGHTMENTION",
    "KXCARNEYMENTION", "KXTALARICOMENTION", "KXFTNMENTION", "KXTHEWEEKNIGHTMENTION",
    "KXSTARMERMENTIONB", "KXMAMDANIMENTION", "KXMADDOWMENTION", "KXDIMONMENTION",
    "KXUNMENTION", "KXKHANNAMENTION",
]


def get(url: str, retries: int = 4):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def latest_event(series_ticker: str):
    """Prefer a settled event (has result=yes/no per strike); fall back to
    the most recent open one. Returns (event_ticker, status) or (None, None)."""
    for status in ("settled", "open"):
        d = get(f"{API}/events?series_ticker={series_ticker}&status={status}&limit=5")
        evs = d.get("events", [])
        if evs:
            return evs[0]["event_ticker"], status
    return None, None


def markets_detail(event_ticker: str):
    d = get(f"{API}/markets?event_ticker={event_ticker}")
    out = []
    for m in d.get("markets", []):
        out.append({
            "ticker": m["ticker"],
            "sub_title": m.get("yes_sub_title") or "",
            "rules_primary": (m.get("rules_primary") or "").replace("\n", " ").strip(),
            "volume_fp": float(m.get("volume_fp") or 0),
            "result": m.get("result"),
        })
    return out


def main():
    top_n = 6
    if "--top" in sys.argv:
        top_n = int(sys.argv[sys.argv.index("--top") + 1])

    only = None
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].split(","))

    out = {}
    for t in TICKERS:
        if only and t not in only:
            continue
        time.sleep(0.3)
        try:
            ev, status = latest_event(t)
        except Exception as e:
            out[t] = {"error": f"events fetch failed: {e}"}
            continue
        if ev is None:
            out[t] = {"error": "no open or settled events found"}
            continue
        try:
            strikes = markets_detail(ev)
        except Exception as e:
            out[t] = {"error": f"markets fetch failed: {e}", "event_ticker": ev}
            continue
        strikes.sort(key=lambda s: -s["volume_fp"])
        out[t] = {
            "event_ticker": ev,
            "event_status": status,
            "n_strikes": len(strikes),
            "top_strikes": strikes[:top_n],
        }

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
