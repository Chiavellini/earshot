//! Sans-io FIX session state machine: pure state + timers, no sockets.
//!
//! Inputs are `connect` / `on_message` / `on_tick` / `send_app` with the
//! caller's clock (unix ms — SendingTime must be wall time, F5); outputs are
//! `Action`s the transport executes. Fully offline-testable.
//!
//! Posture: KalshiNR (F2/F4) — 141=Y reset-on-logon, we never retransmit
//! (ResendRequest is answered with SequenceReset-GapFill), and an inbound
//! sequence gap is fatal (retransmission recovery exists only on KalshiRT,
//! F9) — the transport reconnects fresh instead.

use std::sync::Arc;

use super::encode::{
    heartbeat, logon, logout, sequence_reset_gapfill, test_request, utc_sending_time,
};
use super::parse::{classify, FixMsg, Inbound};
use super::FixConfig;
use crate::kalshi::auth::Signer;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionState {
    Disconnected,
    LogonSent,
    Established,
    LogoutSent,
}

/// An ExecutionReport lifted out of the borrowed parse for delivery to the
/// transport's ClOrdID correlation map.
#[derive(Debug, Clone)]
pub struct OwnedExecReport {
    pub cl_ord_id: Option<String>,
    pub order_id: Option<String>,
    /// Normalized to the REST ack vocabulary (filled/partial/canceled/…).
    pub status: String,
    pub terminal: bool,
    pub raw: serde_json::Value,
}

#[derive(Debug)]
pub enum Action {
    /// Write these bytes to the socket (already fully encoded + sequenced).
    Send(Vec<u8>),
    /// Hand an ExecutionReport to the order-correlation layer.
    Deliver(OwnedExecReport),
    /// Tear the connection down; the transport decides whether to reconnect.
    Disconnect { reason: String },
}

/// Grace multipliers (standard FIX practice; exact server behavior is a
/// Q-FIX verify-at-live item): probe with a TestRequest after 1.2× the
/// heartbeat interval of silence, give up at 2×.
const PROBE_NUM: u64 = 12;
const PROBE_DEN: u64 = 10;
/// Logon must be answered within this window.
const LOGON_TIMEOUT_MS: u64 = 10_000;

pub struct SessionMachine {
    cfg: FixConfig,
    signer: Arc<Signer>,
    state: SessionState,
    next_out: u64,
    next_in: u64,
    last_sent_ms: u64,
    last_recv_ms: u64,
    pending_test_req: Option<String>,
    test_req_counter: u64,
}

impl SessionMachine {
    pub fn new(cfg: FixConfig, signer: Arc<Signer>, seqs: (u64, u64)) -> Self {
        SessionMachine {
            cfg,
            signer,
            state: SessionState::Disconnected,
            next_out: seqs.0,
            next_in: seqs.1,
            last_sent_ms: 0,
            last_recv_ms: 0,
            pending_test_req: None,
            test_req_counter: 0,
        }
    }

    pub fn state(&self) -> SessionState {
        self.state
    }

    pub fn is_established(&self) -> bool {
        self.state == SessionState::Established
    }

    pub fn seqs(&self) -> (u64, u64) {
        (self.next_out, self.next_in)
    }

    fn alloc_seq(&mut self, now_ms: u64) -> u64 {
        let s = self.next_out;
        self.next_out += 1;
        self.last_sent_ms = now_ms;
        s
    }

    /// Begin the session: emit the Logon. NR posture always resets (141=Y).
    pub fn connect(&mut self, now_ms: u64) -> Vec<Action> {
        self.state = SessionState::LogonSent;
        self.next_out = 1;
        self.next_in = 1;
        self.last_recv_ms = now_ms;
        self.pending_test_req = None;
        let seq = self.alloc_seq(now_ms);
        let ts = utc_sending_time(now_ms);
        vec![Action::Send(logon(&self.cfg, &self.signer, seq, &ts, true))]
    }

    /// Request a graceful logout.
    pub fn start_logout(&mut self, now_ms: u64) -> Vec<Action> {
        let seq = self.alloc_seq(now_ms);
        let ts = utc_sending_time(now_ms);
        self.state = SessionState::LogoutSent;
        vec![Action::Send(logout(&self.cfg, seq, &ts))]
    }

    /// Allocate a sequence number + SendingTime for an application send
    /// (the 35=D template). `None` unless Established. The caller MUST put
    /// the message on the wire before releasing its lock on this machine —
    /// sequence order equals wire order.
    pub fn send_app(&mut self, now_ms: u64) -> Option<(u64, String)> {
        if self.state != SessionState::Established {
            return None;
        }
        let seq = self.alloc_seq(now_ms);
        Some((seq, utc_sending_time(now_ms)))
    }

    fn disconnect(&mut self, reason: impl Into<String>) -> Vec<Action> {
        self.state = SessionState::Disconnected;
        vec![Action::Disconnect { reason: reason.into() }]
    }

    /// One complete inbound message (as framed by `parse::frame_len`).
    pub fn on_message(&mut self, raw: &[u8], now_ms: u64) -> Vec<Action> {
        let msg = match FixMsg::parse(raw) {
            Ok(m) => m,
            // A TLS stream doesn't corrupt bytes; a framing/checksum error
            // means we've lost sync with the server — tear down.
            Err(e) => return self.disconnect(format!("unparseable inbound: {e}")),
        };
        self.last_recv_ms = now_ms;

        // CompID tolerance (Q-FIX open question #1): log, never die on it.
        if let Some(sender) = msg.get_str(49) {
            if sender != self.cfg.target_comp_id && sender != "Kalshi" {
                tracing::warn!(inbound_sender = sender, "unexpected server CompID (tolerated)");
            }
        }

        let inbound = classify(&msg);

        // Sequence discipline (F9). SequenceReset is the one message allowed
        // to repair the counter; everything else must arrive exactly in order.
        if let Inbound::SequenceReset { new_seq, gap_fill } = inbound {
            if new_seq < self.next_in {
                return self.disconnect(format!(
                    "SequenceReset moves backwards ({new_seq} < {})",
                    self.next_in
                ));
            }
            tracing::info!(new_seq, gap_fill, "inbound SequenceReset");
            self.next_in = new_seq;
            return Vec::new();
        }
        if msg.seq < self.next_in {
            // Fatal per F9 (dup delivery after our counter advanced).
            return self.disconnect(format!("inbound seq too low ({} < {})", msg.seq, self.next_in));
        }
        if msg.seq > self.next_in {
            // A gap is unrecoverable on KalshiNR — reconnect fresh.
            return self.disconnect(format!("inbound seq gap ({} > {})", msg.seq, self.next_in));
        }
        self.next_in += 1;

        match inbound {
            Inbound::Logon { heartbeat_secs } => {
                if self.state == SessionState::LogonSent {
                    tracing::info!(server_hb = ?heartbeat_secs, "FIX session established");
                    self.state = SessionState::Established;
                } else {
                    tracing::warn!("unexpected Logon in state {:?}", self.state);
                }
                Vec::new()
            }
            Inbound::Heartbeat { test_req_id } => {
                if let Some(pending) = &self.pending_test_req {
                    if test_req_id == Some(pending.as_str()) || test_req_id.is_none() {
                        self.pending_test_req = None;
                    }
                }
                Vec::new()
            }
            Inbound::TestRequest { id } => {
                let seq = self.alloc_seq(now_ms);
                let ts = utc_sending_time(now_ms);
                vec![Action::Send(heartbeat(&self.cfg, seq, &ts, id))]
            }
            Inbound::ResendRequest { begin, .. } => {
                // We never retransmit (NR): gap-fill the whole requested range
                // forward to our next outbound seq.
                let ts = utc_sending_time(now_ms);
                self.last_sent_ms = now_ms;
                let gapfill_seq = if begin > 0 { begin } else { self.next_out };
                vec![Action::Send(sequence_reset_gapfill(&self.cfg, gapfill_seq, &ts, self.next_out))]
            }
            Inbound::Logout { text } => {
                if self.state == SessionState::LogoutSent {
                    self.disconnect("logout complete")
                } else {
                    tracing::warn!(?text, "server-initiated logout");
                    let seq = self.alloc_seq(now_ms);
                    let ts = utc_sending_time(now_ms);
                    let mut acts = vec![Action::Send(logout(&self.cfg, seq, &ts))];
                    acts.extend(self.disconnect("server logout"));
                    acts
                }
            }
            Inbound::Reject { ref_seq, reason, text } => {
                tracing::error!(?ref_seq, ?reason, ?text, "FIX session Reject (35=3)");
                Vec::new()
            }
            Inbound::BusinessReject { reason, text } => {
                // 380=8 is the rate limiter (F10) — loud, but not fatal.
                tracing::error!(?reason, ?text, "FIX BusinessMessageReject (35=j)");
                Vec::new()
            }
            Inbound::ExecReport(er) => {
                vec![Action::Deliver(OwnedExecReport {
                    cl_ord_id: er.cl_ord_id.map(String::from),
                    order_id: er.order_id.map(String::from),
                    status: er.normalized_status().to_string(),
                    terminal: er.is_terminal(),
                    raw: er.to_json(),
                })]
            }
            Inbound::CancelReject { cl_ord_id, reason, text } => {
                tracing::error!(?cl_ord_id, ?reason, ?text, "FIX OrderCancelReject (35=9)");
                Vec::new()
            }
            Inbound::SequenceReset { .. } => unreachable!("handled above"),
            Inbound::Other { msg_type } => {
                tracing::debug!(msg_type = ?String::from_utf8_lossy(msg_type), "unhandled FIX message");
                Vec::new()
            }
        }
    }

    /// Drive timers. Call ~1×/second.
    pub fn on_tick(&mut self, now_ms: u64) -> Vec<Action> {
        let hb_ms = u64::from(self.cfg.heartbeat_secs) * 1000;
        match self.state {
            SessionState::Disconnected => Vec::new(),
            SessionState::LogonSent => {
                if now_ms.saturating_sub(self.last_sent_ms) >= LOGON_TIMEOUT_MS {
                    self.disconnect("logon unanswered")
                } else {
                    Vec::new()
                }
            }
            SessionState::Established | SessionState::LogoutSent => {
                let silence = now_ms.saturating_sub(self.last_recv_ms);
                if silence >= 2 * hb_ms {
                    return self.disconnect(format!("peer silent {silence} ms (2× heartbeat)"));
                }
                let mut acts = Vec::new();
                if silence >= hb_ms * PROBE_NUM / PROBE_DEN && self.pending_test_req.is_none() {
                    self.test_req_counter += 1;
                    let id = format!("earshot-{}", self.test_req_counter);
                    let seq = self.alloc_seq(now_ms);
                    let ts = utc_sending_time(now_ms);
                    acts.push(Action::Send(test_request(&self.cfg, seq, &ts, &id)));
                    self.pending_test_req = Some(id);
                }
                if now_ms.saturating_sub(self.last_sent_ms) >= hb_ms {
                    let seq = self.alloc_seq(now_ms);
                    let ts = utc_sending_time(now_ms);
                    acts.push(Action::Send(heartbeat(&self.cfg, seq, &ts, None)));
                }
                acts
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::encode::FixWriter;
    use super::*;
    use rsa::pkcs1::EncodeRsaPrivateKey;

    fn signer() -> Arc<Signer> {
        let key = rsa::RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 2048).unwrap();
        let pem = key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF).unwrap();
        Arc::new(Signer::from_pem("SENDER-KEY".into(), &pem).unwrap())
    }

    fn cfg() -> FixConfig {
        FixConfig {
            host: "mock".into(),
            port: 0,
            sender_comp_id: "SENDER-KEY".into(),
            target_comp_id: "KalshiNR".into(),
            heartbeat_secs: 10,
            tls: false,
        }
    }

    /// Encode a message as the SERVER would (CompIDs swapped).
    fn server_msg(build: impl FnOnce(&mut FixWriter)) -> Vec<u8> {
        let mut w = FixWriter::new();
        build(&mut w);
        w.finish()
    }

    fn server_logon(seq: u64) -> Vec<u8> {
        server_msg(|w| {
            w.begin("A", "KalshiNR", "SENDER-KEY", seq, "20260721-12:00:00.000");
            w.tag_str(98, "0");
            w.tag_str(108, "10");
            w.tag_str(1137, "9");
        })
    }

    fn server_heartbeat(seq: u64) -> Vec<u8> {
        server_msg(|w| w.begin("0", "KalshiNR", "SENDER-KEY", seq, "20260721-12:00:01.000"))
    }

    fn established() -> SessionMachine {
        let mut m = SessionMachine::new(cfg(), signer(), (1, 1));
        let acts = m.connect(1_000);
        assert!(matches!(acts.as_slice(), [Action::Send(b)] if b.windows(4).any(|w| w == b"35=A")));
        assert!(m.on_message(&server_logon(1), 1_100).is_empty());
        assert!(m.is_established());
        m
    }

    #[test]
    fn logon_handshake_establishes() {
        let m = established();
        // Our logon consumed seq 1; server's logon consumed inbound 1.
        assert_eq!(m.seqs(), (2, 2));
    }

    #[test]
    fn heartbeat_fires_when_due_and_testrequest_probes_silence() {
        let mut m = established();
        // Last heard from the server at t=1.1 s (its logon). At t=13.2 s the
        // silence is 12.1 s ≥ 1.2× the 10 s heartbeat → TestRequest probe
        // (which itself resets the outbound timer, so no heartbeat with it).
        let acts = m.on_tick(13_200);
        let sent: Vec<String> = acts
            .iter()
            .filter_map(|a| match a {
                Action::Send(b) => Some(String::from_utf8_lossy(b).replace('\u{1}', "|")),
                _ => None,
            })
            .collect();
        assert!(sent.iter().any(|s| s.contains("|35=1|")), "TestRequest expected: {sent:?}");
        // A server heartbeat clears the probe.
        assert!(m.on_message(&server_heartbeat(2), 13_500).is_empty());
        assert!(m.pending_test_req.is_none());
        // t=24 s: 10.8 s since our TestRequest send (≥ 10 s interval), only
        // 10.5 s of inbound silence (< 12 s) → a plain heartbeat, no probe.
        let acts = m.on_tick(24_000);
        let sent: Vec<String> = acts
            .iter()
            .filter_map(|a| match a {
                Action::Send(b) => Some(String::from_utf8_lossy(b).replace('\u{1}', "|")),
                _ => None,
            })
            .collect();
        assert_eq!(sent.len(), 1, "exactly one send: {sent:?}");
        assert!(sent[0].contains("|35=0|"), "Heartbeat expected: {sent:?}");
    }

    #[test]
    fn silent_peer_disconnects_at_two_heartbeats() {
        let mut m = established();
        let _ = m.on_tick(12_100); // probes
        let acts = m.on_tick(21_200); // 20.1 s of silence ≥ 2×10 s
        assert!(matches!(acts.as_slice(), [Action::Disconnect { .. }]));
        assert_eq!(m.state(), SessionState::Disconnected);
    }

    #[test]
    fn test_request_is_echoed_with_id() {
        let mut m = established();
        let tr = server_msg(|w| {
            w.begin("1", "KalshiNR", "SENDER-KEY", 2, "20260721-12:00:01.000");
            w.tag_str(112, "srv-ping");
        });
        let acts = m.on_message(&tr, 2_000);
        let [Action::Send(b)] = acts.as_slice() else { panic!("expected echo, got {acts:?}") };
        let s = String::from_utf8_lossy(b);
        assert!(s.contains("35=0\u{1}"), "heartbeat echo");
        assert!(s.contains("112=srv-ping\u{1}"), "TestReqID echoed");
    }

    #[test]
    fn resend_request_answered_with_gapfill_never_retransmit() {
        let mut m = established();
        let rr = server_msg(|w| {
            w.begin("2", "KalshiNR", "SENDER-KEY", 2, "20260721-12:00:01.000");
            w.tag_str(7, "1");
            w.tag_str(16, "0");
        });
        let acts = m.on_message(&rr, 2_000);
        let [Action::Send(b)] = acts.as_slice() else { panic!("expected gapfill") };
        let s = String::from_utf8_lossy(b).replace('\u{1}', "|");
        assert!(s.contains("|35=4|"), "SequenceReset: {s}");
        assert!(s.contains("|123=Y|"), "GapFill flag: {s}");
        assert!(s.contains("|36=2|"), "NewSeqNo = next_out: {s}");
    }

    #[test]
    fn seq_too_low_and_seq_gap_are_fatal() {
        let mut m = established();
        // Replay of seq 1 (< expected 2): fatal.
        let acts = m.on_message(&server_heartbeat(1), 2_000);
        assert!(matches!(acts.as_slice(), [Action::Disconnect { reason }] if reason.contains("too low")));

        let mut m = established();
        // Gap (4 > expected 2): fatal on NR.
        let acts = m.on_message(&server_heartbeat(4), 2_000);
        assert!(matches!(acts.as_slice(), [Action::Disconnect { reason }] if reason.contains("gap")));
    }

    #[test]
    fn exec_report_is_delivered_owned() {
        let mut m = established();
        let er = server_msg(|w| {
            w.begin("8", "KalshiNR", "SENDER-KEY", 2, "20260721-12:00:01.000");
            w.tag_str(11, "cl-1");
            w.tag_str(37, "srv-1");
            w.tag_str(150, "F");
            w.tag_str(39, "2");
            w.tag_str(14, "1");
            w.tag_str(151, "0");
        });
        let acts = m.on_message(&er, 2_000);
        let [Action::Deliver(rep)] = acts.as_slice() else { panic!("expected Deliver") };
        assert_eq!(rep.cl_ord_id.as_deref(), Some("cl-1"));
        assert_eq!(rep.order_id.as_deref(), Some("srv-1"));
        assert_eq!(rep.status, "filled");
        assert!(rep.terminal);
    }

    #[test]
    fn graceful_logout_round_trip() {
        let mut m = established();
        let acts = m.start_logout(2_000);
        assert!(matches!(acts.as_slice(), [Action::Send(b)] if b.windows(4).any(|w| w == b"35=5")));
        let bye = server_msg(|w| w.begin("5", "KalshiNR", "SENDER-KEY", 2, "20260721-12:00:02.000"));
        let acts = m.on_message(&bye, 2_500);
        assert!(matches!(acts.as_slice(), [Action::Disconnect { reason }] if reason.contains("complete")));
    }

    #[test]
    fn send_app_only_when_established_and_sequences() {
        let mut m = SessionMachine::new(cfg(), signer(), (1, 1));
        assert!(m.send_app(500).is_none(), "not connected");
        let _ = m.connect(1_000);
        assert!(m.send_app(1_500).is_none(), "logon not answered yet");
        let _ = m.on_message(&server_logon(1), 1_600);
        let (s1, t1) = m.send_app(2_000).unwrap();
        let (s2, _) = m.send_app(2_100).unwrap();
        assert_eq!((s1, s2), (2, 3));
        assert_eq!(t1, utc_sending_time(2_000));
    }

    #[test]
    fn logon_timeout_disconnects() {
        let mut m = SessionMachine::new(cfg(), signer(), (1, 1));
        let _ = m.connect(1_000);
        assert!(m.on_tick(5_000).is_empty());
        let acts = m.on_tick(11_100);
        assert!(matches!(acts.as_slice(), [Action::Disconnect { reason }] if reason.contains("logon")));
    }
}
