//! Dual dial-in end-to-end: two ReplaySource legs → SourceArbiter → the real
//! pipeline → detections. Proves the arbiter drops in behind the frozen
//! `AudioSource`/`run_pipeline` contract with no wiring changes downstream.

use std::time::Duration;

use earshot::config::{KeywordConfig, Policy};
use earshot::decision::DecisionTable;
use earshot::detector::stub::EnergyStubDetector;
use earshot::fixture::{Burst, FixtureSpec};
use earshot::pipeline::preproc::PreprocCfg;
use earshot::pipeline::run_pipeline;
use earshot::source::arbiter::{ArbiterMode, ArbiterParams, SourceArbiter};
use earshot::source::replay::ReplaySource;
use earshot::source::AudioSource;
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

/// Two paced legs replaying the *same* fixture through the arbiter. Paced
/// (speed 1.0) so the arbiter forwards at real time and the pipeline keeps up
/// with no drops — the live semantics a fast-mode run wouldn't exercise.
fn run_dual(mode: ArbiterMode) -> earshot::telemetry::PipelineReport {
    // 2 s fixture, one burst at 1 s → exactly one detection.
    let spec = FixtureSpec {
        sample_rate_hz: 8000,
        total_ms: 2000,
        tone_hz: 440.0,
        amplitude: 0.6,
        bursts: vec![Burst { onset_ms: 1000, dur_ms: 500 }],
    };
    let dir = tempfile::tempdir().unwrap();
    let wav = dir.path().join("fixture.wav");
    spec.write_wav(&wav).unwrap();

    let leg_a = Box::new(ReplaySource::from_wav(&wav, 1.0, SourceId(0)).unwrap());
    let leg_b = Box::new(ReplaySource::from_wav(&wav, 1.0, SourceId(1)).unwrap());
    let params = ArbiterParams {
        mode,
        switch: Duration::from_millis(200),
        align: Duration::from_millis(300),
    };
    let source: Box<dyn AudioSource> =
        Box::new(SourceArbiter::new(vec![leg_a, leg_b], params, SourceId(0)).unwrap());

    let detector = Box::new(EnergyStubDetector::for_standard_fixture());
    let table = DecisionTable::from_keywords(&keywords());
    run_pipeline("arbiter-e2e", 160, PreprocCfg::default(), source, detector, table, None, Default::default()).unwrap()
}

#[test]
fn take_first_feeds_pipeline_one_clean_stream() {
    let report = run_dual(ArbiterMode::TakeFirst);
    // Identical legs align at lag 0 and dedup: ~100 frames (2 s / 20 ms), not
    // 200. A little slack for alignment-window boundary rounding.
    assert!(
        (98..=100).contains(&report.frames_in),
        "expected ~100 deduped frames, got {}",
        report.frames_in
    );
    assert_eq!(report.drops.total(), 0, "arbiter must not overflow the pipeline when paced");
    assert_eq!(report.detections.len(), 1, "the single burst fires once");
}

#[test]
fn failover_feeds_pipeline_from_the_primary() {
    let report = run_dual(ArbiterMode::Failover);
    // Failover forwards only the primary leg → ~100 frames, one detection.
    assert!(
        (98..=100).contains(&report.frames_in),
        "expected ~100 primary frames, got {}",
        report.frames_in
    );
    assert_eq!(report.drops.total(), 0);
    assert_eq!(report.detections.len(), 1);
}
