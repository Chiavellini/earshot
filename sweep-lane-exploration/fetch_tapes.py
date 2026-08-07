#!/usr/bin/env python3
"""fetch_tapes.py — bank public Kalshi trade tapes + settlements for the study.

Read-only. Public, no-auth endpoints only:
    GET /markets?event_ticker=...   -> market list, yes_sub_title, result
    GET /markets/trades?ticker=...  -> the full public trade tape

⚠ We deliberately do NOT use `scripts/fetch-tape.sh`, which writes
`state/<EVENT>/tape-*.json`. This lane must not write to `state/`. Everything
lands in `sweep-lane-exploration/tapes/<EVENT>/`, and the writer refuses any
path outside that tree.

Rate: Basic tier is 200 read tokens/s at 10 tokens/request = 20 req/s
(VERIFICATIONS Q2). We pace well under that; these are unauthenticated reads
and there is no deadline.

Resumable: an event already banked here is skipped unless --refresh.

    python3 fetch_tapes.py --list                 what would be fetched
    python3 fetch_tapes.py --labels-only          just settlements (cheap)
    python3 fetch_tapes.py                        everything missing
    python3 fetch_tapes.py --only AAL,LMT
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
import data as D  # noqa: E402

API = "https://api.elections.kalshi.com/trade-api/v2"
TAPES = os.path.join(HERE, "tapes")
PACE_S = 0.12          # ~8 req/s, comfortably under the 20 req/s read budget
RETRIES = 3


def _safe_write(path, obj):
    """Write JSON, refusing any destination outside this lane's tapes/ tree."""
    full = os.path.abspath(path)
    if not full.startswith(os.path.abspath(TAPES) + os.sep):
        raise RuntimeError(f"refusing to write outside tapes/: {full}")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    tmp = full + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, full)


def _get(url):
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(1.0 + attempt)
                continue
            if 500 <= e.code < 600:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {RETRIES} tries: {url} ({last})")


def _paged(url, key):
    out, cursor = [], ""
    while True:
        u = url + (f"&cursor={cursor}" if cursor else "")
        d = _get(u)
        batch = d.get(key) or []
        out.extend(batch)
        cursor = d.get("cursor") or ""
        time.sleep(PACE_S)
        if not cursor or not batch:
            return out


def fetch_markets(event):
    """[{ticker, yes_sub_title, status, result}] for one event."""
    rows = _paged(f"{API}/markets?event_ticker={event}&limit=1000", "markets")
    return [{"ticker": m.get("ticker"), "yes_sub_title": m.get("yes_sub_title", ""),
             "status": m.get("status", ""), "result": m.get("result", "")}
            for m in rows if m.get("ticker")]


def fetch_trades(ticker):
    return _paged(f"{API}/markets/trades?ticker={ticker}&limit=1000", "trades")


def bank(event, labels_only=False, refresh=False):
    """Fetch one event into tapes/<EVENT>/. Returns a short status string."""
    out_dir = os.path.join(TAPES, event)
    tape_path = os.path.join(out_dir, "tape-fetched.json")
    setl_path = os.path.join(out_dir, "settlements.json")

    if not refresh and os.path.exists(setl_path) and (labels_only or os.path.exists(tape_path)):
        return "skip (already banked)"

    markets = fetch_markets(event)
    if not markets:
        return "EMPTY (no markets returned)"
    _safe_write(setl_path, markets)
    settled = sum(1 for m in markets if m["result"] in ("yes", "no"))
    if labels_only:
        return f"labels only: {len(markets)} markets, {settled} settled"

    trades = {}
    for m in markets:
        trades[m["ticker"]] = fetch_trades(m["ticker"])
    _safe_write(tape_path, {
        "event": event,
        "fetched_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "sweep-lane-exploration/fetch_tapes.py (public, no-auth)",
        "markets": {m["ticker"]: {k: m[k] for k in ("yes_sub_title", "status", "result")}
                    for m in markets},
        "trades": trades,
    })
    n = sum(len(v) for v in trades.values())
    return f"{len(markets)} markets, {settled} settled, {n} trades"


def main(argv):
    only = None
    for a in argv:
        if a.startswith("--only"):
            only = {s.strip().upper() for s in a.split("=", 1)[-1].split(",")} \
                if "=" in a else None
    if "--only" in argv:
        i = argv.index("--only")
        if i + 1 < len(argv):
            only = {s.strip().upper() for s in argv[i + 1].split(",")}

    labels_only = "--labels-only" in argv
    refresh = "--refresh" in argv

    inv = {r["event"]: r for r in D.inventory()}
    targets = []
    for ev, r in sorted(inv.items()):
        if only and not any(k in ev for k in only):
            continue
        need_tape = r["markets"] == 0
        need_lab = r["labelled"] == 0
        if refresh or need_tape or need_lab:
            targets.append((ev, "tape+labels" if need_tape else "labels"))

    if "--list" in argv:
        print(f"{len(targets)} event(s) would be fetched:")
        for ev, what in targets:
            print(f"  {ev:<40} {what}")
        return 0

    print(f"fetching {len(targets)} event(s) into tapes/  (public, no-auth, read-only)")
    ok = fail = 0
    for i, (ev, what) in enumerate(targets, 1):
        lo = labels_only or what == "labels"
        try:
            msg = bank(ev, labels_only=lo, refresh=refresh)
            print(f"  [{i:>2}/{len(targets)}] {ev:<40} {msg}")
            ok += 1
        except Exception as e:                      # noqa: BLE001 - report, keep going
            print(f"  [{i:>2}/{len(targets)}] {ev:<40} FAILED: {e}")
            fail += 1
    print(f"\ndone: {ok} ok, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
