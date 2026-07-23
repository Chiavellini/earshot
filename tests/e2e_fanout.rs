//! Fanout-pipeline e2e (the multi-leg OR'd detection lever, RACE-STRATEGY §5):
//! N legs, each a full source→preproc→chunker→detect chain with its own
//! detector, OR-ed at ONE shared decision table. Proves the three fanout
//! obligations end-to-end:
//!   1. a word heard by only one leg still fires (the 07-22 TARI case),
//!   2. a word heard by both legs fires exactly once (one-shot dedupe),
//!   3. a dead leg never takes the race down with it (leg-local failure).

use std::time::Duration;

use earshot::config::{KeywordConfig, Policy};
use earshot::decision::DecisionTable;
use earshot::detector::stub::EnergyStubDetector;
use earshot::fixture::{Burst, FixtureSpec};
use earshot::pipeline::preproc::PreprocCfg;
use earshot::pipeline::{run_pipeline_fanout, PipelineLeg};
use earshot::source::pipe::PipeSource;
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

fn silence(total_ms: u32) -> FixtureSpec {
    FixtureSpec {
        sample_rate_hz: 8000,
        total_ms,
        tone_hz: 440.0,
        amplitude: 0.6,
        bursts: vec![],
    }
}

fn replay_leg(dir: &std::path::Path, name: &str, spec: &FixtureSpec, speed: f32, id: u16) -> PipelineLeg {
    let wav = dir.join(format!("{name}.wav"));
    spec.write_wav(&wav).unwrap();
    PipelineLeg {
        label: name.to_string(),
        source: Box::new(ReplaySource::from_wav(&wav, speed, SourceId(id)).unwrap()),
        detector: Box::new(EnergyStubDetector::for_standard_fixture()),
    }
}

fn run_fanout(legs: Vec<PipelineLeg>) -> PipelineReport {
    let table = DecisionTable::from_keywords(&keywords());
    run_pipeline_fanout("e2e-fanout", 160, PreprocCfg::default(), legs, table, None, Default::default())
        .unwrap()
}

#[test]
fn word_heard_by_one_leg_only_still_fires() {
    let dir = tempfile::tempdir().unwrap();
    let legs = vec![
        replay_leg(dir.path(), "hears", &FixtureSpec::standard(), 0.0, 0),
        replay_leg(dir.path(), "deaf", &silence(10_000), 0.0, 1),
    ];
    let report = run_fanout(legs);

    // Leg "hears" detects all three bursts; exactly one intent (one-shot).
    assert_eq!(report.detections.len(), 3);
    assert!(report.detections.iter().all(|d| d.leg == "hears"));
    assert_eq!(report.detections.iter().filter(|d| d.intent.is_some()).count(), 1);
    let intent = report.detections.iter().find_map(|d| d.intent.as_ref()).unwrap();
    assert_eq!(intent.source, SourceId(0));

    // Per-leg accounting: both legs ran to completion, silence leg heard 0.
    let per_leg = report.per_leg.as_ref().unwrap();
    assert_eq!(per_leg.len(), 2);
    let hears = per_leg.iter().find(|l| l.label == "hears").unwrap();
    let deaf = per_leg.iter().find(|l| l.label == "deaf").unwrap();
    assert_eq!(hears.detections, 3);
    assert_eq!(deaf.detections, 0);
    assert!(hears.died.is_none() && deaf.died.is_none());
    assert!(deaf.frames_in > 0, "silence leg must still stream frames");
    assert_eq!(report.frames_in, hears.frames_in + deaf.frames_in);
    assert_eq!(report.drops.total(), 0);

    // JSON shape: per_leg present, detections carry their leg label.
    let json: serde_json::Value =
        serde_json::from_str(&serde_json::to_string(&report).unwrap()).unwrap();
    assert_eq!(json["per_leg"].as_array().unwrap().len(), 2);
    assert_eq!(json["detections"][0]["leg"], "hears");
}

#[test]
fn word_heard_by_both_legs_fires_once_earlier_leg_wins() {
    // Paced replay so arrival order is deterministic: leg "early" hears the
    // burst at 500 ms, leg "late" at 1500 ms — the one-shot machine must
    // hand the single intent to "early" and ignore "late"'s detection.
    let dir = tempfile::tempdir().unwrap();
    let mut early = silence(2000);
    early.bursts = vec![Burst { onset_ms: 500, dur_ms: 500 }];
    let mut late = silence(2000);
    late.bursts = vec![Burst { onset_ms: 1500, dur_ms: 500 }];
    let legs = vec![
        replay_leg(dir.path(), "early", &early, 1.0, 0),
        replay_leg(dir.path(), "late", &late, 1.0, 1),
    ];
    let report = run_fanout(legs);

    assert_eq!(report.detections.len(), 2, "both legs must detect their burst");
    let intents: Vec<_> = report.detections.iter().filter(|d| d.intent.is_some()).collect();
    assert_eq!(intents.len(), 1, "one-shot: exactly one intent across legs");
    assert_eq!(intents[0].leg, "early");
    assert_eq!(intents[0].intent.as_ref().unwrap().source, SourceId(0));
    // The late leg's hit is recorded but suppressed — the OR'd-recall
    // backstop is visible in the report, not silently dropped.
    assert!(report
        .detections
        .iter()
        .any(|d| d.leg == "late" && d.intent.is_none()));
    // Chronological interleave across legs.
    for pair in report.detections.windows(2) {
        assert!(pair[1].detection.emit_ts >= pair[0].detection.emit_ts);
    }
}

#[test]
fn dead_leg_is_leg_local_and_the_race_continues() {
    // Leg "dead" produces no bytes and trips the pipe idle guard (the exact
    // failure of ops post-mortem #2, 07-22) — the healthy leg must still
    // detect and fire, and the report must carry the death reason.
    let dir = tempfile::tempdir().unwrap();
    let dead = PipeSource::new(vec!["sleep".into(), "30".into()], 8000, SourceId(1))
        .unwrap()
        .with_idle_timeout(Duration::from_millis(200));
    let legs = vec![
        replay_leg(dir.path(), "alive", &FixtureSpec::standard(), 0.0, 0),
        PipelineLeg {
            label: "dead".into(),
            source: Box::new(dead),
            detector: Box::new(EnergyStubDetector::for_standard_fixture()),
        },
    ];
    let report = run_fanout(legs);

    assert_eq!(report.detections.iter().filter(|d| d.intent.is_some()).count(), 1);
    let per_leg = report.per_leg.as_ref().unwrap();
    let dead = per_leg.iter().find(|l| l.label == "dead").unwrap();
    let alive = per_leg.iter().find(|l| l.label == "alive").unwrap();
    assert!(dead.died.as_deref().unwrap_or_default().contains("idle"), "died: {:?}", dead.died);
    assert!(alive.died.is_none());
    assert_eq!(alive.detections, 3);
}

/// Two live sherpa instances (one per leg) coexist in one process and the
/// shared one-shot still dedupes across them. Model-gated like
/// tests/sherpa_kws.rs: SKIPs without models/, hard-fails under
/// EARSHOT_REQUIRE_MODELS=1.
#[test]
fn two_sherpa_legs_share_one_decision_table() {
    const MODEL_DIR: &str = "models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01";
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join(MODEL_DIR);
    if !dir.exists() {
        if std::env::var_os("EARSHOT_REQUIRE_MODELS").is_some() {
            panic!("EARSHOT_REQUIRE_MODELS set but {MODEL_DIR} missing — run scripts/fetch-models.sh");
        }
        eprintln!("==== SKIP fanout sherpa: {MODEL_DIR} missing — run scripts/fetch-models.sh ====");
        return;
    }
    let sherpa_cfg: earshot::config::SherpaKwsConfig =
        toml::from_str(&format!("model_dir = {:?}", dir.display().to_string())).unwrap();
    let kw: KeywordConfig = toml::from_str(
        r#"
market_ticker    = "KX-TEST"
phrase           = "light up"
variants         = ["light up"]
max_price_cents  = 97
budget_contracts = 200
policy           = "confirmed_only"
"#,
    )
    .unwrap();
    let keywords = vec![kw];
    let mk_detector = || {
        Box::new(
            earshot::detector::sherpa_kws::SherpaKwsDetector::new(&sherpa_cfg, &keywords).unwrap(),
        )
    };
    // Both legs replay the model's own test wav ("…light up here and there…").
    let wav = dir.join("test_wavs/0.wav");
    let legs = vec![
        PipelineLeg {
            label: "leg-a".into(),
            source: Box::new(ReplaySource::from_wav(&wav, 0.0, SourceId(0)).unwrap()),
            detector: mk_detector(),
        },
        PipelineLeg {
            label: "leg-b".into(),
            source: Box::new(ReplaySource::from_wav(&wav, 0.0, SourceId(1)).unwrap()),
            detector: mk_detector(),
        },
    ];
    let table = DecisionTable::from_keywords(&keywords);
    let report =
        run_pipeline_fanout("e2e-fanout-sherpa", 160, PreprocCfg::default(), legs, table, None, Default::default())
            .unwrap();

    let per_leg = report.per_leg.as_ref().unwrap();
    for l in per_leg {
        assert!(l.detections >= 1, "leg {} heard nothing — sherpa instance dead?", l.label);
        assert!(l.died.is_none());
    }
    // Both legs heard the same phrase; the shared one-shot fires exactly once.
    assert_eq!(
        report.detections.iter().filter(|d| d.intent.is_some()).count(),
        1,
        "detections: {:?}",
        report.detections
    );
}

/// All legs dying is a pipeline error, not an empty success.
#[test]
fn all_legs_dead_is_an_error() {
    let mk = |id: u16| {
        Box::new(
            PipeSource::new(vec!["sleep".into(), "30".into()], 8000, SourceId(id))
                .unwrap()
                .with_idle_timeout(Duration::from_millis(150)),
        )
    };
    let legs = vec![
        PipelineLeg { label: "a".into(), source: mk(0), detector: Box::new(EnergyStubDetector::for_standard_fixture()) },
        PipelineLeg { label: "b".into(), source: mk(1), detector: Box::new(EnergyStubDetector::for_standard_fixture()) },
    ];
    let table = DecisionTable::from_keywords(&keywords());
    let out = run_pipeline_fanout("e2e-fanout", 160, PreprocCfg::default(), legs, table, None, Default::default());
    assert!(out.is_err(), "every leg dead must surface as an error");
}
