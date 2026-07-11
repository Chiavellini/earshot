//! Latency instrumentation and reporting (spec §5.9).
//!
//! The recorder is owned by the last hot-path thread and records directly
//! into pre-allocated hdrhistograms (~tens of ns, no locks, no allocation) —
//! at M0's stage count a handoff ring would add machinery without removing
//! any blocking. When M2 adds sign/send/ack edges from the network thread,
//! per-thread recorders merge at report time (hdrhistogram supports `add`).

use std::collections::BTreeMap;
use std::io::Write;
use std::path::Path;

use hdrhistogram::Histogram;
use serde::Serialize;

use crate::types::{DecisionIntent, Detection};

/// Edge names, in pipeline order (BTreeMap sorts alphabetically; the report
/// renders in this order instead). The M2 execution edges live in the same
/// recorder space; a run that never fires an order simply leaves them at
/// count 0 (hidden by the renderer).
pub const EDGES: [&str; 11] = [
    "capture→preproc",
    "preproc→chunk",
    "chunk→detect",
    "detect→decision",
    "capture→decision",
    "decision→exec",
    "presign_age",
    "exec→send",
    "send→ack",
    "decision→ack",
    "capture→ack",
];

pub struct LatencyRecorder {
    hists: BTreeMap<&'static str, Histogram<u64>>,
}

impl Default for LatencyRecorder {
    fn default() -> Self {
        Self::new()
    }
}

impl LatencyRecorder {
    pub fn new() -> Self {
        let mut hists = BTreeMap::new();
        for edge in EDGES {
            // 1 ns .. 60 s range, 3 significant digits: fixed memory, covers
            // everything from ring hops to a stalled stage.
            hists.insert(edge, Histogram::new_with_bounds(1, 60_000_000_000, 3).unwrap());
        }
        LatencyRecorder { hists }
    }

    #[inline]
    pub fn record(&mut self, edge: &'static str, nanos: u64) {
        if let Some(h) = self.hists.get_mut(edge) {
            h.record(nanos.max(1)).ok(); // saturates at 60 s; loud in report via max
        }
    }

    /// Merge another recorder's histograms into this one (used to fold the
    /// executor's recorder into the pipeline report at teardown).
    pub fn merge(&mut self, other: &LatencyRecorder) {
        for (edge, h) in &other.hists {
            if let Some(mine) = self.hists.get_mut(edge) {
                mine.add(h).ok();
            }
        }
    }

    pub fn snapshot(&self) -> BTreeMap<String, EdgeStats> {
        self.hists
            .iter()
            .filter(|(_, h)| !h.is_empty())
            .map(|(edge, h)| {
                (
                    edge.to_string(),
                    EdgeStats {
                        count: h.len(),
                        p50_us: h.value_at_quantile(0.50) / 1000,
                        p90_us: h.value_at_quantile(0.90) / 1000,
                        p95_us: h.value_at_quantile(0.95) / 1000,
                        p99_us: h.value_at_quantile(0.99) / 1000,
                        max_us: h.max() / 1000,
                    },
                )
            })
            .collect()
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct EdgeStats {
    pub count: u64,
    pub p50_us: u64,
    pub p90_us: u64,
    pub p95_us: u64,
    pub p99_us: u64,
    pub max_us: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct DetectionRecord {
    pub detection: Detection,
    pub intent: Option<DecisionIntent>,
    /// emit_ts − audio_ts: detection latency relative to the capture of the
    /// chunk's first frame (dominated by chunk accumulation, by design).
    pub detect_latency_us: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct DropCounters {
    pub source_to_preproc: u64,
    pub preproc_to_chunker: u64,
    pub chunker_to_detector: u64,
}

impl DropCounters {
    pub fn total(&self) -> u64 {
        self.source_to_preproc + self.preproc_to_chunker + self.chunker_to_detector
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct PipelineReport {
    pub event_id: String,
    pub chunk_ms: u32,
    pub frames_in: u64,
    pub chunks_in: u64,
    /// Capture time of the stream's first frame (chunk seq 0). In paced
    /// replays this anchors the wall clock to the content timeline, letting
    /// the bench compute onset→emit latency; `None` if no chunk arrived.
    pub first_capture_ts: Option<crate::clock::MonotonicNs>,
    pub edges: BTreeMap<String, EdgeStats>,
    pub detections: Vec<DetectionRecord>,
    pub drops: DropCounters,
}

impl PipelineReport {
    pub fn write_json(&self, path: &Path) -> std::io::Result<()> {
        let mut f = std::fs::File::create(path)?;
        f.write_all(serde_json::to_string_pretty(self)?.as_bytes())
    }

    pub fn render_table(&self) -> String {
        let mut out = String::new();
        out.push_str(&format!(
            "event {}  chunk {} ms  frames {}  chunks {}  detections {}  drops {}\n\n",
            self.event_id,
            self.chunk_ms,
            self.frames_in,
            self.chunks_in,
            self.detections.len(),
            self.drops.total(),
        ));
        out.push_str(&format!(
            "{:<18} {:>8} {:>10} {:>10} {:>10} {:>10} {:>10}\n",
            "edge (µs)", "count", "p50", "p90", "p95", "p99", "max"
        ));
        for edge in EDGES {
            if let Some(s) = self.edges.get(edge) {
                out.push_str(&format!(
                    "{:<18} {:>8} {:>10} {:>10} {:>10} {:>10} {:>10}\n",
                    edge, s.count, s.p50_us, s.p90_us, s.p95_us, s.p99_us, s.max_us
                ));
            }
        }
        if !self.detections.is_empty() {
            out.push_str("\ndetections:\n");
            for d in &self.detections {
                out.push_str(&format!(
                    "  kw{} {:?} detect_latency {} µs  intent: {}\n",
                    d.detection.keyword.0,
                    d.detection.tier,
                    d.detect_latency_us,
                    d.intent
                        .as_ref()
                        .map(|i| format!("{} x{} @≤{}¢", i.market_ticker, i.tranche_contracts, i.max_price_cents))
                        .unwrap_or_else(|| "suppressed (state machine)".into()),
                ));
            }
        }
        out
    }
}

pub fn init_logging(json: bool) {
    use tracing_subscriber::EnvFilter;
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    if json {
        tracing_subscriber::fmt().with_env_filter(filter).json().init();
    } else {
        tracing_subscriber::fmt().with_env_filter(filter).init();
    }
}
