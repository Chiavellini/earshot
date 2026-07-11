//! End-to-end replay regression test (spec P4 / M0 acceptance):
//! generated fixture → ReplaySource → pipeline → detections + latency report.

use std::time::Instant;

use earshot::config::{KeywordConfig, Policy};
use earshot::decision::DecisionTable;
use earshot::detector::stub::EnergyStubDetector;
use earshot::fixture::{Burst, FixtureSpec};
use earshot::pipeline::preproc::PreprocCfg;
use earshot::pipeline::run_pipeline;
use earshot::source::replay::ReplaySource;
use earshot::telemetry::PipelineReport;
use earshot::types::SourceId;

fn keywords() -> Vec<KeywordConfig> {
    vec![KeywordConfig {
        market_ticker: "KX-TEST-AI".into(),
        phrase: "artificial intelligence".into(),
        variants: vec!["artificial intelligence".into()],
        max_price_cents: 97,
        budget_contracts: 200,
        policy: Policy::TwoTier,
        rule_risk_haircut_cents: 2,
        fast_prefixes: vec![],
        fast_threshold: None,
        confirmed_threshold: None,
        boost: None,
        min_confidence_fast: 0.0,
        min_confidence_confirmed: 0.0,
    }]
}

fn run(spec: &FixtureSpec, speed: f32) -> PipelineReport {
    let dir = tempfile::tempdir().unwrap();
    let wav = dir.path().join("fixture.wav");
    spec.write_wav(&wav).unwrap();
    let source = Box::new(ReplaySource::from_wav(&wav, speed, SourceId(0)).unwrap());
    let detector = Box::new(EnergyStubDetector::for_standard_fixture());
    let table = DecisionTable::from_keywords(&keywords());
    run_pipeline("e2e", 160, PreprocCfg::default(), source, detector, table, None).unwrap()
}

fn assert_report_sane(report: &PipelineReport, expected_detections: usize) {
    assert_eq!(report.detections.len(), expected_detections);
    assert_eq!(report.drops.total(), 0, "no stage may drop under normal replay");

    // Every audio-pipeline edge histogram must be populated (the execution
    // edges only exist when the M2 executor is attached).
    for edge in ["capture→preproc", "preproc→chunk", "chunk→detect", "detect→decision", "capture→decision"] {
        let stats = report.edges.get(edge).unwrap_or_else(|| panic!("missing edge {edge}"));
        assert!(stats.count > 0, "edge {edge} recorded nothing");
    }

    // Per detection: the timestamp chain is strictly ordered and the intent
    // chain follows the two-tier state machine (first Fast hit gets 30%).
    for d in &report.detections {
        assert!(d.detection.emit_ts > d.detection.audio_ts);
        if let Some(intent) = &d.intent {
            assert!(intent.decision_ts >= d.detection.emit_ts);
            assert_eq!(intent.tranche_contracts, 60);
            assert_eq!(intent.market_ticker, "KX-TEST-AI");
        }
    }
    // One-shot: exactly one intent regardless of how many bursts fire Fast.
    assert_eq!(report.detections.iter().filter(|d| d.intent.is_some()).count(), 1);

    // Detections must be ordered in time.
    for pair in report.detections.windows(2) {
        assert!(pair[1].detection.audio_ts > pair[0].detection.audio_ts);
    }

    // report.json round-trips.
    let json = serde_json::to_string(report).unwrap();
    let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
    assert_eq!(parsed["detections"].as_array().unwrap().len(), expected_detections);
}

#[test]
fn fast_mode_detects_all_bursts_with_full_chain() {
    let report = run(&FixtureSpec::standard(), 0.0);
    assert_eq!(report.frames_in, 500);
    assert_eq!(report.chunks_in, 63); // 62 full 160 ms chunks + 80 ms remainder
    assert_report_sane(&report, 3);
}

#[test]
fn paced_mode_matches_wall_clock_and_detects() {
    // Short fixture so the paced test stays fast: 2 s, one burst at 1 s.
    let spec = FixtureSpec {
        sample_rate_hz: 8000,
        total_ms: 2000,
        tone_hz: 440.0,
        amplitude: 0.6,
        bursts: vec![Burst { onset_ms: 1000, dur_ms: 500 }],
    };
    let start = Instant::now();
    let report = run(&spec, 1.0);
    let wall = start.elapsed().as_secs_f64();

    assert_report_sane(&report, 1);
    // Absolute-deadline pacing: total wall time within 5% of the audio length
    // (plus a small constant for thread setup/teardown).
    assert!(
        (1.9..=2.4).contains(&wall),
        "2 s fixture at speed 1 took {wall:.2} s"
    );
    // Real-time pacing means detection latency ≈ chunk accumulation, well
    // under one chunk + slack.
    let e2e = report.edges.get("capture→decision").unwrap();
    assert!(
        e2e.p95_us < 170_000,
        "capture→decision p95 {} µs exceeds chunk_ms + 10 ms",
        e2e.p95_us
    );
}
