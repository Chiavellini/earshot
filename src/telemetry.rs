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
pub const EDGES: [&str; 16] = [
    "capture→preproc",
    "preproc→chunk",
    "chunk→detect",
    // ring_wake — push→wake cost of the detect hop's SPSC park (recorded only
    // when the consumer actually parked; absent under spin mode). With
    // unpark-on-send this should sit at unpark-syscall scale (~µs), far under
    // the 100 µs PARK_TIMEOUT backstop.
    "ring_wake",
    "detect→decision",
    "capture→decision",
    // handoff→exec — pure mpsc scheduler-wake hop of the detect→fire
    // boundary, isolated from the detect-thread critical-section tail by
    // stamping `handoff_ts` immediately before try_send. decision→exec stays
    // the aggregate (tail + wake).
    "handoff→exec",
    "decision→exec",
    // book_age — WS book staleness at decision time. Was recorded but MISSING
    // from this list (silently dropped by record()'s get_mut miss) until the
    // latency pass; kept next to presign_age as the two "age" diagnostics.
    "book_age",
    "presign_age",
    "exec→send",
    // send→ack split at TTFB: send→hdrs is network + matching-engine time,
    // hdrs→ack is HTTP body read + JSON parse. send→ack is kept as the
    // end-to-end aggregate (p95 of a sum ≠ sum of p95s).
    "send→hdrs",
    "hdrs→ack",
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
            hists.insert(
                edge,
                Histogram::new_with_bounds(1, 60_000_000_000, 3).unwrap(),
            );
        }
        LatencyRecorder { hists }
    }

    #[inline]
    pub fn record(&mut self, edge: &'static str, nanos: u64) {
        if let Some(h) = self.hists.get_mut(edge) {
            // saturating: a stalled stage must show up as a loud 60 s max,
            // not vanish (plain record() ERRORS above the bound and would
            // silently drop exactly the samples that matter most).
            h.saturating_record(nanos.max(1));
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
    /// Present when assembly intentionally prevented this detection from
    /// reaching (and therefore consuming) the one-shot decision table. Phone
    /// runs use `pre_admission`; the detection remains evidence, not an intent.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub suppression: Option<String>,
    /// emit_ts − audio_ts: detection latency relative to the capture of the
    /// chunk's first frame (dominated by chunk accumulation, by design).
    pub detect_latency_us: u64,
    /// Label of the audio leg that heard this (fanout mode). Empty (and
    /// omitted from JSON) in single-source mode, keeping those reports
    /// unchanged.
    #[serde(skip_serializing_if = "String::is_empty")]
    pub leg: String,
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

    /// Dropped share of ingress frames, 0.0–100.0. `None` when no frame ever
    /// arrived, which is a DIFFERENT failure (armed but deaf) and must not be
    /// reported as a clean 0%.
    ///
    /// Only the ingress ring can drop — interior stages backpressure
    /// (`ring.rs`, `chunker.rs`) — so `frames_in` is the right denominator.
    /// Until 2026-08-07 no rate existed anywhere in the crate: the 3.3 / 4.1 /
    /// 11.3% figures in the 26AUG05 post-mortem were computed by hand from
    /// `report.json`, and the live `arm` path had no drop gate at all while
    /// `replay` and `bench` both hard-fail on a single dropped frame.
    pub fn pct_of(&self, frames_in: u64) -> Option<f64> {
        (frames_in > 0).then(|| self.total() as f64 * 100.0 / frames_in as f64)
    }
}

/// Per-leg rollup for fanout mode: one entry per audio leg, so a dead or
/// lagging leg is visible in the report even when the other leg won every
/// race.
#[derive(Debug, Clone, Serialize)]
pub struct LegSummary {
    pub label: String,
    pub frames_in: u64,
    pub chunks_in: u64,
    pub detections: u64,
    pub drops: DropCounters,
    /// `drops.total() / frames_in` as a percentage; omitted when no frame
    /// arrived. Serialized so graders never have to recompute it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub drop_pct: Option<f64>,
    /// `Some(reason)` when this leg's source ended with an error (leg-local
    /// failure policy: siblings keep running).
    pub died: Option<String>,
}

/// How a run ended. Written on EVERY terminated run, not only the clean ones.
///
/// 2026-08-05: `arm()` wrote `report.json` on exactly one of ~14 exit paths, so
/// a run that died left nothing behind — MELI ended on the pipe idle guard
/// (`src/source/pipe.rs`) and its whole record was lost, which is
/// indistinguishable from an event that never armed. A run must always be able
/// to say how it ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExitReason {
    /// Assembly built the graph but refused to release it because a required
    /// startup receipt (currently `listener.ready`) could not be persisted.
    /// No source frames or order intents were allowed through.
    StartupError,
    /// Source reached end-of-stream and every stage drained normally.
    CleanDrain,
    /// The source returned an error (idle guard, WS/pipe read failure, dial
    /// never landed). The MELI 2026-08-05 class.
    SourceError,
    /// Fanout mode: every leg died, so the pipeline had nothing left to read.
    AllLegsDied,
    /// The durable one-shot decision state could not be appended or synced.
    /// The session was killed; this is neither an audio-source failure nor a
    /// thread panic and must remain separately gradeable.
    DecisionStateError,
    /// Durable order/wire evidence became unhealthy. The session was killed
    /// and the run cannot be classified as a clean drain.
    OrderEvidenceError,
    /// SIGTERM — the unattended kill (launchd, launcher trap, supervisor).
    Sigterm,
    /// `<state-dir>/STOP` or SIGINT: gate disarmed, pipeline winds down.
    StopFile,
    /// A stage thread panicked or a join failed.
    PanicOrJoin,
}

/// `reason` plus whatever the failure said, so a post-mortem does not have to
/// go back to the log to learn *why* a run stopped.
#[derive(Debug, Clone, Serialize)]
pub struct ExitRecord {
    pub reason: ExitReason,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

impl ExitRecord {
    pub fn clean() -> Self {
        ExitRecord {
            reason: ExitReason::CleanDrain,
            detail: None,
        }
    }

    pub fn with(reason: ExitReason, detail: impl Into<String>) -> Self {
        ExitRecord {
            reason,
            detail: Some(detail.into()),
        }
    }
}

/// The SIGTERM last-words record.
///
/// A killed run cannot drain, so it cannot produce a `PipelineReport` — but it
/// must still say how it ended, and in the SAME shape as a full report, so
/// post-event grading can ask one question of every run instead of
/// special-casing the stub. `terminated` is retained for readers that predate
/// `exit`.
pub fn sigterm_stub_json(
    session_order_ids: &[String],
    pending_client_order_ids: &[String],
    ledger_spent_cents: u64,
) -> serde_json::Value {
    serde_json::json!({
        "terminated": "SIGTERM",
        "exit": ExitRecord::with(ExitReason::Sigterm, "kill watcher: SIGTERM"),
        "partial_report": true,
        "session_order_ids": session_order_ids,
        "pending_client_order_ids": pending_client_order_ids,
        "ledger_spent_cents": ledger_spent_cents,
    })
}

/// Assemble the run record that reaches `report.json`.
///
/// Split out of `main.rs::arm` so the assembly is testable without a binary:
/// the failure it guards against is a run that ends and leaves NOTHING on
/// disk, which is exactly what happened to MELI on 2026-08-05. `report` is
/// `None` when the pipeline never produced one — the record is still emitted,
/// carrying the event, the exit and whatever orders the executor managed.
pub fn build_run_json(
    event_id: &str,
    report: Option<&PipelineReport>,
    exec: Option<&crate::kalshi::executor::ExecReport>,
    exit: &ExitRecord,
) -> Result<serde_json::Value, serde_json::Error> {
    let mut json = match report {
        Some(r) => serde_json::to_value(r)?,
        None => serde_json::json!({ "event_id": event_id, "partial_report": true }),
    };
    // Written here rather than trusted from the struct so both the full and the
    // partial record carry it in the same place.
    json["exit"] = serde_json::to_value(exit)?;
    match exec {
        Some(e) => {
            // `orders` is a list of INTENTS; these three say what actually
            // happened to them. Emitted as first-class keys so no reader has to
            // re-derive the distinction that produced the false "23 orders
            // placed" on 2026-08-05 (the true number was 0).
            json["intents"] = e.orders.len().into();
            let send_attempted = e.orders.iter().filter(|o| o.send_attempted()).count();
            json["send_attempted"] = send_attempted.into();
            // Compatibility key. Its historical spelling overstates what a
            // send timestamp proves; new readers use `send_attempted`.
            json["sent"] = send_attempted.into();
            json["acked"] = e.orders.iter().filter(|o| o.acked()).count().into();
            json["orders"] = serde_json::to_value(&e.orders)?;
            json["lost_races"] = e.lost_races.into();
            json["observe_noncross"] = e.observe_noncross.into();
        }
        None => {
            json["intents"] = 0.into();
            json["sent"] = 0.into();
            json["send_attempted"] = 0.into();
            json["acked"] = 0.into();
            json["orders"] = serde_json::json!([]);
            json["executor_report_missing"] = true.into();
        }
    }
    Ok(json)
}

#[derive(Debug, Clone, Serialize)]
pub struct PipelineReport {
    pub event_id: String,
    pub chunk_ms: u32,
    /// Which detector produced `detections`, so `confidence` can be read at
    /// all. `sherpa_kws` reports a constant 1.0 sentinel while `streaming_ctc`
    /// reports a real posterior; without this field the two are
    /// indistinguishable in a banked report, and roughly half the arm configs
    /// run each. A 2026-08-07 settlement study had to infer the engine from
    /// the sentinel value to avoid pooling them. `None` on reports written
    /// before this field existed, and omitted from JSON so those keep their
    /// exact shape.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub engine: Option<String>,
    pub frames_in: u64,
    pub chunks_in: u64,
    /// Capture time of the stream's first frame (chunk seq 0). In paced
    /// replays this anchors the wall clock to the content timeline, letting
    /// the bench compute onset→emit latency; `None` if no chunk arrived.
    pub first_capture_ts: Option<crate::clock::MonotonicNs>,
    pub edges: BTreeMap<String, EdgeStats>,
    pub detections: Vec<DetectionRecord>,
    pub drops: DropCounters,
    /// Run-wide `drops.total() / frames_in` as a percentage; omitted when no
    /// frame arrived. See `DropCounters::pct_of`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub drop_pct: Option<f64>,
    /// Fanout mode only (`None` in single-source mode, omitted from JSON so
    /// existing reports keep their exact shape).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub per_leg: Option<Vec<LegSummary>>,
    /// First durable decision-state failure. Presence means the risk session
    /// was killed and no later detection was eligible to trade.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub decision_journal_error: Option<String>,
}

impl PipelineReport {
    pub fn write_json(&self, path: &Path) -> std::io::Result<()> {
        let mut f = std::fs::File::create(path)?;
        f.write_all(serde_json::to_string_pretty(self)?.as_bytes())
    }

    /// `" (4.13%)"`, or empty when no frame arrived. A blank is deliberate:
    /// "0.00%" on a run that heard nothing would read as clean.
    fn render_table_pct_suffix(p: Option<f64>) -> String {
        match p {
            Some(p) => format!(" ({p:.2}%)"),
            None => String::new(),
        }
    }

    pub fn render_table(&self) -> String {
        let pct_suffix = Self::render_table_pct_suffix;
        let mut out = String::new();
        out.push_str(&format!(
            "event {}  chunk {} ms  frames {}  chunks {}  detections {}  drops {}{}\n\n",
            self.event_id,
            self.chunk_ms,
            self.frames_in,
            self.chunks_in,
            self.detections.len(),
            self.drops.total(),
            pct_suffix(self.drop_pct),
        ));
        if let Some(legs) = &self.per_leg {
            for l in legs {
                out.push_str(&format!(
                    "leg {:<14} frames {:>8}  chunks {:>6}  detections {:>3}  drops {:>4}{}{}\n",
                    l.label,
                    l.frames_in,
                    l.chunks_in,
                    l.detections,
                    l.drops.total(),
                    pct_suffix(l.drop_pct),
                    l.died
                        .as_deref()
                        .map(|d| format!("  DIED: {d}"))
                        .unwrap_or_default(),
                ));
            }
            out.push('\n');
        }
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
                        .map(|i| format!(
                            "{} x{} @≤{}¢",
                            i.market_ticker, i.tranche_contracts, i.max_price_cents
                        ))
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
        tracing_subscriber::fmt()
            .with_env_filter(filter)
            .json()
            .init();
    } else {
        tracing_subscriber::fmt().with_env_filter(filter).init();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 2026-08-05: MELI died on the pipe idle guard and wrote no report, which
    /// on disk is indistinguishable from an event that never armed. Every
    /// terminated run must be able to state its exit, and the reason must
    /// survive serialization in a form an ops script can match on.
    #[test]
    fn exit_record_serializes_reason_as_snake_case() {
        let v = serde_json::to_value(ExitRecord::with(
            ExitReason::SourceError,
            "pipe source idle past 30s — dead webcast stream",
        ))
        .unwrap();
        assert_eq!(v["reason"], "source_error");
        assert!(v["detail"].as_str().unwrap().contains("idle past 30s"));
    }

    /// A clean drain carries no detail — and the key must be ABSENT rather
    /// than null, so `jq 'has("detail")'` distinguishes "ended cleanly" from
    /// "ended badly but nobody recorded why".
    #[test]
    fn clean_exit_omits_detail_entirely() {
        let v = serde_json::to_value(ExitRecord::clean()).unwrap();
        assert_eq!(v["reason"], "clean_drain");
        assert!(v.get("detail").is_none(), "detail must be omitted, got {v}");
    }

    /// Every reason an ops script may branch on must round-trip to a distinct,
    /// stable string; renaming one silently breaks post-event grading.
    #[test]
    fn every_exit_reason_has_a_distinct_wire_name() {
        let all = [
            ExitReason::StartupError,
            ExitReason::CleanDrain,
            ExitReason::SourceError,
            ExitReason::AllLegsDied,
            ExitReason::DecisionStateError,
            ExitReason::OrderEvidenceError,
            ExitReason::Sigterm,
            ExitReason::StopFile,
            ExitReason::PanicOrJoin,
        ];
        let names: Vec<String> = all
            .iter()
            .map(|r| {
                serde_json::to_value(r)
                    .unwrap()
                    .as_str()
                    .unwrap()
                    .to_string()
            })
            .collect();
        let mut sorted = names.clone();
        sorted.sort();
        sorted.dedup();
        assert_eq!(sorted.len(), all.len(), "duplicate wire names in {names:?}");
        assert!(names.contains(&"startup_error".to_string()));
        assert!(names.contains(&"all_legs_died".to_string()));
        assert!(names.contains(&"decision_state_error".to_string()));
        assert!(names.contains(&"order_evidence_error".to_string()));
    }

    /// The SIGTERM stub is the only record an unattended kill leaves behind.
    /// It must answer the same question a full report answers — "how did this
    /// end?" — in the same place, or grading has to special-case it and will
    /// eventually forget to.
    #[test]
    fn sigterm_stub_carries_the_same_exit_shape_as_a_full_report() {
        let v = sigterm_stub_json(
            &["ord-1".to_string(), "ord-2".to_string()],
            &["client-ambiguous".to_string()],
            97,
        );
        assert_eq!(v["exit"]["reason"], "sigterm");
        assert_eq!(v["partial_report"], true);
        assert_eq!(v["ledger_spent_cents"], 97);
        assert_eq!(v["session_order_ids"].as_array().unwrap().len(), 2);
        assert_eq!(v["pending_client_order_ids"][0], "client-ambiguous");
        // Retained for readers that predate `exit`.
        assert_eq!(v["terminated"], "SIGTERM");
    }

    /// A run that dies before the pipeline yields anything still has to name
    /// its event and keep `orders` an array — ops scripts index it directly.
    #[test]
    fn partial_record_names_its_event_and_always_has_orders() {
        let v = build_run_json(
            "KX-EVENT",
            None,
            None,
            &ExitRecord::with(ExitReason::SourceError, "dead stream"),
        )
        .unwrap();
        assert_eq!(v["event_id"], "KX-EVENT");
        assert_eq!(v["partial_report"], true);
        assert!(v["orders"].is_array() && v["orders"].as_array().unwrap().is_empty());
        assert_eq!(v["executor_report_missing"], true);
    }

    /// hdrhistogram guarantees 3 significant digits — quantile values may be
    /// off by up to 0.1%, so assert within that.
    /// 2026-08-07. The 3.3 / 4.1 / 11.3% figures in the 26AUG05 post-mortem
    /// were hand arithmetic because no rate existed in the crate. The one that
    /// matters is the None case: a run that heard NOTHING must not report a
    /// clean 0%, because "armed but deaf" is a different failure from "armed
    /// and lossless" and AAOI-26AUG06 was graded benign on exactly that
    /// confusion.
    #[test]
    fn drop_pct_is_none_when_no_frame_arrived() {
        let none = DropCounters {
            source_to_preproc: 0,
            preproc_to_chunker: 0,
            chunker_to_detector: 0,
        };
        assert_eq!(none.pct_of(0), None, "no frames must not read as 0%");
        assert_eq!(none.pct_of(1000), Some(0.0));
        assert_eq!(PipelineReport::render_table_pct_suffix(None), "");
        assert_eq!(
            PipelineReport::render_table_pct_suffix(Some(0.0)),
            " (0.00%)"
        );

        // The real TLN-26AUG05 numbers: 9,395 dropped of 227,209 = 4.13%.
        let tln = DropCounters {
            source_to_preproc: 9_395,
            preproc_to_chunker: 0,
            chunker_to_detector: 0,
        };
        let pct = tln.pct_of(227_209).expect("frames present");
        assert!((pct - 4.135).abs() < 0.01, "got {pct}, want ~4.13");
        assert_eq!(
            PipelineReport::render_table_pct_suffix(Some(pct)),
            " (4.13%)"
        );

        // Only the ingress ring can drop, but the rate sums all three stages.
        let all = DropCounters {
            source_to_preproc: 1,
            preproc_to_chunker: 2,
            chunker_to_detector: 3,
        };
        assert_eq!(all.total(), 6);
        assert_eq!(all.pct_of(600), Some(1.0));
    }

    fn close(got: u64, want: u64) -> bool {
        let tol = (want / 500).max(1); // ±0.2% (two sig-digit buckets), min 1 µs
        got.abs_diff(want) <= tol
    }

    #[test]
    fn known_values_produce_correct_quantiles() {
        let mut r = LatencyRecorder::new();
        // 1..=100 ms in nanos: p50 = 50 ms, p95 = 95 ms, max = 100 ms.
        for ms in 1..=100u64 {
            r.record("send→ack", ms * 1_000_000);
        }
        let snap = r.snapshot();
        let s = &snap["send→ack"];
        assert_eq!(s.count, 100);
        assert!(close(s.p50_us, 50_000), "p50 {} µs", s.p50_us);
        assert!(close(s.p95_us, 95_000), "p95 {} µs", s.p95_us);
        assert!(close(s.max_us, 100_000), "max {} µs", s.max_us);
    }

    #[test]
    fn empty_edges_are_omitted_and_unknown_edges_are_noops() {
        let mut r = LatencyRecorder::new();
        r.record("capture→ack", 1000);
        r.record("not-an-edge", 1000); // silently ignored, never panics
        let snap = r.snapshot();
        assert_eq!(snap.len(), 1);
        assert!(snap.contains_key("capture→ack"));
    }

    #[test]
    fn latency_pass_edges_are_registered_not_silently_dropped() {
        // Regression guard: book_age was recorded by the executor but MISSING
        // from EDGES, so record() dropped it via the get_mut miss. handoff→exec
        // and ring_wake are the new detect→fire wake diagnostics. All must be
        // real histogram edges, i.e. actually show up in the snapshot.
        let mut r = LatencyRecorder::new();
        for edge in ["book_age", "handoff→exec", "ring_wake"] {
            r.record(edge, 1234);
        }
        let snap = r.snapshot();
        for edge in ["book_age", "handoff→exec", "ring_wake"] {
            assert_eq!(
                snap.get(edge).map(|s| s.count),
                Some(1),
                "edge {edge} dropped"
            );
        }
    }

    #[test]
    fn zero_clamps_and_sixty_seconds_saturates() {
        let mut r = LatencyRecorder::new();
        r.record("exec→send", 0); // clamped to 1 ns, still counted
        r.record("exec→send", 120_000_000_000); // > 60 s bound: saturates, no panic
        let s = &r.snapshot()["exec→send"];
        assert_eq!(s.count, 2);
        assert!(s.max_us >= 60_000_000, "saturated max {} µs", s.max_us);
    }

    #[test]
    fn merge_folds_counts_across_recorders() {
        // The exact arm-time fold: executor recorder merged into pipeline's.
        let mut pipeline = LatencyRecorder::new();
        let mut exec = LatencyRecorder::new();
        pipeline.record("capture→decision", 5_000_000);
        exec.record("send→ack", 90_000_000);
        exec.record("send→ack", 100_000_000);
        pipeline.merge(&exec);
        let snap = pipeline.snapshot();
        assert_eq!(snap["capture→decision"].count, 1);
        assert_eq!(snap["send→ack"].count, 2);
        assert!(close(snap["send→ack"].max_us, 100_000));
    }
}
