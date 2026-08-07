#!/usr/bin/env python3
"""pm_sweep.py — Polymarket trade-tape fetch + sweep-onset detection.

Polymarket-side counterpart to pm-expansion-exploration/latency/lib/
kalshi_sweep.py (same sweep-detection logic, different venue). Uses the same
call shape already proven in scripts/pm-capture.py (`data-api.polymarket.com
/trades?market=<conditionId>`) and already confirmed free/no-auth/full-history
in pm-expansion-exploration/latency/rogan/NOTES.md (482 trades recovered from
a 22-month-old settled market, no truncation).

PM's price field is already a 0..1 decimal (same convention as Kalshi's
yes_price_dollars), so the sweep-onset threshold logic transfers directly —
only the transport (unix-seconds `timestamp`) differs from Kalshi's
cursor-paginated, ISO8601 tape.

SECOND BUG FOUND AND FIXED 2026-08-07: "no cursor pagination observed
needed at this scale" was wrong for a high-volume market. The endpoint
returns NEWEST-first and silently truncates at `limit` with no error --
confirmed live: SOTU's ALIEN condition has 548 total trades, but
`trades(cid)` with the old unpaginated default (limit=500) silently
dropped the OLDEST 48 (fetchable only via `&offset=500`). Worse, the
retained batch's earliest trade then LOOKS LIKE a real sweep onset to
sweep_onset() (it's the first data point available), when it may just be
the truncation boundary -- a false onset, not a refusal. Fixed via
offset-paginated fetching until a page returns fewer than `limit` rows.

BUG FOUND AND FIXED 2026-08-07: each trade also carries an `outcome` field
("Yes" or "No") -- Polymarket's binary markets trade two complementary
token sides, and a "No" trade's own `price` is the price of the NO token,
NOT the market's implied YES probability. A No trade at price=0.28 means
YES is implied ~72% likely (1 - 0.28), the OPPOSITE of what `price` alone
suggests. `trades()` previously used `price` directly regardless of
`outcome`, corrupting `_yes` for every No-side trade -- confirmed live
against KEYNOTE-TSMC's tape: 86 of 116 trades were No-side, and the raw
(uncorrected) `_yes` series flickered nonsensically between ~0.001 and
~0.96 second-to-second; the corrected (outcome-aware) series is a coherent,
slowly-evolving price. This affected every Polymarket venue built on this
module (rogan/, sotu/, keynote/) -- their sweep_onset()-derived verdicts
were computed on this corrupted `_yes` and need re-verification, not just
today's fill-window check.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import urllib.request

API = "https://data-api.polymarket.com/trades"


def get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def implied_yes(price: float, outcome: str) -> float:
    """A trade's own `price` is only the market's implied YES probability
    when outcome == "Yes"; a No-side trade at `price` implies YES ~= 1 -
    price. See module docstring's 2026-08-07 bug note."""
    return price if outcome == "Yes" else 1.0 - price


def trades(condition_id: str, page_size: int = 500) -> list[dict]:
    """All public trades for one Polymarket condition, oldest first. Pages
    via `offset` until a page comes back short (endpoint returns
    newest-first internally; the full set is sorted oldest-first below
    regardless)."""
    out, offset = [], 0
    while True:
        url = f"{API}?market={condition_id}&limit={page_size}&offset={offset}"
        batch = get(url)
        out.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    for t in out:
        t["_ts"] = datetime.fromtimestamp(t["timestamp"], tz=timezone.utc)
        t["_yes"] = implied_yes(float(t["price"]), t.get("outcome"))
    out.sort(key=lambda t: t["_ts"])
    return out


def sweep_onset(
    tr: list[dict],
    t0: datetime,
    sweep_price: float = 0.90,
    sweep_hold: float = 0.80,
    burst_gap_s: float = 5.0,
    prepriced: float = 0.85,
    window_before_min: float = 60.0,
    window_after_min: float = 300.0,
    hold_window_min: float | None = None,
):
    """First trade of the burst that permanently reprices toward $1.

    Same shape and defaults family as lib/kalshi_sweep.py's sweep_onset() —
    window_before/after widened here since a 3-hour podcast, unlike a 22-min
    newscast, needs a much wider search window around its own start.
    """
    if not tr:
        return None, "no-trades"
    idx = None
    for i, t in enumerate(tr):
        if hold_window_min is None:
            window = tr[i:]
        else:
            cutoff = t["_ts"] + timedelta(minutes=hold_window_min)
            window = [u for u in tr[i:] if u["_ts"] <= cutoff]
        if t["_yes"] >= sweep_price and all(u["_yes"] >= sweep_hold for u in window):
            idx = i
            break
    if idx is None:
        return None, "never-repriced"
    pre = [t for t in tr if t["_ts"] < t0 - timedelta(seconds=60)]
    if pre and pre[-1]["_yes"] >= prepriced:
        return None, "pre-priced"
    while idx > 0 and (tr[idx]["_ts"] - tr[idx - 1]["_ts"]).total_seconds() < burst_gap_s:
        idx -= 1
    onset = tr[idx]["_ts"]
    if not (t0 - timedelta(minutes=window_before_min) <= onset <= t0 + timedelta(minutes=window_after_min)):
        return None, "outside-window"
    return onset, "ok"


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

    print("pm_sweep self-test:")

    chk("Yes-side trade: price IS the implied YES price", 0.73, implied_yes(0.73, "Yes"))
    chk("No-side trade: implied YES is 1 - price, not price itself",
        0.72, round(implied_yes(0.28, "No"), 2))
    chk("No-side trade at a near-zero No price implies YES near-certain",
        True, implied_yes(0.001, "No") > 0.99)

    def tr(px, t_offset_s, t0):
        ts = t0 + timedelta(seconds=t_offset_s)
        return {"_ts": ts, "_yes": px}

    t0 = datetime(2026, 7, 28, 17, 0, 0, tzinfo=timezone.utc)

    tape = [tr(0.03, -30, t0), tr(0.04, 0, t0), tr(0.95, 4910, t0), tr(0.97, 4911, t0), tr(0.99, 5000, t0)]
    onset, why = sweep_onset(tape, t0)
    chk("sweep onset finds the burst start", "ok", why)
    chk("onset is the burst's first trade", t0 + timedelta(seconds=4910), onset)

    flat = [tr(0.05, 0, t0), tr(0.06, 30, t0)]
    chk("never repriced refuses", (None, "never-repriced"), sweep_onset(flat, t0))

    chk("empty tape refuses", (None, "no-trades"), sweep_onset([], t0))

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
