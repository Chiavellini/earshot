#!/usr/bin/env python3
"""Does this issuer have an OPEN Kalshi mention board right now?

WHY THIS EXISTS (2026-08-04). Two earnings calls — Spotify and Progressive —
were captured to WAV by `earshot bridge-probe` while their Kalshi boards were
open and repricing. SPOT traded 157,217 contracts across 18 markets and PGR
~170,000 across 12, and we placed zero orders on either because no detector was
ever attached. Nothing in the system objected, because a record-only tool has
no idea whether the thing it is recording is tradeable.

This is the one fact that distinguishes "corpus work" from "we just missed a
board", and it is a single public, unauthenticated GET.

Two consumers, deliberately sharing one implementation:
  * the refuse-to-record guard in scripts/chain-capture.sh / bridge-probe.sh
  * the missed-arm reconciliation in scripts/missed-arm-check.py

Exit codes (the guard reads these, not stdout):
  0  an open mention board EXISTS for this issuer
  1  no open board  — recording is plainly corpus work
  2  UNDETERMINED   — network/parse failure. Never silently "no": a guard that
                     fails open is exactly how this class recurs.

Read-only. Public endpoint, no credentials.

Usage:
  scripts/board-open.py --issuer SPOT
  scripts/board-open.py --issuer PGR --json
  scripts/board-open.py --check          # offline self-test
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES_PREFIX = "KXEARNINGSMENTION"
UA = "earshot-board-open/1.0"


def series_for(issuer):
    """Issuer → mention series ticker. Inverse of discover-events.issuer_of()."""
    iss = issuer.strip().upper()
    return iss if iss.startswith(SERIES_PREFIX) else SERIES_PREFIX + iss


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def any_board(issuer, fetch=fetch):
    """→ dict(traded=bool, series=…, markets=int, volume=float) for ANY status.

    The guard asks "is it open NOW" and must fail closed. Reconciliation asks a
    different question — "was this tradeable while we were recording it?" — and
    by the time it runs the board may have settled. SPOT is exactly that: open
    and repricing at 12:02Z, closed by 15:00Z. Keying retrospection on `open`
    would clear the very miss it exists to catch.

    `traded` means markets exist AND carry volume: a listed-but-untraded board
    is not a missed opportunity.
    """
    series = series_for(issuer)
    mk = fetch(f"{BASE}/markets?series_ticker={series}&limit=200")
    markets = mk.get("markets") or []
    vol = 0.0
    for m in markets:
        v = m.get("volume_fp", m.get("volume")) or 0
        try:
            vol += float(v)
        except (TypeError, ValueError):
            pass
    return {"traded": bool(markets) and vol > 0, "series": series,
            "markets": len(markets), "volume": vol}


def event_board(event_ticker, fetch=fetch):
    """→ dict(traded, series, markets, volume) for ONE event, not the series.

    `any_board` queries by SERIES, so it sums volume across every mention event
    that issuer has ever had. That is right when all we know is an issuer (a
    bridge-probe capture names no event), and badly wrong when we do know the
    event: on 2026-08-07 the DKNG 26AUG07 board carried volume 0 while the
    series carried 135,341 from prior weeks, so grading the staged set against
    the series turned a board nobody traded into a six-figure "miss". A grader
    that inflates the stake is the same cry-wolf failure as one that invents
    the miss.

    `event_ticker` is the config's `[event].id` with any lane suffix stripped
    (`…DKNG-26AUG07-WEBCAST` → `…DKNG-26AUG07`).
    """
    et = re.sub(r"-(WEBCAST|PHONE|ACOUSTIC)$", "", event_ticker)
    mk = fetch(f"{BASE}/markets?event_ticker={et}&limit=200")
    markets = mk.get("markets") or []
    vol = 0.0
    for m in markets:
        v = m.get("volume_fp", m.get("volume")) or 0
        try:
            vol += float(v)
        except (TypeError, ValueError):
            pass
    return {"traded": bool(markets) and vol > 0, "series": et,
            "markets": len(markets), "volume": vol}


def open_board(issuer, fetch=fetch):
    """→ dict(open=bool, series=…, events=[…], markets=int).

    Raises on network/parse failure so the caller can exit 2 rather than
    mistake "we could not ask" for "there is nothing there".
    """
    series = series_for(issuer)
    ev = fetch(f"{BASE}/events?series_ticker={series}&status=open&limit=50")
    events = [e.get("event_ticker") for e in (ev.get("events") or [])
              if e.get("event_ticker")]
    if not events:
        return {"open": False, "series": series, "events": [], "markets": 0}
    mk = fetch(f"{BASE}/markets?series_ticker={series}&status=open&limit=200")
    markets = [m for m in (mk.get("markets") or [])]
    return {"open": True, "series": series, "events": events,
            "markets": len(markets)}


def check():
    """Offline self-test — injected fetchers, no network."""
    assert series_for("spot") == "KXEARNINGSMENTIONSPOT"
    assert series_for("KXEARNINGSMENTIONPGR") == "KXEARNINGSMENTIONPGR", \
        "an already-qualified series must not be double-prefixed"

    # THE PGR SHAPE: the event ticker is dated 26JUL15 and the board is open
    # anyway. The guard must key on OPEN, never on the date in the ticker —
    # that date is exactly the thing that lied on 2026-08-04.
    def pgr(url):
        if "/events?" in url:
            return {"events": [{"event_ticker": "KXEARNINGSMENTIONPGR-26JUL15"}]}
        return {"markets": [{"ticker": f"T{i}"} for i in range(12)]}
    r = open_board("PGR", fetch=pgr)
    assert r["open"] and r["markets"] == 12, r
    assert r["events"] == ["KXEARNINGSMENTIONPGR-26JUL15"], r

    def none(url):
        return {"events": []}
    assert open_board("NOPE", fetch=none)["open"] is False

    # A null `events` key must read as "no board", not explode.
    assert open_board("NULL", fetch=lambda u: {"events": None})["open"] is False

    # Failure must PROPAGATE. If this ever returns open=False on an exception,
    # the guard fails open and the whole point is lost.
    def boom(url):
        raise urllib.error.URLError("no route to host")
    try:
        open_board("X", fetch=boom)
        raise AssertionError("network failure must raise, never return False")
    except urllib.error.URLError:
        pass
    # any_board: the retrospective question, which must survive settlement.
    def settled(url):
        return {"markets": [{"ticker": "A", "volume_fp": "157217"},
                            {"ticker": "B", "volume_fp": "0"}]}
    ab = any_board("SPOT", fetch=settled)
    assert ab["traded"] and ab["markets"] == 2 and ab["volume"] == 157217.0, ab
    # listed but never traded is NOT a missed board
    assert not any_board("X", fetch=lambda u: {
        "markets": [{"ticker": "A", "volume_fp": "0"}]})["traded"]
    assert not any_board("X", fetch=lambda u: {"markets": []})["traded"]
    # junk volume must not crash the sweep
    assert not any_board("X", fetch=lambda u: {
        "markets": [{"ticker": "A", "volume_fp": None}]})["traded"]

    print("board-open --check: OK (series naming, PGR open-with-stale-ticker, "
          "empty board, null events, fail-loud on network error, any_board "
          "retrospection incl. settled/zero-volume/junk)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--issuer")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--any-status", action="store_true",
                    help="retrospective: was there a TRADED board, open or not")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        check()
        return 0
    if not a.issuer:
        ap.error("--issuer required (or --check)")
    if a.any_status:
        try:
            r = any_board(a.issuer)
        except Exception as exc:
            print(f"board-open: UNDETERMINED for {a.issuer}: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(r) if a.json else
              (f"TRADED: {r['series']} — {r['markets']} market(s), "
               f"{r['volume']:.0f} contracts" if r["traded"]
               else f"none: no traded {r['series']} board"))
        return 0 if r["traded"] else 1
    try:
        r = open_board(a.issuer)
    except Exception as exc:
        print(f"board-open: UNDETERMINED for {a.issuer}: {exc}", file=sys.stderr)
        if a.json:
            print(json.dumps({"open": None, "error": str(exc)}))
        return 2
    if a.json:
        print(json.dumps(r))
    elif r["open"]:
        print(f"OPEN: {r['series']} — {len(r['events'])} event(s), "
              f"{r['markets']} market(s): {', '.join(r['events'])}")
    else:
        print(f"none: no open {r['series']} board")
    return 0 if r["open"] else 1


if __name__ == "__main__":
    sys.exit(main())
