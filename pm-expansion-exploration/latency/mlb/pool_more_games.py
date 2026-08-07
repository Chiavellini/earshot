#!/usr/bin/env python3
"""pool_more_games.py — discover additional settled KXMLBMENTION games with
real ERRO/WILD strikes, resolve each to an MLB Stats API game_pk, and run
them through pack_lag_mlb.run() to grow the pooled raw-offset sample beyond
the original n=1 (gamePk 824649, ERRO only, 2026-08-05).

One-shot orchestration script, not a tested primitive — it automates the
discovery+run loop pack_lag_mlb.py's own docstring calls for ("pool this
strike code across many games before trusting it as a real Δrace number").
GRAN/WALK/BUNT stay excluded (GRAN/WALK confirmed GAMED on the one game
tested — priced off pre-game commentary base rate, not the in-game event;
BUNT has matcher logic but was never validated) — see pack_lag_mlb.py's
`matchers` table, unchanged here.

pack_lag_mlb.py only ever returns a raw single-point offset per (event,
strike) — it has no cross-game slope_fit (unlike pack_lag_wnba.py, which
normalizes to each game's own tipoff). This script does not add one; it
pools raw offsets and reports median/spread, which is the aggregation the
docstring itself calls for.

Usage:
  pool_more_games.py [--target N] [--max-events N] [--strikes ERRO,WILD]
"""

from __future__ import annotations

import argparse
import datetime
import json
import statistics
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pack_lag_mlb as plm  # noqa: E402

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
MLB_API = "https://statsapi.mlb.com/api/v1"
SERIES = "KXMLBMENTION"
EXCLUDED = {"GRAN", "WALK", "BUNT"}  # GAMED / never-validated, see pack_lag_mlb.py


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


_TEAM_ABBR = None


def team_abbreviations():
    global _TEAM_ABBR
    if _TEAM_ABBR is None:
        d = get(f"{MLB_API}/teams?sportId=1")
        _TEAM_ABBR = {t["abbreviation"].upper(): t["id"] for t in d.get("teams", []) if t.get("abbreviation")}
    return _TEAM_ABBR


def ticker_date_and_suffix(event_ticker: str):
    """'KXMLBMENTION-26AUG01NYYCHC' -> ('2026-08-01', 'NYYCHC')."""
    rest = event_ticker[len(SERIES) + 1:]
    date_part, suffix = rest[:7], rest[7:]
    dt = datetime.datetime.strptime(date_part, "%y%b%d")
    return dt.strftime("%Y-%m-%d"), suffix


_SCHED_CACHE: dict = {}


def resolve_game_pk(date_str: str, code_suffix: str):
    """-> (game_pk_or_None, reason_str). Single-game days need no team-code
    disambiguation; multi-game days are resolved via abbreviation splitting,
    refusing rather than guessing if no split is unambiguous."""
    if date_str not in _SCHED_CACHE:
        _SCHED_CACHE[date_str] = get(f"{MLB_API}/schedule?sportId=1&date={date_str}")
    sched = _SCHED_CACHE[date_str]
    games = [g for day in sched.get("dates", []) for g in day.get("games", [])]
    if not games:
        return None, "no MLB games scheduled that date"
    if len(games) == 1:
        return games[0]["gamePk"], "single game that date"
    abbrs = team_abbreviations()
    splits = [(code_suffix[:i], code_suffix[i:]) for i in range(1, len(code_suffix))
              if code_suffix[:i] in abbrs and code_suffix[i:] in abbrs]
    for away_abbr, home_abbr in splits:
        for g in games:
            away_id = g["teams"]["away"]["team"]["id"]
            home_id = g["teams"]["home"]["team"]["id"]
            if abbrs.get(away_abbr) == away_id and abbrs.get(home_abbr) == home_id:
                return g["gamePk"], "abbreviation match"
    return None, f"ambiguous: {len(games)} games that date, no abbreviation split matched {code_suffix!r}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=20,
                     help="stop once this many clean points collected per strike")
    ap.add_argument("--max-events", type=int, default=120)
    ap.add_argument("--strikes", default="ERRO,WILD")
    a = ap.parse_args()
    wanted_strikes = set(a.strikes.split(","))

    pooled = {s: [] for s in wanted_strikes}
    refusals = []
    n_events_seen = 0

    for ev in settled_events(SERIES):
        n_events_seen += 1
        if n_events_seen > a.max_events:
            break
        if all(len(pooled[s]) >= a.target for s in wanted_strikes):
            break
        event_ticker = ev["event_ticker"]
        try:
            date_str, suffix = ticker_date_and_suffix(event_ticker)
        except Exception as e:
            refusals.append({"event_ticker": event_ticker, "reason": f"ticker parse failed: {e}"})
            continue

        try:
            mkts = markets_for_event(event_ticker)
        except Exception as e:
            refusals.append({"event_ticker": event_ticker, "reason": f"markets fetch failed: {e}"})
            continue

        candidates = []
        for m in mkts:
            code = m["ticker"].rsplit("-", 1)[-1]
            if (code in wanted_strikes and code not in EXCLUDED
                    and m.get("result") == "yes" and float(m.get("volume_fp") or 0) > 0):
                candidates.append(code)
        if not candidates:
            continue
        candidates = [c for c in candidates if len(pooled[c]) < a.target]
        if not candidates:
            continue

        game_pk, why = resolve_game_pk(date_str, suffix)
        if game_pk is None:
            refusals.append({"event_ticker": event_ticker, "reason": f"game_pk: {why}"})
            continue

        for code in candidates:
            try:
                result = plm.run(event_ticker, code, game_pk)
            except Exception as e:
                refusals.append({"event_ticker": event_ticker, "strike": code, "reason": f"run() raised: {e}"})
                continue
            if result.get("verdict") == "SINGLE-POINT-ONLY":
                pooled[code].append({
                    "event_ticker": event_ticker,
                    "game_pk": game_pk,
                    "raw_offset_s": result["raw_offset_s"],
                    "play_end_utc": result["play_end_utc"],
                })
            else:
                refusals.append({"event_ticker": event_ticker, "strike": code,
                                  "reason": result.get("reason", result.get("verdict"))})

    summary = {}
    for code, pts in pooled.items():
        offs = [p["raw_offset_s"] for p in pts]
        if offs:
            summary[code] = {
                "n": len(offs),
                "median_s": statistics.median(offs),
                "mean_s": round(statistics.fmean(offs), 2),
                "stdev_s": round(statistics.pstdev(offs), 2) if len(offs) > 1 else None,
                "min_s": min(offs),
                "max_s": max(offs),
                "points": pts,
            }
        else:
            summary[code] = {"n": 0}

    out = {"events_scanned": n_events_seen, "summary": summary, "refusals": refusals}
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
