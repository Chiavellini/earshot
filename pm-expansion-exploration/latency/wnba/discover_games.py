#!/usr/bin/env python3
"""discover_games.py — find settled KXWNBAMENTION events with a real
TRAV/TECH strike (result=yes, volume>0), and resolve each to an ESPN event
id, printing ready-to-paste KNOWN_GAMES tuples for pack_lag_wnba.py.

Read-only discovery only — does not edit pack_lag_wnba.py itself.

Usage:
  discover_games.py
"""

from __future__ import annotations

import datetime
import json
import urllib.request

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
ESPN_BASE = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/wnba"
SERIES = "KXWNBAMENTION"
TRUSTED_STRIKES = {"TRAV", "TECH"}


def get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def settled_events(series_ticker: str, page_limit: int = 200):
    cursor = None
    while True:
        url = f"{KALSHI_API}/events?series_ticker={series_ticker}&status=settled&limit={page_limit}"
        if cursor:
            url += f"&cursor={cursor}"
        d = get(url)
        for e in d.get("events", []):
            yield e
        cursor = d.get("cursor")
        if not cursor:
            break


def markets_for_event(event_ticker: str):
    return get(f"{KALSHI_API}/markets?event_ticker={event_ticker}").get("markets", [])


def ticker_date_and_suffix(event_ticker: str):
    rest = event_ticker[len(SERIES) + 1:]
    date_part, suffix = rest[:7], rest[7:]
    dt = datetime.datetime.strptime(date_part, "%y%b%d")
    return dt, suffix


_SB_CACHE: dict = {}


def espn_scoreboard(date_yyyymmdd: str):
    if date_yyyymmdd not in _SB_CACHE:
        _SB_CACHE[date_yyyymmdd] = get(f"{ESPN_BASE}/scoreboard?dates={date_yyyymmdd}")
    return _SB_CACHE[date_yyyymmdd]


def resolve_espn_event_id(dt: datetime.datetime, code_suffix: str):
    """Try the ticker's own date and date+1 (known one-day labeling offset
    between Kalshi and ESPN). Single-game days need no disambiguation;
    multi-game days match via shortName team abbreviations."""
    for delta in (0, 1):
        d = dt + datetime.timedelta(days=delta)
        ymd = d.strftime("%Y%m%d")
        sb = espn_scoreboard(ymd)
        events = sb.get("events", [])
        if not events:
            continue
        if len(events) == 1:
            return events[0]["id"], f"single game on {ymd}"
        for e in events:
            short = (e.get("shortName") or "").upper().replace(" ", "").replace("@", "")
            if short and short in code_suffix.upper():
                return e["id"], f"shortName match on {ymd}"
    return None, "no unambiguous ESPN event found on ticker date or date+1"


def main():
    found = []
    refusals = []
    for ev in settled_events(SERIES):
        event_ticker = ev["event_ticker"]
        try:
            dt, suffix = ticker_date_and_suffix(event_ticker)
        except Exception as e:
            refusals.append({"event_ticker": event_ticker, "reason": f"ticker parse failed: {e}"})
            continue
        try:
            mkts = markets_for_event(event_ticker)
        except Exception as e:
            refusals.append({"event_ticker": event_ticker, "reason": f"markets fetch failed: {e}"})
            continue
        strikes = []
        for m in mkts:
            code = m["ticker"].rsplit("-", 1)[-1]
            if code in TRUSTED_STRIKES and m.get("result") == "yes" and float(m.get("volume_fp") or 0) > 0:
                strikes.append(code)
        if not strikes:
            continue
        espn_id, why = resolve_espn_event_id(dt, suffix)
        if espn_id is None:
            refusals.append({"event_ticker": event_ticker, "reason": f"espn_id: {why}"})
            continue
        found.append({"event_ticker": event_ticker, "espn_event_id": espn_id,
                       "strikes_with_volume": strikes, "why": why})

    print(json.dumps({"found": found, "refusals": refusals}, indent=2))
    print("\n# KNOWN_GAMES additions:")
    for f in found:
        print(f'    ("{f["espn_event_id"]}", "{f["event_ticker"]}"),  # {f["strikes_with_volume"]}')


if __name__ == "__main__":
    main()
