#!/usr/bin/env python3
"""wallclock_anchor.py — bridge MonotonicNs (capture_ns) to wall-clock UTC.

Same technique as scripts/abs-lag.py (production, read-only reference, never
modified): raw = CLOCK_MONOTONIC_RAW sample, utc = time.time() sample, taken
together on the same host. capture-bakeoff's `.jsonl` output (per
src/clock.rs) uses CLOCK_UPTIME_RAW on macOS / CLOCK_MONOTONIC_RAW on Linux
for MonotonicNs — chrony/NTP-slew-immune, matching this anchor's own clock
source.

Needed because capture-bakeoff itself has no wall-clock anchor at all (its
CaptureReport carries only MonotonicNs `capture_ns` per frame) and — unlike
the WNT precedent, which leans on HLS `#EXT-X-PROGRAM-DATE-TIME` tags via
scripts/abs-lag.py/pdt-log.py — KNBR's stream is plain MP3/Icecast, not HLS,
so there is no PDT tag to anchor against. This is the necessary fallback: a
direct (monotonic, wall) sample pair, taken on the SAME host, as close as
possible to the capture window (no sleep in between — a laptop sleep can
break the monotonic clock's free-running guarantee).

Error bound, stated explicitly rather than assumed zero (same discipline as
pm-expansion-exploration/profitability/extract_decisions.py's bridge_error_s):
this technique's error grows with (a) scheduler jitter between the two
clock_gettime calls within one `sample()` (microseconds, negligible), and
(b) elapsed wall-clock time between the anchor sample and the capture_ns
being converted (bounded by the host's monotonic-vs-NTP drift rate, usually
low ppm-scale, but NOT measured here — take a fresh anchor immediately
before AND after each capture session and compare the two conversions of a
shared reference point rather than trusting a single sample over a long
session).

Usage:
  wallclock_anchor.py --sample                       # print one anchor now
  wallclock_anchor.py --sample --out anchor.json
  wallclock_anchor.py --to-utc anchor.json <capture_ns>
  wallclock_anchor.py --self-test
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone


def sample():
    """One (monotonic_raw_ns, utc_s) pair, taken back-to-back."""
    raw_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
    utc_s = time.time()
    return {
        "raw_monotonic_ns": raw_ns,
        "utc_s": utc_s,
        "clock_source": "CLOCK_MONOTONIC_RAW",
        "sampled_at_utc": datetime.fromtimestamp(utc_s, timezone.utc).isoformat(),
        "warning": (
            "Anchor pair, not ground truth. Error grows with wall-clock time "
            "elapsed between this sample and the capture_ns being converted, "
            "and with any host sleep/hibernate in between (breaks the "
            "monotonic-clock free-running guarantee). Take a fresh sample "
            "immediately before AND after each capture session and compare "
            "the two conversions of a known shared reference point — do not "
            "assume drift is zero over a long session."
        ),
    }


def to_utc(anchor, capture_ns):
    """capture_ns (MonotonicNs, same clock source as the anchor) -> utc seconds."""
    return anchor["utc_s"] + (capture_ns - anchor["raw_monotonic_ns"]) / 1e9


def self_test():
    p = f = 0

    def chk(name, want, got):
        nonlocal p, f
        ok = abs(want - got) < 1e-6 if isinstance(want, float) else want == got
        if ok:
            print(f"  PASS  {name}")
            p += 1
        else:
            print(f"  FAIL  {name} (expected {want!r}, got {got!r})")
            f += 1

    print("wallclock_anchor self-test:")

    anchor = {"raw_monotonic_ns": 1_000_000_000_000, "utc_s": 1_785_000_000.0}
    chk("same instant maps to the same utc", 1_785_000_000.0,
        to_utc(anchor, 1_000_000_000_000))
    chk("+1s of monotonic maps to +1s utc", 1_785_000_001.0,
        to_utc(anchor, 1_000_000_000_000 + 1_000_000_000))
    chk("-2s of monotonic maps to -2s utc", 1_784_999_998.0,
        to_utc(anchor, 1_000_000_000_000 - 2_000_000_000))
    chk("+140.56s (the measured MLB pack-lag scale) round-trips",
        1_785_000_140.56, round(to_utc(anchor, 1_000_000_000_000 + 140_560_000_000), 2))

    # A real sample() call must actually work on this host and produce a
    # plausible-looking pair (utc_s within a sane range, raw_monotonic_ns
    # positive) — this is the thing that could silently fail on a platform
    # without CLOCK_MONOTONIC_RAW exposed to Python.
    real = sample()
    chk("sample() utc_s is a plausible unix timestamp", True,
        1_700_000_000 < real["utc_s"] < 2_000_000_000)
    chk("sample() raw_monotonic_ns is positive", True,
        real["raw_monotonic_ns"] > 0)
    chk("sample() carries the warning field", True, bool(real.get("warning")))

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", action="store_true", help="take and print/write one anchor")
    ap.add_argument("--out", default=None, help="write the anchor JSON here instead of stdout")
    ap.add_argument("--to-utc", metavar="ANCHOR_JSON", help="convert a capture_ns using this anchor file")
    ap.add_argument("capture_ns", nargs="?", type=int, help="MonotonicNs value to convert (with --to-utc)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()

    if a.sample:
        anc = sample()
        text = json.dumps(anc, indent=2)
        if a.out:
            with open(a.out, "w") as f:
                f.write(text)
            print(f"wrote {a.out}")
        else:
            print(text)
        return 0

    if a.to_utc:
        with open(a.to_utc) as f:
            anc = json.load(f)
        if a.capture_ns is None:
            print("FATAL: capture_ns positional argument required with --to-utc", file=sys.stderr)
            return 2
        utc = to_utc(anc, a.capture_ns)
        print(f"{utc}  ({datetime.fromtimestamp(utc, timezone.utc).isoformat()})")
        return 0

    print("FATAL: one of --sample / --to-utc / --self-test required", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
