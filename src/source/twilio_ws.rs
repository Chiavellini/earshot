//! `TwilioWsSource` — M3 live capture, adapter #2 (spec §5.2).
//!
//! Twilio Media Streams delivers the dial-in call's inbound audio as JSON
//! messages over a WebSocket **Twilio opens toward us**: we are the WS
//! server, Twilio is the client (`<Start><Stream url="wss://…"/>`). Each
//! `media` message carries base64 G.711 μ-law at 8 kHz mono, nominally
//! 160 bytes = 20 ms. Message shapes are pinned with evidence in
//! `docs/TWILIO.md` — never code against remembered Twilio details.
//!
//! `capture_ts` is stamped at message arrival (network clock domain): it
//! includes WS transit but not the PSTN leg, so "capture latency" here is
//! the earliest observable point of the audio on this machine.

use std::net::TcpListener as StdTcpListener;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use base64::Engine as _;
use futures_util::{SinkExt, StreamExt};
use serde_json::Value;
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::Message;

use super::{
    AudioSource, LatencyClass, SourceDescriptor, SourceError, SourceExit, SourceKind,
};
use crate::clock::MonotonicNs;
use crate::ring::RingSender;
use crate::types::{AudioFrame, SourceId};

pub const TWILIO_SAMPLE_RATE_HZ: u32 = 8000;

/// G.711 μ-law → linear PCM (ITU-T G.711 table; max magnitude ±32124).
const fn build_ulaw_lut() -> [i16; 256] {
    let mut lut = [0i16; 256];
    let mut i = 0usize;
    while i < 256 {
        let u = !(i as u8);
        let sign = u & 0x80 != 0;
        let exponent = ((u >> 4) & 0x07) as i32;
        let mantissa = (u & 0x0F) as i32;
        let magnitude = (((mantissa << 3) + 0x84) << exponent) - 0x84;
        lut[i] = if sign { -magnitude as i16 } else { magnitude as i16 };
        i += 1;
    }
    lut
}

const ULAW_LUT: [i16; 256] = build_ulaw_lut();

pub fn decode_ulaw(bytes: &[u8]) -> Vec<i16> {
    bytes.iter().map(|&b| ULAW_LUT[b as usize]).collect()
}

pub struct TwilioWsSource {
    listener: StdTcpListener,
    source_id: SourceId,
    /// How long to wait for Twilio to connect after arming (the dial happens
    /// after the pipeline is up; a call that never lands must fail loud).
    pub accept_timeout: Duration,
    /// Max silence between WS messages once connected. Twilio streams media
    /// continuously (silence is still frames), so a stall is a dead call.
    pub idle_timeout: Duration,
}

impl TwilioWsSource {
    /// Binds synchronously so `arm` refuses at construction if the port is
    /// taken (P3), and tests can read the ephemeral port before connecting.
    pub fn bind(addr: &str, source_id: SourceId) -> Result<Self, SourceError> {
        let listener = StdTcpListener::bind(addr)?;
        listener.set_nonblocking(true)?;
        Ok(TwilioWsSource {
            listener,
            source_id,
            accept_timeout: Duration::from_secs(900),
            idle_timeout: Duration::from_secs(30),
        })
    }

    pub fn local_addr(&self) -> std::io::Result<std::net::SocketAddr> {
        self.listener.local_addr()
    }
}

fn protocol_err(msg: impl Into<String>) -> SourceError {
    SourceError::Protocol(msg.into())
}

/// Fail-loud check of the `start` message's mediaFormat: this pipeline is
/// wired for 8 kHz mono μ-law; anything else must refuse, never resample
/// silently (layer failure policy).
fn check_media_format(start: &Value) -> Result<(), SourceError> {
    let fmt = &start["start"]["mediaFormat"];
    let encoding = fmt["encoding"].as_str().unwrap_or("?");
    let rate = fmt["sampleRate"].as_u64().unwrap_or(0);
    let channels = fmt["channels"].as_u64().unwrap_or(0);
    if encoding != "audio/x-mulaw" || rate != TWILIO_SAMPLE_RATE_HZ as u64 || channels != 1 {
        return Err(protocol_err(format!(
            "unexpected mediaFormat {encoding:?}/{rate} Hz/{channels} ch — expected audio/x-mulaw/8000/1"
        )));
    }
    Ok(())
}

#[async_trait]
impl AudioSource for TwilioWsSource {
    fn descriptor(&self) -> SourceDescriptor {
        SourceDescriptor {
            kind: SourceKind::Sip,
            codec: "g711_ulaw",
            nominal_latency: LatencyClass::Telephony,
            clock_domain: "network",
        }
    }

    async fn run(
        self: Box<Self>,
        mut tx: RingSender<AudioFrame>,
    ) -> Result<SourceExit, SourceError> {
        let listener = TcpListener::from_std(self.listener)?;
        tracing::info!(addr = %listener.local_addr()?, "twilio source: waiting for stream connection");

        let (stream, peer) = tokio::time::timeout(self.accept_timeout, listener.accept())
            .await
            .map_err(|_| protocol_err("no Twilio connection before accept_timeout — call never landed?"))??;
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .map_err(|e| protocol_err(format!("websocket handshake: {e}")))?;
        tracing::info!(%peer, "twilio stream connected");

        let b64 = base64::engine::general_purpose::STANDARD;
        let mut seq = 0u64;
        let mut last_twilio_seq: Option<u64> = None;
        let mut started = false;

        loop {
            let msg = match tokio::time::timeout(self.idle_timeout, ws.next()).await {
                Err(_) => return Err(protocol_err("stream idle past idle_timeout — dead call")),
                Ok(None) => {
                    tracing::warn!("twilio socket closed without a stop event");
                    return Ok(SourceExit::EndOfStream);
                }
                Ok(Some(Err(e))) => return Err(protocol_err(format!("websocket read: {e}"))),
                Ok(Some(Ok(m))) => m,
            };

            let text = match msg {
                Message::Text(t) => t,
                Message::Ping(p) => {
                    let _ = ws.send(Message::Pong(p)).await;
                    continue;
                }
                Message::Close(_) => {
                    tracing::info!("twilio websocket close frame");
                    return Ok(SourceExit::EndOfStream);
                }
                _ => continue, // pong/binary: nothing Twilio sends matters here
            };
            let v: Value = serde_json::from_str(&text)
                .map_err(|e| protocol_err(format!("bad stream JSON: {e}")))?;

            match v["event"].as_str().unwrap_or("?") {
                "connected" => {
                    tracing::info!(protocol = v["protocol"].as_str().unwrap_or("?"), "twilio: connected");
                }
                "start" => {
                    check_media_format(&v)?;
                    started = true;
                    tracing::info!(
                        stream_sid = v["start"]["streamSid"].as_str().unwrap_or("?"),
                        call_sid = v["start"]["callSid"].as_str().unwrap_or("?"),
                        tracks = ?v["start"]["tracks"],
                        "twilio: stream started (mediaFormat verified 8 kHz μ-law mono)"
                    );
                }
                "media" => {
                    if !started {
                        return Err(protocol_err("media before start event"));
                    }
                    if let Some(ts) = v["sequenceNumber"].as_str().and_then(|s| s.parse::<u64>().ok()) {
                        if let Some(prev) = last_twilio_seq {
                            if ts != prev + 1 {
                                tracing::warn!(prev, got = ts, "twilio sequence gap — network loss");
                            }
                        }
                        last_twilio_seq = Some(ts);
                    }
                    let payload = v["media"]["payload"]
                        .as_str()
                        .ok_or_else(|| protocol_err("media event without payload"))?;
                    let bytes = b64
                        .decode(payload)
                        .map_err(|e| protocol_err(format!("payload base64: {e}")))?;
                    let frame = AudioFrame {
                        pcm: Arc::from(decode_ulaw(&bytes).as_slice()),
                        sample_rate_hz: TWILIO_SAMPLE_RATE_HZ,
                        capture_ts: MonotonicNs::now(),
                        source: self.source_id,
                        seq,
                        preproc_ts: MonotonicNs(0),
                    };
                    seq += 1;
                    // Live semantics: ingress is THE legitimate drop point —
                    // never block the capture path on a stalled consumer.
                    if tx.send(frame).is_err() {
                        tracing::warn!(seq, "ring full: twilio frame dropped");
                    }
                }
                "stop" => {
                    tracing::info!(frames = seq, "twilio: stream stopped");
                    return Ok(SourceExit::EndOfStream);
                }
                other => tracing::debug!(event = other, "twilio: ignored event"),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ulaw_known_values_and_symmetry() {
        // ITU-T G.711 endpoints: 0xFF/0x7F are ±0, 0x80/0x00 are ±32124.
        assert_eq!(ULAW_LUT[0xFF], 0);
        assert_eq!(ULAW_LUT[0x7F], 0);
        assert_eq!(ULAW_LUT[0x80], 32124);
        assert_eq!(ULAW_LUT[0x00], -32124);
        // Positive and negative codes mirror in magnitude.
        for b in 0x80..=0xFFu8 {
            assert_eq!(ULAW_LUT[b as usize], -ULAW_LUT[(b & 0x7F) as usize]);
        }
        // Monotonic decreasing magnitude within the positive half.
        for b in 0x80..0xFFu8 {
            assert!(ULAW_LUT[b as usize] >= ULAW_LUT[(b + 1) as usize]);
        }
    }

    #[test]
    fn decode_ulaw_maps_per_byte() {
        let got = decode_ulaw(&[0xFF, 0x80, 0x00]);
        assert_eq!(got, vec![0, 32124, -32124]);
    }

    #[test]
    fn bind_on_ephemeral_port_reports_addr() {
        let src = TwilioWsSource::bind("127.0.0.1:0", SourceId(1)).unwrap();
        let addr = src.local_addr().unwrap();
        assert_ne!(addr.port(), 0);
        assert_eq!(src.descriptor().codec, "g711_ulaw");
        // A second bind on the same port must refuse (P3: fail at arm).
        assert!(TwilioWsSource::bind(&addr.to_string(), SourceId(1)).is_err());
    }
}
