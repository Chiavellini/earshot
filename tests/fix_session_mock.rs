//! FixTransport integration against an in-process mock FIX acceptor
//! (plaintext TCP — the `tls: false` test seam): logon handshake with real
//! RawData signature validation, order → ExecutionReport correlation,
//! reject mapping, ER-timeout fallback, and TestRequest echo.

use std::sync::Arc;
use std::sync::OnceLock;
use std::time::Duration;

use earshot::kalshi::auth::Signer;
use earshot::kalshi::fix::encode::{logon_prehash, FixOrderTemplate, FixWriter, SOH};
use earshot::kalshi::fix::observation_tape::{
    verify_fix_observation_tape, FixObservationTapeCloseReason, FixObservationTapeIdentity,
    FixObservationTapeOptions, FixObservationTapeTask,
};
use earshot::kalshi::fix::parse::{frame_len, FixMsg};
use earshot::kalshi::fix::transport::{
    FixOrderObservation, FixRejectKind, FixSendAttempt, FixSendResult, FixTracedSendResult,
    FixTransport,
};
use earshot::kalshi::fix::FixConfig;
use rsa::pkcs1::EncodeRsaPrivateKey;
use rsa::pss::VerifyingKey;
use rsa::sha2::Sha256;
use rsa::signature::Verifier;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

fn test_keypair() -> (Arc<Signer>, rsa::RsaPublicKey) {
    static KEYPAIR: OnceLock<(Arc<Signer>, rsa::RsaPublicKey)> = OnceLock::new();
    let (signer, public) = KEYPAIR.get_or_init(|| {
        let key = rsa::RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 2048).unwrap();
        let pem = key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF).unwrap();
        (
            Arc::new(Signer::from_pem("SENDER-KEY".into(), &pem).unwrap()),
            key.to_public_key(),
        )
    });
    (signer.clone(), public.clone())
}

fn cfg(port: u16) -> FixConfig {
    FixConfig {
        host: "127.0.0.1".into(),
        port,
        sender_comp_id: "SENDER-KEY".into(),
        target_comp_id: "KalshiNR".into(),
        heartbeat_secs: 10,
        tls: false,
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

/// Server-side message builder (CompIDs mirrored).
fn server_msg(seq: u64, build: impl FnOnce(&mut FixWriter)) -> Vec<u8> {
    let mut w = FixWriter::new();
    let _ = seq; // seq is written by `begin` inside `build`
    build(&mut w);
    w.finish()
}

/// Accept one connection, validate the Logon RawData signature against the
/// real public key, reply with a server Logon (seq 1). Returns the socket,
/// its read buffer, and the next server seq.
async fn accept_with_logon(
    listener: TcpListener,
    pubkey: rsa::RsaPublicKey,
) -> (TcpStream, Vec<u8>, u64) {
    let (mut stream, _) = listener.accept().await.unwrap();
    let mut buf = Vec::new();
    let frame = read_frame(&mut stream, &mut buf)
        .await
        .expect("logon frame");
    let msg = FixMsg::parse(&frame).expect("parseable logon");
    assert_eq!(msg.msg_type, b"A");
    assert_eq!(msg.get_str(49), Some("SENDER-KEY"));
    assert_eq!(msg.get_str(56), Some("KalshiNR"));
    assert_eq!(
        msg.get_str(141),
        Some("Y"),
        "KalshiNR requires reset-on-logon"
    );
    assert_eq!(msg.get_str(1137), Some("9"));

    // Validate the RawData signature exactly as the venue would (F3).
    let sending_time = msg.get_str(52).unwrap();
    let sig_b64 = msg.get_str(96).expect("RawData present");
    let sig_bytes = {
        use base64::Engine;
        base64::engine::general_purpose::STANDARD
            .decode(sig_b64)
            .unwrap()
    };
    let verifying: VerifyingKey<Sha256> = VerifyingKey::new(pubkey);
    let sig = rsa::pss::Signature::try_from(sig_bytes.as_slice()).unwrap();
    let prehash = logon_prehash(sending_time, msg.seq, "SENDER-KEY", "KalshiNR");
    verifying
        .verify(&prehash, &sig)
        .expect("logon signature must verify");

    let reply = server_msg(1, |w| {
        w.begin("A", "KalshiNR", "SENDER-KEY", 1, sending_time);
        w.tag_str(98, "0");
        w.tag_str(108, "10");
        w.tag_str(1137, "9");
    });
    stream.write_all(&reply).await.unwrap();
    (stream, buf, 2)
}

fn ioc_template(c: &FixConfig, cl_ord_id: &str) -> FixOrderTemplate {
    FixOrderTemplate::ioc_buy_yes(c, "KX-FIXTEST", 97, 60, cl_ord_id.to_string())
}

fn attempted(result: FixSendResult) -> FixSendAttempt {
    match result {
        FixSendResult::Attempted(attempt) => attempt,
        other => panic!("expected a wire attempt, got {other:?}"),
    }
}

fn traced_attempt(
    result: FixTracedSendResult,
) -> earshot::kalshi::fix::transport::FixTracedSendAttempt {
    match result {
        FixTracedSendResult::Attempted(attempt) => attempt,
        other => panic!("expected a traced wire attempt, got {other:?}"),
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn handshake_fires_order_and_correlates_fill() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (signer, pubkey) = test_keypair();

    let server = tokio::spawn(async move {
        let (mut stream, mut buf, mut seq) = accept_with_logon(listener, pubkey).await;
        // Expect exactly one 35=D, reply with a PendingNew then a filled ER
        // (the transport must hold the non-terminal one and resolve on the
        // terminal).
        let frame = read_frame(&mut stream, &mut buf)
            .await
            .expect("order frame");
        let msg = FixMsg::parse(&frame).unwrap();
        assert_eq!(msg.msg_type, b"D");
        assert_eq!(msg.get_str(55), Some("KX-FIXTEST"));
        assert_eq!(msg.get_str(44), Some("97"), "price is integer cents");
        assert_eq!(msg.get_str(59), Some("3"), "IOC");
        let cl = msg.get_str(11).unwrap().to_string();
        let pending = server_msg(seq, |w| {
            w.begin("8", "KalshiNR", "SENDER-KEY", seq, "20260721-12:00:00.050");
            w.tag_str(11, &cl);
            w.tag_str(37, "srv-fix-1");
            w.tag_str(150, "A");
            w.tag_str(39, "A");
        });
        seq += 1;
        let filled = server_msg(seq, |w| {
            w.begin("8", "KalshiNR", "SENDER-KEY", seq, "20260721-12:00:00.060");
            w.tag_str(11, &cl);
            w.tag_str(37, "srv-fix-1");
            w.tag_str(150, "F");
            w.tag_str(39, "2");
            w.tag_str(14, "60");
            w.tag_str(151, "0");
            w.tag_str(31, "97");
            w.tag_str(32, "60");
        });
        stream.write_all(&pending).await.unwrap();
        stream.write_all(&filled).await.unwrap();
        // Hold the socket open until the client is done.
        let _ = read_frame(&mut stream, &mut buf).await;
    });

    let c = cfg(port);
    let transport = FixTransport::connect(c.clone(), signer, Duration::from_secs(2))
        .await
        .expect("handshake");
    assert!(transport.is_connected().await);

    let tpl = ioc_template(&c, "fix-cl-1");
    let attempt = attempted(transport.submit_order(&tpl, "fix-cl-1").await);
    let send_ts = attempt.send_ts;
    let observation = attempt.outcome.expect("terminal fill");
    let er = observation.report;
    assert_eq!(er.order_id.as_deref(), Some("srv-fix-1"));
    assert_eq!(er.status, "filled");
    assert!(er.terminal);
    assert!(
        observation.recv_ts.0 > send_ts.0,
        "ER receive must postdate the send"
    );
    assert!(observation.ack_ts >= observation.recv_ts);
    assert_eq!(er.raw["last_px"], "97");

    transport.shutdown().await;
    server.abort();
}

#[tokio::test(flavor = "multi_thread")]
async fn ordered_stream_exposes_order_id_before_terminal_and_trace_keeps_every_report() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (signer, pubkey) = test_keypair();
    let (expected_raw_tx, expected_raw_rx) = tokio::sync::oneshot::channel();

    let server = tokio::spawn(async move {
        let (mut stream, mut buf, mut seq) = accept_with_logon(listener, pubkey).await;
        let frame = read_frame(&mut stream, &mut buf)
            .await
            .expect("order frame");
        let msg = FixMsg::parse(&frame).unwrap();
        let cl = msg.get_str(11).unwrap().to_string();

        let new = server_msg(seq, |w| {
            w.begin("8", "KalshiNR", "SENDER-KEY", seq, "20260721-12:00:00.040");
            w.tag_str(11, &cl);
            w.tag_str(37, "srv-ordered-1");
            w.tag_str(150, "0");
            w.tag_str(39, "0");
            w.tag_str(14, "0");
            w.tag_str(151, "60");
        });
        seq += 1;
        let partial_one = server_msg(seq, |w| {
            w.begin("8", "KalshiNR", "SENDER-KEY", seq, "20260721-12:00:00.050");
            w.tag_str(11, &cl);
            w.tag_str(37, "srv-ordered-1");
            w.tag_str(150, "F");
            w.tag_str(39, "1");
            w.tag_str(14, "10");
            w.tag_str(151, "50");
            w.tag_str(31, "96");
            w.tag_str(32, "10");
        });
        seq += 1;
        let partial_two = server_msg(seq, |w| {
            w.begin("8", "KalshiNR", "SENDER-KEY", seq, "20260721-12:00:00.060");
            w.tag_str(11, &cl);
            w.tag_str(37, "srv-ordered-1");
            w.tag_str(150, "F");
            w.tag_str(39, "1");
            w.tag_str(14, "25");
            w.tag_str(151, "35");
            w.tag_str(31, "97");
            w.tag_str(32, "15");
        });
        let expected_prefix = [new.clone(), partial_one.clone(), partial_two.clone()];
        stream.write_all(&new).await.unwrap();
        stream.write_all(&partial_one).await.unwrap();
        stream.write_all(&partial_two).await.unwrap();

        // Leave a deterministic window in which the first order id is
        // observable while the submission is still awaiting terminal state.
        tokio::time::sleep(Duration::from_millis(75)).await;
        seq += 1;
        let filled = server_msg(seq, |w| {
            w.begin("8", "KalshiNR", "SENDER-KEY", seq, "20260721-12:00:00.070");
            w.tag_str(11, &cl);
            w.tag_str(37, "srv-ordered-1");
            w.tag_str(150, "F");
            w.tag_str(39, "2");
            w.tag_str(14, "60");
            w.tag_str(151, "0");
            w.tag_str(31, "97");
            w.tag_str(32, "35");
        });
        expected_raw_tx
            .send([
                expected_prefix[0].clone(),
                expected_prefix[1].clone(),
                expected_prefix[2].clone(),
                filled.clone(),
            ])
            .unwrap();
        stream.write_all(&filled).await.unwrap();
        let _ = read_frame(&mut stream, &mut buf).await;
    });

    let c = cfg(port);
    let transport = FixTransport::connect(c.clone(), signer, Duration::from_secs(2))
        .await
        .unwrap();
    let mut stream = transport.subscribe_observations();
    let submit_transport = transport.clone();
    let template = ioc_template(&c, "fix-cl-ordered");
    let submit = tokio::spawn(async move {
        submit_transport
            .submit_order_traced(&template, "fix-cl-ordered")
            .await
    });

    let first = tokio::time::timeout(Duration::from_secs(1), stream.recv())
        .await
        .expect("first observation promptly")
        .expect("observation stream open");
    let FixOrderObservation::Execution(first_er) = first.observation else {
        panic!("first observation must be an ExecutionReport")
    };
    assert_eq!(first.sequence, 1);
    assert_eq!(first_er.report.status, "new");
    assert_eq!(first_er.report.order_id.as_deref(), Some("srv-ordered-1"));
    let first_wire = FixMsg::parse(&first_er.raw).expect("stream preserves exact ER frame");
    assert_eq!(first_wire.msg_type, b"8");
    assert_eq!(first_wire.seq, 2);
    assert_eq!(first_wire.get_str(11), Some("fix-cl-ordered"));
    assert_eq!(first_wire.get_str(39), Some("0"));
    assert!(
        !submit.is_finished(),
        "order id must be visible before terminal wait resolves"
    );

    let attempt = traced_attempt(submit.await.unwrap());
    let statuses: Vec<_> = attempt
        .observations
        .iter()
        .map(|observation| match observation {
            FixOrderObservation::Execution(value) => value.report.status.as_str(),
            FixOrderObservation::Reject(_) => "reject",
        })
        .collect();
    assert_eq!(statuses, ["new", "partial", "partial", "filled"]);
    let cumulative: Vec<_> = attempt
        .observations
        .iter()
        .filter_map(|observation| match observation {
            FixOrderObservation::Execution(value) => value.report.raw["cum_qty"].as_str(),
            FixOrderObservation::Reject(_) => None,
        })
        .collect();
    assert_eq!(cumulative, ["0", "10", "25", "60"]);
    let traced_raw: Vec<Vec<u8>> = attempt
        .observations
        .iter()
        .map(|observation| match observation {
            FixOrderObservation::Execution(value) => value.raw.clone(),
            FixOrderObservation::Reject(_) => panic!("ordered trace must contain only ERs"),
        })
        .collect();
    let expected_raw = expected_raw_rx.await.expect("server raw-frame fixtures");
    assert_eq!(traced_raw.as_slice(), expected_raw.as_slice());
    assert_eq!(
        first_er.raw, traced_raw[0],
        "stream and trace must carry identical bytes"
    );
    for (index, raw) in traced_raw.iter().enumerate() {
        let frame = FixMsg::parse(raw).expect("traced ER retains a complete valid frame");
        assert_eq!(frame.msg_type, b"8");
        assert_eq!(frame.seq, index as u64 + 2);
        assert_eq!(frame.get_str(11), Some("fix-cl-ordered"));
    }
    let terminal = attempt.outcome.expect("filled terminal report");
    assert_eq!(terminal.report.order_id.as_deref(), Some("srv-ordered-1"));
    assert_eq!(terminal.raw, traced_raw[3]);
    assert_eq!(attempt.first_response_recv_ts, Some(first_er.recv_ts));
    assert_eq!(attempt.first_response_ack_ts, Some(first_er.ack_ts));
    assert!(terminal.recv_ts > first_er.recv_ts);

    let mut streamed_statuses = vec!["new".to_string()];
    for expected_sequence in 2..=4 {
        let envelope = tokio::time::timeout(Duration::from_secs(1), stream.recv())
            .await
            .expect("remaining observation promptly")
            .expect("observation stream open");
        assert_eq!(envelope.sequence, expected_sequence);
        let FixOrderObservation::Execution(value) = envelope.observation else {
            panic!("ordered stream must contain only this order's ERs")
        };
        assert_eq!(
            value.raw,
            traced_raw[(expected_sequence - 1) as usize],
            "stream and trace must preserve the same exact frame"
        );
        streamed_statuses.push(value.report.status);
    }
    assert_eq!(streamed_statuses, ["new", "partial", "partial", "filled"]);

    transport.shutdown().await;
    server.abort();
}

#[tokio::test(flavor = "multi_thread")]
async fn rejected_order_maps_to_rejected_status() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (signer, pubkey) = test_keypair();

    let server = tokio::spawn(async move {
        let (mut stream, mut buf, seq) = accept_with_logon(listener, pubkey).await;
        let frame = read_frame(&mut stream, &mut buf)
            .await
            .expect("order frame");
        let msg = FixMsg::parse(&frame).unwrap();
        let cl = msg.get_str(11).unwrap().to_string();
        let rejected = server_msg(seq, |w| {
            w.begin("8", "KalshiNR", "SENDER-KEY", seq, "20260721-12:00:00.050");
            w.tag_str(11, &cl);
            w.tag_str(150, "8");
            w.tag_str(39, "8");
            w.tag_str(103, "6");
            w.tag_str(58, "not authorized");
        });
        stream.write_all(&rejected).await.unwrap();
        let _ = read_frame(&mut stream, &mut buf).await;
    });

    let c = cfg(port);
    let transport = FixTransport::connect(c.clone(), signer, Duration::from_secs(2))
        .await
        .unwrap();
    let tpl = ioc_template(&c, "fix-cl-2");
    // Exercise the terminal-only compatibility wrapper as well as rejection.
    let (er, _, _) = transport
        .send_order(&tpl, "fix-cl-2")
        .await
        .expect("a reject is still a terminal observation");
    assert_eq!(er.status, "rejected");
    assert!(er.terminal);
    assert_eq!(er.raw["text"], "not authorized");

    transport.shutdown().await;
    server.abort();
}

#[tokio::test(flavor = "multi_thread")]
async fn session_and_business_rejects_are_typed_and_exactly_correlated() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (signer, pubkey) = test_keypair();

    let server = tokio::spawn(async move {
        let (mut stream, mut buf, mut server_seq) = accept_with_logon(listener, pubkey).await;

        let first = read_frame(&mut stream, &mut buf)
            .await
            .expect("first order");
        let first = FixMsg::parse(&first).unwrap();
        assert_eq!(first.seq, 2);
        let session_reject = server_msg(server_seq, |w| {
            w.begin(
                "3",
                "KalshiNR",
                "SENDER-KEY",
                server_seq,
                "20260721-12:00:00.050",
            );
            w.tag_str(45, "2");
            w.tag_str(372, "D");
            w.tag_str(373, "5");
            w.tag_str(58, "bad application sequence");
        });
        stream.write_all(&session_reject).await.unwrap();
        server_seq += 1;

        let second = read_frame(&mut stream, &mut buf)
            .await
            .expect("second order");
        let second = FixMsg::parse(&second).unwrap();
        assert_eq!(second.seq, 3);
        let second_cl = second.get_str(11).unwrap().to_string();
        let business_reject = server_msg(server_seq, |w| {
            w.begin(
                "j",
                "KalshiNR",
                "SENDER-KEY",
                server_seq,
                "20260721-12:00:00.060",
            );
            w.tag_str(372, "D");
            w.tag_str(379, &second_cl);
            w.tag_str(380, "8");
            w.tag_str(58, "rate limited");
        });
        stream.write_all(&business_reject).await.unwrap();
        let _ = read_frame(&mut stream, &mut buf).await;
    });

    let c = cfg(port);
    let transport = FixTransport::connect(c.clone(), signer, Duration::from_secs(2))
        .await
        .unwrap();
    let mut observations = transport.subscribe_observations();

    let first = traced_attempt(
        transport
            .submit_order_traced(
                &ioc_template(&c, "fix-cl-session-reject"),
                "fix-cl-session-reject",
            )
            .await,
    );
    let first_failure = first
        .outcome
        .expect_err("session reject is a typed failure");
    let first_reject = first_failure
        .protocol_reject
        .expect("session reject evidence");
    assert_eq!(first_reject.kind, FixRejectKind::Session);
    assert_eq!(first_reject.ref_seq, Some(2));
    assert_eq!(first_reject.ref_msg_type.as_deref(), Some("D"));
    assert_eq!(
        first_reject.client_order_id.as_deref(),
        Some("fix-cl-session-reject")
    );
    assert_eq!(first_reject.reason.as_deref(), Some("5"));

    let second = traced_attempt(
        transport
            .submit_order_traced(
                &ioc_template(&c, "fix-cl-business-reject"),
                "fix-cl-business-reject",
            )
            .await,
    );
    let second_failure = second
        .outcome
        .expect_err("business reject is a typed failure");
    let second_reject = second_failure
        .protocol_reject
        .expect("business reject evidence");
    assert_eq!(second_reject.kind, FixRejectKind::Business);
    assert_eq!(second_reject.ref_msg_type.as_deref(), Some("D"));
    assert_eq!(
        second_reject.business_ref_id.as_deref(),
        Some("fix-cl-business-reject")
    );
    assert_eq!(
        second_reject.client_order_id.as_deref(),
        Some("fix-cl-business-reject")
    );
    assert_eq!(second_reject.reason.as_deref(), Some("8"));

    let streamed_first = observations.recv().await.unwrap();
    let streamed_second = observations.recv().await.unwrap();
    assert_eq!((streamed_first.sequence, streamed_second.sequence), (1, 2));
    assert!(matches!(
        streamed_first.observation,
        FixOrderObservation::Reject(ref value) if value.kind == FixRejectKind::Session
    ));
    assert!(matches!(
        streamed_second.observation,
        FixOrderObservation::Reject(ref value) if value.kind == FixRejectKind::Business
    ));

    transport.shutdown().await;
    server.abort();
}

#[tokio::test(flavor = "multi_thread")]
async fn swallowed_er_times_out_for_ambiguous_path() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (signer, pubkey) = test_keypair();

    let server = tokio::spawn(async move {
        let (mut stream, mut buf, _seq) = accept_with_logon(listener, pubkey).await;
        // Swallow the order: read it, answer nothing.
        let _ = read_frame(&mut stream, &mut buf).await;
        let _ = read_frame(&mut stream, &mut buf).await;
    });

    let c = cfg(port);
    let transport = FixTransport::connect(c.clone(), signer, Duration::from_millis(300))
        .await
        .unwrap();
    let tpl = ioc_template(&c, "fix-cl-3");
    let attempt = attempted(transport.submit_order(&tpl, "fix-cl-3").await);
    assert!(attempt.send_ts.0 > 0, "wire attempt must retain send_ts");
    let failure = attempt.outcome.expect_err("must time out");
    assert!(
        failure
            .error
            .to_string()
            .contains("no terminal ExecutionReport"),
        "got: {}",
        failure.error
    );
    assert!(failure.last_observation.is_none());

    transport.shutdown().await;
    server.abort();
}

#[tokio::test(flavor = "multi_thread")]
async fn partial_then_timeout_retains_latest_exchange_observation() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (signer, pubkey) = test_keypair();

    let server = tokio::spawn(async move {
        let (mut stream, mut buf, mut seq) = accept_with_logon(listener, pubkey).await;
        let frame = read_frame(&mut stream, &mut buf)
            .await
            .expect("order frame");
        let msg = FixMsg::parse(&frame).unwrap();
        let cl = msg.get_str(11).unwrap().to_string();
        let pending = server_msg(seq, |w| {
            w.begin("8", "KalshiNR", "SENDER-KEY", seq, "20260721-12:00:00.040");
            w.tag_str(11, &cl);
            w.tag_str(37, "srv-fix-partial-timeout");
            w.tag_str(150, "A");
            w.tag_str(39, "A");
        });
        seq += 1;
        let partial = server_msg(seq, |w| {
            w.begin("8", "KalshiNR", "SENDER-KEY", seq, "20260721-12:00:00.050");
            w.tag_str(11, &cl);
            w.tag_str(37, "srv-fix-partial-timeout");
            w.tag_str(150, "F");
            w.tag_str(39, "1");
            w.tag_str(14, "7");
            w.tag_str(151, "53");
            w.tag_str(31, "96");
            w.tag_str(32, "7");
            w.tag_str(6, "96");
        });
        stream.write_all(&pending).await.unwrap();
        stream.write_all(&partial).await.unwrap();
        // Keep the connection alive but never send a terminal report.
        let _ = read_frame(&mut stream, &mut buf).await;
    });

    let c = cfg(port);
    let transport = FixTransport::connect(c.clone(), signer, Duration::from_millis(300))
        .await
        .unwrap();
    let tpl = ioc_template(&c, "fix-cl-partial-timeout");
    let attempt = attempted(transport.submit_order(&tpl, "fix-cl-partial-timeout").await);
    let failure = attempt.outcome.expect_err("terminal report must time out");
    let last = failure
        .last_observation
        .expect("partial report must survive timeout");
    assert_eq!(last.report.status, "partial");
    assert!(!last.report.terminal);
    assert_eq!(
        last.report.order_id.as_deref(),
        Some("srv-fix-partial-timeout")
    );
    assert_eq!(last.report.raw["cum_qty"], "7");
    assert_eq!(last.report.raw["leaves_qty"], "53");
    assert_eq!(last.report.raw["last_px"], "96");
    assert_eq!(last.report.raw["last_qty"], "7");
    assert_eq!(last.report.raw["avg_px"], "96");
    assert!(last.recv_ts > attempt.send_ts);
    assert!(last.ack_ts >= last.recv_ts);

    transport.shutdown().await;
    server.abort();
}

#[tokio::test(flavor = "multi_thread")]
async fn partial_then_disconnect_retains_latest_exchange_observation() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (signer, pubkey) = test_keypair();

    let server = tokio::spawn(async move {
        let (mut stream, mut buf, seq) = accept_with_logon(listener, pubkey).await;
        let frame = read_frame(&mut stream, &mut buf)
            .await
            .expect("order frame");
        let msg = FixMsg::parse(&frame).unwrap();
        let cl = msg.get_str(11).unwrap().to_string();
        let partial = server_msg(seq, |w| {
            w.begin("8", "KalshiNR", "SENDER-KEY", seq, "20260721-12:00:00.050");
            w.tag_str(11, &cl);
            w.tag_str(37, "srv-fix-partial-disconnect");
            w.tag_str(150, "F");
            w.tag_str(39, "1");
            w.tag_str(14, "9");
            w.tag_str(151, "51");
            w.tag_str(31, "95");
            w.tag_str(32, "9");
        });
        stream.write_all(&partial).await.unwrap();
        stream.shutdown().await.unwrap();
    });

    let c = cfg(port);
    let transport = FixTransport::connect(c.clone(), signer, Duration::from_secs(2))
        .await
        .unwrap();
    let tpl = ioc_template(&c, "fix-cl-partial-disconnect");
    let attempt = attempted(
        transport
            .submit_order(&tpl, "fix-cl-partial-disconnect")
            .await,
    );
    let failure = attempt
        .outcome
        .expect_err("disconnect must fail terminal wait");
    assert!(
        failure.error.to_string().contains("connection lost"),
        "got: {}",
        failure.error
    );
    let last = failure
        .last_observation
        .expect("partial report must survive disconnect");
    assert_eq!(last.report.status, "partial");
    assert_eq!(
        last.report.order_id.as_deref(),
        Some("srv-fix-partial-disconnect")
    );
    assert_eq!(last.report.raw["cum_qty"], "9");
    assert_eq!(last.report.raw["leaves_qty"], "51");
    assert!(last.recv_ts > attempt.send_ts);

    transport.shutdown().await;
    server.await.unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn disconnected_before_send_is_not_attempted() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (signer, pubkey) = test_keypair();

    let server = tokio::spawn(async move {
        let (mut stream, mut buf, _seq) = accept_with_logon(listener, pubkey).await;
        // Graceful client shutdown emits Logout; no application order follows.
        let frame = read_frame(&mut stream, &mut buf)
            .await
            .expect("logout frame");
        let msg = FixMsg::parse(&frame).unwrap();
        assert_eq!(msg.msg_type, b"5");
    });

    let c = cfg(port);
    let transport = FixTransport::connect(c.clone(), signer, Duration::from_secs(2))
        .await
        .unwrap();
    transport.shutdown().await;

    let tpl = ioc_template(&c, "fix-cl-not-attempted");
    match transport.submit_order(&tpl, "fix-cl-not-attempted").await {
        FixSendResult::NotAttempted { error } => {
            assert!(error.to_string().contains("disconnected"), "got: {error}");
        }
        other => panic!("expected NotAttempted, got {other:?}"),
    }

    server.await.unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn server_test_request_gets_heartbeat_echo() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (signer, pubkey) = test_keypair();
    let (echo_tx, echo_rx) = tokio::sync::oneshot::channel::<String>();

    let server = tokio::spawn(async move {
        let (mut stream, mut buf, seq) = accept_with_logon(listener, pubkey).await;
        let tr = server_msg(seq, |w| {
            w.begin("1", "KalshiNR", "SENDER-KEY", seq, "20260721-12:00:00.050");
            w.tag_str(112, "srv-ping");
        });
        stream.write_all(&tr).await.unwrap();
        // The next client frame must be the heartbeat echo.
        let frame = read_frame(&mut stream, &mut buf).await.expect("echo frame");
        let msg = FixMsg::parse(&frame).unwrap();
        assert_eq!(msg.msg_type, b"0");
        echo_tx
            .send(msg.get_str(112).unwrap_or("").to_string())
            .unwrap();
        let _ = read_frame(&mut stream, &mut buf).await;
    });

    let c = cfg(port);
    let transport = FixTransport::connect(c, signer, Duration::from_secs(2))
        .await
        .unwrap();
    let echoed = tokio::time::timeout(Duration::from_secs(2), echo_rx)
        .await
        .expect("echo within 2 s")
        .unwrap();
    assert_eq!(echoed, "srv-ping");

    transport.shutdown().await;
    server.abort();
}

#[tokio::test(flavor = "multi_thread")]
async fn mandatory_banker_preserves_orphan_er_coalesced_with_logon() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (signer, _pubkey) = test_keypair();

    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut buf = Vec::new();
        let request = read_frame(&mut stream, &mut buf)
            .await
            .expect("logon frame");
        let request = FixMsg::parse(&request).unwrap();
        assert_eq!(request.msg_type, b"A");
        let sending_time = request.get_str(52).unwrap();

        let logon = server_msg(1, |w| {
            w.begin("A", "KalshiNR", "SENDER-KEY", 1, sending_time);
            w.tag_str(98, "0");
            w.tag_str(108, "10");
            w.tag_str(1137, "9");
        });
        let orphan = server_msg(2, |w| {
            w.begin("8", "KalshiNR", "SENDER-KEY", 2, "20260812-17:00:00.001");
            w.tag_str(37, "orphan-during-logon");
            w.tag_str(150, "F");
            w.tag_str(39, "2");
            w.tag_str(14, "1");
            w.tag_str(151, "0");
            w.tag_str(31, "97");
            w.tag_str(32, "1");
        });
        let mut coalesced = logon;
        coalesced.extend_from_slice(&orphan);
        stream.write_all(&coalesced).await.unwrap();
        let _ = read_frame(&mut stream, &mut buf).await;
    });

    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("fix-order-observations.jsonl");
    let identity = FixObservationTapeIdentity {
        run_id: "20260812T170000Z-handshake".into(),
        event_id: "FIX-HANDSHAKE".into(),
        kalshi_env: "demo".into(),
        account_fingerprint: format!("sha256:{}", "b".repeat(64)),
        plan_sha256: "a".repeat(64),
        endpoint_id: format!("fix://127.0.0.1:{port}/KalshiNR"),
    };
    let (tap, mut tape) = FixObservationTapeTask::start(
        &path,
        identity.clone(),
        FixObservationTapeOptions {
            queue_capacity: 16,
            producer_quiescence_timeout: Duration::from_secs(2),
        },
    )
    .await
    .unwrap();
    let transport = FixTransport::connect_with_mandatory_observer(
        cfg(port),
        signer,
        Duration::from_secs(2),
        None,
        tap,
    )
    .await
    .expect("Logon and preinstalled banker");

    tape.request_close(FixObservationTapeCloseReason::Requested)
        .unwrap();
    transport.shutdown().await;
    drop(transport);
    let summary = tokio::time::timeout(Duration::from_secs(3), tape.wait())
        .await
        .expect("mandatory producer quiesces")
        .unwrap();
    assert!(summary.complete);
    assert_eq!(summary.observation_rows, 1);
    assert_eq!(summary.execution_rows, 1);
    assert_eq!(summary.orphan_rows, 1);
    assert_eq!(summary.accepted_deliveries, 1);
    assert!(
        verify_fix_observation_tape(&path, &identity)
            .unwrap()
            .complete
    );

    server.abort();
}

/// Sanity: SOH really is the wire delimiter our helpers assume.
#[test]
fn soh_constant() {
    assert_eq!(SOH, 0x01);
}
