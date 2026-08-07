#!/usr/bin/env python3
"""pack_lag_powell.py — how far behind a real Powell utterance does the
KXPOWELLMENTION pack react? Zero live capture; joins a public post-hoc
transcript against Kalshi's real trade tape via slope-fit, same discipline
as scripts/wnt-pack-lag.py (production, read-only reference).

TARGET EVENT: KXPOWELLMENTION-26MAY31 is NOT an FOMC press conference
(confirmed live 2026-08-06, correcting the open question this task was
given) — it resolves on Jerome Powell's acceptance remarks at the 2026 JFK
Profile in Courage Award Ceremony (Sunday 2026-05-31 evening, JFK Library
Foundation). Three real, actively-traded strikes confirmed via
GET /markets/{ticker}:
  TRUM  "Trump"           (rules_primary: "If Jerome Powell says Trump...")
  GOOD  "Good Evening"    (two-word phrase)
  PRES  "President"

LEG 1, and the real finding this run surfaces: a clean, unambiguous
single-speaker recording of the actual speech exists (JFK Library
Foundation's own YouTube upload, "2026 Profile in Courage Award Ceremony:
Jerome Powell Full Speech", 526s) -- but that upload has NO live-broadcast
timestamp (live_status=not_live, no release_timestamp; only a 2-days-later
upload_date). A second candidate, a long-running "US News LIVE" 24/7 feed
(vvS9Tf0ir_U, 12205s, live_status=was_live, real release_timestamp
2026-06-01T00:37:27Z) DOES carry a real broadcast anchor -- but its own
captions show "Powell" mentioned 40 times spread from t=1240s to t=10355s
and "courage" 105 times starting at t=91s, consistent with a rolling news
channel repeatedly covering/discussing the ceremony in separate segments,
not one continuous airing of the ceremony itself. Using this feed's
release_timestamp as t0 for word offsets drawn from the CLEAN JFK Library
speech video would silently mix two unrelated clocks.

VERDICT: NOT-RESOLVABLE, refusing at the anchor-selection stage rather
than fabricating a join. No public source checked in this pass ties the
clean single-speaker recording to a real-world wall-clock start time
precisely enough to trust a slope-fit -- the market/trade side and the
target-phrase side are both real and confirmed; only the wall-clock bridge
is missing. This is the same honest gap wallclock_anchor.py's own
docstring flags for non-HLS/non-PDT streams, one level upstream: there is
no HLS manifest here at all to even attempt a PDT-tag anchor against.

Usage:
  pack_lag_powell.py --self-test
  pack_lag_powell.py --run    # attempts the join anyway using the rolling-
                               # feed anchor, clearly labeled untrustworthy,
                               # for the record (does not overwrite the
                               # NOT-RESOLVABLE verdict above)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import kalshi_sweep as ks  # noqa: E402
import slope_fit as sf  # noqa: E402
from phrase_match import all_occurrences  # noqa: E402

KALSHI_EVENT = "KXPOWELLMENTION-26MAY31"
CORPUS = Path(__file__).resolve().parent / "corpus" / "20260531"

# strike_code -> accepted alternative phrases. GOOD is a two-word phrase
# ("Good Evening"); all_occurrences() only matches single tokens, so GOOD
# is checked as a bigram manually in matched_pairs() rather than forced
# through the single-word helper.
TARGET_STRIKES = {
    "TRUM": ["trump"],
    "PRES": ["president"],
}
GOOD_PHRASE = ["good", "evening"]

# The long rolling-news feed's own broadcast anchor -- kept here, clearly
# labeled untrustworthy per the module docstring, not used for the primary
# verdict.
UNTRUSTED_FEED_RELEASE_TS = 1780274247  # 2026-06-01T00:37:27Z


def load_words(path=None):
    path = path or (CORPUS / "words.json")
    return json.load(open(path))


def bigram_occurrences(words, phrase):
    """First-token-normalized bigram scan -- GOOD's "Good Evening" is the
    one multi-word target in this event; phrase_match's flat helpers only
    cover single words, so this is a minimal local extension rather than
    stretching the shared module for a one-off two-word case."""
    from phrase_match import norm
    normed = [norm(w["word"]) for w in words]
    out = []
    for i in range(len(normed) - 1):
        if normed[i] == phrase[0] and normed[i + 1] == phrase[1]:
            out.append(words[i + 1]["end"])
    return sorted(out)


def matched_pairs(words=None, t0=None):
    words = words if words is not None else load_words()
    rows = []
    pairs = []
    for code, alts in TARGET_STRIKES.items():
        occs = all_occurrences(words, alts)
        _record(code, occs, t0, rows, pairs)
    occs = bigram_occurrences(words, GOOD_PHRASE)
    _record("GOOD", occs, t0, rows, pairs)
    return rows, pairs


def _record(code, occs, t0, rows, pairs):
    if not occs:
        rows.append({"strike": code, "verdict": "NOT-RESOLVABLE",
                      "reason": "phrase never appears in transcript"})
        return
    if t0 is None:
        # No trustworthy anchor -- refuse before ever touching the network
        # (no trade tape is needed to state "we have no clock to place it
        # on"), keeping this path self-test-safe.
        rows.append({"strike": code, "occurrences_s": [round(o, 2) for o in occs],
                      "verdict": "NOT-RESOLVABLE",
                      "reason": "no trustworthy wall-clock anchor available (see module docstring)"})
        return
    ticker = f"{KALSHI_EVENT}-{code}"
    tr = ks.trades(ticker)
    onset, why = ks.sweep_onset(tr, t0, window_after_min=180.0, hold_window_min=60.0)
    if onset is None:
        rows.append({"strike": code, "occurrences_s": [round(o, 2) for o in occs],
                      "verdict": "NOT-RESOLVABLE", "reason": why})
        return
    sweep_off = (onset - t0).total_seconds()
    first_off = occs[0]
    rows.append({
        "strike": code, "occurrences_s": [round(o, 2) for o in occs],
        "sweep_offset_s": round(sweep_off, 2), "sweep_wallclock": onset.isoformat(),
        "first_occurrence_delta_s": round(sweep_off - first_off, 2), "verdict": "ok",
    })
    pairs.append((first_off, sweep_off))


def run(attempt_untrusted_anchor=False):
    words = load_words()
    if not attempt_untrusted_anchor:
        rows, pairs = matched_pairs(words, t0=None)
        return {
            "event": KALSHI_EVENT, "n_pairs": 0,
            "verdict": "NOT-RESOLVABLE",
            "reason": (
                "no trustworthy public wall-clock anchor found for this ceremony's "
                "actual start time -- the clean single-speaker VOD (JFK Library "
                "Foundation, 526s) has no live-broadcast timestamp at all, and the "
                "one candidate that DOES have a real release_timestamp (a 24/7 "
                "rolling news feed) shows 'Powell'/'courage' scattered across "
                "40/105 occurrences over a 9000+ second span -- evidence of "
                "repeated news coverage, not one continuous ceremony airing, so "
                "using its timestamp against the clean VOD's offsets would mix "
                "two unrelated clocks rather than measure a real lag"
            ),
            "rows": rows,
        }
    t0 = datetime.fromtimestamp(UNTRUSTED_FEED_RELEASE_TS, timezone.utc)
    rows, pairs = matched_pairs(words, t0=t0)
    fit = sf.fit(pairs, min_fit_points=3, min_fit_span_s=60.0) if len(pairs) >= 3 else {
        "verdict": "NOT-RESOLVABLE", "reason": f"only {len(pairs)} pair(s), need >=3"}
    return {
        "event": KALSHI_EVENT, "t0_utc_UNTRUSTED": t0.isoformat(),
        "rows": rows, "n_pairs": len(pairs), "fit": fit,
        "caveat": "t0 is the untrusted rolling-feed anchor -- see module docstring; "
                  "do not treat this fit as a real measurement.",
    }


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

    print("pack_lag_powell self-test:")
    words = [
        {"word": "Good", "start": 0.0, "end": 0.3},
        {"word": "evening,", "start": 0.4, "end": 0.8},
        {"word": "thank", "start": 1.0, "end": 1.2},
        {"word": "you,", "start": 1.3, "end": 1.4},
        {"word": "Mr.", "start": 2.0, "end": 2.1},
        {"word": "President", "start": 2.2, "end": 2.6},
        {"word": "and", "start": 3.0, "end": 3.1},
        {"word": "Trump", "start": 4.0, "end": 4.3},
    ]
    chk("bigram_occurrences finds 'Good Evening'", [0.8], bigram_occurrences(words, GOOD_PHRASE))
    chk("all_occurrences finds 'President'", [2.6], all_occurrences(words, TARGET_STRIKES["PRES"]))
    chk("all_occurrences finds 'Trump'", [4.3], all_occurrences(words, TARGET_STRIKES["TRUM"]))

    rows, pairs = matched_pairs(words, t0=None)
    chk("no anchor -> every matched strike reports NOT-RESOLVABLE with the anchor reason",
        True, all(r["verdict"] == "NOT-RESOLVABLE" for r in rows if "occurrences_s" in r))

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
    ap.add_argument("--run", action="store_true", help="attempt the join with the untrusted anchor, for the record")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    result = run(attempt_untrusted_anchor=a.run)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
