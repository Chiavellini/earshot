#!/usr/bin/env python3
"""Join two or more `--arrival-log` files on the URL hash and report the lead.

The honest leg-vs-leg arrival measurement. Both legs fetch the SAME segment URLs
(proven byte-identical in the V2 gate: segs 2673 vs prefetch_fetched 2672) and
run on the same box, so joining on the URL hash and differencing `t_done` gives
exactly "how much earlier did this leg have this segment's audio" — with no PDT
derivation, no clock alignment, and no cross-correlation.

Reported as the median of PAIRED per-segment differences, which is what the race
arithmetic needs. A wide IQR means the legs were not in steady state and the
number should not be quoted.

Usage: arrival-lead.py <baseline.log> <candidate.log> [more.log ...]
       positive lead = candidate had the segment EARLIER than baseline
"""
import statistics as st
import sys


def load(path):
    """hash -> (t_done, fetch_secs, mode, t_first). First sighting wins; a retry
    that re-fetches the same segment must not overwrite the arrival that counted.

    t_first is when AUDIO STARTS FLOWING to the decoder — equal to t_done for a
    buffered fetch (nothing is emitted until the body is complete) and up to
    ~1.4 s earlier for a streamed one. Reporting only t_done would be blind to
    exactly what --stream-body exists for."""
    out = {}
    for line in open(path):
        f = line.split()
        if len(f) < 4:
            continue
        h, t, d, mode = f[0], float(f[1]), float(f[2]), f[3]
        tf = float(f[4]) if len(f) > 4 else t
        if h not in out:
            out[h] = (t, d, mode, tf)
    return out


if len(sys.argv) < 3:
    print(__doc__)
    raise SystemExit(2)

base_path = sys.argv[1]
base = load(base_path)
print(f"baseline {base_path}: {len(base)} segments")

for cand_path in sys.argv[2:]:
    cand = load(cand_path)
    common = sorted(set(base) & set(cand))
    if len(common) < 30:
        print(f"\n{cand_path}: only {len(common)} paired segments — NOT RESOLVABLE")
        continue
    # positive => candidate arrived EARLIER (baseline t_done is later).
    # Baseline is always buffered, so its audio-available instant is t_done.
    leads = sorted(base[h][0] - cand[h][0] for h in common)
    first = sorted(base[h][0] - cand[h][3] for h in common)
    q1 = leads[len(leads) // 4]
    q3 = leads[(3 * len(leads)) // 4]
    f1 = first[len(first) // 4]
    f3 = first[(3 * len(first)) // 4]
    fetch_b = st.median([base[h][1] for h in common])
    fetch_c = st.median([cand[h][1] for h in common])
    modes = {}
    for h in common:
        modes[cand[h][2]] = modes.get(cand[h][2], 0) + 1
    print(f"\n{cand_path}: {len(cand)} segments, {len(common)} paired")
    print(f"   LEAD (complete)   median = {st.median(leads):+.3f} s   "
          f"IQR [{q1:+.3f}, {q3:+.3f}]   min {leads[0]:+.3f}   max {leads[-1]:+.3f}")
    print(f"   LEAD (first byte) median = {st.median(first):+.3f} s   "
          f"IQR [{f1:+.3f}, {f3:+.3f}]   min {first[0]:+.3f}   max {first[-1]:+.3f}")
    print(f"   median fetch duration: baseline {fetch_b:.3f} s, candidate {fetch_c:.3f} s")
    print(f"   candidate modes: {modes}")
    spread = q3 - q1
    if spread > 1.0:
        print(f"   ⚠ IQR spread {spread:.2f} s — legs likely not in steady state; "
              f"do not quote this median")
