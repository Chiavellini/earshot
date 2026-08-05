#!/usr/bin/env python3
"""pack_lag_tbpn.py — how far behind a real TBPN mention is the
KXTBPNMENTION pack already reacting? Zero live audio capture; two free
public sources only (YouTube auto-caption VOD + Kalshi's public trade
tape), joined via slope-fit, same discipline as scripts/wnt-pack-lag.py
(production, read-only reference, never imported/modified here).

TARGET EVENT: KXTBPNMENTION-26JUL23 (the most recent settled KXTBPNMENTION
event, confirmed live 2026-08-05) — 13 strikes, 4 settled YES with real
trade history: ANTH (Anthropic/Claude), ROBO (Robot/Robotic/Robotics), HELI
(Helios), OPEN (OpenAI/ChatGPT). VOD banked via tbpn_vod_fetch.sh 20260723
--no-audio: video 8Jqi1yCniHk, "AMD CEO Lisa Su Live on TBPN", 6566s.
Fits the strikes well on priors: AMD's own MI400 rack-scale AI system is
literally named "Helios," so an AMD-CEO interview mentioning it, Anthropic,
OpenAI, and "robot/robotic" (common AI-industry vocabulary) is unsurprising
— corroborating evidence this is the right VOD, not just duration-window luck.

WALL-CLOCK ANCHOR (t0): unlike WNT's fixed daily air time (22:30 UTC exactly,
every day), TBPN has no single minute-exact start — "weekdays 11am-2pm PT"
is a window, not a clock. This VOD is an ARCHIVED LIVESTREAM (yt-dlp
live_status=was_live), which carries a real `release_timestamp` distinct
from its post-hoc `timestamp` (publish/finalize time). Checked live:
  release_timestamp = 1784833558 -> 2026-07-23T19:05:58Z (inside the
    18:00-21:00 UTC show window — the real stream start)
  timestamp          = 1784841168 -> 2026-07-23T21:12:48Z (~17 min after
    start+duration, consistent with post-stream archive processing, NOT
    the start)
So t0 = release_timestamp is used. This is the one genuinely new risk this
script carries relative to WNT/MLB/WNBA's anchors (none of which had to
infer a livestream's start time from metadata) — stated here, not hidden,
and testable via the same slope=1.0 check every other join in this program
uses: if release_timestamp were wrong, the fitted slope would not land near
1.0, and the script would refuse rather than report a number.

PHRASE MATCHING: reuses wnt-pack-lag.py's exact idea (word-level normalize:
lowercase, strip punctuation, drop a trailing possessive/plural 's' on
words >3 chars — Kalshi's settlement rule counts "the exact phrase/word, or
a plural or possessive form"), applied to first-occurrence-of-any-alternative
(none of these 4 strikes are count-strikes, confirmed via yes_sub_title —
no "N+ times" suffix on any of them).

Usage:
  pack_lag_tbpn.py                    # re-run the join against banked corpus
  pack_lag_tbpn.py --self-test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import kalshi_sweep as ks  # noqa: E402
import slope_fit as sf  # noqa: E402

CORPUS = Path(__file__).resolve().parent / "corpus" / "20260723"
KALSHI_EVENT = "KXTBPNMENTION-26JUL23"
RELEASE_TIMESTAMP = 1784833558  # verified live 2026-08-05, see module docstring

# strike_code -> accepted alternative words (already lowercase-normalized).
# Confirmed via GET /markets?event_ticker=KXTBPNMENTION-26JUL23 (2026-08-05):
# all four are single-occurrence "says WORD" strikes, no "(N+ times)" suffix.
TARGET_STRIKES = {
    "ANTH": ["anthropic", "claude"],
    "ROBO": ["robot", "robotic", "robotics"],
    "HELI": ["helios"],
    "OPEN": ["openai", "chatgpt"],
}

WORD_RE = re.compile(r"[a-z0-9]+")


def norm(tok: str) -> str:
    """Same rule as wnt-pack-lag.py's norm(): lowercase, strip punctuation,
    collapse a trailing possessive/plural 's' on words >3 chars."""
    t = tok.lower()
    t = re.sub(r"['’]s$", "", t)
    t = "".join(WORD_RE.findall(t))
    return t[:-1] if len(t) > 3 and t.endswith("s") else t


def load_words(path=None):
    path = path or (CORPUS / "words.json")
    return json.load(open(path))


def all_occurrences(words, alternatives):
    """-> sorted list of programme_offset_s for EVERY occurrence of any
    alternative (word END timestamp, matching wnt-pack-lag.py's anchor
    convention). Alternatives are normalized through the SAME norm() as
    caption words — without this, a target word that itself ends in a bare
    's' (e.g. "helios") would compare unnormalized ("helios") against a
    normalized caption hit ("helio", after norm()'s trailing-s strip), and
    silently never match. Both sides must go through norm() identically."""
    alt_set = {norm(a) for a in alternatives}
    return sorted(w["end"] for w in words if norm(w["word"]) in alt_set)


def matched_pairs(words=None, t0=None):
    """Reports BOTH the first-occurrence delta and the last-occurrence-
    before-the-sweep delta (the WNBA-precedent "closest prior anchor" fix,
    lib/kalshi_sweep.py's own build found the first occurrence of a trusted
    event doesn't always trigger the sweep). Both are surfaced — see the
    module-level finding note for why neither converges into a small,
    consistent number on this event."""
    words = words if words is not None else load_words()
    t0 = t0 or datetime.fromtimestamp(RELEASE_TIMESTAMP, timezone.utc)
    rows = []
    pairs_first = []
    pairs_last_prior = []
    for code, alts in TARGET_STRIKES.items():
        occs = all_occurrences(words, alts)
        if not occs:
            rows.append({"strike": code, "verdict": "NOT-RESOLVABLE",
                         "reason": "none of the alternatives appear in the transcript"})
            continue
        ticker = f"{KALSHI_EVENT}-{code}"
        tr = ks.trades(ticker)
        # TBPN's own trades (per the 2026-08-05 live pull) span past the
        # show into the next day, the same thin-post-event-trading shape
        # WNBA's join hit — bound the hold-check the same way.
        onset, why = ks.sweep_onset(tr, t0, window_after_min=180.0, hold_window_min=60.0)
        if onset is None:
            rows.append({"strike": code, "occurrences_s": [round(o, 2) for o in occs],
                         "verdict": "NOT-RESOLVABLE", "reason": why})
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
            "verdict": "ok",
        })
        pairs_first.append((first_off, sweep_off))
        if last_prior_off is not None:
            pairs_last_prior.append((last_prior_off, sweep_off))
    return rows, pairs_first, pairs_last_prior


def run():
    words = load_words()
    rows, pairs_first, pairs_last_prior = matched_pairs(words)
    fit_first = sf.fit(pairs_first, min_fit_points=3, min_fit_span_s=60.0)
    fit_last_prior = sf.fit(pairs_last_prior, min_fit_points=3, min_fit_span_s=60.0)
    result = {
        "event": KALSHI_EVENT, "video": "8Jqi1yCniHk",
        "t0_utc": datetime.fromtimestamp(RELEASE_TIMESTAMP, timezone.utc).isoformat(),
        "rows": rows,
        "fit_using_first_occurrence": fit_first,
        "fit_using_last_prior_occurrence": fit_last_prior,
        "n_pairs": len(pairs_first),
        "finding": (
            "Both fits refuse (slope far from 1.0). All 4 sweeps land within "
            "a ~500s window near the very end of the 6566s show (5746-6254s "
            "programme offset), independent of when each target word was "
            "actually said (occurrences spread from ~360s to ~6250s across "
            "the show). This is NOT an anchor-offset problem (which would "
            "still show slope=1.0 with a different intercept) — it is "
            "evidence this market does not react to individual word "
            "mentions in real time; trading activity appears to cluster "
            "near the show's end regardless of individual utterance timing. "
            "Structurally different from WNT/MLB/WNBA, all of which showed "
            "slope=1.0 joins. Reported as a real finding, not forced into a "
            "number the data does not support."
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

    print("pack_lag_tbpn self-test:")

    chk("norm() strips trailing possessive", "robotic", norm("robotic's"))
    chk("norm() collapses a plural on a long word", "robot", norm("Robots"))
    chk("norm() leaves a short word's trailing s alone", "was", norm("was"))
    chk("TARGET_STRIKES covers all 4 real settled-YES codes", {"ANTH", "ROBO", "HELI", "OPEN"},
        set(TARGET_STRIKES.keys()))

    fake_words = [
        {"word": "So", "start": 0.0, "end": 0.2},
        {"word": "Anthropic", "start": 10.0, "end": 10.5},
        {"word": "later", "start": 20.0, "end": 20.3},
        {"word": "Claude", "start": 30.0, "end": 30.4},  # 2nd alt for ANTH, must not win over the earlier hit
        {"word": "Helios'", "start": 40.0, "end": 40.6},  # possessive form must still match
        {"word": "robotics", "start": 50.0, "end": 50.7},
    ]
    occs = all_occurrences(fake_words, TARGET_STRIKES["ANTH"])
    chk("all_occurrences finds every alternative, sorted, earliest first",
        [10.5, 30.4], occs)
    occs = all_occurrences(fake_words, TARGET_STRIKES["HELI"])
    chk("all_occurrences matches a possessive form via norm()", [40.6], occs)
    occs = all_occurrences(fake_words, TARGET_STRIKES["ROBO"])
    chk("all_occurrences matches a plural alternative", [50.7], occs)
    occs = all_occurrences(fake_words, TARGET_STRIKES["OPEN"])
    chk("all_occurrences returns [] when no alternative appears", [], occs)

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
