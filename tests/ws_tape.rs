//! Recording tap on the Kalshi WS book feed (cross-venue lane, Q-XVENUE).
//!
//! The tape is EVIDENCE, so the properties under test are evidentiary ones:
//! it must be byte-verbatim, it must include frames the book parser rejects,
//! it must never be silently truncated, and switching it on must not change
//! what the executor sees.

use std::sync::Arc;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::WebSocketStream;

use earshot::kalshi::auth::Signer;
use earshot::kalshi::book::{run_ws_url_tapped, BookCache, FrameTap, RawFrame};
use rsa::pkcs1::EncodeRsaPrivateKey;

const TICKER: &str = "KX-TAPE-TEST";
const WAIT: Duration = Duration::from_secs(5);

fn test_signer() -> Arc<Signer> {
    let key = rsa::RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 2048).unwrap();
    let pem = key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF).unwrap();
    Arc::new(Signer::from_pem("tape-test-key".into(), &pem).unwrap())
}

fn fp(cents: u32) -> String {
    format!("{}.{:02}00", cents / 100, cents % 100)
}

fn snapshot(yes: &[(u32, u32)], seq: u64) -> String {
    json!({
        "type": "orderbook_snapshot", "sid": 1, "seq": seq,
        "msg": {
            "market_ticker": TICKER,
            "yes_dollars_fp": yes.iter().map(|(p, q)| json!([fp(*p), format!("{q}.00")])).collect::<Vec<_>>(),
            "no_dollars_fp": Vec::<Value>::new(),
        }
    })
    .to_string()
}

async fn accept(listener: &TcpListener) -> WebSocketStream<TcpStream> {
    let (stream, _) = tokio::time::timeout(WAIT, listener.accept()).await.unwrap().unwrap();
    let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
    loop {
        match tokio::time::timeout(WAIT, ws.next()).await.unwrap().unwrap().unwrap() {
            Message::Text(_) => break,
            Message::Ping(p) => ws.send(Message::Pong(p)).await.unwrap(),
            other => panic!("unexpected pre-subscribe frame: {other:?}"),
        }
    }
    ws
}

async fn recv_frame(rx: &mut tokio::sync::mpsc::Receiver<RawFrame>) -> RawFrame {
    tokio::time::timeout(WAIT, rx.recv()).await.expect("tap timed out").expect("tap closed")
}

/// The tape must be the wire, byte for byte — not a re-serialization of it.
/// A re-encoded tape silently normalizes key order, number formatting, and
/// whitespace, and those are precisely the details a wire-format dispute needs.
#[tokio::test]
async fn tape_is_byte_verbatim_and_clock_stamped() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let cache = BookCache::new(&[TICKER.to_string()]);
    let (tap, mut rx) = FrameTap::new(64);
    let client = tokio::spawn(run_ws_url_tapped(
        url,
        test_signer(),
        cache.clone(),
        vec![TICKER.to_string()],
        Some(tap),
    ));

    let mut ws = accept(&listener).await;
    let sent = snapshot(&[(40, 100)], 3);
    let before = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos() as u64;
    ws.send(Message::Text(sent.clone())).await.unwrap();

    let got = recv_frame(&mut rx).await;
    assert_eq!(got.text, sent, "tape must record the wire text verbatim");
    assert!(got.wall_ns >= before, "wall stamp precedes the send");
    assert!(got.mono_ns > 0, "monotonic stamp missing");

    client.abort();
}

/// A frame the book parser throws away is often the most interesting one on
/// the tape (an `error` message, a shape change, a market we did not model).
/// Recording only what parsed would hide exactly those.
#[tokio::test]
async fn tape_records_frames_the_parser_discards() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let cache = BookCache::new(&[TICKER.to_string()]);
    let (tap, mut rx) = FrameTap::new(64);
    let client = tokio::spawn(run_ws_url_tapped(
        url,
        test_signer(),
        cache.clone(),
        vec![TICKER.to_string()],
        Some(tap),
    ));

    let mut ws = accept(&listener).await;
    let err_msg = json!({"type": "error", "msg": {"code": 6, "msg": "unknown"}}).to_string();
    let unknown = json!({"type": "some_future_channel", "msg": {"x": 1}}).to_string();
    let garbage = "{not json at all".to_string();
    for m in [&err_msg, &unknown, &garbage] {
        ws.send(Message::Text(m.clone())).await.unwrap();
    }

    let mut seen = Vec::new();
    for _ in 0..3 {
        seen.push(recv_frame(&mut rx).await.text);
    }
    assert_eq!(seen, vec![err_msg, unknown, garbage]);
    // None of them should have reached the book.
    assert!(cache.read(TICKER).map(|b| b.seq == 0).unwrap_or(true));

    client.abort();
}

/// Overflow must be COUNTED, never blocking and never silent: a wedged writer
/// that stalled the feed would starve the executor's book, and a truncated
/// tape that looked complete would corrupt an analysis instead of failing it.
#[tokio::test]
async fn overflow_is_counted_and_never_blocks_the_feed() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let cache = BookCache::new(&[TICKER.to_string()]);
    // Capacity 1 with a receiver we deliberately never drain.
    let (tap, _rx) = FrameTap::new(1);
    let dropped = tap.dropped.clone();
    let client = tokio::spawn(run_ws_url_tapped(
        url,
        test_signer(),
        cache.clone(),
        vec![TICKER.to_string()],
        Some(tap),
    ));

    let mut ws = accept(&listener).await;
    for seq in 1..=40u64 {
        ws.send(Message::Text(snapshot(&[(40, 100 + seq as u32)], seq))).await.unwrap();
    }

    // The book must still converge even though the tap is jammed.
    let deadline = tokio::time::Instant::now() + WAIT;
    loop {
        if cache.read(TICKER).map(|b| b.seq >= 40).unwrap_or(false) {
            break;
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "a jammed tap stalled the book feed — this must never happen"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(
        dropped.load(std::sync::atomic::Ordering::Relaxed) > 0,
        "overflow must be counted, not silently discarded"
    );

    client.abort();
}
