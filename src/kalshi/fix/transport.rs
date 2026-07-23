//! Tokio driver for the FIX session: TCP(+TLS) socket, reader task feeding
//! the sans-io `SessionMachine`, a 1 s ticker for its timers, and the
//! ClOrdID → oneshot correlation map that turns an ExecutionReport into the
//! `send_order` return value.
//!
//! Locking contract: `Io` (machine + write half) lives behind ONE async
//! mutex, and every sequence-number allocation writes its message to the
//! socket before releasing that lock — sequence order IS wire order.
//! Heartbeats double as the connection warmer (FIX's native analogue of
//! `RestClient::spawn_conn_warmer`).
//!
//! Reconnect: on any disconnect the supervisor retries with capped backoff
//! (floor 1 s — Logon is rate-limited, F10) and a fresh reset session
//! (KalshiNR, 141=Y). In-flight orders fail over to the executor's
//! ambiguous-error path (REST status query by client id) — never a blind
//! resend here.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex as StdMutex, Weak};
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::{oneshot, Mutex};

use super::encode::{FixOrderTemplate, FixWriter};
use super::parse::frame_len;
use super::session::{Action, OwnedExecReport, SessionMachine};
use super::FixConfig;
use crate::clock::MonotonicNs;
use crate::kalshi::auth::{now_ms, Signer};
use crate::kalshi::KalshiError;

type Reader = Box<dyn AsyncRead + Send + Unpin>;
type Writer = Box<dyn AsyncWrite + Send + Unpin>;

/// How long `connect` waits for the server's Logon.
const LOGON_WAIT: Duration = Duration::from_secs(5);
/// Reconnect backoff: 1 s floor (Logon is rate-limited), 30 s cap.
const BACKOFF_FLOOR: Duration = Duration::from_secs(1);
const BACKOFF_CAP: Duration = Duration::from_secs(30);

struct Io {
    machine: SessionMachine,
    writer: Writer,
    connected: bool,
}

/// An ExecutionReport delivered to a waiting order, with the receive
/// timestamp (the FIX analogue of REST's response-headers instant).
type ErSlot = oneshot::Sender<(OwnedExecReport, MonotonicNs)>;

pub struct FixTransport {
    cfg: FixConfig,
    signer: Arc<Signer>,
    io: Mutex<Io>,
    pending: StdMutex<HashMap<String, ErSlot>>,
    er_timeout: Duration,
    shutdown: AtomicBool,
}

impl FixTransport {
    /// Connect, complete the Logon handshake (fail-fast — mirrors the
    /// `wait_for_books` arm posture), then spawn the reader + ticker +
    /// reconnect supervisor. `er_timeout` bounds how long a fired order
    /// waits for its ExecutionReport before the caller falls back to the
    /// ambiguous-error status query.
    pub async fn connect(
        cfg: FixConfig,
        signer: Arc<Signer>,
        er_timeout: Duration,
    ) -> Result<Arc<Self>, KalshiError> {
        let (machine, reader, writer, leftover) = establish(&cfg, &signer).await?;
        let this = Arc::new(FixTransport {
            cfg,
            signer,
            io: Mutex::new(Io { machine, writer, connected: true }),
            pending: StdMutex::new(HashMap::new()),
            er_timeout,
            shutdown: AtomicBool::new(false),
        });
        spawn_reader(Arc::downgrade(&this), reader, leftover);
        spawn_ticker(Arc::downgrade(&this));
        Ok(this)
    }

    /// Fire one prestaged 35=D and await its terminal ExecutionReport.
    /// `cl_ord_id` must be the template's ClOrdID (the correlation key —
    /// identical to the REST idempotency key).
    pub async fn send_order(
        &self,
        template: &FixOrderTemplate,
        cl_ord_id: &str,
    ) -> Result<(OwnedExecReport, MonotonicNs, MonotonicNs), KalshiError> {
        let (tx, rx) = oneshot::channel();
        self.pending.lock().unwrap().insert(cl_ord_id.to_string(), tx);
        let send_ts = {
            let mut io = self.io.lock().await;
            if !io.connected {
                self.pending.lock().unwrap().remove(cl_ord_id);
                return Err(KalshiError::Ws("fix: disconnected".into()));
            }
            let Some((seq, sending_time)) = io.machine.send_app(now_ms() as u64) else {
                self.pending.lock().unwrap().remove(cl_ord_id);
                return Err(KalshiError::Ws("fix: session not established".into()));
            };
            let (mut w, mut out) = (FixWriter::new(), Vec::with_capacity(256));
            template.assemble(seq, &sending_time, &mut w, &mut out);
            let send_ts = MonotonicNs::now();
            write_or_drop(&mut io, &out).await.map_err(|e| {
                self.pending.lock().unwrap().remove(cl_ord_id);
                KalshiError::Ws(format!("fix write: {e}"))
            })?;
            send_ts
            // Lock released here: seq order == wire order held.
        };
        match tokio::time::timeout(self.er_timeout, rx).await {
            Ok(Ok((er, recv_ts))) => Ok((er, send_ts, recv_ts)),
            Ok(Err(_)) => Err(KalshiError::Ws("fix: connection lost awaiting ExecutionReport".into())),
            Err(_) => {
                self.pending.lock().unwrap().remove(cl_ord_id);
                Err(KalshiError::Ws(format!(
                    "fix: no ExecutionReport within {:?}",
                    self.er_timeout
                )))
            }
        }
    }

    pub async fn is_connected(&self) -> bool {
        self.io.lock().await.connected
    }

    pub fn config(&self) -> &FixConfig {
        &self.cfg
    }

    /// Graceful teardown: Logout, stop reconnecting.
    pub async fn shutdown(&self) {
        self.shutdown.store(true, Ordering::SeqCst);
        let mut io = self.io.lock().await;
        if io.connected {
            for a in io.machine.start_logout(now_ms() as u64) {
                if let Action::Send(bytes) = a {
                    let _ = write_or_drop(&mut io, &bytes).await;
                }
            }
            io.connected = false;
        }
    }

    /// Run the machine's actions while holding the io lock; returns false if
    /// the session disconnected.
    async fn apply_actions(&self, io: &mut Io, actions: Vec<Action>, recv_ts: MonotonicNs) -> bool {
        for action in actions {
            match action {
                Action::Send(bytes) => {
                    if write_or_drop(io, &bytes).await.is_err() {
                        io.connected = false;
                        return false;
                    }
                }
                Action::Deliver(er) => {
                    if !er.terminal {
                        tracing::debug!(cl_ord_id = ?er.cl_ord_id, status = %er.status, "non-terminal ER (held)");
                        continue;
                    }
                    let slot = er
                        .cl_ord_id
                        .as_deref()
                        .and_then(|id| self.pending.lock().unwrap().remove(id));
                    match slot {
                        Some(tx) => {
                            let _ = tx.send((er, recv_ts));
                        }
                        None => {
                            tracing::warn!(cl_ord_id = ?er.cl_ord_id, order_id = ?er.order_id,
                                "uncorrelated terminal ExecutionReport");
                        }
                    }
                }
                Action::Disconnect { reason } => {
                    tracing::warn!(%reason, "fix session disconnect");
                    io.connected = false;
                    return false;
                }
            }
        }
        true
    }

    /// Drop every waiter (their `rx` errors → callers fall back to the REST
    /// status query). Called when the connection dies.
    fn fail_pending(&self) {
        self.pending.lock().unwrap().clear();
    }
}

async fn write_or_drop(io: &mut Io, bytes: &[u8]) -> std::io::Result<()> {
    io.writer.write_all(bytes).await?;
    io.writer.flush().await
}

/// Dial + TLS + Logon handshake. Returns the established machine, the split
/// halves, and any bytes read past the handshake (must be fed to the reader).
async fn establish(
    cfg: &FixConfig,
    signer: &Arc<Signer>,
) -> Result<(SessionMachine, Reader, Writer, Vec<u8>), KalshiError> {
    let tcp = TcpStream::connect((cfg.host.as_str(), cfg.port))
        .await
        .map_err(|e| KalshiError::Ws(format!("fix connect {}:{}: {e}", cfg.host, cfg.port)))?;
    tcp.set_nodelay(true).ok(); // an order is one small packet (same rationale as REST)
    let (mut reader, mut writer): (Reader, Writer) = if cfg.tls {
        let connector = tokio_native_tls::native_tls::TlsConnector::new()
            .map_err(|e| KalshiError::Ws(format!("tls init: {e}")))?;
        let stream = tokio_native_tls::TlsConnector::from(connector)
            .connect(&cfg.host, tcp)
            .await
            .map_err(|e| KalshiError::Ws(format!("tls handshake: {e}")))?;
        let (r, w) = tokio::io::split(stream);
        (Box::new(r), Box::new(w))
    } else {
        let (r, w) = tcp.into_split();
        (Box::new(r), Box::new(w))
    };

    let mut machine = SessionMachine::new(cfg.clone(), signer.clone(), (1, 1));
    for action in machine.connect(now_ms() as u64) {
        if let Action::Send(bytes) = action {
            writer
                .write_all(&bytes)
                .await
                .map_err(|e| KalshiError::Ws(format!("fix logon write: {e}")))?;
            writer.flush().await.map_err(|e| KalshiError::Ws(format!("fix logon flush: {e}")))?;
        }
    }

    let mut buf = Vec::with_capacity(4096);
    let deadline = tokio::time::Instant::now() + LOGON_WAIT;
    while !machine.is_established() {
        let mut chunk = [0u8; 4096];
        let n = tokio::time::timeout_at(deadline, reader.read(&mut chunk))
            .await
            .map_err(|_| KalshiError::Ws("fix: logon unanswered (timeout)".into()))?
            .map_err(|e| KalshiError::Ws(format!("fix logon read: {e}")))?;
        if n == 0 {
            return Err(KalshiError::Ws("fix: server closed during logon".into()));
        }
        buf.extend_from_slice(&chunk[..n]);
        while let Some(len) = frame_len(&buf) {
            let frame: Vec<u8> = buf.drain(..len).collect();
            for action in machine.on_message(&frame, now_ms() as u64) {
                match action {
                    Action::Send(bytes) => {
                        writer
                            .write_all(&bytes)
                            .await
                            .map_err(|e| KalshiError::Ws(format!("fix write: {e}")))?;
                        writer.flush().await.map_err(|e| KalshiError::Ws(format!("fix flush: {e}")))?;
                    }
                    Action::Deliver(_) => {} // no orders can be in flight yet
                    Action::Disconnect { reason } => {
                        return Err(KalshiError::Ws(format!("fix logon refused: {reason}")));
                    }
                }
            }
        }
    }
    Ok((machine, reader, writer, buf))
}

fn spawn_reader(weak: Weak<FixTransport>, mut reader: Reader, mut buf: Vec<u8>) {
    tokio::spawn(async move {
        loop {
            // Drain complete frames already buffered.
            loop {
                let Some(len) = frame_len(&buf) else { break };
                let frame: Vec<u8> = buf.drain(..len).collect();
                let recv_ts = MonotonicNs::now();
                let Some(this) = weak.upgrade() else { return };
                let mut io = this.io.lock().await;
                let actions = io.machine.on_message(&frame, now_ms() as u64);
                if !this.apply_actions(&mut io, actions, recv_ts).await {
                    drop(io);
                    this.fail_pending();
                    spawn_reconnect(Arc::downgrade(&this));
                    return;
                }
            }
            let mut chunk = [0u8; 4096];
            match reader.read(&mut chunk).await {
                Ok(0) | Err(_) => {
                    let Some(this) = weak.upgrade() else { return };
                    tracing::warn!("fix socket closed");
                    this.io.lock().await.connected = false;
                    this.fail_pending();
                    spawn_reconnect(Arc::downgrade(&this));
                    return;
                }
                Ok(n) => buf.extend_from_slice(&chunk[..n]),
            }
        }
    });
}

fn spawn_ticker(weak: Weak<FixTransport>) {
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tick.tick().await;
            let Some(this) = weak.upgrade() else { return };
            let mut io = this.io.lock().await;
            if !io.connected {
                return; // reconnect supervisor owns recovery; new ticker spawns with it
            }
            let actions = io.machine.on_tick(now_ms() as u64);
            if !this.apply_actions(&mut io, actions, MonotonicNs::now()).await {
                drop(io);
                this.fail_pending();
                spawn_reconnect(Arc::downgrade(&this));
                return;
            }
        }
    });
}

fn spawn_reconnect(weak: Weak<FixTransport>) {
    tokio::spawn(async move {
        let mut backoff = BACKOFF_FLOOR;
        loop {
            tokio::time::sleep(backoff).await;
            let Some(this) = weak.upgrade() else { return };
            if this.shutdown.load(Ordering::SeqCst) {
                return;
            }
            match establish(&this.cfg, &this.signer).await {
                Ok((machine, reader, writer, leftover)) => {
                    tracing::info!("fix session re-established");
                    {
                        let mut io = this.io.lock().await;
                        io.machine = machine;
                        io.writer = writer;
                        io.connected = true;
                    }
                    spawn_reader(Arc::downgrade(&this), reader, leftover);
                    spawn_ticker(Arc::downgrade(&this));
                    return;
                }
                Err(e) => {
                    tracing::warn!(next_retry = ?backoff, "fix reconnect failed: {e}");
                    backoff = (backoff * 2).min(BACKOFF_CAP);
                }
            }
        }
    });
}
