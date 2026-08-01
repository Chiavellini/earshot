# Node: `runtime.order_transport`

## Job

Convert one already-approved, prestaged order into one wire send and a
wire-neutral acknowledgement.

## Owns

The `OrderTransport`, `StageWire`, `WireStaged`, `SendOutcome`, and
`TransportAck` seam in `src/kalshi/transport.rs`, the REST client/wire behavior
in `src/kalshi/rest.rs`, and shared Kalshi error/environment definitions in
`src/kalshi/mod.rs`.

## Does not own

It does not detect or decide, cross the book, approve risk, choose price/size,
authenticate policy, or implement FIX framing/session internals. FIX is the
separate `runtime.kalshi.fix` node under `src/kalshi/fix/`.

## Input contracts

`send_order` accepts a `PreStaged` whose `WireStaged` variant matches the armed
transport. REST requires fresh presigned headers; FIX receives a configured
live session from the FIX node. Client order ID is the idempotency key.

## Output contracts

Success returns `SendOutcome { ack, send_ts, presign_age_ns }`; `TransportAck`
normalizes order ID, status, raw forensic payload, headers/first-byte time, and
fully parsed acknowledgement time across wires.

## Side effects and exclusive resources

It sends HTTPS or delegates to the exclusive FIX session and may consume a
transport-specific sequence. A live transport instance belongs to one arm;
credentials and sockets must not be shared accidentally across event accounts.

## Failure contract

Mismatched prestaging is a validation error. Network, protocol, and rejected
order failures are returned as `KalshiError`; ambiguous outcomes must be
reconciled by client order ID and never blindly retried.

## Parallel work boundary

Wire-neutral and REST changes stay in `src/kalshi/{mod.rs,rest.rs,transport.rs}`.
FIX internals, executor policy, authentication, and risk are separate nodes;
changing `SendOutcome` requires the executor contract.

## Verification

Run `cargo test --test exec_mock` for REST,
`cargo test --test exec_fix_mock` for the seam, and the FIX node's own session
tests when its delegated behavior changes.

## Migration status

The transport enum and normalized acknowledgement exist. Prestaging is still
constructed inside `src/kalshi/executor.rs`, and live FIX access remains
unverified as recorded in `VERIFICATIONS.md`; a future `StagedOrder` contract
should remove the executor/transport construction coupling.

