#!/usr/bin/env python3
"""extract_decisions.py — bridge our own decision timestamps onto a wall clock.

READ-ONLY against production artifacts under state/<EVENT>-WEBCAST/. Writes
only inside pm-expansion-exploration/.

Why this exists: `scripts/xvenue-report.py`'s own `mention_anchor()` docstring
says it is "deliberately NOT anchored on our own detections: the arm report
carries `audio_ts`/`decision_ts` as monotonic nanoseconds with no wall-clock
pair anywhere in the run's artifacts, so a detection cannot be placed on a
venue clock at all." This script builds that missing bridge, approximately,
so `reanchor_takeable.py` can test whether OUR OWN reachable-action time
(not Kalshi's reprice) is when Polymarket was actually exploitable.

The bridge: state/<EVENT>-WEBCAST/listener.ready carries {"unix_ts": <whole
second wall clock>} written near pipeline start. report.json carries
`first_capture_ts` on the same MonotonicNs clock as every detection's
audio_ts/decision_ts. bridge_offset_ns = unix_ts*1e9 - first_capture_ts.

This is an APPROXIMATION, not ground truth — two error sources, neither
currently measured:
  1. unix_ts is truncated to the whole second (up to 1s of slop).
  2. The gap between "listener ready" and "first audio frame actually
     captured" is unmeasured.
Every output row carries an explicit bridge_error_s so downstream tools never
present a reanchored timestamp as exact. The real fix — emit one
(MonotonicNs, wall-clock) sample pair from the production arm path itself —
is out of scope here; it would touch src/ and belongs in a normal reviewed
change, not this exploration.

Usage:
  extract_decisions.py --event-dir state/KXEARNINGSMENTIONPLTR-26AUG03-WEBCAST \\
      --out pm-expansion-exploration/captures/PLTR-26AUG03/decisions.json
  extract_decisions.py --self-test
"""

import argparse
import json
import os
import sys

BRIDGE_ERROR_S_DEFAULT = 2.0  # whole-second unix_ts truncation + unmeasured
                               # listener-ready -> first-frame gap. Tighten
                               # only once that gap is actually measured.


def load_report(event_dir):
    with open(os.path.join(event_dir, "report.json")) as f:
        return json.load(f)


def load_listener_ready(event_dir):
    with open(os.path.join(event_dir, "listener.ready")) as f:
        return json.load(f)


def extract(event_dir, bridge_error_s=BRIDGE_ERROR_S_DEFAULT):
    report = load_report(event_dir)
    listener = load_listener_ready(event_dir)
    first_capture_ts = report.get("first_capture_ts")
    unix_ts = listener.get("unix_ts")
    if first_capture_ts is None or unix_ts is None:
        raise SystemExit(
            f"FATAL: missing first_capture_ts (report.json) or unix_ts "
            f"(listener.ready) under {event_dir}")
    bridge_offset_ns = unix_ts * 1_000_000_000 - first_capture_ts

    words = {}
    for d in report.get("detections", []):
        intent = d.get("intent")
        if not intent:
            continue                       # non-decisive leg (backstop/repeat)
        ticker = intent.get("market_ticker")
        if not ticker or intent.get("decision_ts") is None:
            continue
        existing = words.get(ticker)
        # Keep the earliest firing decision per ticker if more than one leg
        # produced an intent for the same word (fanout mode).
        if existing is None or intent["decision_ts"] < existing["decision_ts_mono_ns"]:
            words[ticker] = {
                "market_ticker": ticker,
                "audio_ts_mono_ns": intent["audio_ts"],
                "decision_ts_mono_ns": intent["decision_ts"],
                "detect_latency_us": d.get("detect_latency_us"),
                "tier": intent.get("tier"),
                "leg": d.get("leg"),
                "source": "armed-run",
            }

    return {
        "event_dir": event_dir,
        "first_capture_ts_mono_ns": first_capture_ts,
        "listener_ready_unix_ts": unix_ts,
        "bridge_offset_ns": bridge_offset_ns,
        "bridge_method": ("listener.ready(unix_ts, whole-second wall) - "
                           "report.json.first_capture_ts(mono_ns)"),
        "bridge_error_s": bridge_error_s,
        "bridge_warning": (
            "APPROXIMATION, not ground truth: unix_ts is whole-second-"
            "truncated and the gap between 'listener ready' and 'first audio "
            "frame captured' is unmeasured. Do not treat any reach_anchor_ns "
            f"derived from this bridge as exact; it carries at least "
            f"+/-{bridge_error_s}s of uncertainty."
        ),
        "words": words,
    }


def self_test():
    import tempfile
    p = f = 0

    def chk(name, want, got):
        nonlocal p, f
        if want == got:
            print(f"  PASS  {name}")
            p += 1
        else:
            print(f"  FAIL  {name} (expected {want!r}, got {got!r})")
            f += 1

    print("extract_decisions self-test:")
    with tempfile.TemporaryDirectory() as d:
        report = {
            "first_capture_ts": 1_000_000_000,
            "detections": [
                {"detect_latency_us": 100, "leg": "a",
                 "intent": {"audio_ts": 2_000_000_000, "decision_ts": 2_100_000_000,
                            "market_ticker": "K-WORD1", "tier": "Confirmed"}},
                # A repeat/backstop leg with no intent must be skipped.
                {"detect_latency_us": 50, "leg": "b", "intent": None},
                # A later, slower duplicate for the same ticker must lose to
                # the earlier one already recorded.
                {"detect_latency_us": 500, "leg": "c",
                 "intent": {"audio_ts": 2_000_000_000, "decision_ts": 2_900_000_000,
                            "market_ticker": "K-WORD1", "tier": "Confirmed"}},
            ],
        }
        with open(os.path.join(d, "report.json"), "w") as f_:
            json.dump(report, f_)
        with open(os.path.join(d, "listener.ready"), "w") as f_:
            json.dump({"port": 0, "pid": 1, "unix_ts": 1_000_000_001}, f_)

        out = extract(d)
        chk("bridge_offset_ns computed",
            1_000_000_001 * 1_000_000_000 - 1_000_000_000,
            out["bridge_offset_ns"])
        chk("one word extracted (dup dropped)", 1, len(out["words"]))
        chk("earliest decision_ts wins over the slower duplicate",
            2_100_000_000, out["words"]["K-WORD1"]["decision_ts_mono_ns"])
        chk("bridge_error_s carried through", BRIDGE_ERROR_S_DEFAULT,
            out["bridge_error_s"])
        chk("non-intent leg contributes nothing extra", 1, len(out["words"]))

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--event-dir", help="e.g. state/KXEARNINGSMENTIONPLTR-26AUG03-WEBCAST")
    ap.add_argument("--out", help="decisions.json path to write")
    ap.add_argument("--bridge-error-s", type=float, default=BRIDGE_ERROR_S_DEFAULT)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not a.event_dir or not a.out:
        print("FATAL: --event-dir and --out required (or --self-test)", file=sys.stderr)
        return 2

    result = extract(a.event_dir, a.bridge_error_s)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"wrote {a.out}: {len(result['words'])} word(s) with a fired decision")
    print(f"bridge_offset_ns={result['bridge_offset_ns']} "
          f"bridge_error_s={result['bridge_error_s']}")
    print(f"WARNING: {result['bridge_warning']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
