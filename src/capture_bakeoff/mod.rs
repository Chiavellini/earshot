//! Telephony ingress bake-off harness (Work Order §3; docs/INGRESS-BAKEOFF.md).
//!
//! Stands up two (or more) capture paths — Twilio Media Streams and/or a
//! direct SIP leg — against the *same* live call, records both PCM streams
//! stamped at the socket boundary, and reports, per path: inter-frame jitter
//! percentiles (p50/p95/**p99** — tail matters most), stalls > 100 ms, and the
//! true cross-stream delta in ms recovered by cross-correlation on our actual
//! bridges. Emits a provider→lead recommendation that states the runner-up's
//! ms cost, so the tradeoff is explicit rather than asserted.
//!
//! This is a diagnostic tap: it records audio to disk (opt-in, non-production —
//! see docs/TWILIO.md) and does NOT route through the detector pipeline.

use std::path::PathBuf;
use std::time::Duration;

use serde::Serialize;
use tokio::time::Instant;

use crate::clock::MonotonicNs;
use crate::ring::{self, RingReceiver};
use crate::source::AudioSource;
use crate::xcorr;

type AnyErr = Box<dyn std::error::Error>;

/// A long inter-frame interval on a 20 ms stream is a total-loss hazard: one
/// stalled frame can be the word we needed (Work Order §0).
const STALL_THRESHOLD: Duration = Duration::from_millis(100);
const LEG_RING_CAP: usize = 4096; // generous: the drain loop must never drop
/// Cross-correlation search bounds (samples are cheap here; this is offline).
/// The ±30 s lag window exists to see the webcast tuned-vs-default join gap
/// (~2 HLS segments plus probe delta); a full-rate naive sweep over that
/// window is infeasible, so `wide_lag` searches in two stages.
const XCORR_WINDOW_SECS: u64 = 60;
const XCORR_MAX_LAG_SECS: u64 = 30;
/// Coarse-pass decimation factor (16 kHz → 1 kHz) for the two-stage search.
const XCORR_DECIM: usize = 16;

pub struct CaptureArgs {
    /// Built capture legs (the arbiter/`build_leg` seam constructs these).
    pub legs: Vec<Box<dyn AudioSource>>,
    /// Provider labels, one per leg (feeds the provider→lead table).
    pub labels: Vec<String>,
    /// Safety cap on capture wall-time; legs that end sooner (replay EOS, call
    /// hangup) stop naturally.
    pub max_secs: u64,
    /// If set, write `<label>.wav` + `<label>.jsonl` (per-frame capture_ts) here.
    pub record_dir: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct CaptureReport {
    pub legs: Vec<LegReport>,
    /// Each non-reference leg vs leg 0: the recovered inter-stream delta.
    pub cross: Vec<CrossPair>,
    pub recommendation: Recommendation,
}

#[derive(Debug, Serialize)]
pub struct LegReport {
    pub label: String,
    pub frames: u64,
    pub samples: u64,
    pub sample_rate_hz: u32,
    pub duration_ms: u64,
    /// Inter-frame jitter = |interval − median interval|, in microseconds.
    pub jitter_p50_us: u64,
    pub jitter_p95_us: u64,
    pub jitter_p99_us: u64,
    pub jitter_max_us: u64,
    /// Inter-frame intervals longer than 100 ms (each is a trade-loss hazard).
    pub stalls_over_100ms: u64,
    pub wav_path: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct CrossPair {
    pub reference: String,
    pub other: String,
    /// `other` arrival minus `reference` arrival for the same audio content;
    /// positive means `other` is the laggier path.
    pub delta_ms: f64,
    /// Cross-correlation confidence in [-1, 1]; distrust delta below ~0.5.
    pub peak: f64,
}

#[derive(Debug, Serialize)]
pub struct Recommendation {
    pub winner: String,
    pub runner_up: Option<String>,
    /// The explicit tradeoff: how much p99 tail jitter the runner-up costs.
    pub runner_up_cost_ms: Option<f64>,
    pub basis: String,
}

/// One captured frame: arrival stamp + decoded PCM.
struct Frame {
    capture_ts: MonotonicNs,
    pcm: std::sync::Arc<[i16]>,
}

struct LegCapture {
    label: String,
    sample_rate_hz: u32,
    frames: Vec<Frame>,
}

pub fn run_capture_bakeoff(args: CaptureArgs) -> Result<CaptureReport, AnyErr> {
    if args.legs.len() != args.labels.len() {
        return Err("labels must match legs one-to-one".into());
    }
    if args.legs.len() < 2 {
        return Err("capture bake-off needs at least 2 legs".into());
    }
    for (i, l) in args.labels.iter().enumerate() {
        if args.labels[..i].contains(l) {
            return Err(format!(
                "duplicate leg label {l:?} — <label>.wav recordings would collide; \
                 set a distinct `label` on the [[sources]] entry"
            )
            .into());
        }
    }

    // Drive all legs on one current-thread runtime (IO enabled for network
    // sources), same pattern as run_pipeline.
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let captures = rt.block_on(capture_all(args.legs, &args.labels, args.max_secs));

    // Optional recording.
    let mut wav_paths = vec![None; captures.len()];
    if let Some(dir) = &args.record_dir {
        std::fs::create_dir_all(dir)?;
        for (i, cap) in captures.iter().enumerate() {
            wav_paths[i] = Some(write_leg(dir, cap)?);
        }
    }

    let legs: Vec<LegReport> = captures
        .iter()
        .enumerate()
        .map(|(i, c)| analyze_leg(c, wav_paths[i].clone()))
        .collect();
    let cross = cross_deltas(&captures);
    let recommendation = recommend(&legs);

    Ok(CaptureReport { legs, cross, recommendation })
}

/// Spawn every leg into its own ring and drain concurrently until all legs end
/// or `max_secs` elapses.
async fn capture_all(
    legs: Vec<Box<dyn AudioSource>>,
    labels: &[String],
    max_secs: u64,
) -> Vec<LegCapture> {
    let mut rxs: Vec<RingReceiver<crate::types::AudioFrame>> = Vec::new();
    let mut handles = Vec::new();
    let mut caps: Vec<LegCapture> = Vec::new();
    for (i, inner) in legs.into_iter().enumerate() {
        let (tx, rx) = ring::ring(LEG_RING_CAP);
        rxs.push(rx);
        handles.push(tokio::spawn(async move { inner.run(tx).await }));
        caps.push(LegCapture { label: labels[i].clone(), sample_rate_hz: 0, frames: Vec::new() });
    }

    let deadline = Instant::now() + Duration::from_secs(max_secs);
    loop {
        let mut progressed = false;
        for (i, rx) in rxs.iter_mut().enumerate() {
            while let Some(frame) = rx.try_recv() {
                progressed = true;
                if caps[i].sample_rate_hz == 0 {
                    caps[i].sample_rate_hz = frame.sample_rate_hz;
                }
                caps[i].frames.push(Frame { capture_ts: frame.capture_ts, pcm: frame.pcm });
            }
        }
        if Instant::now() >= deadline {
            tracing::warn!(max_secs, "capture bake-off: max duration reached — stopping");
            for h in &handles {
                h.abort();
            }
            break;
        }
        if !progressed && handles.iter().all(|h| h.is_finished()) {
            break;
        }
        if progressed {
            tokio::task::yield_now().await;
        } else {
            tokio::time::sleep(Duration::from_micros(200)).await;
        }
    }
    caps
}

fn analyze_leg(cap: &LegCapture, wav_path: Option<String>) -> LegReport {
    let samples: u64 = cap.frames.iter().map(|f| f.pcm.len() as u64).sum();
    let sr = cap.sample_rate_hz.max(1);
    let duration_ms = samples * 1000 / sr as u64;

    // Inter-frame intervals (ns) from capture_ts deltas.
    let mut intervals: Vec<u64> = cap
        .frames
        .windows(2)
        .map(|w| w[1].capture_ts.saturating_since(w[0].capture_ts))
        .collect();
    let stalls = intervals.iter().filter(|&&d| d >= STALL_THRESHOLD.as_nanos() as u64).count() as u64;

    intervals.sort_unstable();
    let median = if intervals.is_empty() { 0 } else { intervals[intervals.len() / 2] };
    // Jitter = absolute deviation of each interval from the median interval.
    let mut jitter: Vec<u64> = intervals.iter().map(|&d| d.abs_diff(median)).collect();
    jitter.sort_unstable();

    LegReport {
        label: cap.label.clone(),
        frames: cap.frames.len() as u64,
        samples,
        sample_rate_hz: cap.sample_rate_hz,
        duration_ms,
        jitter_p50_us: pct(&jitter, 0.50) / 1000,
        jitter_p95_us: pct(&jitter, 0.95) / 1000,
        jitter_p99_us: pct(&jitter, 0.99) / 1000,
        jitter_max_us: jitter.last().copied().unwrap_or(0) / 1000,
        stalls_over_100ms: stalls,
        wav_path,
    }
}

/// Nearest-rank percentile over a pre-sorted slice (ns in, ns out).
fn pct(sorted: &[u64], q: f64) -> u64 {
    if sorted.is_empty() {
        return 0;
    }
    let rank = ((q * sorted.len() as f64).ceil() as usize).clamp(1, sorted.len());
    sorted[rank - 1]
}

/// True inter-stream delta of each leg vs leg 0, recovered by aligning the
/// PCM (content lag) and adding the difference in first-frame arrival stamps.
fn cross_deltas(caps: &[LegCapture]) -> Vec<CrossPair> {
    let mut out = Vec::new();
    let Some(ref_cap) = caps.first() else { return out };
    if ref_cap.frames.is_empty() {
        return out;
    }
    let sr = ref_cap.sample_rate_hz.max(1) as f64;
    let ref_pcm = concat(&ref_cap.frames, XCORR_WINDOW_SECS, ref_cap.sample_rate_hz);
    let ref_t0 = ref_cap.frames[0].capture_ts;
    let max_lag = (XCORR_MAX_LAG_SECS * ref_cap.sample_rate_hz as u64) as usize;

    for other in &caps[1..] {
        if other.frames.is_empty() {
            continue;
        }
        let opcm = concat(&other.frames, XCORR_WINDOW_SECS, other.sample_rate_hz);
        let (lag, peak) = wide_lag(&ref_pcm, &opcm, max_lag);
        // ref[i] ≈ other[i+lag]: content at ref index i sits at other index
        // i+lag. Arrival of that content: ref_t0 + i/sr vs other_t0 + (i+lag)/sr.
        let start_skew_ms = other.frames[0].capture_ts.0 as f64 / 1e6 - ref_t0.0 as f64 / 1e6;
        let content_lag_ms = lag as f64 / sr * 1000.0;
        out.push(CrossPair {
            reference: ref_cap.label.clone(),
            other: other.label.clone(),
            delta_ms: start_skew_ms + content_lag_ms,
            peak,
        });
    }
    out
}

/// Two-stage wide-window lag search: coarse `best_lag` on block-mean
/// decimated audio, then a sample-exact `best_lag_in` refine around the
/// coarse peak. Coarse quantization error is ±1 decimated step, so a
/// ±4·DECIM full-rate margin covers it with room to spare. Result precision
/// is identical to a full-rate sweep (the sub-second telephony deltas that
/// feed the virtual race stay sample-exact) at a small fraction of the cost.
fn wide_lag(a: &[i16], b: &[i16], max_lag: usize) -> (i64, f64) {
    let da = decimate(a, XCORR_DECIM);
    let db = decimate(b, XCORR_DECIM);
    let (coarse, _) = xcorr::best_lag(&da, &db, max_lag / XCORR_DECIM);
    let center = coarse * XCORR_DECIM as i64;
    let margin = (4 * XCORR_DECIM) as i64;
    xcorr::best_lag_in(a, b, center - margin, center + margin)
}

/// Block-mean decimation. Mean, not stride sampling — striding aliases
/// high-frequency content, which can move the correlation peak.
fn decimate(x: &[i16], factor: usize) -> Vec<i16> {
    x.chunks(factor)
        .map(|c| (c.iter().map(|&s| s as i32).sum::<i32>() / c.len() as i32) as i16)
        .collect()
}

fn concat(frames: &[Frame], window_secs: u64, sr: u32) -> Vec<i16> {
    let cap = (window_secs * sr as u64) as usize;
    let mut out = Vec::with_capacity(cap.min(1 << 22));
    for f in frames {
        if out.len() >= cap {
            break;
        }
        out.extend_from_slice(&f.pcm);
    }
    out
}

/// Rank by p99 tail jitter (the Work Order's first priority), then stalls.
fn recommend(legs: &[LegReport]) -> Recommendation {
    // A dead leg (0 frames) reports zero jitter — exclude it so it can't
    // outrank live legs (a 3-leg run with one failed leg "recommended" the
    // dead one before this filter).
    let mut ranked: Vec<&LegReport> = legs.iter().filter(|l| l.frames > 0).collect();
    ranked.sort_by(|a, b| {
        a.jitter_p99_us
            .cmp(&b.jitter_p99_us)
            .then(a.stalls_over_100ms.cmp(&b.stalls_over_100ms))
    });
    let winner = ranked.first();
    let runner = ranked.get(1);
    Recommendation {
        winner: winner.map(|l| l.label.clone()).unwrap_or_default(),
        runner_up: runner.map(|l| l.label.clone()),
        runner_up_cost_ms: match (winner, runner) {
            (Some(w), Some(r)) => {
                Some((r.jitter_p99_us as f64 - w.jitter_p99_us as f64) / 1000.0)
            }
            _ => None,
        },
        basis: "lowest p99 inter-frame jitter, tie-broken on stalls (tail-first)".into(),
    }
}

fn write_leg(dir: &std::path::Path, cap: &LegCapture) -> Result<String, AnyErr> {
    let safe: String = cap.label.chars().map(|c| if c.is_alphanumeric() { c } else { '_' }).collect();
    let wav = dir.join(format!("{safe}.wav"));
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate: cap.sample_rate_hz.max(8000),
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let mut w = hound::WavWriter::create(&wav, spec)?;
    for f in &cap.frames {
        for &s in f.pcm.iter() {
            w.write_sample(s)?;
        }
    }
    w.finalize()?;

    // Per-frame sidecar: capture_ts + sample count, the record used to audit
    // jitter/loss offline.
    let jsonl = dir.join(format!("{safe}.jsonl"));
    let mut lines = String::new();
    for (seq, f) in cap.frames.iter().enumerate() {
        lines.push_str(&format!(
            "{{\"seq\":{seq},\"capture_ns\":{},\"samples\":{}}}\n",
            f.capture_ts.0,
            f.pcm.len()
        ));
    }
    std::fs::write(&jsonl, lines)?;
    Ok(wav.display().to_string())
}

impl CaptureReport {
    pub fn render_table(&self) -> String {
        let mut out = String::from("ingress bake-off\n\n");
        out.push_str(&format!(
            "{:<14} {:>7} {:>9} {:>10} {:>10} {:>10} {:>7}\n",
            "provider", "frames", "dur_ms", "jit_p50µs", "jit_p95µs", "jit_p99µs", "stalls"
        ));
        for l in &self.legs {
            out.push_str(&format!(
                "{:<14} {:>7} {:>9} {:>10} {:>10} {:>10} {:>7}\n",
                l.label, l.frames, l.duration_ms, l.jitter_p50_us, l.jitter_p95_us, l.jitter_p99_us,
                l.stalls_over_100ms
            ));
        }
        out.push_str("\ncross-stream delta (vs reference):\n");
        for c in &self.cross {
            out.push_str(&format!(
                "  {} → {}: {:+.1} ms  (peak {:.2})\n",
                c.reference, c.other, c.delta_ms, c.peak
            ));
        }
        let r = &self.recommendation;
        out.push_str(&format!("\nrecommend: {} ({})\n", r.winner, r.basis));
        if let (Some(ru), Some(cost)) = (&r.runner_up, r.runner_up_cost_ms) {
            out.push_str(&format!("  runner-up {ru} costs {cost:+.2} ms p99 jitter\n"));
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::source::replay::ReplaySource;
    use crate::types::SourceId;

    /// Decorrelated-noise WAV so cross-correlation has a sharp peak (a pure
    /// tone would be ambiguous at the tone period).
    fn noise_wav(path: &std::path::Path, n_samples: usize, lead_silence: usize) {
        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: 8000,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut w = hound::WavWriter::create(path, spec).unwrap();
        for _ in 0..lead_silence {
            w.write_sample(0i16).unwrap();
        }
        for i in 0..n_samples {
            let h = (i as u64)
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            w.write_sample((h >> 33) as i16).unwrap();
        }
        w.finalize().unwrap();
    }

    #[test]
    fn recovers_known_cross_delta() {
        let dir = tempfile::tempdir().unwrap();
        let a = dir.path().join("a.wav");
        let b = dir.path().join("b.wav");
        // Leg B is leg A delayed by 800 samples (100 ms @ 8 kHz).
        noise_wav(&a, 16000, 0);
        noise_wav(&b, 16000, 800);

        let leg_a = Box::new(ReplaySource::from_wav(&a, 0.0, SourceId(0)).unwrap());
        let leg_b = Box::new(ReplaySource::from_wav(&b, 0.0, SourceId(1)).unwrap());
        let report = run_capture_bakeoff(CaptureArgs {
            legs: vec![leg_a, leg_b],
            labels: vec!["twilio".into(), "telnyx".into()],
            max_secs: 10,
            record_dir: None,
        })
        .unwrap();

        assert_eq!(report.legs.len(), 2);
        assert_eq!(report.cross.len(), 1);
        let c = &report.cross[0];
        // Fast-mode replay stamps both starts near-together, so the delta is
        // dominated by the 100 ms content lead. Peak must be confident.
        assert!(c.peak > 0.9, "weak alignment peak {}", c.peak);
        assert!(
            (95.0..=105.0).contains(&c.delta_ms),
            "expected ~100 ms cross delta, got {:.1}",
            c.delta_ms
        );
    }

    #[test]
    fn recovers_multisecond_cross_delta() {
        // Webcast tuned-vs-default join gaps are ~2 HLS segments (≈ 10 s) —
        // far beyond the old ±5 s window. Leg B is leg A delayed by 64 000
        // samples (8 s @ 8 kHz); the two-stage wide_lag must recover it.
        let dir = tempfile::tempdir().unwrap();
        let a = dir.path().join("a.wav");
        let b = dir.path().join("b.wav");
        noise_wav(&a, 96000, 0);
        noise_wav(&b, 96000, 64000);

        let report = run_capture_bakeoff(CaptureArgs {
            legs: vec![
                Box::new(ReplaySource::from_wav(&a, 0.0, SourceId(0)).unwrap()),
                Box::new(ReplaySource::from_wav(&b, 0.0, SourceId(1)).unwrap()),
            ],
            labels: vec!["pipe".into(), "pipe_tuned".into()],
            max_secs: 30,
            record_dir: None,
        })
        .unwrap();

        let c = &report.cross[0];
        assert!(c.peak > 0.8, "weak alignment peak {}", c.peak);
        assert!(
            (7900.0..=8100.0).contains(&c.delta_ms),
            "expected ~8000 ms cross delta, got {:.1}",
            c.delta_ms
        );
    }

    #[test]
    fn dead_leg_is_never_recommended() {
        let live = LegReport {
            label: "pipe".into(),
            frames: 1000,
            samples: 320_000,
            sample_rate_hz: 16000,
            duration_ms: 20_000,
            jitter_p50_us: 5,
            jitter_p95_us: 40,
            jitter_p99_us: 90,
            jitter_max_us: 200,
            stalls_over_100ms: 0,
            wav_path: None,
        };
        let dead = LegReport {
            label: "pipe_tuned".into(),
            frames: 0,
            samples: 0,
            sample_rate_hz: 0,
            duration_ms: 0,
            jitter_p50_us: 0,
            jitter_p95_us: 0,
            jitter_p99_us: 0,
            jitter_max_us: 0,
            stalls_over_100ms: 0,
            wav_path: None,
        };
        let r = recommend(&[live, dead]);
        assert_eq!(r.winner, "pipe");
        assert_eq!(r.runner_up, None);
    }

    #[test]
    fn rejects_duplicate_labels() {
        let dir = tempfile::tempdir().unwrap();
        let a = dir.path().join("a.wav");
        let b = dir.path().join("b.wav");
        noise_wav(&a, 8000, 0);
        noise_wav(&b, 8000, 0);
        let err = run_capture_bakeoff(CaptureArgs {
            legs: vec![
                Box::new(ReplaySource::from_wav(&a, 0.0, SourceId(0)).unwrap()),
                Box::new(ReplaySource::from_wav(&b, 0.0, SourceId(1)).unwrap()),
            ],
            labels: vec!["pipe".into(), "pipe".into()],
            max_secs: 10,
            record_dir: None,
        })
        .unwrap_err();
        assert!(err.to_string().contains("collide"), "unexpected error: {err}");
    }

    #[test]
    fn records_wav_and_sidecar() {
        let dir = tempfile::tempdir().unwrap();
        let a = dir.path().join("a.wav");
        let b = dir.path().join("b.wav");
        noise_wav(&a, 8000, 0);
        noise_wav(&b, 8000, 0);
        let out = dir.path().join("rec");
        let report = run_capture_bakeoff(CaptureArgs {
            legs: vec![
                Box::new(ReplaySource::from_wav(&a, 0.0, SourceId(0)).unwrap()),
                Box::new(ReplaySource::from_wav(&b, 0.0, SourceId(1)).unwrap()),
            ],
            labels: vec!["twilio".into(), "telnyx".into()],
            max_secs: 10,
            record_dir: Some(out.clone()),
        })
        .unwrap();
        assert!(report.legs[0].wav_path.is_some());
        assert!(out.join("twilio.wav").exists());
        assert!(out.join("twilio.jsonl").exists());
        // render_table must not panic and must name the winner.
        assert!(report.render_table().contains("recommend:"));
    }
}
