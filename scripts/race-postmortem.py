#!/usr/bin/env python3
"""race-postmortem.py — where did this event's money go, and where did the time go?

WHY THIS EXISTS (2026-08-10, HIMS). Every event so far has ended with a
narrative ("we lost by a bit") and no decomposition, so the same class of loss
keeps being rediscovered. The blocker was never analysis effort — it was that
the join could not be computed at all:

  * `report.json` stamps `audio_ts` / `decision_ts` / `send_ts` / `ack_ts` in
    MONOTONIC ns (`src/clock.rs` — CLOCK_MONOTONIC_RAW on Linux).
  * the Kalshi public tape stamps `created_time` in UTC.
  * the file that bridges them, `clock_anchor.json`, had readers in the tree
    (`drace-wallclock.py`; also the since-retired WNT scorer) and, until
    2026-08-10, ZERO events that ever wrote one.

So `drace-wallclock.py` — the project's only Δrace instrument — had never had
its required input. This script writes the funnel and the Δrace from artifacts
that now exist, and it is deliberately loud about what it CANNOT resolve rather
than filling the gap with a plausible number.

SIGN CONVENTION (docs/COMPETITOR-ANATOMY.md): **negative = we are behind.**

WHAT IT REFUSES TO INFER
  * It does not claim a word was spoken. Without banked audio there is no
    transcript, so "the market moved and we never fired" is reported as
    NOT_RESOLVABLE(no-audio), never as a detector miss.
  * It does not use `report.edges{}` for latency. Measured 26AUG05 TLN: 371 of
    534 ms was in-process, and a capture->decision edge understates the real
    budget ~4x. Every latency number here is anchored to an EXTERNAL reference
    (the tape) or to a full audio_ts->ack_ts span.
  * It does not count `len(orders)` as orders placed. The non-crossing posture
    short-circuits before send_order(), leaving records with send_ts=null. Only
    `send_ts is not None` counts as reaching the wire.

Usage:
    scripts/race-postmortem.py --event KXEARNINGSMENTIONHIMS-26AUG10-PHONE \
        [--baseline state/<E>/tape-precall.json] [--call-start 2026-08-10T21:00:00Z] \
        [--json state/<E>/postmortem.json]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

REPO = os.environ.get("EARSHOT_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def die(msg, code=2):
    print(f"POSTMORTEM-ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def iso_to_ns(s):
    """Kalshi `created_time` -> epoch ns. Tolerates Z and variable fractional digits."""
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Some rows carry more than 6 fractional digits; truncate to micros.
        head, _, tail = s.partition(".")
        frac = "".join(c for c in tail if c.isdigit())[:6].ljust(6, "0")
        off = tail[len(frac):] if not tail[len(frac):].startswith("+") else tail[len(frac):]
        dt = datetime.fromisoformat(f"{head}.{frac}{off or '+00:00'}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1e9)


def ns_to_iso(ns):
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Anchor:
    """mono_ns -> UTC ns. Two or more samples correct oscillator drift linearly.

    CLOCK_MONOTONIC_RAW is deliberately NOT NTP-disciplined, so a single anchor
    carries the oscillator's whole error over the span (a naive single anchor
    was ~2.1 s wrong over 3.7 days in the ledger). With >=2 samples we fit the
    actual rate instead of assuming 1.0.
    """

    def __init__(self, path):
        with open(path) as f:
            d = json.load(f)
        samples = d.get("samples") or [{"mono_ns": d["mono_ns"], "wall_ns": d["wall_ns"]}]
        samples = sorted(samples, key=lambda s: s["mono_ns"])
        self.samples = samples
        self.clock = d.get("clock", "?")
        a = samples[0]
        b = samples[-1]
        self.m0, self.w0 = a["mono_ns"], a["wall_ns"]
        span = b["mono_ns"] - a["mono_ns"]
        # rate = wall ns per mono ns. 1.0 when we only have one sample.
        self.rate = ((b["wall_ns"] - a["wall_ns"]) / span) if span > 0 else 1.0
        self.n = len(samples)
        self.drift_ppm = (self.rate - 1.0) * 1e6 if span > 0 else None
        self.span_s = span / 1e9 if span > 0 else 0.0

    def to_utc(self, mono_ns):
        return int(self.w0 + (mono_ns - self.m0) * self.rate)


def suffix(ticker):
    return ticker.rsplit("-", 1)[-1]


def load(path, what):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print(f"WARN: unreadable {what}: {e}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--event", required=True)
    ap.add_argument("--state-dir")
    ap.add_argument("--report")
    ap.add_argument("--tape")
    ap.add_argument("--baseline")
    ap.add_argument("--anchor")
    ap.add_argument("--call-start", help="ISO UTC; prints before this are pre-call noise")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    sd = args.state_dir or os.path.join(REPO, "state", args.event)
    report_p = args.report or os.path.join(sd, "report.json")
    tape_p = args.tape or os.path.join(sd, "tape-postcall.json")
    base_p = args.baseline or os.path.join(sd, "tape-precall.json")
    anchor_p = args.anchor or os.path.join(sd, "clock_anchor.json")

    report = load(report_p, "report.json")
    if report is None:
        die(f"no report at {report_p} — the arm never wrote one (did it run?)")
    tape = load(tape_p, "tape")
    baseline = load(base_p, "baseline tape")

    anchor = None
    anchor_note = ""
    if os.path.exists(anchor_p):
        try:
            anchor = Anchor(anchor_p)
            if anchor.n < 2:
                anchor_note = ("SINGLE anchor sample — no drift correction. Δrace carries the "
                               "oscillator error over the run; take a second sample after the call.")
            else:
                anchor_note = (f"{anchor.n} samples over {anchor.span_s/60:.1f} min, "
                               f"drift {anchor.drift_ppm:+.1f} ppm")
        except (KeyError, ValueError) as e:
            anchor_note = f"anchor unreadable ({e})"
    else:
        anchor_note = "NO clock_anchor.json — mono→UTC is NOT RESOLVABLE, so no Δrace can be computed"

    call_start_ns = iso_to_ns(args.call_start) if args.call_start else None

    # ── our side ────────────────────────────────────────────────────────────
    orders = report.get("orders") or []
    detections = report.get("detections") or []

    sent = [o for o in orders if o.get("send_ts")]
    filled = [o for o in sent if o.get("order_status") == "filled"]
    refused = [o for o in orders if o.get("refusal")]
    lost = [o for o in sent if o.get("lost_race")]

    by_mkt = {}
    for d in detections:
        it = d.get("intent") or {}
        t = it.get("market_ticker")
        if t:
            by_mkt.setdefault(t, {"detections": [], "orders": []})["detections"].append(d)
    for o in orders:
        t = o.get("ticker")
        if t:
            by_mkt.setdefault(t, {"detections": [], "orders": []})["orders"].append(o)

    # ── competitor side: prints that appeared only after the call started ────
    def trades_of(tp):
        return (tp or {}).get("trades") or {}

    post = trades_of(tape)
    pre = trades_of(baseline)
    pre_ids = {t: {x.get("trade_id") for x in v} for t, v in pre.items()}

    new_prints = {}
    for tkr, rows in post.items():
        fresh = [r for r in rows if r.get("trade_id") not in pre_ids.get(tkr, set())]
        if call_start_ns is not None:
            fresh = [r for r in fresh if iso_to_ns(r["created_time"]) >= call_start_ns]
        fresh.sort(key=lambda r: r["created_time"])
        if fresh:
            new_prints[tkr] = fresh

    # ── funnel ──────────────────────────────────────────────────────────────
    all_tickers = sorted(set(list(by_mkt) + list(new_prints)) or list((tape or {}).get("markets") or {}))
    rows = []
    for tkr in all_tickers:
        mine = by_mkt.get(tkr, {"detections": [], "orders": []})
        dets = mine["detections"]
        ords = mine["orders"]
        my_sent = [o for o in ords if o.get("send_ts")]
        my_fill = [o for o in my_sent if o.get("order_status") == "filled"]
        comp = new_prints.get(tkr, [])

        if my_fill:
            bucket = "FILLED"
        elif my_sent:
            bucket = "SENT_NO_FILL" + ("(lost_race)" if any(o.get("lost_race") for o in my_sent) else "")
        elif ords:
            reasons = sorted({(o.get("refusal") or {}).get("kind", str(o.get("refusal")))
                              if isinstance(o.get("refusal"), dict) else str(o.get("refusal"))
                              for o in ords if o.get("refusal")})
            bucket = "REFUSED:" + ",".join(r for r in reasons if r and r != "None") if reasons else "INTENT_NO_ORDER"
        elif dets:
            bucket = "DETECTED_NO_INTENT"
        elif comp:
            bucket = "NOT_RESOLVABLE(no-audio): market moved, we never fired"
        else:
            bucket = "QUIET (no prints, no detections)"

        drace_ms = None
        drace_note = ""
        if comp and anchor and my_sent:
            first_comp = iso_to_ns(comp[0]["created_time"])
            # our reference is the earliest wire moment we can defend
            ours = min(o["send_ts"] for o in my_sent)
            drace_ms = (first_comp - anchor.to_utc(ours)) / 1e6
            drace_note = "ours=send_ts (order path excluded)"
        elif comp and not anchor:
            drace_note = "NOT_RESOLVABLE: no clock anchor"
        elif comp and not my_sent:
            drace_note = "we never reached the wire — nothing to race"

        rows.append({
            "ticker": tkr, "word": suffix(tkr), "bucket": bucket,
            "detections": len(dets), "orders": len(ords), "sent": len(my_sent),
            "filled": len(my_fill), "competitor_prints": len(comp),
            "first_competitor_utc": comp[0]["created_time"] if comp else None,
            "drace_ms": drace_ms, "drace_note": drace_note,
        })

    # ── latency decomposition on our own side (external-anchored spans) ──────
    spans = []
    for o in sent:
        a, d, s = o.get("audio_ts"), o.get("decision_ts"), o.get("send_ts")
        ack = o.get("ack_ts") or o.get("hdrs_ts")
        if a and d and s:
            spans.append({
                "ticker": o["ticker"],
                "decode_policy_ms": (d - a) / 1e6,
                "build_ms": (s - d) / 1e6,
                "wire_ms": ((ack - s) / 1e6) if ack else None,
                "total_ms": ((ack or s) - a) / 1e6,
            })

    def med(xs):
        xs = sorted(x for x in xs if x is not None)
        return xs[len(xs) // 2] if xs else None

    out = {
        "event": args.event,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "anchor": anchor_note,
        "call_start": args.call_start,
        "totals": {
            "detections": len(detections), "order_records": len(orders),
            "reached_wire": len(sent), "filled": len(filled),
            "refused": len(refused), "lost_race": len(lost),
            "competitor_markets_that_moved": len(new_prints),
        },
        "latency_medians_ms": {
            "decode_and_policy": med([s["decode_policy_ms"] for s in spans]),
            "order_build": med([s["build_ms"] for s in spans]),
            "wire": med([s["wire_ms"] for s in spans]),
            "audio_to_ack_total": med([s["total_ms"] for s in spans]),
        },
        "per_market": rows,
        "spans": spans,
    }

    # ── human report ────────────────────────────────────────────────────────
    print(f"═══ race post-mortem: {args.event} ═══")
    print(f"anchor: {anchor_note}")
    t = out["totals"]
    print(f"\nFUNNEL  detections={t['detections']}  order_records={t['order_records']}  "
          f"REACHED_WIRE={t['reached_wire']}  filled={t['filled']}  "
          f"refused={t['refused']}  lost_race={t['lost_race']}")
    print("  (reached_wire counts send_ts!=null — order_records alone is the "
          "number that produced a false '23 orders placed')")

    lm = out["latency_medians_ms"]
    print("\nOUR LATENCY (median ms, only for orders that reached the wire):")
    for k, v in lm.items():
        print(f"  {k:22s} {v:8.1f}" if v is not None else f"  {k:22s}   n/a")
    print("  decode_and_policy is audio_ts→decision_ts: the term that owned the "
          "26AUG05 race (360 ms model lag). Not read from edges{}.")

    print("\nPER MARKET:")
    hdr = f"  {'word':6s} {'bucket':34s} {'det':>4s} {'sent':>5s} {'fill':>5s} {'compΔ':>8s}  first-competitor-print"
    print(hdr)
    for r in sorted(rows, key=lambda r: (r["drace_ms"] is None, r["drace_ms"] or 0)):
        d = f"{r['drace_ms']:+8.0f}" if r["drace_ms"] is not None else "       ·"
        print(f"  {r['word']:6s} {r['bucket'][:34]:34s} {r['detections']:4d} "
              f"{r['sent']:5d} {r['filled']:5d} {d}  {r['first_competitor_utc'] or ''}")
    print("\nΔ sign: NEGATIVE = we were behind the competitor's first print.")

    gaps = [r for r in rows if r["bucket"].startswith("NOT_RESOLVABLE")]
    if gaps:
        print(f"\n⚠ {len(gaps)} market(s) moved with no detection and NO banked audio — "
              "cannot tell a detector miss from a word never spoken. Bank call audio "
              "(scripts/capture-sidecar.sh) to close this permanently.")

    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out), exist_ok=True)
        with open(args.json_out, "w") as f:
            json.dump(out, f, indent=1)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
