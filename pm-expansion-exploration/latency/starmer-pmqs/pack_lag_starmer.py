#!/usr/bin/env python3
"""pack_lag_starmer.py — how far behind a real PMQs utterance does the
Polymarket "What will Keir Starmer say at PMQs" pack react? Zero live
capture; two free public sources (UK Parliament's official verbatim
record + Polymarket's public trades endpoint), joined via slope-fit, same
discipline as scripts/wnt-pack-lag.py (production, read-only reference,
never imported/modified here) and pm-expansion-exploration/latency/lib/.

TARGET EVENT: the Polymarket event "What will Keir Starmer say at the
next Prime Minister's Questions event?" (slug ...-event-564, closed
2026-04-15, $224,854 volume — confirmed live as the highest-volume of 5+
found instances). PMQs is weekly, Wednesdays, House of Commons.

Real-world date identified from the trade tape itself, not assumed: the
event's markets show real repricing activity clustering 2026-04-12 to
2026-04-15, with the decisive settlement burst on 2026-04-15 — confirmed
a Wednesday. This is "the next PMQs" the market resolved on.

LEG 1 — Hansard/TheyWorkForYou, NOT raw YouTube auto-captions. UK
Parliament's official record (hansard.parliament.uk) is bot-walled (403
on direct fetch); TheyWorkForYou.com mirrors the same verbatim Hansard
text with no ASR uncertainty at all (word-perfect, human-transcribed) —
categorically better ground truth than any caption track used elsewhere
in this investigation. Fetched: https://www.theyworkforyou.com/debates/?id=2026-04-15f.816.6
("Engagements" — Hansard's standing name for the PMQs opening question),
104 paragraphs, 6620 words.

THE REAL LIMITATION, stated plainly: Hansard/TheyWorkForYou's text has NO
per-utterance timestamps (checked directly — no time markers exist on the
page). Unlike WNT (fixed air time), MLB/WNBA (official play-by-play
timestamps), or even TBPN (a video seek-bar), there is no way to recover
exactly when in the session each word was spoken from the transcript
alone. **This script approximates each word's utterance time via LINEAR
INTERPOLATION across the session's estimated real-world window**
(word_index / total_words) * session_duration, added to an assumed
session start of 11:00 UTC (12:00pm BST, PMQs' standing weekly slot).
Session duration is estimated at 2640s (44 min) from the transcript's own
6620-word length at ~150 words/minute (a standard parliamentary speaking
pace) rather than assumed — this estimate could be wrong by several
minutes. This is a REAL, ADDITIONAL source of anchor error beyond every
other venue tested so far, and it is exactly what slope_fit's own
slope≈1.0 check is for: if this linear-interpolation approximation is too
crude to be useful, the fit will refuse rather than report a number built
on a bad clock.

Usage:
  pack_lag_starmer.py                    # re-run the join against banked corpus
  pack_lag_starmer.py --self-test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import pm_sweep as pms  # noqa: E402
import slope_fit as sf  # noqa: E402
from phrase_match import all_occurrences  # noqa: E402

CORPUS = Path(__file__).resolve().parent / "corpus" / "20260415"
EVENT_SLUG = "what-will-keir-starmer-say-at-the-next-prime-ministers-questions-event-564"

# Estimated PMQs session window (see module docstring's "real limitation"
# section) -- 12:00pm BST standing slot, ~44min at ~150 wpm for 6620 words.
SESSION_START_UTC = datetime(2026, 4, 15, 11, 0, 0, tzinfo=timezone.utc)
SESSION_DURATION_S = 2640.0  # 44 min

# strike_code -> (condition_id, accepted alternative words). Only strikes
# CONFIRMED to have settled YES with real trade volume are included --
# checked live 2026-08-06 (final trade price ~0.99-0.999 on all 6).
TARGET_STRIKES = {
    "WAR": ("0xa9ed264715ec413d70dc44a95c7f270bc1a56d9f09d33a792b1565c0ac881e45", ["war"]),
    "TRUM": ("0x1919e3219713a9b4d011000ec46d87c808d227ac81fbaeca7758d2a49fda0743", ["trump"]),
    "NHS": ("0xd4d7af60fd0cd4fc1a6860a4e9c68c07aa44b170f67ec361feec51ede437bb39", ["nhs"]),
    "URGE": ("0x29c8766b2455cb147676e7b6469aca94bd1bec1d5300f622f3e5ae1f776e209d", ["urgent"]),
    "UTUR": ("0x91e0e71acd827f74519c748de4304fd71cdb04dd0dcd4ed3c9b6a1df63191f34", ["uturn", "u-turn"]),
    "OIL": ("0xcf95396b5ef0568316c59128168e0bc96bd5b7b43161d57d6006056b4c940b85", ["oil", "gas"]),
}

WORD_RE = re.compile(r"[a-z0-9]+")


def load_raw_words(path=None):
    path = path or (CORPUS / "raw_words.json")
    return json.load(open(path))


def build_timed_words(raw_words=None):
    """Lift a plain word list into phrase_match's {"word","start","end"}
    shape via linear interpolation across SESSION_START_UTC's window,
    expressed as seconds-from-session-start (matching every other venue's
    "programme_offset_s" convention)."""
    raw_words = raw_words if raw_words is not None else load_raw_words()
    n = len(raw_words)
    out = []
    for i, w in enumerate(raw_words):
        t = (i / n) * SESSION_DURATION_S if n else 0.0
        out.append({"word": w, "start": t, "end": t})
    return out


def matched_pairs(timed_words=None):
    timed_words = timed_words if timed_words is not None else build_timed_words()
    rows = []
    pairs = []
    for code, (cid, alts) in TARGET_STRIKES.items():
        occs = all_occurrences(timed_words, alts)
        if not occs:
            rows.append({"strike": code, "verdict": "NOT-RESOLVABLE",
                         "reason": "none of the alternatives appear in the transcript"})
            continue
        first_off = occs[0]
        utterance_utc = SESSION_START_UTC + timedelta(seconds=first_off)
        tr = pms.trades(cid)
        # PMQs trades were observed spanning well past the session itself
        # (settlement/confirmation trades continuing for hours) -- bound
        # the hold-check the same way WNBA/TBPN's builds needed to.
        onset, why = pms.sweep_onset(tr, utterance_utc, window_before_min=90.0,
                                      window_after_min=300.0, hold_window_min=60.0)
        if onset is None:
            rows.append({"strike": code, "n_occurrences": len(occs),
                         "first_occurrence_s": round(first_off, 2),
                         "utterance_utc_est": utterance_utc.isoformat(),
                         "verdict": "NOT-RESOLVABLE", "reason": why, "n_trades": len(tr)})
            continue
        sweep_off = (onset - SESSION_START_UTC).total_seconds()
        delta = sweep_off - first_off
        rows.append({
            "strike": code,
            "n_occurrences": len(occs),
            "first_occurrence_s": round(first_off, 2),
            "utterance_utc_est": utterance_utc.isoformat(),
            "sweep_offset_s": round(sweep_off, 2),
            "sweep_utc": onset.isoformat(),
            "delta_s": round(delta, 2),
            "n_trades": len(tr),
            "verdict": "ok",
        })
        pairs.append((first_off, sweep_off))
    return rows, pairs


def run():
    rows, pairs = matched_pairs()
    fit = sf.fit(pairs, min_fit_points=3, min_fit_span_s=60.0)
    result = {
        "event_slug": EVENT_SLUG,
        "session_start_utc_estimate": SESSION_START_UTC.isoformat(),
        "session_duration_s_estimate": SESSION_DURATION_S,
        "anchor_caveat": (
            "Leg-1 timestamps are LINEAR-INTERPOLATION ESTIMATES across an "
            "assumed session window, not real per-utterance timestamps "
            "(Hansard/TheyWorkForYou carries no timing data at all). This "
            "is a materially cruder anchor than every other venue tested "
            "in this investigation. Trust the slope check, not the raw "
            "numbers, to judge whether this approximation was good enough."
        ),
        "rows": rows,
        "fit": fit,
        "n_pairs": len(pairs),
    }
    print(json.dumps(result, indent=2, default=str))
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

    print("pack_lag_starmer self-test:")

    chk("TARGET_STRIKES covers all 6 confirmed-YES codes",
        {"WAR", "TRUM", "NHS", "URGE", "UTUR", "OIL"}, set(TARGET_STRIKES.keys()))

    fake_raw = ["the", "war", "in", "ukraine", "and", "trump", "said", "nhs",
                "funding", "is", "urgent", "and", "oil", "prices"]
    timed = build_timed_words(fake_raw)
    chk("build_timed_words assigns increasing offsets", True,
        all(timed[i]["start"] <= timed[i + 1]["start"] for i in range(len(timed) - 1)))
    chk("build_timed_words spans the full session duration", True,
        timed[-1]["start"] < SESSION_DURATION_S)

    occs = all_occurrences(timed, ["war"])
    chk("finds 'war' at word position 1 of 14", True, len(occs) == 1 and occs[0] < timed[-1]["start"])

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
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
