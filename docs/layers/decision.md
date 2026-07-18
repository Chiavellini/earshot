# Layer: decision

## Role
Maps acoustic `Detection`s to sized order intents via a precompiled table and
a one-shot per-keyword state machine (spec §5.6). Owns policy (fast_only /
confirmed_only / two_tier) and tranche arithmetic. NOT responsible for:
book reads, EV/fee math, risk checks, or anything network — those live in
the executor.

## Entry door
- **Type:** `&types::Detection`.
- **Mechanism:** `DecisionTable::decide(&mut self, &Detection) ->
  Option<DecisionIntent>` (`src/decision.rs`), called inline on the `detect`
  OS thread right after `Detector::process`.

## Exit door
- **Type:** `types::DecisionIntent` — `keyword`, `market_ticker`,
  `max_price_cents`, `tranche_contracts`, `tier`, `audio_ts`,
  `decision_ts: MonotonicNs` (stamped here), `handoff_ts: MonotonicNs`
  (stamped 0 here; the detect loop stamps the real value on the sent clone
  immediately before `try_send` — it feeds the `handoff→exec` edge).
- **Mechanism:** returned to the detect loop, which — FIRE FIRST — if an
  executor is attached, `try_send`s a clone into
  `tokio::sync::mpsc::Sender<DecisionIntent>` (cap 64), and only THEN records
  edges and logs (nothing but the trade on the critical span). Full/closed
  channel = counted LOUD drop (`intent channel full/closed: intent DROPPED`,
  logged after the handoff attempt) — the detect thread never blocks on the
  executor.

## Obligations
- O(1) lookup, no allocation on the miss path; the table is compiled once
  from `config::KeywordConfig` at arm time (`DecisionTable::from_keywords`).
- Confidence gate BEFORE any state transition: `det.confidence <
  min_confidence_fast/_confirmed` (per tier, from config; default 0.0 =
  pass everything) returns `None` without consuming the one-shot machine.
  Engines without real scores (sherpa_kws) emit confidence 1.0, so the
  gates compose but default off.
- State machine per keyword: `Armed → FastFired → Done` (two_tier) or
  `Armed → Done` (single-tier). Once `Done`, further detections return
  `None` forever (one-shot; repeats are logged and ignored).
- Tranche arithmetic: Fast releases `FAST_TRANCHE_PCT` (30%) of budget;
  Confirmed releases the remainder — or the full budget if Fast never fired.
  **The executor's `prestage_orders` mirrors this arithmetic exactly; change
  them together** (`src/kalshi/executor.rs::prestage_orders`).
- Stamp `decision_ts` inside `decide` (it feeds the `detect→decision` edge).

## Failure policy
- Unknown `KeywordId` → `None` (log upstream); cannot panic on the hot path.

## How to replace or edit this layer
- Everything is `src/decision.rs`. Contract: `decide`'s signature,
  `DecisionIntent` semantics, one-shot guarantee, tranche sync with
  `prestage_orders`.
- Free: richer policies, per-tier budgets — extend `Policy` in config,
  mirror in prestage, update both docs.

## How to test in isolation
- Pure unit tests, no I/O: build a `DecisionTable` from literal
  `KeywordConfig`s and feed `Detection`s (see tests in `decision.rs` for
  two_tier / confirmed_only / one-shot coverage).
