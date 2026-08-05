#!/usr/bin/env python3
"""slope_fit.py — generic two-clock join with a refuse-don't-fabricate gate.

Extracted as a PATTERN from scripts/wnt-pack-lag.py's session() fit block
(production, untouched, never imported from here). The core idea: join a
"programme clock" (offsets into a leg-1 timing reference — a VOD transcript,
a play-by-play feed) against a "market clock" (Kalshi/Polymarket sweep
onsets) via least-squares. The fitted slope must land near 1.0 — that is the
real alignment check: it says both clocks advance together, which a wrong
VOD, a mis-parsed feed, or news-driven repricing would all break. The
intercept (offset) is what we actually want: how far behind (or ahead of)
the event the market's reaction sits.

If the slope is wrong, or there isn't enough data to trust a line, this
refuses (NOT-RESOLVABLE + reason) rather than printing a number — the same
discipline wnt-pack-lag.py itself uses, restated here so every venue-class
build inherits it by construction instead of by copy-paste discipline.
"""

from __future__ import annotations


def fit(
    pairs: list[tuple[float, float]],
    slope_tolerance: float = 0.02,
    min_fit_points: int = 3,
    min_fit_span_s: float = 240.0,
):
    """pairs: [(programme_offset_s, market_wall_offset_s), ...] — both axes
    already expressed as seconds from the SAME nominal t0.

    Re-derive slope_tolerance/min_fit_points/min_fit_span_s per venue class
    rather than reusing WNT's (a 22-minute newscast has far less span to
    work with than a 3-hour game or podcast) — these are defaults, not a
    universal constant.

    Returns a dict: {verdict: "OK"|"NOT-RESOLVABLE", ...}. On "OK":
    slope, span_s, offset_s (the intercept), deltas (per-pair y-slope*x,
    i.e. the individual offset estimates the intercept was fit from).
    """
    n = len(pairs)
    if n < min_fit_points:
        return {
            "verdict": "NOT-RESOLVABLE",
            "reason": f"needs >={min_fit_points} fit points, have {n}",
        }

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    span = max(xs) - min(xs)
    if span < min_fit_span_s:
        return {
            "verdict": "NOT-RESOLVABLE",
            "reason": f"points span only {span:.0f}s (need {min_fit_span_s:.0f}s) — "
                      f"too little of the event to trust a line",
        }

    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return {"verdict": "NOT-RESOLVABLE", "reason": "all fit points share one x value"}
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    intercept = my - slope * mx

    if abs(slope - 1.0) > slope_tolerance:
        return {
            "verdict": "NOT-RESOLVABLE",
            "reason": f"slope {slope:.3f} != 1.00 — the two clocks do not advance "
                      f"together, so the join is wrong",
            "slope": round(slope, 4),
        }

    deltas = [y - x for x, y in pairs]
    return {
        "verdict": "OK",
        "slope": round(slope, 4),
        "span_s": round(span, 1),
        "n": n,
        "offset_s": round(intercept, 2),
        "offset_mean_s": round(sum(deltas) / len(deltas), 2),
        "offset_spread_s": round(max(deltas) - min(deltas), 2),
        "deltas": [round(d, 2) for d in deltas],
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

    print("slope_fit self-test:")

    # Perfect slope-1.0 line with a constant -6s offset (pack at the event).
    pairs = [(x, x - 6.0) for x in (0, 100, 200, 300, 400)]
    r = fit(pairs)
    chk("clean slope-1 line resolves OK", "OK", r["verdict"])
    chk("recovers the -6s offset", -6.0, r["offset_s"])
    chk("slope reads 1.0", 1.0, r["slope"])

    # Too few points to trust.
    r = fit([(0, -6.0), (100, -6.0)])
    chk("too few points refuses", "NOT-RESOLVABLE", r["verdict"])

    # Enough points but they don't span enough of the event.
    r = fit([(0, -6.0), (10, -6.0), (20, -6.0)], min_fit_span_s=240.0)
    chk("too little span refuses", "NOT-RESOLVABLE", r["verdict"])

    # Wrong slope (e.g. a mis-joined feed) must refuse, not report a fabricated offset.
    pairs_wrong = [(0, 0), (100, 40), (200, 90), (300, 145), (400, 210)]
    r = fit(pairs_wrong)
    chk("bad slope refuses instead of reporting a number", "NOT-RESOLVABLE", r["verdict"])
    chk("bad-slope refusal still reports the measured slope", True, "slope" in r)

    # A delayed pack (e.g. +45s, watching a delayed stream) is recovered correctly.
    pairs_delayed = [(x, x + 45.0) for x in (0, 150, 300, 450)]
    r = fit(pairs_delayed)
    chk("delayed pack resolves OK", "OK", r["verdict"])
    chk("recovers +45s offset", 45.0, r["offset_s"])

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
