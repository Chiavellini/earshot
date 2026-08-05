#!/usr/bin/env python3
"""xvenue-report.py — the frozen cross-venue artifact: Δtime and Δvolume.

WHY THIS EXISTS (2026-08-04). `pm-edge-report.py` answered the question once,
to a terminal. It had no `--out`, so the only surviving record of the 26AUG03
result is a table hand-typed into a postmortem; its inputs sat under gitignored
`state/`; and its one Δt came from an unpinned `--move-threshold` default that,
when nudged 5c, moved the answer from -6.2 s to -187.5 s. None of that is
reproducible, so none of it is evidence.

This writes a JSON artifact, a markdown rendering, and a row in a committed
ledger, from normalized records only (scripts/xvenue-normalize.py).

Two axes, never blended:

  DeltaVOLUME  Both venues in micro-dollar NOTIONAL, the only unit they share.
               Plus spread and top-of-book depth. This axis worked on 26AUG03
               (Kalshi 65x Polymarket on the identical event) and is the half
               that survives thin Polymarket books.

  DeltaTIME    Lead/lag from mid-price motion. Estimated INDEPENDENTLY on
               contiguous segments and reported as median + spread across
               segments, because a single whole-session number cannot tell a
               real lag from an artifact. Segments that disagree are the
               finding, not noise to average away.

Refusals are first-class. The tool emits NOT_RESOLVABLE, UNSTABLE, or VOID
rather than a number it cannot defend:
  * comparing a `poll` feed against a `push` feed without declaring the bias
  * comparing rows whose `venue_ts_granularity_ns` differ
  * a lag smaller than the joint measurement floor
  * a Polymarket market that closes before the Kalshi instrument opens
    (the GRAB class: no shared instant exists, so no lag exists to find)

Usage:
  scripts/xvenue-report.py --norm norm.jsonl --pair pltr-26aug03 \\
      --out state/xvenue/PLTR/report.json [--anchor 2026-08-03T21:05:00Z]
  scripts/xvenue-report.py --self-test

Exit codes: 0 report written / 1 nothing comparable / 2 undetermined.
"""

import argparse
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone

XV_SCHEMA = 2
# Normalized-row schemas this report can read. Schema 1 rows carry no ladder,
# so the takeable axis refuses on them rather than reporting a zero — a zero
# would read as "no liquidity", which is the opposite of "not recorded".
XV_ROW_SCHEMA_ACCEPTED = (1, 2)
ONE_E6 = 1_000_000

# ── takeable axis ─────────────────────────────────────────────────────────
# Edge tiers in micro-dollars. "Takeable at >=10c" means: resting size we could
# lift at a price that still leaves 10c per $1 IF the word is said. This is a
# different question from Δvolume — how much TRADED — and it is the one that
# decides whether a venue is worth executing on. A venue can trade almost
# nothing and still be worth taking from; that is what thin, slow flow IS.
EDGE_TIERS_E6 = (50_000, 100_000, 200_000, 500_000)
# Windows around the mention anchor. 5s is roughly our capture→decision budget,
# 30s is a human reacting to the same words, 300s is the rest of the call.
TAKEABLE_WINDOWS_S = (5, 30, 300)
# Ordered worst-to-best so a mixed set reports its WORST provenance.
DEPTH_RANK = {"none": 0, "top5": 1, "full": 2}
# A word is priced when the Kalshi tape crosses from clearly-unsaid to
# clearly-said. Deliberately wide: a narrow band makes the anchor sensitive to
# a single print, which is the 26AUG03 --move-threshold failure.
ANCHOR_LOW_E6 = 600_000
ANCHOR_HIGH_E6 = 900_000

# Resample grid for the lag estimator. 250 ms is well under any lag worth
# trading and well above scheduler jitter on either recorder.
GRID_NS = 250_000_000
MAX_LAG_STEPS = 40           # +-10 s at 250 ms
MIN_SEGMENTS = 4
SEGMENT_TARGET = 8
# A session shorter than this is a smoke test, not an event. It may still write
# a report; it must NEVER write a ledger row, because the ledger is what the
# node's ">=3 overlapping events" gate is counted from and a 40 s health check
# flipping that gate to MET would be a fabricated result.
MIN_LEDGER_SPAN_S = 300
# A lag inside the joint floor is not a small lag, it is no measurement.
FLOOR_MULTIPLE = 1.0

QUOTED = re.compile(r'"([^"]+)"')


def load(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    for r in rows:
        if r.get("schema_version") not in XV_ROW_SCHEMA_ACCEPTED:
            raise SystemExit(
                f"FATAL: {path} holds schema {r.get('schema_version')!r}, "
                f"this report reads {XV_ROW_SCHEMA_ACCEPTED}")
    return rows


# ── comparability ─────────────────────────────────────────────────────────

def joint_floor_ns(k_rows, p_rows):
    """The coarser of the two sides' measurement floors.

    A pushed feed's floor is the grid we resample onto; a polled feed's floor
    is its sampling interval, which is far coarser and is the reason
    scripts/pm-tape.py exists.
    """
    def side(rows):
        worst = GRID_NS
        for r in rows:
            if r.get("feed_kind") == "poll" and r.get("poll_interval_ns"):
                worst = max(worst, r["poll_interval_ns"])
            g = r.get("venue_ts_granularity_ns")
            if r.get("feed_kind") == "archive" and g:
                worst = max(worst, g)
        return worst
    return max(side(k_rows), side(p_rows))


def feed_kinds(rows):
    return sorted({r.get("feed_kind") for r in rows if r.get("feed_kind")})


# ── Δvolume ───────────────────────────────────────────────────────────────

def volume_axis(rows):
    """Per-venue traded notional, counts, spread and depth — in shared units.

    Callers MUST pass the YES-filtered universe. Polymarket lists both legs of
    every market, so counting the raw tape doubles its instrument count and its
    notional against a Kalshi side that only has a yes leg.
    """
    out = {}
    for venue in ("kalshi", "polymarket"):
        tr = [r for r in rows if r["venue"] == venue and r["kind"] == "trade"]
        qt = [r for r in rows if r["venue"] == venue and r["kind"] == "quote"]
        notional = sum(r.get("notional_e6") or 0 for r in tr)
        units = sum(r.get("size_e6") or 0 for r in tr)
        spreads = [r["spread_e6"] for r in qt if r.get("spread_e6") is not None]
        depth = [(r.get("bid_size_e6") or 0) + (r.get("ask_size_e6") or 0)
                 for r in qt if r.get("bid_size_e6") is not None
                 or r.get("ask_size_e6") is not None]
        out[venue] = {
            "trades": len(tr),
            "quotes": len(qt),
            "instruments": len({r["instrument"] for r in rows if r["venue"] == venue}),
            "traded_units": round(units / ONE_E6, 2),
            "traded_notional_usd": round(notional / ONE_E6, 2),
            "median_spread_usd": round(statistics.median(spreads) / ONE_E6, 4) if spreads else None,
            "median_top_depth_units": round(statistics.median(depth) / ONE_E6, 2) if depth else None,
            "feed_kinds": feed_kinds([r for r in rows if r["venue"] == venue]),
        }
    # Same-window comparison. A ratio taken over two tapes that cover
    # different spans is not a venue measurement, it is a coverage
    # measurement — and this was live: the AMD 26AUG04 Polymarket capture
    # ended ~24h BEFORE the call, so only 4.4% of Kalshi's notional on that
    # event sits inside the shared window. Comparing the full tapes reported
    # 292x; the same window reports 10.8x.
    kts = [r["venue_ts_ns"] for r in rows
           if r["venue"] == "kalshi" and r.get("venue_ts_ns")]
    pts = [r["venue_ts_ns"] for r in rows
           if r["venue"] == "polymarket" and r.get("venue_ts_ns")]
    if kts and pts:
        lo, hi = max(min(kts), min(pts)), min(max(kts), max(pts))
        shared = {}
        for venue in ("kalshi", "polymarket"):
            tot = sum(r.get("notional_e6") or 0 for r in rows
                      if r["venue"] == venue and r["kind"] == "trade")
            inw = sum(r.get("notional_e6") or 0 for r in rows
                      if r["venue"] == venue and r["kind"] == "trade"
                      and r.get("venue_ts_ns") and lo <= r["venue_ts_ns"] <= hi)
            shared[venue] = {
                "traded_notional_usd": round(inw / ONE_E6, 2),
                # What fraction of this venue's activity the shared window
                # actually contains. A low number here invalidates the
                # full-tape ratio above it.
                "coverage_pct": round(100.0 * inw / tot, 1) if tot else None,
            }
        kn, pn = (shared["kalshi"]["traded_notional_usd"],
                  shared["polymarket"]["traded_notional_usd"])
        shared["window_s"] = round((hi - lo) / 1e9, 1)
        shared["notional_kalshi_per_pm"] = round(kn / pn, 2) if pn else None
        out["shared_window"] = shared
    else:
        out["shared_window"] = None

    k, p = out["kalshi"], out["polymarket"]
    out["ratio"] = {
        "trades_kalshi_per_pm": round(k["trades"] / p["trades"], 2) if p["trades"] else None,
        "notional_kalshi_per_pm": (round(k["traded_notional_usd"] / p["traded_notional_usd"], 2)
                                   if p["traded_notional_usd"] else None),
        "deeper_venue": (None if not (k["traded_notional_usd"] or p["traded_notional_usd"])
                         else ("kalshi" if k["traded_notional_usd"] >= p["traded_notional_usd"]
                               else "polymarket")),
    }
    return out


# ── Δdepth: what is actually takeable ─────────────────────────────────────

def mention_anchor(k_trades, ticker, lo_ns=None, hi_ns=None):
    """When did Kalshi decide the word was said? -> venue_ts_ns, or None.

    `lo_ns`/`hi_ns` bound the search to the window where the OTHER venue
    actually has data. Without that bound this is wrong, not merely imprecise:
    the Kalshi trades tape for PLTR spans 13 days across two quarters, and the
    unbounded search anchored "international" on a crossing 3.9 days before
    the call it was supposed to describe.

    The Kalshi public trades tape is microsecond-stamped and dense (hundreds of
    prints per market against Polymarket's handful), so it is the only clock in
    this dataset precise enough to anchor a window. Both venues' `venue_ts_ns`
    are true UTC, which is what makes them comparable; `recv_wall_ns` is not,
    because the archive side has none.

    Deliberately NOT anchored on our own detections: the arm report carries
    `audio_ts`/`decision_ts` as monotonic nanoseconds with no wall-clock pair
    anywhere in the run's artifacts, so a detection cannot be placed on a venue
    clock at all. Fixing that is a one-field change in the arm path.
    """
    s = sorted((r["venue_ts_ns"], r["price_e6"]) for r in k_trades
               if r["instrument"] == ticker and r.get("venue_ts_ns")
               and r.get("price_e6") is not None
               and (hi_ns is None or r["venue_ts_ns"] <= hi_ns))
    below = [t for t, px in s if px < ANCHOR_LOW_E6]
    if not below:
        return None                      # never cheap: nothing to cross FROM
    t0 = below[-1]
    above = [t for t, px in s if t > t0 and px > ANCHOR_HIGH_E6]
    if not above:
        return None                      # never dear: the word was not said
    # Only the CROSSING has to fall inside the other venue's window. The
    # unsaid state that precedes it routinely predates the capture by days —
    # a contract can sit at 5c for a week before the call — so bounding the
    # `below` search too would reject every real anchor.
    if lo_ns is not None and above[0] < lo_ns:
        return None
    return above[0]


def _ts(r):
    """The clock to place a quote on. The venue's own when it gives one (both
    PM feeds do, and the Kalshi trades archive does); our receive clock
    otherwise, because Kalshi's book WS carries `seq` but no server time. The
    tape header records `clock_offset_s` so the substitution stays bounded."""
    return r.get("venue_ts_ns") or r.get("recv_wall_ns")


def _takeable_at(ladder, cap_e6):
    """USD resting on the ask side at a price at or below `cap_e6`."""
    return sum(px * sz for px, sz in ladder or [] if px <= cap_e6) / (ONE_E6 * ONE_E6)


def _sellable_at(ladder, floor_e6):
    """USD receivable by hitting YES bids at or above `floor_e6`.

    The mirror trade: selling YES is profitable when the word is NOT said. It
    is reported for completeness, but it is NOT the detection-actionable side —
    "was said" is a signal we get in milliseconds, "will never be said" is one
    we only have at the end of the call.
    """
    return sum(px * sz for px, sz in ladder or [] if px >= floor_e6) / (ONE_E6 * ONE_E6)


def _window_stats(samples, t0, t1):
    """Time-weighted statistics over [t0, t1]. -> dict or None.

    TWO things this gets right that a plain median over snapshots does not,
    both of which produced wrong numbers on the first run against PLTR:

    1. LOCF seeding. The book in force at t0 is the last snapshot at or BEFORE
       t0. This matters more here than anywhere: at the instant a word is
       said, the resting book IS the stale pre-mention one. Starting at the
       first snapshot after t0 skips exactly the book we would be taking from.

    2. Time weighting. pm-capture.py dedupes on the CLOB book hash, so a
       snapshot means "the book changed", not "we sampled". A book that sits
       untouched for 200 s appears once. An unweighted median therefore counts
       a 200 s state and a 200 ms state equally — it measures how often the
       book churned, not what was resting there.
    """
    seed = [v for t, v in samples if t <= t0]
    inside = [(t, v) for t, v in samples if t0 < t <= t1]
    if seed:
        pts = [(t0, seed[-1])] + inside
    elif inside:
        pts = inside
    else:
        return None
    segs = []                                   # (duration_ns, usd)
    for i, (t, v) in enumerate(pts):
        end = pts[i + 1][0] if i + 1 < len(pts) else t1
        if end > t:
            segs.append((end - t, v))
    if not segs:
        segs = [(0, pts[-1][1])]
    total = sum(d for d, _ in segs)
    vals = sorted(segs, key=lambda s: s[1])
    acc, p50 = 0, vals[-1][1]
    for d, v in vals:
        acc += d
        if total and acc >= total / 2:
            p50 = v
            break
    best = cur = 0
    for d, v in segs:
        cur = cur + d if v > 0 else 0
        best = max(best, cur)
    return {
        "snapshots": len(pts),
        "covered_s": round(total / 1e9, 1),
        "max_usd": round(max(v for _, v in segs), 2),
        "p50_usd": round(p50, 2),
        "persisted_s": round(best / 1e9, 1),
    }


def takeable_axis(k_rows, p_rows, pairs, precision_e6, p_all_rows=None):
    """Per matched market: how much size rests at a price we could take.

    This is the axis Δvolume cannot answer. On AMD 26AUG04 Polymarket traded
    $335 all event while resting several thousand dollars at an exploitable
    price — a book almost nobody was hitting. Volume measures the competition;
    this measures the opportunity.
    """
    k_trades = [r for r in k_rows if r["kind"] == "trade"]
    # The anchor is only meaningful inside the span Polymarket actually
    # recorded; see mention_anchor().
    p_ts = [_ts(r) for r in p_rows if _ts(r)]
    lo_ns, hi_ns = (min(p_ts), max(p_ts)) if p_ts else (None, None)
    # NO-leg rows, keyed by question. Buying the NO token at q is economically
    # short-YES; hitting a NO BID at q is synthetic long YES at (1 - q). That
    # is a second pool of liquidity for the same exposure, and counting only
    # the YES ask book understates what is reachable.
    no_by_label = {}
    for r in (p_all_rows or []):
        if r.get("outcome") == "NO" and r["kind"] == "quote" and r.get("label"):
            no_by_label.setdefault(r["label"], []).append(r)
    results = []
    for kt, pt, word in pairs:
        quotes = sorted(
            (r for r in p_rows
             if r["instrument"] == pt and r["kind"] == "quote" and _ts(r)),
            key=_ts)
        entry = {"word": word, "kalshi_ticker": kt, "pm_instrument": pt,
                 "quotes": len(quotes), "anchor_ns": None, "verdict": None,
                 "note": None, "depth_kind_worst": None, "tiers": {}}

        withladder = [r for r in quotes if r.get("book_depth_kind") in ("full", "top5")]
        if not withladder:
            entry["verdict"] = "NOT_RESOLVABLE"
            # Diagnose from EVERY quote row for this instrument, not just the
            # clock-bearing ones: a row set that fails the timestamp filter
            # would otherwise report the wrong reason for the refusal.
            kinds = {r.get("book_depth_kind") for r in p_rows
                     if r["instrument"] == pt and r["kind"] == "quote"}
            entry["note"] = (
                "no row carries a ladder"
                + (" (schema 1 rows never recorded one)" if None in kinds else
                   " (best-bid/best-ask frames only — sizes were never sent)"))
            results.append(entry)
            continue

        entry["depth_kind_worst"] = min(
            (r.get("book_depth_kind") for r in withladder),
            key=lambda k: DEPTH_RANK.get(k, 0))
        anchor = mention_anchor(k_trades, kt, lo_ns, hi_ns)
        entry["anchor_ns"] = anchor
        no_rows = sorted(
            (r for r in no_by_label.get(withladder[0].get("label"), [])
             if _ts(r) and r.get("book_depth_kind") in ("full", "top5")),
            key=_ts)
        entry["no_leg_quotes"] = len(no_rows)
        t_first, t_last = _ts(withladder[0]), _ts(withladder[-1])

        for tier in EDGE_TIERS_E6:
            cap = ONE_E6 - tier                       # buy YES at or below this
            key = f"{tier // 10_000}c"
            windows = {}
            spans = [("all", (t_first, t_last))]
            if anchor:
                spans += [(f"{w}s", (anchor, anchor + w * 1_000_000_000))
                          for w in TAKEABLE_WINDOWS_S]
            for name, (t0, t1) in spans:
                yes_s = [(_ts(r), _takeable_at(r.get("asks_e6"), cap))
                         for r in withladder]
                st = _window_stats(yes_s, t0, t1)
                if st is None:
                    continue
                # Synthetic long YES by hitting NO bids at or above (1 - cap).
                if no_rows:
                    no_s = [(_ts(r), _sellable_at(r.get("bids_e6"), ONE_E6 - cap))
                            for r in no_rows]
                    nst = _window_stats(no_s, t0, t1)
                    if nst:
                        st["via_no_leg_p50_usd"] = nst["p50_usd"]
                        st["via_no_leg_max_usd"] = nst["max_usd"]
                windows[name] = st
            sell_s = [(_ts(r), _sellable_at(r.get("bids_e6"), tier))
                      for r in withladder]
            sell_st = _window_stats(sell_s, t_first, t_last)
            entry["tiers"][key] = {
                "max_price_usd": round(cap / ONE_E6, 3),
                "buy_yes": windows,
                "sell_yes_max_usd": sell_st["max_usd"] if sell_st else None,
                # EV per $1 of payout is (precision - price). A tier whose
                # price ceiling sits above our hit rate is a LOSING trade no
                # matter how much size rests there.
                "ev_positive": (None if precision_e6 is None else cap < precision_e6),
            }

        if precision_e6 is None:
            entry["verdict"] = "NOT_RESOLVABLE"
            entry["note"] = ("depth measured but no --precision supplied; a takeable "
                             "figure without a hit-rate gate is not an edge estimate")
        elif not any(t["ev_positive"] for t in entry["tiers"].values()):
            entry["verdict"] = "NOT_RESOLVABLE"
            entry["note"] = (f"no tier survives a {precision_e6 / ONE_E6:.0%} hit rate — "
                             f"every takeable price is above the break-even")
        elif anchor is None:
            entry["verdict"] = "UNANCHORED"
            entry["note"] = ("Kalshi never crossed unsaid→said for this word, so the "
                             "figures span the whole capture, not the moment that matters")
        else:
            entry["verdict"] = "RESOLVED"
        results.append(entry)
    return results


def takeable_totals(entries, precision_e6):
    """Board-level roll-up: the number the execution gate is argued from."""
    anchored = [e for e in entries if e["verdict"] == "RESOLVED"]
    out = {"markets": len(entries),
           "resolved": len(anchored),
           "unanchored": sum(1 for e in entries if e["verdict"] == "UNANCHORED"),
           "by_tier": {}}
    for tier in EDGE_TIERS_E6:
        key = f"{tier // 10_000}c"
        vals, evp = [], None
        # ONLY anchored markets roll up. Summing an anchored 30s window
        # together with an unanchored whole-capture figure would produce a
        # board total that means nothing — and the whole-capture figure is
        # the flattering one, because it counts size resting on words that
        # were never said, where cheap depth is correct pricing rather than
        # opportunity.
        for e in anchored:
            t = e["tiers"].get(key)
            if not t:
                continue
            evp = t["ev_positive"] if evp is None else evp
            w = t["buy_yes"].get("30s")
            if w:
                vals.append(w["p50_usd"])
        if evp is None and entries:
            evp = (entries[0]["tiers"].get(key) or {}).get("ev_positive")
        out["by_tier"][key] = {
            "board_p50_usd": round(sum(vals), 2) if vals else None,
            "markets_counted": len(vals),
            "window": "30s after the Kalshi reprice",
            "ev_positive": evp,
        }
    if precision_e6 is not None:
        out["max_viable_price_usd"] = round(precision_e6 / ONE_E6, 3)
    return out


# ── Δtime ─────────────────────────────────────────────────────────────────

def series(rows, instrument):
    """(t_ns, mid_e6) sorted, on our receive clock — the only axis both venues
    share (Kalshi's WS carries no server timestamp)."""
    s = [(r["recv_wall_ns"], r["mid_e6"]) for r in rows
         if r["instrument"] == instrument and r.get("kind") == "quote"
         and r.get("mid_e6") is not None and r.get("recv_wall_ns")]
    s.sort()
    return s


def resample(s, t0, t1, grid=GRID_NS):
    """Last-observation-carried-forward onto a fixed grid."""
    if not s:
        return []
    out, i, cur = [], 0, None
    t = t0
    while t <= t1:
        while i < len(s) and s[i][0] <= t:
            cur = s[i][1]
            i += 1
        out.append(cur)
        t += grid
    return out


def diffs(vals):
    d = []
    prev = None
    for v in vals:
        if v is None:
            d.append(0)
            prev = None if prev is None else prev
            continue
        d.append(0 if prev is None else v - prev)
        prev = v
    return d


def best_lag(a, b, max_lag=MAX_LAG_STEPS):
    """argmax_lag corr(a[t], b[t-lag]).

    Positive lag => series `a` (Kalshi) moves AFTER `b` (Polymarket), i.e.
    Polymarket leads. Returns (lag_steps, correlation) or (None, None) when
    neither side actually moves.
    """
    n = min(len(a), len(b))
    if n < 4 * max_lag:
        return None, None
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None, None
    best, bestc = None, None
    for lag in range(-max_lag, max_lag + 1):
        num = 0.0
        for t in range(n):
            u = t - lag
            if 0 <= u < n:
                num += (a[t] - ma) * (b[u] - mb)
        c = num / ((va * vb) ** 0.5)
        if bestc is None or c > bestc:
            best, bestc = lag, c
    return best, bestc


def time_axis(k_rows, p_rows, pairs, floor_ns):
    """Segment-wise lag per matched instrument pair."""
    results = []
    for kt, pt, word in pairs:
        ks, ps = series(k_rows, kt), series(p_rows, pt)
        if len(ks) < 8 or len(ps) < 8:
            results.append({"word": word, "kalshi": kt, "polymarket": pt,
                            "verdict": "NOT_RESOLVABLE",
                            "reason": f"too few quotes (kalshi={len(ks)}, pm={len(ps)})"})
            continue
        t0 = max(ks[0][0], ps[0][0])
        t1 = min(ks[-1][0], ps[-1][0])
        overlap_s = (t1 - t0) / 1e9
        if t1 <= t0:
            results.append({"word": word, "kalshi": kt, "polymarket": pt,
                            "verdict": "VOID",
                            "reason": "the two series never overlap in time — "
                                      "no shared instant exists to compare"})
            continue

        ka = diffs(resample(ks, t0, t1))
        pa = diffs(resample(ps, t0, t1))
        n = min(len(ka), len(pa))
        seg_len = max(4 * MAX_LAG_STEPS, n // SEGMENT_TARGET)
        segs = [(i, min(i + seg_len, n)) for i in range(0, n, seg_len)]
        segs = [(a, b) for a, b in segs if b - a >= 4 * MAX_LAG_STEPS]
        lags = []
        for a, b in segs:
            lag, corr = best_lag(ka[a:b], pa[a:b])
            if lag is not None:
                lags.append(lag)
        if len(lags) < MIN_SEGMENTS:
            results.append({"word": word, "kalshi": kt, "polymarket": pt,
                            "overlap_s": round(overlap_s, 1),
                            "verdict": "NOT_RESOLVABLE",
                            "reason": f"only {len(lags)} usable segments "
                                      f"(need {MIN_SEGMENTS}); one venue barely moves"})
            continue

        med = statistics.median(lags)
        lo, hi = min(lags), max(lags)
        med_ns = med * GRID_NS
        spread_ns = (hi - lo) * GRID_NS
        entry = {
            "word": word, "kalshi": kt, "polymarket": pt,
            "overlap_s": round(overlap_s, 1),
            "segments": len(lags),
            "lag_s_median": round(med_ns / 1e9, 3),
            "lag_s_min": round(lo * GRID_NS / 1e9, 3),
            "lag_s_max": round(hi * GRID_NS / 1e9, 3),
            "floor_s": round(floor_ns / 1e9, 3),
        }
        if spread_ns > 4 * abs(med_ns) or (lo < 0 < hi):
            # Segments disagree on the SIGN or scale. The 26AUG03 lesson: a
            # number that moves this much with the window was never tracking
            # anything.
            entry["verdict"] = "UNSTABLE"
            entry["reason"] = (f"per-segment lag ranges {entry['lag_s_min']}..{entry['lag_s_max']} s "
                               f"around a median of {entry['lag_s_median']} s — not a stable estimate")
        elif abs(med_ns) < FLOOR_MULTIPLE * floor_ns:
            entry["verdict"] = "NOT_RESOLVABLE"
            entry["reason"] = (f"|lag| {abs(med_ns)/1e9:.3f}s is inside the joint "
                               f"measurement floor {floor_ns/1e9:.3f}s")
        else:
            entry["verdict"] = "RESOLVED"
            entry["leader"] = "polymarket" if med > 0 else "kalshi"
        results.append(entry)
    return results


# ── word pairing ──────────────────────────────────────────────────────────

def pm_terms(question):
    return {t.strip().lower() for t in QUOTED.findall(question or "")}


def kalshi_terms(label):
    return {p.strip().lower() for p in (label or "").split("/") if p.strip()}


def match_pairs(k_rows, p_rows):
    """Kalshi packs alternates into one phrase with '/'; Polymarket quotes its
    target inside the question. Pair on set intersection — and only on YES."""
    k_lab, p_lab = {}, {}
    for r in k_rows:
        if r.get("label"):
            k_lab.setdefault(r["instrument"], r["label"])
    for r in p_rows:
        if r.get("outcome") == "NO":
            continue
        if r.get("label"):
            p_lab.setdefault(r["instrument"], r["label"])
    pairs, used = [], set()
    for kt, klab in k_lab.items():
        kt_terms = kalshi_terms(klab)
        if not kt_terms:
            continue
        for pt, plab in p_lab.items():
            if pt in used:
                continue
            shared = kt_terms & pm_terms(plab)
            if shared:
                pairs.append((kt, pt, sorted(shared)[0]))
                used.add(pt)
                break
    return pairs


# ── rendering ─────────────────────────────────────────────────────────────

def to_markdown(rep):
    v = rep["volume"]
    k, p = v["kalshi"], v["polymarket"]
    L = [f"# Cross-venue report — {rep['pair_id']}", "",
         f"`generated_utc: {rep['generated_utc']}` · `schema: {rep['schema_version']}`", "",
         "## Δvolume (shared unit: USD notional)", "",
         "| | Kalshi | Polymarket |", "|---|---:|---:|",
         f"| instruments | {k['instruments']} | {p['instruments']} |",
         f"| quotes | {k['quotes']} | {p['quotes']} |",
         f"| trades | {k['trades']} | {p['trades']} |",
         f"| traded units | {k['traded_units']:,} | {p['traded_units']:,} |",
         f"| traded notional | ${k['traded_notional_usd']:,.2f} | ${p['traded_notional_usd']:,.2f} |",
         f"| median spread | {_usd(k['median_spread_usd'])} | {_usd(p['median_spread_usd'])} |",
         f"| median top depth | {_num(k['median_top_depth_units'])} | {_num(p['median_top_depth_units'])} |",
         f"| feed | {', '.join(k['feed_kinds']) or '—'} | {', '.join(p['feed_kinds']) or '—'} |",
         ""]
    r = v["ratio"]
    if r["trades_kalshi_per_pm"]:
        L.append(f"Kalshi traded **{r['trades_kalshi_per_pm']}x** as often as Polymarket "
                 f"on this pair.")
    if r["deeper_venue"]:
        L.append(f"Deeper venue by traded notional: **{r['deeper_venue']}**.")
    sw = v.get("shared_window")
    if sw:
        cov = sw["kalshi"]["coverage_pct"]
        L += ["", f"Same-window only ({sw['window_s'] / 3600:.1f} h of overlap, holding "
                  f"**{cov}%** of Kalshi's notional): Kalshi ${sw['kalshi']['traded_notional_usd']:,.2f} "
                  f"vs Polymarket ${sw['polymarket']['traded_notional_usd']:,.2f} = "
                  f"**{sw['notional_kalshi_per_pm']}x**."]
    L += ["", "## Δtime (lead/lag)", ""]
    if not rep["time"]:
        L.append("No instrument pair produced a comparable quote series.")
    else:
        L += ["| word | verdict | lag (s) | segments | range | note |",
              "|---|---|---:|---:|---|---|"]
        for e in rep["time"]:
            lag = f"{e['lag_s_median']:+.2f}" if "lag_s_median" in e else "—"
            rng = (f"{e['lag_s_min']:+.2f}..{e['lag_s_max']:+.2f}"
                   if "lag_s_min" in e else "—")
            note = e.get("reason", f"leader: {e.get('leader','—')}")
            L.append(f"| {e['word']} | {e['verdict']} | {lag} | {e.get('segments','—')} | "
                     f"{rng} | {note} |")
    L += ["", "## Δdepth (takeable at an exploitable price)", ""]
    tt = rep.get("takeable_totals") or {}
    if not rep.get("takeable"):
        L.append("No instrument pair produced a ladder.")
    else:
        prec = rep.get("precision_assumed")
        L.append(f"Buying YES on the ask, in a 30s window after Kalshi repriced. "
                 f"Hit rate assumed: "
                 f"{'**' + format(prec, '.0%') + '**' if prec is not None else '**not supplied**'}"
                 f" — EV per $1 is (hit rate − price), so a tier priced above it loses.")
        L += ["", "| edge | max price | board p50 | markets | EV |",
              "|---|---:|---:|---:|---|"]
        for tier in EDGE_TIERS_E6:
            key = f"{tier // 10_000}c"
            b = tt.get("by_tier", {}).get(key) or {}
            cap = (ONE_E6 - tier) / ONE_E6
            ev = b.get("ev_positive")
            L.append(f"| ≥{key} | ${cap:.2f} | "
                     f"{('$%s' % format(b['board_p50_usd'], ',.2f')) if b.get('board_p50_usd') is not None else '—'} | "
                     f"{b.get('markets_counted', 0)} | "
                     f"{'—' if ev is None else ('positive' if ev else '**NEGATIVE**')} |")
        L += ["", "| word | verdict | worst depth | ≥10c p50 | persisted | note |",
              "|---|---|---|---:|---:|---|"]
        for e in rep["takeable"]:
            t10 = (e["tiers"].get("10c") or {}).get("buy_yes", {})
            w = t10.get("30s") or t10.get("all") or {}
            L.append(f"| {e['word']} | {e['verdict']} | {e.get('depth_kind_worst') or '—'} | "
                     f"{('$%s' % format(w['p50_usd'], ',.2f')) if w.get('p50_usd') is not None else '—'} | "
                     f"{('%ss' % w['persisted_s']) if w.get('persisted_s') is not None else '—'} | "
                     f"{e.get('note') or ''} |")
    L += ["", f"**Overall: {rep['verdict']}** — {rep['verdict_reason']}", ""]
    if rep.get("caveats"):
        L += ["## Caveats", ""] + [f"- {c}" for c in rep["caveats"]] + [""]
    return "\n".join(L)


def _usd(x):
    return f"${x:,.4f}" if x is not None else "—"


def _num(x):
    return f"{x:,.2f}" if x is not None else "—"


# ── driver ────────────────────────────────────────────────────────────────

def build(rows, pair_id, ledger_path=None, precision_e6=None):
    k_rows = [r for r in rows if r["venue"] == "kalshi"]
    p_rows = [r for r in rows if r["venue"] == "polymarket" and r.get("outcome") != "NO"]
    caveats = []

    kf, pf = feed_kinds(k_rows), feed_kinds(p_rows)
    if "poll" in pf and "push" in kf:
        caveats.append(
            "ASYMMETRIC FEEDS: Polymarket rows were POLLED while Kalshi was PUSHED. "
            "Sampling alone biases the lag toward Kalshi by up to one poll interval. "
            "Re-record with scripts/pm-tape.py before trusting any Δt sign.")
    if "archive" in kf:
        caveats.append(
            "Kalshi rows come from the post-hoc public trades tape (no receive clock, "
            "second granularity). They support the volume axis only.")

    volume = volume_axis(k_rows + p_rows)
    sw = volume.get("shared_window")
    if sw and (sw["kalshi"]["coverage_pct"] or 0) < 50:
        caveats.append(
            f"CAPTURE BARELY OVERLAPS: only {sw['kalshi']['coverage_pct']}% of Kalshi's "
            f"notional falls inside the window Polymarket was recorded. The full-tape "
            f"ratio compares DIFFERENT time spans and overstates the gap "
            f"({volume['ratio']['notional_kalshi_per_pm']}x vs "
            f"{sw['notional_kalshi_per_pm']}x same-window). Treat the same-window "
            f"figure as the venue comparison and re-capture across the call.")

    floor = joint_floor_ns(k_rows, p_rows)
    pairs = match_pairs(k_rows, p_rows)
    time_res = time_axis(k_rows, p_rows, pairs, floor) if pairs else []
    p_all = [r for r in rows if r["venue"] == "polymarket"]
    take_res = (takeable_axis(k_rows, p_rows, pairs, precision_e6, p_all)
                if pairs else [])
    take_tot = takeable_totals(take_res, precision_e6)

    if take_res and all(e["verdict"] == "NOT_RESOLVABLE" for e in take_res):
        notes = {e["note"] for e in take_res if e.get("note")}
        if any("schema 1" in n for n in notes):
            caveats.append(
                "DEPTH NOT MEASURED: these rows predate the ladder (schema 1). "
                "Re-run scripts/xvenue-normalize.py to recover it from the raw tape.")
        elif precision_e6 is None:
            caveats.append(
                "DEPTH UNGATED: --precision was not supplied, so the takeable figures "
                "below are resting size, NOT edge. A hit rate under the tier price "
                "makes that size a loss.")

    resolved = [e for e in time_res if e["verdict"] == "RESOLVED"]
    unstable = [e for e in time_res if e["verdict"] == "UNSTABLE"]
    if not time_res:
        verdict = "NOT_RESOLVABLE"
        reason = ("no instrument pair had a quote series on both venues; "
                  "the Δvolume axis above still stands")
    elif resolved:
        lead = statistics.median([e["lag_s_median"] for e in resolved])
        who = "Polymarket" if lead > 0 else "Kalshi"
        verdict = "RESOLVED"
        reason = f"{who} leads by {abs(lead):.2f}s at the median of {len(resolved)} resolved pair(s)"
    elif unstable:
        verdict = "UNSTABLE"
        reason = (f"{len(unstable)} pair(s) produced a lag that moves with the window — "
                  f"reporting a number here would repeat the 26AUG03 error")
    else:
        verdict = "NOT_RESOLVABLE"
        reason = ("every matched pair sits inside the measurement floor or lacks motion; "
                  "this is a statement about the instruments, not about venue speed")

    spans = [r["recv_wall_ns"] for r in rows if r.get("recv_wall_ns")]
    span_s = (max(spans) - min(spans)) / 1e9 if len(spans) > 1 else 0.0

    # The ledger counts OVERLAPPING events — that is what the node's ">=3
    # events" gate means. A capture holding one venue is not a cross-venue
    # observation no matter how long it ran, and GRAB 26AUG03 is exactly that
    # (Polymarket recorded, no Kalshi trades tape ever taken). Without this
    # guard the backfill would have advanced the gate on an event with zero
    # Kalshi rows.
    both_venues = bool(k_rows) and bool(p_rows)
    if not both_venues:
        caveats.append(
            "SINGLE VENUE: this capture holds "
            f"{'Polymarket' if p_rows else 'Kalshi'} only, so there is nothing to "
            "compare and it cannot count toward the 3-event gate.")

    rep = {
        "schema_version": XV_SCHEMA,
        "pair_id": pair_id,
        "span_s": round(span_s, 1),
        "ledger_eligible": span_s >= MIN_LEDGER_SPAN_S and both_venues,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "joint_floor_s": round(floor / 1e9, 3),
        "matched_words": len(pairs),
        "volume": volume,
        "time": time_res,
        "takeable": take_res,
        "takeable_totals": take_tot,
        "precision_assumed": (None if precision_e6 is None
                              else round(precision_e6 / ONE_E6, 4)),
        "verdict": verdict,
        "verdict_reason": reason,
        "caveats": caveats,
    }
    return rep


def append_ledger(path, rep):
    """One row per event, committed, so the >=3-event gate is a fact on disk
    rather than a memory."""
    ledger = {"schema_version": XV_SCHEMA, "rows": []}
    if os.path.exists(path):
        try:
            ledger = json.load(open(path))
        except json.JSONDecodeError:
            pass
    v = rep["volume"]
    row = {
        "pair_id": rep["pair_id"],
        "generated_utc": rep["generated_utc"],
        "verdict": rep["verdict"],
        "matched_words": rep["matched_words"],
        "joint_floor_s": rep["joint_floor_s"],
        "kalshi_notional_usd": v["kalshi"]["traded_notional_usd"],
        "pm_notional_usd": v["polymarket"]["traded_notional_usd"],
        "kalshi_trades": v["kalshi"]["trades"],
        "pm_trades": v["polymarket"]["trades"],
        "notional_ratio": v["ratio"]["notional_kalshi_per_pm"],
        # The takeable axis, banked alongside volume: this is the figure the
        # execution gate is argued from, so it must survive on disk rather
        # than in a terminal — the whole reason this script replaced
        # pm-edge-report.py.
        "takeable_p50_usd_10c": (rep.get("takeable_totals", {})
                                 .get("by_tier", {}).get("10c", {}).get("board_p50_usd")),
        "takeable_p50_usd_50c": (rep.get("takeable_totals", {})
                                 .get("by_tier", {}).get("50c", {}).get("board_p50_usd")),
        "takeable_resolved": rep.get("takeable_totals", {}).get("resolved"),
        "precision_assumed": rep.get("precision_assumed"),
        "caveats": rep["caveats"],
    }
    # The row shape gained the takeable fields; say so rather than inheriting
    # the version of whatever was on disk.
    ledger["schema_version"] = XV_SCHEMA
    ledger["rows"] = [r for r in ledger.get("rows", []) if r.get("pair_id") != rep["pair_id"]]
    ledger["rows"].append(row)
    ledger["rows"].sort(key=lambda r: r.get("pair_id", ""))
    ledger["events"] = len(ledger["rows"])
    # The node's failure contract: no pooled verdict below 3 overlapping events.
    ledger["gate_3_events_met"] = ledger["events"] >= 3
    with open(path, "w") as f:
        json.dump(ledger, f, indent=2)
    return ledger


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

    print("xvenue-report self-test:")

    S = 1_000_000_000

    # ── takeable axis ─────────────────────────────────────────────────────
    # Time weighting: a small value that holds for 100 s must beat a large
    # one that lasts 1 s. Unweighted, the median would say $500.
    w = _window_stats([(0, 500.0), (1 * S, 10.0), (101 * S, 10.0)], 0, 101 * S)
    chk("p50 is time-weighted, not per-snapshot", 10.0, w["p50_usd"])
    chk("max still sees the spike", 500.0, w["max_usd"])
    # LOCF: the book at the anchor is the last one BEFORE it. At the instant a
    # word is said, the resting book is the stale pre-mention book.
    w = _window_stats([(0, 42.0), (100 * S, 0.0)], 50 * S, 60 * S)
    chk("window seeds from the pre-anchor book", 42.0, w["p50_usd"])
    chk("window covers its full span", 10.0, w["covered_s"])
    chk("no sample at or before the window start is not a crash",
        None, _window_stats([(90 * S, 1.0)], 0, 10 * S))
    # persisted: depth that drops to zero ends the run.
    w = _window_stats([(0, 5.0), (10 * S, 0.0), (20 * S, 5.0)], 0, 30 * S)
    chk("persisted counts the longest run, not the total", 10.0, w["persisted_s"])

    def ktr(px, t_s):
        return {"venue": "kalshi", "kind": "trade", "instrument": "E-W",
                "price_e6": px, "venue_ts_ns": t_s * S}

    # Crossing at t=100 from an unsaid state recorded at t=1.
    tape = [ktr(50_000, 1), ktr(950_000, 100), ktr(970_000, 200)]
    chk("anchor is the unsaid→said crossing", 100 * S, mention_anchor(tape, "E-W"))
    chk("anchor accepted inside the other venue's window",
        100 * S, mention_anchor(tape, "E-W", 90 * S, 300 * S))
    # The `below` print routinely predates the capture by days; bounding it
    # too rejected every real anchor on PLTR.
    chk("unsaid evidence may predate the window",
        100 * S, mention_anchor(tape, "E-W", 99 * S, 300 * S))
    # But a crossing outside the window is the 3.9-day-stale anchor bug.
    chk("crossing before the window is refused",
        None, mention_anchor(tape, "E-W", 150 * S, 300 * S))
    chk("a word never said has no anchor",
        None, mention_anchor([ktr(50_000, 1), ktr(60_000, 2)], "E-W"))
    chk("a word never cheap has no anchor",
        None, mention_anchor([ktr(950_000, 1), ktr(970_000, 2)], "E-W"))

    def q(venue, inst, label, t_ns, mid, feed="push", poll=None, outcome="YES"):
        return {"schema_version": XV_SCHEMA, "pair_id": "t", "venue": venue,
                "kind": "quote", "instrument": inst, "label": label,
                "outcome": outcome, "recv_wall_ns": t_ns, "recv_mono_ns": t_ns,
                "venue_ts_ns": None, "venue_ts_granularity_ns": None,
                "feed_kind": feed, "poll_interval_ns": poll,
                "bid_e6": mid - 1000, "ask_e6": mid + 1000, "mid_e6": mid,
                "spread_e6": 2000, "bid_size_e6": 5_000_000, "ask_size_e6": 5_000_000}

    def tr(venue, inst, px, sz):
        return {"schema_version": XV_SCHEMA, "pair_id": "t", "venue": venue,
                "kind": "trade", "instrument": inst, "label": "w", "outcome": "YES",
                "recv_wall_ns": 1, "recv_mono_ns": 1, "venue_ts_ns": None,
                "venue_ts_granularity_ns": None, "feed_kind": "push",
                "poll_interval_ns": None, "price_e6": px, "size_e6": sz,
                "notional_e6": (px * sz) // ONE_E6}

    chk("word pair matches quoted term",
        [("K1", "P1", "anthropic")],
        match_pairs([q("kalshi", "K1", "Anthropic/anthropic", 0, 1)],
                    [q("polymarket", "P1", 'Will X say "Anthropic"?', 0, 1)]))
    chk("NO leg never pairs", [],
        match_pairs([q("kalshi", "K1", "Anthropic", 0, 1)],
                    [q("polymarket", "P1", 'Will X say "Anthropic"?', 0, 1, outcome="NO")]))

    # A polled side must raise the joint floor to its sampling interval.
    chk("poll interval raises the floor", 1_000_000_000,
        joint_floor_ns([q("kalshi", "K", "w", 0, 1)],
                       [q("polymarket", "P", "w", 0, 1, feed="poll", poll=1_000_000_000)]))

    # Constructed lead: Polymarket moves, Kalshi repeats it 8 steps (2 s) later.
    import random
    random.seed(7)
    steps = 1600
    walk = [0]
    for _ in range(steps):
        walk.append(walk[-1] + random.choice([-2000, 0, 2000]))
    lead = 8
    krows, prows = [], []
    for i in range(steps):
        t = i * GRID_NS
        prows.append(q("polymarket", "P1", 'say "word"', t, 500000 + walk[i]))
        krows.append(q("kalshi", "K1", "word", t, 500000 + walk[max(0, i - lead)]))
    res = time_axis(krows, prows, [("K1", "P1", "word")], GRID_NS)[0]
    chk("constructed 2s PM lead is RESOLVED", "RESOLVED", res["verdict"])
    chk("recovers the injected lag", 2.0, res["lag_s_median"])
    chk("names Polymarket the leader", "polymarket", res.get("leader"))

    # Identical series => zero lag => refused, not reported as a tie-win.
    same = [q("kalshi", "K1", "word", i * GRID_NS, 500000 + walk[i]) for i in range(steps)]
    samep = [q("polymarket", "P1", 'say "word"', i * GRID_NS, 500000 + walk[i]) for i in range(steps)]
    res0 = time_axis(same, samep, [("K1", "P1", "word")], GRID_NS)[0]
    chk("zero lag is NOT_RESOLVABLE, not a win", "NOT_RESOLVABLE", res0["verdict"])

    # Non-overlapping windows are VOID (the GRAB class).
    a = [q("kalshi", "K1", "word", 10_000_000_000 + i * GRID_NS, 500000) for i in range(20)]
    b = [q("polymarket", "P1", 'say "word"', i * GRID_NS, 500000) for i in range(20)]
    chk("disjoint windows are VOID", "VOID",
        time_axis(a, b, [("K1", "P1", "word")], GRID_NS)[0]["verdict"])

    # Volume axis in shared units.
    vol = volume_axis([tr("kalshi", "K1", 250000, 400_000_000),
                       tr("polymarket", "P1", 500000, 10_000_000)])
    chk("kalshi notional $100", 100.0, vol["kalshi"]["traded_notional_usd"])
    chk("pm notional $5", 5.0, vol["polymarket"]["traded_notional_usd"])
    chk("notional ratio 20x", 20.0, vol["ratio"]["notional_kalshi_per_pm"])
    chk("deeper venue named", "kalshi", vol["ratio"]["deeper_venue"])

    # Rows with no ladder must REFUSE, never report $0 takeable. A zero reads
    # as "nothing was resting there"; the truth is "we never recorded it".
    old = build([q("kalshi", "K1", "word", i * GRID_NS, 500000) for i in range(steps)] +
                [q("polymarket", "P1", 'say "word"', i * GRID_NS, 500000)
                 for i in range(steps)], "t", precision_e6=750_000)
    chk("schema-1 rows refuse the depth axis", "NOT_RESOLVABLE",
        old["takeable"][0]["verdict"])
    chk("and say why", True, "schema 1" in (old["takeable"][0]["note"] or ""))
    chk("board total is null, not zero", None,
        old["takeable_totals"]["by_tier"]["10c"]["board_p50_usd"])
    chk("missing depth raises a caveat", True,
        any("DEPTH NOT MEASURED" in c for c in old["caveats"]))

    # Volume: a shared window that holds little of one venue's activity must
    # be reported separately from the full-tape ratio (the AMD 26AUG04 case,
    # where the two differ by 27x).
    kfull = [dict(tr("kalshi", "K1", 500000, 100_000_000), venue_ts_ns=t * S)
             for t in (1, 5000)]
    ponly = [dict(tr("polymarket", "P1", 500000, 10_000_000), venue_ts_ns=t * S)
             for t in (1, 2)]
    vw = volume_axis(kfull + ponly)["shared_window"]
    chk("shared window holds half of Kalshi's notional", 50.0,
        vw["kalshi"]["coverage_pct"])
    chk("same-window ratio is not the full-tape ratio", 5.0,
        vw["notional_kalshi_per_pm"])

    # A poll-vs-push mix must produce a loud caveat, not a quiet number.
    rep = build([q("kalshi", "K1", "word", i * GRID_NS, 500000 + walk[i]) for i in range(steps)] +
                [q("polymarket", "P1", 'say "word"', i * GRID_NS, 500000 + walk[i],
                   feed="poll", poll=1_000_000_000) for i in range(steps)], "t")
    chk("asymmetric feeds raise a caveat", True,
        any("ASYMMETRIC FEEDS" in c for c in rep["caveats"]))

    # A single-venue capture must never reach the evidence ledger either: the
    # gate counts OVERLAPPING events, and GRAB 26AUG03 is Polymarket-only.
    solo = build([q("polymarket", "P1", 'say "word"', i * GRID_NS, 500000)
                  for i in range(steps)], "t")
    chk("a one-venue capture is ledger-INeligible", False, solo["ledger_eligible"])
    chk("and says so out loud", True,
        any("SINGLE VENUE" in c for c in solo["caveats"]))

    # A smoke-length session must never reach the evidence ledger.
    short = build([q("kalshi", "K1", "word", i * GRID_NS, 500000) for i in range(40)] +
                  [q("polymarket", "P1", 'say "word"', i * GRID_NS, 500000) for i in range(40)], "t")
    chk("10s session is ledger-INeligible", False, short["ledger_eligible"])
    chk("long session is ledger-eligible", True, rep["ledger_eligible"])

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--norm", help="normalized records (xvenue-normalize.py)")
    ap.add_argument("--pair", default="")
    ap.add_argument("--out", help="report JSON path")
    ap.add_argument("--ledger", default="slate/xvenue-ledger.json")
    ap.add_argument("--no-ledger", action="store_true")
    # No default. A takeable figure gated on an assumed hit rate we invented
    # would be the most flattering number this lane could produce, so the
    # caller has to state one and it gets recorded in the artifact.
    ap.add_argument("--precision", type=float, default=None,
                    help="measured detector precision 0..1 (e.g. 0.75, RDDT 26JUL30). "
                         "Without it the depth axis reports size but refuses to call it edge.")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not a.norm or not a.out:
        print("FATAL: --norm and --out required (or --self-test)", file=sys.stderr)
        return 2
    if a.precision is not None and not 0.0 < a.precision <= 1.0:
        print("FATAL: --precision must be a hit rate in (0, 1]", file=sys.stderr)
        return 2
    try:
        rows = load(a.norm)
        rep = build(rows, a.pair or os.path.basename(a.norm),
                    precision_e6=(None if a.precision is None
                                  else int(round(a.precision * ONE_E6))))
        with open(a.out, "w") as f:
            json.dump(rep, f, indent=2)
        md = os.path.splitext(a.out)[0] + ".md"
        with open(md, "w") as f:
            f.write(to_markdown(rep))
        print(to_markdown(rep))
        if a.no_ledger:
            pass
        elif not rep["ledger_eligible"]:
            why = (f"session span {rep['span_s']}s is under {MIN_LEDGER_SPAN_S}s, so this "
                   f"is a smoke run" if rep["span_s"] < MIN_LEDGER_SPAN_S else
                   "only one venue is present, so there is no cross-venue observation")
            print(f"ledger: SKIPPED — {why} and must not count toward the "
                  f"3-event evidence gate")
        else:
            led = append_ledger(a.ledger, rep)
            print(f"ledger: {a.ledger} — {led['events']} event(s), "
                  f"3-event gate {'MET' if led['gate_3_events_met'] else 'NOT met'}")
        print(f"wrote {a.out} and {md}")
        return 0 if rep["matched_words"] or rep["volume"]["kalshi"]["quotes"] else 1
    except SystemExit:
        raise
    except Exception as e:                                       # noqa: BLE001
        print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
