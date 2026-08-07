#!/usr/bin/env python3
"""score_arms.py — score every arm over TRAIN then TEST, per PRE-REGISTRATION.md.

    python3 score_arms.py --split train
    python3 score_arms.py --split test
    python3 score_arms.py --split all --json out/arms.json
    python3 score_arms.py --split train --legacy      # replication gate
    python3 score_arms.py --split test  --grid        # S3 parameter sensitivity

Read-only. Writes only into this lane's out/.
"""

from __future__ import annotations

import json
import os
import random
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

import arms as A          # noqa: E402
import data as D          # noqa: E402
import sweep_segment as ss  # noqa: E402
import windows            # noqa: E402

OUT = os.path.join(HERE, "out")

# PRE-REGISTRATION.md P6 — the events the prior was derived on.
TRAIN = {
    "KXEARNINGSMENTIONAAL-26JUL23", "KXEARNINGSMENTIONALLY-26JUL21",
    "KXEARNINGSMENTIONAMD-26AUG04", "KXEARNINGSMENTIONAXP-26JUL24",
    "KXEARNINGSMENTIONGEV-26JUL22", "KXEARNINGSMENTIONGOOGL-26JUL22",
    "KXEARNINGSMENTIONINTC-26JUL23", "KXEARNINGSMENTIONLMT-26JUL23",
    "KXEARNINGSMENTIONTSLA-26JUL22",
}


def collect(split="all", window_free=False, seg_kw=None, arm_kw=None, legacy=False):
    """Run every arm over every usable event. Returns (trades, skipped)."""
    seg_kw = dict(seg_kw or {})
    arm_kw = dict(arm_kw or {})
    trades, skipped = [], []

    for event, run_dir in sorted(D.event_dirs().items()):
        if split == "train" and event not in TRAIN:
            continue
        if split == "test" and event in TRAIN:
            continue

        tape = D.load_tape(event, run_dir)
        labels = D.load_labels(event, run_dir)
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
        call = start + windows.WINDOW_BEFORE if start else None

        eps = ss.event_episodes(tape, window=win, legacy_entry=legacy,
                                first_only=legacy, **seg_kw)
        by_ticker = defaultdict(list)
        for e in eps:
            by_ticker[e.ticker].append(e)

        day = D.event_day(event)
        for ticker, raw in tape.items():
            result = labels.get(ticker)
            if result not in ("yes", "no"):
                continue
            mine = by_ticker.get(ticker, [])

            for ep in mine:
                for fn in (A.follow, A.fade):
                    r = fn(ep, raw, result, **arm_kw.get(fn.__name__, {}))
                    if r:
                        r.update(event=event, day=day, result=result)
                        trades.append(r)

            if call is not None:
                r = A.level(ticker, raw, call, result, **arm_kw.get("level", {}))
                if r:
                    r.update(event=event, day=day, result=result)
                    trades.append(r)
                r = A.missed(ticker, raw, call, result, had_episode=bool(mine),
                             **arm_kw.get("missed", {}))
                if r:
                    r.update(event=event, day=day, result=result)
                    trades.append(r)

    return trades, skipped


def bootstrap_ci(filled, n=10_000, seed=20260806):
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


def summarise(trades, arm):
    rows = [t for t in trades if t["arm"] == arm]
    filled = [t for t in rows if t["outcome"] == "filled"]
    out = {
        "arm": arm,
        "triggered": len(rows),
        "filled": len(filled),
        "no_offer": sum(1 for t in rows if t["outcome"] == "no-offer"),
        "ev_refused": sum(1 for t in rows if t["outcome"] == "ev-gate-refused"),
        "events": len({t["event"] for t in filled}),
        "days": len({t["day"] for t in filled}),
        "total_cents": sum(t["pnl"] for t in filled),
        "per_trade": (sum(t["pnl"] for t in filled) / len(filled)) if filled else None,
        "win_rate": (sum(1 for t in filled if t["pnl"] > 0) / len(filled)) if filled else None,
        "oracle": bool(rows and rows[0].get("oracle")),
    }
    lo, hi = bootstrap_ci(filled)
    out["ci_lo"], out["ci_hi"] = lo, hi
    return out


def verdict(s):
    """PRE-REGISTRATION.md P7. Applies only to TEST."""
    if s["oracle"]:
        return "UPPER-BOUND-ONLY"
    if s["filled"] == 0:
        return "NO-DATA"
    if s["ci_hi"] is not None and s["ci_hi"] < 0:
        return "REFUTED"
    if (s["per_trade"] is not None and s["per_trade"] >= 5
            and s["ci_lo"] is not None and s["ci_lo"] > 0
            and s["filled"] >= 40 and s["events"] >= 8 and s["days"] >= 4):
        return "PROMOTABLE"
    return "NOT-RESOLVED"


def buckets(trades, arm):
    """P(YES) and per-trade P&L by terminal-price bucket. S1 lives here."""
    rows = [t for t in trades if t["arm"] == arm and t["outcome"] == "filled"
            and t.get("terminal") is not None]
    out = []
    for lo, hi in ((50, 59), (60, 69), (70, 79), (80, 89), (90, 100)):
        s = [t for t in rows if lo <= t["terminal"] <= hi]
        if not s:
            continue
        out.append({
            "bucket": f"{lo}-{hi}", "n": len(s),
            "p_yes": sum(1 for t in s if t["result"] == "yes") / len(s),
            "per_trade": sum(t["pnl"] for t in s) / len(s),
        })
    return out


def report(split, trades, skipped, show_verdict):
    print(f"\n{'='*78}\nSPLIT: {split.upper()}   "
          f"{len({t['event'] for t in trades})} events contributed, "
          f"{len(trades)} arm triggers")
    if skipped:
        why = defaultdict(list)
        for ev, r in skipped:
            why[r].append(ev.replace("KXEARNINGSMENTION", "").replace("KXWORLDNEWSMENTION", "WNT"))
        print("  excluded: " + "; ".join(f"{r} ({len(v)}): {','.join(sorted(v))}"
                                         for r, v in sorted(why.items())))
    print(f"\n{'arm':<9}{'trig':>6}{'fill':>6}{'noOff':>6}{'ev':>4}"
          f"{'ev/trade':>10}{'95% CI':>18}{'win':>7}{'evts':>6}{'days':>6}  verdict")
    for arm in ("follow", "fade", "level", "missed"):
        s = summarise(trades, arm)
        ci = (f"[{s['ci_lo']:+.1f},{s['ci_hi']:+.1f}]"
              if s["ci_lo"] is not None else "n/a")
        pt = f"{s['per_trade']:+.1f}c" if s["per_trade"] is not None else "—"
        wr = f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "—"
        v = verdict(s) if show_verdict else ""
        print(f"{arm:<9}{s['triggered']:>6}{s['filled']:>6}{s['no_offer']:>6}"
              f"{s['ev_refused']:>4}{pt:>10}{ci:>18}{wr:>7}{s['events']:>6}{s['days']:>6}  {v}")

    for arm in ("follow", "fade"):
        b = buckets(trades, arm)
        if not b:
            continue
        print(f"\n  {arm} by sweep terminal price:")
        print(f"    {'bucket':<9}{'n':>5}{'P(YES)':>9}{'per-trade':>12}")
        for r in b:
            print(f"    {r['bucket']:<9}{r['n']:>5}{r['p_yes']:>9.0%}{r['per_trade']:>+11.1f}c")


def main(argv):
    split = "all"
    if "--split" in argv:
        split = argv[argv.index("--split") + 1]
    legacy = "--legacy" in argv
    wf = "--window-free" in argv

    trades, skipped = collect(split, window_free=wf, legacy=legacy)
    report(split, trades, skipped, show_verdict=(split == "test"))

    if "--json" in argv:
        path = argv[argv.index("--json") + 1]
        full = os.path.abspath(os.path.join(HERE, path) if not os.path.isabs(path) else path)
        if not full.startswith(os.path.abspath(HERE) + os.sep):
            raise RuntimeError(f"refusing to write outside the lane: {full}")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            json.dump({"split": split, "legacy": legacy, "window_free": wf,
                       "summary": [summarise(trades, a)
                                   for a in ("follow", "fade", "level", "missed")],
                       "buckets": {a: buckets(trades, a) for a in ("follow", "fade")},
                       "skipped": skipped, "trades": trades}, f, indent=1)
        print(f"\nwrote {full}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
