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
  - `RiskGate::record_send(price_cents, count)` — worst-case in-memory debit,
    called BEFORE every send (disk write is off-path — see Obligations).
  - `RiskGate::spawn_persister(handle)` — background ledger writer;
    `RiskGate::flush()` forces one synchronous write at teardown.
  - `RiskGate::arm() / kill() / is_killed()`.
- Constructed once per session: `RiskGate::new(RiskConfig, state_dir)`
  (`src/risk.rs`); `RiskConfig` deserializes from the `[risk]` TOML block and
  is `clamped()` to the hard consts.

## Exit door
- **Type:** `Result<(), risk::Refusal>` verdicts (typed reasons, rendered
  into OrderRecords), plus side effects: `<state-dir>/probe_ledger.json`
  (cumulative real-money spend, survives restarts) and the kill watcher.
  `RiskGate::new` takes the state dir as a `&Path`; `arm`'s `--state-dir` flag
  (default `state/`) sets it, so concurrent events each get their own ledger +
  STOP and never collide (`docs/MULTI-EVENT.md` §8).
- **Coordinated kill watcher:**
  `spawn_coordinated_kill_watcher(gate, stop_file, on_kill) ->
  (JoinHandle, Receiver<CoordinatedKill<T>>)` polls `<state-dir>/STOP` every
  200 ms and listens for SIGINT/SIGTERM. Every trigger first kills the gate,
  awaits session-scoped cancellation, and durably flushes the ledger; only
  then does it deliver `CoordinatedKill { trigger, kill_to_cancel_ms,
  cancellation }` to runtime assembly. Runtime drains sources/pipeline/report
  and consumes `event.acknowledge()`. The coordinated watcher itself never
  exits the process. A missing receiver or an event dropped without ack is a
  loud failure and releases the watcher instead of hanging. `KillTrigger` is
  a copyable/serializable evidence value (`stop_file`, `sigint`, `sigterm`).
- **Compatibility wrapper:** `spawn_kill_watcher(gate, stop_file, on_kill,
  on_terminate)` retains the old contract for callers not yet migrated.
  STOP/SIGINT acknowledge and return. Only this wrapper runs `on_terminate`
  and `process::exit(143)` after a coordinated SIGTERM; the new API never
  short-circuits runtime finalizers. Measured STOP→cancelled: 140 ms.

## Obligations
- Hard consts (`HARD_MAX_ORDER_NOTIONAL_CENTS`, `HARD_MAX_EVENT_…`,
  `HARD_DAILY_LOSS_…`, `PROBE_LEDGER_CAP_CENTS = 100` for M2,
  `MENTION_FILL_CAP_CENTS = 50` for the fill posture) are NOT
  user-overridable upward at runtime (spec §11). Raising one is a code
  change with review, never a config change.
- Unknown staleness (`u64::MAX`) is stale — refuse (P3). This is NOT relaxed by
  `book_stale_enforce`; unknown means no snapshot exists at all.
- **`book_age_ns` must be the per-ticker snapshot age.** From this gate's
  introduction until 2026-08-07 the executor passed
  `BookCache::feed_age_ns()` — socket-wide liveness, held under ~500 ms by the
  ping loop — so `Refusal::BookStale` never fired once. Proof from the record:
  DASH-UBER on a 96,774 ms snapshot was refused for `EventNotional`, checked
  twelve lines later.
- **Known-but-old is recorded, not refused, unless `book_stale_enforce`.**
  Enforcement is opt-in (default `false`) because the evidence does not support
  refusing: over 56 banked intents carrying a book age, the configured 2000 ms
  limit would refuse **43%**, including two of the three fills ever made
  (DASH-AFFO at 40.6 s, FIG-CODE at 2.7 s — both settled YES). P(YES) by age is
  0.78 / 0.75 / 0.58 across ≤2 s / 2–30 s / >30 s, and the oldest bucket's mean
  ask is 62.7¢ versus 79.6¢ elsewhere, so most of that decline is the price
  effect rather than staleness. A stale quote also cannot cost more than the
  cap: the order is a marketable limit at `max_price_cents`.
- The in-memory ledger debit (`record_send`) is synchronous and BEFORE every
  send — the authoritative in-session cap; an ambiguous order outcome still
  counts as spent. The DISK write is OFF the fire path: `record_send` only
  pokes a `Notify`, and `spawn_persister` (wired in `main.rs::arm`) coalesces
  those into writes. Single-shot commands and tests carry no persister — a
  `Drop`/`flush()` synchronous write makes their debit durable. The kill
  watcher also flushes on every trigger (on SIGTERM neither the persister nor
  `Drop` gets another chance). Worst case, a crash between a send and the next
  persist double-counts a few cents against the $1 probe cap; the in-memory
  total is never lost within a session.
- Ledger disk writes are ATOMIC (`fsio::write_atomic`, temp+rename) and a
  PRESENT-but-corrupt `probe_ledger.json` makes `RiskGate::new` refuse to
  start (missing file = fresh event = zero). Never default corrupt money
  state to zero — that silently erases the cross-run backstop
  (post-mortem 2026-07-15).
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

## Live postures (category gates live in `src/kalshi/validator.rs`)
The RiskGate envelope is identical in both; only the market-category gate
selected by `arm`/`probe-order` differs:
- **Probe (default, M2):** `m2_live_gate` — cap ≤ 1¢ AND non-Mentions only.
- **Mention-probe (`--allow-mention`):** `mention_probe_gate` — cap ≤ 1¢,
  Mentions-category permitted (the verification run). `probe-order
  --allow-mention` additionally requires IOC and a price strictly below the
  best ask (`assert_non_crossing`) so the drill provably cannot fill.
- **Mention-FILL (`--i-am-live --allow-mention --fillable`):** `mention_fill_gate`
  — Mentions permitted AND a per-order price cap up to
  `MENTION_FILL_CAP_CENTS` (= 50, `src/risk.rs`), so an order may cross a real
  ask and actually fill. This is the deliberate step past the no-fill probe.
  It relaxes ONLY the per-order price ceiling; real spend stays bounded by the
  config notional caps and the unchanged $1 `PROBE_LEDGER_CAP_CENTS` (both
  enforced in `check_order`). `--fillable` requires both `--i-am-live` and
  `--allow-mention` (guarded in `main.rs::arm`). Raising the ceiling itself is
  a code change with review, never per-run. Event-prep (`earshot prepare
  --fillable`) sets each keyword's `max_price_cents = min(live ask, ceiling)`
  — the smallest cap that fills.

## How to test in isolation
- Pure unit tests with tempdir state (see `risk.rs` tests: clamping,
  refusal ordering, ledger cap + persistence across gates, corrupt-ledger
  refusal, atomic-persist residue).
- Kill watcher: `risk.rs` unit tests cover typed STOP delivery, gate-kill and
  cancellation ordering, acknowledgement, a missing event receiver, and an
  unacknowledged dropped event. `tests/exec_mock.rs::kill_switch_stop_file_cancels_and_disarms`
  covers the compatibility STOP path; `tests/kill_sigterm.rs` raises a REAL
  SIGTERM at its own test process and pins compatibility exit/report behavior.
  Live: `earshot kill-drill --auto`.
