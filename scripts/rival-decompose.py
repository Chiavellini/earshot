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

Usage: rival-decompose.py
"""
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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


if __name__ == "__main__":
    main()
