# Source-feed latency investigation

Scope: does any fixed-schedule mention-market venue have a demonstrably
low-latency source feed — and specifically, is a low-latency sports
commentator feed obtainable at all, given the existing "linear-TV blocker"
verdict for sports was never independently measured. Read-only /
research-and-new-code-only against `pm-expansion-exploration/latency/`;
nothing under `configs/`, `scripts/` (existing), `src/`, `docs/`, `state/`,
or `slate/` is ever modified.

## Pre-registered decision rule (stated before any number below is used to decide anything)

Two decision rules already exist in this codebase, use **different metrics
with different sign conventions**, and must never be silently conflated:

1. **`scripts/wnt-pack-lag.py`'s own `offset` rule** (production, read-only
   reference): `offset` = how far behind (positive) or ahead of (negative)
   the real-world event the trading pack's reaction sits, from a slope-fit
   join of a leg-1 timing reference against the market's own sweep onsets.
   `worst offset < 15s` ⇒ the pack is already AT the broadcast — no feed
   can win, close the lane. `median offset > 20s` ⇒ the pack is materially
   delayed — a live feed is worth trying. Otherwise: MARGINAL, judge the
   distribution, not one number.
2. **`docs/COMPETITOR-ANATOMY.md`'s `Δrace` convention**: a *different*
   quantity (our-detection-relative-to-sweep, not pack-relative-to-event),
   opposite-signed thresholds (`≥−2s` contestable, `≤−5s` dead).

**Every result below is reported against rule (1)**, since every pack-lag
build in this investigation follows `wnt-pack-lag.py`'s own pattern
directly (leg-1 timing reference × Kalshi/PM sweep onset × slope-fit). Rule
(2) is not used here and should not be retrofit onto these numbers without
restating them in Δrace's own terms first.

## What's here

- `schedule-facts.json`/`.md` — real-world-vs-Kalshi-ticker-history schedule
  facts, replacing the unreliable `slate/census.json` `cadence` field.
- `lib/kalshi_sweep.py`, `lib/slope_fit.py` — the generic two-clock join +
  refuse-don't-fabricate core, pattern-extracted from `wnt-pack-lag.py`,
  reused by every venue build below.
- `mlb/`, `wnba/` — pack-lag builds, run against real settled games.
- `tbpn/` — leg-1 (VOD/caption) build only, banked for the next join.
- `rogan/NOTES.md` — Polymarket trades-depth verification.
- `sports-feed-research/` — MLB/WNBA/NHL/NFL commentator-feed accessibility.
- `latency-scoreboard.md` — the final synthesis.

## Headline finding

**Sports does not show WNT's failure mode.** WNT's own trading pack reacts
at essentially zero offset (median −6.31s — already at the broadcast, no
feed can beat it). MLB's one measured strike (a fielding error) shows the
pack reacting **+140.56s** late; WNBA's three measured strikes (traveling,
technical foul, across 2 games) show **+12.0s / +16.6s / +26.4s**, slope
0.997 (the join is trustworthy). Both are small samples, but both point the
same direction: **the existing sports mention-market pack is not already
unbeatable, unlike WNT's.** The blanket PARKED verdict inherited by
analogy from WNT does not hold up against real, if early, data.

See `latency-scoreboard.md` for the full table and every caveat.

## Update 2026-08-05 (later same day): live capture test, before spending anything

Per user direction, moved from research to an actual live-capture test using
the real system — see `mlb/LIVE-CAPTURE-STATUS.md` for full detail. Headline:
a real, free (no subscription) MLB radio stream was resolved and verified
working (KNBR 680, Giants, Triton Digital CDN), and the full mechanism —
`capture-bakeoff` → wall-clock anchor → `earshot replay` (confirmed, at the
Rust type-system level, structurally incapable of placing an order) → real
`PipelineReport` with genuine detection-latency numbers — was built and
shaken out end-to-end successfully. The only blocker is external: Kalshi has
not listed a new `KXMLBMENTION`/`KXWNBAMENTION` event since 2026-07-30. A
poll script (`poll_new_market.py`, since generalized to also watch
`KXTBPNMENTION` — see the Update below) is ready to catch the moment one
reappears, at which point the live three-way comparison (our detection vs.
the real event vs. the market's price pump) can run immediately.
