#!/usr/bin/env python3
"""E0 — what does N ms of detector lookahead actually COST in wall time?

The detector does not fire the instant a word ends; it must consume some audio
past the word end first (`emit_lag_ms`, measured offline by `bench-detector`).
On a smooth feed that lag costs exactly itself in wall time. On a live HLS feed
it does not: audio arrives in **segment-sized bursts**, so trailing audio the
engine needs is often ALREADY IN HAND (cost ~0), and occasionally sits in the
next segment (cost up to a full segment).

This script measures that conversion directly from a banked capture — no model,
no assumption about segment duration or burst shape. For every frame it asks:

    wait(i, A) = capture_ns[i + A/frame_ms] - capture_ns[i]

i.e. "starting from the frame where the word ended, how long until A ms more
audio had physically arrived?" The distribution of that over all i IS the
live cost of A ms of lookahead at this venue, including whatever the CDN,
the fetcher and the decoder actually did on the day.

Usage:
    arrival-profile.py CAPTURE.jsonl [--lookahead-ms 400 ...] [--rate 16000]

Input is the per-frame log written by the capture bake-off:
    {"seq":0,"capture_ns":298634394075625,"samples":320}
"""
import argparse
import json
import statistics
import sys

GAP_BURST_NS = 50_000_000     # 50 ms of silence between frames ⇒ new burst
STALL_NS = 3_000_000_000      # 3 s ⇒ a stall, not a segment boundary


def pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    r = max(1, min(len(s), int(q * len(s) + 0.999999)))
    return s[r - 1]


def load(path):
    seq, cap, samp = [], [], []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            seq.append(r["seq"])
            cap.append(r["capture_ns"])
            samp.append(r["samples"])
    return seq, cap, samp


def burst_structure(cap, audio_ms):
    """Segment the arrival stream into bursts and describe them.

    ⚠ THE STALL THRESHOLD MUST SCALE WITH SEGMENT DURATION (fixed 2026-07-27).
    A fixed 3 s threshold reports EVERY SEGMENT BOUNDARY as a stall at any venue
    whose segments are ≥3 s. That is not a subtle bias: on the banked 4 s
    Notified captures it produced 1090–1384 "stalls" per run against 17–20 on a
    2 s IVS capture taken CONCURRENTLY ON THE SAME HOST — an apparent 60×
    venue difference that is entirely an artifact of the constant. The threshold
    is now derived from the measured burst period.

    ⚠ `max_gap_s` INCLUDES TEARDOWN. The largest gap in every banked capture
    lands at 99–100 % of the run — it is the capture being shut down, not a
    delivery failure. Excluding the final gap is reported separately below;
    quoting the raw max as a "worst-case delivery gap" is wrong and was.
    """
    gaps = [cap[i + 1] - cap[i] for i in range(len(cap) - 1)]
    bounds = [i for i, g in enumerate(gaps) if g >= GAP_BURST_NS]
    periods = []
    payloads, spans = [], []
    for a, b in zip(bounds, bounds[1:]):
        periods.append((cap[b] - cap[a]) / 1e6)              # burst-to-burst, ms
        payloads.append(audio_ms[b] - audio_ms[a])           # audio delivered, ms
        spans.append((cap[b] - cap[a + 1]) / 1e6)            # time spent delivering
    period_ms = statistics.median(periods) if periods else None
    # A stall is a gap materially longer than this venue's own burst cadence.
    stall_ns = max(STALL_NS, 1.5 * period_ms * 1e6) if period_ms else STALL_NS
    stalls = [g for g in gaps if g >= stall_ns]
    # teardown-excluded view: drop the final inter-frame gap
    body = gaps[:-1] if len(gaps) > 1 else gaps
    return {
        "bursts": len(bounds),
        "period_ms": period_ms,
        "payload_ms": statistics.median(payloads) if payloads else None,
        "span_ms": statistics.median(spans) if spans else None,
        "stalls": len(stalls),
        "stall_thresh_s": stall_ns / 1e9,
        "max_gap_s": max(gaps) / 1e9 if gaps else 0.0,
        "max_gap_excl_teardown_s": max(body) / 1e9 if body else 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl")
    ap.add_argument("--rate", type=int, default=16000)
    ap.add_argument("--lookahead-ms", type=int, nargs="+", default=[200, 300, 400, 500])
    a = ap.parse_args()

    seq, cap, samp = load(a.jsonl)
    if len(cap) < 100:
        print(f"NOT RESOLVABLE — only {len(cap)} frames in {a.jsonl}")
        return 1

    # Audio position of each frame, from the samples actually delivered — never
    # assume a fixed frame size.
    audio_ms, t = [], 0.0
    for s in samp:
        audio_ms.append(t)
        t += s * 1000.0 / a.rate
    frame_ms = statistics.median(
        [audio_ms[i + 1] - audio_ms[i] for i in range(min(500, len(audio_ms) - 1))]
    )

    b = burst_structure(cap, audio_ms)
    dur_s = (audio_ms[-1] - audio_ms[0]) / 1000.0
    wall_s = (cap[-1] - cap[0]) / 1e9
    print(f"file            {a.jsonl}")
    print(f"frames          {len(cap)}  frame={frame_ms:.1f} ms  audio={dur_s:.1f} s  wall={wall_s:.1f} s")
    if b["period_ms"]:
        r = b["payload_ms"] / b["span_ms"] if b["span_ms"] and b["span_ms"] > 0 else float("inf")
        print(f"bursts          {b['bursts']}  period={b['period_ms']:.0f} ms  "
              f"payload={b['payload_ms']:.0f} ms  delivered-over={b['span_ms']:.0f} ms  "
              f"=> {r:.1f}x real time inside a burst")
    else:
        print("bursts          NONE DETECTED — arrivals are smooth at this gap threshold")
    print(f"stalls(>{b['stall_thresh_s']:.1f}s)   {b['stalls']}   "
          f"max gap {b['max_gap_s']:.2f} s "
          f"(excl. teardown {b['max_gap_excl_teardown_s']:.2f} s)")
    if b["stalls"]:
        print("  ⚠ stalls inflate the tail below; they are real capture events, not model noise")

    print()
    print("wall cost of detector lookahead, measured over every frame position:")
    print(f"{'lookahead':>10} {'n':>7} {'p50':>8} {'p90':>8} {'p95':>8} {'max':>9} {'>1 burst':>9}")
    for A in a.lookahead_ms:
        step = int(round(A / frame_ms))
        if step < 1 or step >= len(cap):
            print(f"{A:>9}ms  SKIPPED — lookahead below one frame or beyond the capture")
            continue
        waits = [(cap[i + step] - cap[i]) / 1e6 for i in range(len(cap) - step)]
        # "paid a burst" = the trailing audio was NOT already in hand.
        thresh = (b["period_ms"] or 0) * 0.5
        paid = sum(1 for w in waits if w > thresh) / len(waits) * 100 if thresh else float("nan")
        print(f"{A:>9}ms {len(waits):>7} {pct(waits,0.50):>8.1f} {pct(waits,0.90):>8.1f} "
              f"{pct(waits,0.95):>8.1f} {max(waits):>9.1f} {paid:>8.1f}%")
    print()
    print("Read the p50 as the TYPICAL cost and the '>1 burst' column as how often")
    print("the lookahead spills past the segment you already had. Mean cost tends to")
    print("A regardless of segment size; the DISTRIBUTION is what a race feels.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
