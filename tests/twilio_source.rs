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
    json!({
        "event": "start",
        "sequenceNumber": "1",
        "start": {
            "accountSid": "ACtest", "streamSid": "MZtest", "callSid": "CAtest",
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
async fn no_connection_times_out_loud() {
    let (mut src, _url) = { bound_source(1, 1) };
    src.accept_timeout = Duration::from_millis(200);
    let (tx, _rx) = ring::ring::<AudioFrame>(64);
    let err = src.run(tx).await.unwrap_err();
    assert!(err.to_string().contains("accept_timeout"), "got: {err}");
}
