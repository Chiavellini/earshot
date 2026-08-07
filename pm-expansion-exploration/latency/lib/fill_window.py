#!/usr/bin/env python3
"""fill_window.py — a different question than sweep_onset()'s.

sweep_onset() (kalshi_sweep.py / pm_sweep.py) answers "when does price
finally, durably cross toward $1" -- built for a RACE framing (are we
faster than the pack). This module answers a materially different
question raised 2026-08-07: even in a venue where the pack reacted before
we could ("GAMED"/"EMPTY-PACK"), was there ever a moment AFTER our own
detector would have confirmed the resolving word/event where a fillable
price below our ceiling (97c, per the standing evidence-gated fill posture)
was still available? If yes, a buy-and-hold strategy doesn't need to win
any race at all -- it only needs a fill to exist once, which "the market
already knew" does not by itself rule out. What DOES rule it out: no
seller left in the book after confirmation (illiquid dead market), or
price already at/above ceiling by the time confirmation is possible.

Both are real, distinct failure modes and this module reports which one
(if either) applies, rather than collapsing them into one verdict.
"""

from __future__ import annotations

from datetime import datetime


def fill_window(trades: list[dict], ref_time: datetime, ceiling: float = 0.97):
    """trades: normalized trade list (each dict has '_ts' datetime, '_yes'
    float 0..1) as returned by kalshi_sweep.trades()/pm_sweep.trades().
    ref_time: the instant our own detector would have confirmed the
    resolving word/event -- NOT sweep_onset()'s threshold-crossing time.

    Returns a dict, never fabricating a verdict the trade tape doesn't
    support:
      price_at_or_before_ref: last trade price at/before ref_time, or None
      trades_after_ref: count of trades strictly after ref_time
      fillable_after_ref: [(seconds_after_ref, price), ...] for every
        post-ref trade priced BELOW ceiling
      last_fillable_s: seconds after ref_time of the LAST fillable trade
        (None if zero) -- how long the window stayed open, a lower bound
        (the window may have stayed open past the last actual trade)
      first_at_ceiling_s: seconds after ref_time of the first trade AT or
        ABOVE ceiling (None if price never reaches ceiling in this tape)
      verdict:
        "NO-POST-REF-TRADES" -- market never traded again after ref_time
          at all (Netanyahu's shape: no seller to fill into, regardless
          of what price theory says)
        "NO-WINDOW" -- traded again, but every post-ref trade was already
          >= ceiling (market beat us to the ceiling before we could act)
        "WINDOW-OPEN" -- at least one fillable (< ceiling) trade exists
          after ref_time -- a buy-and-hold fill was genuinely available
    """
    after = [t for t in trades if t["_ts"] > ref_time]
    before_or_at = [t for t in trades if t["_ts"] <= ref_time]
    price_before = before_or_at[-1]["_yes"] if before_or_at else None

    if not after:
        return {
            "price_at_or_before_ref": price_before,
            "trades_after_ref": 0,
            "fillable_after_ref": [],
            "last_fillable_s": None,
            "first_at_ceiling_s": None,
            "verdict": "NO-POST-REF-TRADES",
        }

    fillable = [
        (round((t["_ts"] - ref_time).total_seconds(), 2), t["_yes"])
        for t in after if t["_yes"] < ceiling
    ]
    at_ceiling = [t for t in after if t["_yes"] >= ceiling]
    first_ceiling_s = (
        round((at_ceiling[0]["_ts"] - ref_time).total_seconds(), 2) if at_ceiling else None
    )

    verdict = "WINDOW-OPEN" if fillable else "NO-WINDOW"
    return {
        "price_at_or_before_ref": price_before,
        "trades_after_ref": len(after),
        "fillable_after_ref": fillable,
        "last_fillable_s": fillable[-1][0] if fillable else None,
        "first_at_ceiling_s": first_ceiling_s,
        "verdict": verdict,
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

    print("fill_window self-test:")
    from datetime import timezone, timedelta

    t0 = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)

    def tr(px, off_s):
        return {"_ts": t0 + timedelta(seconds=off_s), "_yes": px}

    # Netanyahu shape: nothing trades again after confirmation.
    dead = [tr(0.30, -600), tr(0.92, -60)]
    r = fill_window(dead, t0)
    chk("no post-ref trades -> NO-POST-REF-TRADES", "NO-POST-REF-TRADES", r["verdict"])
    chk("price_at_or_before_ref reflects the last pre-ref trade", 0.92, r["price_at_or_before_ref"])

    # Market already at ceiling by the time we could confirm.
    already_there = [tr(0.30, -600), tr(0.98, -1), tr(0.99, 5), tr(0.99, 30)]
    r = fill_window(already_there, t0)
    chk("every post-ref trade already at ceiling -> NO-WINDOW", "NO-WINDOW", r["verdict"])
    chk("first_at_ceiling_s is the first post-ref trade", 5.0, r["first_at_ceiling_s"])

    # A genuine fillable window: cheap for a while after confirmation, then swept.
    open_window = [tr(0.30, -600), tr(0.55, 2), tr(0.60, 10), tr(0.99, 90), tr(0.99, 120)]
    r = fill_window(open_window, t0)
    chk("fillable trades after ref -> WINDOW-OPEN", "WINDOW-OPEN", r["verdict"])
    chk("last_fillable_s is the last sub-ceiling trade", 10.0, r["last_fillable_s"])
    chk("first_at_ceiling_s is when it finally sweeps", 90.0, r["first_at_ceiling_s"])
    chk("fillable_after_ref carries both cheap trades", [(2.0, 0.55), (10.0, 0.60)],
        r["fillable_after_ref"])

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
