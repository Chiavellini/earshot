//! BookCache WS feed integration against a local tokio-tungstenite server:
//! the previously-untested `run_ws_url` loop — subscribe handshake, snapshot
//! → cache convergence, reconnect + re-subscribe after a drop, ping-driven
//! liveness on a quiet market, and error-message resilience.

use std::sync::Arc;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::WebSocketStream;

use earshot::kalshi::auth::Signer;
use earshot::kalshi::book::{run_ws_url, BookCache};
use rsa::pkcs1::EncodeRsaPrivateKey;

const TICKER: &str = "KX-WS-TEST";
const WAIT: Duration = Duration::from_secs(5);

fn test_signer() -> Arc<Signer> {
    let key = rsa::RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 2048).unwrap();
    let pem = key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF).unwrap();
    Arc::new(Signer::from_pem("ws-test-key".into(), &pem).unwrap())
}

fn fp(cents: u32) -> String {
    format!("{}.{:02}00", cents / 100, cents % 100)
}

fn snapshot(yes: &[(u32, u32)], no: &[(u32, u32)], seq: u64) -> String {
    json!({
        "type": "orderbook_snapshot", "sid": 1, "seq": seq,
        "msg": {
            "market_ticker": TICKER,
            "yes_dollars_fp": yes.iter().map(|(p,q)| json!([fp(*p), format!("{q}.00")])).collect::<Vec<_>>(),
            "no_dollars_fp": no.iter().map(|(p,q)| json!([fp(*p), format!("{q}.00")])).collect::<Vec<_>>(),
        }
    })
    .to_string()
}

fn delta(side: &str, price: u32, d: i64, seq: u64) -> String {
    json!({
        "type": "orderbook_delta", "sid": 1, "seq": seq,
        "msg": {
            "market_ticker": TICKER, "side": side,
            "price_dollars": fp(price), "delta_fp": format!("{d}.00"),
        }
    })
    .to_string()
}

/// Accept one WS connection, asserting the signed-handshake headers, and
/// return the stream plus the client's first message (the subscribe cmd).
async fn accept_and_read_subscribe(listener: &TcpListener) -> (WebSocketStream<TcpStream>, Value) {
    let (stream, _) = tokio::time::timeout(WAIT, listener.accept()).await.unwrap().unwrap();
    let mut saw_auth = (false, false, false);
    let ws = tokio_tungstenite::accept_hdr_async(stream, |req: &_, resp| {
        let h = tokio_tungstenite::tungstenite::handshake::server::Request::headers(req);
        saw_auth = (
            h.contains_key("KALSHI-ACCESS-KEY"),
            h.contains_key("KALSHI-ACCESS-SIGNATURE"),
            h.contains_key("KALSHI-ACCESS-TIMESTAMP"),
        );
        Ok(resp)
    })
    .await
    .unwrap();
    assert_eq!(saw_auth, (true, true, true), "signed WS handshake headers missing");

    let mut ws = ws;
    let sub = loop {
        match tokio::time::timeout(WAIT, ws.next()).await.unwrap().unwrap().unwrap() {
            Message::Text(t) => break serde_json::from_str::<Value>(&t).unwrap(),
            Message::Ping(p) => ws.send(Message::Pong(p)).await.unwrap(),
            other => panic!("unexpected pre-subscribe frame: {other:?}"),
        }
    };
    assert_eq!(sub["cmd"], "subscribe");
    assert_eq!(sub["params"]["channels"][0], "orderbook_delta");
    assert_eq!(sub["params"]["market_tickers"][0], TICKER);
    (ws, sub)
}

async fn wait_for_seq(cache: &BookCache, seq: u64) {
    let deadline = tokio::time::Instant::now() + WAIT;
    loop {
        if cache.read(TICKER).map(|b| b.seq >= seq).unwrap_or(false) {
            return;
        }
        assert!(tokio::time::Instant::now() < deadline, "book never reached seq {seq}");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

fn spawn_client(url: String, cache: Arc<BookCache>) -> tokio::task::JoinHandle<()> {
    tokio::spawn(run_ws_url(url, test_signer(), cache, vec![TICKER.to_string()]))
}

#[tokio::test]
async fn subscribes_and_populates_cache() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let cache = BookCache::new(&[TICKER.to_string()]);
    let client = spawn_client(url, cache.clone());

    let (mut ws, _) = accept_and_read_subscribe(&listener).await;
    ws.send(Message::Text(snapshot(&[(40, 100)], &[(55, 200)], 3))).await.unwrap();
    wait_for_seq(&cache, 3).await;
    let book = cache.read(TICKER).unwrap();
    assert_eq!(book.best_bid(), Some((40, 100)));
    assert_eq!(book.best_ask(), Some((45, 200))); // no 55 ≡ yes ask 45
    assert!(cache.feed_age_ns() < 1_000_000_000);

    ws.send(Message::Text(delta("no", 55, -200, 4))).await.unwrap();
    wait_for_seq(&cache, 4).await;
    assert_eq!(cache.read(TICKER).unwrap().best_ask(), None);
    client.abort();
}

#[tokio::test]
async fn reconnects_and_resubscribes_after_server_drop() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let cache = BookCache::new(&[TICKER.to_string()]);
    let client = spawn_client(url, cache.clone());

    // Connection 1: snapshot, then hard drop.
    let (mut ws, _) = accept_and_read_subscribe(&listener).await;
    ws.send(Message::Text(snapshot(&[(40, 100)], &[], 1))).await.unwrap();
    wait_for_seq(&cache, 1).await;
    drop(ws);

    // Connection 2 must arrive (≈500 ms backoff) WITH a fresh subscribe, and
    // its snapshot must fully replace the old book — no drift across drops.
    let t0 = tokio::time::Instant::now();
    let (mut ws2, _) = accept_and_read_subscribe(&listener).await;
    assert!(t0.elapsed() < Duration::from_secs(3), "reconnect took {:?}", t0.elapsed());
    ws2.send(Message::Text(snapshot(&[(60, 7)], &[(30, 9)], 2))).await.unwrap();
    wait_for_seq(&cache, 2).await;
    let book = cache.read(TICKER).unwrap();
    assert_eq!(book.best_bid(), Some((60, 7)));
    assert_eq!(book.best_ask(), Some((70, 9)));
    assert_eq!(book.yes_bids.len(), 1, "old levels must not survive the reconnect");
    client.abort();
}

#[tokio::test]
async fn pings_keep_feed_age_fresh_on_quiet_market() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let cache = BookCache::new(&[TICKER.to_string()]);
    let client = spawn_client(url, cache.clone());

    let (mut ws, _) = accept_and_read_subscribe(&listener).await;
    ws.send(Message::Text(snapshot(&[(40, 100)], &[], 1))).await.unwrap();
    wait_for_seq(&cache, 1).await;

    // Quiet market: no book traffic for 2 s. The client pings every 500 ms;
    // answer them and the feed must never look stale.
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    let mut pings = 0;
    while tokio::time::Instant::now() < deadline {
        match tokio::time::timeout(Duration::from_millis(900), ws.next()).await {
            Ok(Some(Ok(Message::Ping(p)))) => {
                pings += 1;
                ws.send(Message::Pong(p)).await.unwrap();
            }
            Ok(Some(Ok(_))) => {}
            other => panic!("expected client pings on a quiet market, got {other:?}"),
        }
        assert!(
            cache.feed_age_ns() < 1_500_000_000,
            "feed went stale on a quiet-but-healthy connection"
        );
    }
    assert!(pings >= 2, "expected ≥2 pings in 2 s, saw {pings}");
    client.abort();
}

#[tokio::test]
async fn error_message_does_not_poison_the_cache() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let cache = BookCache::new(&[TICKER.to_string()]);
    let client = spawn_client(url, cache.clone());

    let (mut ws, _) = accept_and_read_subscribe(&listener).await;
    ws.send(Message::Text(json!({"type":"error","msg":{"code":6,"msg":"bad"}}).to_string()))
        .await
        .unwrap();
    ws.send(Message::Text("not json at all".into())).await.unwrap();
    ws.send(Message::Text(snapshot(&[(40, 100)], &[], 9))).await.unwrap();
    wait_for_seq(&cache, 9).await;
    assert_eq!(cache.read(TICKER).unwrap().best_bid(), Some((40, 100)));
    client.abort();
}
