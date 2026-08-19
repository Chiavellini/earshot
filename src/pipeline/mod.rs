//! Pipeline wiring (spec §4): dedicated OS threads for the hot path, a
//! current-thread tokio runtime driving the source, bounded SPSC rings
//! between stages. Each hot thread applies its `[perf]` pin/SCHED_FIFO at
//! spawn (src/perf.rs, docs/OS-TUNING.md); default config = no-op.
//!
//! Two entry points share the same stage code:
//! - `run_pipeline` — the single-source chain (source → preproc → chunker →
//!   detect+decide). The default; behavior unchanged.
//! - `run_pipeline_fanout` — N independent per-leg chains, each with its own
//!   `Detector`, OR-ed at the decision layer: every leg's detect thread calls
//!   `decide()` on ONE shared `DecisionTable` behind a mutex, so the one-shot
//!   per-keyword state machine is the dedupe point (first leg to hear a word
//!   wins the lock and fires; the loser gets `None`). A leg whose source errors
//!   dies alone (`died` in the report); siblings keep racing.

pub mod chunker;
pub mod preproc;

use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicU64;
use std::sync::{Arc, Mutex};

use crate::audio_evidence::{AudioEvidenceHealth, AudioEvidenceTap, AudioEvidenceTapError};
use crate::clock::MonotonicNs;
use crate::config::KeywordConfig;
use crate::decision::{DecisionHealth, DecisionJournalError, DecisionJournalLease, DecisionTable};
use crate::detector::Detector;
use crate::ring;
use crate::source::{AudioSource, SourceError};
use crate::telemetry::{
    DetectionRecord, DropCounters, LatencyRecorder, LegSummary, PipelineReport,
};
use crate::types::{AudioChunk, AudioFrame, DecisionIntent, Detection};

const FRAME_RING_CAP: usize = 256;
const CHUNK_RING_CAP: usize = 64;

/// Assembly-owned interlock before the one-shot decision table. It is
/// intentionally a read-only predicate on the hot path: phone supervision
/// owns the durable admission receipt, while the detector may continue to
/// record pre-admission evidence without consuming keyword state.
#[derive(Clone)]
pub struct DecisionAdmission {
    check: Arc<dyn Fn() -> bool + Send + Sync>,
    suppressed_reason: &'static str,
}

impl DecisionAdmission {
    pub fn always_open() -> Self {
        DecisionAdmission {
            check: Arc::new(|| true),
            suppressed_reason: "closed",
        }
    }

    pub fn new(
        check: impl Fn() -> bool + Send + Sync + 'static,
        suppressed_reason: &'static str,
    ) -> Self {
        DecisionAdmission {
            check: Arc::new(check),
            suppressed_reason,
        }
    }

    pub fn is_open(&self) -> bool {
        (self.check)()
    }
}

impl Default for DecisionAdmission {
    fn default() -> Self {
        Self::always_open()
    }
}

/// A pipeline failure, carrying whatever evidence the run had already gathered.
///
/// WHY (2026-08-07). `run_pipeline_fanout` computes `per_leg`, `frames_total`,
/// `chunks_total` and the drop counters for every leg, and then — when the last
/// leg dies — threw all of it away with a bare `return Err(e)`. That is the
/// mechanical reason AAOI-26AUG06's `report.json` carries no `frames_in` at
/// all: the run armed, heard 32 s of a dying stream, and its own summary was
/// discarded one line after being built, so the report degraded to
/// `{"partial_report": true}`. Every grader downstream then had to infer what
/// happened, and `post-event-report.sh` concluded it was fine.
///
/// A run that died is exactly the run whose frame counts you want. The failure
/// still propagates — `replay` and `bench` keep failing on `?` — but the
/// evidence now travels with it.
#[derive(Debug)]
pub struct PipelineFailure {
    pub error: SourceError,
    /// Per-leg frames/chunks/drops accounted before the failure. `Some` when a
    /// fanout run lost every leg; `None` when the failure preceded any
    /// accounting (a single source, or a build-time error).
    pub partial: Option<Box<PipelineReport>>,
    pub kind: PipelineFailureKind,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PipelineFailureKind {
    Startup,
    Source,
    DecisionJournal,
}

/// Accounting for one post-preprocessor evidence-forwarding door.
///
/// `observed_frames` is every post-preprocessor frame drained from this leg.
/// `published_frames` is the strict detector-visible prefix: a frame counts
/// only after evidence admission succeeds and the frame has entered the
/// evidence-to-chunker ring.  Once `error` is set, the stage continues to
/// drain upstream but increments only `discarded_after_failure`.
#[derive(Debug)]
pub struct AudioEvidenceLegReport {
    pub label: String,
    pub preprocessed_frames: u64,
    pub observed_frames: u64,
    pub published_frames: u64,
    pub discarded_after_failure: u64,
    pub error: Option<AudioEvidenceTapError>,
}

/// Consolidated audio-door accounting returned independently of the durable
/// bank's later `finish`/`abort` result.
#[derive(Debug)]
pub struct AudioEvidenceForwardReport {
    pub preprocessed_frames: u64,
    pub observed_frames: u64,
    pub published_frames: u64,
    pub discarded_after_failure: u64,
    pub per_leg: Vec<AudioEvidenceLegReport>,
}

impl AudioEvidenceForwardReport {
    pub fn is_healthy(&self) -> bool {
        self.per_leg.iter().all(|leg| leg.error.is_none())
            && self.preprocessed_frames == self.observed_frames
            && self.observed_frames == self.published_frames
            && self.discarded_after_failure == 0
    }

    pub fn first_error(&self) -> Option<&AudioEvidenceTapError> {
        self.per_leg.iter().find_map(|leg| leg.error.as_ref())
    }
}

/// Successful result from an explicitly audio-banked pipeline variant.
#[derive(Debug)]
pub struct PipelineWithAudioReport {
    pub pipeline: PipelineReport,
    pub audio_evidence: AudioEvidenceForwardReport,
}

/// Failure from an explicitly audio-banked pipeline variant.
///
/// Ordinary source/decision failures preserve their existing typed
/// `PipelineFailure`.  Evidence failure is a separate variant whose partial
/// pipeline report uses `published_frames` as `frames_in`, and whose per-leg
/// report retains the first typed `AudioEvidenceTapError`.  If both happened,
/// evidence remains the primary classification while the concurrent pipeline
/// failure is retained rather than discarded.
#[derive(Debug)]
pub enum PipelineWithAudioFailure {
    Pipeline {
        failure: PipelineFailure,
        audio_evidence: AudioEvidenceForwardReport,
    },
    AudioEvidence {
        partial: Box<PipelineReport>,
        audio_evidence: AudioEvidenceForwardReport,
        concurrent_pipeline_failure: Option<Box<PipelineFailure>>,
    },
}

impl PipelineWithAudioFailure {
    pub fn partial(&self) -> Option<&PipelineReport> {
        match self {
            Self::Pipeline { failure, .. } => failure.partial.as_deref(),
            Self::AudioEvidence { partial, .. } => Some(partial),
        }
    }

    pub fn audio_evidence(&self) -> &AudioEvidenceForwardReport {
        match self {
            Self::Pipeline { audio_evidence, .. } | Self::AudioEvidence { audio_evidence, .. } => {
                audio_evidence
            }
        }
    }
}

impl std::fmt::Display for PipelineWithAudioFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Pipeline { failure, .. } => write!(f, "{failure}"),
            Self::AudioEvidence { audio_evidence, .. } => match audio_evidence.first_error() {
                Some(error) => write!(f, "audio evidence forwarding failed: {error}"),
                None => f.write_str("audio evidence forwarding accounting is inconsistent"),
            },
        }
    }
}

impl std::error::Error for PipelineWithAudioFailure {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Pipeline { failure, .. } => Some(failure),
            Self::AudioEvidence { audio_evidence, .. } => audio_evidence
                .first_error()
                .map(|error| error as &(dyn std::error::Error + 'static)),
        }
    }
}

impl From<SourceError> for PipelineFailure {
    fn from(error: SourceError) -> Self {
        PipelineFailure {
            error,
            partial: None,
            kind: PipelineFailureKind::Source,
        }
    }
}

impl std::fmt::Display for PipelineFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Delegates, so existing `e.to_string()` callers (the ExitRecord detail
        // in main.rs::arm) read exactly as they did before.
        write!(f, "{}", self.error)
    }
}

impl std::error::Error for PipelineFailure {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.error)
    }
}

impl PipelineFailure {
    /// Assembly refused to release the source graph after spawning it.  This
    /// is distinct from a source that started and later failed: no audio was
    /// consumed and no decision intent could leave the graph.
    pub fn startup(detail: impl Into<String>) -> Self {
        PipelineFailure {
            error: SourceError::Protocol(format!("pipeline startup refused: {}", detail.into())),
            partial: None,
            kind: PipelineFailureKind::Startup,
        }
    }

    fn decision_journal(detail: String, mut report: PipelineReport) -> Self {
        report.decision_journal_error = Some(detail.clone());
        PipelineFailure {
            error: SourceError::Protocol(format!("decision journal failure: {detail}")),
            partial: Some(Box::new(report)),
            kind: PipelineFailureKind::DecisionJournal,
        }
    }
}

/// The assembly-owned result of selecting live durable decision state or an
/// ephemeral rehearsal table. The lease is deliberately separate from the
/// table so the composer can retain it through executor drain and report
/// persistence after the pipeline has dropped its table.
pub struct PreparedDecisionState {
    pub table: DecisionTable,
    pub health: DecisionHealth,
    pub lease: Option<DecisionJournalLease>,
    pub journal_path: Option<PathBuf>,
    /// Stable digest of the complete decision plan used both by the decision
    /// journal and the per-order evidence receipt.
    pub decision_plan_sha1: String,
}

#[derive(Debug, thiserror::Error)]
pub enum DecisionStateError {
    #[error("invalid live decision-state scope: {0}")]
    InvalidScope(String),
    #[error(transparent)]
    Journal(#[from] DecisionJournalError),
}

fn safe_scope_component(value: &str, label: &str) -> Result<(), DecisionStateError> {
    if value.is_empty()
        || !value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
    {
        return Err(DecisionStateError::InvalidScope(format!(
            "{label} {value:?} is not a non-empty path-safe identifier"
        )));
    }
    Ok(())
}

/// Derive the source-neutral Kalshi event scope from the market tickers. The
/// final ticker segment identifies the individual contract; every keyword in
/// one live decision table must share the same parent event ticker. Requiring
/// that invariant is safer than hashing the full market set: partially
/// overlapping configs then refuse as plan-incompatible instead of acquiring
/// independent locks and double-firing their shared markets.
pub fn canonical_market_event(keywords: &[KeywordConfig]) -> Result<&str, DecisionStateError> {
    let first = keywords.first().ok_or_else(|| {
        DecisionStateError::InvalidScope("live config has no keyword markets".into())
    })?;
    let (event, contract) = first.market_ticker.rsplit_once('-').ok_or_else(|| {
        DecisionStateError::InvalidScope(format!(
            "market ticker {:?} has no event/contract boundary",
            first.market_ticker
        ))
    })?;
    safe_scope_component(event, "market event")?;
    safe_scope_component(contract, "market contract")?;
    for keyword in &keywords[1..] {
        let Some((other_event, other_contract)) = keyword.market_ticker.rsplit_once('-') else {
            return Err(DecisionStateError::InvalidScope(format!(
                "market ticker {:?} has no event/contract boundary",
                keyword.market_ticker
            )));
        };
        safe_scope_component(other_contract, "market contract")?;
        if other_event != event {
            return Err(DecisionStateError::InvalidScope(format!(
                "keyword markets span event tickers {event:?} and {other_event:?}"
            )));
        }
    }
    Ok(event)
}

/// Canonical source-neutral journal path. Operational state directories are
/// lane-specific siblings (`...-PHONE`, `...-WEBCAST`); their parent is the
/// state root, where one env+market-event journal arbitrates every lane.
pub fn decision_journal_path(
    state_dir: &Path,
    kalshi_env: &str,
    keywords: &[KeywordConfig],
) -> Result<PathBuf, DecisionStateError> {
    safe_scope_component(kalshi_env, "Kalshi environment")?;
    let event = canonical_market_event(keywords)?;
    let root = state_dir
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty() && *parent != Path::new("/"))
        .ok_or_else(|| {
            DecisionStateError::InvalidScope(format!(
                "state dir {} must be an event-specific child of a state root",
                state_dir.display()
            ))
        })?;
    Ok(root.join(format!("decision-{kalshi_env}-{event}.jsonl")))
}

pub fn prepare_decision_state(
    keywords: &[KeywordConfig],
    kalshi_env: &str,
    state_dir: &Path,
    live: bool,
) -> Result<PreparedDecisionState, DecisionStateError> {
    let plan_path = state_dir.join("decision-plan.digest");
    let decision_plan_sha1 = crate::decision::persistent_plan_sha1(keywords, &plan_path)?;
    if !live {
        let table = DecisionTable::from_keywords(keywords);
        return Ok(PreparedDecisionState {
            health: table.health(),
            table,
            lease: None,
            journal_path: None,
            decision_plan_sha1,
        });
    }
    let path = decision_journal_path(state_dir, kalshi_env, keywords)?;
    let table = DecisionTable::from_keywords_with_journal(keywords, &path)?;
    let health = table.health();
    let lease = table.journal_lease()?;
    Ok(PreparedDecisionState {
        table,
        health,
        lease,
        journal_path: Some(path),
        decision_plan_sha1,
    })
}

/// One audio leg of a fanout pipeline: its own source and its own detector
/// instance (model memory is deliberately duplicated per leg — sharing one
/// sherpa recognizer across threads via the FFI is an unforced risk for a few
/// MB of savings; see docs/layers/detector.md).
pub struct PipelineLeg {
    /// Unique display label (config `source_label`); stamps every
    /// `DetectionRecord.leg` and the per-leg report row.
    pub label: String,
    pub source: Box<dyn AudioSource>,
    pub detector: Box<dyn Detector>,
}

/// What a detect thread returns at teardown.
struct DetectOut {
    recorder: LatencyRecorder,
    records: Vec<DetectionRecord>,
    chunks: u64,
    first_capture_ts: Option<MonotonicNs>,
}

struct AudioEvidenceForwardOut {
    observed_frames: u64,
    published_frames: u64,
    discarded_after_failure: u64,
    error: Option<AudioEvidenceTapError>,
}

fn poisoned_audio_evidence_error(health: &AudioEvidenceHealth) -> Option<AudioEvidenceTapError> {
    if let Some(detail) = &health.writer_error {
        return Some(AudioEvidenceTapError::WriterFailed(detail.clone()));
    }
    if health.dropped_frames != 0 {
        return Some(AudioEvidenceTapError::Full);
    }
    if health.closed_attempts != 0 || !health.accepting {
        return Some(AudioEvidenceTapError::Closed);
    }
    if health.invalid_attempts != 0 {
        return Some(AudioEvidenceTapError::InvalidFrame(format!(
            "shared audio evidence bank observed {} invalid frame attempt(s)",
            health.invalid_attempts
        )));
    }
    None
}

/// Evidence admission is deliberately a separate optional assembly stage so
/// the preprocessor algorithm and all legacy/no-evidence graphs remain
/// unchanged.  The stage receives only post-preprocessor frames.  It banks a
/// frame first, publishes it to the chunker second, and counts it only after
/// that publish returns.  On the first local or shared tap fault it forwards
/// nothing else, but drains the preprocessor ring to closure.
fn run_audio_evidence_forwarder(
    mut rx: ring::RingReceiver<AudioFrame>,
    mut tx: ring::RingSender<AudioFrame>,
    tap: AudioEvidenceTap,
) -> AudioEvidenceForwardOut {
    let mut observed_frames = 0u64;
    let mut published_frames = 0u64;
    let mut discarded_after_failure = 0u64;
    let mut error = None;

    while let Some(frame) = rx.recv() {
        observed_frames += 1;
        if error.is_some() {
            discarded_after_failure += 1;
            continue;
        }

        let admission = poisoned_audio_evidence_error(&tap.health())
            .map_or_else(|| tap.try_record_accepted(&frame), Err);
        match admission {
            Ok(()) => {
                tx.send_blocking(frame);
                published_frames += 1;
            }
            Err(first_error) => {
                error = Some(first_error);
                discarded_after_failure += 1;
            }
        }
    }

    // Catch an asynchronous writer failure that arrived after the final frame
    // admission.  The durable bank remains the ultimate completion authority,
    // but a fault already visible at stage teardown must not be returned as a
    // healthy pipeline door.
    if error.is_none() {
        error = poisoned_audio_evidence_error(&tap.health());
    }

    AudioEvidenceForwardOut {
        observed_frames,
        published_frames,
        discarded_after_failure,
        error,
    }
}

/// Handles for one leg's spawned stage threads plus its drop counters.
struct StageHandles {
    preproc: std::thread::JoinHandle<u64>,
    audio_evidence: Option<std::thread::JoinHandle<AudioEvidenceForwardOut>>,
    chunker: std::thread::JoinHandle<u64>,
    detect: std::thread::JoinHandle<DetectOut>,
    src_drops: Arc<AtomicU64>,
    preproc_drops: Arc<AtomicU64>,
    chunker_drops: Arc<AtomicU64>,
}

/// Spawn the preproc → chunker → detect threads for one chain and return the
/// source-side ring sender. `suffix` is empty for the single-source pipeline
/// (thread names stay "preproc"/"chunker"/"detect") and "-<i>" per fanout leg.
#[allow(clippy::too_many_arguments)]
fn spawn_stages(
    suffix: &str,
    chunk_ms: u32,
    preproc_cfg: preproc::PreprocCfg,
    detector: Box<dyn Detector>,
    admission: DecisionAdmission,
    decide: impl FnMut(&Detection) -> Result<Option<DecisionIntent>, DecisionJournalError>
        + Send
        + 'static,
    intent_tx: Option<tokio::sync::mpsc::Sender<DecisionIntent>>,
    perf: crate::perf::PerfSection,
    leg_label: String,
    audio_evidence_tap: Option<AudioEvidenceTap>,
    verifier: Option<crate::verifier::VerifierClient>,
) -> (ring::RingSender<AudioFrame>, StageHandles) {
    let (src_tx, mut preproc_rx) = ring::ring::<AudioFrame>(FRAME_RING_CAP);
    let (preproc_tx, post_preproc_rx) = ring::ring::<AudioFrame>(FRAME_RING_CAP);
    let (mut chunker_rx, audio_evidence) = match audio_evidence_tap {
        Some(tap) => {
            let (evidence_tx, chunker_rx) = ring::ring::<AudioFrame>(FRAME_RING_CAP);
            let handle = std::thread::Builder::new()
                .name(format!("audio-evidence-forward{suffix}"))
                .spawn(move || run_audio_evidence_forwarder(post_preproc_rx, evidence_tx, tap))
                .expect("spawn audio evidence forwarder");
            (chunker_rx, Some(handle))
        }
        None => (post_preproc_rx, None),
    };
    let (chunker_tx, mut detect_rx) = ring::ring::<AudioChunk>(CHUNK_RING_CAP);

    // RT boxes: hot consumers busy-spin instead of park/unpark (~100 ns
    // reaction). Config validation guarantees realtime + distinct cores
    // (and refuses [perf] tuning entirely in fanout mode).
    if perf.spin_hot_rings {
        preproc_rx.set_spin(true);
        chunker_rx.set_spin(true);
        detect_rx.set_spin(true);
    }

    let src_drops = src_tx.drops_handle();
    let preproc_drops = preproc_tx.drops_handle();
    let chunker_drops = chunker_tx.drops_handle();

    let preproc = std::thread::Builder::new()
        .name(format!("preproc{suffix}"))
        .spawn(move || {
            crate::perf::apply_to_current_thread(
                "preproc",
                perf.core_preproc,
                perf.fifo_priority(),
            );
            preproc::run(preproc_rx, preproc_tx, preproc_cfg)
        })
        .expect("spawn preproc");

    let chunker = std::thread::Builder::new()
        .name(format!("chunker{suffix}"))
        .spawn(move || {
            crate::perf::apply_to_current_thread(
                "chunker",
                perf.core_chunker,
                perf.fifo_priority(),
            );
            chunker::run(chunker_rx, chunker_tx, chunk_ms)
        })
        .expect("spawn chunker");

    // Detector + decision share a thread: decision is O(µs), a fourth thread
    // buys nothing (spec §4). This thread owns the latency recorder.
    let detect = std::thread::Builder::new()
        .name(format!("detect{suffix}"))
        .spawn(move || {
            crate::perf::apply_to_current_thread("detect", perf.core_detect, perf.fifo_priority());
            detect_loop(
                detect_rx, detector, admission, decide, intent_tx, leg_label, verifier,
            )
        })
        .expect("spawn detect");

    (
        src_tx,
        StageHandles {
            preproc,
            audio_evidence,
            chunker,
            detect,
            src_drops,
            preproc_drops,
            chunker_drops,
        },
    )
}

/// The detect+decide stage, shared by both entry points. `decide` abstracts
/// table ownership: the single-source pipeline captures its owned
/// `DecisionTable` (no lock anywhere); fanout legs each capture a clone of the
/// shared `Arc<Mutex<DecisionTable>>` and lock through the durable eligible
/// transition. A journal failure is terminal for decisions: the stage keeps
/// draining so upstream threads cannot deadlock, but performs no more detector
/// or decision work and the pipeline returns a structured failure.
#[allow(clippy::too_many_arguments)]
fn detect_loop(
    mut detect_rx: ring::RingReceiver<AudioChunk>,
    mut detector: Box<dyn Detector>,
    admission: DecisionAdmission,
    mut decide: impl FnMut(&Detection) -> Result<Option<DecisionIntent>, DecisionJournalError>,
    intent_tx: Option<tokio::sync::mpsc::Sender<DecisionIntent>>,
    leg_label: String,
    verifier: Option<crate::verifier::VerifierClient>,
) -> DetectOut {
    let mut recorder = LatencyRecorder::new();
    let mut records: Vec<DetectionRecord> = Vec::new();
    let mut chunks = 0u64;
    let mut first_capture_ts = None;
    let mut decision_failed = false;
    // Verifier tail: the last `tail_ms` of this leg's PCM, snapshotted per
    // request. Sized on the first chunk (sample rate known then).
    let mut tail: Option<crate::verifier::PcmTail> = None;
    while let Some(chunk) = detect_rx.recv() {
        if chunk.seq == 0 {
            first_capture_ts = Some(chunk.first_capture_ts);
        }
        chunks += 1;
        // Detect-hop park wake cost (None when the chunk was already
        // waiting, or in spin mode). The other hops aren't recorded —
        // this hop is the representative one and owns the recorder.
        if let Some(wake_ns) = detect_rx.take_last_wake_ns() {
            recorder.record("ring_wake", wake_ns);
        }
        if decision_failed {
            // Continue draining the bounded rings so source/preproc/chunker
            // threads can finish. The composer sees the shared health signal
            // immediately and kills the risk gate; this stage never attempts
            // another detector or decision after durable state becomes
            // unknowable.
            continue;
        }
        let detections = detector.process(&chunk);
        let after = MonotonicNs::now();

        // FIRE FIRST: decide + hand off before any telemetry or
        // logging — nothing but the trade sits on the critical span.
        // Edge values are recorded (and intents logged) below, after
        // every intent of this chunk has left the thread; the
        // timestamps are captured here so the deferred bookkeeping
        // measures the same instants.
        let mut decided: smallvec::SmallVec<
            [(
                Detection,
                Option<DecisionIntent>,
                MonotonicNs,
                bool,
                bool,
                Option<&'static str>,
            ); 2],
        > = smallvec::SmallVec::new();
        for det in detections {
            if !admission.is_open() {
                // Crucially, do not call `decide`: suppression must not append
                // to the durable one-shot journal or consume keyword state.
                decided.push((
                    det,
                    None,
                    MonotonicNs::now(),
                    false,
                    false,
                    Some(admission.suppressed_reason),
                ));
                continue;
            }
            let intent = match decide(&det) {
                Ok(intent) => intent,
                Err(error) => {
                    tracing::error!(
                        error = %error,
                        leg = %leg_label,
                        "decision journal failure — decision stage disabled"
                    );
                    decision_failed = true;
                    decided.push((det, None, MonotonicNs::now(), false, true, None));
                    break;
                }
            };
            let decision_ts = intent
                .as_ref()
                .map(|i| i.decision_ts)
                .unwrap_or_else(MonotonicNs::now);
            let mut dropped = false;
            if let (Some(i), Some(tx)) = (&intent, &intent_tx) {
                // Stamp the handoff instant on the sent clone so the
                // executor can isolate the mpsc wake hop
                // (handoff→exec) from this thread's tail.
                let mut sent = i.clone();
                sent.handoff_ts = MonotonicNs::now();
                dropped = tx.try_send(sent).is_err();
            }
            decided.push((det, intent, decision_ts, dropped, false, None));
        }

        // Verifier (D2): AFTER every intent of this chunk has left the thread.
        // The tail INCLUDES this chunk (the word just completed in it).
        if let Some(v) = verifier.as_ref() {
            let t = tail.get_or_insert_with(|| {
                crate::verifier::PcmTail::new(v.tail_ms(), chunk.sample_rate_hz)
            });
            t.push(&chunk.pcm);
            for (det, intent, decision_ts, _dropped, journal_failed, suppression) in decided.iter()
            {
                if *journal_failed || suppression.is_some() {
                    continue;
                }
                let intent_bearing = intent.is_some();
                if !v.wants(det.tier, intent_bearing) {
                    continue;
                }
                let posted = v.request(crate::verifier::VerifyRequest {
                    keyword: det.keyword,
                    tier: det.tier,
                    intent_bearing,
                    source: chunk.source,
                    leg: leg_label.clone(),
                    audio_pos_ms: det.audio_pos_ms,
                    audio_consumed_ms: det.audio_consumed_ms,
                    pcm: t.snapshot(),
                    sample_rate_hz: chunk.sample_rate_hz,
                    decision_ts: intent.as_ref().map(|_| *decision_ts),
                });
                if posted.is_err() {
                    tracing::warn!(
                        keyword = det.keyword.0,
                        leg = %leg_label,
                        intent_bearing,
                        "verifier queue full — request dropped (executor will not wait)"
                    );
                }
            }
        }

        // Off the critical span: per-chunk edges, then per-detection
        // edges, logs, and records.
        recorder.record(
            "capture→preproc",
            chunk
                .first_preproc_ts
                .saturating_since(chunk.first_capture_ts),
        );
        recorder.record(
            "preproc→chunk",
            chunk.chunk_ts.saturating_since(chunk.first_preproc_ts),
        );
        recorder.record("chunk→detect", after.saturating_since(chunk.chunk_ts));

        for (det, intent, decision_ts, dropped, journal_failed, suppression) in decided {
            recorder.record("detect→decision", decision_ts.saturating_since(det.emit_ts));
            recorder.record(
                "capture→decision",
                decision_ts.saturating_since(det.audio_ts),
            );
            if let Some(reason) = suppression {
                tracing::warn!(
                    keyword = det.keyword.0,
                    tier = ?det.tier,
                    leg = %leg_label,
                    %reason,
                    "detection suppressed before one-shot decision table"
                );
            } else if journal_failed {
                tracing::error!(
                    keyword = det.keyword.0,
                    tier = ?det.tier,
                    leg = %leg_label,
                    "detection refused because decision durability failed"
                );
            } else {
                match &intent {
                    Some(i) => {
                        tracing::info!(
                            keyword = det.keyword.0,
                            ticker = %i.market_ticker,
                            tranche = i.tranche_contracts,
                            tier = ?det.tier,
                            leg = %leg_label,
                            "decision intent"
                        );
                        if dropped {
                            tracing::error!(
                                keyword = det.keyword.0,
                                "intent channel full/closed: intent DROPPED"
                            );
                        }
                    }
                    None => tracing::info!(
                        keyword = det.keyword.0,
                        tier = ?det.tier,
                        leg = %leg_label,
                        "detection ignored (keyword state machine)"
                    ),
                }
            }
            records.push(DetectionRecord {
                detect_latency_us: det.emit_ts.saturating_since(det.audio_ts) / 1000,
                detection: det,
                intent,
                suppression: suppression.map(str::to_string),
                leg: leg_label.clone(),
            });
        }
    }
    DetectOut {
        recorder,
        records,
        chunks,
        first_capture_ts,
    }
}

fn join_audio_evidence_stage(
    label: String,
    preprocessed_frames: u64,
    handle: Option<std::thread::JoinHandle<AudioEvidenceForwardOut>>,
) -> (u64, Option<AudioEvidenceLegReport>) {
    let Some(handle) = handle else {
        return (preprocessed_frames, None);
    };
    let mut out = handle
        .join()
        .expect("audio evidence forwarder thread panicked");
    if out.error.is_none() && out.observed_frames != preprocessed_frames {
        out.error = Some(AudioEvidenceTapError::InvalidFrame(format!(
            "assembly count mismatch: preprocessor published {preprocessed_frames} frame(s), \
             evidence stage observed {}",
            out.observed_frames
        )));
    }
    let published_frames = out.published_frames;
    (
        published_frames,
        Some(AudioEvidenceLegReport {
            label,
            preprocessed_frames,
            observed_frames: out.observed_frames,
            published_frames,
            discarded_after_failure: out.discarded_after_failure,
            error: out.error,
        }),
    )
}

fn consolidate_audio_evidence(per_leg: Vec<AudioEvidenceLegReport>) -> AudioEvidenceForwardReport {
    AudioEvidenceForwardReport {
        preprocessed_frames: per_leg.iter().map(|leg| leg.preprocessed_frames).sum(),
        observed_frames: per_leg.iter().map(|leg| leg.observed_frames).sum(),
        published_frames: per_leg.iter().map(|leg| leg.published_frames).sum(),
        discarded_after_failure: per_leg.iter().map(|leg| leg.discarded_after_failure).sum(),
        per_leg,
    }
}

pub struct PipelineAssemblyOutcome {
    pub result: Result<PipelineReport, PipelineFailure>,
    pub audio_evidence: Option<AudioEvidenceForwardReport>,
}

fn require_audio_evidence(
    outcome: PipelineAssemblyOutcome,
) -> Result<PipelineWithAudioReport, PipelineWithAudioFailure> {
    let audio_evidence = outcome
        .audio_evidence
        .expect("with-audio composer omitted its evidence stage report");
    match outcome.result {
        Ok(pipeline) if audio_evidence.is_healthy() => Ok(PipelineWithAudioReport {
            pipeline,
            audio_evidence,
        }),
        Ok(partial) => Err(PipelineWithAudioFailure::AudioEvidence {
            partial: Box::new(partial),
            audio_evidence,
            concurrent_pipeline_failure: None,
        }),
        Err(failure) if audio_evidence.is_healthy() => Err(PipelineWithAudioFailure::Pipeline {
            failure,
            audio_evidence,
        }),
        Err(failure) => {
            let partial = failure
                .partial
                .clone()
                .expect("post-run audio failure omitted partial pipeline report");
            Err(PipelineWithAudioFailure::AudioEvidence {
                partial,
                audio_evidence,
                concurrent_pipeline_failure: Some(Box::new(failure)),
            })
        }
    }
}

/// `intent_tx`: the decision stage's exit door toward the executor (M2).
/// `None` = log-only (M0 replay mode). `try_send` from the sync detect
/// thread never blocks; a full channel is a counted, loud drop.
///
/// `perf`: OS/RT tuning plan ([perf] config). Each hot thread applies its own
/// pin/priority at spawn; `PerfSection::default()` is a guaranteed no-op.
pub fn run_pipeline(
    event_id: &str,
    chunk_ms: u32,
    preproc_cfg: preproc::PreprocCfg,
    source: Box<dyn AudioSource>,
    detector: Box<dyn Detector>,
    table: DecisionTable,
    intent_tx: Option<tokio::sync::mpsc::Sender<crate::types::DecisionIntent>>,
    perf: crate::perf::PerfSection,
) -> Result<PipelineReport, PipelineFailure> {
    run_pipeline_admitted(
        event_id,
        chunk_ms,
        preproc_cfg,
        source,
        detector,
        table,
        intent_tx,
        perf,
        DecisionAdmission::always_open(),
    )
}

/// Assembly variant with a live admission interlock. Existing replay/bench
/// callers use `run_pipeline`, whose door is permanently open.
#[allow(clippy::too_many_arguments)]
pub fn run_pipeline_admitted(
    event_id: &str,
    chunk_ms: u32,
    preproc_cfg: preproc::PreprocCfg,
    source: Box<dyn AudioSource>,
    detector: Box<dyn Detector>,
    table: DecisionTable,
    intent_tx: Option<tokio::sync::mpsc::Sender<crate::types::DecisionIntent>>,
    perf: crate::perf::PerfSection,
    admission: DecisionAdmission,
) -> Result<PipelineReport, PipelineFailure> {
    run_pipeline_admitted_inner(
        event_id,
        chunk_ms,
        preproc_cfg,
        source,
        detector,
        table,
        intent_tx,
        perf,
        admission,
        None,
        None,
    )
    .result
}

/// Admission-aware single-source composer whose detector-visible audio is
/// durably banked through the post-preprocessor evidence door.
///
/// The returned `audio_evidence.published_frames` is the exact count the
/// caller must pass to `AudioEvidenceBank::finish`.  On any evidence-stage
/// error, the returned partial `PipelineReport.frames_in` is the successfully
/// banked and detector-visible prefix, never the larger drained input count.
#[allow(clippy::too_many_arguments)]
pub fn run_pipeline_admitted_with_audio(
    event_id: &str,
    chunk_ms: u32,
    preproc_cfg: preproc::PreprocCfg,
    source: Box<dyn AudioSource>,
    detector: Box<dyn Detector>,
    table: DecisionTable,
    intent_tx: Option<tokio::sync::mpsc::Sender<crate::types::DecisionIntent>>,
    perf: crate::perf::PerfSection,
    admission: DecisionAdmission,
    audio_evidence_tap: AudioEvidenceTap,
) -> Result<PipelineWithAudioReport, PipelineWithAudioFailure> {
    require_audio_evidence(run_pipeline_admitted_inner(
        event_id,
        chunk_ms,
        preproc_cfg,
        source,
        detector,
        table,
        intent_tx,
        perf,
        admission,
        Some(audio_evidence_tap),
        None,
    ))
}

/// Optional stage hooks for the live arm composer: the durable audio-evidence
/// tap and the fill-path verifier client (detection-accuracy program D2). Bench
/// and replay callers use the plain entry points, which pass none.
#[derive(Default)]
pub struct StageHooks {
    pub audio_evidence_tap: Option<AudioEvidenceTap>,
    pub verifier: Option<crate::verifier::VerifierClient>,
}

/// Single-source composer with hooks. With an audio tap the outcome is the
/// audio-evidence-checked shape (`require_audio_evidence`); without one it is
/// the plain result. Callers that need the typed audio report should keep
/// using `run_pipeline_admitted_with_audio` unless they also need a verifier.
#[allow(clippy::too_many_arguments)]
pub fn run_pipeline_admitted_with_hooks(
    event_id: &str,
    chunk_ms: u32,
    preproc_cfg: preproc::PreprocCfg,
    source: Box<dyn AudioSource>,
    detector: Box<dyn Detector>,
    table: DecisionTable,
    intent_tx: Option<tokio::sync::mpsc::Sender<crate::types::DecisionIntent>>,
    perf: crate::perf::PerfSection,
    admission: DecisionAdmission,
    hooks: StageHooks,
) -> PipelineAssemblyOutcome {
    run_pipeline_admitted_inner(
        event_id,
        chunk_ms,
        preproc_cfg,
        source,
        detector,
        table,
        intent_tx,
        perf,
        admission,
        hooks.audio_evidence_tap,
        hooks.verifier,
    )
}

/// Fanout composer with hooks (see `run_pipeline_admitted_with_hooks`).
#[allow(clippy::too_many_arguments)]
pub fn run_pipeline_fanout_admitted_with_hooks(
    event_id: &str,
    chunk_ms: u32,
    preproc_cfg: preproc::PreprocCfg,
    legs: Vec<PipelineLeg>,
    table: DecisionTable,
    intent_tx: Option<tokio::sync::mpsc::Sender<DecisionIntent>>,
    perf: crate::perf::PerfSection,
    admission: DecisionAdmission,
    hooks: StageHooks,
) -> PipelineAssemblyOutcome {
    run_pipeline_fanout_admitted_inner(
        event_id,
        chunk_ms,
        preproc_cfg,
        legs,
        table,
        intent_tx,
        perf,
        admission,
        hooks.audio_evidence_tap,
        hooks.verifier,
    )
}

/// The audio-evidence-checked view of a hooks outcome — same contract as
/// `run_pipeline_admitted_with_audio` (`published_frames` is the exact count
/// for `AudioEvidenceBank::finish`).
pub fn require_audio_evidence_outcome(
    outcome: PipelineAssemblyOutcome,
) -> Result<PipelineWithAudioReport, PipelineWithAudioFailure> {
    require_audio_evidence(outcome)
}

#[allow(clippy::too_many_arguments)]
fn run_pipeline_admitted_inner(
    event_id: &str,
    chunk_ms: u32,
    preproc_cfg: preproc::PreprocCfg,
    source: Box<dyn AudioSource>,
    detector: Box<dyn Detector>,
    mut table: DecisionTable,
    intent_tx: Option<tokio::sync::mpsc::Sender<crate::types::DecisionIntent>>,
    perf: crate::perf::PerfSection,
    admission: DecisionAdmission,
    audio_evidence_tap: Option<AudioEvidenceTap>,
    verifier: Option<crate::verifier::VerifierClient>,
) -> PipelineAssemblyOutcome {
    // Read before the detector is moved into its thread.
    let engine = detector.engine_name();
    let decision_health = table.health();
    let (src_tx, stages) = spawn_stages(
        "",
        chunk_ms,
        preproc_cfg,
        detector,
        admission,
        move |det| table.decide_checked(det),
        intent_tx,
        perf,
        String::new(), // single-source records carry no leg label
        audio_evidence_tap,
        verifier,
    );

    // Drive the source on a current-thread runtime. IO is enabled because
    // network sources (Twilio WS) run here too, not just file replay.
    // Capture timestamps originate on this thread — pin it too when asked.
    crate::perf::apply_to_current_thread("source", perf.core_source, perf.fifo_priority());
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("tokio runtime");
    let source_outcome = rt.block_on(source.run(src_tx));
    match &source_outcome {
        Ok(exit) => tracing::info!(?exit, "source finished"),
        Err(error) => tracing::error!(%error, "source failed; draining stages for evidence"),
    }

    let preprocessed_frames = stages.preproc.join().expect("preproc thread panicked");
    let (frames_in, audio_evidence) =
        join_audio_evidence_stage(String::new(), preprocessed_frames, stages.audio_evidence);
    let chunks_from_chunker = stages.chunker.join().expect("chunker thread panicked");
    let out = stages.detect.join().expect("detect thread panicked");
    debug_assert_eq!(chunks_from_chunker, out.chunks);

    use std::sync::atomic::Ordering;
    let drops = DropCounters {
        source_to_preproc: stages.src_drops.load(Ordering::Relaxed),
        preproc_to_chunker: stages.preproc_drops.load(Ordering::Relaxed),
        chunker_to_detector: stages.chunker_drops.load(Ordering::Relaxed),
    };
    let report = PipelineReport {
        event_id: event_id.to_string(),
        chunk_ms,
        engine: Some(engine.to_string()),
        frames_in,
        chunks_in: out.chunks,
        first_capture_ts: out.first_capture_ts,
        edges: out.recorder.snapshot(),
        detections: out.records,
        drops: drops.clone(),
        drop_pct: drops.pct_of(frames_in),
        per_leg: None,
        decision_journal_error: None,
    };
    let result = if let Some(detail) = decision_health.fault_detail() {
        Err(PipelineFailure::decision_journal(detail, report))
    } else {
        match source_outcome {
            Ok(_) => Ok(report),
            Err(error) => Err(PipelineFailure {
                error,
                partial: Some(Box::new(report)),
                kind: PipelineFailureKind::Source,
            }),
        }
    };
    PipelineAssemblyOutcome {
        result,
        audio_evidence: audio_evidence.map(|leg| consolidate_audio_evidence(vec![leg])),
    }
}

/// Fanout mode: run every leg's full chain concurrently against ONE decision
/// table. Config validation guarantees ≥ 2 legs, unique labels, and a default
/// `[perf]` section (per-core pin maps are single-chain; refusing beats
/// silently stacking N spinning threads on one core).
///
/// Failure policy: a leg's source error is leg-local — logged loud, recorded
/// as `per_leg[i].died`, siblings keep running; the pipeline returns when all
/// legs have ended. Only if EVERY leg died with a source error does the whole
/// pipeline return that error (the first one).
pub fn run_pipeline_fanout(
    event_id: &str,
    chunk_ms: u32,
    preproc_cfg: preproc::PreprocCfg,
    legs: Vec<PipelineLeg>,
    table: DecisionTable,
    intent_tx: Option<tokio::sync::mpsc::Sender<DecisionIntent>>,
    perf: crate::perf::PerfSection,
) -> Result<PipelineReport, PipelineFailure> {
    run_pipeline_fanout_admitted(
        event_id,
        chunk_ms,
        preproc_cfg,
        legs,
        table,
        intent_tx,
        perf,
        DecisionAdmission::always_open(),
    )
}

#[allow(clippy::too_many_arguments)]
pub fn run_pipeline_fanout_admitted(
    event_id: &str,
    chunk_ms: u32,
    preproc_cfg: preproc::PreprocCfg,
    legs: Vec<PipelineLeg>,
    table: DecisionTable,
    intent_tx: Option<tokio::sync::mpsc::Sender<DecisionIntent>>,
    perf: crate::perf::PerfSection,
    admission: DecisionAdmission,
) -> Result<PipelineReport, PipelineFailure> {
    run_pipeline_fanout_admitted_inner(
        event_id,
        chunk_ms,
        preproc_cfg,
        legs,
        table,
        intent_tx,
        perf,
        admission,
        None,
        None,
    )
    .result
}

/// Admission-aware fanout composer with one shared run-scoped audio bank.
/// Each leg gets a cloned tap and its own post-preprocessor forwarding stage;
/// the composer drops the caller tap immediately after spawning the legs so a
/// clean return leaves no surviving producer.  `published_frames` is summed
/// across legs and is the exact value for `AudioEvidenceBank::finish`.
#[allow(clippy::too_many_arguments)]
pub fn run_pipeline_fanout_admitted_with_audio(
    event_id: &str,
    chunk_ms: u32,
    preproc_cfg: preproc::PreprocCfg,
    legs: Vec<PipelineLeg>,
    table: DecisionTable,
    intent_tx: Option<tokio::sync::mpsc::Sender<DecisionIntent>>,
    perf: crate::perf::PerfSection,
    admission: DecisionAdmission,
    audio_evidence_tap: AudioEvidenceTap,
) -> Result<PipelineWithAudioReport, PipelineWithAudioFailure> {
    require_audio_evidence(run_pipeline_fanout_admitted_inner(
        event_id,
        chunk_ms,
        preproc_cfg,
        legs,
        table,
        intent_tx,
        perf,
        admission,
        Some(audio_evidence_tap),
        None,
    ))
}

#[allow(clippy::too_many_arguments)]
fn run_pipeline_fanout_admitted_inner(
    event_id: &str,
    chunk_ms: u32,
    preproc_cfg: preproc::PreprocCfg,
    legs: Vec<PipelineLeg>,
    table: DecisionTable,
    intent_tx: Option<tokio::sync::mpsc::Sender<DecisionIntent>>,
    perf: crate::perf::PerfSection,
    admission: DecisionAdmission,
    audio_evidence_tap: Option<AudioEvidenceTap>,
    verifier: Option<crate::verifier::VerifierClient>,
) -> PipelineAssemblyOutcome {
    assert!(
        legs.len() >= 2,
        "fanout requires >= 2 legs (config validation)"
    );
    let audio_evidence_enabled = audio_evidence_tap.is_some();
    let decision_health = table.health();
    let shared = Arc::new(Mutex::new(table));

    struct LegRun {
        label: String,
        stages: StageHandles,
        source_thread: std::thread::JoinHandle<Result<crate::source::SourceExit, SourceError>>,
    }

    let mut runs = Vec::with_capacity(legs.len());
    // Legs each carry their own detector. In every shipped fanout config they
    // are the same engine, but nothing enforces that, so report the distinct
    // set rather than asserting one — a mixed run must not be recorded as if
    // one engine produced every detection.
    let mut engines: Vec<&'static str> = Vec::new();
    for (i, leg) in legs.into_iter().enumerate() {
        let shared = shared.clone();
        let admission = admission.clone();
        let leg_audio_evidence_tap = audio_evidence_tap.as_ref().cloned();
        let e = leg.detector.engine_name();
        if !engines.contains(&e) {
            engines.push(e);
        }
        let (src_tx, stages) = spawn_stages(
            &format!("-{i}"),
            chunk_ms,
            preproc_cfg,
            leg.detector,
            admission,
            move |det| {
                shared
                    .lock()
                    .expect("decision table poisoned")
                    .decide_checked(det)
            },
            intent_tx.clone(),
            perf,
            leg.label.clone(),
            leg_audio_evidence_tap,
            verifier.clone(),
        );
        let source = leg.source;
        let label = leg.label.clone();
        let source_thread = std::thread::Builder::new()
            .name(format!("source-{i}"))
            .spawn(move || {
                let rt = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .expect("tokio runtime");
                let out = rt.block_on(source.run(src_tx));
                match &out {
                    Ok(exit) => tracing::info!(leg = %label, ?exit, "leg source finished"),
                    Err(e) => {
                        tracing::error!(leg = %label, error = %e, "LEG DIED — siblings continue")
                    }
                }
                out
            })
            .expect("spawn leg source");
        runs.push(LegRun {
            label: leg.label,
            stages,
            source_thread,
        });
    }
    // `start_new` returns one caller-owned producer. Fanout clones it once per
    // leg; this explicit drop is what lets a clean bank later prove producer
    // quiescence after all forwarding threads join.
    drop(audio_evidence_tap);
    // Same for the verifier client: legs hold clones; ours must not keep the
    // verifier's request queue open past the last detect thread.
    drop(verifier);
    // The per-leg clones own the channel now; drop ours so the executor sees
    // all senders gone once every leg's detect thread ends.
    drop(intent_tx);

    // Join everything. Source-thread join order doesn't matter: each leg's
    // chain drains independently once its source ends (rings close on drop).
    let mut recorder = LatencyRecorder::new();
    let mut records: Vec<DetectionRecord> = Vec::new();
    let mut per_leg = Vec::with_capacity(runs.len());
    let mut frames_total = 0u64;
    let mut chunks_total = 0u64;
    let mut first_capture_ts: Option<MonotonicNs> = None;
    let mut drops_total = DropCounters {
        source_to_preproc: 0,
        preproc_to_chunker: 0,
        chunker_to_detector: 0,
    };
    let mut first_err: Option<SourceError> = None;
    let mut died_count = 0usize;
    let mut audio_evidence_legs = Vec::with_capacity(runs.len());
    let n_legs = runs.len();

    use std::sync::atomic::Ordering;
    for run in runs {
        let source_result = run
            .source_thread
            .join()
            .expect("leg source thread panicked");
        let preprocessed_frames = run.stages.preproc.join().expect("preproc thread panicked");
        let (frames, audio_evidence_leg) = join_audio_evidence_stage(
            run.label.clone(),
            preprocessed_frames,
            run.stages.audio_evidence,
        );
        if let Some(audio_evidence_leg) = audio_evidence_leg {
            audio_evidence_legs.push(audio_evidence_leg);
        }
        let _chunker = run.stages.chunker.join().expect("chunker thread panicked");
        let out = run.stages.detect.join().expect("detect thread panicked");

        let died = match source_result {
            Ok(_) => None,
            Err(e) => {
                let msg = e.to_string();
                if first_err.is_none() {
                    first_err = Some(e);
                }
                died_count += 1;
                Some(msg)
            }
        };

        recorder.merge(&out.recorder);
        frames_total += frames;
        chunks_total += out.chunks;
        first_capture_ts = match (first_capture_ts, out.first_capture_ts) {
            (Some(a), Some(b)) => Some(if b < a { b } else { a }),
            (a, b) => a.or(b),
        };
        let drops = DropCounters {
            source_to_preproc: run.stages.src_drops.load(Ordering::Relaxed),
            preproc_to_chunker: run.stages.preproc_drops.load(Ordering::Relaxed),
            chunker_to_detector: run.stages.chunker_drops.load(Ordering::Relaxed),
        };
        drops_total.source_to_preproc += drops.source_to_preproc;
        drops_total.preproc_to_chunker += drops.preproc_to_chunker;
        drops_total.chunker_to_detector += drops.chunker_to_detector;
        per_leg.push(LegSummary {
            label: run.label,
            frames_in: frames,
            chunks_in: out.chunks,
            detections: out.records.len() as u64,
            drop_pct: drops.pct_of(frames),
            drops,
            died,
        });
        records.extend(out.records);
    }

    // Interleave legs chronologically so the report reads as one timeline.
    records.sort_by_key(|r| r.detection.emit_ts);

    // Built BEFORE the all-legs-died check so a dead run keeps its evidence.
    // The old order returned Err here and discarded per_leg/frames_total — see
    // `PipelineFailure`.
    let report = PipelineReport {
        event_id: event_id.to_string(),
        chunk_ms,
        engine: Some(engines.join("+")),
        frames_in: frames_total,
        chunks_in: chunks_total,
        first_capture_ts,
        edges: recorder.snapshot(),
        detections: records,
        drop_pct: drops_total.pct_of(frames_total),
        drops: drops_total,
        per_leg: Some(per_leg),
        decision_journal_error: None,
    };

    let result = if let Some(detail) = decision_health.fault_detail() {
        Err(PipelineFailure::decision_journal(detail, report))
    } else if died_count == n_legs {
        // No leg produced audio to the end — surface the failure instead of an
        // empty "successful" report, but hand the summary up with it so the
        // run can still describe itself on disk.
        if let Some(error) = first_err {
            Err(PipelineFailure {
                error,
                partial: Some(Box::new(report)),
                kind: PipelineFailureKind::Source,
            })
        } else {
            Ok(report)
        }
    } else {
        Ok(report)
    };

    PipelineAssemblyOutcome {
        result,
        audio_evidence: audio_evidence_enabled
            .then(|| consolidate_audio_evidence(audio_evidence_legs)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audio_evidence::{
        replay_audio_evidence, verify_audio_evidence, AudioEvidenceBank, AudioEvidenceConfig,
        AudioEvidenceError, AudioEvidenceIdentity, AudioEvidencePaths,
    };
    use crate::config::Policy;
    use crate::detector::stub::EnergyStubDetector;
    use crate::detector::Detector;
    use crate::fixture::{Burst, FixtureSpec};
    use crate::source::replay::ReplaySource;
    use crate::types::SourceId;
    use std::collections::BTreeMap;
    use std::time::Duration;

    fn audio_config(
        dir: &tempfile::TempDir,
        stem: &str,
        source_labels: BTreeMap<u16, String>,
    ) -> AudioEvidenceConfig {
        let root = dir.path().join(stem);
        let mut config = AudioEvidenceConfig::new(
            AudioEvidenceIdentity {
                run_id: format!("run-{stem}"),
                event_id: format!("event-{stem}"),
                plan_sha256: "a".repeat(64),
            },
            AudioEvidencePaths {
                raw: root.join("audio.raw"),
                frames: root.join("audio.frames.jsonl"),
                manifest: root.join("audio.manifest.json"),
                commit: root.join("audio.commit.json"),
                lock: root.join("audio.lock"),
                wav_dir: root.join("wav"),
            },
        );
        config.sync_every_frames = 1;
        config.producer_quiescence_timeout = Duration::from_millis(100);
        config.writer_join_timeout = Duration::from_secs(2);
        config.source_labels = source_labels;
        config
    }

    fn audio_keyword(market_ticker: &str) -> KeywordConfig {
        KeywordConfig {
            market_ticker: market_ticker.into(),
            phrase: "tone".into(),
            variants: vec!["tone".into()],
            max_price_cents: 90,
            budget_contracts: 1,
            policy: Policy::FastOnly,
            rule_risk_haircut_cents: 0,
            fast_prefixes: vec![],
            fast_threshold: None,
            confirmed_threshold: None,
            boost: None,
            min_confidence_fast: 0.0,
            min_confidence_confirmed: 0.0,
        }
    }

    struct TwoHitDetector {
        hits: u8,
    }

    impl Detector for TwoHitDetector {
        fn process(&mut self, chunk: &AudioChunk) -> smallvec::SmallVec<[Detection; 2]> {
            if self.hits >= 2 {
                return smallvec::SmallVec::new();
            }
            self.hits += 1;
            smallvec::smallvec![Detection {
                keyword: crate::types::KeywordId(0),
                confidence: 1.0,
                tier: crate::types::Tier::Confirmed,
                audio_ts: chunk.first_capture_ts,
                emit_ts: MonotonicNs::now(),
                source: chunk.source,
                audio_pos_ms: u64::from(self.hits) * 160,
                audio_consumed_ms: u64::from(self.hits) * 160,
            }]
        }

        fn engine_name(&self) -> &'static str {
            "two_hit_test"
        }
    }

    #[test]
    fn closed_admission_records_but_does_not_consume_one_shot_state() {
        let dir = tempfile::tempdir().unwrap();
        let wav = dir.path().join("two-chunks.wav");
        FixtureSpec {
            sample_rate_hz: 8_000,
            total_ms: 400,
            tone_hz: 440.0,
            amplitude: 0.5,
            bursts: vec![Burst {
                onset_ms: 0,
                dur_ms: 400,
            }],
        }
        .write_wav(&wav)
        .unwrap();
        let source = Box::new(ReplaySource::from_wav(&wav, 1.0, SourceId(0)).unwrap());
        let keyword = KeywordConfig {
            market_ticker: "KX-ADMIT".into(),
            phrase: "admit".into(),
            variants: vec!["admit".into()],
            max_price_cents: 90,
            budget_contracts: 1,
            policy: Policy::ConfirmedOnly,
            rule_risk_haircut_cents: 0,
            fast_prefixes: vec![],
            fast_threshold: None,
            confirmed_threshold: None,
            boost: None,
            min_confidence_fast: 0.0,
            min_confidence_confirmed: 0.0,
        };
        let table = DecisionTable::from_keywords(&[keyword]);
        let checks = Arc::new(std::sync::atomic::AtomicU64::new(0));
        let checks_for_gate = checks.clone();
        let admission = DecisionAdmission::new(
            move || checks_for_gate.fetch_add(1, std::sync::atomic::Ordering::SeqCst) > 0,
            "pre_admission",
        );
        let (intent_tx, mut intent_rx) = tokio::sync::mpsc::channel(4);

        let report = run_pipeline_admitted(
            "ADMISSION",
            160,
            preproc::PreprocCfg::default(),
            source,
            Box::new(TwoHitDetector { hits: 0 }),
            table,
            Some(intent_tx),
            Default::default(),
            admission,
        )
        .unwrap();

        assert_eq!(report.detections.len(), 2);
        assert_eq!(
            report.detections[0].suppression.as_deref(),
            Some("pre_admission")
        );
        assert!(report.detections[0].intent.is_none());
        assert!(report.detections[1].suppression.is_none());
        assert!(report.detections[1].intent.is_some());
        assert!(intent_rx.blocking_recv().is_some());
        assert!(
            intent_rx.try_recv().is_err(),
            "one-shot state fired more than once"
        );
    }

    #[test]
    fn journal_fault_is_a_pipeline_failure_with_partial_evidence() {
        let dir = tempfile::tempdir().unwrap();
        let wav = dir.path().join("fault.wav");
        FixtureSpec {
            sample_rate_hz: 8000,
            total_ms: 1200,
            tone_hz: 440.0,
            amplitude: 0.6,
            bursts: vec![Burst {
                onset_ms: 400,
                dur_ms: 500,
            }],
        }
        .write_wav(&wav)
        .unwrap();
        let keywords = vec![KeywordConfig {
            market_ticker: "KX-PIPE-FAULT".into(),
            phrase: "tone".into(),
            variants: vec!["tone".into()],
            max_price_cents: 90,
            budget_contracts: 1,
            policy: Policy::FastOnly,
            rule_risk_haircut_cents: 0,
            fast_prefixes: vec![],
            fast_threshold: None,
            confirmed_threshold: None,
            boost: None,
            min_confidence_fast: 0.0,
            min_confidence_confirmed: 0.0,
        }];
        let mut table =
            DecisionTable::from_keywords_with_journal(&keywords, dir.path().join("decision.jsonl"))
                .unwrap();
        table.fail_next_append_before_write();
        let source = Box::new(ReplaySource::from_wav(&wav, 0.0, SourceId(0)).unwrap());
        let detector = Box::new(EnergyStubDetector::for_standard_fixture());

        let failure = run_pipeline(
            "PIPE-FAULT",
            160,
            preproc::PreprocCfg::default(),
            source,
            detector,
            table,
            None,
            Default::default(),
        )
        .unwrap_err();

        assert_eq!(failure.kind, PipelineFailureKind::DecisionJournal);
        let partial = failure
            .partial
            .expect("journal fault discarded pipeline evidence");
        assert!(partial.decision_journal_error.is_some());
        let json = serde_json::to_value(&*partial).unwrap();
        assert!(json["decision_journal_error"]
            .as_str()
            .unwrap()
            .contains("injected append failure"));
        assert!(!partial.detections.is_empty());
        assert!(partial
            .detections
            .iter()
            .all(|record| record.intent.is_none()));
    }

    #[test]
    fn audio_door_banks_exact_post_preprocessor_detector_stream() {
        let dir = tempfile::tempdir().unwrap();
        let wav = dir.path().join("resample.wav");
        FixtureSpec {
            sample_rate_hz: 8_000,
            total_ms: 400,
            tone_hz: 440.0,
            amplitude: 0.4,
            bursts: vec![Burst {
                onset_ms: 0,
                dur_ms: 400,
            }],
        }
        .write_wav(&wav)
        .unwrap();
        let config = audio_config(&dir, "single", BTreeMap::from([(7, "phone".into())]));
        let paths = config.paths.clone();
        let (bank, tap) = AudioEvidenceBank::start_new(config).unwrap();
        let source = Box::new(ReplaySource::from_wav(&wav, 0.0, SourceId(7)).unwrap());
        let table = DecisionTable::from_keywords(&[audio_keyword("KX-AUDIO-SINGLE")]);

        let result = run_pipeline_admitted_with_audio(
            "AUDIO-SINGLE",
            160,
            preproc::PreprocCfg {
                resample_to_hz: Some(16_000),
            },
            source,
            Box::new(EnergyStubDetector::for_standard_fixture()),
            table,
            None,
            Default::default(),
            DecisionAdmission::always_open(),
            tap,
        )
        .unwrap();

        assert!(result.audio_evidence.is_healthy());
        assert_eq!(result.audio_evidence.preprocessed_frames, 20);
        assert_eq!(result.audio_evidence.observed_frames, 20);
        assert_eq!(result.audio_evidence.published_frames, 20);
        assert_eq!(result.pipeline.frames_in, 20);
        let published = result.audio_evidence.published_frames;
        let commit = bank.finish(published).unwrap();
        let replay = replay_audio_evidence(&paths.raw).unwrap();
        assert_eq!(replay.len(), published as usize);
        assert!(replay.iter().all(|frame| frame.source == SourceId(7)));
        assert!(replay.iter().all(|frame| frame.sample_rate_hz == 16_000));
        assert!(replay.iter().all(|frame| frame.pcm.len() == 320));
        assert!(replay
            .iter()
            .all(|frame| frame.preproc_ts >= frame.capture_ts));
        assert_eq!(
            verify_audio_evidence(&paths.manifest, &paths.commit).unwrap(),
            commit
        );
    }

    #[test]
    fn audio_door_failure_stops_visibility_but_drains_post_preprocessor_input() {
        let dir = tempfile::tempdir().unwrap();
        let wav = dir.path().join("drain.wav");
        FixtureSpec {
            sample_rate_hz: 8_000,
            total_ms: 400,
            tone_hz: 440.0,
            amplitude: 0.4,
            bursts: vec![Burst {
                onset_ms: 0,
                dur_ms: 400,
            }],
        }
        .write_wav(&wav)
        .unwrap();
        let mut config = audio_config(&dir, "drain", BTreeMap::new());
        config.queue_capacity = 1;
        config.test_writer_start_delay = Some(Duration::from_millis(150));
        let (bank, tap) = AudioEvidenceBank::start_new(config).unwrap();
        let source = Box::new(ReplaySource::from_wav(&wav, 0.0, SourceId(9)).unwrap());
        let table = DecisionTable::from_keywords(&[audio_keyword("KX-AUDIO-DRAIN")]);

        let failure = run_pipeline_admitted_with_audio(
            "AUDIO-DRAIN",
            160,
            preproc::PreprocCfg::default(),
            source,
            Box::new(EnergyStubDetector::for_standard_fixture()),
            table,
            None,
            Default::default(),
            DecisionAdmission::always_open(),
            tap,
        )
        .unwrap_err();

        let PipelineWithAudioFailure::AudioEvidence {
            partial,
            audio_evidence,
            concurrent_pipeline_failure,
        } = failure
        else {
            panic!("queue poison was not classified as audio evidence failure");
        };
        assert!(concurrent_pipeline_failure.is_none());
        assert_eq!(audio_evidence.preprocessed_frames, 20);
        assert_eq!(audio_evidence.observed_frames, 20);
        assert_eq!(audio_evidence.published_frames, 1);
        assert_eq!(audio_evidence.discarded_after_failure, 19);
        assert!(matches!(
            audio_evidence.first_error(),
            Some(AudioEvidenceTapError::Full)
        ));
        assert_eq!(partial.frames_in, 1);
        assert!(matches!(
            bank.finish(audio_evidence.published_frames),
            Err(AudioEvidenceError::Incomplete { .. })
        ));
    }

    #[test]
    fn fanout_audio_door_clones_then_quiesces_shared_bank_taps() {
        let dir = tempfile::tempdir().unwrap();
        let wav = dir.path().join("fanout.wav");
        FixtureSpec {
            sample_rate_hz: 8_000,
            total_ms: 200,
            tone_hz: 440.0,
            amplitude: 0.4,
            bursts: vec![Burst {
                onset_ms: 0,
                dur_ms: 200,
            }],
        }
        .write_wav(&wav)
        .unwrap();
        let config = audio_config(
            &dir,
            "fanout",
            BTreeMap::from([(1, "phone".into()), (2, "webcast".into())]),
        );
        let paths = config.paths.clone();
        let (bank, tap) = AudioEvidenceBank::start_new(config).unwrap();
        let legs = vec![
            PipelineLeg {
                label: "phone".into(),
                source: Box::new(ReplaySource::from_wav(&wav, 0.0, SourceId(1)).unwrap()),
                detector: Box::new(EnergyStubDetector::for_standard_fixture()),
            },
            PipelineLeg {
                label: "webcast".into(),
                source: Box::new(ReplaySource::from_wav(&wav, 0.0, SourceId(2)).unwrap()),
                detector: Box::new(EnergyStubDetector::for_standard_fixture()),
            },
        ];
        let table = DecisionTable::from_keywords(&[audio_keyword("KX-AUDIO-FANOUT")]);

        let result = run_pipeline_fanout_admitted_with_audio(
            "AUDIO-FANOUT",
            160,
            preproc::PreprocCfg::default(),
            legs,
            table,
            None,
            Default::default(),
            DecisionAdmission::always_open(),
            tap,
        )
        .unwrap();

        assert!(result.audio_evidence.is_healthy());
        assert_eq!(result.audio_evidence.per_leg.len(), 2);
        assert_eq!(result.audio_evidence.preprocessed_frames, 20);
        assert_eq!(result.audio_evidence.published_frames, 20);
        assert_eq!(result.pipeline.frames_in, 20);
        let published = result.audio_evidence.published_frames;
        let commit = bank.finish(published).unwrap();
        assert_eq!(commit.wav_segments.len(), 2);
        let replay = replay_audio_evidence(&paths.raw).unwrap();
        let source_one = replay
            .iter()
            .filter(|frame| frame.source == SourceId(1))
            .count();
        let source_two = replay
            .iter()
            .filter(|frame| frame.source == SourceId(2))
            .count();
        assert_eq!((source_one, source_two), (10, 10));
        assert_eq!(
            verify_audio_evidence(&paths.manifest, &paths.commit).unwrap(),
            commit
        );
    }

    /// Verifier hook (D2): two fanout legs, one shared one-shot table ⇒ exactly
    /// ONE intent-bearing request per keyword (the winning leg's tail), and
    /// under `sample = all_confirmed` every other Confirmed detection is a
    /// non-intent request. The tail delivered to the verifier ends at the
    /// firing chunk and is at most `tail_ms` long. Nothing gates (shadow).
    #[test]
    fn fanout_verifier_requests_once_per_keyword_with_the_leg_tail() {
        use crate::verifier::{build_with_transcriber, transcriber::MockTranscriber};
        let dir = tempfile::tempdir().unwrap();
        let wav = dir.path().join("verify.wav");
        FixtureSpec {
            sample_rate_hz: 8_000,
            total_ms: 800,
            tone_hz: 440.0,
            amplitude: 0.4,
            bursts: vec![Burst {
                onset_ms: 0,
                dur_ms: 800,
            }],
        }
        .write_wav(&wav)
        .unwrap();
        let mut kw = audio_keyword("KX-VERIFY");
        kw.policy = Policy::ConfirmedOnly;
        let kws = [kw];
        let section: crate::config::VerifierSection = toml::from_str(
            "mode = \"shadow\"\nmodel_dir = \"/nonexistent\"\nsample = \"all_confirmed\"\ntail_ms = 1000\n",
        )
        .unwrap();
        let build = build_with_transcriber(
            &section,
            &kws,
            Box::new(MockTranscriber(Box::new(
                |_| Ok("nothing said here".into()),
            ))),
        );
        let legs = vec![
            PipelineLeg {
                label: "phone".into(),
                source: Box::new(ReplaySource::from_wav(&wav, 0.0, SourceId(1)).unwrap()),
                detector: Box::new(TwoHitDetector { hits: 0 }),
            },
            PipelineLeg {
                label: "webcast".into(),
                source: Box::new(ReplaySource::from_wav(&wav, 0.0, SourceId(2)).unwrap()),
                detector: Box::new(TwoHitDetector { hits: 0 }),
            },
        ];
        let table = DecisionTable::from_keywords(&kws);
        let outcome = run_pipeline_fanout_admitted_with_hooks(
            "VERIFY-FANOUT",
            160,
            preproc::PreprocCfg::default(),
            legs,
            table,
            None,
            Default::default(),
            DecisionAdmission::always_open(),
            StageHooks {
                audio_evidence_tap: None,
                verifier: Some(build.client.clone()),
            },
        );
        let report = outcome.result.unwrap();
        // 2 legs × 2 detections = 4 Confirmed detections, exactly one became an intent
        assert_eq!(report.detections.len(), 4);
        assert_eq!(
            report
                .detections
                .iter()
                .filter(|d| d.intent.is_some())
                .count(),
            1
        );
        build.client.shutdown();
        let vr = build.handle.join();
        assert_eq!(
            vr.rows.len(),
            4,
            "all_confirmed ⇒ every Confirmed detection was verified: {vr:?}"
        );
        assert_eq!(vr.rows.iter().filter(|r| r.intent_bearing).count(), 1);
        for r in &vr.rows {
            assert!(r.tail_ms > 0 && r.tail_ms <= 1000, "{r:?}");
            assert_eq!(r.verdict, crate::verifier::Verdict::Rejected);
        }
        assert_eq!(vr.dropped, 0);
    }
}
