//! Local order-book cache fed by the Kalshi WebSocket (spec §5.7: "read the
//! local WS book cache, never a blocking REST fetch").
//!
//! Concurrency: the WS reader task owns mutable scratch ladders and publishes
//! an immutable `Arc<Book>` per update into an `ArcSwap` — readers (the
//! executor, on the intent hot path) do wait-free loads and can never observe
//! a torn book. The ticker set is fixed at arm time, so the map itself is
//! never resized.
//!
//! Wire format (pinned live 2026-07-10 — the docs' `use_yes_price` flag is
//! NOT honored on this protocol version, each side arrives in its own leg):
//!   snapshot: msg.{yes,no}_dollars_fp = [["0.9900","23844.00"], …]
//!   delta:    msg.{price_dollars, delta_fp, side: "yes"|"no"}
//! A snapshot may omit either dollar-array key when that entire side is empty
//! (pinned live in VERIFICATIONS.md Q-XTAPE); omission means an empty side,
//! while a present non-array value is malformed and rejects the snapshot.
//! Conversion done here: a NO bid at p¢ IS a YES ask at (100−p)¢. All prices
//! integer cents internally, never floats.
//!
//! Quantities (re-pinned live 2026-07-15, VERIFICATIONS.md): Kalshi supports
//! FRACTIONAL contracts — `*_fp` sizes/deltas carry hundredths ("0.77",
//! "3075.67"). Quantities are therefore integer CENTI-contracts internally
//! (qty 77 = 0.77 contracts): exact, so deltas never drift, and a 0.77-
//! contract level is never rounded out of the ladder (that truncation cost
//! us the real best-ask during the 2026-07-15 MS run). Prices: mention
//! markets are `price_level_structure: "linear_cent"`; `deci_cent` (0.1¢
//! steps) exists on other market classes and is REFUSED at arm time by the
//! validator — this parser drops sub-cent price levels loudly if one ever
//! appears.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError, TryRecvError};
use std::sync::Arc;
use std::time::{Duration, Instant};

use arc_swap::ArcSwap;
use futures_util::{SinkExt, StreamExt};
use rsa::sha2::{Digest, Sha256};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::auth::Signer;
use super::{KalshiEnv, KalshiError};
use crate::clock::MonotonicNs;

#[derive(Debug, Clone, Default)]
pub struct Book {
    /// (price_cents, qty_centi_contracts), best (highest) first.
    /// qty 100 = 1 whole contract; fractional wire sizes stay exact.
    pub yes_bids: Vec<(u32, u32)>,
    /// (price_cents, qty_centi_contracts), best (lowest) first — from the no
    /// side, already yes-leg priced via use_yes_price.
    pub yes_asks: Vec<(u32, u32)>,
    pub seq: u64,
    pub as_of: MonotonicNs,
}

impl Book {
    pub fn best_ask(&self) -> Option<(u32, u32)> {
        self.yes_asks.first().copied()
    }
    pub fn best_bid(&self) -> Option<(u32, u32)> {
        self.yes_bids.first().copied()
    }
    /// Total quantity (centi-contracts) offered at or below `cap_cents`.
    pub fn depth_at_or_below(&self, cap_cents: u32) -> u32 {
        self.yes_asks
            .iter()
            .take_while(|(p, _)| *p <= cap_cents)
            .map(|(_, q)| q)
            .sum()
    }
}

pub struct BookCache {
    books: HashMap<String, ArcSwap<Book>>,
    snapshot_ready: HashMap<String, std::sync::atomic::AtomicBool>,
    last_ws_msg: AtomicU64, // MonotonicNs of last message on the socket
    ws_observations: AtomicU64,
}

impl BookCache {
    pub fn new(tickers: &[String]) -> Arc<Self> {
        let books = tickers
            .iter()
            .map(|t| (t.clone(), ArcSwap::from_pointee(Book::default())))
            .collect();
        let snapshot_ready = tickers
            .iter()
            .map(|ticker| (ticker.clone(), std::sync::atomic::AtomicBool::new(false)))
            .collect();
        Arc::new(BookCache {
            books,
            snapshot_ready,
            last_ws_msg: AtomicU64::new(0),
            ws_observations: AtomicU64::new(0),
        })
    }

    /// Wait-free read (hot path).
    pub fn read(&self, ticker: &str) -> Option<Arc<Book>> {
        self.books.get(ticker).map(|b| b.load_full())
    }

    pub fn tickers(&self) -> impl Iterator<Item = &String> {
        self.books.keys()
    }

    /// ns since the last WS message; u64::MAX before the first one.
    pub fn feed_age_ns(&self) -> u64 {
        let last = self.last_ws_msg.load(Ordering::Relaxed);
        if last == 0 {
            u64::MAX
        } else {
            MonotonicNs::now().saturating_since(MonotonicNs(last))
        }
    }

    /// Public for tests and the WS task; not part of any layer's door.
    #[doc(hidden)]
    pub fn touch(&self) {
        self.last_ws_msg
            .store(MonotonicNs::now().0, Ordering::Relaxed);
        // Release pairs with a recorder's Acquire baseline/snapshot. Seeing
        // the increment therefore also makes the preceding receive stamp
        // visible to its terminal-health calculation.
        self.ws_observations.fetch_add(1, Ordering::Release);
    }

    #[doc(hidden)]
    pub fn observation_count(&self) -> u64 {
        self.ws_observations.load(Ordering::Acquire)
    }

    /// Public for tests and the WS task; not part of any layer's door.
    #[doc(hidden)]
    pub fn publish(&self, ticker: &str, book: Book) {
        if let Some(slot) = self.books.get(ticker) {
            slot.store(Arc::new(book));
        }
    }

    /// Publish a book that came from a successfully parsed full snapshot and
    /// mark that ticker usable. Empty ladders are valid readiness: the market
    /// can genuinely have no resting liquidity.
    #[doc(hidden)]
    pub fn publish_snapshot(&self, ticker: &str, book: Book) {
        self.publish(ticker, book);
        if let Some(ready) = self.snapshot_ready.get(ticker) {
            ready.store(true, Ordering::Release);
        }
    }

    #[doc(hidden)]
    pub fn snapshot_is_ready(&self, ticker: &str) -> bool {
        self.snapshot_ready
            .get(ticker)
            .is_some_and(|ready| ready.load(Ordering::Acquire))
    }
}

/// Mutable scratch state owned by the WS task. Quantities in centi-contracts.
#[derive(Default)]
struct Ladders {
    yes_bids: BTreeMap<u32, u32>,
    yes_asks: BTreeMap<u32, u32>,
    seq: u64,
}

/// "0.9900" → 99¢. Integer parsing only; None for anything outside 0–100¢.
/// A SUB-CENT price (deci_cent structure, e.g. "0.3355") is refused loudly —
/// truncating it would merge distinct wire levels and corrupt delta
/// bookkeeping. The arm-time validator refuses deci_cent markets outright,
/// so live never reaches this; None here drops the level, P3-style.
pub fn dollars_to_cents(s: &str) -> Option<u32> {
    let (int, frac) = s.split_once('.').unwrap_or((s, ""));
    let dollars: u32 = int.parse().ok()?;
    if frac.chars().skip(2).any(|c| c != '0') {
        tracing::error!(price = s, "sub-cent price level (deci_cent?) — dropping level; validator should have refused this market");
        return None;
    }
    let frac2 = format!("{:0<2}", frac.chars().take(2).collect::<String>());
    let cents_part: u32 = frac2.parse().ok()?;
    let cents = dollars * 100 + cents_part;
    (cents <= 100).then_some(cents)
}

/// Fixed-point quantity ("23844.00", "-11429.00", "0.77") → CENTI-contracts
/// (hundredths), exact. Kalshi supports fractional contracts (pinned
/// 2026-07-15); parsing must be exact or deltas drift and sub-1.0 levels
/// vanish. Digits beyond hundredths are logged loudly if nonzero (a new
/// precision drift would recreate the 2026-07-15 bug class).
fn fp_to_centi_contracts(s: &str) -> Option<i64> {
    let neg = s.starts_with('-');
    let s = s.trim_start_matches('-');
    let (int, frac) = s.split_once('.').unwrap_or((s, ""));
    let whole: i64 = int.parse().ok()?;
    let frac2 = format!("{:0<2}", frac.chars().take(2).collect::<String>());
    let centi_part: i64 = frac2.parse().ok()?;
    if frac.chars().skip(2).any(|c| c != '0') {
        tracing::warn!(
            qty = s,
            "quantity finer than centi-contracts — truncating beyond hundredths"
        );
    }
    let centi = whole.checked_mul(100)?.checked_add(centi_part)?;
    Some(if neg { -centi } else { centi })
}

impl Ladders {
    fn to_book(&self) -> Book {
        Book {
            yes_bids: self.yes_bids.iter().rev().map(|(&p, &q)| (p, q)).collect(),
            yes_asks: self.yes_asks.iter().map(|(&p, &q)| (p, q)).collect(),
            seq: self.seq,
            as_of: MonotonicNs::now(),
        }
    }

    /// `price_cents` is in the side's OWN leg; the no→yes conversion
    /// (yes_ask = 100 − no_bid) happens here and only here.
    fn apply_level(&mut self, side: &str, price_cents: u32, qty_or_delta: i64, is_delta: bool) {
        let (ladder, key) = match side {
            "yes" => (&mut self.yes_bids, price_cents),
            "no" => (&mut self.yes_asks, 100 - price_cents.min(100)),
            _ => return,
        };
        let qty = if is_delta {
            (ladder.get(&key).copied().unwrap_or(0) as i64 + qty_or_delta).max(0) as u32
        } else {
            qty_or_delta.max(0) as u32
        };
        if qty == 0 {
            ladder.remove(&key);
        } else {
            ladder.insert(key, qty);
        }
    }
}

/// Apply one parsed WS message to the scratch ladders; returns the ticker
/// whose book changed. Wire shapes pinned from live capture (see module doc).
struct AppliedMessage {
    ticker: String,
    snapshot: bool,
}

fn apply_message(scratch: &mut HashMap<String, Ladders>, msg: &Value) -> Option<AppliedMessage> {
    let kind = msg["type"].as_str()?;
    let m = &msg["msg"];
    let ticker = m["market_ticker"].as_str()?.to_string();

    match kind {
        "orderbook_snapshot" => {
            // Parse into a replacement ladder first. One malformed level must
            // reject the entire snapshot: publishing a partial baseline and
            // then marking it ready would make both trading and evidence lie.
            let ladders = parse_full_snapshot(msg)?;
            scratch.insert(ticker.clone(), ladders);
            Some(AppliedMessage {
                ticker,
                snapshot: true,
            })
        }
        "orderbook_delta" => {
            let price = m["price_dollars"].as_str().and_then(dollars_to_cents)?;
            let delta = m["delta_fp"].as_str().and_then(fp_to_centi_contracts)?;
            let side = m["side"].as_str()?;
            let ladders = scratch.entry(ticker.clone()).or_default();
            ladders.apply_level(side, price, delta, true);
            ladders.seq = msg["seq"].as_u64().unwrap_or(ladders.seq + 1);
            Some(AppliedMessage {
                ticker,
                snapshot: false,
            })
        }
        _ => None,
    }
}

/// Parse a complete snapshot without mutating caller state. This exact helper
/// is shared by the live cache, the tape writer, and the offline verifier so
/// "snapshot ready" has one meaning at all three boundaries.
fn parse_full_snapshot(msg: &Value) -> Option<Ladders> {
    if msg["type"] != "orderbook_snapshot" {
        return None;
    }
    let m = &msg["msg"];
    m["market_ticker"].as_str()?;
    // Kalshi omits a side key when that side has zero levels. This is not the
    // same as accepting arbitrary missing snapshot structure: ticker and seq
    // remain mandatory, and a present side must still be an array whose every
    // level parses atomically. Q-XTAPE pins the omitted-empty-side wire fact.
    fn side_levels<'a>(message: &'a Value, key: &str) -> Option<&'a [Value]> {
        match message.get(key) {
            None => Some(&[]),
            Some(Value::Array(levels)) => Some(levels.as_slice()),
            Some(_) => None,
        }
    }
    let yes_levels = side_levels(m, "yes_dollars_fp")?;
    let no_levels = side_levels(m, "no_dollars_fp")?;
    let mut ladders = Ladders::default();
    for (side, levels) in [("yes", yes_levels), ("no", no_levels)] {
        for level in levels {
            let pair = level.as_array()?;
            if pair.len() != 2 {
                return None;
            }
            let price = pair[0].as_str().and_then(dollars_to_cents)?;
            let qty = pair[1].as_str().and_then(fp_to_centi_contracts)?;
            if !(0..=u32::MAX as i64).contains(&qty) {
                return None;
            }
            ladders.apply_level(side, price, qty, false);
        }
    }
    ladders.seq = msg["seq"].as_u64()?;
    Some(ladders)
}

fn valid_snapshot_ticker(raw: &str) -> Option<String> {
    let msg: Value = serde_json::from_str(raw).ok()?;
    parse_full_snapshot(&msg)?;
    msg["msg"]["market_ticker"].as_str().map(str::to_string)
}

/// One verbatim WS frame with the clocks we received it on.
///
/// The tape stores the wire text UNPARSED on purpose. `dollars_to_cents`
/// drops sub-cent levels and the ladder is integer cents by design — both
/// correct for the trading path, both lossy. A recorder that stored the parsed
/// book could only ever answer questions this parser already anticipated, and
/// cross-venue analysis is exactly the case where that bites (Polymarket ticks
/// at 0.001, verified live 2026-08-04). Raw text re-normalizes forever.
#[derive(Debug, Clone)]
pub struct RawFrame {
    /// Nanoseconds since the Unix epoch on OUR host — the cross-venue time
    /// axis. Kalshi's WS frames carry `seq` but no server timestamp, so this
    /// receive stamp is the only clock available on this side.
    pub wall_ns: u64,
    pub mono_ns: u64,
    pub text: String,
}

/// The evidence protocol version. Protocol 2 adds a cryptographically bound
/// commit receipt, tape-local per-ticker snapshot readiness, and separate
/// bounded producer-quiescence/backlog-drain phases.
pub const BOOK_TAPE_SCHEMA_VERSION: u32 = 2;

// `xvenue-normalize.py` currently consumes only the raw frame envelope and
// ignores lifecycle rows. Keep that envelope marker at 1 while the stricter
// evidence protocol is independently identified and verified as version 2.
const BOOK_TAPE_ENVELOPE_SCHEMA_VERSION: u32 = 1;
const BOOK_TAPE_CAPABILITIES: [&str; 5] = [
    "sha256_bound_commit",
    "pending_close_commit",
    "per_ticker_snapshot_readiness",
    "bounded_producer_quiescence",
    "bounded_backlog_drain",
];

const BOOK_TAPE_CONTROL_POLL: Duration = Duration::from_millis(10);

/// Immutable provenance written before the feed is started.
#[derive(Debug, Clone)]
pub struct BookTapeSession {
    pub endpoint: String,
    pub event: Option<String>,
    pub label: String,
    pub tickers: Vec<String>,
    pub host: String,
    pub git_sha: String,
    pub clock_offset_s: Option<f64>,
    pub planned_secs: Option<u64>,
}

/// Recorder timing policy. Heartbeats are evidence rows, not log messages:
/// they prove that a quiet tape was still open and observing a healthy feed.
#[derive(Debug, Clone)]
pub struct BookTapeOptions {
    pub heartbeat_interval: Duration,
    pub flush_interval: Duration,
    pub stale_after: Duration,
    /// From explicit close intent until the final tracked sender drops.
    pub producer_quiescence_timeout: Duration,
    /// From the final sender drop until every accepted frame is written.
    pub backlog_drain_timeout: Duration,
}

impl Default for BookTapeOptions {
    fn default() -> Self {
        Self {
            heartbeat_interval: Duration::from_secs(1),
            flush_interval: Duration::from_secs(1),
            stale_after: Duration::from_secs(2),
            producer_quiescence_timeout: Duration::from_secs(5),
            backlog_drain_timeout: Duration::from_secs(5),
        }
    }
}

/// Requested, graceful reasons for closing a tape. The request records intent;
/// the writer does not finalize until every `FrameTap` producer has exited.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BookTapeCloseReason {
    Deadline,
    Sigterm,
    Sigint,
    Requested,
}

impl BookTapeCloseReason {
    fn as_str(self) -> &'static str {
        match self {
            Self::Deadline => "deadline",
            Self::Sigterm => "sigterm",
            Self::Sigint => "sigint",
            Self::Requested => "requested",
        }
    }
}

/// Terminal recorder state. `Partial` means terminal accounting was durable
/// but at least one completeness gate failed (producer loss, drops, unknown or
/// stale feed); `WriterError` means the returned in-memory summary is the only
/// terminal status because the tape may not contain a durable pending-close
/// row or commit receipt. Its `terminal_accounting_complete` field says
/// whether all accepted/drop counts were knowable.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BookTapeStatus {
    Closed,
    Partial,
    WriterError,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BookTapeFeedHealth {
    Unknown,
    Healthy,
    Stale,
}

#[derive(Debug, Clone)]
struct BookTapeReadiness {
    ready: Vec<String>,
    missing: Vec<String>,
}

impl BookTapeReadiness {
    fn all_ready(&self) -> bool {
        self.missing.is_empty() && !self.ready.is_empty()
    }
}

fn proven_readiness(
    requested: &[String],
    snapshot_proofs: &HashMap<String, u64>,
    through_mono_ns: u64,
) -> BookTapeReadiness {
    let proven: HashSet<&str> = snapshot_proofs
        .iter()
        .filter_map(|(ticker, mono_ns)| (*mono_ns <= through_mono_ns).then_some(ticker.as_str()))
        .collect();
    let mut ready = Vec::new();
    let mut missing = Vec::new();
    for ticker in requested {
        if proven.contains(ticker.as_str()) {
            ready.push(ticker.clone());
        } else {
            missing.push(ticker.clone());
        }
    }
    BookTapeReadiness { ready, missing }
}

#[derive(Debug, Clone)]
struct CloseSnapshot {
    wall_ns: u64,
    mono_ns: u64,
    transport_age_ns: Option<u64>,
    transport_health: BookTapeFeedHealth,
    observed_after_start: bool,
}

#[derive(Debug, Clone)]
struct CloseRequest {
    reason: BookTapeCloseReason,
    snapshot: CloseSnapshot,
    producer_deadline: Instant,
}

/// Result returned by the writer task even when its output device fails.
/// Callers must treat `complete == false` or `writer_error.is_some()` as an
/// evidence failure, never as a zero-activity market.
#[derive(Debug, Clone, Serialize)]
pub struct BookTapeSummary {
    pub status: BookTapeStatus,
    pub complete: bool,
    pub reason: String,
    pub frame_rows: u64,
    pub heartbeat_rows: u64,
    pub unwritten_frames: u64,
    pub tap_dropped: u64,
    pub ever_observed_feed: bool,
    pub requested_tickers: u64,
    pub ready_tickers: Vec<String>,
    pub missing_tickers: Vec<String>,
    pub terminal_feed_age_ns: Option<u64>,
    pub terminal_feed_health: BookTapeFeedHealth,
    pub start_wall_ns: u64,
    pub start_mono_ns: u64,
    pub end_wall_ns: u64,
    pub end_mono_ns: u64,
    pub writer_error: Option<String>,
    /// True only after every tracked FrameTap sender has dropped.
    pub producer_quiesced: bool,
    /// False when a live producer made exact accepted/drop accounting
    /// impossible at the shutdown deadline.
    pub terminal_accounting_complete: bool,
    /// The protocol-2 `close` row is a durable PENDING record and always says
    /// `complete = false`. Offline completion requires the separate receipt.
    pub close_written: bool,
    pub committed: bool,
    pub commit_path: String,
}

#[derive(Debug, Clone)]
pub struct BookTapeVerification {
    pub committed: bool,
    pub complete: bool,
    pub status: Option<String>,
    pub reason: String,
}

/// Handle for the dedicated blocking writer task. `request_close` must happen
/// before the caller stops and joins the producer. The writer then observes
/// channel disconnection as its quiescence proof, snapshots the final drop
/// counter, and only then attempts terminal durability.
pub struct BookTapeHandle {
    close_tx: Option<mpsc::Sender<CloseRequest>>,
    join: Option<tokio::task::JoinHandle<BookTapeSummary>>,
    summary: Option<BookTapeSummary>,
    failure_rx: tokio::sync::watch::Receiver<Option<String>>,
    cache: Arc<BookCache>,
    stale_after: Duration,
    producer_quiescence_timeout: Duration,
    start_observation_count: u64,
}

impl BookTapeHandle {
    pub fn request_close(&mut self, reason: BookTapeCloseReason) -> io::Result<()> {
        let sender = self.close_tx.take().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::AlreadyExists,
                "book tape close already requested",
            )
        })?;
        let snapshot = close_snapshot(&self.cache, self.stale_after, self.start_observation_count);
        let request = CloseRequest {
            reason,
            snapshot,
            producer_deadline: Instant::now() + self.producer_quiescence_timeout,
        };
        sender.send(request).map_err(|_| {
            io::Error::new(
                io::ErrorKind::BrokenPipe,
                "book tape writer already stopped",
            )
        })
    }

    /// Subscribe before entering the event wait loop. A write/sync failure is
    /// published immediately even though the writer remains alive long enough
    /// to observe producer quiescence and finalize exact drop accounting.
    pub fn subscribe_failures(&self) -> tokio::sync::watch::Receiver<Option<String>> {
        self.failure_rx.clone()
    }

    pub fn is_finished(&self) -> bool {
        self.summary.is_some() || self.join.as_ref().is_none_or(|join| join.is_finished())
    }

    /// Cancellation-safe: polling this in `tokio::select!` does not detach the
    /// blocking task when another branch wins.
    pub async fn wait(&mut self) -> Result<BookTapeSummary, String> {
        if let Some(summary) = &self.summary {
            return Ok(summary.clone());
        }
        let result = {
            let join = self
                .join
                .as_mut()
                .ok_or_else(|| "book tape join missing".to_string())?;
            (&mut *join).await
        };
        self.join.take();
        match result {
            Ok(summary) => {
                self.summary = Some(summary.clone());
                Ok(summary)
            }
            Err(error) => Err(format!("book tape blocking task failed: {error}")),
        }
    }
}

/// Library-owned raw-book recorder. `start_new` performs every open, write,
/// flush, and sync operation on Tokio's blocking pool. It durably writes the
/// session header before returning the `FrameTap`, so a path collision or
/// header durability failure happens before the feed can start.
pub struct BookTapeTask;

impl BookTapeTask {
    pub async fn start_new(
        path: impl AsRef<Path>,
        session: BookTapeSession,
        cache: Arc<BookCache>,
        tap_capacity: usize,
        options: BookTapeOptions,
    ) -> io::Result<(FrameTap, BookTapeHandle)> {
        validate_tape_options(&session, tap_capacity, &options)?;
        let mut session_tickers = session.tickers.clone();
        session_tickers.sort();
        let mut cache_tickers: Vec<_> = cache.tickers().cloned().collect();
        cache_tickers.sort();
        if session_tickers != cache_tickers {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "book tape session tickers must exactly match the BookCache ticker set",
            ));
        }
        let prepare_path = path.as_ref().to_path_buf();
        let prepared = tokio::task::spawn_blocking(move || prepare_tape(prepare_path, session))
            .await
            .map_err(|error| {
                io::Error::new(
                    io::ErrorKind::Other,
                    format!("book tape preparation task failed: {error}"),
                )
            })??;

        let (tap, rx) = FrameTap::new(tap_capacity);
        let dropped = tap.dropped.clone();
        let producers = Arc::clone(&tap.producers);
        // A pre-existing BookCache is allowed as a capability input, but its
        // earlier socket traffic is not evidence for this new tape session.
        let start_observation_count = cache.observation_count();
        let (close_tx, close_rx) = mpsc::channel();
        let (failure_tx, failure_rx) = tokio::sync::watch::channel(None);
        let requested_tickers = prepared.requested_tickers.clone();
        let worker = BookTapeWorker {
            sink: Box::new(prepared.sink),
            committer: Box::new(prepared.committer),
            cache: cache.clone(),
            rx,
            close_rx,
            dropped,
            producers,
            options: options.clone(),
            start_wall_ns: prepared.start_wall_ns,
            start_mono_ns: prepared.start_mono_ns,
            requested_tickers: requested_tickers.clone(),
            start_observation_count,
            failure_tx,
        };
        let join = tokio::task::spawn_blocking(move || worker.run());
        Ok((
            tap,
            BookTapeHandle {
                close_tx: Some(close_tx),
                join: Some(join),
                summary: None,
                failure_rx,
                cache: Arc::clone(&cache),
                stale_after: options.stale_after,
                producer_quiescence_timeout: options.producer_quiescence_timeout,
                start_observation_count,
            },
        ))
    }
}

fn validate_tape_options(
    session: &BookTapeSession,
    tap_capacity: usize,
    options: &BookTapeOptions,
) -> io::Result<()> {
    if tap_capacity == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "book tape tap capacity must be greater than zero",
        ));
    }
    if options.heartbeat_interval.is_zero()
        || options.flush_interval.is_zero()
        || options.stale_after.is_zero()
        || options.producer_quiescence_timeout.is_zero()
        || options.backlog_drain_timeout.is_zero()
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "book tape heartbeat, flush, stale, producer, and backlog intervals must be greater than zero",
        ));
    }
    if session
        .clock_offset_s
        .is_some_and(|value| !value.is_finite())
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "book tape clock offset must be finite when present",
        ));
    }
    if session.tickers.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "book tape requires at least one requested ticker",
        ));
    }
    let mut unique = session.tickers.clone();
    unique.sort();
    unique.dedup();
    if unique.len() != session.tickers.len() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "book tape requested tickers must be unique",
        ));
    }
    Ok(())
}

trait TapeSink: Send {
    fn write_row(&mut self, value: &Value) -> io::Result<()>;
    fn sync_data(&mut self) -> io::Result<()>;
    fn sync_all(&mut self) -> io::Result<()>;
    fn fingerprint(&mut self) -> io::Result<TapeFingerprint>;
}

struct FileTapeSink {
    writer: BufWriter<File>,
    path: PathBuf,
}

impl TapeSink for FileTapeSink {
    fn write_row(&mut self, value: &Value) -> io::Result<()> {
        write_json_line(&mut self.writer, value)
    }

    fn sync_data(&mut self) -> io::Result<()> {
        self.writer.flush()?;
        self.writer.get_ref().sync_data()
    }

    fn sync_all(&mut self) -> io::Result<()> {
        self.writer.flush()?;
        self.writer.get_ref().sync_all()
    }

    fn fingerprint(&mut self) -> io::Result<TapeFingerprint> {
        self.writer.flush()?;
        fingerprint_reader(File::open(&self.path)?)
    }
}

#[derive(Debug, Clone)]
struct TapeFingerprint {
    bytes: u64,
    sha256: String,
}

trait TapeCommitter: Send {
    fn commit(&mut self, receipt: &Value) -> io::Result<()>;
    fn final_path(&self) -> &Path;
}

struct FileTapeCommitter {
    final_path: PathBuf,
    pending_path: PathBuf,
}

impl TapeCommitter for FileTapeCommitter {
    fn commit(&mut self, receipt: &Value) -> io::Result<()> {
        let mut pending = BufWriter::new(
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&self.pending_path)?,
        );
        write_json_line(&mut pending, receipt)?;
        pending.flush()?;
        pending.get_ref().sync_all()?;
        drop(pending);

        // hard_link is the exclusive, atomic publication step: unlike rename
        // on Unix it refuses an existing final receipt instead of replacing it.
        std::fs::hard_link(&self.pending_path, &self.final_path)?;
        if let Err(error) = sync_parent_dir(&self.final_path) {
            let _ = std::fs::remove_file(&self.final_path);
            let _ = std::fs::remove_file(&self.pending_path);
            let _ = sync_parent_dir(&self.final_path);
            return Err(error);
        }
        if let Err(error) = std::fs::remove_file(&self.pending_path) {
            tracing::warn!(
                path = %self.pending_path.display(),
                %error,
                "durable tape commit published but pending link remains"
            );
        }
        Ok(())
    }

    fn final_path(&self) -> &Path {
        &self.final_path
    }
}

struct PreparedTape {
    sink: FileTapeSink,
    committer: FileTapeCommitter,
    start_wall_ns: u64,
    start_mono_ns: u64,
    requested_tickers: Vec<String>,
}

fn prepare_tape(path: PathBuf, session: BookTapeSession) -> io::Result<PreparedTape> {
    let final_path = book_tape_commit_path(&path);
    let pending_path = book_tape_pending_commit_path(&path);
    if final_path.exists() || pending_path.exists() {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "book tape commit receipt path already exists",
        ));
    }
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)?;
    let mut sink = FileTapeSink {
        writer: BufWriter::new(file),
        path: path.clone(),
    };
    let start_wall_ns = wall_ns_now();
    let start_mono_ns = MonotonicNs::now().0;
    let requested_tickers = session.tickers.clone();
    sink.write_row(&json!({
        "t": "session",
        "schema_version": BOOK_TAPE_ENVELOPE_SCHEMA_VERSION,
        "book_tape_protocol_version": BOOK_TAPE_SCHEMA_VERSION,
        "capabilities": BOOK_TAPE_CAPABILITIES,
        "venue": "kalshi",
        "feed_kind": "push",
        "endpoint": session.endpoint,
        "channel": "orderbook_delta",
        "event": session.event,
        "label": session.label,
        "tickers": session.tickers,
        "host": session.host,
        "git_sha": session.git_sha,
        "clock_offset_s": session.clock_offset_s,
        "start_wall_ns": start_wall_ns,
        "start_mono_ns": start_mono_ns,
        "planned_secs": session.planned_secs,
        "commit_receipt": final_path,
    }))?;
    sink.sync_all()?;
    sync_parent_dir(&path)?;
    Ok(PreparedTape {
        sink,
        committer: FileTapeCommitter {
            final_path,
            pending_path,
        },
        start_wall_ns,
        start_mono_ns,
        requested_tickers,
    })
}

struct BookTapeWorker {
    sink: Box<dyn TapeSink>,
    committer: Box<dyn TapeCommitter>,
    cache: Arc<BookCache>,
    rx: mpsc::Receiver<RawFrame>,
    close_rx: mpsc::Receiver<CloseRequest>,
    dropped: Arc<AtomicU64>,
    producers: Arc<FrameTapProducerState>,
    options: BookTapeOptions,
    start_wall_ns: u64,
    start_mono_ns: u64,
    requested_tickers: Vec<String>,
    start_observation_count: u64,
    failure_tx: tokio::sync::watch::Sender<Option<String>>,
}

impl BookTapeWorker {
    fn run(mut self) -> BookTapeSummary {
        let mut frame_rows = 0u64;
        let mut heartbeat_rows = 0u64;
        let mut unwritten_frames = 0u64;
        let mut earliest_frame_mono_ns = None::<u64>;
        let mut snapshot_proofs = HashMap::<String, u64>::new();
        let mut writer_error = None;
        let mut close_request = None;
        let mut control_disconnected = false;
        let mut producer_timed_out = false;
        let mut backlog_timed_out = false;
        let mut producer_quiesced = false;
        let mut terminal_accounting_complete = false;
        let mut next_heartbeat = Instant::now() + self.options.heartbeat_interval;
        let mut next_sync = Instant::now() + self.options.flush_interval;

        loop {
            self.poll_close(&mut close_request, &mut control_disconnected);
            let producer_state = self.producers.snapshot();
            let now = Instant::now();
            if let Some(request) = &close_request {
                if producer_state.active > 0 && now >= request.producer_deadline {
                    producer_timed_out = true;
                    self.record_writer_error(
                        &mut writer_error,
                        "producer quiescence timeout: FrameTap sender still alive".to_string(),
                    );
                    break;
                }
                if producer_state.active == 0
                    && producer_state
                        .zero_since
                        .is_some_and(|zero_since| zero_since > request.producer_deadline)
                {
                    // The writer may have been inside a blocking I/O call at
                    // the producer deadline. Preserve the historical miss:
                    // a sender that dropped late must not become "on time"
                    // merely because we observed its drop later. With zero
                    // producers the remaining accepted backlog is finite, so
                    // account it exactly before returning without a commit.
                    unwritten_frames =
                        unwritten_frames.saturating_add(self.rx.try_iter().count() as u64);
                    producer_timed_out = true;
                    producer_quiesced = true;
                    terminal_accounting_complete = true;
                    self.record_writer_error(
                        &mut writer_error,
                        "producer quiescence timeout: last FrameTap sender dropped after deadline"
                            .to_string(),
                    );
                    break;
                }
            }
            if producer_state.active == 0
                && producer_state.zero_since.is_some_and(|zero_since| {
                    now >= zero_since + self.options.backlog_drain_timeout
                })
            {
                match self.rx.try_recv() {
                    Ok(_frame) => {
                        let remaining = 1u64.saturating_add(self.rx.try_iter().count() as u64);
                        unwritten_frames = unwritten_frames.saturating_add(remaining);
                        backlog_timed_out = true;
                        producer_quiesced = true;
                        terminal_accounting_complete = true;
                        self.record_writer_error(
                            &mut writer_error,
                            format!(
                                "backlog drain timeout: {remaining} accepted frame(s) remained unwritten"
                            ),
                        );
                        break;
                    }
                    Err(TryRecvError::Disconnected) => {
                        producer_quiesced = true;
                        terminal_accounting_complete = true;
                        break;
                    }
                    Err(TryRecvError::Empty) => {}
                }
            }

            if writer_error.is_none() && Instant::now() >= next_heartbeat {
                let through_mono_ns = close_request
                    .as_ref()
                    .map_or_else(|| MonotonicNs::now().0, |request| request.snapshot.mono_ns);
                let readiness =
                    proven_readiness(&self.requested_tickers, &snapshot_proofs, through_mono_ns);
                let ever_observed_feed = self.observed_after_start()
                    || earliest_frame_mono_ns.is_some_and(|mono_ns| mono_ns <= through_mono_ns);
                let row = self.heartbeat_row(
                    frame_rows,
                    heartbeat_rows + 1,
                    ever_observed_feed,
                    &readiness,
                    through_mono_ns,
                );
                match self.sink.write_row(&row) {
                    Ok(()) => heartbeat_rows += 1,
                    Err(error) => self.record_writer_error(
                        &mut writer_error,
                        format!("heartbeat write: {error}"),
                    ),
                }
                next_heartbeat = Instant::now() + self.options.heartbeat_interval;
            }
            if writer_error.is_none() && Instant::now() >= next_sync {
                if let Err(error) = self.sink.sync_data() {
                    self.record_writer_error(
                        &mut writer_error,
                        format!("periodic sync_data: {error}"),
                    );
                }
                next_sync = Instant::now() + self.options.flush_interval;
            }

            let mut wait = if writer_error.is_some() {
                BOOK_TAPE_CONTROL_POLL
            } else {
                BOOK_TAPE_CONTROL_POLL
                    .min(next_heartbeat.saturating_duration_since(Instant::now()))
                    .min(next_sync.saturating_duration_since(Instant::now()))
            };
            let deadline = if producer_state.active == 0 {
                producer_state
                    .zero_since
                    .map(|zero_since| zero_since + self.options.backlog_drain_timeout)
            } else {
                close_request
                    .as_ref()
                    .map(|request| request.producer_deadline)
            };
            if let Some(deadline) = deadline {
                wait = wait.min(deadline.saturating_duration_since(Instant::now()));
            }
            match self.rx.recv_timeout(wait) {
                Ok(_frame) if writer_error.is_some() => unwritten_frames += 1,
                Ok(frame) => match self.write_frame(&frame) {
                    Ok(()) => {
                        frame_rows += 1;
                        earliest_frame_mono_ns = Some(
                            earliest_frame_mono_ns
                                .map_or(frame.mono_ns, |current| current.min(frame.mono_ns)),
                        );
                        if let Some(ticker) = valid_snapshot_ticker(&frame.text) {
                            if self
                                .requested_tickers
                                .iter()
                                .any(|requested| requested == &ticker)
                            {
                                snapshot_proofs
                                    .entry(ticker)
                                    .and_modify(|mono_ns| *mono_ns = (*mono_ns).min(frame.mono_ns))
                                    .or_insert(frame.mono_ns);
                            }
                        }
                    }
                    Err(error) => {
                        unwritten_frames += 1;
                        self.record_writer_error(
                            &mut writer_error,
                            format!("frame write: {error}"),
                        );
                    }
                },
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => {
                    // SyncSender disconnect is the producer-quiescence proof:
                    // every queued frame was returned before this branch.
                    self.poll_close(&mut close_request, &mut control_disconnected);
                    producer_quiesced = true;
                    terminal_accounting_complete = true;
                    break;
                }
            }
        }

        let requested_close = close_request.is_some();
        let terminal_snapshot = close_request
            .as_ref()
            .map(|request| request.snapshot.clone())
            .unwrap_or_else(|| {
                close_snapshot(
                    &self.cache,
                    self.options.stale_after,
                    self.start_observation_count,
                )
            });
        let reason = if producer_timed_out {
            "producer_quiescence_timeout".to_string()
        } else if backlog_timed_out {
            "backlog_drain_timeout".to_string()
        } else {
            close_request.as_ref().map_or_else(
                || {
                    if control_disconnected {
                        "shutdown_channel_closed".to_string()
                    } else {
                        "feed_closed".to_string()
                    }
                },
                |request| request.reason.as_str().to_string(),
            )
        };
        let terminal_feed_age_ns = terminal_snapshot.transport_age_ns;
        let terminal_feed_health = terminal_snapshot.transport_health;
        let readiness = proven_readiness(
            &self.requested_tickers,
            &snapshot_proofs,
            terminal_snapshot.mono_ns,
        );
        let ever_observed_feed = terminal_snapshot.observed_after_start
            || earliest_frame_mono_ns.is_some_and(|mono_ns| mono_ns <= terminal_snapshot.mono_ns);
        let end_wall_ns = wall_ns_now();
        let end_mono_ns = MonotonicNs::now().0;
        let tap_dropped = self.dropped.load(Ordering::Relaxed);
        let candidate_complete = requested_close
            && writer_error.is_none()
            && producer_quiesced
            && terminal_accounting_complete
            && tap_dropped == 0
            && unwritten_frames == 0
            && ever_observed_feed
            && readiness.all_ready()
            && terminal_feed_health == BookTapeFeedHealth::Healthy;
        let candidate_status = if writer_error.is_some() {
            BookTapeStatus::WriterError
        } else if candidate_complete {
            BookTapeStatus::Closed
        } else {
            BookTapeStatus::Partial
        };

        let mut close_written = false;
        let mut committed = false;
        if writer_error.is_none() {
            let pending = self.pending_close_row(
                candidate_status,
                candidate_complete,
                &reason,
                frame_rows,
                heartbeat_rows,
                unwritten_frames,
                tap_dropped,
                ever_observed_feed,
                producer_quiesced,
                terminal_accounting_complete,
                &readiness,
                terminal_feed_age_ns,
                terminal_feed_health,
                terminal_snapshot.wall_ns,
                terminal_snapshot.mono_ns,
                end_wall_ns,
                end_mono_ns,
            );
            match self
                .sink
                .write_row(&pending)
                .and_then(|_| self.sink.sync_all())
            {
                Ok(()) => close_written = true,
                Err(error) => self.record_writer_error(
                    &mut writer_error,
                    format!("pending close sync_all: {error}"),
                ),
            }
        }
        let mut tape_fingerprint = None;
        if close_written && writer_error.is_none() {
            match self.sink.fingerprint() {
                Ok(fingerprint) => tape_fingerprint = Some(fingerprint),
                Err(error) => self
                    .record_writer_error(&mut writer_error, format!("tape fingerprint: {error}")),
            }
        }
        if close_written && writer_error.is_none() {
            let fingerprint = tape_fingerprint
                .as_ref()
                .expect("fingerprint exists when terminal I/O has no error");
            let receipt = json!({
                "t": "commit",
                "schema_version": BOOK_TAPE_SCHEMA_VERSION,
                "book_tape_protocol_version": BOOK_TAPE_SCHEMA_VERSION,
                "capabilities": BOOK_TAPE_CAPABILITIES,
                "durable": true,
                "tape_bytes": fingerprint.bytes,
                "tape_sha256": fingerprint.sha256,
                "status": candidate_status,
                "complete": candidate_complete,
                "reason": reason,
                "frame_rows": frame_rows,
                "heartbeat_rows": heartbeat_rows,
                "unwritten_frames": unwritten_frames,
                "tap_dropped": tap_dropped,
                "ever_observed_feed": ever_observed_feed,
                "producer_quiesced": producer_quiesced,
                "terminal_accounting_complete": terminal_accounting_complete,
                "requested_tickers": self.requested_tickers,
                "ready_tickers": readiness.ready,
                "missing_tickers": readiness.missing,
                "terminal_feed_age_ns": terminal_feed_age_ns,
                "terminal_feed_health": terminal_feed_health,
                "close_wall_ns": terminal_snapshot.wall_ns,
                "close_mono_ns": terminal_snapshot.mono_ns,
                "start_wall_ns": self.start_wall_ns,
                "start_mono_ns": self.start_mono_ns,
                "end_wall_ns": end_wall_ns,
                "end_mono_ns": end_mono_ns,
            });
            match self.committer.commit(&receipt) {
                Ok(()) => committed = true,
                Err(error) => self.record_writer_error(
                    &mut writer_error,
                    format!("commit receipt sync_all: {error}"),
                ),
            }
        }

        BookTapeSummary {
            status: if writer_error.is_some() {
                BookTapeStatus::WriterError
            } else {
                candidate_status
            },
            complete: committed && candidate_complete && writer_error.is_none(),
            reason,
            frame_rows,
            heartbeat_rows,
            unwritten_frames,
            tap_dropped,
            ever_observed_feed,
            requested_tickers: self.requested_tickers.len() as u64,
            ready_tickers: readiness.ready,
            missing_tickers: readiness.missing,
            terminal_feed_age_ns,
            terminal_feed_health,
            start_wall_ns: self.start_wall_ns,
            start_mono_ns: self.start_mono_ns,
            end_wall_ns,
            end_mono_ns,
            writer_error,
            producer_quiesced,
            terminal_accounting_complete,
            close_written,
            committed,
            commit_path: self.committer.final_path().display().to_string(),
        }
    }

    fn poll_close(&self, close_request: &mut Option<CloseRequest>, disconnected: &mut bool) {
        loop {
            match self.close_rx.try_recv() {
                Ok(request) => {
                    if close_request.is_none() {
                        *close_request = Some(request);
                    }
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => {
                    *disconnected = true;
                    break;
                }
            }
        }
    }

    fn record_writer_error(&self, target: &mut Option<String>, error: String) {
        if target.is_none() {
            self.failure_tx.send_replace(Some(error.clone()));
            *target = Some(error);
        }
    }

    fn write_frame(&mut self, frame: &RawFrame) -> io::Result<()> {
        self.sink.write_row(&json!({
            "t": "frame",
            "wall_ns": frame.wall_ns,
            "mono_ns": frame.mono_ns,
            "raw": frame.text,
        }))
    }

    fn heartbeat_row(
        &self,
        frame_rows: u64,
        heartbeat_rows: u64,
        ever_observed_feed: bool,
        readiness: &BookTapeReadiness,
        readiness_through_mono_ns: u64,
    ) -> Value {
        let (feed_age_ns, feed_health) = self.feed_health();
        json!({
            "t": "heartbeat",
            "wall_ns": wall_ns_now(),
            "mono_ns": MonotonicNs::now().0,
            "feed_age_ns": feed_age_ns,
            "feed_health": feed_health,
            "ever_observed_feed": ever_observed_feed,
            "readiness_through_mono_ns": readiness_through_mono_ns,
            "requested_tickers": self.requested_tickers,
            "ready_tickers": readiness.ready,
            "missing_tickers": readiness.missing,
            "frame_rows": frame_rows,
            "heartbeat_rows": heartbeat_rows,
            "tap_dropped": self.dropped.load(Ordering::Relaxed),
            "writer_status": "open",
            "writer_error": Value::Null,
        })
    }

    #[allow(clippy::too_many_arguments)]
    fn pending_close_row(
        &self,
        candidate_status: BookTapeStatus,
        candidate_complete: bool,
        reason: &str,
        frame_rows: u64,
        heartbeat_rows: u64,
        unwritten_frames: u64,
        tap_dropped: u64,
        ever_observed_feed: bool,
        producer_quiesced: bool,
        terminal_accounting_complete: bool,
        readiness: &BookTapeReadiness,
        terminal_feed_age_ns: Option<u64>,
        terminal_feed_health: BookTapeFeedHealth,
        close_wall_ns: u64,
        close_mono_ns: u64,
        end_wall_ns: u64,
        end_mono_ns: u64,
    ) -> Value {
        json!({
            "t": "close",
            "status": "pending",
            "candidate_status": candidate_status,
            // Never claim completion in the tape itself. The separately
            // fsynced and atomically published receipt is the commit point.
            "complete": false,
            "candidate_complete": candidate_complete,
            "commit_required": true,
            "reason": reason,
            // Keep the original schema-1 names for existing readers.
            "rows": frame_rows,
            "dropped": tap_dropped,
            "frame_rows": frame_rows,
            "heartbeat_rows": heartbeat_rows,
            "unwritten_frames": unwritten_frames,
            "tap_dropped": tap_dropped,
            "ever_observed_feed": ever_observed_feed,
            "producer_quiesced": producer_quiesced,
            "terminal_accounting_complete": terminal_accounting_complete,
            "requested_tickers": self.requested_tickers,
            "ready_tickers": readiness.ready,
            "missing_tickers": readiness.missing,
            "start_wall_ns": self.start_wall_ns,
            "start_mono_ns": self.start_mono_ns,
            "end_wall_ns": end_wall_ns,
            "end_mono_ns": end_mono_ns,
            "close_wall_ns": close_wall_ns,
            "close_mono_ns": close_mono_ns,
            "feed_age_ns": terminal_feed_age_ns,
            "feed_health": terminal_feed_health,
            "writer_status": "pending_commit",
            "writer_error": Value::Null,
        })
    }

    fn feed_health(&self) -> (Option<u64>, BookTapeFeedHealth) {
        transport_health(&self.cache, self.options.stale_after)
    }

    fn observed_after_start(&self) -> bool {
        self.cache.observation_count() > self.start_observation_count
    }
}

fn transport_health(cache: &BookCache, stale_after: Duration) -> (Option<u64>, BookTapeFeedHealth) {
    let age = cache.feed_age_ns();
    if age == u64::MAX {
        (None, BookTapeFeedHealth::Unknown)
    } else if age <= stale_after.as_nanos() as u64 {
        (Some(age), BookTapeFeedHealth::Healthy)
    } else {
        (Some(age), BookTapeFeedHealth::Stale)
    }
}

fn close_snapshot(
    cache: &BookCache,
    stale_after: Duration,
    start_observation_count: u64,
) -> CloseSnapshot {
    let observed_after_start = cache.observation_count() > start_observation_count;
    let (transport_age_ns, transport_health) = transport_health(cache, stale_after);
    CloseSnapshot {
        wall_ns: wall_ns_now(),
        mono_ns: MonotonicNs::now().0,
        transport_age_ns,
        transport_health,
        observed_after_start,
    }
}

pub fn book_tape_commit_path(path: impl AsRef<Path>) -> PathBuf {
    path_with_suffix(path.as_ref(), ".commit.json")
}

fn book_tape_pending_commit_path(path: &Path) -> PathBuf {
    path_with_suffix(path, ".commit.pending")
}

fn path_with_suffix(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_os_string();
    name.push(suffix);
    PathBuf::from(name)
}

fn sync_parent_dir(path: &Path) -> io::Result<()> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    File::open(parent)?.sync_all()
}

fn fingerprint_reader(mut reader: impl Read) -> io::Result<TapeFingerprint> {
    let mut hasher = Sha256::new();
    let mut bytes = 0u64;
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let read = reader.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
        bytes = bytes
            .checked_add(read as u64)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "tape length overflow"))?;
    }
    let digest = hasher.finalize();
    let sha256 = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    Ok(TapeFingerprint { bytes, sha256 })
}

/// Offline verifier for the protocol-2 two-phase terminal protocol. A tape is
/// complete only when its last close row is explicitly pending and a valid,
/// byte-bound, atomically published commit receipt exists beside it.
pub fn verify_book_tape(path: impl AsRef<Path>) -> io::Result<BookTapeVerification> {
    let path = path.as_ref();
    let fingerprint = fingerprint_reader(File::open(path)?)?;
    let file = File::open(path)?;
    let mut session = None;
    let mut close = None;
    let mut frame_rows = 0u64;
    let mut heartbeat_rows = 0u64;
    let mut last_heartbeat_dropped = 0u64;
    let mut snapshot_proofs = HashMap::<String, u64>::new();
    let mut logical_index = 0usize;
    let mut last_row_type = None;
    for (physical_index, line) in BufReader::new(file).lines().enumerate() {
        let line = line?;
        if line.trim().is_empty() {
            return Err(invalid_tape(format!(
                "blank tape row on line {}",
                physical_index + 1
            )));
        }
        let row: Value = serde_json::from_str(&line).map_err(|error| {
            invalid_tape(format!(
                "invalid tape JSON on line {}: {error}",
                physical_index + 1
            ))
        })?;
        let row_type = row["t"].as_str().ok_or_else(|| {
            invalid_tape(format!("tape row {} has no string t", physical_index + 1))
        })?;
        last_row_type = Some(row_type.to_string());
        match row_type {
            "session" => {
                if logical_index != 0 || session.is_some() {
                    return Err(invalid_tape("session must appear exactly once and first"));
                }
                validate_session_row(&row)?;
                session = Some(row);
            }
            "frame" => {
                if session.is_none() || close.is_some() {
                    return Err(invalid_tape("frame outside session/close boundaries"));
                }
                let mono_ns = row["mono_ns"].as_u64();
                let raw = row["raw"].as_str();
                if row["wall_ns"].as_u64().is_none() || mono_ns.is_none() || raw.is_none() {
                    return Err(invalid_tape("frame row has invalid clocks or raw payload"));
                }
                frame_rows += 1;
                if let Some(ticker) = valid_snapshot_ticker(raw.unwrap()) {
                    let requested = json_string_vec(session.as_ref().unwrap(), "tickers")?;
                    if requested.iter().any(|requested| requested == &ticker) {
                        snapshot_proofs
                            .entry(ticker)
                            .and_modify(|proof_mono_ns| {
                                *proof_mono_ns = (*proof_mono_ns).min(mono_ns.unwrap())
                            })
                            .or_insert(mono_ns.unwrap());
                    }
                }
            }
            "heartbeat" => {
                if session.is_none() || close.is_some() {
                    return Err(invalid_tape("heartbeat outside session/close boundaries"));
                }
                heartbeat_rows += 1;
                if row["heartbeat_rows"].as_u64() != Some(heartbeat_rows)
                    || row["frame_rows"].as_u64() != Some(frame_rows)
                {
                    return Err(invalid_tape(
                        "heartbeat row counters do not match tape prefix",
                    ));
                }
                let dropped = row["tap_dropped"]
                    .as_u64()
                    .ok_or_else(|| invalid_tape("heartbeat tap_dropped is not u64"))?;
                if dropped < last_heartbeat_dropped {
                    return Err(invalid_tape("heartbeat drop count moved backwards"));
                }
                last_heartbeat_dropped = dropped;
                let requested = json_string_vec(session.as_ref().unwrap(), "tickers")?;
                let through_mono_ns = row["readiness_through_mono_ns"]
                    .as_u64()
                    .ok_or_else(|| invalid_tape("heartbeat readiness boundary is not u64"))?;
                let expected = proven_readiness(&requested, &snapshot_proofs, through_mono_ns);
                validate_exact_readiness(&row, &requested, &expected)?;
            }
            "close" => {
                if session.is_none() || close.is_some() {
                    return Err(invalid_tape("close must appear exactly once"));
                }
                close = Some(row);
            }
            other => return Err(invalid_tape(format!("unknown tape row type {other:?}"))),
        }
        logical_index += 1;
    }

    let session = session.ok_or_else(|| invalid_tape("tape has no session row"))?;
    let close = close.ok_or_else(|| invalid_tape("tape has no pending close row"))?;
    if last_row_type.as_deref() != Some("close") {
        return Err(invalid_tape("pending close is not the final tape row"));
    }
    let requested = json_string_vec(&session, "tickers")?;
    let close_mono_ns = close["close_mono_ns"]
        .as_u64()
        .ok_or_else(|| invalid_tape("pending close monotonic boundary is not u64"))?;
    let terminal_readiness = proven_readiness(&requested, &snapshot_proofs, close_mono_ns);
    validate_close_row(
        &close,
        &session,
        frame_rows,
        heartbeat_rows,
        last_heartbeat_dropped,
        &terminal_readiness,
    )?;

    let commit_path = book_tape_commit_path(path);
    let receipt_text = match std::fs::read_to_string(&commit_path) {
        Ok(text) => text,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Ok(BookTapeVerification {
                committed: false,
                complete: false,
                status: close["candidate_status"].as_str().map(str::to_string),
                reason: "commit_receipt_missing".to_string(),
            });
        }
        Err(error) => return Err(error),
    };
    let mut receipt_rows = receipt_text.lines();
    let receipt_line = receipt_rows.next().ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidData, "empty book tape commit receipt")
    })?;
    if receipt_line.trim().is_empty() {
        return Err(invalid_tape("blank book tape commit receipt row"));
    }
    let receipt: Value = serde_json::from_str(receipt_line)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    if receipt_rows.next().is_some() {
        return Err(invalid_tape(
            "book tape commit receipt has more than one row",
        ));
    }
    if close["status"] != "pending"
        || close["complete"] != false
        || close["commit_required"] != true
        || receipt["t"] != "commit"
        || receipt["schema_version"] != BOOK_TAPE_SCHEMA_VERSION
        || receipt["book_tape_protocol_version"] != BOOK_TAPE_SCHEMA_VERSION
        || receipt["capabilities"] != json!(BOOK_TAPE_CAPABILITIES)
        || receipt["durable"] != true
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "book tape terminal protocol fields are invalid",
        ));
    }
    if receipt["tape_bytes"].as_u64() != Some(fingerprint.bytes)
        || receipt["tape_sha256"].as_str() != Some(fingerprint.sha256.as_str())
    {
        return Err(invalid_tape(
            "book tape byte length or SHA-256 does not match commit",
        ));
    }
    for field in [
        "status",
        "reason",
        "frame_rows",
        "heartbeat_rows",
        "unwritten_frames",
        "tap_dropped",
        "ever_observed_feed",
        "producer_quiesced",
        "terminal_accounting_complete",
        "requested_tickers",
        "ready_tickers",
        "missing_tickers",
        "terminal_feed_age_ns",
        "terminal_feed_health",
        "close_wall_ns",
        "close_mono_ns",
        "start_wall_ns",
        "start_mono_ns",
        "end_wall_ns",
        "end_mono_ns",
    ] {
        let close_field = match field {
            "status" => "candidate_status",
            "terminal_feed_age_ns" => "feed_age_ns",
            "terminal_feed_health" => "feed_health",
            other => other,
        };
        if receipt[field] != close[close_field] {
            return Err(invalid_tape(format!(
                "book tape commit mismatch for {field}"
            )));
        }
    }
    if receipt["complete"] != close["candidate_complete"] {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "book tape commit completeness mismatch",
        ));
    }
    let status = receipt["status"].as_str().map(str::to_string);
    let ready = json_string_vec(&close, "ready_tickers")?;
    let missing = json_string_vec(&close, "missing_tickers")?;
    let recomputed_complete = status.as_deref() == Some("closed")
        && close["candidate_status"] == "closed"
        && close["candidate_complete"] == true
        && close["tap_dropped"] == 0
        && close["unwritten_frames"] == 0
        && close["ever_observed_feed"] == true
        && close["producer_quiesced"] == true
        && close["terminal_accounting_complete"] == true
        && missing.is_empty()
        && ready.len() == requested.len()
        && close["feed_health"] == "healthy";
    if receipt["complete"].as_bool().unwrap_or(false) != recomputed_complete {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "book tape commit claims completeness without satisfying terminal gates",
        ));
    }
    Ok(BookTapeVerification {
        committed: true,
        complete: recomputed_complete,
        status,
        reason: receipt["reason"].as_str().unwrap_or("unknown").to_string(),
    })
}

fn validate_session_row(row: &Value) -> io::Result<()> {
    if row["schema_version"] != BOOK_TAPE_ENVELOPE_SCHEMA_VERSION
        || row["book_tape_protocol_version"] != BOOK_TAPE_SCHEMA_VERSION
        || row["capabilities"] != json!(BOOK_TAPE_CAPABILITIES)
        || row["venue"] != "kalshi"
        || row["feed_kind"] != "push"
        || row["channel"] != "orderbook_delta"
    {
        return Err(invalid_tape(
            "unsupported tape schema, protocol, or capabilities",
        ));
    }
    let tickers = json_string_vec(row, "tickers")?;
    if tickers.is_empty() {
        return Err(invalid_tape("session has no requested tickers"));
    }
    let mut unique = tickers.clone();
    unique.sort();
    unique.dedup();
    if unique.len() != tickers.len() {
        return Err(invalid_tape("session requested tickers are not unique"));
    }
    Ok(())
}

fn validate_close_row(
    close: &Value,
    session: &Value,
    frame_rows: u64,
    heartbeat_rows: u64,
    last_heartbeat_dropped: u64,
    expected_readiness: &BookTapeReadiness,
) -> io::Result<()> {
    if close["status"] != "pending"
        || close["complete"] != false
        || close["commit_required"] != true
        || close["frame_rows"].as_u64() != Some(frame_rows)
        || close["rows"].as_u64() != Some(frame_rows)
        || close["heartbeat_rows"].as_u64() != Some(heartbeat_rows)
        || close["producer_quiesced"] != true
        || close["terminal_accounting_complete"] != true
        || close["tap_dropped"].as_u64() != close["dropped"].as_u64()
        || close["tap_dropped"]
            .as_u64()
            .is_none_or(|dropped| dropped < last_heartbeat_dropped)
    {
        return Err(invalid_tape(
            "pending close counters or protocol fields are invalid",
        ));
    }
    let requested = json_string_vec(session, "tickers")?;
    if json_string_vec(close, "requested_tickers")? != requested {
        return Err(invalid_tape(
            "pending close requested tickers differ from session",
        ));
    }
    validate_exact_readiness(close, &requested, expected_readiness)
}

fn validate_exact_readiness(
    row: &Value,
    requested: &[String],
    expected: &BookTapeReadiness,
) -> io::Result<()> {
    validate_row_ticker_partition(row, requested)?;
    if json_string_vec(row, "ready_tickers")? != expected.ready
        || json_string_vec(row, "missing_tickers")? != expected.missing
    {
        return Err(invalid_tape(
            "ready/missing tickers are not proven by accepted snapshot frames",
        ));
    }
    Ok(())
}

fn validate_row_ticker_partition(row: &Value, requested: &[String]) -> io::Result<()> {
    if json_string_vec(row, "requested_tickers")? != requested {
        return Err(invalid_tape("row requested tickers differ from session"));
    }
    let ready = json_string_vec(row, "ready_tickers")?;
    let missing = json_string_vec(row, "missing_tickers")?;
    let mut union = ready.clone();
    union.extend(missing.iter().cloned());
    let mut sorted_requested = requested.to_vec();
    union.sort();
    sorted_requested.sort();
    if union != sorted_requested {
        return Err(invalid_tape(
            "ready/missing tickers do not partition requested tickers",
        ));
    }
    let mut unique = union;
    unique.dedup();
    if unique.len() != requested.len() {
        return Err(invalid_tape(
            "ready/missing ticker partition contains duplicates",
        ));
    }
    Ok(())
}

fn json_string_vec(row: &Value, field: &str) -> io::Result<Vec<String>> {
    row[field]
        .as_array()
        .ok_or_else(|| invalid_tape(format!("{field} is not an array")))?
        .iter()
        .map(|value| {
            value
                .as_str()
                .map(str::to_string)
                .ok_or_else(|| invalid_tape(format!("{field} contains a non-string")))
        })
        .collect()
}

fn invalid_tape(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

fn write_json_line(writer: &mut impl Write, value: &Value) -> io::Result<()> {
    serde_json::to_writer(&mut *writer, value)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    writer.write_all(b"\n")
}

/// A recording tap on the book feed.
///
/// `try_send` only: a slow or wedged recorder must NEVER stall the book feed
/// that the executor reads. Overflow increments `dropped` instead of blocking,
/// so a truncated tape is always visible as a number rather than as silence.
pub struct FrameTap {
    tx: Option<mpsc::SyncSender<RawFrame>>,
    pub dropped: Arc<AtomicU64>,
    producers: Arc<FrameTapProducerState>,
}

#[derive(Debug, Clone, Copy)]
struct FrameTapProducerSnapshot {
    active: u64,
    zero_since: Option<Instant>,
}

#[derive(Debug)]
struct FrameTapProducerState {
    state: std::sync::Mutex<FrameTapProducerSnapshot>,
}

impl FrameTapProducerState {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            state: std::sync::Mutex::new(FrameTapProducerSnapshot {
                active: 1,
                zero_since: None,
            }),
        })
    }

    fn register(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.active = state
            .active
            .checked_add(1)
            .expect("FrameTap producer count overflow");
        state.zero_since = None;
    }

    fn unregister(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        debug_assert!(state.active > 0, "FrameTap producer count underflow");
        state.active = state.active.saturating_sub(1);
        if state.active == 0 {
            state.zero_since = Some(Instant::now());
        }
    }

    fn snapshot(&self) -> FrameTapProducerSnapshot {
        *self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl Clone for FrameTap {
    fn clone(&self) -> Self {
        let tx = self
            .tx
            .as_ref()
            .expect("live FrameTap has a sender")
            .clone();
        self.producers.register();
        Self {
            tx: Some(tx),
            dropped: Arc::clone(&self.dropped),
            producers: Arc::clone(&self.producers),
        }
    }
}

impl Drop for FrameTap {
    fn drop(&mut self) {
        // Disconnect the channel first. Once `active == 0`, the worker may
        // safely drain every remaining accepted frame without future sends.
        drop(self.tx.take());
        self.producers.unregister();
    }
}

impl FrameTap {
    pub fn new(capacity: usize) -> (Self, mpsc::Receiver<RawFrame>) {
        assert!(capacity > 0, "frame tap capacity must be greater than zero");
        let (tx, rx) = mpsc::sync_channel(capacity);
        let producers = FrameTapProducerState::new();
        (
            Self {
                tx: Some(tx),
                dropped: Arc::new(AtomicU64::new(0)),
                producers,
            },
            rx,
        )
    }

    /// Offer an already-stamped frame without blocking. This is the public
    /// injection seam for tests and alternate feed adapters; keeping the
    /// sender private prevents untracked clones from defeating quiescence.
    pub fn try_offer_frame(&self, frame: RawFrame) -> bool {
        let sent = self
            .tx
            .as_ref()
            .expect("live FrameTap has a sender")
            .try_send(frame)
            .is_ok();
        if !sent {
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
        sent
    }

    fn offer(&self, text: &str) {
        let frame = RawFrame {
            wall_ns: wall_ns_now(),
            mono_ns: MonotonicNs::now().0,
            text: text.to_string(),
        };
        self.try_offer_frame(frame);
    }
}

/// Wall-clock nanoseconds since the Unix epoch. Distinct from
/// `clock::MonotonicNs`, which is the ONLY clock allowed for latency inside
/// the runtime; this one exists solely so a recorded frame can be joined
/// against another venue's tape after the fact.
fn wall_ns_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0)
}

/// WS reader with reconnect. Runs until aborted. On any error it reconnects
/// after 500 ms and re-subscribes — a fresh subscribe always yields a fresh
/// snapshot, so the book can never silently drift across reconnects.
/// Staleness during the gap is visible via `feed_age_ns()`.
pub async fn run_ws(
    env: KalshiEnv,
    signer: Arc<Signer>,
    cache: Arc<BookCache>,
    tickers: Vec<String>,
) {
    run_ws_url(env.ws_url.to_string(), signer, cache, tickers).await
}

/// URL-parameterized reader — the testable seam (`tests/ws_book.rs` points
/// it at a local ws:// server; prod always enters via `run_ws`).
pub async fn run_ws_url(
    ws_url: String,
    signer: Arc<Signer>,
    cache: Arc<BookCache>,
    tickers: Vec<String>,
) {
    run_ws_url_tapped(ws_url, signer, cache, tickers, None).await
}

/// Same reader with an optional recording tap. The arm path enters through
/// `run_ws` / `run_ws_url`, which pass `None` — recording is strictly additive
/// and cannot change what the executor sees.
pub async fn run_ws_url_tapped(
    ws_url: String,
    signer: Arc<Signer>,
    cache: Arc<BookCache>,
    tickers: Vec<String>,
    tap: Option<FrameTap>,
) {
    let mut attempt = 0u32;
    loop {
        attempt += 1;
        match connect_and_stream(&ws_url, &signer, &cache, &tickers, tap.as_ref()).await {
            Ok(()) => tracing::info!("ws stream ended cleanly"),
            Err(e) => tracing::warn!(attempt, "ws error: {e}; reconnecting"),
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
}

async fn connect_and_stream(
    ws_url: &str,
    signer: &Arc<Signer>,
    cache: &Arc<BookCache>,
    tickers: &[String],
    tap: Option<&FrameTap>,
) -> Result<(), KalshiError> {
    let path = "/trade-api/ws/v2";
    let host = ws_url
        .trim_start_matches("wss://")
        .trim_start_matches("ws://")
        .split('/')
        .next()
        .unwrap()
        .to_string();
    let mut req = tokio_tungstenite::tungstenite::http::Request::builder()
        .uri(ws_url)
        .header("Host", host)
        .header("Upgrade", "websocket")
        .header("Connection", "Upgrade")
        .header(
            "Sec-WebSocket-Key",
            tokio_tungstenite::tungstenite::handshake::client::generate_key(),
        )
        .header("Sec-WebSocket-Version", "13");
    for (k, v) in signer.headers_now("GET", path) {
        req = req.header(k, v);
    }
    let request = req.body(()).map_err(|e| KalshiError::Ws(e.to_string()))?;

    let (mut ws, _) = tokio_tungstenite::connect_async(request)
        .await
        .map_err(|e| KalshiError::Ws(e.to_string()))?;

    // NOTE: docs advertise use_yes_price but the live protocol ignores it
    // (verified 2026-07-10) — sides arrive in their own legs and we convert.
    let sub = json!({
        "id": 1,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta"],
            "market_tickers": tickers,
        }
    });
    ws.send(tokio_tungstenite::tungstenite::Message::Text(
        sub.to_string(),
    ))
    .await
    .map_err(|e| KalshiError::Ws(e.to_string()))?;

    let mut scratch: HashMap<String, Ladders> = HashMap::new();
    // A quiet market sends no deltas for seconds; liveness must not depend on
    // trading activity. We ping every 500 ms — pongs (any frame) refresh
    // feed_age, so the staleness guard measures CONNECTION health.
    let mut ping = tokio::time::interval(Duration::from_millis(500));
    ping.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        let frame = tokio::select! {
            f = ws.next() => match f {
                Some(f) => f.map_err(|e| KalshiError::Ws(e.to_string()))?,
                None => break,
            },
            _ = ping.tick() => {
                ws.send(tokio_tungstenite::tungstenite::Message::Ping(vec![]))
                    .await
                    .map_err(|e| KalshiError::Ws(e.to_string()))?;
                continue;
            }
        };
        cache.touch();
        let text = match frame {
            tokio_tungstenite::tungstenite::Message::Text(t) => t,
            tokio_tungstenite::tungstenite::Message::Close(_) => break,
            // Ping (server) is auto-ponged by tungstenite; Pong answers ours.
            _ => continue,
        };
        // Tap BEFORE parsing, and tap unconditionally: a frame this parser
        // rejects (or an `error` message) is exactly the frame a later
        // investigation wants, so the tape is verbatim and complete or it is
        // not evidence.
        if let Some(tap) = tap {
            tap.offer(&text);
        }
        tracing::debug!(msg = %text.chars().take(2000).collect::<String>(), "ws raw");
        let Ok(msg) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        if msg["type"] == "error" {
            tracing::error!(%text, "ws error message");
            continue;
        }
        if let Some(applied) = apply_message(&mut scratch, &msg) {
            let book = scratch[&applied.ticker].to_book();
            if applied.snapshot {
                cache.publish_snapshot(&applied.ticker, book);
            } else {
                cache.publish(&applied.ticker, book);
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fp(cents: u32) -> String {
        format!("{}.{:02}00", cents / 100, cents % 100)
    }

    /// Wire-exact snapshot: sides in their OWN legs (no side = NO prices).
    /// Quantities given in WHOLE contracts (the wire's "N.00"); parsed books
    /// hold centi-contracts, so expectations below are ×100.
    fn snapshot(ticker: &str, yes: &[(u32, u32)], no: &[(u32, u32)], seq: u64) -> Value {
        json!({
            "type": "orderbook_snapshot", "sid": 1, "seq": seq,
            "msg": {
                "market_ticker": ticker,
                "yes_dollars_fp": yes.iter().map(|(p,q)| json!([fp(*p), format!("{q}.00")])).collect::<Vec<_>>(),
                "no_dollars_fp": no.iter().map(|(p,q)| json!([fp(*p), format!("{q}.00")])).collect::<Vec<_>>(),
            }
        })
    }

    fn delta(ticker: &str, side: &str, price: u32, d: i64, seq: u64) -> Value {
        json!({
            "type": "orderbook_delta", "sid": 1, "seq": seq,
            "msg": {
                "market_ticker": ticker, "side": side,
                "price_dollars": fp(price), "delta_fp": format!("{d}.00"),
            }
        })
    }

    #[test]
    fn parses_dollar_strings_exactly() {
        assert_eq!(dollars_to_cents("0.9900"), Some(99));
        assert_eq!(dollars_to_cents("0.01"), Some(1));
        assert_eq!(dollars_to_cents("1.00"), Some(100));
        assert_eq!(dollars_to_cents("1.01"), None); // out of contract range
        assert_eq!(dollars_to_cents("0.3355"), None); // sub-cent (deci_cent) refused
        assert_eq!(fp_to_centi_contracts("23844.00"), Some(2_384_400));
        assert_eq!(fp_to_centi_contracts("-11429.00"), Some(-1_142_900));
        // Fractional contracts (pinned live 2026-07-15) are EXACT.
        assert_eq!(fp_to_centi_contracts("0.77"), Some(77));
        assert_eq!(fp_to_centi_contracts("-0.30"), Some(-30));
        assert_eq!(fp_to_centi_contracts("9888.38"), Some(988_838));
    }

    #[test]
    fn fractional_levels_survive_and_deltas_never_drift() {
        let mut scratch = HashMap::new();
        // A 0.77-contract NO bid at 74¢ ≡ YES ask at 26¢ — the level the old
        // whole-contract truncation deleted (2026-07-15 MS run).
        apply_message(
            &mut scratch,
            &json!({
                "type": "orderbook_snapshot", "sid": 1, "seq": 1,
                "msg": {
                    "market_ticker": "T",
                    "yes_dollars_fp": [["0.2000", "0.30"]],
                    "no_dollars_fp": [["0.7400", "0.77"]],
                }
            }),
        );
        let book = scratch["T"].to_book();
        assert_eq!(book.best_ask(), Some((26, 77)));
        assert_eq!(book.best_bid(), Some((20, 30)));

        // Ten −0.03 deltas exactly empty the 0.30 yes level; the old parser
        // truncated each to 0 and left phantom liquidity forever.
        for seq in 2..12 {
            apply_message(
                &mut scratch,
                &json!({
                    "type": "orderbook_delta", "sid": 1, "seq": seq,
                    "msg": {
                        "market_ticker": "T", "side": "yes",
                        "price_dollars": "0.2000", "delta_fp": "-0.03",
                    }
                }),
            );
        }
        let book = scratch["T"].to_book();
        assert_eq!(book.best_bid(), None, "exactly-drained level must be gone");
        assert_eq!(
            book.best_ask(),
            Some((26, 77)),
            "untouched fractional level stays"
        );
    }

    #[test]
    fn snapshot_then_deltas_converge_with_leg_conversion() {
        let mut scratch = HashMap::new();
        // NO bids at 55¢/50¢ ≡ YES asks at 45¢/50¢.
        apply_message(
            &mut scratch,
            &snapshot("T", &[(40, 100), (39, 50)], &[(55, 200), (50, 10)], 7),
        );
        let book = scratch["T"].to_book();
        assert_eq!(book.best_bid(), Some((40, 10_000)));
        assert_eq!(book.best_ask(), Some((45, 20_000)));
        assert_eq!(book.seq, 7);

        // NO bid at 55 removed → YES ask at 45 gone; YES bid at 40 reduced.
        apply_message(&mut scratch, &delta("T", "no", 55, -200, 8));
        apply_message(&mut scratch, &delta("T", "yes", 40, -60, 9));
        let book = scratch["T"].to_book();
        assert_eq!(book.best_ask(), Some((50, 1_000)));
        assert_eq!(book.best_bid(), Some((40, 4_000)));
        assert_eq!(book.seq, 9);
    }

    #[test]
    fn malformed_snapshot_is_rejected_without_mutating_prior_book() {
        let mut scratch = HashMap::new();
        assert!(
            apply_message(&mut scratch, &snapshot("T", &[(40, 10)], &[(55, 20)], 7),).is_some()
        );
        let before = scratch["T"].to_book();
        let malformed = json!({
            "type": "orderbook_snapshot", "sid": 1, "seq": 8,
            "msg": {
                "market_ticker": "T",
                "yes_dollars_fp": [["0.4100", "12.00"], ["bad", "3.00"]],
                "no_dollars_fp": [],
            }
        });
        assert!(apply_message(&mut scratch, &malformed).is_none());
        let after = scratch["T"].to_book();
        assert_eq!(after.yes_bids, before.yes_bids);
        assert_eq!(after.yes_asks, before.yes_asks);
        assert_eq!(after.seq, before.seq);
    }

    #[test]
    fn omitted_snapshot_side_is_an_explicit_empty_ladder() {
        let mut scratch = HashMap::new();
        let yes_only = json!({
            "type": "orderbook_snapshot", "sid": 1, "seq": 1,
            "msg": {
                "market_ticker": "YES-ONLY",
                "yes_dollars_fp": [["0.4000", "12.50"]]
            }
        });
        assert!(apply_message(&mut scratch, &yes_only).is_some());
        let book = scratch["YES-ONLY"].to_book();
        assert_eq!(book.best_bid(), Some((40, 1_250)));
        assert_eq!(book.best_ask(), None);

        let no_only = json!({
            "type": "orderbook_snapshot", "sid": 1, "seq": 2,
            "msg": {
                "market_ticker": "NO-ONLY",
                "no_dollars_fp": [["0.5500", "3.00"]]
            }
        });
        assert!(apply_message(&mut scratch, &no_only).is_some());
        let book = scratch["NO-ONLY"].to_book();
        assert_eq!(book.best_bid(), None);
        assert_eq!(book.best_ask(), Some((45, 300)));

        let malformed = json!({
            "type": "orderbook_snapshot", "sid": 1, "seq": 3,
            "msg": {
                "market_ticker": "MALFORMED",
                "yes_dollars_fp": null
            }
        });
        assert!(apply_message(&mut scratch, &malformed).is_none());
        assert!(!scratch.contains_key("MALFORMED"));
    }

    #[test]
    fn depth_at_or_below_cap() {
        let mut scratch = HashMap::new();
        // NO bids at 97/95/3/1 ≡ YES asks at 3/5/97/99.
        apply_message(
            &mut scratch,
            &snapshot("T", &[], &[(97, 10), (95, 20), (3, 500), (1, 1000)], 1),
        );
        let book = scratch["T"].to_book();
        assert_eq!(book.depth_at_or_below(97), 53_000);
        assert_eq!(book.depth_at_or_below(2), 0);
    }

    #[test]
    fn cache_publish_and_staleness() {
        let cache = BookCache::new(&["T".to_string()]);
        assert_eq!(cache.feed_age_ns(), u64::MAX);
        cache.touch();
        cache.publish(
            "T",
            Book {
                seq: 3,
                ..Default::default()
            },
        );
        assert!(cache.feed_age_ns() < 1_000_000_000);
        assert_eq!(cache.read("T").unwrap().seq, 3);
        assert!(cache.read("UNKNOWN").is_none());
    }

    #[derive(Clone, Default)]
    struct RecordedRows(Arc<std::sync::Mutex<Vec<Value>>>);

    impl RecordedRows {
        fn fingerprint(&self) -> io::Result<TapeFingerprint> {
            let mut bytes = Vec::new();
            for row in self.0.lock().unwrap().iter() {
                write_json_line(&mut bytes, row)?;
            }
            fingerprint_reader(bytes.as_slice())
        }
    }

    struct MemorySink {
        rows: RecordedRows,
        fail_sync_all: bool,
    }

    impl TapeSink for MemorySink {
        fn write_row(&mut self, value: &Value) -> io::Result<()> {
            self.rows.0.lock().unwrap().push(value.clone());
            Ok(())
        }

        fn sync_data(&mut self) -> io::Result<()> {
            Ok(())
        }

        fn sync_all(&mut self) -> io::Result<()> {
            if self.fail_sync_all {
                Err(io::Error::new(
                    io::ErrorKind::Other,
                    "injected terminal sync failure",
                ))
            } else {
                Ok(())
            }
        }

        fn fingerprint(&mut self) -> io::Result<TapeFingerprint> {
            self.rows.fingerprint()
        }
    }

    struct MemoryCommitter {
        receipts: RecordedRows,
        final_path: PathBuf,
    }

    impl TapeCommitter for MemoryCommitter {
        fn commit(&mut self, receipt: &Value) -> io::Result<()> {
            self.receipts.0.lock().unwrap().push(receipt.clone());
            Ok(())
        }

        fn final_path(&self) -> &Path {
            &self.final_path
        }
    }

    fn test_worker(
        sink: Box<dyn TapeSink>,
        committer: Box<dyn TapeCommitter>,
        cache: Arc<BookCache>,
        rx: mpsc::Receiver<RawFrame>,
        close_rx: mpsc::Receiver<CloseRequest>,
        dropped: Arc<AtomicU64>,
        producers: Arc<FrameTapProducerState>,
        failure_tx: tokio::sync::watch::Sender<Option<String>>,
    ) -> BookTapeWorker {
        BookTapeWorker {
            sink,
            committer,
            cache,
            rx,
            close_rx,
            dropped,
            producers,
            options: BookTapeOptions {
                heartbeat_interval: Duration::from_secs(30),
                flush_interval: Duration::from_secs(30),
                stale_after: Duration::from_secs(2),
                producer_quiescence_timeout: Duration::from_secs(1),
                backlog_drain_timeout: Duration::from_secs(1),
            },
            start_wall_ns: 1,
            start_mono_ns: 2,
            requested_tickers: vec!["T".to_string()],
            start_observation_count: 0,
            failure_tx,
        }
    }

    fn test_close_request(cache: &BookCache) -> CloseRequest {
        CloseRequest {
            reason: BookTapeCloseReason::Requested,
            snapshot: close_snapshot(cache, Duration::from_secs(2), 0),
            producer_deadline: Instant::now() + Duration::from_secs(1),
        }
    }

    #[test]
    fn terminal_sync_failure_never_publishes_a_complete_receipt() {
        let rows = RecordedRows::default();
        let receipts = RecordedRows::default();
        let cache = BookCache::new(&["T".to_string()]);
        cache.touch();
        cache.publish_snapshot("T", Book::default());
        let (tap, rx) = FrameTap::new(8);
        let dropped = tap.dropped.clone();
        let producers = Arc::clone(&tap.producers);
        assert!(tap.try_offer_frame(RawFrame {
            wall_ns: 3,
            mono_ns: 4,
            text: snapshot("T", &[], &[], 1).to_string(),
        }));
        let (close_tx, close_rx) = mpsc::channel();
        close_tx.send(test_close_request(&cache)).unwrap();
        drop(close_tx);
        drop(tap);
        let (failure_tx, failure_rx) = tokio::sync::watch::channel(None);
        let worker = test_worker(
            Box::new(MemorySink {
                rows: rows.clone(),
                fail_sync_all: true,
            }),
            Box::new(MemoryCommitter {
                receipts: receipts.clone(),
                final_path: PathBuf::from("test.commit.json"),
            }),
            cache,
            rx,
            close_rx,
            dropped,
            producers,
            failure_tx,
        );

        let summary = worker.run();
        assert_eq!(summary.status, BookTapeStatus::WriterError);
        assert!(!summary.complete);
        assert!(!summary.close_written);
        assert!(!summary.committed);
        assert!(summary
            .writer_error
            .as_deref()
            .unwrap()
            .contains("sync_all"));
        assert!(failure_rx.borrow().as_deref().unwrap().contains("sync_all"));
        assert!(receipts.0.lock().unwrap().is_empty());
        let rows = rows.0.lock().unwrap();
        let pending = rows.iter().find(|row| row["t"] == "close").unwrap();
        assert_eq!(pending["status"], "pending");
        assert_eq!(pending["complete"], false);
        assert_eq!(pending["candidate_complete"], true);
    }

    struct BlockingSink {
        rows: RecordedRows,
        entered: Arc<std::sync::atomic::AtomicBool>,
        gate: Arc<(std::sync::Mutex<bool>, std::sync::Condvar)>,
    }

    impl TapeSink for BlockingSink {
        fn write_row(&mut self, value: &Value) -> io::Result<()> {
            self.rows.0.lock().unwrap().push(value.clone());
            if value["t"] == "frame" {
                self.entered.store(true, Ordering::Release);
                let (lock, wake) = &*self.gate;
                let mut released = lock.lock().unwrap();
                while !*released {
                    released = wake.wait(released).unwrap();
                }
            }
            Ok(())
        }

        fn sync_data(&mut self) -> io::Result<()> {
            Ok(())
        }

        fn sync_all(&mut self) -> io::Result<()> {
            Ok(())
        }

        fn fingerprint(&mut self) -> io::Result<TapeFingerprint> {
            self.rows.fingerprint()
        }
    }

    #[test]
    fn blocked_sink_does_not_stall_a_single_thread_tokio_runtime() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap();
        runtime.block_on(async {
            let rows = RecordedRows::default();
            let receipts = RecordedRows::default();
            let entered = Arc::new(std::sync::atomic::AtomicBool::new(false));
            let gate = Arc::new((std::sync::Mutex::new(false), std::sync::Condvar::new()));
            let cache = BookCache::new(&["T".to_string()]);
            cache.publish_snapshot("T", Book::default());
            let (tap, rx) = FrameTap::new(8);
            let dropped = tap.dropped.clone();
            let producers = Arc::clone(&tap.producers);
            assert!(tap.try_offer_frame(RawFrame {
                wall_ns: 3,
                mono_ns: 4,
                text: snapshot("T", &[], &[], 1).to_string(),
            }));
            let (close_tx, close_rx) = mpsc::channel();
            let (failure_tx, _failure_rx) = tokio::sync::watch::channel(None);
            let mut worker = test_worker(
                Box::new(BlockingSink {
                    rows,
                    entered: entered.clone(),
                    gate: gate.clone(),
                }),
                Box::new(MemoryCommitter {
                    receipts,
                    final_path: PathBuf::from("test.commit.json"),
                }),
                Arc::clone(&cache),
                rx,
                close_rx,
                dropped,
                producers,
                failure_tx,
            );
            worker.options.stale_after = Duration::from_millis(25);
            worker.options.producer_quiescence_timeout = Duration::from_millis(25);
            let writer = tokio::task::spawn_blocking(move || worker.run());

            tokio::time::timeout(Duration::from_secs(1), async {
                while !entered.load(Ordering::Acquire) {
                    tokio::time::sleep(Duration::from_millis(5)).await;
                }
            })
            .await
            .expect("blocking sink was never entered");

            // This timer runs on the runtime's only async worker while the
            // recorder is deliberately blocked on the blocking pool.
            tokio::time::timeout(
                Duration::from_millis(200),
                tokio::time::sleep(Duration::from_millis(20)),
            )
            .await
            .expect("blocking tape sink stalled the async runtime");

            // Refresh transport immediately before close intent so the
            // captured snapshot is unambiguously healthy even on a heavily
            // contended test host.
            cache.touch();

            // Capture terminal health while the feed is healthy, then hold
            // the writer in a deliberately slow frame drain until the live
            // cache itself is stale. The terminal record must carry the
            // close-intent snapshot, not a later post-drain observation.
            close_tx
                .send(CloseRequest {
                    reason: BookTapeCloseReason::Requested,
                    snapshot: close_snapshot(&cache, Duration::from_millis(25), 0),
                    producer_deadline: Instant::now() + Duration::from_millis(25),
                })
                .unwrap();
            // Producer quiescence is now proven before the deliberately slow
            // accepted-frame drain crosses the producer deadline.
            drop(tap);
            tokio::time::sleep(Duration::from_millis(70)).await;
            assert_eq!(
                transport_health(&cache, Duration::from_millis(25)).1,
                BookTapeFeedHealth::Stale
            );

            {
                let (lock, wake) = &*gate;
                *lock.lock().unwrap() = true;
                wake.notify_all();
            }
            drop(close_tx);
            let summary = writer.await.unwrap();
            assert!(summary.complete);
            assert!(summary.committed);
            assert_eq!(summary.terminal_feed_health, BookTapeFeedHealth::Healthy);
        });
    }

    #[test]
    fn expired_backlog_deadline_counts_every_accepted_unwritten_frame() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap();
        runtime.block_on(async {
            let rows = RecordedRows::default();
            let receipts = RecordedRows::default();
            let entered = Arc::new(std::sync::atomic::AtomicBool::new(false));
            let gate = Arc::new((std::sync::Mutex::new(false), std::sync::Condvar::new()));
            let cache = BookCache::new(&["T".to_string()]);
            let (tap, rx) = FrameTap::new(8);
            let dropped = Arc::clone(&tap.dropped);
            let producers = Arc::clone(&tap.producers);
            assert!(tap.try_offer_frame(RawFrame {
                wall_ns: 3,
                mono_ns: 4,
                text: snapshot("T", &[], &[], 1).to_string(),
            }));
            assert!(tap.try_offer_frame(RawFrame {
                wall_ns: 5,
                mono_ns: 6,
                text: "accepted-backlog".to_string(),
            }));
            let (close_tx, close_rx) = mpsc::channel();
            let (failure_tx, failure_rx) = tokio::sync::watch::channel(None);
            let mut worker = test_worker(
                Box::new(BlockingSink {
                    rows,
                    entered: Arc::clone(&entered),
                    gate: Arc::clone(&gate),
                }),
                Box::new(MemoryCommitter {
                    receipts: receipts.clone(),
                    final_path: PathBuf::from("test.commit.json"),
                }),
                Arc::clone(&cache),
                rx,
                close_rx,
                dropped,
                producers,
                failure_tx,
            );
            worker.options.backlog_drain_timeout = Duration::from_millis(25);
            let writer = tokio::task::spawn_blocking(move || worker.run());

            tokio::time::timeout(Duration::from_secs(1), async {
                while !entered.load(Ordering::Acquire) {
                    tokio::time::sleep(Duration::from_millis(5)).await;
                }
            })
            .await
            .expect("blocking sink was never entered");
            cache.touch();
            close_tx.send(test_close_request(&cache)).unwrap();
            drop(tap);
            tokio::time::sleep(Duration::from_millis(70)).await;
            {
                let (lock, wake) = &*gate;
                *lock.lock().unwrap() = true;
                wake.notify_all();
            }
            drop(close_tx);

            let summary = writer.await.unwrap();
            assert_eq!(summary.status, BookTapeStatus::WriterError);
            assert_eq!(summary.reason, "backlog_drain_timeout");
            assert_eq!(summary.frame_rows, 1);
            assert_eq!(summary.unwritten_frames, 1);
            assert!(summary.producer_quiesced);
            assert!(summary.terminal_accounting_complete);
            assert!(!summary.close_written);
            assert!(!summary.committed);
            assert!(summary
                .writer_error
                .as_deref()
                .unwrap()
                .contains("1 accepted frame(s) remained unwritten"));
            assert!(failure_rx
                .borrow()
                .as_deref()
                .unwrap()
                .contains("backlog drain timeout"));
            assert!(receipts.0.lock().unwrap().is_empty());
        });
    }

    #[test]
    fn producer_drop_observed_after_blocking_io_keeps_original_deadline_truth() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap();
        runtime.block_on(async {
            let rows = RecordedRows::default();
            let receipts = RecordedRows::default();
            let entered = Arc::new(std::sync::atomic::AtomicBool::new(false));
            let gate = Arc::new((std::sync::Mutex::new(false), std::sync::Condvar::new()));
            let cache = BookCache::new(&["T".to_string()]);
            let (tap, rx) = FrameTap::new(8);
            let dropped = Arc::clone(&tap.dropped);
            let producers = Arc::clone(&tap.producers);
            assert!(tap.try_offer_frame(RawFrame {
                wall_ns: 3,
                mono_ns: 4,
                text: snapshot("T", &[], &[], 1).to_string(),
            }));
            let (close_tx, close_rx) = mpsc::channel();
            let (failure_tx, _failure_rx) = tokio::sync::watch::channel(None);
            let mut worker = test_worker(
                Box::new(BlockingSink {
                    rows,
                    entered: Arc::clone(&entered),
                    gate: Arc::clone(&gate),
                }),
                Box::new(MemoryCommitter {
                    receipts: receipts.clone(),
                    final_path: PathBuf::from("test.commit.json"),
                }),
                Arc::clone(&cache),
                rx,
                close_rx,
                dropped,
                producers,
                failure_tx,
            );
            worker.options.producer_quiescence_timeout = Duration::from_millis(25);
            let writer = tokio::task::spawn_blocking(move || worker.run());
            tokio::time::timeout(Duration::from_secs(1), async {
                while !entered.load(Ordering::Acquire) {
                    tokio::time::sleep(Duration::from_millis(5)).await;
                }
            })
            .await
            .expect("blocking sink was never entered");

            cache.touch();
            close_tx
                .send(CloseRequest {
                    reason: BookTapeCloseReason::Requested,
                    snapshot: close_snapshot(&cache, Duration::from_secs(2), 0),
                    producer_deadline: Instant::now() + Duration::from_millis(25),
                })
                .unwrap();
            tokio::time::sleep(Duration::from_millis(50)).await;
            drop(tap);
            {
                let (lock, wake) = &*gate;
                *lock.lock().unwrap() = true;
                wake.notify_all();
            }
            drop(close_tx);

            let summary = writer.await.unwrap();
            assert_eq!(summary.status, BookTapeStatus::WriterError);
            assert_eq!(summary.reason, "producer_quiescence_timeout");
            assert!(summary.producer_quiesced);
            assert!(summary.terminal_accounting_complete);
            assert!(!summary.close_written);
            assert!(!summary.committed);
            assert!(summary
                .writer_error
                .as_deref()
                .unwrap()
                .contains("dropped after deadline"));
            assert!(receipts.0.lock().unwrap().is_empty());
        });
    }
}
