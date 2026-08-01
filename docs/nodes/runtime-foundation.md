# Node: `runtime.foundation`

## Job

Provide the stable in-process primitives on which every runtime node exchanges
data, time, and bounded work.

## Owns

Core value types in `src/types.rs`, monotonic time in `src/clock.rs`, bounded
ring semantics in `src/ring.rs`, and atomic file helpers in `src/fsio.rs`.
It consumes the separate `runtime.telemetry` contract where instrumentation is
needed.

## Does not own

It does not choose a media source, assemble threads, detect keywords, decide
orders, validate markets, or send network requests.

## Input contracts

Primitive constructors receive already-validated values. Queue users provide
typed payloads and explicitly choose ingress drop semantics versus interior
backpressure; timestamps use `MonotonicNs`, never wall-clock time.

## Output contracts

The stable runtime vocabulary is `AudioFrame`, `AudioChunk`, `Detection`,
`DecisionIntent`, `SourceId`, `KeywordId`, and `MonotonicNs`, plus bounded
`RingSender<T>`/`RingReceiver<T>` doors. Field additions are cross-node
contract changes.

## Side effects and exclusive resources

Rings allocate bounded in-process buffers; clock reads the host monotonic
clock; `fsio` performs local atomic replacement. Foundation code owns no
network endpoint, credential, or long-lived process.

## Failure contract

Invalid queue capacity or I/O is returned or fails at construction. A full
ingress ring is observable to the caller; interior users must not silently
discard. Timestamp and sequence invariants must never be repaired by guessing.

## Parallel work boundary

This node may change only the shared primitives above. Any public field, queue
behavior, timestamp meaning, or serialization change requires affected node
contracts first; algorithm-specific helpers stay with their algorithm node.

## Verification

Run `cargo test ring::`, `cargo test clock::`, and `cargo test telemetry::`,
then `cargo test --test e2e_replay` and `cargo test --test perf_budget` for the
nearest shared-contract seams.

## Migration status

The primitives are implemented and already used as typed doors. Their
documentation is currently spread across `docs/ARCHITECTURE.md` and several
`docs/layers/*.md` files; telemetry is already tracked separately by
`docs/layers/telemetry.md`.
