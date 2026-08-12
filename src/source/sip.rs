//! `SipRtpSource` — raw SIP/RTP dial-in ingress, MEDIA PLANE (adapter #5).
//!
//! Scope: this is the carrier-independent **media plane** — bind a UDP socket,
//! receive RTP, dejitter/reorder, decode the payload codec to PCM, and emit
//! `AudioFrame`s. It is a fill-in behind the frozen `AudioSource` trait, so
//! nothing downstream changes. The SIP **signaling** plane (REGISTER/INVITE/SDP
//! negotiation against a live carrier) is deliberately NOT here — it is gated on
//! the ingress bake-off naming a carrier (docs/INGRESS-BAKEOFF.md §5). Once
//! signaling delivers RTP to our bound port (or a SIP gateway/sidecar does), this
//! source consumes it unchanged.
//!
//! Why this exists ahead of the carrier: the media plane has no carrier
//! dependency and is fully testable against a local RTP source (an in-process
//! sender or `ffmpeg -f rtp`); it also owns the **jitter buffer**, which unblocks
//! the `capture-bakeoff --jitter-buffer-ms` sweep. The leg it will eventually
//! replace (Twilio conference→media-stream fork) is worth ~100–250 ms.
//!
//! Codec: G.711 µ-law / 8 kHz narrowband today (reusing `twilio_ws::decode_ulaw`),
//! parity with the Twilio path. G.722 wideband is a follow-on (the ASR-quality win).

use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;

use super::twilio_ws::decode_ulaw;
use super::{AudioSource, LatencyClass, SourceDescriptor, SourceError, SourceExit, SourceKind};
use crate::clock::MonotonicNs;
use crate::ring::RingSender;
use crate::types::{AudioFrame, SourceId};

/// G.711 narrowband; the only codec the media plane decodes today.
pub const SIP_SAMPLE_RATE_HZ: u32 = 8000;
/// A dead stream (no RTP for this long) ends the source rather than hanging.
const IDLE_TIMEOUT: Duration = Duration::from_secs(8);
/// Nominal RTP packetization (20 ms/packet for G.711) — maps jitter ms → depth.
const PACKET_MS: u32 = 20;
const MAX_DATAGRAM: usize = 2048;

/// Parse an RTP packet header (RFC 3550 §5.1), returning `(sequence, payload)`.
/// Returns `None` for anything that isn't a well-formed RTP v2 packet. Handles
/// CSRC list, the one-word extension header, and padding.
fn parse_rtp(buf: &[u8]) -> Option<(u16, &[u8])> {
    if buf.len() < 12 {
        return None;
    }
    if buf[0] >> 6 != 2 {
        return None; // version must be 2
    }
    let padding = buf[0] & 0x20 != 0;
    let extension = buf[0] & 0x10 != 0;
    let cc = (buf[0] & 0x0F) as usize;
    let seq = u16::from_be_bytes([buf[2], buf[3]]);

    let mut off = 12 + 4 * cc;
    if extension {
        if buf.len() < off + 4 {
            return None;
        }
        let ext_words = u16::from_be_bytes([buf[off + 2], buf[off + 3]]) as usize;
        off += 4 + 4 * ext_words;
    }
    if off > buf.len() {
        return None;
    }
    let mut end = buf.len();
    if padding && end > off {
        let pad = buf[end - 1] as usize;
        if end >= off + pad && pad > 0 {
            end -= pad;
        }
    }
    Some((seq, &buf[off..end]))
}

/// A bounded reorder/jitter buffer over RTP sequence numbers. Releases packets
/// in strict sequence order; tolerates reordering up to `depth` packets, and on
/// a persistent gap (buffer deeper than `depth`) declares the missing packet
/// lost and advances past it — so latency is bounded to ~`depth` packets.
struct JitterBuffer {
    buf: BTreeMap<u64, (MonotonicNs, Vec<u8>)>,
    next: Option<u64>,
    depth: usize,
    // 16-bit RTP seq → monotonic 64-bit extension (cycle-counted).
    last16: Option<u16>,
    cycles: u64,
}

impl JitterBuffer {
    fn new(jitter_ms: u32) -> Self {
        // At least 1 packet of reorder tolerance; jitter_ms/20 otherwise.
        let depth = ((jitter_ms / PACKET_MS) as usize).max(1);
        JitterBuffer {
            buf: BTreeMap::new(),
            next: None,
            depth,
            last16: None,
            cycles: 0,
        }
    }

    /// Extend a 16-bit RTP seq to a monotonic 64-bit value, counting wrap cycles.
    /// Common-case correct (forward delivery + small reorders away from the wrap
    /// boundary); a reorder straddling the 65535→0 wrap is a rare edge left for a
    /// follow-on (documented).
    fn extend(&mut self, s: u16) -> u64 {
        match self.last16 {
            None => {
                self.last16 = Some(s);
                s as u64
            }
            Some(last) => {
                let forward = s.wrapping_sub(last) < 0x8000;
                if forward {
                    if s < last {
                        self.cycles += 1; // crossed the wrap going forward
                    }
                    self.last16 = Some(s);
                }
                (self.cycles << 16) | s as u64
            }
        }
    }

    fn push(&mut self, seq16: u16, ts: MonotonicNs, payload: Vec<u8>) {
        let e = self.extend(seq16);
        if self.next.is_none() {
            self.next = Some(e);
        }
        // Drop a packet older than what we've already released.
        if e >= self.next.unwrap() {
            self.buf.insert(e, (ts, payload));
        }
    }

    /// Release everything currently deliverable, in order.
    fn drain_ready(&mut self) -> Vec<(MonotonicNs, Vec<u8>)> {
        let mut out = Vec::new();
        loop {
            while let Some(n) = self.next {
                if let Some(item) = self.buf.remove(&n) {
                    out.push(item);
                    self.next = Some(n + 1);
                } else {
                    break;
                }
            }
            if self.buf.len() > self.depth {
                // Persistent gap: the next expected packet is lost. Skip to the
                // oldest buffered seq and let the loop release from there.
                if let Some((&smallest, _)) = self.buf.iter().next() {
                    self.next = Some(smallest);
                    continue;
                }
            }
            break;
        }
        out
    }

    /// Flush remaining buffered packets in order (stream end).
    fn flush(&mut self) -> Vec<(MonotonicNs, Vec<u8>)> {
        std::mem::take(&mut self.buf).into_values().collect()
    }
}

/// RTP media-plane audio source. Binds a UDP socket and turns inbound RTP into
/// `AudioFrame`s; the signaling that points a carrier's media at this port is
/// separate and gated.
pub struct SipRtpSource {
    socket: std::net::UdpSocket,
    source_id: SourceId,
    jitter_ms: u32,
    idle_timeout: Duration,
}

impl SipRtpSource {
    /// Bind the RTP receive socket. `addr` is `host:port` (e.g. `0.0.0.0:40000`,
    /// or `127.0.0.1:0` in tests to get an ephemeral port).
    pub fn bind(addr: &str, source_id: SourceId, jitter_ms: u32) -> Result<Self, SourceError> {
        let socket = std::net::UdpSocket::bind(addr)?;
        socket.set_nonblocking(true)?;
        Ok(SipRtpSource {
            socket,
            source_id,
            jitter_ms,
            idle_timeout: IDLE_TIMEOUT,
        })
    }

    /// Shorten the dead-stream idle timeout (tests; default 8 s).
    pub fn with_idle_timeout(mut self, d: Duration) -> Self {
        self.idle_timeout = d;
        self
    }

    pub fn local_addr(&self) -> std::io::Result<SocketAddr> {
        self.socket.local_addr()
    }
}

#[async_trait]
impl AudioSource for SipRtpSource {
    fn descriptor(&self) -> SourceDescriptor {
        SourceDescriptor {
            kind: SourceKind::Sip,
            codec: "g711-ulaw",
            nominal_latency: LatencyClass::Telephony,
            clock_domain: "network",
        }
    }

    async fn run(self: Box<Self>, tx: RingSender<AudioFrame>) -> Result<SourceExit, SourceError> {
        let mut tx = tx;
        let socket = tokio::net::UdpSocket::from_std(self.socket)?;
        let mut jitter = JitterBuffer::new(self.jitter_ms);
        let mut buf = vec![0u8; MAX_DATAGRAM];
        let mut seq: u64 = 0; // dense internal AudioFrame seq (capture order)

        loop {
            let recv = tokio::time::timeout(self.idle_timeout, socket.recv_from(&mut buf)).await;
            let n = match recv {
                Ok(Ok((n, _peer))) => n,
                Ok(Err(e)) => return Err(SourceError::Io(e)),
                Err(_) => {
                    // Idle timeout: dead stream. Flush and end.
                    for (ts, payload) in jitter.flush() {
                        emit(&mut tx, &mut seq, self.source_id, ts, &payload);
                    }
                    tracing::info!(frames = seq, "sip/rtp: idle timeout, stream ended");
                    return Ok(SourceExit::EndOfStream);
                }
            };
            // capture_ts stamped at arrival (network clock domain) — earliest
            // observable point of the audio on this machine.
            let ts = MonotonicNs::now();
            if let Some((rtp_seq, payload)) = parse_rtp(&buf[..n]) {
                jitter.push(rtp_seq, ts, payload.to_vec());
                for (fts, payload) in jitter.drain_ready() {
                    emit(&mut tx, &mut seq, self.source_id, fts, &payload);
                }
            } else {
                tracing::debug!(len = n, "sip/rtp: non-RTP datagram ignored");
            }
        }
    }
}

/// Decode one RTP payload to PCM and hand a frame to the ring. Ingress is THE
/// legitimate drop point — never block the capture path on a stalled consumer
/// (same policy as the Twilio source).
fn emit(
    tx: &mut RingSender<AudioFrame>,
    seq: &mut u64,
    source: SourceId,
    capture_ts: MonotonicNs,
    payload: &[u8],
) {
    if payload.is_empty() {
        return;
    }
    let frame = AudioFrame {
        pcm: Arc::from(decode_ulaw(payload).as_slice()),
        sample_rate_hz: SIP_SAMPLE_RATE_HZ,
        capture_ts,
        source,
        seq: *seq,
        preproc_ts: MonotonicNs(0),
    };
    let s = *seq;
    *seq += 1;
    if tx.send(frame).is_err() {
        tracing::warn!(seq = s, "ring full: sip/rtp frame dropped");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a minimal RTP packet: v2, no padding/ext/csrc, PT 0 (PCMU).
    fn rtp(seq: u16, payload: &[u8]) -> Vec<u8> {
        let mut p = vec![0x80, 0x00]; // v2, PT=0
        p.extend_from_slice(&seq.to_be_bytes());
        p.extend_from_slice(&0u32.to_be_bytes()); // timestamp
        p.extend_from_slice(&0u32.to_be_bytes()); // ssrc
        p.extend_from_slice(payload);
        p
    }

    #[test]
    fn parse_rejects_non_rtp_and_short() {
        assert!(parse_rtp(&[]).is_none());
        assert!(parse_rtp(&[0u8; 8]).is_none()); // too short
        assert!(parse_rtp(&[0x40, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]).is_none()); // version 1
    }

    #[test]
    fn parse_extracts_seq_and_payload() {
        let pkt = rtp(1234, &[0xff, 0x7f, 0x00]);
        let (seq, payload) = parse_rtp(&pkt).unwrap();
        assert_eq!(seq, 1234);
        assert_eq!(payload, &[0xff, 0x7f, 0x00]);
    }

    #[test]
    fn parse_handles_csrc_and_padding() {
        // cc=1 (one CSRC word), padding with 2 pad bytes.
        let mut p = vec![0xA1, 0x00]; // v2, padding=1, cc=1
        p.extend_from_slice(&7u16.to_be_bytes());
        p.extend_from_slice(&0u32.to_be_bytes());
        p.extend_from_slice(&0u32.to_be_bytes());
        p.extend_from_slice(&0u32.to_be_bytes()); // 1 CSRC
        p.extend_from_slice(&[0xaa, 0xbb]); // 2 payload bytes
        p.extend_from_slice(&[0x00, 0x02]); // 2 pad bytes, last = count
        let (seq, payload) = parse_rtp(&p).unwrap();
        assert_eq!(seq, 7);
        assert_eq!(payload, &[0xaa, 0xbb]);
    }

    fn ts() -> MonotonicNs {
        MonotonicNs(0)
    }

    #[test]
    fn jitter_releases_in_order() {
        let mut j = JitterBuffer::new(40); // depth 2
        j.push(0, ts(), vec![0]);
        j.push(1, ts(), vec![1]);
        let out: Vec<u8> = j.drain_ready().into_iter().map(|(_, p)| p[0]).collect();
        assert_eq!(out, vec![0, 1]);
    }

    #[test]
    fn jitter_reorders_within_depth() {
        let mut j = JitterBuffer::new(40); // depth 2
        j.push(0, ts(), vec![0]);
        assert_eq!(
            j.drain_ready()
                .into_iter()
                .map(|(_, p)| p[0])
                .collect::<Vec<_>>(),
            vec![0]
        );
        // 2 arrives before 1 — held until 1 fills the gap.
        j.push(2, ts(), vec![2]);
        assert!(j.drain_ready().is_empty(), "must hold 2 while 1 is missing");
        j.push(1, ts(), vec![1]);
        let out: Vec<u8> = j.drain_ready().into_iter().map(|(_, p)| p[0]).collect();
        assert_eq!(out, vec![1, 2]);
    }

    #[test]
    fn jitter_skips_persistent_gap() {
        let mut j = JitterBuffer::new(20); // depth 1
        j.push(0, ts(), vec![0]);
        assert_eq!(
            j.drain_ready()
                .into_iter()
                .map(|(_, p)| p[0])
                .collect::<Vec<_>>(),
            vec![0]
        );
        // 1 is lost forever; 2 and 3 arrive → once deeper than depth, skip 1.
        j.push(2, ts(), vec![2]);
        j.push(3, ts(), vec![3]);
        let out: Vec<u8> = j.drain_ready().into_iter().map(|(_, p)| p[0]).collect();
        assert_eq!(out, vec![2, 3], "gap at seq 1 skipped, 2 and 3 released");
    }

    #[test]
    fn jitter_drops_late_packet() {
        let mut j = JitterBuffer::new(40);
        j.push(5, ts(), vec![5]);
        j.drain_ready();
        // seq 4 arrives after 5 already released — dropped, not re-emitted.
        j.push(4, ts(), vec![4]);
        assert!(j.drain_ready().is_empty());
    }
}
