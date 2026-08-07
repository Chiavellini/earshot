#!/usr/bin/env python3
"""pack_lag_netanyahu.py — how far behind a real Netanyahu utterance is the
KXNETANYAHUMENTION pack already reacting? Zero live audio capture; two free
public sources only (Fox News's own closed-caption VTT + Kalshi's public
trade tape), joined via slope-fit, same discipline as scripts/wnt-pack-lag.py
(production, read-only reference, never imported/modified here).

CANDIDATE SELECTION, stated plainly. The plan's original candidate,
`KXNETANYAHUMENTION-26JUL19`, was tried first. `KXVANCEMENTION-26MAY29` (the
plan's own fallback) was checked and rejected: every real strike settled
"no" while `NQE` ("Event does not qualify") settled "yes" — the event never
happened, so there is no real utterance to find a leg-1 reference for.
Returned to Netanyahu. `KXNETANYAHUMENTION-26JUL19`'s underlying real-world
event is Fox News's "Sunday Morning Futures with Maria Bartiromo" — but the
ticker's "26JUL19" does NOT match the episode's actual air date. Confirmed
live (WebSearch + yt-dlp metadata): Netanyahu's actual appearance aired
2026-07-26 (`timestamp: 1785102535` = 2026-07-26T21:48:55Z per yt-dlp's
extraction of https://www.foxnews.com/video/6402195227112), corroborated by
a real quote match (the show's own X/Twitter account: "If Iran attacks
Israel... Israel's response will be very, very forceful" — consistent with
the NUCL/TERR/HAMA/DRON word set actually settling YES on this Kalshi
event). Kalshi's ticker date likely reflects when the market was LISTED,
not the guest's actual air date — the same kind of ticker-date vs.
real-schedule gap already found for other series in this program
(schedule-facts.md's KXFTNMENTION discrepancy). Treated as the correct
event because the slope-fit itself is the real check: a wrong VOD would
show a slope far from 1.0, and this run does not.

LEG 1 IS A GENUINE STEP UP IN QUALITY, worth flagging for future venues:
Fox News's video page carries PROFESSIONAL closed captions (extracted via
yt-dlp's native `foxnews` extractor), not YouTube ASR — no spelling-drift
risk, though captions are phrase-level (short scrolling cues), not
strictly word-timestamped, so word offsets within a cue are interpolated
(see vtt_parse.py). t0 = the video's publish timestamp (VOD-anchored, same
convention pack_lag_rogan.py uses for a pre-recorded asset, not a
live-broadcast anchor like WNT's fixed daily air time — this IS a caveat:
if Fox published the VOD non-trivially after the live segment aired, t0 is
late and every offset below is an UNDERSTATEMENT of the true reaction lag).

TARGET STRIKES: the 6 single-mention YES-settled strikes (of 14 total; NQE
excluded as a non-occurrence catch-all, TRUM excluded as a "(3+ times)"
count-strike — matching this program's standing count-strike discipline).

Usage:
  pack_lag_netanyahu.py                    # re-run the join against banked corpus
  pack_lag_netanyahu.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kalshi_sweep as ks  # noqa: E402
import slope_fit as sf  # noqa: E402
from phrase_match import all_occurrences  # noqa: E402

CORPUS = Path(__file__).resolve().parent / "corpus" / "netanyahu-20260726"
KALSHI_EVENT = "KXNETANYAHUMENTION-26JUL19"
VIDEO_TIMESTAMP = 1785102535  # yt-dlp `timestamp`, verified live 2026-08-06

# strike_code -> accepted alternative words. Single-mention YES strikes only
# (confirmed via GET /markets?event_ticker=KXNETANYAHUMENTION-26JUL19,
# 2026-08-06): NQE and TRUM (a count strike) excluded.
TARGET_STRIKES = {
    "GAZA": ["gaza", "palestine"],
    "LEBA": ["lebanon"],
    "NUCL": ["nuclear"],
    "TERR": ["terrorist", "terrorism"],
    "DRON": ["drone"],
    "HAMA": ["hamas", "hezbollah"],
}


def load_words(path=None):
    path = path or (CORPUS / "words.json")
    return json.load(open(path))


def matched_pairs(words=None, t0=None):
    """Same dual-anchor reporting as pack_lag_tbpn.py: first-occurrence and
    last-occurrence-before-the-sweep, since it's not obvious in advance
    which a given pack actually reacts to."""
    words = words if words is not None else load_words()
    t0 = t0 or datetime.fromtimestamp(VIDEO_TIMESTAMP, timezone.utc)
    rows = []
    pairs_first = []
    pairs_last_prior = []
    for code, alts in TARGET_STRIKES.items():
        occs = all_occurrences(words, alts, fuzzy=True)
        if not occs:
            rows.append({"strike": code, "verdict": "NOT-RESOLVABLE",
                         "reason": "none of the alternatives appear in the transcript"})
            continue
        ticker = f"{KALSHI_EVENT}-{code}"
        tr = ks.trades(ticker)
        # A 41-minute cable interview, not a 22-minute newscast or a 3-hour
        # game/podcast -- window sized in between, bounded hold-check per
        # the WNBA/TBPN precedent (thin post-event trading risk).
        onset, why = ks.sweep_onset(tr, t0, window_after_min=90.0, hold_window_min=45.0)
        if onset is None:
            rows.append({"strike": code, "occurrences_s": [round(o, 2) for o in occs],
                         "verdict": "NOT-RESOLVABLE", "reason": why, "n_trades": len(tr)})
            continue
        sweep_off = (onset - t0).total_seconds()
        first_off = occs[0]
        prior = [o for o in occs if o <= sweep_off]
        last_prior_off = prior[-1] if prior else None
        rows.append({
            "strike": code,
            "occurrences_s": [round(o, 2) for o in occs],
            "sweep_offset_s": round(sweep_off, 2),
            "sweep_wallclock": onset.isoformat(),
            "first_occurrence_delta_s": round(sweep_off - first_off, 2),
            "last_prior_occurrence_delta_s": (round(sweep_off - last_prior_off, 2)
                                               if last_prior_off is not None else None),
            "n_trades": len(tr),
            "verdict": "ok",
        })
        pairs_first.append((first_off, sweep_off))
        if last_prior_off is not None:
            pairs_last_prior.append((last_prior_off, sweep_off))
    return rows, pairs_first, pairs_last_prior


def run():
    words = load_words()
    rows, pairs_first, pairs_last_prior = matched_pairs(words)
    fit_first = sf.fit(pairs_first, min_fit_points=3, min_fit_span_s=120.0)
    fit_last_prior = sf.fit(pairs_last_prior, min_fit_points=3, min_fit_span_s=120.0)
    result = {
        "event": KALSHI_EVENT,
        "real_world_event": "Fox News Sunday Morning Futures w/ Maria Bartiromo, aired 2026-07-26",
        "video_source": "https://www.foxnews.com/video/6402195227112",
        "t0_utc": datetime.fromtimestamp(VIDEO_TIMESTAMP, timezone.utc).isoformat(),
        "rows": rows,
        "fit_using_first_occurrence": fit_first,
        "fit_using_last_prior_occurrence": fit_last_prior,
        "n_pairs": len(pairs_first),
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

    print("pack_lag_netanyahu self-test:")

    chk("TARGET_STRIKES covers the 6 single-mention YES strikes",
        {"GAZA", "LEBA", "NUCL", "TERR", "DRON", "HAMA"}, set(TARGET_STRIKES.keys()))
    chk("TRUM (count-strike) deliberately excluded", False, "TRUM" in TARGET_STRIKES)
    chk("NQE (non-occurrence catch-all) deliberately excluded", False, "NQE" in TARGET_STRIKES)

    fake_words = [
        {"word": "Israeli", "start": 0.0, "end": 0.3},
        {"word": "response", "start": 1.0, "end": 1.4},
        {"word": "would", "start": 2.0, "end": 2.2},
        {"word": "be", "start": 2.5, "end": 2.6},
        {"word": "forceful", "start": 3.0, "end": 3.5},
        {"word": "against", "start": 10.0, "end": 10.3},
        {"word": "Hezbollah", "start": 11.0, "end": 11.6},
        {"word": "and", "start": 12.0, "end": 12.2},
        {"word": "Hamas", "start": 13.0, "end": 13.4},
    ]
    occs = all_occurrences(fake_words, TARGET_STRIKES["HAMA"], fuzzy=True)
    chk("all_occurrences finds both alternatives for HAMA, sorted",
        [11.6, 13.4], occs)
    occs = all_occurrences(fake_words, TARGET_STRIKES["DRON"], fuzzy=True)
    chk("all_occurrences returns [] when the word never appears", [], occs)

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
