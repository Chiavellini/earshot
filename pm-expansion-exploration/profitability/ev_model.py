#!/usr/bin/env python3
"""ev_model.py — sensitivity-grid EV model for "confirmed-word -> take PM
liquidity." Design intent: never produce one flattering headline number.
Every input is tagged by evidence class (measured / assumed / unresolved)
and every output is a grid, not a point estimate.

    EV_per_attempt = precision * upside_per_hit
                    - (1 - precision) * downside_per_miss
                    - execution_costs

`downside_per_miss` matters and must not be zero: a taker order that crosses
Polymarket's ask fills immediately by construction (that's what "taking"
means) — there is no cancel-before-fill escape on a false positive. A miss
loses the entire price paid, not nothing.

Inputs and their evidence class:
  - detector_precision  MEASURED, but from a DIFFERENT event (RDDT 26JUL30,
    cited in docs/EXPANSION.md) — an external-transfer assumption until a
    same-event number exists.
  - detector_latency     Two numbers exist on disk and DISAGREE:
    docs/BAKEOFF.md's onset->emit p95 (~1000-1300ms, engine-benchmarked from
    true word onset) vs. the armed PLTR run's own report.json "capture->
    decision" edge (p50 123ms / p90 188ms, n=81 — a narrower, later pipeline
    span). This script does NOT pick one silently — both are surfaced.
  - kalshi_taker_fee     MEASURED: ceil_cent(0.07 * C * P * (1-P)) per order,
    VERIFICATIONS.md (no settlement fee).
  - pm_order_latency_ms  ASSUMED sensitivity grid (100/300/1000ms) — no PM
    execution code has ever run in this repo, so no measured number exists.
  - pm_taker_fee,
    polygon_gas          UNRESOLVED / NEED-A-LOOKUP. No PM execution code
    exists anywhere in this repo (scripts/pm-capture.py, pm-tape.py,
    book-tape.rs, xvenue-normalize.py, xvenue-report.py are all read-only
    market data). Placeholder 0.0 here is NOT a measurement — see the "Open
    Questions" note this script prints, and profitability/README.md.
  - captured_edge_per_hit  OUTPUT of reanchor_takeable.py — currently n=1
    anchored word, itself carrying a bridge-error caveat.

Usage:
  ev_model.py --out pm-expansion-exploration/profitability/out/ev-grid.csv
  ev_model.py --self-test
"""

import argparse
import csv
import itertools
import math
import sys

INPUT_NOTES = {
    "detector_precision": {
        "class": "measured-cross-event",
        "note": "RDDT 26JUL30 precision (docs/EXPANSION.md), transferred to a "
                "different event; needs a same-event number before trusting.",
    },
    "detector_latency_ms": {
        "class": "measured-but-disagreeing",
        "note": "docs/BAKEOFF.md onset->emit p95 ~1000-1300ms vs. armed PLTR "
                "run's capture->decision p50 123ms/p90 188ms — different "
                "spans, not interchangeable. Reconcile before trusting either "
                "in a downstream latency budget.",
    },
    "kalshi_taker_fee": {
        "class": "measured",
        "note": "ceil_cent(0.07 * C * P * (1-P)) per order, VERIFICATIONS.md.",
    },
    "pm_order_latency_ms": {
        "class": "assumed",
        "note": "No PM execution code has ever run; sensitivity grid only, no default.",
    },
    "pm_taker_fee_usd_per_dollar": {
        "class": "unresolved-need-a-lookup",
        "note": "No PM execution code exists in this repo. 0.0 placeholder below is "
                "NOT a measurement.",
    },
    "polygon_gas_usd_per_order": {
        "class": "unresolved-need-a-lookup",
        "note": "Believed off-chain-signed/relayed (gasless per trade) but unverified "
                "in-repo. 0.0 placeholder below is NOT a measurement.",
    },
    "captured_edge_per_hit": {
        "class": "output-of-reanchor_takeable",
        "note": "n=1 anchored word currently (PLTR ai slop), bridge-error-bounded. "
                "Feed real p50_usd/price_paid figures in via --price-points once "
                "more events land.",
    },
}


def kalshi_taker_fee_usd(price_usd, contracts=1):
    """ceil_cent(0.07 * C * P * (1-P)) — VERIFICATIONS.md."""
    fee_cents = math.ceil(0.07 * contracts * price_usd * (1 - price_usd) * 100)
    return fee_cents / 100.0


def ev_per_attempt(precision, price_paid_usd, pm_taker_fee_usd=0.0,
                    gas_usd=0.0, contracts=1):
    upside = (1.0 - price_paid_usd) * contracts
    downside = price_paid_usd * contracts
    costs = pm_taker_fee_usd + gas_usd
    return precision * upside - (1 - precision) * downside - costs


def scenario_grid(price_points_usd, precision_values, pm_fee_values, gas_values):
    rows = []
    for price, precision, fee, gas in itertools.product(
            price_points_usd, precision_values, pm_fee_values, gas_values):
        rows.append({
            "price_paid_usd": price,
            "precision": precision,
            "pm_taker_fee_usd": fee,
            "gas_usd": gas,
            "ev_per_attempt_usd": round(ev_per_attempt(precision, price, fee, gas), 4),
        })
    return rows


# Price points at the SAME edge-tier caps xvenue-report.py/reanchor_takeable.py
# use (EDGE_TIERS_E6 = 50k/100k/200k/500k -> max_price_usd = 0.95/0.90/0.80/0.50),
# so this grid is directly comparable to their tier tables.
DEFAULT_PRICE_POINTS = [0.95, 0.90, 0.80, 0.50]
DEFAULT_PRECISION_VALUES = [0.75]      # RDDT cross-event transfer; widen once
                                        # a same-event number exists.
DEFAULT_PM_FEE_VALUES = [0.0]          # NEED-A-LOOKUP placeholder
DEFAULT_GAS_VALUES = [0.0]             # NEED-A-LOOKUP placeholder


def write_csv(rows, path):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def print_notes():
    print("Input evidence classes (see module docstring for detail):")
    for name, meta in INPUT_NOTES.items():
        print(f"  [{meta['class']}] {name}: {meta['note']}")


def self_test():
    p = f = 0

    def chk(name, want, got):
        nonlocal p, f
        ok = abs(want - got) < 1e-9 if isinstance(want, float) else want == got
        if ok:
            print(f"  PASS  {name}")
            p += 1
        else:
            print(f"  FAIL  {name} (expected {want!r}, got {got!r})")
            f += 1

    print("ev_model self-test:")

    # Kalshi fee: 1c order, precision-independent formula sanity check.
    chk("fee at price=0.5 is at its max (P*(1-P) maximized)",
        math.ceil(0.07 * 1 * 0.5 * 0.5 * 100) / 100.0,
        kalshi_taker_fee_usd(0.5))
    chk("fee at price=0.01 is tiny", math.ceil(0.07 * 0.01 * 0.99 * 100) / 100.0,
        kalshi_taker_fee_usd(0.01))

    # A sure-thing (precision=1.0) buying at any price below $1 must be
    # strictly positive EV (upside always realized, no downside ever hits).
    chk("precision=1.0 is always profitable below $1", True,
        ev_per_attempt(1.0, 0.50) > 0)

    # A coin-flip (precision=0.5) at price=0.50 nets exactly zero before costs
    # (symmetric upside/downside).
    chk("precision=0.5 at price=0.5 is EV-neutral before costs", 0.0,
        ev_per_attempt(0.5, 0.50))

    # The downside term must be wired in: a low precision at a high price
    # must be net negative, not silently clamped to zero.
    ev_low_precision = ev_per_attempt(0.10, 0.90)
    chk("low precision at high price is a real loss", True, ev_low_precision < 0)
    chk("loss magnitude matches the miss-cost formula",
        round(0.10 * 0.10 - 0.90 * 0.90, 4), round(ev_low_precision, 4))

    # Costs must strictly reduce EV.
    base = ev_per_attempt(0.75, 0.50)
    with_costs = ev_per_attempt(0.75, 0.50, pm_taker_fee_usd=0.02, gas_usd=0.01)
    chk("fees and gas strictly reduce EV", True, with_costs < base)
    chk("cost reduction matches exactly", round(base - 0.03, 4), round(with_costs, 4))

    grid = scenario_grid([0.5, 0.9], [0.5, 0.9], [0.0], [0.0])
    chk("grid size is the cartesian product", 4, len(grid))

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="CSV path for the scenario grid")
    ap.add_argument("--price-points", default=",".join(str(x) for x in DEFAULT_PRICE_POINTS),
                     help="comma-separated price-paid-per-$1 points")
    ap.add_argument("--precision", default=",".join(str(x) for x in DEFAULT_PRECISION_VALUES),
                     help="comma-separated precision (hit-rate) values")
    ap.add_argument("--pm-taker-fee", default=",".join(str(x) for x in DEFAULT_PM_FEE_VALUES),
                     help="comma-separated PM taker fee USD/order values (UNRESOLVED)")
    ap.add_argument("--gas", default=",".join(str(x) for x in DEFAULT_GAS_VALUES),
                     help="comma-separated Polygon gas USD/order values (UNRESOLVED)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()

    prices = [float(x) for x in a.price_points.split(",") if x.strip()]
    precisions = [float(x) for x in a.precision.split(",") if x.strip()]
    fees = [float(x) for x in a.pm_taker_fee.split(",") if x.strip()]
    gases = [float(x) for x in a.gas.split(",") if x.strip()]

    print_notes()
    print()
    grid = scenario_grid(prices, precisions, fees, gases)
    for row in grid:
        print(f"price=${row['price_paid_usd']:.2f} precision={row['precision']:.0%} "
              f"pm_fee=${row['pm_taker_fee_usd']:.2f} gas=${row['gas_usd']:.2f} "
              f"-> EV/attempt = ${row['ev_per_attempt_usd']:+.4f}")
    if a.out:
        write_csv(grid, a.out)
        print(f"\nwrote {a.out} ({len(grid)} scenarios)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
