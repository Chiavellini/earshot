#!/usr/bin/env python3
"""pack_lag_rogan.py — how far behind (or ahead of) a real utterance does the
Polymarket JRE-mention trading pack react? Zero live capture; two free
public sources only (YouTube auto-captions + PM's public trades endpoint),
joined via slope-fit, same discipline as scripts/wnt-pack-lag.py (production,
read-only reference, never imported/modified here).

MARKET SELECTION, stated plainly because it wasn't the first candidate
tried. The originally-flagged settled market ("Will Trump say 'trans' on
The Joe Rogan Experience?", Oct 2024) requires SPEAKER ATTRIBUTION — only
Trump's utterances count, and YouTube auto-captions carry no speaker
diarization, so a naive first-occurrence match could easily land on Rogan
or a producer saying "trans" instead. That market was NOT used here.

Used instead: "What will be said on the first Joe Rogan Experience episode
of the week? (July 27)" (Polymarket event, closed 2026-08-02) — every
strike resolves on the word being "mentioned by anyone... any usage of the
term, regardless of context" (verbatim from the market's own rules text),
exactly matching WNT/TBPN's "any commentator says X" shape. The specific
episode was identified as Joe Rogan Experience #2531 (Forrest Galante,
uploaded 2026-07-28) — the one numbered JRE episode released inside the
market's own resolution window, unambiguous (JRE MMA Show is a separately-
excluded series per the market's rules text).

REAL RUN, 2026-08-05, against 3 single-mention strikes with real trade
volume on that event (of ~19 strikes, most are 10+/20+/50+/100+/150+ COUNT
strikes -- deliberately excluded here, same discipline wnt-pack-lag.py
applies to Kalshi's count strikes, since a caption track cannot reproduce
which occurrence was the Nth). ALL THREE came back "pre-priced" -- and
inspecting the raw trade timelines (not just the verdict) turned up a real,
useful finding, more informative than a clean offset would have been:

  CRAZY: repriced to ~0.93 on 2026-07-27 -- a full DAY before this episode
    even published (2026-07-28T17:00:07Z). "Crazy" is said 79x in this one
    episode alone -- the market is pricing the base-rate near-certainty
    that ANY JRE episode says "crazy" eventually, not reacting to this
    episode's content at all. Same phenomenon as MLB's GRAND SLAM/WALK OFF
    finding (pack_lag_mlb.py) -- common-enough words get pre-priced off
    priors, not off the actual utterance.

  ALIEN: price actually DIPS to 0.49-0.69 for 3 days pre-publish (genuine
    uncertainty, unlike CRAZY), then jumps 0.87->0.99 within 2 minutes of
    publish (2026-07-28T17:00:07Z -> 17:02:11Z). Root cause FOUND: the
    video's own YouTube description reads "...his new special, 'Alien
    Sharks: Untamed America,' premieres July 29..." -- the word "alien" is
    sitting in plain text, readable at t=0, with ZERO audio required. A
    pack (bot or human) scanning the description at publish beats ANY
    audio-based detector, however fast, for any word that happens to leak
    into show notes/titles. This is a real, previously-unknown fast channel
    for this venue class -- not a latency number, but arguably a MORE
    important finding for anyone building against JRE-style markets.

  SPY: price also jumps to ~0.99 within ~2 minutes of publish
    (17:00:07Z -> 17:02:20Z) -- but "spy" appears NOWHERE in the video's
    description, tags, or chapters (all checked). The word's actual first
    utterance in the transcript is at content-offset 4910s (81+ minutes
    in) -- nowhere near 2 minutes. This is UNRESOLVED, stated plainly
    rather than guessed at: neither the "audio-utterance" model nor the
    "description-scan" model explains this timing. Left open rather than
    forced into a number that cannot be defended.

READING THE RESULT. This run produced zero usable (utterance, sweep) pairs
-- not a failure of the method, but a real finding the method was built to
surface: pack-lag testing on WNT/MLB/WNBA needed only ONE anchor model
(actual event/utterance time). JRE's weekly word-markets have at least
THREE distinct anchor models in play at once (base-rate prior pricing,
description-text scanning, and a still-unexplained third pattern), and
conflating them into a single "offset" would have been the same mistake
already flagged and refused elsewhere in this program: reporting a number
this data cannot actually defend.

Usage:
  pack_lag_rogan.py                    # re-run the live join
  pack_lag_rogan.py --self-test
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

CORPUS = Path(__file__).resolve().parent / "corpus" / "20260728"

# (strike_label, condition_id, [alternative words])
TARGETS = [
    ("SPY", "0x16835cbdfe56dc2986afb958e9a877212031c095361a0d65f7686c5680abf5d6", ["spy"]),
    ("ALIEN", "0x589fcfd77e62b85425999731c380e6428908fa193c883ee12d3d48aa621ffdf1", ["alien"]),
    ("CRAZY", "0xcf0f1ff41b65d4e357368b03278cb2a0b1b3880442125cb7497a4eacfd21b1be", ["crazy"]),
]

# first_occurrence() now comes from lib/phrase_match.py (the consolidated
# matcher — this script used to carry its own simpler norm()/first_occurrence()
# copy, with no possessive/plural handling; switching to the shared,
# fuller version is behavior-preserving for these 3 targets specifically
# (none of "spy"/"alien"/"crazy" trip norm()'s trailing-s/possessive rules),
# confirmed by re-running live post-refactor and reproducing the original
# banked result bit-for-bit. fuzzy stays False (this script's original
# default) — these are common, unambiguous words with no ASR-spelling risk.


def run(video_upload_utc: datetime):
    words = json.loads((CORPUS / "words.json").read_text())
    rows = []
    pairs = []
    for label, cid, alts in TARGETS:
        offset_s = first_occurrence(words, alts)
        if offset_s is None:
            rows.append({"strike": label, "reason": "word never appears in transcript"})
            continue
        utterance_utc = video_upload_utc + timedelta(seconds=offset_s)
        tr = pms.trades(cid)
        onset, why = pms.sweep_onset(tr, utterance_utc, hold_window_min=180.0)
        if onset is None:
            rows.append({"strike": label, "programme_offset_s": round(offset_s, 2),
                         "utterance_utc": utterance_utc.isoformat(), "reason": why,
                         "n_trades": len(tr)})
            continue
        delta = (onset - utterance_utc).total_seconds()
        rows.append({
            "strike": label,
            "programme_offset_s": round(offset_s, 2),
            "utterance_utc": utterance_utc.isoformat(),
            "sweep_utc": onset.isoformat(),
            "delta_s": round(delta, 2),
            "n_trades": len(tr),
        })
        pairs.append((offset_s, (onset - video_upload_utc).total_seconds()))

    result = {"video_upload_utc": video_upload_utc.isoformat(), "rows": rows, "n_pairs": len(pairs)}
    if len(pairs) >= 3:
        result["fit"] = sf.fit(pairs, min_fit_points=3, min_fit_span_s=60.0)
    else:
        result["fit"] = {"verdict": "NOT-RESOLVABLE",
                          "reason": f"only {len(pairs)} matched pair(s) — need >=3 for a slope-fit; "
                                    f"reporting raw per-strike offsets instead (see rows[].delta_s)"}
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

    print("pack_lag_rogan self-test (matcher logic only, no network):")
    words = [
        {"word": "the", "start": 0.0}, {"word": "crazy", "start": 5.0},
        {"word": "spy,", "start": 10.0}, {"word": "spy", "start": 20.0},
    ]
    chk("finds first occurrence, punctuation stripped", 10.0, first_occurrence(words, ["spy"]))
    chk("case/normalization insensitive", 5.0, first_occurrence(words, ["Crazy"]))
    chk("returns None when word never appears", None, first_occurrence(words, ["alien"]))
    chk("multi-alternative matches whichever comes first", 5.0,
        first_occurrence(words, ["nonexistent", "crazy"]))
    chk("TARGETS table has 3 real-looking strikes", 3, len(TARGETS))
    for label, cid, alts in TARGETS:
        chk(f"{label} conditionId looks like a real PM condition id", True, cid.startswith("0x") and len(cid) > 40)

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

    meta = json.loads((CORPUS / "meta.json").read_text())
    # words.json's `start` is seconds-from-video-start. Anchored to the
    # video's PUBLISH timestamp (yt-dlp's `timestamp` field, second
    # precision -- NOT the day-granularity `upload_date`), per meta.json's
    # own anchor_note: for a pre-recorded VOD, a trader cannot react to
    # content before the video is public, so publish_ts + content_offset is
    # the correct proxy for "when could a listener have possibly heard
    # this" -- unlike WNT/MLB/WNBA, where the anchor is a real-world live
    # broadcast/game-action instant, not a publish event.
    if "upload_timestamp_unix" not in meta:
        print("FATAL: meta.json has no upload_timestamp_unix anchor -- refusing to "
              "fall back to day-granularity upload_date, which is not precise enough "
              "for a seconds-scale offset measurement", file=sys.stderr)
        return 2
    video_upload_utc = datetime.fromtimestamp(meta["upload_timestamp_unix"], tz=timezone.utc)
    result = run(video_upload_utc)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
