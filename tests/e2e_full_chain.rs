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
use earshot::pipeline::run_pipeline;
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
        .respond_with(ResponseTemplate::new(201).set_body_json(serde_json::json!({
            "order": {"order_id": "srv-e2e", "status": "canceled"}
        })))
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
            RiskConfig { allowlist_tickers: vec![TICKER.into()], ..Default::default() },
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
    let presigner = Presigner::new(signer, "POST", "/trade-api/v2/portfolio/events/orders".into());
    let ctx = Arc::new(ExecCtx {
        transport: OrderTransport::Rest { client: rest.clone(), presigner },
        rest,
        cache,
        gate: gate.clone(),
        prestaged: prestage_orders("E2E", &keywords, StageWire::Rest),
        haircuts: HashMap::from([(0u16, 0u32)]),
        dry_run: false,
        registry: earshot::kalshi::OrderRegistry::default(),
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
        bursts: vec![Burst { onset_ms: 1000, dur_ms: 500 }],
    }
    .write_wav(&wav)
    .unwrap();
    let source = Box::new(ReplaySource::from_wav(&wav, 1.0, SourceId(0)).unwrap());
    let detector = Box::new(EnergyStubDetector::for_standard_fixture());
    let table = DecisionTable::from_keywords(&keywords);

    let (intent_tx, intent_rx) = tokio::sync::mpsc::channel(64);
    let exec_task = tokio::spawn(run_executor(intent_rx, ctx.clone()));
    let mut report = tokio::task::spawn_blocking(move || {
        run_pipeline("E2E", 160, PreprocCfg::default(), source, detector, table, Some(intent_tx), Default::default())
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
    assert!(o.send_ts.is_some() && o.ack_ts.unwrap() > o.send_ts.unwrap());
    assert_eq!(gate.ledger_spent_cents(), 90);
    assert_eq!(report.drops.total(), 0, "clean paced replay must not drop");

    // The arm-time fold: executor edges merged into the pipeline report —
    // ALL 11 edges must be populated in one report, capture→ack included.
    report.edges.extend(exec_report.recorder.snapshot());
    for edge in earshot::telemetry::EDGES {
        let stats = report.edges.get(edge).unwrap_or_else(|| panic!("edge {edge} missing"));
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
