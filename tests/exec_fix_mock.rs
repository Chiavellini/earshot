//! Executor integration through `OrderTransport::Fix`: the full
//! `run_executor` fire path against an in-process mock FIX server, proving
//! the gates (crossing, EV, risk, ledger-before-send) are transport-blind,
//! the ER maps into `OrderRecord`, and the ambiguous path (no ER) falls back
//! to the REST status query by client id — the dual-wire safety net.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use earshot::clock::MonotonicNs;
use earshot::config::{KeywordConfig, Policy};
use earshot::kalshi::auth::Signer;
use earshot::kalshi::book::{Book, BookCache};
use earshot::kalshi::executor::{prestage_orders, run_executor, ExecCtx};
use earshot::kalshi::fix::encode::{logon_prehash, FixWriter};
use earshot::kalshi::fix::parse::{frame_len, FixMsg};
use earshot::kalshi::fix::transport::FixTransport;
use earshot::kalshi::fix::FixConfig;
use earshot::kalshi::rest::RestClient;
use earshot::kalshi::transport::{OrderTransport, StageWire};
use earshot::risk::{RiskConfig, RiskGate};
use earshot::types::{DecisionIntent, KeywordId, Tier};
use rsa::pkcs1::EncodeRsaPrivateKey;
use rsa::pss::VerifyingKey;
use rsa::sha2::Sha256;
use rsa::signature::Verifier;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use wiremock::matchers::{method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

const TICKER: &str = "KX-FIX-EXEC";

fn test_keypair() -> (Arc<Signer>, rsa::RsaPublicKey) {
    let key = rsa::RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 2048).unwrap();
    let pem = key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF).unwrap();
    (
        Arc::new(Signer::from_pem("SENDER-KEY".into(), &pem).unwrap()),
        key.to_public_key(),
    )
}

fn fix_cfg(port: u16) -> FixConfig {
    FixConfig {
        host: "127.0.0.1".into(),
        port,
        sender_comp_id: "SENDER-KEY".into(),
        target_comp_id: "KalshiNR".into(),
        heartbeat_secs: 10,
        tls: false,
    }
}

fn keyword(cap: u8, budget: u32) -> KeywordConfig {
    KeywordConfig {
        market_ticker: TICKER.into(),
        phrase: "test".into(),
        variants: vec!["test".into()],
        max_price_cents: cap,
        budget_contracts: budget,
        policy: Policy::ConfirmedOnly,
        rule_risk_haircut_cents: 0,
        fast_prefixes: vec![],
        fast_threshold: None,
        confirmed_threshold: None,
        boost: None,
        min_confidence_fast: 0.0,
        min_confidence_confirmed: 0.0,
    }
}

fn intent(price: u8, tranche: u32) -> DecisionIntent {
    let now = MonotonicNs::now();
    DecisionIntent {
        keyword: KeywordId(0),
        source: earshot::types::SourceId(0),
        market_ticker: TICKER.into(),
        max_price_cents: price,
        tranche_contracts: tranche,
        tier: Tier::Confirmed,
        audio_ts: now,
        decision_ts: now,
        handoff_ts: now,
    }
}

async fn read_frame(stream: &mut TcpStream, buf: &mut Vec<u8>) -> Option<Vec<u8>> {
    loop {
        if let Some(n) = frame_len(buf) {
            return Some(buf.drain(..n).collect());
        }
        let mut chunk = [0u8; 4096];
        match stream.read(&mut chunk).await {
            Ok(0) | Err(_) => return None,
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
        }
    }
}

async fn accept_with_logon(
    listener: TcpListener,
    pubkey: rsa::RsaPublicKey,
) -> (TcpStream, Vec<u8>) {
    let (mut stream, _) = listener.accept().await.unwrap();
    let mut buf = Vec::new();
    let frame = read_frame(&mut stream, &mut buf)
        .await
        .expect("logon frame");
    let msg = FixMsg::parse(&frame).expect("parseable logon");
    assert_eq!(msg.msg_type, b"A");
    let sending_time = msg.get_str(52).unwrap();
    let sig_bytes = {
        use base64::Engine;
        base64::engine::general_purpose::STANDARD
            .decode(msg.get_str(96).unwrap())
            .unwrap()
    };
    let verifying: VerifyingKey<Sha256> = VerifyingKey::new(pubkey);
    let sig = rsa::pss::Signature::try_from(sig_bytes.as_slice()).unwrap();
    verifying
        .verify(
            &logon_prehash(sending_time, msg.seq, "SENDER-KEY", "KalshiNR"),
            &sig,
        )
        .expect("logon signature");
    let mut w = FixWriter::new();
    w.begin("A", "KalshiNR", "SENDER-KEY", 1, sending_time);
    w.tag_str(98, "0");
    w.tag_str(108, "10");
    w.tag_str(1137, "9");
    stream.write_all(&w.finish()).await.unwrap();
    (stream, buf)
}

struct Rig {
    ctx: Arc<ExecCtx>,
    _state: tempfile::TempDir,
}

/// Executor context wired to a live `FixTransport` + a wiremock REST base
/// (the ambiguous-path status-query wire).
async fn rig(fix: Arc<FixTransport>, signer: Arc<Signer>, rest_base: String, cap: u8) -> Rig {
    let rest =
        RestClient::with_base_and_timeout(rest_base, signer, Duration::from_secs(5)).unwrap();
    let cache = BookCache::new(&[TICKER.to_string()]);
    cache.touch();
    cache.publish(
        TICKER,
        Book {
            yes_bids: vec![(5, 100)],
            yes_asks: vec![(10, 500)],
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
    let kws = vec![keyword(cap, 1)];
    let prestaged = prestage_orders("EV", &kws, StageWire::Fix(fix.config()));
    Rig {
        ctx: Arc::new(ExecCtx {
            transport: OrderTransport::Fix { fix },
            rest: rest.clone(),
            cache,
            gate: Arc::new(gate),
            prestaged,
            haircuts: HashMap::from([(0u16, 0u32)]),
            dry_run: false,
            registry: earshot::kalshi::OrderRegistry::default(),
            order_evidence: None,
            decision_plan_sha1: "0".repeat(40),
            keyword_text: HashMap::new(),
            rest_endpoint_id: rest.endpoint_id(),
        }),
        _state: state,
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn executor_fires_through_fix_and_records_the_fill() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (signer, pubkey) = test_keypair();

    let server = tokio::spawn(async move {
        let (mut stream, mut buf) = accept_with_logon(listener, pubkey).await;
        let frame = read_frame(&mut stream, &mut buf)
            .await
            .expect("order frame");
        let msg = FixMsg::parse(&frame).unwrap();
        assert_eq!(msg.msg_type, b"D");
        assert_eq!(msg.get_str(55), Some(TICKER));
        assert_eq!(msg.get_str(44), Some("15"), "integer-cent price");
        let cl = msg.get_str(11).unwrap().to_string();
        let mut w = FixWriter::new();
        w.begin("8", "KalshiNR", "SENDER-KEY", 2, "20260721-12:00:00.050");
        w.tag_str(11, &cl);
        w.tag_str(37, "srv-fix-exec");
        w.tag_str(150, "F");
        w.tag_str(39, "2");
        w.tag_str(14, "1");
        w.tag_str(151, "0");
        w.tag_str(31, "10");
        w.tag_str(32, "1");
        stream.write_all(&w.finish()).await.unwrap();
        let _ = read_frame(&mut stream, &mut buf).await;
    });

    let rest_mock = MockServer::start().await;
    let fix = FixTransport::connect(fix_cfg(port), signer.clone(), Duration::from_secs(2))
        .await
        .unwrap();
    let rig = rig(fix, signer, rest_mock.uri(), 15).await;

    let (tx, rx) = tokio::sync::mpsc::channel(8);
    tx.send(intent(15, 1)).await.unwrap();
    drop(tx);
    let report = run_executor(rx, rig.ctx.clone()).await;

    assert_eq!(report.orders.len(), 1);
    let o = &report.orders[0];
    assert_eq!(o.order_id.as_deref(), Some("srv-fix-exec"));
    assert_eq!(o.order_status.as_deref(), Some("filled"));
    assert_eq!(
        o.wire_state,
        earshot::kalshi::executor::OrderWireState::Observed
    );
    assert_eq!(o.transport_protocol.as_deref(), Some("fix"));
    assert_eq!(
        o.protocol_status.as_deref(),
        Some("exec_type:F;ord_status:2")
    );
    assert_eq!(o.exchange_accepted, Some(true));
    assert_eq!(o.order_terminal, Some(true));
    assert_eq!(
        o.fill
            .as_ref()
            .and_then(|f| f.cumulative_contracts.as_deref()),
        Some("1")
    );
    assert_eq!(
        o.fill.as_ref().and_then(|f| f.last_fill_price.as_deref()),
        Some("10")
    );
    assert!(!o.lost_race);
    assert!(o.reached_wire() && o.hdrs_ts.is_some() && o.ack_ts.is_some());
    assert!(o.ack_ts.unwrap() >= o.hdrs_ts.unwrap());
    assert!(o.hdrs_ts.unwrap() > o.send_ts.unwrap());
    // Ledger debited before the send (worst case at the cap), registry holds
    // the exchange id for the kill switch.
    assert_eq!(rig.ctx.gate.ledger_spent_cents(), 15);
    assert_eq!(rig.ctx.registry.ids(), vec!["srv-fix-exec".to_string()]);
    // Per-transport honest telemetry: send→ack edges recorded, presign_age
    // ABSENT (FIX has no presign step — a fake zero would poison the stats).
    let edges = report.recorder.snapshot();
    for edge in [
        "exec→send",
        "send→hdrs",
        "hdrs→ack",
        "send→ack",
        "decision→ack",
        "capture→ack",
    ] {
        assert!(
            edges.get(edge).map(|s| s.count > 0).unwrap_or(false),
            "edge {edge} empty"
        );
    }
    assert!(
        edges.get("presign_age").is_none_or(|s| s.count == 0),
        "presign_age must not be recorded on FIX"
    );

    server.abort();
}

#[tokio::test(flavor = "multi_thread")]
async fn swallowed_er_falls_back_to_rest_status_query() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (signer, pubkey) = test_keypair();

    let server = tokio::spawn(async move {
        let (mut stream, mut buf) = accept_with_logon(listener, pubkey).await;
        // Swallow the 35=D: read it, never answer.
        let _ = read_frame(&mut stream, &mut buf).await;
        let _ = read_frame(&mut stream, &mut buf).await;
    });

    // The REST wire has the answer: the order DID land server-side.
    let rest_mock = MockServer::start().await;
    let cl = earshot::kalshi::executor::deterministic_order_client_id(
        "EV",
        TICKER,
        15,
        1,
        0,
        Tier::Confirmed,
    );
    Mock::given(method("GET"))
        .and(path("/portfolio/orders"))
        .and(query_param("client_order_id", cl.clone()))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "orders": [{
                "client_order_id": cl,
                "order_id": "srv-rest-9",
                "status": "canceled"
            }]
        })))
        .expect(1)
        .mount(&rest_mock)
        .await;

    let fix = FixTransport::connect(fix_cfg(port), signer.clone(), Duration::from_millis(300))
        .await
        .unwrap();
    let rig = rig(fix, signer, rest_mock.uri(), 15).await;

    let (tx, rx) = tokio::sync::mpsc::channel(8);
    tx.send(intent(15, 1)).await.unwrap();
    drop(tx);
    let report = run_executor(rx, rig.ctx.clone()).await;

    let o = &report.orders[0];
    // ER never came, but the REST query resolved the ambiguity — no blind
    // retry, order recorded from the query.
    assert_eq!(o.order_id.as_deref(), Some("srv-rest-9"));
    assert_eq!(o.order_status.as_deref(), Some("canceled"));
    assert!(
        o.send_ts.is_some(),
        "FIX timeout happened after the write boundary"
    );
    assert_eq!(
        o.wire_state,
        earshot::kalshi::executor::OrderWireState::Reconciled
    );
    assert_eq!(o.transport_protocol.as_deref(), Some("fix"));
    assert!(o.transport_error.is_some());
    assert!(o.reconciliation_response.is_some());
    assert!(o.refusal.is_none());
    // The debit stands even though the FIX ack never arrived (worst case).
    assert_eq!(rig.ctx.gate.ledger_spent_cents(), 15);

    server.abort();
}

#[tokio::test(flavor = "multi_thread")]
async fn gates_hold_upstream_of_the_fix_wire() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (signer, pubkey) = test_keypair();
    let (order_seen_tx, mut order_seen_rx) = tokio::sync::mpsc::unbounded_channel::<()>();

    let server = tokio::spawn(async move {
        let (mut stream, mut buf) = accept_with_logon(listener, pubkey).await;
        while let Some(frame) = read_frame(&mut stream, &mut buf).await {
            let msg = FixMsg::parse(&frame).unwrap();
            if msg.msg_type == b"D" {
                let _ = order_seen_tx.send(());
            }
        }
    });

    let rest_mock = MockServer::start().await;
    let fix = FixTransport::connect(fix_cfg(port), signer.clone(), Duration::from_millis(300))
        .await
        .unwrap();
    // Cap 8¢ < best ask 10¢ → LOST_RACE upstream of any wire.
    let rig = rig(fix, signer, rest_mock.uri(), 8).await;

    let (tx, rx) = tokio::sync::mpsc::channel(8);
    tx.send(intent(8, 1)).await.unwrap();
    drop(tx);
    let report = run_executor(rx, rig.ctx.clone()).await;

    assert_eq!(report.lost_races, 1);
    assert!(report.orders[0].send_ts.is_none());
    assert_eq!(
        rig.ctx.gate.ledger_spent_cents(),
        0,
        "no debit without a send"
    );
    assert!(
        order_seen_rx.try_recv().is_err(),
        "no 35=D may reach the wire on a lost race"
    );

    server.abort();
}
