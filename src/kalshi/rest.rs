//! Kalshi REST client (VERIFICATIONS.md Q1/Q2). Order entry is REST-only at
//! our tier; V2 shape: dollar-string prices, bid/ask-on-YES sides, IOC as
//! first-class time_in_force. Responses are parsed defensively (Value +
//! extracted fields) — ack shapes get pinned by the live drills.

use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use serde::Serialize;
use serde_json::Value;

use super::auth::{Headers3, Signer};
use super::{KalshiEnv, KalshiError};
use crate::clock::MonotonicNs;

pub const ORDERS_ENDPOINT: &str = "/portfolio/events/orders";

/// How often the connection warmer touches the orders origin to keep a hot
/// TLS/h2 connection pooled. Strictly below `pool_idle_timeout` (90 s) so the
/// socket is never reaped between a quiet arm and the first fire.
const CONN_WARM_INTERVAL: Duration = Duration::from_secs(60);

#[derive(Clone)]
pub struct RestClient {
    /// Full REST base including /trade-api/v2 (host may be a test mock; the
    /// signed path is always the real /trade-api/v2 one).
    base: String,
    client: reqwest::Client,
    signer: Arc<Signer>,
}

/// Convert integer cents to the V2 dollar-string price. Never floats.
pub fn cents_to_dollar_str(cents: u32) -> String {
    format!("{}.{:02}", cents / 100, cents % 100)
}

/// Contract count as the required fixed-point 2-decimal string ("1.00" —
/// plain "1" is rejected with invalid_parameters, pinned 2026-07-10).
pub fn count_to_fp_str(count: u32) -> String {
    format!("{count}.00")
}

/// Classify a non-negative fixed-point exchange quantity without converting
/// through a float (or truncating its fractional part). `"0.00"` is zero;
/// `"0.50"` and `"1.00"` are non-zero; malformed values remain unknown.
fn fixed_point_is_zero(value: &str) -> Option<bool> {
    let mut saw_digit = false;
    let mut saw_dot = false;
    let mut all_zero = true;
    for byte in value.bytes() {
        match byte {
            b'0'..=b'9' => {
                saw_digit = true;
                all_zero &= byte == b'0';
            }
            b'.' if !saw_dot => saw_dot = true,
            _ => return None,
        }
    }
    saw_digit.then_some(all_zero)
}

/// client_order_id must be UUID-shaped. Deterministic derivation from a
/// semantic seed ("{event}:{kw}:{tier}") keeps idempotency: the same intent
/// always yields the same id, so a retry is server-side deduped.
pub fn deterministic_client_order_id(seed: &str) -> String {
    use rsa::sha2::{Digest, Sha256};
    let h = Sha256::digest(seed.as_bytes());
    format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-4{:01x}{:02x}-8{:01x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        h[0], h[1], h[2], h[3], h[4], h[5],
        h[6] & 0x0f, h[7],
        h[8] & 0x0f, h[9],
        h[10], h[11], h[12], h[13], h[14], h[15]
    )
}

#[derive(Debug, Clone, Serialize)]
pub struct OrderBodyV2 {
    pub ticker: String,
    /// Buying YES = "bid" (VERIFICATIONS Q1; pinned by live drill).
    pub side: &'static str,
    pub count: String,
    pub price: String,
    pub time_in_force: &'static str, // "immediate_or_cancel" | "good_till_canceled"
    /// REQUIRED by the live API (400 without it, pinned 2026-07-10):
    /// "taker_at_cross" for IOC sweeps, "maker" for post-only resting.
    pub self_trade_prevention_type: &'static str,
    pub client_order_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub post_only: Option<bool>,
}

impl OrderBodyV2 {
    pub fn ioc_buy_yes(
        ticker: &str,
        price_cents: u32,
        count: u32,
        client_order_id: String,
    ) -> Self {
        OrderBodyV2 {
            ticker: ticker.to_string(),
            side: "bid",
            count: count_to_fp_str(count),
            price: cents_to_dollar_str(price_cents),
            time_in_force: "immediate_or_cancel",
            self_trade_prevention_type: "taker_at_cross",
            client_order_id,
            post_only: None,
        }
    }

    pub fn resting_buy_yes(
        ticker: &str,
        price_cents: u32,
        count: u32,
        client_order_id: String,
    ) -> Self {
        OrderBodyV2 {
            ticker: ticker.to_string(),
            side: "bid",
            count: count_to_fp_str(count),
            price: cents_to_dollar_str(price_cents),
            time_in_force: "good_till_canceled",
            self_trade_prevention_type: "maker",
            client_order_id,
            post_only: Some(true),
        }
    }

    /// Pre-serialize at arm time (spec §5.7 pre-staging).
    pub fn to_bytes(&self) -> Bytes {
        Bytes::from(serde_json::to_vec(self).expect("order body serializes"))
    }
}

#[derive(Debug, Clone)]
pub struct OrderAck {
    pub status_code: u16,
    pub order_id: Option<String>,
    pub order_status: Option<String>,
    pub raw: Value,
    /// Stamped when response headers arrive (`req.send()` resolves) — the TTFB
    /// split point between network+matching and body read+parse.
    pub hdrs_ts: MonotonicNs,
    pub ack_ts: MonotonicNs,
    /// Negotiated wire protocol (h1 vs h2). Copy — no hot-path allocation.
    pub http_version: http::Version,
}

/// Everything observed after an HTTP order request reached the send boundary.
/// Non-2xx responses are observations too: the exchange rejected the request,
/// which is different from a transport failure where no response arrived.
#[derive(Debug)]
pub struct RestOrderObservation {
    pub status_code: u16,
    /// Client id echoed by the exchange response. This is never inferred from
    /// the request: an order id is trusted only when this value exactly
    /// matches the idempotency key that was sent.
    pub echoed_client_order_id: Option<String>,
    pub order_id: Option<String>,
    pub order_status: Option<String>,
    /// Parsed JSON when the body is valid. `None` never erases `raw_body`.
    pub raw: Option<Value>,
    /// Exact response bytes for forensic replay, including malformed JSON.
    pub raw_body: Bytes,
    pub parse_error: Option<String>,
    pub hdrs_ts: MonotonicNs,
    pub ack_ts: MonotonicNs,
    pub http_version: http::Version,
}

impl RestOrderObservation {
    pub fn accepted(&self) -> bool {
        self.status_code < 300
    }
}

#[derive(Debug)]
pub struct RestAttemptFailure {
    pub error: KalshiError,
    /// Present when response headers arrived but body read failed.
    pub last_observation: Option<RestOrderObservation>,
}

#[derive(Debug)]
pub struct RestSendAttempt {
    /// Stamped immediately before `req.send()`: once present, wire delivery is
    /// ambiguous on failure and reconciliation must use client_order_id.
    pub send_ts: MonotonicNs,
    pub outcome: Result<RestOrderObservation, RestAttemptFailure>,
}

#[derive(Debug, Clone)]
pub struct ExactOrderQueryEvidence {
    pub queried_client_order_id: String,
    pub matched_order: Option<Value>,
    pub status_code: u16,
    /// Exact bytes received, including a prefix when the body stream fails.
    pub raw_body: Bytes,
    /// Parsed only after the complete response body has been consumed.
    /// Incomplete bodies are never promoted into exchange-order evidence even
    /// when their prefix happens to be syntactically valid JSON.
    pub parsed: Option<Value>,
    pub parse_error: Option<String>,
    /// Body-stream failure after response headers arrived. A request failure
    /// before headers remains the outer `Result::Err` because no typed HTTP
    /// response exists to preserve.
    pub read_error: Option<String>,
    /// Earliest local observation of this HTTP response (header arrival), not
    /// the later instant at which its body happened to finish or fail.
    pub observed_ts: MonotonicNs,
}

impl ExactOrderQueryEvidence {
    /// Human-readable reason suitable for a durable `ReconcileUnresolved`
    /// detail. The typed fields remain authoritative for programmatic use.
    pub fn unresolved_detail(&self) -> String {
        let mut detail = format!("HTTP {}", self.status_code);
        if let Some(error) = &self.read_error {
            detail.push_str(" body read failed: ");
            detail.push_str(error);
        } else if let Some(error) = &self.parse_error {
            detail.push_str(" JSON parse failed: ");
            detail.push_str(error);
        } else if self.status_code >= 300 {
            detail.push_str(" non-success response cannot establish reconciliation");
        } else if self.matched_order.is_none() {
            detail.push_str(" contained no exact echoed client_order_id");
        }
        detail
    }
}

impl RestClient {
    pub fn new(env: &KalshiEnv, signer: Arc<Signer>) -> Result<Self, KalshiError> {
        Self::with_base(env.rest_base.to_string(), signer)
    }

    /// Test seam: point at a wiremock host. Signing still uses the canonical
    /// /trade-api/v2 path so header assertions match production behavior.
    pub fn with_base(base: String, signer: Arc<Signer>) -> Result<Self, KalshiError> {
        Self::with_base_and_timeout(base, signer, Duration::from_secs(10))
    }

    pub fn with_base_and_timeout(
        base: String,
        signer: Arc<Signer>,
        timeout: Duration,
    ) -> Result<Self, KalshiError> {
        let client = reqwest::Client::builder()
            .timeout(timeout)
            .pool_idle_timeout(Duration::from_secs(90))
            .tcp_keepalive(Duration::from_secs(30))
            // Nagle off: an order is one small packet; coalescing buys nothing
            // but latency. Explicit (self-documenting; guards a reqwest default
            // change) — see VERIFICATIONS.md.
            .tcp_nodelay(true)
            // Keep a negotiated h2 connection alive between orders so a burst
            // multiplexes on one warm socket instead of opening N cold ones.
            // ALPN still negotiates — do NOT force prior-knowledge (it would
            // break the h1 wiremock tests and any h1 endpoint).
            .http2_keep_alive_interval(Duration::from_secs(30))
            .http2_keep_alive_while_idle(true)
            .build()?;
        Ok(RestClient {
            base,
            client,
            signer,
        })
    }

    pub fn signer(&self) -> &Arc<Signer> {
        &self.signer
    }

    /// Stable endpoint identity for evidence receipts. It contains no
    /// credential material.
    pub fn endpoint_id(&self) -> String {
        format!("rest:{}", self.base)
    }

    /// Touch the orders origin to keep a hot TLS/h2 connection pooled (response
    /// dropped). `/exchange/status` is public and avoids order-relevant
    /// rate-limit token cost (VERIFICATIONS Q2). Never on the fire path.
    pub async fn warm(&self) {
        match self
            .client
            .get(format!("{}/exchange/status", self.base))
            .send()
            .await
        {
            Ok(resp) => tracing::debug!(version = ?resp.version(), "connection warm ping ok"),
            Err(e) => tracing::debug!("connection warm ping failed: {e}"),
        }
    }

    /// Spawn the connection warmer: every `CONN_WARM_INTERVAL`, ping the orders
    /// origin so the pooled connection never goes cold between arm and the first
    /// fire. Mirrors `Presigner::spawn_refresher`; wired only in `main.rs::arm`.
    pub fn spawn_conn_warmer(
        &self,
        handle: &tokio::runtime::Handle,
    ) -> tokio::task::JoinHandle<()> {
        let client = self.clone();
        handle.spawn(async move {
            let mut tick = tokio::time::interval(CONN_WARM_INTERVAL);
            tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                tick.tick().await;
                client.warm().await;
            }
        })
    }

    fn signed_path(endpoint: &str) -> String {
        // Sign the path from the API root, without the query string (Q3).
        let no_query = endpoint.split('?').next().unwrap();
        format!("/trade-api/v2{no_query}")
    }

    async fn signed_get(&self, endpoint: &str) -> Result<Value, KalshiError> {
        let headers = self.signer.headers_now("GET", &Self::signed_path(endpoint));
        let mut req = self.client.get(format!("{}{endpoint}", self.base));
        for (k, v) in headers {
            req = req.header(k, v);
        }
        let resp = req.send().await?;
        let status = resp.status().as_u16();
        let body: Value = resp.json().await.unwrap_or(Value::Null);
        if status >= 300 {
            return Err(KalshiError::Api {
                status,
                body: body.to_string(),
            });
        }
        Ok(body)
    }

    // -- read-only ---------------------------------------------------------

    pub async fn balance(&self) -> Result<Value, KalshiError> {
        self.signed_get("/portfolio/balance").await
    }

    pub async fn market(&self, ticker: &str) -> Result<Value, KalshiError> {
        self.signed_get(&format!("/markets/{ticker}")).await
    }

    pub async fn series(&self, series_ticker: &str) -> Result<Value, KalshiError> {
        self.signed_get(&format!("/series/{series_ticker}")).await
    }

    /// List ALL markets in an event, following the cursor. Read-only. Each
    /// returned object is a full market (includes `yes_sub_title`,
    /// `yes_ask_dollars`, `rules_primary`/`rules_secondary`, `can_close_early`).
    /// The query string is not signed (`signed_path` strips it), so the cursor
    /// is safe to append. Used by the event-prep stage (`earshot prepare`).
    pub async fn event_markets(&self, event_ticker: &str) -> Result<Vec<Value>, KalshiError> {
        let mut out = Vec::new();
        let mut cursor: Option<String> = None;
        loop {
            let mut ep = format!("/markets?event_ticker={event_ticker}&limit=200");
            if let Some(c) = &cursor {
                ep.push_str(&format!("&cursor={c}"));
            }
            let v = self.signed_get(&ep).await?;
            if let Some(arr) = v["markets"].as_array() {
                out.extend(arr.iter().cloned());
            }
            match v["cursor"].as_str() {
                Some(c) if !c.is_empty() => cursor = Some(c.to_string()),
                _ => break,
            }
        }
        Ok(out)
    }

    pub async fn account_limits(&self) -> Result<Value, KalshiError> {
        self.signed_get("/account/limits").await
    }

    pub async fn endpoint_costs(&self) -> Result<Value, KalshiError> {
        self.signed_get("/account/endpoint_costs").await
    }

    pub async fn open_orders(&self) -> Result<Vec<Value>, KalshiError> {
        let v = self.signed_get("/portfolio/orders?status=resting").await?;
        Ok(v["orders"].as_array().cloned().unwrap_or_default())
    }

    pub async fn order_by_client_id(
        &self,
        client_order_id: &str,
    ) -> Result<Option<Value>, KalshiError> {
        let evidence = self.order_by_client_id_evidence(client_order_id).await?;
        if evidence.status_code >= 300
            || evidence.read_error.is_some()
            || evidence.parse_error.is_some()
        {
            return Err(KalshiError::Api {
                status: evidence.status_code,
                body: evidence.unresolved_detail(),
            });
        }
        Ok(evidence.matched_order)
    }

    /// Evidence-preserving exact-client-ID query used by crash recovery. Once
    /// response headers arrive this returns typed evidence even for non-2xx or
    /// body-stream failure; exact bytes (or their received prefix), status,
    /// and separate read/parse detail survive. The server-side filter is never
    /// trusted without an exact echoed ID. Only failures before headers remain
    /// the outer `Result::Err`.
    pub async fn order_by_client_id_evidence(
        &self,
        client_order_id: &str,
    ) -> Result<ExactOrderQueryEvidence, KalshiError> {
        let endpoint = format!("/portfolio/orders?client_order_id={client_order_id}");
        let headers = self
            .signer
            .headers_now("GET", &Self::signed_path(&endpoint));
        let mut request = self.client.get(format!("{}{endpoint}", self.base));
        for (key, value) in headers {
            request = request.header(key, value);
        }
        // A failure here happened before response headers existed. There is no
        // status or response byte prefix to type, so it remains the sole outer
        // `Err` case for this evidence-preserving query.
        let mut response = request.send().await?;
        let status_code = response.status().as_u16();
        let observed_ts = MonotonicNs::now();
        let mut raw_body = Vec::new();
        let read_error = loop {
            match response.chunk().await {
                Ok(Some(chunk)) => raw_body.extend_from_slice(&chunk),
                Ok(None) => break None,
                Err(error) => break Some(error.to_string()),
            }
        };
        let raw_body = Bytes::from(raw_body);
        let (parsed, parse_error) = if read_error.is_none() {
            match serde_json::from_slice::<Value>(&raw_body) {
                Ok(value) => (Some(value), None),
                Err(error) => (None, Some(error.to_string())),
            }
        } else {
            // Never treat an incomplete prefix as a complete exchange reply.
            (None, None)
        };
        let v = parsed.as_ref().unwrap_or(&Value::Null);
        // NEVER trust the server to have applied the filter. If the query param
        // is ignored — which it demonstrably can be, e.g. for a non-UUID id
        // (VERIFICATIONS.md: client_order_id must be UUID-shaped) — `orders` is
        // the WHOLE account and `.first()` returns an ARBITRARY order.
        //
        // That is not a cosmetic bug. `executor.rs` calls this on an ambiguous
        // send error and, on Some, does `registry.register(order_id)`. The kill
        // switch cancels ONLY OrderRegistry ids precisely because this account
        // carries unrelated resting orders from other systems (CLAUDE.md), so an
        // unfiltered match would silently widen kill scope to a FOREIGN order.
        // Re-check the id we asked for; the server's filtering is an optimisation,
        // never the guarantee.
        //
        // Conservative rule: accept ONLY an exact echoed client id. A missing
        // id is ambiguous, never permission to adopt an arbitrary account row.
        // Returning None leaves the original send attempt unresolved; it does
        // not prove absence and must not widen the session kill boundary.
        let matched_order = (status_code < 300 && read_error.is_none())
            .then(|| v["orders"].as_array())
            .flatten()
            .and_then(|a| {
                let mine = a
                    .iter()
                    .find(|o| o["client_order_id"].as_str() == Some(client_order_id));
                if mine.is_none() && !a.is_empty() {
                    tracing::warn!(
                        returned = a.len(),
                        "order query returned {} order(s), NONE matching the requested \
                         client_order_id exactly (missing ids are also untrusted) — \
                         refusing to adopt a foreign or unverifiable order id",
                        a.len()
                    );
                }
                mine.cloned()
            });
        Ok(ExactOrderQueryEvidence {
            queried_client_order_id: client_order_id.to_owned(),
            matched_order,
            status_code,
            raw_body,
            parsed,
            parse_error,
            read_error,
            observed_ts,
        })
    }

    // -- order entry -------------------------------------------------------

    /// Rich order-submit door. It never discards `send_ts`, HTTP rejection,
    /// response headers, malformed bodies, or a body-read failure after
    /// headers. Callers can therefore distinguish wire-attempted from
    /// exchange-observed and accepted.
    pub async fn submit_order_presigned(&self, body: Bytes, headers: Headers3) -> RestSendAttempt {
        let mut req = self
            .client
            .post(format!("{}{ORDERS_ENDPOINT}", self.base))
            .header("Content-Type", "application/json")
            .body(body);
        for (k, v) in headers {
            req = req.header(k, v);
        }
        let send_ts = MonotonicNs::now();
        let mut resp = match req.send().await {
            Ok(resp) => resp,
            Err(error) => {
                return RestSendAttempt {
                    send_ts,
                    outcome: Err(RestAttemptFailure {
                        error: KalshiError::Http(error),
                        last_observation: None,
                    }),
                };
            }
        };
        // Headers/TTFB have arrived — the natural split between network+matching
        // (send→hdrs) and body read+parse (hdrs→ack).
        let hdrs_ts = MonotonicNs::now();
        let status_code = resp.status().as_u16();
        let http_version = resp.version();
        // Read in chunks so a truncated response preserves the exact prefix
        // that did arrive. `Response::bytes()` discards that prefix on error,
        // making an ambiguous exchange observation unauditable.
        let mut raw_body = Vec::new();
        loop {
            match resp.chunk().await {
                Ok(Some(chunk)) => raw_body.extend_from_slice(&chunk),
                Ok(None) => break,
                Err(error) => {
                    let ack_ts = MonotonicNs::now();
                    return RestSendAttempt {
                        send_ts,
                        outcome: Err(RestAttemptFailure {
                            error: KalshiError::Http(error),
                            last_observation: Some(RestOrderObservation {
                                status_code,
                                echoed_client_order_id: None,
                                order_id: None,
                                order_status: None,
                                raw: None,
                                raw_body: Bytes::from(raw_body),
                                parse_error: Some("response body read failed".into()),
                                hdrs_ts,
                                ack_ts,
                                http_version,
                            }),
                        }),
                    };
                }
            }
        }
        let raw_body = Bytes::from(raw_body);
        let (raw, parse_error) = match serde_json::from_slice::<Value>(&raw_body) {
            Ok(raw) => (Some(raw), None),
            Err(error) => (None, Some(error.to_string())),
        };
        // `?` defers Debug formatting to the subscriber — zero alloc when the
        // debug level is off (the hot path).
        tracing::debug!(status_code, http_version = ?http_version, "order ack");
        // Ack shape pinned live 2026-07-10: a 201 IOC ack has NO "order"
        // wrapper and NO "status" — top-level fill_count / remaining_count /
        // average_fill_price (fp strings). Older/mocked shapes may still wrap.
        let (echoed_client_order_id, order_id, order_status) = raw
            .as_ref()
            .map(|raw| {
                let order = if raw.get("order").is_some() {
                    &raw["order"]
                } else {
                    raw
                };
                let status = order["status"].as_str().map(String::from).or_else(|| {
                    let zero = |key: &str| order[key].as_str().and_then(fixed_point_is_zero);
                    match (zero("fill_count"), zero("remaining_count")) {
                        (Some(false), Some(true)) => Some("filled".into()),
                        (Some(true), Some(true)) => Some("canceled".into()),
                        (Some(_), Some(_)) => Some("partial".into()),
                        _ => None,
                    }
                });
                (
                    order["client_order_id"].as_str().map(String::from),
                    order["order_id"].as_str().map(String::from),
                    status,
                )
            })
            .unwrap_or((None, None, None));
        // `ack_ts` is the fully-consumed, fully-parsed/extracted boundary.
        // Header arrival remains separately captured by `hdrs_ts`.
        let ack_ts = MonotonicNs::now();
        RestSendAttempt {
            send_ts,
            outcome: Ok(RestOrderObservation {
                status_code,
                echoed_client_order_id,
                order_id,
                order_status,
                raw,
                raw_body,
                parse_error,
                hdrs_ts,
                ack_ts,
                http_version,
            }),
        }
    }

    /// Compatibility door. It intentionally collapses the rich attempted
    /// result back into the historical `Result`; new runtime code must use
    /// `submit_order_presigned` so failures retain `send_ts` and observations.
    pub async fn create_order_presigned(
        &self,
        body: Bytes,
        headers: Headers3,
    ) -> Result<(OrderAck, MonotonicNs), KalshiError> {
        let attempt = self.submit_order_presigned(body, headers).await;
        match attempt.outcome {
            Ok(observation) if observation.accepted() => Ok((
                OrderAck {
                    status_code: observation.status_code,
                    order_id: observation.order_id,
                    order_status: observation.order_status,
                    raw: observation.raw.unwrap_or(Value::Null),
                    hdrs_ts: observation.hdrs_ts,
                    ack_ts: observation.ack_ts,
                    http_version: observation.http_version,
                },
                attempt.send_ts,
            )),
            Ok(observation) => Err(KalshiError::Api {
                status: observation.status_code,
                body: String::from_utf8_lossy(&observation.raw_body).into_owned(),
            }),
            Err(failure) => Err(failure.error),
        }
    }

    /// Convenience (non-hot-path) order create with inline signing.
    pub async fn create_order(
        &self,
        body: &OrderBodyV2,
    ) -> Result<(OrderAck, MonotonicNs), KalshiError> {
        let headers = self
            .signer
            .headers_now("POST", &Self::signed_path(ORDERS_ENDPOINT));
        self.create_order_presigned(body.to_bytes(), headers).await
    }

    /// V2 cancel (the legacy DELETE /portfolio/orders/{id} returns 410,
    /// pinned live 2026-07-10). Response: {order_id, client_order_id,
    /// reduced_by, ts_ms}.
    pub async fn cancel_order(&self, order_id: &str) -> Result<Value, KalshiError> {
        let endpoint = format!("{ORDERS_ENDPOINT}/{order_id}");
        let headers = self
            .signer
            .headers_now("DELETE", &Self::signed_path(&endpoint));
        let mut req = self.client.delete(format!("{}{endpoint}", self.base));
        for (k, v) in headers {
            req = req.header(k, v);
        }
        let resp = req.send().await?;
        let status = resp.status().as_u16();
        let body: Value = resp.json().await.unwrap_or(Value::Null);
        if status >= 300 {
            return Err(KalshiError::Api {
                status,
                body: body.to_string(),
            });
        }
        Ok(body)
    }

    /// Cancel exactly the given order ids (the session registry). NEVER an
    /// account-wide cancel-all: the account carries resting orders from other
    /// systems that this process must not touch. Loop-cancel (2 tokens each)
    /// is fine at kill-switch scale.
    pub async fn cancel_orders(&self, order_ids: &[String]) -> usize {
        let mut cancelled = 0;
        for id in order_ids {
            match self.cancel_order(id).await {
                Ok(resp) => {
                    cancelled += 1;
                    tracing::info!(order_id = %id, reduced_by = %resp["reduced_by"], "cancelled");
                }
                Err(e) => tracing::error!(order_id = %id, "cancel failed: {e}"),
            }
        }
        cancelled
    }

    /// Cancel the exact session-owned ids and return the ids whose DELETE was
    /// acknowledged. Callers that must fail closed use the remainder as an
    /// explicit unresolved set and may retry within a bounded drain window.
    pub async fn cancel_orders_evidence(&self, order_ids: &[String]) -> Vec<String> {
        let mut cancelled = Vec::new();
        for id in order_ids {
            match self.cancel_order(id).await {
                Ok(resp) => {
                    cancelled.push(id.clone());
                    tracing::info!(order_id = %id, reduced_by = %resp["reduced_by"], "cancelled");
                }
                Err(error) => {
                    tracing::error!(order_id = %id, %error, "cancel failed; remains unresolved")
                }
            }
        }
        cancelled
    }

    /// Estimate local-vs-server clock offset from the HTTP Date header
    /// (risk module arm-time guard). Positive = server ahead.
    pub async fn clock_offset_estimate(&self) -> Result<f64, KalshiError> {
        let t0 = std::time::SystemTime::now();
        let resp = self
            .client
            .get(format!("{}/exchange/status", self.base))
            .send()
            .await?;
        let rtt = t0.elapsed().unwrap_or_default();
        let date = resp
            .headers()
            .get(reqwest::header::DATE)
            .and_then(|v| v.to_str().ok())
            .and_then(|s| httpdate_parse(s))
            .ok_or_else(|| KalshiError::Ws("no Date header".into()))?;
        let local_mid = t0 + rtt / 2;
        let offset = date
            .duration_since(local_mid)
            .map(|d| d.as_secs_f64())
            .unwrap_or_else(|e| -e.duration().as_secs_f64());
        Ok(offset)
    }
}

/// Minimal RFC 7231 IMF-fixdate parser ("Sun, 06 Nov 1994 08:49:37 GMT") —
/// avoids a dependency for one header.
fn httpdate_parse(s: &str) -> Option<std::time::SystemTime> {
    let parts: Vec<&str> = s.split_whitespace().collect();
    if parts.len() != 6 {
        return None;
    }
    let day: u64 = parts[1].parse().ok()?;
    let month = match parts[2] {
        "Jan" => 1,
        "Feb" => 2,
        "Mar" => 3,
        "Apr" => 4,
        "May" => 5,
        "Jun" => 6,
        "Jul" => 7,
        "Aug" => 8,
        "Sep" => 9,
        "Oct" => 10,
        "Nov" => 11,
        "Dec" => 12,
        _ => return None,
    };
    let year: u64 = parts[3].parse().ok()?;
    let hms: Vec<u64> = parts[4].split(':').filter_map(|x| x.parse().ok()).collect();
    if hms.len() != 3 {
        return None;
    }
    // Days since epoch (civil-from-days algorithm, valid for years >= 1970).
    let y = if month <= 2 { year - 1 } else { year };
    let era = y / 400;
    let yoe = y - era * 400;
    let doy = (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146097 + doe - 719468;
    let secs = days * 86400 + hms[0] * 3600 + hms[1] * 60 + hms[2];
    Some(std::time::UNIX_EPOCH + Duration::from_secs(secs))
}

#[cfg(test)]
mod tests {
    use super::*;
    use rsa::pkcs1::EncodeRsaPrivateKey;
    use std::sync::OnceLock;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn test_signer() -> Arc<Signer> {
        static PEM: OnceLock<String> = OnceLock::new();
        let pem = PEM.get_or_init(|| {
            let key = rsa::RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 2048).unwrap();
            key.to_pkcs1_pem(rsa::pkcs8::LineEnding::LF)
                .unwrap()
                .to_string()
        });
        Arc::new(Signer::from_pem("rest-test-key".into(), pem).unwrap())
    }

    fn order_headers(signer: &Signer) -> Headers3 {
        signer.headers_now("POST", "/trade-api/v2/portfolio/events/orders")
    }

    #[test]
    fn cents_format_never_floats() {
        assert_eq!(cents_to_dollar_str(1), "0.01");
        assert_eq!(cents_to_dollar_str(97), "0.97");
        assert_eq!(cents_to_dollar_str(100), "1.00");
        assert_eq!(cents_to_dollar_str(50), "0.50");
    }

    #[test]
    fn fixed_point_zero_test_never_truncates_fractional_fills() {
        assert_eq!(fixed_point_is_zero("0.00"), Some(true));
        assert_eq!(fixed_point_is_zero("0"), Some(true));
        assert_eq!(fixed_point_is_zero("0.50"), Some(false));
        assert_eq!(fixed_point_is_zero("1.00"), Some(false));
        assert_eq!(fixed_point_is_zero("-0.00"), None);
        assert_eq!(fixed_point_is_zero("garbage"), None);
    }

    #[test]
    fn ioc_body_shape_matches_v2() {
        let b = OrderBodyV2::ioc_buy_yes("KX-TEST", 97, 60, "ev:0:Fast".into());
        let v: Value = serde_json::from_slice(&b.to_bytes()).unwrap();
        assert_eq!(v["side"], "bid");
        assert_eq!(v["price"], "0.97");
        assert_eq!(v["count"], "60.00");
        assert_eq!(v["time_in_force"], "immediate_or_cancel");
        assert_eq!(v["self_trade_prevention_type"], "taker_at_cross");
        assert_eq!(v["client_order_id"], "ev:0:Fast");
        assert!(v.get("post_only").is_none());
    }

    #[test]
    fn signed_path_strips_query_and_keeps_prefix() {
        assert_eq!(
            RestClient::signed_path("/portfolio/orders?client_order_id=x"),
            "/trade-api/v2/portfolio/orders"
        );
    }

    #[test]
    fn httpdate_parses_imf_fixdate() {
        let t = httpdate_parse("Sun, 06 Nov 1994 08:49:37 GMT").unwrap();
        let secs = t.duration_since(std::time::UNIX_EPOCH).unwrap().as_secs();
        assert_eq!(secs, 784111777);
    }

    #[tokio::test]
    async fn rich_submit_keeps_http_rejection_as_exchange_observation() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path(ORDERS_ENDPOINT))
            .respond_with(
                ResponseTemplate::new(422)
                    .set_body_raw("{\"error\":\"market_closed\"}", "application/json"),
            )
            .mount(&server)
            .await;
        let signer = test_signer();
        let client = RestClient::with_base(server.uri(), signer.clone()).unwrap();
        let attempt = client
            .submit_order_presigned(Bytes::from_static(b"{}"), order_headers(&signer))
            .await;
        let observation = attempt.outcome.expect("HTTP rejection is still observed");

        assert!(attempt.send_ts.0 > 0);
        assert!(!observation.accepted());
        assert_eq!(observation.status_code, 422);
        assert_eq!(observation.raw.as_ref().unwrap()["error"], "market_closed");
        assert_eq!(
            observation.raw_body,
            Bytes::from_static(b"{\"error\":\"market_closed\"}")
        );
        assert!(observation.hdrs_ts >= attempt.send_ts);
        assert!(observation.ack_ts >= observation.hdrs_ts);
    }

    #[tokio::test]
    async fn rich_submit_preserves_malformed_success_body() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path(ORDERS_ENDPOINT))
            .respond_with(ResponseTemplate::new(201).set_body_raw("not-json", "application/json"))
            .mount(&server)
            .await;
        let signer = test_signer();
        let client = RestClient::with_base(server.uri(), signer.clone()).unwrap();
        let attempt = client
            .submit_order_presigned(Bytes::from_static(b"{}"), order_headers(&signer))
            .await;
        let observation = attempt.outcome.unwrap();

        assert!(observation.accepted());
        assert!(observation.raw.is_none());
        assert!(observation.parse_error.is_some());
        assert_eq!(observation.raw_body, Bytes::from_static(b"not-json"));
    }

    #[tokio::test]
    async fn rich_submit_keeps_send_timestamp_on_timeout() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path(ORDERS_ENDPOINT))
            .respond_with(ResponseTemplate::new(201).set_delay(Duration::from_millis(100)))
            .mount(&server)
            .await;
        let signer = test_signer();
        let client = RestClient::with_base_and_timeout(
            server.uri(),
            signer.clone(),
            Duration::from_millis(10),
        )
        .unwrap();
        let attempt = client
            .submit_order_presigned(Bytes::from_static(b"{}"), order_headers(&signer))
            .await;
        let failure = attempt.outcome.unwrap_err();

        assert!(attempt.send_ts.0 > 0);
        assert!(failure.last_observation.is_none());
    }

    #[tokio::test]
    async fn body_truncation_retains_response_headers_as_last_observation() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = vec![0u8; 4096];
            let _ = socket.read(&mut request).await.unwrap();
            socket
                .write_all(
                    b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{}",
                )
                .await
                .unwrap();
        });
        let signer = test_signer();
        let client = RestClient::with_base_and_timeout(
            format!("http://{addr}"),
            signer.clone(),
            Duration::from_secs(1),
        )
        .unwrap();
        let attempt = client
            .submit_order_presigned(Bytes::from_static(b"{}"), order_headers(&signer))
            .await;
        let failure = attempt.outcome.unwrap_err();
        let head = failure
            .last_observation
            .expect("response head was discarded");

        assert!(attempt.send_ts.0 > 0);
        assert_eq!(head.status_code, 201);
        assert_eq!(head.raw_body, Bytes::from_static(b"{}"));
        assert!(head.raw.is_none());
        assert_eq!(
            head.parse_error.as_deref(),
            Some("response body read failed")
        );
        assert!(head.hdrs_ts >= attempt.send_ts);
        server.await.unwrap();
    }

    #[tokio::test]
    async fn exact_id_query_keeps_non_success_response_as_typed_evidence() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/portfolio/orders"))
            .respond_with(ResponseTemplate::new(503).set_body_raw(
                "{\"error\":\"temporarily_unavailable\"}",
                "application/json",
            ))
            .mount(&server)
            .await;
        let client = RestClient::with_base(server.uri(), test_signer()).unwrap();

        let evidence = client
            .order_by_client_id_evidence("00000000-0000-4000-8000-000000000001")
            .await
            .expect("response headers make this typed evidence, not a transport error");

        assert_eq!(evidence.status_code, 503);
        assert_eq!(
            evidence.raw_body,
            Bytes::from_static(b"{\"error\":\"temporarily_unavailable\"}")
        );
        assert_eq!(
            evidence.parsed.as_ref().unwrap()["error"],
            "temporarily_unavailable"
        );
        assert!(evidence.parse_error.is_none());
        assert!(evidence.read_error.is_none());
        assert!(evidence.matched_order.is_none());
        assert_eq!(
            evidence.unresolved_detail(),
            "HTTP 503 non-success response cannot establish reconciliation"
        );
    }

    #[tokio::test]
    async fn exact_id_query_keeps_malformed_complete_body_and_parse_detail() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/portfolio/orders"))
            .respond_with(ResponseTemplate::new(200).set_body_raw("not-json", "application/json"))
            .mount(&server)
            .await;
        let client = RestClient::with_base(server.uri(), test_signer()).unwrap();

        let evidence = client
            .order_by_client_id_evidence("00000000-0000-4000-8000-000000000002")
            .await
            .unwrap();

        assert_eq!(evidence.status_code, 200);
        assert_eq!(evidence.raw_body, Bytes::from_static(b"not-json"));
        assert!(evidence.parsed.is_none());
        assert!(evidence.parse_error.is_some());
        assert!(evidence.read_error.is_none());
        assert!(evidence
            .unresolved_detail()
            .contains("HTTP 200 JSON parse failed"));
    }

    #[tokio::test]
    async fn exact_id_query_keeps_partial_body_and_read_detail() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = vec![0u8; 4096];
            let _ = socket.read(&mut request).await.unwrap();
            socket
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{\"orders\":[",
                )
                .await
                .unwrap();
        });
        let client = RestClient::with_base_and_timeout(
            format!("http://{addr}"),
            test_signer(),
            Duration::from_secs(1),
        )
        .unwrap();

        let evidence = client
            .order_by_client_id_evidence("00000000-0000-4000-8000-000000000003")
            .await
            .expect("headers and partial bytes must survive the body error");

        assert_eq!(evidence.status_code, 200);
        assert_eq!(evidence.raw_body, Bytes::from_static(b"{\"orders\":["));
        assert!(evidence.parsed.is_none());
        assert!(evidence.parse_error.is_none());
        assert!(evidence
            .read_error
            .as_deref()
            .is_some_and(|error| !error.is_empty()));
        assert!(evidence.matched_order.is_none());
        assert!(evidence
            .unresolved_detail()
            .contains("HTTP 200 body read failed"));
        server.await.unwrap();
    }

    #[tokio::test]
    async fn exact_id_query_failure_before_headers_remains_transport_error() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = vec![0u8; 4096];
            let _ = socket.read(&mut request).await.unwrap();
            // Close without an HTTP status line: there is no response evidence
            // that `ExactOrderQueryEvidence` could truthfully represent.
        });
        let client = RestClient::with_base_and_timeout(
            format!("http://{addr}"),
            test_signer(),
            Duration::from_secs(1),
        )
        .unwrap();

        let result = client
            .order_by_client_id_evidence("00000000-0000-4000-8000-000000000004")
            .await;

        assert!(matches!(result, Err(KalshiError::Http(_))));
        server.await.unwrap();
    }
}
