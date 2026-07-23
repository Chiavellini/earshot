//! FIX order-entry (lever E2, offline build 2026-07-21).
//!
//! Wire facts pinned in VERIFICATIONS.md §Q-FIX: FIXT.1.1 transport with
//! application version FIX50SP2, TLS-only endpoints, Logon auth via RawData
//! RSA-PSS signature (same key as REST, different canonical string), prices
//! as integer cents 1–99, ClOrdID as the shared idempotency key with REST.
//!
//! Layout: `encode` (hand-rolled writer + message builders + the prestaged
//! 35=D template), `parse` (zero-copy tag scanner + typed views), `session`
//! (sans-io session state machine), `seqstore` (durable seqnums for a future
//! KalshiRT session), `transport` (tokio TCP+TLS driver behind
//! `OrderTransport::Fix`).
//!
//! Live validation (demo logon, 1¢ IOC probe) is explicitly deferred until
//! FIX/Premier access lands — nothing here has touched a live Kalshi FIX
//! endpoint yet; every "verify at live logon" item is listed in Q-FIX.

pub mod encode;
pub mod parse;
pub mod seqstore;
pub mod session;
pub mod transport;

/// FIX session configuration. Defaults target the production non-retransmission
/// session (KalshiNR): reset-on-logon (141=Y), no replay obligations.
#[derive(Debug, Clone)]
pub struct FixConfig {
    pub host: String,
    pub port: u16,
    /// SenderCompID (tag 49): the FIX API key UUID.
    pub sender_comp_id: String,
    /// TargetCompID (tag 56): "KalshiNR" (port 8228) or "KalshiRT" (8230).
    pub target_comp_id: String,
    /// Heartbeat interval (tag 108), seconds. Kalshi requires > 3.
    pub heartbeat_secs: u32,
    /// TLS on the socket (mandatory on Kalshi; `false` only for the
    /// in-process plaintext mock server in tests).
    pub tls: bool,
}

impl FixConfig {
    /// Production order-entry defaults (VERIFICATIONS Q-FIX F2); caller fills
    /// in the key id.
    pub fn prod_nr(sender_comp_id: String) -> Self {
        FixConfig {
            host: "mm.fix.elections.kalshi.com".into(),
            port: 8228,
            sender_comp_id,
            target_comp_id: "KalshiNR".into(),
            heartbeat_secs: 10,
            tls: true,
        }
    }
}
