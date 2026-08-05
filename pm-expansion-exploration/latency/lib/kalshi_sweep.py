#!/usr/bin/env python3
"""kalshi_sweep.py — generic Kalshi trade-tape fetch + sweep-onset detection.

Extracted as a PATTERN, not an import, from scripts/wnt-pack-lag.py's
`trades()`/`sweep_onset()` (production, untouched, never imported from here
— see that file for the original WNT-specific caller). Those two functions
were already venue-agnostic: they take a ticker and a nominal event-start
time and need nothing WNT-specific. This file is that generic core, reused
by every venue-class pack-lag build under pm-expansion-exploration/latency/.

Read-only against Kalshi's public trades endpoint (no auth, no orders).
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import urllib.request

API = "https://api.elections.kalshi.com/trade-api/v2"


def get(url: str):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def trades(ticker: str) -> list[dict]:
    """All public trades for one Kalshi market ticker, oldest first."""
    out, cursor = [], ""
    while True:
        url = f"{API}/markets/trades?ticker={ticker}&limit=1000"
        if cursor:
            url += f"&cursor={cursor}"
        d = get(url)
        batch = d.get("trades", [])
        out.extend(batch)
        cursor = d.get("cursor") or ""
        if not cursor or not batch:
            break
    for t in out:
        t["_ts"] = datetime.fromisoformat(t["created_time"].replace("Z", "+00:00"))
        t["_yes"] = float(t["yes_price_dollars"])
    out.sort(key=lambda t: t["_ts"])
    return out


def sweep_onset(
    tr: list[dict],
    t0: datetime,
    sweep_price: float = 0.90,
    sweep_hold: float = 0.80,
    burst_gap_s: float = 5.0,
    prepriced: float = 0.85,
    window_before_min: float = 5.0,
    window_after_min: float = 60.0,
    hold_window_min: float | None = None,
):
    """First trade of the burst that (durably) reprices a strike toward $1.

    Same defaults as wnt-pack-lag.py (tuned on a 22-minute newscast); a
    multi-hour venue (a 3-hour game or podcast) may want different
    window_after_min / burst_gap_s — re-derive from that venue's own data
    rather than trusting these blindly, per the house discipline this
    pattern was built under.

    `hold_window_min`: bounds how far forward the "holds >= sweep_hold" check
    looks. Default None reproduces wnt-pack-lag.py's original behavior
    (holds checked against EVERY remaining trade, however far in the
    future). That default silently breaks on venues with thin, noisy
    post-event trading days later: a WNBA market's real in-game sweep (a
    technical foul repriced 20s after the call, confirmed against a
    play-by-play feed) was masked because a single trade nearly 10 HOURS
    after the game dipped to 0.70 before resettling at 0.99 — the
    unbounded "holds forever" check walked straight past the real onset
    to find that much later point instead. Set `hold_window_min` (e.g. 30)
    on any venue where trading continues well past the event itself.

    Returns (onset_datetime_or_None, reason_string).
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

    print("kalshi_sweep self-test:")
    from datetime import timezone

    def tr(px, t_offset_s, t0):
        ts = t0 + timedelta(seconds=t_offset_s)
        return {"_ts": ts, "_yes": px}

    t0 = datetime(2026, 8, 5, 20, 0, 0, tzinfo=timezone.utc)

    # A clean sweep: cheap, then a burst to near-$1 that holds.
    tape = [tr(0.05, -30, t0), tr(0.06, 0, t0), tr(0.95, 10, t0), tr(0.97, 10.5, t0), tr(0.98, 40, t0)]
    onset, why = sweep_onset(tape, t0)
    chk("sweep onset finds the burst start", why, "ok")
    chk("onset is the burst's first trade", t0 + timedelta(seconds=10), onset)

    # No repricing at all -> refuse.
    flat = [tr(0.05, 0, t0), tr(0.06, 30, t0)]
    onset, why = sweep_onset(flat, t0)
    chk("never repriced refuses", (None, "never-repriced"), (onset, why))

    # Already priced high before the event started off news, not the word.
    pre = [tr(0.90, -600, t0), tr(0.91, -300, t0), tr(0.95, 10, t0), tr(0.96, 40, t0)]
    onset, why = sweep_onset(pre, t0)
    chk("pre-priced refuses", (None, "pre-priced"), (onset, why))

    # Sweep well outside the event window is refused, not silently accepted.
    late = [tr(0.05, 0, t0), tr(0.95, 7200, t0), tr(0.96, 7201, t0)]
    onset, why = sweep_onset(late, t0, window_after_min=60.0)
    chk("outside window refuses", (None, "outside-window"), (onset, why))

    # No trades at all.
    chk("empty tape refuses", (None, "no-trades"), sweep_onset([], t0))

    # Real bug found running this against a live KXWNBAMENTION-...-TECH
    # market (2026-08-05): an in-game burst holds cleanly for hours, then
    # ONE thin post-settlement trade the next morning dips below the hold
    # threshold before resettling. Unbounded, "holds forever" walks past
    # the real onset to find that far-later point instead.
    noisy = ([tr(0.05, -30, t0), tr(0.06, 0, t0), tr(0.99, 20, t0), tr(0.99, 30, t0)]
             + [tr(0.99, 3600 * h, t0) for h in range(1, 10)]
             + [tr(0.70, 3600 * 10, t0), tr(0.99, 3600 * 10 + 5, t0)])
    onset, why = sweep_onset(noisy, t0, window_after_min=700.0)
    chk("unbounded hold walks past the real onset (documents the old bug)",
        "ok", why)
    chk("...and lands on the late post-settlement dip, not the real burst",
        t0 + timedelta(hours=10, seconds=5), onset)
    onset, why = sweep_onset(noisy, t0, window_after_min=700.0, hold_window_min=30.0)
    chk("bounded hold_window_min finds the real in-game onset instead",
        "ok", why)
    chk("...at the correct, much-earlier timestamp",
        t0 + timedelta(seconds=20), onset)

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
