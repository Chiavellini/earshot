# Layer: decision

## Role
Maps acoustic `Detection`s to sized order intents via a precompiled table and
a one-shot per-keyword state machine (spec §5.6). Owns policy (fast_only /
confirmed_only / two_tier) and tranche arithmetic. NOT responsible for:
book reads, EV/fee math, risk checks, or anything network — those live in
the executor. It also owns the durable form of the one-shot state: an optional
event-scoped decision journal prevents a new process from re-arming a keyword
that an earlier attempt already consumed.

## Entry door
- **Type:** `&types::Detection` (carries `source: SourceId` — which audio
  leg heard it; detectors copy it from the chunk).
- **Mechanism:** `DecisionTable::decide(&mut self, &Detection) ->
  Option<DecisionIntent>` (`src/decision.rs`), called inline on the `detect`
  OS thread right after `Detector::process`. In fanout mode
  (`pipeline::run_pipeline_fanout`) the ONE table is shared by every leg's
  detect thread behind an `Arc<Mutex<..>>`, locked exactly for the O(µs)
  `decide` call — the one-shot machine is thereby the cross-leg dedupe
  point: two legs racing the same keyword serialize on the lock and the
  second gets `None` (`concurrent_decides_yield_exactly_one_intent` test).
- That O(µs) lock description applies to the in-memory constructor. A
  persistent table deliberately holds the shared decision mutex through one
  durable append on an eligible one-shot transition. Reliability-before-
  intent is the chosen tradeoff: sibling legs wait for that sync, at most
  twice per market. Assembly must expose this delay in run health/latency when
  it wires the persistent path.
- **Persistent constructor:**
  `DecisionTable::from_keywords_with_journal(keywords, path)` exclusively
  locks and replays `path` before the table can be used. The path is scoped to
  the trading event, not to an individual process attempt or source lane, and
  its parent state root must already exist. The legacy
  `from_keywords` constructor remains in-memory for replay, benchmarks, and
  callers that cannot place live orders.

## Exit door
- **Type:** `types::DecisionIntent` — `keyword`, `source` (the leg that won
  the race, copied from the triggering detection), `market_ticker`,
  `max_price_cents`, `tranche_contracts`, `tier`, `audio_ts`,
  `decision_ts: MonotonicNs` (stamped here), `handoff_ts: MonotonicNs`
  (stamped 0 here; the detect loop stamps the real value on the sent clone
  immediately before `try_send` — it feeds the `handoff→exec` edge).
- **Mechanism:** returned to the detect loop, which — FIRE FIRST — if an
  executor is attached, `try_send`s a clone into
  `tokio::sync::mpsc::Sender<DecisionIntent>` (cap 64), and only THEN records
  edges and logs (nothing but the trade on the critical span). Full/closed
  channel = counted LOUD drop (`intent channel full/closed: intent DROPPED`,
  logged after the handoff attempt) — the detect thread never blocks on the
  executor.
- **Durable side receipt:** before returning an eligible intent, a persistent
  table appends and durably syncs one schema-1 JSONL receipt containing
  sequence, wall-clock nanoseconds, stable market ticker, triggering tier,
  policy/price/budget/tranche fields, and resulting one-shot state.
  `decide_checked` exposes persistence failure separately from an ordinary
  ignored detection. `DecisionHealth` publishes the first fault to assembly,
  which kills the risk session and records `decision_state_error`; `decide`
  remains only as a loud fail-closed compatibility door.

## Obligations
- O(1) lookup, no allocation on the miss path; the table is compiled once
  from `config::KeywordConfig` at arm time (`DecisionTable::from_keywords`).
- Confidence gate BEFORE any state transition: `det.confidence <
  min_confidence_fast/_confirmed` (per tier, from config; default 0.0 =
  pass everything) returns `None` without consuming the one-shot machine.
  Engines without real scores (sherpa_kws) emit confidence 1.0, so the
  gates compose but default off.
- State machine per keyword: `Armed → FastFired → Done` (two_tier) or
  `Armed → Done` (single-tier). Once `Done`, further detections return
  `None` forever (one-shot; repeats are logged and ignored).
- Persistent state is replayed by stable `market_ticker`, never config-order
  `KeywordId`. Sequence gaps, a truncated final line, an unknown ticker,
  policy-incompatible history, or an unsupported schema refuses startup; a
  present corrupt journal is never treated as empty.
- Every receipt carries a SHA-1 fingerprint of the complete sorted
  decision-relevant keyword plan. Adding/removing a keyword or changing its
  phrase, variants, tier thresholds, confidence gates, policy, price, budget,
  or haircut refuses replay. Duplicate market tickers are rejected because
  they do not provide a stable persistent identity.
- `<journal>.plan.json` pins that same complete fingerprint durably during
  construction, before any detection. An empty journal therefore cannot be
  reopened by a drifted PHONE/WEBCAST plan merely because the first lane heard
  no eligible word.
- A receipt's policy, price ceiling, total budget, and released tranche must
  match the current keyword config during replay. In particular, a restart
  after a Fast tranche cannot silently recompute the Confirmed remainder from
  a different budget.
- The journal holds a nonblocking exclusive `flock`. Assembly retains a cloned
  `DecisionJournalLease` through executor drain, ledger flush, and final report
  persistence, so a second process cannot overlap the teardown window after
  the pipeline drops its table.
- Receipt order is durability first, in-memory transition second, intent
  return last. An append, short-write, or sync failure poisons the table and
  refuses that and every later eligible intent for the process. It never
  guesses whether an ambiguous write was durable.
- The state root is created by the caller before construction. After
  acquiring the lock, every constructor syncs the file and parent directory
  entry before any decision is accepted; doing this unconditionally closes
  the create-versus-lock race between two processes. Each later receipt uses
  `sync_all`; macOS also
  requests `F_FULLFSYNC` so an acknowledged receipt is pushed beyond volatile
  drive caches.
- Confidence and tier/policy refusals happen before journaling and therefore
  do not consume state. There are at most two synchronous journal writes per
  market (Fast then Confirmed); the miss/repeat path remains O(1) and I/O-free.
- Tranche arithmetic: Fast releases `FAST_TRANCHE_PCT` (30%) of budget;
  Confirmed releases the remainder — or the full budget if Fast never fired.
  **The executor's `prestage_orders` mirrors this arithmetic exactly; change
  them together** (`src/kalshi/executor.rs::prestage_orders`).
- Stamp `decision_ts` inside `decide` (it feeds the `detect→decision` edge).

## Failure policy
- Unknown `KeywordId` → `None` (log upstream); cannot panic on the hot path.
- Persistent constructor failure → arm refusal. Journal append failure →
  `decide_checked` error and publication through `DecisionHealth`; assembly
  must kill the session, cancel its registered orders, bank a fault receipt,
  and terminate the run with `decision_state_error`. A loud fail-closed `None`
  exists only through the compatibility method.

## How to replace or edit this layer
- Everything is `src/decision.rs`. Contract: `decide`'s signature,
  `DecisionIntent` semantics, one-shot guarantee, tranche sync with
  `prestage_orders`.
- Do not move the journal under a run-attempt directory: its purpose is to
  survive process reconstruction for the same event. Run evidence may link to
  it, while event state owns the bytes.
- Live assembly uses the persistent constructor. Dry-run/replay use
  `from_keywords` and cannot create or consume live state. Do not hide path
  selection behind an environment-variable lookup in this layer.
- Free: richer policies, per-tier budgets — extend `Policy` in config,
  mirror in prestage, update both docs.

## How to test in isolation
- Pure unit tests, no I/O: build a `DecisionTable` from literal
  `KeywordConfig`s and feed `Detection`s (see tests in `decision.rs` for
  two_tier / confirmed_only / one-shot coverage).
- Journal unit tests use temporary event directories and prove restart
  rehydration, two-tier continuation, confidence non-consumption, fail-closed
  pre-write, partial-write, and post-write/pre-sync behavior, corrupt/config-
  drift refusal (including an empty journal's pinned plan), duplicate-ticker
  refusal, arm-lifetime lease, shared health signal, and the single-live-arm
  file lock.
  The existing replay/fanout integration tests must remain green for the
  in-memory compatibility constructor.
