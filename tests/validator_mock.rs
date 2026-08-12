//! Previously-untested Kalshi error paths, against wiremock: the async
//! LiveMarketValidator (fetch/refuse/rules-storage/category), the 201
//! ack-shape inference branches, and clock_offset_estimate sign/magnitude.

use std::sync::Arc;
use std::time::Duration;

use earshot::config::EventConfig;
use earshot::kalshi::auth::Signer;
use earshot::kalshi::rest::{OrderBodyV2, RestClient};
use earshot::kalshi::validator::{m2_live_gate, mention_probe_gate, LiveMarketValidator};
use rsa::pkcs1::EncodeRsaPrivateKey;
use serde_json::json;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const TICKER: &str = "KXVALTEST-26JUL14-WORD";

fn test_rest(server: &MockServer) -> RestClient {
    let key = rsa::RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 2048).unwrap();
    let pem = key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF).unwrap();
    let signer = Arc::new(Signer::from_pem("val-key".into(), &pem).unwrap());
    RestClient::with_base_and_timeout(server.uri(), signer, Duration::from_secs(5)).unwrap()
}

fn cfg_with_ticker() -> EventConfig {
    toml::from_str(&format!(
        r#"
        [event]
        id = "VAL-TEST"
        starts_at = "2026-07-14T12:30:00Z"
        [[sources]]
        kind = "replay"
        path = "unused.wav"
        [kalshi]
        series = "KXVALTEST"
        [[keywords]]
        market_ticker = "{TICKER}"
        phrase = "word"
        variants = ["word"]
        max_price_cents = 1
        budget_contracts = 1
        policy = "confirmed_only"
        "#
    ))
    .unwrap()
}

fn market_body(status: &str) -> serde_json::Value {
    json!({"market": {
        "ticker": TICKER,
        "event_ticker": "KXVALTEST-26JUL14",
        "status": status,
        "rules_primary": "Resolves YES if the word is said.",
        "rules_secondary": "",
        "early_close_condition": "utterance",
        "can_close_early": true,
    }})
}

#[tokio::test]
async fn unfetchable_market_refuses_to_arm() {
    let server = MockServer::start().await;
    // No mock: 404. Validation must refuse, not guess (P3).
    let rest = test_rest(&server);
    let state = tempfile::tempdir().unwrap();
    let v = LiveMarketValidator {
        rest: &rest,
        state_dir: state.path(),
    };
    let err = v.validate(&cfg_with_ticker()).await.unwrap_err();
    assert!(err.to_string().contains("not fetchable"), "got: {err}");
}

#[tokio::test]
async fn non_active_market_refuses_to_arm() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path(format!("/markets/{TICKER}")))
        .respond_with(ResponseTemplate::new(200).set_body_json(market_body("closed")))
        .mount(&server)
        .await;
    let rest = test_rest(&server);
    let state = tempfile::tempdir().unwrap();
    let v = LiveMarketValidator {
        rest: &rest,
        state_dir: state.path(),
    };
    let err = v.validate(&cfg_with_ticker()).await.unwrap_err();
    assert!(err.to_string().contains("not active"), "got: {err}");
}

#[tokio::test]
async fn happy_path_stores_rules_and_resolves_category() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path(format!("/markets/{TICKER}")))
        .respond_with(ResponseTemplate::new(200).set_body_json(market_body("active")))
        .mount(&server)
        .await;
    Mock::given(method("GET"))
        .and(path("/series/KXVALTEST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "series": {"ticker": "KXVALTEST", "category": "Mentions"}
        })))
        .mount(&server)
        .await;

    let rest = test_rest(&server);
    let state = tempfile::tempdir().unwrap();
    let v = LiveMarketValidator {
        rest: &rest,
        state_dir: state.path(),
    };
    let markets = v.validate(&cfg_with_ticker()).await.unwrap();

    assert_eq!(markets.len(), 1);
    assert_eq!(markets[0].category.as_deref(), Some("Mentions"));
    assert_eq!(markets[0].can_close_early, Some(true));
    // Rules audit trail written for exactly this market.
    let rules_path = state.path().join("rules").join(format!("{TICKER}.json"));
    let rules: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(rules_path).unwrap()).unwrap();
    assert_eq!(rules["rules_primary"], "Resolves YES if the word is said.");

    // Gate semantics on this market: M2 refuses it, mention-probe allows it.
    assert!(m2_live_gate(&markets, 1).is_err());
    assert!(mention_probe_gate(&markets, 1).is_ok());
}

#[tokio::test]
async fn series_failure_yields_none_category_and_gates_stay_conservative() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path(format!("/markets/{TICKER}")))
        .respond_with(ResponseTemplate::new(200).set_body_json(market_body("active")))
        .mount(&server)
        .await;
    // No /series mock: category lookup fails softly.
    let rest = test_rest(&server);
    let state = tempfile::tempdir().unwrap();
    let v = LiveMarketValidator {
        rest: &rest,
        state_dir: state.path(),
    };
    let markets = v.validate(&cfg_with_ticker()).await.unwrap();
    assert_eq!(markets[0].category, None);
    // Pinned behavior: an unknown category is NOT treated as Mentions by the
    // M2 gate (it only refuses a positive match) — the probe-order path does
    // its own explicit category check before money moves.
    assert!(m2_live_gate(&markets, 1).is_ok());
    assert!(mention_probe_gate(&markets, 1).is_ok());
}

// -- 201 ack-shape inference (rest.rs pinned branches) -----------------------

async fn ack_status_for(body: serde_json::Value) -> Option<String> {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/portfolio/events/orders"))
        .respond_with(ResponseTemplate::new(201).set_body_json(body))
        .mount(&server)
        .await;
    let rest = test_rest(&server);
    let order =
        OrderBodyV2::ioc_buy_yes(TICKER, 1, 1, "11111111-1111-4111-8111-111111111111".into());
    let (ack, _send_ts) = rest.create_order(&order).await.unwrap();
    ack.order_status
}

#[tokio::test]
async fn unwrapped_ack_infers_filled_partial_and_canceled() {
    // Live 201 IOC shape: no "order" wrapper, no "status" — fp count fields.
    let filled = ack_status_for(json!({
        "order_id": "srv-1", "fill_count": "2.00", "remaining_count": "0.00",
    }))
    .await;
    assert_eq!(filled.as_deref(), Some("filled"));

    let partial = ack_status_for(json!({
        "order_id": "srv-2", "fill_count": "1.00", "remaining_count": "3.00",
    }))
    .await;
    assert_eq!(partial.as_deref(), Some("partial"));

    let canceled = ack_status_for(json!({
        "order_id": "srv-3", "fill_count": "0.00", "remaining_count": "0.00",
    }))
    .await;
    assert_eq!(canceled.as_deref(), Some("canceled"));

    // Wrapped legacy/mock shape still honored via the explicit status field.
    let wrapped =
        ack_status_for(json!({"order": {"order_id": "srv-4", "status": "executed"}})).await;
    assert_eq!(wrapped.as_deref(), Some("executed"));
}

// -- clock offset (arm-time skew guard input) --------------------------------

async fn offset_with_date(date: &str) -> f64 {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/exchange/status"))
        .respond_with(ResponseTemplate::new(200).insert_header("Date", date))
        .mount(&server)
        .await;
    test_rest(&server).clock_offset_estimate().await.unwrap()
}

#[tokio::test]
async fn clock_offset_sign_and_magnitude() {
    // Server clock far in the future → large positive offset.
    let ahead = offset_with_date("Fri, 01 Jan 2100 00:00:00 GMT").await;
    assert!(ahead > 1.0e9, "expected huge positive offset, got {ahead}");
    // Server clock far in the past → large negative offset.
    let behind = offset_with_date("Sun, 06 Nov 1994 08:49:37 GMT").await;
    assert!(
        behind < -1.0e8,
        "expected huge negative offset, got {behind}"
    );
}
