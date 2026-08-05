#!/usr/bin/env python3
"""book_shape_survey.py — PM book-depth characterization, no anchor needed.

`reanchor_takeable.py` and the frozen `scripts/xvenue-report.py` both need an
anchor (a moment "the word was said") to say anything, and today that anchor
exists for exactly one word (PLTR "ai slop") across two banked events — too
thin to tell "PM's book is structurally wide" apart from "that one snapshot
happened to be wide." This script needs no anchor at all: it pools EVERY
Polymarket quote row across every capture, including instruments that were
never matched to a Kalshi word, and asks purely book-shape questions:

  - How often, at a random moment, is there real size resting at a good
    price (snapshot base rate)?
  - Time-weighted, how much sits there over a whole session (reuses the
    exact `_takeable_at`/`_window_stats` primitives from xvenue-report.py)?
  - Is depth-at-good-price a standing structural feature of these books, or
    does it only appear transiently right after a reprice (binned by time
    since the instrument's own last mid-price move)?

Grows in statistical power with every new capture the multi-event pipeline
lands, independent of whether that event was ever armed.

Usage:
  book_shape_survey.py --norm state/xvenue/PLTR-26AUG03/norm.jsonl \\
      state/xvenue/AMD-26AUG04/norm.jsonl \\
      --out pm-expansion-exploration/profitability/out/book-shape.json
  book_shape_survey.py --self-test
"""

import argparse
import importlib.util
import json
import os
import pathlib
import statistics
import sys
from collections import defaultdict

_XVR_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "xvenue-report.py"


def _load_xvenue_report():
    spec = importlib.util.spec_from_file_location("xvenue_report", str(_XVR_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


xvr = _load_xvenue_report()

TIER_KEYS = [f"{t // 10_000}c" for t in xvr.EDGE_TIERS_E6]


def instrument_rows(rows, instrument):
    return sorted(
        (r for r in rows if r["venue"] == "polymarket" and r["kind"] == "quote"
         and r["instrument"] == instrument and r.get("outcome") != "NO" and xvr._ts(r)),
        key=xvr._ts)


def whole_session_takeable(rows_for_instrument):
    """The exact 'all' window xvenue-report.py's takeable_axis() already
    computes for matched words — extended here to EVERY instrument,
    matched or not."""
    withladder = [r for r in rows_for_instrument if r.get("book_depth_kind") in ("full", "top5")]
    if not withladder:
        return None
    t0, t1 = xvr._ts(withladder[0]), xvr._ts(withladder[-1])
    tiers = {}
    for tier, key in zip(xvr.EDGE_TIERS_E6, TIER_KEYS):
        cap = xvr.ONE_E6 - tier
        yes_s = [(xvr._ts(r), xvr._takeable_at(r.get("asks_e6"), cap)) for r in withladder]
        tiers[key] = xvr._window_stats(yes_s, t0, t1)
    return {
        "covered_s": round((t1 - t0) / 1e9, 1),
        "tiers": tiers,
        "depth_kind_worst": min((r.get("book_depth_kind") for r in withladder),
                                 key=lambda k: xvr.DEPTH_RANK.get(k, 0)),
    }


def snapshot_base_rate(rows_for_instrument):
    """Unweighted: at a random snapshot, is there ANY size below the cap?
    Complementary to the time-weighted figure above — more intuitive, but
    treats a 200s-stale snapshot the same as a 200ms one (see
    xvenue-report.py's _window_stats docstring for why that's the wrong
    question for 'how much money was really there')."""
    withladder = [r for r in rows_for_instrument if r.get("book_depth_kind") in ("full", "top5")]
    n = len(withladder)
    if n == 0:
        return None
    out = {}
    for tier, key in zip(xvr.EDGE_TIERS_E6, TIER_KEYS):
        cap = xvr.ONE_E6 - tier
        nonzero = sum(1 for r in withladder if xvr._takeable_at(r.get("asks_e6"), cap) > 0)
        out[key] = {"snapshots": n, "pct_nonzero": round(100.0 * nonzero / n, 1)}
    return out


def structural_vs_transient(rows_for_instrument, tier_e6=100_000, recent_s=10.0):
    """Bin snapshots by time-since-last-mid-move; compare takeable-at-tier in
    'just moved' vs 'quiet' buckets. Tests EXPANSION.md's claim that PM's ask
    sits near $0.99 'as a matter of course' -- i.e. whether thinness is a
    standing structural feature or only appears right after a reprice."""
    withladder = [r for r in rows_for_instrument
                  if r.get("book_depth_kind") in ("full", "top5") and r.get("mid_e6") is not None]
    if len(withladder) < 3:
        return None
    cap = xvr.ONE_E6 - tier_e6
    just_moved, quiet = [], []
    last_move_ts, prev_mid = None, None
    for r in withladder:
        t = xvr._ts(r)
        if prev_mid is not None and r["mid_e6"] != prev_mid:
            last_move_ts = t
        prev_mid = r["mid_e6"]
        takeable = xvr._takeable_at(r.get("asks_e6"), cap)
        bucket = (just_moved if last_move_ts is not None and (t - last_move_ts) / 1e9 <= recent_s
                  else quiet)
        bucket.append(takeable)

    def summarize(vals):
        if not vals:
            return None
        return {"n": len(vals),
                "pct_nonzero": round(100.0 * sum(1 for v in vals if v > 0) / len(vals), 1),
                "median_usd": round(statistics.median(vals), 2)}

    return {"tier": f"{tier_e6 // 10_000}c", "recent_window_s": recent_s,
            "just_moved": summarize(just_moved), "quiet": summarize(quiet)}


def survey(paths):
    all_rows = []
    for p in paths:
        all_rows.extend(xvr.load(p))
    instruments = sorted({r["instrument"] for r in all_rows
                           if r["venue"] == "polymarket" and r.get("outcome") != "NO"})
    per_instrument = {}
    pooled_snapshot_nonzero = defaultdict(list)
    pooled_whole_session_p50 = defaultdict(list)
    for inst in instruments:
        rows = instrument_rows(all_rows, inst)
        label = next((r.get("label") for r in rows if r.get("label")), None)
        ws = whole_session_takeable(rows)
        sr = snapshot_base_rate(rows)
        sv = structural_vs_transient(rows)
        per_instrument[inst] = {
            "label": label, "n_quotes": len(rows),
            "whole_session": ws, "snapshot_base_rate": sr,
            "structural_vs_transient": sv,
        }
        if sr:
            for key, d in sr.items():
                pooled_snapshot_nonzero[key].append(d["pct_nonzero"])
        if ws:
            for key, d in ws["tiers"].items():
                if d:
                    pooled_whole_session_p50[key].append(d["p50_usd"])

    pooled = {}
    for key in TIER_KEYS:
        pooled[key] = {
            "instruments_with_data": len(pooled_snapshot_nonzero.get(key, [])),
            "mean_pct_snapshots_nonzero": (
                round(statistics.mean(pooled_snapshot_nonzero[key]), 1)
                if pooled_snapshot_nonzero.get(key) else None),
            "mean_whole_session_p50_usd": (
                round(statistics.mean(pooled_whole_session_p50[key]), 2)
                if pooled_whole_session_p50.get(key) else None),
        }
    return {"sources": paths, "n_instruments": len(instruments),
            "per_instrument": per_instrument, "pooled": pooled}


def to_markdown(rep):
    L = [f"# PM book-shape survey", "",
         f"Sources: {', '.join(rep['sources'])}", "",
         f"Instruments pooled: {rep['n_instruments']}", "",
         "## Pooled base rates (unconditioned on any word being said)", "",
         "| tier | instruments w/ data | mean % snapshots nonzero | mean whole-session p50 (USD) |",
         "|---|---:|---:|---:|"]
    for key in TIER_KEYS:
        d = rep["pooled"][key]
        pct = (f"{d['mean_pct_snapshots_nonzero']}"
               if d["mean_pct_snapshots_nonzero"] is not None else "—")
        p50 = (f"${d['mean_whole_session_p50_usd']:,.2f}"
               if d["mean_whole_session_p50_usd"] is not None else "—")
        L.append(f"| ≥{key} | {d['instruments_with_data']} | {pct} | {p50} |")
    L += ["", "## Per-instrument detail", "",
          "| instrument (label) | quotes | ≥10c snapshot % nonzero | just-moved vs quiet (10c) |",
          "|---|---:|---:|---|"]
    for inst, d in rep["per_instrument"].items():
        sr10 = (d.get("snapshot_base_rate") or {}).get("10c") or {}
        sv = d.get("structural_vs_transient") or {}
        jm, q = sv.get("just_moved"), sv.get("quiet")
        sv_str = (f"moved:{jm['pct_nonzero']}% quiet:{q['pct_nonzero']}%"
                  if jm and q else "—")
        label = (d.get("label") or inst)[:60]
        L.append(f"| {label} | {d['n_quotes']} | "
                 f"{sr10.get('pct_nonzero', '—')} | {sv_str} |")
    return "\n".join(L) + "\n"


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

    print("book_shape_survey self-test:")
    S = 1_000_000_000

    def q(inst, t_ns, mid, asks):
        return {"schema_version": xvr.XV_SCHEMA, "pair_id": "t", "venue": "polymarket",
                "kind": "quote", "instrument": inst, "label": "w", "outcome": "YES",
                "recv_wall_ns": t_ns, "recv_mono_ns": t_ns, "venue_ts_ns": t_ns,
                "venue_ts_granularity_ns": 1_000_000, "feed_kind": "push",
                "poll_interval_ns": None, "bid_e6": mid - 1000, "ask_e6": mid + 1000,
                "mid_e6": mid, "spread_e6": 2000, "bid_size_e6": 5_000_000,
                "ask_size_e6": 5_000_000, "asks_e6": asks,
                "bids_e6": [[mid - 1000, 5_000_000]], "book_levels": [1, 1],
                "book_depth_kind": "top5"}

    # Instrument always thin (near $0.99), never takeable at 10c.
    thin = [q("A", i * S, 500_000, [[990_000, 5_000_000]]) for i in range(20)]
    # Instrument with real resting size below 90c the whole session.
    deep = [q("B", i * S, 500_000, [[300_000, 10_000_000]]) for i in range(20)]

    rep = survey_from_rows(thin + deep)
    chk("two instruments pooled", 2, rep["n_instruments"])
    thin_sr = rep["per_instrument"]["A"]["snapshot_base_rate"]["10c"]
    deep_sr = rep["per_instrument"]["B"]["snapshot_base_rate"]["10c"]
    chk("thin book never takeable at 10c", 0.0, thin_sr["pct_nonzero"])
    chk("deep book always takeable at 10c", 100.0, deep_sr["pct_nonzero"])
    chk("pooled mean sits between the two", True,
        0.0 < rep["pooled"]["10c"]["mean_pct_snapshots_nonzero"] < 100.0)

    # structural-vs-transient: a book whose mid NEVER moves has no "just
    # moved" bucket at all (last_move_ts stays None) -- every sample falls
    # into "quiet", which is the correct way to read a book that's
    # structurally deep rather than transiently deep right after a reprice.
    sv = structural_vs_transient(deep)
    chk("always-deep book has no just_moved bucket (mid never moves)",
        None, sv["just_moved"])
    chk("always-deep book's quiet bucket is fully takeable", 100.0,
        sv["quiet"]["pct_nonzero"])

    # A book that starts thin, reprices once, then goes deep should show
    # 'quiet' (post-reprice) as the deep bucket if we look after the move.
    moving = ([q("C", i * S, 500_000, [[990_000, 5_000_000]]) for i in range(5)]
              + [q("C", (5 + i) * S, 300_000, [[300_000, 10_000_000]]) for i in range(20)])
    sv2 = structural_vs_transient(moving, recent_s=3.0)
    chk("post-move bucket exists", True, sv2["just_moved"] is not None)

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def survey_from_rows(rows):
    """Test-only helper mirroring survey() without file I/O."""
    instruments = sorted({r["instrument"] for r in rows
                           if r["venue"] == "polymarket" and r.get("outcome") != "NO"})
    per_instrument = {}
    pooled_snapshot_nonzero = defaultdict(list)
    pooled_whole_session_p50 = defaultdict(list)
    for inst in instruments:
        irows = instrument_rows(rows, inst)
        ws = whole_session_takeable(irows)
        sr = snapshot_base_rate(irows)
        sv = structural_vs_transient(irows)
        per_instrument[inst] = {"label": irows[0].get("label") if irows else None,
                                 "n_quotes": len(irows), "whole_session": ws,
                                 "snapshot_base_rate": sr, "structural_vs_transient": sv}
        if sr:
            for key, d in sr.items():
                pooled_snapshot_nonzero[key].append(d["pct_nonzero"])
        if ws:
            for key, d in ws["tiers"].items():
                if d:
                    pooled_whole_session_p50[key].append(d["p50_usd"])
    pooled = {}
    for key in TIER_KEYS:
        pooled[key] = {
            "instruments_with_data": len(pooled_snapshot_nonzero.get(key, [])),
            "mean_pct_snapshots_nonzero": (round(statistics.mean(pooled_snapshot_nonzero[key]), 1)
                                            if pooled_snapshot_nonzero.get(key) else None),
            "mean_whole_session_p50_usd": (round(statistics.mean(pooled_whole_session_p50[key]), 2)
                                            if pooled_whole_session_p50.get(key) else None),
        }
    return {"sources": ["<in-memory>"], "n_instruments": len(instruments),
            "per_instrument": per_instrument, "pooled": pooled}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--norm", nargs="+", help="one or more normalized record files")
    ap.add_argument("--out", help="report JSON path")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not a.norm or not a.out:
        print("FATAL: --norm and --out required (or --self-test)", file=sys.stderr)
        return 2

    rep = survey(a.norm)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(rep, f, indent=2)
    md = os.path.splitext(a.out)[0] + ".md"
    md_text = to_markdown(rep)
    with open(md, "w") as f:
        f.write(md_text)
    print(md_text)
    print(f"wrote {a.out} and {md}")
    return 0 if rep["n_instruments"] else 1


if __name__ == "__main__":
    sys.exit(main())
