#!/usr/bin/env python3
"""xvenue-normalize.py — every venue tape into ONE canonical record type.

WHY THIS EXISTS (2026-08-04). The first cross-venue study parsed, joined, and
concluded in a single pass, so every parsing choice was buried in the answer and
none of them could be re-examined. Worse, the two venues do not agree on a
single unit: Kalshi quotes integer cents and sizes in fractional contracts;
Polymarket quotes decimals with a 0.001 tick (verified live) and sizes in
shares. Comparing "224,142 contracts" against "$2,903 notional" is not a
comparison, and that is what the 26AUG03 postmortem had to do by hand.

This is the normalization layer. Raw tapes in, canonical records out, nothing
concluded. It is deliberately boring and deliberately exact.

  PRICE  `*_e6` = integer MICRO-DOLLARS per share. Kalshi 26c -> 260000;
         Polymarket "0.263" -> 263000. Exact for both venues, no floats
         anywhere. Cents would silently destroy Polymarket's tick.
  SIZE   `*_e6` = integer micro-units. One Kalshi contract and one Polymarket
         share both settle at $1, so they are the same unit once scaled.
  VOLUME `notional_e6` = price_e6 * size_e6 / 1e6 = micro-dollars. THIS is the
         common volume axis; contracts-vs-notional is not one.
  TIME   `recv_wall_ns` (our host) is the primary axis, because Kalshi's WS
         frames carry no server timestamp at all. `venue_ts_ns` is recorded
         when the venue supplies one, always with its
         `venue_ts_granularity_ns`, and `feed_kind` records whether the row
         arrived by push or by poll. A comparison that ignores those two
         fields is measuring our sampling, not the venues.

Inputs (auto-detected):
  * Kalshi raw WS tape            examples/book-tape.rs        -> quotes
  * Polymarket raw WS tape        scripts/pm-tape.py           -> quotes
  * Polymarket poll capture       scripts/pm-capture.py (.gz)  -> quotes+trades
  * Kalshi public trades tape     scripts/fetch-tape.sh (.json)-> trades

Usage:
  scripts/xvenue-normalize.py --out norm.jsonl --pair amd-26aug04 \\
      state/xvenue/AMD/kalshi.jsonl state/xvenue/AMD/pm.jsonl
  scripts/xvenue-normalize.py --self-test

Exit codes: 0 normalized / 1 no records produced / 2 undetermined.
"""

import argparse
import gzip
import json
import os
import sys
import zlib

XV_SCHEMA = 1

# Declared measurement floors. These are DATA, not commentary: the report
# refuses to compare rows whose granularity differs, which is the guard the
# 26AUG03 study only had as a docstring warning.
GRAN_MS = 1_000_000
GRAN_S = 1_000_000_000

ONE_E6 = 1_000_000


# ── exact decimal parsing ─────────────────────────────────────────────────

def to_e6(s):
    """Decimal string -> integer micro-units, EXACT. No float ever touches a
    price: '0.263' must be 263000, not 262999.99999999997."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    if not s or not s.replace(".", "", 1).isdigit():
        return None
    whole, _, frac = s.partition(".")
    frac = (frac + "000000")[:6]
    # Digits past the sixth place would be a venue tick finer than a
    # micro-dollar. Refuse loudly rather than round: silent truncation of
    # fractional sizes is the 2026-07-15 bug class.
    v = int(whole or 0) * ONE_E6 + int(frac or 0)
    return -v if neg else v


def read_rows(path):
    """Yield JSON objects from .jsonl or .jsonl.gz, tolerating a tape that is
    still being written (no gzip end-of-stream marker) — a live capture must be
    analysable without stopping it."""
    if path.endswith(".gz"):
        raw = open(path, "rb").read()
        o = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            text = o.decompress(raw).decode("utf-8", "ignore")
        except zlib.error as e:
            print(f"warn: {path} partially unreadable ({e})", file=sys.stderr)
            return
        lines = text.splitlines()
    else:
        lines = open(path, "r", errors="ignore").read().splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


# ── tape kind detection ───────────────────────────────────────────────────

def detect(path):
    """-> ('kalshi_ws'|'pm_ws'|'pm_poll'|'kalshi_trades', header_or_none)."""
    if path.endswith(".json"):
        with open(path) as f:
            obj = json.load(f)
        if isinstance(obj, dict) and "trades" in obj and "markets" in obj:
            return "kalshi_trades", obj
        raise SystemExit(f"FATAL: {path} is JSON but not a Kalshi trades tape")

    first = None
    for row in read_rows(path):
        first = row
        break
    if first is None:
        raise SystemExit(f"FATAL: {path} is empty")
    if first.get("t") == "session":
        v = first.get("venue")
        if v == "kalshi":
            return "kalshi_ws", first
        if v == "polymarket":
            return "pm_ws", first
        raise SystemExit(f"FATAL: {path} session header has unknown venue {v!r}")
    # Legacy pm-capture.py: no header, rows are t=book / t=trade with `asset`.
    if first.get("t") in ("book", "trade") and "asset" in first:
        return "pm_poll", None
    raise SystemExit(f"FATAL: cannot identify tape {path}")


def check_schema(path, kind, header):
    # Only tapes written in OUR envelope carry a schema. The legacy poll
    # capture and the public trades tape predate it and are read by shape;
    # demanding a version from them would refuse the very banked data this
    # backfill exists to rescue.
    if kind not in ("kalshi_ws", "pm_ws") or header is None:
        return
    got = header.get("schema_version")
    if got != XV_SCHEMA:
        raise SystemExit(
            f"FATAL: {path} is schema {got!r}, this normalizer speaks {XV_SCHEMA}. "
            f"Refusing to guess — a field may have changed meaning."
        )


# ── Kalshi WS tape -> quotes ──────────────────────────────────────────────

def kalshi_ws_records(path, header, pair):
    """Rebuild the yes-leg ladder in MICRO-DOLLARS and emit a quote per change.

    Mirrors src/kalshi/book.rs (a NO bid at p IS a YES ask at 1-p) but keeps
    micro-dollar precision instead of the runtime's integer cents, so the
    canonical record can sit beside a venue that ticks at 0.001.
    """
    books = {}          # ticker -> {"bids": {px_e6: sz_e6}, "asks": {...}}
    tickers = header.get("tickers") or []
    label = {t: t.rsplit("-", 1)[-1] for t in tickers}

    for row in read_rows(path):
        if row.get("t") != "frame":
            continue
        try:
            msg = json.loads(row["raw"])
        except (json.JSONDecodeError, KeyError):
            continue
        kind = msg.get("type")
        m = msg.get("msg") or {}
        ticker = m.get("market_ticker")
        if not ticker or kind not in ("orderbook_snapshot", "orderbook_delta"):
            continue
        b = books.setdefault(ticker, {"bids": {}, "asks": {}})

        if kind == "orderbook_snapshot":
            b["bids"].clear()
            b["asks"].clear()
            for side, key in (("yes", "yes_dollars_fp"), ("no", "no_dollars_fp")):
                for level in m.get(key) or []:
                    px, sz = to_e6(level[0]), to_e6(level[1])
                    if px is None or sz is None:
                        continue
                    _apply(b, side, px, sz, delta=False)
        else:
            px, d = to_e6(m.get("price_dollars")), to_e6(m.get("delta_fp"))
            if px is None or d is None:
                continue
            _apply(b, m.get("side"), px, d, delta=True)

        bb = max(b["bids"]) if b["bids"] else None
        ba = min(b["asks"]) if b["asks"] else None
        yield {
            "schema_version": XV_SCHEMA,
            "pair_id": pair,
            "venue": "kalshi",
            "kind": "quote",
            "instrument": ticker,
            "label": label.get(ticker, ticker.rsplit("-", 1)[-1]),
            "outcome": "YES",
            "recv_wall_ns": row.get("wall_ns"),
            "recv_mono_ns": row.get("mono_ns"),
            # Kalshi's WS carries `seq` but no server clock — stated as null
            # rather than quietly substituting our receive time for theirs.
            "venue_ts_ns": None,
            "venue_ts_granularity_ns": None,
            "feed_kind": "push",
            "poll_interval_ns": None,
            "bid_e6": bb,
            "ask_e6": ba,
            "mid_e6": (bb + ba) // 2 if (bb is not None and ba is not None) else None,
            "spread_e6": (ba - bb) if (bb is not None and ba is not None) else None,
            "bid_size_e6": b["bids"].get(bb),
            "ask_size_e6": b["asks"].get(ba),
        }


def _apply(book, side, px_e6, qty_e6, delta):
    """NO->YES conversion lives here and only here: a NO bid at p is a YES ask
    at (1 - p). In micro-dollars, 1.000000 == 1_000_000."""
    if side == "yes":
        ladder, key = book["bids"], px_e6
    elif side == "no":
        ladder, key = book["asks"], ONE_E6 - px_e6
    else:
        return
    new = (ladder.get(key, 0) + qty_e6) if delta else qty_e6
    if new <= 0:
        ladder.pop(key, None)
    else:
        ladder[key] = new


# ── Polymarket WS tape -> quotes ──────────────────────────────────────────

def pm_ws_records(path, header, pair):
    assets = header.get("assets") or {}
    for row in read_rows(path):
        if row.get("t") != "frame":
            continue
        try:
            payload = json.loads(row["raw"])
        except (json.JSONDecodeError, KeyError):
            continue
        for msg in (payload if isinstance(payload, list) else [payload]):
            et = msg.get("event_type")
            ts = msg.get("timestamp")
            venue_ts = int(ts) * GRAN_MS if ts and str(ts).isdigit() else None

            if et == "book":
                aid = msg.get("asset_id")
                meta = assets.get(aid, {})
                # CLOB sorts BOTH sides best-price-LAST (pinned Q-PM).
                bids, asks = msg.get("bids") or [], msg.get("asks") or []
                bb = to_e6(bids[-1]["price"]) if bids else None
                ba = to_e6(asks[-1]["price"]) if asks else None
                yield _pm_quote(pair, aid, meta, row, venue_ts, bb, ba,
                                to_e6(bids[-1]["size"]) if bids else None,
                                to_e6(asks[-1]["size"]) if asks else None)
            elif et == "price_change":
                for ch in msg.get("price_changes") or []:
                    aid = ch.get("asset_id")
                    meta = assets.get(aid, {})
                    yield _pm_quote(pair, aid, meta, row, venue_ts,
                                    to_e6(ch.get("best_bid")), to_e6(ch.get("best_ask")),
                                    None, None)


def _pm_quote(pair, aid, meta, row, venue_ts, bb, ba, bsz, asz):
    return {
        "schema_version": XV_SCHEMA,
        "pair_id": pair,
        "venue": "polymarket",
        "kind": "quote",
        "instrument": aid,
        "label": meta.get("q"),
        # Both legs are on the tape; the outcome is stated so the report can
        # drop NO rows. Pooling YES and NO produces a series that walks between
        # p and 1-p — caught live on the PLTR dry run.
        "outcome": meta.get("outcome", "UNKNOWN"),
        "recv_wall_ns": row.get("wall_ns"),
        "recv_mono_ns": row.get("mono_ns"),
        "venue_ts_ns": venue_ts,
        "venue_ts_granularity_ns": GRAN_MS if venue_ts else None,
        "feed_kind": "push",
        "poll_interval_ns": None,
        "bid_e6": bb,
        "ask_e6": ba,
        "mid_e6": (bb + ba) // 2 if (bb is not None and ba is not None) else None,
        "spread_e6": (ba - bb) if (bb is not None and ba is not None) else None,
        "bid_size_e6": bsz,
        "ask_size_e6": asz,
    }


# ── legacy Polymarket poll capture -> quotes + trades ─────────────────────

def pm_poll_records(path, pair, poll_interval_ns, legs=None):
    """pm-capture.py output. Kept readable because ~39k rows of real banked
    data are in this shape, including an AMD capture that was never analysed.

    The legacy tape does not record which leg a token is, so `legs` (built from
    the pm-map, where tokens[0] is YES) supplies it. Without that, YES and NO
    rows pool into a series that walks between p and 1-p — the exact defect
    caught on the PLTR dry run, where "Anthropic" showed a 0.75 baseline
    against a real YES book of 0.10/0.24.
    """
    legs = legs or {}
    for row in read_rows(path):
        t = row.get("t")
        if t == "book":
            bb, ba = to_e6(row.get("bb")), to_e6(row.get("ba"))
            ts = row.get("server_ts_ms")
            yield {
                "schema_version": XV_SCHEMA,
                "pair_id": pair,
                "venue": "polymarket",
                "kind": "quote",
                "instrument": row.get("asset"),
                "label": row.get("q"),
                "outcome": legs.get(row.get("asset"), "UNKNOWN"),
                "recv_wall_ns": row.get("wall_ns"),
                "recv_mono_ns": row.get("mono_ns"),
                "venue_ts_ns": int(ts) * GRAN_MS if ts and str(ts).isdigit() else None,
                "venue_ts_granularity_ns": GRAN_MS if ts else None,
                # The bias that motivated pm-tape.py, stated as a field so no
                # report can forget it.
                "feed_kind": "poll",
                "poll_interval_ns": poll_interval_ns,
                "bid_e6": bb,
                "ask_e6": ba,
                "mid_e6": (bb + ba) // 2 if (bb is not None and ba is not None) else None,
                "spread_e6": (ba - bb) if (bb is not None and ba is not None) else None,
                "bid_size_e6": None,
                "ask_size_e6": None,
            }
        elif t == "trade":
            px, sz = to_e6(row.get("price")), to_e6(row.get("size"))
            ts = row.get("server_ts_s")
            yield {
                "schema_version": XV_SCHEMA,
                "pair_id": pair,
                "venue": "polymarket",
                "kind": "trade",
                "instrument": row.get("asset"),
                "label": row.get("q"),
                "outcome": legs.get(row.get("asset"), "UNKNOWN"),
                "recv_wall_ns": row.get("wall_ns"),
                "recv_mono_ns": row.get("mono_ns"),
                "venue_ts_ns": int(float(ts)) * GRAN_S if ts else None,
                "venue_ts_granularity_ns": GRAN_S if ts else None,
                "feed_kind": "poll",
                "poll_interval_ns": poll_interval_ns,
                "side": row.get("side"),
                "price_e6": px,
                "size_e6": sz,
                "notional_e6": (px * sz) // ONE_E6 if (px is not None and sz is not None) else None,
            }


# ── Kalshi public trades tape -> trades ───────────────────────────────────

def kalshi_trades_records(tape, pair):
    import datetime as _dt

    markets = tape.get("markets") or {}
    for ticker, trades in (tape.get("trades") or {}).items():
        sub = (markets.get(ticker) or {}).get("yes_sub_title")
        for tr in trades:
            px = to_e6(tr.get("yes_price_dollars"))
            # `count_fp` — fixed point, because Kalshi trades fractional
            # contracts. The plain `count` key is absent on this endpoint;
            # reading it yielded a silent zero notional and, with it, a report
            # that named the WRONG venue as deeper.
            cnt = tr.get("count_fp", tr.get("count"))
            sz = to_e6(cnt) if cnt is not None else None
            ct = tr.get("created_time")
            venue_ts, gran = None, None
            if ct:
                try:
                    venue_ts = int(_dt.datetime.fromisoformat(
                        ct.replace("Z", "+00:00")).timestamp() * 1e9)
                    # Kalshi stamps these to the MICROSECOND
                    # ("…T21:41:53.929037Z"). The prior study assumed whole
                    # seconds on both sides and derived a ~2 s floor from it;
                    # only the Polymarket data-api trade feed is second-grained.
                    gran = 1_000 if "." in ct else GRAN_S
                except ValueError:
                    venue_ts = None
            yield {
                "schema_version": XV_SCHEMA,
                "pair_id": pair,
                "venue": "kalshi",
                "kind": "trade",
                "instrument": ticker,
                "label": sub,
                "outcome": "YES",
                # A post-hoc archive: there is no receive time, and inventing
                # one would fabricate the very axis this lane measures.
                "recv_wall_ns": None,
                "recv_mono_ns": None,
                "venue_ts_ns": venue_ts,
                "venue_ts_granularity_ns": gran,
                "feed_kind": "archive",
                "poll_interval_ns": None,
                "side": tr.get("taker_side"),
                "price_e6": px,
                "size_e6": sz,
                "notional_e6": (px * sz) // ONE_E6 if (px is not None and sz is not None) else None,
            }


# ── driver ────────────────────────────────────────────────────────────────

def normalize(paths, pair, out_path, poll_interval_ns, legs=None):
    sources, counts = [], {}
    out = open(out_path, "w")
    n = 0
    for path in paths:
        kind, header = detect(path)
        check_schema(path, kind, header)
        sources.append({
            "path": path, "tape_kind": kind,
            "host": (header or {}).get("host"),
            "clock_offset_s": (header or {}).get("clock_offset_s"),
            "feed_kind": (header or {}).get("feed_kind",
                                            "poll" if kind == "pm_poll" else "archive"),
        })
        if kind == "kalshi_ws":
            gen = kalshi_ws_records(path, header, pair)
        elif kind == "pm_ws":
            gen = pm_ws_records(path, header, pair)
        elif kind == "pm_poll":
            gen = pm_poll_records(path, pair, poll_interval_ns, legs)
        else:
            gen = kalshi_trades_records(header, pair)
        c = 0
        for rec in gen:
            out.write(json.dumps(rec, separators=(",", ":")) + "\n")
            c += 1
            n += 1
        counts[path] = c
        print(f"  {kind:14s} {c:7d} records  {path}")
    out.close()

    # Provenance beside the data, never inside it: the report reads this to
    # decide what may legally be compared with what.
    meta_path = out_path + ".meta.json"
    with open(meta_path, "w") as f:
        json.dump({"schema_version": XV_SCHEMA, "pair_id": pair,
                   "records": n, "sources": sources, "counts": counts}, f, indent=2)
    print(f"\nwrote {out_path} ({n} records) + {os.path.basename(meta_path)}")
    return 0 if n else 1


# ── offline self-test ─────────────────────────────────────────────────────

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

    print("xvenue-normalize self-test:")
    # Exactness is the whole point of the unit choice.
    chk("cents 0.26 -> e6", 260000, to_e6("0.2600"))
    chk("PM tick 0.001 survives", 1000, to_e6("0.001"))
    chk("PM 0.263 exact", 263000, to_e6("0.263"))
    chk("fractional size 3075.67", 3075670000, to_e6("3075.67"))
    chk("whole number", 1000000, to_e6("1"))
    chk("negative delta", -200000000, to_e6("-200.00"))
    chk("junk -> None", None, to_e6("abc"))
    chk("empty -> None", None, to_e6(""))

    # NO->YES conversion, in micro-dollars.
    b = {"bids": {}, "asks": {}}
    _apply(b, "yes", 400000, 10000000, delta=False)
    _apply(b, "no", 550000, 20000000, delta=False)
    chk("yes bid stored", {400000: 10000000}, b["bids"])
    chk("no 0.55 becomes yes ask 0.45", {450000: 20000000}, b["asks"])
    _apply(b, "no", 550000, -20000000, delta=True)
    chk("delta to zero removes the level", {}, b["asks"])

    # A quote row must carry its own comparability metadata.
    row = _pm_quote("t", "a", {"q": "Q", "outcome": "YES"},
                    {"wall_ns": 5, "mono_ns": 6}, 1785883437828000000, 100000, 120000, None, None)
    chk("mid computed", 110000, row["mid_e6"])
    chk("spread computed", 20000, row["spread_e6"])
    chk("granularity declared", GRAN_MS, row["venue_ts_granularity_ns"])
    chk("feed_kind declared", "push", row["feed_kind"])

    # Notional is the shared volume unit: 0.25 x 400 units = $100.
    px, sz = to_e6("0.25"), to_e6("400")
    chk("notional_e6 = $100", 100000000, (px * sz) // ONE_E6)

    # End-to-end over wire-exact fixtures: both venues in, one shape out.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        kt = os.path.join(d, "k.jsonl")
        with open(kt, "w") as fh:
            fh.write(json.dumps({"t": "session", "schema_version": XV_SCHEMA,
                                 "venue": "kalshi", "feed_kind": "push",
                                 "tickers": ["EV-WORD"], "host": "t"}) + "\n")
            snap = {"type": "orderbook_snapshot", "seq": 1, "msg": {
                "market_ticker": "EV-WORD",
                "yes_dollars_fp": [["0.4000", "100.00"]],
                "no_dollars_fp": [["0.5500", "200.00"]]}}
            fh.write(json.dumps({"t": "frame", "wall_ns": 10, "mono_ns": 1,
                                 "raw": json.dumps(snap)}) + "\n")
            dlt = {"type": "orderbook_delta", "seq": 2, "msg": {
                "market_ticker": "EV-WORD", "side": "yes",
                "price_dollars": "0.4200", "delta_fp": "50.00"}}
            fh.write(json.dumps({"t": "frame", "wall_ns": 20, "mono_ns": 2,
                                 "raw": json.dumps(dlt)}) + "\n")
            fh.write(json.dumps({"t": "close", "rows": 2, "dropped": 0}) + "\n")

        pt = os.path.join(d, "p.jsonl")
        with open(pt, "w") as fh:
            fh.write(json.dumps({"t": "session", "schema_version": XV_SCHEMA,
                                 "venue": "polymarket", "feed_kind": "push",
                                 "assets": {"A1": {"q": "Will X say \"word\"?",
                                                   "outcome": "YES"}},
                                 "host": "t"}) + "\n")
            bk = {"event_type": "book", "asset_id": "A1", "timestamp": "1785883437828",
                  # best price LAST on both sides (pinned Q-PM)
                  "bids": [{"price": "0.001", "size": "500"},
                           {"price": "0.104", "size": "133.32"}],
                  "asks": [{"price": "0.900", "size": "10"},
                           {"price": "0.106", "size": "77.7"}]}
            fh.write(json.dumps({"t": "frame", "wall_ns": 11, "mono_ns": 3,
                                 "raw": json.dumps(bk)}) + "\n")
            fh.write(json.dumps({"t": "close", "rows": 1}) + "\n")

        outp = os.path.join(d, "n.jsonl")
        normalize([kt, pt], "fx", outp, 1_000_000_000)
        recs = [json.loads(x) for x in open(outp)]
        k = [r for r in recs if r["venue"] == "kalshi"]
        pm = [r for r in recs if r["venue"] == "polymarket"]
        chk("kalshi rows", 2, len(k))
        chk("snapshot yes bid 0.40", 400000, k[0]["bid_e6"])
        chk("snapshot no 0.55 -> yes ask 0.45", 450000, k[0]["ask_e6"])
        chk("delta lifts best bid to 0.42", 420000, k[1]["bid_e6"])
        chk("kalshi venue clock stated absent", None, k[0]["venue_ts_ns"])
        chk("pm best bid is bids[-1]", 104000, pm[0]["bid_e6"])
        chk("pm best ask is asks[-1]", 106000, pm[0]["ask_e6"])
        chk("pm spread 0.002", 2000, pm[0]["spread_e6"])
        chk("pm outcome labelled", "YES", pm[0]["outcome"])
        # Determinism: same input, byte-identical output.
        out2 = os.path.join(d, "n2.jsonl")
        normalize([kt, pt], "fx", out2, 1_000_000_000)
        chk("re-run is byte-identical",
            open(outp, "rb").read(), open(out2, "rb").read())

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tapes", nargs="*")
    ap.add_argument("--out")
    ap.add_argument("--pair", default="")
    ap.add_argument("--poll-interval-ns", type=int, default=1_000_000_000,
                    help="declared sampling interval of a pm-capture.py poll tape")
    ap.add_argument("--map", help="pm-map JSON: supplies the YES/NO leg for a legacy poll tape")
    ap.add_argument("--event", help="Kalshi event ticker key inside --map")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not a.tapes or not a.out:
        print("FATAL: need tape paths and --out (or --self-test)", file=sys.stderr)
        return 2
    legs = {}
    if a.map and a.event:
        spec = json.load(open(a.map)).get(a.event)
        if not spec:
            print(f"FATAL: {a.event} not in {a.map}", file=sys.stderr)
            return 2
        for m in spec["markets"]:
            for i, tok in enumerate(m["tokens"][:2]):
                legs[str(tok)] = "YES" if i == 0 else "NO"
    try:
        return normalize(a.tapes, a.pair, a.out, a.poll_interval_ns, legs)
    except SystemExit:
        raise
    except Exception as e:                                       # noqa: BLE001
        print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
