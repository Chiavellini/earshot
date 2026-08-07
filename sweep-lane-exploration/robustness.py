#!/usr/bin/env python3
"""robustness.py — the pre-registered checks S1-S6 plus LOEO and the null (P7/P8).

Run after score_arms.py. Read-only; writes only into this lane's out/.
"""

from __future__ import annotations

import json
import os
import random
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
sys.path.insert(0, HERE)

import fill_model as FM                                  # noqa: E402
from score_arms import collect, summarise, bootstrap_ci  # noqa: E402

ARMS = ("follow", "fade", "level")


def _filled(trades, arm):
    return [t for t in trades if t["arm"] == arm and t["outcome"] == "filled"]


def s1_method_sanity(trades):
    """The 90-100c terminal bucket must price at ~0. If it shows edge, we are broken."""
    print("\nS1  METHOD SANITY — the 90-100c bucket must price at ~0")
    print("    (a sweep that ran to 99c is right ~97% of the time and correctly priced;")
    print("     edge THERE would mean the pipeline is manufacturing it)")
    for arm in ("follow", "fade"):
        s = [t for t in _filled(trades, arm)
             if t.get("terminal") is not None and t["terminal"] >= 90]
        if not s:
            print(f"    {arm:<8} n=0  (no trades in bucket — check not informative)")
            continue
        pt = sum(t["pnl"] for t in s) / len(s)
        flag = "  <-- INVESTIGATE" if abs(pt) > 15 and len(s) >= 10 else ""
        print(f"    {arm:<8} n={len(s):<4} per-trade {pt:+.1f}c{flag}")


def s2_window_free(split):
    print("\nS2  WINDOW-FREE VARIANT — no call window, size floor alone")
    tr, _ = collect(split, window_free=True)
    for arm in ARMS:
        s = summarise(tr, arm)
        pt = f"{s['per_trade']:+.1f}c" if s["per_trade"] is not None else "—"
        ci = f"[{s['ci_lo']:+.1f},{s['ci_hi']:+.1f}]" if s["ci_lo"] is not None else "n/a"
        print(f"    {arm:<8} fills={s['filled']:<5} per-trade {pt:>8}  95% CI {ci}")


def s3_grid(split):
    print("\nS3  PARAMETER GRID — an arm surviving only one cell is NOT-RESOLVED")
    print(f"    {'min_term':>9}{'min_jump':>9}{'min_ct':>8}{'wait_s':>8}"
          f"{'follow':>18}{'fade':>18}{'level':>18}")
    cells = 0
    positive = defaultdict(int)
    for mt in (40, 50, 60):
        for mj in (10, 15, 25):
            for mc in (10.0, 25.0, 50.0):
                for wait in (60, 120, 300):
                    tr, _ = collect(split,
                                    seg_kw={"min_terminal": mt, "min_jump": mj,
                                            "min_contracts": mc},
                                    arm_kw={"fade": {"wait_s": wait}})
                    cells += 1
                    cols = []
                    for arm in ARMS:
                        s = summarise(tr, arm)
                        if s["per_trade"] is None:
                            cols.append("       n=0      ")
                            continue
                        if s["ci_lo"] is not None and s["ci_lo"] > 0 and s["per_trade"] >= 5:
                            positive[arm] += 1
                        cols.append(f"{s['per_trade']:+8.1f}c n={s['filled']:<5}")
                    if wait == 120 and mc == 25.0:      # print a readable slice
                        print(f"    {mt:>9}{mj:>9}{mc:>8.0f}{wait:>8}" + "".join(
                            f"{c:>18}" for c in cols))
    print(f"\n    cells evaluated: {cells}")
    for arm in ARMS:
        print(f"    {arm:<8} cells meeting the promotion EV+CI bar: "
              f"{positive[arm]}/{cells}")


def s4_basket(trades):
    print("\nS4  BASKET DISCRIMINATOR (exploratory — cannot promote an arm this round)")
    rows = [t for t in trades if t["arm"] in ("follow", "fade")
            and t.get("basket_n") is not None and t["outcome"] == "filled"]
    for label, sel in (("isolated (basket_n=1)", lambda t: t["basket_n"] == 1),
                       ("basket (basket_n>=2)", lambda t: t["basket_n"] >= 2)):
        s = [t for t in rows if sel(t)]
        if not s:
            print(f"    {label:<24} n=0")
            continue
        p_yes = sum(1 for t in s if t["result"] == "yes") / len(s)
        print(f"    {label:<24} n={len(s):<4} P(YES)={p_yes:.0%}  "
              f"per-trade {sum(t['pnl'] for t in s)/len(s):+.1f}c")


def s6_overlap(trades):
    print("\nS6  FADE / LEVEL OVERLAP — are they the same trades wearing two names?")
    fade_k = {(t["event"], t["ticker"]) for t in _filled(trades, "fade")}
    lvl_k = {(t["event"], t["ticker"]) for t in _filled(trades, "level")}
    both = fade_k & lvl_k
    print(f"    fade markets {len(fade_k)}, level markets {len(lvl_k)}, "
          f"overlap {len(both)}"
          + (f" ({len(both)/len(fade_k):.0%} of fade)" if fade_k else ""))


def loeo(trades):
    print("\nLOEO  LEAVE-ONE-EVENT-OUT — no single event may carry a result")
    for arm in ARMS:
        f = _filled(trades, arm)
        if len(f) < 5:
            print(f"    {arm:<8} n={len(f)} — too few to test")
            continue
        events = sorted({t["event"] for t in f})
        means = []
        for ev in events:
            rest = [t for t in f if t["event"] != ev]
            if rest:
                means.append((sum(t["pnl"] for t in rest) / len(rest), ev))
        full = sum(t["pnl"] for t in f) / len(f)
        lo, hi = min(means), max(means)
        flips = sum(1 for m, _ in means if (m > 0) != (full > 0))
        print(f"    {arm:<8} full {full:+6.1f}c   LOEO range [{lo[0]:+.1f} (drop "
              f"{lo[1].replace('KXEARNINGSMENTION','')}), {hi[0]:+.1f} (drop "
              f"{hi[1].replace('KXEARNINGSMENTION','')})]   sign flips: {flips}/{len(means)}")


def null_test(trades, seed=20260806):
    """EFFICIENT-MARKET null. Not a label shuffle — that one is provably degenerate.

    Permuting settlements among a single-side arm's filled trades is CONSTANT:
        sum_pnl = 100*(#wins) - sum(prices) - sum(fees)
    depends only on the COUNT of wins, not which trades won, so every shuffle
    of a fixed label multiset returns the same mean. Zero variance, zero
    information. (Observed: all three arms returned identical min and max.)

    The null that actually says something: if the market is efficient, buying
    side S at price p wins with probability p/100, and expected P&L is exactly
    -fee. So we draw won_i ~ Bernoulli(p_i/100) and ask whether the arm's
    realized P&L sits outside that distribution.
    """
    print("\nNULL  EFFICIENT-MARKET NULL — under efficiency a trade pays exactly -fee")
    rng = random.Random(seed)
    for arm in ARMS:
        f = _filled(trades, arm)
        if len(f) < 5:
            print(f"    {arm:<8} n={len(f)} — too few to test")
            continue
        means = []
        for _ in range(10_000):
            tot = sum(FM.pnl_cents(t["price"], rng.random() < t["price"] / 100.0)
                      for t in f)
            means.append(tot / len(f))
        means.sort()
        real = sum(t["pnl"] for t in f) / len(f)
        lo, hi = means[int(0.025 * len(means))], means[int(0.975 * len(means))]
        p = sum(1 for m in means if m >= real) / len(means)
        verdict = ("BEATS the efficient null" if real > hi else
                   "WORSE than the efficient null" if real < lo else
                   "indistinguishable from an efficient market")
        print(f"    {arm:<8} real {real:+6.1f}c   efficient null "
              f"{sum(means)/len(means):+.1f}c 95%[{lo:+.1f},{hi:+.1f}]   "
              f"p={p:.3f}  {verdict}")


def s5_lag(trades):
    print("\nS5  SETTLEMENT LAG — capital is tied up from entry to settlement")
    print("    (not computed: settlement timestamps are only banked for 3 events;")
    print("     AAL swept 12:33Z and settled 17:08Z, so the order of magnitude is HOURS.")
    print("     Any EV figure here ignores capital cost and overstates efficiency.)")


def main(argv):
    split = argv[argv.index("--split") + 1] if "--split" in argv else "test"
    trades, _ = collect(split)
    print(f"ROBUSTNESS BATTERY — split={split.upper()}")
    s1_method_sanity(trades)
    s4_basket(trades)
    s6_overlap(trades)
    loeo(trades)
    null_test(trades)
    s5_lag(trades)
    s2_window_free(split)
    if "--grid" in argv:
        s3_grid(split)
    else:
        print("\nS3  parameter grid skipped (pass --grid; it is slow)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
