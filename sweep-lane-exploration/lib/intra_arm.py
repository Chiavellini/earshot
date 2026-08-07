#!/usr/bin/env python3
"""intra_arm.py — the one arm of round 2, per PRE-REGISTRATION-R2.md §R2.

    trigger   a wave-1 that qualifies on its OWN prints (lib/wave_ladder.py)
    action    rest a buy-YES bid at 70c, size 1, for 60s
    exit      hold to settlement

WHY THERE IS NO CANCEL RULE (amendment A1, 2026-08-07)
------------------------------------------------------
The rule first frozen here cancelled the bid the moment a print returned to
wave-1's top, on the argument that once the decisive wave has arrived we are no
longer buying "between the waves". That argument is falsified by TRAIN.

Measured: the cancel removes 10 of 22 fills, and those 10 settled **8/10 YES at
+21.8c** against the 12 it keeps at 9/12 and +20.2c. If the mechanism were real
the removed trades would be the bad ones. They are marginally the good ones — a
market that recovers to 99c is a market whose sweep was right, and cancelling
there discards exactly those. The cancel was not selecting for anything; it was
halving the sample.

So the arm is the simpler thing: a wave-1 reached the top of the ladder, rest a
bid at 70c for 60s, take whatever the book gives back. No cancel logic, one
fewer invented mechanism, and n=22 instead of 12.

`cancel_on_recovery=True` is retained because §S2 requires reporting it, and
because the falsification above is a result worth being able to re-derive. Its
recovery bound is STRICT: a print at exactly wave-1's top cancels, and any cheap
print sharing that microsecond is NOT ours — Kalshi batches a whole ladder into
one timestamp, so anything looser would claim the bottom rung of the very wave
that cancelled the order.

The 60s window itself is unchanged and was chosen before any of this: 15s and
30s both score higher AND clear their CI on TRAIN (+37.0c / +38.1c) where 60s
does not. They are not taken. That is the temptation this file exists to refuse.

Round 1's modules are reused unchanged and not edited (`fill_model` for entry
fills and fees, `exit_model` for the sell side). Read-only. No network.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import exit_model as XM      # noqa: E402
import fill_model as FM      # noqa: E402
import wave_ladder as WL     # noqa: E402
from sweep_segment import normalize  # noqa: E402

# --- Frozen (PRE-REGISTRATION-R2.md §R2) -------------------------------------
CAP = 70                    # the user's number, stated before anything was measured
MAX_WAIT_S = 60             # how long the bid rests; NOT the best cell (see above)
CANCEL_ON_RECOVERY = False  # amendment A1 — the mechanism was falsified on TRAIN
EXIT_TARGET = 97            # for the reported (unused) sell leg
EXIT_HOLD_S = 300


def entry_window(rows, wave, max_wait_s=MAX_WAIT_S,
                 cancel_on_recovery=CANCEL_ON_RECOVERY):
    """How long our bid rests: (end_ts, exclusive_end, reason).

    `exclusive_end` is True when a recovery print ends the window, because that
    print and everything sharing its microsecond belong to the decisive wave,
    not to us.

    `cancel_on_recovery=True` is the superseded variant (amendment A1), kept
    because §S2 requires it to be reported.
    """
    deadline = wave.decision_ts + timedelta(seconds=max_wait_s)
    if cancel_on_recovery:
        for r in rows:
            if r["ts"] > wave.decision_ts and r["cents"] >= wave.top:
                if r["ts"] <= deadline:
                    return r["ts"], True, "recovery"
                break
    return deadline, False, "timeout"


def _first_fill(rows, t0, t1, exclusive_end, cap):
    """Earliest liftable taker-YES print at or below `cap` inside the window.

    EARLIEST, not cheapest — taking the best print in a window is choosing our
    execution in hindsight, the lookahead round 1 found and regression-tested.
    """
    for r in rows:
        if r["taker"] != "yes" or r["ts"] <= t0:
            continue
        if r["ts"] > t1 or (exclusive_end and r["ts"] >= t1):
            break
        if r["cents"] <= cap:
            return r
    return None


def fire(wave, raw_trades, result, cap=CAP, max_wait_s=MAX_WAIT_S,
         exit_target=EXIT_TARGET, exit_hold_s=EXIT_HOLD_S, require_ev_gate=True,
         cancel_on_recovery=CANCEL_ON_RECOVERY):
    """Score the one hypothetical trade this trigger produces. Never returns None.

    Every trigger is classified — `filled | no-offer | ev-gate-refused |
    unsettled` — so a run's fill rate is readable and nothing is silently
    dropped, which is how round 1 kept itself honest.
    """
    rows = normalize(raw_trades)
    t_end, excl, why = entry_window(rows, wave, max_wait_s, cancel_on_recovery)

    out = {"ticker": wave.ticker, "wave_top": wave.top, "baseline": wave.baseline,
           "jump": wave.jump, "wave_contracts": round(wave.contracts, 2),
           "decision_ts": wave.decision_ts.isoformat(),
           "cancel_reason": why, "rest_s": round((t_end - wave.decision_ts).total_seconds(), 3),
           "cap": cap, "outcome": None, "price": None, "size": None, "delay_s": None,
           "pnl": None, "sold_pnl": None, "exit_price": None,
           "worst_mark": None, "underwater_cents": None}

    if result not in ("yes", "no"):
        out["outcome"] = "unsettled"
        return out
    if require_ev_gate and not FM.ev_gate(cap):
        out["outcome"] = "ev-gate-refused"
        return out

    got = _first_fill(rows, wave.decision_ts, t_end, excl, cap)
    if got is None:
        out["outcome"] = "no-offer"
        return out

    out.update(outcome="filled", price=got["cents"], size=round(got["size"], 2),
               delay_s=round((got["ts"] - wave.decision_ts).total_seconds(), 3),
               pnl=FM.pnl_cents(got["cents"], result == "yes"))
    x = XM.score_exit(rows, got["ts"], got["cents"], result,
                      target_cents=exit_target, hold_s=exit_hold_s)
    out.update(sold_pnl=x["sold_pnl"], exit_price=x["exit_price"],
               worst_mark=x["worst_mark"], underwater_cents=x["underwater_cents"])
    return out


# Which knobs belong to finding the trigger, and which to modelling the trade.
# Routing them by name keeps callers from having to know, and makes a typo an
# error rather than a silently ignored parameter.
_TRIGGER_KW = ("min_top", "min_jump", "min_contracts", "min_prints",
               "burst_gap_s", "baseline_lookback_s")


def event_trades(trades_by_ticker, labels, window=None, **kw):
    """Every trade this arm would have made across one event."""
    trig_kw = {k: v for k, v in kw.items() if k in _TRIGGER_KW}
    fire_kw = {k: v for k, v in kw.items() if k not in _TRIGGER_KW}
    out = []
    for ticker, raw in trades_by_ticker.items():
        result = labels.get(ticker)
        if result not in ("yes", "no"):
            continue
        for w in WL.triggers(ticker, raw, window=window, **trig_kw):
            r = fire(w, raw, result, **fire_kw)
            r["result"] = result
            out.append(r)
    return out


# --- self-test ---------------------------------------------------------------

T0 = datetime(2026, 7, 23, 12, 30, tzinfo=timezone.utc)


def _mk(cents, off_s, size, taker="yes"):
    return {"created_time": (T0 + timedelta(seconds=off_s)).isoformat().replace("+00:00", "Z"),
            "yes_price_dollars": f"{cents / 100:.4f}", "count_fp": f"{size:.2f}",
            "taker_side": taker}


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

    print("intra_arm self-test:")
    TK = "KXTEST-26JUL23-AAAA"

    # The canonical shape: wave-1 to 96c, book gives back to 65c, decisive wave.
    base = [_mk(20, 0, 10), _mk(90, 60, 200), _mk(96, 60.3, 150)]
    give = base + [_mk(65, 65, 40), _mk(99, 70, 500)]
    w = WL.triggers(TK, give)[0]
    r = fire(w, give, "yes")
    chk("fires on the give-back at 65c", ("filled", 65), (r["outcome"], r["price"]))
    chk("delay measured from wave-1's LAST print", 4.7, r["delay_s"])
    chk("P&L held to settlement", 33, r["pnl"])          # 35 - fee 2
    chk("the frozen rule rests the full window, no cancel logic (A1)",
        "timeout", r["cancel_reason"])
    chk("the superseded variant cancels on the decisive wave", "recovery",
        fire(w, give, "yes", cancel_on_recovery=True)["cancel_reason"])

    # Nothing offered inside the window -> no-offer, never a free fill.
    r = fire(WL.triggers(TK, base)[0], base, "yes")
    chk("silence yields no-offer", "no-offer", r["outcome"])
    chk("...and a timeout cancel", "timeout", r["cancel_reason"])
    chk("...having rested the full 60s", 60.0, r["rest_s"])

    # THE SUPERSEDED CANCEL RULE (A1). Under it a cheap print after the market
    # recovers is not ours; under the frozen rule it is. Both are asserted so
    # the difference the amendment turns on stays visible in the suite.
    late = base + [_mk(99, 65, 500), _mk(60, 68, 90)]
    chk("cancel variant: a print after recovery is NOT ours", "no-offer",
        fire(WL.triggers(TK, late)[0], late, "no", cancel_on_recovery=True)["outcome"])
    chk("FROZEN rule: the same print IS ours (this is the 10 trades A1 restores)",
        ("filled", 60),
        (lambda x: (x["outcome"], x["price"]))(fire(WL.triggers(TK, late)[0], late, "no")))
    # ...and a print before recovery is ours either way. NOTE the 63s offset: a
    # print 1.7s after wave-1's last would be absorbed INTO wave-1 (burst gap
    # 2.0s), not treated as a give-back. Caught by this suite on first run.
    early = base + [_mk(60, 63, 90), _mk(99, 66, 500)]
    chk("a print before recovery is ours under both", ("filled", 60),
        (lambda x: (x["outcome"], x["price"]))(
            fire(WL.triggers(TK, early)[0], early, "no", cancel_on_recovery=True)))

    # Strict recovery bound: a batched ladder sharing one microsecond is all
    # theirs, including its bottom rung.
    batch = base + [_mk(62, 65, 90), _mk(96, 65, 300)]
    chk("cancel variant: a rung batched INTO the recovery instant is not ours",
        "no-offer",
        fire(WL.triggers(TK, batch)[0], batch, "yes", cancel_on_recovery=True)["outcome"])

    # Timeout boundary.
    at60 = base + [_mk(65, 120.3, 40)]     # exactly 60.0s after decision at 60.3
    chk("a print at exactly the 60s deadline still fills", "filled",
        fire(WL.triggers(TK, at60)[0], at60, "yes")["outcome"])
    past = base + [_mk(65, 121.0, 40)]
    chk("a print past the deadline does not", "no-offer",
        fire(WL.triggers(TK, past)[0], past, "yes")["outcome"])

    # EARLIEST, not cheapest.
    two = base + [_mk(68, 63, 10), _mk(30, 65, 99), _mk(99, 70, 500)]
    chk("takes the earliest qualifying print, not the cheaper later one", 68,
        fire(WL.triggers(TK, two)[0], two, "yes")["price"])

    # Above the cap is not a fill.
    dear = base + [_mk(85, 62, 40), _mk(99, 70, 500)]
    chk("a print above the cap is not a fill", "no-offer",
        fire(WL.triggers(TK, dear)[0], dear, "yes")["outcome"])

    # A taker-NO print cannot be our entry (it is somebody selling).
    non = base + [_mk(65, 62, 40, "no"), _mk(99, 70, 500)]
    chk("a taker-NO print cannot be our entry", "no-offer",
        fire(WL.triggers(TK, non)[0], non, "yes")["outcome"])

    # Losing side, and the exit leg is reported without being chosen.
    r = fire(WL.triggers(TK, give)[0], give, "no")
    chk("a losing trade costs the entry plus fee", -67, r["pnl"])
    r = fire(WL.triggers(TK, give)[0], give, "yes")
    chk("no bid printed, so no sell leg is claimed", None, r["sold_pnl"])
    sellable = base + [_mk(65, 65, 40), _mk(99, 70, 500), _mk(98, 80, 60, "no")]
    r = fire(WL.triggers(TK, sellable)[0], sellable, "yes")
    chk("the sell leg is reported when a bid exists", (98, 30),
        (r["exit_price"], r["sold_pnl"]))
    chk("...and holding still beat it, so the frozen rule holds", True,
        r["pnl"] > r["sold_pnl"])

    # "Riskless" stays checkable: worst mark while holding is always reported.
    dip = base + [_mk(65, 65, 40), _mk(25, 66, 10), _mk(99, 70, 500)]
    r = fire(WL.triggers(TK, dip)[0], dip, "yes")
    chk("a winning trade that marked 40c underwater says so", 40, r["underwater_cents"])

    # Classification is total: unsettled markets are recorded, not dropped.
    chk("an unsettled market is classified, not dropped", "unsettled",
        fire(WL.triggers(TK, give)[0], give, "")["outcome"])
    chk("the executor's ev_gate is applied", "ev-gate-refused",
        fire(WL.triggers(TK, give)[0], give, "yes", cap=99)["outcome"])

    # event_trades honours labels and the min_top override (the control arm).
    ev = {TK: give, "KXTEST-26JUL23-BBBB": give}
    chk("event_trades scores only labelled markets", {TK},
        {t["ticker"] for t in event_trades(ev, {TK: "yes"})})
    # `give` sweeps twice (96c, then 99c), so one market yields two triggers.
    # That is the point of not merging: a market that reprices twice fires twice.
    chk("a market that sweeps twice fires twice", 2, len(event_trades(ev, {TK: "yes"})))
    low = [_mk(20, 0, 10), _mk(60, 60, 200), _mk(45, 63, 30)]
    chk("min_top=95 does not fire on a wave topping at 60c", 0,
        len(event_trades({TK: low}, {TK: "no"})))
    chk("min_top=50 (the control) does", 1,
        len(event_trades({TK: low}, {TK: "no"}, min_top=50)))

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(self_test())
