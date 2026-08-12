#!/usr/bin/env python3
"""Join Kalshi settlement to our own run records — the first tool that can say
whether a detection was RIGHT.

WHY THIS EXISTS (2026-08-07). Every accuracy number the project has ever quoted
for a live event was hand-graded into a postmortem: "42 words said, 35 detected,
43 fired — recall 83%, precision 81%" (docs/history/POSTMORTEM-26AUG05.md). Nothing in
`scripts/` computes it. That matters more than bookkeeping, because the money
path has no term for being wrong: `ev_gate` asks "if this settles YES do I clear
2¢?" and never asks how often it does. At a 1¢ observe cap that is harmless. At
97¢ it is the whole question — buying at price p needs precision > p.

What it produces:
  1. per-market truth   — did we fire, did it settle YES
  2. per-keyword precision with sample counts (the input `ev_gate` lacks)
  3. realized P&L from the fills that actually happened
  4. a POLICY REPLAY — score a proposed gate/allocator against every banked
     event before it ever runs live

LOOKAHEAD DISCIPLINE. Estimating p from settled outcomes and then scoring a
policy on those same outcomes is lookahead bias, and it would make any proposed
gate look brilliant. Per-keyword p is therefore computed LEAVE-ONE-EVENT-OUT:
scoring event E uses only keywords' outcomes from events other than E. A keyword
with no out-of-sample history falls back to the pooled rate, itself computed
without E. Policies are compared on identical inputs, and the shrinkage is
stated in the output rather than buried.

Read-only. Public Kalshi endpoint, no credentials. Writes only its cache.

Usage:
  scripts/settle-score.py                      # score every banked event
  scripts/settle-score.py --event KXEARN...    # one event
  scripts/settle-score.py --keywords           # per-keyword precision table
  scripts/settle-score.py --replay             # policy counterfactual
  scripts/settle-score.py --json
  scripts/settle-score.py --check              # offline self-test

Exit: 0 scored, 2 could not determine (no settlement reachable).
"""
import argparse
import glob
import json
import math
import os
import sys
import tomllib
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://api.elections.kalshi.com/trade-api/v2"
CACHE = os.path.join(REPO, "state", ".settlements.json")

# ── the money arithmetic, mirrored from src/kalshi/executor.rs ───────────────
# Kept in lockstep by name so a drift is visible: fee_cents mirrors
# `fee_cents` (VERIFICATIONS Q5, ceil of 0.07*C*P*(1-P)); ev_cents is the
# CORRECTED gate — the shipped `ev_gate` has no p term and prices at the cap
# rather than the observed ask, which is what this tool exists to measure.


def fee_cents(count: int, price_cents: int) -> int:
    """ceil(7*C*pc*(100-pc) / 10_000) — integer, matches executor.rs:29."""
    num = 7 * count * price_cents * (100 - min(price_cents, 100))
    return -(-num // 10_000)


def ev_cents(p: float, price_cents: int, count: int = 1) -> float:
    """Expected cents per order. Enter at `price`, receive 100 iff YES.

    EV = p*(100 - price - fee) + (1-p)*(-price - fee) = 100p - price - fee.
    At p = 1.0 this is the shipped formula, so the change is a generalization.
    """
    return (100.0 * p - price_cents) * count - fee_cents(count, price_cents)


# ── evidence on disk ────────────────────────────────────────────────────────

def load_cache() -> dict:
    try:
        with open(CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(c: dict) -> None:
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=1, sort_keys=True)
    os.replace(tmp, CACHE)


def fetch_settlement(ticker: str, cache: dict, refresh: bool = False) -> dict | None:
    """{'status','result'} or None. Cached; only finalized rows are cached."""
    if not refresh and ticker in cache:
        return cache[ticker]
    try:
        with urllib.request.urlopen(f"{BASE}/markets/{ticker}", timeout=20) as r:
            m = json.load(r)["market"]
    except (urllib.error.URLError, ValueError, KeyError, OSError):
        return None
    row = {"status": m.get("status"), "result": m.get("result") or ""}
    # Only a finalized market is durable truth; an open one must be re-read.
    if row["status"] in ("finalized", "settled"):
        cache[ticker] = row
    return row


def config_for(event_id: str) -> str | None:
    """Find the arm config whose [event].id is this event."""
    for path in sorted(glob.glob(os.path.join(REPO, "configs", "*.toml"))):
        try:
            with open(path, "rb") as f:
                c = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if (c.get("event") or {}).get("id") == event_id:
            return path
    return None


def banked_events(only: str | None = None) -> list[dict]:
    """Every state/<E>/report.json with its config and keyword table."""
    out = []
    for rp in sorted(glob.glob(os.path.join(REPO, "state", "*", "report.json"))):
        try:
            with open(rp) as f:
                rep = json.load(f)
        except (OSError, ValueError):
            continue
        ev = rep.get("event_id") or os.path.basename(os.path.dirname(rp))
        if only and ev != only:
            continue
        cfg_path = config_for(ev)
        kws = []
        if cfg_path:
            with open(cfg_path, "rb") as f:
                cfg = tomllib.load(f)
            for i, k in enumerate(cfg.get("keywords") or []):
                kws.append({
                    "idx": i,
                    "phrase": k.get("phrase", ""),
                    "ticker": k.get("market_ticker", ""),
                    "max_price_cents": k.get("max_price_cents"),
                })
        out.append({"event_id": ev, "report": rep, "config": cfg_path,
                    "keywords": kws, "report_path": rp})
    return out


def score_event(e: dict, cache: dict, refresh: bool = False) -> dict:
    """Per-market truth for one event."""
    rep, kws = e["report"], e["keywords"]
    by_ticker = {k["ticker"]: k for k in kws if k["ticker"]}

    # A market is FIRED if we produced at least one intent for it. Intents are
    # the trading unit — a detection that never became an intent never risked
    # money. Both shapes carry the ticker.
    fired: set[str] = set()
    for d in rep.get("detections") or []:
        it = d.get("intent") or {}
        if it.get("market_ticker"):
            fired.add(it["market_ticker"])
    for o in rep.get("orders") or []:
        if o.get("ticker"):
            fired.add(o["ticker"])

    settle: dict[str, str] = {}
    unknown: list[str] = []
    for t in sorted(by_ticker) or sorted(fired):
        row = fetch_settlement(t, cache, refresh)
        if row and row.get("result") in ("yes", "no"):
            settle[t] = row["result"]
        else:
            unknown.append(t)

    said = {t for t, r in settle.items() if r == "yes"}
    fired_known = {t for t in fired if t in settle}
    tp = fired_known & said

    # Realized P&L: a marketable limit fills at the RESTING ask, not at our cap
    # (DASH-AFFO: cap 97, best_ask 95, postmortem records the fill at 95¢).
    realized = 0.0
    fills = []
    for o in rep.get("orders") or []:
        if o.get("order_status") != "filled":
            continue
        ask = (o.get("best_ask") or [None])[0]
        price = ask if ask is not None else o.get("price_cents")
        cnt = o.get("count") or 1
        res = settle.get(o.get("ticker", ""))
        fee = fee_cents(cnt, price)
        pnl = ((100 if res == "yes" else 0) - price) * cnt - fee if res else None
        if pnl is not None:
            realized += pnl
        fills.append({"ticker": o.get("ticker"), "price": price, "count": cnt,
                      "settled": res, "fee": fee, "pnl": pnl})

    return {
        "event_id": e["event_id"],
        "config": e["config"],
        "markets": len(by_ticker),
        "settled_known": len(settle),
        "unknown": unknown,
        "fired": sorted(fired),
        "said": sorted(said),
        "tp": sorted(tp),
        "precision": (len(tp) / len(fired_known)) if fired_known else None,
        "recall": (len(tp) / len(said)) if said else None,
        "fills": fills,
        "realized_cents": realized,
        "settle": settle,
        "by_ticker": by_ticker,
    }


# ── per-keyword precision, leave-one-event-out ──────────────────────────────

def keyword_stats(scored: list[dict]) -> dict:
    """phrase -> {'fired': n, 'tp': n, 'events': [...]} across all events."""
    agg: dict[str, dict] = {}
    for s in scored:
        for t in s["fired"]:
            if t not in s["settle"]:
                continue
            kw = s["by_ticker"].get(t)
            phrase = (kw or {}).get("phrase") or t.rsplit("-", 1)[-1]
            a = agg.setdefault(phrase, {"fired": 0, "tp": 0, "events": []})
            a["fired"] += 1
            a["tp"] += 1 if t in s["said"] else 0
            a["events"].append((s["event_id"], t, s["settle"][t]))
    return agg


def p_for(phrase: str, agg: dict, exclude_event: str | None,
          prior_strength: float = 4.0, pooled: float | None = None) -> float:
    """Beta-shrunk precision for one keyword, excluding one event.

    A 1-for-1 keyword must not report p = 1.0 — that is the whole reason for
    shrinkage. `prior_strength` is the weight given to the pooled rate.
    """
    a = agg.get(phrase)
    fired = tp = 0
    if a:
        for ev, _t, res in a["events"]:
            if exclude_event and ev == exclude_event:
                continue
            fired += 1
            tp += 1 if res == "yes" else 0
    if pooled is None:
        pooled = 0.5
    return (tp + prior_strength * pooled) / (fired + prior_strength)


def wilson(k: int, n: int) -> tuple[float, float]:
    """95% Wilson score interval — honest at the small n we actually have."""
    if n == 0:
        return (0.0, 1.0)
    z = 1.96
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def price_bands(scored: list[dict]) -> list[dict]:
    """P(settles YES | we fired, observed ask) bucketed by ask.

    THE POINT OF THIS TABLE. Pooled precision is a blend of two regimes and is
    the wrong number for any single trade. Conditioning on the market's own
    price shows the detector agrees with the market where the market is already
    sure, and disagrees with it — wrongly — where it is not. Both the
    26AUG05 postmortem and the first pass of this work drew the opposite
    conclusion by applying one pooled rate to a price-conditional bet.

    One row per (event, market): the FIRST intent, since later intents on the
    same market are the same decision re-observed. 1¢ observe intents count —
    the ask and the settlement are real regardless of what our cap was.
    """
    seen: dict[tuple[str, str], tuple[int, str, int]] = {}
    for s in scored:
        rep = s.get("report") or {}
        for o in sorted(rep.get("orders") or [], key=lambda x: x.get("decision_ts") or 0):
            ask = (o.get("best_ask") or [None])[0]
            res = s["settle"].get(o.get("ticker", ""))
            if ask is None or res is None:
                continue
            key = (s["event_id"], o.get("ticker", ""))
            seen.setdefault(key, (ask, res, o.get("price_cents") or 0))
    rows = list(seen.values())
    out = []
    for lo, hi in ((0, 20), (20, 40), (40, 60), (60, 80), (80, 95), (95, 101)):
        b = [r for r in rows if lo <= r[0] < hi]
        if not b:
            continue
        yes = sum(1 for r in b if r[1] == "yes")
        mid = (lo + hi) // 2
        ci = wilson(yes, len(b))
        out.append({"band": f"{lo}-{hi - 1}", "n": len(b), "yes": yes,
                    "p": yes / len(b), "ci": ci, "mid": mid,
                    "ev_at_mid": ev_cents(yes / len(b), mid),
                    "ev_at_ci_low": ev_cents(ci[0], mid),
                    "fillable": sum(1 for r in b if r[2] > 1)})
    return out


def pooled_rate(scored: list[dict], exclude_event: str | None = None) -> float:
    fired = tp = 0
    for s in scored:
        if exclude_event and s["event_id"] == exclude_event:
            continue
        for t in s["fired"]:
            if t in s["settle"]:
                fired += 1
                tp += 1 if t in s["said"] else 0
    return (tp / fired) if fired else 0.5


# ── policy replay ───────────────────────────────────────────────────────────

def replay(s: dict, agg: dict, scored: list[dict], policy: str,
           event_cap: int = 100, buffer_cents: int = 3,
           min_ev: int = 2) -> dict:
    """Re-run one event's order records under an allocation policy.

    Inputs are exactly what the executor saw: arrival order, observed best_ask,
    and the per-order cap. Outcomes come from settlement. `p` is out-of-sample.
    """
    orders = sorted((s["report"].get("orders") or []),
                    key=lambda o: o.get("decision_ts") or 0)
    pooled = pooled_rate(scored, exclude_event=s["event_id"])
    spent, taken, pnl = 0, [], 0.0

    def take(o, price, debit):
        nonlocal spent, pnl
        res = s["settle"].get(o.get("ticker", ""))
        if res is None:
            return
        fee = fee_cents(o.get("count") or 1, price)
        got = ((100 if res == "yes" else 0) - price) * (o.get("count") or 1) - fee
        spent += debit
        pnl += got
        taken.append({"ticker": o.get("ticker"), "price": price,
                      "debit": debit, "settled": res, "pnl": got})

    for o in orders:
        ask = (o.get("best_ask") or [None])[0]
        cap = o.get("price_cents") or 0
        if ask is None or ask > cap:
            continue                      # nothing crossable — not a policy choice
        if cap <= 1:
            continue                      # 1¢ observe posture never crosses
        kw = s["by_ticker"].get(o.get("ticker", ""))
        phrase = (kw or {}).get("phrase") or ""
        p = p_for(phrase, agg, exclude_event=s["event_id"], pooled=pooled)

        if policy == "actual":
            # Shipped: debit the CAP, EV computed at the CAP, first-come.
            if spent + cap > event_cap:
                continue
            if ev_cents(1.0, cap) < min_ev:
                continue
            take(o, ask, cap)
        elif policy == "ask_priced":
            # Phase 3 only: debit what we pay (+buffer). Still no p term.
            debit = min(ask + buffer_cents, cap)
            if spent + debit > event_cap:
                continue
            if ev_cents(1.0, ask) < min_ev:
                continue
            take(o, ask, debit)
        elif policy == "ev_gated":
            # Phase 2 + 3: debit what we pay AND require positive EV under p.
            debit = min(ask + buffer_cents, cap)
            if spent + debit > event_cap:
                continue
            if ev_cents(p, ask) < min_ev:
                continue
            take(o, ask, debit)
        else:
            raise ValueError(f"unknown policy {policy!r}")

    return {"policy": policy, "taken": taken, "spent": spent, "pnl": pnl}


# ── offline self-test ───────────────────────────────────────────────────────

def self_check() -> int:
    fails = []

    # fee: the exact cases asserted by executor.rs::fee_matches_verified_cases,
    # so a drift between this tool and the runtime shows up here first.
    for count, price, want in ((1, 50, 2), (100, 97, 21), (1, 1, 1), (0, 50, 0)):
        got = fee_cents(count, price)
        if got != want:
            fails.append(f"fee_cents({count},{price})={got} want {want}")
    # 1 contract at the 97¢ cap: 7*97*3/10_000 = $0.0204 → 1¢, which is what
    # makes risk.rs's "3¢ gross − 1¢ fee = 2¢ = min_ev_cents" true.
    if fee_cents(1, 97) != 1:
        fails.append(f"fee_cents(1,97)={fee_cents(1,97)} want 1")

    # ev at p=1 must reduce to the shipped gate: 100-price-fee
    if abs(ev_cents(1.0, 97) - (100 - 97 - fee_cents(1, 97))) > 1e-9:
        fails.append("ev_cents(p=1) does not reduce to the shipped formula")

    # the numbers that motivated this tool
    if not (-20 < ev_cents(0.81, 97) < -14):
        fails.append(f"ev_cents(.81,97)={ev_cents(0.81,97):.1f} want ~-17")
    if not (40 < ev_cents(0.81, 37) < 46):
        fails.append(f"ev_cents(.81,37)={ev_cents(0.81,37):.1f} want ~+42")

    # shrinkage: 1-for-1 must not read as certainty
    agg = {"w": {"fired": 1, "tp": 1, "events": [("E1", "T", "yes")]}}
    p = p_for("w", agg, exclude_event=None, pooled=0.5)
    if p >= 0.95:
        fails.append(f"1-for-1 keyword shrank to {p:.3f}, want well under 0.95")

    # leave-one-out must actually exclude
    if p_for("w", agg, exclude_event="E1", pooled=0.5) != 0.5:
        fails.append("leave-one-out did not exclude the scored event")

    for f in fails:
        print(f"CHECK-FAIL: {f}", file=sys.stderr)
    print("self-check: " + ("FAILED" if fails else "OK"))
    return 1 if fails else 0


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--keywords", action="store_true")
    ap.add_argument("--bands", action="store_true",
                    help="P(YES | fired, ask) by price band — the headline table")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--buffer-cents", type=int, default=3)
    ap.add_argument("--event-cap", type=int, default=100)
    a = ap.parse_args()

    if a.check:
        return self_check()

    cache = load_cache()
    evs = banked_events(a.event)
    if not evs:
        print("no banked events with a report.json", file=sys.stderr)
        return 2
    scored = [score_event(e, cache, a.refresh) for e in evs]
    for s, e in zip(scored, evs):
        s["report"] = e["report"]
    save_cache(cache)

    have = [s for s in scored if s["settled_known"]]
    if not have:
        print("no settlement reachable for any banked event", file=sys.stderr)
        return 2

    agg = keyword_stats(scored)

    if a.json:
        out = {"events": [{k: v for k, v in s.items() if k != "report"} for s in scored],
               "keywords": agg, "pooled_precision": pooled_rate(scored),
               "price_bands": price_bands(scored)}
        if a.replay:
            out["replay"] = {s["event_id"]: {p: replay(s, agg, scored, p,
                                                       a.event_cap, a.buffer_cents)
                                             for p in ("actual", "ask_priced", "ev_gated")}
                             for s in scored}
        print(json.dumps(out, indent=1, default=str))
        return 0

    # ── per-event table ─────────────────────────────────────────────────────
    print(f"{'event':44}{'mkts':>5}{'fired':>6}{'said':>5}{'TP':>4}"
          f"{'prec':>7}{'recall':>8}{'fills':>6}{'realized¢':>11}")
    tot_r = 0.0
    for s in sorted(scored, key=lambda x: x["event_id"]):
        if not s["settled_known"]:
            continue
        pr = f"{s['precision']:.2f}" if s["precision"] is not None else "  — "
        rc = f"{s['recall']:.2f}" if s["recall"] is not None else "  — "
        nf = sum(1 for f in s["fills"] if f["pnl"] is not None)
        tot_r += s["realized_cents"]
        print(f"{s['event_id'][:44]:44}{s['markets']:>5}{len(s['fired']):>6}"
              f"{len(s['said']):>5}{len(s['tp']):>4}{pr:>7}{rc:>8}{nf:>6}"
              f"{s['realized_cents']:>11.2f}")
    print(f"{'POOLED':44}{'':>5}{'':>6}{'':>5}{'':>4}"
          f"{pooled_rate(scored):>7.2f}{'':>8}{'':>6}{tot_r:>11.2f}")

    if a.keywords:
        print(f"\n{'keyword':24}{'fired':>6}{'right':>6}{'raw':>7}{'shrunk p':>10}   events")
        pooled = pooled_rate(scored)
        for phrase, v in sorted(agg.items(), key=lambda kv: (-kv[1]["fired"], kv[0])):
            raw = v["tp"] / v["fired"] if v["fired"] else 0
            p = p_for(phrase, agg, exclude_event=None, pooled=pooled)
            evs_s = ",".join(f"{e.split('MENTION')[-1][:12]}:{r}" for e, _t, r in v["events"][:4])
            print(f"{phrase[:24]:24}{v['fired']:>6}{v['tp']:>6}{raw:>7.2f}{p:>10.2f}   {evs_s}")

    if a.bands:
        print(f"\nP(settles YES | we fired, observed ask) — first intent per market")
        print(f"{'ask band':>10}{'n':>5}{'YES':>5}{'P':>7}{'95% CI':>15}"
              f"{'EV@mid':>9}{'EV@CIlow':>10}{'fillable':>10}")
        for b in price_bands(scored):
            print(f"{b['band'] + 'c':>10}{b['n']:>5}{b['yes']:>5}{b['p']:>7.2f}"
                  f"{f'[{b['ci'][0]:.2f},{b['ci'][1]:.2f}]':>15}"
                  f"{b['ev_at_mid']:>+8.1f}c{b['ev_at_ci_low']:>+9.1f}c{b['fillable']:>10}")
        print("  EV@CIlow is the same trade priced at the pessimistic end of the\n"
              "  interval — the column that says whether a band is PROVEN, not just\n"
              "  favourable. Break-even precision at 97c is 0.98.")

    if a.replay:
        print(f"\npolicy replay — event cap {a.event_cap}¢, buffer +{a.buffer_cents}¢, "
              f"p is leave-one-event-out")
        print(f"{'event':38}{'actual':>18}{'ask_priced':>18}{'ev_gated':>18}")
        tots = {"actual": 0.0, "ask_priced": 0.0, "ev_gated": 0.0}
        for s in sorted(scored, key=lambda x: x["event_id"]):
            if not s["settled_known"]:
                continue
            cells = []
            for pol in ("actual", "ask_priced", "ev_gated"):
                r = replay(s, agg, scored, pol, a.event_cap, a.buffer_cents)
                tots[pol] += r["pnl"]
                cells.append(f"{len(r['taken'])}ord {r['pnl']:+7.1f}¢")
            if any(not c.startswith("0ord") for c in cells):
                print(f"{s['event_id'].split('MENTION')[-1][:38]:38}"
                      f"{cells[0]:>18}{cells[1]:>18}{cells[2]:>18}")
        print(f"{'TOTAL':38}{tots['actual']:>17.1f}¢{tots['ask_priced']:>17.1f}¢"
              f"{tots['ev_gated']:>17.1f}¢")

    return 0


if __name__ == "__main__":
    sys.exit(main())
