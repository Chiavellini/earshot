#!/usr/bin/env python3
"""Verdict for the `--prefetch` drop-safety gate, read from WALL-ANCHORED samples.

⚠ Why not final WAV durations. `ffmpeg -t $DUR` caps the OUTPUT AUDIO duration,
so a leg that LOSES audio still writes $DUR seconds — it just takes longer in
wall time to get there. Comparing final durations would PASS exactly the failure
this gate exists to catch (S7: the speculative posture wrote a full-length file
while silently missing ~86 s, as ~6 s gaps scattered through the call).

The measure is bytes-on-disk for BOTH legs sampled at the SAME WALL INSTANT.

Two failure modes, not one:
  deficit  prefetch < plain and diverging  => audio LOST — the S7 failure.
  surplus  prefetch >> plain and diverging => audio DUPLICATED, which happens if
           a prefetch URL is not byte-identical to the URL later announced, so
           the segment is fetched twice. Not harmless: duplicate audio confuses
           the detector and pushes the leg behind live.

A small POSITIVE delta is expected and healthy — the prefetch leg legitimately
runs ~1 segment ahead, plus the launch stagger.

Usage: prefetch-verdict.py <samples.log>
"""
import re
import sys

BPS = 32000.0          # 16 kHz mono s16le
DEFICIT_S = -0.5       # pre-registered bar: zero audio deficit
SURPLUS_S = 20.0       # beyond a plausible lead+stagger ⇒ duplication
MIN_SAMPLES = 10

path = sys.argv[1] if len(sys.argv) > 1 else "samples.log"
deltas, skipped = [], 0
for line in open(path):
    if "delta=" not in line:
        continue
    try:
        d = int(line.rsplit("delta=", 1)[1].split()[0])
    except ValueError:
        continue
    # Defence in depth against the 2026-07-27 artifact: a sample where either
    # leg reads ~0 bytes is not a measurement of this run (stale file, or a
    # file ffmpeg has not created yet). Count and report them — never silently
    # drop rows that would change a verdict.
    m = re.search(r"prefetch=(\d+) plain=(\d+)", line)
    if m and (int(m.group(1)) < 1000 or int(m.group(2)) < 1000):
        skipped += 1
        continue
    deltas.append(d)
if skipped:
    print(f"⚠ skipped {skipped} degenerate sample(s) where a leg read ~0 bytes")

if len(deltas) < MIN_SAMPLES:
    print(f"GATE: INCONCLUSIVE — only {len(deltas)} samples (need {MIN_SAMPLES})")
    raise SystemExit(2)

lo, hi, last = min(deltas), max(deltas), deltas[-1]
# Trend over the second half: a real leak grows, jitter does not.
half = deltas[len(deltas) // 2:]
drift = (half[-1] - half[0]) / BPS

print(f"samples={len(deltas)}  "
      f"min={lo/BPS:+.2f}s  max={hi/BPS:+.2f}s  final={last/BPS:+.2f}s  "
      f"second-half drift={drift:+.2f}s")

if lo / BPS < DEFICIT_S:
    print(f"GATE: FAIL — prefetch fell {(-lo)/BPS:.2f} s BEHIND the plain leg "
          f"(pre-registered bar: zero deficit)")
    raise SystemExit(1)
if hi / BPS > SURPLUS_S:
    print(f"GATE: FAIL — prefetch ran {hi/BPS:.2f} s AHEAD; at that size it is "
          f"duplicated audio, not lead")
    raise SystemExit(1)
print("GATE: PASS — no audio deficit and no duplication")
