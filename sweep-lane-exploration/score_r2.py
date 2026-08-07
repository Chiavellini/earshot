#!/usr/bin/env python3
"""score_r2.py — score the intra-sweep arm, per PRE-REGISTRATION-R2.md.

    python3 score_r2.py                          # TRAIN (contaminated, cannot confirm)
    python3 score_r2.py --replicate              # attribute the pre-freeze number
    python3 score_r2.py --sensitivity            # S1-S8
    python3 score_r2.py --split holdout          # REFUSED until the close condition
    python3 score_r2.py --split holdout --close-the-holdout
    python3 score_r2.py --json out/r2-train.json

Read-only. Writes only into this lane's out/.
"""

from __future__ import annotations

import json
import os
import random
import sys
from collections import defaultdict
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

import data as D            # noqa: E402
import fill_model as FM     # noqa: E402
import intra_arm as IA      # noqa: E402
import windows              # noqa: E402

OUT = os.path.join(HERE, "out")

# --- Split boundaries (PRE-REGISTRATION-R2.md §R3/§R4) -----------------------
TRAIN_THROUGH = date(2026, 8, 6)    # everything banked and inspected: contaminated
HOLDOUT_FROM = date(2026, 8, 8)     # the sacred forward set
# 2026-08-07 falls in neither. Those events were mid-flight when TRAIN was
# inspected — their tapes existed but were unlabelled, so they are neither
# safely contaminated nor safely clean. Excluded from both, and reported.

CLOSE_EVENTS = 36                   # derived in §R4 from delta=21c, sigma=45c
CLOSE_DATE = date(2026, 10, 30)

_MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}

_TAPES = {}
# The cache is a clear win for TRAIN (39 events, re-scored dozens of times by the
# sensitivity grid). It is a liability for the series probe: 798 events / 2 GB of
# JSON parses to several GB of Python objects, all held at once, for a single
# pass that never revisits an event. score_probe.py turns it off.
CACHE = True


def event_date(event):
    """`...-26AUG04` -> date(2026, 8, 4). None if the ticker carries no date."""
    tok = D.event_day(event)
    try:
        return date(2000 + int(tok[:2]), _MONTHS[tok[2:5]], int(tok[5:7]))
    except (ValueError, KeyError, IndexError):
        return None


def split_of(event):
    d = event_date(event)
    if d is None:
        return "undated"
    if d <= TRAIN_THROUGH:
        return "train"
    if d >= HOLDOUT_FROM:
        return "holdout"
    return "gap"


def _load(event, run_dir):
    if not CACHE:
        return D.load_tape(event, run_dir), D.load_labels(event, run_dir)
    if event not in _TAPES:
        _TAPES[event] = (D.load_tape(event, run_dir), D.load_labels(event, run_dir))
    return _TAPES[event]


def collect(split="train", window_free=False, events=None, **arm_kw):
    """Every trade the arm makes on one split. Returns (trades, skipped).

    `events` overrides the split entirely with an explicit event-ticker list.
    It exists for the series probe (PRE-REGISTRATION-R2-PROBE.md), whose events
    have no `state/` run dir — `D.event_dirs()` globs `state/` only. No loader
    change was needed: `data.load_tape()` / `load_labels()` already look in
    `tapes/<EVENT>/` FIRST and fall back to `state/`. Passing `events` also
    bypasses `split_of()`, because the caller has chosen the set deliberately
    and a date filter would silently re-cut it.
    """
    trades, skipped = [], []
    universe = (sorted((e, e) for e in events) if events is not None
                else sorted(D.event_dirs().items()))
    for event, run_dir in universe:
        if events is None and split != "all" and split_of(event) != split:
            continue
        tape, labels = _load(event, run_dir)
        if not tape:
            skipped.append((event, "no-tape"))
            continue
        if not labels:
            skipped.append((event, "no-labels"))
            continue
        start, end, why = windows.window(event)
        if start is None and not window_free:
            skipped.append((event, why))
            continue
        win = None if window_free else (start, end)
        for t in IA.event_trades(tape, labels, window=win, **arm_kw):
            t.update(event=event, day=D.event_day(event))
            trades.append(t)
    return trades, skipped


# --- statistics ---------------------------------------------------------------

def wilson(k, n, z=1.96):
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return ((c - m) / d, (c + m) / d)


def bootstrap_ci(filled, n=10_000, seed=20260807):
    """95% CI on mean P&L, resampled BY EVENT — trades within an event correlate."""
    if not filled:
        return (None, None)
    by_event = defaultdict(list)
    for t in filled:
        by_event[t["event"]].append(t["pnl"])
    groups = list(by_event.values())
    if len(groups) < 2:
        return (None, None)
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        pool = []
        for _ in range(len(groups)):
            pool.extend(groups[rng.randrange(len(groups))])
        if pool:
            means.append(sum(pool) / len(pool))
    means.sort()
    return (means[int(0.025 * len(means))], means[int(0.975 * len(means))])


def summarise(trades):
    filled = [t for t in trades if t["outcome"] == "filled"]
    s = {"triggered": len(trades), "filled": len(filled),
         "no_offer": sum(1 for t in trades if t["outcome"] == "no-offer"),
         "ev_refused": sum(1 for t in trades if t["outcome"] == "ev-gate-refused"),
         "unsettled": sum(1 for t in trades if t["outcome"] == "unsettled"),
         "events": len({t["event"] for t in filled}),
         "days": len({t["day"] for t in filled}),
         "wins": sum(1 for t in filled if t["result"] == "yes"),
         "per_trade": (sum(t["pnl"] for t in filled) / len(filled)) if filled else None,
         "avg_price": (sum(t["price"] for t in filled) / len(filled)) if filled else None,
         "total_cents": sum(t["pnl"] for t in filled)}
    s["p_yes"] = (s["wins"] / len(filled)) if filled else None
    s["wilson_lo"], s["wilson_hi"] = wilson(s["wins"], len(filled))
    s["ci_lo"], s["ci_hi"] = bootstrap_ci(filled)
    return s


def verdict(s):
    """PRE-REGISTRATION-R2.md §R5. Applies only to HOLDOUT."""
    if s["filled"] == 0:
        return "NO-DATA"
    if s["ci_hi"] is not None and s["ci_hi"] < 0 and s["filled"] >= 20:
        return "REFUTED"
    if (s["per_trade"] is not None and s["per_trade"] >= 5
            and s["ci_lo"] is not None and s["ci_lo"] > 0
            and s["filled"] >= 20 and s["events"] >= 30 and s["days"] >= 10):
        return "PROMOTABLE (pending the multiplicity + null conditions)"
    return "NOT-RESOLVED"


def line(label, s):
    """One row. `buy` and `need` are printed together on purpose: an arm buying
    at 54c needs P(YES) > 0.54, so a Wilson bound is only readable next to the
    price it has to beat."""
    pt = f"{s['per_trade']:+.1f}c" if s["per_trade"] is not None else "—"
    ci = f"[{s['ci_lo']:+.1f},{s['ci_hi']:+.1f}]" if s["ci_lo"] is not None else "n/a"
    py = f"{s['wins']}/{s['filled']}" if s["filled"] else "0/0"
    wl = f"[{s['wilson_lo']:.2f},{s['wilson_hi']:.2f}]" if s["wilson_lo"] is not None else "n/a"
    bp = f"{s['avg_price']:.0f}c" if s["avg_price"] is not None else "—"
    clears = ""
    if s["wilson_lo"] is not None and s["avg_price"] is not None:
        clears = "yes" if s["wilson_lo"] > s["avg_price"] / 100.0 else "NO"
    # Widths sized for the series probe, where the control reaches 7,713 fills
    # out of 29,247 triggers. At the round-2 widths those two ran together as
    # "77133260/7713" — a printed number that is not any number.
    print(f"  {label:<40}{s['triggered']:>7}{s['filled']:>7}{py:>11}{wl:>14}{bp:>6}"
          f"{clears:>7}{pt:>9}{ci:>17}{s['events']:>6}{s['days']:>5}")


HDR = (f"  {'variant':<40}{'trig':>7}{'fill':>7}{'yes':>11}{'Wilson P(YES)':>14}"
       f"{'buy':>6}{'clears':>7}{'ev/trade':>9}{'95% CI':>17}{'evt':>6}{'day':>5}")


# --- reports ------------------------------------------------------------------

def replication(split):
    """Attribute the exploratory number, and show what amendment A1 restored."""
    print("\nREPLICATION — the exploratory pass measured +21.0c, 17/22, 10 events\n")
    print(HDR)
    for label, kw in (
        ("exploratory pass = FROZEN RULE (A1)", {}),
        ("superseded: + cancel on recovery", dict(cancel_on_recovery=True)),
    ):
        t, _ = collect(split, **kw)
        line(label, summarise(t))
    print("\n  Row 1 is the rule as first frozen. Amendment A1 dropped its cancel")
    print("  logic after TRAIN falsified the mechanism: the 10 trades the cancel")
    print("  removes settle 8/10 YES at +21.8c, i.e. it discards winners, not")
    print("  stale entries. Row 0 is therefore both the frozen rule AND the exact")
    print("  number that motivated the study — the replication gate is that these")
    print("  two facts coincide.")


def control(split):
    print("\nCONTROL — the same machinery with no wave-1 filter (round 1's population)\n")
    print(HDR)
    for label, mt in (("frozen: wave-1 >= 95c", 95), ("control: wave-1 >= 50c", 50)):
        t, _ = collect(split, min_top=mt)
        line(label, summarise(t))
    print("\n  The control MUST stay negative. If the filter stops separating these")
    print("  two populations, the primitive has broken and nothing else counts.")


def sensitivity(split):
    print("\nS1  THE TWO TUNED CONSTANTS — sign must hold across both ranges (§R5)\n")
    print(f"  {'wave1':>6}{'cap':>5}{'fill':>6}{'yes':>8}{'ev/trade':>11}{'95% CI':>18}")
    for mt in (90, 95, 98):
        for cap in (60, 70, 80, 90):
            t, _ = collect(split, min_top=mt, cap=cap)
            s = summarise(t)
            pt = f"{s['per_trade']:+.1f}c" if s["per_trade"] is not None else "—"
            ci = f"[{s['ci_lo']:+.1f},{s['ci_hi']:+.1f}]" if s["ci_lo"] is not None else "n/a"
            flag = "  <-- SIGN FLIP" if (s["per_trade"] or 0) < 0 else ""
            print(f"  {mt:>6}{cap:>5}{s['filled']:>6}{s['wins']:>4}/{s['filled']:<3}"
                  f"{pt:>11}{ci:>18}{flag}")

    print("\nS2  CANCEL POLICY — the largest known lever, reported in full\n")
    print(HDR)
    t, _ = collect(split, cancel_on_recovery=True)
    line("recovery cancel (SUPERSEDED, A1)", summarise(t))
    for w in (15, 30, 60, 120, 300):
        t, _ = collect(split, max_wait_s=w)
        line(f"fixed {w}s window" + ("  <-- FROZEN" if w == 60 else ""), summarise(t))
    print("\n  15s and 30s score higher than the frozen 60s AND clear their CI where")
    print("  60s does not. They are NOT taken (§A1): 60s was chosen before any of")
    print("  this as deliberately-not-the-best, and re-choosing it now on score is")
    print("  the failure mode pre-registration exists to prevent. Logged as a")
    print("  round-3 hypothesis, not a result.")

    print("\nS3  MERGED vs UNMERGED — the parameter this whole study turns on\n")
    print("  Round 1 merged legs (EPISODE_MERGE_S=120) and fired at the episode END;")
    print("  its audit found merge_s drove the result and was missing from its grid.")
    print("  Round 2 does not merge at all. Round 1's own numbers, for comparison:")
    print("     merged 120s, fire at end : -22.5c  n=14   (out/scoreboard.md)")
    print("     unmerged, fire at end    : +10.9c  n=46")
    t, _ = collect(split)
    s = summarise(t)
    print(f"     unmerged, fire BETWEEN   : {s['per_trade']:+.1f}c  n={s['filled']}"
          f"   (this study)")

    print("\nS4  BURST GAP — does the wave boundary itself matter?\n")
    print(HDR)
    for g in (1.0, 2.0, 4.0):
        t, _ = collect(split, burst_gap_s=g)
        line(f"burst gap {g}s" + (" (FROZEN)" if g == 2.0 else ""), summarise(t))

    print("\nS5  MINIMUM PRINTED SIZE AT ENTRY — is the edge just dust?\n")
    print(HDR)
    base, _ = collect(split)
    for ms in (0, 5, 25):
        sel = [t for t in base if t["outcome"] != "filled" or (t["size"] or 0) >= ms]
        line(f"min printed size {ms} ct", summarise(sel))
    print("\n  If the edge survives only at 0, it lives in 1-contract prints we could")
    print("  not have captured at size, and §R9 says abandon.")

    print("\nS6  WINDOW-FREE — no call window, size floor alone\n")
    print(HDR)
    t, _ = collect(split, window_free=True)
    line("window-free", summarise(t))


def excursion(trades):
    print("\nS7  ADVERSE EXCURSION — keeping 'riskless' honest\n")
    f = [t for t in trades if t["outcome"] == "filled"]
    if not f:
        print("  no fills")
        return
    marked = [t for t in f if t["underwater_cents"] is not None]
    if not marked:
        print("  no positions were markable while held")
        return
    u = sorted(t["underwater_cents"] for t in marked)
    winners = [t for t in marked if t["pnl"] > 0]
    deep = [t for t in winners if t["underwater_cents"] >= 20]
    print(f"  positions markable while held : {len(marked)}/{len(f)}")
    print(f"  cents underwater at the worst : median {u[len(u)//2]}, max {u[-1]}, "
          f"{sum(1 for x in u if x == 0)} never underwater")
    print(f"  WINNERS that were >=20c underwater on the way: {len(deep)}/{len(winners)}")
    print("  A trade that finished green after marking 40c against us was a winning")
    print("  gamble, not a riskless one. This is why the rule is not called riskless.")


def size_weighted(trades):
    """S8 — the number an M4 cap-raise argument would rest on."""
    print("\nS8  SIZE-WEIGHTED CEILING — what this could be worth unclamped\n")
    f = [t for t in trades if t["outcome"] == "filled"]
    if not f:
        print("  no fills")
        return
    one = sum(t["pnl"] for t in f)
    full = 0
    for t in f:
        n = max(1, int(t["size"]))
        full += FM.pnl_cents(t["price"], t["result"] == "yes", count=n)
    tot_ct = sum(max(1, int(t["size"])) for t in f)
    print(f"  1 contract per fill (the live envelope) : {one:+d}c "
          f"= ${one/100:+.2f} over {len(f)} fills")
    print(f"  full printed size at each fill          : {full:+d}c "
          f"= ${full/100:+.2f} over {tot_ct} contracts")
    print("\n  PROBE_LEDGER_CAP_CENTS = 100 caps this lane at $1 for its lifetime and")
    print("  max_order_notional_cents = 100 admits exactly one contract per order,")
    print("  so the first row is all that is reachable today. The second row is a")
    print("  CEILING and assumes we displace the actual taker entirely — a stronger")
    print("  assumption than the fill model's, stacked on top of it.")


def null_test(trades, seed=20260807):
    print("\nNULL  EFFICIENT-MARKET — under efficiency a trade pays exactly -fee\n")
    f = [t for t in trades if t["outcome"] == "filled"]
    if len(f) < 5:
        print(f"  n={len(f)} — too few to test")
        return
    rng = random.Random(seed)
    means = []
    for _ in range(10_000):
        means.append(sum(FM.pnl_cents(t["price"], rng.random() < t["price"] / 100.0)
                         for t in f) / len(f))
    means.sort()
    real = sum(t["pnl"] for t in f) / len(f)
    lo, hi = means[250], means[9750]
    p = sum(1 for m in means if m >= real) / len(means)
    v = ("BEATS the efficient null" if real > hi else
         "WORSE than the efficient null" if real < lo else
         "indistinguishable from an efficient market")
    print(f"  real {real:+.1f}c   null {sum(means)/len(means):+.1f}c "
          f"95%[{lo:+.1f},{hi:+.1f}]   p={p:.3f}   {v}")


def loeo(trades):
    print("\nLOEO  LEAVE-ONE-EVENT-OUT — no single event may carry the result\n")
    f = [t for t in trades if t["outcome"] == "filled"]
    if len(f) < 5:
        print(f"  n={len(f)} — too few to test")
        return
    full = sum(t["pnl"] for t in f) / len(f)
    means = []
    for ev in sorted({t["event"] for t in f}):
        rest = [t for t in f if t["event"] != ev]
        if rest:
            means.append((sum(t["pnl"] for t in rest) / len(rest), ev))
    flips = sum(1 for m, _ in means if (m > 0) != (full > 0))
    lo, hi = min(means), max(means)
    short = lambda e: e.replace("KXEARNINGSMENTION", "").replace("KXWORLDNEWSMENTION", "WNT")
    print(f"  full {full:+.1f}c   range [{lo[0]:+.1f} (drop {short(lo[1])}), "
          f"{hi[0]:+.1f} (drop {short(hi[1])})]   sign flips: {flips}/{len(means)}")


def inventory():
    n = defaultdict(list)
    for ev in sorted(D.event_dirs()):
        n[split_of(ev)].append(ev)
    print("EVENT INVENTORY BY SPLIT")
    for k in ("train", "gap", "holdout", "undated"):
        if n[k]:
            print(f"  {k:<9}{len(n[k]):>4}")
    return n


def main(argv):
    split = argv[argv.index("--split") + 1] if "--split" in argv else "train"
    inv = inventory()

    if split == "holdout":
        have = len(inv["holdout"])
        if "--close-the-holdout" not in argv:
            print(f"\nREFUSED. The holdout is sealed until it closes "
                  f"(PRE-REGISTRATION-R2.md §R4).")
            print(f"  banked holdout events : {have}")
            print(f"  close condition       : {CLOSE_EVENTS} events, or {CLOSE_DATE}")
            print("  Counting banked events is not peeking; scoring them is.")
            print("  When the condition is met, pass --close-the-holdout. It is a")
            print("  ONE-SHOT: re-running after seeing the result is an amendment.")
            return 2
        if have < CLOSE_EVENTS and date.today() < CLOSE_DATE:
            print(f"\nREFUSED. --close-the-holdout passed, but the condition is not met: "
                  f"{have}/{CLOSE_EVENTS} events and {date.today()} < {CLOSE_DATE}.")
            return 2
        print(f"\n*** HOLDOUT OPENED — this is the one shot. ***")

    trades, skipped = collect(split)
    print(f"\n{'='*104}\nSPLIT: {split.upper()}   "
          f"{len({t['event'] for t in trades})} events contributed, {len(trades)} triggers")
    if split == "train":
        print("TRAIN IS CONTAMINATED (§R3): every event here was inspected before the")
        print("rule was frozen. These numbers define the rule; they cannot confirm it.")
    if skipped:
        why = defaultdict(list)
        for ev, r in skipped:
            why[r].append(ev.replace("KXEARNINGSMENTION", "").replace("KXWORLDNEWSMENTION", "WNT"))
        print("  excluded: " + "; ".join(f"{r} ({len(v)}): {','.join(sorted(v))}"
                                         for r, v in sorted(why.items())))

    print()
    print(HDR)
    s = summarise(trades)
    line("FROZEN RULE (wave1>=95, cap 70, 60s)", s)
    if split == "holdout":
        print(f"\n  VERDICT: {verdict(s)}")

    control(split)
    if "--replicate" in argv:
        replication(split)
    loeo(trades)
    null_test(trades)
    excursion(trades)
    size_weighted(trades)
    if "--sensitivity" in argv:
        sensitivity(split)

    if "--json" in argv:
        path = argv[argv.index("--json") + 1]
        full = os.path.abspath(path if os.path.isabs(path) else os.path.join(HERE, path))
        if not full.startswith(os.path.abspath(HERE) + os.sep):
            raise RuntimeError(f"refusing to write outside the lane: {full}")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            json.dump({"split": split, "summary": s, "skipped": skipped,
                       "trades": trades}, fh, indent=1)
        print(f"\nwrote {full}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
