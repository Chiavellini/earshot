#!/usr/bin/env python3
"""Δrace in WALL CLOCK: our flag vs the competitor's pump, per word.

Why this exists. `capture-bakeoff` records PCM only — no detector — so a banked
capture has no detections. Replaying that PCM gives detections whose
`audio_pos_ms` is a position on the STREAM timeline (wall-clock-free by design,
`src/types.rs:57-65`), which cannot be compared to a Kalshi trade timestamp.
This joins the two domains:

    audio_pos_ms  --(leg .jsonl cumulative samples)-->  capture_ns
                  --(CLOCK_UPTIME_RAW anchor)-------->  UTC
                  --(vs first wave-1 trade)----------->  Δ

Sign follows docs/COMPETITOR-ANATOMY.md: **negative = we are behind.**

⚠ Two things this does NOT do, on purpose:
  * It does not subtract the detector's emit lag. `A` (520 ms sherpa_kws,
    measured) locates the WORD END in our stream; that is the input to a
    FEED/OURS decomposition, a different question from "when would our order
    have printed". Both are reported, separately labelled.
  * It does not model the order path. `decision→print` is 9.9 ms from the
    us-east box but 102 ms `send→hdrs` from CDMX (VERIFICATIONS Q1); the
    capture host is named in the output so the right one gets added.

Usage:
    scripts/drace-wallclock.py <record-dir> <leg> <report.json> <tape.json>
"""

import json
import sys
from bisect import bisect_right


def load_frames(rec_dir, leg):
    """Cumulative-sample -> capture_ns index for one leg.

    The .jsonl is one row per 20 ms frame: {"seq","capture_ns","samples"}.
    Frames can be dropped at ingress (counted, never silently), so position is
    accumulated from `samples`, never inferred from `seq`.
    """
    cum, ns, total = [], [], 0
    with open(f"{rec_dir}/{leg}.jsonl") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            cum.append(total)
            ns.append(r["capture_ns"])
            total += r["samples"]
    return cum, ns, total


def pos_to_utc(pos_ms, cum, ns, anchor, rate=16000):
    """UTC of the frame carrying stream position `pos_ms`."""
    target = int(pos_ms * rate / 1000)
    i = bisect_right(cum, target) - 1
    if i < 0 or i >= len(ns):
        return None
    delta_ns = ns[i] - anchor["mono_ns"]
    return (anchor["wall_ns"] + delta_ns) / 1e9


def wave1(trades, floor=0.50, window=2.0, need=3):
    """First >=`need` YES trades at >=`floor` within `window` seconds."""
    hi = sorted((t for t in trades if float(t["yes_price_dollars"]) >= floor),
                key=lambda t: t["created_time"])
    for i in range(len(hi) - need + 1):
        if _ep(hi[i + need - 1]) - _ep(hi[i]) <= window:
            return hi[i]
    return None


def _ep(t):
    import datetime as dt
    return dt.datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp()


def main():
    rec, leg, report_p, tape_p = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    anchor = json.load(open(f"{rec}/clock_anchor.json"))
    cum, ns, total = load_frames(rec, leg)
    report = json.load(open(report_p))
    tape = json.load(open(tape_p))

    import datetime as dt
    def U(x):
        return dt.datetime.fromtimestamp(x, dt.timezone.utc).strftime("%H:%M:%S.%f")[:-3]

    print(f"leg={leg}  frames={len(cum)}  audio={total/16000:.1f}s")
    print(f"anchor taken {anchor['taken_utc']} ({anchor['clock']})")
    t0, t1 = pos_to_utc(0, cum, ns, anchor), pos_to_utc(total / 16 - 20, cum, ns, anchor)
    print(f"capture spans {U(t0)}Z .. {U(t1)}Z\n")

    A_MS = 520  # sherpa_kws armed lookahead, measured (LATENCY-PROOF E0b/E0c)

    # first detection per keyword — the one that would have raced
    first = {}
    for d in report.get("detections", []):
        det = d["detection"]
        k = det["keyword"]
        if k not in first or det["audio_pos_ms"] < first[k]["audio_pos_ms"]:
            first[k] = det

    print(f"{'strike':<6} {'our flag':<14} {'pump':<14} {'Δflag':>9} {'word-end est':<14} {'Δfeed':>9}")
    for tk, mk in sorted(tape.get("markets", {}).items()):
        w1 = wave1(mk.get("trades", []))
        s = tk.rsplit("-", 1)[1]
        det = next((v for k, v in first.items() if k.lower() in (mk.get("yes_sub_title") or "").lower()
                    or (mk.get("yes_sub_title") or "").lower().startswith(k.lower())), None)
        if det is None and w1 is None:
            continue
        ours = pos_to_utc(det["audio_pos_ms"], cum, ns, anchor) if det else None
        wend = pos_to_utc(det["audio_pos_ms"] - A_MS, cum, ns, anchor) if det else None
        pump = _ep(w1) if w1 else None
        df = f"{ours - pump:+.3f}s" if (ours and pump) else "-"
        de = f"{wend - pump:+.3f}s" if (wend and pump) else "-"
        print(f"{s:<6} {(U(ours) if ours else 'NO DETECTION'):<14} "
              f"{(U(pump) if pump else 'no sweep'):<14} {df:>9} "
              f"{(U(wend) if wend else '-'):<14} {de:>9}")

    print("\nΔ negative = we are behind (COMPETITOR-ANATOMY sign convention).")
    print("Δflag = our detection vs pump. Δfeed uses word-end (flag − A=520 ms)")
    print("and is the FEED/OURS input, not a race result. Order path NOT added:")
    print("+9.9 ms from the us-east box, +102 ms send→hdrs from CDMX.")


if __name__ == "__main__":
    main()
