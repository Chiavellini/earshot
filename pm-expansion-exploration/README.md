# pm-expansion-exploration

Exploratory research, requested 2026-08-05, split into two independent
tracks. Both are read-only against production and write only inside this
directory.

**Isolation guarantee**: nothing here is ever registered into `configs/`,
and no file under `configs/`, `scripts/` (existing), `src/`, `docs/`,
`state/`, or `slate/` is ever modified by anything in this directory.
Existing tools (`xvenue-autorun.sh`, `pm-map-build.py`, `xvenue-report.py`,
`mentions-census.py`, `venue-probe.sh`) are called only in their
already-safe, already-read-only/capture-only modes — no orders, no
production config changes, no writes back into `state/`/`slate/`. This
directory is confirmed **not** governed by `architecture/nodes.toml`'s
`governed_globs` — no node-contract/doc-lint obligation while this stays
exploratory. `git status` should never show a diff outside this directory
from this work.

## Track 1 — `profitability/`: was the "PM is unexploitable" verdict measuring the right clock?

The prior xvenue lane anchored "could we have taken this PM liquidity" on
**Kalshi's own reprice moment** and found $0.00 takeable. Our edge comes
from the call audio directly, independent of any market — so this track
recomputes the same depth math anchored on **our own reachable-action
time** instead, plus a broader (non-anchor-dependent) book-shape survey and
a sensitivity-grid EV model. See `profitability/README.md` for results.

Multi-event live capture: `CAPTURE-PIPELINE.md`, `call-times.json`,
`maps/`, `captures/`.

## Track 2 — `other-venues/`: wider mention-market venue survey

Beyond earnings calls: FOMC, hearings, debates, political speeches,
podcasts, and whatever a systematic discovery pass turns up beyond the 5
candidates `docs/EXPANSION.md` already named. See `other-venues/README.md`.

## Status (2026-08-05)

Both tracks have a first complete pass. See each subdirectory's own README
for results, open questions, and recommended next steps — headline points:

- **Profitability**: the prior "$0.00 takeable" verdict was anchored on
  Kalshi's reprice moment; reanchored on our own reachable-action time the
  result is inconclusive at n=1 (inside the wall-clock bridge's own error
  bound), but a broader, non-anchor-dependent book-shape survey shows PM's
  book is NOT uniformly wide the way the single anchored word suggested —
  10 of 32 pooled instruments show consistently real resting depth. A live
  capture opportunity (ABNB, 2026-08-06 21:00 UTC) is staged; today's other
  6 Kalshi earnings events have no Polymarket mirror at all right now.
- **Other venues**: 206 non-earnings candidate tickers found beyond
  `docs/EXPANSION.md`'s original 5; TBPN scores highest (fixed schedule,
  single confirmed channel, no hard gate) but is currently dormant on
  Kalshi; `trump-bilateral-pressers` has real, measured, recurring PM
  volume ($22K-$1.23M) and an already-existing (dormant) Kalshi series.
