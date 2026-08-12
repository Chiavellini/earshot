//! Crash-safe, restart-replayable order lifecycle evidence.
//!
//! Callers submit typed lifecycle commands to a dedicated writer thread. The
//! thread validates every command with the same reducer used during replay,
//! appends one canonical JSON line, and durably syncs it before acknowledging
//! the caller. `intent_evaluated` is the first receipt even when no send is
//! requested; `invocation_prepared` is the first delivery-ambiguous boundary.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::sync::{mpsc, Arc, OnceLock, RwLock};
use std::thread::{self, JoinHandle};

use base64::Engine as _;
use rsa::sha2::{Digest, Sha256};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::oneshot;

use crate::clock::MonotonicNs;

pub const ORDER_EVIDENCE_SCHEMA_VERSION: u8 = 4;

/// Identity that must remain stable for the entire event run.
///
/// `account_fingerprint` identifies the account, not a rotating credential or
/// key ID. Endpoints are deliberately absent: each attempt/observation records
/// its own endpoint so a FIX attempt may be reconciled through REST.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderEvidenceIdentity {
    pub run_id: String,
    pub event_id: String,
    pub kalshi_env: String,
    pub account_fingerprint: String,
    pub plan_sha256: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OrderSide {
    Yes,
    No,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OrderAction {
    Buy,
    Sell,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OrderTimeInForce {
    ImmediateOrCancel,
    GoodTillCanceled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EvidenceTier {
    Fast,
    Confirmed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TransportProtocol {
    Rest,
    Fix,
}

/// Exact bytes plus their independently checkable fingerprint.
///
/// Grouping the four fields in one type makes partial raw evidence impossible
/// through the typed API. Replay still validates all four against one another.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawEvidence {
    pub media_type: String,
    pub bytes_base64: String,
    pub bytes_len: u64,
    pub bytes_sha256: String,
}

impl RawEvidence {
    pub fn from_bytes(media_type: impl Into<String>, bytes: &[u8]) -> Self {
        Self {
            media_type: media_type.into(),
            bytes_base64: base64::engine::general_purpose::STANDARD.encode(bytes),
            bytes_len: bytes.len() as u64,
            bytes_sha256: sha256_hex(bytes),
        }
    }

    pub fn decode(&self) -> Result<Vec<u8>, String> {
        validate_raw_evidence(self)
    }
}

/// Complete outcome of evaluating one execution intent.
///
/// This is always the first receipt, including outcomes that never request a
/// send (lost race, stale/missing book, risk refusal, or a killed session).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IntentEvaluated {
    pub client_order_id: String,
    pub market_ticker: String,
    pub side: OrderSide,
    pub action: OrderAction,
    pub price_cents: u8,
    pub contract_count: u32,
    pub time_in_force: OrderTimeInForce,
    pub tier: EvidenceTier,
    pub keyword_id: u16,
    pub keyword: String,
    pub source_id: u16,
    pub audio_ts: u64,
    pub decision_ts: u64,
    pub exec_ts: u64,
    pub book_as_of_ts: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub best_ask_cents: Option<u8>,
    /// Digest of the canonical logical/prestaged order request.
    pub request_sha256: String,
    /// Digest of the decision plan that authorized this one-shot transition.
    pub decision_plan_sha1: String,
}

/// Durable proof that the evaluated intent advanced to a send request.
///
/// It contains only the transition timestamp because immutable order and
/// detector context already lives in the required `IntentEvaluated` parent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SendRequested {
    pub client_order_id: String,
    pub market_ticker: String,
    pub requested_ts: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NotAttempted {
    pub client_order_id: String,
    pub market_ticker: String,
    pub refusal_ts: u64,
    pub reason: String,
}

/// Exact outbound request/frame durably prepared before transport invocation.
///
/// Once this receipt exists, restart recovery remains conservative: a crash
/// could have happened between the actual invocation and its later durable
/// `WireAttempted` receipt, so the client ID must be reconciled exactly.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InvocationPrepared {
    pub client_order_id: String,
    pub market_ticker: String,
    pub protocol: TransportProtocol,
    pub endpoint_id: String,
    pub prepared_ts: u64,
    pub raw: RawEvidence,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WireAttempted {
    pub client_order_id: String,
    pub market_ticker: String,
    pub protocol: TransportProtocol,
    pub endpoint_id: String,
    pub send_ts: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FillEvidence {
    /// Exact fixed-point strings from the exchange; never rounded through f64.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub filled_count_fp: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub remaining_count_fp: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub average_price_dollars: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub latest_fill_count_fp: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub latest_fill_price_dollars: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub latest_fill_exchange_ts: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExchangeObserved {
    pub client_order_id: String,
    pub market_ticker: String,
    pub protocol: TransportProtocol,
    pub endpoint_id: String,
    pub observed_ts: u64,
    pub protocol_status: String,
    pub exchange_accepted: Option<bool>,
    pub order_terminal: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub echoed_client_order_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub order_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub order_status: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exchange_event_ts: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fill: Option<FillEvidence>,
    /// Exact response bytes or exact FIX frame.
    pub raw: RawEvidence,
    /// Best-effort parsed representation; raw bytes remain authoritative.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parsed: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

/// Diagnostic failure after the transport invocation boundary.
///
/// This is never terminal order evidence: delivery remains ambiguous until an
/// exact reconciliation or terminal exchange observation resolves it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TransportFailed {
    pub client_order_id: String,
    pub market_ticker: String,
    pub protocol: TransportProtocol,
    pub endpoint_id: String,
    pub failure_ts: u64,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Reconciled {
    pub client_order_id: String,
    pub market_ticker: String,
    pub protocol: TransportProtocol,
    pub endpoint_id: String,
    pub observed_ts: u64,
    pub queried_client_order_id: String,
    pub echoed_client_order_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub order_id: Option<String>,
    pub order_status: String,
    pub exchange_accepted: Option<bool>,
    pub order_terminal: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exchange_event_ts: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fill: Option<FillEvidence>,
    pub raw: RawEvidence,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parsed: Option<Value>,
}

/// One exact-client-ID query that did not yield a verifiable matching row.
/// It is an observation at an instant, never proof that no order exists.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReconcileUnresolved {
    pub client_order_id: String,
    pub market_ticker: String,
    pub protocol: TransportProtocol,
    pub endpoint_id: String,
    pub observed_ts: u64,
    pub queried_client_order_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raw: Option<RawEvidence>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parsed: Option<Value>,
    pub detail: String,
}

/// Typed commands accepted by the evidence writer.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", content = "receipt", rename_all = "snake_case")]
pub enum OrderEvidenceCommand {
    IntentEvaluated(IntentEvaluated),
    SendRequested(SendRequested),
    NotAttempted(NotAttempted),
    InvocationPrepared(InvocationPrepared),
    WireAttempted(WireAttempted),
    ExchangeObserved(ExchangeObserved),
    TransportFailed(TransportFailed),
    Reconciled(Reconciled),
    ReconcileUnresolved(ReconcileUnresolved),
}

impl OrderEvidenceCommand {
    fn client_order_id(&self) -> &str {
        match self {
            Self::IntentEvaluated(value) => &value.client_order_id,
            Self::SendRequested(value) => &value.client_order_id,
            Self::NotAttempted(value) => &value.client_order_id,
            Self::InvocationPrepared(value) => &value.client_order_id,
            Self::WireAttempted(value) => &value.client_order_id,
            Self::ExchangeObserved(value) => &value.client_order_id,
            Self::TransportFailed(value) => &value.client_order_id,
            Self::Reconciled(value) => &value.client_order_id,
            Self::ReconcileUnresolved(value) => &value.client_order_id,
        }
    }

    fn market_ticker(&self) -> &str {
        match self {
            Self::IntentEvaluated(value) => &value.market_ticker,
            Self::SendRequested(value) => &value.market_ticker,
            Self::NotAttempted(value) => &value.market_ticker,
            Self::InvocationPrepared(value) => &value.market_ticker,
            Self::WireAttempted(value) => &value.market_ticker,
            Self::ExchangeObserved(value) => &value.market_ticker,
            Self::TransportFailed(value) => &value.market_ticker,
            Self::Reconciled(value) => &value.market_ticker,
            Self::ReconcileUnresolved(value) => &value.market_ticker,
        }
    }

    pub fn kind(&self) -> OrderTransitionKind {
        match self {
            Self::IntentEvaluated(_) => OrderTransitionKind::IntentEvaluated,
            Self::SendRequested(_) => OrderTransitionKind::SendRequested,
            Self::NotAttempted(_) => OrderTransitionKind::NotAttempted,
            Self::InvocationPrepared(_) => OrderTransitionKind::InvocationPrepared,
            Self::WireAttempted(_) => OrderTransitionKind::WireAttempted,
            Self::ExchangeObserved(_) => OrderTransitionKind::ExchangeObserved,
            Self::TransportFailed(_) => OrderTransitionKind::TransportFailed,
            Self::Reconciled(_) => OrderTransitionKind::Reconciled,
            Self::ReconcileUnresolved(_) => OrderTransitionKind::ReconcileUnresolved,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OrderTransitionKind {
    IntentEvaluated,
    SendRequested,
    NotAttempted,
    InvocationPrepared,
    WireAttempted,
    ExchangeObserved,
    TransportFailed,
    Reconciled,
    ReconcileUnresolved,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OrderEvidenceEvent {
    pub schema_version: u8,
    pub journal_seq: u64,
    pub order_seq: u64,
    pub wall_unix_ns: u64,
    pub monotonic_ns: u64,
    pub process_instance_id: String,
    pub run_id: String,
    pub event_id: String,
    pub kalshi_env: String,
    pub account_fingerprint: String,
    pub plan_sha256: String,
    pub client_order_id: String,
    pub market_ticker: String,
    /// Process instance that authored the prior event, or empty for the first
    /// event. Together with `previous_event_sha256`, this makes a process
    /// restart boundary explicit rather than inferring it from adjacent rows.
    pub previous_process_instance_id: String,
    pub previous_event_sha256: String,
    pub transition: OrderEvidenceCommand,
    pub event_sha256: String,
}

impl OrderEvidenceEvent {
    pub fn kind(&self) -> OrderTransitionKind {
        self.transition.kind()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecoveryAction {
    /// Only the durable request exists: no outbound invocation was prepared
    /// and no invocation boundary was crossed.
    ConcludeNotAttempted,
    ReconcileExactClientId,
    ObserveOrCancelKnownOrder {
        order_id: String,
    },
}

/// Current replayed state for one immutable client order ID.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrderRecoveryState {
    pub client_order_id: String,
    pub market_ticker: String,
    pub evaluation: IntentEvaluated,
    pub send_request: Option<SendRequested>,
    pub invocation_prepared: Option<InvocationPrepared>,
    pub wire_attempt: Option<WireAttempted>,
    pub order_id: Option<String>,
    pub order_status: Option<String>,
    pub exchange_accepted: Option<bool>,
    pub terminal: bool,
    pub last_kind: OrderTransitionKind,
    pub last_observed_ts: Option<u64>,
    pub order_seq: u64,
    transport_failed: bool,
}

impl OrderRecoveryState {
    pub fn recovery_action(&self) -> Option<RecoveryAction> {
        if self.terminal {
            None
        } else if let Some(order_id) = &self.order_id {
            Some(RecoveryAction::ObserveOrCancelKnownOrder {
                order_id: order_id.clone(),
            })
        } else if self.invocation_prepared.is_none() && self.wire_attempt.is_none() {
            Some(RecoveryAction::ConcludeNotAttempted)
        } else {
            Some(RecoveryAction::ReconcileExactClientId)
        }
    }

    pub fn requires_reconciliation(&self) -> bool {
        matches!(
            self.recovery_action(),
            Some(RecoveryAction::ReconcileExactClientId)
        )
    }
}

#[derive(Debug, Clone)]
pub struct OrderEvidenceSnapshot {
    pub events: Vec<OrderEvidenceEvent>,
    pub orders: Vec<OrderRecoveryState>,
}

impl OrderEvidenceSnapshot {
    pub fn unresolved_recoveries(&self) -> Vec<OrderRecoveryState> {
        self.orders
            .iter()
            .filter(|order| order.recovery_action().is_some())
            .cloned()
            .collect()
    }
}

#[derive(Debug, thiserror::Error)]
pub enum OrderEvidenceError {
    #[error("order evidence I/O at {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("invalid order evidence {path}: {detail}")]
    Invalid { path: PathBuf, detail: String },
    #[error("order evidence unavailable: {0}")]
    Unavailable(String),
}

#[derive(Debug, Clone)]
pub struct OrderEvidenceHealth {
    fault: Arc<OnceLock<String>>,
}

impl OrderEvidenceHealth {
    pub fn fault_detail(&self) -> Option<String> {
        self.fault.get().cloned()
    }

    pub fn is_healthy(&self) -> bool {
        self.fault.get().is_none()
    }

    fn poison(&self, detail: String) {
        let _ = self.fault.set(detail);
    }
}

struct RecordMessage {
    command: OrderEvidenceCommand,
    acknowledgement: oneshot::Sender<Result<OrderEvidenceEvent, OrderEvidenceError>>,
}

enum WriterMessage {
    Record(RecordMessage),
    Shutdown,
}

/// Cloneable producer door. Its async API waits for a durable acknowledgement
/// while all file I/O and fsync work happens on the dedicated writer thread.
#[derive(Debug, Clone)]
pub struct OrderEvidenceSink {
    sender: mpsc::Sender<WriterMessage>,
    health: OrderEvidenceHealth,
}

impl OrderEvidenceSink {
    pub async fn record(
        &self,
        command: OrderEvidenceCommand,
    ) -> Result<OrderEvidenceEvent, OrderEvidenceError> {
        let acknowledgement = self.enqueue(command)?;
        acknowledgement.await.map_err(|_| self.writer_stopped())?
    }

    /// Synchronous compatibility door for non-Tokio control code and tests.
    /// Async runtime code must use `record` so fsync never blocks its caller.
    pub fn record_blocking(
        &self,
        command: OrderEvidenceCommand,
    ) -> Result<OrderEvidenceEvent, OrderEvidenceError> {
        let acknowledgement = self.enqueue(command)?;
        acknowledgement
            .blocking_recv()
            .map_err(|_| self.writer_stopped())?
    }

    pub async fn record_send_requested(
        &self,
        receipt: SendRequested,
    ) -> Result<OrderEvidenceEvent, OrderEvidenceError> {
        self.record(OrderEvidenceCommand::SendRequested(receipt))
            .await
    }

    pub async fn record_intent_evaluated(
        &self,
        receipt: IntentEvaluated,
    ) -> Result<OrderEvidenceEvent, OrderEvidenceError> {
        self.record(OrderEvidenceCommand::IntentEvaluated(receipt))
            .await
    }

    pub async fn record_not_attempted(
        &self,
        receipt: NotAttempted,
    ) -> Result<OrderEvidenceEvent, OrderEvidenceError> {
        self.record(OrderEvidenceCommand::NotAttempted(receipt))
            .await
    }

    pub async fn record_invocation_prepared(
        &self,
        receipt: InvocationPrepared,
    ) -> Result<OrderEvidenceEvent, OrderEvidenceError> {
        self.record(OrderEvidenceCommand::InvocationPrepared(receipt))
            .await
    }

    pub async fn record_wire_attempted(
        &self,
        receipt: WireAttempted,
    ) -> Result<OrderEvidenceEvent, OrderEvidenceError> {
        self.record(OrderEvidenceCommand::WireAttempted(receipt))
            .await
    }

    pub async fn record_exchange_observed(
        &self,
        receipt: ExchangeObserved,
    ) -> Result<OrderEvidenceEvent, OrderEvidenceError> {
        self.record(OrderEvidenceCommand::ExchangeObserved(receipt))
            .await
    }

    pub async fn record_transport_failed(
        &self,
        receipt: TransportFailed,
    ) -> Result<OrderEvidenceEvent, OrderEvidenceError> {
        self.record(OrderEvidenceCommand::TransportFailed(receipt))
            .await
    }

    pub async fn record_reconciled(
        &self,
        receipt: Reconciled,
    ) -> Result<OrderEvidenceEvent, OrderEvidenceError> {
        self.record(OrderEvidenceCommand::Reconciled(receipt)).await
    }

    pub async fn record_reconcile_unresolved(
        &self,
        receipt: ReconcileUnresolved,
    ) -> Result<OrderEvidenceEvent, OrderEvidenceError> {
        self.record(OrderEvidenceCommand::ReconcileUnresolved(receipt))
            .await
    }

    fn enqueue(
        &self,
        command: OrderEvidenceCommand,
    ) -> Result<oneshot::Receiver<Result<OrderEvidenceEvent, OrderEvidenceError>>, OrderEvidenceError>
    {
        if let Some(detail) = self.health.fault_detail() {
            return Err(OrderEvidenceError::Unavailable(detail));
        }
        let (acknowledgement, receiver) = oneshot::channel();
        self.sender
            .send(WriterMessage::Record(RecordMessage {
                command,
                acknowledgement,
            }))
            .map_err(|_| self.writer_stopped())?;
        Ok(receiver)
    }

    pub fn health(&self) -> OrderEvidenceHealth {
        self.health.clone()
    }

    fn writer_stopped(&self) -> OrderEvidenceError {
        let detail = "order evidence writer stopped before durable acknowledgement".to_owned();
        self.health.poison(detail.clone());
        OrderEvidenceError::Unavailable(detail)
    }
}

/// Exclusive journal lease plus its complete, continuously updated replay.
pub struct OrderEvidenceJournal {
    sender: Option<mpsc::Sender<WriterMessage>>,
    writer: Option<JoinHandle<()>>,
    health: OrderEvidenceHealth,
    snapshot: Arc<RwLock<OrderEvidenceSnapshot>>,
}

impl OrderEvidenceJournal {
    /// Opens one journal under an already-created run directory.
    ///
    /// This function never creates ancestors. Assembly/RunPlan ownership must
    /// establish the canonical parent before the runtime opens evidence.
    pub fn open(
        path: impl AsRef<Path>,
        identity: OrderEvidenceIdentity,
    ) -> Result<Self, OrderEvidenceError> {
        validate_identity(&identity)?;
        let path = path.as_ref().to_path_buf();
        let parent = path
            .parent()
            .filter(|candidate| !candidate.as_os_str().is_empty())
            .ok_or_else(|| OrderEvidenceError::Invalid {
                path: path.clone(),
                detail: "journal path has no parent".into(),
            })?;
        if !parent.is_dir() {
            return Err(OrderEvidenceError::Invalid {
                path: path.clone(),
                detail: format!(
                    "canonical parent {} must already exist and be a directory",
                    parent.display()
                ),
            });
        }
        match std::fs::symlink_metadata(&path) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(OrderEvidenceError::Invalid {
                    path,
                    detail: "journal path must not be a symbolic link".into(),
                });
            }
            Ok(metadata) if !metadata.is_file() => {
                return Err(OrderEvidenceError::Invalid {
                    path,
                    detail: "journal path exists but is not a regular file".into(),
                });
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(source) => {
                return Err(OrderEvidenceError::Io { path, source });
            }
        }

        let mut file = OpenOptions::new()
            .create(true)
            .read(true)
            .append(true)
            .open(&path)
            .map_err(|source| OrderEvidenceError::Io {
                path: path.clone(),
                source,
            })?;
        lock_exclusive_nonblocking(&file, &path)?;
        durable_sync(&file, &path)?;
        sync_directory(parent)?;

        let mut raw = Vec::new();
        file.read_to_end(&mut raw)
            .map_err(|source| OrderEvidenceError::Io {
                path: path.clone(),
                source,
            })?;
        let (reducer, events) = replay(&path, &raw, &identity)?;
        let snapshot = Arc::new(RwLock::new(OrderEvidenceSnapshot {
            events,
            orders: reducer.orders.values().cloned().collect(),
        }));
        let health = OrderEvidenceHealth {
            fault: Arc::new(OnceLock::new()),
        };
        let (sender, receiver) = mpsc::channel();
        let writer_path = path.clone();
        let writer_health = health.clone();
        let writer_snapshot = Arc::clone(&snapshot);
        let process_instance_id = process_instance_id();
        let writer = thread::Builder::new()
            .name("order-evidence-writer".into())
            .spawn(move || {
                writer_loop(
                    writer_path,
                    file,
                    reducer,
                    process_instance_id,
                    receiver,
                    writer_health,
                    writer_snapshot,
                );
            })
            .map_err(|source| OrderEvidenceError::Io {
                path: path.clone(),
                source,
            })?;

        Ok(Self {
            sender: Some(sender),
            writer: Some(writer),
            health,
            snapshot,
        })
    }

    pub fn sink(&self) -> OrderEvidenceSink {
        OrderEvidenceSink {
            sender: self
                .sender
                .as_ref()
                .expect("order evidence journal already closed")
                .clone(),
            health: self.health.clone(),
        }
    }

    pub fn health(&self) -> OrderEvidenceHealth {
        self.health.clone()
    }

    pub fn snapshot(&self) -> OrderEvidenceSnapshot {
        self.snapshot
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }

    pub fn replayed_events(&self) -> Vec<OrderEvidenceEvent> {
        self.snapshot().events
    }

    pub fn unresolved_recoveries(&self) -> Vec<OrderRecoveryState> {
        self.snapshot().unresolved_recoveries()
    }

    pub fn close(mut self) -> Result<(), OrderEvidenceError> {
        self.stop_writer()
    }

    fn stop_writer(&mut self) -> Result<(), OrderEvidenceError> {
        if let Some(sender) = self.sender.take() {
            let _ = sender.send(WriterMessage::Shutdown);
        }
        if let Some(writer) = self.writer.take() {
            writer.join().map_err(|_| {
                let detail = "order evidence writer thread panicked".to_owned();
                self.health.poison(detail.clone());
                OrderEvidenceError::Unavailable(detail)
            })?;
        }
        Ok(())
    }
}

impl Drop for OrderEvidenceJournal {
    fn drop(&mut self) {
        let _ = self.stop_writer();
    }
}

fn writer_loop(
    path: PathBuf,
    mut file: File,
    mut reducer: Reducer,
    process_instance_id: String,
    receiver: mpsc::Receiver<WriterMessage>,
    health: OrderEvidenceHealth,
    snapshot: Arc<RwLock<OrderEvidenceSnapshot>>,
) {
    let mut poisoned = health.fault_detail();
    while let Ok(message) = receiver.recv() {
        match message {
            WriterMessage::Shutdown => {
                for queued in receiver.try_iter() {
                    if let WriterMessage::Record(queued) = queued {
                        let detail = poisoned.clone().unwrap_or_else(|| {
                            "order evidence journal closed before queued durable acknowledgement"
                                .to_owned()
                        });
                        health.poison(detail.clone());
                        let _ = queued
                            .acknowledgement
                            .send(Err(OrderEvidenceError::Unavailable(detail)));
                    }
                }
                break;
            }
            WriterMessage::Record(message) => {
                let result = if let Some(detail) = &poisoned {
                    Err(OrderEvidenceError::Unavailable(detail.clone()))
                } else {
                    append_one(
                        &path,
                        &mut file,
                        &mut reducer,
                        &process_instance_id,
                        message.command,
                    )
                };

                match result {
                    Ok(event) => {
                        {
                            let mut current =
                                snapshot.write().unwrap_or_else(|lock| lock.into_inner());
                            current.events.push(event.clone());
                            current.orders = reducer.orders.values().cloned().collect();
                        }
                        let _ = message.acknowledgement.send(Ok(event));
                    }
                    Err(error) => {
                        let detail = error.to_string();
                        health.poison(detail.clone());
                        poisoned = Some(detail);
                        let _ = message.acknowledgement.send(Err(error));
                    }
                }
            }
        }
    }
}

fn append_one(
    path: &Path,
    file: &mut File,
    reducer: &mut Reducer,
    process_instance_id: &str,
    transition: OrderEvidenceCommand,
) -> Result<OrderEvidenceEvent, OrderEvidenceError> {
    let client_order_id = transition.client_order_id().to_owned();
    let market_ticker = transition.market_ticker().to_owned();
    let order_seq = reducer
        .orders
        .get(&client_order_id)
        .map_or(0, |state| state.order_seq + 1);
    let wall = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let mut event = OrderEvidenceEvent {
        schema_version: ORDER_EVIDENCE_SCHEMA_VERSION,
        journal_seq: reducer.next_journal_seq,
        order_seq,
        wall_unix_ns: wall.as_secs().saturating_mul(1_000_000_000) + u64::from(wall.subsec_nanos()),
        monotonic_ns: MonotonicNs::now().0,
        process_instance_id: process_instance_id.to_owned(),
        run_id: reducer.identity.run_id.clone(),
        event_id: reducer.identity.event_id.clone(),
        kalshi_env: reducer.identity.kalshi_env.clone(),
        account_fingerprint: reducer.identity.account_fingerprint.clone(),
        plan_sha256: reducer.identity.plan_sha256.clone(),
        client_order_id,
        market_ticker,
        previous_process_instance_id: reducer.previous_process_instance_id.clone(),
        previous_event_sha256: reducer.previous_event_sha256.clone(),
        transition,
        event_sha256: String::new(),
    };
    event.event_sha256 = event_sha256(&event).map_err(|detail| OrderEvidenceError::Invalid {
        path: path.to_path_buf(),
        detail,
    })?;
    let reduced = reducer
        .validate(&event)
        .map_err(|detail| OrderEvidenceError::Invalid {
            path: path.to_path_buf(),
            detail,
        })?;

    let mut encoded = serde_json::to_vec(&event).map_err(|error| OrderEvidenceError::Invalid {
        path: path.to_path_buf(),
        detail: format!("serialize event: {error}"),
    })?;
    encoded.push(b'\n');
    let written = file
        .write(&encoded)
        .map_err(|source| OrderEvidenceError::Io {
            path: path.to_path_buf(),
            source,
        })?;
    if written != encoded.len() {
        return Err(OrderEvidenceError::Io {
            path: path.to_path_buf(),
            source: std::io::Error::new(
                std::io::ErrorKind::WriteZero,
                format!("short append: {written}/{}", encoded.len()),
            ),
        });
    }
    sync_file(file).map_err(|source| OrderEvidenceError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    reducer.commit(event.clone(), reduced);
    Ok(event)
}

#[derive(Debug)]
struct Reducer {
    identity: OrderEvidenceIdentity,
    next_journal_seq: u64,
    previous_process_instance_id: String,
    previous_event_sha256: String,
    orders: BTreeMap<String, OrderRecoveryState>,
}

impl Reducer {
    fn new(identity: OrderEvidenceIdentity) -> Self {
        Self {
            previous_event_sha256: identity.plan_sha256.clone(),
            identity,
            next_journal_seq: 0,
            previous_process_instance_id: String::new(),
            orders: BTreeMap::new(),
        }
    }

    fn apply(&mut self, event: OrderEvidenceEvent) -> Result<(), String> {
        let reduced = self.validate(&event)?;
        self.commit(event, reduced);
        Ok(())
    }

    fn validate(&self, event: &OrderEvidenceEvent) -> Result<OrderRecoveryState, String> {
        validate_event_envelope(self, event)?;
        let previous = self.orders.get(&event.client_order_id);
        let expected_order_seq = previous.map_or(0, |state| state.order_seq + 1);
        if event.order_seq != expected_order_seq {
            return Err(format!(
                "client {} expected order_seq {expected_order_seq}, got {}",
                event.client_order_id, event.order_seq
            ));
        }
        reduce_transition(previous, event)
    }

    fn commit(&mut self, event: OrderEvidenceEvent, reduced: OrderRecoveryState) {
        self.next_journal_seq += 1;
        self.previous_process_instance_id = event.process_instance_id.clone();
        self.previous_event_sha256 = event.event_sha256;
        self.orders.insert(reduced.client_order_id.clone(), reduced);
    }
}

fn validate_event_envelope(reducer: &Reducer, event: &OrderEvidenceEvent) -> Result<(), String> {
    if event.schema_version != ORDER_EVIDENCE_SCHEMA_VERSION {
        return Err(format!(
            "unsupported schema_version {}",
            event.schema_version
        ));
    }
    if event.journal_seq != reducer.next_journal_seq {
        return Err(format!(
            "expected journal_seq {}, got {}",
            reducer.next_journal_seq, event.journal_seq
        ));
    }
    if event.previous_event_sha256 != reducer.previous_event_sha256 {
        return Err(format!(
            "journal_seq {} previous hash mismatch",
            event.journal_seq
        ));
    }
    if event.previous_process_instance_id != reducer.previous_process_instance_id {
        return Err(format!(
            "journal_seq {} previous process instance mismatch",
            event.journal_seq
        ));
    }
    if event.run_id != reducer.identity.run_id
        || event.event_id != reducer.identity.event_id
        || event.kalshi_env != reducer.identity.kalshi_env
        || event.account_fingerprint != reducer.identity.account_fingerprint
        || event.plan_sha256 != reducer.identity.plan_sha256
    {
        return Err(format!(
            "journal_seq {} identity mismatch",
            event.journal_seq
        ));
    }
    validate_identifier("process_instance_id", &event.process_instance_id)?;
    validate_identifier("client_order_id", &event.client_order_id)?;
    validate_identifier("market_ticker", &event.market_ticker)?;
    if event.transition.client_order_id() != event.client_order_id
        || event.transition.market_ticker() != event.market_ticker
    {
        return Err(format!(
            "journal_seq {} transition identity mismatch",
            event.journal_seq
        ));
    }
    if !is_lower_hex(&event.event_sha256, 64) {
        return Err(format!(
            "journal_seq {} invalid event_sha256",
            event.journal_seq
        ));
    }
    let expected_hash = event_sha256(event)?;
    if event.event_sha256 != expected_hash {
        return Err(format!(
            "journal_seq {} event hash mismatch",
            event.journal_seq
        ));
    }
    Ok(())
}

fn reduce_transition(
    previous: Option<&OrderRecoveryState>,
    event: &OrderEvidenceEvent,
) -> Result<OrderRecoveryState, String> {
    if let Some(state) = previous {
        if state.market_ticker != event.market_ticker {
            return Err(format!(
                "client {} market ticker changed from {} to {}",
                event.client_order_id, state.market_ticker, event.market_ticker
            ));
        }
        if state.terminal {
            return Err(format!(
                "client {} received {:?} after terminal {:?}",
                event.client_order_id,
                event.kind(),
                state.last_kind
            ));
        }
    }

    match &event.transition {
        OrderEvidenceCommand::IntentEvaluated(evaluation) => {
            if previous.is_some() {
                return Err(format!(
                    "duplicate intent_evaluated for {}",
                    event.client_order_id
                ));
            }
            validate_intent_evaluated(evaluation)?;
            Ok(OrderRecoveryState {
                client_order_id: event.client_order_id.clone(),
                market_ticker: event.market_ticker.clone(),
                evaluation: evaluation.clone(),
                send_request: None,
                invocation_prepared: None,
                wire_attempt: None,
                order_id: None,
                order_status: None,
                exchange_accepted: None,
                terminal: false,
                last_kind: OrderTransitionKind::IntentEvaluated,
                last_observed_ts: None,
                order_seq: event.order_seq,
                transport_failed: false,
            })
        }
        OrderEvidenceCommand::SendRequested(request) => {
            let mut state = require_previous(previous, event)?;
            if state.last_kind != OrderTransitionKind::IntentEvaluated
                || state.send_request.is_some()
                || state.invocation_prepared.is_some()
                || state.wire_attempt.is_some()
            {
                return Err("send_requested must occur exactly once after intent_evaluated".into());
            }
            validate_send_requested(request, &state.evaluation)?;
            state.send_request = Some(request.clone());
            state.last_kind = OrderTransitionKind::SendRequested;
            state.order_seq = event.order_seq;
            Ok(state)
        }
        OrderEvidenceCommand::NotAttempted(receipt) => {
            let mut state = require_previous(previous, event)?;
            if !matches!(
                state.last_kind,
                OrderTransitionKind::IntentEvaluated
                    | OrderTransitionKind::SendRequested
                    | OrderTransitionKind::InvocationPrepared
            ) || state.wire_attempt.is_some()
            {
                return Err("not_attempted is legal only before a wire-attempt receipt".into());
            }
            validate_nonzero("refusal_ts", receipt.refusal_ts)?;
            validate_nonempty("reason", &receipt.reason)?;
            let minimum_ts = state
                .invocation_prepared
                .as_ref()
                .map(|prepared| prepared.prepared_ts)
                .or_else(|| {
                    state
                        .send_request
                        .as_ref()
                        .map(|request| request.requested_ts)
                })
                .unwrap_or(state.evaluation.exec_ts);
            if receipt.refusal_ts < minimum_ts {
                return Err("not_attempted refusal_ts precedes its prior transition".into());
            }
            state.terminal = true;
            state.last_kind = OrderTransitionKind::NotAttempted;
            state.order_seq = event.order_seq;
            Ok(state)
        }
        OrderEvidenceCommand::InvocationPrepared(receipt) => {
            let mut state = require_previous(previous, event)?;
            if state.last_kind != OrderTransitionKind::SendRequested
                || state.invocation_prepared.is_some()
                || state.wire_attempt.is_some()
            {
                return Err(
                    "invocation_prepared must occur exactly once after send_requested".into(),
                );
            }
            validate_endpoint(&receipt.endpoint_id)?;
            validate_nonzero("prepared_ts", receipt.prepared_ts)?;
            let request = state
                .send_request
                .as_ref()
                .expect("SendRequested kind requires receipt");
            if receipt.prepared_ts < request.requested_ts {
                return Err("invocation prepared_ts precedes requested_ts".into());
            }
            validate_raw_evidence(&receipt.raw)?;
            state.invocation_prepared = Some(receipt.clone());
            state.last_kind = OrderTransitionKind::InvocationPrepared;
            state.order_seq = event.order_seq;
            Ok(state)
        }
        OrderEvidenceCommand::WireAttempted(receipt) => {
            let mut state = require_previous(previous, event)?;
            if state.last_kind != OrderTransitionKind::InvocationPrepared
                || state.wire_attempt.is_some()
            {
                return Err(
                    "wire_attempted must occur exactly once after invocation_prepared".into(),
                );
            }
            let prepared = state
                .invocation_prepared
                .as_ref()
                .expect("InvocationPrepared kind requires receipt");
            validate_wire_attempt(receipt, prepared)?;
            state.wire_attempt = Some(receipt.clone());
            state.last_kind = OrderTransitionKind::WireAttempted;
            state.order_seq = event.order_seq;
            Ok(state)
        }
        OrderEvidenceCommand::ExchangeObserved(observation) => {
            let mut state = require_previous(previous, event)?;
            let attempt = state
                .wire_attempt
                .as_ref()
                .ok_or_else(|| "exchange_observed requires wire_attempted".to_owned())?;
            validate_observation(observation, &event.client_order_id, attempt)?;
            apply_order_observation(
                &mut state,
                observation.order_id.as_deref(),
                observation.order_status.as_deref(),
                observation.exchange_accepted,
                observation.order_terminal,
                observation.observed_ts,
            )?;
            state.last_kind = OrderTransitionKind::ExchangeObserved;
            state.order_seq = event.order_seq;
            Ok(state)
        }
        OrderEvidenceCommand::TransportFailed(failure) => {
            let mut state = require_previous(previous, event)?;
            let attempt = state
                .wire_attempt
                .as_ref()
                .ok_or_else(|| "transport_failed requires wire_attempted".to_owned())?;
            if state.transport_failed {
                return Err("duplicate transport_failed for one wire attempt".into());
            }
            validate_transport_match(
                failure.protocol,
                &failure.endpoint_id,
                failure.failure_ts,
                attempt,
            )?;
            validate_nonempty("transport failure detail", &failure.detail)?;
            state.transport_failed = true;
            state.last_observed_ts = Some(failure.failure_ts);
            state.last_kind = OrderTransitionKind::TransportFailed;
            state.order_seq = event.order_seq;
            Ok(state)
        }
        OrderEvidenceCommand::Reconciled(observation) => {
            let mut state = require_previous(previous, event)?;
            if state.invocation_prepared.is_none() {
                return Err("reconciled requires invocation_prepared ambiguity".into());
            }
            validate_reconciled(observation, &event.client_order_id)?;
            apply_order_observation(
                &mut state,
                observation.order_id.as_deref(),
                Some(&observation.order_status),
                observation.exchange_accepted,
                observation.order_terminal,
                observation.observed_ts,
            )?;
            state.last_kind = OrderTransitionKind::Reconciled;
            state.order_seq = event.order_seq;
            Ok(state)
        }
        OrderEvidenceCommand::ReconcileUnresolved(observation) => {
            let mut state = require_previous(previous, event)?;
            if state.invocation_prepared.is_none() {
                return Err("reconcile_unresolved requires invocation_prepared ambiguity".into());
            }
            validate_endpoint(&observation.endpoint_id)?;
            validate_nonzero("reconciliation observed_ts", observation.observed_ts)?;
            if observation.queried_client_order_id != event.client_order_id {
                return Err("unresolved reconciliation query client ID is not exact".into());
            }
            if let Some(raw) = &observation.raw {
                validate_raw_evidence(raw)?;
            }
            validate_nonempty("reconciliation detail", &observation.detail)?;
            state.last_observed_ts = Some(observation.observed_ts);
            state.last_kind = OrderTransitionKind::ReconcileUnresolved;
            state.order_seq = event.order_seq;
            Ok(state)
        }
    }
}

fn require_previous(
    previous: Option<&OrderRecoveryState>,
    event: &OrderEvidenceEvent,
) -> Result<OrderRecoveryState, String> {
    previous.cloned().ok_or_else(|| {
        format!(
            "first transition for {} must be intent_evaluated, got {:?}",
            event.client_order_id,
            event.kind()
        )
    })
}

fn validate_intent_evaluated(evaluation: &IntentEvaluated) -> Result<(), String> {
    validate_identifier("client_order_id", &evaluation.client_order_id)?;
    validate_identifier("market_ticker", &evaluation.market_ticker)?;
    if !(1..=99).contains(&evaluation.price_cents) {
        return Err("price_cents must be in 1..=99".into());
    }
    if evaluation.contract_count == 0 {
        return Err("contract_count must be positive".into());
    }
    validate_nonempty("keyword", &evaluation.keyword)?;
    for (name, value) in [
        ("audio_ts", evaluation.audio_ts),
        ("decision_ts", evaluation.decision_ts),
        ("exec_ts", evaluation.exec_ts),
    ] {
        validate_nonzero(name, value)?;
    }
    if evaluation.audio_ts > evaluation.decision_ts || evaluation.decision_ts > evaluation.exec_ts {
        return Err("evaluation timestamps must satisfy audio_ts <= decision_ts <= exec_ts".into());
    }
    match (evaluation.book_as_of_ts, evaluation.best_ask_cents) {
        (0, None) => {}
        (0, Some(_)) => {
            return Err("best_ask_cents requires a nonzero book_as_of_ts".into());
        }
        (book_as_of_ts, _) if book_as_of_ts > evaluation.exec_ts => {
            return Err("book_as_of_ts must not be later than exec_ts".into());
        }
        _ => {}
    }
    if evaluation
        .best_ask_cents
        .is_some_and(|price| !(1..=99).contains(&price))
    {
        return Err("best_ask_cents must be in 1..=99 when present".into());
    }
    if !is_lower_hex(&evaluation.request_sha256, 64) {
        return Err("request_sha256 must be 64 lowercase hexadecimal characters".into());
    }
    if !is_lower_hex(&evaluation.decision_plan_sha1, 40) {
        return Err("decision_plan_sha1 must be 40 lowercase hexadecimal characters".into());
    }
    Ok(())
}

fn validate_send_requested(
    request: &SendRequested,
    evaluation: &IntentEvaluated,
) -> Result<(), String> {
    validate_identifier("client_order_id", &request.client_order_id)?;
    validate_identifier("market_ticker", &request.market_ticker)?;
    validate_nonzero("requested_ts", request.requested_ts)?;
    if request.requested_ts < evaluation.exec_ts {
        return Err("send requested_ts precedes evaluation exec_ts".into());
    }
    Ok(())
}

fn validate_observation(
    observation: &ExchangeObserved,
    client_order_id: &str,
    attempt: &WireAttempted,
) -> Result<(), String> {
    validate_transport_match(
        observation.protocol,
        &observation.endpoint_id,
        observation.observed_ts,
        attempt,
    )?;
    validate_nonempty("protocol_status", &observation.protocol_status)?;
    validate_raw_evidence(&observation.raw)?;
    validate_optional_exact_echo(
        client_order_id,
        observation.echoed_client_order_id.as_deref(),
        observation.order_id.as_deref(),
    )?;
    validate_terminal_semantics(
        observation.exchange_accepted,
        observation.order_terminal,
        observation.order_id.as_deref(),
    )?;
    validate_optional_nonempty("order_status", observation.order_status.as_deref())?;
    if let Some(fill) = &observation.fill {
        validate_fill(fill)?;
    }
    Ok(())
}

fn validate_wire_attempt(
    attempt: &WireAttempted,
    prepared: &InvocationPrepared,
) -> Result<(), String> {
    validate_endpoint(&attempt.endpoint_id)?;
    validate_nonzero("send_ts", attempt.send_ts)?;
    if attempt.protocol != prepared.protocol || attempt.endpoint_id != prepared.endpoint_id {
        return Err("wire attempt protocol/endpoint does not match prepared invocation".into());
    }
    if attempt.send_ts < prepared.prepared_ts {
        return Err("wire attempt send_ts precedes prepared_ts".into());
    }
    Ok(())
}

fn validate_reconciled(observation: &Reconciled, client_order_id: &str) -> Result<(), String> {
    validate_endpoint(&observation.endpoint_id)?;
    validate_nonzero("reconciliation observed_ts", observation.observed_ts)?;
    if observation.queried_client_order_id != client_order_id
        || observation.echoed_client_order_id != client_order_id
    {
        return Err(
            "verified reconciliation requires exact queried and echoed client_order_id".into(),
        );
    }
    if let Some(order_id) = &observation.order_id {
        validate_identifier("order_id", order_id)?;
    }
    validate_nonempty("order_status", &observation.order_status)?;
    validate_raw_evidence(&observation.raw)?;
    validate_terminal_semantics(
        observation.exchange_accepted,
        observation.order_terminal,
        observation.order_id.as_deref(),
    )?;
    if let Some(fill) = &observation.fill {
        validate_fill(fill)?;
    }
    Ok(())
}

fn validate_transport_match(
    protocol: TransportProtocol,
    endpoint_id: &str,
    timestamp: u64,
    attempt: &WireAttempted,
) -> Result<(), String> {
    validate_endpoint(endpoint_id)?;
    validate_nonzero("transport timestamp", timestamp)?;
    if protocol != attempt.protocol || endpoint_id != attempt.endpoint_id {
        return Err("exchange observation protocol/endpoint does not match wire attempt".into());
    }
    if timestamp < attempt.send_ts {
        return Err("transport observation timestamp precedes send_ts".into());
    }
    Ok(())
}

fn validate_optional_exact_echo(
    client_order_id: &str,
    echoed_client_order_id: Option<&str>,
    order_id: Option<&str>,
) -> Result<(), String> {
    if let Some(echoed) = echoed_client_order_id {
        if echoed != client_order_id {
            return Err("exchange echoed a different client_order_id".into());
        }
    }
    if order_id.is_some() && echoed_client_order_id != Some(client_order_id) {
        return Err("an exchange order_id requires an exact echoed client_order_id".into());
    }
    validate_optional_nonempty("order_id", order_id)
}

fn validate_terminal_semantics(
    accepted: Option<bool>,
    terminal: bool,
    order_id: Option<&str>,
) -> Result<(), String> {
    if accepted == Some(false) && !terminal {
        return Err("an exchange rejection must be terminal".into());
    }
    if accepted == Some(false) && order_id.is_some() {
        return Err("a rejected observation cannot establish an exchange order_id".into());
    }
    if terminal && accepted != Some(false) && order_id.is_none() {
        return Err("a terminal non-rejection requires a verified exchange order_id".into());
    }
    Ok(())
}

fn apply_order_observation(
    state: &mut OrderRecoveryState,
    order_id: Option<&str>,
    order_status: Option<&str>,
    accepted: Option<bool>,
    terminal: bool,
    observed_ts: u64,
) -> Result<(), String> {
    if let (Some(known), Some(observed)) = (state.order_id.as_deref(), order_id) {
        if known != observed {
            return Err(format!(
                "exchange order_id changed from {known} to {observed}"
            ));
        }
    }
    if let Some(order_id) = order_id {
        state.order_id = Some(order_id.to_owned());
    }
    if let Some(order_status) = order_status {
        state.order_status = Some(order_status.to_owned());
    }
    if accepted.is_some() {
        state.exchange_accepted = accepted;
    }
    state.terminal = terminal;
    state.last_observed_ts = Some(observed_ts);
    Ok(())
}

fn validate_fill(fill: &FillEvidence) -> Result<(), String> {
    let fixed_points = [
        ("filled_count_fp", fill.filled_count_fp.as_deref()),
        ("remaining_count_fp", fill.remaining_count_fp.as_deref()),
        (
            "average_price_dollars",
            fill.average_price_dollars.as_deref(),
        ),
        ("latest_fill_count_fp", fill.latest_fill_count_fp.as_deref()),
        (
            "latest_fill_price_dollars",
            fill.latest_fill_price_dollars.as_deref(),
        ),
    ];
    if fixed_points.iter().all(|(_, value)| value.is_none())
        && fill.latest_fill_exchange_ts.is_none()
    {
        return Err("fill evidence must contain at least one exact exchange field".into());
    }
    for (name, value) in fixed_points {
        if let Some(value) = value {
            validate_fixed_point(name, value)?;
        }
    }
    validate_optional_nonempty(
        "latest_fill_exchange_ts",
        fill.latest_fill_exchange_ts.as_deref(),
    )
}

fn validate_fixed_point(name: &str, value: &str) -> Result<(), String> {
    let mut parts = value.split('.');
    let whole = parts.next().unwrap_or_default();
    let fraction = parts.next();
    if whole.is_empty()
        || !whole.bytes().all(|byte| byte.is_ascii_digit())
        || fraction
            .is_some_and(|part| part.is_empty() || !part.bytes().all(|byte| byte.is_ascii_digit()))
        || parts.next().is_some()
    {
        return Err(format!("{name} must be an unsigned fixed-point string"));
    }
    Ok(())
}

fn validate_raw_evidence(raw: &RawEvidence) -> Result<Vec<u8>, String> {
    validate_nonempty("raw media_type", &raw.media_type)?;
    if !is_lower_hex(&raw.bytes_sha256, 64) {
        return Err("raw bytes_sha256 must be 64 lowercase hexadecimal characters".into());
    }
    let decoded = base64::engine::general_purpose::STANDARD
        .decode(&raw.bytes_base64)
        .map_err(|error| format!("invalid raw bytes base64: {error}"))?;
    if base64::engine::general_purpose::STANDARD.encode(&decoded) != raw.bytes_base64 {
        return Err("raw bytes_base64 is not canonical standard base64".into());
    }
    if raw.bytes_len != decoded.len() as u64 || raw.bytes_sha256 != sha256_hex(&decoded) {
        return Err("raw byte length/fingerprint mismatch".into());
    }
    Ok(decoded)
}

fn replay(
    path: &Path,
    raw: &[u8],
    identity: &OrderEvidenceIdentity,
) -> Result<(Reducer, Vec<OrderEvidenceEvent>), OrderEvidenceError> {
    if !raw.is_empty() && !raw.ends_with(b"\n") {
        return Err(OrderEvidenceError::Invalid {
            path: path.to_path_buf(),
            detail: "truncated final line".into(),
        });
    }
    let mut reducer = Reducer::new(identity.clone());
    let mut events = Vec::new();
    if raw.is_empty() {
        return Ok((reducer, events));
    }
    let complete_lines = &raw[..raw.len() - 1];
    for (index, line) in complete_lines.split(|byte| *byte == b'\n').enumerate() {
        if line.is_empty() {
            return Err(OrderEvidenceError::Invalid {
                path: path.to_path_buf(),
                detail: format!("line {} is blank", index + 1),
            });
        }
        let event: OrderEvidenceEvent =
            serde_json::from_slice(line).map_err(|error| OrderEvidenceError::Invalid {
                path: path.to_path_buf(),
                detail: format!("line {}: {error}", index + 1),
            })?;
        reducer
            .apply(event.clone())
            .map_err(|detail| OrderEvidenceError::Invalid {
                path: path.to_path_buf(),
                detail: format!("line {}: {detail}", index + 1),
            })?;
        events.push(event);
    }
    Ok((reducer, events))
}

fn validate_identity(identity: &OrderEvidenceIdentity) -> Result<(), OrderEvidenceError> {
    for (name, value) in [
        ("run_id", identity.run_id.as_str()),
        ("event_id", identity.event_id.as_str()),
        ("kalshi_env", identity.kalshi_env.as_str()),
        ("account_fingerprint", identity.account_fingerprint.as_str()),
    ] {
        validate_identifier(name, value).map_err(OrderEvidenceError::Unavailable)?;
    }
    if !is_lower_hex(&identity.plan_sha256, 64) {
        return Err(OrderEvidenceError::Unavailable(
            "plan_sha256 must be 64 lowercase hexadecimal characters".into(),
        ));
    }
    Ok(())
}

fn validate_identifier(name: &str, value: &str) -> Result<(), String> {
    validate_nonempty(name, value)?;
    if value.chars().any(char::is_control) {
        return Err(format!("{name} contains control characters"));
    }
    Ok(())
}

fn validate_endpoint(endpoint: &str) -> Result<(), String> {
    validate_identifier("endpoint_id", endpoint)
}

fn validate_nonempty(name: &str, value: &str) -> Result<(), String> {
    if value.trim().is_empty() {
        Err(format!("{name} must be non-empty"))
    } else {
        Ok(())
    }
}

fn validate_optional_nonempty(name: &str, value: Option<&str>) -> Result<(), String> {
    if let Some(value) = value {
        validate_nonempty(name, value)?;
    }
    Ok(())
}

fn validate_nonzero(name: &str, value: u64) -> Result<(), String> {
    if value == 0 {
        Err(format!("{name} must be non-zero"))
    } else {
        Ok(())
    }
}

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn event_sha256(event: &OrderEvidenceEvent) -> Result<String, String> {
    let mut canonical = serde_json::to_value(event)
        .map_err(|error| format!("serialize event for hash: {error}"))?;
    canonical
        .as_object_mut()
        .ok_or_else(|| "event hash material is not an object".to_owned())?
        .remove("event_sha256");
    let encoded = serde_json::to_vec(&canonical)
        .map_err(|error| format!("serialize canonical event hash material: {error}"))?;
    Ok(sha256_hex(&encoded))
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn process_instance_id() -> String {
    static INSTANCE: OnceLock<String> = OnceLock::new();
    INSTANCE
        .get_or_init(|| {
            let wall = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos();
            format!("{}-{wall}", std::process::id())
        })
        .clone()
}

fn sync_file(file: &File) -> std::io::Result<()> {
    file.sync_all()?;
    #[cfg(target_os = "macos")]
    {
        let rc = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_FULLFSYNC) };
        if rc == -1 {
            return Err(std::io::Error::last_os_error());
        }
    }
    Ok(())
}

fn durable_sync(file: &File, path: &Path) -> Result<(), OrderEvidenceError> {
    sync_file(file).map_err(|source| OrderEvidenceError::Io {
        path: path.to_path_buf(),
        source,
    })
}

fn sync_directory(path: &Path) -> Result<(), OrderEvidenceError> {
    File::open(path)
        .and_then(|directory| directory.sync_all())
        .map_err(|source| OrderEvidenceError::Io {
            path: path.to_path_buf(),
            source,
        })
}

#[cfg(unix)]
fn lock_exclusive_nonblocking(file: &File, path: &Path) -> Result<(), OrderEvidenceError> {
    let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc == 0 {
        Ok(())
    } else {
        Err(OrderEvidenceError::Io {
            path: path.to_path_buf(),
            source: std::io::Error::last_os_error(),
        })
    }
}

#[cfg(not(unix))]
fn lock_exclusive_nonblocking(_file: &File, path: &Path) -> Result<(), OrderEvidenceError> {
    Err(OrderEvidenceError::Io {
        path: path.to_path_buf(),
        source: std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "order evidence requires a process lock",
        ),
    })
}

pub fn unresolved_client_ids(states: &[OrderRecoveryState]) -> BTreeSet<String> {
    states
        .iter()
        .filter(|state| state.recovery_action().is_some())
        .map(|state| state.client_order_id.clone())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identity() -> OrderEvidenceIdentity {
        OrderEvidenceIdentity {
            run_id: "run-1".into(),
            event_id: "event-1".into(),
            kalshi_env: "prod".into(),
            account_fingerprint: "account-sha256".into(),
            plan_sha256: "a".repeat(64),
        }
    }

    fn evaluation(client: &str, ticker: &str) -> IntentEvaluated {
        IntentEvaluated {
            client_order_id: client.into(),
            market_ticker: ticker.into(),
            side: OrderSide::Yes,
            action: OrderAction::Buy,
            price_cents: 97,
            contract_count: 5,
            time_in_force: OrderTimeInForce::ImmediateOrCancel,
            tier: EvidenceTier::Confirmed,
            keyword_id: 3,
            keyword: "guidance raised".into(),
            source_id: 2,
            audio_ts: 10,
            decision_ts: 20,
            exec_ts: 40,
            book_as_of_ts: 30,
            best_ask_cents: Some(96),
            request_sha256: "b".repeat(64),
            decision_plan_sha1: "c".repeat(40),
        }
    }

    fn send_request(client: &str, ticker: &str) -> SendRequested {
        SendRequested {
            client_order_id: client.into(),
            market_ticker: ticker.into(),
            requested_ts: 42,
        }
    }

    fn prepared(
        client: &str,
        ticker: &str,
        protocol: TransportProtocol,
        endpoint: &str,
    ) -> InvocationPrepared {
        InvocationPrepared {
            client_order_id: client.into(),
            market_ticker: ticker.into(),
            protocol,
            endpoint_id: endpoint.into(),
            prepared_ts: 45,
            raw: RawEvidence::from_bytes("application/octet-stream", b"exact outbound bytes"),
        }
    }

    fn wire(
        client: &str,
        ticker: &str,
        protocol: TransportProtocol,
        endpoint: &str,
    ) -> WireAttempted {
        WireAttempted {
            client_order_id: client.into(),
            market_ticker: ticker.into(),
            protocol,
            endpoint_id: endpoint.into(),
            send_ts: 50,
        }
    }

    fn event_for(
        reducer: &Reducer,
        process: &str,
        transition: OrderEvidenceCommand,
    ) -> OrderEvidenceEvent {
        let client_order_id = transition.client_order_id().to_owned();
        let market_ticker = transition.market_ticker().to_owned();
        let mut event = OrderEvidenceEvent {
            schema_version: ORDER_EVIDENCE_SCHEMA_VERSION,
            journal_seq: reducer.next_journal_seq,
            order_seq: reducer
                .orders
                .get(&client_order_id)
                .map_or(0, |state| state.order_seq + 1),
            wall_unix_ns: 1,
            monotonic_ns: 1,
            process_instance_id: process.into(),
            run_id: reducer.identity.run_id.clone(),
            event_id: reducer.identity.event_id.clone(),
            kalshi_env: reducer.identity.kalshi_env.clone(),
            account_fingerprint: reducer.identity.account_fingerprint.clone(),
            plan_sha256: reducer.identity.plan_sha256.clone(),
            client_order_id,
            market_ticker,
            previous_process_instance_id: reducer.previous_process_instance_id.clone(),
            previous_event_sha256: reducer.previous_event_sha256.clone(),
            transition,
            event_sha256: String::new(),
        };
        event.event_sha256 = event_sha256(&event).unwrap();
        event
    }

    #[test]
    fn live_and_replay_use_the_same_strict_reducer() {
        let mut live = Reducer::new(identity());
        let first = event_for(
            &live,
            "process-a",
            OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
        );
        live.apply(first.clone()).unwrap();
        let terminal = event_for(
            &live,
            "process-a",
            OrderEvidenceCommand::NotAttempted(NotAttempted {
                client_order_id: "client-a".into(),
                market_ticker: "MARKET-A".into(),
                refusal_ts: 50,
                reason: "risk gate killed before invocation".into(),
            }),
        );
        live.apply(terminal.clone()).unwrap();

        let mut replayed = Reducer::new(identity());
        replayed.apply(first).unwrap();
        replayed.apply(terminal).unwrap();
        assert_eq!(live.orders, replayed.orders);
        assert!(live.orders["client-a"].terminal);
    }

    #[test]
    fn recovery_distinguishes_request_only_from_prepared_or_wire_ambiguity() {
        let mut reducer = Reducer::new(identity());
        let evaluated = event_for(
            &reducer,
            "process-a",
            OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
        );
        reducer.apply(evaluated).unwrap();
        assert_eq!(
            reducer.orders["client-a"].recovery_action(),
            Some(RecoveryAction::ConcludeNotAttempted)
        );
        assert!(!reducer.orders["client-a"].requires_reconciliation());

        let requested = event_for(
            &reducer,
            "process-a",
            OrderEvidenceCommand::SendRequested(send_request("client-a", "MARKET-A")),
        );
        reducer.apply(requested).unwrap();
        assert_eq!(
            reducer.orders["client-a"].recovery_action(),
            Some(RecoveryAction::ConcludeNotAttempted)
        );
        assert!(reducer.orders["client-a"].send_request.is_some());

        let invocation = event_for(
            &reducer,
            "process-a",
            OrderEvidenceCommand::InvocationPrepared(prepared(
                "client-a",
                "MARKET-A",
                TransportProtocol::Rest,
                "rest:prod",
            )),
        );
        reducer.apply(invocation).unwrap();
        assert_eq!(
            reducer.orders["client-a"].recovery_action(),
            Some(RecoveryAction::ReconcileExactClientId)
        );
        assert!(reducer.orders["client-a"].requires_reconciliation());

        let attempted = event_for(
            &reducer,
            "process-a",
            OrderEvidenceCommand::WireAttempted(wire(
                "client-a",
                "MARKET-A",
                TransportProtocol::Rest,
                "rest:prod",
            )),
        );
        reducer.apply(attempted).unwrap();
        assert_eq!(
            reducer.orders["client-a"].recovery_action(),
            Some(RecoveryAction::ReconcileExactClientId)
        );
        assert!(reducer.orders["client-a"].requires_reconciliation());
    }

    #[test]
    fn invocation_boundaries_are_ordered_and_not_attempted_can_close_prepared_refusal() {
        let base = |client: &str| {
            let mut reducer = Reducer::new(identity());
            let evaluated = event_for(
                &reducer,
                "process-a",
                OrderEvidenceCommand::IntentEvaluated(evaluation(client, "MARKET-A")),
            );
            reducer.apply(evaluated).unwrap();
            let requested = event_for(
                &reducer,
                "process-a",
                OrderEvidenceCommand::SendRequested(send_request(client, "MARKET-A")),
            );
            reducer.apply(requested).unwrap();
            reducer
        };

        let mut without_preparation = base("client-a");
        let attempted = event_for(
            &without_preparation,
            "process-a",
            OrderEvidenceCommand::WireAttempted(wire(
                "client-a",
                "MARKET-A",
                TransportProtocol::Rest,
                "rest:prod",
            )),
        );
        assert!(without_preparation
            .apply(attempted)
            .unwrap_err()
            .contains("after invocation_prepared"));

        let mut mismatched = base("client-b");
        let invocation = event_for(
            &mismatched,
            "process-a",
            OrderEvidenceCommand::InvocationPrepared(prepared(
                "client-b",
                "MARKET-A",
                TransportProtocol::Rest,
                "rest:prod",
            )),
        );
        mismatched.apply(invocation).unwrap();
        let wrong_endpoint = event_for(
            &mismatched,
            "process-a",
            OrderEvidenceCommand::WireAttempted(wire(
                "client-b",
                "MARKET-A",
                TransportProtocol::Rest,
                "rest:demo",
            )),
        );
        assert!(mismatched
            .apply(wrong_endpoint)
            .unwrap_err()
            .contains("does not match prepared invocation"));
        let mut early = wire("client-b", "MARKET-A", TransportProtocol::Rest, "rest:prod");
        early.send_ts = 44;
        let early = event_for(
            &mismatched,
            "process-a",
            OrderEvidenceCommand::WireAttempted(early),
        );
        assert!(mismatched
            .apply(early)
            .unwrap_err()
            .contains("send_ts precedes prepared_ts"));

        let mut refused = base("client-c");
        let invocation = event_for(
            &refused,
            "process-a",
            OrderEvidenceCommand::InvocationPrepared(prepared(
                "client-c",
                "MARKET-A",
                TransportProtocol::Fix,
                "fix:prod",
            )),
        );
        refused.apply(invocation).unwrap();
        let terminal = event_for(
            &refused,
            "process-a",
            OrderEvidenceCommand::NotAttempted(NotAttempted {
                client_order_id: "client-c".into(),
                market_ticker: "MARKET-A".into(),
                refusal_ts: 50,
                reason: "transport synchronously refused before invocation".into(),
            }),
        );
        refused.apply(terminal).unwrap();
        assert!(refused.orders["client-c"].terminal);
        assert_eq!(refused.orders["client-c"].recovery_action(), None);
    }

    #[test]
    fn reducer_rejects_observation_without_wire_and_post_terminal_transition() {
        let mut reducer = Reducer::new(identity());
        let first = event_for(
            &reducer,
            "process-a",
            OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
        );
        reducer.apply(first).unwrap();
        let raw = RawEvidence::from_bytes("application/json", b"{}");
        let observed = event_for(
            &reducer,
            "process-a",
            OrderEvidenceCommand::ExchangeObserved(ExchangeObserved {
                client_order_id: "client-a".into(),
                market_ticker: "MARKET-A".into(),
                protocol: TransportProtocol::Rest,
                endpoint_id: "rest:prod".into(),
                observed_ts: 60,
                protocol_status: "http:400".into(),
                exchange_accepted: Some(false),
                order_terminal: true,
                echoed_client_order_id: Some("client-a".into()),
                order_id: None,
                order_status: Some("rejected".into()),
                exchange_event_ts: None,
                fill: None,
                raw,
                parsed: None,
                detail: None,
            }),
        );
        assert!(reducer.apply(observed).is_err());

        let not_attempted = event_for(
            &reducer,
            "process-a",
            OrderEvidenceCommand::NotAttempted(NotAttempted {
                client_order_id: "client-a".into(),
                market_ticker: "MARKET-A".into(),
                refusal_ts: 70,
                reason: "refused".into(),
            }),
        );
        reducer.apply(not_attempted).unwrap();
        let after_terminal = event_for(
            &reducer,
            "process-a",
            OrderEvidenceCommand::ReconcileUnresolved(ReconcileUnresolved {
                client_order_id: "client-a".into(),
                market_ticker: "MARKET-A".into(),
                protocol: TransportProtocol::Rest,
                endpoint_id: "rest:prod".into(),
                observed_ts: 80,
                queried_client_order_id: "client-a".into(),
                raw: None,
                parsed: None,
                detail: "none".into(),
            }),
        );
        assert!(reducer.apply(after_terminal).is_err());
    }

    #[test]
    fn event_hash_covers_the_complete_event() {
        let reducer = Reducer::new(identity());
        let mut event = event_for(
            &reducer,
            "process-a",
            OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
        );
        assert!(reducer.validate(&event).is_ok());
        if let OrderEvidenceCommand::IntentEvaluated(evaluation) = &mut event.transition {
            evaluation.contract_count += 1;
        }
        assert!(reducer
            .validate(&event)
            .unwrap_err()
            .contains("event hash mismatch"));
    }

    #[test]
    fn every_non_request_variant_is_illegal_as_a_first_transition() {
        let raw = RawEvidence::from_bytes("application/json", b"{}");
        let commands = vec![
            OrderEvidenceCommand::SendRequested(send_request("client-a", "MARKET-A")),
            OrderEvidenceCommand::NotAttempted(NotAttempted {
                client_order_id: "client-a".into(),
                market_ticker: "MARKET-A".into(),
                refusal_ts: 10,
                reason: "refused".into(),
            }),
            OrderEvidenceCommand::InvocationPrepared(prepared(
                "client-a",
                "MARKET-A",
                TransportProtocol::Rest,
                "rest:prod",
            )),
            OrderEvidenceCommand::WireAttempted(wire(
                "client-a",
                "MARKET-A",
                TransportProtocol::Rest,
                "rest:prod",
            )),
            OrderEvidenceCommand::ExchangeObserved(ExchangeObserved {
                client_order_id: "client-a".into(),
                market_ticker: "MARKET-A".into(),
                protocol: TransportProtocol::Rest,
                endpoint_id: "rest:prod".into(),
                observed_ts: 20,
                protocol_status: "http:400".into(),
                exchange_accepted: Some(false),
                order_terminal: true,
                echoed_client_order_id: Some("client-a".into()),
                order_id: None,
                order_status: Some("rejected".into()),
                exchange_event_ts: None,
                fill: None,
                raw: raw.clone(),
                parsed: None,
                detail: None,
            }),
            OrderEvidenceCommand::TransportFailed(TransportFailed {
                client_order_id: "client-a".into(),
                market_ticker: "MARKET-A".into(),
                protocol: TransportProtocol::Rest,
                endpoint_id: "rest:prod".into(),
                failure_ts: 20,
                detail: "timeout".into(),
            }),
            OrderEvidenceCommand::Reconciled(Reconciled {
                client_order_id: "client-a".into(),
                market_ticker: "MARKET-A".into(),
                protocol: TransportProtocol::Rest,
                endpoint_id: "rest:prod".into(),
                observed_ts: 20,
                queried_client_order_id: "client-a".into(),
                echoed_client_order_id: "client-a".into(),
                order_id: Some("order-a".into()),
                order_status: "resting".into(),
                exchange_accepted: Some(true),
                order_terminal: false,
                exchange_event_ts: None,
                fill: None,
                raw: raw.clone(),
                parsed: None,
            }),
            OrderEvidenceCommand::ReconcileUnresolved(ReconcileUnresolved {
                client_order_id: "client-a".into(),
                market_ticker: "MARKET-A".into(),
                protocol: TransportProtocol::Rest,
                endpoint_id: "rest:prod".into(),
                observed_ts: 20,
                queried_client_order_id: "client-a".into(),
                raw: Some(raw),
                parsed: None,
                detail: "no exact row".into(),
            }),
        ];

        for command in commands {
            let reducer = Reducer::new(identity());
            let event = event_for(&reducer, "process-a", command);
            assert!(
                reducer.validate(&event).is_err(),
                "{:?} was accepted as a first transition",
                event.kind()
            );
        }
    }

    #[test]
    fn exchange_order_id_is_immutable_across_observations() {
        let mut reducer = Reducer::new(identity());
        let requested = event_for(
            &reducer,
            "process-a",
            OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
        );
        reducer.apply(requested).unwrap();
        let requested = event_for(
            &reducer,
            "process-a",
            OrderEvidenceCommand::SendRequested(send_request("client-a", "MARKET-A")),
        );
        reducer.apply(requested).unwrap();
        let invocation = event_for(
            &reducer,
            "process-a",
            OrderEvidenceCommand::InvocationPrepared(prepared(
                "client-a",
                "MARKET-A",
                TransportProtocol::Rest,
                "rest:prod",
            )),
        );
        reducer.apply(invocation).unwrap();
        let attempted = event_for(
            &reducer,
            "process-a",
            OrderEvidenceCommand::WireAttempted(wire(
                "client-a",
                "MARKET-A",
                TransportProtocol::Rest,
                "rest:prod",
            )),
        );
        reducer.apply(attempted).unwrap();
        let observation = |order_id: &str, observed_ts: u64| ExchangeObserved {
            client_order_id: "client-a".into(),
            market_ticker: "MARKET-A".into(),
            protocol: TransportProtocol::Rest,
            endpoint_id: "rest:prod".into(),
            observed_ts,
            protocol_status: "http:201".into(),
            exchange_accepted: Some(true),
            order_terminal: false,
            echoed_client_order_id: Some("client-a".into()),
            order_id: Some(order_id.into()),
            order_status: Some("resting".into()),
            exchange_event_ts: None,
            fill: None,
            raw: RawEvidence::from_bytes("application/json", b"{}"),
            parsed: None,
            detail: None,
        };
        let first = event_for(
            &reducer,
            "process-a",
            OrderEvidenceCommand::ExchangeObserved(observation("order-a", 60)),
        );
        reducer.apply(first).unwrap();
        let changed = event_for(
            &reducer,
            "process-a",
            OrderEvidenceCommand::ExchangeObserved(observation("order-b", 70)),
        );
        assert!(reducer
            .validate(&changed)
            .unwrap_err()
            .contains("order_id changed"));
    }

    #[test]
    fn fill_evidence_requires_one_exact_field_and_validates_each_fixed_point() {
        let empty = FillEvidence {
            filled_count_fp: None,
            remaining_count_fp: None,
            average_price_dollars: None,
            latest_fill_count_fp: None,
            latest_fill_price_dollars: None,
            latest_fill_exchange_ts: None,
        };
        assert!(validate_fill(&empty)
            .unwrap_err()
            .contains("at least one exact exchange field"));

        let partial = FillEvidence {
            latest_fill_count_fp: Some("0.50".into()),
            latest_fill_price_dollars: Some("0.97".into()),
            latest_fill_exchange_ts: Some("2026-08-12T12:00:00Z".into()),
            ..empty.clone()
        };
        validate_fill(&partial).unwrap();

        for field in [
            "filled_count_fp",
            "remaining_count_fp",
            "average_price_dollars",
            "latest_fill_count_fp",
            "latest_fill_price_dollars",
        ] {
            let mut invalid = empty.clone();
            match field {
                "filled_count_fp" => invalid.filled_count_fp = Some("1e3".into()),
                "remaining_count_fp" => invalid.remaining_count_fp = Some("1e3".into()),
                "average_price_dollars" => invalid.average_price_dollars = Some("1e3".into()),
                "latest_fill_count_fp" => invalid.latest_fill_count_fp = Some("1e3".into()),
                "latest_fill_price_dollars" => {
                    invalid.latest_fill_price_dollars = Some("1e3".into())
                }
                _ => unreachable!(),
            }
            assert!(validate_fill(&invalid).unwrap_err().contains(field));
        }
    }
}
