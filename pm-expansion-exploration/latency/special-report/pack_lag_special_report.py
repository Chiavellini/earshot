#!/usr/bin/env python3
"""pack_lag_special_report.py — how far behind a real utterance does the
KXSPECIALREPORTMENTION pack react? Zero live capture; joins a public
post-hoc timing reference against Kalshi's public trade tape via slope-fit,
same discipline as scripts/wnt-pack-lag.py (production, read-only
reference, never imported/modified here).

STATUS: NO-SIGNAL-YET. This script is built and self-tested, but has never
been run against real leg-1 data — none was obtainable (see below). It is
ready to run the moment a transcript/VOD becomes available for this show.

TARGET EVENT: KXSPECIALREPORTMENTION-26JUN23 (Fox News "Special Report
with Bret Baier", June 23, 2026). Real, actively-traded: 4 settled-YES
strikes with real trade history — RUSS (Russia/Russian, 46 trades,
0.76->0.99), TRUM (Trump, 3+ times, 103 trades, 0.72->0.99), UKRA (Ukraine,
66 trades, 0.78->0.99), OIL (Oil/Gas/Gasoline, 103 trades, 0.52->0.99) —
confirmed live 2026-08-06 via GET /markets/trades?ticker=<ticker>.

CRITICAL CORRECTION TO THE ORIGINAL PREMISE, found while pulling the real
rules text: this was picked as a "second newscast, comparable to WNT" test
case. It is NOT that. Every strike's rules_primary reads "If Mark Rutte
says <WORD> as part of Fox News: Special Report" — this market resolves on
NATO Secretary General Mark Rutte specifically, a guest interviewed on the
show that day, not on the show's own anchor/narration content generically.
That is structurally the SAME speaker-attribution problem already flagged
and worked around in pack_lag_rogan.py (the Trump/"trans" market that was
deliberately NOT used there) — a caption/transcript track with no speaker
diarization cannot reliably tell "Rutte said X" apart from "Baier said X
while asking the question" or "a different guest said X". Any future join
against this specific event needs a diarized or interview-segment-only
transcript, not a plain caption track, or the match will be unreliable
regardless of timing accuracy. This is noted here rather than hidden.

WHY THIS IS NO-SIGNAL-YET (leg-1 unobtainable, checked 2026-08-06):
Fox News gates full "Special Report" episodes behind the Fox Nation
subscription service (nation.foxnews.com/special-report-with-bret-baier-audio/)
— unlike ABC/CBS/NBC, there is no freely obtainable full-episode YouTube
VOD. The specific Rutte interview segment was also checked for a standalone
free clip (networks sometimes post individual newsmaker segments even when
full episodes are paywalled) — none found; the only public artifacts are
FoxNews.com text writeups (e.g. foxnews.com/media/nato-secretary-mark-rutte-praises-trumps-iran-strategy-addresses-presidents-frustration-allies)
quoting excerpts, not a timestamped transcript usable for word-level offset
matching. This is a real, structural finding about the venue class, not a
search-effort gap: cable news content generally, and Fox News specifically,
is much less freely obtainable than broadcast network news — directly
relevant to `docs/EXPANSION.md`'s original (never fully tested) concern
about linear/cable-news obtainability.

Usage:
  pack_lag_special_report.py --self-test    # matcher logic only, no network
  pack_lag_special_report.py                # refuses: no banked corpus yet
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import kalshi_sweep as ks  # noqa: E402
import slope_fit as sf  # noqa: E402
from phrase_match import all_occurrences  # noqa: E402

CORPUS = Path(__file__).resolve().parent / "corpus" / "20260623"
KALSHI_EVENT = "KXSPECIALREPORTMENTION-26JUN23"

# strike_code -> accepted alternative words. Confirmed live 2026-08-06 via
# GET /markets?event_ticker=KXSPECIALREPORTMENTION-26JUN23. TRUM is a
# count-strike ("Trump (3+ times)") -- excluded here on purpose, same
# discipline wnt-pack-lag.py applies: a caption track cannot reliably
# reproduce which occurrence was the Nth, so only single-mention strikes
# are attempted for a slope-fit.
TARGET_STRIKES = {
    "RUSS": ["russia", "russian"],
    "UKRA": ["ukraine"],
    "OIL": ["oil", "gas", "gasoline"],
}
# TRUM excluded (count-strike, "3+ times") -- see above.
COUNT_STRIKES_EXCLUDED = {"TRUM": "Trump (3+ times)"}

# All strikes here carry the Mark-Rutte speaker-attribution caveat from the
# module docstring -- a plain (non-diarized) transcript match answers "was
# this word said anywhere in the show," not "did Rutte specifically say it."
# Report results with that caveat attached, not as an unqualified offset.


def load_words(path=None):
    path = path or (CORPUS / "words.json")
    if not path.exists():
        raise SystemExit(
            f"FATAL: no banked transcript at {path} -- leg-1 was not obtainable "
            f"for this venue (Fox Nation paywall, no free YouTube VOD/clip found "
            f"2026-08-06). This script is ready to run the moment a transcript "
            f"becomes available; see the module docstring for what was checked."
        )
    return json.loads(path.read_text())


def matched_pairs(words, t0):
    rows, pairs = [], []
    for code, alts in TARGET_STRIKES.items():
        occs = all_occurrences(words, alts)
        if not occs:
            rows.append({"strike": code, "verdict": "NOT-RESOLVABLE",
                         "reason": "none of the alternatives appear in the transcript"})
            continue
        ticker = f"{KALSHI_EVENT}-{code}"
        tr = ks.trades(ticker)
        onset, why = ks.sweep_onset(tr, t0, window_after_min=180.0, hold_window_min=60.0)
        if onset is None:
            rows.append({"strike": code, "occurrences_s": occs,
                         "verdict": "NOT-RESOLVABLE", "reason": why})
            continue
        sweep_off = (onset - t0).total_seconds()
        rows.append({
            "strike": code,
            "occurrences_s": [round(o, 2) for o in occs],
            "sweep_offset_s": round(sweep_off, 2),
            "sweep_wallclock": onset.isoformat(),
            "first_occurrence_delta_s": round(sweep_off - occs[0], 2),
            "verdict": "ok",
            "caveat": "no speaker diarization -- cannot confirm Rutte (not "
                      "Baier or another guest) said this word",
        })
        pairs.append((occs[0], sweep_off))
    return rows, pairs


def run(t0_utc: str):
    words = load_words()
    t0 = datetime.fromisoformat(t0_utc)
    rows, pairs = matched_pairs(words, t0)
    fit = sf.fit(pairs, min_fit_points=3, min_fit_span_s=60.0)
    result = {"event": KALSHI_EVENT, "t0_utc": t0_utc, "rows": rows,
              "fit": fit, "n_pairs": len(pairs),
              "count_strikes_excluded": COUNT_STRIKES_EXCLUDED,
              "speaker_attribution_caveat": (
                  "Every strike here resolves on Mark Rutte specifically saying "
                  "the word, not the show generally -- a non-diarized transcript "
                  "match cannot distinguish Rutte from Baier or another guest. "
                  "Any 'ok' row above should be read as an upper bound on how "
                  "early the word appeared in the show, not a confirmed Rutte "
                  "utterance time.")}
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

    print("pack_lag_special_report self-test (matcher logic only, no network):")

    chk("TARGET_STRIKES excludes the count-strike TRUM", False, "TRUM" in TARGET_STRIKES)
    chk("TARGET_STRIKES covers the 3 single-mention settled-YES codes",
        {"RUSS", "UKRA", "OIL"}, set(TARGET_STRIKES.keys()))

    fake_words = [
        {"word": "So", "start": 0.0, "end": 0.2},
        {"word": "Russia", "start": 10.0, "end": 10.5},
        {"word": "later", "start": 20.0, "end": 20.3},
        {"word": "Russian", "start": 30.0, "end": 30.4},
        {"word": "Ukraine", "start": 40.0, "end": 40.6},
        {"word": "gasoline", "start": 50.0, "end": 50.7},
    ]
    occs = all_occurrences(fake_words, TARGET_STRIKES["RUSS"])
    chk("all_occurrences finds every alternative, sorted", [10.5, 30.4], occs)
    occs = all_occurrences(fake_words, TARGET_STRIKES["UKRA"])
    chk("all_occurrences matches a single-word strike", [40.6], occs)
    occs = all_occurrences(fake_words, TARGET_STRIKES["OIL"])
    chk("all_occurrences matches an alternate form (gasoline)", [50.7], occs)

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    try:
        run("2026-06-23T22:00:00+00:00")  # Special Report air time, 6pm ET
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
