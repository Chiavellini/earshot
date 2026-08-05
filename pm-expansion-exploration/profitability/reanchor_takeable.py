#!/usr/bin/env python3
"""reanchor_takeable.py — takeable PM size, anchored on OUR OWN reachable-
action time instead of the moment Kalshi's book repriced.

`scripts/xvenue-report.py`'s `takeable_axis()` anchors its 30s/300s lookback
windows on `mention_anchor()` — the instant KALSHI's own trades tape crosses
unsaid->said. On the one measurable case (PLTR "ai slop") that found $0.00
takeable below 90c. But Kalshi's reprice is some OTHER trader's reaction
time; it is not causally tied to when WE could have acted on Polymarket. Our
edge comes from the call audio directly, independent of any market's book.

The corrected anchor:

    T_reach = decision_ts_mono_ns + bridge_offset_ns + L_pm_order_latency

  - decision_ts_mono_ns: our detector's own decision timestamp for this word
    (from extract_decisions.py's decisions.json). This is ALREADY
    audio_ts + detect_latency, i.e. already downstream of whatever webcast/
    ingress delay existed — no separate ingress-lag term is needed, because
    that same delay is baked identically into both "when we decided" and
    "what PM state existed at that wall-clock instant." It cancels out.
  - bridge_offset_ns: monotonic->wall bridge from extract_decisions.py,
    carrying an explicit bridge_error_s. Never treat T_reach as exact.
  - L_pm_order_latency: PM order-placement latency. No PM execution code has
    ever run, so this has NO default — it is a required sensitivity grid
    (--pm-order-latency-ms), never a single assumed number.

This script REUSES scripts/xvenue-report.py's depth math verbatim (imported,
not reimplemented) so results are directly comparable to the frozen tool's
Kalshi-anchored figures. Only the anchor timestamp source differs; the ladder
math, LOCF seeding, and time-weighting in `_window_stats` are identical.

Usage:
  reanchor_takeable.py --norm state/xvenue/PLTR-26AUG03/norm.jsonl \\
      --decisions pm-expansion-exploration/captures/PLTR-26AUG03/decisions.json \\
      --pm-order-latency-ms 100,300,1000 --precision 0.75 \\
      --out pm-expansion-exploration/profitability/out/PLTR-26AUG03-reanchored.json
  reanchor_takeable.py --self-test

Exit codes: 0 report written / 1 nothing comparable / 2 error.
"""

import argparse
import importlib.util
import json
import os
import pathlib
import sys

_XVR_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "xvenue-report.py"


def _load_xvenue_report():
    spec = importlib.util.spec_from_file_location("xvenue_report", str(_XVR_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


xvr = _load_xvenue_report()


def reach_anchor_ns(decision, bridge_offset_ns, pm_latency_ms):
    return (decision["decision_ts_mono_ns"] + bridge_offset_ns
            + int(round(pm_latency_ms * 1_000_000)))


def reanchored_takeable(k_rows, p_rows, pairs, precision_e6, decisions,
                         bridge_offset_ns, pm_latency_ms_grid, p_all_rows=None):
    """MIRRORS xvenue_report.takeable_axis() — only the anchor source differs.

    decisions: dict[kalshi_ticker] -> decisions.json "words" entry.
    """
    k_trades = [r for r in k_rows if r["kind"] == "trade"]
    no_by_label = {}
    for r in (p_all_rows or []):
        if r.get("outcome") == "NO" and r["kind"] == "quote" and r.get("label"):
            no_by_label.setdefault(r["label"], []).append(r)

    p_ts_all = [xvr._ts(r) for r in p_rows if xvr._ts(r)]
    lo_ns, hi_ns = (min(p_ts_all), max(p_ts_all)) if p_ts_all else (None, None)

    results = []
    for kt, pt, word in pairs:
        quotes = sorted(
            (r for r in p_rows
             if r["instrument"] == pt and r["kind"] == "quote" and xvr._ts(r)),
            key=xvr._ts)
        entry = {"word": word, "kalshi_ticker": kt, "pm_instrument": pt,
                 "quotes": len(quotes), "verdict": None, "note": None,
                 "kalshi_reprice_anchor_ns": None, "reach_anchor_ns": {},
                 "anchor_delta_s": {}, "bridge_offset_ns": bridge_offset_ns,
                 "tiers": {}}

        withladder = [r for r in quotes if r.get("book_depth_kind") in ("full", "top5")]
        if not withladder:
            entry["verdict"] = "NOT_RESOLVABLE"
            entry["note"] = "no row carries a ladder for this instrument"
            results.append(entry)
            continue

        decision = decisions.get(kt)
        if decision is None:
            entry["verdict"] = "NO_DECISION_TS"
            entry["note"] = (
                "matched cross-venue but never armed by production (or never "
                "fired a decision) for this word — no decision_ts exists to "
                "anchor on. Expected for most words; the reanchored figure "
                "only exists on the armed(production) ^ captured(exploration) "
                "^ said intersection.")
            results.append(entry)
            continue

        # Kalshi-reprice anchor, computed identically to the frozen tool —
        # kept purely for side-by-side comparison, never used to gate this
        # script's own verdict.
        kalshi_anchor = xvr.mention_anchor(k_trades, kt, lo_ns, hi_ns)
        entry["kalshi_reprice_anchor_ns"] = kalshi_anchor

        no_rows = sorted(
            (r for r in no_by_label.get(withladder[0].get("label"), [])
             if xvr._ts(r) and r.get("book_depth_kind") in ("full", "top5")),
            key=xvr._ts)

        entry["depth_kind_worst"] = min(
            (r.get("book_depth_kind") for r in withladder),
            key=lambda k: xvr.DEPTH_RANK.get(k, 0))

        any_scenario_resolved = False
        for tier in xvr.EDGE_TIERS_E6:
            cap = xvr.ONE_E6 - tier
            key = f"{tier // 10_000}c"
            entry["tiers"][key] = {
                "max_price_usd": round(cap / xvr.ONE_E6, 3),
                "buy_yes": {},
                "ev_positive": (None if precision_e6 is None else cap < precision_e6),
            }

        for pm_ms in pm_latency_ms_grid:
            scenario_key = f"{pm_ms}ms"
            reach = reach_anchor_ns(decision, bridge_offset_ns, pm_ms)
            entry["reach_anchor_ns"][scenario_key] = reach
            if kalshi_anchor is not None:
                entry["anchor_delta_s"][scenario_key] = round(
                    (kalshi_anchor - reach) / 1e9, 3)

            for tier in xvr.EDGE_TIERS_E6:
                cap = xvr.ONE_E6 - tier
                key = f"{tier // 10_000}c"
                scenario_windows = {}
                for w in xvr.TAKEABLE_WINDOWS_S:
                    t0, t1 = reach, reach + w * 1_000_000_000
                    yes_s = [(xvr._ts(r), xvr._takeable_at(r.get("asks_e6"), cap))
                             for r in withladder]
                    st = xvr._window_stats(yes_s, t0, t1)
                    if st is None:
                        continue
                    if no_rows:
                        no_s = [(xvr._ts(r), xvr._sellable_at(r.get("bids_e6"), xvr.ONE_E6 - cap))
                                for r in no_rows]
                        nst = xvr._window_stats(no_s, t0, t1)
                        if nst:
                            st["via_no_leg_p50_usd"] = nst["p50_usd"]
                            st["via_no_leg_max_usd"] = nst["max_usd"]
                    scenario_windows[f"{w}s"] = st
                    any_scenario_resolved = True
                entry["tiers"][key]["buy_yes"][scenario_key] = scenario_windows

        if precision_e6 is None:
            entry["verdict"] = "NOT_RESOLVABLE"
            entry["note"] = "depth measured but no --precision supplied"
        elif not any_scenario_resolved:
            entry["verdict"] = "NOT_RESOLVABLE"
            entry["note"] = ("every reach-anchor scenario falls outside the window "
                              "Polymarket was actually recorded")
        else:
            entry["verdict"] = "RESOLVED"
        results.append(entry)
    return results


def to_markdown(pair_id, entries, pm_latency_ms_grid, precision, bridge_error_s):
    L = [f"# Reanchored takeable-size — {pair_id}", "",
         f"Anchor: OUR OWN decision_ts + bridge_offset + PM order latency "
         f"(grid: {', '.join(str(m) + 'ms' for m in pm_latency_ms_grid)}), "
         f"NOT Kalshi's reprice. bridge_error_s={bridge_error_s} "
         f"(APPROXIMATION — see extract_decisions.py).", "",
         f"Hit rate assumed: {('%.0f%%' % (precision * 100)) if precision is not None else 'not supplied'}",
         ""]
    L += ["| word | verdict | kalshi anchor vs reach (s) | ≥10c p50 (best scenario) | note |",
          "|---|---|---|---:|---|"]
    for e in entries:
        deltas = e.get("anchor_delta_s") or {}
        delta_str = (", ".join(f"{k}:{v:+.2f}" for k, v in deltas.items())
                     if deltas else "—")
        t10 = (e.get("tiers", {}).get("10c") or {}).get("buy_yes", {})
        best_p50 = None
        for scenario_windows in t10.values():
            w = scenario_windows.get("30s") or scenario_windows.get("300s") or scenario_windows.get("5s")
            if w and (best_p50 is None or w["p50_usd"] > best_p50):
                best_p50 = w["p50_usd"]
        L.append(f"| {e['word']} | {e['verdict']} | {delta_str} | "
                  f"{('$%.2f' % best_p50) if best_p50 is not None else '—'} | "
                  f"{e.get('note') or ''} |")
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

    print("reanchor_takeable self-test:")
    S = 1_000_000_000

    decision = {"decision_ts_mono_ns": 100 * S}
    chk("reach anchor with zero bridge/latency", 100 * S,
        reach_anchor_ns(decision, 0, 0))
    chk("reach anchor adds bridge and PM latency",
        100 * S + 5 * S + int(0.3 * S),
        reach_anchor_ns(decision, 5 * S, 300))

    def q(venue, inst, label, t_ns, mid, feed="push", outcome="YES",
          asks=None, bids=None):
        return {"schema_version": xvr.XV_SCHEMA, "pair_id": "t", "venue": venue,
                "kind": "quote", "instrument": inst, "label": label,
                "outcome": outcome, "recv_wall_ns": t_ns, "recv_mono_ns": t_ns,
                "venue_ts_ns": t_ns, "venue_ts_granularity_ns": 1_000_000,
                "feed_kind": feed, "poll_interval_ns": None,
                "bid_e6": mid - 1000, "ask_e6": mid + 1000, "mid_e6": mid,
                "spread_e6": 2000, "bid_size_e6": 5_000_000, "ask_size_e6": 5_000_000,
                "asks_e6": asks or [[mid + 1000, 5_000_000]],
                "bids_e6": bids or [[mid - 1000, 5_000_000]],
                "book_levels": [1, 1], "book_depth_kind": "top5"}

    def ktr(px, t_s):
        return {"venue": "kalshi", "kind": "trade", "instrument": "E-W",
                "price_e6": px, "venue_ts_ns": t_s * S}

    k_rows = [ktr(50_000, 1), ktr(950_000, 100)]
    p_rows = [q("polymarket", "P1", 'say "word"', 90 * S, 500_000,
                asks=[[300_000, 10_000_000]]),
              q("polymarket", "P1", 'say "word"', 101 * S, 500_000,
                asks=[[300_000, 10_000_000]])]
    pairs = [("E-W", "P1", "word")]

    res = reanchored_takeable(k_rows, p_rows, pairs, precision_e6=750_000,
                               decisions={}, bridge_offset_ns=0,
                               pm_latency_ms_grid=[100])
    chk("no decisions.json entry -> NO_DECISION_TS", "NO_DECISION_TS",
        res[0]["verdict"])

    decisions = {"E-W": {"decision_ts_mono_ns": 85 * S}}
    res = reanchored_takeable(k_rows, p_rows, pairs, precision_e6=750_000,
                               decisions=decisions, bridge_offset_ns=0,
                               pm_latency_ms_grid=[100, 1000])
    e = res[0]
    chk("resolved when a decision and ladder both exist", "RESOLVED", e["verdict"])
    chk("kalshi reprice anchor computed for comparison", 100 * S,
        e["kalshi_reprice_anchor_ns"])
    chk("reach anchor(s) recorded per scenario", {100, 1000},
        set(int(k.replace("ms", "")) for k in e["reach_anchor_ns"]))
    chk("anchor_delta_s is kalshi_anchor - reach_anchor",
        round((100 * S - (85 * S + int(0.1 * S))) / 1e9, 3),
        e["anchor_delta_s"]["100ms"])
    t10 = e["tiers"]["10c"]["buy_yes"]["100ms"]
    chk("takeable size found at the reach anchor (LOCF seed)", True,
        t10["30s"]["p50_usd"] > 0)

    no_ladder_rows = [q("polymarket", "P2", 'say "other"', 90 * S, 500_000)]
    for r in no_ladder_rows:
        r["book_depth_kind"] = "none"
        r["asks_e6"] = None
    res2 = reanchored_takeable(k_rows, no_ladder_rows, [("E-W", "P2", "other")],
                                precision_e6=750_000, decisions=decisions,
                                bridge_offset_ns=0, pm_latency_ms_grid=[100])
    chk("no ladder -> NOT_RESOLVABLE before decision lookup", "NOT_RESOLVABLE",
        res2[0]["verdict"])

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--norm", help="normalized records (scripts/xvenue-normalize.py output)")
    ap.add_argument("--decisions", help="decisions.json from extract_decisions.py")
    ap.add_argument("--pair", default="")
    ap.add_argument("--out", help="report JSON path")
    ap.add_argument("--pm-order-latency-ms", default="100,300,1000",
                     help="comma-separated sensitivity grid, e.g. 100,300,1000")
    ap.add_argument("--precision", type=float, default=None,
                     help="measured detector precision 0..1, same semantics as "
                          "xvenue-report.py --precision")
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
    pm_latency_ms_grid = [int(x) for x in a.pm_order_latency_ms.split(",") if x.strip()]

    decisions = {}
    bridge_offset_ns = 0
    bridge_error_s = None
    if a.decisions:
        with open(a.decisions) as f:
            dj = json.load(f)
        decisions = dj["words"]
        bridge_offset_ns = dj["bridge_offset_ns"]
        bridge_error_s = dj["bridge_error_s"]

    rows = xvr.load(a.norm)
    k_rows = [r for r in rows if r["venue"] == "kalshi"]
    p_rows = [r for r in rows if r["venue"] == "polymarket" and r.get("outcome") != "NO"]
    p_all_rows = [r for r in rows if r["venue"] == "polymarket"]
    pairs = xvr.match_pairs(k_rows, p_rows)
    precision_e6 = None if a.precision is None else int(round(a.precision * xvr.ONE_E6))

    entries = reanchored_takeable(k_rows, p_rows, pairs, precision_e6, decisions,
                                   bridge_offset_ns, pm_latency_ms_grid, p_all_rows)
    pair_id = a.pair or os.path.basename(a.norm)
    report = {
        "schema_version": xvr.XV_SCHEMA,
        "pair_id": pair_id,
        "pm_order_latency_ms_grid": pm_latency_ms_grid,
        "bridge_offset_ns": bridge_offset_ns,
        "bridge_error_s": bridge_error_s,
        "precision_assumed": a.precision,
        "matched_words": len(pairs),
        "words_with_decision_ts": sum(1 for e in entries if e["verdict"] not in
                                       ("NO_DECISION_TS", "NOT_RESOLVABLE") or
                                       e.get("kalshi_reprice_anchor_ns") is not None
                                       or e["kalshi_ticker"] in decisions),
        "entries": entries,
    }
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(report, f, indent=2)
    md = os.path.splitext(a.out)[0] + ".md"
    md_text = to_markdown(pair_id, entries, pm_latency_ms_grid, a.precision, bridge_error_s)
    with open(md, "w") as f:
        f.write(md_text)
    print(md_text)
    print(f"wrote {a.out} and {md}")
    return 0 if entries else 1


if __name__ == "__main__":
    sys.exit(main())
