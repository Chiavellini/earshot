#!/usr/bin/env python3
"""pm-capture.py — read-only Polymarket book/trade capture (measure-only leg, Q-PM).

Records Polymarket mention-market top-of-book changes and trades alongside an
armed Kalshi event, so pm-edge-report.py can later join PM price motion against
our detection timestamps. It places no orders, holds no keys, and touches no
live earshot path — a pure sidecar.

Transport is curl subprocesses, NOT python's own HTTP stack: clob.polymarket.com
sits behind a TLS-fingerprint filter that 403s python/urllib regardless of
User-Agent while passing curl (verified live 2026-07-30, see VERIFICATIONS.md
Q-PM). gamma/data-api tolerate python but curl is used throughout for one code
path.

Input map (JSON, machine-generated — see Q-PM pin for the generator recipe):
  { "<KALSHI_EVENT_TICKER>": {
      "pm_event_slug": "...", "pm_event_title": "...",
      "markets": [ {"question": "...", "condition_id": "0x...",
                    "tokens": ["<yes_token>", "<no_token>"], "pm_slug": "..."} ] } }

Output: state/pm/<KALSHI_EVENT>/pm-<UTCDATE>.jsonl.gz, one row per book CHANGE
(deduped on the CLOB `hash` field) and per new trade. Every row carries
wall_ns + mono_ns (our clocks) and the venue's own timestamp, so clock mapping
stays recoverable either way.

Usage:
  python3 scripts/pm-capture.py --map configs/pm-map-26jul30.json \
      --out state/pm --until 2026-07-30T23:59:00Z
"""

import argparse
import gzip
import json
import os
import signal
import subprocess
import sys
import time
import zlib
from datetime import datetime, timezone

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
CLOB_BOOKS = "https://clob.polymarket.com/books"
DATA_TRADES = "https://data-api.polymarket.com/trades"
BOOK_TOP_LEVELS = 5
TRADES_LIMIT = 200


def curl_json(args, timeout):
    """Run curl, parse JSON. Returns (obj, None) or (None, errstring)."""
    cmd = ["curl", "-s", "--max-time", str(timeout), "-H", f"User-Agent: {UA}"] + args
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return None, "curl hard-timeout"
    if out.returncode != 0:
        return None, f"curl rc={out.returncode}"
    try:
        return json.loads(out.stdout), None
    except json.JSONDecodeError:
        body = out.stdout[:120]
        return None, f"non-JSON response: {body!r}"


class EventWriter:
    """One gzip JSONL file per Kalshi event; sync-flushed so a hard kill loses
    at most the zlib window, and the file stays a valid gzip stream."""

    def __init__(self, out_root, kalshi_event):
        d = os.path.join(out_root, kalshi_event)
        os.makedirs(d, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        self.path = os.path.join(d, f"pm-{stamp}.jsonl.gz")
        self.fh = gzip.open(self.path, "ab")
        self.rows = 0

    def write(self, row):
        self.fh.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
        self.rows += 1
        if self.rows % 50 == 0:
            self.fh.flush(zlib.Z_SYNC_FLUSH)

    def close(self):
        self.fh.flush(zlib.Z_SYNC_FLUSH)
        self.fh.close()


def top(levels):
    return [[l["price"], l["size"]] for l in (levels or [])[:BOOK_TOP_LEVELS]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True)
    ap.add_argument("--out", default="state/pm")
    ap.add_argument("--until", required=True, help="UTC ISO8601 hard stop, e.g. 2026-07-30T23:59:00Z")
    ap.add_argument("--books-interval", type=float, default=1.0)
    ap.add_argument("--trades-interval", type=float, default=10.0)
    ap.add_argument("--heartbeat-secs", type=float, default=60.0)
    args = ap.parse_args()

    until = datetime.fromisoformat(args.until.replace("Z", "+00:00")).timestamp()
    with open(args.map) as f:
        pmmap = json.load(f)

    # indexes: asset -> (event, question); trade batch URL over all condition ids
    asset_idx = {}
    cond_idx = {}
    all_tokens = []
    writers = {}
    for ev, spec in pmmap.items():
        writers[ev] = EventWriter(args.out, ev)
        for m in spec["markets"]:
            cond_idx[m["condition_id"]] = ev
            for t in m["tokens"]:
                asset_idx[t] = (ev, m["question"])
                all_tokens.append(t)
    books_payload = json.dumps([{"token_id": t} for t in all_tokens])
    trades_url = f"{DATA_TRADES}?market={','.join(cond_idx)}&limit={TRADES_LIMIT}"

    print(f"pm-capture: {len(pmmap)} events, {len(cond_idx)} markets, "
          f"{len(all_tokens)} tokens; until {args.until}", flush=True)

    stop = {"flag": False}

    def on_sig(signum, _frame):
        stop["flag"] = True
        print(f"pm-capture: signal {signum}, closing", flush=True)

    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)

    last_hash = {}          # asset -> clob book hash
    seen_trades = set()     # composite trade keys
    counts = {"book_rows": 0, "trade_rows": 0, "errors": 0}
    last_trades = 0.0
    last_beat = time.time()

    while not stop["flag"] and time.time() < until:
        cycle_start = time.time()

        books, err = curl_json(
            ["-X", "POST", "-H", "Content-Type: application/json",
             "-d", books_payload, CLOB_BOOKS], timeout=8)
        wall_ns, mono_ns = time.time_ns(), time.monotonic_ns()
        if err or not isinstance(books, list):
            counts["errors"] += 1
            if counts["errors"] % 30 == 1:
                print(f"pm-capture: books error ({err})", flush=True)
        else:
            for b in books:
                asset = b.get("asset_id")
                h = b.get("hash")
                if asset not in asset_idx or h == last_hash.get(asset):
                    continue
                last_hash[asset] = h
                ev, q = asset_idx[asset]
                bids, asks = b.get("bids") or [], b.get("asks") or []
                writers[ev].write({
                    "t": "book", "wall_ns": wall_ns, "mono_ns": mono_ns,
                    "server_ts_ms": b.get("timestamp"), "asset": asset, "q": q,
                    # CLOB levels are sorted worst-first; best is the LAST entry
                    # (verified live 2026-07-30 — see Q-PM).
                    "bb": bids[-1]["price"] if bids else None,
                    "ba": asks[-1]["price"] if asks else None,
                    "top_bids": top(list(reversed(bids))),
                    "top_asks": top(list(reversed(asks))),
                    "hash": h,
                })
                counts["book_rows"] += 1

        if cycle_start - last_trades >= args.trades_interval:
            last_trades = cycle_start
            trades, terr = curl_json([trades_url], timeout=8)
            wall_ns, mono_ns = time.time_ns(), time.monotonic_ns()
            if terr or not isinstance(trades, list):
                counts["errors"] += 1
            else:
                for t in trades:
                    cid = t.get("conditionId")
                    if cid not in cond_idx:
                        continue
                    key = (t.get("asset"), t.get("timestamp"), t.get("price"),
                           t.get("size"), t.get("side"), t.get("proxyWallet"))
                    if key in seen_trades:
                        continue
                    seen_trades.add(key)
                    writers[cond_idx[cid]].write({
                        "t": "trade", "wall_ns": wall_ns, "mono_ns": mono_ns,
                        "server_ts_s": t.get("timestamp"), "asset": t.get("asset"),
                        "q": t.get("title"), "side": t.get("side"),
                        "price": t.get("price"), "size": t.get("size"),
                    })
                    counts["trade_rows"] += 1

        if time.time() - last_beat >= args.heartbeat_secs:
            last_beat = time.time()
            print(f"pm-capture beat: books={counts['book_rows']} "
                  f"trades={counts['trade_rows']} errors={counts['errors']}", flush=True)

        time.sleep(max(0.0, args.books_interval - (time.time() - cycle_start)))

    for w in writers.values():
        w.close()
    print(f"pm-capture done: {counts}", flush=True)


if __name__ == "__main__":
    main()
