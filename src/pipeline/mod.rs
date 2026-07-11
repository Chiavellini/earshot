//! Pipeline wiring (spec §4): dedicated OS threads for the hot path, a
//! current-thread tokio runtime driving the source, bounded SPSC rings
//! between stages. No pinning/SCHED_FIFO in M0 — that's M5 OS tuning.

pub mod chunker;
pub mod preproc;

use crate::clock::MonotonicNs;
use crate::decision::DecisionTable;
use crate::detector::Detector;
use crate::ring;
use crate::source::{AudioSource, SourceError};
use crate::telemetry::{DetectionRecord, DropCounters, LatencyRecorder, PipelineReport};
use crate::types::{AudioChunk, AudioFrame};

const FRAME_RING_CAP: usize = 256;
const CHUNK_RING_CAP: usize = 64;

/// `intent_tx`: the decision stage's exit door toward the executor (M2).
/// `None` = log-only (M0 replay mode). `try_send` from the sync detect
/// thread never blocks; a full channel is a counted, loud drop.
pub fn run_pipeline(
    event_id: &str,
    chunk_ms: u32,
    preproc_cfg: preproc::PreprocCfg,
    source: Box<dyn AudioSource>,
    mut detector: Box<dyn Detector>,
    mut table: DecisionTable,
    intent_tx: Option<tokio::sync::mpsc::Sender<crate::types::DecisionIntent>>,
) -> Result<PipelineReport, SourceError> {
    let (src_tx, preproc_rx) = ring::ring::<AudioFrame>(FRAME_RING_CAP);
    let (preproc_tx, chunker_rx) = ring::ring::<AudioFrame>(FRAME_RING_CAP);
    let (chunker_tx, mut detect_rx) = ring::ring::<AudioChunk>(CHUNK_RING_CAP);

    let src_drops = src_tx.drops_handle();
    let preproc_drops = preproc_tx.drops_handle();
    let chunker_drops = chunker_tx.drops_handle();

    let preproc_thread = std::thread::Builder::new()
        .name("preproc".into())
        .spawn(move || preproc::run(preproc_rx, preproc_tx, preproc_cfg))
        .expect("spawn preproc");

    let chunker_thread = std::thread::Builder::new()
        .name("chunker".into())
        .spawn(move || chunker::run(chunker_rx, chunker_tx, chunk_ms))
        .expect("spawn chunker");

    // Detector + decision share a thread: decision is O(µs), a fourth thread
    // buys nothing (spec §4). This thread owns the latency recorder.
    let detect_thread = std::thread::Builder::new()
        .name("detect".into())
        .spawn(move || {
            let mut recorder = LatencyRecorder::new();
            let mut records: Vec<DetectionRecord> = Vec::new();
            let mut chunks = 0u64;
            let mut first_capture_ts = None;
            while let Some(chunk) = detect_rx.recv() {
                if chunk.seq == 0 {
                    first_capture_ts = Some(chunk.first_capture_ts);
                }
                chunks += 1;
                let detections = detector.process(&chunk);
                let after = MonotonicNs::now();
                recorder.record(
                    "capture→preproc",
                    chunk.first_preproc_ts.saturating_since(chunk.first_capture_ts),
                );
                recorder.record(
                    "preproc→chunk",
                    chunk.chunk_ts.saturating_since(chunk.first_preproc_ts),
                );
                recorder.record("chunk→detect", after.saturating_since(chunk.chunk_ts));

                for det in detections {
                    let intent = table.decide(&det);
                    let decision_ts = intent
                        .as_ref()
                        .map(|i| i.decision_ts)
                        .unwrap_or_else(MonotonicNs::now);
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
                            if let Some(tx) = &intent_tx {
                                if tx.try_send(i.clone()).is_err() {
                                    tracing::error!(
                                        keyword = det.keyword.0,
                                        "intent channel full/closed: intent DROPPED"
                                    );
                                }
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

    // Drive the source on a current-thread runtime; network-heavy sources
    // (SIP/HLS) later share a multi-thread runtime owned by the caller.
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_time()
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
