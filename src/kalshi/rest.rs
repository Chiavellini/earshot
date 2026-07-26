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
    pub fn ioc_buy_yes(ticker: &str, price_cents: u32, count: u32, client_order_id: String) -> Self {
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

    pub fn resting_buy_yes(ticker: &str, price_cents: u32, count: u32, client_order_id: String) -> Self {
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
        Ok(RestClient { base, client, signer })
    }

    pub fn signer(&self) -> &Arc<Signer> {
        &self.signer
    }

    /// Touch the orders origin to keep a hot TLS/h2 connection pooled (response
    /// dropped). `/exchange/status` is public and avoids order-relevant
    /// rate-limit token cost (VERIFICATIONS Q2). Never on the fire path.
    pub async fn warm(&self) {
        match self.client.get(format!("{}/exchange/status", self.base)).send().await {
            Ok(resp) => tracing::debug!(version = ?resp.version(), "connection warm ping ok"),
            Err(e) => tracing::debug!("connection warm ping failed: {e}"),
        }
    }

    /// Spawn the connection warmer: every `CONN_WARM_INTERVAL`, ping the orders
    /// origin so the pooled connection never goes cold between arm and the first
    /// fire. Mirrors `Presigner::spawn_refresher`; wired only in `main.rs::arm`.
    pub fn spawn_conn_warmer(&self, handle: &tokio::runtime::Handle) -> tokio::task::JoinHandle<()> {
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
            return Err(KalshiError::Api { status, body: body.to_string() });
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

    pub async fn order_by_client_id(&self, client_order_id: &str) -> Result<Option<Value>, KalshiError> {
        let v = self
            .signed_get(&format!("/portfolio/orders?client_order_id={client_order_id}"))
            .await?;
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
        // Conservative rule: reject only a MISMATCH, never an absence. If the
        // response echoes `client_order_id` and it differs, that order is not
        // ours and must not be returned. If the field is absent we cannot tell,
        // so keep the previous behaviour (first entry) rather than turning a
        // landed order into "not on book" — a false negative here mis-records a
        // real fill.
        Ok(v["orders"].as_array().and_then(|a| {
            let mine = a.iter().find(|o| match o["client_order_id"].as_str() {
                Some(id) => id == client_order_id,
                None => true,
            });
            if let Some(o) = mine {
                if o["client_order_id"].as_str().is_none() {
                    tracing::warn!(
                        "order query response omits client_order_id — cannot verify \
                         the server filtered; accepting first entry"
                    );
                }
            } else if !a.is_empty() {
                tracing::warn!(
                    returned = a.len(),
                    "order query returned {} order(s), NONE matching the requested \
                     client_order_id — server filter ignored; refusing to adopt a \
                     foreign order id",
                    a.len()
                );
            }
            mine.cloned()
        }))
    }

    // -- order entry -------------------------------------------------------

    /// POST a pre-serialized order body with pre-computed headers (hot path).
    /// `send_ts` is stamped immediately before the request hits the wire.
    pub async fn create_order_presigned(
        &self,
        body: Bytes,
        headers: Headers3,
    ) -> Result<(OrderAck, MonotonicNs), KalshiError> {
        let mut req = self
            .client
            .post(format!("{}{ORDERS_ENDPOINT}", self.base))
            .header("Content-Type", "application/json")
            .body(body);
        for (k, v) in headers {
            req = req.header(k, v);
        }
        let send_ts = MonotonicNs::now();
        let resp = req.send().await?;
        // Headers/TTFB have arrived — the natural split between network+matching
        // (send→hdrs) and body read+parse (hdrs→ack).
        let hdrs_ts = MonotonicNs::now();
        let status_code = resp.status().as_u16();
        let http_version = resp.version();
        let raw: Value = resp.json().await.unwrap_or(Value::Null);
        let ack_ts = MonotonicNs::now();
        // `?` defers Debug formatting to the subscriber — zero alloc when the
        // debug level is off (the hot path).
        tracing::debug!(status_code, http_version = ?http_version, "order ack");
        if status_code >= 300 {
            return Err(KalshiError::Api { status: status_code, body: raw.to_string() });
        }
        // Ack shape pinned live 2026-07-10: a 201 IOC ack has NO "order"
        // wrapper and NO "status" — top-level fill_count / remaining_count /
        // average_fill_price (fp strings). Older/mocked shapes may still wrap.
        let order = if raw.get("order").is_some() { &raw["order"] } else { &raw };
        let order_status = order["status"].as_str().map(String::from).or_else(|| {
            let fp = |k: &str| order[k].as_str().and_then(|s| s.split('.').next()?.parse::<i64>().ok());
            match (fp("fill_count"), fp("remaining_count")) {
                (Some(fill), Some(0)) if fill > 0 => Some("filled".into()),
                (Some(0), Some(0)) => Some("canceled".into()), // IOC, nothing crossed
                (Some(_), Some(_)) => Some("partial".into()),
                _ => None,
            }
        });
        Ok((
            OrderAck {
                status_code,
                order_id: order["order_id"].as_str().map(String::from),
                order_status,
                raw: raw.clone(),
                hdrs_ts,
                ack_ts,
                http_version,
            },
            send_ts,
        ))
    }

    /// Convenience (non-hot-path) order create with inline signing.
    pub async fn create_order(&self, body: &OrderBodyV2) -> Result<(OrderAck, MonotonicNs), KalshiError> {
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
        let headers = self.signer.headers_now("DELETE", &Self::signed_path(&endpoint));
        let mut req = self.client.delete(format!("{}{endpoint}", self.base));
        for (k, v) in headers {
            req = req.header(k, v);
        }
        let resp = req.send().await?;
        let status = resp.status().as_u16();
        let body: Value = resp.json().await.unwrap_or(Value::Null);
        if status >= 300 {
            return Err(KalshiError::Api { status, body: body.to_string() });
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

    /// Estimate local-vs-server clock offset from the HTTP Date header
    /// (risk module arm-time guard). Positive = server ahead.
    pub async fn clock_offset_estimate(&self) -> Result<f64, KalshiError> {
        let t0 = std::time::SystemTime::now();
        let resp = self.client.get(format!("{}/exchange/status", self.base)).send().await?;
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
        "Jan" => 1, "Feb" => 2, "Mar" => 3, "Apr" => 4, "May" => 5, "Jun" => 6,
        "Jul" => 7, "Aug" => 8, "Sep" => 9, "Oct" => 10, "Nov" => 11, "Dec" => 12,
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

    #[test]
    fn cents_format_never_floats() {
        assert_eq!(cents_to_dollar_str(1), "0.01");
        assert_eq!(cents_to_dollar_str(97), "0.97");
        assert_eq!(cents_to_dollar_str(100), "1.00");
        assert_eq!(cents_to_dollar_str(50), "0.50");
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
}
