//! Pipeline wiring (spec §4): dedicated OS threads for the hot path, a
//! current-thread tokio runtime driving the source, bounded SPSC rings
//! between stages. Each hot thread applies its `[perf]` pin/SCHED_FIFO at
//! spawn (src/perf.rs, docs/OS-TUNING.md); default config = no-op.

pub mod chunker;
pub mod preproc;

use crate::clock::MonotonicNs;
use crate::decision::DecisionTable;
use crate::detector::Detector;
use crate::ring;
use crate::source::{AudioSource, SourceError};
use crate::telemetry::{DetectionRecord, DropCounters, LatencyRecorder, PipelineReport};
use crate::types::{AudioChunk, AudioFrame, DecisionIntent, Detection};

const FRAME_RING_CAP: usize = 256;
const CHUNK_RING_CAP: usize = 64;

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
    mut detector: Box<dyn Detector>,
    mut table: DecisionTable,
    intent_tx: Option<tokio::sync::mpsc::Sender<crate::types::DecisionIntent>>,
    perf: crate::perf::PerfSection,
) -> Result<PipelineReport, SourceError> {
    let (src_tx, mut preproc_rx) = ring::ring::<AudioFrame>(FRAME_RING_CAP);
    let (preproc_tx, mut chunker_rx) = ring::ring::<AudioFrame>(FRAME_RING_CAP);
    let (chunker_tx, mut detect_rx) = ring::ring::<AudioChunk>(CHUNK_RING_CAP);

    // RT boxes: hot consumers busy-spin instead of park/unpark (~100 ns
    // reaction). Config validation guarantees realtime + distinct cores.
    if perf.spin_hot_rings {
        preproc_rx.set_spin(true);
        chunker_rx.set_spin(true);
        detect_rx.set_spin(true);
    }

    let src_drops = src_tx.drops_handle();
    let preproc_drops = preproc_tx.drops_handle();
    let chunker_drops = chunker_tx.drops_handle();

    let preproc_thread = std::thread::Builder::new()
        .name("preproc".into())
        .spawn(move || {
            crate::perf::apply_to_current_thread("preproc", perf.core_preproc, perf.fifo_priority());
            preproc::run(preproc_rx, preproc_tx, preproc_cfg)
        })
        .expect("spawn preproc");

    let chunker_thread = std::thread::Builder::new()
        .name("chunker".into())
        .spawn(move || {
            crate::perf::apply_to_current_thread("chunker", perf.core_chunker, perf.fifo_priority());
            chunker::run(chunker_rx, chunker_tx, chunk_ms)
        })
        .expect("spawn chunker");

    // Detector + decision share a thread: decision is O(µs), a fourth thread
    // buys nothing (spec §4). This thread owns the latency recorder.
    let detect_thread = std::thread::Builder::new()
        .name("detect".into())
        .spawn(move || {
            crate::perf::apply_to_current_thread("detect", perf.core_detect, perf.fifo_priority());
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
                    let intent = table.decide(&det);
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
                            "detection ignored (keyword state machine)"
                        ),
                    }
                    records.push(DetectionRecord {
                        detect_latency_us: det.emit_ts.saturating_since(det.audio_ts) / 1000,
                        detection: det,
                        intent,
                    });
                }
            }
            (recorder, records, chunks, first_capture_ts)
        })
        .expect("spawn detect");

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

    let frames_in = preproc_thread.join().expect("preproc thread panicked");
    let chunks_from_chunker = chunker_thread.join().expect("chunker thread panicked");
    let (recorder, detections, chunks_in, first_capture_ts) =
        detect_thread.join().expect("detect thread panicked");
    debug_assert_eq!(chunks_from_chunker, chunks_in);

    use std::sync::atomic::Ordering;
    Ok(PipelineReport {
        event_id: event_id.to_string(),
        chunk_ms,
        frames_in,
        chunks_in,
        first_capture_ts,
        edges: recorder.snapshot(),
        detections,
        drops: DropCounters {
            source_to_preproc: src_drops.load(Ordering::Relaxed),
            preproc_to_chunker: preproc_drops.load(Ordering::Relaxed),
            chunker_to_detector: chunker_drops.load(Ordering::Relaxed),
        },
    })
}
