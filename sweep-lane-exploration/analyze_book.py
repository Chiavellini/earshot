#!/usr/bin/env python3
"""analyze_book.py — the measurement that decides the arm.

    python3 analyze_book.py --tape book-tapes/<E>-<stamp>.jsonl
    python3 analyze_book.py --tape <t> --event KXEARNINGSMENTIONXYZ-26AUG12
    python3 analyze_book.py --tape <t> --self-test

THE QUESTION
------------
Every fill in this study is credited because SOMEBODY ELSE took that offer. The
public trades tape shows prints, not resting size, so it cannot distinguish

    "an offer rested at 68c for nine seconds and we would have had it"
        from
    "one offer existed for 300 microseconds and we would have lost the race"

Both look identical: one taker-YES print at 68c. The arm's entire premise is
the first reading (PRE-REGISTRATION-R2.md §R8). This module replays a real book
tape and answers it directly: at each wave-1 trigger, was there resting YES ask
size at or below the cap, how much, and for how long?

§R9 makes it a kill switch: if fewer than half of triggers had resting size at
<=70c, the arm is dead regardless of what the holdout P&L says.

THE SIDE CONVERSION IS THE WHOLE TRICK
--------------------------------------
To BUY YES at 70c we must lift a resting YES ASK at <=70c. Kalshi's wire has no
"yes ask" leg — each side arrives in its own leg and a NO BID at p cents IS a
YES ASK at (100 - p) cents (`src/kalshi/book.rs`, pinned live 2026-07-10).

    a YES ask at <= 70c  ==  a NO bid at >= 30c

Reading `yes_dollars_fp` here — the obvious-looking leg — would measure YES
BIDS, i.e. what we could SELL into, and would answer the wrong question while
looking right. That is the trap this docstring exists to mark.

Sizes are `*_fp` fixed-point strings carrying hundredths; held as integer
CENTI-contracts so deltas never drift (0.77 contracts = 77). Rounding a 0.77
level out of the ladder cost the real best-ask during the 2026-07-15 MS run.

Read-only. Writes only into this lane's out/.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

import data as D            # noqa: E402
import wave_ladder as WL    # noqa: E402
import windows              # noqa: E402


def _cents(s):
    """'0.9900' -> 99. Sub-cent levels are dropped LOUDLY, never rounded."""
    try:
        v = float(s) * 100.0
    except (TypeError, ValueError):
        return None
    r = round(v)
    return r if abs(v - r) < 1e-6 else None


def _centi(s):
    """'3075.67' -> 307567 centi-contracts. Exact, so deltas never drift."""
    try:
        return int(round(float(s) * 100.0))
    except (TypeError, ValueError):
        return None


class Book:
    """YES asks only, in integer cents -> centi-contracts.

    We track one side on purpose. The arm buys YES and nothing else, so the YES
    bid ladder is not evidence about it, and carrying it would invite exactly
    the leg confusion the module docstring warns about.
    """

    __slots__ = ("asks",)

    def __init__(self):
        self.asks = {}

    def _apply(self, side, price_cents, qty, is_delta):
        if side != "no":                      # only the NO leg makes YES asks
            return
        key = 100 - min(price_cents, 100)
        cur = self.asks.get(key, 0)
        new = max(0, cur + qty) if is_delta else max(0, qty)
        if new:
            self.asks[key] = new
        else:
            self.asks.pop(key, None)

    def snapshot(self, msg):
        self.asks.clear()
        for level in (msg.get("no_dollars_fp") or []):
            p, q = _cents(level[0]), _centi(level[1])
            if p is not None and q is not None:
                self._apply("no", p, q, False)

    def delta(self, msg):
        p, q = _cents(msg.get("price_dollars")), _centi(msg.get("delta_fp"))
        if p is not None and q is not None:
            self._apply(msg.get("side"), p, q, True)

    def size_at_or_below(self, cap_cents):
        """Contracts (not centi) offered at or below `cap_cents`."""
        return sum(q for p, q in self.asks.items() if p <= cap_cents) / 100.0

    def best_ask(self):
        return min(self.asks) if self.asks else None


def replay(path):
    """Yield (ts, ticker, Book) after every frame that changes a book.

    Timestamps are our RECEIVE stamp: Kalshi WS frames carry no server time, so
    the tape's only axis is `wall_ns`. The session header records
    `clock_offset_s` against the exchange for exactly this reason; it is
    reported, not silently applied.
    """
    books, session = {}, None
    for raw_line in open(path):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if row.get("t") == "session":
            session = row
            yield ("session", session, None)
            continue
        if row.get("t") == "close":
            yield ("close", row, None)
            continue
        if row.get("t") != "frame":
            continue
        try:
            msg = json.loads(row["raw"])
        except (KeyError, json.JSONDecodeError):
            continue
        kind, m = msg.get("type"), msg.get("msg") or {}
        ticker = m.get("market_ticker")
        if not ticker or kind not in ("orderbook_snapshot", "orderbook_delta"):
            continue
        b = books.setdefault(ticker, Book())
        (b.snapshot if kind == "orderbook_snapshot" else b.delta)(m)
        ts = datetime.fromtimestamp(row["wall_ns"] / 1e9, timezone.utc)
        yield (ts, ticker, b)


def liftable_history(path, ticker, cap_cents):
    """[(ts, contracts_offered_at_or_below_cap)] for one market, in order."""
    out = []
    for ts, tk, b in replay(path):
        if tk != ticker or not isinstance(ts, datetime):
            continue
        out.append((ts, b.size_at_or_below(cap_cents)))
    return out


def at_triggers(path, event, cap_cents=70, window_s=60):
    """For each wave-1 trigger: was there resting size at <= cap, and for how long?

    Returns one row per trigger. `seconds_available` is dwell time — the sum of
    intervals during the window where liftable size was non-zero. That is the
    number that separates "we could have had it" from "we would have lost the
    race": 9 seconds of resting size is a limit order; 300 microseconds is a
    sprint we lose by 534 ms.
    """
    run_dir = D.event_dirs().get(event, event)
    tape, labels = D.load_tape(event, run_dir), D.load_labels(event, run_dir)
    if not tape:
        return None, f"no trade tape for {event}"
    start, end, why = windows.window(event)
    win = None if start is None else (start, end)

    hist = {}
    for ts, tk, b in replay(path):
        if not isinstance(ts, datetime):
            continue
        hist.setdefault(tk, []).append((ts, b.size_at_or_below(cap_cents)))

    rows = []
    for ticker, raw in sorted(tape.items()):
        for w in WL.triggers(ticker, raw, window=win):
            h = hist.get(ticker, [])
            t0, t1 = w.decision_ts, w.decision_ts + timedelta(seconds=window_s)
            seg = [(t, s) for t, s in h if t0 <= t <= t1]
            dwell, peak = 0.0, 0.0
            for i, (t, s) in enumerate(seg):
                peak = max(peak, s)
                if s > 0:
                    nxt = seg[i + 1][0] if i + 1 < len(seg) else t1
                    dwell += (nxt - t).total_seconds()
            rows.append({
                "ticker": ticker, "result": labels.get(ticker),
                "wave_top": w.top, "decision_ts": w.decision_ts.isoformat(),
                "book_updates_in_window": len(seg),
                "peak_liftable_contracts": round(peak, 2),
                "seconds_available": round(dwell, 3),
                "had_size": peak > 0,
                "covered": bool(seg),      # did the tape actually cover this instant
            })
    return rows, None


def report(path, event, cap_cents=70, window_s=60):
    sess = close = None
    for ts, row, _ in replay(path):
        if ts == "session":
            sess = row
        elif ts == "close":
            close = row
    print(f"BOOK TAPE  {os.path.basename(path)}")
    if sess:
        print(f"  markets {len(sess.get('tickers') or [])}   schema {sess.get('schema_version')}"
              f"   clock_offset_s {sess.get('clock_offset_s')}   host {sess.get('host')}")
    if close:
        d = close.get("dropped") or 0
        print(f"  frames {close.get('rows')}   dropped {d}   reason {close.get('reason')}"
              + ("   ⚠ INCOMPLETE" if d else ""))
    else:
        print("  ⚠ NO CLOSE ROW — tape is truncated; do not read absence of size "
              "as absence of offers")

    if not event:
        print("\n  (no --event given; pass one to score triggers against it)")
        return 0

    rows, err = at_triggers(path, event, cap_cents, window_s)
    if err:
        print(f"\n  {err}")
        return 1
    if not rows:
        print(f"\n  no wave-1 triggers in {event}")
        return 0

    cov = [r for r in rows if r["covered"]]
    had = [r for r in cov if r["had_size"]]
    print(f"\nAT THE TRIGGER INSTANT — resting YES ask size at <= {cap_cents}c "
          f"within {window_s}s\n")
    print(f"  {'market':<34}{'w1':>4}{'upd':>6}{'peak ct':>9}{'sec avail':>11}  settled")
    for r in sorted(rows, key=lambda x: -x["peak_liftable_contracts"]):
        print(f"  {r['ticker'].replace('KXEARNINGSMENTION',''):<34}{r['wave_top']:>4}"
              f"{r['book_updates_in_window']:>6}{r['peak_liftable_contracts']:>9.1f}"
              f"{r['seconds_available']:>11.2f}  {r['result'] or '—'}"
              + ("" if r["covered"] else "   (NOT COVERED BY TAPE)"))

    print(f"\n  triggers covered by the tape : {len(cov)}/{len(rows)}")
    if cov:
        frac = len(had) / len(cov)
        print(f"  ...with resting size <= {cap_cents}c : {len(had)}/{len(cov)} = {frac:.0%}")
        if had:
            dw = sorted(r["seconds_available"] for r in had)
            print(f"  seconds available            : median {dw[len(dw)//2]:.2f}, "
                  f"min {dw[0]:.2f}, max {dw[-1]:.2f}")
        print(f"\n  §R9 KILL SWITCH: abandon if this is under 50%.  -> "
              f"{'ABANDON' if frac < 0.5 else 'survives'}")
    print("\n  Dwell time is the load-bearing number. Seconds of resting size means a")
    print("  limit order gets it; microseconds means a race we lose by 534 ms.")
    return 0


# --- self-test ---------------------------------------------------------------

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

    print("analyze_book self-test:")

    chk("dollars parse to integer cents", 99, _cents("0.9900"))
    chk("a sub-cent level is dropped, not rounded", None, _cents("0.9950"))
    chk("fp size parses to centi-contracts", 307567, _centi("3075.67"))
    chk("a fractional 0.77 contract survives as 77 centi", 77, _centi("0.77"))

    b = Book()
    # THE SIDE CONVERSION: a NO bid at 30c IS a YES ask at 70c.
    b.snapshot({"no_dollars_fp": [["0.3000", "500.00"], ["0.2500", "100.00"]],
                "yes_dollars_fp": [["0.6000", "999.00"]]})
    chk("a NO bid at 30c becomes a YES ask at 70c", {70: 50000, 75: 10000}, b.asks)
    chk("the YES leg is ignored — it is bids, not asks", False, 40 in b.asks)
    chk("liftable at <=70c counts only the 70c level", 500.0, b.size_at_or_below(70))
    chk("liftable at <=75c counts both", 600.0, b.size_at_or_below(75))
    chk("liftable at <=69c is nothing", 0.0, b.size_at_or_below(69))
    chk("best ask is the lowest YES ask", 70, b.best_ask())

    # Deltas accumulate exactly and remove a level at zero.
    b.delta({"price_dollars": "0.3000", "delta_fp": "-200.00", "side": "no"})
    chk("a negative delta reduces the level", 300.0, b.size_at_or_below(70))
    b.delta({"price_dollars": "0.3000", "delta_fp": "-300.00", "side": "no"})
    chk("a level driven to zero is removed", False, 70 in b.asks)
    b.delta({"price_dollars": "0.3000", "delta_fp": "-999.00", "side": "no"})
    chk("a delta below zero clamps, never goes negative", 0.0, b.size_at_or_below(70))
    b.delta({"price_dollars": "0.4000", "delta_fp": "0.77", "side": "no"})
    chk("a 0.77-contract level is NOT rounded away", 0.77, b.size_at_or_below(60))
    # The 75c ask from the opening snapshot is still resting here; assert on the
    # KEY SET, not on a total, or the check silently tests the wrong thing.
    before = dict(b.asks)
    b.delta({"price_dollars": "0.9000", "delta_fp": "500.00", "side": "yes"})
    chk("a YES-leg delta creates no level at all", before, b.asks)
    chk("...in particular none at 10c or 90c", False,
        10 in b.asks or 90 in b.asks)

    # A snapshot replaces the ladder rather than merging into it.
    b.snapshot({"no_dollars_fp": [["0.5000", "10.00"]]})
    chk("a snapshot replaces the ladder", {50: 1000}, b.asks)

    # Round-trip a tiny tape through replay(), including a truncated one.
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write(json.dumps({"t": "session", "tickers": ["T"], "schema_version": 1}) + "\n")
        fh.write(json.dumps({"t": "frame", "wall_ns": 1785786002101145000, "raw": json.dumps(
            {"type": "orderbook_snapshot", "msg": {"market_ticker": "T",
             "no_dollars_fp": [["0.3500", "250.00"]]}})}) + "\n")
        fh.write(json.dumps({"t": "frame", "wall_ns": 1785786003101145000, "raw": "{not json"}) + "\n")
        fh.write(json.dumps({"t": "close", "reason": "deadline", "rows": 2, "dropped": 0}) + "\n")
        tmp = fh.name
    got = list(replay(tmp))
    chk("replay yields session, one book update, and close", 3, len(got))
    chk("a malformed frame is skipped, not fatal", "close", got[-1][0])
    chk("the replayed book has the converted YES ask", 250.0,
        got[1][2].size_at_or_below(65))
    hist = liftable_history(tmp, "T", 65)
    chk("liftable_history returns one point for one update", 1, len(hist))
    os.unlink(tmp)

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def main(argv):
    if "--self-test" in argv:
        return self_test()
    if "--tape" not in argv:
        print(__doc__.strip().split("\n\n")[1])
        return 2
    tape = argv[argv.index("--tape") + 1]
    event = argv[argv.index("--event") + 1] if "--event" in argv else None
    cap = int(argv[argv.index("--cap") + 1]) if "--cap" in argv else 70
    return report(tape, event, cap)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
