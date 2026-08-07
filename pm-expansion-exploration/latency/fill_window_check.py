#!/usr/bin/env python3
"""fill_window_check.py — re-examine the TBPN/SOTU/keynote (EMPTY-PACK)
results through lib/fill_window.py's lens instead of sweep_onset()'s: not
"did the pack react fast," but "was a sub-97c fill ever available after our
own detector would have confirmed the word" (see mention-market-
exploitability-scoreboard.md's 2026-08-07 fill-availability addendum for
why these are different questions).

Reuses each venue's already-banked corpus (no live-capture/new sourcing)
and already-known target strikes/condition-tickers — only the analysis is
new. ref_time = the EARLIEST occurrence of the target word/phrase (the most
generous test: the first instant our detector could possibly have fired).

Usage:
  fill_window_check.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))

import kalshi_sweep as ks  # noqa: E402
import pm_sweep as pms  # noqa: E402
from fill_window import fill_window  # noqa: E402
from phrase_match import all_occurrences, first_occurrence, occurrences  # noqa: E402


def check_tbpn():
    sys.path.insert(0, str(HERE / "tbpn"))
    import pack_lag_tbpn as tb  # noqa: E402

    words = tb.load_words()
    t0 = datetime.fromtimestamp(tb.RELEASE_TIMESTAMP, timezone.utc)
    out = []
    for code, alts in tb.TARGET_STRIKES.items():
        occs = all_occurrences(words, alts)
        if not occs:
            out.append({"strike": code, "verdict": "NOT-RESOLVABLE", "reason": "no occurrence"})
            continue
        ref_time = t0 + timedelta(seconds=occs[0])
        ticker = f"{tb.KALSHI_EVENT}-{code}"
        tr = ks.trades(ticker)
        r = fill_window(tr, ref_time)
        r.update({"strike": code, "ticker": ticker, "first_occurrence_s": round(occs[0], 2),
                  "ref_time_utc": ref_time.isoformat()})
        out.append(r)
    return out


def check_sotu():
    sys.path.insert(0, str(HERE / "sotu"))
    import pack_lag_sotu as st  # noqa: E402

    words = json.loads((st.CORPUS / "words.json").read_text())
    out = []
    for label, cid, alts, risk in st.TARGETS:
        offset_s = first_occurrence(words, alts)
        if offset_s is None:
            out.append({"strike": label, "verdict": "NOT-RESOLVABLE", "reason": "no occurrence"})
            continue
        ref_time = st.T0 + timedelta(seconds=offset_s)
        tr = pms.trades(cid)
        r = fill_window(tr, ref_time)
        r.update({"strike": label, "condition_id": cid, "first_occurrence_s": round(offset_s, 2),
                  "ref_time_utc": ref_time.isoformat()})
        if risk:
            r["risk_note"] = risk
        out.append(r)
    return out


def check_keynote():
    sys.path.insert(0, str(HERE / "keynote"))
    import pack_lag_keynote as kn  # noqa: E402

    words = kn.load_words()
    t0 = datetime.fromtimestamp(kn.RELEASE_TIMESTAMP, timezone.utc)
    out = []
    for label, cid, phrases in kn.TARGET_STRIKES:
        occs = occurrences(words, phrases, fuzzy=False)
        if not occs:
            out.append({"strike": label, "verdict": "NOT-RESOLVABLE", "reason": "no occurrence"})
            continue
        first_off, _how = occs[0]
        ref_time = t0 + timedelta(seconds=first_off)
        tr = pms.trades(cid)
        r = fill_window(tr, ref_time)
        r.update({"strike": label, "condition_id": cid, "first_occurrence_s": round(first_off, 2),
                  "ref_time_utc": ref_time.isoformat()})
        out.append(r)
    return out


def main():
    result = {
        "tbpn": check_tbpn(),
        "sotu": check_sotu(),
        "keynote": check_keynote(),
    }
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
