//! Zero-copy FIX tag scanner + typed inbound views.
//!
//! `frame_len` finds complete messages in a TCP read buffer; `FixMsg::parse`
//! validates framing, BodyLength and CheckSum and indexes tag→value slices;
//! `classify` lifts a parsed message into the typed `Inbound` the session
//! machine consumes. CompIDs (49/56) are deliberately NOT asserted here —
//! the server-side values are a Q-FIX verify-at-live item; the session layer
//! logs a mismatch instead of dying on it.

use smallvec::SmallVec;

use super::encode::SOH;

#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum ParseError {
    #[error("truncated or unframed message")]
    Truncated,
    #[error("malformed field near byte {0}")]
    Malformed(usize),
    #[error("BodyLength says {declared}, measured {measured}")]
    BadBodyLength { declared: usize, measured: usize },
    #[error("checksum declared {declared:03}, computed {computed:03}")]
    BadChecksum { declared: u8, computed: u8 },
}

/// Length of the first complete message at the start of `buf`, if any:
/// "8=…\x019=<len>\x01" + len body bytes + "10=NNN\x01" (7 bytes).
pub fn frame_len(buf: &[u8]) -> Option<usize> {
    // Find the SOH ending 8=…
    let p1 = buf.iter().position(|&b| b == SOH)? + 1;
    let rest = &buf[p1..];
    if !rest.starts_with(b"9=") {
        return None;
    }
    let p2 = rest.iter().position(|&b| b == SOH)?;
    let len: usize = std::str::from_utf8(&rest[2..p2]).ok()?.parse().ok()?;
    let total = p1 + p2 + 1 + len + 7;
    (buf.len() >= total).then_some(total)
}

/// A parsed message borrowing the wire bytes. Duplicate tags keep the FIRST
/// occurrence (sufficient for the session/ER tags we read; repeating groups
/// are not modeled — nothing Kalshi sends us needs them yet).
#[derive(Debug)]
pub struct FixMsg<'a> {
    pub msg_type: &'a [u8],
    pub seq: u64,
    fields: SmallVec<[(u32, (u32, u32)); 24]>,
    raw: &'a [u8],
}

impl<'a> FixMsg<'a> {
    /// Parse exactly one full message (as delimited by `frame_len`).
    pub fn parse(raw: &'a [u8]) -> Result<FixMsg<'a>, ParseError> {
        let total = frame_len(raw).ok_or(ParseError::Truncated)?;
        if total != raw.len() {
            return Err(ParseError::Truncated);
        }
        let mut fields: SmallVec<[(u32, (u32, u32)); 24]> = SmallVec::new();
        let mut i = 0usize;
        let mut body_start = 0usize;
        let mut checksum_at = 0usize;
        let mut declared_len = 0usize;
        let mut declared_cks = 0u8;
        while i < raw.len() {
            let eq = raw[i..]
                .iter()
                .position(|&b| b == b'=')
                .ok_or(ParseError::Malformed(i))?
                + i;
            let tag: u32 = std::str::from_utf8(&raw[i..eq])
                .ok()
                .and_then(|s| s.parse().ok())
                .ok_or(ParseError::Malformed(i))?;
            let soh = raw[eq + 1..]
                .iter()
                .position(|&b| b == SOH)
                .ok_or(ParseError::Malformed(eq))?
                + eq
                + 1;
            let val = &raw[eq + 1..soh];
            match tag {
                9 => {
                    declared_len = std::str::from_utf8(val)
                        .ok()
                        .and_then(|s| s.parse().ok())
                        .ok_or(ParseError::Malformed(i))?;
                    body_start = soh + 1;
                }
                10 => {
                    checksum_at = i;
                    declared_cks = std::str::from_utf8(val)
                        .ok()
                        .and_then(|s| s.parse().ok())
                        .ok_or(ParseError::Malformed(i))?;
                }
                _ => {
                    if !fields.iter().any(|(t, _)| *t == tag) {
                        fields.push((tag, (eq as u32 + 1, soh as u32)));
                    }
                }
            }
            i = soh + 1;
        }
        if checksum_at == 0 || body_start == 0 {
            return Err(ParseError::Truncated);
        }
        let measured = checksum_at - body_start;
        if measured != declared_len {
            return Err(ParseError::BadBodyLength {
                declared: declared_len,
                measured,
            });
        }
        let computed = raw[..checksum_at]
            .iter()
            .fold(0u8, |a, b| a.wrapping_add(*b));
        if computed != declared_cks {
            return Err(ParseError::BadChecksum {
                declared: declared_cks,
                computed,
            });
        }
        let msg = FixMsg {
            msg_type: b"",
            seq: 0,
            fields,
            raw,
        };
        let msg_type = msg.get(35).ok_or(ParseError::Malformed(body_start))?;
        let seq = msg.get_u64(34).ok_or(ParseError::Malformed(body_start))?;
        Ok(FixMsg {
            msg_type,
            seq,
            ..msg
        })
    }

    pub fn get(&self, tag: u32) -> Option<&'a [u8]> {
        self.fields
            .iter()
            .find(|(t, _)| *t == tag)
            .map(|(_, (s, e))| &self.raw[*s as usize..*e as usize])
    }

    pub fn get_str(&self, tag: u32) -> Option<&'a str> {
        self.get(tag).and_then(|b| std::str::from_utf8(b).ok())
    }

    pub fn get_u64(&self, tag: u32) -> Option<u64> {
        self.get_str(tag).and_then(|s| s.parse().ok())
    }

    /// First byte of a single-char code field (ExecType, OrdStatus…).
    pub fn get_char(&self, tag: u32) -> Option<u8> {
        self.get(tag).and_then(|b| b.first().copied())
    }
}

/// ExecutionReport (35=8) view — the fields the transport correlates and the
/// executor forensically records (F7).
#[derive(Debug, Clone)]
pub struct ExecReportView<'a> {
    pub exec_type: u8,                   // 150
    pub ord_status: u8,                  // 39
    pub order_id: Option<&'a str>,       // 37 (exchange id)
    pub cl_ord_id: Option<&'a str>,      // 11 (our idempotency key)
    pub cum_qty: Option<&'a str>,        // 14
    pub leaves_qty: Option<&'a str>,     // 151
    pub last_px: Option<&'a str>,        // 31 (cents)
    pub last_qty: Option<&'a str>,       // 32
    pub avg_px: Option<&'a str>,         // 6
    pub ord_rej_reason: Option<&'a str>, // 103
    pub text: Option<&'a str>,           // 58
}

impl<'a> ExecReportView<'a> {
    pub fn from_msg(m: &FixMsg<'a>) -> Option<Self> {
        Some(ExecReportView {
            exec_type: m.get_char(150)?,
            ord_status: m.get_char(39)?,
            order_id: m.get_str(37),
            cl_ord_id: m.get_str(11),
            cum_qty: m.get_str(14),
            leaves_qty: m.get_str(151),
            last_px: m.get_str(31),
            last_qty: m.get_str(32),
            avg_px: m.get_str(6),
            ord_rej_reason: m.get_str(103),
            text: m.get_str(58),
        })
    }

    /// Terminal for an IOC: anything but PendingNew/New tells the racer the
    /// engine is done with this order (F7: F=Trade may repeat, but OrdStatus
    /// goes terminal on the last one).
    pub fn is_terminal(&self) -> bool {
        // OrdStatus: 0=New, 1=Partial, 2=Filled, 4=Canceled, 8=Rejected, C=Expired.
        matches!(self.ord_status, b'2' | b'4' | b'8' | b'C')
    }

    /// Normalized status string matching the REST ack vocabulary so
    /// `OrderRecord.order_status` reads identically across transports.
    pub fn normalized_status(&self) -> &'static str {
        match self.ord_status {
            b'0' => "new",
            b'1' => "partial",
            b'2' => "filled",
            b'4' => "canceled",
            b'8' => "rejected",
            b'C' => "expired",
            _ => "unknown",
        }
    }

    /// Forensic JSON for `TransportAck.raw` (mirrors REST keeping the body).
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "exec_type": (self.exec_type as char).to_string(),
            "ord_status": (self.ord_status as char).to_string(),
            "order_id": self.order_id,
            "client_order_id": self.cl_ord_id,
            "cum_qty": self.cum_qty,
            "leaves_qty": self.leaves_qty,
            "last_px": self.last_px,
            "last_qty": self.last_qty,
            "avg_px": self.avg_px,
            "ord_rej_reason": self.ord_rej_reason,
            "text": self.text,
        })
    }
}

/// Typed inbound message for the session machine.
#[derive(Debug)]
pub enum Inbound<'a> {
    Logon {
        heartbeat_secs: Option<u64>,
    },
    Heartbeat {
        test_req_id: Option<&'a str>,
    },
    TestRequest {
        id: Option<&'a str>,
    },
    ResendRequest {
        begin: u64,
        end: u64,
    },
    SequenceReset {
        new_seq: u64,
        gap_fill: bool,
    },
    Logout {
        text: Option<&'a str>,
    },
    /// Session-level Reject (35=3): 45=RefSeqNum, 373=SessionRejectReason.
    Reject {
        ref_seq: Option<u64>,
        reason: Option<&'a str>,
        text: Option<&'a str>,
    },
    /// BusinessMessageReject (35=j): 380=BusinessRejectReason (8 = rate limit).
    BusinessReject {
        reason: Option<&'a str>,
        text: Option<&'a str>,
    },
    ExecReport(ExecReportView<'a>),
    /// OrderCancelReject (35=9).
    CancelReject {
        cl_ord_id: Option<&'a str>,
        reason: Option<&'a str>,
        text: Option<&'a str>,
    },
    Other {
        msg_type: &'a [u8],
    },
}

pub fn classify<'a>(m: &FixMsg<'a>) -> Inbound<'a> {
    match m.msg_type {
        b"A" => Inbound::Logon {
            heartbeat_secs: m.get_u64(108),
        },
        b"0" => Inbound::Heartbeat {
            test_req_id: m.get_str(112),
        },
        b"1" => Inbound::TestRequest { id: m.get_str(112) },
        b"2" => Inbound::ResendRequest {
            begin: m.get_u64(7).unwrap_or(0),
            end: m.get_u64(16).unwrap_or(0),
        },
        b"4" => Inbound::SequenceReset {
            new_seq: m.get_u64(36).unwrap_or(0),
            gap_fill: m.get(123) == Some(b"Y"),
        },
        b"5" => Inbound::Logout {
            text: m.get_str(58),
        },
        b"3" => Inbound::Reject {
            ref_seq: m.get_u64(45),
            reason: m.get_str(373),
            text: m.get_str(58),
        },
        b"j" => Inbound::BusinessReject {
            reason: m.get_str(380),
            text: m.get_str(58),
        },
        b"8" => match ExecReportView::from_msg(m) {
            Some(er) => Inbound::ExecReport(er),
            None => Inbound::Other {
                msg_type: m.msg_type,
            },
        },
        b"9" => Inbound::CancelReject {
            cl_ord_id: m.get_str(11),
            reason: m.get_str(102),
            text: m.get_str(58),
        },
        other => Inbound::Other { msg_type: other },
    }
}

#[cfg(test)]
mod tests {
    use super::super::encode::{heartbeat, FixWriter};
    use super::super::FixConfig;
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

    #[test]
    fn round_trips_an_encoded_heartbeat() {
        let msg = heartbeat(&cfg(), 7, "20260721-12:00:00.000", Some("ping-1"));
        let parsed = FixMsg::parse(&msg).unwrap();
        assert_eq!(parsed.msg_type, b"0");
        assert_eq!(parsed.seq, 7);
        assert_eq!(parsed.get_str(112), Some("ping-1"));
        assert_eq!(parsed.get_str(49), Some("SENDER-KEY"));
        assert_eq!(parsed.get_str(56), Some("KalshiNR"));
        assert!(matches!(
            classify(&parsed),
            Inbound::Heartbeat {
                test_req_id: Some("ping-1")
            }
        ));
    }

    /// Canned ExecutionReport byte vector → typed view.
    #[test]
    fn parses_a_canned_execution_report() {
        let mut w = FixWriter::new();
        w.begin("8", "KalshiNR", "SENDER-KEY", 3, "20260721-12:00:00.100");
        w.tag_str(11, "abc-123");
        w.tag_str(37, "srv-777");
        w.tag_str(150, "F");
        w.tag_str(39, "2");
        w.tag_str(14, "60");
        w.tag_str(151, "0");
        w.tag_str(31, "97");
        w.tag_str(32, "60");
        w.tag_str(6, "96.5");
        let raw = w.finish();

        let parsed = FixMsg::parse(&raw).unwrap();
        let Inbound::ExecReport(er) = classify(&parsed) else {
            panic!("not classified as ER")
        };
        assert_eq!(er.exec_type, b'F');
        assert_eq!(er.ord_status, b'2');
        assert_eq!(er.order_id, Some("srv-777"));
        assert_eq!(er.cl_ord_id, Some("abc-123"));
        assert_eq!(er.cum_qty, Some("60"));
        assert_eq!(er.leaves_qty, Some("0"));
        assert_eq!(er.last_px, Some("97"));
        assert!(er.is_terminal());
        assert_eq!(er.normalized_status(), "filled");
        assert_eq!(er.to_json()["order_id"], "srv-777");
    }

    #[test]
    fn rejected_order_er() {
        let mut w = FixWriter::new();
        w.begin("8", "KalshiNR", "SENDER-KEY", 4, "20260721-12:00:00.100");
        w.tag_str(11, "abc-123");
        w.tag_str(150, "8");
        w.tag_str(39, "8");
        w.tag_str(103, "6");
        w.tag_str(58, "not authorized");
        let raw = w.finish();
        let parsed = FixMsg::parse(&raw).unwrap();
        let Inbound::ExecReport(er) = classify(&parsed) else {
            panic!("not an ER")
        };
        assert!(er.is_terminal());
        assert_eq!(er.normalized_status(), "rejected");
        assert_eq!(er.ord_rej_reason, Some("6"));
        assert_eq!(er.text, Some("not authorized"));
    }

    #[test]
    fn corrupt_checksum_rejected() {
        let mut msg = heartbeat(&cfg(), 7, "20260721-12:00:00.000", None);
        // Flip a body byte without touching length: seq "7" → "8".
        let pos = msg.windows(4).position(|w| w == b"34=7").unwrap() + 3;
        msg[pos] = b'8';
        match FixMsg::parse(&msg) {
            Err(ParseError::BadChecksum { .. }) => {}
            other => panic!("expected BadChecksum, got {other:?}"),
        }
    }

    #[test]
    fn bad_bodylength_rejected() {
        let msg = heartbeat(&cfg(), 7, "20260721-12:00:00.000", None);
        let s = String::from_utf8(msg.clone()).unwrap();
        // Declare one byte more than the real body (keeps digit-count so the
        // frame stays self-consistent enough to reach the length check).
        let bad = s.replacen("9=61", "9=62", 1).into_bytes();
        // frame_len now wants one more byte than exists → Truncated; feed it
        // an extra byte to reach the BodyLength check.
        let mut padded = bad.clone();
        padded.push(b'X');
        match FixMsg::parse(&padded) {
            Err(ParseError::BadBodyLength { .. }) | Err(ParseError::Malformed(_)) => {}
            other => panic!("expected BadBodyLength/Malformed, got {other:?}"),
        }
        assert_eq!(FixMsg::parse(&bad).unwrap_err(), ParseError::Truncated);
    }

    #[test]
    fn frame_len_splits_a_stream() {
        let one = heartbeat(&cfg(), 1, "20260721-12:00:00.000", None);
        let two = heartbeat(&cfg(), 2, "20260721-12:00:01.000", None);
        let mut stream = one.clone();
        stream.extend_from_slice(&two);
        let n = frame_len(&stream).unwrap();
        assert_eq!(n, one.len());
        assert_eq!(FixMsg::parse(&stream[..n]).unwrap().seq, 1);
        let m = frame_len(&stream[n..]).unwrap();
        assert_eq!(m, two.len());
        assert_eq!(FixMsg::parse(&stream[n..n + m]).unwrap().seq, 2);
        // A partial tail is not a frame.
        assert_eq!(frame_len(&stream[n..n + m - 1]), None);
    }
}
