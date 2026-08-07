#!/usr/bin/env python3
"""wave_ladder.py — decompose a repricing into WAVES, with strict causality.

Round 1 asked "what is left after the repricing finishes". This module asks the
question round 1 could not: **what happens between the waves.**

`sweep_segment.episodes()` merges legs (`EPISODE_MERGE_S = 120`) on purpose —
one repricing that arrives as four bursts must fire an arm once, not four times,
or every downstream confidence interval is overstated. That is right for an arm
that trades the *end* of a repricing. It is fatal for an arm that trades the
*middle*, because the merged object's terminal price is a fact about the future.

That is not hypothetical. Scoring the intra-sweep arm with merged-episode
qualification measured 15 wins out of 15. Re-scored so that a wave qualifies on
its own prints alone, the same rule measured 17 of 22. The missing five losers
were markets whose FIRST wave stopped well below 95c and only reached it later —
i.e. markets we would not have fired on at all, admitted to the sample by
information that did not exist yet. The gap between those two numbers is the
entire reason this module exists separately.

STRUCTURAL GUARANTEE
--------------------
A `Wave` holds its own prints and the resting price BEFORE it, and nothing else.
It carries no reference to the tape it came from. `qualifies()` accepts a `Wave`
and no other data. There is therefore no expression a caller can write inside
the trigger decision that reads a later row — the information is not reachable,
rather than merely unused. `self_test` asserts this by inspecting `__slots__`,
so a future field holding the full tape fails the suite rather than quietly
reopening the hole.

Everything after the trigger — fills, cancels, settlement — is future by
definition and lives in `fill_model` / `exit_model` / `score_r2`, which are
allowed to see it because that is what they are modelling.

Read-only. No network.
"""

from __future__ import annotations

import bisect
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sweep_segment import _baseline, normalize  # noqa: E402

# --- Frozen defaults (PRE-REGISTRATION-R2.md) --------------------------------
BURST_GAP_S = 2.0         # same wave while prints arrive closer than this
MIN_TOP = 95              # a wave-1 must REACH the competitor's ladder to count
MIN_JUMP = 15             # ...and must have MOVED the price to get there
MIN_CONTRACTS = 25.0      # size floor separating a ladder from a retail poke
MIN_PRINTS = 3            # a small multi-print walk still qualifies
BASELINE_LOOKBACK_S = 60  # resting price this long before onset


class Wave:
    """One burst of taker-YES prints, sealed off from everything after it.

    `__slots__` is the enforcement mechanism, not a micro-optimisation: it is
    what makes "this object cannot see the future" checkable. Do not add a field
    holding the source tape, the episode, or the settlement.
    """

    __slots__ = ("ticker", "onset", "end", "top", "baseline",
                 "contracts", "n_prints", "prints")

    def __init__(self, ticker, prints, baseline):
        self.ticker = ticker
        self.prints = list(prints)
        self.onset = self.prints[0]["ts"]
        self.end = self.prints[-1]["ts"]
        self.top = max(r["cents"] for r in self.prints)
        self.baseline = baseline
        self.contracts = sum(r["size"] for r in self.prints)
        self.n_prints = len(self.prints)

    @property
    def decision_ts(self):
        """The instant we can act: when this wave's last print lands."""
        return self.end

    @property
    def jump(self):
        return self.top - self.baseline

    @property
    def duration_s(self):
        return (self.end - self.onset).total_seconds()

    def as_dict(self):
        return {"ticker": self.ticker, "onset": self.onset.isoformat(),
                "top_cents": self.top, "baseline_cents": self.baseline,
                "jump_cents": self.jump, "contracts": round(self.contracts, 2),
                "n_prints": self.n_prints, "duration_s": round(self.duration_s, 3)}

    def __repr__(self):
        return (f"<Wave {self.ticker.split('-')[-1]} {self.onset:%H:%M:%S.%f} "
                f"{self.baseline}->{self.top}c x{self.contracts:.0f} n={self.n_prints}>")


def qualifies(wave, min_top=MIN_TOP, min_jump=MIN_JUMP,
              min_contracts=MIN_CONTRACTS, min_prints=MIN_PRINTS):
    """Would we fire on this wave, judged only on what it is?

    Takes a Wave and nothing else. Adding a second data argument to this
    signature is how the lookahead bug would come back; don't.
    """
    return (wave.top >= min_top
            and wave.jump >= min_jump
            and (wave.contracts >= min_contracts or wave.n_prints >= min_prints))


def _baseline_fast(rows, all_ts, onset_ts, lookback_s):
    """Bisect equivalent of `sweep_segment._baseline`. Identical result, O(log n).

    PERFORMANCE, NOT SEMANTICS — and it is not a micro-optimisation. `_baseline`
    rebuilds two filtered lists over the WHOLE tape on every call, and `waves()`
    calls it once per burst, so the cost is O(prints x bursts) per market. On
    TRAIN's tapes that is invisible. On the series probe it is not: profiling one
    World Cup event (31 markets, 16,344 prints) put 5,698 `_baseline` calls at
    83% of total runtime, which extrapolated to ~54 minutes for the probe.

    `rows` is time-ordered, so:
        pre = rows[:bisect_left(ts, onset)]              (ts <  onset)
        old = rows[:bisect_right(ts, onset - lookback)]  (ts <= cutoff)
        answer = old[-1] if old else pre[0], or None if pre is empty
    `sweep_segment._baseline` is left untouched: round 1 still uses it, its tapes
    are small, and its self-tests are frozen. The self-test below asserts the two
    agree on randomised input.
    """
    i = bisect.bisect_left(all_ts, onset_ts)
    if i == 0:                                   # nothing traded before the burst
        return None
    j = bisect.bisect_right(all_ts, onset_ts - lookback_s)
    return rows[j - 1]["cents"] if j > 0 else rows[0]["cents"]


def waves(ticker, raw_trades, burst_gap_s=BURST_GAP_S,
          baseline_lookback_s=BASELINE_LOOKBACK_S):
    """Every taker-YES burst on one market, oldest first. No merging, ever.

    Merging is what round 1 does and what this module exists not to do. A market
    that reprices in four legs yields four waves here; the arm fires on the
    first that qualifies and the rest are the future it is trading against.
    """
    rows = normalize(raw_trades)
    all_ts = [r["ts"].timestamp() for r in rows]      # built once, not per burst
    buys = [r for r in rows if r["taker"] == "yes"]
    out = []
    i = 0
    while i < len(buys):
        j = i
        while (j + 1 < len(buys)
               and (buys[j + 1]["ts"] - buys[j]["ts"]).total_seconds() < burst_gap_s):
            j += 1
        burst = buys[i:j + 1]
        # The baseline is the only thing read outside the burst, and it is
        # strictly EARLIER than the burst — the safe direction.
        base = _baseline_fast(rows, all_ts, burst[0]["ts"].timestamp(),
                              baseline_lookback_s)
        out.append(Wave(ticker, burst, burst[0]["cents"] if base is None else base))
        i = j + 1
    return out


def triggers(ticker, raw_trades, window=None, burst_gap_s=BURST_GAP_S,
             baseline_lookback_s=BASELINE_LOOKBACK_S, **qual):
    """The waves we would actually have fired on, oldest first.

    `window` is an optional (start, end); a wave whose ONSET falls outside is
    dropped rather than rescored. Window bounds come from `windows.py`, which
    refuses rather than inferring a call time — round 1's density heuristic put
    INTC's window at 20:30Z and scored pre-call prints as sweeps.
    """
    out = []
    for w in waves(ticker, raw_trades, burst_gap_s, baseline_lookback_s):
        if window is not None and not (window[0] <= w.onset <= window[1]):
            continue
        if qualifies(w, **qual):
            out.append(w)
    return out


def event_triggers(trades_by_ticker, window=None, **kw):
    """Every trigger across one event's markets, oldest first."""
    out = []
    for ticker, tr in trades_by_ticker.items():
        out.extend(triggers(ticker, tr, window=window, **kw))
    out.sort(key=lambda w: w.onset)
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

    print("wave_ladder self-test:")
    TK = "KXTEST-26JUL23-AAAA"

    # --- THE ANTI-LOOKAHEAD REGRESSION ---------------------------------------
    # This is the fixture from sweep_segment's own self-test: one repricing
    # arriving as three legs, 30 -> 58 -> 82 -> 99. sweep_segment MERGES it into
    # a single episode with terminal 99, so an arm asking "did wave-1 reach 95?"
    # against the merged object answers YES using information from 20s in the
    # future. Wave-1's own top is 58. It must NOT qualify.
    legs = ([_mk(30, 0, 20)]
            + [_mk(55, 60, 100), _mk(58, 60.2, 80)]      # leg 1  top 58
            + [_mk(75, 72, 90), _mk(82, 72.3, 70)]       # leg 2, 12s later
            + [_mk(95, 80, 60), _mk(99, 80.4, 50)])      # leg 3  -> 99
    import sweep_segment as ss
    merged = ss.episodes(TK, legs)
    chk("round-1 merges the three legs into one episode (terminal 99)",
        (1, 99), (len(merged), merged[0].terminal))
    w = waves(TK, legs)
    chk("wave_ladder does NOT merge: one wave per leg", 4, len(w))
    chk("leg-1's own top is 58, not the merged 99", 58, w[1].top)
    chk("REGRESSION: leg 1 must not qualify on future information",
        False, qualifies(w[1]))
    chk("...and the whole tape yields exactly one trigger, the leg that reached 95",
        1, len(triggers(TK, legs)))
    chk("...which is leg 3, not leg 1", 99, triggers(TK, legs)[0].top)

    # Structural guarantee: a Wave cannot reach the tape it came from.
    chk("Wave carries no reference to the source tape", True,
        not ({"rows", "tape", "episode", "trades", "result", "settlement"}
             & set(Wave.__slots__)))
    chk("qualifies() takes exactly one data argument", 1,
        qualifies.__code__.co_argcount - 4)   # 4 threshold kwargs

    # --- positive control ----------------------------------------------------
    hit = [_mk(20, 0, 10), _mk(88, 60, 200), _mk(96, 60.3, 150)]
    t = triggers(TK, hit)
    chk("a wave that reaches 96c on its own prints fires", 1, len(t))
    chk("top is the wave's own high", 96, t[0].top)
    chk("baseline is the resting price before it, not the previous print", 20, t[0].baseline)
    chk("jump measured against the baseline", 76, t[0].jump)
    chk("decision_ts is the wave's LAST print, not its first",
        60.3, (t[0].decision_ts - T0).total_seconds())
    chk("contracts summed across the wave", 350.0, t[0].contracts)

    # --- each threshold rejects on its own -----------------------------------
    chk("a wave topping at 94c does not fire", 0,
        len(triggers(TK, [_mk(20, 0, 10), _mk(94, 60, 200)])))
    chk("a 96c print that did NOT move the price does not fire", 0,
        len(triggers(TK, [_mk(90, 0, 200), _mk(96, 60, 200)])))     # jump 6
    chk("a 1-contract poke to 96c does not fire", 0,
        len(triggers(TK, [_mk(5, 0, 1), _mk(96, 60, 1)])))
    chk("...but a 3-print walk to 96c does, via min_prints", 1,
        len(triggers(TK, [_mk(5, 0, 1), _mk(80, 60, 1), _mk(90, 60.2, 1), _mk(96, 60.4, 1)])))
    chk("taker-NO prints never form a wave", 0,
        len(triggers(TK, [_mk(5, 0, 100, "no"), _mk(96, 60, 500, "no")])))

    # --- burst boundaries ----------------------------------------------------
    # Prints 1.9s apart are one wave; 2.1s apart are two. The boundary is where
    # COMPETITOR-ANATOMY puts wave-3 (~+1.0-1.6s after wave-1).
    chk("prints 1.9s apart are ONE wave", 1,
        len(waves(TK, [_mk(50, 0, 10), _mk(96, 1.9, 200)])))
    chk("prints 2.1s apart are TWO waves", 2,
        len(waves(TK, [_mk(50, 0, 10), _mk(96, 2.1, 200)])))

    # --- window filtering ----------------------------------------------------
    chk("an out-of-window wave is dropped", 0,
        len(triggers(TK, hit, window=(T0 + timedelta(hours=1), T0 + timedelta(hours=2)))))
    chk("an in-window wave is kept", 1,
        len(triggers(TK, hit, window=(T0 - timedelta(minutes=5), T0 + timedelta(minutes=5)))))

    # --- a market that re-sweeps yields several triggers ---------------------
    twice = hit + [_mk(30, 600, 50), _mk(97, 700, 300)]
    chk("a decay-then-resweep fires twice", 2, len(triggers(TK, twice)))

    # --- count_fp, not count (VERIFICATIONS Q-XTAPE) -------------------------
    odd = [{"created_time": "2026-07-23T12:30:00Z", "yes_price_dollars": "0.2000",
            "count": 999, "taker_side": "yes"},
           {"created_time": "2026-07-23T12:31:00Z", "yes_price_dollars": "0.9600",
            "count": 999, "taker_side": "yes"}]
    chk("`count` reads as zero size, so no size-floor pass", 0, len(triggers(TK, odd)))

    # --- event_triggers is ordered by time, across markets -------------------
    ev = {"KXTEST-26JUL23-BBBB": hit,
          "KXTEST-26JUL23-CCCC": [_mk(20, 0, 10), _mk(96, 30, 200)]}
    got = event_triggers(ev)
    chk("event_triggers returns both markets' triggers", 2, len(got))
    chk("...ordered by onset", True, got[0].onset <= got[1].onset)

    # _baseline_fast must agree with sweep_segment._baseline EXACTLY. This is a
    # performance rewrite of a function the arm's entry price depends on, so it
    # is fuzzed rather than spot-checked.
    import random as _r
    from sweep_segment import _baseline as _slow
    _r.seed(20260807)
    mismatch = 0
    for _trial in range(300):
        n = _r.randint(0, 40)
        tape2 = sorted(
            (_mk(_r.randint(1, 99), _r.uniform(0, 400), _r.uniform(0.1, 200),
                 _r.choice(("yes", "no"))) for _ in range(n)),
            key=lambda x: x["created_time"])
        rws = normalize(tape2)
        tsx = [x["ts"].timestamp() for x in rws]
        for _q in range(6):
            on = T0 + timedelta(seconds=_r.uniform(-30, 430))
            for lb in (0, 1, 60, 600):
                if _slow(rws, on, lb) != _baseline_fast(rws, tsx, on.timestamp(), lb):
                    mismatch += 1
    chk("_baseline_fast matches _baseline over 7200 randomised cases",
        0, mismatch)

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(self_test())
