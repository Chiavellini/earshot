//! Tokio driver for the FIX session: TCP(+TLS) socket, reader task feeding
//! the sans-io `SessionMachine`, a 1 s ticker for its timers, and the
//! ClOrdID → channel correlation map that delivers every ExecutionReport
//! observed for an order while the caller waits for a terminal report.
//!
//! Locking contract: the writer mutex is the outer serialization door for
//! every action that can allocate an outbound sequence. The session mutex is
//! separate, so inbound ER/reject frames (which never allocate a response)
//! can be validated and correlated while a socket write/flush is pending.
//! Allocation still occurs while holding the writer door, so sequence order
//! IS wire order.
//! Heartbeats double as the connection warmer (FIX's native analogue of
//! `RestClient::spawn_conn_warmer`).
//!
//! Reconnect: on any disconnect the supervisor retries with capped backoff
//! (floor 1 s — Logon is rate-limited, F10) and a fresh reset session
//! (KalshiNR, 141=Y). In-flight orders fail over to the executor's
//! ambiguous-error path (REST status query by client id) — never a blind
//! resend here.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex as StdMutex, Weak};
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::{mpsc, Mutex};

use super::encode::{FixOrderTemplate, FixWriter};
use super::observation_tape::FixObservationTap;
use super::parse::{frame_len, FixMsg};
use super::session::{Action, OwnedExecReport, SessionMachine};
use super::FixConfig;
use crate::clock::MonotonicNs;
use crate::kalshi::auth::{now_ms, Signer};
use crate::kalshi::KalshiError;

type Reader = Box<dyn AsyncRead + Send + Unpin>;
type Writer = Box<dyn AsyncWrite + Send + Unpin>;

struct Established {
    machine: SessionMachine,
    reader: Reader,
    writer: Writer,
    leftover: Vec<u8>,
}

/// How long `connect` waits for the server's Logon.
const LOGON_WAIT: Duration = Duration::from_secs(5);
/// Reconnect backoff: 1 s floor (Logon is rate-limited), 30 s cap.
const BACKOFF_FLOOR: Duration = Duration::from_secs(1);
const BACKOFF_CAP: Duration = Duration::from_secs(30);

struct SessionIo {
    machine: SessionMachine,
    connected: bool,
    generation: u64,
}

/// One parsed ExecutionReport observed for a waiting order.
#[derive(Debug, Clone)]
pub struct FixExecutionObservation {
    pub report: OwnedExecReport,
    /// Instant the complete FIX frame was removed from the socket buffer.
    pub recv_ts: MonotonicNs,
    /// Instant parsing and ClOrdID correlation completed.
    pub ack_ts: MonotonicNs,
    /// Exact validated FIX frame as received, including BodyLength and
    /// CheckSum. This is the byte evidence behind `report`; it is preserved
    /// unchanged on both the live observation stream and traced result.
    pub raw: Vec<u8>,
}

/// FIX reject category preserved at the transport boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FixRejectKind {
    /// Session-level Reject (35=3).
    Session,
    /// BusinessMessageReject (35=j).
    Business,
}

/// A typed session/business reject. These messages are not ExecutionReports,
/// but they are exchange evidence and must never disappear into logs.
#[derive(Debug, Clone)]
pub struct FixRejectObservation {
    pub kind: FixRejectKind,
    pub recv_ts: MonotonicNs,
    pub ack_ts: MonotonicNs,
    /// Tag 45, when the reject references an outbound sequence number.
    pub ref_seq: Option<u64>,
    /// Tag 372, when supplied by a BusinessMessageReject.
    pub ref_msg_type: Option<String>,
    /// Tag 379, when supplied by a BusinessMessageReject.
    pub business_ref_id: Option<String>,
    pub reason: Option<String>,
    pub text: Option<String>,
    /// Exact framed FIX bytes, including BodyLength and CheckSum.
    pub raw: Vec<u8>,
    /// Exact correlation when tag 45 maps to this generation's outbound
    /// order sequence or tag 379 equals an in-flight ClOrdID.
    pub client_order_id: Option<String>,
}

/// Every order-relevant inbound observation, in socket receive order.
#[derive(Debug, Clone)]
pub enum FixOrderObservation {
    Execution(FixExecutionObservation),
    Reject(FixRejectObservation),
}

/// Ordered envelope delivered by [`FixTransport::subscribe_observations`].
/// `sequence` is process-local and strictly increasing across reconnects.
#[derive(Debug, Clone)]
pub struct FixObservationEnvelope {
    pub sequence: u64,
    pub generation: u64,
    pub observation: FixOrderObservation,
}

/// Immutable endpoint identity attached to the durable pre-write receipt.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FixEndpointIdentity {
    pub host: String,
    pub port: u16,
    pub tls: bool,
    pub sender_comp_id: String,
    pub target_comp_id: String,
}

/// Exact order bytes and identity presented to the pre-write durability gate.
/// The frame already contains its assigned MsgSeqNum, SendingTime,
/// TransactTime, BodyLength, and CheckSum.
#[derive(Debug, Clone)]
pub struct FixPreWriteEvidence {
    pub generation: u64,
    pub endpoint: FixEndpointIdentity,
    pub client_order_id: String,
    pub msg_seq_num: u64,
    pub sending_time: String,
    /// Sampled after assembly and before awaiting the evidence gate. This is
    /// not `send_ts`; the socket write has not yet been entered.
    pub prepared_ts: MonotonicNs,
    pub outbound_frame: Arc<[u8]>,
}

pub type FixPreWriteFuture<'a> = Pin<Box<dyn Future<Output = Result<(), KalshiError>> + Send + 'a>>;

/// Awaitable durability gate invoked after exact frame assembly and before
/// `send_ts`/`write_all`. Implementations may enqueue/await a journal writer,
/// but must not perform blocking disk I/O on the Tokio runtime thread.
pub trait FixPreWriteHook: Send + Sync {
    fn before_write<'a>(&'a self, evidence: &'a FixPreWriteEvidence) -> FixPreWriteFuture<'a>;
}

/// A send was attempted but no terminal ExecutionReport was observed.
#[derive(Debug)]
pub struct FixAttemptFailure {
    pub error: KalshiError,
    /// Latest non-terminal report, if the exchange acknowledged progress
    /// before the connection or terminal wait failed.
    pub last_observation: Option<FixExecutionObservation>,
    /// Exact correlated FIX reject, when one resolved this attempt.
    pub protocol_reject: Option<FixRejectObservation>,
}

/// Evidence retained after the first socket write attempt.
#[derive(Debug)]
pub struct FixSendAttempt {
    /// Sampled immediately before `write_all`; its presence means the wire
    /// outcome is ambiguous even when the write itself reports an error.
    pub send_ts: MonotonicNs,
    /// `Ok` always contains a terminal report. `Err` retains the latest
    /// non-terminal observation and an exactly correlated FIX reject, if any.
    pub outcome: Result<FixExecutionObservation, FixAttemptFailure>,
}

/// Rich FIX submission result. This distinction is intentionally made at the
/// transport boundary so callers never have to infer whether a failed order
/// may have reached the exchange.
#[derive(Debug)]
#[must_use = "FIX submission results carry wire-attempt evidence"]
pub enum FixSendResult {
    /// The socket write was never attempted and no `send_ts` exists.
    NotAttempted { error: KalshiError },
    /// A write was attempted. The enclosed timestamp survives write errors,
    /// disconnects, terminal-report timeouts, and successful terminal acks.
    Attempted(FixSendAttempt),
}

/// Full attempted-order trace. Unlike the compatibility result, this retains
/// every New/Partial/Trade/terminal ER and any correlated FIX reject.
#[derive(Debug)]
pub struct FixTracedSendAttempt {
    pub send_ts: MonotonicNs,
    pub observations: Vec<FixOrderObservation>,
    pub first_response_recv_ts: Option<MonotonicNs>,
    pub first_response_ack_ts: Option<MonotonicNs>,
    pub outcome: Result<FixExecutionObservation, FixAttemptFailure>,
}

/// Evidence-complete FIX submission result. Existing callers may continue to
/// use [`FixTransport::submit_order`]; evidence integrations should consume
/// this result and subscribe to the live observation door before firing.
#[derive(Debug)]
#[must_use = "FIX traced submission results carry ordered exchange evidence"]
pub enum FixTracedSendResult {
    NotAttempted { error: KalshiError },
    Attempted(FixTracedSendAttempt),
}

/// Every correlated order observation is sent through this slot.
type OrderSlot = mpsc::UnboundedSender<FixOrderObservation>;

#[derive(Clone)]
struct PendingOrder {
    generation: u64,
    tx: OrderSlot,
}

#[derive(Default)]
struct AttemptEvidence {
    observations: Vec<FixOrderObservation>,
    first_response_recv_ts: Option<MonotonicNs>,
    first_response_ack_ts: Option<MonotonicNs>,
    last_execution: Option<FixExecutionObservation>,
}

enum AttemptResolution {
    Terminal(FixExecutionObservation),
    Reject(FixRejectObservation),
}

impl AttemptEvidence {
    fn record(&mut self, observation: FixOrderObservation) -> Option<AttemptResolution> {
        let (recv_ts, ack_ts) = match &observation {
            FixOrderObservation::Execution(value) => (value.recv_ts, value.ack_ts),
            FixOrderObservation::Reject(value) => (value.recv_ts, value.ack_ts),
        };
        self.first_response_recv_ts.get_or_insert(recv_ts);
        self.first_response_ack_ts.get_or_insert(ack_ts);

        let resolution = match &observation {
            FixOrderObservation::Execution(value) => {
                self.last_execution = Some(value.clone());
                value
                    .report
                    .terminal
                    .then(|| AttemptResolution::Terminal(value.clone()))
            }
            FixOrderObservation::Reject(value) => Some(AttemptResolution::Reject(value.clone())),
        };
        self.observations.push(observation);
        resolution
    }

    fn attempted(
        self,
        send_ts: MonotonicNs,
        outcome: Result<FixExecutionObservation, FixAttemptFailure>,
    ) -> FixTracedSendResult {
        FixTracedSendResult::Attempted(FixTracedSendAttempt {
            send_ts,
            observations: self.observations,
            first_response_recv_ts: self.first_response_recv_ts,
            first_response_ack_ts: self.first_response_ack_ts,
            outcome,
        })
    }

    fn failed(
        self,
        send_ts: MonotonicNs,
        error: KalshiError,
        protocol_reject: Option<FixRejectObservation>,
    ) -> FixTracedSendResult {
        let last_observation = self.last_execution.clone();
        self.attempted(
            send_ts,
            Err(FixAttemptFailure {
                error,
                last_observation,
                protocol_reject,
            }),
        )
    }
}

pub struct FixTransport {
    cfg: FixConfig,
    signer: Arc<Signer>,
    self_weak: Weak<FixTransport>,
    io: Mutex<SessionIo>,
    /// Socket writes are serialized independently from session state. This
    /// lets the reader validate/correlate ERs while a write or flush is
    /// pending, without weakening sequence-order serialization.
    writer: Mutex<Writer>,
    pending: StdMutex<HashMap<String, PendingOrder>>,
    outbound_orders: StdMutex<HashMap<(u64, u64), String>>,
    observers: StdMutex<Vec<mpsc::UnboundedSender<FixObservationEnvelope>>>,
    /// Mandatory run banker, installed before dial/logon when present. Unlike
    /// best-effort subscribers, a failed offer makes the transport unusable.
    mandatory_observer: Option<FixObservationTap>,
    observation_sequence: Arc<AtomicU64>,
    pre_write_hook: Option<Arc<dyn FixPreWriteHook>>,
    er_timeout: Duration,
    shutdown: AtomicBool,
    reconnecting: AtomicBool,
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
        Self::connect_inner(cfg, signer, er_timeout, None, None).await
    }

    /// Connect with a mandatory pre-write evidence gate. The hook is fixed
    /// for this transport instance so it cannot be detached after arming.
    pub async fn connect_with_pre_write_hook(
        cfg: FixConfig,
        signer: Arc<Signer>,
        er_timeout: Duration,
        hook: Arc<dyn FixPreWriteHook>,
    ) -> Result<Arc<Self>, KalshiError> {
        Self::connect_inner(cfg, signer, er_timeout, Some(hook), None).await
    }

    /// Connect with a mandatory, preinstalled raw observation banker. The tap
    /// exists before TCP dial and Logon, so order-relevant frames coalesced with
    /// the Logon response cannot fall into the post-connect subscription gap.
    /// A failed tap offer fails the handshake or shuts down the live session.
    pub async fn connect_with_mandatory_observer(
        cfg: FixConfig,
        signer: Arc<Signer>,
        er_timeout: Duration,
        pre_write_hook: Option<Arc<dyn FixPreWriteHook>>,
        mandatory_observer: FixObservationTap,
    ) -> Result<Arc<Self>, KalshiError> {
        Self::connect_inner(
            cfg,
            signer,
            er_timeout,
            pre_write_hook,
            Some(mandatory_observer),
        )
        .await
    }

    async fn connect_inner(
        cfg: FixConfig,
        signer: Arc<Signer>,
        er_timeout: Duration,
        pre_write_hook: Option<Arc<dyn FixPreWriteHook>>,
        mandatory_observer: Option<FixObservationTap>,
    ) -> Result<Arc<Self>, KalshiError> {
        ensure_mandatory_observer_healthy(mandatory_observer.as_ref())?;
        let observation_sequence = Arc::new(AtomicU64::new(0));
        let Established {
            machine,
            reader,
            writer,
            leftover,
        } = establish(
            &cfg,
            &signer,
            1,
            mandatory_observer.as_ref(),
            &observation_sequence,
        )
        .await?;
        ensure_mandatory_observer_healthy(mandatory_observer.as_ref())?;
        let this = Arc::new_cyclic(|self_weak| FixTransport {
            cfg,
            signer,
            self_weak: self_weak.clone(),
            io: Mutex::new(SessionIo {
                machine,
                connected: true,
                generation: 1,
            }),
            writer: Mutex::new(writer),
            pending: StdMutex::new(HashMap::new()),
            outbound_orders: StdMutex::new(HashMap::new()),
            observers: StdMutex::new(Vec::new()),
            mandatory_observer,
            observation_sequence,
            pre_write_hook,
            er_timeout,
            shutdown: AtomicBool::new(false),
            reconnecting: AtomicBool::new(false),
        });
        spawn_reader(Arc::downgrade(&this), 1, reader, leftover);
        spawn_ticker(Arc::downgrade(&this), 1);
        Ok(this)
    }

    /// Subscribe to the lossless in-process observation door.
    ///
    /// Register once at arm time, before any order can fire. Sending into the
    /// returned unbounded channel never performs disk I/O or awaits the
    /// consumer. Slow durable sinks therefore cannot block the FIX reader;
    /// their own queue health remains an integration-layer responsibility.
    pub fn subscribe_observations(&self) -> mpsc::UnboundedReceiver<FixObservationEnvelope> {
        let (tx, rx) = mpsc::unbounded_channel();
        self.observers.lock().unwrap().push(tx);
        rx
    }

    /// Fire one prestaged 35=D and preserve evidence across every failure
    /// boundary. `cl_ord_id` must be the template's ClOrdID (the correlation
    /// key — identical to the REST idempotency key).
    pub async fn submit_order(
        &self,
        template: &FixOrderTemplate,
        cl_ord_id: &str,
    ) -> FixSendResult {
        match self.submit_order_traced(template, cl_ord_id).await {
            FixTracedSendResult::NotAttempted { error } => FixSendResult::NotAttempted { error },
            FixTracedSendResult::Attempted(attempt) => FixSendResult::Attempted(FixSendAttempt {
                send_ts: attempt.send_ts,
                outcome: attempt.outcome,
            }),
        }
    }

    /// Fire one prestaged order and retain the complete correlated report
    /// sequence. This is the result-oriented companion to the live
    /// [`Self::subscribe_observations`] door.
    pub async fn submit_order_traced(
        &self,
        template: &FixOrderTemplate,
        cl_ord_id: &str,
    ) -> FixTracedSendResult {
        let (tx, mut rx) = mpsc::unbounded_channel();
        let mut writer = self.writer.lock().await;
        let (generation, seq, send_ts) = {
            let mut io = self.io.lock().await;
            if !io.connected {
                return FixTracedSendResult::NotAttempted {
                    error: KalshiError::Ws("fix: disconnected".into()),
                };
            }
            if self.shutdown.load(Ordering::SeqCst) {
                return FixTracedSendResult::NotAttempted {
                    error: KalshiError::Ws("fix: transport shut down".into()),
                };
            }
            {
                let pending = self.pending.lock().unwrap();
                if pending.contains_key(cl_ord_id) {
                    return FixTracedSendResult::NotAttempted {
                        error: KalshiError::Validation(format!(
                            "fix: duplicate in-flight ClOrdID {cl_ord_id}"
                        )),
                    };
                }
            }
            let Some((seq, sending_time)) = io.machine.send_app(now_ms() as u64) else {
                return FixTracedSendResult::NotAttempted {
                    error: KalshiError::Ws("fix: session not established".into()),
                };
            };
            let generation = io.generation;
            self.pending
                .lock()
                .unwrap()
                .insert(cl_ord_id.to_string(), PendingOrder { generation, tx });
            self.outbound_orders
                .lock()
                .unwrap()
                .insert((generation, seq), cl_ord_id.to_string());

            let (mut encoder, mut out) = (FixWriter::new(), Vec::with_capacity(256));
            template.assemble(seq, &sending_time, &mut encoder, &mut out);
            let outbound_frame: Arc<[u8]> = out.into();
            let pre_write_evidence = FixPreWriteEvidence {
                generation,
                endpoint: FixEndpointIdentity {
                    host: self.cfg.host.clone(),
                    port: self.cfg.port,
                    tls: self.cfg.tls,
                    sender_comp_id: self.cfg.sender_comp_id.clone(),
                    target_comp_id: self.cfg.target_comp_id.clone(),
                },
                client_order_id: cl_ord_id.to_string(),
                msg_seq_num: seq,
                sending_time,
                prepared_ts: MonotonicNs::now(),
                outbound_frame: outbound_frame.clone(),
            };
            drop(io);

            if let Some(hook) = &self.pre_write_hook {
                if let Err(error) = hook.before_write(&pre_write_evidence).await {
                    // send_app consumed a sequence. Since the gate prevented
                    // the write, recycle the whole NR session rather than
                    // create an outbound sequence gap.
                    drop(writer);
                    let current = self
                        .disconnect_generation(generation, "pre-write evidence gate failed")
                        .await;
                    self.fail_pending_generation(generation);
                    if current {
                        spawn_reconnect(self.self_weak.clone());
                    }
                    return FixTracedSendResult::NotAttempted {
                        error: KalshiError::Ws(format!(
                            "fix pre-write evidence gate refused order: {error}"
                        )),
                    };
                }
            }

            let send_ts = MonotonicNs::now();
            if let Err(error) = write_or_drop(&mut writer, outbound_frame.as_ref()).await {
                drop(writer);
                let current = self
                    .disconnect_generation(generation, "application write/flush failed")
                    .await;
                self.fail_pending_generation(generation);
                if current {
                    spawn_reconnect(self.self_weak.clone());
                }

                let mut evidence = AttemptEvidence::default();
                while let Ok(observation) = rx.try_recv() {
                    if let Some(resolution) = evidence.record(observation) {
                        return match resolution {
                            AttemptResolution::Terminal(terminal) => {
                                evidence.attempted(send_ts, Ok(terminal))
                            }
                            AttemptResolution::Reject(reject) => {
                                evidence.failed(send_ts, reject_error(&reject), Some(reject))
                            }
                        };
                    }
                }
                return evidence.failed(
                    send_ts,
                    KalshiError::Ws(format!("fix write/flush: {error}")),
                    None,
                );
            }
            (generation, seq, send_ts)
        };
        drop(writer);

        let deadline = tokio::time::Instant::now() + self.er_timeout;
        let mut evidence = AttemptEvidence::default();
        loop {
            match tokio::time::timeout_at(deadline, rx.recv()).await {
                Ok(Some(observation)) => {
                    if let Some(resolution) = evidence.record(observation) {
                        self.remove_outbound_order(generation, seq, cl_ord_id);
                        return match resolution {
                            AttemptResolution::Terminal(terminal) => {
                                evidence.attempted(send_ts, Ok(terminal))
                            }
                            AttemptResolution::Reject(reject) => {
                                evidence.failed(send_ts, reject_error(&reject), Some(reject))
                            }
                        };
                    }
                }
                Ok(None) => {
                    self.remove_outbound_order(generation, seq, cl_ord_id);
                    return evidence.failed(
                        send_ts,
                        KalshiError::Ws(
                            "fix: connection lost awaiting terminal ExecutionReport".into(),
                        ),
                        None,
                    );
                }
                Err(_) => {
                    // Removal and dispatch serialize on the pending mutex.
                    // Once removal wins, every already-correlated observation
                    // is either in `rx` or already present in `evidence`.
                    self.pending.lock().unwrap().remove(cl_ord_id);
                    self.remove_outbound_order(generation, seq, cl_ord_id);
                    while let Ok(observation) = rx.try_recv() {
                        if let Some(resolution) = evidence.record(observation) {
                            return match resolution {
                                AttemptResolution::Terminal(terminal) => {
                                    evidence.attempted(send_ts, Ok(terminal))
                                }
                                AttemptResolution::Reject(reject) => {
                                    evidence.failed(send_ts, reject_error(&reject), Some(reject))
                                }
                            };
                        }
                    }
                    return evidence.failed(
                        send_ts,
                        KalshiError::Ws(format!(
                            "fix: no terminal ExecutionReport within {:?}",
                            self.er_timeout
                        )),
                        None,
                    );
                }
            }
        }
    }

    /// Compatibility wrapper for the original terminal-only door.
    ///
    /// On failure this deliberately discards `send_ts` and any non-terminal
    /// observation. New callers that need the complete ordered trace must use
    /// [`Self::submit_order_traced`] and the live observation subscription.
    pub async fn send_order(
        &self,
        template: &FixOrderTemplate,
        cl_ord_id: &str,
    ) -> Result<(OwnedExecReport, MonotonicNs, MonotonicNs), KalshiError> {
        match self.submit_order(template, cl_ord_id).await {
            FixSendResult::NotAttempted { error } => Err(error),
            FixSendResult::Attempted(FixSendAttempt {
                send_ts,
                outcome: Ok(observation),
            }) => Ok((observation.report, send_ts, observation.recv_ts)),
            FixSendResult::Attempted(FixSendAttempt {
                outcome: Err(failure),
                ..
            }) => Err(failure.error),
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
        let mut writer = self.writer.lock().await;
        let mut io = self.io.lock().await;
        let generation = io.generation;
        if io.connected {
            for a in io.machine.start_logout(now_ms() as u64) {
                if let Action::Send(bytes) = a {
                    let _ = write_or_drop(&mut writer, &bytes).await;
                }
            }
            io.connected = false;
        }
        drop(io);
        drop(writer);
        self.fail_pending_generation(generation);
    }

    /// Process one complete frame for exactly one connection generation.
    /// A stale reader returns without touching the replacement session.
    async fn process_frame(&self, generation: u64, frame: &[u8], recv_ts: MonotonicNs) -> bool {
        let reject = reject_from_frame(frame, recv_ts);
        if frame_can_allocate_outbound(frame) {
            // Any session response must share the same writer serialization
            // door as application sends so sequence order remains wire order.
            let mut writer = self.writer.lock().await;
            let mut io = self.io.lock().await;
            if io.generation != generation {
                return true;
            }
            if !io.connected {
                // A failed application write may leave residual inbound
                // frames on the old socket. Never allocate a response
                // sequence after that generation was declared dead.
                return false;
            }
            let actions = io.machine.on_message(frame, now_ms() as u64);
            return self
                .apply_actions(
                    &mut io,
                    Some(&mut writer),
                    actions,
                    reject,
                    recv_ts,
                    Some(frame),
                )
                .await;
        }

        // ERs/rejects never allocate an outbound sequence. They deliberately
        // bypass a possibly stalled socket flush so parsed evidence can be
        // correlated and queued before write-error teardown closes waiters.
        let mut io = self.io.lock().await;
        if io.generation != generation {
            return true;
        }
        let actions = io.machine.on_message(frame, now_ms() as u64);
        self.apply_actions(&mut io, None, actions, reject, recv_ts, Some(frame))
            .await
    }

    /// Run validated machine actions while holding the session lock.
    async fn apply_actions(
        &self,
        io: &mut SessionIo,
        mut writer: Option<&mut Writer>,
        actions: Vec<Action>,
        reject: Option<FixRejectObservation>,
        recv_ts: MonotonicNs,
        raw_frame: Option<&[u8]>,
    ) -> bool {
        for action in actions {
            match action {
                Action::Send(bytes) => {
                    let Some(writer) = writer.as_deref_mut() else {
                        tracing::error!(
                            generation = io.generation,
                            "FIX frame unexpectedly allocated a response without writer serialization"
                        );
                        io.connected = false;
                        return false;
                    };
                    if write_or_drop(writer, &bytes).await.is_err() {
                        io.connected = false;
                        return false;
                    }
                }
                Action::Deliver(er) => {
                    let Some(raw_frame) = raw_frame else {
                        tracing::error!(
                            generation = io.generation,
                            "FIX ExecutionReport action has no inbound frame evidence"
                        );
                        io.connected = false;
                        return false;
                    };
                    if !self.dispatch_execution(io.generation, er, recv_ts, raw_frame) {
                        io.connected = false;
                        return false;
                    }
                }
                Action::Disconnect { reason } => {
                    tracing::warn!(%reason, "fix session disconnect");
                    io.connected = false;
                    return false;
                }
            }
        }
        if let Some(reject) = reject {
            if !self.dispatch_reject(io.generation, reject) {
                io.connected = false;
                return false;
            }
        }
        true
    }

    fn dispatch_execution(
        &self,
        generation: u64,
        report: OwnedExecReport,
        recv_ts: MonotonicNs,
        raw_frame: &[u8],
    ) -> bool {
        let cl_ord_id = report.cl_ord_id.clone();
        let order_id = report.order_id.clone();
        let status = report.status.clone();
        let terminal = report.terminal;
        let mut pending = self.pending.lock().unwrap();
        let slot = cl_ord_id.as_deref().and_then(|id| {
            let current = pending
                .get(id)
                .is_some_and(|entry| entry.generation == generation);
            if !current {
                return None;
            }
            if terminal {
                pending.remove(id)
            } else {
                pending.get(id).cloned()
            }
        });
        // This timestamp is sampled only after exact generation/ClOrdID
        // correlation has completed.
        let observation = FixExecutionObservation {
            report,
            recv_ts,
            ack_ts: MonotonicNs::now(),
            raw: raw_frame.to_vec(),
        };
        if !self.publish(
            generation,
            FixOrderObservation::Execution(observation.clone()),
        ) {
            drop(pending);
            return false;
        }
        let delivered = slot.as_ref().map(|entry| {
            entry
                .tx
                .send(FixOrderObservation::Execution(observation.clone()))
                .is_ok()
        });
        if delivered == Some(false) {
            if let Some(id) = cl_ord_id.as_deref() {
                pending.remove(id);
            }
        }
        drop(pending);

        if terminal {
            if let Some(id) = cl_ord_id.as_deref() {
                self.remove_outbound_for_client(generation, id);
            }
        }

        match delivered {
            Some(true) if !terminal => {
                tracing::debug!(cl_ord_id = ?cl_ord_id, %status,
                    "non-terminal ExecutionReport delivered");
            }
            Some(true) => {}
            _ => {
                tracing::warn!(?cl_ord_id, ?order_id, %status, terminal,
                    "uncorrelated ExecutionReport preserved on observation stream");
            }
        }
        true
    }

    fn dispatch_reject(&self, generation: u64, mut reject: FixRejectObservation) -> bool {
        if reject.client_order_id.is_none() {
            reject.client_order_id = reject.ref_seq.and_then(|seq| {
                self.outbound_orders
                    .lock()
                    .unwrap()
                    .get(&(generation, seq))
                    .cloned()
            });
        }
        if reject.client_order_id.is_none() {
            reject.client_order_id = reject.business_ref_id.as_deref().and_then(|id| {
                self.pending
                    .lock()
                    .unwrap()
                    .get(id)
                    .filter(|entry| entry.generation == generation)
                    .map(|_| id.to_string())
            });
        }
        reject.ack_ts = MonotonicNs::now();

        let client_order_id = reject.client_order_id.clone();
        let slot = client_order_id.as_deref().and_then(|id| {
            let mut pending = self.pending.lock().unwrap();
            let slot = pending
                .get(id)
                .filter(|entry| entry.generation == generation)
                .cloned();
            if slot.is_some() {
                pending.remove(id);
            }
            slot
        });
        if let Some(id) = client_order_id.as_deref() {
            self.remove_outbound_for_client(generation, id);
        }
        if !self.publish(generation, FixOrderObservation::Reject(reject.clone())) {
            return false;
        }
        let delivered =
            slot.map(|entry| entry.tx.send(FixOrderObservation::Reject(reject)).is_ok());

        if delivered != Some(true) {
            tracing::warn!(
                ?client_order_id,
                "uncorrelated FIX reject preserved on observation stream"
            );
        }
        true
    }

    fn publish(&self, generation: u64, observation: FixOrderObservation) -> bool {
        let sequence = self.observation_sequence.fetch_add(1, Ordering::SeqCst) + 1;
        let envelope = FixObservationEnvelope {
            sequence,
            generation,
            observation,
        };
        if let Some(mandatory) = &self.mandatory_observer {
            if let Err(error) = mandatory.try_record(envelope.clone()) {
                tracing::error!(
                    ?error,
                    sequence,
                    generation,
                    "mandatory FIX observation banker refused inbound evidence"
                );
                self.shutdown.store(true, Ordering::SeqCst);
                return false;
            }
        }
        self.observers
            .lock()
            .unwrap()
            .retain(|subscriber| subscriber.send(envelope.clone()).is_ok());
        true
    }

    fn remove_outbound_order(&self, generation: u64, seq: u64, cl_ord_id: &str) {
        let removed = self
            .outbound_orders
            .lock()
            .unwrap()
            .remove(&(generation, seq));
        debug_assert!(removed.as_deref().is_none_or(|id| id == cl_ord_id));
    }

    fn remove_outbound_for_client(&self, generation: u64, cl_ord_id: &str) {
        self.outbound_orders
            .lock()
            .unwrap()
            .retain(|(entry_generation, _), id| *entry_generation != generation || id != cl_ord_id);
    }

    /// Close only the failed generation's waiters. Already-sent channel
    /// values remain drainable by each receiver.
    fn fail_pending_generation(&self, generation: u64) {
        self.pending
            .lock()
            .unwrap()
            .retain(|_, entry| entry.generation != generation);
        self.outbound_orders
            .lock()
            .unwrap()
            .retain(|(entry_generation, _), _| *entry_generation != generation);
    }

    /// Mark a generation disconnected iff it is still current. Returns true
    /// only to the task that owns current-session recovery.
    async fn disconnect_generation(&self, generation: u64, reason: &str) -> bool {
        let mut io = self.io.lock().await;
        if io.generation != generation {
            tracing::debug!(generation, current = io.generation, %reason,
                "stale FIX task ignored after reconnect");
            return false;
        }
        io.connected = false;
        true
    }

    async fn generation_is_current(&self, generation: u64) -> bool {
        self.io.lock().await.generation == generation
    }
}

async fn write_or_drop(writer: &mut Writer, bytes: &[u8]) -> std::io::Result<()> {
    writer.write_all(bytes).await?;
    writer.flush().await
}

fn frame_can_allocate_outbound(frame: &[u8]) -> bool {
    FixMsg::parse(frame)
        .map(|message| matches!(message.msg_type, b"1" | b"2" | b"5"))
        .unwrap_or(false)
}

fn reject_from_frame(frame: &[u8], recv_ts: MonotonicNs) -> Option<FixRejectObservation> {
    let message = FixMsg::parse(frame).ok()?;
    let kind = match message.msg_type {
        b"3" => FixRejectKind::Session,
        b"j" => FixRejectKind::Business,
        _ => return None,
    };
    let reason_tag = match kind {
        FixRejectKind::Session => 373,
        FixRejectKind::Business => 380,
    };
    Some(FixRejectObservation {
        kind,
        recv_ts,
        // Replaced after exact transport correlation.
        ack_ts: recv_ts,
        ref_seq: message.get_u64(45),
        ref_msg_type: message.get_str(372).map(str::to_string),
        business_ref_id: message.get_str(379).map(str::to_string),
        reason: message.get_str(reason_tag).map(str::to_string),
        text: message.get_str(58).map(str::to_string),
        raw: frame.to_vec(),
        client_order_id: None,
    })
}

fn reject_error(reject: &FixRejectObservation) -> KalshiError {
    let kind = match reject.kind {
        FixRejectKind::Session => "SessionReject",
        FixRejectKind::Business => "BusinessMessageReject",
    };
    KalshiError::Ws(format!(
        "fix {kind}: reason={} text={}",
        reject.reason.as_deref().unwrap_or("unspecified"),
        reject.text.as_deref().unwrap_or("unspecified")
    ))
}

/// Dial + TLS + Logon handshake. A mandatory banker, when supplied, exists
/// before the dial and receives order-relevant frames even when they are
/// coalesced into the read that completes Logon. Any partial trailing frame is
/// returned to the normal reader.
async fn establish(
    cfg: &FixConfig,
    signer: &Arc<Signer>,
    generation: u64,
    mandatory_observer: Option<&FixObservationTap>,
    observation_sequence: &AtomicU64,
) -> Result<Established, KalshiError> {
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
            writer
                .flush()
                .await
                .map_err(|e| KalshiError::Ws(format!("fix logon flush: {e}")))?;
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
            let recv_ts = MonotonicNs::now();
            let mut reject = reject_from_frame(&frame, recv_ts);
            for action in machine.on_message(&frame, now_ms() as u64) {
                match action {
                    Action::Send(bytes) => {
                        writer
                            .write_all(&bytes)
                            .await
                            .map_err(|e| KalshiError::Ws(format!("fix write: {e}")))?;
                        writer
                            .flush()
                            .await
                            .map_err(|e| KalshiError::Ws(format!("fix flush: {e}")))?;
                    }
                    Action::Deliver(report) => {
                        bank_handshake_observation(
                            mandatory_observer,
                            observation_sequence,
                            generation,
                            FixOrderObservation::Execution(FixExecutionObservation {
                                report,
                                recv_ts,
                                ack_ts: MonotonicNs::now(),
                                raw: frame.clone(),
                            }),
                        )?;
                    }
                    Action::Disconnect { reason } => {
                        return Err(KalshiError::Ws(format!("fix logon refused: {reason}")));
                    }
                }
            }
            if let Some(mut reject) = reject.take() {
                reject.ack_ts = MonotonicNs::now();
                bank_handshake_observation(
                    mandatory_observer,
                    observation_sequence,
                    generation,
                    FixOrderObservation::Reject(reject),
                )?;
            }
        }
    }
    Ok(Established {
        machine,
        reader,
        writer,
        leftover: buf,
    })
}

fn bank_handshake_observation(
    mandatory_observer: Option<&FixObservationTap>,
    observation_sequence: &AtomicU64,
    generation: u64,
    observation: FixOrderObservation,
) -> Result<(), KalshiError> {
    let Some(mandatory_observer) = mandatory_observer else {
        tracing::warn!(
            generation,
            "order-relevant FIX frame arrived during Logon without a mandatory banker"
        );
        return Ok(());
    };
    let sequence = observation_sequence.fetch_add(1, Ordering::SeqCst) + 1;
    mandatory_observer
        .try_record(FixObservationEnvelope {
            sequence,
            generation,
            observation,
        })
        .map_err(|error| {
            KalshiError::Ws(format!(
                "mandatory FIX observation banker refused handshake evidence: {error:?}"
            ))
        })
}

fn ensure_mandatory_observer_healthy(
    mandatory_observer: Option<&FixObservationTap>,
) -> Result<(), KalshiError> {
    if let Some(detail) = mandatory_observer.and_then(|tap| tap.health().fault_detail()) {
        Err(KalshiError::Ws(format!(
            "mandatory FIX observation banker is unhealthy: {detail}"
        )))
    } else {
        Ok(())
    }
}

fn spawn_reader(weak: Weak<FixTransport>, generation: u64, mut reader: Reader, mut buf: Vec<u8>) {
    tokio::spawn(async move {
        loop {
            // Drain complete frames already buffered.
            loop {
                let Some(len) = frame_len(&buf) else { break };
                let frame: Vec<u8> = buf.drain(..len).collect();
                let recv_ts = MonotonicNs::now();
                let Some(this) = weak.upgrade() else { return };
                if !this.generation_is_current(generation).await {
                    return;
                }
                if !this.process_frame(generation, &frame, recv_ts).await {
                    let current = this
                        .disconnect_generation(generation, "reader/session failure")
                        .await;
                    if current {
                        this.fail_pending_generation(generation);
                        spawn_reconnect(Arc::downgrade(&this));
                    }
                    return;
                }
            }
            let mut chunk = [0u8; 4096];
            match reader.read(&mut chunk).await {
                Ok(0) | Err(_) => {
                    let Some(this) = weak.upgrade() else { return };
                    let current = this
                        .disconnect_generation(generation, "reader socket closed")
                        .await;
                    if current {
                        tracing::warn!(generation, "fix socket closed");
                        this.fail_pending_generation(generation);
                        spawn_reconnect(Arc::downgrade(&this));
                    }
                    return;
                }
                Ok(n) => buf.extend_from_slice(&chunk[..n]),
            }
        }
    });
}

fn spawn_ticker(weak: Weak<FixTransport>, generation: u64) {
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tick.tick().await;
            let Some(this) = weak.upgrade() else { return };
            let mut writer = this.writer.lock().await;
            let mut io = this.io.lock().await;
            if io.generation != generation || !io.connected {
                return; // reconnect supervisor owns recovery; new ticker spawns with it
            }
            let actions = io.machine.on_tick(now_ms() as u64);
            if !this
                .apply_actions(
                    &mut io,
                    Some(&mut writer),
                    actions,
                    None,
                    MonotonicNs::now(),
                    None,
                )
                .await
            {
                drop(io);
                drop(writer);
                let current = this
                    .disconnect_generation(generation, "ticker/session failure")
                    .await;
                if current {
                    this.fail_pending_generation(generation);
                    spawn_reconnect(Arc::downgrade(&this));
                }
                return;
            }
        }
    });
}

fn spawn_reconnect(weak: Weak<FixTransport>) {
    let Some(owner) = weak.upgrade() else { return };
    if owner.reconnecting.swap(true, Ordering::SeqCst) {
        return;
    }
    drop(owner);
    tokio::spawn(async move {
        let mut backoff = BACKOFF_FLOOR;
        loop {
            tokio::time::sleep(backoff).await;
            let Some(this) = weak.upgrade() else { return };
            if this.shutdown.load(Ordering::SeqCst) {
                this.reconnecting.store(false, Ordering::SeqCst);
                return;
            }
            if let Err(error) = ensure_mandatory_observer_healthy(this.mandatory_observer.as_ref())
            {
                tracing::error!(%error, "FIX reconnect refused by mandatory evidence health");
                this.shutdown.store(true, Ordering::SeqCst);
                this.reconnecting.store(false, Ordering::SeqCst);
                return;
            }
            let candidate_generation = {
                let mut io = this.io.lock().await;
                // Generation identifies a socket attempt, not merely a
                // successfully installed session. This keeps any order frame
                // banked during a failed Logon distinct from the next retry.
                io.generation = io.generation.saturating_add(1);
                io.generation
            };
            match establish(
                &this.cfg,
                &this.signer,
                candidate_generation,
                this.mandatory_observer.as_ref(),
                &this.observation_sequence,
            )
            .await
            {
                Ok(Established {
                    machine,
                    reader,
                    writer,
                    leftover,
                }) => {
                    if this.shutdown.load(Ordering::SeqCst) {
                        this.reconnecting.store(false, Ordering::SeqCst);
                        return;
                    }
                    if let Err(error) =
                        ensure_mandatory_observer_healthy(this.mandatory_observer.as_ref())
                    {
                        tracing::error!(%error, "FIX reconnect result refused by mandatory evidence health");
                        this.shutdown.store(true, Ordering::SeqCst);
                        this.reconnecting.store(false, Ordering::SeqCst);
                        return;
                    }
                    tracing::info!("fix session re-established");
                    let generation = {
                        let mut writer_slot = this.writer.lock().await;
                        if this.shutdown.load(Ordering::SeqCst) {
                            this.reconnecting.store(false, Ordering::SeqCst);
                            return;
                        }
                        let mut io = this.io.lock().await;
                        io.generation = candidate_generation;
                        io.machine = machine;
                        io.connected = true;
                        *writer_slot = writer;
                        io.generation
                    };
                    let retry_if_immediately_lost = Arc::downgrade(&this);
                    spawn_reader(Arc::downgrade(&this), generation, reader, leftover);
                    spawn_ticker(Arc::downgrade(&this), generation);
                    this.reconnecting.store(false, Ordering::SeqCst);
                    let io = this.io.lock().await;
                    let immediately_lost = io.generation == generation && !io.connected;
                    drop(io);
                    if immediately_lost && !this.shutdown.load(Ordering::SeqCst) {
                        spawn_reconnect(retry_if_immediately_lost);
                    }
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

#[cfg(test)]
mod tests {
    use std::io;
    use std::pin::Pin;
    use std::sync::atomic::AtomicBool;
    use std::sync::OnceLock;
    use std::task::{Context, Poll, Waker};

    use rsa::pkcs1::EncodeRsaPrivateKey;

    use super::*;
    use crate::kalshi::fix::encode::FixWriter;

    fn signer() -> Arc<Signer> {
        static SIGNER: OnceLock<Arc<Signer>> = OnceLock::new();
        SIGNER
            .get_or_init(|| {
                // Transport scheduling tests do not verify key strength; a
                // small valid PSS key keeps these deterministic seams fast.
                let key = rsa::RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 1024).unwrap();
                let pem = key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF).unwrap();
                Arc::new(Signer::from_pem("SENDER-KEY".into(), &pem).unwrap())
            })
            .clone()
    }

    fn cfg() -> FixConfig {
        FixConfig {
            host: "mock".into(),
            port: 0,
            sender_comp_id: "SENDER-KEY".into(),
            target_comp_id: "KalshiNR".into(),
            heartbeat_secs: 10,
            tls: false,
        }
    }

    fn established_machine(cfg: &FixConfig, signer: &Arc<Signer>) -> SessionMachine {
        let mut machine = SessionMachine::new(cfg.clone(), signer.clone(), (1, 1));
        let _ = machine.connect(1_000);
        let mut writer = FixWriter::new();
        writer.begin("A", "KalshiNR", "SENDER-KEY", 1, "20260721-12:00:00.000");
        writer.tag_str(98, "0");
        writer.tag_str(108, "10");
        writer.tag_str(1137, "9");
        assert!(machine.on_message(&writer.finish(), 1_100).is_empty());
        assert!(machine.is_established());
        machine
    }

    fn transport_with_writer(
        writer: impl AsyncWrite + Send + Unpin + 'static,
        generation: u64,
    ) -> Arc<FixTransport> {
        transport_with_writer_and_hook(writer, generation, None)
    }

    fn transport_with_writer_and_hook(
        writer: impl AsyncWrite + Send + Unpin + 'static,
        generation: u64,
        pre_write_hook: Option<Arc<dyn FixPreWriteHook>>,
    ) -> Arc<FixTransport> {
        let cfg = cfg();
        let signer = signer();
        let machine = established_machine(&cfg, &signer);
        Arc::new_cyclic(|weak| FixTransport {
            cfg,
            signer,
            self_weak: weak.clone(),
            io: Mutex::new(SessionIo {
                machine,
                connected: true,
                generation,
            }),
            writer: Mutex::new(Box::new(writer)),
            pending: StdMutex::new(HashMap::new()),
            outbound_orders: StdMutex::new(HashMap::new()),
            observers: StdMutex::new(Vec::new()),
            mandatory_observer: None,
            observation_sequence: Arc::new(AtomicU64::new(0)),
            pre_write_hook,
            er_timeout: Duration::from_secs(1),
            shutdown: AtomicBool::new(false),
            reconnecting: AtomicBool::new(false),
        })
    }

    #[tokio::test]
    async fn stale_reader_generation_cannot_disconnect_or_clear_replacement_session() {
        let transport = transport_with_writer(tokio::io::sink(), 2);
        let (tx, _rx) = mpsc::unbounded_channel();
        transport.pending.lock().unwrap().insert(
            "new-generation-order".into(),
            PendingOrder { generation: 2, tx },
        );

        let current = transport
            .disconnect_generation(1, "late EOF from generation 1")
            .await;
        if current {
            transport.fail_pending_generation(1);
        }

        let io = transport.io.lock().await;
        assert_eq!(io.generation, 2);
        assert!(
            io.connected,
            "stale reader must not kill replacement session"
        );
        drop(io);
        assert!(
            transport
                .pending
                .lock()
                .unwrap()
                .contains_key("new-generation-order"),
            "stale reader must not clear replacement pending state"
        );
    }

    struct CountingWriter {
        writes: Arc<AtomicU64>,
    }

    impl AsyncWrite for CountingWriter {
        fn poll_write(
            self: Pin<&mut Self>,
            _cx: &mut Context<'_>,
            bytes: &[u8],
        ) -> Poll<io::Result<usize>> {
            self.writes.fetch_add(1, Ordering::SeqCst);
            Poll::Ready(Ok(bytes.len()))
        }

        fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Ready(Ok(()))
        }

        fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Ready(Ok(()))
        }
    }

    #[derive(Default)]
    struct RefusingEvidenceHook {
        seen: StdMutex<Vec<FixPreWriteEvidence>>,
    }

    impl FixPreWriteHook for RefusingEvidenceHook {
        fn before_write<'a>(&'a self, evidence: &'a FixPreWriteEvidence) -> FixPreWriteFuture<'a> {
            Box::pin(async move {
                self.seen.lock().unwrap().push(evidence.clone());
                Err(KalshiError::Ws("injected durable journal failure".into()))
            })
        }
    }

    #[tokio::test]
    async fn pre_write_evidence_failure_prevents_socket_write_and_recycles_sequence() {
        let writes = Arc::new(AtomicU64::new(0));
        let hook = Arc::new(RefusingEvidenceHook::default());
        let transport = transport_with_writer_and_hook(
            CountingWriter {
                writes: writes.clone(),
            },
            7,
            Some(hook.clone()),
        );
        let template = FixOrderTemplate::ioc_buy_yes(
            transport.config(),
            "KX-PREWRITE",
            97,
            60,
            "fix-prewrite-order".into(),
        );

        let result = transport
            .submit_order_traced(&template, "fix-prewrite-order")
            .await;
        let FixTracedSendResult::NotAttempted { error } = result else {
            panic!("a refused pre-write receipt must prove NotAttempted")
        };
        assert!(error.to_string().contains("durable journal failure"));
        assert_eq!(
            writes.load(Ordering::SeqCst),
            0,
            "write_all must not be entered"
        );

        let seen = hook.seen.lock().unwrap();
        assert_eq!(seen.len(), 1);
        let evidence = &seen[0];
        assert_eq!(evidence.generation, 7);
        assert_eq!(evidence.client_order_id, "fix-prewrite-order");
        assert_eq!(evidence.msg_seq_num, 2);
        assert_eq!(evidence.endpoint.host, "mock");
        assert_eq!(evidence.endpoint.port, 0);
        assert!(!evidence.endpoint.tls);
        assert_eq!(evidence.endpoint.sender_comp_id, "SENDER-KEY");
        assert_eq!(evidence.endpoint.target_comp_id, "KalshiNR");
        let parsed = FixMsg::parse(evidence.outbound_frame.as_ref()).unwrap();
        assert_eq!(parsed.msg_type, b"D");
        assert_eq!(parsed.seq, evidence.msg_seq_num);
        assert_eq!(parsed.get_str(11), Some("fix-prewrite-order"));
        assert_eq!(parsed.get_str(52), Some(evidence.sending_time.as_str()));
        assert_eq!(parsed.get_str(60), Some(evidence.sending_time.as_str()));
        drop(seen);
        assert!(transport.pending.lock().unwrap().is_empty());
        assert!(transport.outbound_orders.lock().unwrap().is_empty());
        assert!(
            !transport.is_connected().await,
            "consumed sequence requires a fresh session"
        );
        transport.shutdown().await;
    }

    struct FlushGateState {
        entered: AtomicBool,
        release_error: AtomicBool,
        waker: StdMutex<Option<Waker>>,
    }

    impl FlushGateState {
        fn new() -> Arc<Self> {
            Arc::new(Self {
                entered: AtomicBool::new(false),
                release_error: AtomicBool::new(false),
                waker: StdMutex::new(None),
            })
        }

        fn fail(&self) {
            self.release_error.store(true, Ordering::SeqCst);
            if let Some(waker) = self.waker.lock().unwrap().take() {
                waker.wake();
            }
        }
    }

    struct FlushErrorWriter {
        state: Arc<FlushGateState>,
    }

    impl AsyncWrite for FlushErrorWriter {
        fn poll_write(
            self: Pin<&mut Self>,
            _cx: &mut Context<'_>,
            bytes: &[u8],
        ) -> Poll<io::Result<usize>> {
            Poll::Ready(Ok(bytes.len()))
        }

        fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
            self.state.entered.store(true, Ordering::SeqCst);
            if self.state.release_error.load(Ordering::SeqCst) {
                Poll::Ready(Err(io::Error::new(
                    io::ErrorKind::BrokenPipe,
                    "injected flush ambiguity",
                )))
            } else {
                *self.state.waker.lock().unwrap() = Some(cx.waker().clone());
                Poll::Pending
            }
        }

        fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Ready(Ok(()))
        }
    }

    #[tokio::test]
    async fn flush_error_drains_every_report_already_correlated_by_reader() {
        let gate = FlushGateState::new();
        let transport = transport_with_writer(
            FlushErrorWriter {
                state: gate.clone(),
            },
            1,
        );
        let template = FixOrderTemplate::ioc_buy_yes(
            transport.config(),
            "KX-FLUSH",
            97,
            60,
            "fix-flush-order".into(),
        );
        let submit_transport = transport.clone();
        let submit = tokio::spawn(async move {
            submit_transport
                .submit_order_traced(&template, "fix-flush-order")
                .await
        });

        tokio::time::timeout(Duration::from_secs(1), async {
            while !gate.entered.load(Ordering::SeqCst) {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("flush entered");

        let mut partial = FixWriter::new();
        partial.begin("8", "KalshiNR", "SENDER-KEY", 2, "20260721-12:00:00.050");
        partial.tag_str(11, "fix-flush-order");
        partial.tag_str(37, "srv-flush-order");
        partial.tag_str(150, "F");
        partial.tag_str(39, "1");
        partial.tag_str(14, "7");
        partial.tag_str(151, "53");
        assert!(
            transport
                .process_frame(1, &partial.finish(), MonotonicNs::now())
                .await
        );

        let mut filled = FixWriter::new();
        filled.begin("8", "KalshiNR", "SENDER-KEY", 3, "20260721-12:00:00.060");
        filled.tag_str(11, "fix-flush-order");
        filled.tag_str(37, "srv-flush-order");
        filled.tag_str(150, "F");
        filled.tag_str(39, "2");
        filled.tag_str(14, "60");
        filled.tag_str(151, "0");
        assert!(
            transport
                .process_frame(1, &filled.finish(), MonotonicNs::now())
                .await
        );
        gate.fail();

        let FixTracedSendResult::Attempted(attempt) = submit.await.unwrap() else {
            panic!("write_all was entered")
        };
        let statuses: Vec<_> = attempt
            .observations
            .iter()
            .filter_map(|observation| match observation {
                FixOrderObservation::Execution(value) => Some(value.report.status.as_str()),
                FixOrderObservation::Reject(_) => None,
            })
            .collect();
        assert_eq!(statuses, ["partial", "filled"]);
        let terminal = attempt
            .outcome
            .expect("terminal evidence outranks the ambiguous flush error");
        assert_eq!(terminal.report.order_id.as_deref(), Some("srv-flush-order"));
        transport.shutdown().await;
    }
}
