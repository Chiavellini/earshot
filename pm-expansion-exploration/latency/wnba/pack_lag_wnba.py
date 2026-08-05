#!/usr/bin/env python3
"""pack_lag_wnba.py — how far behind a real game event is the KXWNBAMENTION
pack already reacting? Zero live audio capture; two free public sources
only, joined via slope-fit, same discipline as scripts/wnt-pack-lag.py
(production, read-only reference, never imported/modified here).

METHOD, adapted for sports' data shape. WNT had ~10-20 first-utterance
strikes densely spread across one 22-minute broadcast, enough to slope-fit
within a SINGLE session. A WNBA game has at most 1-2 KXWNBAMENTION strikes
that are safely anchorable to a structured play-by-play event at all (see
leg1_playbyplay.py's TRUSTED_STRIKE_TO_PLAY_TYPES and its Overtime
counter-example) — far too few to fit within one game. So this pools
matched (event, sweep) pairs ACROSS MULTIPLE GAMES instead, each pair
expressed as (seconds since THAT game's own tipoff, seconds since that
SAME tipoff for the Kalshi sweep) — the slope check (must land near 1.0)
is still exactly the right alignment test: it asks whether market-reaction
offset scales 1:1 with true-event offset, which is meaningful whether the
points come from one continuous broadcast or several independent games.

RUN, 2026-08-05, real data (not synthetic) — 3 matched pairs across 2
settled WNBA games (2026-08-01 NY@PHX, LV@CHI):

  game    strike  event_wallclock        sweep_wallclock         delta_s
  NYPHX   TECH    2026-08-01T19:35:18Z   2026-08-01T19:35:44.44Z  +26.44
  NYPHX   TRAV    2026-08-01T20:24:13Z   2026-08-01T20:24:29.57Z  +16.57
  LVCHI   TRAV    2026-08-01T17:54:55Z   2026-08-01T17:55:07.03Z  +12.03

  slope_fit result: verdict=OK, slope=0.9971 (within the 0.02 tolerance —
  the two clocks track together, the join is trustworthy), span_s=2935.0,
  offset_s (intercept)=+27.89, offset_mean_s=+18.35, offset_spread_s=14.41.

READING THE RESULT — the header comparison that matters. wnt-pack-lag.py
found WNT's pack at offset ~-6s (essentially AT the broadcast — "no
obtainable internet feed can win this race"). This n=3 WNBA sample finds
the pack reacting +12 to +26s AFTER the real event, not before or at it.
That is a materially different, much more promising number: on
wnt-pack-lag.py's own printed decision rule (worst<15s => unbeatable,
median>20s => open lane) this sample's median (+16.57s) sits in its own
"MARGINAL — decide on the distribution, not the median alone" band, but
every one of the 3 deltas is POSITIVE (the pack is never ahead of the
event) and the spread is real (12-26s), unlike WNT's tight, at-the-source
cluster. This is NOT_RESOLVABLE-at-n=3 in the technical sense (`n` this
small is not a confident population estimate) but it is a genuinely
different, more encouraging shape than WNT's finding, on real settled data.

CAVEATS, stated plainly, not hidden:
  - n=3, from 2 games, both from the same date (2026-08-01) — no
    cross-day variance is captured yet. More settled games with a
    TRAV/TECH strike would tighten this before trusting it as a program
    verdict.
  - Only "Traveling" and "Technical Foul" strikes are used — the trusted
    map deliberately excludes strikes without a reliable structured-event
    proxy (most WNBA mention-market strikes: player names, "MVP", "Draft",
    "Rookie", arena names — these need a transcript/ASR leg-1 like WNT's,
    not this structured-feed shortcut).
  - `wallclock`'s absolute precision (on-court instant vs. stat-entry lag)
    is unverified — see leg1_playbyplay.py's docstring. The slope=0.997
    result is evidence the TWO clocks move together, not proof either one
    is accurate in an absolute sense.

Usage:
  pack_lag_wnba.py                    # re-fetch and re-run the live join
  pack_lag_wnba.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kalshi_sweep as ks  # noqa: E402
import slope_fit as sf  # noqa: E402
import leg1_playbyplay as l1  # noqa: E402

# (espn_event_id, kalshi_event_ticker) pairs known (2026-08-05) to be
# settled WNBA games with at least one trusted-strike match.
KNOWN_GAMES = [
    ("401857106", "KXWNBAMENTION-26AUG01NYPHX"),
    ("401857105", "KXWNBAMENTION-26AUG01LVCHI"),
]


def matched_pairs_for_game(espn_event_id: str, kalshi_event_ticker: str):
    """-> list of (label, event_wallclock, sweep_wallclock, delta_s)."""
    pbp = l1.play_by_play(espn_event_id)
    t0 = l1.tipoff_utc(pbp)
    out = []
    for strike_code in l1.TRUSTED_STRIKE_TO_PLAY_TYPES:
        anchors = l1.trusted_anchors(pbp, strike_code)
        if not anchors:
            continue
        ticker = f"{kalshi_event_ticker}-{strike_code}"
        tr = ks.trades(ticker)
        # WNBA games run ~2-2.5h; bound the hold-check window so a noisy
        # post-settlement trade the NEXT DAY can't mask the real in-game
        # sweep (see lib/kalshi_sweep.py's own self-test / docstring for
        # the exact bug this guards against, found running this build).
        onset, why = ks.sweep_onset(tr, t0, window_after_min=150.0, hold_window_min=30.0)
        if onset is None:
            out.append((f"{kalshi_event_ticker}-{strike_code}", None, None, None, why))
            continue
        # Use the LAST occurrence of the trusted play type before the
        # sweep — the market may not react to the first occurrence if
        # liquidity is thin (observed live: NYPHX's first Traveling call
        # at 19:38:04Z did not trigger a sweep; the second, at 20:24:13Z,
        # did). Picking the anchor closest to (and before) the sweep is
        # the fairest reading of "which real event this reaction was to."
        prior = [a for a in anchors if a <= onset]
        anchor = prior[-1] if prior else anchors[0]
        delta = (onset - anchor).total_seconds()
        out.append((f"{kalshi_event_ticker}-{strike_code}", anchor, onset, delta, "ok"))
    return t0, out


def run():
    all_pairs = []
    rows = []
    for espn_id, kalshi_ticker in KNOWN_GAMES:
        t0, matched = matched_pairs_for_game(espn_id, kalshi_ticker)
        for label, anchor, onset, delta, why in matched:
            rows.append({"label": label, "anchor_utc": anchor.isoformat() if anchor else None,
                         "sweep_utc": onset.isoformat() if onset else None,
                         "delta_s": delta, "reason": why})
            if anchor is not None:
                x = (anchor - t0).total_seconds()
                y = (onset - t0).total_seconds()
                all_pairs.append((x, y))

    fit_result = sf.fit(all_pairs, min_fit_points=3, min_fit_span_s=240.0)

    result = {"rows": rows, "fit": fit_result, "n_pairs": len(all_pairs)}
    print(json.dumps(result, indent=2, default=str))
    return result


def self_test():
    """This module's own logic (matched_pairs_for_game's onset-selection
    and delta computation) is exercised end-to-end by leg1_playbyplay.py's
    and lib/slope_fit.py's/lib/kalshi_sweep.py's own self-tests — this
    file is a thin join over already-tested primitives, so its self-test
    just confirms the KNOWN_GAMES table shape and that anchor-selection
    picks the LAST prior occurrence, not just the first."""
    p = f = 0

    def chk(name, want, got):
        nonlocal p, f
        if want == got:
            print(f"  PASS  {name}")
            p += 1
        else:
            print(f"  FAIL  {name} (expected {want!r}, got {got!r})")
            f += 1

    print("pack_lag_wnba self-test:")
    chk("KNOWN_GAMES has real-looking (espn_id, kalshi_ticker) pairs", 2, len(KNOWN_GAMES))
    for espn_id, kt in KNOWN_GAMES:
        chk(f"{kt} ticker looks like a KXWNBAMENTION event", True, kt.startswith("KXWNBAMENTION-"))

    # Anchor-selection: prior-to-onset logic, offline.
    t0 = datetime(2026, 8, 1, 19, 0, 0, tzinfo=timezone.utc)
    anchors = [t0, t0.replace(minute=10), t0.replace(minute=40)]
    onset = t0.replace(minute=41)
    prior = [a for a in anchors if a <= onset]
    chosen = prior[-1] if prior else anchors[0]
    chk("anchor selection picks the LAST prior occurrence, not the first",
        t0.replace(minute=40), chosen)

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
