#!/usr/bin/env python3
"""poll_new_market.py — has Kalshi resumed listing a paused mention series?

Read-only, no auth. Originally built for `KXMLBMENTION`/`KXWNBAMENTION`,
which stopped listing new events after 2026-07-30 (last MLB event:
KXMLBMENTION-26AUG01NYYCHC, settled 2026-08-01) while other Kalshi
products, including MLB moneyline markets, kept listing normally over the
same window — a real pause, not a naming-convention change or an outage
(confirmed live: /exchange/status shows trading_active=true). Also watches
`KXTBPNMENTION`, whose listing cadence is irregular by nature rather than
paused (12 historical events since 2025-10-23, gaps ranging 4-98 days, no
day-of-week pattern) — same "poll and wait" shape, different underlying
cause.

This script is the dependency check for every live-capture-vs-price-pump
test in this investigation: there is nothing to test against until a new
event appears on the relevant series.

Usage:
  poll_new_market.py                          # KXMLBMENTION, human output
  poll_new_market.py --series KXWNBAMENTION
  poll_new_market.py --series KXTBPNMENTION
  poll_new_market.py --json
  poll_new_market.py --self-test
"""

import argparse
import json
import sys
import urllib.request

API = "https://api.elections.kalshi.com/trade-api/v2"

# The last known event per series, as of the date noted — the baseline this
# script checks against. Update the relevant entry once a genuinely new
# event is confirmed, so the script keeps reporting "new vs. this run"
# rather than "new vs. a stale hardcoded date forever."
KNOWN_LATEST = {
    "KXMLBMENTION": "KXMLBMENTION-26AUG01NYYCHC",     # as of 2026-08-05
    "KXWNBAMENTION": "KXWNBAMENTION-26AUG01NYPHX",    # as of 2026-08-05
    "KXTBPNMENTION": "KXTBPNMENTION-26JUL23",         # as of 2026-08-05
}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def latest_events(series, limit=5):
    """Newest-first page of events for a series, unfiltered by status."""
    url = f"{API}/events?series_ticker={series}&limit={limit}"
    return get(url).get("events", [])


def check(series, known_latest):
    events = latest_events(series)
    tickers = [e["event_ticker"] for e in events]
    # Is the known baseline still the newest ticker Kalshi reports?
    is_stale = bool(tickers) and tickers[0] == known_latest
    return {
        "series": series,
        "known_latest": known_latest,
        "pulled_latest": tickers[0] if tickers else None,
        "all_pulled": tickers,
        "new_since_baseline": [] if is_stale else [t for t in tickers if t != known_latest],
        "verdict": "STILL_PAUSED" if is_stale else ("NO_EVENTS_AT_ALL" if not tickers else "NEW_EVENT_FOUND"),
    }


def self_test():
    p = f = 0

    def chk(name, want, got):
        nonlocal p, f
        if want == got:
            print(f"  PASS  {name}")
            p += 1
        else:
            print(f"  FAIL  {name} (expected {want!r}, got {got!r})")
            f += 1

    print("poll_new_market self-test (logic only, no network):")

    global latest_events
    orig = latest_events

    # Simulate: API still returns the known baseline as newest -> paused.
    latest_events = lambda series, limit=5: [{"event_ticker": "KXMLBMENTION-26AUG01NYYCHC"}]
    r = check("KXMLBMENTION", "KXMLBMENTION-26AUG01NYYCHC")
    chk("MLB baseline unchanged -> STILL_PAUSED", "STILL_PAUSED", r["verdict"])
    chk("MLB: no new tickers reported", [], r["new_since_baseline"])

    # Simulate: a genuinely new event appears ahead of the baseline.
    latest_events = lambda series, limit=5: [
        {"event_ticker": "KXMLBMENTION-26AUG07SFTEX"},
        {"event_ticker": "KXMLBMENTION-26AUG01NYYCHC"},
    ]
    r = check("KXMLBMENTION", "KXMLBMENTION-26AUG01NYYCHC")
    chk("MLB: new ticker ahead of baseline -> NEW_EVENT_FOUND", "NEW_EVENT_FOUND", r["verdict"])
    chk("MLB: new ticker surfaced correctly", ["KXMLBMENTION-26AUG07SFTEX"], r["new_since_baseline"])

    # Simulate: series returns nothing at all.
    latest_events = lambda series, limit=5: []
    r = check("KXMLBMENTION", "KXMLBMENTION-26AUG01NYYCHC")
    chk("MLB: empty series -> NO_EVENTS_AT_ALL", "NO_EVENTS_AT_ALL", r["verdict"])

    # TBPN: same logic, different series/baseline — confirms the
    # generalization (moved out of mlb/, watching a 3rd series) works
    # identically rather than accidentally being MLB-ticker-shaped.
    latest_events = lambda series, limit=5: [{"event_ticker": "KXTBPNMENTION-26JUL23"}]
    r = check("KXTBPNMENTION", KNOWN_LATEST["KXTBPNMENTION"])
    chk("TBPN baseline unchanged -> STILL_PAUSED", "STILL_PAUSED", r["verdict"])

    latest_events = lambda series, limit=5: [
        {"event_ticker": "KXTBPNMENTION-26AUG10"},
        {"event_ticker": "KXTBPNMENTION-26JUL23"},
    ]
    r = check("KXTBPNMENTION", KNOWN_LATEST["KXTBPNMENTION"])
    chk("TBPN: new ticker ahead of baseline -> NEW_EVENT_FOUND", "NEW_EVENT_FOUND", r["verdict"])
    chk("TBPN: new ticker surfaced correctly", ["KXTBPNMENTION-26AUG10"], r["new_since_baseline"])

    latest_events = orig

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", default="KXMLBMENTION", choices=list(KNOWN_LATEST.keys()))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()

    result = check(a.series, KNOWN_LATEST[a.series])
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{result['series']}: {result['verdict']}")
        print(f"  known baseline : {result['known_latest']}")
        print(f"  pulled latest  : {result['pulled_latest']}")
        if result["new_since_baseline"]:
            print(f"  NEW EVENTS: {', '.join(result['new_since_baseline'])}")
    return 0 if result["verdict"] != "NEW_EVENT_FOUND" else 3  # distinct exit code = actionable


if __name__ == "__main__":
    sys.exit(main())
