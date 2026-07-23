//! FixTransport integration against an in-process mock FIX acceptor
//! (plaintext TCP — the `tls: false` test seam): logon handshake with real
//! RawData signature validation, order → ExecutionReport correlation,
//! reject mapping, ER-timeout fallback, and TestRequest echo.

use std::sync::Arc;
use std::time::Duration;

use earshot::kalshi::auth::Signer;
use earshot::kalshi::fix::encode::{logon_prehash, FixWriter, SOH};
use earshot::kalshi::fix::encode::FixOrderTemplate;
use earshot::kalshi::fix::parse::{frame_len, FixMsg};
use earshot::kalshi::fix::transport::FixTransport;
use earshot::kalshi::fix::FixConfig;
use rsa::pkcs1::EncodeRsaPrivateKey;
use rsa::pss::VerifyingKey;
use rsa::sha2::Sha256;
use rsa::signature::Verifier;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

fn test_keypair() -> (Arc<Signer>, rsa::RsaPublicKey) {
    let key = rsa::RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 2048).unwrap();
    let pem = key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF).unwrap();
    let signer = Arc::new(Signer::from_pem("SENDER-KEY".into(), &pem).unwrap());
    (signer, key.to_public_key())
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
    let frame = read_frame(&mut stream, &mut buf).await.expect("logon frame");
    let msg = FixMsg::parse(&frame).expect("parseable logon");
    assert_eq!(msg.msg_type, b"A");
    assert_eq!(msg.get_str(49), Some("SENDER-KEY"));
    assert_eq!(msg.get_str(56), Some("KalshiNR"));
    assert_eq!(msg.get_str(141), Some("Y"), "KalshiNR requires reset-on-logon");
    assert_eq!(msg.get_str(1137), Some("9"));

    // Validate the RawData signature exactly as the venue would (F3).
    let sending_time = msg.get_str(52).unwrap();
    let sig_b64 = msg.get_str(96).expect("RawData present");
    let sig_bytes = {
        use base64::Engine;
        base64::engine::general_purpose::STANDARD.decode(sig_b64).unwrap()
    };
    let verifying: VerifyingKey<Sha256> = VerifyingKey::new(pubkey);
    let sig = rsa::pss::Signature::try_from(sig_bytes.as_slice()).unwrap();
    let prehash = logon_prehash(sending_time, msg.seq, "SENDER-KEY", "KalshiNR");
    verifying.verify(&prehash, &sig).expect("logon signature must verify");

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
        let frame = read_frame(&mut stream, &mut buf).await.expect("order frame");
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
    let (er, send_ts, recv_ts) = transport.send_order(&tpl, "fix-cl-1").await.expect("fill");
    assert_eq!(er.order_id.as_deref(), Some("srv-fix-1"));
    assert_eq!(er.status, "filled");
    assert!(er.terminal);
    assert!(recv_ts.0 > send_ts.0, "ER receive must postdate the send");
    assert_eq!(er.raw["last_px"], "97");

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
        let frame = read_frame(&mut stream, &mut buf).await.expect("order frame");
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
    let transport = FixTransport::connect(c.clone(), signer, Duration::from_secs(2)).await.unwrap();
    let tpl = ioc_template(&c, "fix-cl-2");
    let (er, _, _) = transport.send_order(&tpl, "fix-cl-2").await.expect("a reject is still an ack");
    assert_eq!(er.status, "rejected");
    assert!(er.terminal);
    assert_eq!(er.raw["text"], "not authorized");

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
    let transport = FixTransport::connect(c.clone(), signer, Duration::from_millis(300)).await.unwrap();
    let tpl = ioc_template(&c, "fix-cl-3");
    let err = transport.send_order(&tpl, "fix-cl-3").await.expect_err("must time out");
    assert!(err.to_string().contains("no ExecutionReport"), "got: {err}");

    transport.shutdown().await;
    server.abort();
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
        echo_tx.send(msg.get_str(112).unwrap_or("").to_string()).unwrap();
        let _ = read_frame(&mut stream, &mut buf).await;
    });

    let c = cfg(port);
    let transport = FixTransport::connect(c, signer, Duration::from_secs(2)).await.unwrap();
    let echoed = tokio::time::timeout(Duration::from_secs(2), echo_rx)
        .await
        .expect("echo within 2 s")
        .unwrap();
    assert_eq!(echoed, "srv-ping");

    transport.shutdown().await;
    server.abort();
}

/// Sanity: SOH really is the wire delimiter our helpers assume.
#[test]
fn soh_constant() {
    assert_eq!(SOH, 0x01);
}
