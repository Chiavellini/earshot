# Layer: executor

## Role
Turns `DecisionIntent`s into IOC marketable-limit orders on Kalshi within
single-digit milliseconds of software time (spec §5.7): local book read →
LOST_RACE check → EV gate → risk gate → pre-staged wire form →
`OrderTransport::submit_order` → observation/reconciliation bookkeeping. NOT responsible for:
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
- **Wire:** `transport::OrderTransport::submit_order(&PreStaged)`
  (`src/kalshi/transport.rs`) — an enum over the two order wires, selected by
  config `[kalshi] transport` (default `rest`) and wired in `main.rs::arm`:
  - `Rest`: `POST {base}/portfolio/events/orders` with pre-serialized `Bytes`
    body and presigned `KALSHI-ACCESS-*` headers
    (`rest::RestClient::create_order_presigned`; the `Presigner` lives INSIDE
    this arm). Warm TCP/h2 connection (Nagle off; one-shot pre-warm + a
    keep-warm task wired in `main.rs::arm`) — pinned in VERIFICATIONS.md.
  - `Fix`: prestaged 35=D template patched with seqnum/SendingTime and put on
    the persistent FIX session (`fix::transport::FixTransport`); heartbeats
    are its native connection warmer. Offline-built; live validation pending
    (VERIFICATIONS §Q-FIX).
  Each transport prestages its own wire form (`WireStaged::Rest{body}` /
  `WireStaged::Fix{template}`) via `StageWire`; the executor is wire-blind.
  Returns `SubmitResult`: `NotAttempted` has no wire timestamp;
  `Attempted(SendAttempt)` always carries `send_ts` plus either a normalized
  protocol observation or a failure with its latest partial observation.
  `presign_age_ns` is `None` on FIX because no presign step exists there.
- **Type:** `executor::OrderRecord` per intent (ticker, price, count, tier,
  `dry_run`, `lost_race`, refusal, status, order_id, full ts chain,
  `wire_state`, protocol status, independent accepted/terminal flags, exact
  parsed response plus base64 exact REST bytes, transport/parse errors, exact
  fill strings, reconciliation receipt, `best_ask` book snapshot + exact
  `book_as_of_ts` + `book_age_ns` snapshot age + `ev_at_ask_cents`)
  aggregated into
  `ExecReport { orders, lost_races, observe_noncross, recorder }`, returned
  when the channel closes.
- **`orders` is a list of INTENTS, not of orders placed.** Every one of the
  five non-fire early returns (non-crossing, EV gate, dry-run, risk refusal,
  send-failed-and-proven-absent) pushes a record into it. The only honest
  answers are the predicates on `OrderRecord`:
  - `send_attempted()` ⇔ `send_ts.is_some()` — the transport invocation
    timestamp. It does **not** by itself prove socket delivery or exchange
    receipt. `reached_wire()` remains only as a compatibility alias with
    exactly these weaker semantics.
  - `acked()` ⇔ `order_id.is_some()` — strictly stronger; also true for an
    order recovered by the ambiguous-outcome status query, which is why it
    keys on the id rather than `ack_ts`.
  `telemetry::build_run_json` emits `intents`/`sent`/`acked` from these so no
  reader re-derives them. Counting `orders.len()` reported "23 orders placed"
  on 2026-08-05 when the true number was 0 — every record had short-circuited
  at the non-crossing branch. `scripts/missed-arm-check.py` and
  `scripts/run-verdict.sh` count `sent`, falling back to `send_ts != null` for
  reports written before that key existed.
- Before transport invocation, the deterministic `client_order_id` enters
  `kalshi::OrderRegistry` through `begin_attempt`. This is the linearization
  boundary with kill: kill closes future attempts and can exact-id reconcile
  every earlier ambiguous attempt even before an exchange `order_id` arrives.
  Verified order ids are attached to that client id and form the cancel set;
  terminal rejection/pre-write failure resolves it without widening scope.
  Duplicate client-id attempts in one process are refused.
- On a RunPlan-backed live arm, the executor first durably appends
  `IntentEvaluated` with the complete immutable request/evaluation context:
  request SHA, decision-plan SHA-1, keyword/source identity, all
  audio/decision/exec/book timestamps, and the exact book state. This occurs
  before every send and no-send branch, so lost-race, EV, dry-run, risk, and
  kill refusals remain measurable. A later `SendRequested` records only that
  execution asked transport to proceed; it is not wire evidence.
- REST and FIX then durably append `InvocationPrepared` with the exact
  assembled outbound bytes before invoking their transport boundary. The
  subsequent `WireAttempted` records the actual transport invocation and its
  `send_ts`; it does not duplicate or invent raw bytes. Each exchange
  observation/fill, transport failure, reconciliation result, and no-send
  conclusion is durably acknowledged independently. Evidence refusal prevents
  a new wire attempt and poisons health so assembly kills the session.
- `wire_state` is stable evidence vocabulary: `not_attempted`,
  `attempted_unobserved`, `observed`, or `reconciled`. A response rejection is
  `observed` with `exchange_accepted=false`; it is not rewritten as “send
  failed.” A body-read failure retains response headers/status, exact bytes
  received, and its parse/read error. A reconciled row retains the original
  attempt/partial response and banks the later query response separately.
- `FillEvidence` preserves exchange fixed-point strings verbatim. REST price
  fields have `price_unit=dollars`; FIX price fields have `price_unit=cents`.
  Filled quantity/status is therefore auditable without inferring from a
  generic acknowledgement flag.

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
- **`ev_gate` prices at the CAP and assumes the detection is correct.** Both
  are known gaps, recorded rather than patched:
  - It never sees the observed ask, so a 34¢ opportunity is scored as a 97¢
    one (2¢ instead of 64¢). `OrderRecord::ev_at_ask_cents` records the
    ask-priced value beside it so the divergence is visible per order.
  - It has no P(wrong) term. That is harmless at a 1¢ observe cap and is the
    whole question at 97¢. The fix is NOT a per-keyword precision prior:
    replayed against every banked event that policy skips the winning 97¢
    trades and buys the losing cheap ones (+25.0¢ → −4.0¢), because measured
    P(YES | we fired) is a function of the **ask** — 29/29 at 95–100¢ versus
    5/19 at 20–59¢ (`scripts/settle-score.py --bands`). Any correctness term
    must condition on price, and at n=64 no band clears its own confidence
    interval, so nothing gates on this yet.
- `dry_run` short-circuits BEFORE the risk gate: shadow mode reports exactly
  what would have been sent.
- Debit the ledger (`RiskGate::record_send`) BEFORE the send — an ambiguous
  timeout still counts as spent. The debit is in-memory only; the durable disk
  write runs off this path (risk layer's background persister).
- The full risk policy check precedes `OrderRegistry::begin_attempt`; after
  that atomic send/kill boundary, the executor rechecks kill before debiting.
  A permit that wins just before kill is classified as pre-kill in-flight,
  remains visible through `active_attempts`, and must be reconciled/cancelled;
  no new permit can start after kill closes the registry.
- Any attempted outcome without a conclusive rejection or order id →
  `order_by_client_id` status query; NEVER blind retry. `client_order_id` is a
  deterministic UUID from `deterministic_order_client_id(run_identity,
  ticker, price_cents, count, keyword_index, tier)`, whose stable seed contains
  that full logical request. A retry is therefore server-deduped without
  letting another immutable run or differently shaped tranche adopt the id.
- Pre-staged bodies (`prestage_orders`) MUST mirror the decision layer's
  tranche arithmetic per policy. A missing arm-time artifact is a terminal
  `not_attempted` refusal. Live inline construction is forbidden: it would
  evade the reviewed body, deterministic id, and evidence plan.
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
  order, loud log. HTTP ≥300 and FIX rejection are protocol observations with
  `exchange_accepted=false`; transport failures after `send_ts` remain
  attempted and trigger reconciliation. Nothing here may panic; every intent
  produces exactly one OrderRecord.

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
  exact fill/HTTP evidence, LOST_RACE, dry-run,
  ambiguous-timeout-no-repost/reconciliation, kill-switch scope.
- `tests/exec_fix_mock.rs`: the same executor through `OrderTransport::Fix`
  against an in-process mock FIX server — exact ExecutionReport/fill mapping,
  gates upstream of the wire, and the dual-wire ambiguous path (no ER → REST
  status query while retaining the FIX attempt).
- Wire truth — `cargo test --lib executor`:
  `no_early_return_counts_as_having_reached_the_wire` walks all five non-fire
  exits and asserts none is `reached_wire()`/`acked()`;
  `sent_and_acked_are_distinct_and_both_reachable` covers the success path and
  the recovered-after-ambiguous-send case;
  `run_json_counts_intents_sent_and_acked_separately` pins the report counters.
- Live: `earshot probe-order` (1¢/1-contract, non-mention, $1 ledger).

## Fill-path verifier hold (D2, 2026-08-18)

`ExecCtx.verifier: Option<VerifierClient>` (`docs/nodes/verifier.md`). In
`handle_intent`, BEFORE the book read: for a Confirmed intent whose staged
price is above `OBSERVE_PRICE_CENTS` and whose ticker is opted in under
`[verifier].mode = "gate"`, the executor `await_verdict`s until
`decision_ts + budget_ms`; `Rejected` ⇒ `refusal = "verifier_veto"` (durable
`not_attempted`, nothing sent); `Timeout` ⇒ proceeds (`on_timeout = send`) or
`refusal = "verifier_timeout"` (`hold`); `Error`/`Dropped`/`NotRequested` ⇒
proceeds. In `shadow` mode nothing waits; an already-available verdict is
recorded. 1¢ observe intents never wait in any mode. `OrderRecord` gains
`verify_verdict` / `verify_wait_ns` / `verify_gated` (omitted when no verifier).
Tests: `tests/exec_mock.rs::verifier_gate::*`.
