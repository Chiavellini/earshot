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
  `FrameTap { tx, dropped }` — the VERBATIM wire text plus our two clocks.
- **Mechanism:** `run_ws_url_tapped(url, signer, cache, tickers, Some(tap))`.
  `run_ws` / `run_ws_url` pass `None`, so the arm path is unchanged and the
  tap cannot alter what the executor reads.
- **Why raw, not parsed:** this layer's own exit door is lossy on purpose —
  integer cents, sub-cent levels dropped, unknown types skipped. Correct for
  trading, useless as evidence. Polymarket ticks at 0.001 (verified live
  2026-08-04), so a cents tape could not answer a cross-venue question at all.
  The tape re-normalizes forever; a parsed tape answers only what this parser
  already anticipated.
- **Consumer:** `examples/book-tape.rs` → `state/xvenue/<event>/kalshi-*.jsonl`,
  normalized by `scripts/xvenue-normalize.py` (`XV_SCHEMA`).

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

## Failure policy
- Unknown message types are skipped; `type: "error"` frames are logged loud.
- Parse failures skip the message (defensive), never crash the feed.
- The tap uses `try_send` ONLY. A wedged recorder must never stall the feed the
  executor reads, so overflow increments `FrameTap::dropped` instead of
  blocking. `book-tape` writes that count into the tape's `close` row: a
  truncated tape is always visible as a number, never as silence.

## How to replace or edit this layer
- Everything is `src/kalshi/book.rs`; wiring in `main.rs::arm` / `book_soak`,
  and `examples/book-tape.rs` for the recorder.
- Contract: `Book` semantics (yes-leg integer cents, best-first ordering),
  `read`/`feed_age_ns` signatures, publish-complete-books rule.
- Wire-shape changes: pin with a live capture (RUST_LOG=…book=debug, `earshot
  book`) and record in VERIFICATIONS.md before editing the parser.

## How to test in isolation
- Unit tests build wire-exact JSON (`snapshot()`/`delta()` helpers in the
  file) and assert ladder convergence + leg conversion — no network.
- Live read-only soak: `earshot book --ticker T --secs N` prints
  bid/ask/levels/seq/feed_age every second.
- Tap properties: `cargo test --test ws_tape` — byte-verbatim recording,
  parser-rejected frames still taped, and a jammed tap counting drops while
  the book keeps converging.
- Live recorder: `cargo run --release --example book-tape -- --event E
  --out /tmp/k.jsonl --secs 60` (read-only; references no order endpoint).
