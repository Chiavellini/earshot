//! Bench-harness self-test: tone fixture + stub detector + fixture-derived
//! ground truth ⇒ perfect scores. No model, no corpus — always runs, so the
//! scorer/matcher math stays pinned even on machines without models/.

use earshot::bench::score::{score_phrase, MatchWindow};
use earshot::config::{KeywordConfig, Policy};
use earshot::decision::DecisionTable;
use earshot::detector::stub::EnergyStubDetector;
use earshot::fixture::FixtureSpec;
use earshot::pipeline::preproc::PreprocCfg;
use earshot::pipeline::run_pipeline;
use earshot::source::replay::ReplaySource;
use earshot::types::{SourceId, Tier};

fn keywords() -> Vec<KeywordConfig> {
    vec![KeywordConfig {
        market_ticker: "KX-TEST".into(),
        phrase: "tone".into(),
        variants: vec!["tone".into()],
        max_price_cents: 97,
        budget_contracts: 200,
        policy: Policy::TwoTier,
        rule_risk_haircut_cents: 0,
        fast_prefixes: vec![],
        fast_threshold: None,
        confirmed_threshold: None,
        boost: None,
        min_confidence_fast: 0.0,
        min_confidence_confirmed: 0.0,
    }]
}

#[test]
fn fixture_selftest_scores_perfect_recall_zero_fa() {
    let spec = FixtureSpec::standard();
    let dir = tempfile::tempdir().unwrap();
    let wav = dir.path().join("fixture.wav");
    spec.write_wav(&wav).unwrap();

    // Lossless replay (speed=0) — exactly the bench accuracy pass.
    let source = Box::new(ReplaySource::from_wav(&wav, 0.0, SourceId(0)).unwrap());
    let detector = Box::new(EnergyStubDetector::for_standard_fixture());
    let table = DecisionTable::from_keywords(&keywords());
    let report =
        run_pipeline("selftest", 160, PreprocCfg::default(), source, detector, table, None).unwrap();

    assert_eq!(report.drops.total(), 0);
    assert!(report.first_capture_ts.is_some());

    let occurrences = spec.occurrences("tone");
    assert_eq!(occurrences.len(), 3);
    let dets: Vec<_> = report.detections.iter().collect();
    let occs: Vec<_> = occurrences.iter().collect();
    let scores = score_phrase("tone", &dets, &occs, MatchWindow::default(), None);

    let fast = scores.iter().find(|s| s.tier == Tier::Fast).unwrap();
    assert_eq!(
        (fast.true_positives, fast.misses, fast.false_alarms),
        (3, 0, 0),
        "stub vs fixture must be perfect: {scores:?}"
    );
    assert_eq!(fast.recall(), 1.0);
    assert_eq!(fast.precision(), 1.0);

    // The stub emits no Confirmed tier — that row is all-miss by design.
    let conf = scores.iter().find(|s| s.tier == Tier::Confirmed).unwrap();
    assert_eq!((conf.true_positives, conf.false_alarms), (0, 0));
}

/// Paced run: latency numbers are real wall-clock onset→emit and must sit
/// within one chunk of the burst onset (plus scheduling noise).
#[test]
fn fixture_selftest_paced_latency_is_chunk_dominated() {
    let spec = FixtureSpec::standard();
    let dir = tempfile::tempdir().unwrap();
    let wav = dir.path().join("fixture.wav");
    spec.write_wav(&wav).unwrap();

    let source = Box::new(ReplaySource::from_wav(&wav, 1.0, SourceId(0)).unwrap());
    let detector = Box::new(EnergyStubDetector::for_standard_fixture());
    let table = DecisionTable::from_keywords(&keywords());
    let report =
        run_pipeline("selftest-paced", 160, PreprocCfg::default(), source, detector, table, None)
            .unwrap();

    let occurrences = spec.occurrences("tone");
    let dets: Vec<_> = report.detections.iter().collect();
    let occs: Vec<_> = occurrences.iter().collect();
    let scores =
        score_phrase("tone", &dets, &occs, MatchWindow::default(), report.first_capture_ts);
    let fast = scores.iter().find(|s| s.tier == Tier::Fast).unwrap();
    assert_eq!(fast.latency_ms.len(), 3);
    for &l in &fast.latency_ms {
        // Burst onset → chunk seal (≤2 chunks: onset may straddle a boundary)
        // → detect. Generous scheduling headroom, but must be sub-second.
        assert!((0..800).contains(&l), "implausible paced latency {l} ms: {scores:?}");
    }
}
