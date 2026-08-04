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
use hmac::{Hmac, Mac};
use serde_json::Value;
use sha1::Sha1;
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::handshake::server::{ErrorResponse, Request, Response};
use tokio_tungstenite::tungstenite::http::StatusCode;
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

// ── X-Twilio-Signature validation ────────────────────────────────────────
//
// Twilio signs each request (including the Media Streams WS upgrade) with
//   X-Twilio-Signature = base64( HMAC-SHA1( authToken, URL + sorted params ) )
// where URL is the exact `<Stream url="wss://…">` and, for POSTs, params are
// each key+value concatenated in lexicographic key order. The WS upgrade is a
// GET with no body params (Media Streams `<Parameter>`s arrive in the `start`
// message, not the URL), so the signed string is just the URL.
//
// Scheme pinned in VERIFICATIONS.md (from official docs; live-confirmed at the
// loopback rehearsal). We are the WS server behind a public endpoint — this is
// the only proof the connection is really Twilio and not a spoofed dialer.

/// Raw HMAC-SHA1 (kept separate so a standard known-answer vector can test the
/// crypto independently of Twilio's URL-concatenation scheme).
fn hmac_sha1(key: &[u8], data: &[u8]) -> Vec<u8> {
    let mut mac = Hmac::<Sha1>::new_from_slice(key).expect("HMAC accepts any key length");
    mac.update(data);
    mac.finalize().into_bytes().to_vec()
}

/// The expected `X-Twilio-Signature` for a URL with already-sorted params.
pub fn twilio_signature(auth_token: &str, url: &str, sorted_params: &[(String, String)]) -> String {
    let mut data = url.to_string();
    for (k, v) in sorted_params {
        data.push_str(k);
        data.push_str(v);
    }
    base64::engine::general_purpose::STANDARD.encode(hmac_sha1(auth_token.as_bytes(), data.as_bytes()))
}

/// Constant-time compare of the provided signature against the expected one.
pub fn valid_twilio_signature(auth_token: &str, url: &str, provided: &str) -> bool {
    let expected = twilio_signature(auth_token, url, &[]);
    constant_time_eq(expected.as_bytes(), provided.as_bytes())
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
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
    /// How many mid-call stream ends the source survives by re-entering its
    /// accept loop instead of ending the pipeline. 0 (default) = one-shot,
    /// the pre-2026-07-29 behavior. The redial itself is out-of-process
    /// (scripts/redial-supervisor.sh watches `redial.request`).
    pub max_reconnects: u32,
    /// How long a re-accept (attempt > 0) waits for the redialed call's
    /// stream. Much shorter than `accept_timeout`: the supervisor redials
    /// within seconds, and an expired window ends the stream cleanly.
    pub reaccept_window: Duration,
    /// Where to write the redial request marker on a recoverable drop
    /// (`$EARSHOT_REDIAL_DIR/redial.request`). None = no marker (tests,
    /// probe, one-shot postures).
    pub redial_request_path: Option<std::path::PathBuf>,
    /// Where to write the stream-connected marker on every accepted `start`
    /// (`$EARSHOT_REDIAL_DIR/stream.connected`). The launcher's no-stream
    /// watchdog polls it after each dial (a placed call is not a stream —
    /// 2026-07-29: calls connected and ran to completion while nothing ever
    /// accept()ed). None = no marker (tests, probe, one-shot postures); the
    /// env-gating keeps bridge-probe a bare recorder.
    pub stream_connected_path: Option<std::path::PathBuf>,
    /// Twilio auth token for `X-Twilio-Signature` validation (from
    /// `TWILIO_AUTH_TOKEN`; never in repo). `None` disables validation — a
    /// loud dev/loopback-only posture.
    auth_token: Option<String>,
    /// The exact `<Stream url="wss://…">` Twilio was configured with, used as
    /// the signature base (from `TWILIO_STREAM_URL`). When absent, the URL is
    /// reconstructed from the `Host` header + request target.
    public_url: Option<String>,
}

impl TwilioWsSource {
    /// Binds synchronously so `arm` refuses at construction if the port is
    /// taken (P3), and tests can read the ephemeral port before connecting.
    /// Signature-validation material is read from the environment (same
    /// posture as Kalshi creds — never in the repo).
    pub fn bind(addr: &str, source_id: SourceId) -> Result<Self, SourceError> {
        let listener = StdTcpListener::bind(addr)?;
        listener.set_nonblocking(true)?;
        Ok(TwilioWsSource {
            listener,
            source_id,
            accept_timeout: Duration::from_secs(900),
            idle_timeout: Duration::from_secs(30),
            max_reconnects: 0,
            reaccept_window: Duration::from_secs(120),
            redial_request_path: std::env::var("EARSHOT_REDIAL_DIR")
                .ok()
                .filter(|s| !s.is_empty())
                .map(|d| std::path::Path::new(&d).join("redial.request")),
            stream_connected_path: std::env::var("EARSHOT_REDIAL_DIR")
                .ok()
                .filter(|s| !s.is_empty())
                .map(|d| std::path::Path::new(&d).join("stream.connected")),
            auth_token: std::env::var("TWILIO_AUTH_TOKEN").ok().filter(|s| !s.is_empty()),
            public_url: std::env::var("TWILIO_STREAM_URL").ok().filter(|s| !s.is_empty()),
        })
    }

    /// Explicit signature material (tests / non-env wiring).
    pub fn with_auth(mut self, auth_token: impl Into<String>, public_url: Option<String>) -> Self {
        self.auth_token = Some(auth_token.into());
        self.public_url = public_url;
        self
    }

    pub fn local_addr(&self) -> std::io::Result<std::net::SocketAddr> {
        self.listener.local_addr()
    }
}

/// Handshake callback: enforce `X-Twilio-Signature` before the WS upgrade
/// completes. Rejecting here fails the handshake loudly (403), so a spoofed
/// dialer never becomes an audio source. With no token configured we accept
/// but warn — dev/loopback only.
fn check_signature(
    req: &Request,
    resp: Response,
    auth_token: Option<&str>,
    public_url: Option<&str>,
) -> Result<Response, ErrorResponse> {
    let Some(token) = auth_token else {
        tracing::warn!(
            "X-Twilio-Signature validation DISABLED (no TWILIO_AUTH_TOKEN) — dev/loopback only"
        );
        return Ok(resp);
    };
    let reject = |msg: &str| {
        tracing::error!(msg, "twilio handshake rejected");
        ErrorResponse::new(Some(format!("rejected: {msg}")))
    };
    let provided = req
        .headers()
        .get("X-Twilio-Signature")
        .and_then(|v| v.to_str().ok())
        .ok_or_else(|| {
            let mut e = reject("missing X-Twilio-Signature");
            *e.status_mut() = StatusCode::FORBIDDEN;
            e
        })?;
    let url = match public_url {
        Some(u) => u.to_string(),
        None => {
            let host = req
                .headers()
                .get("host")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("");
            format!("wss://{host}{}", req.uri())
        }
    };
    if valid_twilio_signature(token, &url, provided) {
        tracing::info!("X-Twilio-Signature verified");
        Ok(resp)
    } else {
        let mut e = reject("X-Twilio-Signature mismatch");
        *e.status_mut() = StatusCode::FORBIDDEN;
        Err(e)
    }
}

fn protocol_err(msg: impl Into<String>) -> SourceError {
    SourceError::Protocol(msg.into())
}

type WsStream = tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>;

/// Bounded window for a mid-stream candidate connection to complete its WS
/// handshake, and again for its `start` message to arrive. While either step
/// is in progress the LIVE stream is not being read, so both must stay short
/// (worst case ≈ 2 × 2 s of unread frames, buffered in the kernel — and only
/// when a connection actually lands mid-stream).
const SUPERSEDE_STEP_TIMEOUT: Duration = Duration::from_secs(2);

/// A connection landed while a stream is LIVE (`max_reconnects > 0` only —
/// the one-shot posture never polls the listener mid-stream). Validate it
/// exactly like a first accept (signature-checked handshake), then read up to
/// its `start` event:
/// - DIFFERENT callSid → the redial landing: return the started stream so the
///   caller supersedes the half-open predecessor;
/// - SAME callSid as the current call → our own zombie re-attach
///   (StaleReattach semantics): dropped, current stream keeps running;
/// - handshake failure, malformed/wrong-format `start`, missing callSid, or a
///   step timeout → candidate dropped. A garbage connect must never kill the
///   live audio (same posture as the accept loop's handshake resilience).
async fn try_supersede(
    stream: tokio::net::TcpStream,
    peer: std::net::SocketAddr,
    auth_token: Option<&str>,
    public_url: Option<&str>,
    current_call_sid: Option<&str>,
) -> Option<(WsStream, String)> {
    // TCP_NODELAY: 20 ms media frames must never wait on Nagle.
    let _ = stream.set_nodelay(true);
    let hs = tokio_tungstenite::accept_hdr_async(stream, |req: &Request, resp: Response| {
        check_signature(req, resp, auth_token, public_url)
    });
    let mut ws = match tokio::time::timeout(SUPERSEDE_STEP_TIMEOUT, hs).await {
        Ok(Ok(ws)) => ws,
        Ok(Err(e)) => {
            tracing::warn!(%peer, error = %e, "mid-stream handshake failed — candidate dropped, live stream continues");
            return None;
        }
        Err(_) => {
            tracing::warn!(%peer, "mid-stream handshake timed out — candidate dropped, live stream continues");
            return None;
        }
    };
    let deadline = tokio::time::Instant::now() + SUPERSEDE_STEP_TIMEOUT;
    loop {
        let msg = match tokio::time::timeout_at(deadline, ws.next()).await {
            Err(_) | Ok(None) => {
                tracing::warn!(%peer, "mid-stream candidate closed/sent no start in time — dropped");
                return None;
            }
            Ok(Some(Err(e))) => {
                tracing::warn!(%peer, error = %e, "mid-stream candidate read failed — dropped");
                return None;
            }
            Ok(Some(Ok(m))) => m,
        };
        let text = match msg {
            Message::Text(t) => t,
            _ => continue, // ping/pong/binary before start: nothing that matters yet
        };
        let Ok(v) = serde_json::from_str::<Value>(&text) else {
            tracing::warn!(%peer, "mid-stream candidate sent bad JSON — dropped");
            return None;
        };
        match v["event"].as_str().unwrap_or("?") {
            "connected" => continue,
            "start" => {
                if let Err(e) = check_media_format(&v) {
                    tracing::warn!(%peer, error = %e, "mid-stream candidate start refused — dropped");
                    return None;
                }
                let Some(sid) = v["start"]["callSid"].as_str() else {
                    tracing::warn!(%peer, "mid-stream candidate start has no callSid — dropped");
                    return None;
                };
                if Some(sid) == current_call_sid {
                    tracing::warn!(
                        call_sid = sid,
                        "current call's stream re-attached while live — stale zombie, dropped (not adopted)"
                    );
                    return None;
                }
                tracing::info!(
                    stream_sid = v["start"]["streamSid"].as_str().unwrap_or("?"),
                    call_sid = sid,
                    tracks = ?v["start"]["tracks"],
                    "twilio: superseding stream started (mediaFormat verified 8 kHz μ-law mono)"
                );
                return Some((ws, sid.to_string()));
            }
            other => {
                tracing::warn!(%peer, event = other, "mid-stream candidate event before start — dropped");
                return None;
            }
        }
    }
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
        let auth_token = self.auth_token;
        let public_url = self.public_url;
        let accept_timeout = self.accept_timeout;
        let idle_timeout = self.idle_timeout;
        let max_reconnects = self.max_reconnects;
        let reaccept_window = self.reaccept_window;
        let redial_request_path = self.redial_request_path;
        let stream_connected_path = self.stream_connected_path;
        let source_id = self.source_id;
        let listener = TcpListener::from_std(self.listener)?;
        tracing::info!(addr = %listener.local_addr()?, max_reconnects, "twilio source: waiting for stream connection");

        /// How one accepted stream ended. Recoverable ends may re-enter the
        /// accept loop (mid-call persistence, 2026-07-29); protocol violations
        /// never reach here — they fail loud in place.
        enum StreamEnd {
            Stop,
            SocketClosed,
            CloseFrame,
            Idle,
            WsError(String),
            /// A `start` carrying the PREVIOUS call's callSid after a drop: a
            /// stale half-open stream, not the redial. Never a real call end.
            StaleReattach,
            /// The redial landed while the previous stream was still half-open
            /// (`max_reconnects > 0` only): a signature-validated NEW-callSid
            /// stream accepted mid-loop. Carries the already-started stream;
            /// consumes NO reconnect attempt and requests NO redial.
            Superseded(WsStream),
        }

        let b64 = base64::engine::general_purpose::STANDARD;
        // Hoisted across re-accepts: frame `seq` stays strictly increasing for
        // the whole run (layer obligation — downstream dedup/timing rely on
        // it), and the previous callSid gates out a stale re-attach.
        let mut seq = 0u64;
        let mut last_call_sid: Option<String> = None;
        let mut attempt: u32 = 0;
        // A stream adopted at supersede time: its WS handshake AND `start`
        // were already validated mid-swap, so the next iteration resumes
        // reading it instead of re-entering the accept loop.
        let mut adopted: Option<WsStream> = None;

        loop {
            let (mut ws, mut started) = if let Some(ws) = adopted.take() {
                (ws, true)
            } else {
                // Accept until one connection completes the WS handshake. A public
                // tunnel URL sees stray scanners and spoofed dialers; a failed or
                // rejected handshake must not kill the armed pipeline (that would
                // let any unauthenticated TCP connect deny service mid-event).
                // Fail-loud still applies to the accepted stream itself.
                let window = if attempt == 0 { accept_timeout } else { reaccept_window };
                let deadline = tokio::time::Instant::now() + window;
                let (ws, peer) = loop {
                    let Some(remaining) = deadline
                        .checked_duration_since(tokio::time::Instant::now())
                        .filter(|d| !d.is_zero())
                    else {
                        if attempt == 0 {
                            return Err(protocol_err(
                                "no Twilio connection before accept_timeout — call never landed?",
                            ));
                        }
                        // We survived a drop but no redial landed. The supervisor
                        // owns "is the call actually over" (elapsed time, hard
                        // stop) and chose not to redial — end the stream cleanly
                        // so the pipeline drains and the report is written.
                        tracing::warn!(
                            attempt,
                            reaccept_secs = reaccept_window.as_secs(),
                            frames = seq,
                            "no reconnect within reaccept_window — treating as end of stream"
                        );
                        return Ok(SourceExit::EndOfStream);
                    };
                    let Ok(accepted) = tokio::time::timeout(remaining, listener.accept()).await else {
                        continue; // window elapsed — the deadline check above decides
                    };
                    let (stream, peer) = accepted?;
                    // TCP_NODELAY: 20 ms media frames must never wait on Nagle.
                    let _ = stream.set_nodelay(true);
                    match tokio_tungstenite::accept_hdr_async(stream, |req: &Request, resp: Response| {
                        check_signature(req, resp, auth_token.as_deref(), public_url.as_deref())
                    })
                    .await
                    {
                        Ok(ws) => break (ws, peer),
                        Err(e) => {
                            tracing::warn!(%peer, error = %e, "handshake failed — connection dropped, still waiting for Twilio");
                            continue;
                        }
                    }
                };
                tracing::info!(%peer, attempt, "twilio stream connected");
                (ws, false)
            };

            let mut last_twilio_seq: Option<u64> = None;
            // Idle deadline advances only when the LIVE stream produces a
            // message — listener wakeups (scanner spam) never extend it.
            let mut idle_deadline = tokio::time::Instant::now() + idle_timeout;

            let end = loop {
                let msg = if max_reconnects == 0 {
                    // One-shot posture: the listener is never polled while a
                    // stream is live (byte-identical pre-persistence behavior).
                    match tokio::time::timeout(idle_timeout, ws.next()).await {
                        Err(_) => break StreamEnd::Idle,
                        Ok(None) => {
                            tracing::warn!("twilio socket closed without a stop event");
                            break StreamEnd::SocketClosed;
                        }
                        Ok(Some(Err(e))) => break StreamEnd::WsError(e.to_string()),
                        Ok(Some(Ok(m))) => m,
                    }
                } else {
                    // Redial concurrency: the redialed call must not rot in the
                    // kernel backlog behind THIS half-open socket until it hits
                    // idle_timeout — 2026-07-29 incident: `Recv-Q 3` on the
                    // listener while `nc -z` stayed green, then a burst drain
                    // stamping every buffered frame at drain time. Both arms
                    // are cancel-safe (Stream::next / TcpListener::accept);
                    // only try_supersede's bounded handshake/start steps may
                    // briefly (≤ 2 s each) hold up reads on the live stream.
                    tokio::select! {
                        biased;
                        r = tokio::time::timeout_at(idle_deadline, ws.next()) => match r {
                            Err(_) => break StreamEnd::Idle,
                            Ok(None) => {
                                tracing::warn!("twilio socket closed without a stop event");
                                break StreamEnd::SocketClosed;
                            }
                            Ok(Some(Err(e))) => break StreamEnd::WsError(e.to_string()),
                            Ok(Some(Ok(m))) => m,
                        },
                        accepted = listener.accept() => {
                            let Ok((stream, peer)) = accepted else {
                                continue; // transient accept error must not kill live audio
                            };
                            match try_supersede(
                                stream,
                                peer,
                                auth_token.as_deref(),
                                public_url.as_deref(),
                                last_call_sid.as_deref(),
                            )
                            .await
                            {
                                Some((new_ws, new_sid)) => {
                                    tracing::warn!(
                                        %peer,
                                        old_call_sid = last_call_sid.as_deref().unwrap_or(""),
                                        new_call_sid = %new_sid,
                                        frames = seq,
                                        "REDIAL LANDED mid-stream — superseding the half-open stream with the new call"
                                    );
                                    last_call_sid = Some(new_sid);
                                    break StreamEnd::Superseded(new_ws);
                                }
                                None => continue, // logged inside; live stream keeps running
                            }
                        }
                    }
                };
                idle_deadline = tokio::time::Instant::now() + idle_timeout;

                let text = match msg {
                    Message::Text(t) => t,
                    Message::Ping(p) => {
                        let _ = ws.send(Message::Pong(p)).await;
                        continue;
                    }
                    Message::Close(_) => {
                        tracing::info!("twilio websocket close frame");
                        break StreamEnd::CloseFrame;
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
                        let call_sid = v["start"]["callSid"].as_str().map(str::to_string);
                        if attempt > 0 && call_sid.is_some() && call_sid == last_call_sid {
                            tracing::warn!(
                                call_sid = call_sid.as_deref().unwrap_or("?"),
                                "twilio: previous call's stream re-attached after a drop — stale, dropping it and waiting for the redial"
                            );
                            break StreamEnd::StaleReattach;
                        }
                        started = true;
                        tracing::info!(
                            stream_sid = v["start"]["streamSid"].as_str().unwrap_or("?"),
                            call_sid = call_sid.as_deref().unwrap_or("?"),
                            tracks = ?v["start"]["tracks"],
                            attempt,
                            "twilio: stream started (mediaFormat verified 8 kHz μ-law mono)"
                        );
                        last_call_sid = call_sid;
                        write_stream_connected(
                            stream_connected_path.as_deref(),
                            last_call_sid.as_deref(),
                            attempt,
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
                            source: source_id,
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
                        // event-autorun.sh greps this exact line for its
                        // call-complete heuristic — keep it on every stop.
                        tracing::info!(frames = seq, "twilio: stream stopped");
                        break StreamEnd::Stop;
                    }
                    other => tracing::debug!(event = other, "twilio: ignored event"),
                }
            };

            match end {
                // The redial landed mid-stream: adopt the already-started new
                // stream (the old half-open socket drops here, with `ws`).
                // Deliberately consumes NO reconnect attempt — it IS the
                // redial arriving — and requests NO redial.
                StreamEnd::Superseded(new_ws) => {
                    // The adopted stream's `start` was consumed in
                    // try_supersede, so mark connection here (last_call_sid
                    // already holds the new sid).
                    write_stream_connected(
                        stream_connected_path.as_deref(),
                        last_call_sid.as_deref(),
                        attempt,
                    );
                    adopted = Some(new_ws);
                    continue;
                }
                // Stale stream: drop it and keep waiting for the real redial.
                // Deliberately does NOT consume a reconnect attempt (it is our
                // own zombie, not a new drop); the re-accept window resets.
                StreamEnd::StaleReattach => continue,
                end if attempt >= max_reconnects => {
                    // Budget exhausted (0 = the pre-redial one-shot): map each
                    // end to its historical return, byte-for-byte.
                    return match end {
                        StreamEnd::Stop | StreamEnd::CloseFrame | StreamEnd::SocketClosed => {
                            Ok(SourceExit::EndOfStream)
                        }
                        StreamEnd::Idle => {
                            Err(protocol_err("stream idle past idle_timeout — dead call"))
                        }
                        StreamEnd::WsError(e) => Err(protocol_err(format!("websocket read: {e}"))),
                        StreamEnd::StaleReattach | StreamEnd::Superseded(_) => {
                            unreachable!("handled above")
                        }
                    };
                }
                end => {
                    attempt += 1;
                    // Marker `reason` vocabulary is a wire contract with
                    // scripts/redial-supervisor.sh — exactly one of
                    // stop|idle|socket_closed|close_frame|ws_error.
                    let (reason, detail) = match &end {
                        StreamEnd::Stop => ("stop", None),
                        StreamEnd::SocketClosed => ("socket_closed", None),
                        StreamEnd::CloseFrame => ("close_frame", None),
                        StreamEnd::Idle => ("idle", None),
                        StreamEnd::WsError(e) => ("ws_error", Some(e.as_str())),
                        StreamEnd::StaleReattach | StreamEnd::Superseded(_) => {
                            unreachable!("handled above")
                        }
                    };
                    tracing::warn!(
                        attempt,
                        max_reconnects,
                        reason,
                        detail,
                        frames = seq,
                        "twilio stream ended mid-call — requesting redial, re-entering accept"
                    );
                    request_redial(redial_request_path.as_deref(), last_call_sid.as_deref(), attempt, reason);
                }
            }
        }
    }
}

/// Ask the out-of-process supervisor for a redial: atomically (re)write the
/// marker file it watches (`$EARSHOT_REDIAL_DIR/redial.request`). The watch
/// signal is the file's mtime; the body is the pinned wire contract with
/// `scripts/redial-supervisor.sh`:
/// `{"unix_ts":<int-seconds>,"old_call_sid":"<sid-or-empty>","attempt":<int>,
/// "reason":"stop"|"idle"|"socket_closed"|"close_frame"|"ws_error"}`.
fn request_redial(path: Option<&std::path::Path>, old_call_sid: Option<&str>, attempt: u32, reason: &str) {
    let Some(path) = path else {
        tracing::warn!("no EARSHOT_REDIAL_DIR set — re-accept is open but nobody was asked to redial");
        return;
    };
    let unix_ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let sid = old_call_sid.unwrap_or("");
    let body = format!(
        "{{\"unix_ts\":{unix_ts},\"old_call_sid\":\"{sid}\",\"attempt\":{attempt},\"reason\":\"{reason}\"}}\n"
    );
    if let Err(e) = crate::fsio::write_atomic(path, body.as_bytes()) {
        tracing::error!(path = %path.display(), error = %e, "could not write redial request");
    }
}

/// Tell the launcher's no-stream watchdog a stream actually landed:
/// atomically (re)write `$EARSHOT_REDIAL_DIR/stream.connected`. Written on
/// every accepted `start` (attempt 0, redials, supersede adoptions). Body:
/// `{"unix_ts":<int-seconds>,"call_sid":"<sid-or-empty>","attempt":<int>}`.
/// Absent path = no marker (bridge-probe stays a bare recorder).
fn write_stream_connected(path: Option<&std::path::Path>, call_sid: Option<&str>, attempt: u32) {
    let Some(path) = path else { return };
    let unix_ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let sid = call_sid.unwrap_or("");
    let body = format!("{{\"unix_ts\":{unix_ts},\"call_sid\":\"{sid}\",\"attempt\":{attempt}}}\n");
    if let Err(e) = crate::fsio::write_atomic(path, body.as_bytes()) {
        tracing::error!(path = %path.display(), error = %e, "could not write stream-connected marker");
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
    fn hmac_sha1_matches_rfc2202_vector() {
        // RFC 2202 test case 2 — pins the crypto independently of Twilio.
        let mac = hmac_sha1(b"Jefe", b"what do ya want for nothing?");
        let hex: String = mac.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(hex, "effcdf6ae5eb2fa2d27416d5f184df9c259a7c79");
    }

    #[test]
    fn signature_round_trips_and_rejects_tampering() {
        let token = "test_auth_token_12345";
        let url = "wss://box.example.com/audio";
        let sig = twilio_signature(token, url, &[]);
        assert_eq!(sig.len(), 28, "base64 of a 20-byte SHA1 digest");
        assert!(valid_twilio_signature(token, url, &sig));
        // Wrong token, wrong url, tampered signature, wrong length: all reject.
        assert!(!valid_twilio_signature("other_token", url, &sig));
        assert!(!valid_twilio_signature(token, "wss://evil.example/audio", &sig));
        let mut bad = sig.clone();
        bad.replace_range(0..1, if sig.starts_with('A') { "B" } else { "A" });
        assert!(!valid_twilio_signature(token, url, &bad));
        assert!(!valid_twilio_signature(token, url, "short"));
    }

    #[test]
    fn signature_includes_sorted_params() {
        // POST-style params concatenate key+value in key order after the URL.
        let token = "tok";
        let url = "https://x/y";
        let with = twilio_signature(token, url, &[("a".into(), "1".into()), ("b".into(), "2".into())]);
        let url_only = twilio_signature(token, url, &[]);
        assert_ne!(with, url_only);
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
