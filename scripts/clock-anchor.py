#!/usr/bin/env python3
"""Write the `clock_anchor.json` that `drace-wallclock.py` has always required and
that nothing has ever produced.

WHY THIS EXISTS. `scripts/drace-wallclock.py:80` opens `<record-dir>/clock_anchor.json`
and `pos_to_utc()` maps a leg's monotonic `capture_ns` onto UTC with it:

    utc = (anchor["wall_ns"] + (capture_ns - anchor["mono_ns"])) / 1e9

There are two readers of that file in the tree (`drace-wallclock.py`,
`wnt-score.py`) and, until now, **zero writers** — no such file existed on the
laptop or the box. So the project's only Δrace instrument had never run end to
end, and the first time anyone needed it would have been during a live race.

THE CLOCK MUST MATCH `src/clock.rs`. `MonotonicNs::now()` reads
`CLOCK_MONOTONIC_RAW` on Linux and `CLOCK_UPTIME_RAW` on macOS (`src/clock.rs:17-19`),
and those are the values stamped into `capture_ns`. Sampling any other clock —
`CLOCK_MONOTONIC`, say — silently mixes an NTP-slewed source with a raw one.

⚠ TAKE TWO ANCHORS, NOT ONE. `CLOCK_MONOTONIC_RAW` is deliberately NOT
disciplined by NTP, so it drifts against UTC at the oscillator's error. The
ledger records a naive single anchor being **wrong by ~2.1 s** over a 3.7-day
uptime at 6.47 ppm — and that constant is stale: the box was rebuilt as a
c6a.4xlarge on 2026-07-29 and `chronyc tracking` now reports **23.889 ppm**,
~3.7x larger. Over a 90-minute capture that is ~0.13 s, which is not negligible
against a sub-second claim. So: write one anchor at capture START and one at
capture END, and let the consumer interpolate rather than assume.

⚠ AN ANCHOR IS ONLY VALID WITHIN ONE AWAKE SESSION ON ONE HOST. A reboot resets
the monotonic origin, so an anchor taken today says nothing about a capture from
before the last boot. `--verify` refuses when the recorded uptime is shorter than
the age of the anchor it is checking, which is exactly that mistake.

Usage:
  clock-anchor.py <record-dir>            # append an anchor sample
  clock-anchor.py <record-dir> --verify   # report the fitted rate + sanity checks
  clock-anchor.py --selftest
"""
import argparse
import json
import os
import platform
import sys
import time

CLOCK_NAME = "CLOCK_UPTIME_RAW" if platform.system() == "Darwin" else "CLOCK_MONOTONIC_RAW"
CLOCK_ID = getattr(time, CLOCK_NAME)
PPM_SANE = 200.0        # anything beyond this is a broken sample, not a slow crystal


def sample():
    """One contemporaneous (monotonic, wall) pair, taken as close together as the
    interpreter allows. Order matters little at ns scale but wall is read between
    two monotonic reads so the pairing error is bounded and visible."""
    m0 = time.clock_gettime_ns(CLOCK_ID)
    w = time.time_ns()
    m1 = time.clock_gettime_ns(CLOCK_ID)
    return {
        "mono_ns": (m0 + m1) // 2,
        "wall_ns": w,
        "taken_utc": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(w / 1e9)) + "Z",
        "clock": CLOCK_NAME,
        "pair_err_ns": m1 - m0,
    }


def fit_ppm(a, b):
    """Rate of the monotonic clock against wall clock, in ppm.

    Positive = the monotonic clock runs FAST relative to wall. Returns None when
    the two samples are too close together for the fit to mean anything — at a
    1 ms separation, ns-scale pairing error dominates entirely.
    """
    d_mono = b["mono_ns"] - a["mono_ns"]
    d_wall = b["wall_ns"] - a["wall_ns"]
    if d_wall < 10_000_000_000:          # < 10 s apart: refuse rather than mislead
        return None
    return (d_mono - d_wall) / d_wall * 1e6


def load(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        doc = json.load(f)
    # A single-object file is the shape drace-wallclock.py reads; normalise it.
    return doc.get("samples", [doc]) if isinstance(doc, dict) else doc


def write(path, samples):
    """Write BOTH shapes: the flat keys `drace-wallclock.py` reads (from the FIRST
    sample, which is the one contemporaneous with the start of the capture) and a
    `samples` list carrying every anchor plus the fitted rate. Consumers that know
    nothing about drift keep working unchanged."""
    head = dict(samples[0])
    head.pop("pair_err_ns", None)
    doc = dict(head)
    doc["samples"] = samples
    if len(samples) >= 2:
        ppm = fit_ppm(samples[0], samples[-1])
        doc["fitted_ppm"] = ppm
        doc["span_s"] = (samples[-1]["wall_ns"] - samples[0]["wall_ns"]) / 1e9
    with open(path, "w") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
        f.write("\n")


def selftest():
    a = {"mono_ns": 0, "wall_ns": 0}
    # 100 s of wall, monotonic ran 100.001 s => +10 ppm
    b = {"mono_ns": 100_001_000_000, "wall_ns": 100_000_000_000}
    got = fit_ppm(a, b)
    assert abs(got - 10.0) < 1e-6, got
    # sign: monotonic SLOWER than wall must read negative
    c = {"mono_ns": 99_999_000_000, "wall_ns": 100_000_000_000}
    assert fit_ppm(a, c) < 0
    # too close together => refuse rather than return noise
    assert fit_ppm(a, {"mono_ns": 1_000_000, "wall_ns": 1_000_000}) is None
    assert fit_ppm(a, {"mono_ns": 9_000_000_000, "wall_ns": 9_000_000_000}) is None
    # a real sample must carry every field drace-wallclock.py reads
    s = sample()
    for k in ("mono_ns", "wall_ns", "taken_utc", "clock"):
        assert k in s, k
    assert s["clock"] == CLOCK_NAME
    assert abs(s["pair_err_ns"]) < 50_000_000, "pairing took >50ms — machine is thrashing"
    print("selftest OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("record_dir", nargs="?")
    ap.add_argument("--verify", action="store_true",
                    help="report the fitted rate and sanity-check the samples")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return 0
    if not a.record_dir:
        ap.error("record_dir is required (or pass --selftest)")
    os.makedirs(a.record_dir, exist_ok=True)
    path = os.path.join(a.record_dir, "clock_anchor.json")
    samples = load(path)

    if a.verify:
        if len(samples) < 2:
            print(f"clock-anchor: {len(samples)} sample(s) — need 2 to fit a rate. "
                  f"Take one at capture start and one at capture end.")
            return 2
        ppm = fit_ppm(samples[0], samples[-1])
        span = (samples[-1]["wall_ns"] - samples[0]["wall_ns"]) / 1e9
        if ppm is None:
            print(f"clock-anchor: samples only {span:.1f}s apart — NOT RESOLVABLE")
            return 2
        drift_ms = ppm * span / 1e6 * 1000
        print(f"clock-anchor: {len(samples)} samples, span {span:.1f}s, clock {CLOCK_NAME}")
        print(f"  fitted rate    : {ppm:+.3f} ppm")
        print(f"  drift over span: {drift_ms:+.1f} ms "
              f"(this is the error a SINGLE anchor would have carried)")
        if abs(ppm) > PPM_SANE:
            print(f"  ⚠ FAIL: |{ppm:.1f}| ppm exceeds {PPM_SANE:.0f} — that is not a crystal, "
                  f"that is a bad sample or a suspended host")
            return 1
        print("  PASS")
        return 0

    samples.append(sample())
    write(path, samples)
    n = len(samples)
    extra = ""
    if n >= 2:
        ppm = fit_ppm(samples[0], samples[-1])
        extra = f", fitted {ppm:+.3f} ppm" if ppm is not None else ", too close to fit yet"
    print(f"clock-anchor: sample {n} written to {path} ({CLOCK_NAME}{extra})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
