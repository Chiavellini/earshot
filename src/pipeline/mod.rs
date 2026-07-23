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

use std::sync::atomic::AtomicU64;
use std::sync::{Arc, Mutex};

use crate::clock::MonotonicNs;
use crate::decision::DecisionTable;
use crate::detector::Detector;
use crate::ring;
use crate::source::{AudioSource, SourceError};
use crate::telemetry::{DetectionRecord, DropCounters, LatencyRecorder, LegSummary, PipelineReport};
use crate::types::{AudioChunk, AudioFrame, DecisionIntent, Detection};

const FRAME_RING_CAP: usize = 256;
const CHUNK_RING_CAP: usize = 64;

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

/// Handles for one leg's spawned stage threads plus its drop counters.
struct StageHandles {
    preproc: std::thread::JoinHandle<u64>,
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
    decide: impl FnMut(&Detection) -> Option<DecisionIntent> + Send + 'static,
    intent_tx: Option<tokio::sync::mpsc::Sender<DecisionIntent>>,
    perf: crate::perf::PerfSection,
    leg_label: String,
) -> (ring::RingSender<AudioFrame>, StageHandles) {
    let (src_tx, mut preproc_rx) = ring::ring::<AudioFrame>(FRAME_RING_CAP);
    let (preproc_tx, mut chunker_rx) = ring::ring::<AudioFrame>(FRAME_RING_CAP);
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
            crate::perf::apply_to_current_thread("preproc", perf.core_preproc, perf.fifo_priority());
            preproc::run(preproc_rx, preproc_tx, preproc_cfg)
        })
        .expect("spawn preproc");

    let chunker = std::thread::Builder::new()
        .name(format!("chunker{suffix}"))
        .spawn(move || {
            crate::perf::apply_to_current_thread("chunker", perf.core_chunker, perf.fifo_priority());
            chunker::run(chunker_rx, chunker_tx, chunk_ms)
        })
        .expect("spawn chunker");

    // Detector + decision share a thread: decision is O(µs), a fourth thread
    // buys nothing (spec §4). This thread owns the latency recorder.
    let detect = std::thread::Builder::new()
        .name(format!("detect{suffix}"))
        .spawn(move || {
            crate::perf::apply_to_current_thread("detect", perf.core_detect, perf.fifo_priority());
            detect_loop(detect_rx, detector, decide, intent_tx, leg_label)
        })
        .expect("spawn detect");

    (
        src_tx,
        StageHandles { preproc, chunker, detect, src_drops, preproc_drops, chunker_drops },
    )
}

/// The detect+decide stage, shared by both entry points. `decide` abstracts
/// table ownership: the single-source pipeline captures its owned
/// `DecisionTable` (no lock anywhere); fanout legs each capture a clone of the
/// shared `Arc<Mutex<DecisionTable>>` and lock exactly for the O(µs) `decide`.
fn detect_loop(
    mut detect_rx: ring::RingReceiver<AudioChunk>,
    mut detector: Box<dyn Detector>,
    mut decide: impl FnMut(&Detection) -> Option<DecisionIntent>,
    intent_tx: Option<tokio::sync::mpsc::Sender<DecisionIntent>>,
    leg_label: String,
) -> DetectOut {
    let mut recorder = LatencyRecorder::new();
    let mut records: Vec<DetectionRecord> = Vec::new();
    let mut chunks = 0u64;
    let mut first_capture_ts = None;
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
        let detections = detector.process(&chunk);
        let after = MonotonicNs::now();

        // FIRE FIRST: decide + hand off before any telemetry or
        // logging — nothing but the trade sits on the critical span.
        // Edge values are recorded (and intents logged) below, after
        // every intent of this chunk has left the thread; the
        // timestamps are captured here so the deferred bookkeeping
        // measures the same instants.
        let mut decided: smallvec::SmallVec<[(Detection, Option<DecisionIntent>, MonotonicNs, bool); 2]> =
            smallvec::SmallVec::new();
        for det in detections {
            let intent = decide(&det);
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
            decided.push((det, intent, decision_ts, dropped));
        }

        // Off the critical span: per-chunk edges, then per-detection
        // edges, logs, and records.
        recorder.record(
            "capture→preproc",
            chunk.first_preproc_ts.saturating_since(chunk.first_capture_ts),
        );
        recorder.record(
            "preproc→chunk",
            chunk.chunk_ts.saturating_since(chunk.first_preproc_ts),
        );
        recorder.record("chunk→detect", after.saturating_since(chunk.chunk_ts));

        for (det, intent, decision_ts, dropped) in decided {
            recorder.record(
                "detect→decision",
                decision_ts.saturating_since(det.emit_ts),
            );
            recorder.record(
                "capture→decision",
                decision_ts.saturating_since(det.audio_ts),
            );
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
            records.push(DetectionRecord {
                detect_latency_us: det.emit_ts.saturating_since(det.audio_ts) / 1000,
                detection: det,
                intent,
                leg: leg_label.clone(),
            });
        }
    }
    DetectOut { recorder, records, chunks, first_capture_ts }
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
    mut table: DecisionTable,
    intent_tx: Option<tokio::sync::mpsc::Sender<crate::types::DecisionIntent>>,
    perf: crate::perf::PerfSection,
) -> Result<PipelineReport, SourceError> {
    let (src_tx, stages) = spawn_stages(
        "",
        chunk_ms,
        preproc_cfg,
        detector,
        move |det| table.decide(det),
        intent_tx,
        perf,
        String::new(), // single-source records carry no leg label
    );

    // Drive the source on a current-thread runtime. IO is enabled because
    // network sources (Twilio WS) run here too, not just file replay.
    // Capture timestamps originate on this thread — pin it too when asked.
    crate::perf::apply_to_current_thread("source", perf.core_source, perf.fifo_priority());
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("tokio runtime");
    let exit = rt.block_on(source.run(src_tx))?;
    tracing::info!(?exit, "source finished");

    let frames_in = stages.preproc.join().expect("preproc thread panicked");
    let chunks_from_chunker = stages.chunker.join().expect("chunker thread panicked");
    let out = stages.detect.join().expect("detect thread panicked");
    debug_assert_eq!(chunks_from_chunker, out.chunks);

    use std::sync::atomic::Ordering;
    Ok(PipelineReport {
        event_id: event_id.to_string(),
        chunk_ms,
        frames_in,
        chunks_in: out.chunks,
        first_capture_ts: out.first_capture_ts,
        edges: out.recorder.snapshot(),
        detections: out.records,
        drops: DropCounters {
            source_to_preproc: stages.src_drops.load(Ordering::Relaxed),
            preproc_to_chunker: stages.preproc_drops.load(Ordering::Relaxed),
            chunker_to_detector: stages.chunker_drops.load(Ordering::Relaxed),
        },
        per_leg: None,
    })
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
) -> Result<PipelineReport, SourceError> {
    assert!(legs.len() >= 2, "fanout requires >= 2 legs (config validation)");
    let shared = Arc::new(Mutex::new(table));

    struct LegRun {
        label: String,
        stages: StageHandles,
        source_thread: std::thread::JoinHandle<Result<crate::source::SourceExit, SourceError>>,
    }

    let mut runs = Vec::with_capacity(legs.len());
    for (i, leg) in legs.into_iter().enumerate() {
        let shared = shared.clone();
        let (src_tx, stages) = spawn_stages(
            &format!("-{i}"),
            chunk_ms,
            preproc_cfg,
            leg.detector,
            move |det| shared.lock().expect("decision table poisoned").decide(det),
            intent_tx.clone(),
            perf,
            leg.label.clone(),
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
                    Err(e) => tracing::error!(leg = %label, error = %e, "LEG DIED — siblings continue"),
                }
                out
            })
            .expect("spawn leg source");
        runs.push(LegRun { label: leg.label, stages, source_thread });
    }
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
    let mut drops_total = DropCounters { source_to_preproc: 0, preproc_to_chunker: 0, chunker_to_detector: 0 };
    let mut first_err: Option<SourceError> = None;
    let mut died_count = 0usize;
    let n_legs = runs.len();

    use std::sync::atomic::Ordering;
    for run in runs {
        let source_result = run.source_thread.join().expect("leg source thread panicked");
        let frames = run.stages.preproc.join().expect("preproc thread panicked");
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
            drops,
            died,
        });
        records.extend(out.records);
    }

    if died_count == n_legs {
        // No leg produced audio to the end — surface the failure instead of
        // an empty "successful" report.
        if let Some(e) = first_err {
            return Err(e);
        }
    }

    // Interleave legs chronologically so the report reads as one timeline.
    records.sort_by_key(|r| r.detection.emit_ts);

    Ok(PipelineReport {
        event_id: event_id.to_string(),
        chunk_ms,
        frames_in: frames_total,
        chunks_in: chunks_total,
        first_capture_ts,
        edges: recorder.snapshot(),
        detections: records,
        drops: drops_total,
        per_leg: Some(per_leg),
    })
}
