//! `earshot verify-report` — retrospective verifier over a BANKED run (D2 V0):
//! for every Confirmed detection in the run report, slice the leg's own banked
//! audio (`capture/wav/source-<S>-segment-<N>-16000hz.wav`, concatenated in
//! seq order == the detector's `audio_pos_ms` timeline) at
//! `[audio_pos − tail_ms, audio_pos]`, run the offline transcriber, and record
//! the verdict. Zero risk, no live wiring; the rows join `detection-score.py`'s
//! TP/FP labels by (keyword, pos_ms) to give precision-per-verdict BEFORE the
//! verifier gates anything.

use std::path::{Path, PathBuf};

use serde::Serialize;

use super::transcriber::{SherpaOfflineTranscriber, TailTranscriber};
use crate::detector::text_match::VariantMatcher;
use crate::types::KeywordId;

#[derive(Debug, Serialize)]
pub struct RetroRow {
    pub keyword: u16,
    pub phrase: String,
    pub source: u16,
    pub leg: String,
    pub audio_pos_ms: u64,
    pub confidence: f32,
    pub intent: bool,
    pub verdict: String,
    pub variant: Option<usize>,
    pub transcript: String,
    pub tail_ms: u32,
    pub decode_us: u64,
}

#[derive(Debug, Serialize)]
pub struct RetroReport {
    pub run_dir: String,
    pub model_dir: String,
    pub model_type: String,
    pub tail_ms: u32,
    pub rows: Vec<RetroRow>,
    pub decode_p50_us: u64,
    pub decode_p95_us: u64,
}

struct Segment {
    path: PathBuf,
    offset_samples: u64,
    sample_count: u64,
}

fn leg_segments(
    run_dir: &Path,
    manifest: &serde_json::Value,
    source: u16,
) -> Result<Vec<Segment>, String> {
    let mut segs: Vec<(u64, PathBuf, u64)> = Vec::new();
    for s in manifest["wav_segments"]
        .as_array()
        .ok_or("manifest has no wav_segments")?
    {
        if s["source"].as_u64() != Some(source as u64) {
            continue;
        }
        let seg_no = s["segment"].as_u64().unwrap_or(0);
        let recorded = PathBuf::from(s["path"].as_str().unwrap_or(""));
        let path = if recorded.is_file() {
            recorded
        } else {
            // manifests carry the writing host's absolute paths; fall back to the run dir
            run_dir
                .join("capture")
                .join("wav")
                .join(recorded.file_name().unwrap_or_default())
        };
        segs.push((seg_no, path, s["sample_count"].as_u64().unwrap_or(0)));
    }
    segs.sort_by_key(|(n, _, _)| *n);
    let mut out = Vec::new();
    let mut off = 0u64;
    for (_, path, n) in segs {
        out.push(Segment {
            path,
            offset_samples: off,
            sample_count: n,
        });
        off += n;
    }
    Ok(out)
}

/// Read samples [start, end) on the leg timeline across segment files.
fn read_span(segs: &[Segment], start: u64, end: u64) -> Result<Vec<i16>, String> {
    let mut out = Vec::with_capacity((end - start) as usize);
    for s in segs {
        let s_end = s.offset_samples + s.sample_count;
        if s_end <= start || s.offset_samples >= end {
            continue;
        }
        let lo = start.max(s.offset_samples) - s.offset_samples;
        let hi = end.min(s_end) - s.offset_samples;
        let mut r = hound::WavReader::open(&s.path)
            .map_err(|e| format!("open {}: {e}", s.path.display()))?;
        r.seek(lo as u32)
            .map_err(|e| format!("seek {}: {e}", s.path.display()))?;
        for smp in r.samples::<i16>().take((hi - lo) as usize) {
            out.push(smp.map_err(|e| e.to_string())?);
        }
    }
    Ok(out)
}

pub fn run(
    run_dir: &Path,
    model_dir: &Path,
    model_type: &str,
    num_threads: u32,
    tail_ms: u32,
    config_override: Option<&Path>,
) -> Result<RetroReport, String> {
    let plan: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(run_dir.join("control").join("run-plan.json"))
            .map_err(|e| format!("run-plan.json: {e}"))?,
    )
    .map_err(|e| e.to_string())?;
    let repo = std::env::var("EARSHOT_REPO")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."));
    let cfg_path = match config_override {
        Some(p) => p.to_path_buf(),
        None => repo.join(
            plan["config_path"]
                .as_str()
                .ok_or("run-plan lacks config_path")?,
        ),
    };
    let cfg = crate::config::load(&cfg_path)
        .map_err(|e| format!("config {}: {e}", cfg_path.display()))?;
    let report_rel = plan["artifacts"]["runtime_report"]
        .as_str()
        .ok_or("run-plan lacks artifacts.runtime_report")?;
    let report: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(run_dir.join(report_rel)).map_err(|e| format!("report: {e}"))?,
    )
    .map_err(|e| e.to_string())?;
    let manifest: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(run_dir.join("capture").join("audio.manifest.json"))
            .map_err(|e| format!("audio.manifest.json: {e}"))?,
    )
    .map_err(|e| e.to_string())?;
    let rate: u32 = manifest["wav_segments"][0]["sample_rate_hz"]
        .as_u64()
        .unwrap_or(16_000) as u32;
    let labels: std::collections::HashMap<u16, String> = manifest["source_labels"]
        .as_object()
        .map(|m| {
            m.iter()
                .filter_map(|(k, v)| Some((k.parse().ok()?, v.as_str()?.to_string())))
                .collect()
        })
        .unwrap_or_default();

    let matcher = VariantMatcher::from_keywords(&cfg.keywords);
    let mut transcriber = SherpaOfflineTranscriber::new(model_dir, model_type, num_threads)?;
    let mut segs_by_source: std::collections::HashMap<u16, Vec<Segment>> = Default::default();
    let mut rows = Vec::new();
    let mut decode = Vec::new();
    let tail_samples = (tail_ms as u64) * rate as u64 / 1000;
    for r in report["detections"]
        .as_array()
        .ok_or("report has no detections")?
    {
        let d = &r["detection"];
        if d["tier"].as_str() != Some("Confirmed") {
            continue;
        }
        let kw = d["keyword"].as_u64().unwrap_or(0) as u16;
        let source = d["source"].as_u64().unwrap_or(0) as u16;
        let pos_ms = d["audio_pos_ms"].as_u64().unwrap_or(0);
        let segs = match segs_by_source.get(&source) {
            Some(s) => s,
            None => {
                let s = leg_segments(run_dir, &manifest, source)?;
                segs_by_source.insert(source, s);
                &segs_by_source[&source]
            }
        };
        let end = pos_ms * rate as u64 / 1000;
        let start = end.saturating_sub(tail_samples);
        let pcm = read_span(segs, start, end)?;
        let t0 = std::time::Instant::now();
        let text = transcriber.transcribe(&pcm, rate)?;
        let dus = t0.elapsed().as_micros() as u64;
        decode.push(dus);
        let variant = matcher.contains(KeywordId(kw), &text);
        rows.push(RetroRow {
            keyword: kw,
            phrase: cfg
                .keywords
                .get(kw as usize)
                .map(|k| k.phrase.clone())
                .unwrap_or_default(),
            source,
            leg: labels
                .get(&source)
                .cloned()
                .unwrap_or_else(|| r["leg"].as_str().unwrap_or("").to_string()),
            audio_pos_ms: pos_ms,
            confidence: d["confidence"].as_f64().unwrap_or(0.0) as f32,
            intent: !r["intent"].is_null(),
            verdict: if text.trim().is_empty() {
                "error:empty".into()
            } else if variant.is_some() {
                "confirmed".into()
            } else {
                "rejected".into()
            },
            variant,
            transcript: text,
            tail_ms: (pcm.len() as u64 * 1000 / rate as u64) as u32,
            decode_us: dus,
        });
    }
    decode.sort_unstable();
    let pct = |p: f64| {
        if decode.is_empty() {
            0
        } else {
            decode[((decode.len() - 1) as f64 * p) as usize]
        }
    };
    Ok(RetroReport {
        run_dir: run_dir.display().to_string(),
        model_dir: model_dir.display().to_string(),
        model_type: model_type.into(),
        tail_ms,
        decode_p50_us: pct(0.5),
        decode_p95_us: pct(0.95),
        rows,
    })
}
