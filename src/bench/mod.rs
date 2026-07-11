//! Detector bake-off harness (docs/BAKEOFF.md): runs engine × chunk_ms grids
//! over the corpus and scores detections against manifest ground truth.
//!
//! Two run modes per grid cell:
//! - speed=0 (lossless) — accuracy: precision/recall per tier + FA/hour.
//!   Wall clock is meaningless here; matching uses `Detection.audio_pos_ms`.
//! - `--paced` (speed=1.0) — latency: onset→emit wall-clock percentiles,
//!   anchored on `PipelineReport.first_capture_ts`. Real-time, so run it on
//!   a subset (`--calls`).

pub mod manifest;
pub mod score;

use std::collections::BTreeMap;
use std::path::PathBuf;

use serde::Serialize;

use crate::bench::manifest::{CallManifest, Split};
use crate::bench::score::{percentile, MatchWindow, TierScore};
use crate::config::{self, EngineKind};
use crate::decision::DecisionTable;
use crate::detector::factory;
use crate::pipeline::preproc::PreprocCfg;
use crate::pipeline::run_pipeline;
use crate::source::replay::ReplaySource;
use crate::telemetry::DetectionRecord;
use crate::types::{SourceId, Tier};

type AnyErr = Box<dyn std::error::Error>;

pub struct BenchArgs {
    pub config_path: PathBuf,
    pub corpus: PathBuf,
    pub engines: Vec<EngineKind>,
    pub chunk_ms: Vec<u32>,
    pub split: Split,
    pub paced: bool,
    /// Optional call-id filter (paced runs are real-time — subset them).
    pub calls: Option<Vec<String>>,
}

#[derive(Debug, Serialize)]
pub struct BenchReport {
    pub split: String,
    pub paced: bool,
    pub corpus_hours: f64,
    pub calls: Vec<String>,
    pub cells: Vec<GridCell>,
}

#[derive(Debug, Serialize)]
pub struct GridCell {
    pub engine: String,
    pub chunk_ms: u32,
    pub rows: Vec<AggRow>,
    /// One "phrase tier call@pos_ms" line per false alarm — listen to the
    /// audio there to classify engine error vs aligner miss (JSON only).
    pub fa_autopsy: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct AggRow {
    pub phrase: String,
    pub tier: Tier,
    pub true_positives: u32,
    pub misses: u32,
    pub false_alarms: u32,
    pub recall: f64,
    pub precision: f64,
    pub fa_per_hour: f64,
    pub latency_p50_ms: Option<i64>,
    pub latency_p95_ms: Option<i64>,
    pub latency_max_ms: Option<i64>,
}

pub fn run_bench(args: &BenchArgs) -> Result<BenchReport, AnyErr> {
    let all = manifest::load_dir(&args.corpus)?;
    let calls: Vec<&CallManifest> = all
        .iter()
        .filter(|c| c.split == args.split)
        .filter(|c| args.calls.as_ref().map_or(true, |ids| ids.contains(&c.id)))
        .collect();
    if calls.is_empty() {
        return Err("no corpus calls match the split/call filter".into());
    }
    let corpus_hours: f64 = calls.iter().map(|c| c.duration_ms as f64 / 3_600_000.0).sum();

    // Warn once about ground-truth phrases the config can't score.
    let cfg = config::load(&args.config_path)?;
    let known: Vec<&str> = cfg.keywords.iter().map(|k| k.phrase.as_str()).collect();
    for call in &calls {
        for occ in &call.occurrences {
            if !known.contains(&occ.phrase.as_str()) {
                tracing::warn!(call = %call.id, phrase = %occ.phrase, "occurrence phrase not in config keywords — ignored");
            }
        }
    }

    let mut cells = Vec::new();
    for &engine in &args.engines {
        for &chunk_ms in &args.chunk_ms {
            cells.push(run_cell(args, engine, chunk_ms, &calls)?);
        }
    }
    Ok(BenchReport {
        split: format!("{:?}", args.split).to_lowercase(),
        paced: args.paced,
        corpus_hours,
        calls: calls.iter().map(|c| c.id.clone()).collect(),
        cells,
    })
}

fn run_cell(
    args: &BenchArgs,
    engine: EngineKind,
    chunk_ms: u32,
    calls: &[&CallManifest],
) -> Result<GridCell, AnyErr> {
    // Aggregate per (phrase, tier) across calls.
    let mut agg: BTreeMap<(String, Tier), TierScore> = BTreeMap::new();
    let mut fa_autopsy = Vec::new();
    let mut corpus_hours = 0f64;

    for call in calls {
        // Fresh config per run: engine/chunk overrides must not leak between
        // cells, and detectors are stateful (one per run).
        let mut cfg = config::load(&args.config_path)?;
        cfg.detector.engine = engine;
        cfg.pipeline.chunk_ms = chunk_ms;

        let speed = if args.paced { 1.0 } else { 0.0 };
        let source = Box::new(ReplaySource::from_wav(&call.audio, speed, SourceId(0))?);
        let detector = factory::build_detector(&cfg)?;
        let table = DecisionTable::from_keywords(&cfg.keywords);
        let preproc = PreprocCfg { resample_to_hz: cfg.pipeline.resample_to_hz };
        tracing::info!(engine = ?engine, chunk_ms, call = %call.id, paced = args.paced, "bench run");
        let report = run_pipeline(&call.id, chunk_ms, preproc, source, detector, table, None)?;
        if report.drops.total() > 0 {
            return Err(format!("bench run {} dropped {} frames", call.id, report.drops.total()).into());
        }

        corpus_hours += call.duration_ms as f64 / 3_600_000.0;
        let anchor = if args.paced { report.first_capture_ts } else { None };
        for (i, kw) in cfg.keywords.iter().enumerate() {
            let dets: Vec<&DetectionRecord> = report
                .detections
                .iter()
                .filter(|d| d.detection.keyword.0 as usize == i)
                .collect();
            let occs: Vec<&manifest::Occurrence> = call
                .occurrences
                .iter()
                .filter(|o| o.phrase == kw.phrase)
                .collect();
            for s in score::score_phrase(&kw.phrase, &dets, &occs, MatchWindow::default(), anchor) {
                for &pos in &s.false_alarm_pos_ms {
                    fa_autopsy.push(format!("{} {:?} {}@{}ms", s.phrase, s.tier, call.id, pos));
                }
                let entry = agg.entry((s.phrase.clone(), s.tier)).or_insert_with(|| TierScore {
                    phrase: s.phrase.clone(),
                    tier: s.tier,
                    true_positives: 0,
                    misses: 0,
                    false_alarms: 0,
                    false_alarm_pos_ms: Vec::new(),
                    latency_ms: Vec::new(),
                });
                entry.true_positives += s.true_positives;
                entry.misses += s.misses;
                entry.false_alarms += s.false_alarms;
                entry.latency_ms.extend(s.latency_ms);
            }
        }
    }

    let rows = agg
        .into_values()
        .map(|mut s| {
            s.latency_ms.sort_unstable();
            AggRow {
                recall: s.recall(),
                precision: s.precision(),
                fa_per_hour: s.false_alarms as f64 / corpus_hours.max(f64::EPSILON),
                latency_p50_ms: percentile(&s.latency_ms, 0.50),
                latency_p95_ms: percentile(&s.latency_ms, 0.95),
                latency_max_ms: s.latency_ms.last().copied(),
                phrase: s.phrase,
                tier: s.tier,
                true_positives: s.true_positives,
                misses: s.misses,
                false_alarms: s.false_alarms,
            }
        })
        .collect();
    Ok(GridCell { engine: format!("{engine:?}").to_lowercase(), chunk_ms, rows, fa_autopsy })
}

impl BenchReport {
    pub fn render_table(&self) -> String {
        let mut out = format!(
            "bake-off  split={}  paced={}  corpus={:.2}h  calls={}\n\n",
            self.split,
            self.paced,
            self.corpus_hours,
            self.calls.len()
        );
        out.push_str(&format!(
            "{:<12} {:>6} {:<26} {:>9} {:>4} {:>5} {:>4} {:>7} {:>9} {:>8} {:>8} {:>8}\n",
            "engine", "chunk", "phrase", "tier", "tp", "miss", "fa", "recall", "precision", "fa/h",
            "p50ms", "p95ms"
        ));
        for cell in &self.cells {
            for r in &cell.rows {
                out.push_str(&format!(
                    "{:<12} {:>6} {:<26} {:>9} {:>4} {:>5} {:>4} {:>7.2} {:>9.2} {:>8.2} {:>8} {:>8}\n",
                    cell.engine,
                    cell.chunk_ms,
                    r.phrase,
                    format!("{:?}", r.tier),
                    r.true_positives,
                    r.misses,
                    r.false_alarms,
                    r.recall,
                    r.precision,
                    r.fa_per_hour,
                    r.latency_p50_ms.map_or("-".into(), |v| v.to_string()),
                    r.latency_p95_ms.map_or("-".into(), |v| v.to_string()),
                ));
            }
        }
        out
    }
}
