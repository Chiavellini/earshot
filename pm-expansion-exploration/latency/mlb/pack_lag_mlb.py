#!/usr/bin/env python3
"""pack_lag_mlb.py — MLB pack-lag check: how far behind (or ahead of) a
real in-game event does the KXMLBMENTION trading pack react?

REAL RUN, 2026-08-05, against gamePk 824649 (NYY @ CHC, 2026-08-01, settled):

Of KXMLBMENTION-26AUG01NYYCHC's 19 strikes, only "Error" (KXMLBMENTION-
26AUG01NYYCHC-ERRO) had an unambiguous 1:1 match against this game's actual
play log: one real Field Error at 2026-08-02T00:21:59.550Z (play end time).
"Grand Slam" and "Walk Off" both settled YES on this SAME game despite
NEITHER event occurring in it — their trade tapes show GRAN/WALK/BUNT were
already priced 0.3-0.72 more than 24 HOURS before the game even started,
i.e. priced off ordinary commentary base rates ("that's not a walk-off
situation", "reminds me of a grand slam last week"), not off any in-game
trigger. This means the play-by-play shortcut this file was built to test
works for only a narrow slice of MLB strikes (rare, discrete, unambiguous
plays), not the class as a whole.

Because only ONE strike per game has a trustworthy structural ground truth,
a single game cannot supply slope_fit.fit()'s required >=3 points / >=240s
span (see lib/slope_fit.py) -- MLB's own game length would supply plenty of
SPAN if there were more matched points, but there is only one per game. This
run therefore reports the raw single-point offset (informative, not a
fitted-and-verified slope) and states plainly that a real fit needs pooling
the same structural strike (e.g. "Error", or "Wild Pitch") across MANY
games, not one.

RAW RESULT for the one matched point (ERRO, this game):
  play end (leg 1, MLB Stats API) : 2026-08-02T00:21:59.550Z
  Kalshi ERRO sweep onset (leg 2) : see live run output below
  raw offset                      : sweep_onset - play_end, in seconds

Usage:
  pack_lag_mlb.py <event_ticker> <strike_code> <game_pk>
    e.g. pack_lag_mlb.py KXMLBMENTION-26AUG01NYYCHC ERRO 824649
  pack_lag_mlb.py --self-test
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
import kalshi_sweep as ks  # noqa: E402
import slope_fit  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import leg1_playbyplay as leg1  # noqa: E402


def run(event_ticker: str, strike_code: str, game_pk: int) -> dict:
    ticker = f"{event_ticker}-{strike_code}"
    plays = leg1.fetch_plays(game_pk)

    # Locate the one real play this strike code plausibly maps to. This is
    # deliberately narrow/manual (not a generic word matcher) -- exactly the
    # kind of leg-1 judgment call the plan flagged as venue-specific.
    matchers = {
        "ERRO": lambda pl: "error" in (pl["result"].get("event") or "").lower(),
        "GRAN": lambda pl: "grand slam" in (pl["result"].get("description") or "").lower(),
        "WALK": lambda pl: pl["about"].get("inning", 0) >= 9
                            and not pl["about"].get("isTopInning")
                            and pl["about"].get("isScoringPlay"),
        "WILD": lambda pl: "wild pitch" in (pl["result"].get("event") or "").lower(),
        "BUNT": lambda pl: "bunt" in (pl["result"].get("description") or "").lower(),
    }
    if strike_code not in matchers:
        return {"verdict": "NOT-RESOLVABLE",
                "reason": f"no leg-1 matcher defined for strike code {strike_code!r}"}

    hits = leg1.find_event(plays, matchers[strike_code])
    if not hits:
        return {"verdict": "NOT-RESOLVABLE",
                "reason": f"no play in this game matches {strike_code!r} — either it "
                          f"genuinely didn't happen (check whether Kalshi still settled "
                          f"YES: if so, the strike resolved on the WORD being said, not "
                          f"the event occurring, and this leg-1 shortcut does not apply)"}

    play_end = datetime.fromisoformat(hits[0]["endTime"].replace("Z", "+00:00"))

    tr = ks.trades(ticker)
    onset, why = ks.sweep_onset(tr, play_end, window_before_min=180.0, window_after_min=30.0)
    if onset is None:
        return {"verdict": "NOT-RESOLVABLE", "reason": f"sweep_onset: {why}",
                "matched_play": hits[0], "n_trades": len(tr)}

    raw_offset_s = (onset - play_end).total_seconds()
    return {
        "verdict": "SINGLE-POINT-ONLY",
        "note": "one matched strike per game cannot satisfy slope_fit's >=3-point "
                "requirement -- this is a raw offset, not a verified slope-1.0 fit. "
                "Pool this strike code across many games before trusting it as a "
                "real Δrace number.",
        "matched_play": hits[0],
        "play_end_utc": hits[0]["endTime"],
        "kalshi_sweep_onset_utc": onset.isoformat().replace("+00:00", "Z"),
        "raw_offset_s": round(raw_offset_s, 2),
        "n_trades": len(tr),
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

    print("pack_lag_mlb self-test (matcher logic only, no network):")
    fake_error_play = {
        "result": {"event": "Field Error", "description": "reaches on a fielding error"},
        "about": {"startTime": "2026-08-02T00:21:00.000Z", "endTime": "2026-08-02T00:21:59.550Z",
                  "inning": 5, "isTopInning": False, "isScoringPlay": False},
    }
    fake_strikeout = {
        "result": {"event": "Strikeout", "description": "x"},
        "about": {"startTime": "2026-08-01T23:15:06.929Z", "endTime": "2026-08-01T23:17:01.853Z",
                  "inning": 1, "isTopInning": True, "isScoringPlay": False},
    }
    hits = leg1.find_event([fake_error_play, fake_strikeout],
                            lambda pl: "error" in (pl["result"].get("event") or "").lower())
    chk("error matcher finds exactly the error play", 1, len(hits))
    chk("non-error play excluded", "Field Error", hits[0]["event"])

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("event_ticker", nargs="?")
    ap.add_argument("strike_code", nargs="?")
    ap.add_argument("game_pk", nargs="?", type=int)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not (a.event_ticker and a.strike_code and a.game_pk):
        print("usage: pack_lag_mlb.py <event_ticker> <strike_code> <game_pk> | --self-test",
              file=sys.stderr)
        return 2
    result = run(a.event_ticker, a.strike_code, a.game_pk)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("verdict") != "NOT-RESOLVABLE" else 1


if __name__ == "__main__":
    sys.exit(main())
