//! The first single test of the WHOLE chain: fixture audio → paced
//! ReplaySource → preproc → chunker → stub detector → decision →
//! intent channel → executor → signed POST (wiremock) → merged report.
//! Everything before this exercised the two halves only in isolation.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use earshot::config::{KeywordConfig, Policy};
use earshot::decision::DecisionTable;
use earshot::detector::stub::EnergyStubDetector;
use earshot::fixture::{Burst, FixtureSpec};
use earshot::kalshi::auth::{Presigner, Signer};
use earshot::kalshi::book::{Book, BookCache};
use earshot::kalshi::executor::{prestage_orders, run_executor, ExecCtx};
use earshot::kalshi::rest::RestClient;
use earshot::kalshi::transport::{OrderTransport, StageWire};
use earshot::pipeline::preproc::PreprocCfg;
use earshot::pipeline::{decision_journal_path, prepare_decision_state, run_pipeline};
use earshot::risk::{RiskConfig, RiskGate};
use earshot::source::replay::ReplaySource;
use earshot::types::SourceId;
use rsa::pkcs1::EncodeRsaPrivateKey;
use wiremock::matchers::{body_partial_json, header_exists, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const TICKER: &str = "KX-E2E-FULL";

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn audio_in_order_out_with_all_edges_recorded() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/portfolio/events/orders"))
        .and(header_exists("KALSHI-ACCESS-KEY"))
        .and(header_exists("KALSHI-ACCESS-SIGNATURE"))
        .and(header_exists("KALSHI-ACCESS-TIMESTAMP"))
        .and(body_partial_json(serde_json::json!({
            "ticker": TICKER,
            "side": "bid",
            "price": "0.90",
            "count": "1.00",
            "time_in_force": "immediate_or_cancel",
        })))
        .respond_with(|request: &wiremock::Request| {
            let request_body: serde_json::Value = request.body_json().unwrap();
            let client_order_id = request_body["client_order_id"]
                .as_str()
                .expect("executor request omitted client_order_id");
            ResponseTemplate::new(201).set_body_json(serde_json::json!({
                "order": {
                    "order_id": "srv-e2e",
                    "client_order_id": client_order_id,
                    "status": "canceled"
                }
            }))
        })
        .expect(1) // the one-shot decision machine must yield exactly one POST
        .mount(&server)
        .await;

    // Execution side: the exact rig `main.rs::arm` builds, minus live WS.
    let key = rsa::RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 2048).unwrap();
    let pem = key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF).unwrap();
    let signer = Arc::new(Signer::from_pem("e2e-key".into(), &pem).unwrap());
    let rest =
        RestClient::with_base_and_timeout(server.uri(), signer.clone(), Duration::from_secs(5))
            .unwrap();
    let cache = BookCache::new(&[TICKER.to_string()]);
    cache.touch();
    cache.publish(
        TICKER,
        Book {
            yes_bids: vec![(40, 100)],
            yes_asks: vec![(85, 500)],
            seq: 1,
            as_of: earshot::clock::MonotonicNs::now(),
        },
    );
    let state = tempfile::tempdir().unwrap();
    let gate = Arc::new(
        RiskGate::new(
            RiskConfig {
                allowlist_tickers: vec![TICKER.into()],
                ..Default::default()
            },
            state.path(),
        )
        .unwrap(),
    );
    gate.arm();
    let keywords = vec![KeywordConfig {
        market_ticker: TICKER.into(),
        phrase: "tone".into(),
        variants: vec!["tone".into()],
        max_price_cents: 90,
        budget_contracts: 1, // 1 × 90¢ worst case stays inside the $1 ledger
        policy: Policy::FastOnly,
        rule_risk_haircut_cents: 0,
        fast_prefixes: vec![],
        fast_threshold: None,
        confirmed_threshold: None,
        boost: None,
        min_confidence_fast: 0.0,
        min_confidence_confirmed: 0.0,
    }];
    let presigner = Presigner::new(
        signer,
        "POST",
        "/trade-api/v2/portfolio/events/orders".into(),
    );
    let ctx = Arc::new(ExecCtx {
        transport: OrderTransport::Rest {
            client: rest.clone(),
            presigner,
        },
        rest: rest.clone(),
        cache,
        gate: gate.clone(),
        prestaged: prestage_orders("E2E", &keywords, StageWire::Rest),
        haircuts: HashMap::from([(0u16, 0u32)]),
        dry_run: false,
        registry: earshot::kalshi::OrderRegistry::default(),
        order_evidence: None,
        decision_plan_sha1: "0".repeat(40),
        keyword_text: HashMap::new(),
        rest_endpoint_id: rest.endpoint_id(),
    });

    // Audio side: 2 s fixture, one burst at 1 s, real-time pacing — the
    // same threading main.rs::arm uses (pipeline on a blocking thread).
    let dir = tempfile::tempdir().unwrap();
    let wav = dir.path().join("f.wav");
    FixtureSpec {
        sample_rate_hz: 8000,
        total_ms: 2000,
        tone_hz: 440.0,
        amplitude: 0.6,
        bursts: vec![Burst {
            onset_ms: 1000,
            dur_ms: 500,
        }],
    }
    .write_wav(&wav)
    .unwrap();
    let source = Box::new(ReplaySource::from_wav(&wav, 1.0, SourceId(0)).unwrap());
    let detector = Box::new(EnergyStubDetector::for_standard_fixture());
    let table = DecisionTable::from_keywords(&keywords);

    let (intent_tx, intent_rx) = tokio::sync::mpsc::channel(64);
    let exec_task = tokio::spawn(run_executor(intent_rx, ctx.clone()));
    let mut report = tokio::task::spawn_blocking(move || {
        run_pipeline(
            "E2E",
            160,
            PreprocCfg::default(),
            source,
            detector,
            table,
            Some(intent_tx),
            Default::default(),
        )
    })
    .await
    .unwrap()
    .unwrap();
    let exec_report = exec_task.await.unwrap();

    // Order landed, once, with the full timestamp chain.
    assert_eq!(exec_report.orders.len(), 1);
    let o = &exec_report.orders[0];
    assert_eq!(o.order_id.as_deref(), Some("srv-e2e"));
    assert!(!o.dry_run && !o.lost_race);
    // `reached_wire()` is the one definition of "this got to Kalshi"
    // (src/kalshi/executor.rs) — asserting send_ts by hand is how four
    // copies of the predicate drifted apart before 2026-08-05.
    assert!(o.reached_wire() && o.ack_ts.unwrap() > o.send_ts.unwrap());
    assert_eq!(gate.ledger_spent_cents(), 90);
    assert_eq!(report.drops.total(), 0, "clean paced replay must not drop");

    // The arm-time fold: executor edges merged into the pipeline report —
    // ALL 11 edges must be populated in one report, capture→ack included.
    report.edges.extend(exec_report.recorder.snapshot());
    for edge in earshot::telemetry::EDGES {
        let stats = report
            .edges
            .get(edge)
            .unwrap_or_else(|| panic!("edge {edge} missing"));
        assert!(stats.count > 0, "edge {edge} recorded nothing");
    }
    // Sanity on the headline number: audio capture → order ack against a
    // local mock must be far under a second (chunk accumulation dominates).
    let full = &report.edges["capture→ack"];
    assert!(
        full.p95_us < 500_000,
        "capture→ack p95 {} µs — chain is stalling somewhere",
        full.p95_us
    );
}

/// The operational restart loop used to construct a fresh in-memory table,
/// allowing the same market to fire again. Exercise the actual pipeline twice
/// against one event journal: attempt one emits, attempt two still detects the
/// audio but cannot emit a second intent.
#[test]
fn pipeline_reconstruction_preserves_event_one_shot_state() {
    let dir = tempfile::tempdir().unwrap();
    let wav = dir.path().join("restart.wav");
    let journal = dir.path().join("decision.jsonl");
    FixtureSpec {
        sample_rate_hz: 8000,
        total_ms: 1200,
        tone_hz: 440.0,
        amplitude: 0.6,
        bursts: vec![Burst {
            onset_ms: 400,
            dur_ms: 500,
        }],
    }
    .write_wav(&wav)
    .unwrap();
    let keywords = vec![KeywordConfig {
        market_ticker: TICKER.into(),
        phrase: "tone".into(),
        variants: vec!["tone".into()],
        max_price_cents: 90,
        budget_contracts: 1,
        policy: Policy::FastOnly,
        rule_risk_haircut_cents: 0,
        fast_prefixes: vec![],
        fast_threshold: None,
        confirmed_threshold: None,
        boost: None,
        min_confidence_fast: 0.0,
        min_confidence_confirmed: 0.0,
    }];

    for (attempt, expected_intents) in [(1, 1), (2, 0)] {
        let source = Box::new(ReplaySource::from_wav(&wav, 0.0, SourceId(0)).unwrap());
        let detector = Box::new(EnergyStubDetector::for_standard_fixture());
        let table = DecisionTable::from_keywords_with_journal(&keywords, &journal).unwrap();
        let (intent_tx, mut intent_rx) = tokio::sync::mpsc::channel(64);
        let report = run_pipeline(
            "E2E-RESTART",
            160,
            PreprocCfg::default(),
            source,
            detector,
            table,
            Some(intent_tx),
            Default::default(),
        )
        .unwrap();
        assert!(
            !report.detections.is_empty(),
            "attempt {attempt} heard no tone"
        );

        let mut intents = 0;
        while intent_rx.try_recv().is_ok() {
            intents += 1;
        }
        assert_eq!(
            intents, expected_intents,
            "attempt {attempt} violated persistent one-shot state"
        );
    }
}

#[test]
fn assembly_boundary_shares_live_state_across_source_lane_ids() {
    let root = tempfile::tempdir().unwrap();
    let state_root = root.path().join("state");
    let phone_state = state_root.join("KXEARNINGSMENTIONTEST-26AUG11-PHONE");
    let webcast_state = state_root.join("KXEARNINGSMENTIONTEST-26AUG11-WEBCAST");
    std::fs::create_dir_all(&phone_state).unwrap();
    std::fs::create_dir_all(&webcast_state).unwrap();
    let keywords = vec![KeywordConfig {
        market_ticker: "KXEARNINGSMENTIONTEST-26AUG11-TONE".into(),
        phrase: "tone".into(),
        variants: vec!["tone".into()],
        max_price_cents: 90,
        budget_contracts: 1,
        policy: Policy::FastOnly,
        rule_risk_haircut_cents: 0,
        fast_prefixes: vec![],
        fast_threshold: None,
        confirmed_threshold: None,
        boost: None,
        min_confidence_fast: 0.0,
        min_confidence_confirmed: 0.0,
    }];

    let phone_path = decision_journal_path(&phone_state, "prod", &keywords).unwrap();
    let webcast_path = decision_journal_path(&webcast_state, "prod", &keywords).unwrap();
    assert_eq!(
        phone_path, webcast_path,
        "source-lane suffix split one-shot state"
    );

    let dry = prepare_decision_state(&keywords, "prod", &phone_state, false).unwrap();
    assert!(dry.journal_path.is_none() && dry.lease.is_none());
    assert!(!phone_path.exists(), "dry arm created live decision state");

    let first = prepare_decision_state(&keywords, "prod", &phone_state, true).unwrap();
    assert_eq!(first.journal_path.as_deref(), Some(phone_path.as_path()));
    assert!(
        prepare_decision_state(&keywords, "prod", &webcast_state, true).is_err(),
        "webcast lane acquired a second live table for the phone lane's markets"
    );

    // Pipeline teardown drops the table first; the composer-held lease must
    // keep exclusivity until executor/report teardown completes.
    drop(first.table);
    assert!(
        prepare_decision_state(&keywords, "prod", &webcast_state, true).is_err(),
        "journal unlocked before the arm-lifetime lease was dropped"
    );
    drop(first.lease);
    assert!(prepare_decision_state(&keywords, "prod", &webcast_state, true).is_ok());
}
