//! Recording tap on the Kalshi WS book feed (born in the cross-venue program,
//! removed 2026-08-12; the tap is core book evidence — Q-XTAPE).
//!
//! The tape is EVIDENCE, so the properties under test are evidentiary ones:
//! it must be byte-verbatim, it must include frames the book parser rejects,
//! it must never be silently truncated, and switching it on must not change
//! what the executor sees.

use std::path::Path;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::WebSocketStream;

use earshot::kalshi::auth::Signer;
use earshot::kalshi::book::{
    book_tape_commit_path, run_ws_url_tapped, verify_book_tape, Book, BookCache,
    BookTapeCloseReason, BookTapeFeedHealth, BookTapeOptions, BookTapeSession, BookTapeStatus,
    BookTapeTask, FrameTap, RawFrame,
};
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
    let (stream, _) = tokio::time::timeout(WAIT, listener.accept())
        .await
        .unwrap()
        .unwrap();
    let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
    loop {
        match tokio::time::timeout(WAIT, ws.next())
            .await
            .unwrap()
            .unwrap()
            .unwrap()
        {
            Message::Text(_) => break,
            Message::Ping(p) => ws.send(Message::Pong(p)).await.unwrap(),
            other => panic!("unexpected pre-subscribe frame: {other:?}"),
        }
    }
    ws
}

async fn recv_frame(rx: &std::sync::mpsc::Receiver<RawFrame>) -> RawFrame {
    let deadline = tokio::time::Instant::now() + WAIT;
    loop {
        match rx.try_recv() {
            Ok(frame) => return frame,
            Err(std::sync::mpsc::TryRecvError::Empty) => {
                assert!(tokio::time::Instant::now() < deadline, "tap timed out");
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
            Err(std::sync::mpsc::TryRecvError::Disconnected) => panic!("tap closed"),
        }
    }
}

fn tape_session() -> BookTapeSession {
    BookTapeSession {
        endpoint: "ws://example.invalid".to_string(),
        event: Some("TEST-EVENT".to_string()),
        label: "integration-test".to_string(),
        tickers: vec![TICKER.to_string()],
        host: "test-host".to_string(),
        git_sha: "test-sha".to_string(),
        clock_offset_s: Some(0.001),
        planned_secs: Some(5),
    }
}

fn fast_tape_options() -> BookTapeOptions {
    BookTapeOptions {
        heartbeat_interval: Duration::from_millis(30),
        flush_interval: Duration::from_millis(20),
        stale_after: Duration::from_secs(2),
        producer_quiescence_timeout: Duration::from_millis(500),
        backlog_drain_timeout: Duration::from_millis(500),
    }
}

fn tape_rows(path: &Path) -> Vec<Value> {
    std::fs::read_to_string(path)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
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

/// Transport liveness is not usable-book readiness. An error frame followed
/// by healthy ping/pong traffic proves only that the socket is alive; without
/// a successfully parsed snapshot for the requested ticker, the evidence run
/// must remain partial.
#[tokio::test]
async fn error_and_pongs_without_snapshot_are_incomplete() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("transport-only.jsonl");
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let cache = BookCache::new(&[TICKER.to_string()]);
    let (tap, mut writer) = BookTapeTask::start_new(
        &path,
        tape_session(),
        Arc::clone(&cache),
        64,
        fast_tape_options(),
    )
    .await
    .unwrap();
    let client = tokio::spawn(run_ws_url_tapped(
        url,
        test_signer(),
        Arc::clone(&cache),
        vec![TICKER.to_string()],
        Some(tap),
    ));

    let mut ws = accept(&listener).await;
    ws.send(Message::Text(
        json!({"type": "error", "msg": {"code": 6, "msg": "unknown"}}).to_string(),
    ))
    .await
    .unwrap();
    loop {
        match tokio::time::timeout(WAIT, ws.next())
            .await
            .unwrap()
            .unwrap()
            .unwrap()
        {
            Message::Ping(payload) => {
                ws.send(Message::Pong(payload)).await.unwrap();
                break;
            }
            Message::Pong(_) => {}
            other => panic!("expected client ping after error, got {other:?}"),
        }
    }
    let deadline = tokio::time::Instant::now() + WAIT;
    while cache.feed_age_ns() == u64::MAX {
        assert!(
            tokio::time::Instant::now() < deadline,
            "transport never became live"
        );
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert!(!cache.snapshot_is_ready(TICKER));

    writer
        .request_close(BookTapeCloseReason::Requested)
        .unwrap();
    client.abort();
    let _ = client.await;
    let summary = writer.wait().await.unwrap();
    assert_eq!(summary.status, BookTapeStatus::Partial);
    assert!(!summary.complete);
    assert_eq!(summary.terminal_feed_health, BookTapeFeedHealth::Healthy);
    assert_eq!(summary.ready_tickers, Vec::<String>::new());
    assert_eq!(summary.missing_tickers, vec![TICKER.to_string()]);
    assert!(!verify_book_tape(&path).unwrap().complete);
}

/// An empty snapshot is still a successful snapshot. Readiness describes
/// whether a ticker has a usable full-book baseline, not whether it contains
/// resting liquidity.
#[tokio::test]
async fn empty_valid_snapshot_satisfies_book_readiness() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("empty-snapshot.jsonl");
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let cache = BookCache::new(&[TICKER.to_string()]);
    let (tap, mut writer) = BookTapeTask::start_new(
        &path,
        tape_session(),
        Arc::clone(&cache),
        64,
        fast_tape_options(),
    )
    .await
    .unwrap();
    let client = tokio::spawn(run_ws_url_tapped(
        url,
        test_signer(),
        Arc::clone(&cache),
        vec![TICKER.to_string()],
        Some(tap),
    ));

    let mut ws = accept(&listener).await;
    ws.send(Message::Text(snapshot(&[], 1))).await.unwrap();
    let deadline = tokio::time::Instant::now() + WAIT;
    while !cache.snapshot_is_ready(TICKER) {
        assert!(
            tokio::time::Instant::now() < deadline,
            "empty snapshot was not applied"
        );
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert_eq!(cache.read(TICKER).unwrap().seq, 1);
    assert!(cache.read(TICKER).unwrap().yes_bids.is_empty());
    assert!(cache.read(TICKER).unwrap().yes_asks.is_empty());

    writer
        .request_close(BookTapeCloseReason::Requested)
        .unwrap();
    client.abort();
    let _ = client.await;
    let summary = writer.wait().await.unwrap();
    assert_eq!(summary.status, BookTapeStatus::Closed);
    assert!(summary.complete);
    assert_eq!(summary.ready_tickers, vec![TICKER.to_string()]);
    assert!(summary.missing_tickers.is_empty());
    assert!(verify_book_tape(&path).unwrap().complete);
}

/// A snapshot is one atomic baseline. Silently dropping one malformed level
/// and blessing the remainder would make both the live book and tape
/// readiness claim stronger than the wire supports.
#[tokio::test]
async fn malformed_snapshot_level_never_proves_ticker_readiness() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("malformed-snapshot.jsonl");
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let cache = BookCache::new(&[TICKER.to_string()]);
    let (tap, mut writer) = BookTapeTask::start_new(
        &path,
        tape_session(),
        Arc::clone(&cache),
        64,
        fast_tape_options(),
    )
    .await
    .unwrap();
    let client = tokio::spawn(run_ws_url_tapped(
        url,
        test_signer(),
        Arc::clone(&cache),
        vec![TICKER.to_string()],
        Some(tap),
    ));

    let mut ws = accept(&listener).await;
    ws.send(Message::Text(
        json!({
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 8,
            "msg": {
                "market_ticker": TICKER,
                "yes_dollars_fp": [["0.4000", "10.00"], ["not-a-price", "5.00"]],
                "no_dollars_fp": [],
            }
        })
        .to_string(),
    ))
    .await
    .unwrap();
    tokio::time::sleep(Duration::from_millis(60)).await;
    assert!(!cache.snapshot_is_ready(TICKER));
    assert_eq!(cache.read(TICKER).unwrap().seq, 0);
    assert!(cache.read(TICKER).unwrap().yes_bids.is_empty());

    writer
        .request_close(BookTapeCloseReason::Requested)
        .unwrap();
    client.abort();
    let _ = client.await;
    let summary = writer.wait().await.unwrap();
    assert_eq!(summary.status, BookTapeStatus::Partial);
    assert!(!summary.complete);
    assert!(summary.ready_tickers.is_empty());
    assert_eq!(summary.missing_tickers, vec![TICKER.to_string()]);
    assert!(!verify_book_tape(&path).unwrap().complete);

    // The offline verifier must derive readiness from raw frames, not trust a
    // close row rewritten to bless the malformed snapshot.
    let mut rows = tape_rows(&path);
    let close = rows.last_mut().unwrap();
    close["ready_tickers"] = json!([TICKER]);
    close["missing_tickers"] = json!([]);
    let rewritten = rows
        .iter()
        .map(Value::to_string)
        .collect::<Vec<_>>()
        .join("\n")
        + "\n";
    std::fs::write(&path, rewritten).unwrap();
    let error = verify_book_tape(&path).unwrap_err();
    assert!(error
        .to_string()
        .contains("not proven by accepted snapshot frames"));
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
        ws.send(Message::Text(snapshot(&[(40, 100 + seq as u32)], seq)))
            .await
            .unwrap();
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

/// After the required initial snapshot, no deltas is not the same thing as no
/// recorder. Timer-driven heartbeat rows must make a quiet, healthy feed
/// visibly alive and must reach disk via the independent periodic sync timer.
#[tokio::test]
async fn quiet_healthy_market_writes_and_flushes_heartbeats() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("quiet.jsonl");
    let cache = BookCache::new(&[TICKER.to_string()]);
    let (tap, mut writer) = BookTapeTask::start_new(
        &path,
        tape_session(),
        Arc::clone(&cache),
        64,
        fast_tape_options(),
    )
    .await
    .unwrap();
    cache.touch();
    cache.publish_snapshot(TICKER, Book::default());
    assert!(tap.try_offer_frame(RawFrame {
        wall_ns: 1,
        mono_ns: 1,
        text: snapshot(&[], 1),
    }));

    tokio::time::sleep(Duration::from_millis(85)).await;
    let first_len = std::fs::metadata(&path).unwrap().len();
    tokio::time::sleep(Duration::from_millis(80)).await;
    let second_len = std::fs::metadata(&path).unwrap().len();
    assert!(second_len > first_len, "quiet tape did not keep growing");

    writer
        .request_close(BookTapeCloseReason::Requested)
        .unwrap();
    drop(tap);
    let summary = writer.wait().await.unwrap();
    assert_eq!(summary.status, BookTapeStatus::Closed);
    assert!(summary.complete);
    assert!(summary.close_written);
    assert!(summary.committed);
    assert_eq!(summary.frame_rows, 1);
    assert!(summary.heartbeat_rows >= 2);
    assert_eq!(summary.tap_dropped, 0);
    assert!(summary.ever_observed_feed);
    assert_eq!(summary.terminal_feed_health, BookTapeFeedHealth::Healthy);

    let rows = tape_rows(&path);
    let heartbeats: Vec<_> = rows.iter().filter(|row| row["t"] == "heartbeat").collect();
    assert!(heartbeats.len() >= 2);
    for heartbeat in heartbeats {
        assert!(heartbeat["wall_ns"].as_u64().unwrap() > 0);
        assert!(heartbeat["mono_ns"].as_u64().unwrap() > 0);
        assert_eq!(heartbeat["feed_health"], "healthy");
        assert!(heartbeat["feed_age_ns"].as_u64().is_some());
        assert_eq!(heartbeat["frame_rows"], 1);
        assert_eq!(heartbeat["tap_dropped"], 0);
        assert_eq!(heartbeat["writer_status"], "open");
        assert!(heartbeat["writer_error"].is_null());
    }
    let close = rows.last().unwrap();
    assert_eq!(close["t"], "close");
    assert_eq!(close["status"], "pending");
    assert_eq!(close["candidate_status"], "closed");
    assert_eq!(close["complete"], false);
    assert_eq!(close["candidate_complete"], true);
    assert_eq!(close["commit_required"], true);
    assert_eq!(close["producer_quiesced"], true);
    assert_eq!(close["terminal_accounting_complete"], true);
    assert_eq!(close["reason"], "requested");
    assert_eq!(close["rows"], 1);
    assert_eq!(close["dropped"], 0);
    assert!(close["end_wall_ns"].as_u64().unwrap() > 0);
    assert!(close["end_mono_ns"].as_u64().unwrap() > 0);

    let verification = verify_book_tape(&path).unwrap();
    assert!(verification.committed);
    assert!(verification.complete);
    assert_eq!(verification.status.as_deref(), Some("closed"));

    // The pending close row alone can never be mistaken for completeness.
    std::fs::remove_file(book_tape_commit_path(&path)).unwrap();
    let without_receipt = verify_book_tape(&path).unwrap();
    assert!(!without_receipt.committed);
    assert!(!without_receipt.complete);
    assert_eq!(without_receipt.reason, "commit_receipt_missing");
}

/// Cache state that predates the tape is not evidence. A recorder wired onto
/// an already-warm runtime must still capture a full snapshot for every
/// requested ticker before it can claim completeness.
#[tokio::test]
async fn prewarmed_cache_without_taped_snapshot_is_incomplete() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("prewarmed.jsonl");
    let cache = BookCache::new(&[TICKER.to_string()]);
    cache.touch();
    cache.publish_snapshot(TICKER, Book::default());
    let (tap, mut writer) =
        BookTapeTask::start_new(&path, tape_session(), cache, 64, fast_tape_options())
            .await
            .unwrap();

    tokio::time::sleep(Duration::from_millis(45)).await;
    writer
        .request_close(BookTapeCloseReason::Requested)
        .unwrap();
    drop(tap);
    let summary = writer.wait().await.unwrap();
    assert_eq!(summary.status, BookTapeStatus::Partial);
    assert!(!summary.complete);
    assert_eq!(summary.frame_rows, 0);
    assert!(!summary.ever_observed_feed);
    assert!(summary.ready_tickers.is_empty());
    assert_eq!(summary.missing_tickers, vec![TICKER.to_string()]);
    assert!(!verify_book_tape(&path).unwrap().complete);
}

/// A tape path identifies one immutable run. Retrying the recorder against it
/// must fail before returning a tap and must leave every existing byte alone.
#[tokio::test]
async fn recorder_refuses_to_overwrite_an_existing_tape() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("existing.jsonl");
    std::fs::write(&path, b"prior evidence\n").unwrap();
    let cache = BookCache::new(&[TICKER.to_string()]);

    let error = match BookTapeTask::start_new(&path, tape_session(), cache, 64, fast_tape_options())
        .await
    {
        Ok(_) => panic!("recorder unexpectedly replaced an existing tape"),
        Err(error) => error,
    };
    assert_eq!(error.kind(), std::io::ErrorKind::AlreadyExists);
    assert_eq!(std::fs::read(&path).unwrap(), b"prior evidence\n");
}

/// Overflow accounting belongs in the terminal evidence, not only stderr.
/// The earlier feed test proves real `try_send` overflow increments this same
/// atomic; this test proves the reusable writer carries it into its summary.
#[tokio::test]
async fn close_summary_accounts_for_frames_and_tap_drops() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("drops.jsonl");
    let cache = BookCache::new(&[TICKER.to_string()]);
    cache.touch();
    cache.publish_snapshot(TICKER, Book::default());
    let (tap, mut writer) =
        BookTapeTask::start_new(&path, tape_session(), cache, 8, fast_tape_options())
            .await
            .unwrap();
    for seq in 1..=2 {
        assert!(tap.try_offer_frame(RawFrame {
            wall_ns: seq,
            mono_ns: seq + 10,
            text: format!("raw-{seq}"),
        }));
    }
    writer.request_close(BookTapeCloseReason::Deadline).unwrap();
    // This happens after close intent but before producer quiescence. The
    // terminal snapshot must still see it.
    tap.dropped.fetch_add(7, Ordering::Relaxed);
    drop(tap);
    let summary = writer.wait().await.unwrap();
    assert_eq!(
        summary.frame_rows, 2,
        "accepted frames must drain on shutdown"
    );
    assert_eq!(summary.tap_dropped, 7);
    assert_eq!(summary.status, BookTapeStatus::Partial);
    assert!(
        !summary.complete,
        "a tape with dropped frames cannot be complete"
    );

    let rows = tape_rows(&path);
    let close = rows.last().unwrap();
    assert_eq!(close["frame_rows"], 2);
    assert_eq!(close["tap_dropped"], 7);
    assert_eq!(close["status"], "pending");
    assert_eq!(close["candidate_status"], "partial");
    assert_eq!(close["candidate_complete"], false);
    assert!(close["writer_error"].is_null());
    let receipt: Value = serde_json::from_str(
        std::fs::read_to_string(book_tape_commit_path(&path))
            .unwrap()
            .trim(),
    )
    .unwrap();
    assert_eq!(receipt["status"], "partial");
    assert_eq!(receipt["complete"], false);
}

/// Losing every producer without a requested shutdown is distinguishable
/// from a clean, zero-frame recording. The sink is still closed and flushed,
/// but its terminal status is deliberately partial.
#[tokio::test]
async fn producer_loss_writes_a_partial_close_summary() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("partial.jsonl");
    let cache = BookCache::new(&[TICKER.to_string()]);
    cache.touch();
    cache.publish_snapshot(TICKER, Book::default());
    let (tap, mut writer) =
        BookTapeTask::start_new(&path, tape_session(), cache, 8, fast_tape_options())
            .await
            .unwrap();
    drop(tap);
    let summary = writer.wait().await.unwrap();
    assert_eq!(summary.status, BookTapeStatus::Partial);
    assert!(!summary.complete);
    assert_eq!(summary.reason, "feed_closed");
    assert!(summary.close_written);
    assert!(summary.committed);

    let rows = tape_rows(&path);
    let close = rows.last().unwrap();
    assert_eq!(close["status"], "pending");
    assert_eq!(close["candidate_status"], "partial");
    assert_eq!(close["complete"], false);
    assert_eq!(close["reason"], "feed_closed");
}

#[tokio::test]
async fn requested_close_waits_for_producer_quiescence_and_counts_late_frames() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("quiescence.jsonl");
    let cache = BookCache::new(&[TICKER.to_string()]);
    let (tap, mut writer) = BookTapeTask::start_new(
        &path,
        tape_session(),
        Arc::clone(&cache),
        8,
        fast_tape_options(),
    )
    .await
    .unwrap();
    cache.touch();
    cache.publish_snapshot(TICKER, Book::default());
    assert!(tap.try_offer_frame(RawFrame {
        wall_ns: 1,
        mono_ns: 1,
        text: snapshot(&[], 1),
    }));

    writer
        .request_close(BookTapeCloseReason::Requested)
        .unwrap();
    tokio::time::sleep(Duration::from_millis(60)).await;
    assert!(
        !writer.is_finished(),
        "close intent finalized before producer exit"
    );
    assert!(tap.try_offer_frame(RawFrame {
        wall_ns: 9,
        mono_ns: u64::MAX,
        text: "after-close-intent".to_string(),
    }));
    drop(tap);

    let summary = writer.wait().await.unwrap();
    assert_eq!(summary.status, BookTapeStatus::Closed);
    assert!(summary.complete);
    assert_eq!(summary.reason, "requested");
    assert_eq!(summary.frame_rows, 2);
    assert_eq!(summary.tap_dropped, 0);
}

#[tokio::test]
async fn every_frame_tap_clone_must_quiesce_before_commit() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("clone-quiescence.jsonl");
    let cache = BookCache::new(&[TICKER.to_string()]);
    let (tap, mut writer) = BookTapeTask::start_new(
        &path,
        tape_session(),
        Arc::clone(&cache),
        8,
        fast_tape_options(),
    )
    .await
    .unwrap();
    cache.touch();
    cache.publish_snapshot(TICKER, Book::default());
    assert!(tap.try_offer_frame(RawFrame {
        wall_ns: 1,
        mono_ns: 1,
        text: snapshot(&[], 1),
    }));
    let final_clone = tap.clone();

    writer
        .request_close(BookTapeCloseReason::Requested)
        .unwrap();
    drop(tap);
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert!(
        !writer.is_finished(),
        "one live clone falsely counted as quiescent"
    );
    drop(final_clone);

    let summary = writer.wait().await.unwrap();
    assert_eq!(summary.status, BookTapeStatus::Closed);
    assert!(summary.complete);
    assert!(summary.producer_quiesced);
    assert!(summary.terminal_accounting_complete);
    assert!(verify_book_tape(&path).unwrap().complete);
}

#[tokio::test]
async fn never_connected_tape_is_committed_but_incomplete() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("unknown.jsonl");
    let cache = BookCache::new(&[TICKER.to_string()]);
    let (tap, mut writer) =
        BookTapeTask::start_new(&path, tape_session(), cache, 8, fast_tape_options())
            .await
            .unwrap();
    writer
        .request_close(BookTapeCloseReason::Requested)
        .unwrap();
    drop(tap);

    let summary = writer.wait().await.unwrap();
    assert_eq!(summary.status, BookTapeStatus::Partial);
    assert!(!summary.complete);
    assert!(summary.committed);
    assert!(!summary.ever_observed_feed);
    assert_eq!(summary.terminal_feed_age_ns, None);
    assert_eq!(summary.terminal_feed_health, BookTapeFeedHealth::Unknown);
    assert!(!verify_book_tape(&path).unwrap().complete);
}

#[tokio::test]
async fn terminally_stale_tape_cannot_be_complete() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("stale.jsonl");
    let cache = BookCache::new(&[TICKER.to_string()]);
    let mut options = fast_tape_options();
    options.stale_after = Duration::from_millis(25);
    let (tap, mut writer) =
        BookTapeTask::start_new(&path, tape_session(), Arc::clone(&cache), 8, options)
            .await
            .unwrap();
    cache.touch();
    cache.publish_snapshot(TICKER, Book::default());
    assert!(tap.try_offer_frame(RawFrame {
        wall_ns: 1,
        mono_ns: 1,
        text: snapshot(&[], 1),
    }));
    tokio::time::sleep(Duration::from_millis(70)).await;
    writer
        .request_close(BookTapeCloseReason::Requested)
        .unwrap();
    drop(tap);

    let summary = writer.wait().await.unwrap();
    assert_eq!(summary.status, BookTapeStatus::Partial);
    assert!(!summary.complete);
    assert!(summary.committed);
    assert!(summary.ever_observed_feed);
    assert_eq!(summary.terminal_feed_health, BookTapeFeedHealth::Stale);
    assert!(summary.terminal_feed_age_ns.unwrap() >= 25_000_000);
}

/// A leaked producer must fail closed within a bounded interval. Waiting
/// forever would strand event teardown; finalizing anyway could undercount
/// frames or drops still held by that producer.
#[tokio::test]
async fn leaked_frame_tap_times_out_without_commit() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("leaked-producer.jsonl");
    let cache = BookCache::new(&[TICKER.to_string()]);
    cache.touch();
    cache.publish_snapshot(TICKER, Book::default());
    let mut options = fast_tape_options();
    options.producer_quiescence_timeout = Duration::from_millis(60);
    let (tap, mut writer) = BookTapeTask::start_new(&path, tape_session(), cache, 8, options)
        .await
        .unwrap();

    writer
        .request_close(BookTapeCloseReason::Requested)
        .unwrap();
    let summary = tokio::time::timeout(Duration::from_secs(1), writer.wait())
        .await
        .expect("writer hung past its producer-quiescence deadline")
        .unwrap();
    assert_eq!(summary.status, BookTapeStatus::WriterError);
    assert!(!summary.complete);
    assert!(!summary.close_written);
    assert!(!summary.committed);
    assert!(!summary.producer_quiesced);
    assert!(!summary.terminal_accounting_complete);
    assert_eq!(summary.reason, "producer_quiescence_timeout");
    assert!(summary
        .writer_error
        .as_deref()
        .unwrap()
        .contains("FrameTap sender still alive"));
    assert!(!book_tape_commit_path(&path).exists());
    drop(tap);
}

async fn assert_bound_tape_rejects_payload_tamper(replacement: &str) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("bound.jsonl");
    let cache = BookCache::new(&[TICKER.to_string()]);
    cache.touch();
    cache.publish_snapshot(TICKER, Book::default());
    let (tap, mut writer) =
        BookTapeTask::start_new(&path, tape_session(), cache, 8, fast_tape_options())
            .await
            .unwrap();
    assert!(tap.try_offer_frame(RawFrame {
        wall_ns: 7,
        mono_ns: 8,
        text: json!({
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "tamper_marker": "tamper-target",
            "msg": {
                "market_ticker": TICKER,
                "yes_dollars_fp": [],
                "no_dollars_fp": [],
            }
        })
        .to_string(),
    }));
    writer
        .request_close(BookTapeCloseReason::Requested)
        .unwrap();
    drop(tap);
    let summary = writer.wait().await.unwrap();
    assert!(summary.complete);
    assert!(verify_book_tape(&path).unwrap().complete);

    let original = std::fs::read_to_string(&path).unwrap();
    assert_eq!(original.matches("tamper-target").count(), 1);
    let tampered = original.replacen("tamper-target", replacement, 1);
    assert_ne!(tampered.len(), 0);
    std::fs::write(&path, tampered).unwrap();

    let error = verify_book_tape(&path).unwrap_err();
    assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
    assert!(
        error.to_string().contains("SHA-256"),
        "tamper was not rejected by the byte binding: {error}"
    );
}

#[tokio::test]
async fn commit_binding_rejects_deleted_tape_byte() {
    assert_bound_tape_rejects_payload_tamper("tamper-targe").await;
}

#[tokio::test]
async fn commit_binding_rejects_modified_tape_byte() {
    assert_bound_tape_rejects_payload_tamper("tamper-Xarget").await;
}

#[tokio::test]
async fn commit_binding_rejects_inserted_tape_byte() {
    assert_bound_tape_rejects_payload_tamper("tamper-targetX").await;
}

/// Protocol-1 direct-close tapes predate the bound receipt and readiness
/// gates. They must fail loudly instead of being interpreted under protocol 2.
#[test]
fn old_direct_close_schema_is_not_accepted_as_current_evidence() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("old-schema.jsonl");
    let old = format!(
        "{}\n{}\n",
        json!({
            "t": "session",
            "schema_version": 1,
            "venue": "kalshi",
            "feed_kind": "push",
            "channel": "orderbook_delta",
            "tickers": [TICKER],
        }),
        json!({
            "t": "close",
            "status": "closed",
            "complete": true,
            "rows": 0,
            "dropped": 0,
        }),
    );
    std::fs::write(&path, old).unwrap();

    let error = verify_book_tape(&path).unwrap_err();
    assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
    assert!(error.to_string().contains("unsupported tape schema"));
}
