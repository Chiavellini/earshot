# Node: `runtime.order_transport`

## Job

Convert one already-approved, prestaged order into an evidence-preserving wire
attempt. Preserve the distinction between not attempted, attempted without an
exchange observation, observed rejection, observed acceptance, terminal order,
and fill evidence.

## Owns

The `OrderTransport`, `StageWire`, `WireStaged`, `SubmitResult`, `SendAttempt`,
`TransportObservation`, `TransportFailure`, and `FillEvidence` seam in
`src/kalshi/transport.rs`, plus the REST client/wire behavior in
`src/kalshi/rest.rs`. The older `SendOutcome`/`TransportAck` types are a
temporary compatibility facade only.

## Does not own

It does not detect or decide, cross the book, approve risk, choose price/size,
authenticate policy, or implement FIX framing/session internals. FIX is the
separate `runtime.kalshi.fix` node under `src/kalshi/fix/`.

## Input contracts

`submit_order` accepts a `PreStaged` whose `WireStaged` variant matches the
armed transport. REST requires fresh presigned headers; FIX receives a
configured live session from the FIX node. Client order ID is the idempotency
and reconciliation key.

## Output contracts

The outer result is one of:

- `NotAttempted { error }`: validation or session state refused the order
  before any socket-write boundary was entered. No `send_ts` exists.
- `Attempted(SendAttempt { send_ts, presign_age_ns, outcome })`: the transport
  entered the send boundary. From this point a failure is ambiguous and must
  be reconciled by client order ID.

An attempted outcome is either a `TransportObservation` or a
`TransportFailure` with its latest partial observation. An observation records
the protocol and protocol status, acceptance and terminal state independently,
order ID/status, exact parsed payload, exact REST response bytes, parse error,
first observation time, full acknowledgement time when one exists, and exact
fixed-point fill fields. REST prices retain dollar-string units; FIX prices
retain cent-string units. A rejection is evidence, not a transport error.

## Side effects and exclusive resources

It sends HTTPS or delegates to the exclusive FIX session and may consume a
transport-specific sequence. A live transport instance belongs to one arm;
credentials and sockets must not be shared accidentally across event accounts.

## Failure contract

Mismatched prestaging is `NotAttempted`. Any failure after `send_ts` remains an
`Attempted` result even when no response arrives. Response headers survive a
body-read failure; malformed JSON survives as exact bytes plus a parse error;
non-2xx HTTP and FIX rejection ExecutionReports survive as observations.
Ambiguous outcomes must be reconciled by client order ID and never blindly
retried.

`RestClient::order_by_client_id_evidence` preserves the exact query response
bytes, status, parse failure, timestamp, and only an exactly echoed matching
row. Assembly uses this door for restart recovery; one missing row is an
observation, never proof of absence.

`send_order` remains temporarily for non-executor compatibility callers. It
deliberately collapses rich failure evidence and must not be used by new code;
the production executor uses `submit_order`.

## Parallel work boundary

Wire-neutral and REST changes stay in `src/kalshi/{rest.rs,transport.rs}`.
FIX internals, executor policy, authentication, and risk are separate nodes;
the rich result is additive so the separately claimed executor can migrate
without coupling transport internals into execution policy.

## Verification

Run `cargo test --lib kalshi::rest`, `cargo test --lib kalshi::transport`,
`cargo test --test exec_mock`, and `cargo test --test exec_fix_mock`; run the
FIX node's own session tests when its delegated behavior changes. REST tests
pin HTTP rejection, malformed success bytes, timeout after `send_ts`, and
truncated bodies. Seam tests pin exact REST/FIX fill strings and incomplete
acknowledgements.

REST body reads are chunked: when `Content-Length` is truncated or a later
body read fails, the observation retains the exact byte prefix already
received instead of replacing it with an empty body. Fixed-point fill status
derivation examines every digit (`0.50` is non-zero) and never truncates
through an integer or float. `ack_ts` is sampled only after complete body read,
JSON parse, and field extraction; header/TTFB remains `hdrs_ts`.

## Migration status

The evidence-preserving REST and FIX doors exist and the executor records the
rich attempt/observation/fill state in every `OrderRecord`. Prestaging is still
constructed inside `src/kalshi/executor.rs`, and live FIX access remains
unverified as recorded in `VERIFICATIONS.md`; a later `StagedOrder` contract
may remove the executor/transport construction coupling.
