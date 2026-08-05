#!/usr/bin/env python3
"""leg1_playbyplay.py — fetch + characterize MLB Stats API play-by-play as a
candidate "leg 1" timing reference for the pack-lag pattern (see
pm-expansion-exploration/latency/lib/slope_fit.py).

VERIFIED LIVE (2026-08-05) against a real, settled game
(gamePk 824649, NYY @ CHC, 2026-08-01): the endpoint is free, unauthenticated,
and every play carries `about.startTime`/`about.endTime` as real ISO-8601
UTC timestamps. Internal consistency check passed: boxscore rosters match
the players named in playByPlay for both teams (an initial-looking mismatch
against this investigator's own prior knowledge of MLB rosters turned out to
be simply because this is a simulated 2026 season with different
roster/trade outcomes than the real world — not a data bug).

WHAT REMAINS UNVERIFIED, flagged rather than assumed: whether `startTime`
reflects the actual on-field instant or a stat-recorder's data-entry lag.
No independent broadcast-VOD timeline was available to cross-check this
against. Treat any offset derived from this leg 1 as carrying that
unquantified uncertainty on top of whatever the slope-fit itself reports.

CRITICAL FINDING, from inspecting one real game's KXMLBMENTION strikes
against its actual play log (see pack_lag_mlb.py's docstring for the full
writeup): most "announcer word" strikes on this market class do NOT map
cleanly onto a discrete in-game statistical event. "Grand Slam" and "Walk
Off" both settled YES on a game where NEITHER a grand slam NOR a walk-off
occurred — the market resolves on the WORD being said (which happens in
ordinary baseball commentary regardless of whether the specific play
occurs), not on the event itself. Their Kalshi trade tapes confirm this:
GRAN/WALK/BUNT were already priced 0.3-0.72 more than 24h before the game
even started, i.e. priced off base-rate commentary likelihood, not off any
in-game trigger. Only "Error" had a clean 1:1 match (a real Field Error at
2026-08-02T00:21:59.550Z, market settled YES) in this one game. This means
the structured-play-by-play shortcut this file was built to test only
applies to a NARROW subset of MLB mention strikes (rare, unambiguous,
discrete events like a real error/wild-pitch/grand-slam actually occurring)
— NOT to the majority, which need a transcript/ASR approach like WNT after
all.

Usage:
  leg1_playbyplay.py <gamePk>              # fetch + print play summary
  leg1_playbyplay.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

API = "https://statsapi.mlb.com/api/v1"


def get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_plays(game_pk: int) -> list[dict]:
    """Raw allPlays list from the playByPlay endpoint."""
    d = get(f"{API}/game/{game_pk}/playByPlay")
    return d.get("allPlays", [])


def fetch_boxscore_rosters(game_pk: int) -> dict:
    """{'away': {'team': name, 'players': {full_name set}}, 'home': {...}} —
    used to sanity-check playByPlay identity against the actual game, the
    same discipline as WNT's source-identity gate (wnt-daily.sh)."""
    d = get(f"{API}/game/{game_pk}/boxscore")
    out = {}
    for side in ("away", "home"):
        team = d["teams"][side]["team"]["name"]
        names = {p["person"]["fullName"] for p in d["teams"][side]["players"].values()}
        out[side] = {"team": team, "players": names}
    return out


def characterize(plays: list[dict]) -> dict:
    """Summarize what's actually in a play-by-play feed: field presence,
    timestamp span, and simple candidate "structural" events (ones with a
    plain-language `event` name that could serve as ground truth for a
    Kalshi mention strike whose word is tied to a discrete play)."""
    n = len(plays)
    have_start = sum(1 for p in plays if p.get("about", {}).get("startTime"))
    have_end = sum(1 for p in plays if p.get("about", {}).get("endTime"))
    events = sorted({p["result"].get("event") for p in plays if p["result"].get("event")})
    starts = [p["about"]["startTime"] for p in plays if p.get("about", {}).get("startTime")]
    return {
        "n_plays": n,
        "plays_with_startTime": have_start,
        "plays_with_endTime": have_end,
        "distinct_event_types": events,
        "first_play_start": starts[0] if starts else None,
        "last_play_start": starts[-1] if starts else None,
    }


def find_event(plays: list[dict], predicate) -> list[dict]:
    """Every play matching predicate(play) -> bool, with its timestamps."""
    out = []
    for p in plays:
        if predicate(p):
            out.append({
                "event": p["result"].get("event"),
                "description": p["result"].get("description"),
                "startTime": p["about"].get("startTime"),
                "endTime": p["about"].get("endTime"),
                "inning": p["about"].get("inning"),
                "halfInning": p["about"].get("halfInning"),
            })
    return out


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

    print("leg1_playbyplay self-test (synthetic fixture, no network):")

    fixture = [
        {"result": {"event": "Strikeout", "description": "x"},
         "about": {"startTime": "2026-08-01T23:15:06.929Z", "endTime": "2026-08-01T23:17:01.853Z"}},
        {"result": {"event": "Field Error", "description": "reaches on a fielding error"},
         "about": {"startTime": "2026-08-02T00:21:00.000Z", "endTime": "2026-08-02T00:21:59.550Z"}},
        {"result": {"event": "Groundout", "description": "x"},
         "about": {"startTime": "2026-08-02T00:22:10.000Z", "endTime": None}},
    ]
    c = characterize(fixture)
    chk("counts all plays", 3, c["n_plays"])
    chk("counts startTime presence", 3, c["plays_with_startTime"])
    chk("counts endTime presence (one missing)", 2, c["plays_with_endTime"])
    chk("distinct event types sorted", ["Field Error", "Groundout", "Strikeout"], c["distinct_event_types"])

    errors = find_event(fixture, lambda pl: "error" in (pl["result"].get("event") or "").lower())
    chk("finds the one error play", 1, len(errors))
    chk("error play carries its endTime", "2026-08-02T00:21:59.550Z", errors[0]["endTime"])

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("game_pk", nargs="?", type=int)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.game_pk:
        print("usage: leg1_playbyplay.py <gamePk> | --self-test", file=sys.stderr)
        return 2
    plays = fetch_plays(a.game_pk)
    print(json.dumps(characterize(plays), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
