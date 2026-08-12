use std::time::Duration;

use earshot::clock::MonotonicNs;
use earshot::kalshi::fix::observation_tape::{
    verify_fix_observation_tape, FixObservationTapeCloseReason, FixObservationTapeEvent,
    FixObservationTapeIdentity, FixObservationTapeOptions, FixObservationTapeRecordKind,
    FixObservationTapeTask,
};
use earshot::kalshi::fix::parse::{ExecReportView, FixMsg};
use earshot::kalshi::fix::session::OwnedExecReport;
use earshot::kalshi::fix::transport::{
    FixExecutionObservation, FixObservationEnvelope, FixOrderObservation, FixRejectKind,
    FixRejectObservation,
};

const SOH: u8 = 0x01;

fn identity() -> FixObservationTapeIdentity {
    FixObservationTapeIdentity {
        run_id: "20260812T120000Z-test".into(),
        event_id: "TEST-26AUG12".into(),
        kalshi_env: "demo".into(),
        account_fingerprint: format!("sha256:{}", "b".repeat(64)),
        plan_sha256: "a".repeat(64),
        endpoint_id: "fix://demo.example:8228/KalshiNR".into(),
    }
}

fn options() -> FixObservationTapeOptions {
    FixObservationTapeOptions {
        queue_capacity: 32,
        producer_quiescence_timeout: Duration::from_secs(1),
    }
}

fn fix_frame(fields: &[(&str, &str)]) -> Vec<u8> {
    let mut body = Vec::new();
    for (tag, value) in fields {
        body.extend_from_slice(tag.as_bytes());
        body.push(b'=');
        body.extend_from_slice(value.as_bytes());
        body.push(SOH);
    }
    let mut frame = format!("8=FIXT.1.1\x019={}\x01", body.len()).into_bytes();
    frame.extend_from_slice(&body);
    let checksum = frame.iter().fold(0u8, |sum, byte| sum.wrapping_add(*byte));
    frame.extend_from_slice(format!("10={checksum:03}\x01").as_bytes());
    FixMsg::parse(&frame).expect("test FIX frame validates");
    frame
}

fn execution_frame() -> Vec<u8> {
    fix_frame(&[
        ("35", "8"),
        ("34", "41"),
        ("49", "KalshiNR"),
        ("56", "CLIENT"),
        ("52", "20260812-17:00:00.000"),
        ("37", "exchange-order-1"),
        ("11", "client-order-1"),
        ("150", "F"),
        ("39", "2"),
        ("14", "1.00"),
        ("151", "0.00"),
        ("31", "0.97"),
        ("32", "1.00"),
        ("6", "0.97"),
    ])
}

fn execution(sequence: u64, raw: Vec<u8>) -> FixObservationEnvelope {
    let message = FixMsg::parse(&raw).unwrap();
    let view = ExecReportView::from_msg(&message).unwrap();
    FixObservationEnvelope {
        sequence,
        generation: 1,
        observation: FixOrderObservation::Execution(FixExecutionObservation {
            report: OwnedExecReport {
                cl_ord_id: view.cl_ord_id.map(str::to_string),
                order_id: view.order_id.map(str::to_string),
                status: view.normalized_status().to_string(),
                terminal: view.is_terminal(),
                raw: view.to_json(),
            },
            recv_ts: MonotonicNs(100 + sequence),
            ack_ts: MonotonicNs(110 + sequence),
            raw,
        }),
    }
}

fn session_reject(sequence: u64) -> FixObservationEnvelope {
    let raw = fix_frame(&[
        ("35", "3"),
        ("34", "42"),
        ("49", "KalshiNR"),
        ("56", "CLIENT"),
        ("52", "20260812-17:00:00.001"),
        ("45", "9"),
        ("372", "D"),
        ("373", "5"),
        ("58", "unknown outbound sequence"),
    ]);
    FixObservationEnvelope {
        sequence,
        generation: 1,
        observation: FixOrderObservation::Reject(FixRejectObservation {
            kind: FixRejectKind::Session,
            recv_ts: MonotonicNs(200 + sequence),
            ack_ts: MonotonicNs(210 + sequence),
            ref_seq: Some(9),
            ref_msg_type: Some("D".into()),
            business_ref_id: None,
            reason: Some("5".into()),
            text: Some("unknown outbound sequence".into()),
            raw,
            client_order_id: None,
        }),
    }
}

fn read_events(path: &std::path::Path) -> Vec<FixObservationTapeEvent> {
    std::fs::read_to_string(path)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

#[tokio::test]
async fn exact_execution_is_hash_chained_and_committed() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("fix-order-observations.jsonl");
    let (tap, mut handle) = FixObservationTapeTask::start(&path, identity(), options())
        .await
        .unwrap();
    let raw = execution_frame();
    tap.try_record(execution(1, raw.clone())).unwrap();
    handle
        .request_close(FixObservationTapeCloseReason::Requested)
        .unwrap();
    drop(tap);
    let summary = handle.wait().await.unwrap();

    assert!(summary.complete);
    assert!(summary.committed);
    assert_eq!(summary.accepted_deliveries, 1);
    assert_eq!(summary.observation_rows, 1);
    assert_eq!(summary.execution_rows, 1);
    assert_eq!(summary.correlated_rows, 1);
    assert_eq!(summary.orphan_rows, 0);
    assert!(summary
        .commit_path
        .as_ref()
        .is_some_and(|path| { std::path::Path::new(path).is_file() }));

    let verification = verify_fix_observation_tape(&path, &identity()).unwrap();
    assert!(verification.complete);
    assert_eq!(verification.record_count, 3);
    assert_eq!(verification.observation_rows, 1);
    assert_eq!(verification.session_count, 1);
    assert_eq!(verification.incomplete_sessions, 0);

    let events = read_events(&path);
    assert_eq!(events[0].previous_record_sha256, identity().plan_sha256);
    assert_eq!(events[1].previous_record_sha256, events[0].record_sha256);
    assert_eq!(events[2].previous_record_sha256, events[1].record_sha256);
    let FixObservationTapeRecordKind::Observation { observation } = &events[1].record else {
        panic!("middle row is not an observation");
    };
    assert_eq!(observation.raw.decode().unwrap(), raw);
    assert_eq!(observation.fix_msg_type, "8");
    assert_eq!(observation.fix_msg_seq_num, 41);
    assert_eq!(observation.client_order_id(), Some("client-order-1"));
    assert_eq!(observation.execution.as_ref().unwrap().status, "filled");
    assert!(observation.execution.as_ref().unwrap().terminal);
}

#[tokio::test]
async fn orphan_reject_is_banked_without_an_order_parent() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("fix-order-observations.jsonl");
    let (tap, mut handle) = FixObservationTapeTask::start(&path, identity(), options())
        .await
        .unwrap();
    tap.try_record(session_reject(1)).unwrap();
    handle
        .request_close(FixObservationTapeCloseReason::Requested)
        .unwrap();
    drop(tap);
    let summary = handle.wait().await.unwrap();

    assert!(summary.complete);
    assert_eq!(summary.reject_rows, 1);
    assert_eq!(summary.orphan_rows, 1);
    let events = read_events(&path);
    let FixObservationTapeRecordKind::Observation { observation } = &events[1].record else {
        panic!("middle row is not an observation");
    };
    assert_eq!(observation.client_order_id(), None);
    assert_eq!(observation.fix_msg_type, "3");
    assert_eq!(observation.reject.as_ref().unwrap().ref_seq, Some(9));
}

#[tokio::test]
async fn same_delivery_is_idempotent_but_repeated_frame_receipts_are_retained() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("fix-order-observations.jsonl");
    let (tap, mut handle) = FixObservationTapeTask::start(&path, identity(), options())
        .await
        .unwrap();
    let first = execution(1, execution_frame());
    tap.try_record(first.clone()).unwrap();
    tap.try_record(first).unwrap();
    tap.try_record(execution(2, execution_frame())).unwrap();
    handle
        .request_close(FixObservationTapeCloseReason::Requested)
        .unwrap();
    drop(tap);
    let summary = handle.wait().await.unwrap();

    assert!(summary.complete);
    assert_eq!(summary.accepted_deliveries, 3);
    assert_eq!(summary.durably_accounted_deliveries, 3);
    assert_eq!(summary.observation_rows, 2);
    assert_eq!(summary.idempotent_retries, 1);
    assert_eq!(summary.duplicate_frames, 1);
    let events = read_events(&path);
    assert_eq!(events.len(), 4);
    let FixObservationTapeRecordKind::Observation { observation: first } = &events[1].record else {
        panic!("first evidence row is not an observation");
    };
    let FixObservationTapeRecordKind::Observation {
        observation: second,
    } = &events[2].record
    else {
        panic!("second evidence row is not an observation");
    };
    assert_ne!(first.receipt_id, second.receipt_id);
    assert_eq!(first.frame_key, second.frame_key);
    assert_eq!(second.duplicate_of_tape_seq, Some(events[1].tape_seq));
}

#[tokio::test]
async fn same_receipt_with_different_projection_is_a_loud_evidence_fault() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("fix-order-observations.jsonl");
    let (tap, mut handle) = FixObservationTapeTask::start(&path, identity(), options())
        .await
        .unwrap();
    let first = execution(1, execution_frame());
    let mut conflicting = first.clone();
    let FixOrderObservation::Execution(observation) = &mut conflicting.observation else {
        unreachable!();
    };
    observation.ack_ts = MonotonicNs(observation.ack_ts.0 + 1);
    tap.try_record(first).unwrap();
    tap.try_record(conflicting).unwrap();
    handle
        .request_close(FixObservationTapeCloseReason::Requested)
        .unwrap();
    drop(tap);
    let summary = handle.wait().await.unwrap();

    assert!(!summary.complete);
    assert!(!summary.committed);
    assert_eq!(summary.accepted_deliveries, 2);
    assert_eq!(summary.observation_rows, 1);
    assert_eq!(summary.unwritten_observations, 1);
    assert!(summary
        .writer_error
        .as_deref()
        .is_some_and(|detail| detail.contains("reused with different evidence")));
    assert!(handle
        .health()
        .fault_detail()
        .is_some_and(|detail| detail.contains("reused with different evidence")));
}

#[tokio::test]
async fn clean_restart_appends_a_new_session_and_preserves_duplicate_grouping() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("fix-order-observations.jsonl");
    let (tap, mut handle) = FixObservationTapeTask::start(&path, identity(), options())
        .await
        .unwrap();
    tap.try_record(execution(1, execution_frame())).unwrap();
    handle
        .request_close(FixObservationTapeCloseReason::Requested)
        .unwrap();
    drop(tap);
    assert!(handle.wait().await.unwrap().complete);
    drop(handle);

    let (tap, mut handle) = FixObservationTapeTask::start(&path, identity(), options())
        .await
        .unwrap();
    tap.try_record(execution(1, execution_frame())).unwrap();
    handle
        .request_close(FixObservationTapeCloseReason::Requested)
        .unwrap();
    drop(tap);
    let summary = handle.wait().await.unwrap();
    assert!(summary.complete);
    assert_eq!(summary.duplicate_frames, 1);

    let verification = verify_fix_observation_tape(&path, &identity()).unwrap();
    assert!(verification.complete);
    assert_eq!(verification.session_count, 2);
    assert_eq!(verification.observation_rows, 2);
    assert_eq!(verification.record_count, 6);
}

#[tokio::test]
async fn missing_prior_commit_permanently_marks_the_run_tape_incomplete() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("fix-order-observations.jsonl");
    let (tap, mut handle) = FixObservationTapeTask::start(&path, identity(), options())
        .await
        .unwrap();
    handle
        .request_close(FixObservationTapeCloseReason::Requested)
        .unwrap();
    drop(tap);
    let first = handle.wait().await.unwrap();
    assert!(first.complete);
    std::fs::remove_file(first.commit_path.unwrap()).unwrap();
    drop(handle);

    let (tap, mut handle) = FixObservationTapeTask::start(&path, identity(), options())
        .await
        .unwrap();
    handle
        .request_close(FixObservationTapeCloseReason::Requested)
        .unwrap();
    drop(tap);
    let second = handle.wait().await.unwrap();
    assert!(!second.complete);
    assert_eq!(second.prior_incomplete_sessions, 1);

    let verification = verify_fix_observation_tape(&path, &identity()).unwrap();
    assert!(!verification.complete);
    assert_eq!(verification.incomplete_sessions, 1);
}

#[tokio::test]
async fn tamper_and_truncated_tail_are_refused() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("fix-order-observations.jsonl");
    let (tap, mut handle) = FixObservationTapeTask::start(&path, identity(), options())
        .await
        .unwrap();
    tap.try_record(execution(1, execution_frame())).unwrap();
    handle
        .request_close(FixObservationTapeCloseReason::Requested)
        .unwrap();
    drop(tap);
    assert!(handle.wait().await.unwrap().complete);
    drop(handle);

    let original = std::fs::read(&path).unwrap();
    let mut tampered = original.clone();
    let position = tampered
        .windows("client-order-1".len())
        .position(|window| window == b"client-order-1")
        .unwrap();
    tampered[position] = b'X';
    std::fs::write(&path, tampered).unwrap();
    let error = verify_fix_observation_tape(&path, &identity()).unwrap_err();
    assert!(error.to_string().contains("record SHA-256 mismatch"));

    std::fs::write(&path, &original[..original.len() - 1]).unwrap();
    let error = verify_fix_observation_tape(&path, &identity()).unwrap_err();
    assert!(error.to_string().contains("truncated final line"));
}

#[tokio::test]
async fn active_writer_holds_an_exclusive_tape_lease() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("fix-order-observations.jsonl");
    let (tap, mut handle) = FixObservationTapeTask::start(&path, identity(), options())
        .await
        .unwrap();
    let error = match FixObservationTapeTask::start(&path, identity(), options()).await {
        Ok(_) => panic!("second writer unexpectedly acquired the same tape"),
        Err(error) => error,
    };
    assert!(error.to_string().contains("I/O"));

    handle
        .request_close(FixObservationTapeCloseReason::Requested)
        .unwrap();
    drop(tap);
    assert!(handle.wait().await.unwrap().complete);
}
