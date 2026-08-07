#!/usr/bin/env python3
"""sweep_segment.py — segment a Kalshi trade tape into sweep EPISODES.

Why this exists rather than reusing `kalshi_sweep.sweep_onset()` directly:
that function answers "when did this strike durably reprice toward $1", which
is the right question for pack-lag. It returns ONE onset per market and its
defaults (`sweep_price=0.90`, `sweep_hold=0.80`) require the sweep to REACH
90c. This study's entire signal lives in the sweeps that *stall* at 50-89c and
never reach 90 — those would be invisible to it. We also need every episode,
not the first, because markets sweep more than once.

So: same tape, different primitive. `kalshi_sweep.trades()` is still the
fetcher (imported, not copied).

A SWEEP EPISODE is a burst of taker-YES prints, and is defined by intrinsic
properties only — price, size, timing — never by a hand-drawn time window:

  burst      consecutive taker-YES prints with inter-print gap < burst_gap_s
  terminal   the burst's highest print price (where the sweep STOPPED)
  entry      the last print before the burst (its own first print if none)
  jump       terminal - entry
  qualifies  terminal >= min_terminal AND jump >= min_jump
             AND (contracts >= min_contracts OR n_prints >= min_prints)

The size floor is doing real work: it is what separates a competitor's
aggressive ladder from a 1-contract retail poke. Measured on AAL/LMT 26JUL23,
in-call qualifying bursts carry 18-914 contracts while out-of-window ones are
overwhelmingly 1-22. See PRE-REGISTRATION.md §S2 for the sensitivity grid.

Read-only. No network except through kalshi_sweep.trades().
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "pm-expansion-exploration", "latency", "lib"))
import kalshi_sweep  # noqa: E402  (exploration-tier, imported not copied)

# --- Pre-registered defaults (PRE-REGISTRATION.md S2) ------------------------
BURST_GAP_S = 2.0        # COMPETITOR-ANATOMY: wave-2 lands +120-280ms, wave-3 ~+1.0-1.6s
MIN_TERMINAL = 50        # cents; below this the market never made a directional claim
MIN_JUMP = 15            # cents; a sweep must MOVE the price, not just print at a level
MIN_CONTRACTS = 25.0     # size floor separating a ladder from a retail poke
MIN_PRINTS = 3           # a multi-print walk qualifies even if small
BASKET_WINDOW_S = 2.0    # strikes sweeping this close together = one release-text basket
BASELINE_LOOKBACK_S = 60  # entry = the resting price this long before onset, not the last print
EPISODE_MERGE_S = 120    # legs of one repricing this close together are ONE episode
PREPRICED_CENTS = 50     # resting at/above this before the call = already-committed market


def _baseline(rows, onset, lookback_s):
    """The price the market was RESTING at before a burst.

    Not the immediately preceding print: a sustained repricing arrives in legs
    (AAL BASI walks 52->99c over 15s in four bursts), and measuring each leg's
    jump against the previous leg makes every leg look like a small move. That
    is what made the first version of this primitive reject real sweeps.

    Degrades rather than failing closed: if nothing traded `lookback_s` before
    the burst, fall back to the nearest earlier print. Returning None there
    would force entry = the burst's own first price, making jump 0 and
    silently rejecting every market whose first real trading IS the sweep.
    """
    pre = [r for r in rows if r["ts"] < onset]
    if not pre:
        return None
    old = [r for r in pre if r["ts"] <= onset - timedelta(seconds=lookback_s)]
    return (old[-1] if old else pre[0])["cents"]


class Episode:
    __slots__ = ("ticker", "onset", "end", "entry", "terminal", "contracts",
                 "n_prints", "duration_s", "basket_n", "prints")

    def __init__(self, ticker, onset, end, entry, terminal, contracts, n_prints, prints):
        self.ticker = ticker
        self.onset = onset
        self.end = end
        self.entry = entry
        self.terminal = terminal
        self.contracts = contracts
        self.n_prints = n_prints
        self.duration_s = (end - onset).total_seconds()
        self.basket_n = 1          # filled in by mark_baskets()
        self.prints = prints

    @property
    def jump(self):
        return self.terminal - self.entry

    def as_dict(self):
        return {
            "ticker": self.ticker,
            "onset": self.onset.isoformat(),
            "terminal_cents": self.terminal,
            "entry_cents": self.entry,
            "jump_cents": self.jump,
            "contracts": round(self.contracts, 2),
            "n_prints": self.n_prints,
            "duration_s": round(self.duration_s, 3),
            "basket_n": self.basket_n,
        }

    def __repr__(self):
        return (f"<Episode {self.ticker.split('-')[-1]} {self.onset:%H:%M:%S.%f}"
                f" {self.entry}->{self.terminal}c x{self.contracts:.0f} n={self.n_prints}"
                f" basket={self.basket_n}>")


def normalize(raw_trades):
    """Banked-tape rows or live `kalshi_sweep.trades()` rows -> a uniform list.

    Both carry the same wire fields; banked rows just lack the `_ts`/`_yes`
    derived keys that trades() adds. Sizes are `count_fp` (fractional) — the
    plain `count` key is absent and reading it yields a silent zero notional
    (VERIFICATIONS Q-XTAPE).
    """
    out = []
    for t in raw_trades:
        ts = t.get("_ts")
        if ts is None:
            ts = datetime.fromisoformat(t["created_time"].replace("Z", "+00:00"))
        yes = t.get("_yes")
        if yes is None:
            yes = float(t["yes_price_dollars"])
        out.append({
            "ts": ts,
            "cents": round(yes * 100),
            "size": float(t.get("count_fp", 0.0)),
            "taker": t.get("taker_side", ""),
        })
    out.sort(key=lambda r: r["ts"])
    return out


def resting_price(raw_trades, at, lookback_s=BASELINE_LOOKBACK_S):
    """What price was this market resting at, `at` a given time? None if untraded.

    This is the LEVEL signal, independent of any sweep. It matters because the
    acceptance test showed that the markets the repo calls wrong-way "basket
    sweeps" (AAL WORL/CENT/IRAN, INTC ARIZ/DESK/GOVE/MOBI) were in fact already
    priced 50-75c BEFORE the call and never repriced during it. They are not
    sweeps at all; they are pre-committed levels.
    """
    rows = normalize(raw_trades)
    return _baseline(rows, at + timedelta(seconds=lookback_s), lookback_s)


def episodes(ticker, raw_trades, window=None, burst_gap_s=BURST_GAP_S,
             min_terminal=MIN_TERMINAL, min_jump=MIN_JUMP,
             min_contracts=MIN_CONTRACTS, min_prints=MIN_PRINTS,
             baseline_lookback_s=BASELINE_LOOKBACK_S,
             merge_s=EPISODE_MERGE_S, first_only=False, legacy_entry=False):
    """All qualifying sweep episodes on one market, oldest first.

    `window` is an optional (start, end) pair of datetimes. Episodes whose
    ONSET falls outside it are dropped. Pass None to run window-free.
    `first_only=True` and `legacy_entry=True` together reproduce the
    provisional pre-study behaviour for the replication gate.
    """
    rows = normalize(raw_trades)
    buys = [r for r in rows if r["taker"] == "yes"]
    if not buys:
        return []

    out = []
    i = 0
    while i < len(buys):
        j = i
        while j + 1 < len(buys) and (buys[j + 1]["ts"] - buys[j]["ts"]).total_seconds() < burst_gap_s:
            j += 1
        burst = buys[i:j + 1]
        terminal = max(r["cents"] for r in burst)
        if legacy_entry:
            entry = buys[i - 1]["cents"] if i > 0 else burst[0]["cents"]
        else:
            entry = _baseline(rows, burst[0]["ts"], baseline_lookback_s)
            if entry is None:
                entry = burst[0]["cents"]
        contracts = sum(r["size"] for r in burst)
        big = contracts >= min_contracts or len(burst) >= min_prints
        if terminal >= min_terminal and (terminal - entry) >= min_jump and big:
            ep = Episode(ticker, burst[0]["ts"], burst[-1]["ts"], entry, terminal,
                         contracts, len(burst), burst)
            if window is None or (window[0] <= ep.onset <= window[1]):
                out.append(ep)
                if first_only:
                    break
        i = j + 1

    return out if (first_only or merge_s is None) else merge_legs(out, merge_s)


def merge_legs(eps, merge_s=EPISODE_MERGE_S):
    """Collapse legs of ONE repricing into ONE episode.

    Statistical necessity, not cosmetics: AAL BASI's single 52->99c repricing
    arrives as four bursts. Left split, the arm fires four times on one event,
    inflating n with four perfectly correlated observations and overstating
    every confidence interval downstream.
    """
    if not eps:
        return []
    out = [eps[0]]
    for e in eps[1:]:
        prev = out[-1]
        if (e.onset - prev.end).total_seconds() <= merge_s:
            prev.end = max(prev.end, e.end)
            prev.terminal = max(prev.terminal, e.terminal)
            prev.contracts += e.contracts
            prev.n_prints += e.n_prints
            prev.duration_s = (prev.end - prev.onset).total_seconds()
            prev.prints = prev.prints + e.prints
        else:
            out.append(e)
    return out


def mark_baskets(all_eps, basket_window_s=BASKET_WINDOW_S):
    """Annotate each episode with how many DISTINCT strikes swept alongside it.

    COMPETITOR-ANATOMY attributes the wrong-way AAL (WORL/CENT/IRAN) and INTC
    (ARIZ/DESK/GOVE/MOBI) sweeps to release-text-driven baskets fired in one
    instant, as opposed to heard mentions which arrive alone. Simultaneity is
    therefore a candidate discriminator, and this is the feature that tests it.
    """
    eps = sorted(all_eps, key=lambda e: e.onset)
    for e in eps:
        lo = e.onset - timedelta(seconds=basket_window_s)
        hi = e.onset + timedelta(seconds=basket_window_s)
        e.basket_n = len({o.ticker for o in eps if lo <= o.onset <= hi})
    return eps


def event_episodes(trades_by_ticker, window=None, **kw):
    """All episodes across every market in one event, basket-annotated."""
    out = []
    for ticker, tr in trades_by_ticker.items():
        out.extend(episodes(ticker, tr, window=window, **kw))
    return mark_baskets(out)


# --- self-test ---------------------------------------------------------------

def _mk(cents, off_s, size, taker="yes", t0=None):
    t0 = t0 or datetime(2026, 7, 23, 12, 30, tzinfo=timezone.utc)
    return {"created_time": (t0 + timedelta(seconds=off_s)).isoformat().replace("+00:00", "Z"),
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

    print("sweep_segment self-test:")
    T = "KXTEST-26JUL23-AAAA"

    # A clean ladder: cheap, then a burst walking 20 -> 85.
    tape = [_mk(20, 0, 5), _mk(60, 10, 50), _mk(75, 10.3, 60), _mk(85, 10.6, 40)]
    eps = episodes(T, tape)
    chk("finds one episode", 1, len(eps))
    chk("terminal is where the sweep STOPPED", 85, eps[0].terminal)
    chk("entry is the print before the burst", 20, eps[0].entry)
    chk("contracts summed across the burst", 150.0, eps[0].contracts)
    chk("onset is the burst's first print", 10.0, (eps[0].onset - eps[0].onset.replace(
        hour=12, minute=30, second=0, microsecond=0)).total_seconds())

    # A continuation that only nudges the price is NOT a second episode:
    # 85 -> 97 is a 12c jump, below min_jump. This is deliberate.
    chk("a 12c continuation is not a second episode",
        1, len(episodes(T, tape + [_mk(88, 100, 40), _mk(97, 100.4, 80)])))

    # A real re-sweep is: the first sweep decays away, then the market is
    # swept again. That is the "markets sweep more than once" case, and it is
    # what the provisional one-episode-per-market pass was missing.
    tape2 = tape + [_mk(35, 200, 30), _mk(90, 300, 200), _mk(95, 300.4, 80)]
    chk("a decay-then-resweep IS a second episode", 2, len(episodes(T, tape2)))
    chk("second episode's terminal is its own", 95, episodes(T, tape2)[1].terminal)
    chk("first_only reproduces the one-episode provisional behaviour",
        1, len(episodes(T, tape2, first_only=True)))

    # The size floor: a 1-contract poke to 90c is not a sweep.
    chk("1-contract poke is rejected", 0, len(episodes(T, [_mk(5, 0, 1), _mk(90, 10, 1)])))
    # ...but a multi-print small walk still qualifies via min_prints.
    chk("small multi-print walk qualifies",
        1, len(episodes(T, [_mk(5, 0, 1), _mk(60, 10, 1), _mk(70, 10.2, 1), _mk(80, 10.4, 1)])))

    # A print at a high level that did not MOVE the price is not a sweep.
    chk("no jump -> not a sweep", 0, len(episodes(T, [_mk(88, 0, 500), _mk(90, 10, 500)])))
    # A big move that stops below min_terminal is not a sweep.
    chk("terminal below floor -> not a sweep", 0, len(episodes(T, [_mk(2, 0, 500), _mk(40, 10, 500)])))

    # Taker-NO prints must never form a sweep (they are the FADE side).
    chk("taker-NO prints are not sweeps",
        0, len(episodes(T, [_mk(5, 0, 100, "no"), _mk(90, 10, 500, "no")])))

    # The stalled sweep that kalshi_sweep.sweep_onset() cannot see, and that
    # carries this study's entire signal.
    stall = [_mk(20, 0, 10), _mk(65, 10, 200)]
    chk("a sweep that STALLS at 65c is found", 1, len(episodes(T, stall)))
    chk("...with terminal 65, not promoted to 90", 65, episodes(T, stall)[0].terminal)

    # Basket annotation: three strikes firing in the same instant.
    ev = {f"KXTEST-26JUL23-{s}": [_mk(10, 0, 10), _mk(70, 30 + k * 0.3, 200)]
          for k, s in enumerate(("WORL", "CENT", "IRAN"))}
    ev["KXTEST-26JUL23-LONE"] = [_mk(10, 0, 10), _mk(70, 900, 200)]
    got = event_episodes(ev)
    by = {e.ticker.split("-")[-1]: e.basket_n for e in got}
    chk("simultaneous strikes are marked as a basket of 3", 3, by["WORL"])
    chk("an isolated sweep is a basket of 1", 1, by["LONE"])

    # Window filtering drops out-of-window onsets rather than rescoring them.
    t0 = datetime(2026, 7, 23, 12, 30, tzinfo=timezone.utc)
    chk("window excludes an out-of-window episode",
        0, len(episodes(T, tape, window=(t0 + timedelta(hours=1), t0 + timedelta(hours=2)))))
    chk("window keeps an in-window episode",
        1, len(episodes(T, tape, window=(t0 - timedelta(minutes=5), t0 + timedelta(minutes=5)))))

    # count_fp, not count: a row with only `count` must read as zero size, so
    # such a burst survives only via min_prints, never via the size floor.
    odd = [{"created_time": "2026-07-23T12:30:00Z", "yes_price_dollars": "0.2000",
            "count": 999, "taker_side": "yes"},
           {"created_time": "2026-07-23T12:30:10Z", "yes_price_dollars": "0.8000",
            "count": 999, "taker_side": "yes"}]
    chk("count (not count_fp) reads as zero size, so no size-floor pass",
        0, len(episodes(T, odd)))

    # Leg-splitting: one repricing arriving as several bursts must be ONE
    # episode, or the arm fires several times on one correlated event.
    legs = ([_mk(30, 0, 20)]
            + [_mk(55, 60, 100), _mk(58, 60.2, 80)]      # leg 1
            + [_mk(75, 72, 90), _mk(82, 72.3, 70)]       # leg 2, 12s later
            + [_mk(95, 80, 60), _mk(99, 80.4, 50)])      # leg 3
    merged = episodes(T, legs)
    chk("three legs of one repricing merge into ONE episode", 1, len(merged))
    chk("merged terminal is the highest leg", 99, merged[0].terminal)
    chk("merged onset is the FIRST leg's onset", 60.0,
        (merged[0].onset - datetime(2026, 7, 23, 12, 30, tzinfo=timezone.utc)).total_seconds())
    chk("merged contracts sum across all legs", 450.0, merged[0].contracts)
    chk("merge_s=None leaves the legs split", 3, len(episodes(T, legs, merge_s=None)))

    # Baseline entry vs legacy entry: the flaw the acceptance test exposed.
    # Leg 2 measured against leg 1's last print is a 17c move; measured
    # against the resting price 60s earlier it is a 52c move.
    split = episodes(T, legs, merge_s=None)
    chk("baseline entry uses the resting price, not the previous leg",
        30, split[1].entry)
    legacy = episodes(T, legs, merge_s=None, legacy_entry=True)
    chk("legacy entry uses the previous print (reproduces the old behaviour)",
        58, legacy[1].entry)

    # resting_price: the LEVEL signal, for markets that never sweep at all.
    prepriced = [_mk(66, -600, 50), _mk(67, -300, 30), _mk(68, 200, 40)]
    t0 = datetime(2026, 7, 23, 12, 30, tzinfo=timezone.utc)
    chk("resting_price reads the pre-call level", 67, resting_price(prepriced, t0))
    chk("resting_price is None for an untraded market", None, resting_price([], t0))

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(self_test())
