#!/usr/bin/env python3
"""arms.py — the four trading arms, exactly as frozen in PRE-REGISTRATION.md §P5.

Each arm is a pure function: (market context) -> a trade dict or None. Nothing
here reads settlement to decide WHETHER to trade — only to score the trade
after the fact. That separation is the whole guard against lookahead, so the
trigger functions never receive `result` at all.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fill_model as FM  # noqa: E402
import sweep_segment as ss  # noqa: E402

# --- P5 parameters -----------------------------------------------------------
FOLLOW_CAP = 70
FOLLOW_WINDOW_S = 30
STALL_MAX = 89
STALL_WAIT_S = 120
STALL_RECOVERED = 95      # if it got here while we waited, the sweep was right: no trade
FADE_CAP = 60
FADE_WINDOW_S = 1800
LEVEL_LO, LEVEL_HI = 50, 89
LEVEL_CAP = 60
LEVEL_WINDOW_S = 3600
MISSED_CAP = 40
MISSED_WINDOW_S = 3600


def _rows(raw):
    return ss.normalize(raw)


def follow(ep, raw, result, cap=FOLLOW_CAP, window_s=FOLLOW_WINDOW_S):
    """Buy the YES residual after a sweep episode ends. The idea as posed."""
    r = FM.score(_rows(raw), ep.end, "yes", cap, window_s, result)
    r.update(arm="follow", ticker=ep.ticker, at=ep.end.isoformat(),
             terminal=ep.terminal, basket_n=ep.basket_n)
    return r


def fade(ep, raw, result, cap=FADE_CAP, window_s=FADE_WINDOW_S,
         stall_max=STALL_MAX, wait_s=STALL_WAIT_S, recovered=STALL_RECOVERED):
    """Buy NO after a sweep STALLS below `stall_max` and fails to recover.

    Returns None when the trigger does not fire (a real sweep that ran to 99
    is not a fade candidate) — distinct from firing and finding no offer.
    """
    if ep.terminal > stall_max:
        return None
    rows = _rows(raw)
    decide_at = ep.end + timedelta(seconds=wait_s)
    # Everything below uses only prices observable BEFORE decide_at.
    seen = [r["cents"] for r in rows if ep.end < r["ts"] <= decide_at]
    if seen and max(seen) >= recovered:
        return None                      # the market resolved the ambiguity against us
    r = FM.score(rows, decide_at, "no", cap, window_s, result)
    r.update(arm="fade", ticker=ep.ticker, at=decide_at.isoformat(),
             terminal=ep.terminal, basket_n=ep.basket_n)
    return r


def level(ticker, raw, call_start, result, lo=LEVEL_LO, hi=LEVEL_HI,
          cap=LEVEL_CAP, window_s=LEVEL_WINDOW_S):
    """Buy NO on a market merely RESTING at 50-89c at call start.

    The arm the acceptance test forced into existence: AAL WORL/CENT/IRAN and
    INTC ARIZ/DESK/GOVE/MOBI were never swept during their calls, they were
    already sitting there, pre-priced off the press release.
    """
    resting = ss.resting_price(raw, call_start)
    if resting is None or not (lo <= resting <= hi):
        return None
    r = FM.score(_rows(raw), call_start, "no", cap, window_s, result)
    r.update(arm="level", ticker=ticker, at=call_start.isoformat(),
             terminal=resting, basket_n=0)
    return r


def missed(ticker, raw, call_start, result, had_episode,
           cap=MISSED_CAP, window_s=MISSED_WINDOW_S):
    """Buy YES cheap where the word WAS said but the market never swept.

    Settlement-confirmed 3x in the repo (LMT NASA ~20c for 52 min, AAL RECO,
    LMT SIKO). Ground truth for "the word was said" is the settlement itself,
    which is lookahead — so this arm is scored ONLY as an upper bound on the
    channel's size, never as a tradeable rule. Flagged `oracle=True`.
    """
    if had_episode or result != "yes":
        return None
    r = FM.score(_rows(raw), call_start, "yes", cap, window_s, result)
    r.update(arm="missed", ticker=ticker, at=call_start.isoformat(),
             terminal=None, basket_n=0, oracle=True)
    return r


def self_test():
    from datetime import datetime, timezone
    p = f = 0

    def chk(name, want, got):
        nonlocal p, f
        if want == got:
            print(f"  PASS  {name}")
            p += 1
        else:
            print(f"  FAIL  {name} (expected {want!r}, got {got!r})")
            f += 1

    print("arms self-test:")
    t0 = datetime(2026, 7, 23, 12, 30, tzinfo=timezone.utc)

    def mk(cents, off, size, taker="yes"):
        return {"created_time": (t0 + timedelta(seconds=off)).isoformat().replace("+00:00", "Z"),
                "yes_price_dollars": f"{cents/100:.4f}", "count_fp": f"{size:.2f}",
                "taker_side": taker}

    T = "KXTEST-26JUL23-AAAA"
    # A sweep that stalls at 65c, then a SMALL residual YES print, then a NO print.
    #
    # The residual must be small (2 ct, one print) or the merge rule absorbs it
    # into the episode. That is not a fixture quirk, it is what "residual"
    # means here: anything large enough to qualify as a sweep in its own right
    # IS part of the repricing, not something left over from it. FOLLOW
    # therefore fills, by construction, on sub-threshold size.
    raw = [mk(20, 0, 30), mk(65, 100, 200),
           mk(68, 105, 2),                        # residual YES, liftable, sub-threshold
           mk(64, 400, 40, "no")]                 # NO buyable at 36c, after decide_at
    eps = ss.episodes(T, raw)
    chk("fixture yields exactly one episode", 1, len(eps))
    ep = eps[0]
    chk("the small residual is NOT merged into the episode", 65, ep.terminal)

    # FOLLOW: cap 70 lifts the 68c residual; settles NO -> lose 68 + fee.
    r = follow(ep, raw, "no")
    chk("follow fills at the 68c residual", ("filled", 68), (r["outcome"], r["price"]))
    chk("follow P&L is the loss plus fee", -68 - FM.fee_cents(1, 68), r["pnl"])
    # cap below the residual -> no fill, recorded not dropped.
    chk("follow below the residual finds no offer", "no-offer",
        follow(ep, raw, "no", cap=60)["outcome"])

    # FADE: fires because terminal 65 <= STALL_MAX, buys NO at 36, settles NO.
    r = fade(ep, raw, "no")
    chk("fade fires on a stalled sweep", ("filled", 36), (r["outcome"], r["price"]))
    chk("fade wins when it settles NO", 100 - 36 - FM.fee_cents(1, 36), r["pnl"])

    # FADE must NOT fire on a sweep that ran to 99.
    raw2 = [mk(20, 0, 30), mk(99, 100, 200), mk(98, 400, 40, "no")]
    chk("fade does not fire on a 99c sweep", None, fade(ss.episodes(T, raw2)[0], raw2, "yes"))

    # FADE must NOT fire if the market recovered to >=95 during the wait.
    raw3 = [mk(20, 0, 30), mk(65, 100, 200), mk(97, 150, 80), mk(96, 400, 40, "no")]
    chk("fade stands down if the market recovered while we waited",
        None, fade(ss.episodes(T, raw3)[0], raw3, "yes"))

    # ...and the recovery check must only see prices BEFORE the decision time.
    # Here the market runs to 99c at +900s, long after the +220s decision. That
    # must NOT retroactively cancel a trade we had already committed to.
    raw4 = [mk(20, 0, 30), mk(65, 100, 200), mk(64, 400, 40, "no"), mk(99, 900, 80)]
    r = fade(ss.episodes(T, raw4)[0], raw4, "yes")
    chk("a recovery after the decision does not cancel the trade (no lookahead)",
        "filled", r["outcome"])
    chk("...and that trade correctly LOSES, since it settled YES",
        -36 - FM.fee_cents(1, 36), r["pnl"])

    # LEVEL: a market resting at 66c at call start, never swept.
    rest = [mk(66, -600, 50), mk(67, -300, 30), mk(65, 500, 40, "no")]
    r = level(T, rest, t0, "no")
    chk("level fires on a 67c resting market", ("filled", 35), (r["outcome"], r["price"]))
    chk("level does not fire on a 20c market", None,
        level(T, [mk(20, -600, 50)], t0, "no"))
    chk("level does not fire on a 97c market", None,
        level(T, [mk(97, -600, 50)], t0, "no"))

    # MISSED is oracle-flagged and only fires on an unswept YES.
    miss = [mk(20, 100, 50), mk(22, 200, 50)]
    r = missed(T, miss, t0, "yes", had_episode=False)
    chk("missed fires on an unswept market that settled YES", "filled", r["outcome"])
    chk("missed is flagged as oracle (not a tradeable rule)", True, r["oracle"])
    chk("missed does not fire where a sweep happened", None,
        missed(T, miss, t0, "yes", had_episode=True))
    chk("missed does not fire on a NO settle", None,
        missed(T, miss, t0, "no", had_episode=False))

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(self_test())
