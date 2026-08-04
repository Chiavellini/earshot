#!/usr/bin/env python3
"""pm-edge-report.py — cross-venue lead/lag between Kalshi and Polymarket.

Answers one question with numbers: **when the same fact hits both venues, which
one moves first, and by how much?** If one venue is reliably slower, its lag is
tradeable — a move on the fast venue is a signal for the slow one.

Two modes, kept strictly apart because they measure different things:

  word    — both venues list the SAME instrument class (a spoken-mention
            market). Overlapping words are matched automatically and each pair
            gets a Δt. This is the real lead/lag answer.
  release — the venues list DIFFERENT instruments (e.g. Kalshi prices spoken
            words while Polymarket prices GAAP EPS). There is no per-word
            comparison to make; what is shared is a RELEASE INSTANT both
            venues reprice against. Reported separately, never pooled.

The mode comes from the map's `clock` field (scripts/pm-map-build.py), so a
release-clock event cannot silently be averaged into word-mode results.

  scripts/pm-edge-report.py --map configs/pm-map-26aug03.json \\
      --event KXEARNINGSMENTIONPLTR-26AUG03 \\
      --pm-capture state/pm/KXEARNINGSMENTIONPLTR-26AUG03/pm-20260803.jsonl.gz \\
      --kalshi-tape state/KXEARNINGSMENTIONPLTR-26AUG03/tape-2026-08-03.json \\
      --anchor 2026-08-03T21:00:00Z

CAVEAT THIS TOOL CANNOT REMOVE: both venues' trade streams are second-
granularity (Kalshi `created_time`, PM `server_ts_s`). A Δt under ~2 s is
inside the joint quantisation floor and must be read as "no resolvable
difference", NOT as a win. PM book rows carry millisecond server timestamps and
are used when available, which is why book-derived and trade-derived Δt are
reported in separate columns rather than blended.
"""

import argparse
import gzip
import json
import re
import sys
import zlib
from datetime import datetime, timezone

# 16+MAX_WBITS = gzip framing. A capture that is still running (or was hard
# killed) has no end-of-stream marker; gzip.open() raises EOFError on it. The
# decompressobj keeps whatever decoded cleanly, which is exactly what we want
# from a live tape.
def pm_rows(path):
    raw = open(path, "rb").read()
    o = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        txt = o.decompress(raw).decode("utf-8", "ignore")
    except zlib.error as e:
        print(f"warn: pm capture partially unreadable ({e})", file=sys.stderr)
        return
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def iso_to_epoch(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def epoch_to_iso(e):
    return datetime.fromtimestamp(e, timezone.utc).strftime("%H:%M:%S")


QUOTED = re.compile(r'"([^"]+)"')


def pm_terms(question):
    """Extract the quoted target word(s) from a PM mention question."""
    return {t.strip().lower() for t in QUOTED.findall(question or "")}


def kalshi_terms(sub_title):
    """Kalshi packs alternates into one phrase with '/'."""
    return {p.strip().lower() for p in (sub_title or "").split("/") if p.strip()}


def first_move(events, anchor, baseline, thresh):
    """First (ts, price) at/after anchor whose price departs baseline by thresh."""
    for ts, px in events:
        if ts < anchor:
            continue
        if baseline is None or abs(px - baseline) >= thresh:
            return ts, px
    return None, None


def baseline_before(events, anchor):
    b = None
    for ts, px in events:
        if ts < anchor:
            b = px
        else:
            break
    return b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True)
    ap.add_argument("--event", required=True)
    ap.add_argument("--pm-capture", required=True)
    ap.add_argument("--kalshi-tape", required=True)
    ap.add_argument("--anchor", required=True, help="UTC ISO8601: call start (word) or release instant (release)")
    ap.add_argument("--move-threshold", type=float, default=0.10,
                    help="price departure from baseline that counts as a pump (0-1 scale)")
    a = ap.parse_args()

    pmmap = json.load(open(a.map))
    spec = pmmap.get(a.event)
    if not spec:
        raise SystemExit(f"FATAL: {a.event} not in {a.map}")
    mode = spec.get("clock", "word")
    anchor = iso_to_epoch(a.anchor)

    # ── PM side: trades (second) and book top-of-book (millisecond) ──────────
    # ONLY the YES token. Each PM market has a yes/no token PAIR and pm-capture
    # records both under the same `q`; pooling them produces a nonsense series
    # that walks between p and 1-p. Caught on the PLTR dry run: "Anthropic"
    # showed a 0.75 baseline against a real YES book of 0.10/0.24 — that 0.75
    # was the NO token. tokens[0] is YES per the Gamma clobTokenIds order that
    # scripts/pm-map-build.py preserves.
    yes_assets = {m["tokens"][0] for m in spec["markets"]}
    pm_tr, pm_bk = {}, {}
    skipped_no = 0
    for r in pm_rows(a.pm_capture):
        q = r.get("q")
        if not q:
            continue
        if r.get("asset") not in yes_assets:
            skipped_no += 1
            continue
        if r.get("t") == "trade":
            pm_tr.setdefault(q, []).append((float(r["server_ts_s"]), float(r["price"])))
        elif r.get("t") == "book":
            bb, ba = r.get("bb"), r.get("ba")
            if bb in (None, "") or ba in (None, ""):
                continue
            mid = (float(bb) + float(ba)) / 2.0
            pm_bk.setdefault(q, []).append((float(r["server_ts_ms"]) / 1000.0, mid))
    for d in (pm_tr, pm_bk):
        for v in d.values():
            v.sort()

    # ── Kalshi side: full tape only (never a limit=100 tail-pull) ────────────
    tape = json.load(open(a.kalshi_tape))
    k_tr = {}
    for tkr, trades in (tape.get("trades") or {}).items():
        rows = []
        for t in trades:
            try:
                rows.append((iso_to_epoch(t["created_time"]), float(t["yes_price_dollars"])))
            except (KeyError, ValueError):
                continue
        rows.sort()
        k_tr[tkr] = rows
    k_title = {t: (v or {}).get("yes_sub_title") for t, v in (tape.get("markets") or {}).items()}

    print(f"event   : {a.event}")
    print(f"mode    : {mode}   anchor {a.anchor}   move-threshold {a.move_threshold}")
    print(f"pm      : {sum(len(v) for v in pm_tr.values())} trades / "
          f"{sum(len(v) for v in pm_bk.values())} book rows over {len(pm_bk)} markets "
          f"(YES token only; {skipped_no} NO-token rows excluded)")
    print(f"kalshi  : {sum(len(v) for v in k_tr.values())} trades over {len(k_tr)} markets\n")

    if mode == "release":
        print("RELEASE-CLOCK MODE — the venues list DIFFERENT instruments, so there is no")
        print("per-word Δt. Reported: first trade on each venue after the release instant.")
        print("This measures venue reaction speed to a shared public fact. It is NOT a")
        print("word-level signal and must not be pooled with word-mode rows.\n")
        pm_first = min(((ts, q) for q, v in pm_tr.items() for ts, _ in v if ts >= anchor),
                       default=(None, None))
        k_first = min(((ts, t) for t, v in k_tr.items() for ts, _ in v if ts >= anchor),
                      default=(None, None))
        print(f"  PM     first trade after anchor: {epoch_to_iso(pm_first[0]) if pm_first[0] else '—':>10}  {pm_first[1] or ''}")
        print(f"  Kalshi first trade after anchor: {epoch_to_iso(k_first[0]) if k_first[0] else '—':>10}  {k_first[1] or ''}")
        if pm_first[0] and k_first[0]:
            d = k_first[0] - pm_first[0]
            lead = "PM" if d > 0 else "Kalshi"
            print(f"\n  Δ = {d:+.1f}s  → {lead} reacted first"
                  f"{'  (INSIDE the ~2 s quantisation floor — not resolvable)' if abs(d) < 2 else ''}")
        return

    # ── word mode: auto-match overlapping targets ───────────────────────────
    pairs = []
    for tkr, sub in k_title.items():
        kt = kalshi_terms(sub)
        if not kt:
            continue
        for q in set(list(pm_tr) + list(pm_bk)):
            if kt & pm_terms(q):
                pairs.append((tkr, sub, q))
                break

    if not pairs:
        print("no overlapping words matched — nothing to compare")
        return

    print(f"{'word':<24} {'K base':>7} {'K pump':>9} {'PM base':>8} {'PM pump':>9} "
          f"{'PMbook':>9} {'Δ trade':>9}  leader")
    print("-" * 92)
    deltas = []
    for tkr, sub, q in sorted(pairs, key=lambda p: p[1] or ""):
        ke = k_tr.get(tkr, [])
        pe = pm_tr.get(q, [])
        pb = pm_bk.get(q, [])
        kb, pbase, bbase = baseline_before(ke, anchor), baseline_before(pe, anchor), baseline_before(pb, anchor)
        kt_, kp = first_move(ke, anchor, kb, a.move_threshold)
        pt_, pp = first_move(pe, anchor, pbase, a.move_threshold)
        bt_, _ = first_move(pb, anchor, bbase, a.move_threshold)
        d = (kt_ - pt_) if (kt_ and pt_) else None
        if d is not None:
            deltas.append(d)
        leader = "—"
        if d is not None:
            leader = "tie(<2s)" if abs(d) < 2 else ("PM" if d > 0 else "Kalshi")
        print(f"{(sub or tkr)[:24]:<24} "
              f"{(f'{kb:.2f}' if kb is not None else '—'):>7} "
              f"{(epoch_to_iso(kt_) if kt_ else '—'):>9} "
              f"{(f'{pbase:.2f}' if pbase is not None else '—'):>8} "
              f"{(epoch_to_iso(pt_) if pt_ else '—'):>9} "
              f"{(epoch_to_iso(bt_) if bt_ else '—'):>9} "
              f"{(f'{d:+.1f}s' if d is not None else '—'):>9}  {leader}")

    print("-" * 92)
    if deltas:
        deltas.sort()
        med = deltas[len(deltas) // 2]
        resolvable = [d for d in deltas if abs(d) >= 2]
        print(f"n={len(deltas)} matched  median Δ={med:+.1f}s  "
              f"({len(resolvable)} outside the ~2 s quantisation floor)")
        if not resolvable:
            print("VERDICT: NOT RESOLVABLE — every Δ sits inside the joint second-granularity")
            print("floor. This does not mean the venues are equally fast; it means these")
            print("instruments cannot answer it. Use PM book (ms) vs a Kalshi WS recording.")
        else:
            lead = "PM leads" if med > 0 else "Kalshi leads"
            print(f"VERDICT: {lead} by {abs(med):.1f}s at the median.")
    else:
        print("no market moved on both venues — no Δt computable")


if __name__ == "__main__":
    main()
