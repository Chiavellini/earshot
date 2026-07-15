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

use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use arc_swap::ArcSwap;
use futures_util::{SinkExt, StreamExt};
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
    last_ws_msg: AtomicU64, // MonotonicNs of last message on the socket
}

impl BookCache {
    pub fn new(tickers: &[String]) -> Arc<Self> {
        let books = tickers
            .iter()
            .map(|t| (t.clone(), ArcSwap::from_pointee(Book::default())))
            .collect();
        Arc::new(BookCache { books, last_ws_msg: AtomicU64::new(0) })
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
        self.last_ws_msg.store(MonotonicNs::now().0, Ordering::Relaxed);
    }

    /// Public for tests and the WS task; not part of any layer's door.
    #[doc(hidden)]
    pub fn publish(&self, ticker: &str, book: Book) {
        if let Some(slot) = self.books.get(ticker) {
            slot.store(Arc::new(book));
        }
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
        tracing::warn!(qty = s, "quantity finer than centi-contracts — truncating beyond hundredths");
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
fn apply_message(scratch: &mut HashMap<String, Ladders>, msg: &Value) -> Option<String> {
    let kind = msg["type"].as_str()?;
    let m = &msg["msg"];
    let ticker = m["market_ticker"].as_str()?.to_string();
    let ladders = scratch.entry(ticker.clone()).or_default();

    match kind {
        "orderbook_snapshot" => {
            ladders.yes_bids.clear();
            ladders.yes_asks.clear();
            for (side, key) in [("yes", "yes_dollars_fp"), ("no", "no_dollars_fp")] {
                if let Some(levels) = m[key].as_array() {
                    for level in levels {
                        let price = level[0].as_str().and_then(dollars_to_cents);
                        let qty = level[1].as_str().and_then(fp_to_centi_contracts);
                        if let (Some(p), Some(q)) = (price, qty) {
                            ladders.apply_level(side, p, q, false);
                        }
                    }
                }
            }
            ladders.seq = msg["seq"].as_u64().unwrap_or(0);
            Some(ticker)
        }
        "orderbook_delta" => {
            let price = m["price_dollars"].as_str().and_then(dollars_to_cents)?;
            let delta = m["delta_fp"].as_str().and_then(fp_to_centi_contracts)?;
            let side = m["side"].as_str()?;
            ladders.apply_level(side, price, delta, true);
            ladders.seq = msg["seq"].as_u64().unwrap_or(ladders.seq + 1);
            Some(ticker)
        }
        _ => None,
    }
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
    let mut attempt = 0u32;
    loop {
        attempt += 1;
        match connect_and_stream(&ws_url, &signer, &cache, &tickers).await {
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
    ws.send(tokio_tungstenite::tungstenite::Message::Text(sub.to_string()))
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
        tracing::debug!(msg = %text.chars().take(2000).collect::<String>(), "ws raw");
        let Ok(msg) = serde_json::from_str::<Value>(&text) else { continue };
        if msg["type"] == "error" {
            tracing::error!(%text, "ws error message");
            continue;
        }
        if let Some(ticker) = apply_message(&mut scratch, &msg) {
            cache.publish(&ticker, scratch[&ticker].to_book());
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
        assert_eq!(book.best_ask(), Some((26, 77)), "untouched fractional level stays");
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
        cache.publish("T", Book { seq: 3, ..Default::default() });
        assert!(cache.feed_age_ns() < 1_000_000_000);
        assert_eq!(cache.read("T").unwrap().seq, 3);
        assert!(cache.read("UNKNOWN").is_none());
    }
}
