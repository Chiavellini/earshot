//! TwilioWsSource integration: a synthetic in-process WS client speaks the
//! pinned Media Streams protocol (docs/TWILIO.md) against a bound source.
//! No network beyond loopback, no Twilio account.

use std::time::Duration;

use base64::Engine as _;
use futures_util::SinkExt;
use serde_json::json;
use tokio_tungstenite::tungstenite::Message;

use earshot::ring;
use earshot::source::twilio_ws::TwilioWsSource;
use earshot::source::{AudioSource, SourceExit};
use earshot::types::{AudioFrame, SourceId};

fn bound_source(accept_secs: u64, idle_secs: u64) -> (Box<TwilioWsSource>, String) {
    let mut src = TwilioWsSource::bind("127.0.0.1:0", SourceId(7)).unwrap();
    src.accept_timeout = Duration::from_secs(accept_secs);
    src.idle_timeout = Duration::from_secs(idle_secs);
    let addr = format!("ws://{}", src.local_addr().unwrap());
    (Box::new(src), addr)
}

fn start_msg(encoding: &str, rate: u32, channels: u32) -> String {
    start_msg_sid(encoding, rate, channels, "CAtest")
}

fn start_msg_sid(encoding: &str, rate: u32, channels: u32, call_sid: &str) -> String {
    json!({
        "event": "start",
        "sequenceNumber": "1",
        "start": {
            "accountSid": "ACtest", "streamSid": "MZtest", "callSid": call_sid,
            "tracks": ["inbound"],
            "mediaFormat": {"encoding": encoding, "sampleRate": rate, "channels": channels},
        },
        "streamSid": "MZtest",
    })
    .to_string()
}

fn media_msg(twilio_seq: u64, payload_bytes: &[u8]) -> String {
    let payload = base64::engine::general_purpose::STANDARD.encode(payload_bytes);
    json!({
        "event": "media",
        "sequenceNumber": twilio_seq.to_string(),
        "media": {"track": "inbound", "chunk": twilio_seq.to_string(),
                  "timestamp": (twilio_seq * 20).to_string(), "payload": payload},
        "streamSid": "MZtest",
    })
    .to_string()
}

#[tokio::test]
async fn full_session_delivers_decoded_frames_in_order() {
    let (src, url) = bound_source(5, 5);
    let (tx, mut rx) = ring::ring::<AudioFrame>(1024);
    let run = tokio::spawn(src.run(tx));

    let consumer = std::thread::spawn(move || {
        let mut frames = Vec::new();
        while let Some(f) = rx.recv() {
            frames.push(f);
        }
        frames
    });

    let (mut ws, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws.send(Message::Text(json!({"event":"connected","protocol":"Call","version":"1.0.0"}).to_string()))
        .await
        .unwrap();
    ws.send(Message::Text(start_msg("audio/x-mulaw", 8000, 1))).await.unwrap();
    // 0xFF is μ-law +0; 0x80 is +32124 — one “loud” frame in the middle.
    for i in 0..25u64 {
        let byte = if i == 12 { 0x80 } else { 0xFF };
        ws.send(Message::Text(media_msg(i + 2, &[byte; 160]))).await.unwrap();
    }
    ws.send(Message::Text(json!({"event":"stop","sequenceNumber":"27","streamSid":"MZtest"}).to_string()))
        .await
        .unwrap();

    let exit = run.await.unwrap().unwrap();
    assert_eq!(exit, SourceExit::EndOfStream);
    let frames = consumer.join().unwrap();
    assert_eq!(frames.len(), 25);
    for (i, f) in frames.iter().enumerate() {
        assert_eq!(f.seq, i as u64, "source seq must be dense");
        assert_eq!(f.sample_rate_hz, 8000);
        assert_eq!(f.pcm.len(), 160, "160 μ-law bytes = 160 samples = 20 ms");
        let expect = if i == 12 { 32124 } else { 0 };
        assert!(f.pcm.iter().all(|&s| s == expect), "frame {i} decoded wrong");
    }
    for pair in frames.windows(2) {
        assert!(pair[1].capture_ts >= pair[0].capture_ts);
    }
}

#[tokio::test]
async fn wrong_media_format_fails_loud() {
    let (src, url) = bound_source(5, 5);
    let (tx, _rx) = ring::ring::<AudioFrame>(64);
    let run = tokio::spawn(src.run(tx));

    let (mut ws, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws.send(Message::Text(start_msg("audio/l16", 16000, 1))).await.unwrap();

    let err = run.await.unwrap().unwrap_err();
    assert!(err.to_string().contains("mediaFormat"), "got: {err}");
}

#[tokio::test]
async fn media_before_start_fails_loud() {
    let (src, url) = bound_source(5, 5);
    let (tx, _rx) = ring::ring::<AudioFrame>(64);
    let run = tokio::spawn(src.run(tx));

    let (mut ws, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws.send(Message::Text(media_msg(1, &[0xFF; 160]))).await.unwrap();

    let err = run.await.unwrap().unwrap_err();
    assert!(err.to_string().contains("before start"), "got: {err}");
}

#[tokio::test]
async fn twilio_seq_gap_warns_but_streams_on() {
    let (src, url) = bound_source(5, 5);
    let (tx, mut rx) = ring::ring::<AudioFrame>(64);
    let run = tokio::spawn(src.run(tx));
    let consumer = std::thread::spawn(move || {
        let mut n = 0;
        while rx.recv().is_some() {
            n += 1;
        }
        n
    });

    let (mut ws, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws.send(Message::Text(start_msg("audio/x-mulaw", 8000, 1))).await.unwrap();
    ws.send(Message::Text(media_msg(2, &[0xFF; 160]))).await.unwrap();
    ws.send(Message::Text(media_msg(9, &[0xFF; 160]))).await.unwrap(); // gap: lost packets
    ws.send(Message::Text(json!({"event":"stop","streamSid":"MZtest"}).to_string())).await.unwrap();

    assert_eq!(run.await.unwrap().unwrap(), SourceExit::EndOfStream);
    assert_eq!(consumer.join().unwrap(), 2, "both frames delivered despite the gap");
}

#[tokio::test]
async fn stray_connection_does_not_kill_the_listener() {
    let (src, url) = bound_source(5, 5);
    let (tx, mut rx) = ring::ring::<AudioFrame>(64);
    let run = tokio::spawn(src.run(tx));
    let consumer = std::thread::spawn(move || {
        let mut n = 0;
        while rx.recv().is_some() {
            n += 1;
        }
        n
    });

    // A scanner/health-probe style connection: raw TCP, no WS handshake.
    // Public tunnel URLs see these constantly; they must not deny service.
    let addr = url.trim_start_matches("ws://").to_string();
    let mut stray = tokio::net::TcpStream::connect(&addr).await.unwrap();
    tokio::io::AsyncWriteExt::write_all(&mut stray, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        .await
        .unwrap();
    drop(stray);

    // The real (synthetic-Twilio) session must still be accepted afterwards.
    let (mut ws, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws.send(Message::Text(start_msg("audio/x-mulaw", 8000, 1))).await.unwrap();
    ws.send(Message::Text(media_msg(2, &[0xFF; 160]))).await.unwrap();
    ws.send(Message::Text(json!({"event":"stop","streamSid":"MZtest"}).to_string())).await.unwrap();

    assert_eq!(run.await.unwrap().unwrap(), SourceExit::EndOfStream);
    assert_eq!(consumer.join().unwrap(), 1, "frame delivered after stray connection");
}

#[tokio::test]
async fn no_connection_times_out_loud() {
    let (mut src, _url) = { bound_source(1, 1) };
    src.accept_timeout = Duration::from_millis(200);
    let (tx, _rx) = ring::ring::<AudioFrame>(64);
    let err = src.run(tx).await.unwrap_err();
    assert!(err.to_string().contains("accept_timeout"), "got: {err}");
}

// ── mid-call persistence (re-accept loop, 2026-07-29) ──────────────────────

#[tokio::test]
async fn drop_then_redial_reattaches_with_seq_continuity() {
    let (mut src, url) = bound_source(5, 5);
    src.max_reconnects = 1;
    src.reaccept_window = Duration::from_secs(5);
    let redial_dir = tempfile::tempdir().unwrap();
    let marker = redial_dir.path().join("redial.request");
    src.redial_request_path = Some(marker.clone());

    let (tx, mut rx) = ring::ring::<AudioFrame>(64);
    let run = tokio::spawn(src.run(tx));
    let consumer = std::thread::spawn(move || {
        let mut frames = Vec::new();
        while let Some(f) = rx.recv() {
            frames.push(f);
        }
        frames
    });

    // Call 1: two frames, then the socket dies WITHOUT a stop (the MSFT-class
    // far-end drop as the source sees it).
    let (mut ws1, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws1.send(Message::Text(start_msg_sid("audio/x-mulaw", 8000, 1, "CA1"))).await.unwrap();
    ws1.send(Message::Text(media_msg(2, &[0xFF; 160]))).await.unwrap();
    ws1.send(Message::Text(media_msg(3, &[0xFF; 160]))).await.unwrap();
    drop(ws1);

    // The redial supervisor's marker must appear before the new call lands.
    let mut requested = false;
    for _ in 0..50 {
        if marker.exists() {
            requested = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(requested, "redial.request never written after the drop");
    let req = std::fs::read_to_string(&marker).unwrap();
    assert!(req.contains("\"old_call_sid\":\"CA1\""), "marker: {req}");
    assert!(req.contains("\"attempt\":1"), "marker: {req}");

    // Call 2 (the redial): NEW callSid, two more frames, clean stop.
    let (mut ws2, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws2.send(Message::Text(start_msg_sid("audio/x-mulaw", 8000, 1, "CA2"))).await.unwrap();
    ws2.send(Message::Text(media_msg(2, &[0x80; 160]))).await.unwrap();
    ws2.send(Message::Text(media_msg(3, &[0x80; 160]))).await.unwrap();
    ws2.send(Message::Text(json!({"event":"stop","streamSid":"MZtest"}).to_string())).await.unwrap();

    assert_eq!(run.await.unwrap().unwrap(), SourceExit::EndOfStream);
    let frames = consumer.join().unwrap();
    assert_eq!(frames.len(), 4, "both calls' frames delivered");
    for (i, f) in frames.iter().enumerate() {
        assert_eq!(f.seq, i as u64, "seq must stay dense ACROSS the re-accept");
    }
    // Frames 2-3 came from the second call (0x80 = +32124).
    assert!(frames[2].pcm.iter().all(|&s| s == 32124));
}

#[tokio::test]
async fn stale_same_callsid_reattach_is_dropped_not_adopted() {
    let (mut src, url) = bound_source(5, 5);
    src.max_reconnects = 1;
    src.reaccept_window = Duration::from_secs(5);

    let (tx, mut rx) = ring::ring::<AudioFrame>(64);
    let run = tokio::spawn(src.run(tx));
    let consumer = std::thread::spawn(move || {
        let mut n = 0;
        while rx.recv().is_some() {
            n += 1;
        }
        n
    });

    // Call 1 drops without a stop.
    let (mut ws1, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws1.send(Message::Text(start_msg_sid("audio/x-mulaw", 8000, 1, "CA1"))).await.unwrap();
    ws1.send(Message::Text(media_msg(2, &[0xFF; 160]))).await.unwrap();
    drop(ws1);
    tokio::time::sleep(Duration::from_millis(300)).await;

    // A zombie of the SAME call re-attaches: must be dropped, not adopted.
    let (mut zombie, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    let _ = zombie.send(Message::Text(start_msg_sid("audio/x-mulaw", 8000, 1, "CA1"))).await;
    let _ = zombie.send(Message::Text(media_msg(2, &[0x80; 160]))).await;

    // The REAL redial still gets in afterwards.
    tokio::time::sleep(Duration::from_millis(300)).await;
    let (mut ws2, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws2.send(Message::Text(start_msg_sid("audio/x-mulaw", 8000, 1, "CA2"))).await.unwrap();
    ws2.send(Message::Text(media_msg(2, &[0xFF; 160]))).await.unwrap();
    ws2.send(Message::Text(json!({"event":"stop","streamSid":"MZtest"}).to_string())).await.unwrap();

    assert_eq!(run.await.unwrap().unwrap(), SourceExit::EndOfStream);
    assert_eq!(consumer.join().unwrap(), 2, "zombie frames must not be adopted");
}

#[tokio::test]
async fn redial_marker_carries_schema_with_reason() {
    // Wire contract with scripts/redial-supervisor.sh: the marker is JSON
    // {"unix_ts":<int>,"old_call_sid":"<sid-or-empty>","attempt":<int>,
    //  "reason":"stop"|"idle"|"socket_closed"|"close_frame"|"ws_error"}.
    let (mut src, url) = bound_source(5, 5);
    src.max_reconnects = 1;
    src.reaccept_window = Duration::from_millis(400);
    let redial_dir = tempfile::tempdir().unwrap();
    let marker = redial_dir.path().join("redial.request");
    src.redial_request_path = Some(marker.clone());

    let (tx, _rx) = ring::ring::<AudioFrame>(64);
    let run = tokio::spawn(src.run(tx));

    // An abrupt client drop surfaces as a WS read error → reason "ws_error".
    let (mut ws, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws.send(Message::Text(start_msg_sid("audio/x-mulaw", 8000, 1, "CA1"))).await.unwrap();
    ws.send(Message::Text(media_msg(2, &[0xFF; 160]))).await.unwrap();
    drop(ws);

    let exit = tokio::time::timeout(Duration::from_secs(5), run).await
        .expect("expired reaccept window must end the run")
        .unwrap()
        .unwrap();
    assert_eq!(exit, SourceExit::EndOfStream);

    let req: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&marker).unwrap()).unwrap();
    assert!(req["unix_ts"].as_u64().unwrap() > 0, "marker: {req}");
    assert_eq!(req["old_call_sid"].as_str().unwrap(), "CA1", "marker: {req}");
    assert_eq!(req["attempt"].as_u64().unwrap(), 1, "marker: {req}");
    let reason = req["reason"].as_str().expect("reason must be a string");
    assert!(
        ["stop", "idle", "socket_closed", "close_frame", "ws_error"].contains(&reason),
        "reason outside the pinned vocabulary: {req}"
    );
    assert_eq!(reason, "ws_error", "abrupt drop must map to ws_error: {req}");
}

#[tokio::test]
async fn live_stream_superseded_by_new_callsid_without_burning_an_attempt() {
    // 2026-07-29 incident class: the redial lands while the OLD stream is
    // still half-open. The new connection must not rot in the kernel backlog
    // until idle_timeout — the source must swap to it promptly.
    let (mut src, url) = bound_source(5, 30); // idle 30 s: a swap that waited for idle would trip the 1 s bounds below
    src.max_reconnects = 1;
    src.reaccept_window = Duration::from_secs(5);
    let redial_dir = tempfile::tempdir().unwrap();
    let marker = redial_dir.path().join("redial.request");
    src.redial_request_path = Some(marker.clone());

    let (tx, mut rx) = ring::ring::<AudioFrame>(64);
    let run = tokio::spawn(src.run(tx));
    // Forward frames into an async channel so arrival can be bounded in time.
    let (fwd_tx, mut fwd_rx) = tokio::sync::mpsc::unbounded_channel::<AudioFrame>();
    let forwarder = std::thread::spawn(move || {
        while let Some(f) = rx.recv() {
            let _ = fwd_tx.send(f);
        }
    });
    async fn next_frame(
        rx: &mut tokio::sync::mpsc::UnboundedReceiver<AudioFrame>,
        what: &str,
    ) -> AudioFrame {
        tokio::time::timeout(Duration::from_secs(1), rx.recv())
            .await
            .unwrap_or_else(|_| panic!("{what} did not arrive within 1 s"))
            .unwrap()
    }

    // Stream A (callSid CA1): one frame, then silent — half-open.
    let (mut ws_a, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws_a.send(Message::Text(start_msg_sid("audio/x-mulaw", 8000, 1, "CA1"))).await.unwrap();
    ws_a.send(Message::Text(media_msg(2, &[0xFF; 160]))).await.unwrap();
    let f0 = next_frame(&mut fwd_rx, "frame from stream A").await;
    assert_eq!(f0.seq, 0);
    assert!(f0.pcm.iter().all(|&s| s == 0));

    // Stream B (callSid CA2) connects while A is still open: the redial
    // landing. Frames from B must flow within ~1 s of the swap.
    let (mut ws_b, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws_b.send(Message::Text(start_msg_sid("audio/x-mulaw", 8000, 1, "CA2"))).await.unwrap();
    ws_b.send(Message::Text(media_msg(2, &[0x80; 160]))).await.unwrap();
    ws_b.send(Message::Text(media_msg(3, &[0x80; 160]))).await.unwrap();
    let f1 = next_frame(&mut fwd_rx, "first frame from superseding stream B").await;
    assert_eq!(f1.seq, 1, "seq must stay dense ACROSS the supersede splice");
    assert!(f1.pcm.iter().all(|&s| s == 32124), "frame must come from B");
    let f2 = next_frame(&mut fwd_rx, "second frame from stream B").await;
    assert_eq!(f2.seq, 2);
    assert!(!marker.exists(), "superseding must NOT trigger a redial request");
    drop(ws_a);

    // B did not consume the reconnect attempt: a REAL drop of B still gets
    // the (single-budget) redial — marker written with attempt 1.
    drop(ws_b);
    let mut requested = false;
    for _ in 0..50 {
        if marker.exists() {
            requested = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(requested, "real drop after a supersede must still redial (attempt not burned)");
    let req: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&marker).unwrap()).unwrap();
    assert_eq!(req["attempt"].as_u64().unwrap(), 1, "supersede consumed an attempt: {req}");
    assert_eq!(req["old_call_sid"].as_str().unwrap(), "CA2", "marker: {req}");
    assert_eq!(req["reason"].as_str().unwrap(), "ws_error", "marker: {req}");

    // The redial (callSid CA3) re-attaches normally; clean stop ends the run.
    let (mut ws_c, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws_c.send(Message::Text(start_msg_sid("audio/x-mulaw", 8000, 1, "CA3"))).await.unwrap();
    ws_c.send(Message::Text(media_msg(2, &[0xFF; 160]))).await.unwrap();
    ws_c.send(Message::Text(json!({"event":"stop","streamSid":"MZtest"}).to_string())).await.unwrap();

    let f3 = next_frame(&mut fwd_rx, "frame from redial stream C").await;
    assert_eq!(f3.seq, 3, "seq dense across supersede AND redial");
    assert_eq!(run.await.unwrap().unwrap(), SourceExit::EndOfStream);
    forwarder.join().unwrap();
}

#[tokio::test]
async fn one_shot_second_connection_does_not_disturb_live_stream() {
    // max_reconnects = 0 (default): the listener is never polled while a
    // stream is live, so a second connection just waits in the backlog and
    // the live stream is untouched — pre-persistence behavior unchanged.
    let (src, url) = bound_source(5, 5);
    let (tx, mut rx) = ring::ring::<AudioFrame>(64);
    let run = tokio::spawn(src.run(tx));
    let consumer = std::thread::spawn(move || {
        let mut n = 0;
        while rx.recv().is_some() {
            n += 1;
        }
        n
    });

    let (mut ws_a, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws_a.send(Message::Text(start_msg_sid("audio/x-mulaw", 8000, 1, "CA1"))).await.unwrap();
    ws_a.send(Message::Text(media_msg(2, &[0xFF; 160]))).await.unwrap();

    // A second WS client: its handshake must never be answered mid-stream.
    let second = tokio::time::timeout(
        Duration::from_millis(500),
        tokio_tungstenite::connect_async(&url),
    )
    .await;
    assert!(second.is_err(), "one-shot source must not accept a second connection mid-stream");

    // Stream A keeps flowing and ends cleanly.
    ws_a.send(Message::Text(media_msg(3, &[0xFF; 160]))).await.unwrap();
    ws_a.send(Message::Text(json!({"event":"stop","streamSid":"MZtest"}).to_string())).await.unwrap();
    assert_eq!(run.await.unwrap().unwrap(), SourceExit::EndOfStream);
    assert_eq!(consumer.join().unwrap(), 2, "both of A's frames delivered untouched");
}

#[tokio::test]
async fn default_one_shot_drop_still_fails_loud_immediately() {
    // max_reconnects defaults to 0: an abrupt connection reset must surface
    // as the historical loud error at once, not wait out any reaccept window.
    let (src, url) = bound_source(5, 5);
    let (tx, _rx) = ring::ring::<AudioFrame>(64);
    let run = tokio::spawn(src.run(tx));

    let (mut ws, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws.send(Message::Text(start_msg("audio/x-mulaw", 8000, 1))).await.unwrap();
    ws.send(Message::Text(media_msg(2, &[0xFF; 160]))).await.unwrap();
    drop(ws);

    let err = tokio::time::timeout(Duration::from_secs(3), run).await
        .expect("one-shot drop must not wait for a reaccept window")
        .unwrap()
        .unwrap_err();
    assert!(err.to_string().contains("websocket read"), "got: {err}");
}

#[tokio::test]
async fn exhausted_budget_expires_reaccept_window_as_end_of_stream() {
    // A drop is survived, but no redial ever lands: the window expiring is a
    // CLEAN end (the supervisor may have judged the call over), not an error.
    let (mut src, url) = bound_source(5, 5);
    src.max_reconnects = 1;
    src.reaccept_window = Duration::from_millis(300);

    let (tx, _rx) = ring::ring::<AudioFrame>(64);
    let run = tokio::spawn(src.run(tx));

    let (mut ws, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws.send(Message::Text(start_msg("audio/x-mulaw", 8000, 1))).await.unwrap();
    drop(ws);

    let exit = tokio::time::timeout(Duration::from_secs(5), run).await
        .expect("expired reaccept window must end the run")
        .unwrap()
        .unwrap();
    assert_eq!(exit, SourceExit::EndOfStream);
}
