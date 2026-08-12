# Node: `runtime.order_evidence`

## Job

Durably journal the complete outcome of every evaluated live execution intent
so a process crash cannot erase either a no-send decision or whether transport
was requested, prepared, invoked, observed by the exchange, or reconciled
later.

This is a reliability boundary, not a latency optimization. A session report
is a derived presentation; this journal is the crash-safe source of truth from
which reports and detector-to-execution measurements must eventually be built.

## Owns

`src/kalshi/order_evidence.rs`, its versioned JSONL schema, its ordered writer,
its strict state reducer/replay, and `tests/order_evidence.rs`.

It owns evidence persistence only. It never chooses, sizes, prices, authorizes,
sends, retries, reconciles, or cancels an order.

## Does not own

It does not choose, size, price, authorize, send, retry, reconcile, or cancel
orders. Those remain decision, executor, risk, transport, and assembly jobs.
It does not own RunPlan construction, account selection, transport endpoint
selection, or report presentation.

## Input contracts

The event RunPlan supplies one immutable journal identity:

- run ID and event ID;
- resolved Kalshi environment;
- a stable account fingerprint, distinct from a rotating key/credential ID;
- the RunPlan's immutable `plan_sha256`.

Endpoint identity is intentionally not journal-wide. Every wire attempt,
exchange observation, and reconciliation records its own protocol and endpoint
so a FIX attempt can be recovered through an exact-client-ID REST query without
creating false identity drift.

The executor/transport submit typed `OrderEvidenceCommand` values through
`OrderEvidenceSink`. The first command for a client ID is always
`IntentEvaluated`, containing the complete immutable intent and evaluation:

- client order ID, ticker, side, action, price, count, and time in force;
- tier, keyword ID/text, and source ID;
- audio, decision, executor, and book-as-of monotonic timestamps plus the
  observed best ask;
- canonical request SHA-256 and decision-plan SHA-1.

`book_as_of_ts = 0` with no best ask means there was no usable snapshot. A
nonzero book timestamp with no best ask means the snapshot was fresh but the
book was empty. A recorded best ask always requires a nonzero book timestamp.
This distinction keeps stale/missing-book and real empty-book no-send outcomes
measurable rather than dropping them before evidence begins.

That first receipt is durably acknowledged for every evaluated intent,
including lost-race, stale-book, risk-refusal, and killed-session outcomes.
`SendRequested { client_order_id, market_ticker, requested_ts }` is a later,
optional transition and must satisfy `requested_ts >= IntentEvaluated.exec_ts`.
Neither receipt proves a socket write or increments a sent-order count. If
replay finds no later `InvocationPrepared`, startup durably closes the intent
as `NotAttempted` without querying the exchange.

`InvocationPrepared` occurs only after `SendRequested`, durably records the
protocol, endpoint, preparation time, and exact assembled outbound bytes, and
requires `prepared_ts >= requested_ts`. Once it exists, a crash is
delivery-ambiguous: invocation could occur before the later receipt syncs.
`WireAttempted` is the distinct actual invocation receipt and records
`send_ts`; it no longer owns raw bytes. `ExchangeObserved` is emitted
once for every REST response or FIX ExecutionReport, including New and every
partial or Trade report. `TransportFailed` records an error after invocation
but remains delivery-ambiguous. Reconciliation emits either `Reconciled`, with
both queried and echoed client IDs exactly matching, or `ReconcileUnresolved`,
which is one observation at one instant and never proof of absence.

Fill evidence contains only exact fields the exchange actually supplied. Its
cumulative/remaining quantity, average price, latest-fill quantity, and
latest-fill price are independent optional fixed-point strings; latest-fill
exchange timestamp is independently optional. At least one exact field must
exist. Missing cumulative quantity is never fabricated as zero.

## Output contracts

The concrete door is `OrderEvidenceSink::record`: an async-friendly producer
that queues one typed command to a dedicated ordered writer and awaits its
durable acknowledgement. The caller performs no file write or fsync on its
Tokio task. `record_blocking` exists only for non-Tokio compatibility callers.

The output is one exclusively locked append-only JSONL file of schema-v4
`OrderEvidenceEvent` rows. Every row carries:

- contiguous journal and per-order sequence numbers;
- UTC and monotonic persistence times and an honestly named
  `process_instance_id`;
- the immutable RunPlan/event/environment/account identity;
- immutable client-order/ticker identity and one typed transition;
- `previous_process_instance_id`, `previous_event_sha256`, and a canonical
  `event_sha256` covering the entire event, with the first row anchored to
  `plan_sha256` and an empty previous-process value;
- exact raw evidence as one all-or-none `RawEvidence` object containing media
  type, base64 bytes, length, and SHA-256.

`OrderEvidenceJournal::snapshot` retains the full replay, not only a summary,
and is updated only after the corresponding row has synced. Its
`unresolved_recoveries` are concrete `OrderRecoveryState` objects. Each exposes
one of three required recovery actions:

- `ConcludeNotAttempted` when `IntentEvaluated` is durable but no
  `InvocationPrepared` exists, whether or not `SendRequested` exists;
- `ReconcileExactClientId` when a prepared invocation or wire attempt is
  durable, delivery remains ambiguous, and no exchange order ID is verified;
- `ObserveOrCancelKnownOrder` when a verified nonterminal order ID exists.

`NotAttempted` is terminal because it proves transport was refused before its
invocation boundary. Terminal rejection, fill, cancel, or expiry is also
terminal. No transition is legal after terminal state.

## State-machine invariants

One reducer validates both live appends and startup replay. It enforces all of
the following before state advances:

1. `IntentEvaluated` is first and occurs once per client ID. It preserves both
   send and no-send outcomes, including the missing/fresh-empty/priced book
   distinction.
2. Client ID and ticker never mutate.
3. `SendRequested` is optional, occurs at most once immediately after
   evaluation, and has `requested_ts >= exec_ts`.
4. `InvocationPrepared` occurs at most once immediately after the send
   request, has `prepared_ts >= requested_ts`, and preserves exact outbound
   bytes.
5. `NotAttempted` occurs before `WireAttempted` and is terminal. Legal no-send
   paths are `IntentEvaluated → NotAttempted`, `IntentEvaluated →
   SendRequested → NotAttempted`, and `IntentEvaluated → SendRequested →
   InvocationPrepared → NotAttempted`. A live caller
   may use it after preparation only when transport explicitly proves refusal;
   crash recovery treats a prepared invocation as ambiguous.
6. `WireAttempted` occurs at most once after matching preparation, has
   `send_ts >= prepared_ts`, and precedes exchange observations.
7. Exchange observations match the wire attempt's protocol and endpoint.
8. Any exchange order ID is accepted only with an exact echoed client ID and
   can never change later.
9. Reconciliation requires a prepared invocation. Verified reconciliation
   requires both queried and echoed client IDs to equal the journal's client
   ID; reconciliation endpoints may differ from the send endpoint. `order_id`
   is optional only for a terminal exchange rejection, because rejected orders
   correctly have no cancellable ID.
10. Rejections are terminal; terminal non-rejections require a verified order
   ID; terminal state accepts no successor.
11. Every raw evidence object decodes canonically and matches both its recorded
   length and SHA-256.
12. Journal/per-order sequences, immutable identity, previous hash, and event
    hash all match exactly.

## Side effects and exclusive resources

The canonical RunPlan owner must pre-create the run directory. The evidence
node refuses a missing parent and never calls `create_dir_all`, so it cannot
silently invent a second state root. Opening takes a nonblocking exclusive
file lock and syncs the file and existing parent directory. Each accepted
transition uses one append write followed by `fsync`/macOS `F_FULLFSYNC`; only
then does the writer acknowledge and publish the new snapshot.

The journal lease must remain alive for the session. A second writer is
refused. Clean close drains the already-acknowledged lifecycle and releases the
lock; every acknowledgement was already durable, so shutdown is not the first
time evidence reaches disk.

## Failure contract

Startup refuses a missing canonical parent, lock contention, a truncated tail,
blank line, invalid/unknown JSON field, schema or sequence gap, identity drift,
illegal transition, raw-evidence mismatch, or broken event-hash chain.

At runtime, *any* illegal append, serialization/write/sync error, lost writer,
or missing acknowledgement irreversibly poisons `OrderEvidenceHealth`. The
failed command receives an explicit error and every later command is refused;
there is no in-memory fallback. Assembly must treat this health fault as a
session kill and continuously cancel/reconcile all session orders.

An append or sync failure is itself ambiguous: bytes may be present even when
the caller receives an error. Restart therefore replays the file strictly and
never retries a client ID blindly.

## Parallel work boundary

This node exposes persistence and recovery objects. Executor and FIX nodes own
the placement of each lifecycle callback; risk owns the kill-safe send permit;
runtime assembly owns canonical path/identity construction, health containment,
and recovery before activation. None may reach into this writer's file internals.

## Verification

Run:

```sh
cargo test --lib kalshi::order_evidence
cargo test --test order_evidence
cargo check --lib
scripts/doc-lint.sh
```

Focused tests cover shared live/replay reduction, complete evaluation
retention, every legal no-send path, missing versus fresh-empty book evidence,
transition timestamp ordering, terminal `NotAttempted`, illegal transitions
and health poisoning, exact-ID
reconciliation, FIX-to-REST recovery, unresolved restarts, raw fingerprints,
blank/truncated/corrupt streams, hash-chain and identity drift, exclusive
locking, concurrent total ordering, and the async acknowledgement door.

Nearest integration tests must additionally prove that journal refusal causes
no transport invocation; every FIX New/partial/Trade report is durably recorded
and registers its order ID immediately; a risk kill cannot linearize before a
later send; and startup recovery completes before live activation.

## Migration status

The typed journal, writer, replay, reducer, health signal, and recovery model
are implemented, but the contract remains **compatibility** until executor,
REST/FIX observation callbacks, RunPlan identity/path construction, startup
recovery, fault containment, and report derivation are wired and exercised at
their integration seams. Until then, `ExecReport` remains graceful-teardown
evidence and cannot substitute for this ledger after a crash.
