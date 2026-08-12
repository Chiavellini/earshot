//! Order transport seam (the execution exit door onto the wire).
//!
//! The executor fires through `OrderTransport::submit_order`; each arm owns
//! its wire's prestaged form and per-send machinery. REST: pre-serialized JSON
//! body + presigned headers (RSA off the hot path). The FIX arm lands with
//! the E2 offline build behind this same seam.
//!
//! Everything upstream of `submit_order` — crossing check, EV gate, RiskGate,
//! ledger debit — is transport-blind and lives in `executor::handle_intent`.

use std::sync::Arc;

use serde_json::Value;

use super::auth::Presigner;
use super::executor::PreStaged;
use super::fix::encode::FixOrderTemplate;
use super::fix::transport::{
    FixExecutionObservation, FixOrderObservation, FixRejectKind, FixRejectObservation,
    FixTracedSendAttempt, FixTracedSendResult, FixTransport,
};
use super::fix::FixConfig;
use super::rest::{OrderBodyV2, RestAttemptFailure, RestClient, RestOrderObservation};
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

/// Exact fill fields as the exchange encoded them. Quantities/prices remain
/// strings so evidence never changes fixed-point precision or silently mixes
/// REST dollar prices with FIX cent prices.
#[derive(Debug, Clone, serde::Serialize)]
pub struct FillEvidence {
    pub cumulative_contracts: Option<String>,
    pub remaining_contracts: Option<String>,
    pub last_fill_contracts: Option<String>,
    pub last_fill_price: Option<String>,
    pub average_price: Option<String>,
    pub price_unit: &'static str,
}

/// One exchange/protocol observation after a send attempt. Rejection is an
/// observation (`accepted = Some(false)`), not a transport failure.
#[derive(Debug, Clone, serde::Serialize)]
pub struct TransportObservation {
    pub protocol: &'static str,
    pub protocol_status: String,
    pub accepted: Option<bool>,
    pub order_terminal: Option<bool>,
    /// Exact client id carried by the exchange observation. Never populated
    /// from the outbound request.
    pub echoed_client_order_id: Option<String>,
    pub order_id: Option<String>,
    pub order_status: Option<String>,
    pub raw: Value,
    /// Exact HTTP bytes when applicable; FIX preserves exact typed fields in
    /// `raw` because the parser lifts the framed tags before this seam.
    pub raw_body: Option<Vec<u8>>,
    pub parse_error: Option<String>,
    pub observed_ts: MonotonicNs,
    pub ack_ts: Option<MonotonicNs>,
    pub fill: Option<FillEvidence>,
}

#[derive(Debug)]
pub struct TransportFailure {
    pub error: KalshiError,
}

#[derive(Debug)]
pub struct SendAttempt {
    pub protocol: &'static str,
    pub send_ts: MonotonicNs,
    pub presign_age_ns: Option<u64>,
    /// Every correlated exchange/protocol observation in receive order.
    pub observations: Vec<TransportObservation>,
    pub outcome: Result<(), TransportFailure>,
}

#[derive(Debug)]
pub enum SubmitResult {
    /// Validation/session failure before the transport entered a socket write.
    NotAttempted { error: KalshiError },
    /// `send_ts` exists, so failure is ambiguous until reconciled by the
    /// idempotent client order id.
    Attempted(SendAttempt),
}

fn value_string(value: &Value, key: &str) -> Option<String> {
    value.get(key).and_then(Value::as_str).map(String::from)
}

fn rest_fill(raw: &Value) -> Option<FillEvidence> {
    let order = if raw.get("order").is_some() {
        &raw["order"]
    } else {
        raw
    };
    let fill = FillEvidence {
        cumulative_contracts: value_string(order, "fill_count"),
        remaining_contracts: value_string(order, "remaining_count"),
        last_fill_contracts: None,
        last_fill_price: None,
        average_price: value_string(order, "average_fill_price"),
        price_unit: "dollars",
    };
    if fill.cumulative_contracts.is_some()
        || fill.remaining_contracts.is_some()
        || fill.average_price.is_some()
    {
        Some(fill)
    } else {
        None
    }
}

fn rest_observation(
    observation: RestOrderObservation,
    body_complete: bool,
) -> TransportObservation {
    // A normal application response can conclusively accept/reject. Timeout,
    // gateway, and server failures cannot: the exchange may have processed
    // the request before the intermediary produced 408/5xx, so reconcile by
    // client id instead of recording a false rejection.
    let accepted = match observation.status_code {
        200..=299 => Some(true),
        400..=499 if !matches!(observation.status_code, 408 | 425 | 429) => Some(false),
        _ => None,
    };
    let raw = observation.raw.unwrap_or(Value::Null);
    let fill = rest_fill(&raw);
    let order_terminal = if accepted == Some(false) {
        Some(true)
    } else {
        observation
            .order_status
            .as_deref()
            .and_then(|status| match status {
                "filled" | "canceled" | "rejected" | "expired" => Some(true),
                "resting" | "pending" | "partial" => Some(false),
                _ => None,
            })
    };
    TransportObservation {
        protocol: "rest",
        protocol_status: format!("http:{}", observation.status_code),
        accepted,
        order_terminal,
        echoed_client_order_id: raw
            .get("order")
            .unwrap_or(&raw)
            .get("client_order_id")
            .and_then(Value::as_str)
            .map(String::from),
        order_id: observation.order_id,
        order_status: observation.order_status,
        raw,
        raw_body: Some(observation.raw_body.to_vec()),
        parse_error: observation.parse_error,
        observed_ts: observation.hdrs_ts,
        ack_ts: body_complete.then_some(observation.ack_ts),
        fill,
    }
}

fn rest_failure(error: KalshiError) -> TransportFailure {
    TransportFailure { error }
}

fn fix_observation(observation: FixExecutionObservation) -> TransportObservation {
    let raw_frame = observation.raw;
    let report = observation.report;
    let fill = FillEvidence {
        cumulative_contracts: value_string(&report.raw, "cum_qty"),
        remaining_contracts: value_string(&report.raw, "leaves_qty"),
        last_fill_contracts: value_string(&report.raw, "last_qty"),
        last_fill_price: value_string(&report.raw, "last_px"),
        average_price: value_string(&report.raw, "avg_px"),
        price_unit: "cents",
    };
    let fill = if fill.cumulative_contracts.is_some()
        || fill.remaining_contracts.is_some()
        || fill.last_fill_contracts.is_some()
        || fill.last_fill_price.is_some()
        || fill.average_price.is_some()
    {
        Some(fill)
    } else {
        None
    };
    let exec_type = value_string(&report.raw, "exec_type").unwrap_or_else(|| "?".into());
    let ord_status = value_string(&report.raw, "ord_status").unwrap_or_else(|| "?".into());
    let accepted = match report.status.as_str() {
        "rejected" => Some(false),
        // Expired/canceled/filled require a previously accepted order.
        "new" | "partial" | "filled" | "canceled" | "expired" => Some(true),
        _ => None,
    };
    TransportObservation {
        protocol: "fix",
        protocol_status: format!("exec_type:{exec_type};ord_status:{ord_status}"),
        accepted,
        order_terminal: Some(report.terminal),
        echoed_client_order_id: report.cl_ord_id,
        order_id: report.order_id,
        order_status: Some(report.status),
        raw: report.raw,
        raw_body: Some(raw_frame),
        parse_error: None,
        observed_ts: observation.recv_ts,
        ack_ts: Some(observation.ack_ts),
        fill,
    }
}

fn fix_reject_observation(observation: FixRejectObservation) -> TransportObservation {
    // `FixTransport` may correlate a Reject to an outbound ClOrdID through
    // RefSeqNum (45) and its private generation/sequence map.  That is useful
    // routing information, but it is not an ID echoed by the exchange.  Only
    // an actual tag 11 in the inbound frame is exchange-observed identity.
    let echoed_client_order_id = fix_tag(&observation.raw, b"11");
    let correlation_method = if echoed_client_order_id.is_some() {
        "exchange_cl_ord_id"
    } else if observation.ref_seq.is_some() {
        "local_outbound_sequence"
    } else if observation.business_ref_id.as_deref() == observation.client_order_id.as_deref() {
        "business_ref_id"
    } else {
        "uncorrelated"
    };
    let kind = match observation.kind {
        FixRejectKind::Session => "session_reject",
        FixRejectKind::Business => "business_reject",
    };
    let raw = serde_json::json!({
        "kind": kind,
        "ref_seq": observation.ref_seq,
        "ref_msg_type": observation.ref_msg_type,
        "business_ref_id": observation.business_ref_id,
        "correlated_client_order_id": observation.client_order_id,
        "correlation_method": correlation_method,
        "reason": observation.reason,
        "text": observation.text,
    });
    TransportObservation {
        protocol: "fix",
        protocol_status: kind.to_owned(),
        accepted: Some(false),
        order_terminal: Some(true),
        echoed_client_order_id,
        order_id: None,
        order_status: Some("rejected".into()),
        raw,
        raw_body: Some(observation.raw),
        parse_error: None,
        observed_ts: observation.recv_ts,
        ack_ts: Some(observation.ack_ts),
        fill: None,
    }
}

fn fix_tag(raw: &[u8], tag: &[u8]) -> Option<String> {
    raw.split(|byte| *byte == 1).find_map(|field| {
        let equals = field.iter().position(|byte| *byte == b'=')?;
        let (key, tail) = field.split_at(equals);
        let value = tail.get(1..)?;
        (key == tag).then(|| String::from_utf8_lossy(value).into_owned())
    })
}

fn fix_order_observation(observation: FixOrderObservation) -> TransportObservation {
    match observation {
        FixOrderObservation::Execution(value) => fix_observation(value),
        FixOrderObservation::Reject(value) => fix_reject_observation(value),
    }
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
    pub fn stage(
        &self,
        ticker: &str,
        price_cents: u32,
        count: u32,
        client_order_id: String,
    ) -> WireStaged {
        match self {
            StageWire::Rest => WireStaged::Rest {
                body: OrderBodyV2::ioc_buy_yes(ticker, price_cents, count, client_order_id)
                    .to_bytes(),
            },
            StageWire::Fix(cfg) => WireStaged::Fix {
                template: FixOrderTemplate::ioc_buy_yes(
                    cfg,
                    ticker,
                    price_cents,
                    count,
                    client_order_id,
                ),
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

    /// Evidence-preserving fire-path door. It keeps the full state sequence:
    /// not attempted → wire attempted → exchange observed → accepted/rejected
    /// → terminal/fill observation. No error after `send_ts` can collapse back
    /// into `NotAttempted`.
    pub async fn submit_order(&self, staged: &PreStaged) -> SubmitResult {
        match (self, &staged.wire) {
            (OrderTransport::Rest { client, presigner }, WireStaged::Rest { body }) => {
                let (headers, presign_age_ns) = presigner.get();
                let attempt = client.submit_order_presigned(body.clone(), headers).await;
                let outcome = match attempt.outcome {
                    Ok(observation) => (vec![rest_observation(observation, true)], Ok(())),
                    Err(failure) => {
                        let RestAttemptFailure {
                            error,
                            last_observation,
                        } = failure;
                        let observations = last_observation
                            .map(|observation| vec![rest_observation(observation, false)])
                            .unwrap_or_default();
                        (observations, Err(rest_failure(error)))
                    }
                };
                SubmitResult::Attempted(SendAttempt {
                    protocol: "rest",
                    send_ts: attempt.send_ts,
                    presign_age_ns: Some(presign_age_ns),
                    observations: outcome.0,
                    outcome: outcome.1,
                })
            }
            (OrderTransport::Fix { fix }, WireStaged::Fix { template }) => {
                match fix
                    .submit_order_traced(template, &staged.client_order_id)
                    .await
                {
                    FixTracedSendResult::NotAttempted { error } => {
                        SubmitResult::NotAttempted { error }
                    }
                    FixTracedSendResult::Attempted(FixTracedSendAttempt {
                        send_ts,
                        observations,
                        outcome,
                        ..
                    }) => {
                        let observations = observations
                            .into_iter()
                            .map(fix_order_observation)
                            .collect();
                        let outcome = match outcome {
                            Ok(_) => Ok(()),
                            Err(failure) => Err(TransportFailure {
                                error: failure.error,
                            }),
                        };
                        SubmitResult::Attempted(SendAttempt {
                            protocol: "fix",
                            send_ts,
                            presign_age_ns: None,
                            observations,
                            outcome,
                        })
                    }
                }
            }
            _ => SubmitResult::NotAttempted {
                error: KalshiError::Validation(
                    "prestaged wire form does not match the armed transport".into(),
                ),
            },
        }
    }

    /// The one fire-path method: consume the prestaged wire form, put the
    /// order on the wire, return the normalized ack + timing splits.
    /// Compatibility facade: failures discard the richer attempted evidence;
    /// new executor code must migrate to `submit_order`.
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

#[cfg(test)]
mod tests {
    use super::super::fix::session::OwnedExecReport;
    use super::*;

    #[test]
    fn rest_observation_preserves_exact_fill_strings_and_rejection_state() {
        let observation = RestOrderObservation {
            status_code: 201,
            echoed_client_order_id: Some("client-1".into()),
            order_id: Some("rest-1".into()),
            order_status: Some("filled".into()),
            raw: Some(serde_json::json!({
                "order_id": "rest-1",
                "fill_count": "7.00",
                "remaining_count": "0.00",
                "average_fill_price": "0.9700"
            })),
            raw_body: bytes::Bytes::from_static(b"raw-rest-body"),
            parse_error: None,
            hdrs_ts: MonotonicNs(10),
            ack_ts: MonotonicNs(11),
            http_version: http::Version::HTTP_2,
        };
        let normalized = rest_observation(observation, true);
        let fill = normalized.fill.unwrap();
        assert_eq!(normalized.accepted, Some(true));
        assert_eq!(normalized.order_terminal, Some(true));
        assert_eq!(fill.cumulative_contracts.as_deref(), Some("7.00"));
        assert_eq!(fill.remaining_contracts.as_deref(), Some("0.00"));
        assert_eq!(fill.average_price.as_deref(), Some("0.9700"));
        assert_eq!(fill.price_unit, "dollars");
        assert_eq!(normalized.raw_body.unwrap(), b"raw-rest-body");
        assert_eq!(normalized.ack_ts, Some(MonotonicNs(11)));
    }

    #[test]
    fn incomplete_rest_body_retains_headers_but_has_no_ack() {
        let normalized = rest_observation(
            RestOrderObservation {
                status_code: 503,
                echoed_client_order_id: None,
                order_id: None,
                order_status: None,
                raw: None,
                raw_body: bytes::Bytes::new(),
                parse_error: Some("response body read failed".into()),
                hdrs_ts: MonotonicNs(20),
                ack_ts: MonotonicNs(21),
                http_version: http::Version::HTTP_11,
            },
            false,
        );
        assert_eq!(normalized.protocol_status, "http:503");
        assert_eq!(
            normalized.accepted, None,
            "5xx is delivery-ambiguous, not a rejection"
        );
        assert_eq!(normalized.order_terminal, None);
        assert_eq!(normalized.observed_ts, MonotonicNs(20));
        assert_eq!(normalized.ack_ts, None);
        assert!(normalized.parse_error.is_some());
    }

    #[test]
    fn fix_observation_preserves_partial_fill_and_protocol_status() {
        let normalized = fix_observation(FixExecutionObservation {
            report: OwnedExecReport {
                cl_ord_id: Some("client-1".into()),
                order_id: Some("fix-1".into()),
                status: "partial".into(),
                terminal: false,
                raw: serde_json::json!({
                    "exec_type": "F",
                    "ord_status": "1",
                    "cum_qty": "9",
                    "leaves_qty": "51",
                    "last_qty": "9",
                    "last_px": "96",
                    "avg_px": "96"
                }),
            },
            recv_ts: MonotonicNs(30),
            ack_ts: MonotonicNs(31),
            raw: Vec::new(),
        });
        let fill = normalized.fill.unwrap();
        assert_eq!(normalized.accepted, Some(true));
        assert_eq!(normalized.order_terminal, Some(false));
        assert_eq!(normalized.protocol_status, "exec_type:F;ord_status:1");
        assert_eq!(fill.cumulative_contracts.as_deref(), Some("9"));
        assert_eq!(fill.last_fill_price.as_deref(), Some("96"));
        assert_eq!(fill.price_unit, "cents");
    }
}
