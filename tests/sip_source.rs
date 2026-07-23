//! `SipRtpSource` integration tests: the RTP media plane end-to-end over a real
//! loopback UDP socket. An in-process sender emits RTP-framed G.711 µ-law to the
//! source's bound port; we assert dense capture-order seq, µ-law decode, jitter
//! reordering, loss handling, and the dead-stream idle guard — all with no
//! carrier and no SIP signaling (that plane is gated on the ingress bake-off).

use std::time::Duration;

use earshot::ring;
use earshot::source::sip::{SipRtpSource, SIP_SAMPLE_RATE_HZ};
use earshot::source::{AudioSource, SourceExit};
use earshot::types::SourceId;

/// Build a minimal RTP packet (v2, no padding/ext/csrc, PT 0 = PCMU).
fn rtp(seq: u16, payload: &[u8]) -> Vec<u8> {
    let mut p = vec![0x80u8, 0x00];
    p.extend_from_slice(&seq.to_be_bytes());
    p.extend_from_slice(&0u32.to_be_bytes()); // timestamp
    p.extend_from_slice(&0u32.to_be_bytes()); // ssrc
    p.extend_from_slice(payload);
    p
}

/// Run the source against a set of (seq, payload) packets sent in the given
/// order over real loopback UDP, and collect the emitted AudioFrames.
async fn run_with_packets(packets: Vec<(u16, Vec<u8>)>) -> Vec<earshot::types::AudioFrame> {
    let src = SipRtpSource::bind("127.0.0.1:0", SourceId(0), 40)
        .unwrap()
        .with_idle_timeout(Duration::from_millis(300));
    let port = src.local_addr().unwrap().port();
    let (tx, mut rx) = ring::ring(256);

    let handle = tokio::spawn(async move { Box::new(src).run(tx).await });

    // Send packets from a client socket to the source's port.
    let client = tokio::net::UdpSocket::bind("127.0.0.1:0").await.unwrap();
    client.connect(("127.0.0.1", port)).await.unwrap();
    for (seq, payload) in &packets {
        client.send(&rtp(*seq, payload)).await.unwrap();
        tokio::time::sleep(Duration::from_millis(5)).await;
    }

    // The source ends on the idle timeout after the last packet.
    let exit = handle.await.unwrap().unwrap();
    assert_eq!(exit, SourceExit::EndOfStream);

    let mut frames = Vec::new();
    while let Some(f) = rx.try_recv() {
        frames.push(f);
    }
    frames
}

#[tokio::test]
async fn in_order_rtp_decodes_and_dense_seq() {
    // µ-law 0xFF ≈ 0, 0x00 ≈ max negative; just assert decode ran + seq dense.
    let packets = vec![
        (100, vec![0xff, 0xff]),
        (101, vec![0x00, 0x00]),
        (102, vec![0x7f, 0x7f]),
    ];
    let frames = run_with_packets(packets).await;
    assert_eq!(frames.len(), 3, "one frame per packet");
    for (i, f) in frames.iter().enumerate() {
        assert_eq!(f.seq, i as u64, "internal seq must be dense capture order");
        assert_eq!(f.sample_rate_hz, SIP_SAMPLE_RATE_HZ);
        assert_eq!(f.pcm.len(), 2, "2 µ-law bytes → 2 PCM samples");
        assert!(f.capture_ts.0 > 0, "capture_ts stamped at arrival");
    }
    // µ-law 0x00 decodes to a large-magnitude sample; 0xff to ~0.
    assert!(frames[1].pcm[0].abs() > frames[0].pcm[0].abs());
}

#[tokio::test]
async fn reordered_packets_are_delivered_in_sequence() {
    // Send 0, then 2 before 1: output must be 0,1,2 in order (payload byte = seq).
    let packets = vec![(0, vec![0]), (2, vec![2]), (1, vec![1])];
    let frames = run_with_packets(packets).await;
    let order: Vec<u8> = frames.iter().map(|f| ulaw_first_byte_marker(f)).collect();
    assert_eq!(order, vec![0, 1, 2], "jitter buffer must reorder to sequence order");
}

#[tokio::test]
async fn lost_packet_is_skipped_not_stalled() {
    // seq 1 never arrives; 0,2,3 do. Depth is 40ms/20 = 2, so a persistent gap
    // is skipped: expect 0,2,3 (never 1), and the stream must not stall.
    let packets = vec![(0, vec![0]), (2, vec![2]), (3, vec![3])];
    let frames = run_with_packets(packets).await;
    let order: Vec<u8> = frames.iter().map(|f| ulaw_first_byte_marker(f)).collect();
    assert_eq!(order, vec![0, 2, 3], "gap at seq 1 skipped");
}

#[tokio::test]
async fn non_rtp_datagram_ignored() {
    // A stray non-RTP datagram (version bits != 2) must not crash or emit.
    let src = SipRtpSource::bind("127.0.0.1:0", SourceId(0), 40)
        .unwrap()
        .with_idle_timeout(Duration::from_millis(300));
    let port = src.local_addr().unwrap().port();
    let (tx, mut rx) = ring::ring(64);
    let handle = tokio::spawn(async move { Box::new(src).run(tx).await });
    let client = tokio::net::UdpSocket::bind("127.0.0.1:0").await.unwrap();
    client.connect(("127.0.0.1", port)).await.unwrap();
    client.send(&[0x40, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]).await.unwrap(); // version 1
    client.send(&rtp(1, &[0x12])).await.unwrap(); // one valid packet
    let exit = handle.await.unwrap().unwrap();
    assert_eq!(exit, SourceExit::EndOfStream);
    let mut n = 0;
    while rx.try_recv().is_some() {
        n += 1;
    }
    assert_eq!(n, 1, "only the valid RTP packet emits a frame");
}

#[tokio::test]
async fn idle_timeout_ends_a_dead_stream() {
    // No packets ever: the idle guard must end the source (never hang).
    let src = SipRtpSource::bind("127.0.0.1:0", SourceId(0), 40)
        .unwrap()
        .with_idle_timeout(Duration::from_millis(200));
    let (tx, _rx) = ring::ring(16);
    let started = std::time::Instant::now();
    let exit = Box::new(src).run(tx).await.unwrap();
    assert_eq!(exit, SourceExit::EndOfStream);
    assert!(started.elapsed() < Duration::from_secs(3), "idle guard must fire fast");
}

/// The test packets carry a single byte = the intended ordering marker; decode
/// it back through the same µ-law LUT the source uses so the assertion is on the
/// delivered PCM, not on raw bytes.
fn ulaw_first_byte_marker(f: &earshot::types::AudioFrame) -> u8 {
    // We sent 1-byte payloads whose value we want to recover as the marker.
    // The source decoded µ-law → PCM; invert by matching against the LUT for the
    // small set of markers used in tests (0,1,2,3).
    for b in 0u8..=3 {
        if earshot::source::twilio_ws::decode_ulaw(&[b])[0] == f.pcm[0] {
            return b;
        }
    }
    255
}
