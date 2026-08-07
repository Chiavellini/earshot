#!/usr/bin/env python3
"""replication.py — reproduce the provisional pre-study numbers, then explain the gap.

PRE-REGISTRATION.md P1 records a prior of FOLLOW -27.5c/trade (n=33) and FADE
+15.4c (n=77) over 9 events. The corrected pipeline does not reproduce those.
That is expected — three of the corrections were made *because* the provisional
method was wrong — but "expected" is not an explanation, so this file changes
one thing at a time and attributes the movement.

Step 0 is the provisional method transcribed verbatim, including its flaws:
    * window   = the densest 1800s trade bucket, widened to [-900s, +5400s]
    * sweep    = first taker-YES print >=50c that jumps >=20c or follows a >300s gap
    * peak     = max price within 1.5s of that print
    * FOLLOW   = ASSUME a fill at min(peak,70), skipped when peak > 70
    * FADE     = ASSUME a fill at (100-peak), skipped when peak >= 99
    * fee      = ceil(7*P*(100-P)/10000), count 1

Each later step swaps in one corrected component. Read-only.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

import data as D          # noqa: E402
import fill_model as FM   # noqa: E402
import sweep_segment as ss  # noqa: E402
import windows            # noqa: E402
from score_arms import TRAIN  # noqa: E402


def density_window(tape):
    """The provisional window heuristic, transcribed verbatim. Known circular."""
    allt = sorted(ss.normalize(v)[i]["ts"].timestamp()
                  for v in tape.values() for i in range(len(ss.normalize(v))))
    if not allt:
        return None
    b = defaultdict(int)
    for t in allt:
        b[int(t // 1800)] += 1
    c = max(b, key=lambda k: b[k])
    return (dt.datetime.fromtimestamp(c * 1800 - 900, dt.UTC),
            dt.datetime.fromtimestamp(c * 1800 + 5400, dt.UTC))


def provisional_sweeps(tape, win):
    """First-crossing 'sweep' per market, exactly as the provisional pass found them."""
    out = []
    for ticker, raw in tape.items():
        rows = ss.normalize(raw)
        y = [r for r in rows if win[0] <= r["ts"] <= win[1]]
        buys = [r for r in y if r["taker"] == "yes"]
        if not buys:
            continue
        on = None
        for i, r in enumerate(buys):
            if r["cents"] >= 50 and (
                i == 0 or buys[i - 1]["cents"] <= r["cents"] - 20
                or (r["ts"] - buys[i - 1]["ts"]).total_seconds() > 300
            ):
                on = r["ts"]
                break
        if on is None:
            continue
        peak = max(r["cents"] for r in buys
                   if on <= r["ts"] <= on + dt.timedelta(seconds=1.5))
        out.append((ticker, on, peak, raw))
    return out


def run(step, use_real_window, use_new_segment, use_real_fills):
    tot = {"follow": [0, 0], "fade": [0, 0]}      # [sum_pnl, n]
    for event, run_dir in sorted(D.event_dirs().items()):
        if event not in TRAIN:
            continue
        tape, labels = D.load_tape(event, run_dir), D.load_labels(event, run_dir)
        if not tape or not labels:
            continue

        if use_real_window:
            s, e, _ = windows.window(event)
            if s is None:
                continue
            win = (s, e)
        else:
            win = density_window(tape)
            if win is None:
                continue

        if use_new_segment:
            cands = [(x.ticker, x.end, x.terminal, tape[x.ticker])
                     for x in ss.event_episodes(tape, window=win)]
        else:
            cands = provisional_sweeps(tape, win)

        for ticker, at, peak, raw in cands:
            res = labels.get(ticker)
            if res not in ("yes", "no"):
                continue
            rows = ss.normalize(raw)

            # FOLLOW at a 70c cap
            if peak <= 70:
                if use_real_fills:
                    g = FM.fill_yes(rows, at, 30, 70)
                    px = g[0] if g else None
                else:
                    px = min(peak, 70)
                if px is not None:
                    tot["follow"][0] += FM.pnl_cents(px, res == "yes")
                    tot["follow"][1] += 1

            # FADE at (100 - peak)
            if peak < 99:
                if use_real_fills:
                    g = FM.fill_no(rows, at, 1800, 100 - peak)
                    px = g[0] if g else None
                else:
                    px = 100 - peak
                if px is not None:
                    tot["fade"][0] += FM.pnl_cents(px, res == "no")
                    tot["fade"][1] += 1

    f = tot["follow"]
    d = tot["fade"]
    print(f"{step:<52}"
          f"FOLLOW {f[0]/f[1] if f[1] else 0:>+7.1f}c n={f[1]:<4}"
          f"FADE {d[0]/d[1] if d[1] else 0:>+7.1f}c n={d[1]:<4}")
    return tot


def main():
    print("Replication of the provisional prior, one correction at a time")
    print("(TRAIN events only; PRE-REGISTRATION.md P1 recorded "
          "FOLLOW -27.5c n=33, FADE +15.4c n=77)\n")
    run("0. provisional method, transcribed verbatim", False, False, False)
    run("1. + real call windows (was: trade density)", True, False, False)
    run("2. + corrected sweep primitive (was: first >=50c)", True, True, False)
    run("3. + execution-realistic fills  = FULL PIPELINE", True, True, True)
    print("\nStep 0 is the number to compare against the prior. Steps 1-3 each\n"
          "isolate one correction; the movement between rows IS the explanation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
