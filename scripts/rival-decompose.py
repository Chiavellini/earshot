#!/usr/bin/env python3
"""A1/A3 — how much of the competitor's lead is EARLIER AUDIO vs EARLIER FIRING?

THE QUESTION. `docs/LATENCY-PROOF.md` §THE GAP IS LOCATED reports a "FEED gap"
per race: the interval between the competitor's price-moving sweep and the
instant the **word-END** audio reached our box. It is read as "they get the feed
~1 s before us".

THE CONFOUND, flagged in that section's own caveats and never computed. The gap
is anchored on word-END because that is when OUR detector can fire. A competitor
firing on word-START never needed the word-end audio at all — and the audio for
the word's START reached our box EARLIER, by exactly the word's duration. So:

    earlier_audio = FEED_gap - word_duration          (if they fire at word start)
    earlier_audio = FEED_gap                          (if they fire at word end)

The truth is bounded by those two, and the word durations are already banked in
`corpus/manifests/*.toml` from the same forced alignment that produced the
word-end anchors. No new measurement is needed to put a bound on this.

WHAT THIS IS NOT. This is a deterministic decomposition (a subtraction), not a
hypothesis test — there is no p-value to pre-register. The one place bias could
enter is CHOOSING WHICH UTTERANCE each race refers to when a phrase occurs
several times. That choice is made here by a cross-validated clock mapping, not
by inspection of the answer, and the mapping residuals are printed so the choice
is auditable.

THE DISCRIMINATING TEST (`--per-market`, added 2026-07-27 for PayPal 07-28).
The subtraction above BOUNDS the answer but cannot CHOOSE between the two models,
because at n=3 every route to a verdict is underpowered. One event with a spread
of word lengths does choose, because the models make OPPOSITE predictions about
how the gap VARIES with word duration:

    word-START firing:  gap = earlier_audio + duration   ⇒  slope on duration = +1.0
    word-END   firing:  gap = earlier_audio              ⇒  slope on duration =  0.0

So the estimator is a REGRESSION SLOPE, not a correlation. This matters twice:

1. A slope has the two competing models as literal point predictions (+1 and 0),
   so the result is "excludes one / excludes the other / excludes neither" rather
   than a correlation that has to be argued about. This is the same shape as the
   batching-covariance test that established the competitor is on our feed
   (measured slope -0.084 against +1.00 predicted for a continuous feed).
2. **The slope is invariant to an additive constant.** `Δrace = FEED + OURS`, and
   `OURS` does not depend on word duration — so regressing Δrace on duration gives
   the SAME slope as regressing the FEED gap on duration. That is what makes this
   runnable on a tape we were already pulling: it does NOT require the per-segment
   audio-arrival instrumentation that the level-based decomposition needs, and
   which is not banked (LATENCY-PROOF §R1, "two instrumentation gaps exposed").
   The INTERCEPT is not interpretable without that instrumentation; the slope is.

PayPal's board spans `stash` to `european payments council`, which is the spread
this needs. Pre-registration and the honest power statement print with the result.

Usage:
  rival-decompose.py                       # the pinned R1 decomposition (default)
  rival-decompose.py --per-market <rows.json> [--label "PYPL 07-28"]

`rows.json` is a list of objects, one per contested market:
  [{"market": "STAS", "phrase": "stash",
    "gap_s": 1.31,            # Δrace or FEED gap, SIGN: positive = we are behind
    "word_dur_s": 0.28}, ...]
Optionally omit `word_dur_s` and pass `--manifest corpus/manifests/<call>.toml`
to read durations from the forced alignment by `phrase`.
"""
import argparse
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Two-sided t critical values at alpha=0.05, indexed by degrees of freedom.
# Hardcoded so this script keeps its zero-dependency property (no scipy on the
# box). df = n - 2 for a simple regression.
T_CRIT_95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000,
}


def t_crit(df):
    """Nearest tabulated t at or below df — CONSERVATIVE (wider interval)."""
    if df < 1:
        return float("nan")
    ks = [k for k in sorted(T_CRIT_95) if k <= df]
    return T_CRIT_95[ks[-1]] if ks else float("nan")

# FEED gaps exactly as pinned in LATENCY-PROOF §"Result — the feed term
# dominates and is venue-invariant" (price-moving sweep scoring).
RACES = [
    # event,  venue class,        seg_s, edge_s, market, manifest phrase, FEED gap s
    ("AXP 07-24", "Amazon IVS",      2.0, 4.0, "NFL",  None,          1.241),
    ("LMT 07-23", "Notified/CDN",    4.0, 9.0, "GOLD", "golden dome", 1.112),
    ("LMT 07-23", "Notified/CDN",    4.0, 9.0, "EURO", "europe",      1.125),
    ("LMT 07-23", "Notified/CDN",    4.0, 9.0, "MORF", "morfius",     0.754),
]

REPORTS = {"LMT 07-23": "state/KXEARNINGSMENTIONLMT-26JUL23/report.json"}
MANIFESTS = {"LMT 07-23": "corpus/manifests/lmt-q2-2026.toml"}


def load_lmt():
    rep = json.load(open(ROOT / REPORTS["LMT 07-23"]))
    t0 = rep["first_capture_ts"]
    det = {o["ticker"].split("-")[-1]: (o["audio_ts"] - t0) / 1e9 for o in rep["orders"]}
    man = tomllib.load(open(ROOT / MANIFESTS["LMT 07-23"], "rb"))
    occ = {}
    for o in man["occurrences"]:
        occ.setdefault(o["phrase"], []).append((o["onset_ms"] / 1000, o["end_ms"] / 1000))
    return det, occ


def main():
    det, occ = load_lmt()

    # ── Step 1: fit the corpus-WAV → arm-capture clock offset Δ ──────────────
    # The corpus WAV and the arm's capture start at different instants. For each
    # race, every candidate utterance implies a Δ. The TRUE Δ is the one several
    # independent races agree on; a wrong pairing lands hundreds of seconds away.
    print("=" * 78)
    print("STEP 1 — fit Δ (corpus-WAV → arm-capture offset), cross-validated")
    print("=" * 78)
    cands = {}
    for _, _, _, _, mkt, phrase, _ in RACES:
        if phrase is None or mkt not in det:
            continue
        cands[mkt] = [(det[mkt] - end, on, end) for on, end in occ.get(phrase, [])]
        print(f"\n{mkt} (detector fired at {det[mkt]:.3f}s capture-rel), phrase {phrase!r}:")
        for d, on, end in cands[mkt]:
            print(f"    utterance @ {on:8.2f}–{end:8.2f}s  ⇒  Δ = {d:9.3f} s")

    # the consensus Δ: the value on which the most races agree within 2 s
    alld = [d for v in cands.values() for d, _, _ in v]
    best, bestn = None, -1
    for d in alld:
        n = sum(any(abs(d - x) < 2.0 for x, _, _ in v) for v in cands.values())
        if n > bestn:
            best, bestn = d, n
    print(f"\n⇒ consensus Δ ≈ {best:.3f} s, agreed by {bestn} of {len(cands)} races")

    chosen = {}
    for mkt, v in cands.items():
        m = [(abs(d - best), on, end) for d, on, end in v]
        m.sort()
        if m and m[0][0] < 2.0:
            chosen[mkt] = (m[0][1], m[0][2], m[0][0])
            print(f"    {mkt}: utterance {m[0][1]:.2f}–{m[0][2]:.2f}s "
                  f"(dur {1000*(m[0][2]-m[0][1]):.0f} ms), Δ residual {m[0][0]:.3f} s")
        else:
            print(f"    {mkt}: NO utterance within 2 s of consensus Δ — unresolved")

    # ── Step 2: the decomposition ───────────────────────────────────────────
    print()
    print("=" * 78)
    print("STEP 2 — decompose the FEED gap")
    print("=" * 78)
    print(f"{'race':<12}{'venue':<15}{'FEED gap':>10}{'word dur':>10}"
          f"{'residual (earlier AUDIO)':>26}")
    print("-" * 78)
    resid = []
    for ev, venue, seg, edge, mkt, phrase, gap in RACES:
        if mkt in chosen:
            on, end, _ = chosen[mkt]
            dur = end - on
            r = gap - dur
            resid.append((mkt, r))
            print(f"{mkt:<12}{venue:<15}{gap:>9.3f}s{dur:>9.3f}s{r:>25.3f}s")
        else:
            print(f"{mkt:<12}{venue:<15}{gap:>9.3f}s{'unknown':>10}"
                  f"{'NOT COMPUTABLE':>26}")
    print("-" * 78)

    if resid:
        # ⚠ COMPARE THE SAME RACES ON BOTH SIDES. An earlier version of this
        # printed the 4-race raw spread against the 3-race residual spread,
        # which flatters the result by including a race the residual cannot use.
        done = {m for m, _ in resid}
        paired = [(x[4], x[6], dict(resid)[x[4]]) for x in RACES if x[4] in done]
        g = sorted(p[1] for p in paired)
        r = sorted(p[2] for p in paired)
        print(f"\nOn the SAME {len(paired)} races ({', '.join(sorted(done))}):")
        print(f"  raw FEED gap        range {min(g):.3f}–{max(g):.3f} s  "
              f"spread {max(g)-min(g):.3f} s")
        print(f"  residual (word-start) range {min(r):.3f}–{max(r):.3f} s  "
              f"spread {max(r)-min(r):.3f} s")

        # correlation between word duration and raw gap — the word-start model
        # predicts positive correlation (longer word ⇒ more of the gap is the word).
        xs = [chosen[m][1] - chosen[m][0] for m, _ in resid]
        ys = [dict((x[4], x[6]) for x in RACES)[m] for m, _ in resid]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        sxx = sum((a - mx) ** 2 for a in xs)
        syy = sum((b - my) ** 2 for b in ys)
        rho = sxy / (sxx * syy) ** 0.5 if sxx and syy else float("nan")
        print(f"\n  corr(word duration, raw gap) = {rho:+.3f} at n={n}")
        print(f"  ⚠ AT n={n} THIS IS NOT RESOLVABLE: p<0.05 would need |r|>0.997.")
        print("    Report the direction, never the significance.")

        excl = [x[4] for x in RACES if x[4] not in done]
        if excl:
            print(f"\n  ⚠ excluded for want of a banked word duration: {', '.join(excl)}")
            print("    — and those are the ONLY non-Notified races, so the")
            print("    venue-invariance question CANNOT be asked of the residual.")


def per_market(rows_path, manifest_path=None, label="event"):
    """Regress the gap on word duration for ONE event. Slope +1 ⇒ word-start
    firing, slope 0 ⇒ word-end firing. See the module docstring."""
    rows = json.load(open(rows_path))

    if manifest_path:
        man = tomllib.load(open(manifest_path, "rb"))
        durs = {}
        for o in man["occurrences"]:
            durs.setdefault(o["phrase"], []).append((o["end_ms"] - o["onset_ms"]) / 1000)
        for r in rows:
            if "word_dur_s" not in r and r.get("phrase") in durs:
                d = durs[r["phrase"]]
                # A phrase spoken more than once has no single duration. Refuse
                # rather than average: averaging would silently invent the very
                # quantity the test regresses on.
                if len(d) == 1:
                    r["word_dur_s"] = d[0]
                else:
                    r["_skip"] = f"{len(d)} utterances of {r['phrase']!r} — ambiguous duration"

    usable, skipped = [], []
    for r in rows:
        if r.get("_skip"):
            skipped.append((r.get("market", "?"), r["_skip"]))
        elif "word_dur_s" not in r or "gap_s" not in r:
            skipped.append((r.get("market", "?"), "missing gap_s or word_dur_s"))
        else:
            usable.append(r)

    print("=" * 78)
    print(f"PER-MARKET SLOPE TEST — {label}")
    print("word-START firing ⇒ slope = +1.0    word-END firing ⇒ slope = 0.0")
    print("=" * 78)
    print(f"{'market':<10}{'phrase':<28}{'word dur':>10}{'gap (behind)':>15}")
    print("-" * 78)
    for r in sorted(usable, key=lambda x: x["word_dur_s"]):
        print(f"{r.get('market','?'):<10}{r.get('phrase',''):<28}"
              f"{r['word_dur_s']:>9.3f}s{r['gap_s']:>14.3f}s")
    print("-" * 78)

    if skipped:
        # NEVER silently drop a row — a scorer that discards what would move the
        # answer is worse than one that fails (LATENCY-PROOF §V4).
        print(f"\n⚠ EXCLUDED {len(skipped)} of {len(rows)} rows, named:")
        for m, why in skipped:
            print(f"    {m}: {why}")

    n = len(usable)
    if n < 3:
        print(f"\n⇒ VERDICT: NOT COMPUTABLE — {n} usable rows, a slope needs ≥3.")
        return

    xs = [r["word_dur_s"] for r in usable]
    ys = [r["gap_s"] for r in usable]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((a - mx) ** 2 for a in xs)
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    syy = sum((b - my) ** 2 for b in ys)

    if sxx == 0:
        print("\n⇒ VERDICT: NOT COMPUTABLE — every word has the same duration, so "
              "there is no spread to regress on. This test needs a mixed board.")
        return

    slope = sxy / sxx
    icept = my - slope * mx
    resid_ss = sum((b - (icept + slope * a)) ** 2 for a, b in zip(xs, ys))
    df = n - 2
    se = ((resid_ss / df) / sxx) ** 0.5 if df > 0 and resid_ss > 0 else 0.0
    tc = t_crit(df)
    lo, hi = slope - tc * se, slope + tc * se
    rho = sxy / (sxx * syy) ** 0.5 if syy else float("nan")

    print(f"\n  n = {n}   duration spread {min(xs):.3f}–{max(xs):.3f} s")
    print(f"  slope  = {slope:+.3f} s per s of word duration")
    print(f"  95% CI = [{lo:+.3f}, {hi:+.3f}]   (SE {se:.3f}, df {df})")
    print(f"  intercept = {icept:+.3f} s   ⚠ NOT interpretable as 'earlier audio' "
          "unless gap_s is a FEED gap, not Δrace")
    print(f"  corr = {rho:+.3f}")

    start_in = lo <= 1.0 <= hi
    end_in = lo <= 0.0 <= hi
    print()
    if start_in and not end_in:
        print("⇒ VERDICT: consistent with WORD-START firing, EXCLUDES word-end.")
        print("   Their true feed advantage is smaller than the pinned gap, and the")
        print("   remainder is a DETECTOR difference — i.e. partly ours to close.")
    elif end_in and not start_in:
        print("⇒ VERDICT: consistent with WORD-END firing, EXCLUDES word-start.")
        print("   The pinned FEED gap stands at full size; R1's re-attribution is")
        print("   REFUTED and the campaign's feed spending is correctly aimed.")
    elif start_in and end_in:
        print("⇒ VERDICT: NOT RESOLVED — the interval admits BOTH models.")
        print("   This is a real outcome, not a failed run. Report it as a null and")
        print("   do not pick the model that suits the plan.")
    else:
        print("⇒ VERDICT: EXCLUDES BOTH point models — something in the framing is")
        print("   wrong (mis-paired utterances, mixed venues, or a sign error).")
        print("   Do NOT interpret the slope until that is explained.")

    print(f"\n  Power, stated whichever way it lands: separating +1.0 from 0.0 needs")
    print(f"  the CI half-width below 0.5; it is {tc*se:.3f}. ", end="")
    print("SUFFICIENT." if tc * se < 0.5 else "INSUFFICIENT — widen the board or pool events.")
    print("  Pre-registered before the data existed: a null is a publishable result,")
    print("  the estimator is the SLOPE (not corr), and rows are never dropped silently.")
    print("\n  ⚠ Counter-evidence on the record, unchanged by this run: the MS phone-lane")
    print("    analysis concluded they land at/after word-end, never before. A single")
    print("    event at a single venue class does not overturn that on its own.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--per-market", metavar="ROWS.json",
                    help="per-market slope test for one event (see docstring)")
    ap.add_argument("--manifest", metavar="CALL.toml",
                    help="fill word_dur_s from a forced-alignment manifest")
    ap.add_argument("--label", default="event", help="label for the report header")
    a = ap.parse_args()
    if a.per_market:
        per_market(a.per_market, a.manifest, a.label)
    else:
        main()
