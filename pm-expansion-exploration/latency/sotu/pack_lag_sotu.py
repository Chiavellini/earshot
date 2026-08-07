#!/usr/bin/env python3
"""pack_lag_sotu.py — how far behind (or ahead of) the real utterance does
the Polymarket "What will Trump say during the State of the Union address?"
trading pack react? Zero live capture; two free public sources only
(YouTube auto-captions of the C-SPAN broadcast + PM's public trades
endpoint), joined via slope-fit, same discipline as scripts/wnt-pack-lag.py
(production, read-only reference, never imported/modified here).

EVENT: 2026 State of the Union address, Donald Trump, 2026-02-24, House
Chamber. Multiple public sources (WebSearch, 2026-08-06) agree the address
began 9:12pm EST (2026-02-25T02:12:00Z) and ran 1h47m40s (6460s).
Polymarket event: what-will-trump-say-during-the-state-of-the-union-address
(closed, $3.41M total volume, 44 strikes).

LEG 1 — VOD: C-SPAN's YouTube upload "President Trump 2026 State of the
Union Address & Democratic Response" (pjGvA-D0Fcs, 7986s, was_live).
**ANCHOR CAVEAT, stated plainly rather than hidden**: this video's
`release_timestamp` (2026-02-25T00:01:00Z) implies the recording starts
2h11m before the speech itself — but the caption content actually begins at
t=99.84s with "Members of Congress, I..." (immediately followed by
"Speaker" at t=138.68s — consistent with an opening "Mr. Speaker" address),
which contradicts a 2-hour pre-show gap. release_timestamp is NOT trusted
here; instead t0 is estimated as
known_speech_start_utc (02:12:00Z) - 99.84s ≈ 02:10:20Z, i.e. assuming the
video begins ~100s before Trump's first substantive words. This is a real,
unverified assumption (it doesn't account for any applause/entrance/
procedural-announcement time before speech proper began) — the slope_fit's
own slope≈1.0 check is the actual validator of whether this anchor is
usable, not this docstring's confidence.

TARGET STRIKES: single-mention (non-count) strikes with real trade volume,
picked from the 44-strike board (count strikes like "America 25+ times",
"Job 20+ times" excluded — same discipline as every prior script in this
program, since a caption track cannot reliably reproduce which occurrence
was the Nth). One deliberately-flagged risk: EPSTEIN's only transcript hit
lands at t=7658s, near the very end of a video that also contains the
Democratic response — this may not be a Trump utterance at all (possible
speaker-attribution contamination, same class of risk flagged in
pack_lag_rogan.py for the Trump/Rogan market). Reported, not silently
trusted.

Usage:
  pack_lag_sotu.py                    # re-run the live join
  pack_lag_sotu.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import slope_fit as sf  # noqa: E402
import pm_sweep as pms  # noqa: E402
from phrase_match import first_occurrence  # noqa: E402

CORPUS = Path(__file__).resolve().parent / "corpus" / "20260224"

KNOWN_SPEECH_START_UTC = datetime(2026, 2, 25, 2, 12, 0, tzinfo=timezone.utc)
ANCHOR_OFFSET_S = 99.84  # see module docstring — an estimate, not a measured fact
T0 = KNOWN_SPEECH_START_UTC - timedelta(seconds=ANCHOR_OFFSET_S)

# (strike_label, condition_id, [alternative words], risk_note)
TARGETS = [
    ("EPSTEIN", "0xf76ffde3888ec2a602c9cedf469b5324cd638d26b4f113d05093cddf51e42754",
     ["epstein"], "ONLY hit at t=7658s, near video end -- possible Dem-response contamination"),
    ("ALIEN", "0xbed351bb30927153045ccbab822757e8ec08e8c9456d478e13186ac241e593c1",
     ["alien"], None),
    ("HOTTEST", "0x8ead7f9e65789ff96181381994ed0a9da8096d85f3786c13690c1d0b8842d65f",
     ["hottest"], None),
    ("HOCKEY", "0x3ef9ac143b71fcf8c193fef5f407c70ef97f9dd2e71b68a979330749615cb9fa",
     ["hockey"], None),
    ("EGG", "0xa947253e4bbf22197ab9be3b6d80f22bd45fb8f9d98f905b7e872b7610ef747a",
     ["egg"], None),
    ("KAMALA", "0xf253a04edf606c610e291262bc7eb5a15020b6a0a61f4c72b8bc6de84eab6765",
     ["kamala", "harris"], None),
]


def run():
    words = json.loads((CORPUS / "words.json").read_text())
    rows = []
    pairs = []
    for label, cid, alts, risk in TARGETS:
        offset_s = first_occurrence(words, alts)
        if offset_s is None:
            rows.append({"strike": label, "reason": "word never appears in transcript"})
            continue
        utterance_utc = T0 + timedelta(seconds=offset_s)
        tr = pms.trades(cid)
        onset, why = pms.sweep_onset(tr, utterance_utc, hold_window_min=180.0,
                                      window_before_min=60 * 24 * 30, window_after_min=60 * 24 * 3)
        row = {"strike": label, "programme_offset_s": round(offset_s, 2),
               "utterance_utc": utterance_utc.isoformat(), "n_trades": len(tr)}
        if risk:
            row["risk_note"] = risk
        if onset is None:
            row["reason"] = why
            rows.append(row)
            continue
        delta = (onset - utterance_utc).total_seconds()
        row["sweep_utc"] = onset.isoformat()
        row["delta_s"] = round(delta, 2)
        rows.append(row)
        pairs.append((offset_s, (onset - T0).total_seconds()))

    result = {"t0_utc": T0.isoformat(), "anchor_offset_s": ANCHOR_OFFSET_S,
              "anchor_caveat": "estimated, not measured -- see module docstring",
              "rows": rows, "n_pairs": len(pairs)}
    if len(pairs) >= 3:
        result["fit"] = sf.fit(pairs, min_fit_points=3, min_fit_span_s=60.0)
    else:
        result["fit"] = {"verdict": "NOT-RESOLVABLE",
                          "reason": f"only {len(pairs)} matched pair(s) — need >=3 for a slope-fit"}
    return result


def self_test():
    p = f = 0

    def chk(name, want, got):
        nonlocal p, f
        if want == got:
            print(f"  PASS  {name}")
            p += 1
        else:
            print(f"  FAIL  {name} (expected {want!r}, got {got!r})")
            f += 1

    print("pack_lag_sotu self-test (matcher logic only, no network):")
    words = [
        {"word": "Members", "start": 0.0}, {"word": "Speaker,", "start": 5.0},
        {"word": "hottest", "start": 10.0}, {"word": "Egg", "start": 20.0},
    ]
    chk("finds first occurrence, punctuation stripped", 5.0, first_occurrence(words, ["speaker"]))
    chk("case-insensitive match", 20.0, first_occurrence(words, ["egg"]))
    chk("returns None when absent", None, first_occurrence(words, ["alien"]))
    chk("TARGETS table has 6 real-looking strikes", 6, len(TARGETS))
    for label, cid, alts, risk in TARGETS:
        chk(f"{label} conditionId looks like a real PM condition id", True,
            cid.startswith("0x") and len(cid) > 40)
    chk("T0 is before the known speech start (anchor subtracts, not adds)",
        True, T0 < KNOWN_SPEECH_START_UTC)

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    result = run()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
