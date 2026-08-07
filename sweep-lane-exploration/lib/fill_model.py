#!/usr/bin/env python3
"""fill_model.py — would we actually have been filled, and at what net P&L?

The provisional pre-study numbers assumed a fill at an INFERRED price: buy the
YES residual "at the sweep peak", buy NO "at (100 - peak)". Neither is
evidence. A trade proves a level TRADED; it does not prove an offer was
resting there for us.

The rule here is deliberately conservative and one-sided in the study's
disfavour:

    A fill is credited only where a same-side TAKER PRINT at our price or
    better exists in the window.

  * To buy YES we must lift a YES offer. A `taker_side == "yes"` print at
    price P proves a YES offer existed at P and was liftable.
  * To buy NO we must lift a NO offer. A `taker_side == "no"` print at
    yes-price P proves NO was buyable at (100 - P) at that instant.

This UNDERSTATES the opportunity — resting size nobody happened to hit is
invisible — which is the correct direction for an error to point.

Fees mirror `src/kalshi/executor.rs::fee_cents` exactly:
    fee = ceil(7 * count * price * (100 - price) / 10_000)      [integer cents]
Read from source 2026-08-06, not re-derived, and then VERIFIED by compiling
that exact Rust function and diffing its output against this one over
count in {1,5,10} x price in 1..=100: **300/300 exact**. Kalshi charges no
settlement fee and no maker fee on these series (VERIFICATIONS Q5).
"""

from __future__ import annotations

import math
import sys
from datetime import timedelta

# One contract. The live envelope cannot do more: MENTION_FILL_CAP_CENTS = 97
# against max_order_notional_cents = 100 admits exactly one contract per order,
# and PROBE_LEDGER_CAP_CENTS = 100 is a cumulative $1 across ALL runs
# (src/risk.rs). Sizing is therefore not a free parameter of this study.
COUNT = 1


def fee_cents(count, price_cents):
    """Mirror of src/kalshi/executor.rs::fee_cents. Integer cents, rounded UP."""
    price_cents = min(price_cents, 100)
    num = 7 * count * price_cents * (100 - price_cents)
    return -(-num // 10_000)          # div_ceil


def ev_gate(cap_cents, count=COUNT, haircut_cents=0, min_ev_cents=2):
    """Mirror of src/kalshi/executor.rs::ev_gate. Worst-case: all fills at cap."""
    if cap_cents + haircut_cents >= 100:
        return False
    gross = (100 - cap_cents - haircut_cents) * count
    return gross - fee_cents(count, cap_cents) >= min_ev_cents * count


def _prints(rows, side, t0, t1):
    """Prints STRICTLY after t0. The exclusive bound is load-bearing.

    An inclusive bound let FOLLOW fill on the sweep's own final print — i.e.
    claim size the sweeper had just consumed, at the sweeper's price. Kalshi
    batches a ladder into one microsecond timestamp ("10-11 fills same ms",
    COMPETITOR-ANATOMY), so `>` correctly drops the entire simultaneous batch
    rather than just one row of it.
    """
    return [r for r in rows if r["taker"] == side and t0 < r["ts"] <= t1]


def fill_yes(rows, after, within_s, max_price_cents):
    """FIRST liftable YES fill at or below `max_price_cents`. None if no fill.

    ⚠ EARLIEST, not cheapest. Taking the cheapest print in the window is
    choosing our execution in hindsight — a lookahead bias, and a large one:
    over a 3600s window it lets the model buy NO at 1c on a market that
    happened to touch 99c at some point during the hour. We model a resting
    order at the cap placed at `after`, which fills the first time someone
    offers at or under it.

    Returns (price_cents, size_available, seconds_after).
    """
    end = after + timedelta(seconds=within_s)
    for r in _prints(rows, "yes", after, end):
        if r["cents"] <= max_price_cents:
            return r["cents"], r["size"], (r["ts"] - after).total_seconds()
    return None


def fill_no(rows, after, within_s, max_price_cents):
    """FIRST liftable NO fill at or below `max_price_cents` (a NO price).

    A taker-NO print at yes-price P is a NO purchase at (100 - P). Earliest,
    not cheapest — see fill_yes.
    """
    end = after + timedelta(seconds=within_s)
    for r in _prints(rows, "no", after, end):
        if (100 - r["cents"]) <= max_price_cents:
            return 100 - r["cents"], r["size"], (r["ts"] - after).total_seconds()
    return None


def pnl_cents(price_cents, won, count=COUNT):
    """Net cents on a binary settle. `won` = the side we bought resolved true."""
    fee = fee_cents(count, price_cents)
    return (100 - price_cents) * count - fee if won else -price_cents * count - fee


def score(rows, onset, side, max_price_cents, within_s, result,
          count=COUNT, require_ev_gate=True):
    """Score one hypothetical trade. Returns a dict, always — never silently drops.

    `result` is the settlement, "yes" or "no". `side` is what we buy.
    `outcome` is one of: filled | no-offer | ev-gate-refused | unsettled.
    """
    out = {"side": side, "cap": max_price_cents, "outcome": None,
           "price": None, "size": None, "delay_s": None, "pnl": None}

    if result not in ("yes", "no"):
        out["outcome"] = "unsettled"
        return out

    if require_ev_gate and not ev_gate(max_price_cents, count):
        out["outcome"] = "ev-gate-refused"
        return out

    got = (fill_yes if side == "yes" else fill_no)(rows, onset, within_s, max_price_cents)
    if got is None:
        out["outcome"] = "no-offer"
        return out

    price, size, delay = got
    out.update(outcome="filled", price=price, size=round(size, 2),
               delay_s=round(delay, 3), pnl=pnl_cents(price, result == side, count))
    return out


# --- self-test ---------------------------------------------------------------

def _row(cents, off_s, size, taker, t0):
    return {"ts": t0 + timedelta(seconds=off_s), "cents": cents,
            "size": size, "taker": taker}


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

    print("fill_model self-test:")

    # Fee formula, checked against values computed from executor.rs's integer
    # div_ceil. 1 contract at 70c: 7*70*30 = 14700 -> ceil(14700/10000) = 2.
    chk("fee at 70c", 2, fee_cents(1, 70))
    chk("fee at 50c (worst case)", 2, fee_cents(1, 50))
    chk("fee at 97c", 1, fee_cents(1, 97))
    chk("fee at 99c", 1, fee_cents(1, 99))
    chk("fee at 1c", 1, fee_cents(1, 1))
    chk("fee at 100c is zero", 0, fee_cents(1, 100))
    chk("fee rounds UP, never down", 1, fee_cents(1, 2))   # 7*2*98=1372 -> 1

    # ev_gate mirrors the executor: at cap 97 with min_ev 2 it must REFUSE
    # (gross 3 - fee 1 = 2 >= 2 passes), at cap 98 it must refuse.
    chk("ev_gate passes at 70c", True, ev_gate(70))
    chk("ev_gate passes at 97c (gross 3 - fee 1 = 2)", True, ev_gate(97))
    chk("ev_gate refuses at 99c", False, ev_gate(99))
    chk("ev_gate refuses at/above 100c", False, ev_gate(100))

    t0 = datetime(2026, 7, 23, 12, 30, tzinfo=timezone.utc)

    # A YES fill is credited only where a taker-YES print at or below cap exists.
    rows = [_row(70, 5, 40, "yes", t0), _row(85, 8, 10, "yes", t0)]
    chk("YES fills at the first liftable print", (70, 40, 5.0),
        fill_yes(rows, t0, 30, 75))
    chk("YES with cap below every print does not fill", None, fill_yes(rows, t0, 30, 60))
    chk("YES outside the window does not fill", None, fill_yes(rows, t0, 3, 75))

    # REGRESSION (found 2026-08-06): taking the CHEAPEST print in the window is
    # choosing execution in hindsight. Over 3600s that let the model buy NO at
    # 1c because the market touched 99c at some point in the hour.
    later_cheaper = [_row(65, 5, 10, "yes", t0), _row(20, 600, 99, "yes", t0)]
    chk("takes the EARLIEST fill, not a cheaper one later", (65, 10, 5.0),
        fill_yes(later_cheaper, t0, 3600, 70))
    no_later_cheaper = [_row(60, 5, 10, "no", t0), _row(99, 600, 99, "no", t0)]
    chk("same for NO: earliest, not the 1c print an hour later", (40, 10, 5.0),
        fill_no(no_later_cheaper, t0, 3600, 50))

    # taker-NO prints must not satisfy a YES fill, and vice versa.
    chk("a taker-NO print cannot fill a YES buy", None,
        fill_yes([_row(70, 5, 40, "no", t0)], t0, 30, 75))

    # A taker-NO print at yes-price 70 is a NO purchase at 30.
    chk("NO fill price is 100 - yes_price", (30, 40, 5.0),
        fill_no([_row(70, 5, 40, "no", t0)], t0, 30, 40))
    # A print above our NO cap is skipped; the next one that clears it fills.
    non = [_row(40, 5, 10, "no", t0), _row(80, 6, 25, "no", t0)]
    chk("a print outside the cap is skipped, the next one fills", (20, 25, 6.0),
        fill_no(non, t0, 30, 50))

    # P&L arithmetic, both directions, fee-inclusive.
    chk("buy YES at 70, settles YES", 28, pnl_cents(70, True))     # 30 - 2
    chk("buy YES at 70, settles NO", -72, pnl_cents(70, False))    # -70 - 2
    chk("buy NO at 30, settles NO", 68, pnl_cents(30, True))       # 70 - 2
    chk("buy NO at 30, settles YES", -32, pnl_cents(30, False))    # -30 - 2

    # score() classifies every case rather than dropping any.
    s = score(rows, t0, "yes", 75, 30, "yes")
    chk("score: filled and profitable", ("filled", 70, 28), (s["outcome"], s["price"], s["pnl"]))
    s = score(rows, t0, "yes", 75, 30, "no")
    chk("score: filled and losing", ("filled", -72), (s["outcome"], s["pnl"]))
    chk("score: no offer is recorded, not dropped", "no-offer",
        score(rows, t0, "yes", 60, 30, "yes")["outcome"])
    chk("score: ev-gate refusal is recorded", "ev-gate-refused",
        score(rows, t0, "yes", 99, 30, "yes")["outcome"])
    chk("score: unsettled market is recorded", "unsettled",
        score(rows, t0, "yes", 75, 30, "")["outcome"])

    # The direction-of-error guarantee: an untaken resting offer is invisible,
    # so a window with no prints at all yields no-offer, never a free fill.
    chk("silence yields no-offer, never a free fill", "no-offer",
        score([], t0, "no", 40, 30, "no")["outcome"])

    # REGRESSION (found 2026-08-06): with an inclusive lower bound, a fill was
    # credited on the sweep's OWN final print - size the sweeper had just
    # taken, at the sweeper's price. The bound must be strict.
    at = t0
    same_ms = [_row(65, 0, 200, "yes", t0)]           # the sweep's last print
    chk("no fill on a print at the decision instant itself", None,
        fill_yes(same_ms, at, 30, 70))
    chk("a later print DOES fill", (68, 2, 5.0),
        fill_yes(same_ms + [_row(68, 5, 2, "yes", t0)], at, 30, 70))
    # Kalshi batches a whole ladder into one microsecond; all of it must drop.
    batch = [_row(60 + i, 0, 50, "yes", t0) for i in range(6)]
    chk("an entire simultaneous batch is excluded, not just one row", None,
        fill_yes(batch, at, 30, 70))

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(self_test())
