# Layer: risk

## Role
The last gate before money moves and the emergency stop (spec §5.8). Hard
ceilings are code constants that config can only tighten. NOT responsible
for: EV/fee economics (executor) or market validation (validator).

## Entry door
- **Type/mechanism:** direct calls from the executor and CLI drills:
  - `RiskGate::check_order(ticker, price_cents, count, book_age_ns,
    audio_age_ns) -> Result<(), Refusal>` — check order, in order:
    killed? → armed? → allowlist → book stale → audio stale → order notional
    → event notional → probe ledger.
  - `RiskGate::record_send(price_cents, count)` — worst-case debit, called
    BEFORE every send.
  - `RiskGate::arm() / kill() / is_killed()`.
- Constructed once per session: `RiskGate::new(RiskConfig, state_dir)`
  (`src/risk.rs`); `RiskConfig` deserializes from the `[risk]` TOML block and
  is `clamped()` to the hard consts.

## Exit door
- **Type:** `Result<(), risk::Refusal>` verdicts (typed reasons, rendered
  into OrderRecords), plus side effects: `state/probe_ledger.json`
  (cumulative real-money spend, survives restarts) and the kill watcher.
- **Kill watcher:** `spawn_kill_watcher(gate, stop_file, on_kill)` — polls
  `state/STOP` every 200 ms + SIGINT; on trigger runs `on_kill` (cancel the
  session's orders) then flips the gate. Measured STOP→cancelled: 140 ms.

## Obligations
- Hard consts (`HARD_MAX_ORDER_NOTIONAL_CENTS`, `HARD_MAX_EVENT_…`,
  `HARD_DAILY_LOSS_…`, `PROBE_LEDGER_CAP_CENTS = 100` for M2) are NOT
  user-overridable upward at runtime (spec §11). Raising one is a code
  change with review, never a config change.
- Unknown staleness (`u64::MAX`) is stale — refuse (P3).
- Ledger is debited before sends and persisted synchronously; an ambiguous
  order outcome still counts as spent.
- The kill path must cancel ONLY the session's `OrderRegistry` ids — the
  account carries ~52 unrelated resting orders from other systems
  (VERIFICATIONS, operational note). Never widen to account-wide.

## Failure policy
- Ledger persist failure → loud error log (the in-memory ledger still
  enforces); state-dir creation failure → constructor error, nothing arms.

## How to replace or edit this layer
- Everything is `src/risk.rs`. Contract: check order shown above, hard-const
  non-overridability, ledger-before-send, session-scoped kill.
- M2→M4 evolution: the `PROBE_LEDGER_CAP` posture is expected to be replaced
  by real per-event notional budgets — that is a reviewed code change here
  plus an update to this doc and the executor doc.

## How to test in isolation
- Pure unit tests with tempdir state (see `risk.rs` tests: clamping,
  refusal ordering, ledger cap + persistence across gates).
- Kill watcher: `tests/exec_mock.rs::kill_switch_stop_file_cancels_and_disarms`
  (tokio test, no network). Live: `earshot kill-drill --auto`.
