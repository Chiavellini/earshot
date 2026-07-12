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
use earshot::kalshi::rest::RestClient;
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
        market_ticker: TICKER.into(),
        max_price_cents: price,
        tranche_contracts: tranche,
        tier,
        audio_ts: now,
        decision_ts: now,
    }
}

struct Rig {
    ctx: Arc<ExecCtx>,
    _state: tempfile::TempDir,
}

async fn rig(server: &MockServer, cap: u8, budget: u32, dry_run: bool, timeout: Duration) -> Rig {
    rig_kws(server, vec![keyword(cap, budget)], dry_run, timeout).await
}

async fn rig_kws(server: &MockServer, kws: Vec<KeywordConfig>, dry_run: bool, timeout: Duration) -> Rig {
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
        RiskConfig { allowlist_tickers: vec![TICKER.into()], ..Default::default() },
        state.path(),
    )
    .unwrap();
    gate.arm();
    let presigner = Presigner::new(signer, "POST", "/trade-api/v2/portfolio/events/orders".into());
    let haircuts: HashMap<u16, u32> = (0..kws.len() as u16).map(|i| (i, 0u32)).collect();
    Rig {
        ctx: Arc::new(ExecCtx {
            rest,
            presigner,
            cache,
            gate: Arc::new(gate),
            prestaged: prestage_orders("EV", &kws),
            haircuts,
            dry_run,
            registry: earshot::kalshi::OrderRegistry::default(),
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
            "client_order_id": earshot::kalshi::rest::deterministic_client_order_id("EV:0:Confirmed"),
        })))
        .respond_with(ResponseTemplate::new(201).set_body_json(serde_json::json!({
            "order": {"order_id": "srv-1", "status": "canceled"}
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
    assert!(!o.lost_race);
    assert!(o.send_ts.is_some() && o.ack_ts.is_some());
    assert!(o.ack_ts.unwrap() > o.send_ts.unwrap());
    let edges = report.recorder.snapshot();
    for edge in ["decision→exec", "presign_age", "exec→send", "send→ack", "decision→ack", "capture→ack"] {
        assert!(edges.get(edge).map(|s| s.count > 0).unwrap_or(false), "edge {edge} empty");
    }
    assert_eq!(rig.ctx.gate.ledger_spent_cents(), 90); // 1 × 90¢ worst-case debit
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
    let o = &report.orders[0];
    assert!(o.lost_race);
    assert!(o.send_ts.is_none());
    assert_eq!(o.best_ask, Some((85, 500)));
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
                .set_body_json(serde_json::json!({"order": {"order_id": "slow", "status": "canceled"}})),
        )
        .expect(1) // exactly one POST: a blind retry would make this 2 and fail
        .mount(&server)
        .await;
    // Status query finds the order landed after all.
    Mock::given(method("GET"))
        .and(path("/portfolio/orders"))
        .and(query_param(
            "client_order_id",
            earshot::kalshi::rest::deterministic_client_order_id("EV:0:Confirmed"),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "orders": [{"order_id": "srv-9", "status": "canceled"}]
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
    // Ledger debited despite the ambiguous send — worst case counts.
    assert_eq!(rig.ctx.gate.ledger_spent_cents(), 90);
}

#[tokio::test]
async fn burst_intents_overlap_not_serialize() {
    let server = MockServer::start().await;
    // Every POST takes 200 ms. Six intents fired in one burst must complete
    // in ~one delay (spawned concurrently), not six (serialized).
    Mock::given(method("POST"))
        .and(path("/portfolio/events/orders"))
        .respond_with(
            ResponseTemplate::new(201)
                .set_delay(Duration::from_millis(200))
                .set_body_json(serde_json::json!({"order": {"order_id": "srv-b", "status": "canceled"}})),
        )
        .expect(6)
        .mount(&server)
        .await;

    // 6 keywords at 15¢ × 1 contract: 90¢ total stays inside the $1 ledger.
    let kws: Vec<KeywordConfig> = (0..6).map(|_| keyword(15, 1)).collect();
    let rig = rig_kws(&server, kws, false, Duration::from_secs(5)).await;
    // Ask must sit under the 15¢ cap for the sweep to be marketable.
    rig.ctx.cache.publish(
        TICKER,
        Book { yes_bids: vec![(5, 100)], yes_asks: vec![(10, 500)], seq: 2, as_of: MonotonicNs::now() },
    );

    let (tx, rx) = tokio::sync::mpsc::channel(8);
    for kw in 0..6u16 {
        tx.send(intent_for(kw, 15, 1, Tier::Confirmed)).await.unwrap();
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
    assert!(wall >= Duration::from_millis(200), "mock delay never happened?");
    // Distinct idempotency keys per keyword, and exact ledger math under
    // concurrent record_send calls.
    let mut ids: Vec<&str> = report.orders.iter().map(|o| o.client_order_id.as_str()).collect();
    ids.sort();
    ids.dedup();
    assert_eq!(ids.len(), 6, "client_order_ids must be distinct across the burst");
    assert_eq!(rig.ctx.gate.ledger_spent_cents(), 90); // 6 × 15¢, no lost updates
}

#[tokio::test]
async fn burst_with_mixed_outcomes_accounts_exactly() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/portfolio/events/orders"))
        .respond_with(ResponseTemplate::new(201).set_body_json(
            serde_json::json!({"order": {"order_id": "srv-m", "status": "canceled"}}),
        ))
        .expect(1) // only the marketable keyword may POST
        .mount(&server)
        .await;

    // kw0 cap 15¢ ≥ ask 10¢ → fires; kw1 cap 5¢ < ask → LOST_RACE.
    let rig = rig_kws(&server, vec![keyword(15, 1), keyword(5, 1)], false, Duration::from_secs(5)).await;
    rig.ctx.cache.publish(
        TICKER,
        Book { yes_bids: vec![(2, 100)], yes_asks: vec![(10, 500)], seq: 2, as_of: MonotonicNs::now() },
    );

    let (tx, rx) = tokio::sync::mpsc::channel(8);
    tx.send(intent_for(0, 15, 1, Tier::Confirmed)).await.unwrap();
    tx.send(intent_for(1, 5, 1, Tier::Confirmed)).await.unwrap();
    drop(tx);
    let report = run_executor(rx, rig.ctx.clone()).await;

    assert_eq!(report.orders.len(), 2);
    assert_eq!(report.lost_races, 1);
    let fired: Vec<_> = report.orders.iter().filter(|o| !o.lost_race).collect();
    assert_eq!(fired.len(), 1);
    assert_eq!(fired[0].order_id.as_deref(), Some("srv-m"));
    assert_eq!(rig.ctx.gate.ledger_spent_cents(), 15, "only the fired order debits");
}

#[tokio::test]
async fn kill_switch_stop_file_cancels_and_disarms() {
    let state = tempfile::tempdir().unwrap();
    let gate = Arc::new(
        RiskGate::new(
            RiskConfig { allowlist_tickers: vec![TICKER.into()], ..Default::default() },
            state.path(),
        )
        .unwrap(),
    );
    gate.arm();
    let stop = state.path().join("STOP");
    let cancelled = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let c2 = cancelled.clone();
    let watcher = earshot::risk::spawn_kill_watcher(gate.clone(), stop.clone(), move || {
        let c = c2.clone();
        async move {
            c.store(true, std::sync::atomic::Ordering::SeqCst);
        }
    });
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(gate.check_order(TICKER, 1, 1, 0, 0).is_ok());
    std::fs::write(&stop, "x").unwrap();
    watcher.await.unwrap();
    assert!(gate.is_killed());
    assert!(cancelled.load(std::sync::atomic::Ordering::SeqCst));
    assert_eq!(gate.check_order(TICKER, 1, 1, 0, 0), Err(earshot::risk::Refusal::Killed));
}
