# Node: `runtime.market_validation`

## Job

Prove that every configured live market and selected trading posture is valid
at arm time, or refuse the run.

## Owns

`LiveMarketValidator`, `ValidatedMarket`, live-category lookup, rules snapshots,
and the `m2_live_gate`, `mention_probe_gate`, `mention_fill_gate`, and
`assert_non_crossing` checks in `src/kalshi/validator.rs`.

## Does not own

It does not discover events, prepare keywords, maintain the order book, set
account-wide risk policy, assemble the runtime, or encode and send an order.

## Input contracts

The node receives a validated `EventConfig`, a read-capable `RestClient`, an
event-owned state directory, and an explicit posture/cap. Unknown live market
state is unsafe, not a default.

## Output contracts

Success returns `Vec<ValidatedMarket>` with ticker, active status, category, and
early-close capability, and writes an arm-time rules snapshot for every
keyword. Posture gates return `Ok(())` only for the requested envelope.

## Side effects and exclusive resources

It performs read-only Kalshi REST requests and atomically writes
`<state-dir>/rules/<ticker>.json`. The event run exclusively owns that rules
directory; this node never places an order.

## Failure contract

Missing/inactive markets, unsupported price-level structure, unavailable rules,
unknown best ask for a non-crossing proof, or a cap outside posture fail closed.
Partial validation must not be represented as a valid event.

## Parallel work boundary

Live validation rules stay in `src/kalshi/validator.rs` and
`tests/validator_mock.rs`. Event preparation, risk accounting, book semantics,
and transport behavior require their own node changes.

## Verification

Run `cargo test kalshi::validator` and
`cargo test --test validator_mock`. Exercise each posture and confirm the rules
snapshot is written only for a fully evaluated request.

## Migration status

The checks exist, but `src/main.rs::arm` currently selects and sequences them
with assembly logic, and success is split between a vector and loose JSON
files. The target is a versioned `MarketValidationReport` consumed by assembly
and retained with the run.

