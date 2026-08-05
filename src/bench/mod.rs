//! Detector bake-off harness (docs/BAKEOFF.md): runs engine × chunk_ms ×
//! model_dir grids over the corpus and scores detections against manifest
//! ground truth.
//!
//! The `model_dir` axis exists so two exports of the same architecture (e.g.
//! the 320 ms-quantum `…-ctc-small-2024-03-18` against a chunk-16 re-export at
//! 160 ms) can be compared INTERLEAVED IN ONE RUN. That matters more than it
//! sounds: this laptop has 3.5× wall-clock run variance, so a latency delta
//! measured across two separate invocations is not defensible, while cells
//! within one run share machine state (the E0c method, docs/LATENCY-PROOF.md).
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
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::bench::manifest::{CallManifest, Split};
use crate::bench::score::{percentile, MatchWindow, TierScore};
use crate::config::{self, EngineKind, EventConfig};
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
    /// Model directories to sweep, overriding the selected engine's
    /// `model_dir`. EMPTY = one cell that leaves the config alone, which is
    /// byte-for-byte the pre-knob behavior.
    pub model_dirs: Vec<PathBuf>,
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
    /// The model this cell ran, when `--model-dir` supplied one. `None` means
    /// the config's own `model_dir` was used and is the only model in the grid.
    /// Recorded as the full path: a latency delta is meaningless without
    /// knowing exactly which two artifacts produced it.
    pub model_dir: Option<String>,
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
    /// Audio-domain lag behind word end (`score::TierScore::emit_lag_ms`).
    /// Present in LOSSLESS runs too, unlike the `latency_*` columns.
    pub emit_lag_p50_ms: Option<i64>,
    pub emit_lag_p95_ms: Option<i64>,
    pub emit_lag_max_ms: Option<i64>,
    pub emit_lag_min_ms: Option<i64>,
    /// Cross-engine-comparable lag (`score::TierScore::consumed_lag_ms`). This
    /// is the one to compare BETWEEN engines; `emit_lag_*` is engine-defined.
    pub consumed_lag_p50_ms: Option<i64>,
    pub consumed_lag_p95_ms: Option<i64>,
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

    // Empty `model_dirs` = a single cell that leaves the config's model_dir
    // alone, so an existing invocation produces an identical grid.
    let model_axis: Vec<Option<&Path>> = if args.model_dirs.is_empty() {
        vec![None]
    } else {
        args.model_dirs.iter().map(|p| Some(p.as_path())).collect()
    };

    let mut cells = Vec::new();
    for &engine in &args.engines {
        for &chunk_ms in &args.chunk_ms {
            for &model_dir in &model_axis {
                cells.push(run_cell(args, engine, chunk_ms, model_dir, &calls)?);
            }
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

/// Point the selected engine at `dir`.
///
/// REFUSES rather than silently ignoring. A `--model-dir` that no-ops would
/// produce two cells that ran the SAME artifact and read as "the model made no
/// difference" — the exact shape of the `chunk_ms` 40/80/160 result that turned
/// out to be byte-identical cells (docs/LATENCY-PROOF.md §E0c). A refusal is a
/// bad command line; a silent no-op is a false measurement in BAKEOFF.md.
fn apply_model_dir(cfg: &mut EventConfig, engine: EngineKind, dir: &Path) -> Result<(), AnyErr> {
    if !dir.is_dir() {
        return Err(format!("--model-dir {} is not a directory", dir.display()).into());
    }
    match engine {
        EngineKind::StreamingCtc => match cfg.detector.streaming_ctc.as_mut() {
            Some(s) => s.model_dir = dir.to_path_buf(),
            None => {
                return Err("--model-dir with engine=streaming_ctc needs a \
                            [detector.streaming_ctc] section to override"
                    .into())
            }
        },
        EngineKind::SherpaKws => match cfg.detector.sherpa_kws.as_mut() {
            Some(s) => s.model_dir = dir.to_path_buf(),
            None => {
                return Err("--model-dir with engine=sherpa_kws needs a \
                            [detector.sherpa_kws] section to override"
                    .into())
            }
        },
        EngineKind::Stub => {
            return Err("engine=stub loads no model — --model-dir would be ignored, \
                        producing duplicate cells that look like a null result"
                .into())
        }
    }
    Ok(())
}

fn run_cell(
    args: &BenchArgs,
    engine: EngineKind,
    chunk_ms: u32,
    model_dir: Option<&Path>,
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
        if let Some(dir) = model_dir {
            apply_model_dir(&mut cfg, engine, dir)?;
        }

        let speed = if args.paced { 1.0 } else { 0.0 };
        let source = Box::new(ReplaySource::from_wav(&call.audio, speed, SourceId(0))?);
        let detector = factory::build_detector(&cfg)?;
        let table = DecisionTable::from_keywords(&cfg.keywords);
        let preproc = PreprocCfg { resample_to_hz: cfg.pipeline.resample_to_hz };
        tracing::info!(
            engine = ?engine,
            chunk_ms,
            model_dir = model_dir.map(|d| d.display().to_string()),
            call = %call.id,
            paced = args.paced,
            "bench run"
        );
        let report = run_pipeline(&call.id, chunk_ms, preproc, source, detector, table, None, Default::default())?;
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
                    emit_lag_ms: Vec::new(),
                    consumed_lag_ms: Vec::new(),
                });
                entry.true_positives += s.true_positives;
                entry.misses += s.misses;
                entry.false_alarms += s.false_alarms;
                entry.latency_ms.extend(s.latency_ms);
                entry.emit_lag_ms.extend(s.emit_lag_ms);
                entry.consumed_lag_ms.extend(s.consumed_lag_ms);
            }
        }
    }

    let rows = agg
        .into_values()
        .map(|mut s| {
            s.latency_ms.sort_unstable();
            s.emit_lag_ms.sort_unstable();
            s.consumed_lag_ms.sort_unstable();
            AggRow {
                recall: s.recall(),
                precision: s.precision(),
                fa_per_hour: s.false_alarms as f64 / corpus_hours.max(f64::EPSILON),
                latency_p50_ms: percentile(&s.latency_ms, 0.50),
                latency_p95_ms: percentile(&s.latency_ms, 0.95),
                latency_max_ms: s.latency_ms.last().copied(),
                emit_lag_p50_ms: percentile(&s.emit_lag_ms, 0.50),
                emit_lag_p95_ms: percentile(&s.emit_lag_ms, 0.95),
                emit_lag_max_ms: s.emit_lag_ms.last().copied(),
                emit_lag_min_ms: s.emit_lag_ms.first().copied(),
                consumed_lag_p50_ms: percentile(&s.consumed_lag_ms, 0.50),
                consumed_lag_p95_ms: percentile(&s.consumed_lag_ms, 0.95),
                phrase: s.phrase,
                tier: s.tier,
                true_positives: s.true_positives,
                misses: s.misses,
                false_alarms: s.false_alarms,
            }
        })
        .collect();
    Ok(GridCell {
        engine: format!("{engine:?}").to_lowercase(),
        chunk_ms,
        model_dir: model_dir.map(|d| d.display().to_string()),
        rows,
        fa_autopsy,
    })
}

const MODEL_COL_MAX: usize = 28;

/// Last path component, tail-elided — full model paths are 60+ chars and would
/// wreck the table. Elision keeps the TAIL because sibling exports differ at
/// the end (`…-ctc-small-2024-03-18` vs `…-ctc-small-chunk16`), never the
/// start. The JSON keeps the full path.
fn model_label(dir: &str) -> String {
    let base = dir.rsplit('/').find(|s| !s.is_empty()).unwrap_or(dir);
    let n = base.chars().count();
    if n <= MODEL_COL_MAX {
        return base.to_string();
    }
    let tail: String = base.chars().skip(n - (MODEL_COL_MAX - 1)).collect();
    format!("…{tail}")
}

impl BenchReport {
    pub fn render_table(&self) -> String {
        // Only widen the table when the grid actually swept models; a
        // single-model run stays byte-identical to the format the BAKEOFF.md
        // tables were pasted from.
        let show_model = self.cells.iter().any(|c| c.model_dir.is_some());
        let model_w = self
            .cells
            .iter()
            .filter_map(|c| c.model_dir.as_deref())
            .map(|d| model_label(d).chars().count())
            .chain(std::iter::once("model".len()))
            .max()
            .unwrap_or(5);

        let mut out = format!(
            "bake-off  split={}  paced={}  corpus={:.2}h  calls={}\n\n",
            self.split,
            self.paced,
            self.corpus_hours,
            self.calls.len()
        );
        let model_head =
            if show_model { format!("{:<model_w$} ", "model") } else { String::new() };
        out.push_str(&format!(
            "{:<12} {:>6} {model_head}{:<26} {:>9} {:>4} {:>5} {:>4} {:>7} {:>9} {:>8} {:>8} {:>8} {:>9} {:>9} {:>9}\n",
            "engine", "chunk", "phrase", "tier", "tp", "miss", "fa", "recall", "precision", "fa/h",
            "p50ms", "p95ms", "lag50ms", "lag95ms", "cons50ms"
        ));
        for cell in &self.cells {
            let model_col = if show_model {
                let label =
                    cell.model_dir.as_deref().map_or_else(|| "-".to_string(), model_label);
                format!("{label:<model_w$} ")
            } else {
                String::new()
            };
            for r in &cell.rows {
                out.push_str(&format!(
                    "{:<12} {:>6} {model_col}{:<26} {:>9} {:>4} {:>5} {:>4} {:>7.2} {:>9.2} {:>8.2} {:>8} {:>8} {:>9} {:>9} {:>9}\n",
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
                    r.emit_lag_p50_ms.map_or("-".into(), |v| v.to_string()),
                    r.emit_lag_p95_ms.map_or("-".into(), |v| v.to_string()),
                    r.consumed_lag_p50_ms.map_or("-".into(), |v| v.to_string()),
                ));
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn example_cfg(extra: &str) -> EventConfig {
        let base = std::fs::read_to_string(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("configs/example-event.toml"),
        )
        .unwrap();
        toml::from_str(&format!("{base}\n{extra}")).unwrap()
    }

    fn cfg_dir() -> PathBuf {
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("configs")
    }

    #[test]
    fn model_label_is_the_basename() {
        assert_eq!(model_label("models/ctc-small-2024-03-18"), "ctc-small-2024-03-18");
        assert_eq!(model_label("models/ctc-16/"), "ctc-16");
        assert_eq!(model_label("bare"), "bare");
    }

    /// Sibling exports share a long prefix and differ at the END, so elision
    /// must keep the tail — a head-truncated label would render two different
    /// models as the same string.
    #[test]
    fn long_labels_elide_the_head_and_stay_distinguishable() {
        let a = model_label("models/sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18");
        let b = model_label("models/sherpa-onnx-streaming-zipformer-ctc-small-chunk16");
        assert_eq!(a.chars().count(), MODEL_COL_MAX);
        assert_eq!(b.chars().count(), MODEL_COL_MAX);
        assert_ne!(a, b, "elided labels collided: {a} vs {b}");
        assert!(a.ends_with("2024-03-18") && b.ends_with("chunk16"), "{a} / {b}");
        assert!(a.starts_with('…') && b.starts_with('…'));
    }

    /// A `--model-dir` that cannot be applied must ERROR, never no-op. A silent
    /// no-op emits two cells that ran the SAME artifact and reads as "the model
    /// made no difference" — indistinguishable from a real null result, which is
    /// exactly how the chunk_ms 40/80/160 cells came out byte-identical
    /// (docs/LATENCY-PROOF.md §E0c).
    #[test]
    fn model_dir_refuses_rather_than_being_ignored() {
        // stub loads no model at all
        let mut cfg = example_cfg("");
        let err = apply_model_dir(&mut cfg, EngineKind::Stub, &cfg_dir()).unwrap_err().to_string();
        assert!(err.contains("stub"), "{err}");

        // engine selected, but no matching section exists to override
        let mut cfg = example_cfg("");
        assert!(apply_model_dir(&mut cfg, EngineKind::StreamingCtc, &cfg_dir()).is_err());
        let mut cfg = example_cfg("");
        assert!(apply_model_dir(&mut cfg, EngineKind::SherpaKws, &cfg_dir()).is_err());

        // a path that is not a directory (typo'd export → would fall back to
        // the configured model and silently duplicate the baseline cell)
        let mut cfg = example_cfg("[detector.streaming_ctc]\nmodel_dir = \"models/orig\"\n");
        let missing = cfg_dir().join("definitely-not-a-real-export");
        assert!(apply_model_dir(&mut cfg, EngineKind::StreamingCtc, &missing).is_err());
    }

    #[test]
    fn model_dir_overrides_the_configured_model() {
        let mut cfg = example_cfg("[detector.streaming_ctc]\nmodel_dir = \"models/orig\"\n");
        apply_model_dir(&mut cfg, EngineKind::StreamingCtc, &cfg_dir()).unwrap();
        assert_eq!(cfg.detector.streaming_ctc.as_ref().unwrap().model_dir, cfg_dir());

        let mut cfg = example_cfg("[detector.sherpa_kws]\nmodel_dir = \"models/orig\"\n");
        apply_model_dir(&mut cfg, EngineKind::SherpaKws, &cfg_dir()).unwrap();
        assert_eq!(cfg.detector.sherpa_kws.as_ref().unwrap().model_dir, cfg_dir());
    }

    fn report_with(model_dirs: Vec<Option<&str>>) -> BenchReport {
        BenchReport {
            split: "dev".into(),
            paced: false,
            corpus_hours: 1.0,
            calls: vec!["c1".into()],
            cells: model_dirs
                .into_iter()
                .map(|m| GridCell {
                    engine: "streaming_ctc".into(),
                    chunk_ms: 80,
                    model_dir: m.map(str::to_string),
                    rows: Vec::new(),
                    fa_autopsy: Vec::new(),
                })
                .collect(),
        }
    }

    /// A single-model run must render exactly the pre-knob table — BAKEOFF.md's
    /// historical tables were pasted from that format.
    #[test]
    fn table_gains_a_model_column_only_when_models_were_swept() {
        let head = |r: &BenchReport| r.render_table().lines().nth(2).unwrap().to_string();

        assert!(!head(&report_with(vec![None])).contains("model"));
        let swept = head(&report_with(vec![Some("models/a"), Some("models/b")]));
        assert!(swept.contains("model"), "{swept}");
        // column sits between chunk and phrase
        let (c, m, p) = (
            swept.find("chunk").unwrap(),
            swept.find("model").unwrap(),
            swept.find("phrase").unwrap(),
        );
        assert!(c < m && m < p, "{swept}");
    }
}
