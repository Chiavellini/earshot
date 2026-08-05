#!/usr/bin/env python3
"""schedule-cross-check.py — deep Kalshi ticker-date history, real day-of-week
and gap histogram, replacing slate/census.json's unreliable `cadence` field.

`scripts/mentions-census.py`'s `cadence_of()` buckets the MEDIAN GAP over at
most 6 SAMPLED events into daily/weekly/monthly/quarterly+/single-or-unknown.
That is both an undercount (a sport that plays nightly but whose 6 sampled
events happen to be one week's worth of already-open tickers reads
"single-or-unknown") and an overcount (a candidate whose 6 events happen to
land ~7 days apart by coincidence reads "weekly" even if genuinely ad-hoc).

This pulls much deeper event history per series (both settled and currently
open) and computes the REAL day-of-week distribution and gap distribution
from the full set, instead of a 6-sample median.

READ-ONLY against Kalshi's public API (no auth needed for GETs). Never
writes to slate/census.json — output goes only under
pm-expansion-exploration/latency/.

Usage:
  schedule-cross-check.py KXMLBMENTION KXWNBAMENTION KXTBPNMENTION ...
  schedule-cross-check.py --out pm-expansion-exploration/latency/kalshi-history.json <tickers...>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone

API = "https://api.elections.kalshi.com/trade-api/v2"
DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def ticker_date(event_ticker: str):
    """KXMLBMENTION-26AUG01NYYCHC -> date(2026,8,1). None if unparseable."""
    m = DATE_RE.search(event_ticker)
    if not m:
        return None
    yy, mon, dd = m.groups()
    mon = mon.upper()
    if mon not in MONTHS:
        return None
    try:
        return datetime(2000 + int(yy), MONTHS[mon], int(dd), tzinfo=timezone.utc).date()
    except ValueError:
        return None


def deep_events(series_ticker: str, limit: int = 200):
    """Pull up to `limit` events per status (settled + open), deep history.

    NOTE: Kalshi's /events objects carry NO top-level `status` field at all
    (verified live 2026-08-05 -- status lives on the nested /markets, not
    the event). So "is this event open" can only be answered by WHICH
    status-filtered query returned it, not by inspecting the returned
    object -- hence the explicit `open_tickers` provenance set below,
    rather than trusting an e.get("status") that doesn't exist.
    """
    out = {}
    open_tickers = set()
    for status_q, is_open_pull in (("&status=settled", False), ("&status=open", True)):
        cursor = ""
        pulled = 0
        while pulled < limit:
            url = f"{API}/events?series_ticker={series_ticker}&limit=200{status_q}"
            if cursor:
                url += f"&cursor={cursor}"
            for attempt in range(4):
                try:
                    d = get(url)
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code == 429 and attempt < 3:
                        time.sleep(2.0 * (attempt + 1))
                        continue
                    out.setdefault("_error", str(exc))
                    d = {"events": []}
                    break
            batch = d.get("events", [])
            for e in batch:
                out[e["event_ticker"]] = e
                if is_open_pull:
                    open_tickers.add(e["event_ticker"])
            pulled += len(batch)
            cursor = d.get("cursor") or ""
            if not cursor or not batch:
                break
            time.sleep(0.3)
    out["_open_tickers"] = open_tickers
    return out


def histogram(series_ticker: str, events: dict):
    open_tickers = events.get("_open_tickers", set())
    dates = sorted({d for et in events if et not in ("_error", "_open_tickers")
                     for d in [ticker_date(et)] if d})
    dow_counts = Counter(d.weekday() for d in dates)  # 0=Mon .. 6=Sun
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    gap_hist = Counter(gaps)
    return {
        "series_ticker": series_ticker,
        "n_events_pulled": len(events) - (1 if "_error" in events else 0),
        "n_dated_events": len(dates),
        "date_range": [str(dates[0]), str(dates[-1])] if dates else None,
        "day_of_week_counts": {
            ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][k]: v
            for k, v in sorted(dow_counts.items())
        },
        "gap_days_histogram": dict(sorted(gap_hist.items())),
        "median_gap_days": (sorted(gaps)[len(gaps) // 2] if gaps else None),
        "open_events_now": sorted(open_tickers),
        "fetch_error": events.get("_error"),
    }


def pm_open_events(tag_slug: str = "mention-markets"):
    """Live Polymarket open-events pull under the mention-markets tag, for
    cross-referencing against a Kalshi candidate's title/issuer."""
    req = urllib.request.Request(
        f"https://gamma-api.polymarket.com/events?closed=false&tag_slug={tag_slug}&limit=200",
        headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="+", help="Kalshi series tickers, e.g. KXMLBMENTION")
    ap.add_argument("--out", help="write full per-ticker JSON here")
    ap.add_argument("--limit", type=int, default=200)
    a = ap.parse_args()

    results = {}
    for t in a.tickers:
        ev = deep_events(t, a.limit)
        h = histogram(t, ev)
        results[t] = h
        print(f"{t}: {h['n_dated_events']} dated events, "
              f"{h['date_range']}, dow={h['day_of_week_counts']}, "
              f"median_gap={h['median_gap_days']}d, "
              f"open_now={len(h['open_events_now'])}")
        if h["fetch_error"]:
            print(f"  FETCH ERROR: {h['fetch_error']}")

    if a.out:
        with open(a.out, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
