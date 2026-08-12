use std::collections::BTreeSet;
use std::sync::{Arc, Barrier};

use earshot::kalshi::order_evidence::{
    unresolved_client_ids, EvidenceTier, ExchangeObserved, IntentEvaluated, InvocationPrepared,
    NotAttempted, OrderAction, OrderEvidenceCommand, OrderEvidenceError, OrderEvidenceIdentity,
    OrderEvidenceJournal, OrderSide, OrderTimeInForce, RawEvidence, ReconcileUnresolved,
    Reconciled, RecoveryAction, SendRequested, TransportFailed, TransportProtocol, WireAttempted,
};

fn identity() -> OrderEvidenceIdentity {
    OrderEvidenceIdentity {
        run_id: "run-26aug11-001".into(),
        event_id: "KXEARNINGSMENTIONTEST-26AUG11".into(),
        kalshi_env: "prod".into(),
        account_fingerprint: "sha256:stable-account".into(),
        plan_sha256: "a".repeat(64),
    }
}

fn evaluation(client: &str, ticker: &str) -> IntentEvaluated {
    IntentEvaluated {
        client_order_id: client.into(),
        market_ticker: ticker.into(),
        side: OrderSide::Yes,
        action: OrderAction::Buy,
        price_cents: 97,
        contract_count: 5,
        time_in_force: OrderTimeInForce::ImmediateOrCancel,
        tier: EvidenceTier::Confirmed,
        keyword_id: 7,
        keyword: "raises full-year guidance".into(),
        source_id: 2,
        audio_ts: 10,
        decision_ts: 20,
        exec_ts: 40,
        book_as_of_ts: 30,
        best_ask_cents: Some(96),
        request_sha256: "b".repeat(64),
        decision_plan_sha1: "c".repeat(40),
    }
}

fn send_request(client: &str, ticker: &str) -> SendRequested {
    SendRequested {
        client_order_id: client.into(),
        market_ticker: ticker.into(),
        requested_ts: 42,
    }
}

#[test]
fn evaluation_retains_missing_and_empty_book_outcomes() {
    for (client, book_as_of_ts, best_ask_cents) in [
        ("no-snapshot", 0, None),
        ("fresh-empty-book", 30, None),
        ("fresh-priced-book", 30, Some(96)),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("orders.jsonl");
        let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
        let mut evidence = evaluation(client, "MARKET-A");
        evidence.book_as_of_ts = book_as_of_ts;
        evidence.best_ask_cents = best_ask_cents;
        record(
            &journal,
            OrderEvidenceCommand::IntentEvaluated(evidence.clone()),
        );
        assert_eq!(journal.snapshot().orders[0].evaluation, evidence);
    }

    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    let mut impossible = evaluation("priced-without-snapshot", "MARKET-A");
    impossible.book_as_of_ts = 0;
    assert!(matches!(
        journal
            .sink()
            .record_blocking(OrderEvidenceCommand::IntentEvaluated(impossible)),
        Err(OrderEvidenceError::Invalid { .. })
    ));
}

#[test]
fn no_send_and_pre_invocation_refusal_paths_are_distinct_and_terminal() {
    #[derive(Clone, Copy)]
    enum Path {
        Evaluated,
        Requested,
        Prepared,
    }

    for (client, path_kind) in [
        ("evaluated-only", Path::Evaluated),
        ("send-requested", Path::Requested),
        ("invocation-prepared", Path::Prepared),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("orders.jsonl");
        let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
        record(
            &journal,
            OrderEvidenceCommand::IntentEvaluated(evaluation(client, "MARKET-A")),
        );
        if matches!(path_kind, Path::Requested | Path::Prepared) {
            record(
                &journal,
                OrderEvidenceCommand::SendRequested(send_request(client, "MARKET-A")),
            );
        }
        if matches!(path_kind, Path::Prepared) {
            record(
                &journal,
                OrderEvidenceCommand::InvocationPrepared(prepared(
                    client,
                    "MARKET-A",
                    TransportProtocol::Rest,
                    "rest:prod",
                )),
            );
        }
        record(
            &journal,
            OrderEvidenceCommand::NotAttempted(NotAttempted {
                client_order_id: client.into(),
                market_ticker: "MARKET-A".into(),
                refusal_ts: 50,
                reason: "pre-invocation outcome".into(),
            }),
        );
        let state = &journal.snapshot().orders[0];
        assert!(state.terminal);
        assert_eq!(state.recovery_action(), None);
        assert_eq!(
            state.send_request.is_some(),
            matches!(path_kind, Path::Requested | Path::Prepared)
        );
        assert_eq!(
            state.invocation_prepared.is_some(),
            matches!(path_kind, Path::Prepared)
        );
    }
}

#[test]
fn request_and_preparation_timestamps_cannot_precede_their_parents() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("early-request.jsonl");
    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    record(
        &journal,
        OrderEvidenceCommand::IntentEvaluated(evaluation("early-request", "MARKET-A")),
    );
    let mut request = send_request("early-request", "MARKET-A");
    request.requested_ts = 39;
    assert!(matches!(
        journal
            .sink()
            .record_blocking(OrderEvidenceCommand::SendRequested(request)),
        Err(OrderEvidenceError::Invalid { .. })
    ));

    let path = dir.path().join("early-preparation.jsonl");
    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    record(
        &journal,
        OrderEvidenceCommand::IntentEvaluated(evaluation("early-preparation", "MARKET-A")),
    );
    let mut request = send_request("early-preparation", "MARKET-A");
    request.requested_ts = 48;
    record(&journal, OrderEvidenceCommand::SendRequested(request));
    assert!(matches!(
        journal
            .sink()
            .record_blocking(OrderEvidenceCommand::InvocationPrepared(prepared(
                "early-preparation",
                "MARKET-A",
                TransportProtocol::Rest,
                "rest:prod",
            ))),
        Err(OrderEvidenceError::Invalid { .. })
    ));
}

fn wire(client: &str, ticker: &str, protocol: TransportProtocol, endpoint: &str) -> WireAttempted {
    WireAttempted {
        client_order_id: client.into(),
        market_ticker: ticker.into(),
        protocol,
        endpoint_id: endpoint.into(),
        send_ts: 50,
    }
}

fn prepared(
    client: &str,
    ticker: &str,
    protocol: TransportProtocol,
    endpoint: &str,
) -> InvocationPrepared {
    InvocationPrepared {
        client_order_id: client.into(),
        market_ticker: ticker.into(),
        protocol,
        endpoint_id: endpoint.into(),
        prepared_ts: 45,
        raw: RawEvidence::from_bytes("application/octet-stream", b"exact outbound bytes"),
    }
}

fn terminal_rejection(client: &str, ticker: &str) -> ExchangeObserved {
    ExchangeObserved {
        client_order_id: client.into(),
        market_ticker: ticker.into(),
        protocol: TransportProtocol::Rest,
        endpoint_id: "rest:prod".into(),
        observed_ts: 60,
        protocol_status: "http:400".into(),
        exchange_accepted: Some(false),
        order_terminal: true,
        echoed_client_order_id: Some(client.into()),
        order_id: None,
        order_status: Some("rejected".into()),
        exchange_event_ts: None,
        fill: None,
        raw: RawEvidence::from_bytes("application/json", br#"{"error":"bad request"}"#),
        parsed: Some(serde_json::json!({"error": "bad request"})),
        detail: None,
    }
}

fn record(journal: &OrderEvidenceJournal, command: OrderEvidenceCommand) {
    journal.sink().record_blocking(command).unwrap();
}

fn record_attempt(
    journal: &OrderEvidenceJournal,
    client: &str,
    ticker: &str,
    protocol: TransportProtocol,
    endpoint: &str,
) {
    record(
        journal,
        OrderEvidenceCommand::SendRequested(send_request(client, ticker)),
    );
    record(
        journal,
        OrderEvidenceCommand::InvocationPrepared(prepared(client, ticker, protocol, endpoint)),
    );
    record(
        journal,
        OrderEvidenceCommand::WireAttempted(wire(client, ticker, protocol, endpoint)),
    );
}

#[test]
fn complete_evaluation_and_full_event_history_survive_restart() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    let expected = evaluation("client-a", "MARKET-A");
    {
        let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
        record(
            &journal,
            OrderEvidenceCommand::IntentEvaluated(expected.clone()),
        );
        let snapshot = journal.snapshot();
        assert_eq!(snapshot.events.len(), 1);
        assert_eq!(snapshot.events[0].schema_version, 4);
        assert!(matches!(
            &snapshot.events[0].transition,
            OrderEvidenceCommand::IntentEvaluated(_)
        ));
        assert_eq!(snapshot.orders[0].evaluation, expected);
        assert_eq!(
            snapshot.orders[0].recovery_action(),
            Some(RecoveryAction::ConcludeNotAttempted)
        );
        journal.close().unwrap();
    }

    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    assert_eq!(journal.replayed_events().len(), 1);
    let first_process = journal.replayed_events()[0].process_instance_id.clone();
    let sink = journal.sink();
    let restarted_event = sink
        .record_blocking(OrderEvidenceCommand::IntentEvaluated(evaluation(
            "client-after-restart",
            "MARKET-AFTER-RESTART",
        )))
        .unwrap();
    assert_eq!(restarted_event.previous_process_instance_id, first_process);
    assert_eq!(
        journal
            .snapshot()
            .orders
            .iter()
            .find(|order| order.client_order_id == "client-a")
            .unwrap()
            .evaluation
            .keyword_id,
        7
    );
    assert_eq!(
        unresolved_client_ids(&journal.snapshot().orders),
        BTreeSet::from(["client-a".into(), "client-after-restart".into()])
    );
}

#[test]
fn evaluation_only_restart_is_terminalized_without_reconciliation() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    {
        let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
        record(
            &journal,
            OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
        );
        journal.close().unwrap();
    }

    {
        let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
        let recovery = &journal.unresolved_recoveries()[0];
        assert_eq!(
            recovery.recovery_action(),
            Some(RecoveryAction::ConcludeNotAttempted)
        );
        record(
            &journal,
            OrderEvidenceCommand::NotAttempted(NotAttempted {
                client_order_id: recovery.client_order_id.clone(),
                market_ticker: recovery.market_ticker.clone(),
                refusal_ts: 50,
                reason: "startup replay found no durable wire-attempt boundary".into(),
            }),
        );
        assert!(journal.unresolved_recoveries().is_empty());
        journal.close().unwrap();
    }

    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    assert!(journal.unresolved_recoveries().is_empty());
    let state = &journal.snapshot().orders[0];
    assert!(state.terminal);
    assert_eq!(
        state.last_kind,
        earshot::kalshi::order_evidence::OrderTransitionKind::NotAttempted
    );
}

#[test]
fn prepared_invocation_restart_remains_delivery_ambiguous() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    {
        let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
        record(
            &journal,
            OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
        );
        record(
            &journal,
            OrderEvidenceCommand::SendRequested(send_request("client-a", "MARKET-A")),
        );
        record(
            &journal,
            OrderEvidenceCommand::InvocationPrepared(prepared(
                "client-a",
                "MARKET-A",
                TransportProtocol::Rest,
                "rest:prod",
            )),
        );
        journal.close().unwrap();
    }

    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    let state = &journal.unresolved_recoveries()[0];
    assert_eq!(
        state.recovery_action(),
        Some(RecoveryAction::ReconcileExactClientId)
    );
    assert!(state.requires_reconciliation());
    assert!(state.wire_attempt.is_none());
    assert!(state.invocation_prepared.is_some());
}

#[test]
fn replay_retains_every_partial_exchange_observation_in_order() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    {
        let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
        record(
            &journal,
            OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
        );
        record_attempt(
            &journal,
            "client-a",
            "MARKET-A",
            TransportProtocol::Fix,
            "fix:prod",
        );
        for (observed_ts, status, filled) in [
            (60, "new", "0.00"),
            (70, "partially_filled", "0.50"),
            (80, "partially_filled", "1.25"),
        ] {
            record(
                &journal,
                OrderEvidenceCommand::ExchangeObserved(ExchangeObserved {
                    client_order_id: "client-a".into(),
                    market_ticker: "MARKET-A".into(),
                    protocol: TransportProtocol::Fix,
                    endpoint_id: "fix:prod".into(),
                    observed_ts,
                    protocol_status: status.into(),
                    exchange_accepted: Some(true),
                    order_terminal: false,
                    echoed_client_order_id: Some("client-a".into()),
                    order_id: Some("order-a".into()),
                    order_status: Some(status.into()),
                    exchange_event_ts: Some(format!("exchange-ts-{observed_ts}")),
                    fill: Some(earshot::kalshi::order_evidence::FillEvidence {
                        filled_count_fp: Some(filled.into()),
                        remaining_count_fp: Some("3.75".into()),
                        average_price_dollars: Some("0.97".into()),
                        latest_fill_count_fp: None,
                        latest_fill_price_dollars: None,
                        latest_fill_exchange_ts: Some(format!("fill-ts-{observed_ts}")),
                    }),
                    raw: RawEvidence::from_bytes(
                        "application/fix",
                        format!("35=8|39={status}|14={filled}").as_bytes(),
                    ),
                    parsed: None,
                    detail: None,
                }),
            );
        }
        assert_eq!(journal.replayed_events().len(), 7);
        journal.close().unwrap();
    }

    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    let events = journal.replayed_events();
    assert_eq!(events.len(), 7);
    let statuses = events
        .iter()
        .filter_map(|event| match &event.transition {
            OrderEvidenceCommand::ExchangeObserved(observation) => {
                Some(observation.protocol_status.as_str())
            }
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(statuses, ["new", "partially_filled", "partially_filled"]);
}

#[test]
fn not_attempted_is_terminal_and_never_requires_reconciliation() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    {
        let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
        record(
            &journal,
            OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
        );
        record(
            &journal,
            OrderEvidenceCommand::NotAttempted(NotAttempted {
                client_order_id: "client-a".into(),
                market_ticker: "MARKET-A".into(),
                refusal_ts: 45,
                reason: "risk permit refused before transport".into(),
            }),
        );
        assert!(journal.unresolved_recoveries().is_empty());
        journal.close().unwrap();
    }
    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    assert!(journal.unresolved_recoveries().is_empty());
    assert!(journal.snapshot().orders[0].terminal);
}

#[test]
fn terminal_exchange_rejection_is_not_unresolved_after_restart() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    {
        let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
        record(
            &journal,
            OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
        );
        record_attempt(
            &journal,
            "client-a",
            "MARKET-A",
            TransportProtocol::Rest,
            "rest:prod",
        );
        record(
            &journal,
            OrderEvidenceCommand::ExchangeObserved(terminal_rejection("client-a", "MARKET-A")),
        );
        journal.close().unwrap();
    }
    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    assert!(journal.unresolved_recoveries().is_empty());
}

#[test]
fn exact_terminal_rejection_reconciliation_needs_no_order_id() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    record(
        &journal,
        OrderEvidenceCommand::IntentEvaluated(evaluation("client-rejected", "MARKET-A")),
    );
    record(
        &journal,
        OrderEvidenceCommand::SendRequested(send_request("client-rejected", "MARKET-A")),
    );
    record(
        &journal,
        OrderEvidenceCommand::InvocationPrepared(prepared(
            "client-rejected",
            "MARKET-A",
            TransportProtocol::Rest,
            "rest:prod",
        )),
    );
    record(
        &journal,
        OrderEvidenceCommand::Reconciled(Reconciled {
            client_order_id: "client-rejected".into(),
            market_ticker: "MARKET-A".into(),
            protocol: TransportProtocol::Rest,
            endpoint_id: "rest:prod".into(),
            observed_ts: 60,
            queried_client_order_id: "client-rejected".into(),
            echoed_client_order_id: "client-rejected".into(),
            order_id: None,
            order_status: "rejected".into(),
            exchange_accepted: Some(false),
            order_terminal: true,
            exchange_event_ts: None,
            fill: None,
            raw: RawEvidence::from_bytes("application/json", b"{\"status\":\"rejected\"}"),
            parsed: Some(serde_json::json!({"status": "rejected"})),
        }),
    );
    assert!(journal.unresolved_recoveries().is_empty());
}

#[test]
fn exact_id_reconciliation_can_cross_from_fix_to_rest() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    record(
        &journal,
        OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
    );
    record_attempt(
        &journal,
        "client-a",
        "MARKET-A",
        TransportProtocol::Fix,
        "fix:prod",
    );
    record(
        &journal,
        OrderEvidenceCommand::TransportFailed(TransportFailed {
            client_order_id: "client-a".into(),
            market_ticker: "MARKET-A".into(),
            protocol: TransportProtocol::Fix,
            endpoint_id: "fix:prod".into(),
            failure_ts: 70,
            detail: "session disconnected before terminal ER".into(),
        }),
    );
    record(
        &journal,
        OrderEvidenceCommand::Reconciled(Reconciled {
            client_order_id: "client-a".into(),
            market_ticker: "MARKET-A".into(),
            protocol: TransportProtocol::Rest,
            endpoint_id: "rest:prod".into(),
            observed_ts: 80,
            queried_client_order_id: "client-a".into(),
            echoed_client_order_id: "client-a".into(),
            order_id: Some("order-123".into()),
            order_status: "canceled".into(),
            exchange_accepted: Some(true),
            order_terminal: true,
            exchange_event_ts: Some("2026-08-11T14:00:00Z".into()),
            fill: None,
            raw: RawEvidence::from_bytes("application/json", br#"{"client_order_id":"client-a"}"#),
            parsed: Some(serde_json::json!({"client_order_id": "client-a"})),
        }),
    );
    assert!(journal.unresolved_recoveries().is_empty());
    assert_eq!(
        journal.snapshot().orders[0].order_id.as_deref(),
        Some("order-123")
    );
}

#[test]
fn unresolved_queries_remain_loud_and_repeatable() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    record(
        &journal,
        OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
    );
    record(
        &journal,
        OrderEvidenceCommand::SendRequested(send_request("client-a", "MARKET-A")),
    );
    record(
        &journal,
        OrderEvidenceCommand::InvocationPrepared(prepared(
            "client-a",
            "MARKET-A",
            TransportProtocol::Rest,
            "rest:prod",
        )),
    );
    for observed_ts in [60, 70] {
        record(
            &journal,
            OrderEvidenceCommand::ReconcileUnresolved(ReconcileUnresolved {
                client_order_id: "client-a".into(),
                market_ticker: "MARKET-A".into(),
                protocol: TransportProtocol::Rest,
                endpoint_id: "rest:prod".into(),
                observed_ts,
                queried_client_order_id: "client-a".into(),
                raw: Some(RawEvidence::from_bytes("application/json", b"[]")),
                parsed: Some(serde_json::json!([])),
                detail: "no exactly echoed row in this response".into(),
            }),
        );
    }
    assert_eq!(journal.unresolved_recoveries().len(), 1);
    assert_eq!(journal.replayed_events().len(), 5);
}

#[test]
fn illegal_append_poisons_health_and_all_later_appends() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    let sink = journal.sink();
    let error = sink
        .record_blocking(OrderEvidenceCommand::NotAttempted(NotAttempted {
            client_order_id: "client-a".into(),
            market_ticker: "MARKET-A".into(),
            refusal_ts: 10,
            reason: "invalid first transition".into(),
        }))
        .unwrap_err();
    assert!(matches!(error, OrderEvidenceError::Invalid { .. }));
    assert!(!journal.health().is_healthy());
    assert!(matches!(
        sink.record_blocking(OrderEvidenceCommand::IntentEvaluated(evaluation(
            "client-b", "MARKET-B"
        ))),
        Err(OrderEvidenceError::Unavailable(_))
    ));
    assert!(journal.replayed_events().is_empty());
}

#[test]
fn duplicate_wire_ticker_drift_and_observation_before_wire_are_refused() {
    for invalid in ["duplicate_wire", "ticker_drift", "observed_before_wire"] {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("orders.jsonl");
        let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
        record(
            &journal,
            OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
        );
        if invalid == "duplicate_wire" {
            record_attempt(
                &journal,
                "client-a",
                "MARKET-A",
                TransportProtocol::Rest,
                "rest:prod",
            );
        }
        let command = match invalid {
            "duplicate_wire" => OrderEvidenceCommand::WireAttempted(wire(
                "client-a",
                "MARKET-A",
                TransportProtocol::Rest,
                "rest:prod",
            )),
            "ticker_drift" => OrderEvidenceCommand::NotAttempted(NotAttempted {
                client_order_id: "client-a".into(),
                market_ticker: "MARKET-B".into(),
                refusal_ts: 55,
                reason: "wrong ticker".into(),
            }),
            _ => OrderEvidenceCommand::ExchangeObserved(terminal_rejection("client-a", "MARKET-A")),
        };
        assert!(matches!(
            journal.sink().record_blocking(command),
            Err(OrderEvidenceError::Invalid { .. })
        ));
        assert!(!journal.health().is_healthy());
    }
}

#[test]
fn verified_reconciliation_requires_exact_echo_and_poisons_on_mismatch() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    record(
        &journal,
        OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
    );
    record(
        &journal,
        OrderEvidenceCommand::SendRequested(send_request("client-a", "MARKET-A")),
    );
    record(
        &journal,
        OrderEvidenceCommand::InvocationPrepared(prepared(
            "client-a",
            "MARKET-A",
            TransportProtocol::Rest,
            "rest:prod",
        )),
    );
    let result = journal
        .sink()
        .record_blocking(OrderEvidenceCommand::Reconciled(Reconciled {
            client_order_id: "client-a".into(),
            market_ticker: "MARKET-A".into(),
            protocol: TransportProtocol::Rest,
            endpoint_id: "rest:prod".into(),
            observed_ts: 60,
            queried_client_order_id: "client-a".into(),
            echoed_client_order_id: "foreign-client".into(),
            order_id: Some("foreign-order".into()),
            order_status: "resting".into(),
            exchange_accepted: Some(true),
            order_terminal: false,
            exchange_event_ts: None,
            fill: None,
            raw: RawEvidence::from_bytes("application/json", b"{}"),
            parsed: Some(serde_json::json!({})),
        }));
    assert!(matches!(result, Err(OrderEvidenceError::Invalid { .. })));
    assert!(!journal.health().is_healthy());
}

#[test]
fn known_nonterminal_order_requires_observation_or_cancellation() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    record(
        &journal,
        OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
    );
    record(
        &journal,
        OrderEvidenceCommand::SendRequested(send_request("client-a", "MARKET-A")),
    );
    record(
        &journal,
        OrderEvidenceCommand::InvocationPrepared(prepared(
            "client-a",
            "MARKET-A",
            TransportProtocol::Rest,
            "rest:prod",
        )),
    );
    record(
        &journal,
        OrderEvidenceCommand::Reconciled(Reconciled {
            client_order_id: "client-a".into(),
            market_ticker: "MARKET-A".into(),
            protocol: TransportProtocol::Rest,
            endpoint_id: "rest:prod".into(),
            observed_ts: 60,
            queried_client_order_id: "client-a".into(),
            echoed_client_order_id: "client-a".into(),
            order_id: Some("order-a".into()),
            order_status: "resting".into(),
            exchange_accepted: Some(true),
            order_terminal: false,
            exchange_event_ts: None,
            fill: None,
            raw: RawEvidence::from_bytes("application/json", b"{}"),
            parsed: Some(serde_json::json!({})),
        }),
    );
    assert_eq!(
        journal.unresolved_recoveries()[0].recovery_action(),
        Some(RecoveryAction::ObserveOrCancelKnownOrder {
            order_id: "order-a".into()
        })
    );
}

#[test]
fn transition_after_terminal_observation_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    record(
        &journal,
        OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
    );
    record_attempt(
        &journal,
        "client-a",
        "MARKET-A",
        TransportProtocol::Rest,
        "rest:prod",
    );
    record(
        &journal,
        OrderEvidenceCommand::ExchangeObserved(terminal_rejection("client-a", "MARKET-A")),
    );
    assert!(journal
        .sink()
        .record_blocking(OrderEvidenceCommand::ReconcileUnresolved(
            ReconcileUnresolved {
                client_order_id: "client-a".into(),
                market_ticker: "MARKET-A".into(),
                protocol: TransportProtocol::Rest,
                endpoint_id: "rest:prod".into(),
                observed_ts: 80,
                queried_client_order_id: "client-a".into(),
                raw: None,
                parsed: None,
                detail: "should not be legal".into(),
            }
        ))
        .is_err());
}

#[test]
fn raw_evidence_rejects_partial_or_tampered_fingerprints() {
    let mut raw = RawEvidence::from_bytes("application/json", b"exact response");
    assert_eq!(raw.decode().unwrap(), b"exact response");
    raw.bytes_len += 1;
    assert!(raw.decode().is_err());
    raw.bytes_len -= 1;
    raw.bytes_sha256 = "0".repeat(64);
    assert!(raw.decode().is_err());
}

#[test]
fn hash_chain_tampering_refuses_restart() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    {
        let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
        record(
            &journal,
            OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
        );
        record(
            &journal,
            OrderEvidenceCommand::IntentEvaluated(evaluation("client-b", "MARKET-B")),
        );
        journal.close().unwrap();
    }
    let raw = std::fs::read_to_string(&path).unwrap();
    let mut rows: Vec<serde_json::Value> = raw
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    rows[1]["previous_event_sha256"] = serde_json::Value::String("0".repeat(64));
    let damaged = rows
        .iter()
        .map(serde_json::to_string)
        .collect::<Result<Vec<_>, _>>()
        .unwrap()
        .join("\n")
        + "\n";
    std::fs::write(&path, damaged).unwrap();
    assert!(OrderEvidenceJournal::open(&path, identity()).is_err());
}

#[test]
fn blank_line_truncated_tail_and_identity_drift_are_refused() {
    for corruption in ["blank", "truncated", "identity"] {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("orders.jsonl");
        {
            let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
            record(
                &journal,
                OrderEvidenceCommand::IntentEvaluated(evaluation("client-a", "MARKET-A")),
            );
            journal.close().unwrap();
        }
        match corruption {
            "blank" => {
                let raw = std::fs::read_to_string(&path).unwrap();
                std::fs::write(&path, format!("{raw}\n")).unwrap();
                assert!(OrderEvidenceJournal::open(&path, identity()).is_err());
            }
            "truncated" => {
                let mut raw = std::fs::read(&path).unwrap();
                raw.pop();
                std::fs::write(&path, raw).unwrap();
                assert!(OrderEvidenceJournal::open(&path, identity()).is_err());
            }
            _ => {
                let mut changed = identity();
                changed.account_fingerprint = "different-account".into();
                assert!(OrderEvidenceJournal::open(&path, changed).is_err());
            }
        }
    }
}

#[test]
fn canonical_parent_must_exist_and_second_writer_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let missing = dir.path().join("missing").join("orders.jsonl");
    assert!(OrderEvidenceJournal::open(&missing, identity()).is_err());

    let path = dir.path().join("orders.jsonl");
    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    assert!(OrderEvidenceJournal::open(&path, identity()).is_err());
    journal.close().unwrap();
    assert!(OrderEvidenceJournal::open(&path, identity()).is_ok());
}

#[test]
fn concurrent_producers_receive_one_total_durable_order() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    let sink = journal.sink();
    let barrier = Arc::new(Barrier::new(17));
    let mut workers = Vec::new();
    for index in 0..16 {
        let sink = sink.clone();
        let barrier = Arc::clone(&barrier);
        workers.push(std::thread::spawn(move || {
            let client = format!("client-{index:02}");
            let ticker = format!("MARKET-{index:02}");
            barrier.wait();
            sink.record_blocking(OrderEvidenceCommand::IntentEvaluated(evaluation(
                &client, &ticker,
            )))
            .unwrap()
        }));
    }
    barrier.wait();
    let mut acknowledged = workers
        .into_iter()
        .map(|worker| worker.join().unwrap().journal_seq)
        .collect::<Vec<_>>();
    acknowledged.sort_unstable();
    assert_eq!(acknowledged, (0..16).collect::<Vec<_>>());
    let snapshot = journal.snapshot();
    assert_eq!(snapshot.events.len(), 16);
    assert!(snapshot
        .events
        .iter()
        .enumerate()
        .all(|(index, event)| event.journal_seq == index as u64));
    assert_eq!(snapshot.orders.len(), 16);
}

#[tokio::test(flavor = "current_thread")]
async fn async_door_returns_only_after_durable_event_is_visible() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("orders.jsonl");
    let journal = OrderEvidenceJournal::open(&path, identity()).unwrap();
    let event = journal
        .sink()
        .record_intent_evaluated(evaluation("client-async", "MARKET-ASYNC"))
        .await
        .unwrap();
    assert_eq!(event.journal_seq, 0);
    assert_eq!(journal.snapshot().events, vec![event]);
    assert_eq!(std::fs::read_to_string(path).unwrap().lines().count(), 1);
}
