#!/usr/bin/env python3
"""audit_r2.py — the quant-audit gates that score_r2.py does not already cover.

score_r2.py covers Gate 4 (S1-S8, LOEO) and the efficient-market null. What it
does not cover, and what this file adds:

  PLACEBO      Gate 3's real analogue. The permutation test in the framework
               shuffles a return series; the equivalent here is to keep every
               market the arm selected and DESTROY ONLY THE TIMING — fire at a
               random instant in the call window instead of at wave-1. If the
               edge survives that, it was never about wave-1, and the arm is
               just "buy cheap mention markets". This is the single most
               informative check available on banked data and round 1 never
               ran it.

  SHIFT        Gate 1's lookahead probe. Delay every decision by N seconds. A
               result that collapses under a small shift is riding information
               that arrives at the decision instant; a result built on a real
               multi-second give-back should degrade gracefully.

  COST         Gate 4's hard gate: must survive 2x costs. Kalshi's taker fee is
               exact (mirrored from executor.rs, verified 300/300), so 2x is not
               fee uncertainty — it stands in for slippage and for the capital
               cost of holding to settlement, which nothing here models.

  DAYS         Gate 4's year-by-year stability, at the granularity we have.

Read-only. Writes nothing.
"""

from __future__ import annotations

import bisect
import os
import random
import sys
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
sys.path.insert(0, HERE)

import data as D              # noqa: E402
import fill_model as FM       # noqa: E402
import intra_arm as IA        # noqa: E402
import wave_ladder as WL      # noqa: E402
import windows                # noqa: E402
from score_r2 import bootstrap_ci, collect, split_of, summarise, wilson  # noqa: E402
from sweep_segment import normalize  # noqa: E402


def _events(split, events=None, window_free=False):
    """Scoreable events, either by split or from an explicit list.

    `events` + `window_free` are for the series probe, whose events have no
    config and therefore no resolvable call time — see score_r2.collect().
    When window-free, the "window" handed downstream is the tape's own span, so
    a random placebo draw still has a bounded interval to draw from.
    """
    universe = (sorted((e, e) for e in events) if events is not None
                else sorted(D.event_dirs().items()))
    for event, run_dir in universe:
        if events is None and split_of(event) != split:
            continue
        tape, labels = D.load_tape(event, run_dir), D.load_labels(event, run_dir)
        if not tape or not labels:
            continue
        if window_free:
            ts = [r["ts"] for tr in tape.values() for r in normalize(tr)]
            if len(ts) < 2:
                continue
            yield event, tape, labels, (min(ts), max(ts))
            continue
        start, end, _ = windows.window(event)
        if start is None:
            continue
        yield event, tape, labels, (start, end)


def _compact_yes(rows):
    """Taker-YES prints as parallel (epoch-float, cents) arrays, time-ordered.

    PERFORMANCE, NOT SEMANTICS. The dict-of-datetimes form is fine for TRAIN's
    39 events but the series probe is 798 events / ~15k markets / millions of
    prints, and the placebos rescan every market on every draw. Holding two flat
    lists instead of a list of dicts cuts both the memory and the per-draw scan
    by about an order of magnitude, and lets `_fill_fast` bisect straight to the
    window instead of walking the tape from the start.

    Float epochs are exact enough here: the conversion is deterministic, so two
    prints Kalshi batched into the same microsecond still compare equal and the
    strict `>` bound still excludes the whole batch — which is the property
    `fill_model._prints` exists to guarantee.
    """
    ts, ce = [], []
    for r in rows:
        if r["taker"] == "yes":
            ts.append(r["ts"].timestamp())
            ce.append(r["cents"])
    return ts, ce


def _pool(split, want_trigger, events=None, window_free=False):
    """Markets that did (or did not) have a qualifying wave-1, in compact form."""
    out = []
    for _event, tape, labels, win in _events(split, events, window_free):
        for ticker, raw in tape.items():
            res = labels.get(ticker)
            if res not in ("yes", "no"):
                continue
            trig = WL.triggers(ticker, raw, window=None if window_free else win)
            if bool(trig) is want_trigger:
                ts, ce = _compact_yes(normalize(raw))
                if not ts:
                    continue
                out.append((ticker, (ts, ce), res,
                            (win[0].timestamp(), win[1].timestamp()),
                            [w.decision_ts.timestamp() for w in trig]))
    return out


def _fill_fast(compact, t0, wait_s, cap):
    """First taker-YES print in (t0, t0+wait_s] at or below cap. Cents, or None.

    Identical to the semantics of `fill_model.fill_yes`: strictly after t0,
    EARLIEST qualifying print rather than cheapest.
    """
    ts, ce = compact
    i = bisect.bisect_right(ts, t0)
    end = t0 + wait_s
    n = len(ts)
    while i < n and ts[i] <= end:
        if ce[i] <= cap:
            return ce[i]
        i += 1
    return None


def placebo_timing(split="train", draws=2000, seed=20260807,
                   cap=IA.CAP, wait_s=IA.MAX_WAIT_S, causal=True,
                   events=None, window_free=False):
    """Same markets, same rule, RANDOM trigger time. Destroys ONLY the timing.

    ⚠ `causal=False` reproduces the FIRST version of this test, which was
    INVALID and is kept only so the error is inspectable. It drew the random
    instant from the whole call window, which lets the placebo fire BEFORE the
    market ever swept — buying at 40c a market that is about to be repriced to
    99c, on the strength of knowing it will be. That is lookahead, and it made
    the placebo score +32.3c, *beating* the arm, which reads as "wave-1 adds
    nothing" when it actually reads "an oracle beats us".

    `causal=True` draws from [first qualifying wave-1, window end], so the
    placebo knows exactly what the arm knows and differs only in WHEN it acts.
    """
    rng = random.Random(seed)
    pool = _pool(split, True, events, window_free)
    if not pool:
        return None
    means, fills = [], []
    for _ in range(draws):
        tot = n = 0
        for _tk, compact, res, win, trig_ts in pool:
            lo = trig_ts[0] if causal else win[0]
            span = win[1] - lo - wait_s
            for _ in range(len(trig_ts)):
                t0 = lo + rng.uniform(0, max(0.0, span))
                got = _fill_fast(compact, t0, wait_s, cap)
                if got is None:
                    continue
                tot += FM.pnl_cents(got, res == "yes")
                n += 1
        if n:
            means.append(tot / n)
            fills.append(n)
    if not means:
        return None
    means.sort()
    return {"draws": len(means), "mean": sum(means) / len(means),
            "lo": means[int(0.025 * len(means))], "hi": means[int(0.975 * len(means))],
            "max": means[-1], "avg_fills": sum(fills) / len(fills),
            "markets": len(pool)}


def placebo_selection(split="train", draws=2000, seed=20260808,
                      cap=IA.CAP, wait_s=IA.MAX_WAIT_S,
                      events=None, window_free=False):
    """Does wave-1 pick BETTER MARKETS, or just cheap ones?

    Same rule, same random-time machinery, but on the markets in the same events
    that NEVER had a qualifying wave-1. If these pay the same, the trigger is
    not selecting anything and the arm is just "buy cheap mention markets".
    """
    rng = random.Random(seed)
    pool = _pool(split, False, events, window_free)
    if not pool:
        return None
    means = []
    for _ in range(draws):
        tot = n = 0
        for _tk, compact, res, win, _t in pool:
            span = win[1] - win[0] - wait_s
            t0 = win[0] + rng.uniform(0, max(0.0, span))
            got = _fill_fast(compact, t0, wait_s, cap)
            if got is None:
                continue
            tot += FM.pnl_cents(got, res == "yes")
            n += 1
        if n:
            means.append(tot / n)
    if not means:
        return None
    means.sort()
    return {"draws": len(means), "mean": sum(means) / len(means),
            "lo": means[int(0.025 * len(means))], "hi": means[int(0.975 * len(means))],
            "markets": len(pool)}


def shift(split="train", offsets=(0, 1, 5, 15, 60)):
    """Delay every decision by N seconds. Gate 1's lookahead probe."""
    out = []
    for off in offsets:
        tot = n = wins = 0
        for event, tape, labels, win in _events(split):
            for ticker, raw in tape.items():
                res = labels.get(ticker)
                if res not in ("yes", "no"):
                    continue
                rows = normalize(raw)
                for w in WL.triggers(ticker, raw, window=win):
                    t0 = w.decision_ts + timedelta(seconds=off)
                    t1 = t0 + timedelta(seconds=IA.MAX_WAIT_S)
                    got = next((r for r in rows
                                if r["taker"] == "yes" and t0 < r["ts"] <= t1
                                and r["cents"] <= IA.CAP), None)
                    if got is None:
                        continue
                    tot += FM.pnl_cents(got["cents"], res == "yes")
                    wins += res == "yes"
                    n += 1
        out.append((off, n, wins, tot / n if n else None))
    return out


def cost(trades, multipliers=(1.0, 1.5, 2.0, 3.0)):
    """Gate 4's hard gate. 2x stands in for slippage AND unmodelled capital cost."""
    f = [t for t in trades if t["outcome"] == "filled"]
    out = []
    for m in multipliers:
        tot = 0
        for t in f:
            gross = (100 - t["price"]) if t["result"] == "yes" else -t["price"]
            tot += gross - m * FM.fee_cents(1, t["price"])
        out.append((m, tot / len(f) if f else None))
    return out


def by_day(trades):
    f = [t for t in trades if t["outcome"] == "filled"]
    days = {}
    for t in f:
        days.setdefault(t["day"], []).append(t["pnl"])
    return sorted((d, len(v), sum(v) / len(v)) for d, v in days.items())


def main(argv):
    split = argv[argv.index("--split") + 1] if "--split" in argv else "train"
    trades, _ = collect(split)
    s = summarise(trades)
    real = s["per_trade"]
    print(f"QUANT-AUDIT — round 2, split={split.upper()}")
    print(f"  arm as frozen: {real:+.1f}c over {s['filled']} fills, "
          f"{s['wins']}/{s['filled']} YES\n")

    print("GATE 3a  PLACEBO / TIMING — same markets, random instant AFTER wave-1")
    pt_ = placebo_timing(split, causal=True)
    if pt_ is None:
        print("  no data")
    else:
        print(f"  {pt_['draws']} draws over {pt_['markets']} markets "
              f"(~{pt_['avg_fills']:.0f} fills each)")
        print(f"  placebo : {pt_['mean']:+.1f}c  95%[{pt_['lo']:+.1f},{pt_['hi']:+.1f}]"
              f"  best draw {pt_['max']:+.1f}c")
        print(f"  arm     : {real:+.1f}c   -> "
              + ("acting AT wave-1 beats acting later on the same markets"
                 if real > pt_["hi"] else
                 "INSIDE the placebo band — the timing adds nothing"))

    print("\n         (the INVALID first version of this test, kept visible:")
    bad = placebo_timing(split, draws=400, causal=False)
    if bad:
        print(f"          drawing from the WHOLE window scores {bad['mean']:+.1f}c and")
        print( "          'beats' the arm — because it can fire BEFORE the sweep, i.e.")
        print( "          buy at 40c a market it already knows will reprice to 99c.")
        print( "          That is lookahead, not evidence. Fixed above.)")

    print("\nGATE 3b  PLACEBO / SELECTION — same rule on markets that NEVER swept")
    ps = placebo_selection(split)
    if ps is None:
        print("  no data")
    else:
        print(f"  {ps['markets']} non-triggering markets in the same events")
        print(f"  placebo : {ps['mean']:+.1f}c  95%[{ps['lo']:+.1f},{ps['hi']:+.1f}]")
        print(f"  arm     : {real:+.1f}c   -> "
              + ("wave-1 selects genuinely better markets"
                 if real > ps["hi"] else
                 "INSIDE the band — the trigger is not selecting anything"))

    print("\nGATE 1  SHIFT — delay every decision; lookahead collapses, structure decays")
    print(f"  {'offset':>8}{'fills':>7}{'yes':>8}{'per-trade':>12}")
    for off, n, wins, pt in shift(split):
        mark = "  <-- as frozen" if off == 0 else ""
        print(f"  {off:>7}s{n:>7}{wins:>4}/{n:<3}"
              f"{(f'{pt:+.1f}c' if pt is not None else '—'):>12}{mark}")
    print("  A one-second shift that destroys the result would mean we are reading")
    print("  the decision instant itself. A multi-second give-back should not care.")

    print("\nGATE 4  COST — must clear 2x. Stands in for slippage and for the")
    print("        capital cost of holding to settlement, which nothing here models.")
    for m, pt in cost(trades):
        flag = "  <-- HARD GATE" if m == 2.0 else ""
        print(f"  {m:>4.1f}x fees : {pt:+.1f}c{flag}")

    print("\nGATE 4  STABILITY BY DAY — a result carried by one session is not robust")
    d = by_day(trades)
    pos = sum(1 for _, _, pt in d if pt > 0)
    for day, n, pt in d:
        print(f"  {day:<10}{n:>4} fills{pt:>+9.1f}c")
    print(f"  positive on {pos}/{len(d)} days"
          + ("" if pos / max(len(d), 1) >= 0.6 else "   <-- BELOW the 60% bar"))

    print("\nGATE 5  SACRED HOLDOUT — forward-only, code-enforced")
    print("  score_r2.py --split holdout refuses until 36 events or 2026-10-30.")
    print("  TRAIN is fully contaminated and declared as such; it specifies only.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
