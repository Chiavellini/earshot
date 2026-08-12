//! Run-scoped, append-only evidence for order-relevant inbound FIX frames.
//!
//! This tape deliberately does not implement an order state machine.  It banks
//! every observation delivered by the FIX session, including frames that are
//! late, duplicate on the wire, or cannot be correlated to an order intent.
//! The stricter per-order journal may consume a legal projection later, but it
//! is never the authority for what arrived on the socket.

use std::collections::{HashMap, HashSet};
use std::fs::{File, OpenOptions};
use std::io::{self, BufWriter, Read, Write};
#[cfg(unix)]
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{mpsc, Arc, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::Engine as _;
use rsa::sha2::{Digest, Sha256};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::clock::MonotonicNs;

use super::parse::{ExecReportView, FixMsg};
use super::transport::{
    FixObservationEnvelope, FixOrderObservation, FixRejectKind, FixRejectObservation,
};

pub const FIX_OBSERVATION_TAPE_SCHEMA_VERSION: u32 = 1;
pub const FIX_OBSERVATION_RAW_MEDIA_TYPE: &str = "application/fix; version=FIXT.1.1";

const WORKER_POLL: Duration = Duration::from_millis(10);

/// Immutable identity pinned by the RunPlan and FIX connection configuration.
/// `endpoint_id` must not contain credentials; the account fingerprint already
/// supplies the stable account identity.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FixObservationTapeIdentity {
    pub run_id: String,
    pub event_id: String,
    pub kalshi_env: String,
    pub account_fingerprint: String,
    pub plan_sha256: String,
    pub endpoint_id: String,
}

#[derive(Debug, Clone)]
pub struct FixObservationTapeOptions {
    /// Maximum observations accepted without blocking the FIX reader.
    pub queue_capacity: usize,
    /// Bound from close intent until every producer has dropped.
    pub producer_quiescence_timeout: Duration,
}

impl Default for FixObservationTapeOptions {
    fn default() -> Self {
        Self {
            queue_capacity: 16_384,
            producer_quiescence_timeout: Duration::from_secs(5),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FixObservationTapeCloseReason {
    Requested,
    Deadline,
    Sigint,
    Sigterm,
    EvidenceFault,
}

impl FixObservationTapeCloseReason {
    fn as_str(self) -> &'static str {
        match self {
            Self::Requested => "requested",
            Self::Deadline => "deadline",
            Self::Sigint => "sigint",
            Self::Sigterm => "sigterm",
            Self::EvidenceFault => "evidence_fault",
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum FixObservationTapeError {
    #[error("FIX observation tape I/O at {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: io::Error,
    },
    #[error("invalid FIX observation tape {path}: {detail}")]
    Invalid { path: PathBuf, detail: String },
    #[error("FIX observation tape unavailable: {0}")]
    Unavailable(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FixObservationOfferError {
    Full,
    Closed,
    Unavailable,
}

#[derive(Debug)]
struct HealthInner {
    fault: OnceLock<String>,
    notification: tokio::sync::watch::Sender<Option<String>>,
}

#[derive(Debug, Clone)]
pub struct FixObservationTapeHealth {
    inner: Arc<HealthInner>,
}

impl FixObservationTapeHealth {
    pub fn fault_detail(&self) -> Option<String> {
        self.inner.fault.get().cloned()
    }

    pub fn is_healthy(&self) -> bool {
        self.inner.fault.get().is_none()
    }

    pub fn subscribe_failures(&self) -> tokio::sync::watch::Receiver<Option<String>> {
        self.inner.notification.subscribe()
    }

    fn poison(&self, detail: impl Into<String>) {
        let detail = detail.into();
        if self.inner.fault.set(detail.clone()).is_ok() {
            self.inner.notification.send_replace(Some(detail));
        }
    }
}

/// Nonblocking producer door intended to be installed in the FIX transport
/// before its reader starts. A rejected offer is a run-fatal evidence fault;
/// it is counted and surfaced instead of silently disappearing.
pub struct FixObservationTap {
    sender: Option<mpsc::SyncSender<FixObservationEnvelope>>,
    accepted: Arc<AtomicU64>,
    dropped: Arc<AtomicU64>,
    health: FixObservationTapeHealth,
}

impl Clone for FixObservationTap {
    fn clone(&self) -> Self {
        Self {
            sender: Some(
                self.sender
                    .as_ref()
                    .expect("live FIX observation tap has a sender")
                    .clone(),
            ),
            accepted: Arc::clone(&self.accepted),
            dropped: Arc::clone(&self.dropped),
            health: self.health.clone(),
        }
    }
}

impl FixObservationTap {
    pub fn try_record(
        &self,
        observation: FixObservationEnvelope,
    ) -> Result<(), FixObservationOfferError> {
        if self.health.fault_detail().is_some() {
            self.dropped.fetch_add(1, Ordering::Relaxed);
            return Err(FixObservationOfferError::Unavailable);
        }
        match self
            .sender
            .as_ref()
            .expect("live FIX observation tap has a sender")
            .try_send(observation)
        {
            Ok(()) => {
                self.accepted.fetch_add(1, Ordering::Release);
                Ok(())
            }
            Err(mpsc::TrySendError::Full(_)) => {
                self.dropped.fetch_add(1, Ordering::Relaxed);
                self.health.poison("FIX observation tape queue overflow");
                Err(FixObservationOfferError::Full)
            }
            Err(mpsc::TrySendError::Disconnected(_)) => {
                self.dropped.fetch_add(1, Ordering::Relaxed);
                self.health
                    .poison("FIX observation tape writer stopped while producer remained live");
                Err(FixObservationOfferError::Closed)
            }
        }
    }

    pub fn accepted(&self) -> u64 {
        self.accepted.load(Ordering::Acquire)
    }

    pub fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Acquire)
    }

    pub fn health(&self) -> FixObservationTapeHealth {
        self.health.clone()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixRawEvidence {
    pub media_type: String,
    pub bytes_base64: String,
    pub bytes_len: u64,
    pub bytes_sha256: String,
}

impl FixRawEvidence {
    fn from_bytes(bytes: &[u8]) -> Self {
        Self {
            media_type: FIX_OBSERVATION_RAW_MEDIA_TYPE.to_string(),
            bytes_base64: base64::engine::general_purpose::STANDARD.encode(bytes),
            bytes_len: bytes.len() as u64,
            bytes_sha256: sha256_hex(bytes),
        }
    }

    pub fn decode(&self) -> Result<Vec<u8>, String> {
        validate_raw_evidence(self)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FixObservationKind {
    ExecutionReport,
    SessionReject,
    BusinessReject,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixExecutionProjection {
    pub client_order_id: Option<String>,
    pub order_id: Option<String>,
    pub status: String,
    pub terminal: bool,
    pub parsed_fields: Value,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixRejectProjection {
    pub client_order_id: Option<String>,
    pub ref_seq: Option<u64>,
    pub ref_msg_type: Option<String>,
    pub business_ref_id: Option<String>,
    pub reason: Option<String>,
    pub text: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixObservationRecord {
    pub source_sequence: u64,
    pub generation: u64,
    pub receipt_id: String,
    pub frame_key: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duplicate_of_tape_seq: Option<u64>,
    pub kind: FixObservationKind,
    pub fix_msg_type: String,
    pub fix_msg_seq_num: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fix_sending_time: Option<String>,
    pub recv_monotonic_ns: u64,
    pub ack_monotonic_ns: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub execution: Option<FixExecutionProjection>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reject: Option<FixRejectProjection>,
    pub raw: FixRawEvidence,
}

impl FixObservationRecord {
    pub fn client_order_id(&self) -> Option<&str> {
        self.execution
            .as_ref()
            .and_then(|execution| execution.client_order_id.as_deref())
            .or_else(|| {
                self.reject
                    .as_ref()
                    .and_then(|reject| reject.client_order_id.as_deref())
            })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum FixObservationTapeRecordKind {
    SessionStarted {
        #[serde(skip_serializing_if = "Option::is_none")]
        recovered_unclean_process_instance_id: Option<String>,
    },
    Observation {
        observation: FixObservationRecord,
    },
    SessionClosed {
        reason: String,
        close_requested: bool,
        producer_quiesced: bool,
        terminal_accounting_complete: bool,
        session_complete: bool,
        tape_complete: bool,
        accepted_deliveries: u64,
        durably_accounted_deliveries: u64,
        observation_rows: u64,
        idempotent_retries: u64,
        execution_rows: u64,
        reject_rows: u64,
        correlated_rows: u64,
        orphan_rows: u64,
        duplicate_frames: u64,
        dropped_observations: u64,
        unwritten_observations: u64,
        prior_incomplete_sessions: u64,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixObservationTapeEvent {
    pub schema_version: u32,
    pub tape_seq: u64,
    pub wall_unix_ns: u64,
    pub monotonic_ns: u64,
    pub process_instance_id: String,
    pub run_id: String,
    pub event_id: String,
    pub kalshi_env: String,
    pub account_fingerprint: String,
    pub plan_sha256: String,
    pub endpoint_id: String,
    pub previous_process_instance_id: String,
    pub previous_record_sha256: String,
    pub record: FixObservationTapeRecordKind,
    pub record_sha256: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct FixObservationTapeSummary {
    pub complete: bool,
    pub committed: bool,
    pub reason: String,
    pub process_instance_id: String,
    pub accepted_deliveries: u64,
    pub durably_accounted_deliveries: u64,
    pub observation_rows: u64,
    pub idempotent_retries: u64,
    pub execution_rows: u64,
    pub reject_rows: u64,
    pub correlated_rows: u64,
    pub orphan_rows: u64,
    pub duplicate_frames: u64,
    pub dropped_observations: u64,
    pub unwritten_observations: u64,
    pub prior_incomplete_sessions: u64,
    pub producer_quiesced: bool,
    pub terminal_accounting_complete: bool,
    pub close_record_sha256: Option<String>,
    pub commit_path: Option<String>,
    pub writer_error: Option<String>,
}

#[derive(Debug, Clone)]
pub struct FixObservationTapeVerification {
    pub complete: bool,
    pub record_count: u64,
    pub observation_rows: u64,
    pub session_count: u64,
    pub incomplete_sessions: u64,
    pub active_process_instance_id: Option<String>,
    pub final_record_sha256: String,
}

struct CloseRequest {
    reason: FixObservationTapeCloseReason,
    producer_deadline: Instant,
}

pub struct FixObservationTapeHandle {
    close_sender: Option<mpsc::Sender<CloseRequest>>,
    join: Option<tokio::task::JoinHandle<FixObservationTapeSummary>>,
    summary: Option<FixObservationTapeSummary>,
    health: FixObservationTapeHealth,
    producer_quiescence_timeout: Duration,
}

impl FixObservationTapeHandle {
    pub fn request_close(
        &mut self,
        reason: FixObservationTapeCloseReason,
    ) -> Result<(), FixObservationTapeError> {
        let sender = self.close_sender.take().ok_or_else(|| {
            FixObservationTapeError::Unavailable(
                "FIX observation tape close already requested".to_string(),
            )
        })?;
        sender
            .send(CloseRequest {
                reason,
                producer_deadline: Instant::now() + self.producer_quiescence_timeout,
            })
            .map_err(|_| {
                let detail = "FIX observation tape writer stopped before close request".to_string();
                self.health.poison(detail.clone());
                FixObservationTapeError::Unavailable(detail)
            })
    }

    pub fn health(&self) -> FixObservationTapeHealth {
        self.health.clone()
    }

    pub fn subscribe_failures(&self) -> tokio::sync::watch::Receiver<Option<String>> {
        self.health.subscribe_failures()
    }

    pub fn is_finished(&self) -> bool {
        self.summary.is_some() || self.join.as_ref().is_none_or(|join| join.is_finished())
    }

    /// Cancellation-safe: losing a `tokio::select!` race keeps the writer join
    /// handle attached to this object.
    pub async fn wait(&mut self) -> Result<FixObservationTapeSummary, String> {
        if let Some(summary) = &self.summary {
            return Ok(summary.clone());
        }
        let result = {
            let join = self
                .join
                .as_mut()
                .ok_or_else(|| "FIX observation tape join missing".to_string())?;
            (&mut *join).await
        };
        self.join.take();
        match result {
            Ok(summary) => {
                self.summary = Some(summary.clone());
                Ok(summary)
            }
            Err(error) => {
                let detail = format!("FIX observation tape blocking task failed: {error}");
                self.health.poison(detail.clone());
                Err(detail)
            }
        }
    }
}

pub struct FixObservationTapeTask;

impl FixObservationTapeTask {
    /// Open/replay the run tape under an already-created canonical parent,
    /// acquire its exclusive lease, and sync a new session-start record before
    /// returning the producer door.
    pub async fn start(
        path: impl AsRef<Path>,
        identity: FixObservationTapeIdentity,
        options: FixObservationTapeOptions,
    ) -> Result<(FixObservationTap, FixObservationTapeHandle), FixObservationTapeError> {
        validate_options(&identity, &options)?;
        let path = path.as_ref().to_path_buf();
        let prepared = tokio::task::spawn_blocking(move || prepare(path, identity))
            .await
            .map_err(|error| {
                FixObservationTapeError::Unavailable(format!(
                    "FIX observation tape preparation task failed: {error}"
                ))
            })??;

        let (sender, receiver) = mpsc::sync_channel(options.queue_capacity);
        let (close_sender, close_receiver) = mpsc::channel();
        let accepted = Arc::new(AtomicU64::new(0));
        let dropped = Arc::new(AtomicU64::new(0));
        let (notification, _) = tokio::sync::watch::channel(None);
        let health = FixObservationTapeHealth {
            inner: Arc::new(HealthInner {
                fault: OnceLock::new(),
                notification,
            }),
        };
        let worker = Worker {
            path: prepared.path,
            file: prepared.file,
            reducer: prepared.reducer,
            process_instance_id: prepared.process_instance_id,
            receiver,
            close_receiver,
            accepted: Arc::clone(&accepted),
            dropped: Arc::clone(&dropped),
            health: health.clone(),
        };
        let join = tokio::task::spawn_blocking(move || worker.run());
        Ok((
            FixObservationTap {
                sender: Some(sender),
                accepted,
                dropped,
                health: health.clone(),
            },
            FixObservationTapeHandle {
                close_sender: Some(close_sender),
                join: Some(join),
                summary: None,
                health,
                producer_quiescence_timeout: options.producer_quiescence_timeout,
            },
        ))
    }
}

struct Prepared {
    path: PathBuf,
    file: File,
    reducer: Reducer,
    process_instance_id: String,
}

fn prepare(
    path: PathBuf,
    identity: FixObservationTapeIdentity,
) -> Result<Prepared, FixObservationTapeError> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .ok_or_else(|| invalid(&path, "tape path has no parent"))?;
    if !parent.is_dir() {
        return Err(invalid(
            &path,
            format!(
                "canonical parent {} must already exist and be a directory",
                parent.display()
            ),
        ));
    }
    match std::fs::symlink_metadata(&path) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            return Err(invalid(&path, "tape path must not be a symbolic link"));
        }
        Ok(metadata) if !metadata.is_file() => {
            return Err(invalid(&path, "tape path exists but is not a regular file"));
        }
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(source) => return Err(io_error(&path, source)),
    }
    let mut file = OpenOptions::new()
        .create(true)
        .read(true)
        .append(true)
        .open(&path)
        .map_err(|source| io_error(&path, source))?;
    lock_exclusive_nonblocking(&file, &path)?;
    sync_file(&file).map_err(|source| io_error(&path, source))?;
    sync_parent_dir(&path).map_err(|source| io_error(&path, source))?;

    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)
        .map_err(|source| io_error(&path, source))?;
    let mut reducer = replay_bytes(&path, &bytes, &identity)?;
    let process_instance_id = new_process_instance_id();
    let recovered = reducer
        .active
        .as_ref()
        .map(|session| session.process_instance_id.clone());
    let start = reducer.build_event(
        &process_instance_id,
        FixObservationTapeRecordKind::SessionStarted {
            recovered_unclean_process_instance_id: recovered,
        },
    )?;
    append_event(&path, &mut file, &mut reducer, start)?;
    Ok(Prepared {
        path,
        file,
        reducer,
        process_instance_id,
    })
}

struct Worker {
    path: PathBuf,
    file: File,
    reducer: Reducer,
    process_instance_id: String,
    receiver: mpsc::Receiver<FixObservationEnvelope>,
    close_receiver: mpsc::Receiver<CloseRequest>,
    accepted: Arc<AtomicU64>,
    dropped: Arc<AtomicU64>,
    health: FixObservationTapeHealth,
}

impl Worker {
    fn run(mut self) -> FixObservationTapeSummary {
        let mut close_request = None;
        let mut control_disconnected = false;
        let mut writer_error = None;
        let mut unwritten = 0u64;

        loop {
            self.poll_close(&mut close_request, &mut control_disconnected);
            if close_request
                .as_ref()
                .is_some_and(|request| Instant::now() >= request.producer_deadline)
            {
                let pending = self.receiver.try_iter().count() as u64;
                unwritten = unwritten.saturating_add(pending);
                let detail = "FIX observation producer did not quiesce before close deadline";
                self.health.poison(detail);
                return self.summary(
                    close_request
                        .as_ref()
                        .map(|request| request.reason.as_str()),
                    false,
                    false,
                    false,
                    unwritten,
                    None,
                    None,
                    writer_error.or_else(|| Some(detail.to_string())),
                );
            }

            let wait = close_request.as_ref().map_or(WORKER_POLL, |request| {
                WORKER_POLL.min(
                    request
                        .producer_deadline
                        .saturating_duration_since(Instant::now()),
                )
            });
            match self.receiver.recv_timeout(wait) {
                Ok(envelope) => {
                    if writer_error.is_some() {
                        unwritten += 1;
                        continue;
                    }
                    if let Err(error) = self.record(envelope) {
                        unwritten += 1;
                        let detail = error.to_string();
                        self.health.poison(detail.clone());
                        writer_error = Some(detail);
                    }
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    self.poll_close(&mut close_request, &mut control_disconnected);
                    let requested = close_request.is_some();
                    let reason = close_request
                        .as_ref()
                        .map_or("producer_disconnected_without_close", |request| {
                            request.reason.as_str()
                        });
                    if !requested {
                        self.health
                            .poison("FIX observation producer disconnected without close intent");
                    }
                    if writer_error.is_some() {
                        return self.summary(
                            Some(reason),
                            requested,
                            true,
                            true,
                            unwritten,
                            None,
                            None,
                            writer_error,
                        );
                    }
                    return self.finalize(reason, requested, unwritten);
                }
            }
        }
    }

    fn poll_close(
        &self,
        close_request: &mut Option<CloseRequest>,
        control_disconnected: &mut bool,
    ) {
        if close_request.is_some() || *control_disconnected {
            return;
        }
        match self.close_receiver.try_recv() {
            Ok(request) => *close_request = Some(request),
            Err(mpsc::TryRecvError::Empty) => {}
            Err(mpsc::TryRecvError::Disconnected) => *control_disconnected = true,
        }
    }

    fn record(&mut self, envelope: FixObservationEnvelope) -> Result<(), FixObservationTapeError> {
        let source_sequence = envelope.sequence;
        if let Some(existing) = self
            .reducer
            .receipt_for_envelope(&self.process_instance_id, &envelope)?
        {
            let active = self.reducer.active.as_mut().expect("active tape session");
            if existing == source_sequence {
                active.idempotent_retries += 1;
                active.durably_accounted_deliveries += 1;
                return Ok(());
            }
        }
        let observation = self
            .reducer
            .make_observation(&self.process_instance_id, envelope)?;
        let event = self.reducer.build_event(
            &self.process_instance_id,
            FixObservationTapeRecordKind::Observation { observation },
        )?;
        append_event(&self.path, &mut self.file, &mut self.reducer, event)
    }

    fn finalize(
        &mut self,
        reason: &str,
        close_requested: bool,
        unwritten: u64,
    ) -> FixObservationTapeSummary {
        let accepted = self.accepted.load(Ordering::Acquire);
        let dropped = self.dropped.load(Ordering::Acquire);
        let active = self.reducer.active.as_ref().expect("active tape session");
        let durably_accounted = active.durably_accounted_deliveries;
        let terminal_accounting_complete = accepted == durably_accounted && unwritten == 0;
        let session_complete = close_requested
            && terminal_accounting_complete
            && dropped == 0
            && self.health.fault_detail().is_none();
        let tape_complete = session_complete && self.reducer.prior_incomplete_sessions == 0;
        let record = FixObservationTapeRecordKind::SessionClosed {
            reason: reason.to_string(),
            close_requested,
            producer_quiesced: true,
            terminal_accounting_complete,
            session_complete,
            tape_complete,
            accepted_deliveries: accepted,
            durably_accounted_deliveries: durably_accounted,
            observation_rows: active.observation_rows,
            idempotent_retries: active.idempotent_retries,
            execution_rows: active.execution_rows,
            reject_rows: active.reject_rows,
            correlated_rows: active.correlated_rows,
            orphan_rows: active.orphan_rows,
            duplicate_frames: active.duplicate_frames,
            dropped_observations: dropped,
            unwritten_observations: unwritten,
            prior_incomplete_sessions: self.reducer.prior_incomplete_sessions,
        };
        let event = match self.reducer.build_event(&self.process_instance_id, record) {
            Ok(event) => event,
            Err(error) => {
                let detail = error.to_string();
                self.health.poison(detail.clone());
                return self.summary(
                    Some(reason),
                    close_requested,
                    true,
                    terminal_accounting_complete,
                    unwritten,
                    None,
                    None,
                    Some(detail),
                );
            }
        };
        let close_hash = event.record_sha256.clone();
        let close_seq = event.tape_seq;
        if let Err(error) = append_event(&self.path, &mut self.file, &mut self.reducer, event) {
            let detail = error.to_string();
            self.health.poison(detail.clone());
            return self.summary(
                Some(reason),
                close_requested,
                true,
                terminal_accounting_complete,
                unwritten,
                None,
                None,
                Some(detail),
            );
        }
        let commit_path = match publish_commit(
            &self.path,
            &self.process_instance_id,
            close_seq,
            &close_hash,
            session_complete,
            tape_complete,
        ) {
            Ok(path) => Some(path),
            Err(error) => {
                let detail = error.to_string();
                self.health.poison(detail.clone());
                return self.summary(
                    Some(reason),
                    close_requested,
                    true,
                    terminal_accounting_complete,
                    unwritten,
                    Some(close_hash),
                    None,
                    Some(detail),
                );
            }
        };
        self.summary(
            Some(reason),
            close_requested,
            true,
            terminal_accounting_complete,
            unwritten,
            Some(close_hash),
            commit_path,
            None,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn summary(
        &self,
        reason: Option<&str>,
        close_requested: bool,
        producer_quiesced: bool,
        terminal_accounting_complete: bool,
        unwritten: u64,
        close_record_sha256: Option<String>,
        commit_path: Option<PathBuf>,
        writer_error: Option<String>,
    ) -> FixObservationTapeSummary {
        let active = self
            .reducer
            .active
            .as_ref()
            .or(self.reducer.last_closed.as_ref());
        let accepted = self.accepted.load(Ordering::Acquire);
        let dropped = self.dropped.load(Ordering::Acquire);
        let durably_accounted = active.map_or(accepted.saturating_sub(unwritten), |session| {
            session.durably_accounted_deliveries
        });
        let committed = commit_path.is_some();
        let complete = committed
            && close_requested
            && producer_quiesced
            && terminal_accounting_complete
            && dropped == 0
            && writer_error.is_none()
            && self.reducer.prior_incomplete_sessions == 0;
        FixObservationTapeSummary {
            complete,
            committed,
            reason: reason.unwrap_or("unknown").to_string(),
            process_instance_id: self.process_instance_id.clone(),
            accepted_deliveries: accepted,
            durably_accounted_deliveries: durably_accounted,
            observation_rows: active.map_or(0, |session| session.observation_rows),
            idempotent_retries: active.map_or(0, |session| session.idempotent_retries),
            execution_rows: active.map_or(0, |session| session.execution_rows),
            reject_rows: active.map_or(0, |session| session.reject_rows),
            correlated_rows: active.map_or(0, |session| session.correlated_rows),
            orphan_rows: active.map_or(0, |session| session.orphan_rows),
            duplicate_frames: active.map_or(0, |session| session.duplicate_frames),
            dropped_observations: dropped,
            unwritten_observations: unwritten,
            prior_incomplete_sessions: self.reducer.prior_incomplete_sessions,
            producer_quiesced,
            terminal_accounting_complete,
            close_record_sha256,
            commit_path: commit_path.map(|path| path.display().to_string()),
            writer_error,
        }
    }
}

#[derive(Debug, Clone)]
struct SessionState {
    process_instance_id: String,
    next_source_sequence: u64,
    observation_rows: u64,
    idempotent_retries: u64,
    durably_accounted_deliveries: u64,
    execution_rows: u64,
    reject_rows: u64,
    correlated_rows: u64,
    orphan_rows: u64,
    duplicate_frames: u64,
}

impl SessionState {
    fn new(process_instance_id: String) -> Self {
        Self {
            process_instance_id,
            next_source_sequence: 1,
            observation_rows: 0,
            idempotent_retries: 0,
            durably_accounted_deliveries: 0,
            execution_rows: 0,
            reject_rows: 0,
            correlated_rows: 0,
            orphan_rows: 0,
            duplicate_frames: 0,
        }
    }
}

#[derive(Debug, Clone)]
struct ClosedSession {
    process_instance_id: String,
    close_tape_seq: u64,
    close_record_sha256: String,
    declared_session_complete: bool,
    declared_tape_complete: bool,
    end_offset: usize,
}

#[derive(Debug, Clone)]
struct Reducer {
    identity: FixObservationTapeIdentity,
    next_tape_seq: u64,
    previous_process_instance_id: String,
    previous_record_sha256: String,
    active: Option<SessionState>,
    last_closed: Option<SessionState>,
    seen_receipts: HashMap<String, FixObservationRecord>,
    first_frames: HashMap<String, u64>,
    seen_process_instances: HashSet<String>,
    prior_incomplete_sessions: u64,
    total_observation_rows: u64,
    session_count: u64,
    closed_sessions: Vec<ClosedSession>,
}

impl Reducer {
    fn new(identity: FixObservationTapeIdentity) -> Self {
        Self {
            previous_record_sha256: identity.plan_sha256.clone(),
            identity,
            next_tape_seq: 0,
            previous_process_instance_id: String::new(),
            active: None,
            last_closed: None,
            seen_receipts: HashMap::new(),
            first_frames: HashMap::new(),
            seen_process_instances: HashSet::new(),
            prior_incomplete_sessions: 0,
            total_observation_rows: 0,
            session_count: 0,
            closed_sessions: Vec::new(),
        }
    }

    fn build_event(
        &self,
        process_instance_id: &str,
        record: FixObservationTapeRecordKind,
    ) -> Result<FixObservationTapeEvent, FixObservationTapeError> {
        let mut event = FixObservationTapeEvent {
            schema_version: FIX_OBSERVATION_TAPE_SCHEMA_VERSION,
            tape_seq: self.next_tape_seq,
            wall_unix_ns: wall_unix_ns(),
            monotonic_ns: MonotonicNs::now().0,
            process_instance_id: process_instance_id.to_string(),
            run_id: self.identity.run_id.clone(),
            event_id: self.identity.event_id.clone(),
            kalshi_env: self.identity.kalshi_env.clone(),
            account_fingerprint: self.identity.account_fingerprint.clone(),
            plan_sha256: self.identity.plan_sha256.clone(),
            endpoint_id: self.identity.endpoint_id.clone(),
            previous_process_instance_id: self.previous_process_instance_id.clone(),
            previous_record_sha256: self.previous_record_sha256.clone(),
            record,
            record_sha256: String::new(),
        };
        event.record_sha256 =
            record_sha256(&event).map_err(|detail| FixObservationTapeError::Invalid {
                path: PathBuf::from("<in-memory>"),
                detail,
            })?;
        Ok(event)
    }

    fn make_observation(
        &self,
        process_instance_id: &str,
        envelope: FixObservationEnvelope,
    ) -> Result<FixObservationRecord, FixObservationTapeError> {
        let active = self.active.as_ref().ok_or_else(|| {
            FixObservationTapeError::Unavailable("observation has no active tape session".into())
        })?;
        if active.process_instance_id != process_instance_id {
            return Err(FixObservationTapeError::Unavailable(
                "observation process instance does not own active tape session".into(),
            ));
        }
        if envelope.sequence != active.next_source_sequence {
            return Err(FixObservationTapeError::Unavailable(format!(
                "expected FIX observation sequence {}, got {}",
                active.next_source_sequence, envelope.sequence
            )));
        }
        observation_from_envelope(
            &self.identity.endpoint_id,
            process_instance_id,
            envelope,
            &self.first_frames,
        )
        .map_err(FixObservationTapeError::Unavailable)
    }

    fn receipt_for_envelope(
        &self,
        process_instance_id: &str,
        envelope: &FixObservationEnvelope,
    ) -> Result<Option<u64>, FixObservationTapeError> {
        let candidate = observation_from_envelope(
            &self.identity.endpoint_id,
            process_instance_id,
            envelope.clone(),
            &self.first_frames,
        )
        .map_err(FixObservationTapeError::Unavailable)?;
        match self.seen_receipts.get(&candidate.receipt_id) {
            Some(existing) => {
                // `duplicate_of_tape_seq` describes this receipt's first frame
                // at append time. On a retry the frame is now already known,
                // so recomputation would add a pointer that the original row
                // correctly lacked. Ignore only that derived field.
                let mut comparable = candidate;
                comparable.duplicate_of_tape_seq = existing.duplicate_of_tape_seq;
                if existing == &comparable {
                    Ok(Some(existing.source_sequence))
                } else {
                    Err(FixObservationTapeError::Unavailable(
                        "FIX observation receipt id was reused with different evidence".into(),
                    ))
                }
            }
            None => Ok(None),
        }
    }

    fn validate_event(
        &mut self,
        event: &FixObservationTapeEvent,
    ) -> Result<Option<ClosedSession>, String> {
        validate_event_envelope(self, event)?;
        match &event.record {
            FixObservationTapeRecordKind::SessionStarted {
                recovered_unclean_process_instance_id,
            } => {
                let expected_recovery = self
                    .active
                    .as_ref()
                    .map(|session| session.process_instance_id.as_str());
                if recovered_unclean_process_instance_id.as_deref() != expected_recovery {
                    return Err(
                        "session start does not identify the exact unclean predecessor".into(),
                    );
                }
                if self.active.take().is_some() {
                    self.prior_incomplete_sessions += 1;
                }
                validate_process_instance_id(&event.process_instance_id)?;
                if !self
                    .seen_process_instances
                    .insert(event.process_instance_id.clone())
                {
                    return Err("process_instance_id was reused by another tape session".into());
                }
                self.active = Some(SessionState::new(event.process_instance_id.clone()));
                self.last_closed = None;
                self.session_count += 1;
                Ok(None)
            }
            FixObservationTapeRecordKind::Observation { observation } => {
                let active = self
                    .active
                    .as_mut()
                    .ok_or_else(|| "observation appears outside a tape session".to_string())?;
                if active.process_instance_id != event.process_instance_id {
                    return Err("observation process instance differs from active session".into());
                }
                if observation.source_sequence != active.next_source_sequence {
                    return Err(format!(
                        "expected source sequence {}, got {}",
                        active.next_source_sequence, observation.source_sequence
                    ));
                }
                validate_observation_record(
                    &self.identity.endpoint_id,
                    &event.process_instance_id,
                    observation,
                    &self.first_frames,
                )?;
                if self.seen_receipts.contains_key(&observation.receipt_id) {
                    return Err("duplicate receipt id was appended instead of deduplicated".into());
                }
                self.seen_receipts
                    .insert(observation.receipt_id.clone(), observation.clone());
                self.first_frames
                    .entry(observation.frame_key.clone())
                    .or_insert(event.tape_seq);
                active.next_source_sequence += 1;
                active.observation_rows += 1;
                active.durably_accounted_deliveries += 1;
                self.total_observation_rows += 1;
                match observation.kind {
                    FixObservationKind::ExecutionReport => active.execution_rows += 1,
                    FixObservationKind::SessionReject | FixObservationKind::BusinessReject => {
                        active.reject_rows += 1
                    }
                }
                if observation.client_order_id().is_some() {
                    active.correlated_rows += 1;
                } else {
                    active.orphan_rows += 1;
                }
                if observation.duplicate_of_tape_seq.is_some() {
                    active.duplicate_frames += 1;
                }
                Ok(None)
            }
            FixObservationTapeRecordKind::SessionClosed {
                reason,
                close_requested,
                producer_quiesced,
                terminal_accounting_complete,
                session_complete,
                tape_complete,
                accepted_deliveries,
                durably_accounted_deliveries,
                observation_rows,
                idempotent_retries,
                execution_rows,
                reject_rows,
                correlated_rows,
                orphan_rows,
                duplicate_frames,
                dropped_observations,
                unwritten_observations,
                prior_incomplete_sessions,
            } => {
                validate_nonempty("close reason", reason)?;
                let active = self
                    .active
                    .as_ref()
                    .ok_or_else(|| "session close appears without an active session".to_string())?;
                if active.process_instance_id != event.process_instance_id {
                    return Err("session close process instance differs from active session".into());
                }
                let expected = [
                    (
                        "observation_rows",
                        *observation_rows,
                        active.observation_rows,
                    ),
                    (
                        "idempotent_retries",
                        *idempotent_retries,
                        active.idempotent_retries,
                    ),
                    ("execution_rows", *execution_rows, active.execution_rows),
                    ("reject_rows", *reject_rows, active.reject_rows),
                    ("correlated_rows", *correlated_rows, active.correlated_rows),
                    ("orphan_rows", *orphan_rows, active.orphan_rows),
                    (
                        "duplicate_frames",
                        *duplicate_frames,
                        active.duplicate_frames,
                    ),
                    (
                        "durably_accounted_deliveries",
                        *durably_accounted_deliveries,
                        active.durably_accounted_deliveries,
                    ),
                    (
                        "prior_incomplete_sessions",
                        *prior_incomplete_sessions,
                        self.prior_incomplete_sessions,
                    ),
                ];
                for (name, actual, expected) in expected {
                    if actual != expected {
                        return Err(format!(
                            "session close {name} {actual} != replay {expected}"
                        ));
                    }
                }
                if *accepted_deliveries != observation_rows + idempotent_retries {
                    return Err("accepted deliveries do not equal observations plus retries".into());
                }
                if durably_accounted_deliveries != accepted_deliveries {
                    return Err("session close leaves accepted deliveries unaccounted".into());
                }
                let recomputed_accounting = *producer_quiesced && *unwritten_observations == 0;
                if *terminal_accounting_complete != recomputed_accounting {
                    return Err("terminal accounting completeness is inconsistent".into());
                }
                let recomputed_session_complete =
                    *close_requested && recomputed_accounting && *dropped_observations == 0;
                if *session_complete != recomputed_session_complete {
                    return Err("session completeness is inconsistent".into());
                }
                let recomputed_tape_complete =
                    recomputed_session_complete && self.prior_incomplete_sessions == 0;
                if *tape_complete != recomputed_tape_complete {
                    return Err("tape completeness is inconsistent".into());
                }
                let closed = ClosedSession {
                    process_instance_id: event.process_instance_id.clone(),
                    close_tape_seq: event.tape_seq,
                    close_record_sha256: event.record_sha256.clone(),
                    declared_session_complete: *session_complete,
                    declared_tape_complete: *tape_complete,
                    end_offset: 0,
                };
                if !session_complete {
                    self.prior_incomplete_sessions += 1;
                }
                self.last_closed = self.active.take();
                Ok(Some(closed))
            }
        }
    }

    fn commit_event(&mut self, event: &FixObservationTapeEvent) {
        self.next_tape_seq += 1;
        self.previous_process_instance_id = event.process_instance_id.clone();
        self.previous_record_sha256 = event.record_sha256.clone();
    }
}

fn append_event(
    path: &Path,
    file: &mut File,
    reducer: &mut Reducer,
    event: FixObservationTapeEvent,
) -> Result<(), FixObservationTapeError> {
    // Validation is intentionally transactional: disk failure must not advance
    // in-memory "durable" counters or close the active session.
    let mut candidate = reducer.clone();
    let closed = candidate
        .validate_event(&event)
        .map_err(|detail| invalid(path, detail))?;
    debug_assert!(
        closed.is_none()
            || matches!(
                event.record,
                FixObservationTapeRecordKind::SessionClosed { .. }
            )
    );
    let mut encoded = serde_json::to_vec(&event)
        .map_err(|error| invalid(path, format!("serialize FIX observation event: {error}")))?;
    encoded.push(b'\n');
    file.write_all(&encoded)
        .map_err(|source| io_error(path, source))?;
    sync_file(file).map_err(|source| io_error(path, source))?;
    candidate.commit_event(&event);
    *reducer = candidate;
    Ok(())
}

fn replay_bytes(
    path: &Path,
    bytes: &[u8],
    identity: &FixObservationTapeIdentity,
) -> Result<Reducer, FixObservationTapeError> {
    if !bytes.is_empty() && !bytes.ends_with(b"\n") {
        return Err(invalid(path, "truncated final line"));
    }
    let mut reducer = Reducer::new(identity.clone());
    let mut offset = 0usize;
    for (index, line_with_newline) in bytes.split_inclusive(|byte| *byte == b'\n').enumerate() {
        let line = &line_with_newline[..line_with_newline.len().saturating_sub(1)];
        offset += line_with_newline.len();
        if line.is_empty() {
            return Err(invalid(path, format!("line {} is blank", index + 1)));
        }
        let event: FixObservationTapeEvent = serde_json::from_slice(line)
            .map_err(|error| invalid(path, format!("line {}: {error}", index + 1)))?;
        let closed = reducer
            .validate_event(&event)
            .map_err(|detail| invalid(path, format!("line {}: {detail}", index + 1)))?;
        reducer.commit_event(&event);
        if let Some(mut closed) = closed {
            closed.end_offset = offset;
            match verify_commit(path, bytes, &closed)? {
                CommitState::Valid => {}
                CommitState::Missing if closed.declared_session_complete => {
                    reducer.prior_incomplete_sessions += 1
                }
                CommitState::Missing => {}
            }
            reducer.closed_sessions.push(closed);
        }
    }
    Ok(reducer)
}

pub fn verify_fix_observation_tape(
    path: impl AsRef<Path>,
    identity: &FixObservationTapeIdentity,
) -> Result<FixObservationTapeVerification, FixObservationTapeError> {
    validate_identity(identity)?;
    let path = path.as_ref();
    let bytes = std::fs::read(path).map_err(|source| io_error(path, source))?;
    let reducer = replay_bytes(path, &bytes, identity)?;
    if reducer.session_count == 0 {
        return Err(invalid(path, "tape has no session-start record"));
    }
    let active_process_instance_id = reducer
        .active
        .as_ref()
        .map(|session| session.process_instance_id.clone());
    let incomplete_sessions =
        reducer.prior_incomplete_sessions + u64::from(active_process_instance_id.is_some());
    let complete = active_process_instance_id.is_none()
        && incomplete_sessions == 0
        && reducer.closed_sessions.last().is_some_and(|closed| {
            closed.declared_session_complete && closed.declared_tape_complete
        });
    Ok(FixObservationTapeVerification {
        complete,
        record_count: reducer.next_tape_seq,
        observation_rows: reducer.total_observation_rows,
        session_count: reducer.session_count,
        incomplete_sessions,
        active_process_instance_id,
        final_record_sha256: reducer.previous_record_sha256,
    })
}

fn observation_from_envelope(
    endpoint_id: &str,
    process_instance_id: &str,
    envelope: FixObservationEnvelope,
    first_frames: &HashMap<String, u64>,
) -> Result<FixObservationRecord, String> {
    let source_sequence = envelope.sequence;
    let generation = envelope.generation;
    let raw_bytes = observation_raw(&envelope.observation).to_vec();
    let raw = FixRawEvidence::from_bytes(&raw_bytes);
    let message =
        FixMsg::parse(&raw_bytes).map_err(|error| format!("invalid FIX frame: {error}"))?;
    let fix_msg_type = std::str::from_utf8(message.msg_type)
        .map_err(|_| "FIX MsgType is not UTF-8".to_string())?
        .to_string();
    let fix_msg_seq_num = message.seq;
    let fix_sending_time = message.get_str(52).map(str::to_string);
    let receipt_id = receipt_id(
        process_instance_id,
        source_sequence,
        generation,
        &raw.bytes_sha256,
    );
    let frame_key = frame_key(endpoint_id, &raw.bytes_sha256);
    let duplicate_of_tape_seq = first_frames.get(&frame_key).copied();

    let (kind, recv_ts, ack_ts, execution, reject) = match envelope.observation {
        FixOrderObservation::Execution(observation) => {
            if message.msg_type != b"8" {
                return Err("execution observation raw frame is not MsgType 8".into());
            }
            let view = ExecReportView::from_msg(&message).ok_or_else(|| {
                "execution observation raw frame lacks required fields".to_string()
            })?;
            let execution = FixExecutionProjection {
                client_order_id: observation.report.cl_ord_id,
                order_id: observation.report.order_id,
                status: observation.report.status,
                terminal: observation.report.terminal,
                parsed_fields: observation.report.raw,
            };
            if execution.client_order_id.as_deref() != view.cl_ord_id
                || execution.order_id.as_deref() != view.order_id
                || execution.status != view.normalized_status()
                || execution.terminal != view.is_terminal()
                || execution.parsed_fields != view.to_json()
            {
                return Err("execution typed projection differs from exact raw frame".into());
            }
            (
                FixObservationKind::ExecutionReport,
                observation.recv_ts.0,
                observation.ack_ts.0,
                Some(execution),
                None,
            )
        }
        FixOrderObservation::Reject(observation) => {
            let (kind, expected_msg_type) = match observation.kind {
                FixRejectKind::Session => (FixObservationKind::SessionReject, b"3".as_slice()),
                FixRejectKind::Business => (FixObservationKind::BusinessReject, b"j".as_slice()),
            };
            if message.msg_type != expected_msg_type {
                return Err("reject typed kind differs from exact raw MsgType".into());
            }
            validate_reject_projection(&message, &observation)?;
            let projection = reject_projection(observation.clone());
            (
                kind,
                observation.recv_ts.0,
                observation.ack_ts.0,
                None,
                Some(projection),
            )
        }
    };
    if recv_ts == 0 || ack_ts < recv_ts {
        return Err("FIX observation timestamps are zero or reversed".into());
    }
    Ok(FixObservationRecord {
        source_sequence,
        generation,
        receipt_id,
        frame_key,
        duplicate_of_tape_seq,
        kind,
        fix_msg_type,
        fix_msg_seq_num,
        fix_sending_time,
        recv_monotonic_ns: recv_ts,
        ack_monotonic_ns: ack_ts,
        execution,
        reject,
        raw,
    })
}

fn validate_observation_record(
    endpoint_id: &str,
    process_instance_id: &str,
    observation: &FixObservationRecord,
    first_frames: &HashMap<String, u64>,
) -> Result<(), String> {
    if observation.source_sequence == 0 || observation.generation == 0 {
        return Err("FIX observation source sequence/generation must be non-zero".into());
    }
    if observation.recv_monotonic_ns == 0
        || observation.ack_monotonic_ns < observation.recv_monotonic_ns
    {
        return Err("FIX observation timestamps are zero or reversed".into());
    }
    let bytes = observation.raw.decode()?;
    let message = FixMsg::parse(&bytes).map_err(|error| format!("invalid FIX frame: {error}"))?;
    if observation.fix_msg_type.as_bytes() != message.msg_type
        || observation.fix_msg_seq_num != message.seq
        || observation.fix_sending_time.as_deref() != message.get_str(52)
    {
        return Err("FIX header projection differs from exact raw frame".into());
    }
    let expected_receipt = receipt_id(
        process_instance_id,
        observation.source_sequence,
        observation.generation,
        &observation.raw.bytes_sha256,
    );
    let expected_frame_key = frame_key(endpoint_id, &observation.raw.bytes_sha256);
    if observation.receipt_id != expected_receipt || observation.frame_key != expected_frame_key {
        return Err("FIX receipt or frame identity hash mismatch".into());
    }
    if observation.duplicate_of_tape_seq != first_frames.get(&expected_frame_key).copied() {
        return Err("FIX duplicate-frame pointer does not identify the first exact frame".into());
    }
    match observation.kind {
        FixObservationKind::ExecutionReport => {
            if message.msg_type != b"8" || observation.reject.is_some() {
                return Err("execution observation has inconsistent kind/projection".into());
            }
            let execution = observation
                .execution
                .as_ref()
                .ok_or_else(|| "execution observation lacks projection".to_string())?;
            let view = ExecReportView::from_msg(&message)
                .ok_or_else(|| "execution raw frame lacks required fields".to_string())?;
            if execution.client_order_id.as_deref() != view.cl_ord_id
                || execution.order_id.as_deref() != view.order_id
                || execution.status != view.normalized_status()
                || execution.terminal != view.is_terminal()
                || execution.parsed_fields != view.to_json()
            {
                return Err("execution projection differs from exact raw frame".into());
            }
        }
        FixObservationKind::SessionReject | FixObservationKind::BusinessReject => {
            if observation.execution.is_some() {
                return Err("reject observation carries an execution projection".into());
            }
            let reject = observation
                .reject
                .as_ref()
                .ok_or_else(|| "reject observation lacks projection".to_string())?;
            let expected_type = match observation.kind {
                FixObservationKind::SessionReject => b"3".as_slice(),
                FixObservationKind::BusinessReject => b"j".as_slice(),
                FixObservationKind::ExecutionReport => unreachable!(),
            };
            if message.msg_type != expected_type {
                return Err("reject observation has inconsistent MsgType".into());
            }
            validate_reject_fields(&message, observation.kind, reject)?;
        }
    }
    Ok(())
}

fn reject_projection(observation: FixRejectObservation) -> FixRejectProjection {
    FixRejectProjection {
        client_order_id: observation.client_order_id,
        ref_seq: observation.ref_seq,
        ref_msg_type: observation.ref_msg_type,
        business_ref_id: observation.business_ref_id,
        reason: observation.reason,
        text: observation.text,
    }
}

fn validate_reject_projection(
    message: &FixMsg<'_>,
    observation: &FixRejectObservation,
) -> Result<(), String> {
    let projection = FixRejectProjection {
        client_order_id: observation.client_order_id.clone(),
        ref_seq: observation.ref_seq,
        ref_msg_type: observation.ref_msg_type.clone(),
        business_ref_id: observation.business_ref_id.clone(),
        reason: observation.reason.clone(),
        text: observation.text.clone(),
    };
    let kind = match observation.kind {
        FixRejectKind::Session => FixObservationKind::SessionReject,
        FixRejectKind::Business => FixObservationKind::BusinessReject,
    };
    validate_reject_fields(message, kind, &projection)
}

fn validate_reject_fields(
    message: &FixMsg<'_>,
    kind: FixObservationKind,
    projection: &FixRejectProjection,
) -> Result<(), String> {
    let reason_tag = match kind {
        FixObservationKind::SessionReject => 373,
        FixObservationKind::BusinessReject => 380,
        FixObservationKind::ExecutionReport => {
            return Err("execution kind passed to reject validator".into())
        }
    };
    if projection.ref_seq != message.get_u64(45)
        || projection.ref_msg_type.as_deref() != message.get_str(372)
        || projection.business_ref_id.as_deref() != message.get_str(379)
        || projection.reason.as_deref() != message.get_str(reason_tag)
        || projection.text.as_deref() != message.get_str(58)
    {
        return Err("reject projection differs from exact raw frame".into());
    }
    Ok(())
}

fn observation_raw(observation: &FixOrderObservation) -> &[u8] {
    match observation {
        FixOrderObservation::Execution(observation) => &observation.raw,
        FixOrderObservation::Reject(observation) => &observation.raw,
    }
}

fn validate_event_envelope(
    reducer: &Reducer,
    event: &FixObservationTapeEvent,
) -> Result<(), String> {
    if event.schema_version != FIX_OBSERVATION_TAPE_SCHEMA_VERSION {
        return Err(format!(
            "unsupported schema version {}",
            event.schema_version
        ));
    }
    if event.tape_seq != reducer.next_tape_seq {
        return Err(format!(
            "expected tape_seq {}, got {}",
            reducer.next_tape_seq, event.tape_seq
        ));
    }
    if event.wall_unix_ns == 0 || event.monotonic_ns == 0 {
        return Err("event clocks must be non-zero".into());
    }
    validate_process_instance_id(&event.process_instance_id)?;
    if event.run_id != reducer.identity.run_id
        || event.event_id != reducer.identity.event_id
        || event.kalshi_env != reducer.identity.kalshi_env
        || event.account_fingerprint != reducer.identity.account_fingerprint
        || event.plan_sha256 != reducer.identity.plan_sha256
        || event.endpoint_id != reducer.identity.endpoint_id
    {
        return Err("immutable tape identity mismatch".into());
    }
    if event.previous_process_instance_id != reducer.previous_process_instance_id
        || event.previous_record_sha256 != reducer.previous_record_sha256
    {
        return Err("previous-record chain mismatch".into());
    }
    if !is_lower_hex(&event.record_sha256, 64) || event.record_sha256 != record_sha256(event)? {
        return Err("record SHA-256 mismatch".into());
    }
    Ok(())
}

fn validate_options(
    identity: &FixObservationTapeIdentity,
    options: &FixObservationTapeOptions,
) -> Result<(), FixObservationTapeError> {
    validate_identity(identity)?;
    if options.queue_capacity == 0 || options.producer_quiescence_timeout.is_zero() {
        return Err(FixObservationTapeError::Unavailable(
            "FIX observation tape capacity and quiescence timeout must be positive".into(),
        ));
    }
    Ok(())
}

fn validate_identity(identity: &FixObservationTapeIdentity) -> Result<(), FixObservationTapeError> {
    for (name, value) in [
        ("run_id", identity.run_id.as_str()),
        ("event_id", identity.event_id.as_str()),
        ("kalshi_env", identity.kalshi_env.as_str()),
        ("account_fingerprint", identity.account_fingerprint.as_str()),
        ("endpoint_id", identity.endpoint_id.as_str()),
    ] {
        validate_nonempty(name, value).map_err(FixObservationTapeError::Unavailable)?;
        if value.chars().any(char::is_control) {
            return Err(FixObservationTapeError::Unavailable(format!(
                "{name} contains control characters"
            )));
        }
    }
    if !is_lower_hex(&identity.plan_sha256, 64) {
        return Err(FixObservationTapeError::Unavailable(
            "plan_sha256 must be 64 lowercase hexadecimal characters".into(),
        ));
    }
    Ok(())
}

fn validate_process_instance_id(value: &str) -> Result<(), String> {
    validate_nonempty("process_instance_id", value)?;
    if !value
        .bytes()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    {
        return Err("process_instance_id contains unsafe characters".into());
    }
    Ok(())
}

fn validate_nonempty(name: &str, value: &str) -> Result<(), String> {
    if value.trim().is_empty() {
        Err(format!("{name} must be non-empty"))
    } else {
        Ok(())
    }
}

fn validate_raw_evidence(raw: &FixRawEvidence) -> Result<Vec<u8>, String> {
    if raw.media_type != FIX_OBSERVATION_RAW_MEDIA_TYPE {
        return Err("unexpected FIX raw evidence media type".into());
    }
    if !is_lower_hex(&raw.bytes_sha256, 64) {
        return Err("raw bytes SHA-256 is not lowercase hexadecimal".into());
    }
    let decoded = base64::engine::general_purpose::STANDARD
        .decode(&raw.bytes_base64)
        .map_err(|error| format!("invalid raw base64: {error}"))?;
    if base64::engine::general_purpose::STANDARD.encode(&decoded) != raw.bytes_base64 {
        return Err("raw base64 is not canonical standard base64".into());
    }
    if raw.bytes_len != decoded.len() as u64 || raw.bytes_sha256 != sha256_hex(&decoded) {
        return Err("raw byte length/fingerprint mismatch".into());
    }
    Ok(decoded)
}

fn receipt_id(
    process_instance_id: &str,
    source_sequence: u64,
    generation: u64,
    raw_sha256: &str,
) -> String {
    hash_json(&serde_json::json!([
        process_instance_id,
        source_sequence,
        generation,
        raw_sha256
    ]))
}

fn frame_key(endpoint_id: &str, raw_sha256: &str) -> String {
    hash_json(&serde_json::json!([endpoint_id, raw_sha256]))
}

fn hash_json(value: &Value) -> String {
    let encoded = serde_json::to_vec(value).expect("hash material is serializable");
    sha256_hex(&encoded)
}

fn record_sha256(event: &FixObservationTapeEvent) -> Result<String, String> {
    let mut canonical = serde_json::to_value(event)
        .map_err(|error| format!("serialize record hash material: {error}"))?;
    canonical
        .as_object_mut()
        .ok_or_else(|| "record hash material is not an object".to_string())?
        .remove("record_sha256");
    serde_json::to_vec(&canonical)
        .map(|encoded| sha256_hex(&encoded))
        .map_err(|error| format!("serialize canonical record hash material: {error}"))
}

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn new_process_instance_id() -> String {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    format!(
        "{}-{}-{}",
        std::process::id(),
        wall_unix_ns(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    )
}

fn wall_unix_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos() as u64
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CommitState {
    Valid,
    Missing,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct CommitReceipt {
    #[serde(rename = "type")]
    kind: String,
    schema_version: u32,
    process_instance_id: String,
    close_tape_seq: u64,
    close_record_sha256: String,
    tape_bytes: u64,
    tape_sha256: String,
    session_complete: bool,
    tape_complete: bool,
    durable: bool,
}

fn commit_path(path: &Path, process_instance_id: &str) -> PathBuf {
    let mut name = path.as_os_str().to_os_string();
    name.push(format!(".commit.{process_instance_id}.json"));
    PathBuf::from(name)
}

fn pending_commit_path(path: &Path, process_instance_id: &str) -> PathBuf {
    let mut name = path.as_os_str().to_os_string();
    name.push(format!(".commit.{process_instance_id}.pending"));
    PathBuf::from(name)
}

fn publish_commit(
    path: &Path,
    process_instance_id: &str,
    close_tape_seq: u64,
    close_record_sha256: &str,
    session_complete: bool,
    tape_complete: bool,
) -> Result<PathBuf, FixObservationTapeError> {
    let bytes = std::fs::read(path).map_err(|source| io_error(path, source))?;
    let final_path = commit_path(path, process_instance_id);
    let pending_path = pending_commit_path(path, process_instance_id);
    if final_path.exists() || pending_path.exists() {
        return Err(invalid(path, "session commit path already exists"));
    }
    let receipt = CommitReceipt {
        kind: "fix_observation_tape_commit".to_string(),
        schema_version: FIX_OBSERVATION_TAPE_SCHEMA_VERSION,
        process_instance_id: process_instance_id.to_string(),
        close_tape_seq,
        close_record_sha256: close_record_sha256.to_string(),
        tape_bytes: bytes.len() as u64,
        tape_sha256: sha256_hex(&bytes),
        session_complete,
        tape_complete,
        durable: true,
    };
    let mut pending = BufWriter::new(
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&pending_path)
            .map_err(|source| io_error(&pending_path, source))?,
    );
    serde_json::to_writer(&mut pending, &receipt)
        .map_err(|error| invalid(&pending_path, format!("serialize commit: {error}")))?;
    pending
        .write_all(b"\n")
        .map_err(|source| io_error(&pending_path, source))?;
    pending
        .flush()
        .map_err(|source| io_error(&pending_path, source))?;
    sync_file(pending.get_ref()).map_err(|source| io_error(&pending_path, source))?;
    drop(pending);
    std::fs::hard_link(&pending_path, &final_path)
        .map_err(|source| io_error(&final_path, source))?;
    sync_parent_dir(&final_path).map_err(|source| io_error(&final_path, source))?;
    if let Err(error) = std::fs::remove_file(&pending_path) {
        tracing::warn!(path = %pending_path.display(), %error,
            "FIX observation commit published but pending link remains");
    }
    Ok(final_path)
}

fn verify_commit(
    path: &Path,
    bytes: &[u8],
    closed: &ClosedSession,
) -> Result<CommitState, FixObservationTapeError> {
    let receipt_path = commit_path(path, &closed.process_instance_id);
    let text = match std::fs::read_to_string(&receipt_path) {
        Ok(text) => text,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(CommitState::Missing),
        Err(source) => return Err(io_error(&receipt_path, source)),
    };
    let mut lines = text.lines();
    let line = lines
        .next()
        .ok_or_else(|| invalid(&receipt_path, "empty session commit receipt"))?;
    if lines.next().is_some() || line.trim().is_empty() {
        return Err(invalid(
            &receipt_path,
            "session commit receipt must contain exactly one nonblank row",
        ));
    }
    let receipt: CommitReceipt = serde_json::from_str(line)
        .map_err(|error| invalid(&receipt_path, format!("invalid commit JSON: {error}")))?;
    let prefix = bytes
        .get(..closed.end_offset)
        .ok_or_else(|| invalid(path, "session commit offset exceeds tape"))?;
    if receipt.kind != "fix_observation_tape_commit"
        || receipt.schema_version != FIX_OBSERVATION_TAPE_SCHEMA_VERSION
        || receipt.process_instance_id != closed.process_instance_id
        || receipt.close_tape_seq != closed.close_tape_seq
        || receipt.close_record_sha256 != closed.close_record_sha256
        || receipt.tape_bytes != prefix.len() as u64
        || receipt.tape_sha256 != sha256_hex(prefix)
        || receipt.session_complete != closed.declared_session_complete
        || receipt.tape_complete != closed.declared_tape_complete
        || !receipt.durable
    {
        return Err(invalid(
            &receipt_path,
            "session commit does not bind the exact tape prefix and close record",
        ));
    }
    Ok(CommitState::Valid)
}

fn invalid(path: &Path, detail: impl Into<String>) -> FixObservationTapeError {
    FixObservationTapeError::Invalid {
        path: path.to_path_buf(),
        detail: detail.into(),
    }
}

fn io_error(path: &Path, source: io::Error) -> FixObservationTapeError {
    FixObservationTapeError::Io {
        path: path.to_path_buf(),
        source,
    }
}

fn sync_file(file: &File) -> io::Result<()> {
    file.sync_all()?;
    #[cfg(target_os = "macos")]
    {
        let rc = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_FULLFSYNC) };
        if rc == -1 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}

fn sync_parent_dir(path: &Path) -> io::Result<()> {
    File::open(path.parent().unwrap_or_else(|| Path::new(".")))?.sync_all()
}

#[cfg(unix)]
fn lock_exclusive_nonblocking(file: &File, path: &Path) -> Result<(), FixObservationTapeError> {
    let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc == 0 {
        Ok(())
    } else {
        Err(io_error(path, io::Error::last_os_error()))
    }
}

#[cfg(not(unix))]
fn lock_exclusive_nonblocking(_file: &File, path: &Path) -> Result<(), FixObservationTapeError> {
    Err(io_error(
        path,
        io::Error::new(
            io::ErrorKind::Unsupported,
            "FIX observation tape requires flock",
        ),
    ))
}
