# Layer: executor

## Role
Turns `DecisionIntent`s into IOC marketable-limit orders on Kalshi within
single-digit milliseconds of software time (spec §5.7): local book read →
LOST_RACE check → EV gate → risk gate → pre-staged body + pre-signed headers
→ HTTPS POST → ack bookkeeping. NOT responsible for: deciding WHAT to buy
(decision layer) or maintaining the book (book-cache layer).

## Entry door
- **Type:** `types::DecisionIntent`.
- **Mechanism:** `tokio::sync::mpsc::Receiver<DecisionIntent>` (cap 64),
  consumed by `executor::run_executor(rx, Arc<ExecCtx>)`
  (`src/kalshi/executor.rs`). Runs until the sender (decision layer) drops.
- Intents are handled CONCURRENTLY (one task each — the Powell burst case);
  aggregation mutexes are held µs and never touch the audio thread.

## Exit door
- **Wire:** `POST {base}/portfolio/events/orders` with pre-serialized
  `Bytes` body and `KALSHI-ACCESS-*` headers
  (`rest::RestClient::create_order_presigned`). The client keeps a warm TCP/h2
  connection (Nagle off; one-shot pre-warm + a keep-warm task wired in
  `main.rs::arm`) so the first fire never pays a cold handshake — pinned in
  VERIFICATIONS.md.
- **Type:** `executor::OrderRecord` per intent (ticker, price, count, tier,
  `dry_run`, `lost_race`, refusal, status, order_id, full ts chain,
  `best_ask` book snapshot) aggregated into `ExecReport { orders,
  lost_races, recorder }`, returned when the channel closes.
- Every acked `order_id` is pushed into `kalshi::OrderRegistry` — the kill
  switch's cancel set.

## Obligations
- Read books ONLY via `BookCache::read` (wait-free); never a REST fetch on
  this path.
- LOST_RACE: no ask at or under the cap → journal + counter, no order. This
  counter is the edge-decay kill-criterion metric (spec §10).
- EV gate before risk gate: `ev_gate(cap, count, haircut, min_ev)` with
  `fee_cents` = ceil-to-cent 0.07 quadratic (conservative vs the observed
  centicent rounding — see VERIFICATIONS).
- `dry_run` short-circuits BEFORE the risk gate: shadow mode reports exactly
  what would have been sent.
- Debit the ledger (`RiskGate::record_send`) BEFORE the send — an ambiguous
  timeout still counts as spent. The debit is in-memory only; the durable disk
  write runs off this path (risk layer's background persister).
- Ambiguous send error → `order_by_client_id` status query; NEVER blind
  retry. `client_order_id` is a deterministic UUID from
  `rest::deterministic_client_order_id("{event}:{kw}:{tier}")` so a retry
  would be server-deduped anyway.
- Pre-staged bodies (`prestage_orders`) MUST mirror the decision layer's
  tranche arithmetic per policy; the inline-build fallback is a logged
  should-never-happen.
- Record edges `decision→exec`, `presign_age`, `exec→send`, `send→hdrs`,
  `hdrs→ack`, `send→ack`, `decision→ack`, `capture→ack` into its own
  `LatencyRecorder` (merged into the pipeline report at teardown). `send→hdrs`
  is network + matching-engine time (TTFB), `hdrs→ack` is body read + parse;
  `send→ack` is kept as the end-to-end aggregate (p95 of a sum ≠ sum of p95s).

## Failure policy
- Risk refusal / EV failure / LOST_RACE → recorded on the OrderRecord, no
  order, loud log. HTTP ≥300 → `KalshiError::Api` recorded. Nothing here may
  panic; every intent produces exactly one OrderRecord.

## How to replace or edit this layer
- Everything is `src/kalshi/executor.rs`; wiring in `main.rs::arm`.
- Contract: entry/exit types, ledger-before-send, no-blind-retry, registry
  registration, LOST_RACE semantics, tranche sync with `decision.rs`.
- Wire-format facts (body fields, ack shape) are pinned in VERIFICATIONS.md
  — update it with evidence before changing any of them.

## How to test in isolation
- `tests/exec_mock.rs`: wiremock REST server + hand-published `BookCache`
  books + tempdir `RiskGate`. Covers fire path (exactly one signed POST),
  LOST_RACE, dry-run, ambiguous-timeout-no-repost, kill-switch scope.
- Live: `earshot probe-order` (1¢/1-contract, non-mention, $1 ledger).
