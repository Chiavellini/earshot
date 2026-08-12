//! Crash-aware, run-scoped evidence for the exact audio stream accepted by
//! the detector path.
//!
//! The raw binary tape is authoritative.  A bounded, non-blocking tap clones
//! the post-preprocessor [`AudioFrame`] immediately before the frame is
//! published into the preprocessor -> chunker ring.  The caller MUST publish
//! only after [`AudioEvidenceTap::try_record_accepted`] succeeds.  This makes
//! the bank's accepted-frame count equal to the detector-consumed stream by
//! construction; a full or failed evidence queue is a run-fatal condition,
//! not a best-effort logging loss.
//!
//! Completion is two phase.  The raw tape and JSONL frame sidecar end in a
//! durable pending-close record, playable per-source/rate/sequence WAV
//! segments and a `complete=false` manifest are materialized, and only then is
//! a byte-bound commit receipt atomically published.  Crash remnants are
//! deliberately retained, but no partial artifact is ever a valid commit.

use std::collections::{BTreeMap, HashSet};
use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufReader, BufWriter, Read, Seek, Write};
#[cfg(unix)]
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rsa::sha2::{Digest, Sha256};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;

use crate::clock::MonotonicNs;
use crate::types::{AudioFrame, SourceId};

pub const AUDIO_EVIDENCE_SCHEMA_VERSION: u32 = 1;

const RAW_MAGIC: &[u8; 8] = b"EARAUD01";
const FRAME_MAGIC: &[u8; 4] = b"FRM1";
const SEAL_MAGIC: &[u8; 4] = b"SEA1";
const MAX_JSON_BYTES: usize = 1024 * 1024;
const MAX_PCM_BYTES: usize = 32 * 1024 * 1024;
const WRITER_POLL: Duration = Duration::from_millis(10);

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AudioEvidenceIdentity {
    pub run_id: String,
    pub event_id: String,
    pub plan_sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AudioEvidencePaths {
    /// Authoritative length-delimited binary frame tape.
    pub raw: PathBuf,
    /// Human-readable frame metadata sidecar.  PCM stays in `raw`.
    pub frames: PathBuf,
    /// Pending manifest.  It always says `complete=false`; the receipt commits.
    pub manifest: PathBuf,
    /// Atomically published, byte-bound completion receipt.
    pub commit: PathBuf,
    /// Process lease, retained as a harmless empty file between runs.
    pub lock: PathBuf,
    /// Exclusive directory containing per-source/discontinuity WAV segments.
    pub wav_dir: PathBuf,
}

#[derive(Debug, Clone)]
pub struct AudioEvidenceConfig {
    pub identity: AudioEvidenceIdentity,
    pub paths: AudioEvidencePaths,
    pub queue_capacity: usize,
    pub sync_every_frames: u64,
    pub producer_quiescence_timeout: Duration,
    pub writer_join_timeout: Duration,
    /// Optional stable names for numeric source/leg ids.  Numeric ids remain
    /// authoritative and filenames never contain labels.
    pub source_labels: BTreeMap<u16, String>,
    /// Deterministic fault hooks used by the focused evidence tests.
    #[cfg(test)]
    #[doc(hidden)]
    pub test_writer_start_delay: Option<Duration>,
    #[cfg(test)]
    #[doc(hidden)]
    pub test_fail_after_frames: Option<u64>,
}

impl AudioEvidenceConfig {
    pub fn new(identity: AudioEvidenceIdentity, paths: AudioEvidencePaths) -> Self {
        Self {
            identity,
            paths,
            queue_capacity: 1024,
            sync_every_frames: 50,
            producer_quiescence_timeout: Duration::from_secs(5),
            writer_join_timeout: Duration::from_secs(10),
            source_labels: BTreeMap::new(),
            #[cfg(test)]
            test_writer_start_delay: None,
            #[cfg(test)]
            test_fail_after_frames: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactFingerprint {
    pub bytes: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WavSegmentManifest {
    pub source: u16,
    pub source_label: Option<String>,
    pub segment: u32,
    pub sample_rate_hz: u32,
    pub first_seq: u64,
    pub last_seq: u64,
    pub first_capture_ts_ns: u64,
    pub last_capture_ts_ns: u64,
    pub first_preproc_ts_ns: u64,
    pub last_preproc_ts_ns: u64,
    pub frame_count: u64,
    pub sample_count: u64,
    pub start_reasons: Vec<String>,
    pub pcm_sha256: String,
    pub path: PathBuf,
    pub wav: ArtifactFingerprint,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AudioEvidenceManifest {
    pub schema_version: u32,
    pub identity: AudioEvidenceIdentity,
    pub status: String,
    /// Always false.  Only a verified `AudioEvidenceCommit` means complete.
    pub complete: bool,
    pub commit_required: bool,
    pub close_reason: String,
    pub frame_count: u64,
    pub sample_count: u64,
    pub expected_published_frames: u64,
    pub bank_accepted_frames: u64,
    pub writer_frames: u64,
    pub dropped_frames: u64,
    pub closed_attempts: u64,
    pub invalid_attempts: u64,
    pub producer_quiesced: bool,
    pub raw_path: PathBuf,
    pub raw: ArtifactFingerprint,
    pub frames_path: PathBuf,
    pub frames: ArtifactFingerprint,
    pub source_labels: BTreeMap<u16, String>,
    pub wav_dir: PathBuf,
    pub wav_segments: Vec<WavSegmentManifest>,
    pub start_wall_ns: u64,
    pub start_mono_ns: u64,
    pub end_wall_ns: u64,
    pub end_mono_ns: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AudioEvidenceCommit {
    pub schema_version: u32,
    pub identity: AudioEvidenceIdentity,
    pub status: String,
    pub complete: bool,
    pub durable: bool,
    pub manifest_path: PathBuf,
    pub manifest: ArtifactFingerprint,
    pub raw_path: PathBuf,
    pub raw: ArtifactFingerprint,
    pub frames_path: PathBuf,
    pub frames: ArtifactFingerprint,
    pub wav_dir: PathBuf,
    pub wav_segments: Vec<WavSegmentManifest>,
    pub frame_count: u64,
    pub sample_count: u64,
    pub close_wall_ns: u64,
    pub close_mono_ns: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AudioEvidencePartial {
    pub paths: AudioEvidencePaths,
    pub reason: String,
    pub accepted_frames: u64,
    pub written_frames: u64,
    pub dropped_frames: u64,
    pub closed_attempts: u64,
    pub invalid_attempts: u64,
    pub producer_quiesced: bool,
    pub manifest_written: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AudioEvidenceHealth {
    pub accepting: bool,
    pub poisoned: bool,
    pub accepted_frames: u64,
    pub written_frames: u64,
    pub dropped_frames: u64,
    pub closed_attempts: u64,
    pub invalid_attempts: u64,
    pub active_taps: usize,
    pub in_flight_taps: usize,
    pub writer_error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AudioEvidenceFrame {
    pub pcm: Arc<[i16]>,
    pub sample_rate_hz: u32,
    pub capture_ts: MonotonicNs,
    pub preproc_ts: MonotonicNs,
    pub source: SourceId,
    pub seq: u64,
    pub record_index: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AudioEvidenceRawInspection {
    pub identity: AudioEvidenceIdentity,
    pub frame_count: u64,
    pub sample_count: u64,
    pub sealed: bool,
    pub candidate_complete: bool,
}

#[derive(Debug, Error)]
pub enum AudioEvidenceTapError {
    #[error("audio evidence queue is full")]
    Full,
    #[error("audio evidence tap is closed")]
    Closed,
    #[error("invalid audio evidence frame: {0}")]
    InvalidFrame(String),
    #[error("audio evidence writer failed: {0}")]
    WriterFailed(String),
}

#[derive(Debug, Error)]
pub enum AudioEvidenceError {
    #[error("invalid audio evidence configuration: {0}")]
    InvalidConfig(String),
    #[error("audio evidence artifact already exists: {path}")]
    ArtifactExists { path: PathBuf },
    #[error("audio evidence lease is busy at {path}: {source}")]
    Busy { path: PathBuf, source: io::Error },
    #[error("audio evidence I/O failed at {path}: {source}")]
    Io { path: PathBuf, source: io::Error },
    #[error("invalid audio evidence at {path}: {detail}")]
    InvalidEvidence { path: PathBuf, detail: String },
    #[error("audio evidence writer failed: {detail}")]
    WriterFailed { detail: String },
    #[error("audio evidence writer did not terminate within {timeout:?}")]
    WriterTimeout { timeout: Duration },
    #[error("audio evidence is partial: {partial:?}")]
    Incomplete { partial: Box<AudioEvidencePartial> },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct RawHeader {
    schema_version: u32,
    identity: AudioEvidenceIdentity,
    format: String,
    start_wall_ns: u64,
    start_mono_ns: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct FrameMeta {
    schema_version: u32,
    record_index: u64,
    source: u16,
    seq: u64,
    sample_rate_hz: u32,
    capture_ts_ns: u64,
    preproc_ts_ns: u64,
    sample_count: u64,
    pcm_sha256: String,
    previous_record_sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct SealPayload {
    schema_version: u32,
    identity: AudioEvidenceIdentity,
    status: String,
    complete: bool,
    commit_required: bool,
    requested_complete: bool,
    candidate_complete: bool,
    reason: String,
    expected_published_frames: u64,
    bank_accepted_frames: u64,
    writer_frames: u64,
    sample_count: u64,
    dropped_frames: u64,
    closed_attempts: u64,
    invalid_attempts: u64,
    producer_quiesced: bool,
    active_taps: usize,
    in_flight_taps: usize,
    previous_record_sha256: String,
    start_wall_ns: u64,
    start_mono_ns: u64,
    end_wall_ns: u64,
    end_mono_ns: u64,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
struct SidecarSession {
    t: String,
    schema_version: u32,
    identity: AudioEvidenceIdentity,
    raw_header_sha256: String,
    start_wall_ns: u64,
    start_mono_ns: u64,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
struct SidecarFrame {
    t: String,
    #[serde(flatten)]
    meta: FrameMeta,
    raw_offset: u64,
    raw_record_bytes: u64,
    record_sha256: String,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
struct SidecarClose {
    t: String,
    #[serde(flatten)]
    seal: SealPayload,
    seal_sha256: String,
}

struct OwnedFrame {
    pcm: Arc<[i16]>,
    sample_rate_hz: u32,
    capture_ts_ns: u64,
    preproc_ts_ns: u64,
    source: u16,
    seq: u64,
}

struct SealRequest {
    requested_complete: bool,
    reason: String,
    expected_published_frames: u64,
    bank_accepted_frames: u64,
    dropped_frames: u64,
    closed_attempts: u64,
    invalid_attempts: u64,
    producer_quiesced: bool,
    active_taps: usize,
    in_flight_taps: usize,
}

enum WriterCommand {
    Frame(OwnedFrame),
    Seal(SealRequest),
}

struct SharedState {
    accepting: AtomicBool,
    seal_pending: AtomicBool,
    accepted_frames: AtomicU64,
    written_frames: AtomicU64,
    dropped_frames: AtomicU64,
    closed_attempts: AtomicU64,
    invalid_attempts: AtomicU64,
    active_taps: AtomicUsize,
    in_flight_taps: AtomicUsize,
    writer_failed: AtomicBool,
    writer_error: Mutex<Option<String>>,
}

impl SharedState {
    fn new() -> Self {
        Self {
            accepting: AtomicBool::new(true),
            seal_pending: AtomicBool::new(false),
            accepted_frames: AtomicU64::new(0),
            written_frames: AtomicU64::new(0),
            dropped_frames: AtomicU64::new(0),
            closed_attempts: AtomicU64::new(0),
            invalid_attempts: AtomicU64::new(0),
            active_taps: AtomicUsize::new(1),
            in_flight_taps: AtomicUsize::new(0),
            writer_failed: AtomicBool::new(false),
            writer_error: Mutex::new(None),
        }
    }

    fn fail_writer(&self, detail: impl Into<String>) {
        let detail = detail.into();
        self.writer_failed.store(true, Ordering::Release);
        if let Ok(mut slot) = self.writer_error.lock() {
            if slot.is_none() {
                *slot = Some(detail);
            }
        }
    }

    fn writer_error(&self) -> Option<String> {
        self.writer_error.lock().ok().and_then(|slot| slot.clone())
    }

    fn health(&self) -> AudioEvidenceHealth {
        let writer_error = self.writer_error();
        let dropped_frames = self.dropped_frames.load(Ordering::Acquire);
        let closed_attempts = self.closed_attempts.load(Ordering::Acquire);
        let invalid_attempts = self.invalid_attempts.load(Ordering::Acquire);
        AudioEvidenceHealth {
            accepting: self.accepting.load(Ordering::Acquire),
            poisoned: self.writer_failed.load(Ordering::Acquire)
                || dropped_frames != 0
                || closed_attempts != 0
                || invalid_attempts != 0,
            accepted_frames: self.accepted_frames.load(Ordering::Acquire),
            written_frames: self.written_frames.load(Ordering::Acquire),
            dropped_frames,
            closed_attempts,
            invalid_attempts,
            active_taps: self.active_taps.load(Ordering::Acquire),
            in_flight_taps: self.in_flight_taps.load(Ordering::Acquire),
            writer_error,
        }
    }
}

pub struct AudioEvidenceTap {
    tx: mpsc::SyncSender<WriterCommand>,
    state: Arc<SharedState>,
}

impl Clone for AudioEvidenceTap {
    fn clone(&self) -> Self {
        self.state.active_taps.fetch_add(1, Ordering::AcqRel);
        Self {
            tx: self.tx.clone(),
            state: self.state.clone(),
        }
    }
}

impl Drop for AudioEvidenceTap {
    fn drop(&mut self) {
        self.state.active_taps.fetch_sub(1, Ordering::AcqRel);
    }
}

struct InFlight<'a>(&'a SharedState);

impl Drop for InFlight<'_> {
    fn drop(&mut self) {
        self.0.in_flight_taps.fetch_sub(1, Ordering::AcqRel);
    }
}

impl AudioEvidenceTap {
    /// Non-blocking evidence admission.  The caller may publish `frame` to the
    /// detector path only after this returns `Ok(())`.
    pub fn try_record_accepted(&self, frame: &AudioFrame) -> Result<(), AudioEvidenceTapError> {
        self.state.in_flight_taps.fetch_add(1, Ordering::AcqRel);
        let _in_flight = InFlight(&self.state);

        if !self.state.accepting.load(Ordering::Acquire) {
            self.state.closed_attempts.fetch_add(1, Ordering::AcqRel);
            return Err(AudioEvidenceTapError::Closed);
        }
        if self.state.writer_failed.load(Ordering::Acquire) {
            return Err(AudioEvidenceTapError::WriterFailed(
                self.state
                    .writer_error()
                    .unwrap_or_else(|| "unknown writer failure".into()),
            ));
        }
        if let Err(detail) = validate_frame(
            frame.sample_rate_hz,
            frame.capture_ts.0,
            frame.preproc_ts.0,
            frame.pcm.len(),
        ) {
            self.state.invalid_attempts.fetch_add(1, Ordering::AcqRel);
            return Err(AudioEvidenceTapError::InvalidFrame(detail));
        }

        let owned = OwnedFrame {
            pcm: frame.pcm.clone(),
            sample_rate_hz: frame.sample_rate_hz,
            capture_ts_ns: frame.capture_ts.0,
            preproc_ts_ns: frame.preproc_ts.0,
            source: frame.source.0,
            seq: frame.seq,
        };
        match self.tx.try_send(WriterCommand::Frame(owned)) {
            Ok(()) => {
                self.state.accepted_frames.fetch_add(1, Ordering::AcqRel);
                Ok(())
            }
            Err(mpsc::TrySendError::Full(_)) => {
                self.state.dropped_frames.fetch_add(1, Ordering::AcqRel);
                Err(AudioEvidenceTapError::Full)
            }
            Err(mpsc::TrySendError::Disconnected(_)) => {
                self.state.closed_attempts.fetch_add(1, Ordering::AcqRel);
                self.state
                    .fail_writer("writer command channel disconnected");
                Err(AudioEvidenceTapError::WriterFailed(
                    "writer command channel disconnected".into(),
                ))
            }
        }
    }

    pub fn health(&self) -> AudioEvidenceHealth {
        self.state.health()
    }
}

pub struct AudioEvidenceBank {
    config: AudioEvidenceConfig,
    state: Arc<SharedState>,
    writer_tx: Option<mpsc::SyncSender<WriterCommand>>,
    writer_result_rx: mpsc::Receiver<Result<SealPayload, String>>,
    writer_handle: Option<JoinHandle<()>>,
    finalized: bool,
}

impl AudioEvidenceBank {
    pub fn start_new(
        config: AudioEvidenceConfig,
    ) -> Result<(Self, AudioEvidenceTap), AudioEvidenceError> {
        validate_config(&config)?;
        prepare_parent_directories(&config.paths)?;

        let lock_file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&config.paths.lock)
            .map_err(|source| AudioEvidenceError::Io {
                path: config.paths.lock.clone(),
                source,
            })?;
        lock_exclusive_nonblocking(&lock_file, &config.paths.lock)?;

        for path in artifact_creation_paths(&config.paths) {
            if path.exists() {
                return Err(AudioEvidenceError::ArtifactExists { path });
            }
        }

        let mut raw_file = create_new_file(&config.paths.raw)?;
        let mut frames_file = create_new_file(&config.paths.frames)?;
        let header = RawHeader {
            schema_version: AUDIO_EVIDENCE_SCHEMA_VERSION,
            identity: config.identity.clone(),
            format: "length_delimited_pcm_s16le_mono".into(),
            start_wall_ns: wall_ns_now(),
            start_mono_ns: MonotonicNs::now().0,
        };
        let header_json = serde_json::to_vec(&header).map_err(|error| {
            AudioEvidenceError::InvalidConfig(format!("header serialization failed: {error}"))
        })?;
        let header_hash = write_raw_header(&mut raw_file, &config.paths.raw, &header_json)?;
        write_json_line(
            &mut frames_file,
            &config.paths.frames,
            &SidecarSession {
                t: "session".into(),
                schema_version: AUDIO_EVIDENCE_SCHEMA_VERSION,
                identity: config.identity.clone(),
                raw_header_sha256: hex(&header_hash),
                start_wall_ns: header.start_wall_ns,
                start_mono_ns: header.start_mono_ns,
            },
        )?;
        durable_sync(&raw_file, &config.paths.raw)?;
        durable_sync(&frames_file, &config.paths.frames)?;
        sync_parent_dir(&config.paths.raw)?;
        if config.paths.frames.parent() != config.paths.raw.parent() {
            sync_parent_dir(&config.paths.frames)?;
        }

        let state = Arc::new(SharedState::new());
        let (writer_tx, writer_rx) = mpsc::sync_channel(config.queue_capacity);
        let (result_tx, writer_result_rx) = mpsc::sync_channel(1);
        let writer_state = state.clone();
        let writer_config = config.clone();
        let writer_header = header.clone();
        let writer_handle = std::thread::Builder::new()
            .name("audio-evidence".into())
            .spawn(move || {
                let result = writer_loop(
                    raw_file,
                    frames_file,
                    lock_file,
                    writer_rx,
                    writer_state.clone(),
                    writer_config,
                    writer_header,
                    header_hash,
                );
                if let Err(detail) = &result {
                    writer_state.fail_writer(detail.clone());
                }
                let _ = result_tx.send(result);
            })
            .map_err(|source| AudioEvidenceError::Io {
                path: config.paths.raw.clone(),
                source,
            })?;

        let tap = AudioEvidenceTap {
            tx: writer_tx.clone(),
            state: state.clone(),
        };
        Ok((
            Self {
                config,
                state,
                writer_tx: Some(writer_tx),
                writer_result_rx,
                writer_handle: Some(writer_handle),
                finalized: false,
            },
            tap,
        ))
    }

    pub fn health(&self) -> AudioEvidenceHealth {
        self.state.health()
    }

    pub fn finish(
        mut self,
        expected_published_frames: u64,
    ) -> Result<AudioEvidenceCommit, AudioEvidenceError> {
        let finalized = self.finalize_writer(expected_published_frames, true, "completed")?;
        let manifest = self.materialize_manifest(&finalized)?;
        if !finalized.candidate_complete {
            let partial = self.partial_from(&finalized, true);
            return Err(AudioEvidenceError::Incomplete {
                partial: Box::new(partial),
            });
        }
        let commit = self.publish_commit(&manifest)?;
        // Verify through the same public door consumers use.  At this point
        // the receipt is the final atomic publication and should be valid by
        // construction; a mismatch is therefore a loud implementation fault.
        let verified =
            verify_audio_evidence(&self.config.paths.manifest, &self.config.paths.commit)?;
        if verified != commit {
            return Err(AudioEvidenceError::InvalidEvidence {
                path: self.config.paths.commit.clone(),
                detail: "published receipt did not round-trip through verifier".into(),
            });
        }
        Ok(commit)
    }

    pub fn abort(mut self, reason: &str) -> Result<AudioEvidencePartial, AudioEvidenceError> {
        if reason.trim().is_empty() || reason.contains('\n') || reason.contains('\r') {
            return Err(AudioEvidenceError::InvalidConfig(
                "abort reason must be non-empty and single-line".into(),
            ));
        }
        let expected = self.state.accepted_frames.load(Ordering::Acquire);
        let finalized = self.finalize_writer(expected, false, reason)?;
        let _ = self.materialize_manifest(&finalized)?;
        Ok(self.partial_from(&finalized, true))
    }

    fn finalize_writer(
        &mut self,
        expected_published_frames: u64,
        requested_complete: bool,
        reason: &str,
    ) -> Result<SealPayload, AudioEvidenceError> {
        self.state.seal_pending.store(true, Ordering::Release);
        self.state.accepting.store(false, Ordering::Release);

        let quiescence_deadline = Instant::now() + self.config.producer_quiescence_timeout;
        loop {
            let active = self.state.active_taps.load(Ordering::Acquire);
            let in_flight = self.state.in_flight_taps.load(Ordering::Acquire);
            if active == 0 && in_flight == 0 {
                break;
            }
            if Instant::now() >= quiescence_deadline {
                break;
            }
            std::thread::sleep(Duration::from_millis(1));
        }

        let active_taps = self.state.active_taps.load(Ordering::Acquire);
        let in_flight_taps = self.state.in_flight_taps.load(Ordering::Acquire);
        let request = SealRequest {
            requested_complete,
            reason: reason.to_owned(),
            expected_published_frames,
            bank_accepted_frames: self.state.accepted_frames.load(Ordering::Acquire),
            dropped_frames: self.state.dropped_frames.load(Ordering::Acquire),
            closed_attempts: self.state.closed_attempts.load(Ordering::Acquire),
            invalid_attempts: self.state.invalid_attempts.load(Ordering::Acquire),
            producer_quiesced: active_taps == 0 && in_flight_taps == 0,
            active_taps,
            in_flight_taps,
        };

        let send_deadline = Instant::now() + self.config.writer_join_timeout;
        let mut command = WriterCommand::Seal(request);
        loop {
            let Some(tx) = self.writer_tx.as_ref() else {
                return Err(AudioEvidenceError::WriterFailed {
                    detail: "writer control sender missing".into(),
                });
            };
            match tx.try_send(command) {
                Ok(()) => break,
                Err(mpsc::TrySendError::Full(returned)) => {
                    command = returned;
                    if Instant::now() >= send_deadline {
                        self.state.seal_pending.store(false, Ordering::Release);
                        self.writer_tx.take();
                        return Err(AudioEvidenceError::WriterTimeout {
                            timeout: self.config.writer_join_timeout,
                        });
                    }
                    std::thread::sleep(Duration::from_millis(1));
                }
                Err(mpsc::TrySendError::Disconnected(_)) => {
                    self.state.seal_pending.store(false, Ordering::Release);
                    self.writer_tx.take();
                    return Err(AudioEvidenceError::WriterFailed {
                        detail: self
                            .state
                            .writer_error()
                            .unwrap_or_else(|| "writer channel disconnected before seal".into()),
                    });
                }
            }
        }
        self.writer_tx.take();

        let result = self
            .writer_result_rx
            .recv_timeout(self.config.writer_join_timeout)
            .map_err(|error| match error {
                mpsc::RecvTimeoutError::Timeout => AudioEvidenceError::WriterTimeout {
                    timeout: self.config.writer_join_timeout,
                },
                mpsc::RecvTimeoutError::Disconnected => AudioEvidenceError::WriterFailed {
                    detail: self
                        .state
                        .writer_error()
                        .unwrap_or_else(|| "writer result channel disconnected".into()),
                },
            })?;
        if let Some(handle) = self.writer_handle.take() {
            handle
                .join()
                .map_err(|_| AudioEvidenceError::WriterFailed {
                    detail: "writer thread panicked".into(),
                })?;
        }
        self.finalized = true;
        result.map_err(|detail| AudioEvidenceError::WriterFailed { detail })
    }

    fn materialize_manifest(
        &self,
        seal: &SealPayload,
    ) -> Result<AudioEvidenceManifest, AudioEvidenceError> {
        let inspection = inspect_raw_with(&self.config.paths.raw, |_| Ok(()))?;
        let actual_seal = inspection
            .seal
            .ok_or_else(|| AudioEvidenceError::InvalidEvidence {
                path: self.config.paths.raw.clone(),
                detail: "sealed writer result but raw tape has no seal".into(),
            })?;
        if &actual_seal != seal {
            return Err(AudioEvidenceError::InvalidEvidence {
                path: self.config.paths.raw.clone(),
                detail: "raw seal differs from writer result".into(),
            });
        }
        verify_frames_sidecar(&self.config.paths.frames, &self.config.paths.raw)?;

        std::fs::create_dir(&self.config.paths.wav_dir).map_err(|source| {
            if source.kind() == io::ErrorKind::AlreadyExists {
                AudioEvidenceError::ArtifactExists {
                    path: self.config.paths.wav_dir.clone(),
                }
            } else {
                AudioEvidenceError::Io {
                    path: self.config.paths.wav_dir.clone(),
                    source,
                }
            }
        })?;
        sync_parent_dir(&self.config.paths.wav_dir)?;
        let wav_segments = derive_wav_segments(
            &self.config.paths.raw,
            &self.config.paths.wav_dir,
            &self.config.source_labels,
            true,
        )?;
        sync_directory(&self.config.paths.wav_dir)?;

        let raw = fingerprint_path(&self.config.paths.raw)?;
        let frames = fingerprint_path(&self.config.paths.frames)?;
        let manifest = AudioEvidenceManifest {
            schema_version: AUDIO_EVIDENCE_SCHEMA_VERSION,
            identity: self.config.identity.clone(),
            status: if seal.candidate_complete {
                "pending_commit"
            } else {
                "partial"
            }
            .into(),
            complete: false,
            commit_required: seal.candidate_complete,
            close_reason: seal.reason.clone(),
            frame_count: seal.writer_frames,
            sample_count: seal.sample_count,
            expected_published_frames: seal.expected_published_frames,
            bank_accepted_frames: seal.bank_accepted_frames,
            writer_frames: seal.writer_frames,
            dropped_frames: seal.dropped_frames,
            closed_attempts: seal.closed_attempts,
            invalid_attempts: seal.invalid_attempts,
            producer_quiesced: seal.producer_quiesced,
            raw_path: self.config.paths.raw.clone(),
            raw,
            frames_path: self.config.paths.frames.clone(),
            frames,
            source_labels: self.config.source_labels.clone(),
            wav_dir: self.config.paths.wav_dir.clone(),
            wav_segments,
            start_wall_ns: seal.start_wall_ns,
            start_mono_ns: seal.start_mono_ns,
            end_wall_ns: seal.end_wall_ns,
            end_mono_ns: seal.end_mono_ns,
        };
        write_atomic_json_new(&self.config.paths.manifest, &manifest)?;
        Ok(manifest)
    }

    fn publish_commit(
        &self,
        manifest: &AudioEvidenceManifest,
    ) -> Result<AudioEvidenceCommit, AudioEvidenceError> {
        if !manifest.commit_required || manifest.status != "pending_commit" {
            return Err(AudioEvidenceError::InvalidEvidence {
                path: self.config.paths.manifest.clone(),
                detail: "refusing to commit a partial manifest".into(),
            });
        }
        let commit = AudioEvidenceCommit {
            schema_version: AUDIO_EVIDENCE_SCHEMA_VERSION,
            identity: manifest.identity.clone(),
            status: "committed".into(),
            complete: true,
            durable: true,
            manifest_path: self.config.paths.manifest.clone(),
            manifest: fingerprint_path(&self.config.paths.manifest)?,
            raw_path: manifest.raw_path.clone(),
            raw: manifest.raw.clone(),
            frames_path: manifest.frames_path.clone(),
            frames: manifest.frames.clone(),
            wav_dir: manifest.wav_dir.clone(),
            wav_segments: manifest.wav_segments.clone(),
            frame_count: manifest.frame_count,
            sample_count: manifest.sample_count,
            close_wall_ns: wall_ns_now(),
            close_mono_ns: MonotonicNs::now().0,
        };
        write_atomic_json_new(&self.config.paths.commit, &commit)?;
        Ok(commit)
    }

    fn partial_from(&self, seal: &SealPayload, manifest_written: bool) -> AudioEvidencePartial {
        AudioEvidencePartial {
            paths: self.config.paths.clone(),
            reason: seal.reason.clone(),
            accepted_frames: seal.bank_accepted_frames,
            written_frames: seal.writer_frames,
            dropped_frames: seal.dropped_frames,
            closed_attempts: seal.closed_attempts,
            invalid_attempts: seal.invalid_attempts,
            producer_quiesced: seal.producer_quiesced,
            manifest_written,
        }
    }
}

impl Drop for AudioEvidenceBank {
    fn drop(&mut self) {
        if self.finalized {
            return;
        }
        self.state.accepting.store(false, Ordering::Release);
        self.state.seal_pending.store(false, Ordering::Release);
        self.writer_tx.take();
        // The writer polls shared state and exits even if an abandoned tap
        // clone still owns a channel sender.  Do not block unwinding here.
        self.writer_handle.take();
    }
}

fn writer_loop(
    mut raw: File,
    mut frames: File,
    _lock: File,
    rx: mpsc::Receiver<WriterCommand>,
    state: Arc<SharedState>,
    config: AudioEvidenceConfig,
    header: RawHeader,
    header_hash: [u8; 32],
) -> Result<SealPayload, String> {
    #[cfg(test)]
    if let Some(delay) = config.test_writer_start_delay {
        std::thread::sleep(delay);
    }
    let mut previous_hash = header_hash;
    let mut writer_frames = 0u64;
    let mut sample_count = 0u64;

    loop {
        let command = match rx.recv_timeout(WRITER_POLL) {
            Ok(command) => command,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                if !state.accepting.load(Ordering::Acquire)
                    && !state.seal_pending.load(Ordering::Acquire)
                    && state.in_flight_taps.load(Ordering::Acquire) == 0
                {
                    return Err("bank dropped without a durable seal".into());
                }
                continue;
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                return Err("all writer senders disconnected without a durable seal".into());
            }
        };

        match command {
            WriterCommand::Frame(frame) => {
                #[cfg(test)]
                if config.test_fail_after_frames == Some(writer_frames) {
                    return Err(format!(
                        "injected writer failure after {writer_frames} frames"
                    ));
                }
                let offset = raw
                    .stream_position()
                    .map_err(|error| format!("raw stream_position failed: {error}"))?;
                let (meta, record_hash, record_bytes) = write_raw_frame(
                    &mut raw,
                    &config.paths.raw,
                    writer_frames,
                    &frame,
                    previous_hash,
                )
                .map_err(|error| error.to_string())?;
                let sidecar = SidecarFrame {
                    t: "frame".into(),
                    meta,
                    raw_offset: offset,
                    raw_record_bytes: record_bytes,
                    record_sha256: hex(&record_hash),
                };
                write_json_line(&mut frames, &config.paths.frames, &sidecar)
                    .map_err(|error| error.to_string())?;
                previous_hash = record_hash;
                writer_frames = writer_frames
                    .checked_add(1)
                    .ok_or_else(|| "writer frame count overflow".to_string())?;
                sample_count = sample_count
                    .checked_add(frame.pcm.len() as u64)
                    .ok_or_else(|| "writer sample count overflow".to_string())?;
                state.written_frames.store(writer_frames, Ordering::Release);
                if writer_frames % config.sync_every_frames == 0 {
                    durable_sync(&raw, &config.paths.raw).map_err(|error| error.to_string())?;
                    durable_sync(&frames, &config.paths.frames)
                        .map_err(|error| error.to_string())?;
                }
            }
            WriterCommand::Seal(request) => {
                let candidate_complete = request.requested_complete
                    && request.expected_published_frames > 0
                    && request.expected_published_frames == request.bank_accepted_frames
                    && request.bank_accepted_frames == writer_frames
                    && request.dropped_frames == 0
                    && request.closed_attempts == 0
                    && request.invalid_attempts == 0
                    && request.producer_quiesced
                    && request.active_taps == 0
                    && request.in_flight_taps == 0
                    && !state.writer_failed.load(Ordering::Acquire);
                let seal = SealPayload {
                    schema_version: AUDIO_EVIDENCE_SCHEMA_VERSION,
                    identity: config.identity.clone(),
                    status: if candidate_complete {
                        "pending_commit"
                    } else {
                        "partial"
                    }
                    .into(),
                    complete: false,
                    commit_required: candidate_complete,
                    requested_complete: request.requested_complete,
                    candidate_complete,
                    reason: request.reason,
                    expected_published_frames: request.expected_published_frames,
                    bank_accepted_frames: request.bank_accepted_frames,
                    writer_frames,
                    sample_count,
                    dropped_frames: request.dropped_frames,
                    closed_attempts: request.closed_attempts,
                    invalid_attempts: request.invalid_attempts,
                    producer_quiesced: request.producer_quiesced,
                    active_taps: request.active_taps,
                    in_flight_taps: request.in_flight_taps,
                    previous_record_sha256: hex(&previous_hash),
                    start_wall_ns: header.start_wall_ns,
                    start_mono_ns: header.start_mono_ns,
                    end_wall_ns: wall_ns_now(),
                    end_mono_ns: MonotonicNs::now().0,
                };
                let seal_hash = write_raw_seal(&mut raw, &config.paths.raw, &seal)
                    .map_err(|error| error.to_string())?;
                write_json_line(
                    &mut frames,
                    &config.paths.frames,
                    &SidecarClose {
                        t: "close".into(),
                        seal: seal.clone(),
                        seal_sha256: hex(&seal_hash),
                    },
                )
                .map_err(|error| error.to_string())?;
                durable_sync(&raw, &config.paths.raw).map_err(|error| error.to_string())?;
                durable_sync(&frames, &config.paths.frames).map_err(|error| error.to_string())?;
                state.seal_pending.store(false, Ordering::Release);
                return Ok(seal);
            }
        }
    }
}

fn validate_frame(
    sample_rate_hz: u32,
    capture_ts_ns: u64,
    preproc_ts_ns: u64,
    sample_count: usize,
) -> Result<(), String> {
    if sample_rate_hz == 0 || sample_rate_hz > 384_000 {
        return Err(format!(
            "sample_rate_hz {sample_rate_hz} is outside 1..=384000"
        ));
    }
    if sample_count == 0 {
        return Err("PCM is empty".into());
    }
    let bytes = sample_count
        .checked_mul(2)
        .ok_or_else(|| "PCM byte length overflow".to_string())?;
    if bytes > MAX_PCM_BYTES {
        return Err(format!(
            "PCM frame has {bytes} bytes, max is {MAX_PCM_BYTES}"
        ));
    }
    if capture_ts_ns == 0 {
        return Err("capture_ts is zero".into());
    }
    if preproc_ts_ns == 0 {
        return Err("preproc_ts is zero".into());
    }
    if preproc_ts_ns < capture_ts_ns {
        return Err(format!(
            "preproc_ts {preproc_ts_ns} precedes capture_ts {capture_ts_ns}"
        ));
    }
    Ok(())
}

fn validate_config(config: &AudioEvidenceConfig) -> Result<(), AudioEvidenceError> {
    if config.identity.run_id.trim().is_empty()
        || config.identity.event_id.trim().is_empty()
        || config.identity.run_id.contains('\n')
        || config.identity.run_id.contains('\r')
        || config.identity.event_id.contains('\n')
        || config.identity.event_id.contains('\r')
    {
        return Err(AudioEvidenceError::InvalidConfig(
            "run_id and event_id must be non-empty single-line values".into(),
        ));
    }
    if config.identity.plan_sha256.len() != 64
        || !config
            .identity
            .plan_sha256
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(AudioEvidenceError::InvalidConfig(
            "plan_sha256 must be 64 lowercase hexadecimal characters".into(),
        ));
    }
    if config.queue_capacity == 0 {
        return Err(AudioEvidenceError::InvalidConfig(
            "queue_capacity must be positive".into(),
        ));
    }
    if config.sync_every_frames == 0 {
        return Err(AudioEvidenceError::InvalidConfig(
            "sync_every_frames must be positive".into(),
        ));
    }
    if config.producer_quiescence_timeout.is_zero() || config.writer_join_timeout.is_zero() {
        return Err(AudioEvidenceError::InvalidConfig(
            "quiescence and writer timeouts must be positive".into(),
        ));
    }
    let paths = [
        &config.paths.raw,
        &config.paths.frames,
        &config.paths.manifest,
        &config.paths.commit,
        &config.paths.lock,
        &config.paths.wav_dir,
    ];
    if paths.iter().any(|path| !path.is_absolute()) {
        return Err(AudioEvidenceError::InvalidConfig(
            "all audio evidence paths must be absolute".into(),
        ));
    }
    let unique: HashSet<&Path> = paths.iter().map(|path| path.as_path()).collect();
    if unique.len() != paths.len() {
        return Err(AudioEvidenceError::InvalidConfig(
            "audio evidence paths must be pairwise distinct".into(),
        ));
    }
    for file in paths.iter().take(5) {
        if file.starts_with(&config.paths.wav_dir) {
            return Err(AudioEvidenceError::InvalidConfig(
                "raw/frames/manifest/commit/lock files may not be inside wav_dir".into(),
            ));
        }
    }
    let mut seen_labels = HashSet::new();
    for label in config.source_labels.values() {
        if label.trim().is_empty() || label.contains('\n') || label.contains('\r') {
            return Err(AudioEvidenceError::InvalidConfig(
                "source labels must be non-empty single-line values".into(),
            ));
        }
        if !seen_labels.insert(label) {
            return Err(AudioEvidenceError::InvalidConfig(
                "source labels must be unique".into(),
            ));
        }
    }
    Ok(())
}

fn prepare_parent_directories(paths: &AudioEvidencePaths) -> Result<(), AudioEvidenceError> {
    let mut parents = HashSet::new();
    for path in [
        &paths.raw,
        &paths.frames,
        &paths.manifest,
        &paths.commit,
        &paths.lock,
    ] {
        if let Some(parent) = path.parent() {
            parents.insert(parent.to_path_buf());
        }
    }
    if let Some(parent) = paths.wav_dir.parent() {
        parents.insert(parent.to_path_buf());
    }
    for parent in parents {
        std::fs::create_dir_all(&parent).map_err(|source| AudioEvidenceError::Io {
            path: parent.clone(),
            source,
        })?;
    }
    Ok(())
}

fn artifact_creation_paths(paths: &AudioEvidencePaths) -> Vec<PathBuf> {
    vec![
        paths.raw.clone(),
        paths.frames.clone(),
        paths.manifest.clone(),
        paths.commit.clone(),
        paths.wav_dir.clone(),
        pending_path(&paths.manifest),
        pending_path(&paths.commit),
    ]
}

fn create_new_file(path: &Path) -> Result<File, AudioEvidenceError> {
    OpenOptions::new()
        .write(true)
        .read(true)
        .create_new(true)
        .open(path)
        .map_err(|source| {
            if source.kind() == io::ErrorKind::AlreadyExists {
                AudioEvidenceError::ArtifactExists {
                    path: path.to_path_buf(),
                }
            } else {
                AudioEvidenceError::Io {
                    path: path.to_path_buf(),
                    source,
                }
            }
        })
}

fn write_raw_header(
    file: &mut File,
    path: &Path,
    payload: &[u8],
) -> Result<[u8; 32], AudioEvidenceError> {
    let len = u32::try_from(payload.len())
        .map_err(|_| AudioEvidenceError::InvalidConfig("raw header is too large".into()))?;
    let len_bytes = len.to_le_bytes();
    let hash = digest_parts(&[RAW_MAGIC, &len_bytes, payload]);
    write_all_at(file, path, RAW_MAGIC)?;
    write_all_at(file, path, &len_bytes)?;
    write_all_at(file, path, payload)?;
    write_all_at(file, path, &hash)?;
    Ok(hash)
}

fn write_raw_frame(
    file: &mut File,
    path: &Path,
    record_index: u64,
    frame: &OwnedFrame,
    previous_hash: [u8; 32],
) -> Result<(FrameMeta, [u8; 32], u64), AudioEvidenceError> {
    validate_frame(
        frame.sample_rate_hz,
        frame.capture_ts_ns,
        frame.preproc_ts_ns,
        frame.pcm.len(),
    )
    .map_err(|detail| AudioEvidenceError::InvalidEvidence {
        path: path.to_path_buf(),
        detail,
    })?;
    let pcm = pcm_to_le_bytes(&frame.pcm);
    let meta = FrameMeta {
        schema_version: AUDIO_EVIDENCE_SCHEMA_VERSION,
        record_index,
        source: frame.source,
        seq: frame.seq,
        sample_rate_hz: frame.sample_rate_hz,
        capture_ts_ns: frame.capture_ts_ns,
        preproc_ts_ns: frame.preproc_ts_ns,
        sample_count: frame.pcm.len() as u64,
        pcm_sha256: hex(&Sha256::digest(&pcm)),
        previous_record_sha256: hex(&previous_hash),
    };
    let meta_json =
        serde_json::to_vec(&meta).map_err(|error| AudioEvidenceError::InvalidEvidence {
            path: path.to_path_buf(),
            detail: format!("frame metadata serialization failed: {error}"),
        })?;
    let meta_len =
        u32::try_from(meta_json.len()).map_err(|_| AudioEvidenceError::InvalidEvidence {
            path: path.to_path_buf(),
            detail: "frame metadata too large".into(),
        })?;
    let pcm_len = pcm.len() as u64;
    let meta_len_bytes = meta_len.to_le_bytes();
    let pcm_len_bytes = pcm_len.to_le_bytes();
    let hash = digest_parts(&[
        FRAME_MAGIC,
        &meta_len_bytes,
        &pcm_len_bytes,
        &meta_json,
        &pcm,
    ]);
    write_all_at(file, path, FRAME_MAGIC)?;
    write_all_at(file, path, &meta_len_bytes)?;
    write_all_at(file, path, &pcm_len_bytes)?;
    write_all_at(file, path, &meta_json)?;
    write_all_at(file, path, &pcm)?;
    write_all_at(file, path, &hash)?;
    let bytes = 4u64 + 4 + 8 + meta_json.len() as u64 + pcm.len() as u64 + 32;
    Ok((meta, hash, bytes))
}

fn write_raw_seal(
    file: &mut File,
    path: &Path,
    seal: &SealPayload,
) -> Result<[u8; 32], AudioEvidenceError> {
    let payload =
        serde_json::to_vec(seal).map_err(|error| AudioEvidenceError::InvalidEvidence {
            path: path.to_path_buf(),
            detail: format!("seal serialization failed: {error}"),
        })?;
    let len = u32::try_from(payload.len()).map_err(|_| AudioEvidenceError::InvalidEvidence {
        path: path.to_path_buf(),
        detail: "seal is too large".into(),
    })?;
    let len_bytes = len.to_le_bytes();
    let hash = digest_parts(&[SEAL_MAGIC, &len_bytes, &payload]);
    write_all_at(file, path, SEAL_MAGIC)?;
    write_all_at(file, path, &len_bytes)?;
    write_all_at(file, path, &payload)?;
    write_all_at(file, path, &hash)?;
    Ok(hash)
}

fn write_all_at(file: &mut File, path: &Path, bytes: &[u8]) -> Result<(), AudioEvidenceError> {
    file.write_all(bytes)
        .map_err(|source| AudioEvidenceError::Io {
            path: path.to_path_buf(),
            source,
        })
}

fn write_json_line(
    file: &mut File,
    path: &Path,
    value: &impl Serialize,
) -> Result<(), AudioEvidenceError> {
    let mut encoded =
        serde_json::to_vec(value).map_err(|error| AudioEvidenceError::InvalidEvidence {
            path: path.to_path_buf(),
            detail: format!("JSON serialization failed: {error}"),
        })?;
    encoded.push(b'\n');
    write_all_at(file, path, &encoded)
}

struct RawInspectionInternal {
    header: RawHeader,
    header_sha256: String,
    frame_count: u64,
    sample_count: u64,
    seal: Option<SealPayload>,
    seal_sha256: Option<String>,
}

struct ParsedRecord {
    frame: AudioEvidenceFrame,
    meta: FrameMeta,
    raw_offset: u64,
    raw_record_bytes: u64,
    record_sha256: String,
}

fn inspect_raw_with(
    path: &Path,
    mut on_frame: impl FnMut(&ParsedRecord) -> Result<(), AudioEvidenceError>,
) -> Result<RawInspectionInternal, AudioEvidenceError> {
    let mut file = File::open(path).map_err(|source| AudioEvidenceError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    let magic = read_exact_array::<8>(&mut file, path, "raw magic")?;
    if &magic != RAW_MAGIC {
        return invalid(path, "raw magic/version mismatch");
    }
    let header_len_bytes = read_exact_array::<4>(&mut file, path, "header length")?;
    let header_len = u32::from_le_bytes(header_len_bytes) as usize;
    if header_len == 0 || header_len > MAX_JSON_BYTES {
        return invalid(path, format!("invalid header length {header_len}"));
    }
    let header_json = read_exact_vec(&mut file, path, header_len, "header JSON")?;
    let header_hash = read_exact_array::<32>(&mut file, path, "header hash")?;
    let expected_header_hash = digest_parts(&[RAW_MAGIC, &header_len_bytes, &header_json]);
    if header_hash != expected_header_hash {
        return invalid(path, "raw header SHA-256 mismatch");
    }
    let header: RawHeader = serde_json::from_slice(&header_json)
        .map_err(|error| invalid_error(path, format!("invalid raw header JSON: {error}")))?;
    if header.schema_version != AUDIO_EVIDENCE_SCHEMA_VERSION
        || header.format != "length_delimited_pcm_s16le_mono"
    {
        return invalid(path, "unsupported raw header protocol");
    }

    let mut previous_hash = header_hash;
    let mut frame_count = 0u64;
    let mut sample_count = 0u64;
    let mut seal = None;
    let mut seal_sha256 = None;
    loop {
        let offset = file
            .stream_position()
            .map_err(|source| AudioEvidenceError::Io {
                path: path.to_path_buf(),
                source,
            })?;
        let marker = match read_marker_or_eof(&mut file, path)? {
            None => break,
            Some(marker) => marker,
        };
        if &marker == FRAME_MAGIC {
            if seal.is_some() {
                return invalid(path, "frame appears after seal");
            }
            let meta_len_bytes = read_exact_array::<4>(&mut file, path, "frame metadata length")?;
            let pcm_len_bytes = read_exact_array::<8>(&mut file, path, "frame PCM length")?;
            let meta_len = u32::from_le_bytes(meta_len_bytes) as usize;
            let pcm_len_u64 = u64::from_le_bytes(pcm_len_bytes);
            let pcm_len = usize::try_from(pcm_len_u64)
                .map_err(|_| invalid_error(path, "PCM length does not fit usize"))?;
            if meta_len == 0 || meta_len > MAX_JSON_BYTES {
                return invalid(path, format!("invalid frame metadata length {meta_len}"));
            }
            if pcm_len == 0 || pcm_len > MAX_PCM_BYTES || pcm_len % 2 != 0 {
                return invalid(path, format!("invalid frame PCM length {pcm_len}"));
            }
            let meta_json = read_exact_vec(&mut file, path, meta_len, "frame metadata")?;
            let pcm_bytes = read_exact_vec(&mut file, path, pcm_len, "frame PCM")?;
            let record_hash = read_exact_array::<32>(&mut file, path, "frame hash")?;
            let expected_hash = digest_parts(&[
                FRAME_MAGIC,
                &meta_len_bytes,
                &pcm_len_bytes,
                &meta_json,
                &pcm_bytes,
            ]);
            if record_hash != expected_hash {
                return invalid(path, format!("frame {frame_count} record SHA-256 mismatch"));
            }
            let meta: FrameMeta = serde_json::from_slice(&meta_json).map_err(|error| {
                invalid_error(
                    path,
                    format!("invalid frame {frame_count} metadata: {error}"),
                )
            })?;
            if meta.schema_version != AUDIO_EVIDENCE_SCHEMA_VERSION
                || meta.record_index != frame_count
                || meta.previous_record_sha256 != hex(&previous_hash)
                || meta.sample_count != (pcm_len / 2) as u64
                || meta.pcm_sha256 != hex(&Sha256::digest(&pcm_bytes))
            {
                return invalid(
                    path,
                    format!("frame {frame_count} metadata/hash chain mismatch"),
                );
            }
            validate_frame(
                meta.sample_rate_hz,
                meta.capture_ts_ns,
                meta.preproc_ts_ns,
                pcm_len / 2,
            )
            .map_err(|detail| invalid_error(path, format!("frame {frame_count}: {detail}")))?;
            let pcm: Vec<i16> = pcm_bytes
                .chunks_exact(2)
                .map(|bytes| i16::from_le_bytes([bytes[0], bytes[1]]))
                .collect();
            let raw_record_bytes = 4u64 + 4 + 8 + meta_len as u64 + pcm_len as u64 + 32;
            on_frame(&ParsedRecord {
                frame: AudioEvidenceFrame {
                    pcm: pcm.into(),
                    sample_rate_hz: meta.sample_rate_hz,
                    capture_ts: MonotonicNs(meta.capture_ts_ns),
                    preproc_ts: MonotonicNs(meta.preproc_ts_ns),
                    source: SourceId(meta.source),
                    seq: meta.seq,
                    record_index: meta.record_index,
                },
                meta,
                raw_offset: offset,
                raw_record_bytes,
                record_sha256: hex(&record_hash),
            })?;
            previous_hash = record_hash;
            frame_count = frame_count
                .checked_add(1)
                .ok_or_else(|| invalid_error(path, "frame count overflow"))?;
            sample_count = sample_count
                .checked_add((pcm_len / 2) as u64)
                .ok_or_else(|| invalid_error(path, "sample count overflow"))?;
        } else if &marker == SEAL_MAGIC {
            if seal.is_some() {
                return invalid(path, "duplicate raw seal");
            }
            let len_bytes = read_exact_array::<4>(&mut file, path, "seal length")?;
            let len = u32::from_le_bytes(len_bytes) as usize;
            if len == 0 || len > MAX_JSON_BYTES {
                return invalid(path, format!("invalid seal length {len}"));
            }
            let payload = read_exact_vec(&mut file, path, len, "seal payload")?;
            let hash = read_exact_array::<32>(&mut file, path, "seal hash")?;
            if hash != digest_parts(&[SEAL_MAGIC, &len_bytes, &payload]) {
                return invalid(path, "seal SHA-256 mismatch");
            }
            let parsed: SealPayload = serde_json::from_slice(&payload)
                .map_err(|error| invalid_error(path, format!("invalid seal JSON: {error}")))?;
            validate_seal(
                &parsed,
                &header,
                frame_count,
                sample_count,
                previous_hash,
                path,
            )?;
            seal_sha256 = Some(hex(&hash));
            seal = Some(parsed);
            if read_marker_or_eof(&mut file, path)?.is_some() {
                return invalid(path, "bytes appear after raw seal");
            }
            break;
        } else {
            return invalid(path, format!("unknown raw record marker {marker:?}"));
        }
    }
    Ok(RawInspectionInternal {
        header,
        header_sha256: hex(&header_hash),
        frame_count,
        sample_count,
        seal,
        seal_sha256,
    })
}

fn validate_seal(
    seal: &SealPayload,
    header: &RawHeader,
    frame_count: u64,
    sample_count: u64,
    previous_hash: [u8; 32],
    path: &Path,
) -> Result<(), AudioEvidenceError> {
    if seal.schema_version != AUDIO_EVIDENCE_SCHEMA_VERSION
        || seal.identity != header.identity
        || seal.complete
        || seal.writer_frames != frame_count
        || seal.sample_count != sample_count
        || seal.previous_record_sha256 != hex(&previous_hash)
        || seal.start_wall_ns != header.start_wall_ns
        || seal.start_mono_ns != header.start_mono_ns
        || seal.end_wall_ns < seal.start_wall_ns
        || seal.end_mono_ns < seal.start_mono_ns
    {
        return invalid(
            path,
            "raw seal identity, counts, clocks, or hash chain are invalid",
        );
    }
    let candidate = seal.requested_complete
        && seal.expected_published_frames > 0
        && seal.expected_published_frames == seal.bank_accepted_frames
        && seal.bank_accepted_frames == seal.writer_frames
        && seal.dropped_frames == 0
        && seal.closed_attempts == 0
        && seal.invalid_attempts == 0
        && seal.producer_quiesced
        && seal.active_taps == 0
        && seal.in_flight_taps == 0;
    if seal.candidate_complete != candidate
        || seal.commit_required != candidate
        || seal.status
            != if candidate {
                "pending_commit"
            } else {
                "partial"
            }
    {
        return invalid(path, "raw seal completeness predicate is inconsistent");
    }
    Ok(())
}

pub fn replay_audio_evidence(path: &Path) -> Result<Vec<AudioEvidenceFrame>, AudioEvidenceError> {
    let mut frames = Vec::new();
    inspect_raw_with(path, |record| {
        frames.push(record.frame.clone());
        Ok(())
    })?;
    Ok(frames)
}

pub fn inspect_audio_evidence_raw(
    path: &Path,
) -> Result<AudioEvidenceRawInspection, AudioEvidenceError> {
    let inspection = inspect_raw_with(path, |_| Ok(()))?;
    Ok(AudioEvidenceRawInspection {
        identity: inspection.header.identity,
        frame_count: inspection.frame_count,
        sample_count: inspection.sample_count,
        sealed: inspection.seal.is_some(),
        candidate_complete: inspection.seal.is_some_and(|seal| seal.candidate_complete),
    })
}

fn verify_frames_sidecar(frames_path: &Path, raw_path: &Path) -> Result<(), AudioEvidenceError> {
    let file = File::open(frames_path).map_err(|source| AudioEvidenceError::Io {
        path: frames_path.to_path_buf(),
        source,
    })?;
    let mut lines = StrictJsonLines::new(BufReader::new(file), frames_path);
    let session: SidecarSession = lines
        .next_typed("session")?
        .ok_or_else(|| invalid_error(frames_path, "frame sidecar is empty"))?;
    if session.t != "session" || session.schema_version != AUDIO_EVIDENCE_SCHEMA_VERSION {
        return invalid(frames_path, "invalid frame sidecar session row");
    }
    let inspection = inspect_raw_with(raw_path, |record| {
        let row: SidecarFrame = lines
            .next_typed("frame")?
            .ok_or_else(|| invalid_error(frames_path, "sidecar ended before raw frames"))?;
        let expected = SidecarFrame {
            t: "frame".into(),
            meta: record.meta.clone(),
            raw_offset: record.raw_offset,
            raw_record_bytes: record.raw_record_bytes,
            record_sha256: record.record_sha256.clone(),
        };
        if row != expected {
            return invalid(
                frames_path,
                format!(
                    "sidecar frame {} differs from raw",
                    record.meta.record_index
                ),
            );
        }
        Ok(())
    })?;
    if session.identity != inspection.header.identity
        || session.raw_header_sha256 != inspection.header_sha256
        || session.start_wall_ns != inspection.header.start_wall_ns
        || session.start_mono_ns != inspection.header.start_mono_ns
    {
        return invalid(frames_path, "sidecar session does not bind the raw header");
    }
    let seal = inspection
        .seal
        .ok_or_else(|| invalid_error(raw_path, "raw tape has no seal"))?;
    let close: SidecarClose = lines
        .next_typed("close")?
        .ok_or_else(|| invalid_error(frames_path, "sidecar has no close row"))?;
    if close.t != "close"
        || close.seal != seal
        || Some(close.seal_sha256.as_str()) != inspection.seal_sha256.as_deref()
    {
        return invalid(frames_path, "sidecar close row differs from raw seal");
    }
    if lines.next_value()?.is_some() {
        return invalid(frames_path, "sidecar has rows after close");
    }
    Ok(())
}

struct StrictJsonLines<'a> {
    reader: BufReader<File>,
    path: &'a Path,
    line_number: usize,
}

impl<'a> StrictJsonLines<'a> {
    fn new(reader: BufReader<File>, path: &'a Path) -> Self {
        Self {
            reader,
            path,
            line_number: 0,
        }
    }

    fn next_value(&mut self) -> Result<Option<Value>, AudioEvidenceError> {
        let mut line = Vec::new();
        let read =
            self.reader
                .read_until(b'\n', &mut line)
                .map_err(|source| AudioEvidenceError::Io {
                    path: self.path.to_path_buf(),
                    source,
                })?;
        if read == 0 {
            return Ok(None);
        }
        self.line_number += 1;
        if !line.ends_with(b"\n") {
            return invalid(
                self.path,
                format!("truncated sidecar line {}", self.line_number),
            );
        }
        line.pop();
        if line.is_empty() {
            return invalid(
                self.path,
                format!("blank sidecar line {}", self.line_number),
            );
        }
        serde_json::from_slice(&line).map(Some).map_err(|error| {
            invalid_error(
                self.path,
                format!("invalid JSON on sidecar line {}: {error}", self.line_number),
            )
        })
    }

    fn next_typed<T: for<'de> Deserialize<'de>>(
        &mut self,
        expected: &str,
    ) -> Result<Option<T>, AudioEvidenceError> {
        self.next_value()?
            .map(|value| {
                serde_json::from_value(value).map_err(|error| {
                    invalid_error(
                        self.path,
                        format!(
                            "invalid {expected} row on line {}: {error}",
                            self.line_number
                        ),
                    )
                })
            })
            .transpose()
    }
}

struct ActiveSegment {
    source: u16,
    source_label: Option<String>,
    segment: u32,
    sample_rate_hz: u32,
    first_seq: u64,
    last_seq: u64,
    first_capture_ts_ns: u64,
    last_capture_ts_ns: u64,
    first_preproc_ts_ns: u64,
    last_preproc_ts_ns: u64,
    frame_count: u64,
    sample_count: u64,
    start_reasons: Vec<String>,
    pcm_hasher: Sha256,
    path: PathBuf,
    writer: Option<hound::WavWriter<BufWriter<File>>>,
}

fn derive_wav_segments(
    raw_path: &Path,
    wav_dir: &Path,
    source_labels: &BTreeMap<u16, String>,
    write: bool,
) -> Result<Vec<WavSegmentManifest>, AudioEvidenceError> {
    let mut active = BTreeMap::<u16, ActiveSegment>::new();
    let mut completed = Vec::<WavSegmentManifest>::new();
    inspect_raw_with(raw_path, |record| {
        let frame = &record.frame;
        let source = frame.source.0;
        let reasons = active.get(&source).map(|previous| {
            let mut reasons = Vec::new();
            if previous.sample_rate_hz != frame.sample_rate_hz {
                reasons.push(format!(
                    "sample_rate_change:{}->{}",
                    previous.sample_rate_hz, frame.sample_rate_hz
                ));
            }
            if previous.last_seq.checked_add(1) != Some(frame.seq) {
                reasons.push(format!(
                    "sequence_discontinuity:{}->{}",
                    previous.last_seq, frame.seq
                ));
            }
            if frame.capture_ts.0 < previous.last_capture_ts_ns {
                reasons.push(format!(
                    "capture_timestamp_regression:{}->{}",
                    previous.last_capture_ts_ns, frame.capture_ts.0
                ));
            }
            if frame.preproc_ts.0 < previous.last_preproc_ts_ns {
                reasons.push(format!(
                    "preproc_timestamp_regression:{}->{}",
                    previous.last_preproc_ts_ns, frame.preproc_ts.0
                ));
            }
            reasons
        });
        if reasons.as_ref().is_some_and(|reasons| !reasons.is_empty()) {
            let old = active.remove(&source).expect("source existed");
            completed.push(finalize_segment(old)?);
        }
        if !active.contains_key(&source) {
            let segment = completed
                .iter()
                .filter(|segment| segment.source == source)
                .count() as u32;
            let start_reasons = reasons
                .filter(|reasons| !reasons.is_empty())
                .unwrap_or_else(|| vec!["source_start".into()]);
            active.insert(
                source,
                start_segment(
                    wav_dir,
                    source_labels.get(&source).cloned(),
                    segment,
                    frame,
                    start_reasons,
                    write,
                )?,
            );
        }
        append_segment(active.get_mut(&source).expect("source inserted"), frame)?;
        Ok(())
    })?;
    for (_, segment) in active {
        completed.push(finalize_segment(segment)?);
    }
    completed.sort_by_key(|segment| (segment.source, segment.segment));
    Ok(completed)
}

fn start_segment(
    wav_dir: &Path,
    source_label: Option<String>,
    segment: u32,
    frame: &AudioEvidenceFrame,
    start_reasons: Vec<String>,
    write: bool,
) -> Result<ActiveSegment, AudioEvidenceError> {
    let path = wav_dir.join(format!(
        "source-{:05}-segment-{segment:04}-{}hz.wav",
        frame.source.0, frame.sample_rate_hz
    ));
    let writer = if write {
        Some(
            hound::WavWriter::create(
                &path,
                hound::WavSpec {
                    channels: 1,
                    sample_rate: frame.sample_rate_hz,
                    bits_per_sample: 16,
                    sample_format: hound::SampleFormat::Int,
                },
            )
            .map_err(|error| artifact_error(&path, format!("create WAV failed: {error}")))?,
        )
    } else {
        None
    };
    Ok(ActiveSegment {
        source: frame.source.0,
        source_label,
        segment,
        sample_rate_hz: frame.sample_rate_hz,
        first_seq: frame.seq,
        last_seq: frame.seq,
        first_capture_ts_ns: frame.capture_ts.0,
        last_capture_ts_ns: frame.capture_ts.0,
        first_preproc_ts_ns: frame.preproc_ts.0,
        last_preproc_ts_ns: frame.preproc_ts.0,
        frame_count: 0,
        sample_count: 0,
        start_reasons,
        pcm_hasher: Sha256::new(),
        path,
        writer,
    })
}

fn append_segment(
    segment: &mut ActiveSegment,
    frame: &AudioEvidenceFrame,
) -> Result<(), AudioEvidenceError> {
    if segment.source != frame.source.0 || segment.sample_rate_hz != frame.sample_rate_hz {
        return invalid(&segment.path, "frame does not belong to active WAV segment");
    }
    for &sample in frame.pcm.iter() {
        let bytes = sample.to_le_bytes();
        segment.pcm_hasher.update(bytes);
        if let Some(writer) = segment.writer.as_mut() {
            writer.write_sample(sample).map_err(|error| {
                artifact_error(&segment.path, format!("write WAV failed: {error}"))
            })?;
        }
    }
    segment.last_seq = frame.seq;
    segment.last_capture_ts_ns = frame.capture_ts.0;
    segment.last_preproc_ts_ns = frame.preproc_ts.0;
    segment.frame_count += 1;
    segment.sample_count = segment
        .sample_count
        .checked_add(frame.pcm.len() as u64)
        .ok_or_else(|| invalid_error(&segment.path, "WAV sample count overflow"))?;
    Ok(())
}

fn finalize_segment(mut segment: ActiveSegment) -> Result<WavSegmentManifest, AudioEvidenceError> {
    if let Some(writer) = segment.writer.take() {
        writer.finalize().map_err(|error| {
            artifact_error(&segment.path, format!("finalize WAV failed: {error}"))
        })?;
        let file = File::open(&segment.path).map_err(|source| AudioEvidenceError::Io {
            path: segment.path.clone(),
            source,
        })?;
        durable_sync(&file, &segment.path)?;
    }
    let (wav_samples, wav_pcm_sha256, wav_rate) = fingerprint_wav_pcm(&segment.path)?;
    let expected_pcm_sha256 = hex(&segment.pcm_hasher.finalize());
    if wav_samples != segment.sample_count
        || wav_pcm_sha256 != expected_pcm_sha256
        || wav_rate != segment.sample_rate_hz
    {
        return invalid(
            &segment.path,
            "WAV sample count, sample rate, or decoded PCM differs from raw tape",
        );
    }
    Ok(WavSegmentManifest {
        source: segment.source,
        source_label: segment.source_label,
        segment: segment.segment,
        sample_rate_hz: segment.sample_rate_hz,
        first_seq: segment.first_seq,
        last_seq: segment.last_seq,
        first_capture_ts_ns: segment.first_capture_ts_ns,
        last_capture_ts_ns: segment.last_capture_ts_ns,
        first_preproc_ts_ns: segment.first_preproc_ts_ns,
        last_preproc_ts_ns: segment.last_preproc_ts_ns,
        frame_count: segment.frame_count,
        sample_count: segment.sample_count,
        start_reasons: segment.start_reasons,
        pcm_sha256: expected_pcm_sha256,
        path: segment.path.clone(),
        wav: fingerprint_path(&segment.path)?,
    })
}

fn fingerprint_wav_pcm(path: &Path) -> Result<(u64, String, u32), AudioEvidenceError> {
    let mut reader = hound::WavReader::open(path)
        .map_err(|error| artifact_error(path, format!("open WAV failed: {error}")))?;
    let spec = reader.spec();
    if spec.channels != 1
        || spec.bits_per_sample != 16
        || spec.sample_format != hound::SampleFormat::Int
    {
        return invalid(path, "WAV must be mono signed PCM16");
    }
    let mut count = 0u64;
    let mut hasher = Sha256::new();
    for sample in reader.samples::<i16>() {
        let sample = sample
            .map_err(|error| artifact_error(path, format!("decode WAV sample failed: {error}")))?;
        hasher.update(sample.to_le_bytes());
        count = count
            .checked_add(1)
            .ok_or_else(|| invalid_error(path, "WAV sample count overflow"))?;
    }
    Ok((count, hex(&hasher.finalize()), spec.sample_rate))
}

pub fn verify_audio_evidence(
    manifest_path: &Path,
    commit_path: &Path,
) -> Result<AudioEvidenceCommit, AudioEvidenceError> {
    let manifest: AudioEvidenceManifest = read_single_json_line(manifest_path)?;
    let commit: AudioEvidenceCommit = read_single_json_line(commit_path)?;
    if manifest.schema_version != AUDIO_EVIDENCE_SCHEMA_VERSION
        || manifest.complete
        || !manifest.commit_required
        || manifest.status != "pending_commit"
        || commit.schema_version != AUDIO_EVIDENCE_SCHEMA_VERSION
        || !commit.complete
        || !commit.durable
        || commit.status != "committed"
        || commit.identity != manifest.identity
        || commit.manifest_path != manifest_path
        || commit.raw_path != manifest.raw_path
        || commit.frames_path != manifest.frames_path
        || commit.wav_dir != manifest.wav_dir
        || commit.frame_count != manifest.frame_count
        || commit.sample_count != manifest.sample_count
        || commit.wav_segments != manifest.wav_segments
    {
        return invalid(
            commit_path,
            "manifest/commit protocol or cross-bindings are invalid",
        );
    }
    let actual_manifest = fingerprint_path(manifest_path)?;
    let actual_raw = fingerprint_path(&manifest.raw_path)?;
    let actual_frames = fingerprint_path(&manifest.frames_path)?;
    if commit.manifest != actual_manifest
        || manifest.raw != actual_raw
        || commit.raw != actual_raw
        || manifest.frames != actual_frames
        || commit.frames != actual_frames
    {
        return invalid(
            commit_path,
            "artifact byte length or SHA-256 differs from receipt",
        );
    }
    let inspection = inspect_raw_with(&manifest.raw_path, |_| Ok(()))?;
    let seal = inspection
        .seal
        .ok_or_else(|| invalid_error(&manifest.raw_path, "raw tape is unsealed"))?;
    if !seal.candidate_complete
        || seal.identity != manifest.identity
        || seal.reason != manifest.close_reason
        || seal.writer_frames != manifest.frame_count
        || seal.sample_count != manifest.sample_count
        || seal.expected_published_frames != manifest.expected_published_frames
        || seal.bank_accepted_frames != manifest.bank_accepted_frames
        || seal.dropped_frames != manifest.dropped_frames
        || seal.closed_attempts != manifest.closed_attempts
        || seal.invalid_attempts != manifest.invalid_attempts
        || seal.producer_quiesced != manifest.producer_quiesced
        || seal.start_wall_ns != manifest.start_wall_ns
        || seal.start_mono_ns != manifest.start_mono_ns
        || seal.end_wall_ns != manifest.end_wall_ns
        || seal.end_mono_ns != manifest.end_mono_ns
        || commit.close_wall_ns < manifest.end_wall_ns
        || commit.close_mono_ns < manifest.end_mono_ns
    {
        return invalid(manifest_path, "manifest does not match complete raw seal");
    }
    verify_frames_sidecar(&manifest.frames_path, &manifest.raw_path)?;
    let expected_segments = derive_wav_segments(
        &manifest.raw_path,
        &manifest.wav_dir,
        &manifest.source_labels,
        false,
    )?;
    if expected_segments != manifest.wav_segments {
        return invalid(
            manifest_path,
            "WAV segment manifest differs from raw tape or WAV bytes",
        );
    }
    Ok(commit)
}

fn read_single_json_line<T: for<'de> Deserialize<'de>>(
    path: &Path,
) -> Result<T, AudioEvidenceError> {
    let raw = std::fs::read(path).map_err(|source| AudioEvidenceError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    if raw.is_empty() || !raw.ends_with(b"\n") {
        return invalid(path, "JSON artifact is empty or missing final newline");
    }
    if raw[..raw.len() - 1].contains(&b'\n') {
        return invalid(path, "JSON artifact must contain exactly one line");
    }
    serde_json::from_slice(&raw[..raw.len() - 1])
        .map_err(|error| invalid_error(path, format!("invalid JSON: {error}")))
}

fn write_atomic_json_new(path: &Path, value: &impl Serialize) -> Result<(), AudioEvidenceError> {
    if path.exists() {
        return Err(AudioEvidenceError::ArtifactExists {
            path: path.to_path_buf(),
        });
    }
    let pending = pending_path(path);
    let mut file = create_new_file(&pending)?;
    write_json_line(&mut file, &pending, value)?;
    durable_sync(&file, &pending)?;
    drop(file);
    // A normal rename can overwrite a final path created between the exists
    // check and publication.  A same-directory hard link is an atomic
    // no-clobber publication: either the final name did not exist and now
    // references the already-fsynced inode, or publication fails loudly and
    // the pending file remains as partial evidence.
    std::fs::hard_link(&pending, path).map_err(|source| {
        if source.kind() == io::ErrorKind::AlreadyExists {
            AudioEvidenceError::ArtifactExists {
                path: path.to_path_buf(),
            }
        } else {
            AudioEvidenceError::Io {
                path: path.to_path_buf(),
                source,
            }
        }
    })?;
    sync_parent_dir(path)?;
    std::fs::remove_file(&pending).map_err(|source| AudioEvidenceError::Io {
        path: pending.clone(),
        source,
    })?;
    sync_parent_dir(path)
}

fn pending_path(path: &Path) -> PathBuf {
    let mut value = path.as_os_str().to_os_string();
    value.push(".pending");
    PathBuf::from(value)
}

fn fingerprint_path(path: &Path) -> Result<ArtifactFingerprint, AudioEvidenceError> {
    let mut file = File::open(path).map_err(|source| AudioEvidenceError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    let mut hasher = Sha256::new();
    let mut bytes = 0u64;
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|source| AudioEvidenceError::Io {
                path: path.to_path_buf(),
                source,
            })?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
        bytes = bytes
            .checked_add(read as u64)
            .ok_or_else(|| invalid_error(path, "artifact length overflow"))?;
    }
    Ok(ArtifactFingerprint {
        bytes,
        sha256: hex(&hasher.finalize()),
    })
}

fn pcm_to_le_bytes(pcm: &[i16]) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(pcm.len() * 2);
    for sample in pcm {
        bytes.extend_from_slice(&sample.to_le_bytes());
    }
    bytes
}

fn digest_parts(parts: &[&[u8]]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    for part in parts {
        hasher.update(part);
    }
    hasher.finalize().into()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn wall_ns_now() -> u64 {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    now.as_secs()
        .saturating_mul(1_000_000_000)
        .saturating_add(u64::from(now.subsec_nanos()))
}

fn read_exact_array<const N: usize>(
    reader: &mut File,
    path: &Path,
    what: &str,
) -> Result<[u8; N], AudioEvidenceError> {
    let mut bytes = [0u8; N];
    reader
        .read_exact(&mut bytes)
        .map_err(|error| read_error(path, what, error))?;
    Ok(bytes)
}

fn read_exact_vec(
    reader: &mut File,
    path: &Path,
    len: usize,
    what: &str,
) -> Result<Vec<u8>, AudioEvidenceError> {
    let mut bytes = vec![0u8; len];
    reader
        .read_exact(&mut bytes)
        .map_err(|error| read_error(path, what, error))?;
    Ok(bytes)
}

fn read_marker_or_eof(
    reader: &mut File,
    path: &Path,
) -> Result<Option<[u8; 4]>, AudioEvidenceError> {
    let mut marker = [0u8; 4];
    match reader.read(&mut marker[..1]) {
        Ok(0) => Ok(None),
        Ok(1) => {
            reader
                .read_exact(&mut marker[1..])
                .map_err(|error| read_error(path, "record marker", error))?;
            Ok(Some(marker))
        }
        Ok(_) => unreachable!(),
        Err(source) => Err(AudioEvidenceError::Io {
            path: path.to_path_buf(),
            source,
        }),
    }
}

fn read_error(path: &Path, what: &str, error: io::Error) -> AudioEvidenceError {
    if error.kind() == io::ErrorKind::UnexpectedEof {
        invalid_error(path, format!("truncated {what}"))
    } else {
        AudioEvidenceError::Io {
            path: path.to_path_buf(),
            source: error,
        }
    }
}

fn durable_sync(file: &File, path: &Path) -> Result<(), AudioEvidenceError> {
    file.sync_all().map_err(|source| AudioEvidenceError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    #[cfg(target_os = "macos")]
    {
        // SAFETY: fcntl reads the valid descriptor and retains no pointer.
        let rc = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_FULLFSYNC) };
        if rc != 0 {
            return Err(AudioEvidenceError::Io {
                path: path.to_path_buf(),
                source: io::Error::last_os_error(),
            });
        }
    }
    Ok(())
}

fn sync_parent_dir(path: &Path) -> Result<(), AudioEvidenceError> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    sync_directory(parent)
}

fn sync_directory(path: &Path) -> Result<(), AudioEvidenceError> {
    let directory = File::open(path).map_err(|source| AudioEvidenceError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    directory
        .sync_all()
        .map_err(|source| AudioEvidenceError::Io {
            path: path.to_path_buf(),
            source,
        })
}

#[cfg(unix)]
fn lock_exclusive_nonblocking(file: &File, path: &Path) -> Result<(), AudioEvidenceError> {
    // SAFETY: flock reads a valid descriptor and retains no pointer.  The
    // writer thread owns this File for the evidence session's whole lifetime.
    let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc == 0 {
        Ok(())
    } else {
        Err(AudioEvidenceError::Busy {
            path: path.to_path_buf(),
            source: io::Error::last_os_error(),
        })
    }
}

#[cfg(not(unix))]
fn lock_exclusive_nonblocking(_file: &File, path: &Path) -> Result<(), AudioEvidenceError> {
    Err(AudioEvidenceError::Busy {
        path: path.to_path_buf(),
        source: io::Error::new(io::ErrorKind::Unsupported, "audio evidence requires flock"),
    })
}

fn invalid<T>(path: &Path, detail: impl Into<String>) -> Result<T, AudioEvidenceError> {
    Err(invalid_error(path, detail))
}

fn invalid_error(path: &Path, detail: impl Into<String>) -> AudioEvidenceError {
    AudioEvidenceError::InvalidEvidence {
        path: path.to_path_buf(),
        detail: detail.into(),
    }
}

fn artifact_error(path: &Path, detail: impl Into<String>) -> AudioEvidenceError {
    invalid_error(path, detail)
}
