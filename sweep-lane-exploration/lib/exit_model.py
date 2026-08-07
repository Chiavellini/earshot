#!/usr/bin/env python3
"""exit_model.py — can we get OUT, and was the trade ever actually riskless?

The intra-sweep idea is sometimes described as riskless: buy at 70c between
wave-1 and the decisive wave, sell into the wave at 97c, never carry settlement
risk. That is a claim about the *sell* side, and round 1 never modelled a sell
side at all — `fill_model.pnl_cents` only knows how to settle. This module is
the missing half, so the claim can be checked instead of assumed.

WHAT PROVES WE COULD HAVE SOLD
------------------------------
On Kalshi every trade at yes-price P is one party buying YES at P and the other
buying NO at (100 - P); `taker_side` says who was aggressive.

  * `taker_side == "no"` at yes-price P  ->  the taker bought NO at (100 - P),
    which is economically SELLING YES at P. To do that they had to match a
    RESTING BUY-YES order at P. So such a print proves a bid at P existed and
    was hittable — i.e. that we could have sold our YES at P.

That is the exact mirror of `fill_model`'s entry rule, and it inherits the same
one-sided conservatism: a bid nobody happened to hit is invisible to us, so this
UNDERSTATES our ability to exit. Errors point away from the strategy.

FEES
----
Both legs are charged the full taker fee (`fill_model.fee_cents`, the mirror of
`src/kalshi/executor.rs::fee_cents`, verified 300/300 against compiled Rust).
Resting the exit would likely make us the maker, and Kalshi charges no maker fee
on these series (VERIFICATIONS Q5) — so the real round trip is probably cheaper
than this. Again: against the strategy.

RISKLESS IS A MEASURABLE CLAIM
------------------------------
`adverse_excursion()` reports the worst price the market printed while we held.
A round trip that finished +27c after the market traded 40c below our entry was
not riskless; it was a winning gamble. Reporting the excursion is what stops
"we always got out" from hiding "we were underwater the whole time".

Read-only. No network.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fill_model import COUNT, fee_cents, pnl_cents  # noqa: E402


def _prints(rows, side, t0, t1):
    """Prints STRICTLY after t0. Same load-bearing exclusive bound as fill_model.

    Kalshi batches a whole ladder into one microsecond timestamp, so an
    inclusive bound would let an exit claim size printed in the same instant we
    entered — selling to ourselves.
    """
    return [r for r in rows if r["taker"] == side and t0 < r["ts"] <= t1]


def sell_yes(rows, after, within_s, min_price_cents):
    """FIRST chance to sell YES at or above `min_price_cents`. None if never.

    EARLIEST, not best. Taking the highest print in the window is choosing our
    exit in hindsight — the same lookahead that, on the entry side, let round 1
    buy NO at 1c because the market touched 99c sometime in the following hour.
    We model a resting sell order placed at `after` that fills the first time
    someone bids at or above it.

    Returns (price_cents, size_available, seconds_after).
    """
    end = after + timedelta(seconds=within_s)
    for r in _prints(rows, "no", after, end):
        if r["cents"] >= min_price_cents:
            return r["cents"], r["size"], (r["ts"] - after).total_seconds()
    return None


def adverse_excursion(rows, t0, t1, entry_cents):
    """Worst mark while holding: (lowest printed price, cents underwater).

    Any print counts, either side — a trade at 20c is evidence the market was at
    20c regardless of who was aggressive. Returns (None, None) if nothing
    printed while we held, which is itself worth reporting: it means the
    position was unmarkable, not that it was safe.
    """
    held = [r for r in rows if t0 < r["ts"] <= t1]
    if not held:
        return None, None
    low = min(r["cents"] for r in held)
    return low, max(0, entry_cents - low)


def round_trip_cents(entry_cents, exit_cents, count=COUNT):
    """Net cents on a buy-then-sell, both legs charged the taker fee."""
    return ((exit_cents - entry_cents) * count
            - fee_cents(count, entry_cents) - fee_cents(count, exit_cents))


def score_exit(rows, entry_ts, entry_cents, result, target_cents=97,
               hold_s=300, count=COUNT):
    """Score one position two ways: sell into the wave, or hold to settlement.

    Returns both, always, so the comparison is visible rather than assumed. The
    caller decides which the frozen rule uses; this module never picks the
    better one, because picking the better one per-trade IS the lookahead.
    """
    out = {"exit_price": None, "exit_delay_s": None,
           "sold_pnl": None, "held_pnl": None,
           "worst_mark": None, "underwater_cents": None}

    if result in ("yes", "no"):
        out["held_pnl"] = pnl_cents(entry_cents, result == "yes", count)

    got = sell_yes(rows, entry_ts, hold_s, target_cents)
    if got is not None:
        px, _, delay = got
        out.update(exit_price=px, exit_delay_s=round(delay, 3),
                   sold_pnl=round_trip_cents(entry_cents, px, count))
        low, under = adverse_excursion(rows, entry_ts,
                                       entry_ts + timedelta(seconds=delay), entry_cents)
    else:
        low, under = adverse_excursion(rows, entry_ts,
                                       entry_ts + timedelta(seconds=hold_s), entry_cents)
    out["worst_mark"], out["underwater_cents"] = low, under
    return out


# --- self-test ---------------------------------------------------------------

T0 = datetime(2026, 7, 23, 12, 30, tzinfo=timezone.utc)


def _row(cents, off_s, size, taker):
    return {"ts": T0 + timedelta(seconds=off_s), "cents": cents,
            "size": size, "taker": taker}


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

    print("exit_model self-test:")

    # A taker-NO print at yes-price 97 proves a resting BID at 97: we could sell.
    rows = [_row(97, 5, 40, "no")]
    chk("a taker-NO print at 97c lets us sell YES at 97c", (97, 40, 5.0),
        sell_yes(rows, T0, 30, 97))
    chk("a bid below our target does not fill us", None,
        sell_yes([_row(90, 5, 40, "no")], T0, 30, 97))
    chk("a bid outside the hold window does not fill us", None,
        sell_yes(rows, T0, 3, 97))

    # A taker-YES print is someone LIFTING an offer. It proves an offer existed,
    # not a bid, so it cannot be our exit. This is the mirror of fill_model's
    # "a taker-NO print cannot fill a YES buy".
    chk("a taker-YES print cannot be our exit", None,
        sell_yes([_row(99, 5, 500, "yes")], T0, 30, 97))

    # EARLIEST, not best: a better bid later must not be preferred.
    ladder = [_row(97, 5, 10, "no"), _row(99, 600, 900, "no")]
    chk("takes the EARLIEST acceptable bid, not a better one later", (97, 10, 5.0),
        sell_yes(ladder, T0, 3600, 97))
    # ...but a bid below target is correctly skipped until one clears it.
    skip = [_row(80, 5, 10, "no"), _row(98, 9, 25, "no")]
    chk("bids below target are skipped, the next clearing one fills", (98, 25, 9.0),
        sell_yes(skip, T0, 30, 97))

    # Strict bound: an exit at the entry instant would be selling to ourselves.
    chk("no exit on a print at the entry instant itself", None,
        sell_yes([_row(99, 0, 500, "no")], T0, 30, 97))
    batch = [_row(95 + i, 0, 50, "no") for i in range(5)]
    chk("an entire simultaneous batch is excluded, not just one row", None,
        sell_yes(batch, T0, 30, 97))

    # Round-trip arithmetic: buy 70, sell 97. fee(70)=2, fee(97)=1.
    chk("buy 70 sell 97 nets 27 - 2 - 1", 24, round_trip_cents(70, 97))
    chk("buy 70 sell 60 loses 10 + fees", -14, round_trip_cents(70, 60))
    chk("a flat round trip still costs both fees", -4, round_trip_cents(70, 70))

    # The riskless claim: a winning round trip that was deeply underwater.
    dip = [_row(30, 10, 200, "yes"), _row(97, 60, 40, "no")]
    s = score_exit(dip, T0, 70, "yes", target_cents=97, hold_s=300)
    chk("a winning round trip is scored as winning", (97, 24), (s["exit_price"], s["sold_pnl"]))
    chk("...and is NOT riskless: it marked 40c underwater on the way",
        (30, 40), (s["worst_mark"], s["underwater_cents"]))

    # Both outcomes are always reported; the module never picks the better one.
    s = score_exit([_row(97, 5, 40, "no")], T0, 70, "yes")
    chk("sold and held are both reported", (24, 28), (s["sold_pnl"], s["held_pnl"]))
    chk("holding beats selling here, and the module says so rather than choosing",
        True, s["held_pnl"] > s["sold_pnl"])

    # No exit available -> sold_pnl stays None, held_pnl still scores.
    s = score_exit([_row(50, 5, 40, "no")], T0, 70, "no")
    chk("no acceptable bid -> no exit, recorded as None", None, s["sold_pnl"])
    chk("...and the held leg is still scored", -72, s["held_pnl"])

    # Unsettled markets are recorded, not silently dropped.
    chk("an unsettled market yields no held P&L", None,
        score_exit(rows, T0, 70, "")["held_pnl"])

    # Silence: nothing printed while we held is reported as unmarkable, not safe.
    s = score_exit([], T0, 70, "yes")
    chk("an unmarkable hold reports no worst mark rather than zero",
        (None, None), (s["worst_mark"], s["underwater_cents"]))

    # Fee mirror is shared with fill_model, not re-derived.
    chk("fees come from fill_model, not a local copy", 2, fee_cents(1, 70))

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(self_test())
