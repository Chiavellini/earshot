#!/usr/bin/env python3
"""pack_lag_keynote.py — how far behind a real utterance does the
Polymarket NVIDIA-keynote trading pack react? Zero live capture; two free
public sources only (YouTube auto-captions + PM's public trades endpoint),
joined via slope-fit, same discipline as scripts/wnt-pack-lag.py (production,
read-only reference, never imported/modified here).

WHY THIS VENUE: picked specifically to isolate whether Rogan's murky result
(pack_lag_rogan.py) was about the "podcast mention market" CLASS, or about
JRE's specific multi-speaker/no-diarization confound. This is a
SINGLE-SPEAKER keynote (Jensen Huang solo on stage) — if it still shows
non-audio-beatable behavior, the class-level explanation is wrong; if it
shows a clean slope=1.0 join like WNT/MLB/WNBA, diarization was the real
confound in Rogan's case, not "podcasts in general."

MARKET / VOD: "What will Jensen Huang say during NVIDIA Keynote?"
(NVIDIA Live at CES 2026, 2026-01-05), event slug
what-will-jensen-huang-say-during-nvidia-keynote, $656K total volume.
Genuinely LOWER VOD-identification risk than every prior venue: Polymarket's
own `resolutionSource` field names the exact video
(https://www.youtube.com/live/0NBILspM4c4) — no channel search, no
duration-window disambiguation, no wrong-program risk at all.

WALL-CLOCK ANCHOR (t0): same situation as TBPN — an archived livestream
(yt-dlp live_status=was_live) with release_timestamp=1767641426
(2026-01-05T19:30:26Z, the real stream-open instant) distinct from
timestamp=1767654258 (2026-01-05T23:04:18Z, post-hoc publish/finalize,
~3.5h later — NOT usable as t0, same lesson as TBPN's release_timestamp
vs timestamp distinction).

A real wrinkle, stated plainly: release_timestamp is when the STREAM opened
(a "Ready? Go." countdown at 30.6s), not when Jensen actually started
speaking ("Welcome to the stage... Jensen Huang" at 237.3s) — nor does
either land near the event description's stated "4 PM ET" (21:00 UTC)
start; the real first speech is ~19:34 UTC, ~1.5h earlier than advertised.
This is NOT resolved by assumption — t0=release_timestamp is used (same
convention as TBPN) and the slope-fit's own slope≈1.0 requirement is the
check: if this anchor were wrong, the fitted slope would not land near 1.0.

TARGETS: 5 single/short-phrase YES-resolved strikes with real trade volume
(count-strikes like "Million/Billion/Trillion 20+ times" and "AI 10+ times"
deliberately excluded — a transcript can't reliably reproduce which
occurrence was the Nth, same discipline every prior venue in this program
applies): TSMC ($16.9K), Grok ($14.9K), Elon/Musk ($13.8K),
Happy New Year ($4.0K), Palantir ($1.7K).

Usage:
  pack_lag_keynote.py                    # re-run the join against banked corpus
  pack_lag_keynote.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import pm_sweep as pms  # noqa: E402
import slope_fit as sf  # noqa: E402
from phrase_match import occurrences  # noqa: E402

CORPUS = Path(__file__).resolve().parent / "corpus" / "20260105"
VIDEO_ID = "0NBILspM4c4"
RELEASE_TIMESTAMP = 1767641426  # verified live 2026-08-06, see module docstring

# (strike_label, condition_id, [[phrase tokens], ...])  -- phrase form lets
# "Happy New Year" be matched as an exact 3-token phrase, not just "happy"
# (which appears 10x in filler contexts the strike does not cover).
TARGET_STRIKES = [
    ("TSMC", "0x6728e73f6de74505107737fa821c8ddb7a841f5ab26e13587d30a519e7c1a695",
     [["tsmc"]]),
    ("GROK", "0x8427c2ae944bb1394023d89ef399ba36c73788901c17787f9a50f19991dfeef7",
     [["grok"]]),
    ("ELON_MUSK", "0x73e3dbc0f7fd7491e9ec4dc4e59860861331c8e87042677814c86deaa1db4970",
     [["elon"], ["musk"]]),
    ("PALANTIR", "0xa5674d5e313f22041cd2054856d2ff2ca81d6ed5219831bc1472d875170dfaed",
     [["palantir"]]),
    ("HAPPY_NEW_YEAR", "0xc9749b5f01f2840e4eda4aefbbb184e5014493616766b129da0f7c95a9391d04",
     [["happy", "new", "year"]]),
]


def load_words(path=None):
    path = path or (CORPUS / "words.json")
    return json.load(open(path))


def run():
    words = load_words()
    t0 = datetime.fromtimestamp(RELEASE_TIMESTAMP, timezone.utc)
    rows = []
    pairs = []
    for label, cid, phrases in TARGET_STRIKES:
        occs = occurrences(words, phrases, fuzzy=False)
        if not occs:
            rows.append({"strike": label, "verdict": "NOT-RESOLVABLE",
                         "reason": "phrase never appears in the transcript"})
            continue
        first_off, how = occs[0]
        utterance_utc = t0 + timedelta(seconds=first_off)
        tr = pms.trades(cid)
        onset, why = pms.sweep_onset(tr, utterance_utc, hold_window_min=60.0,
                                      window_before_min=180.0, window_after_min=300.0)
        if onset is None:
            rows.append({"strike": label, "first_occurrence_s": round(first_off, 2),
                         "utterance_utc": utterance_utc.isoformat(),
                         "verdict": "NOT-RESOLVABLE", "reason": why, "n_trades": len(tr)})
            continue
        delta = (onset - utterance_utc).total_seconds()
        rows.append({
            "strike": label,
            "first_occurrence_s": round(first_off, 2),
            "utterance_utc": utterance_utc.isoformat(),
            "sweep_utc": onset.isoformat(),
            "delta_s": round(delta, 2),
            "n_trades": len(tr),
            "verdict": "ok",
        })
        pairs.append((first_off, (onset - t0).total_seconds()))

    fit = sf.fit(pairs, min_fit_points=3, min_fit_span_s=60.0) if len(pairs) >= 3 else {
        "verdict": "NOT-RESOLVABLE",
        "reason": f"only {len(pairs)} matched pair(s) — need >=3 for a slope-fit",
    }
    result = {
        "event_slug": "what-will-jensen-huang-say-during-nvidia-keynote",
        "video_id": VIDEO_ID,
        "t0_utc": t0.isoformat(),
        "rows": rows,
        "fit": fit,
        "n_pairs": len(pairs),
        "finding": (
            "Only 2/5 strikes produced a usable (utterance, sweep) pair -- not "
            "enough to slope-fit (need >=3) -- but inspecting the RAW trade "
            "tapes for all 5 (not just the black-box sweep_onset verdict) shows "
            "a coherent pattern: GROK/ELON_MUSK/PALANTIR were all already "
            "trading at 0.82-0.96 a full DAY before the keynote even happened "
            "(2026-01-04, keynote was 2026-01-05) -- the same base-rate "
            "pre-pricing phenomenon as Rogan's CRAZY and MLB's Grand Slam/Walk "
            "Off (common-enough industry topics get priced off priors, not off "
            "the actual utterance). The two strikes that DID show a clean "
            "utterance timestamp (TSMC at 77min into the show, HAPPY_NEW_YEAR "
            "at 4min in -- 73 minutes apart) both settled in a tight window "
            "AFTER the keynote ended (~21:02 UTC): 21:33 and 22:33 UTC, only "
            "60 minutes apart despite their utterances being 73 minutes apart "
            "-- echoing TBPN's end-of-show batch-settlement pattern. This is a "
            "SINGLE-SPEAKER venue (Jensen Huang solo), which rules out "
            "multi-speaker diarization as Rogan's confound -- the same two "
            "failure modes (base-rate pre-pricing + post-event batch "
            "settlement) show up here with a clean single speaker, suggesting "
            "this is a general pattern for lower-liquidity 'long-tail' "
            "mention markets, not a JRE-specific or diarization-specific "
            "artifact. Classification: EMPTY-PACK / GAMED (mixed) -- not "
            "CONTESTABLE, not UNBEATABLE -- reported honestly as "
            "under-powered (n=2) rather than forced into a slope number."
        ),
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

    print("pack_lag_keynote self-test (matcher logic only, no network):")

    chk("TARGET_STRIKES has 5 real-looking strikes", 5, len(TARGET_STRIKES))
    for label, cid, phrases in TARGET_STRIKES:
        chk(f"{label} conditionId looks like a real PM condition id",
            True, cid.startswith("0x") and len(cid) > 40)

    fake_words = [
        {"word": "Ready?", "start": 30.6, "end": 30.9},
        {"word": "Happy", "start": 254.0, "end": 254.3},
        {"word": "New", "start": 254.4, "end": 254.6},
        {"word": "Year.", "start": 254.7, "end": 255.0},
        {"word": "happy", "start": 660.4, "end": 660.6},  # filler use, not the phrase
        {"word": "TSMC's", "start": 4632.24, "end": 4632.6},
    ]
    occs = occurrences(fake_words, [["happy", "new", "year"]], fuzzy=False)
    chk("exact 3-token phrase match finds the real greeting (offset = END of "
        "the LAST token, 'Year.'), not the filler 'happy'",
        [(255.0, "exact")], occs)
    occs = occurrences(fake_words, [["tsmc"]], fuzzy=False)
    chk("possessive form matches via norm()", [(4632.6, "exact")], occs)

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
