//! Hand-rolled FIXT.1.1 encoder (no quickfix): a tag writer over reusable
//! buffers, the session-message builders, and the prestaged 35=D template
//! whose only fire-time holes are MsgSeqNum, SendingTime/TransactTime,
//! BodyLength and CheckSum.
//!
//! Wire facts: VERIFICATIONS.md §Q-FIX. Prices are INTEGER CENTS (tag 44,
//! 1–99) — never dollar strings (that is REST's shape; subpenny mode 21005
//! is deliberately not enabled). Quantities are written as plain integers
//! ("60"); the docs say "decimal" without an example, so the exact accepted
//! shape is a pinned verify-at-live-logon item (REST notoriously required
//! "60.00").

use std::io::Write as _;

use super::FixConfig;
use crate::kalshi::auth::Signer;

pub const SOH: u8 = 0x01;
/// FIXT.1.1 BeginString (F1). The application version rides on the Logon's
/// DefaultApplVerID (1137=9 = FIX50SP2).
pub const BEGIN_STRING: &[u8] = b"8=FIXT.1.1";

/// Byte-sum mod 256 of everything before the "10=" field.
fn checksum(bytes: &[u8]) -> u8 {
    bytes.iter().fold(0u8, |a, b| a.wrapping_add(*b))
}

/// Builds one message body (from "35=" through the SOH ending the last
/// field); `finish_into` wraps it with 8=/9= and appends 10=. Reusable: hold
/// one per call site and `begin` clears it.
pub struct FixWriter {
    body: Vec<u8>,
}

impl Default for FixWriter {
    fn default() -> Self {
        Self::new()
    }
}

impl FixWriter {
    pub fn new() -> Self {
        FixWriter {
            body: Vec::with_capacity(512),
        }
    }

    /// Start a message: standard header in fixed order 35, 49, 56, 34, 52.
    pub fn begin(
        &mut self,
        msg_type: &str,
        sender: &str,
        target: &str,
        seq: u64,
        sending_time: &str,
    ) {
        self.body.clear();
        self.tag(35, msg_type.as_bytes());
        self.tag(49, sender.as_bytes());
        self.tag(56, target.as_bytes());
        self.tag_u64(34, seq);
        self.tag(52, sending_time.as_bytes());
    }

    pub fn tag(&mut self, tag: u32, val: &[u8]) {
        write!(self.body, "{tag}=").expect("vec write");
        self.body.extend_from_slice(val);
        self.body.push(SOH);
    }

    pub fn tag_str(&mut self, tag: u32, val: &str) {
        self.tag(tag, val.as_bytes());
    }

    pub fn tag_u64(&mut self, tag: u32, val: u64) {
        write!(self.body, "{tag}={val}").expect("vec write");
        self.body.push(SOH);
    }

    /// Splice pre-encoded "tag=value<SOH>..." bytes (the 35=D template tail).
    pub fn splice(&mut self, encoded: &[u8]) {
        self.body.extend_from_slice(encoded);
    }

    /// Assemble the full wire message into `out` (cleared first):
    /// 8=FIXT.1.1|9=<BodyLength>|<body>|10=<checksum>. BodyLength counts the
    /// bytes after the 9= field's SOH up to and excluding "10="; the checksum
    /// covers every byte before "10=".
    pub fn finish_into(&self, out: &mut Vec<u8>) {
        out.clear();
        out.extend_from_slice(BEGIN_STRING);
        out.push(SOH);
        write!(out, "9={}", self.body.len()).expect("vec write");
        out.push(SOH);
        out.extend_from_slice(&self.body);
        let cks = checksum(out);
        write!(out, "10={cks:03}").expect("vec write");
        out.push(SOH);
    }

    pub fn finish(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(self.body.len() + 32);
        self.finish_into(&mut out);
        out
    }
}

/// UTC SendingTime (tag 52) in the required millisecond format
/// `YYYYMMDD-HH:MM:SS.mmm` (F5). Hand-rolled civil-from-days — no chrono
/// (mirrors the inverse parser in `rest::httpdate_parse`).
pub fn utc_sending_time(unix_ms: u64) -> String {
    let secs = unix_ms / 1000;
    let ms = unix_ms % 1000;
    let days = secs / 86400;
    let tod = secs % 86400;
    let (hh, mi, ss) = (tod / 3600, (tod / 60) % 60, tod % 60);
    // Howard Hinnant's civil_from_days, valid for the unix era.
    let z = days + 719_468;
    let era = z / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + u64::from(m <= 2);
    format!("{y:04}{m:02}{d:02}-{hh:02}:{mi:02}:{ss:02}.{ms:03}")
}

/// The Logon pre-hash string (F3): SendingTime, MsgType, MsgSeqNum,
/// SenderCompID, TargetCompID joined by SOH (no trailing SOH — the docs show
/// separators only; trailing-SOH ambiguity is a Q-FIX verify-at-live item).
/// SendingTime here MUST byte-match tag 52 in the same message.
pub fn logon_prehash(sending_time: &str, seq: u64, sender: &str, target: &str) -> Vec<u8> {
    let mut m = Vec::with_capacity(64);
    m.extend_from_slice(sending_time.as_bytes());
    m.push(SOH);
    m.extend_from_slice(b"A");
    m.push(SOH);
    write!(m, "{seq}").expect("vec write");
    m.push(SOH);
    m.extend_from_slice(sender.as_bytes());
    m.push(SOH);
    m.extend_from_slice(target.as_bytes());
    m
}

/// Logon (35=A): EncryptMethod None, heartbeat, FIX50SP2 marker, RawData
/// RSA-PSS signature over the pre-hash (same key as REST). `reset_seq` sets
/// 141=Y — REQUIRED on KalshiNR (F4).
pub fn logon(
    cfg: &FixConfig,
    signer: &Signer,
    seq: u64,
    sending_time: &str,
    reset_seq: bool,
) -> Vec<u8> {
    let sig = signer.sign_raw(&logon_prehash(
        sending_time,
        seq,
        &cfg.sender_comp_id,
        &cfg.target_comp_id,
    ));
    let mut w = FixWriter::new();
    w.begin(
        "A",
        &cfg.sender_comp_id,
        &cfg.target_comp_id,
        seq,
        sending_time,
    );
    w.tag_str(1137, "9"); // DefaultApplVerID = FIX50SP2
    w.tag_str(98, "0"); // EncryptMethod: none (TLS is the transport layer)
    w.tag_u64(108, u64::from(cfg.heartbeat_secs));
    if reset_seq {
        w.tag_str(141, "Y");
    }
    w.tag_u64(95, sig.len() as u64); // RawDataLength must immediately precede 96
    w.tag_str(96, &sig);
    w.finish()
}

/// Heartbeat (35=0); echoes TestReqID (112) when answering a TestRequest.
pub fn heartbeat(
    cfg: &FixConfig,
    seq: u64,
    sending_time: &str,
    test_req_id: Option<&str>,
) -> Vec<u8> {
    let mut w = FixWriter::new();
    w.begin(
        "0",
        &cfg.sender_comp_id,
        &cfg.target_comp_id,
        seq,
        sending_time,
    );
    if let Some(id) = test_req_id {
        w.tag_str(112, id);
    }
    w.finish()
}

/// TestRequest (35=1).
pub fn test_request(cfg: &FixConfig, seq: u64, sending_time: &str, id: &str) -> Vec<u8> {
    let mut w = FixWriter::new();
    w.begin(
        "1",
        &cfg.sender_comp_id,
        &cfg.target_comp_id,
        seq,
        sending_time,
    );
    w.tag_str(112, id);
    w.finish()
}

/// Logout (35=5).
pub fn logout(cfg: &FixConfig, seq: u64, sending_time: &str) -> Vec<u8> {
    let mut w = FixWriter::new();
    w.begin(
        "5",
        &cfg.sender_comp_id,
        &cfg.target_comp_id,
        seq,
        sending_time,
    );
    w.finish()
}

/// SequenceReset-GapFill (35=4, 123=Y, 36=new): our only lawful answer to a
/// ResendRequest — the NR posture never retransmits application messages.
pub fn sequence_reset_gapfill(
    cfg: &FixConfig,
    seq: u64,
    sending_time: &str,
    new_seq: u64,
) -> Vec<u8> {
    let mut w = FixWriter::new();
    w.begin(
        "4",
        &cfg.sender_comp_id,
        &cfg.target_comp_id,
        seq,
        sending_time,
    );
    w.tag_str(123, "Y");
    w.tag_u64(36, new_seq);
    w.finish()
}

/// OrderCancelRequest (35=F): new ClOrdID + OrigClOrdID + side + symbol (F8).
/// Encoded for completeness; NOT wired into the kill switch (which cancels
/// OrderRegistry ids over REST — same order store, scope invariant untouched).
pub fn order_cancel(
    cfg: &FixConfig,
    seq: u64,
    sending_time: &str,
    cl_ord_id: &str,
    orig_cl_ord_id: &str,
    ticker: &str,
) -> Vec<u8> {
    let mut w = FixWriter::new();
    w.begin(
        "F",
        &cfg.sender_comp_id,
        &cfg.target_comp_id,
        seq,
        sending_time,
    );
    w.tag_str(11, cl_ord_id);
    w.tag_str(41, orig_cl_ord_id);
    w.tag_str(54, "1"); // we only ever work the YES-buy side
    w.tag_str(55, ticker);
    w.finish()
}

/// Prestaged NewOrderSingle (35=D): the body tail (ClOrdID, Symbol, Side,
/// OrderQty, OrdType, Price, TimeInForce, STP) is encoded once at arm time;
/// `assemble` writes only the per-send holes — header seqnum + timestamps —
/// then wraps with BodyLength/CheckSum. This is FIX's analogue of the REST
/// pre-serialized `OrderBodyV2` body.
#[derive(Clone)]
pub struct FixOrderTemplate {
    sender: String,
    target: String,
    /// "11=…<SOH>…2964=1<SOH>" — everything invariant per order.
    tail: Vec<u8>,
    pub cl_ord_id: String,
}

impl FixOrderTemplate {
    /// IOC marketable-limit YES buy at integer-cent `price_cents` (F6):
    /// 54=1 Buy, 40=2 Limit, 44=cents, 59=3 IOC, 2964=1 Taker-At-Cross
    /// (explicit, mirroring REST's self_trade_prevention_type).
    pub fn ioc_buy_yes(
        cfg: &FixConfig,
        ticker: &str,
        price_cents: u32,
        count: u32,
        cl_ord_id: String,
    ) -> Self {
        let mut tail = Vec::with_capacity(128);
        let mut t = |tag: u32, val: &str| {
            write!(tail, "{tag}={val}").expect("vec write");
            tail.push(SOH);
        };
        t(11, &cl_ord_id);
        t(55, ticker);
        t(54, "1");
        t(38, &count.to_string());
        t(40, "2");
        t(44, &price_cents.to_string());
        t(59, "3");
        t(2964, "1");
        FixOrderTemplate {
            sender: cfg.sender_comp_id.clone(),
            target: cfg.target_comp_id.clone(),
            tail,
            cl_ord_id,
        }
    }

    /// Fire-time assembly into reused buffers (µs-class, no allocation once
    /// the buffers are warm). TransactTime (60) is stamped with the same
    /// value as SendingTime.
    pub fn assemble(&self, seq: u64, sending_time: &str, w: &mut FixWriter, out: &mut Vec<u8>) {
        w.begin("D", &self.sender, &self.target, seq, sending_time);
        w.tag(60, sending_time.as_bytes());
        w.splice(&self.tail);
        w.finish_into(out);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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

    /// Replace SOH with '|' for readable assertions.
    fn readable(bytes: &[u8]) -> String {
        String::from_utf8(
            bytes
                .iter()
                .map(|&b| if b == SOH { b'|' } else { b })
                .collect(),
        )
        .unwrap()
    }

    #[test]
    fn sending_time_known_epochs() {
        assert_eq!(utc_sending_time(0), "19700101-00:00:00.000");
        // 2023-12-21 01:50:56.789 UTC (the auth.rs test timestamp).
        assert_eq!(utc_sending_time(1_703_123_456_789), "20231221-01:50:56.789");
        // Leap-day boundary: 2024-02-29 23:59:59.999 UTC.
        assert_eq!(utc_sending_time(1_709_251_199_999), "20240229-23:59:59.999");
        // A 2026 event-era stamp: 2026-07-21 12:00:00.000 UTC.
        assert_eq!(utc_sending_time(1_784_635_200_000), "20260721-12:00:00.000");
    }

    /// Golden vector: full heartbeat, byte-exact. BodyLength 61 and CheckSum
    /// 225 were computed independently (byte-sum mod 256, out-of-band).
    #[test]
    fn heartbeat_golden_vector() {
        let msg = heartbeat(&cfg(), 7, "20260721-12:00:00.000", None);
        assert_eq!(
            readable(&msg),
            "8=FIXT.1.1|9=61|35=0|49=SENDER-KEY|56=KalshiNR|34=7|52=20260721-12:00:00.000|10=225|"
        );
    }

    /// Golden vector: full 35=D, byte-exact. BodyLength 143 and CheckSum 068
    /// were computed independently (byte-sum mod 256, out-of-band).
    #[test]
    fn new_order_single_golden_vector() {
        let tpl = FixOrderTemplate::ioc_buy_yes(&cfg(), "KX-TEST", 97, 60, "abc-123".into());
        let (mut w, mut out) = (FixWriter::new(), Vec::new());
        tpl.assemble(42, "20260721-12:00:00.000", &mut w, &mut out);
        assert_eq!(
            readable(&out),
            "8=FIXT.1.1|9=143|35=D|49=SENDER-KEY|56=KalshiNR|34=42|52=20260721-12:00:00.000|\
             60=20260721-12:00:00.000|11=abc-123|55=KX-TEST|54=1|38=60|40=2|44=97|59=3|2964=1|10=068|"
        );
    }

    /// Template assembly must be byte-identical to a from-scratch encode,
    /// across seqnum widths (the header hole changes BodyLength).
    #[test]
    fn template_assemble_equals_from_scratch() {
        let c = cfg();
        let tpl = FixOrderTemplate::ioc_buy_yes(&c, "KX-TEST", 5, 1, "id-1".into());
        for seq in [1u64, 12_345, 99_999_999] {
            let ts = "20260721-12:00:00.000";
            let (mut w, mut fast) = (FixWriter::new(), Vec::new());
            tpl.assemble(seq, ts, &mut w, &mut fast);

            let mut scratch = FixWriter::new();
            scratch.begin("D", &c.sender_comp_id, &c.target_comp_id, seq, ts);
            scratch.tag_str(60, ts);
            scratch.tag_str(11, "id-1");
            scratch.tag_str(55, "KX-TEST");
            scratch.tag_str(54, "1");
            scratch.tag_str(38, "1");
            scratch.tag_str(40, "2");
            scratch.tag_str(44, "5");
            scratch.tag_str(59, "3");
            scratch.tag_str(2964, "1");
            assert_eq!(fast, scratch.finish(), "seq {seq}");
        }
    }

    /// Reused buffers must not leak bytes between assemblies (a longer
    /// message followed by a shorter one).
    #[test]
    fn reused_buffers_do_not_leak() {
        let c = cfg();
        let long = FixOrderTemplate::ioc_buy_yes(
            &c,
            "KX-A-VERY-LONG-TICKER-NAME",
            97,
            100_000,
            "long-id".into(),
        );
        let short = FixOrderTemplate::ioc_buy_yes(&c, "KX-B", 5, 1, "s".into());
        let (mut w, mut out) = (FixWriter::new(), Vec::new());
        long.assemble(999_999, "20260721-12:00:00.000", &mut w, &mut out);
        short.assemble(1, "20260721-12:00:00.000", &mut w, &mut out);
        let mut fresh_out = Vec::new();
        short.assemble(
            1,
            "20260721-12:00:00.000",
            &mut FixWriter::new(),
            &mut fresh_out,
        );
        assert_eq!(out, fresh_out);
    }

    #[test]
    fn logon_rawdata_signature_verifies_against_prehash() {
        use base64::Engine;
        use rsa::pkcs1::EncodeRsaPrivateKey;
        use rsa::pss::VerifyingKey;
        use rsa::sha2::Sha256;
        use rsa::signature::Verifier;

        let key = rsa::RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 2048).unwrap();
        let pem = key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF).unwrap();
        let signer = Signer::from_pem("SENDER-KEY".into(), &pem).unwrap();

        let c = cfg();
        let ts = "20260721-12:00:00.000";
        let msg = logon(&c, &signer, 1, ts, true);
        let s = readable(&msg);
        // Required logon tags (F4).
        for want in ["35=A|", "1137=9|", "98=0|", "108=10|", "141=Y|", "|95="] {
            assert!(s.contains(want), "missing {want} in {s}");
        }
        // Extract 96= and verify RSA-PSS over the documented pre-hash.
        let sig_b64 = s
            .split('|')
            .find_map(|f| f.strip_prefix("96="))
            .expect("96= present");
        let sig_bytes = base64::engine::general_purpose::STANDARD
            .decode(sig_b64)
            .unwrap();
        let verifying: VerifyingKey<Sha256> = VerifyingKey::new(key.to_public_key());
        let sig = rsa::pss::Signature::try_from(sig_bytes.as_slice()).unwrap();
        let prehash = logon_prehash(ts, 1, "SENDER-KEY", "KalshiNR");
        verifying
            .verify(&prehash, &sig)
            .expect("RawData must verify against SendingTime|MsgType|Seq|Sender|Target");
        // And NOT against a near-miss (REST-style concatenation, no SOH).
        let near_miss = format!("{ts}A1SENDER-KEYKalshiNR");
        assert!(verifying.verify(near_miss.as_bytes(), &sig).is_err());
        // 95 must equal the byte length of 96's value and immediately precede it.
        let len95: usize = s
            .split('|')
            .find_map(|f| f.strip_prefix("95="))
            .unwrap()
            .parse()
            .unwrap();
        assert_eq!(len95, sig_b64.len());
        assert!(s.contains(&format!("95={len95}|96=")));
    }

    #[test]
    fn gapfill_and_cancel_shapes() {
        let c = cfg();
        let g = readable(&sequence_reset_gapfill(&c, 5, "20260721-12:00:00.000", 9));
        assert!(g.contains("35=4|") && g.contains("123=Y|") && g.contains("36=9|"));
        let x = readable(&order_cancel(
            &c,
            6,
            "20260721-12:00:00.000",
            "new-id",
            "orig-id",
            "KX-T",
        ));
        assert!(
            x.contains("35=F|")
                && x.contains("11=new-id|")
                && x.contains("41=orig-id|")
                && x.contains("55=KX-T|")
        );
    }
}
