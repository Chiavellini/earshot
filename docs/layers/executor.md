# Layer: executor

## Role
Turns `DecisionIntent`s into IOC marketable-limit orders on Kalshi within
single-digit milliseconds of software time (spec §5.7): local book read →
LOST_RACE check → EV gate → risk gate → pre-staged wire form →
`OrderTransport::send_order` → ack bookkeeping. NOT responsible for:
deciding WHAT to buy (decision layer), maintaining the book (book-cache
layer), or the wire itself (transport seam — see
`docs/layers/fix-transport.md` for the FIX arm).

## Entry door
- **Type:** `types::DecisionIntent`.
- **Mechanism:** `tokio::sync::mpsc::Receiver<DecisionIntent>` (cap 64),
  consumed by `executor::run_executor(rx, Arc<ExecCtx>)`
  (`src/kalshi/executor.rs`). Runs until the sender (decision layer) drops.
- `run_executor` runs on its OWN OS thread + current-thread runtime (wired in
  `main.rs::arm` via `spawn_blocking`): the detect thread's `try_send` wakes
  that parked thread directly instead of queueing behind the shared 2-worker
  runtime's other work (WS/presign/warmers). On a tuned box the thread is
  pinned/SCHED_FIFO per `[perf] core_executor` (docs/OS-TUNING.md).
- Intents are handled CONCURRENTLY (the Powell burst case) via a
  `FuturesUnordered` polled inside `run_executor` itself — sends overlap on
  the wire with NO per-intent task spawn (no alloc, no scheduler enqueue).
  `tokio::select!` is `biased` toward a fresh intent over harvesting a
  completed send: firing beats bookkeeping. Intents carry `handoff_ts`
  (stamped by the detect thread just before `try_send`) so the wake hop is
  measured as `handoff→exec`, separate from `decision→exec` (the aggregate).

## Exit door
- **Wire:** `transport::OrderTransport::send_order(&PreStaged)`
  (`src/kalshi/transport.rs`) — an enum over the two order wires, selected by
  config `[kalshi] transport` (default `rest`) and wired in `main.rs::arm`:
  - `Rest`: `POST {base}/portfolio/events/orders` with pre-serialized `Bytes`
    body, presigned `KALSHI-ACCESS-*` headers, and the orders URL pre-parsed
    at client construction (no per-send format!/URL-parse)
    (`rest::RestClient::create_order_presigned`; the `Presigner` lives INSIDE
    this arm). Warm TCP/h2 connection (Nagle off; one-shot pre-warm + a
    keep-warm task wired in `main.rs::arm`) — pinned in VERIFICATIONS.md.
  - `Fix`: prestaged 35=D template patched with seqnum/SendingTime and put on
    the persistent FIX session (`fix::transport::FixTransport`); heartbeats
    are its native connection warmer. Offline-built; live validation pending
    (VERIFICATIONS §Q-FIX).
  Each transport prestages its own wire form (`WireStaged::Rest{body}` /
  `WireStaged::Fix{template}`) via `StageWire`; the executor is wire-blind.
  Returns `SendOutcome { ack: TransportAck, send_ts, presign_age_ns }`
  (`presign_age_ns: None` on FIX — no presign step exists there).
- **Type:** `executor::OrderRecord` per intent (ticker, price, count, tier,
  `dry_run`, `lost_race`, refusal, status, order_id, full ts chain,
  `best_ask` book snapshot + `book_age_ns` snapshot age) aggregated into
  `ExecReport { orders, lost_races, observe_noncross, recorder }`, returned
  when the channel closes.
- Every acked `order_id` is pushed into `kalshi::OrderRegistry` — the kill
  switch's cancel set.

## Obligations
- Read books ONLY via `BookCache::read` (wait-free); never a REST fetch on
  this path.
- LOST_RACE: no ask at or under the cap → journal + counter, no order. This
  counter is the edge-decay kill-criterion metric (spec §10) — and it counts
  ONLY fillable-tier caps (> `OBSERVE_PRICE_CENTS` = 1¢). A 1¢ observe-tier
  intent crossing nothing is the DESIGNED outcome (prepare parks unvalidated
  words at 1¢ precisely so they never send); those land in the separate
  `observe_noncross` counter and log at INFO, not WARN. The per-record
  `lost_race` flag still marks the non-crossing branch on both. (The MS
  2026-07-15 headline "4 LOST_RACE" was 4 observes — a designed outcome
  misread as a latency failure.)
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
- Record edges `decision→exec`, `handoff→exec`, `book_age`, `presign_age`,
  `exec→send`, `send→hdrs`, `hdrs→ack`, `send→ack`, `decision→ack`,
  `capture→ack` into its own `LatencyRecorder` (merged into the pipeline
  report at teardown). `send→hdrs` is network + matching-engine time (REST:
  TTFB; FIX: first ExecutionReport received), `hdrs→ack` is body read +
  parse; `send→ack` is kept as the end-to-end aggregate (p95 of a sum ≠ sum
  of p95s). `presign_age` is recorded ONLY when the transport reports one
  (REST arm) — a fake zero on FIX would poison the stats. `book_age` =
  exec_ts − the book snapshot's `as_of` (skipped while the book is still the
  seq-0 placeholder); it distinguishes "our book was stale" from "a
  competitor was genuinely faster" — the MS RECE loss was undiagnosable
  without it.
- **No telemetry lock before the wire**: the pre-send edge values
  (`decision→exec`, `handoff→exec`, `book_age`, `presign_age`) are measured
  at the right instants but recorded via `PendingEdges` only AFTER the send
  (or at a non-fire exit). Between intent arrival and the HTTP send the fire
  path takes no `recorder.lock()`.

## Failure policy
- Risk refusal / EV failure / LOST_RACE → recorded on the OrderRecord, no
  order, loud log. HTTP ≥300 → `KalshiError::Api` recorded. Nothing here may
  panic; every intent produces exactly one OrderRecord.

## How to replace or edit this layer
- Everything is `src/kalshi/executor.rs` (+ the wire seam in
  `src/kalshi/transport.rs`); wiring in `main.rs::arm`.
- Contract: entry/exit types, ledger-before-send, no-blind-retry, registry
  registration, LOST_RACE semantics, tranche sync with `decision.rs`.
- Wire-format facts (body fields, ack shape) are pinned in VERIFICATIONS.md
  — update it with evidence before changing any of them.

## How to test in isolation
- `tests/exec_mock.rs`: wiremock REST server + hand-published `BookCache`
  books + tempdir `RiskGate`. Covers fire path (exactly one signed POST),
  LOST_RACE, dry-run, ambiguous-timeout-no-repost, kill-switch scope.
- `tests/exec_fix_mock.rs`: the same executor through `OrderTransport::Fix`
  against an in-process mock FIX server — fill mapping, gates upstream of the
  wire, and the dual-wire ambiguous path (no ER → REST status query).
- Live: `earshot probe-order` (1¢/1-contract, non-mention, $1 ledger).
