#!/usr/bin/env python3
"""score_probe.py — the ONE-SHOT series-generalization score.

    python3 probe_series.py --score --smoke   # plumbing check on TRAIN (harmless)
    python3 probe_series.py --score           # THE ONE SHOT

`--smoke` runs the identical code path over TRAIN events. TRAIN is already
contaminated, so running it any number of times costs nothing, and it means the
real pass does not double as the debugging session. Developing directly against
the probe set would spend the one shot on a typo.

Everything scored here uses the frozen rule from PRE-REGISTRATION-R2.md §R2 as
amended by A1, WINDOW-FREE (§S6). Window-free is forced, not chosen: probe
events have no config, `lib/windows.py` refuses to guess, and inferring a window
from trade density is the circular error round 1 documented.

⚠ The TRAIN baseline for comparison is therefore the WINDOW-FREE figure —
+24.5c, 20/25, 11 events — not the windowed +21.0c. Comparing against the
windowed number would compare two different rules.

Read-only. Writes only into this lane's out/.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
sys.path.insert(0, HERE)

import audit_r2 as AU        # noqa: E402
import data as D             # noqa: E402
from score_r2 import HDR, collect, line, summarise  # noqa: E402

OUT = os.path.join(HERE, "out")

# PRE-REGISTRATION-R2-PROBE.md §P2 — the declared comparison baseline.
TRAIN_BASELINE = {"per_trade": 24.5, "wins": 20, "filled": 25, "events": 11,
                  "ci": (0.3, 48.0)}


def verdict(probe, control):
    """PRE-REGISTRATION-R2-PROBE.md §P3. Scored once; no other outcome exists."""
    if probe["filled"] == 0:
        return "NOT-RESOLVED (no fills)"
    separates = (probe["per_trade"] is not None and control["per_trade"] is not None
                 and probe["per_trade"] > control["per_trade"])
    if probe["ci_hi"] is not None and probe["ci_hi"] < 0 and probe["filled"] >= 20:
        return "DOES-NOT-GENERALIZE (CI upper bound below zero)"
    if not separates and probe["filled"] >= 20:
        return "DOES-NOT-GENERALIZE (wave-1 filter does not separate from control)"
    if (probe["per_trade"] > 0 and probe["ci_lo"] is not None and probe["ci_lo"] > 0
            and probe["filled"] >= 20 and probe["events"] >= 10 and separates):
        return "GENERALIZES"
    return "NOT-RESOLVED"


def series_clustered_ci(trades, n=10_000, seed=20260807):
    """95% CI resampled BY SERIES, not by event.

    Added 2026-08-07 BEFORE any probe number existed, and recorded as a note on
    the pre-registration rather than a change to it.

    Why: §P3's declared bar is an EVENT-clustered CI, inherited from round 2
    where every event was a separate company's call. The probe population is not
    like that — KXWCMENTION contributes 104 events and KXMLBMENTION 102, all of
    them the same venue with the same market-maker and the same crowd. Treating
    those as 104 independent observations would understate the interval badly,
    and a generalization claim is precisely a claim ACROSS venues.

    So: the declared bar stays event-clustered, and this stricter interval is
    reported alongside. If they disagree, the series-clustered one is the honest
    number and the write-up must say so.
    """
    import random
    filled = [t for t in trades if t["outcome"] == "filled"]
    if not filled:
        return (None, None, 0)
    g = defaultdict(list)
    for t in filled:
        g[t["event"].rsplit("-", 1)[0]].append(t["pnl"])
    groups = list(g.values())
    if len(groups) < 2:
        return (None, None, len(groups))
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        pool = []
        for _ in range(len(groups)):
            pool.extend(groups[rng.randrange(len(groups))])
        if pool:
            means.append(sum(pool) / len(pool))
    means.sort()
    return (means[int(0.025 * len(means))], means[int(0.975 * len(means))], len(groups))


def by_series(trades, top=20):
    g = defaultdict(list)
    for t in trades:
        if t["outcome"] == "filled":
            g[t["event"].rsplit("-", 1)[0]].append(t)
    rows = []
    for s, v in g.items():
        rows.append((s, len(v), sum(1 for x in v if x["result"] == "yes"),
                     sum(x["pnl"] for x in v) / len(v)))
    return sorted(rows, key=lambda r: -r[1])[:top]


def main(argv):
    smoke = "--smoke" in argv
    if smoke:
        import score_r2
        events = sorted(e for e in D.event_dirs() if score_r2.split_of(e) == "train")
        tag = "SMOKE (TRAIN events — plumbing check, costs nothing)"
    else:
        import probe_series
        events = probe_series.scored_events()
        tag = "THE ONE SHOT — series probe, scored once"

    print(f"{'='*112}\n{tag}")
    print(f"events available: {len(events)}")
    if not events:
        print("no events — nothing to score")
        return 2

    # ONE pass, not two. The min_top=95 trigger set is EXACTLY the subset of the
    # min_top=50 set with top >= 95 — `qualifies()` differs in that threshold and
    # nothing else, and `fire()` never sees min_top — so the control can be
    # collected once and the probe filtered out of it. Two collect() calls over
    # 798 events would read 2 GB of tape twice for an identical answer.
    import score_r2
    score_r2.CACHE = False        # 798 events will not fit in RAM as parsed JSON
    ctrl_t, skipped = collect("all", window_free=True, events=events, min_top=50)
    probe_t = [t for t in ctrl_t if t["wave_top"] >= 95]
    probe, control = summarise(probe_t), summarise(ctrl_t)

    if skipped:
        why = defaultdict(int)
        for _e, r in skipped:
            why[r] += 1
        print(f"excluded: {dict(why)}")
    print(f"events contributing triggers: {len({t['event'] for t in probe_t})}\n")

    print(HDR)
    line("PROBE  frozen rule, window-free", probe)
    line("       control: no wave-1 filter", control)
    b = TRAIN_BASELINE
    b_yes = f"{b['wins']}/{b['filled']}"
    b_ci = f"[{b['ci'][0]:+.1f},{b['ci'][1]:+.1f}]"
    b_pt = f"{b['per_trade']:+.1f}c"
    print(f"  {'TRAIN baseline (window-free)':<40}{'—':>6}{b['filled']:>6}{b_yes:>8}"
          f"{'':>14}{'':>6}{'':>7}{b_pt:>9}{b_ci:>17}{b['events']:>5}")

    slo, shi, nser = series_clustered_ci(probe_t)
    clo, chi, _ = series_clustered_ci(ctrl_t)
    if slo is not None:
        print(f"\n  SERIES-clustered 95% CI (stricter — see series_clustered_ci):")
        print(f"    probe   {probe['per_trade']:+.1f}c  [{slo:+.1f},{shi:+.1f}]"
              f"   over {nser} series"
              + ("" if slo > 0 else "   <-- straddles zero at the venue level"))
        if clo is not None:
            print(f"    control {control['per_trade']:+.1f}c  [{clo:+.1f},{chi:+.1f}]")

    v = verdict(probe, control)
    print(f"\n  VERDICT: {v}")
    print(f"  (separation: probe {probe['per_trade']:+.1f}c vs control "
          f"{control['per_trade']:+.1f}c"
          f"  =  {(probe['per_trade'] or 0) - (control['per_trade'] or 0):+.1f}c)")

    print("\nPLACEBOS — what separates a real trigger from 'buy cheap mention markets'")
    pt = AU.placebo_timing("all", draws=400, events=events, window_free=True)
    if pt:
        print(f"  timing    : {pt['mean']:+.1f}c  95%[{pt['lo']:+.1f},{pt['hi']:+.1f}]"
              f"   over {pt['markets']} triggering markets")
    ps = AU.placebo_selection("all", draws=400, events=events, window_free=True)
    if ps:
        print(f"  selection : {ps['mean']:+.1f}c  95%[{ps['lo']:+.1f},{ps['hi']:+.1f}]"
              f"   over {ps['markets']} non-triggering markets")
    print(f"  arm       : {probe['per_trade']:+.1f}c")

    print("\nBY SERIES — EXPLORATORY (§P3: may generate a hypothesis, may not promote)")
    print(f"  {'series':<32}{'fills':>7}{'yes':>8}{'per-trade':>12}")
    for s, n, w, ptr in by_series(probe_t):
        print(f"  {s:<32}{n:>7}{w:>4}/{n:<3}{ptr:>+11.1f}c")

    if not smoke:
        path = os.path.join(OUT, "probe-result.json")
        with open(path, "w") as f:
            json.dump({"events_scored": len(events), "verdict": v,
                       "probe": probe, "control": control,
                       "series_clustered_ci": {"probe": [slo, shi],
                                               "control": [clo, chi],
                                               "n_series": nser},
                       "placebo_timing": pt, "placebo_selection": ps,
                       "train_baseline": TRAIN_BASELINE,
                       "by_series": by_series(probe_t, top=200),
                       "trades": probe_t}, f, indent=1, default=str)
        print(f"\nwrote {path}")
        print("This was the one shot. Re-running it is an amendment to "
              "PRE-REGISTRATION-R2-PROBE.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
