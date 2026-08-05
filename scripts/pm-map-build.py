#!/usr/bin/env python3
"""pm-map-build.py — generate a pm-capture.py input map from Polymarket Gamma.

The 2026-07-30 map (configs/pm-map-26jul30.json) was assembled by hand for one
date, which is fine once and a liability every time after. This is the
generator the Q-PM pin describes as a recipe but never shipped.

  scripts/pm-map-build.py --out configs/pm-map-26aug03.json \\
      KXEARNINGSMENTIONPLTR-26AUG03=what-will-palantir-say-...-20260730163530759 \\
      KXEARNINGSMENTIONGRAB-26AUG03=grab-quarterly-earnings-gaap-eps-08-03-2026-0pt02:release

A `:release` suffix on the slug tags the entry `"clock": "release"` instead of
the default `"word"`. That distinction is load-bearing, not cosmetic:

  * word    — PM lists the SAME instrument class as Kalshi (a spoken-mention
              market). A per-keyword cross-venue Δt is meaningful.
  * release — PM lists a DIFFERENT instrument (e.g. a GAAP-EPS market) that
              settles on the press release, not on spoken words. It still gives
              a shared clock both venues reprice against, but a keyword Δt
              against it would be comparing two different things. pm-edge-report
              must never pool the two.

Gamma nulls nothing we need, but `clobTokenIds` arrives as a JSON-ENCODED
STRING inside the JSON (not a list) — decoded here so downstream never has to.

Transport: python/urllib is fine for gamma-api (only clob.polymarket.com
TLS-fingerprints python — Q-PM). Read-only, no auth, no keys.
"""

import argparse
import json
import sys
import urllib.request
from datetime import datetime

GAMMA = "https://gamma-api.polymarket.com/events"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def fetch_event(slug):
    url = f"{GAMMA}?slug={urllib.parse.quote(slug)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if isinstance(data, dict):
        data = data.get("data", [])
    if not data:
        raise SystemExit(f"FATAL: no Polymarket event for slug {slug!r} — check it is still open")
    return data[0]


def _parse_utc(s):
    """ISO-8601 with a trailing Z -> aware datetime, or None. Deliberately
    tolerant: an unparseable end_date must not fail a build, it must simply
    fail to trigger the guard."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def call_time_for(kalshi_ticker, path):
    """Kalshi call start from slate/call-times.json, or None if unknown."""
    try:
        with open(path) as f:
            table = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    ent = table.get(kalshi_ticker) or {}
    return _parse_utc(ent.get("call_time_utc"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("pairs", nargs="+",
                    help="KALSHI_EVENT_TICKER=pm-event-slug[:release]")
    ap.add_argument("--call-times", default="slate/call-times.json",
                    help="Kalshi call times, for the GRAB-class end_date guard")
    ap.add_argument("--strict", action="store_true",
                    help="refuse the build when a PM market closes before the call")
    a = ap.parse_args()

    out = {}
    for pair in a.pairs:
        if "=" not in pair:
            raise SystemExit(f"FATAL: bad pair {pair!r}, want KALSHI_TICKER=pm-slug[:release]")
        kalshi, slug = pair.split("=", 1)
        clock = "word"
        if slug.endswith(":release"):
            slug, clock = slug[: -len(":release")], "release"

        ev = fetch_event(slug)
        markets = []
        for m in ev.get("markets") or []:
            raw = m.get("clobTokenIds")
            try:
                tokens = json.loads(raw) if isinstance(raw, str) else (raw or [])
            except json.JSONDecodeError:
                tokens = []
            cond = m.get("conditionId") or m.get("condition_id")
            if not cond or len(tokens) < 2:
                print(f"  SKIP (no condition_id/tokens): {(m.get('question') or '')[:60]}",
                      file=sys.stderr)
                continue
            markets.append({
                "question": m.get("question"),
                "condition_id": cond,
                "tokens": [str(tokens[0]), str(tokens[1])],
                "pm_slug": m.get("slug"),
                # Recorded so the GRAB class is machine-detectable: a PM market
                # that closes BEFORE the Kalshi call means the two venues never
                # price the same instant, and any lag or depth number joining
                # them is meaningless (docs/POSTMORTEM-26AUG03.md).
                "end_date": m.get("endDate") or m.get("end_date"),
            })

        if not markets:
            raise SystemExit(f"FATAL: {slug} produced 0 usable markets")

        out[kalshi] = {
            "pm_event_slug": ev.get("slug"),
            "pm_event_title": ev.get("title"),
            "clock": clock,
            "pm_volume_at_build": ev.get("volume"),
            "pm_liquidity_at_build": ev.get("liquidity"),
            "end_date": ev.get("endDate") or ev.get("end_date"),
            "markets": markets,
        }
        print(f"{kalshi}: {len(markets)} markets  clock={clock}  "
              f"vol={float(ev.get('volume') or 0):,.0f}  slug={ev.get('slug')}")

        # The GRAB-class guard, machine-enforced. GRAB 26AUG03's Polymarket
        # market ended 2026-08-03T21:00:00Z, three hours before the Kalshi
        # call: PM resolved on the press release, Kalshi on words spoken
        # later. Pairing them produces a number about two different events.
        call = call_time_for(kalshi, a.call_times)
        if call:
            dead = [m for m in markets
                    if m["end_date"] and _parse_utc(m["end_date"])
                    and _parse_utc(m["end_date"]) <= call]
            if dead:
                msg = (f"  {kalshi}: {len(dead)}/{len(markets)} PM markets CLOSE "
                       f"BEFORE the Kalshi call at {call:%Y-%m-%dT%H:%M:%SZ} "
                       f"(earliest {min(m['end_date'] for m in dead)}). The two "
                       f"venues never price the same instant.")
                if a.strict:
                    raise SystemExit(f"FATAL:\n{msg}")
                print(f"WARN:\n{msg}", file=sys.stderr)

    with open(a.out, "w") as f:
        json.dump(out, f, indent=2)
    total = sum(len(v["markets"]) for v in out.values())
    print(f"\nwrote {a.out}: {len(out)} event(s), {total} market(s)")


if __name__ == "__main__":
    main()
