//! Executor integration tests against a wiremock REST server: fire path,
//! LOST_RACE, and the ambiguous-timeout → status-query path (no blind retry).

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use earshot::clock::MonotonicNs;
use earshot::config::{KeywordConfig, Policy};
use earshot::kalshi::auth::{Presigner, Signer};
use earshot::kalshi::book::{Book, BookCache};
use earshot::kalshi::executor::{prestage_orders, run_executor, ExecCtx};
use earshot::kalshi::order_evidence::{
    OrderEvidenceCommand, OrderEvidenceIdentity, OrderEvidenceJournal, OrderTransitionKind,
};
use earshot::kalshi::rest::RestClient;
use earshot::kalshi::transport::{OrderTransport, StageWire, WireStaged};
use earshot::risk::{RiskConfig, RiskGate};
use earshot::types::{DecisionIntent, KeywordId, Tier};
use rsa::pkcs1::EncodeRsaPrivateKey;
use wiremock::matchers::{body_partial_json, header_exists, method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

const TICKER: &str = "KX-TEST-MOCK";

fn test_signer() -> Arc<Signer> {
    let key = rsa::RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 2048).unwrap();
    let pem = key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF).unwrap();
    Arc::new(Signer::from_pem("mock-key".into(), &pem).unwrap())
}

fn keyword(cap: u8, budget: u32) -> KeywordConfig {
    KeywordConfig {
        market_ticker: TICKER.into(),
        phrase: "test".into(),
        variants: vec!["test".into()],
        max_price_cents: cap,
        budget_contracts: budget,
        policy: Policy::TwoTier,
        rule_risk_haircut_cents: 0,
        fast_prefixes: vec![],
        fast_threshold: None,
        confirmed_threshold: None,
        boost: None,
        min_confidence_fast: 0.0,
        min_confidence_confirmed: 0.0,
    }
}

fn intent(tranche: u32, tier: Tier) -> DecisionIntent {
    intent_for(0, 90, tranche, tier)
}

fn intent_for(kw: u16, price: u8, tranche: u32, tier: Tier) -> DecisionIntent {
    let now = MonotonicNs::now();
    DecisionIntent {
        keyword: KeywordId(kw),
        source: earshot::types::SourceId(0),
        market_ticker: TICKER.into(),
        max_price_cents: price,
        tranche_contracts: tranche,
        tier,
        audio_ts: now,
        decision_ts: now,
        handoff_ts: now,
    }
}

struct Rig {
    ctx: Arc<ExecCtx>,
    _state: tempfile::TempDir,
}

fn evidence_identity() -> OrderEvidenceIdentity {
    OrderEvidenceIdentity {
        run_id: "run-exec-mock-001".into(),
        event_id: "event-exec-mock".into(),
        kalshi_env: "mock".into(),
        account_fingerprint: "sha256:exec-mock-account".into(),
        plan_sha256: "a".repeat(64),
    }
}

fn attach_order_evidence(rig: &mut Rig) -> (OrderEvidenceJournal, std::path::PathBuf) {
    let path = rig._state.path().join("orders.jsonl");
    let journal = OrderEvidenceJournal::open(&path, evidence_identity()).unwrap();
    Arc::get_mut(&mut rig.ctx)
        .expect("test rig must have sole ExecCtx ownership before execution")
        .order_evidence = Some(journal.sink());
    (journal, path)
}

async fn rig(server: &MockServer, cap: u8, budget: u32, dry_run: bool, timeout: Duration) -> Rig {
    rig_kws(server, vec![keyword(cap, budget)], dry_run, timeout).await
}

async fn rig_kws(
    server: &MockServer,
    kws: Vec<KeywordConfig>,
    dry_run: bool,
    timeout: Duration,
) -> Rig {
    let signer = test_signer();
    let rest = RestClient::with_base_and_timeout(server.uri(), signer.clone(), timeout).unwrap();
    let cache = BookCache::new(&[TICKER.to_string()]);
    cache.touch();
    cache.publish(
        TICKER,
        Book {
            yes_bids: vec![(40, 100)],
            yes_asks: vec![(85, 500), (95, 100)],
            seq: 1,
            as_of: MonotonicNs::now(),
        },
    );
    let state = tempfile::tempdir().unwrap();
    let gate = RiskGate::new(
        RiskConfig {
            allowlist_tickers: vec![TICKER.into()],
            ..Default::default()
        },
        state.path(),
    )
    .unwrap();
    gate.arm();
    let presigner = Presigner::new(
        signer,
        "POST",
        "/trade-api/v2/portfolio/events/orders".into(),
    );
    let haircuts: HashMap<u16, u32> = (0..kws.len() as u16).map(|i| (i, 0u32)).collect();
    Rig {
        ctx: Arc::new(ExecCtx {
            transport: OrderTransport::Rest {
                client: rest.clone(),
                presigner,
            },
            rest: rest.clone(),
            cache,
            gate: Arc::new(gate),
            prestaged: prestage_orders("EV", &kws, StageWire::Rest),
            haircuts,
            dry_run,
            registry: earshot::kalshi::OrderRegistry::default(),
            order_evidence: None,
            decision_plan_sha1: "0".repeat(40),
            keyword_text: HashMap::new(),
            rest_endpoint_id: rest.endpoint_id(),
            verifier: None,
        }),
        _state: state,
    }
}

#[tokio::test]
async fn fires_exactly_one_signed_post_per_intent() {
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
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": earshot::kalshi::executor::deterministic_order_client_id(
                "EV", TICKER, 90, 1, 0, Tier::Confirmed,
            ),
        })))
        .respond_with(ResponseTemplate::new(201).set_body_json(serde_json::json!({
            "order": {
                "order_id": "srv-1",
                "client_order_id": earshot::kalshi::executor::deterministic_order_client_id(
                    "EV", TICKER, 90, 1, 0, Tier::Confirmed,
                ),
                "status": "canceled",
                "fill_count": "1.00",
                "remaining_count": "0.00",
                "average_fill_price": "0.8500"
            }
        })))
        .expect(1)
        .mount(&server)
        .await;

    // budget 1 → confirmed-full tranche of 1 contract: 90¢ worst case stays
    // inside the $1 hard probe ledger.
    let rig = rig(&server, 90, 1, false, Duration::from_secs(5)).await;
    let (tx, rx) = tokio::sync::mpsc::channel(8);
    tx.send(intent(1, Tier::Confirmed)).await.unwrap();
    drop(tx);
    let report = run_executor(rx, rig.ctx.clone()).await;

    assert_eq!(report.orders.len(), 1);
    let o = &report.orders[0];
    assert_eq!(o.order_id.as_deref(), Some("srv-1"));
    assert_eq!(o.order_status.as_deref(), Some("canceled"));
    assert_eq!(
        o.wire_state,
        earshot::kalshi::executor::OrderWireState::Observed
    );
    assert_eq!(o.transport_protocol.as_deref(), Some("rest"));
    assert_eq!(o.protocol_status.as_deref(), Some("http:201"));
    assert_eq!(o.exchange_accepted, Some(true));
    assert_eq!(o.order_terminal, Some(true));
    assert_eq!(
        o.fill
            .as_ref()
            .and_then(|f| f.cumulative_contracts.as_deref()),
        Some("1.00")
    );
    assert_eq!(
        o.fill.as_ref().and_then(|f| f.average_price.as_deref()),
        Some("0.8500")
    );
    assert!(
        o.raw_response_body_base64.is_some(),
        "exact HTTP bytes must be banked"
    );
    assert!(!o.lost_race);
    assert!(o.reached_wire() && o.ack_ts.is_some());
    assert!(o.ack_ts.unwrap() > o.send_ts.unwrap());
    let edges = report.recorder.snapshot();
    for edge in [
        "decision→exec",
        "handoff→exec",
        "book_age",
        "presign_age",
        "exec→send",
        "send→ack",
        "decision→ack",
        "capture→ack",
    ] {
        assert!(
            edges.get(edge).map(|s| s.count > 0).unwrap_or(false),
            "edge {edge} empty"
        );
    }
    assert_eq!(rig.ctx.gate.ledger_spent_cents(), 90); // 1 × 90¢ worst-case debit
}

#[tokio::test]
async fn successful_rest_execution_is_durably_journaled_with_exact_bytes_and_verified_id() {
    let server = MockServer::start().await;
    let expected_client_order_id = earshot::kalshi::executor::deterministic_order_client_id(
        "EV",
        TICKER,
        90,
        1,
        0,
        Tier::Confirmed,
    );
    let exact_response = format!(
        r#"{{"order":{{"order_id":"srv-evidence-1","client_order_id":"{expected_client_order_id}","status":"canceled","fill_count":"1.00","remaining_count":"0.00","average_fill_price":"0.8500"}}}}"#
    );
    Mock::given(method("POST"))
        .and(path("/portfolio/events/orders"))
        .and(body_partial_json(serde_json::json!({
            "client_order_id": expected_client_order_id,
        })))
        .respond_with(
            ResponseTemplate::new(201).set_body_raw(exact_response.as_bytes(), "application/json"),
        )
        .expect(1)
        .mount(&server)
        .await;

    let mut rig = rig(&server, 90, 1, false, Duration::from_secs(5)).await;
    let expected_request = match &rig
        .ctx
        .prestaged
        .values()
        .next()
        .expect("one prestaged order")
        .wire
    {
        WireStaged::Rest { body } => body.to_vec(),
        WireStaged::Fix { .. } => panic!("REST rig unexpectedly staged FIX"),
    };
    let (journal, journal_path) = attach_order_evidence(&mut rig);
    let (tx, rx) = tokio::sync::mpsc::channel(8);
    tx.send(intent(1, Tier::Confirmed)).await.unwrap();
    drop(tx);

    let report = run_executor(rx, rig.ctx.clone()).await;
    assert_eq!(report.orders[0].order_id.as_deref(), Some("srv-evidence-1"));
    assert_eq!(rig.ctx.registry.ids(), vec!["srv-evidence-1"]);
    let requests = server.received_requests().await.unwrap();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].body, expected_request);

    // Close and replay from disk: this asserts the sequence survived the
    // writer's acknowledgement/fsync boundary rather than merely residing in
    // the live snapshot.
    journal.close().unwrap();
    let replay = OrderEvidenceJournal::open(&journal_path, evidence_identity()).unwrap();
    let events = replay.replayed_events();
    assert!(events
        .iter()
        .all(|event| event.client_order_id == expected_client_order_id));
    assert_eq!(
        events.iter().map(|event| event.kind()).collect::<Vec<_>>(),
        vec![
            OrderTransitionKind::IntentEvaluated,
            OrderTransitionKind::SendRequested,
            OrderTransitionKind::InvocationPrepared,
            OrderTransitionKind::WireAttempted,
            OrderTransitionKind::ExchangeObserved,
        ]
    );
    assert_eq!(
        events
            .iter()
            .map(|event| event.journal_seq)
            .collect::<Vec<_>>(),
        vec![0, 1, 2, 3, 4]
    );
    assert_eq!(
        events
            .iter()
            .map(|event| event.order_seq)
            .collect::<Vec<_>>(),
        vec![0, 1, 2, 3, 4]
    );

    match &events[2].transition {
        OrderEvidenceCommand::InvocationPrepared(receipt) => {
            assert_eq!(receipt.raw.decode().unwrap(), expected_request);
        }
        other => panic!("expected invocation_prepared, got {other:?}"),
    }
    match &events[4].transition {
        OrderEvidenceCommand::ExchangeObserved(receipt) => {
            assert_eq!(
                receipt.echoed_client_order_id.as_deref(),
                Some(expected_client_order_id.as_str())
            );
            assert_eq!(receipt.order_id.as_deref(), Some("srv-evidence-1"));
            assert_eq!(receipt.raw.decode().unwrap(), exact_response.as_bytes());
            assert_eq!(
                receipt
                    .parsed
                    .as_ref()
                    .and_then(|value| { value["order"]["client_order_id"].as_str() }),
                Some(expected_client_order_id.as_str())
            );
        }
        other => panic!("expected exchange_observed, got {other:?}"),
    }
    assert!(replay.unresolved_recoveries().is_empty());
    replay.close().unwrap();
}

#[tokio::test]
async fn foreign_echo_is_banked_but_never_promoted_or_registered() {
    let server = MockServer::start().await;
    let expected_client_order_id = earshot::kalshi::executor::deterministic_order_client_id(
        "EV",
        TICKER,
        90,
        1,
        0,
        Tier::Confirmed,
    );
    let suspicious_response = br#"{"order":{"order_id":"FOREIGN-POST-ID","client_order_id":"00000000-0000-0000-0000-0000deadbeef","status":"resting"}}"#;
    let suspicious_query = br#"{"orders":[{"order_id":"FOREIGN-QUERY-ID","client_order_id":"00000000-0000-0000-0000-0000deadbeef","status":"resting"}]}"#;
    Mock::given(method("POST"))
        .and(path("/portfolio/events/orders"))
        .respond_with(
            ResponseTemplate::new(201).set_body_raw(suspicious_response, "application/json"),
        )
        .expect(1)
        .mount(&server)
        .await;
    Mock::given(method("GET"))
        .and(path("/portfolio/orders"))
        .and(query_param(
            "client_order_id",
            expected_client_order_id.as_str(),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_raw(suspicious_query, "application/json"))
        .expect(1)
        .mount(&server)
        .await;

    let mut rig = rig(&server, 90, 1, false, Duration::from_secs(5)).await;
    let (journal, journal_path) = attach_order_evidence(&mut rig);
    let (tx, rx) = tokio::sync::mpsc::channel(8);
    tx.send(intent(1, Tier::Confirmed)).await.unwrap();
    drop(tx);

    let report = run_executor(rx, rig.ctx.clone()).await;
    let order = &report.orders[0];
    assert_eq!(order.order_id, None);
    assert!(order
        .refusal
        .as_deref()
        .is_some_and(|detail| detail.contains("AMBIGUOUS")));
    assert!(rig.ctx.registry.ids().is_empty());
    assert_eq!(
        rig.ctx.registry.pending_client_ids(),
        vec![expected_client_order_id.clone()]
    );

    journal.close().unwrap();
    let replay = OrderEvidenceJournal::open(&journal_path, evidence_identity()).unwrap();
    let events = replay.replayed_events();
    assert_eq!(
        events.iter().map(|event| event.kind()).collect::<Vec<_>>(),
        vec![
            OrderTransitionKind::IntentEvaluated,
            OrderTransitionKind::SendRequested,
            OrderTransitionKind::InvocationPrepared,
            OrderTransitionKind::WireAttempted,
            OrderTransitionKind::ExchangeObserved,
            OrderTransitionKind::ReconcileUnresolved,
        ]
    );
    match &events[4].transition {
        OrderEvidenceCommand::ExchangeObserved(receipt) => {
            assert_eq!(receipt.raw.decode().unwrap(), suspicious_response);
            assert_eq!(receipt.echoed_client_order_id, None);
            assert_eq!(receipt.order_id, None);
            assert!(receipt.detail.as_deref().is_some_and(|detail| {
                detail.contains("did not carry the exact expected client_order_id")
            }));
            assert_eq!(
                receipt
                    .parsed
                    .as_ref()
                    .and_then(|value| { value["order"]["order_id"].as_str() }),
                Some("FOREIGN-POST-ID")
            );
        }
        other => panic!("expected exchange_observed, got {other:?}"),
    }
    match &events[5].transition {
        OrderEvidenceCommand::ReconcileUnresolved(receipt) => {
            assert_eq!(
                receipt.raw.as_ref().unwrap().decode().unwrap(),
                suspicious_query
            );
            assert_eq!(
                receipt
                    .parsed
                    .as_ref()
                    .and_then(|value| { value["orders"][0]["order_id"].as_str() }),
                Some("FOREIGN-QUERY-ID")
            );
            assert!(receipt.detail.contains("no exact echoed client ID"));
        }
        other => panic!("expected reconcile_unresolved, got {other:?}"),
    }
    let state = &replay.snapshot().orders[0];
    assert_eq!(state.order_id, None);
    assert!(!state.terminal);
    assert_eq!(state.last_kind, OrderTransitionKind::ReconcileUnresolved);
    replay.close().unwrap();
}

#[tokio::test]
async fn lost_race_sends_nothing() {
    let server = MockServer::start().await;
    // No mock mounted: any POST would 404 and fail the test via the record.
    let rig = rig(&server, 80, 10, false, Duration::from_secs(5)).await; // cap 80 < best ask 85
    let (tx, rx) = tokio::sync::mpsc::channel(8);
    tx.send(intent(3, Tier::Fast)).await.unwrap();
    drop(tx);
    let report = run_executor(rx, rig.ctx.clone()).await;

    assert_eq!(report.lost_races, 1);
    assert_eq!(report.observe_noncross, 0);
    let o = &report.orders[0];
    assert!(o.lost_race);
    assert!(o.send_ts.is_none());
    assert_eq!(o.best_ask, Some((85, 500)));
    assert!(
        o.book_age_ns.is_some(),
        "book snapshot age must be recorded at decision"
    );
    assert_eq!(rig.ctx.gate.ledger_spent_cents(), 0);
}

#[tokio::test]
async fn missing_prestaged_order_fails_closed_without_inline_post() {
    let server = MockServer::start().await;
    // No POST mock: any inline-built live request would be observable as a
    // 404/attempt. Remove the arm-time artifact and require a local refusal.
    let mut rig = rig(&server, 90, 1, false, Duration::from_secs(5)).await;
    Arc::get_mut(&mut rig.ctx).unwrap().prestaged.clear();
    let (tx, rx) = tokio::sync::mpsc::channel(8);
    tx.send(intent(1, Tier::Confirmed)).await.unwrap();
    drop(tx);

    let report = run_executor(rx, rig.ctx.clone()).await;
    let order = &report.orders[0];
    assert_eq!(
        order.wire_state,
        earshot::kalshi::executor::OrderWireState::NotAttempted
    );
    assert!(order.send_ts.is_none());
    assert!(order
        .refusal
        .as_deref()
        .unwrap_or_default()
        .contains("missing arm-time prestaged order"));
    assert_eq!(rig.ctx.gate.ledger_spent_cents(), 0);
    assert!(rig.ctx.registry.is_empty());
}

/// A 1¢ observe-tier intent crossing nothing is the DESIGNED outcome (the
/// observe posture bids 1¢ non-crossing so it never sends) — it must count as
/// observe_noncross, never inflate lost_races. The MS 2026-07-15 run headline
/// ("4 LOST_RACE") was exactly this misclassification.
#[tokio::test]
async fn observe_noncross_is_not_a_lost_race() {
    let server = MockServer::start().await;
    // No mock mounted: any POST would 404 and fail the test via the record.
    let rig = rig(&server, 1, 1, false, Duration::from_secs(5)).await; // 1¢ observe vs 85¢ ask
    let (tx, rx) = tokio::sync::mpsc::channel(8);
    tx.send(intent_for(0, 1, 1, Tier::Confirmed)).await.unwrap();
    drop(tx);
    let report = run_executor(rx, rig.ctx.clone()).await;

    assert_eq!(report.lost_races, 0);
    assert_eq!(report.observe_noncross, 1);
    let o = &report.orders[0];
    assert!(
        o.lost_race,
        "the per-record flag still marks the non-crossing branch"
    );
    assert!(o.send_ts.is_none());
    assert_eq!(rig.ctx.gate.ledger_spent_cents(), 0);
}

#[tokio::test]
async fn dry_run_computes_but_never_sends() {
    let server = MockServer::start().await;
    let rig = rig(&server, 90, 10, true, Duration::from_secs(5)).await;
    let (tx, rx) = tokio::sync::mpsc::channel(8);
    tx.send(intent(3, Tier::Fast)).await.unwrap();
    tx.send(intent(7, Tier::Confirmed)).await.unwrap();
    drop(tx);
    let report = run_executor(rx, rig.ctx.clone()).await;

    assert_eq!(report.orders.len(), 2);
    for o in &report.orders {
        assert_eq!(o.refusal.as_deref(), Some("dry_run"));
        assert!(o.send_ts.is_none());
    }
    assert_eq!(rig.ctx.gate.ledger_spent_cents(), 0);
}

#[tokio::test]
async fn ambiguous_timeout_queries_status_and_never_reposts() {
    let server = MockServer::start().await;
    // The create hangs past the client timeout — outcome ambiguous.
    Mock::given(method("POST"))
        .and(path("/portfolio/events/orders"))
        .respond_with(
            ResponseTemplate::new(201)
                .set_delay(Duration::from_secs(3))
                .set_body_json(
                    serde_json::json!({"order": {"order_id": "slow", "status": "canceled"}}),
                ),
        )
        .expect(1) // exactly one POST: a blind retry would make this 2 and fail
        .mount(&server)
        .await;
    // Status query finds the order landed after all.
    Mock::given(method("GET"))
        .and(path("/portfolio/orders"))
        .and(query_param(
            "client_order_id",
            earshot::kalshi::executor::deterministic_order_client_id(
                "EV",
                TICKER,
                90,
                1,
                0,
                Tier::Confirmed,
            ),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "orders": [{
                "client_order_id": earshot::kalshi::executor::deterministic_order_client_id(
                    "EV", TICKER, 90, 1, 0, Tier::Confirmed,
                ),
                "order_id": "srv-9",
                "status": "canceled"
            }]
        })))
        .expect(1)
        .mount(&server)
        .await;

    let rig = rig(&server, 90, 1, false, Duration::from_millis(300)).await;
    let (tx, rx) = tokio::sync::mpsc::channel(8);
    tx.send(intent(1, Tier::Confirmed)).await.unwrap();
    drop(tx);
    let report = run_executor(rx, rig.ctx.clone()).await;

    let o = &report.orders[0];
    assert_eq!(o.order_id.as_deref(), Some("srv-9"));
    assert_eq!(o.order_status.as_deref(), Some("canceled"));
    assert!(
        o.send_ts.is_some(),
        "timeout happened after the wire boundary"
    );
    assert_eq!(
        o.wire_state,
        earshot::kalshi::executor::OrderWireState::Reconciled
    );
    assert_eq!(o.transport_protocol.as_deref(), Some("rest"));
    assert!(o.transport_error.is_some());
    assert!(o.reconciliation_ts.is_some());
    assert!(o.reconciliation_response.is_some());
    // Ledger debited despite the ambiguous send — worst case counts.
    assert_eq!(rig.ctx.gate.ledger_spent_cents(), 90);
}

#[tokio::test]
async fn status_query_refuses_a_foreign_order_id() {
    // The kill switch cancels ONLY OrderRegistry ids, because this account
    // carries unrelated resting orders from other systems (CLAUDE.md). If the
    // server ignores the client_order_id filter — which it demonstrably can,
    // e.g. for a non-UUID id — the query returns the WHOLE account, and adopting
    // orders[0] would register a FOREIGN id and silently widen kill scope.
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/portfolio/events/orders"))
        .respond_with(
            ResponseTemplate::new(201)
                .set_delay(Duration::from_secs(3))
                .set_body_json(serde_json::json!({"order": {"order_id": "slow"}})),
        )
        .mount(&server)
        .await;
    // Unfiltered response: someone else's resting order, explicitly tagged with
    // a DIFFERENT client_order_id.
    Mock::given(method("GET"))
        .and(path("/portfolio/orders"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "orders": [{
                "order_id": "SOMEONE-ELSES-ORDER",
                "client_order_id": "00000000-0000-0000-0000-0000deadbeef",
                "status": "resting"
            }]
        })))
        .mount(&server)
        .await;

    let rig = rig(&server, 90, 1, false, Duration::from_millis(300)).await;
    let (tx, rx) = tokio::sync::mpsc::channel(8);
    tx.send(intent(1, Tier::Confirmed)).await.unwrap();
    drop(tx);
    let report = run_executor(rx, rig.ctx.clone()).await;

    let o = &report.orders[0];
    assert_ne!(
        o.order_id.as_deref(),
        Some("SOMEONE-ELSES-ORDER"),
        "adopted a foreign order id — kill-switch scope would be widened"
    );
    assert_eq!(
        o.order_id, None,
        "no order of ours landed, so none must be recorded"
    );
    assert!(
        o.refusal.is_some(),
        "an unmatched status query must record a refusal"
    );
    assert!(
        o.refusal
            .as_deref()
            .unwrap()
            .contains("AMBIGUOUS attempted send"),
        "one empty/untrusted query must not claim the order is absent: {:?}",
        o.refusal
    );
    assert!(
        o.send_ts.is_some(),
        "the original wire attempt must survive reconciliation"
    );
}

#[tokio::test]
async fn status_query_refuses_a_row_without_an_echoed_client_id() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/portfolio/events/orders"))
        .respond_with(ResponseTemplate::new(201).set_delay(Duration::from_secs(3)))
        .mount(&server)
        .await;
    Mock::given(method("GET"))
        .and(path("/portfolio/orders"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "orders": [{"order_id": "UNVERIFIABLE", "status": "resting"}]
        })))
        .mount(&server)
        .await;

    let rig = rig(&server, 90, 1, false, Duration::from_millis(300)).await;
    let (tx, rx) = tokio::sync::mpsc::channel(8);
    tx.send(intent(1, Tier::Confirmed)).await.unwrap();
    drop(tx);
    let report = run_executor(rx, rig.ctx.clone()).await;
    let order = &report.orders[0];
    assert_eq!(order.order_id, None);
    assert!(
        rig.ctx.registry.is_empty(),
        "unverifiable id widened kill scope"
    );
    assert!(order.refusal.as_deref().unwrap().contains("AMBIGUOUS"));
}

#[tokio::test]
async fn burst_intents_overlap_not_serialize() {
    let server = MockServer::start().await;
    // Every POST takes 200 ms. Six intents fired in one burst must complete
    // in ~one delay (spawned concurrently), not six (serialized).
    Mock::given(method("POST"))
        .and(path("/portfolio/events/orders"))
        .respond_with(|request: &wiremock::Request| {
            let request_body: serde_json::Value = request.body_json().unwrap();
            let client_order_id = request_body["client_order_id"]
                .as_str()
                .expect("executor request omitted client_order_id");
            ResponseTemplate::new(201)
                .set_delay(Duration::from_millis(200))
                .set_body_json(serde_json::json!({
                    "order": {
                        "order_id": format!("srv-b-{client_order_id}"),
                        "client_order_id": client_order_id,
                        "status": "canceled"
                    }
                }))
        })
        .expect(6)
        .mount(&server)
        .await;

    // 6 keywords at 15¢ × 1 contract: 90¢ total stays inside the $1 ledger.
    let kws: Vec<KeywordConfig> = (0..6).map(|_| keyword(15, 1)).collect();
    let rig = rig_kws(&server, kws, false, Duration::from_secs(5)).await;
    // Ask must sit under the 15¢ cap for the sweep to be marketable.
    rig.ctx.cache.publish(
        TICKER,
        Book {
            yes_bids: vec![(5, 100)],
            yes_asks: vec![(10, 500)],
            seq: 2,
            as_of: MonotonicNs::now(),
        },
    );

    let (tx, rx) = tokio::sync::mpsc::channel(8);
    for kw in 0..6u16 {
        tx.send(intent_for(kw, 15, 1, Tier::Confirmed))
            .await
            .unwrap();
    }
    drop(tx);
    let t0 = std::time::Instant::now();
    let report = run_executor(rx, rig.ctx.clone()).await;
    let wall = t0.elapsed();

    assert_eq!(report.orders.len(), 6);
    assert!(
        wall < Duration::from_millis(700),
        "6 × 200 ms intents took {wall:?} — executor is serializing the burst"
    );
    assert!(
        wall >= Duration::from_millis(200),
        "mock delay never happened?"
    );
    // Distinct idempotency keys per keyword, and exact ledger math under
    // concurrent record_send calls.
    let mut ids: Vec<&str> = report
        .orders
        .iter()
        .map(|o| o.client_order_id.as_str())
        .collect();
    ids.sort();
    ids.dedup();
    assert_eq!(
        ids.len(),
        6,
        "client_order_ids must be distinct across the burst"
    );
    assert_eq!(rig.ctx.gate.ledger_spent_cents(), 90); // 6 × 15¢, no lost updates
}

#[tokio::test]
async fn burst_with_mixed_outcomes_accounts_exactly() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/portfolio/events/orders"))
        .respond_with(ResponseTemplate::new(201).set_body_json(serde_json::json!({
            "order": {
                "order_id": "srv-m",
                "client_order_id": earshot::kalshi::executor::deterministic_order_client_id(
                    "EV", TICKER, 15, 1, 0, Tier::Confirmed,
                ),
                "status": "canceled"
            }
        })))
        .expect(1) // only the marketable keyword may POST
        .mount(&server)
        .await;

    // kw0 cap 15¢ ≥ ask 10¢ → fires; kw1 cap 5¢ < ask → LOST_RACE.
    let rig = rig_kws(
        &server,
        vec![keyword(15, 1), keyword(5, 1)],
        false,
        Duration::from_secs(5),
    )
    .await;
    rig.ctx.cache.publish(
        TICKER,
        Book {
            yes_bids: vec![(2, 100)],
            yes_asks: vec![(10, 500)],
            seq: 2,
            as_of: MonotonicNs::now(),
        },
    );

    let (tx, rx) = tokio::sync::mpsc::channel(8);
    tx.send(intent_for(0, 15, 1, Tier::Confirmed))
        .await
        .unwrap();
    tx.send(intent_for(1, 5, 1, Tier::Confirmed)).await.unwrap();
    drop(tx);
    let report = run_executor(rx, rig.ctx.clone()).await;

    assert_eq!(report.orders.len(), 2);
    assert_eq!(report.lost_races, 1);
    let fired: Vec<_> = report.orders.iter().filter(|o| !o.lost_race).collect();
    assert_eq!(fired.len(), 1);
    assert_eq!(fired[0].order_id.as_deref(), Some("srv-m"));
    assert_eq!(
        rig.ctx.gate.ledger_spent_cents(),
        15,
        "only the fired order debits"
    );
}

#[tokio::test]
async fn kill_switch_stop_file_cancels_and_disarms() {
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
    let stop = state.path().join("STOP");
    let cancelled = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let c2 = cancelled.clone();
    let watcher = earshot::risk::spawn_kill_watcher(
        gate.clone(),
        stop.clone(),
        move || {
            let c = c2.clone();
            async move {
                c.store(true, std::sync::atomic::Ordering::SeqCst);
            }
        },
        || {},
    );
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(gate.check_order(TICKER, 1, 1, 0, 0).is_ok());
    std::fs::write(&stop, "x").unwrap();
    watcher.await.unwrap();
    assert!(gate.is_killed());
    assert!(cancelled.load(std::sync::atomic::Ordering::SeqCst));
    assert_eq!(
        gate.check_order(TICKER, 1, 1, 0, 0),
        Err(earshot::risk::Refusal::Killed)
    );
}

// ---------------------------------------------------------------------------
// Fill-path verifier (detection-accuracy program D2, 2026-08-18)
// ---------------------------------------------------------------------------

mod verifier_gate {
    use super::*;
    use earshot::config::VerifierSection;
    use earshot::verifier::{
        build_with_transcriber, transcriber::MockTranscriber, VerifierBuild, VerifyRequest,
    };

    fn section(mode: &str, on_timeout: &str, budget_ms: u32) -> VerifierSection {
        toml::from_str(&format!(
            "mode = {mode:?}\nmodel_dir = \"/nonexistent\"\nbudget_ms = {budget_ms}\non_timeout = {on_timeout:?}\ntickers = [\"{TICKER}\"]\n"
        ))
        .unwrap()
    }

    fn verifier(
        section: &VerifierSection,
        kws: &[KeywordConfig],
        text: &'static str,
        delay_ms: u64,
    ) -> VerifierBuild {
        build_with_transcriber(
            section,
            kws,
            Box::new(MockTranscriber(Box::new(move |_| {
                std::thread::sleep(Duration::from_millis(delay_ms));
                Ok(text.to_string())
            }))),
        )
    }

    fn post(v: &VerifierBuild, kw: u16, i: &DecisionIntent) {
        v.client
            .request(VerifyRequest {
                keyword: KeywordId(kw),
                tier: Tier::Confirmed,
                intent_bearing: true,
                source: earshot::types::SourceId(0),
                leg: "t".into(),
                audio_pos_ms: 1000,
                audio_consumed_ms: 1000,
                pcm: Arc::from(vec![0i16; 16_000]),
                sample_rate_hz: 16_000,
                decision_ts: Some(i.decision_ts),
            })
            .unwrap();
    }

    /// gate + Rejected ⇒ refusal `verifier_veto`, nothing sent, ledger untouched.
    #[tokio::test]
    async fn gate_veto_sends_nothing() {
        let server = MockServer::start().await; // no POST mock: a send would 404
        let mut rig = rig(&server, 90, 1, false, Duration::from_secs(5)).await;
        let kws = vec![keyword(90, 1)];
        let v = verifier(
            &section("gate", "send", 500),
            &kws,
            "totally unrelated words",
            0,
        );
        Arc::get_mut(&mut rig.ctx).unwrap().verifier = Some(v.client.clone());
        let i = intent_for(0, 90, 1, Tier::Confirmed);
        post(&v, 0, &i);
        let (tx, rx) = tokio::sync::mpsc::channel(8);
        tx.send(i).await.unwrap();
        drop(tx);
        let report = run_executor(rx, rig.ctx.clone()).await;
        let o = &report.orders[0];
        assert_eq!(o.refusal.as_deref(), Some("verifier_veto"), "{o:?}");
        assert_eq!(o.verify_verdict.as_deref(), Some("rejected"));
        assert_eq!(o.verify_gated, Some(true));
        assert!(o.send_ts.is_none());
        assert_eq!(rig.ctx.gate.ledger_spent_cents(), 0);
        v.client.shutdown();
        assert_eq!(v.handle.join().rows.len(), 1);
    }

    /// gate + Confirmed ⇒ proceeds to the wire (dry_run here so no server needed).
    #[tokio::test]
    async fn gate_confirmed_proceeds() {
        let server = MockServer::start().await;
        let mut rig = rig(&server, 90, 1, true, Duration::from_secs(5)).await;
        let kws = vec![keyword(90, 1)];
        let v = verifier(
            &section("gate", "send", 500),
            &kws,
            "the test word was said",
            0,
        );
        Arc::get_mut(&mut rig.ctx).unwrap().verifier = Some(v.client.clone());
        let i = intent_for(0, 90, 1, Tier::Confirmed);
        post(&v, 0, &i);
        let (tx, rx) = tokio::sync::mpsc::channel(8);
        tx.send(i).await.unwrap();
        drop(tx);
        let report = run_executor(rx, rig.ctx.clone()).await;
        let o = &report.orders[0];
        assert_eq!(o.verify_verdict.as_deref(), Some("confirmed"), "{o:?}");
        assert_eq!(
            o.refusal.as_deref(),
            Some("dry_run"),
            "reached the dry-run stop, i.e. past the hold"
        );
        v.client.shutdown();
    }

    /// gate + slow verifier: `on_timeout = send` proceeds with verdict `timeout`;
    /// `on_timeout = hold` refuses as `verifier_timeout`. Both bounded by budget.
    #[tokio::test]
    async fn gate_timeout_policy() {
        for (policy, want_refusal) in [("send", "dry_run"), ("hold", "verifier_timeout")] {
            let server = MockServer::start().await;
            let mut rig = rig(&server, 90, 1, true, Duration::from_secs(5)).await;
            let kws = vec![keyword(90, 1)];
            let v = verifier(&section("gate", policy, 150), &kws, "test", 2000);
            Arc::get_mut(&mut rig.ctx).unwrap().verifier = Some(v.client.clone());
            let i = intent_for(0, 90, 1, Tier::Confirmed);
            post(&v, 0, &i);
            let (tx, rx) = tokio::sync::mpsc::channel(8);
            tx.send(i).await.unwrap();
            drop(tx);
            let t0 = std::time::Instant::now();
            let report = run_executor(rx, rig.ctx.clone()).await;
            let waited = t0.elapsed();
            let o = &report.orders[0];
            assert_eq!(
                o.verify_verdict.as_deref(),
                Some("timeout"),
                "{policy}: {o:?}"
            );
            assert_eq!(o.refusal.as_deref(), Some(want_refusal), "{policy}: {o:?}");
            assert!(
                waited < Duration::from_millis(1500),
                "{policy}: hold must be bounded by budget, took {waited:?}"
            );
            assert!(o.verify_wait_ns.unwrap() >= 100_000_000, "{policy}: {o:?}");
            v.client.shutdown();
            v.handle.join();
        }
    }

    /// shadow mode never waits: the intent proceeds immediately even though the
    /// verifier is slow; the row is still produced for the study.
    #[tokio::test]
    async fn shadow_never_waits() {
        let server = MockServer::start().await;
        let mut rig = rig(&server, 90, 1, true, Duration::from_secs(5)).await;
        let kws = vec![keyword(90, 1)];
        let v = verifier(&section("shadow", "send", 500), &kws, "test", 700);
        Arc::get_mut(&mut rig.ctx).unwrap().verifier = Some(v.client.clone());
        let i = intent_for(0, 90, 1, Tier::Confirmed);
        post(&v, 0, &i);
        let (tx, rx) = tokio::sync::mpsc::channel(8);
        tx.send(i).await.unwrap();
        drop(tx);
        let t0 = std::time::Instant::now();
        let report = run_executor(rx, rig.ctx.clone()).await;
        assert!(
            t0.elapsed() < Duration::from_millis(400),
            "shadow must not hold: {:?}",
            t0.elapsed()
        );
        let o = &report.orders[0];
        assert_eq!(o.verify_gated, Some(false));
        assert!(o.verify_wait_ns.is_none());
        assert_eq!(o.refusal.as_deref(), Some("dry_run"));
        v.client.shutdown();
        assert_eq!(
            v.handle.join().rows.len(),
            1,
            "shadow still records the verdict"
        );
    }

    /// A 1¢ observe intent never waits, even in gate mode with an opted-in ticker.
    #[tokio::test]
    async fn observe_never_waits_in_gate_mode() {
        let server = MockServer::start().await;
        let mut rig = rig(&server, 1, 1, false, Duration::from_secs(5)).await;
        let kws = vec![keyword(1, 1)];
        let v = verifier(&section("gate", "hold", 500), &kws, "nothing", 700);
        Arc::get_mut(&mut rig.ctx).unwrap().verifier = Some(v.client.clone());
        let i = intent_for(0, 1, 1, Tier::Confirmed);
        post(&v, 0, &i);
        let (tx, rx) = tokio::sync::mpsc::channel(8);
        tx.send(i).await.unwrap();
        drop(tx);
        let t0 = std::time::Instant::now();
        let report = run_executor(rx, rig.ctx.clone()).await;
        assert!(
            t0.elapsed() < Duration::from_millis(400),
            "{:?}",
            t0.elapsed()
        );
        let o = &report.orders[0];
        assert_eq!(o.verify_gated, Some(false));
        assert_eq!(report.observe_noncross, 1);
        assert!(o.refusal.is_none(), "{o:?}");
        v.client.shutdown();
    }
}
