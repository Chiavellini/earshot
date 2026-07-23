//! Order transport seam (the execution exit door onto the wire).
//!
//! The executor fires through `OrderTransport::send_order`; each arm owns its
//! wire's prestaged form and per-send machinery. REST: pre-serialized JSON
//! body + presigned headers (RSA off the hot path). The FIX arm lands with
//! the E2 offline build behind this same seam.
//!
//! Everything upstream of `send_order` — crossing check, EV gate, RiskGate,
//! ledger debit — is transport-blind and lives in `executor::handle_intent`.

use std::sync::Arc;

use serde_json::Value;

use super::auth::Presigner;
use super::executor::PreStaged;
use super::fix::encode::FixOrderTemplate;
use super::fix::transport::FixTransport;
use super::fix::FixConfig;
use super::rest::{OrderBodyV2, RestClient};
use super::KalshiError;
use crate::clock::MonotonicNs;

/// Wire-neutral ack. REST's `OrderAck` maps into this at the seam so the
/// executor never handles transport-specific shapes.
#[derive(Debug)]
pub struct TransportAck {
    pub order_id: Option<String>,
    pub order_status: Option<String>,
    /// Raw ack payload (forensics): REST response body; FIX will carry the
    /// ExecutionReport tags re-encoded as JSON.
    pub raw: Value,
    /// TTFB split point (REST: response headers arrived; FIX: first byte of
    /// the ExecutionReport read).
    pub hdrs_ts: MonotonicNs,
    /// Ack fully parsed.
    pub ack_ts: MonotonicNs,
}

#[derive(Debug)]
pub struct SendOutcome {
    pub ack: TransportAck,
    /// Stamped immediately before the bytes hit the wire.
    pub send_ts: MonotonicNs,
    /// Age of the presigned headers used. `None` on wires without a presign
    /// step (FIX), so the `presign_age` telemetry edge stays per-transport
    /// honest instead of recording a fake zero.
    pub presign_age_ns: Option<u64>,
}

/// Prestaged wire form, one variant per transport. Built at arm time by
/// `executor::prestage_orders`; `send_order` consumes its own variant.
#[derive(Clone)]
pub enum WireStaged {
    /// Pre-serialized `OrderBodyV2` JSON (spec §5.7 pre-staging).
    Rest { body: bytes::Bytes },
    /// Pre-encoded 35=D template; only seqnum/timestamps/framing are patched
    /// at fire time.
    Fix { template: FixOrderTemplate },
}

/// How to prestage an order for the active transport — the lightweight
/// handle `prestage_orders` (and the executor's inline fallback) build wire
/// forms through, so staging never needs the live transport itself.
#[derive(Clone, Copy)]
pub enum StageWire<'a> {
    Rest,
    Fix(&'a FixConfig),
}

impl StageWire<'_> {
    pub fn stage(&self, ticker: &str, price_cents: u32, count: u32, client_order_id: String) -> WireStaged {
        match self {
            StageWire::Rest => WireStaged::Rest {
                body: OrderBodyV2::ioc_buy_yes(ticker, price_cents, count, client_order_id).to_bytes(),
            },
            StageWire::Fix(cfg) => WireStaged::Fix {
                template: FixOrderTemplate::ioc_buy_yes(cfg, ticker, price_cents, count, client_order_id),
            },
        }
    }
}

/// The order transports known at compile time. An enum, not a trait object:
/// exactly two wires will ever exist here, the executor hot path stays free
/// of boxed futures, and wiring remains concrete in `main.rs::arm`.
pub enum OrderTransport {
    Rest {
        client: RestClient,
        presigner: Arc<Presigner>,
    },
    Fix {
        fix: Arc<FixTransport>,
    },
}

impl OrderTransport {
    pub fn stage_wire(&self) -> StageWire<'_> {
        match self {
            OrderTransport::Rest { .. } => StageWire::Rest,
            OrderTransport::Fix { fix } => StageWire::Fix(fix.config()),
        }
    }

    /// The one fire-path method: consume the prestaged wire form, put the
    /// order on the wire, return the normalized ack + timing splits.
    pub async fn send_order(&self, staged: &PreStaged) -> Result<SendOutcome, KalshiError> {
        match (self, &staged.wire) {
            (OrderTransport::Rest { client, presigner }, WireStaged::Rest { body }) => {
                let (headers, presign_age) = presigner.get();
                let (ack, send_ts) = client.create_order_presigned(body.clone(), headers).await?;
                Ok(SendOutcome {
                    ack: TransportAck {
                        order_id: ack.order_id,
                        order_status: ack.order_status,
                        raw: ack.raw,
                        hdrs_ts: ack.hdrs_ts,
                        ack_ts: ack.ack_ts,
                    },
                    send_ts,
                    presign_age_ns: Some(presign_age),
                })
            }
            (OrderTransport::Fix { fix }, WireStaged::Fix { template }) => {
                let (er, send_ts, recv_ts) =
                    fix.send_order(template, &staged.client_order_id).await?;
                Ok(SendOutcome {
                    ack: TransportAck {
                        order_id: er.order_id,
                        order_status: Some(er.status),
                        raw: er.raw,
                        hdrs_ts: recv_ts,
                        ack_ts: MonotonicNs::now(),
                    },
                    send_ts,
                    presign_age_ns: None,
                })
            }
            // Should never happen: prestaging goes through `stage_wire`.
            _ => Err(KalshiError::Validation(
                "prestaged wire form does not match the armed transport".into(),
            )),
        }
    }
}
