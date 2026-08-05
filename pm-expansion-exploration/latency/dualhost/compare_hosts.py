#!/usr/bin/env python3
"""Pair detections across two hosts' launch.log files and report the wallclock delta.

Both hosts must be NTP-disciplined against the same reference (box-ready.sh reports
the skew; it was 0 s on 2026-08-05). The delta is then directly the transport +
contention differential for the same utterance on the same bridge.

  python3 compare_hosts.py --box <box>/launch.log --local <laptop>/launch.log

Reads only. Prints a table and a verdict line; exits 0 always (this is a measurement,
not a gate).
"""
import argparse
import re
import sys
from datetime import datetime

# Strip ANSI: tracing writes colour codes even when redirected to a file.
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# 2026-08-05T20:37:05.671976Z  INFO earshot::pipeline: decision intent keyword=3 ...
LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+\w+\s+earshot::pipeline:\s+"
    # non-greedy: "detection ignored (keyword state machine) keyword=5" contains the
    # bare word "keyword" before the real "keyword=" field — must skip to the one with '='.
    r"(?P<kind>decision intent|detection ignored).*?keyword=(?P<kw>\d+)"
)


def parse_ts(s):
    # %f caps at 6 digits; tracing emits exactly 6.
    return datetime.strptime(s.replace("Z", ""), "%Y-%m-%dT%H:%M:%S.%f")


def load(path):
    """-> [(datetime, keyword_id, kind)] in file order."""
    out = []
    with open(path, errors="replace") as fh:
        for raw in fh:
            m = LINE.match(ANSI.sub("", raw))
            if m:
                out.append((parse_ts(m["ts"]), int(m["kw"]), m["kind"]))
    return out


def pair(a, b, window_s):
    """Greedy nearest-match per keyword within window. Returns (pairs, a_only, b_only).

    Same keyword can fire repeatedly on a long call, so match within keyword and
    consume each event at most once.
    """
    used_b = set()
    pairs, a_only = [], []
    for ta, kw, kind_a in a:
        best, best_d = None, None
        for j, (tb, kwb, kind_b) in enumerate(b):
            if kwb != kw or j in used_b:
                continue
            d = abs((ta - tb).total_seconds())
            if d <= window_s and (best_d is None or d < best_d):
                best, best_d = j, d
        if best is None:
            a_only.append((ta, kw, kind_a))
        else:
            used_b.add(best)
            pairs.append((kw, a[a.index((ta, kw, kind_a))][0], b[best][0], kind_a, b[best][2]))
    b_only = [e for j, e in enumerate(b) if j not in used_b]
    return pairs, a_only, b_only


def pct(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(round((p / 100) * (len(v) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--box", required=True, help="launch.log from the us-east-1 box leg")
    ap.add_argument("--local", required=True, help="launch.log from the CDMX laptop leg")
    ap.add_argument("--window", type=float, default=5.0,
                    help="max seconds between two hosts' view of the same utterance")
    args = ap.parse_args()

    box, loc = load(args.box), load(args.local)
    print(f"box   {args.box}: {len(box)} detection events")
    print(f"local {args.local}: {len(loc)} detection events")
    if not box or not loc:
        print("\nNOT COMPARABLE — at least one host produced no detections.")
        return

    pairs, box_only, loc_only = pair(box, loc, args.window)
    if not pairs:
        print(f"\nNOT COMPARABLE — no utterance seen by both hosts within {args.window}s.")
        return

    print(f"\npaired {len(pairs)}  |  box-only {len(box_only)}  |  local-only {len(loc_only)}")
    print("\n  kw   box decision_ts        local decision_ts     delta_ms  (+ = box first)")
    deltas = []
    for kw, tb, tl, kind_b, kind_l in sorted(pairs, key=lambda r: r[1]):
        d = (tl - tb).total_seconds() * 1000
        deltas.append(d)
        flag = "" if kind_b == kind_l else f"  [kinds differ: box={kind_b} local={kind_l}]"
        print(f"  {kw:<4} {tb.strftime('%H:%M:%S.%f')}      {tl.strftime('%H:%M:%S.%f')}"
              f"     {d:+8.1f}{flag}")

    print(f"\ndelta_ms  n={len(deltas)}  p50 {pct(deltas,50):+.1f}  p90 {pct(deltas,90):+.1f}"
          f"  min {min(deltas):+.1f}  max {max(deltas):+.1f}")

    p50 = pct(deltas, 50)
    print()
    if p50 > 150:
        print(f"VERDICT: box leads by {p50:.0f} ms p50 — a real latency lever, competitive with a model swap.")
    elif p50 > 50:
        print(f"VERDICT: box leads by {p50:.0f} ms p50 — worth having, but the decode quantum still dominates.")
    elif p50 > -50:
        print(f"VERDICT: {p50:+.0f} ms p50 — the box is a RELIABILITY fix, not a latency lever. "
              "Put remaining effort into the decode quantum (P1 chunk-16, then the 80 ms family).")
    else:
        print(f"VERDICT: box is SLOWER by {-p50:.0f} ms p50 — investigate before deploying.")

    if box_only or loc_only:
        print(f"\nNOTE: {len(box_only)} box-only and {len(loc_only)} local-only detections are a "
              "RECALL difference, not a latency one — cross-check drops.source_to_preproc in both "
              "report.json files before reading anything into the deltas above.")


if __name__ == "__main__":
    sys.exit(main())
