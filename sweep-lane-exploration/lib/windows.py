#!/usr/bin/env python3
"""windows.py — resolve an event's real call time, or refuse.

Provenance-ranked. Never guesses, never infers a call time from trade
density: inferring the window from the activity you are about to measure is
circular, and the whole point of this lane is that the activity IS the
measurement.

⚠ FINDING (2026-08-06), which corrected the study plan twice:

1. `slate/call-times.json` holds **10 entries, all 26AUG05** — none of the
   nine TRAIN events. The plan named it as the primary source; it is not.
2. `configs/*.toml` `starts_at` IS a good source, but only once the seeded
   placeholder `2026-07-15T00:00:00Z  # TODO: set the real call time (UTC)`
   is rejected. 143 configs carry the field and many are that literal.

With the placeholder filtered, coverage is **47 of 51** mention events, and
the resolved times check out against independently documented sweep onsets:
AAL 12:30Z (doc: sweeps 12:33–12:41Z), INTC 21:00Z (doc: basket 21:01–21:03Z).

That last one matters — it caught a real error. The provisional pre-study
numbers used a trade-density window that placed INTC's window at 20:30Z and
so scored **pre-call** trades as sweeps. Density-inferred windows are
circular (they infer the window from the activity being measured) and this
is what that circularity costs. Hence: resolve or refuse, never infer.

The 4 events that do not resolve are excluded from the primary run and
reported as excluded; a window-free variant shows what they would have
contributed. See PRE-REGISTRATION.md §S2.

Read-only. Reads slate/, pm-expansion-exploration/ and configs/ but never
writes to them.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Configs seeded from the template carry this literal as a placeholder. Treating
# it as a real call time would silently anchor a dozen events to midnight on an
# unrelated day.
PLACEHOLDER_STARTS_AT = "2026-07-15T00:00:00Z"

# How far either side of the call the "in-call" window runs, for the sensitivity
# variant only. Earnings calls run ~45-90 min; the pre-margin catches a sweep
# fired off the press release before the call audio starts.
WINDOW_BEFORE = timedelta(minutes=45)
WINDOW_AFTER = timedelta(minutes=150)


def _parse(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _load_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _from_call_times(event: str, path: str, label: str):
    d = _load_json(path)
    if not isinstance(d, dict):
        return None
    row = d.get(event)
    if not isinstance(row, dict):
        return None
    t = _parse(row.get("call_time_utc", ""))
    if t is None:
        return None
    return t, f"{label}:{row.get('source', 'unstated')}"


def _from_configs(event: str):
    """Only accept a config `starts_at` that is not the seeded placeholder."""
    cfg_dir = os.path.join(REPO, "configs")
    if not os.path.isdir(cfg_dir):
        return None
    want = event.lower()
    for name in sorted(os.listdir(cfg_dir)):
        if not name.endswith(".toml"):
            continue
        path = os.path.join(cfg_dir, name)
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            continue
        if want not in text.lower():
            continue
        m = re.search(r'^\s*starts_at\s*=\s*"([^"]+)"', text, re.M)
        if not m:
            continue
        raw = m.group(1)
        if raw == PLACEHOLDER_STARTS_AT:
            continue
        t = _parse(raw)
        if t is not None:
            return t, f"configs/{name}:starts_at"
    return None


def call_time(event: str, extra_sources: dict | None = None):
    """Return (datetime, provenance) or (None, reason).

    Provenance is always returned so a caller can print where a number came
    from. Sources are ranked: production slate first, exploration copy second,
    a non-placeholder config third, caller-supplied last-resort fourth.
    """
    for path, label in (
        (os.path.join(REPO, "slate", "call-times.json"), "slate/call-times.json"),
        (os.path.join(REPO, "pm-expansion-exploration", "call-times.json"),
         "pm-expansion-exploration/call-times.json"),
    ):
        got = _from_call_times(event, path, label)
        if got:
            return got

    got = _from_configs(event)
    if got:
        return got

    if extra_sources and event in extra_sources:
        t = _parse(extra_sources[event])
        if t is not None:
            return t, "caller-supplied"

    return None, "unknown-call-time"


def window(event: str, extra_sources: dict | None = None):
    """Return (start, end, provenance) or (None, None, reason). Refuses, never guesses."""
    t, why = call_time(event, extra_sources)
    if t is None:
        return None, None, why
    return t - WINDOW_BEFORE, t + WINDOW_AFTER, why


def coverage(events):
    """(known, unknown) split over an iterable of event tickers."""
    known, unknown = {}, []
    for e in events:
        t, why = call_time(e)
        if t is None:
            unknown.append(e)
        else:
            known[e] = (t, why)
    return known, unknown


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

    print("windows self-test:")

    # An event present in the production slate resolves, with provenance.
    t, why = call_time("KXEARNINGSMENTIONDASH-26AUG05")
    chk("slate event resolves", datetime(2026, 8, 5, 20, 30, tzinfo=timezone.utc), t)
    chk("slate provenance names the file", True, why.startswith("slate/call-times.json"))

    # An event in no source REFUSES rather than guessing.
    chk("unknown event refuses",
        (None, "unknown-call-time"), call_time("KXEARNINGSMENTIONNOSUCH-26JAN01"))

    # An event with only a real (non-placeholder) config starts_at resolves,
    # and lands where independently documented sweeps say the call was.
    # AAL: docs/COMPETITOR-ANATOMY.md records sweeps at 12:33-12:41Z.
    t, why = call_time("KXEARNINGSMENTIONAAL-26JUL23")
    chk("AAL resolves from a config", datetime(2026, 7, 23, 12, 30, tzinfo=timezone.utc), t)
    chk("AAL provenance names the config", True, why.startswith("configs/"))

    # INTC is the regression case: the density heuristic put this event's
    # window at 20:30Z and scored pre-call trades as sweeps. The documented
    # basket is 21:01-21:03Z, so the real call time must be 21:00Z.
    t, _ = call_time("KXEARNINGSMENTIONINTC-26JUL23")
    chk("INTC resolves to 21:00Z, not the density-inferred 20:30Z",
        datetime(2026, 7, 23, 21, 0, tzinfo=timezone.utc), t)

    # The regression this module exists to prevent: nothing may silently
    # resolve to the seeded placeholder midnight.
    ph = _parse(PLACEHOLDER_STARTS_AT)
    bad = [e for e in ("KXEARNINGSMENTIONAAL-26JUL23", "KXEARNINGSMENTIONINTC-26JUL23",
                       "KXEARNINGSMENTIONMS-26JUL15", "KXEARNINGSMENTIONLMT-26JUL23")
           if call_time(e)[0] == ph]
    chk("no event resolves to the placeholder", [], bad)

    # window() propagates the refusal instead of returning a half-answer.
    s, e, why = window("KXEARNINGSMENTIONNOSUCH-26JAN01")
    chk("window refuses too", (None, None, "unknown-call-time"), (s, e, why))

    s, e, why = window("KXEARNINGSMENTIONDASH-26AUG05")
    chk("window brackets the call", True,
        s < datetime(2026, 8, 5, 20, 30, tzinfo=timezone.utc) < e)

    # Coverage splits cleanly; ALLY has no resolvable call time in any source.
    known, unknown = coverage([
        "KXEARNINGSMENTIONDASH-26AUG05", "KXEARNINGSMENTIONAAL-26JUL23",
        "KXEARNINGSMENTIONALLY-26JUL21",
    ])
    chk("coverage splits known from unknown", (2, 1), (len(known), len(unknown)))
    chk("ALLY is the unknown one", ["KXEARNINGSMENTIONALLY-26JUL21"], unknown)

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
