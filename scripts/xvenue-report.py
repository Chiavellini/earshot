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

XV_SCHEMA = 1
ONE_E6 = 1_000_000

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
        if r.get("schema_version") != XV_SCHEMA:
            raise SystemExit(
                f"FATAL: {path} holds schema {r.get('schema_version')!r}, "
                f"this report speaks {XV_SCHEMA}")
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
    L += ["", f"**Overall: {rep['verdict']}** — {rep['verdict_reason']}", ""]
    if rep.get("caveats"):
        L += ["## Caveats", ""] + [f"- {c}" for c in rep["caveats"]] + [""]
    return "\n".join(L)


def _usd(x):
    return f"${x:,.4f}" if x is not None else "—"


def _num(x):
    return f"{x:,.2f}" if x is not None else "—"


# ── driver ────────────────────────────────────────────────────────────────

def build(rows, pair_id, ledger_path=None):
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

    floor = joint_floor_ns(k_rows, p_rows)
    pairs = match_pairs(k_rows, p_rows)
    volume = volume_axis(k_rows + p_rows)
    time_res = time_axis(k_rows, p_rows, pairs, floor) if pairs else []

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

    rep = {
        "schema_version": XV_SCHEMA,
        "pair_id": pair_id,
        "span_s": round(span_s, 1),
        "ledger_eligible": span_s >= MIN_LEDGER_SPAN_S,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "joint_floor_s": round(floor / 1e9, 3),
        "matched_words": len(pairs),
        "volume": volume,
        "time": time_res,
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
        "caveats": rep["caveats"],
    }
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

    # A poll-vs-push mix must produce a loud caveat, not a quiet number.
    rep = build([q("kalshi", "K1", "word", i * GRID_NS, 500000 + walk[i]) for i in range(steps)] +
                [q("polymarket", "P1", 'say "word"', i * GRID_NS, 500000 + walk[i],
                   feed="poll", poll=1_000_000_000) for i in range(steps)], "t")
    chk("asymmetric feeds raise a caveat", True,
        any("ASYMMETRIC FEEDS" in c for c in rep["caveats"]))

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
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not a.norm or not a.out:
        print("FATAL: --norm and --out required (or --self-test)", file=sys.stderr)
        return 2
    try:
        rows = load(a.norm)
        rep = build(rows, a.pair or os.path.basename(a.norm))
        with open(a.out, "w") as f:
            json.dump(rep, f, indent=2)
        md = os.path.splitext(a.out)[0] + ".md"
        with open(md, "w") as f:
            f.write(to_markdown(rep))
        print(to_markdown(rep))
        if a.no_ledger:
            pass
        elif not rep["ledger_eligible"]:
            print(f"ledger: SKIPPED — session span {rep['span_s']}s is under "
                  f"{MIN_LEDGER_SPAN_S}s, so this is a smoke run and must not "
                  f"count toward the 3-event evidence gate")
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
