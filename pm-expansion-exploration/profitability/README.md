# Task 1 — Was the "PM is unexploitable" verdict measuring the right clock?

## The question

The prior xvenue lane concluded Polymarket (PM) has nothing worth taking
even once we independently know a word was said, because `takeable_axis()`
in `scripts/xvenue-report.py` found $0.00 resting below 90c in the 30s/300s
after **Kalshi's own book repriced** (PLTR "ai slop", the one anchored word
that exists). `docs/EXPANSION.md` read that as "PM is wide, not slow."

The objection: our edge comes from the call audio directly, not from
watching Kalshi. Anchoring "could we have taken this" on Kalshi's reaction
time is the wrong clock. This directory recomputes the same depth math
anchored on **our own** reachable-action time instead.

## What's here

- `extract_decisions.py` — bridges our detector's `decision_ts` (monotonic,
  no wall-clock pair anywhere in a run's artifacts today — this is a gap
  `xvenue-report.py`'s own `mention_anchor()` docstring calls out explicitly)
  onto a wall clock via `listener.ready`'s `unix_ts`. **Approximation, not
  ground truth** — carries an explicit `bridge_error_s` (default 2.0s: whole-
  second truncation + an unmeasured listener-ready→first-frame gap).
- `reanchor_takeable.py` — imports `scripts/xvenue-report.py` **unmodified**
  and reuses its ladder/time-weighting primitives verbatim, changing only
  the anchor timestamp to `decision_ts + bridge_offset + PM_order_latency`
  (a required sensitivity grid, no default — no PM execution code has ever
  run, so no latency number is measured).
- `book_shape_survey.py` — no anchor needed at all. Pools every PM quote row
  across every capture (matched to a Kalshi word or not) to characterize
  whether thin depth-at-good-price is structural or reprice-transient, at
  much higher n than the single anchored word gives.
- `ev_model.py` — sensitivity-grid EV model. Every input tagged measured /
  assumed / unresolved. No PM execution fee/gas data exists anywhere in this
  repo — flagged as open lookups below, not silently assumed zero.

## Results so far (n=2 historical events, re-run 2026-08-05)

**Verification passed**: `reanchor_takeable.py`'s `kalshi_reprice_anchor_ns`
for PLTR "ai slop" reproduces `scripts/xvenue-report.py`'s own frozen
`anchor_ns` bit-for-bit (`1785790990784292096`), and `book_shape_survey.py`'s
whole-session stats for the same instrument reproduce the frozen tool's
`tiers.10c.buy_yes.all` bit-for-bit (`snapshots=714, max_usd=73.8,
p50_usd=0.0, persisted_s=1220.6`). The imported primitives are wired
correctly.

**The reanchored result, PLTR "ai slop" (the only word with real
`decision_ts` data — production armed this event, exploration captured it,
and the word was actually said):**

| PM order latency | Kalshi anchor − reach anchor | ≥10c takeable, 30s after reach |
|---|---:|---:|
| 100ms | −0.50s | $0.00 |
| 300ms | −0.70s | $0.00 |
| 1000ms | −1.40s | $0.00 |

Two honest findings, not one flattering one:

1. **The bridged reach-anchor lands *after* Kalshi's own reprice, not
   before it** — negative across the whole latency grid, meaning by our own
   crude wall-clock estimate we'd have been acting *later* than Kalshi's own
   reaction on this word, not earlier as hoped. But every one of these
   deltas (0.5–1.4s) sits **inside the bridge's own ±2s error bound** — this
   is not evidence PM was unreachable-first, it's evidence the bridge is too
   crude to say either way on n=1. The real fix (a wall-clock-paired
   timestamp emitted from the production arm path itself) is out of scope
   here — see the note in `extract_decisions.py`'s docstring.
2. **Even reanchored, the $0.00 result persists for this specific word.**
   Whatever the true anchor ordering, PM's book for "ai slop" had nothing
   resting below 90c across the tested window. This one word does not
   rescue the execution-venue thesis on its own.

**`book_shape_survey.py`'s broader, non-anchor-dependent finding is the
more informative one at this sample size.** Pooled across all 32 matched+
unmatched instruments in both historical captures, PM's book is **not
uniformly wide** the way the single anchored word suggested:

- 10 of 32 instruments show **100% of snapshots** with real size resting
  below 90c (e.g. AMD "Meta", "TSMC", "Oracle", "Frontier",
  "Microsoft"/"Azure" — several with mean whole-session p50 in the tens of
  dollars).
- A middle group (PLTR "China"-adjacent AMD word, "Insurance", "Apollo",
  "Leverage", "International", "Anthropic"/"Claude") sits between 14–52%
  nonzero.
- The rest, including the one anchored word ("Slop"/"AI Slop"), are
  structurally thin or fully closed (0% nonzero).

So "PM's ask sits near $0.99 as a matter of course" (`docs/EXPANSION.md`'s
verdict) is true for *some* instruments, not the class as a whole — worth
re-examining once real anchors exist for the currently-100%-nonzero words.
`structural_vs_transient` further shows the deep instruments stay deep
whether or not their mid recently moved (no "quiet" vs "just moved" gap),
i.e. it's a standing feature of those specific books, not a post-reprice
artifact.

## Open questions (need-a-lookup, not assumed)

- **PM taker fee, Polygon gas per order** — no PM execution code exists
  anywhere in this repo. `ev_model.py` uses a `0.0` placeholder, explicitly
  flagged, not a measurement. Needs a small, separately-scoped lookup
  against Polymarket's own docs before the EV grid can be trusted.
- **Detector latency for the EV model's own use** — two disagreeing numbers
  on disk (`docs/BAKEOFF.md`'s onset→emit p95 ~1000-1300ms vs. the armed
  PLTR run's own capture→decision p50 123ms/p90 188ms). Reconcile which span
  applies before using either downstream.
- **Same-event detector precision** — the 75% figure used above is
  transferred from a different event (RDDT 26JUL30), not measured on
  PLTR/AMD themselves.

## Next: more anchored words

The reanchored analysis is only computable on the intersection of (armed by
production) ∩ (captured by this exploration) ∩ (word actually said) — a
narrow slice that was accidental for PLTR (production happened to arm it,
this exploration didn't exist yet, but the artifacts survived on disk). See
`../call-times.json` and `../maps/` for the forward-looking multi-event
capture pipeline that grows this intersection deliberately going forward.
