# Layer: book-cache

## Role
Maintains a local, wait-free-readable copy of the Kalshi order books for the
armed markets, fed by the WebSocket (spec §5.7: "read the local WS book
cache, never a blocking REST fetch"). NOT responsible for: deciding anything
— it is a passive mirror with a staleness stamp.

## Entry door
- **Type:** Kalshi WS frames (`orderbook_snapshot` / `orderbook_delta`).
- **Mechanism:** internal to `book::run_ws(env, signer, cache, tickers)`
  (tokio task, `src/kalshi/book.rs`). The task owns mutable scratch
  `Ladders`; the wire shapes are pinned in the module doc and
  VERIFICATIONS.md (`yes/no_dollars_fp` arrays; `price_dollars`/`delta_fp`
  deltas; per-leg pricing — `use_yes_price` is IGNORED by the live API).

## Exit door
- **Type:** `Arc<book::Book>` — `yes_bids`/`yes_asks: Vec<(u32 cents, u32
  qty_centi_contracts)>` best-first, `seq`, `as_of: MonotonicNs` — plus
  helpers `best_ask()`, `best_bid()`, `depth_at_or_below(cap)`. Quantities
  are CENTI-contracts (100 = 1 whole contract): Kalshi supports fractional
  contracts (pinned 2026-07-15) and exact hundredths keep deltas drift-free
  and sub-1.0 levels present.
- **Mechanism:** `BookCache::read(&ticker) -> Option<Arc<Book>>` — a
  wait-free `ArcSwap` load; readers can never observe a torn book. Plus
  `feed_age_ns() -> u64` (ns since last WS frame; `u64::MAX` before the
  first — unknown is stale).

## Second exit door — the recording tap (cross-venue lane)
- **Type:** `RawFrame { wall_ns, mono_ns, text }` over
  `FrameTap::try_offer_frame(RawFrame)` plus its shared `dropped` counter —
  the VERBATIM wire text plus our two clocks. The underlying sender is private
  so every clone/drop participates in producer-quiescence accounting;
  `BookTapeTask` starts a dedicated blocking writer and returns a
  `BookTapeHandle` whose terminal value is `BookTapeSummary`.
- **Mechanism:** `run_ws_url_tapped(url, signer, cache, tickers, Some(tap))`.
  `run_ws` / `run_ws_url` pass `None`, so the arm path is unchanged and the
  tap cannot alter what the executor reads.
- **Recorder construction:** async
  `BookTapeTask::start_new(path, session, cache, capacity, options)` performs
  exclusive creation, session write, `sync_all`, and parent-directory sync on
  Tokio's blocking pool before returning `(FrameTap, BookTapeHandle)`. An
  existing tape or receipt path is a hard refusal; a second run can never
  truncate the first run's evidence. Every later write and sync remains on
  that dedicated blocking task—disk delay cannot occupy an async runtime
  worker.
- **Tape rows (evidence protocol 2):** the raw frame envelope remains schema 1
  for the existing normalizer, while the session explicitly declares
  `book_tape_protocol_version = 2` and its required capabilities. Rows are one
  `session` first; byte-verbatim `frame` and timer-driven `heartbeat` rows;
  then one pending `close` last. The offline verifier rejects any other order,
  unknown row type, protocol/capability mismatch, or counter that disagrees
  with the rows it actually counted.
- **Transport health versus book readiness:** `feed_age_ns` and
  `feed_health` prove socket traffic, including pong/error traffic. They do
  not prove a usable book. Tape `ready_tickers` changes only after a
  successfully parsed full snapshot for that ticker has itself been accepted
  and written as a frame in this tape; a pre-warmed shared `BookCache` proves
  nothing about a new recording session. An empty but well-formed snapshot is
  ready because zero liquidity is a valid market state. Kalshi may encode an
  empty side by omitting that side's `yes_dollars_fp` or `no_dollars_fp` key
  (VERIFICATIONS Q-XTAPE); the strict parser treats only omission as an empty
  array. A present non-array side or one malformed level rejects the entire
  snapshot atomically. Heartbeats, close, receipt,
  and summary carry the requested/ready/missing ticker partition. The offline
  verifier derives the same proof from raw frame rows using the same strict
  snapshot validator rather than trusting terminal metadata.
- **Two-phase terminal proof:** the tape's `close` row is always
  `status = pending`, `complete = false`, and `commit_required = true`. After
  syncing that row with `sync_all`, the writer creates and syncs
  `<tape>.commit.pending`, atomically publishes it as
  `<tape>.commit.json`, and syncs the parent directory. The receipt binds the
  exact synced tape by byte length and SHA-256. Only that separate receipt may
  say `complete = true`; `verify_book_tape(path)` recomputes the fingerprint
  and rejects deleted, inserted, or modified bytes, a corrupt receipt, and any
  lifecycle/readiness/accounting mismatch. A buffered or altered tape can
  therefore never masquerade as a durable complete run.
- **Quiet-market proof:** after the required initial snapshot, default
  heartbeat and `sync_data` intervals are both one second and run
  independently of incoming frames. A quiet healthy connection therefore
  keeps growing and becoming durable; an empty or
  never-connected tape is never accepted as evidence of an inactive market.
- **Why raw, not parsed:** this layer's own exit door is lossy on purpose —
  integer cents, sub-cent levels dropped, unknown types skipped. Correct for
  trading, useless as evidence. Polymarket ticks at 0.001 (verified live
  2026-08-04), so a cents tape could not answer a cross-venue question at all.
  The tape re-normalizes forever; a parsed tape answers only what this parser
  already anticipated.
- **Consumer:** `examples/book-tape.rs` → `state/xvenue/<event>/kalshi-*.jsonl`,
  normalized by `scripts/xvenue-normalize.py` (raw envelope schema 1; evidence
  protocol 2 is frozen by `BOOK_TAPE_SCHEMA_VERSION`). The example owns only
  credential/ticker discovery, signals, and feed launch; file lifecycle and
  accounting are the reusable library task. Runtime-composer wiring is
  deliberately not part of this change.

## Obligations
- The NO→YES conversion happens here and only here:
  `yes_ask = 100¢ − no_bid` (`Ladders::apply_level`). Everything downstream
  thinks in yes-leg cents.
- Quantities parse EXACTLY (`fp_to_centi_contracts`): truncating fractional
  wire sizes to whole contracts deleted sub-1.0 levels and let delta
  bookkeeping drift (2026-07-15 MS run — see VERIFICATIONS.md). Sub-cent
  PRICE levels (`deci_cent` structure) are refused: the arm-time validator
  rejects non-`linear_cent` markets, and the parser drops such levels loudly
  as belt-and-braces.
- Publish a complete fresh `Arc<Book>` per applied message — no partial
  mutation visible to readers.
- Liveness ≠ market activity: the task pings every 500 ms so `feed_age_ns`
  measures CONNECTION health; a quiet market stays fresh (pinned live —
  a 2 s staleness guard tripped on a quiet book before this).
- On any error: reconnect after 500 ms and re-subscribe — a fresh subscribe
  always yields a fresh snapshot, so books never silently drift across
  reconnects. Staleness during the gap is visible via `feed_age_ns`.
- The ticker set is fixed at construction (`BookCache::new(&tickers)`); the
  map is never resized (readers hold no locks).
- Close is a producer-quiescence protocol, not a receiver cutoff: the caller
  calls `BookTapeHandle::request_close(reason)`, then stops and joins the WS
  producer, and only then awaits `handle.wait()`. The writer keeps accepting
  frames after close intent until every `FrameTap` sender drops. Channel
  disconnection proves all accepted frames were consumed, so the terminal
  dropped snapshot cannot undercount a still-running producer. Producer loss
  before an explicit request remains `partial`; producer closure after a
  request is expected.
- Producer quiescence and accepted-backlog drain have separate five-second
  defaults. If any tracked `FrameTap` remains alive at the producer deadline,
  the writer returns `WriterError`/`producer_quiescence_timeout`, publishes no
  close or commit, and reports `producer_quiesced = false` plus
  `terminal_accounting_complete = false`. Once the last producer drops, a
  separate backlog deadline bounds queued-frame drain. Expiry returns
  `backlog_drain_timeout`, counts every remaining accepted frame as unwritten,
  and publishes no close or commit; it is not mislabeled as a live sender.
- Feed health is captured at close intent, before a potentially slow queue
  drain or disk sync. Tape-local readiness is reconstructed from written
  snapshot frames whose receive stamp is at or before that same close
  boundary. Terminal health/readiness therefore describe the event boundary
  and cannot drift merely because durable shutdown took time.
- `complete = true` requires every condition: explicit close, durable commit
  receipt, proven producer quiescence, complete terminal accounting, zero tap
  drops, zero accepted-but-unwritten frames, at least one session-scoped WS
  observation, terminal `feed_health = healthy`, and a taped successful full
  snapshot for every requested ticker. Never-connected (`unknown`),
  transport-only, missing-ticker, and formerly-connected-but-terminally-stale
  tapes are partial.
- The event wait loop must select on both `BookTapeHandle::wait()` and the
  handle's failure subscription. An early completion or write/sync failure is
  an immediate event failure; it must not remain hidden until the planned
  deadline.

## Failure policy
- Unknown message types are skipped; `type: "error"` frames are logged loud.
- Parse failures skip the message (defensive), never crash the feed. Snapshot
  validation is atomic: one malformed level preserves the prior book and does
  not establish readiness.
- The tap uses `try_send` ONLY. A wedged recorder must never stall the feed the
  executor reads, so overflow increments `FrameTap::dropped` instead of
  blocking. `BookTapeTask` snapshots that count into every heartbeat and the
  close summary: a truncated tape is always visible as a number, never as
  silence. Any nonzero terminal count makes the summary partial and
  `complete = false`, even after an otherwise graceful requested close.
- A recorder open/header error is returned before a tap exists. A later
  write, `sync_data`, `sync_all`, or receipt-publication error is published
  immediately and returns `BookTapeStatus::WriterError` with
  `complete = false`. After an I/O error the blocking task continues consuming
  without writing until producer quiescence, so `unwritten_frames` and the
  final tap drop count are exact. `close_written` means the pending close was
  durably synced; `committed` means the separate receipt was durably
  published.
- A live-producer timeout is the one case where future accepted/drop counts
  cannot be known; its in-memory summary explicitly marks terminal accounting
  incomplete and no durable terminal record is published. A backlog timeout
  happens only after producer count reaches zero, so every remaining accepted
  row is drained and counted as unwritten before returning.
- Recorder failure never propagates into the WS task: `FrameTap::offer`
  remains `try_send`-only, and every refused frame increments `dropped`.

## How to replace or edit this layer
- Everything is `src/kalshi/book.rs`; wiring in `main.rs::arm` / `book_soak`,
  and `examples/book-tape.rs` for the standalone recorder.
- Contract: `Book` semantics (yes-leg integer cents, best-first ordering),
  `read`/`feed_age_ns` signatures, publish-complete-books rule, raw-frame
  fidelity, exclusive tape creation, and terminal accounting.
- Wire-shape changes: pin with a live capture (RUST_LOG=…book=debug, `earshot
  book`) and record in VERIFICATIONS.md before editing the parser.

## How to test in isolation
- Unit tests build wire-exact JSON (`snapshot()`/`delta()` helpers in the
  file) and assert ladder convergence + leg conversion — no network.
- Live read-only soak: `earshot book --ticker T --secs N` prints
  bid/ask/levels/seq/feed_age every second.
- Tap properties: `cargo test --test ws_tape` — byte-verbatim recording,
  parser-rejected frames still taped, a jammed tap counting drops while the
  book keeps converging, quiet-market heartbeat growth/durability, overwrite
  refusal, pre-warmed-cache rejection, malformed-snapshot rejection, bounded
  producer quiescence and backlog drain, final accepted/drop accounting,
  SHA-256 tamper rejection (delete/modify/insert), protocol-1 rejection,
  transport-only incompleteness, empty-snapshot readiness, unknown/stale
  rejection, and complete versus partial close summaries. `cargo test --lib
  kalshi::book` also injects a terminal `sync_all` failure and controllably
  blocked sinks under a single-thread Tokio runtime, including close-time
  health held across a slow drain and exact backlog-timeout accounting.
- Live recorder: `cargo run --release --example book-tape -- --event E
  --out /tmp/k.jsonl --secs 60` (read-only; references no order endpoint and
  refuses if `/tmp/k.jsonl` already exists).
