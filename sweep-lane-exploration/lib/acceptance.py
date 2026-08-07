#!/usr/bin/env python3
"""acceptance.py — does the sweep primitive reproduce onsets the repo already documents?

This is the gate that decides whether `sweep_segment` is measuring the same
thing the rest of the program means by "sweep". It runs against real banked
tapes in `state/` (read-only), not fixtures.

WHAT IT FOUND (2026-08-06), and why the expected set looks the way it does:

The repo's per-tape notes quote two DIFFERENT quantities under one word.

  * For genuinely heard mentions the quoted time IS the repricing onset, to
    the millisecond. `sweep_segment` reproduces all seven of those exactly:
    MS INVE/TAIL/RECE (COMPETITOR-ANATOMY.md wave-1 table) and WNT
    IRAQ/BLAN/MAIN/MIDT (VERIFICATIONS.md Q-WNT-CLOSE).

  * For the wrong-way "basket sweeps" the quoted time is NOT an onset. It is
    the first print >=50c during the call, on a market that had ALREADY been
    repriced before the call off the press release. Measured here:

        AAL  CHIC  97c at 12:22:29Z  (doc calls 12:34:39Z its sweep; call 12:30Z)
        AAL  WORL  66c at 12:27:37Z  (doc: 12:33:13Z)
        AAL  CENT  55c at 12:27:01Z  (doc: 12:33:00Z)
        INTC MOBI  75c at 20:53:54Z  (doc: 21:01-03Z; call 21:00Z)
        INTC GOVE  66c at 20:46:53Z  (doc: 21:01-03Z)
        INTC ARIZ  61c at 20:48:52Z  (doc: 21:01-03Z)

    No repricing happens at the quoted times, so a repricing detector must
    NOT fire there. This file asserts that it does not — the refusals are
    part of the contract, not a shortfall against it.

Consequence for the study, carried into PRE-REGISTRATION.md: "swept to X" and
"resting at X" are different populations and must be scored as different arms.
The provisional pre-study numbers conflated them.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import sweep_segment as ss  # noqa: E402
import windows  # noqa: E402

# Onsets the repo documents as real repricing events, with their source.
DOCUMENTED_ONSETS = {
    "KXEARNINGSMENTIONMS-26JUL15": (
        "docs/COMPETITOR-ANATOMY.md wave-1 table",
        {"INVE": "12:42:28.287", "TAIL": "12:43:26.504", "RECE": "13:27:10.273"},
    ),
    "KXWORLDNEWSMENTION-26JUL29": (
        "VERIFICATIONS.md Q-WNT-CLOSE",
        {"IRAQ": "22:31:15.325", "BLAN": "22:38:16.018", "MAIN": "22:40:29.432",
         "MIDT": "22:43:42.471"},
    ),
}

# Markets the repo calls "swept" that were in fact pre-priced before the call.
# The contract is NOT "these never sweep" — a pre-priced market can still be
# repriced later, and AAL WORL genuinely is (12:37:45Z 68->84c, 12:42:26Z
# 63->83c, both settled NO). The contract is that no episode fires AT THE
# DOCUMENTED TIME, because nothing repriced there.
# (strike -> (resting price before call, time the doc calls its sweep))
DOCUMENTED_PREPRICED = {
    "KXEARNINGSMENTIONAAL-26JUL23": {
        "CHIC": (97, "12:34:39"), "WORL": (66, "12:33:13"), "CENT": (55, "12:33:00")},
    "KXEARNINGSMENTIONINTC-26JUL23": {
        "MOBI": (75, "21:02:00"), "GOVE": (66, "21:02:00"), "ARIZ": (61, "21:01:00")},
}

TOLERANCE_S = 1.0


def load_tape(event):
    """Banked tapes come in two schemas; both are read-only inputs."""
    paths = glob.glob(os.path.join(REPO, "state", event, "tape-*.json"))
    if not paths:
        return None
    d = json.load(open(sorted(paths)[-1]))
    if isinstance(d, dict) and isinstance(d.get("trades"), dict):
        return d["trades"]
    return d  # older bare {ticker: [trades]} schema (MS 26JUL15)


def run():
    p = f = 0

    def chk(name, ok, detail=""):
        nonlocal p, f
        if ok:
            print(f"  PASS  {name}{detail}")
            p += 1
        else:
            print(f"  FAIL  {name}{detail}")
            f += 1

    print("acceptance vs documented onsets (real banked tapes):")

    for event, (source, want) in DOCUMENTED_ONSETS.items():
        tape = load_tape(event)
        if tape is None:
            chk(f"{event}: tape present", False, "  (no banked tape)")
            continue
        start, end, _ = windows.window(event)
        eps = ss.event_episodes(tape, window=(start, end) if start else None)
        got = {}
        for e in eps:
            got.setdefault(e.ticker.split("-")[-1], []).append(e)
        print(f"\n  {event}  [{source}]")
        for strike, doc in sorted(want.items(), key=lambda kv: kv[1]):
            target = datetime.strptime(f"2026-01-01 {doc}", "%Y-%m-%d %H:%M:%S.%f").time()
            best = None
            for e in got.get(strike, []):
                d = abs((e.onset.hour * 3600 + e.onset.minute * 60 + e.onset.second
                         + e.onset.microsecond / 1e6)
                        - (target.hour * 3600 + target.minute * 60 + target.second
                           + target.microsecond / 1e6))
                if best is None or d < best[0]:
                    best = (d, e)
            ok = best is not None and best[0] <= TOLERANCE_S
            detail = (f"  doc {doc} -> got {best[1].onset:%H:%M:%S.%f} "
                      f"({best[1].entry}->{best[1].terminal}c x{best[1].contracts:.0f})"
                      if best else "  no episode found")
            chk(f"{strike} onset reproduced", ok, detail)

    print("\n  pre-priced markets a repricing detector must REFUSE:")
    for event, want in DOCUMENTED_PREPRICED.items():
        tape = load_tape(event)
        start, end, _ = windows.window(event)
        call = start + windows.WINDOW_BEFORE if start else None
        tag = event.split("MENTION")[1]
        for strike, (level, doc_time) in sorted(want.items()):
            ticker = f"{event}-{strike}"
            rows = tape.get(ticker, [])
            resting = ss.resting_price(rows, call) if call else None
            eps = ss.episodes(ticker, rows, window=(start, end) if start else None)
            chk(f"{tag}/{strike} was pre-priced",
                resting is not None and resting >= ss.PREPRICED_CENTS,
                f"  resting {resting}c before call (doc {level}c)")
            hh, mm, sec = (int(x) for x in doc_time.split(":"))
            at_doc = [e for e in eps
                      if abs((e.onset.hour * 3600 + e.onset.minute * 60 + e.onset.second)
                             - (hh * 3600 + mm * 60 + sec)) <= 60]
            other = [f"{e.onset:%H:%M:%S}" for e in eps]
            chk(f"{tag}/{strike} does not fire at the doc's {doc_time}",
                not at_doc,
                f"  {len(eps)} episode(s){' at ' + ','.join(other) if other else ''}")

    print()
    if f:
        print(f"ACCEPTANCE FAIL ({f} failed, {p} passed)")
        return 1
    print(f"ACCEPTANCE PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(run())
