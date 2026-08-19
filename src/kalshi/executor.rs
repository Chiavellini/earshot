//! Execution layer (spec §5.6/§5.7): consumes `DecisionIntent`s from the
//! decision stage, sweeps stale YES offers with a single IOC marketable
//! limit at the cap (the matching engine sweeps all cheaper levels; IOC
//! auto-cancels the unfilled remainder, so pre-staged bodies are fully
//! static per keyword × tier × tranche).
//!
//! Entry door: `tokio::sync::mpsc::Receiver<DecisionIntent>`.
//! Exit door: orders on the wire + `OrderRecord`s in the report.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use base64::Engine as _;
use serde::Serialize;
use serde_json::Value;
use tokio::sync::mpsc;

use super::book::BookCache;
use super::order_evidence::{
    EvidenceTier, ExchangeObserved as DurableExchangeObserved, IntentEvaluated, InvocationPrepared,
    NotAttempted, OrderAction, OrderEvidenceSink, OrderSide, OrderTimeInForce, RawEvidence,
    ReconcileUnresolved, Reconciled, SendRequested, TransportFailed, TransportProtocol,
    WireAttempted,
};
use super::rest::RestClient;
use super::transport::{
    FillEvidence, OrderTransport, StageWire, SubmitResult, TransportObservation, WireStaged,
};
use crate::clock::MonotonicNs;
use crate::config::KeywordConfig;
use crate::decision::FAST_TRANCHE_PCT;
use crate::risk::RiskGate;
use crate::telemetry::LatencyRecorder;
use crate::types::{DecisionIntent, Tier};

/// Fee for an order that fills `count` contracts at `price_cents`
/// (VERIFICATIONS Q5: ceil-to-cent of 0.07 × C × P × (1−P), fee only on
/// matched orders). Integer math: ceil(7·C·pc·(100−pc) / 10_000) cents.
pub fn fee_cents(count: u32, price_cents: u32) -> u32 {
    let num = 7u64 * count as u64 * price_cents as u64 * (100 - price_cents.min(100)) as u64;
    num.div_ceil(10_000) as u32
}

/// Worst-case expected value check: every contract fills at the cap.
/// EV_total = (100 − cap − haircut)·count − fee ≥ min_ev·count.
pub fn ev_gate(cap_cents: u32, count: u32, haircut_cents: u32, min_ev_cents: u32) -> bool {
    if cap_cents + haircut_cents >= 100 {
        return false;
    }
    let gross = (100 - cap_cents - haircut_cents) as u64 * count as u64;
    let fee = fee_cents(count, cap_cents) as u64;
    gross.saturating_sub(fee) >= min_ev_cents as u64 * count as u64
}

/// The observe-posture price: unvalidated words bid 1¢ non-crossing, so their
/// intents are EXPECTED to find no ask at or under the cap (prepare leaves them
/// at 1¢ precisely so they take the non-crossing branch and never send). A
/// non-crossing outcome at this price is by design, not a lost race — the MS
/// 2026-07-15 run reported "4 LOST_RACE" that were all 1¢ observes, which
/// misread as a latency failure. `ExecReport` splits the two counters.
pub const OBSERVE_PRICE_CENTS: u32 = 1;

#[derive(Clone)]
pub struct PreStaged {
    /// Per-transport wire form (REST: pre-serialized JSON body).
    pub wire: WireStaged,
    pub ticker: String,
    pub price_cents: u32,
    pub count: u32,
    pub client_order_id: String,
    /// SHA-256 of the canonical logical request assembled at arm time. Exact
    /// wire bytes are captured independently at the transport boundary.
    pub request_sha256: String,
}

/// Key: (keyword index, tier, tranche count) — the decision table emits a
/// known, finite set of tranches (fast %, confirmed remainder, confirmed
/// full), all enumerable at arm time.
pub type PreStagedMap = HashMap<(u16, Tier, u32), PreStaged>;

pub fn deterministic_order_client_id(
    run_identity: &str,
    ticker: &str,
    price_cents: u32,
    count: u32,
    keyword_index: u16,
    tier: Tier,
) -> String {
    super::rest::deterministic_client_order_id(&format!(
        "{run_identity}:{ticker}:{price_cents}:{count}:{keyword_index}:{tier:?}"
    ))
}

pub fn prestage_orders(
    run_identity: &str,
    keywords: &[KeywordConfig],
    wire: StageWire,
) -> PreStagedMap {
    let mut map = PreStagedMap::new();
    for (idx, kw) in keywords.iter().enumerate() {
        let idx = idx as u16;
        // Mirror DecisionTable's tranche arithmetic exactly, per policy.
        let fast = kw.budget_contracts * FAST_TRANCHE_PCT / 100;
        let variants: Vec<(Tier, u32)> = match kw.policy {
            crate::config::Policy::FastOnly => vec![(Tier::Fast, kw.budget_contracts)],
            crate::config::Policy::ConfirmedOnly => vec![(Tier::Confirmed, kw.budget_contracts)],
            crate::config::Policy::TwoTier => vec![
                (Tier::Fast, fast),
                (Tier::Confirmed, kw.budget_contracts - fast), // confirmed after fast
                (Tier::Confirmed, kw.budget_contracts),        // confirmed without prior fast
            ],
        };
        for (tier, count) in variants {
            if count == 0 {
                continue;
            }
            // Idempotency is scoped to the immutable run and full logical
            // request. A config revision gets a new RunPlan hash and cannot
            // accidentally adopt a prior run's differently-shaped order.
            let client_order_id = deterministic_order_client_id(
                run_identity,
                &kw.market_ticker,
                kw.max_price_cents as u32,
                count,
                idx,
                tier,
            );
            let wire = wire.stage(
                &kw.market_ticker,
                kw.max_price_cents as u32,
                count,
                client_order_id.clone(),
            );
            let request_sha256 = match &wire {
                WireStaged::Rest { body } => super::order_evidence::sha256_hex(body),
                WireStaged::Fix { template: _ } => {
                    // The stable request identity is independent of FIX
                    // session sequence/time. The exact assembled frame is
                    // journaled by FixPreWriteHook before write_all.
                    super::order_evidence::sha256_hex(
                        format!(
                            "{}:{}:{}:{}:{:?}",
                            kw.market_ticker, kw.max_price_cents, count, client_order_id, tier
                        )
                        .as_bytes(),
                    )
                }
            };
            map.insert(
                (idx, tier, count),
                PreStaged {
                    wire,
                    ticker: kw.market_ticker.clone(),
                    price_cents: kw.max_price_cents as u32,
                    count,
                    client_order_id,
                    request_sha256,
                },
            );
        }
    }
    map
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OrderWireState {
    /// An intent stopped before a transport socket-write boundary.
    NotAttempted,
    /// `send_ts` exists, but no exchange/protocol response was observed.
    AttemptedUnobserved,
    /// The armed protocol returned an observation (acceptance and terminal
    /// state are independent fields on the record).
    Observed,
    /// A send failure was resolved through the idempotent client-order-id
    /// query. The original attempt evidence remains alongside this state.
    Reconciled,
}

#[derive(Debug, Clone, Serialize)]
pub struct OrderRecord {
    pub client_order_id: String,
    pub ticker: String,
    pub price_cents: u32,
    pub count: u32,
    pub tier: Tier,
    pub dry_run: bool,
    pub lost_race: bool,
    pub refusal: Option<String>,
    pub order_status: Option<String>,
    pub order_id: Option<String>,
    pub audio_ts: u64,
    pub decision_ts: u64,
    pub exec_ts: u64,
    pub wire_state: OrderWireState,
    pub send_ts: Option<u64>,
    /// Response-headers/TTFB timestamp (send→hdrs vs hdrs→ack forensics).
    pub hdrs_ts: Option<u64>,
    pub ack_ts: Option<u64>,
    /// Wire-specific observation without throwing away protocol semantics.
    pub transport_protocol: Option<String>,
    pub protocol_status: Option<String>,
    pub exchange_accepted: Option<bool>,
    pub order_terminal: Option<bool>,
    pub exchange_observed_ts: Option<u64>,
    pub reconciliation_ts: Option<u64>,
    pub reconciliation_status: Option<String>,
    pub transport_error: Option<String>,
    pub parse_error: Option<String>,
    /// Parsed response/ExecutionReport and exact REST response bytes. Bytes
    /// are base64 so report JSON remains portable and byte-for-byte replayable.
    pub raw_response: Option<Value>,
    pub raw_response_body_base64: Option<String>,
    pub reconciliation_response: Option<Value>,
    pub fill: Option<FillEvidence>,
    /// Book snapshot at decision time (LOST_RACE forensics, spec §5.6).
    /// (price_cents, qty_centi_contracts) at decision time — forensic only.
    pub best_ask: Option<(u32, u32)>,
    /// Exact monotonic timestamp of the book snapshot used for the sweep
    /// check. Together with `decision_ts` and `send_ts`, this makes
    /// detector-vs-sweep timing reconstructible without log joins.
    pub book_as_of_ts: Option<u64>,
    /// Age of that snapshot (exec_ts − book.as_of). Distinguishes "stale book"
    /// from "fast competitor": the MS RECE loss showed best_ask 20¢ at decision
    /// while the public tape had already swept to 98¢ ~250 ms earlier — without
    /// this field the two explanations were indistinguishable post-hoc.
    pub book_age_ns: Option<u64>,
    /// SHADOW ONLY — nothing reads this to make a decision.
    ///
    /// EV in cents at the price we would actually PAY (the observed ask), still
    /// assuming the detection is correct: `100 − ask − fee`. The shipped
    /// `ev_gate` prices at the *cap* instead, so a 34¢ opportunity is scored as
    /// a 97¢ one; recording both makes that divergence visible in every run
    /// rather than only under a joiner that knows the fee formula.
    ///
    /// It is deliberately NOT the whole answer. Settlement joined across every
    /// banked run (`scripts/settle-score.py --bands`, n=64) shows P(YES | we
    /// fired) is a function of the ask — 29/29 at 95–100¢, 5/19 at 20–59¢ — so
    /// a correct gate needs a price-conditional P(wrong) term that this field
    /// does not yet carry. At n=64 no band clears its own confidence interval,
    /// which is why this ships as evidence and gates nothing.
    pub ev_at_ask_cents: Option<i64>,
    /// Fill-path verifier (D2): what the second-stage decode said about this
    /// intent's keyword — `confirmed` / `rejected` / `timeout` / `dropped` /
    /// `not_requested` — and whether the verdict GATED the send (`verify_gated`,
    /// mode = gate on an opted-in fill word). Absent when no verifier ran.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub verify_verdict: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub verify_wait_ns: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub verify_gated: Option<bool>,
}

impl OrderRecord {
    /// Did the process invoke the armed transport for this order?
    ///
    /// THE distinction the 2026-08-05 morning lost. `ExecReport.orders` holds
    /// every intent the executor processed, including the five non-fire early
    /// returns (non-crossing, EV gate, dry-run, risk refusal, send-failed), so
    /// `orders.len()` is an INTENT count and calling it "orders placed"
    /// reported 23 when the true number was 0. `send_ts` is the transport
    /// invocation timestamp. It proves an attempt, not socket delivery or
    /// exchange receipt; those have their own observation fields.
    pub fn send_attempted(&self) -> bool {
        self.send_ts.is_some()
    }

    /// Compatibility spelling retained for existing report readers. Despite
    /// its historical name, this means only `send_attempted()`; it must never
    /// be cited as proof that bytes reached Kalshi.
    pub fn reached_wire(&self) -> bool {
        self.send_attempted()
    }

    /// Did Kalshi acknowledge it with an order id? Strictly stronger than
    /// `reached_wire`: a send can time out ambiguously and still be on the
    /// book, which is why the executor queries by client id before giving up.
    pub fn acked(&self) -> bool {
        self.order_id.is_some()
    }
}

pub struct ExecCtx {
    /// Order entry: the one wire the fire path sends through.
    pub transport: OrderTransport,
    /// Non-hot-path REST (ambiguous-outcome status query by client id, kill
    /// cancels). Stays REST for every entry transport — same order store.
    pub rest: RestClient,
    pub cache: Arc<BookCache>,
    pub gate: Arc<RiskGate>,
    pub prestaged: PreStagedMap,
    pub haircuts: HashMap<u16, u32>, // keyword idx → rule_risk_haircut_cents
    pub dry_run: bool,
    /// Every acked order id lands here; the kill switch cancels exactly this
    /// set (never account-wide — other systems' orders share this account).
    pub registry: super::OrderRegistry,
    /// Present on live RunPlan-backed arms. Every transition is durably
    /// acknowledged before the next lifecycle boundary may execute.
    pub order_evidence: Option<OrderEvidenceSink>,
    pub decision_plan_sha1: String,
    pub keyword_text: HashMap<u16, String>,
    pub rest_endpoint_id: String,
    /// Fill-path verifier client (D2). `None` = no verifier configured. In
    /// `gate` mode a FILL-priced Confirmed intent on an opted-in ticker is held
    /// (≤ `budget_ms` past `decision_ts`) for the second-stage verdict before
    /// the book is read; 1¢ observe intents never wait.
    pub verifier: Option<crate::verifier::VerifierClient>,
}

impl ExecCtx {
    /// Compatibility defaults for rehearsals and focused executor tests. Live
    /// assembly overwrites the evidence fields from its verified RunPlan.
    pub fn without_order_evidence(mut self) -> Self {
        self.order_evidence = None;
        self.decision_plan_sha1 = "0".repeat(40);
        self.keyword_text = HashMap::new();
        self.rest_endpoint_id = self.rest.endpoint_id();
        self
    }
}

pub struct ExecReport {
    pub orders: Vec<OrderRecord>,
    /// Fillable-tier intents (cap > `OBSERVE_PRICE_CENTS`) that found no ask at
    /// or under the cap — genuinely lost races.
    pub lost_races: u32,
    /// 1¢ observe-tier intents that (by design) crossed nothing. Kept separate
    /// so designed behavior never inflates the failure headline.
    pub observe_noncross: u32,
    pub recorder: LatencyRecorder,
}

/// Runs until the intent sender is dropped. Intents are processed
/// concurrently (Powell burst case) via a FuturesUnordered polled in this one
/// task: sends still overlap on the wire (the awaits are network-bound), but
/// there is no per-intent task spawn — no allocation, no scheduler enqueue,
/// no cross-worker hop between intent arrival and handle_intent (the fire
/// path is µs of CPU; a second core buys nothing but jitter). `biased`
/// ordering prefers a fresh intent over harvesting a completed send: firing
/// beats bookkeeping.
pub async fn run_executor(mut rx: mpsc::Receiver<DecisionIntent>, ctx: Arc<ExecCtx>) -> ExecReport {
    use futures_util::stream::{FuturesUnordered, StreamExt};

    let mut orders = Vec::<OrderRecord>::new();
    let recorder = Mutex::new(LatencyRecorder::new());
    let mut inflight = FuturesUnordered::new();

    loop {
        tokio::select! {
            biased;
            maybe = rx.recv() => match maybe {
                Some(intent) => inflight.push(handle_intent(&ctx, &recorder, intent)),
                None => break,
            },
            Some(record) = inflight.next(), if !inflight.is_empty() => orders.push(record),
        }
    }
    // Sender gone: drain the in-flight sends.
    while let Some(record) = inflight.next().await {
        orders.push(record);
    }
    drop(inflight);

    let lost_races = orders
        .iter()
        .filter(|o| o.lost_race && o.price_cents > OBSERVE_PRICE_CENTS)
        .count() as u32;
    let observe_noncross = orders
        .iter()
        .filter(|o| o.lost_race && o.price_cents <= OBSERVE_PRICE_CENTS)
        .count() as u32;
    let recorder = recorder.into_inner().unwrap();
    ExecReport {
        orders,
        lost_races,
        observe_noncross,
        recorder,
    }
}

/// Edge values measured before the send but recorded only AFTER the wire (or
/// at a non-fire exit): the fire path must hold no telemetry lock between
/// intent arrival and the HTTP send. Timestamps are taken at the right
/// moments; only the histogram bookkeeping is deferred.
#[derive(Default)]
struct PendingEdges {
    decision_to_exec: u64,
    handoff_to_exec: Option<u64>,
    book_age: Option<u64>,
    presign_age: Option<u64>,
}

impl PendingEdges {
    fn record_into(&self, rec: &mut LatencyRecorder) {
        rec.record("decision→exec", self.decision_to_exec);
        if let Some(v) = self.handoff_to_exec {
            rec.record("handoff→exec", v);
        }
        if let Some(v) = self.book_age {
            rec.record("book_age", v);
        }
        if let Some(v) = self.presign_age {
            rec.record("presign_age", v);
        }
    }

    fn flush(&self, recorder: &Mutex<LatencyRecorder>) {
        self.record_into(&mut recorder.lock().unwrap());
    }
}

fn order_status_terminal(status: &str) -> Option<bool> {
    match status {
        "filled" | "canceled" | "rejected" | "expired" => Some(true),
        "resting" | "pending" | "partial" => Some(false),
        _ => None,
    }
}

fn exact_value_string(value: &Value, key: &str) -> Option<String> {
    let value = value.get(key)?;
    value
        .as_str()
        .map(String::from)
        .or_else(|| value.as_number().map(ToString::to_string))
}

fn rest_fill_from_order(order: &Value) -> Option<FillEvidence> {
    let fill = FillEvidence {
        cumulative_contracts: exact_value_string(order, "fill_count"),
        remaining_contracts: exact_value_string(order, "remaining_count"),
        last_fill_contracts: None,
        last_fill_price: None,
        average_price: exact_value_string(order, "average_fill_price"),
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

fn durable_tier(tier: Tier) -> EvidenceTier {
    match tier {
        Tier::Fast => EvidenceTier::Fast,
        Tier::Confirmed => EvidenceTier::Confirmed,
    }
}

fn fix_endpoint_id(cfg: &super::fix::FixConfig) -> String {
    format!(
        "fix://{}:{}?tls={}&sender={}&target={}",
        cfg.host, cfg.port, cfg.tls, cfg.sender_comp_id, cfg.target_comp_id
    )
}

fn durable_protocol(protocol: &str) -> Option<TransportProtocol> {
    match protocol {
        "rest" => Some(TransportProtocol::Rest),
        "fix" => Some(TransportProtocol::Fix),
        _ => None,
    }
}

fn durable_fill(
    fill: &FillEvidence,
    exchange_event_ts: Option<String>,
) -> super::order_evidence::FillEvidence {
    let average_price_dollars = match (fill.average_price.as_deref(), fill.price_unit) {
        (Some(value), "cents") => cents_fixed_point_to_dollars(value),
        (Some(value), _) => Some(value.to_owned()),
        (None, _) => None,
    };
    let latest_fill_price_dollars = match (fill.last_fill_price.as_deref(), fill.price_unit) {
        (Some(value), "cents") => cents_fixed_point_to_dollars(value),
        (Some(value), _) => Some(value.to_owned()),
        (None, _) => None,
    };
    let has_latest_fill = fill.last_fill_contracts.is_some() || fill.last_fill_price.is_some();
    super::order_evidence::FillEvidence {
        filled_count_fp: fill.cumulative_contracts.clone(),
        remaining_count_fp: fill.remaining_contracts.clone(),
        average_price_dollars,
        latest_fill_count_fp: fill.last_fill_contracts.clone(),
        latest_fill_price_dollars,
        latest_fill_exchange_ts: has_latest_fill.then_some(exchange_event_ts).flatten(),
    }
}

fn cents_fixed_point_to_dollars(value: &str) -> Option<String> {
    let (whole, fraction) = value.split_once('.').unwrap_or((value, ""));
    if whole.is_empty()
        || !whole.bytes().all(|byte| byte.is_ascii_digit())
        || !fraction.bytes().all(|byte| byte.is_ascii_digit())
    {
        return None;
    }
    let cents = whole.parse::<u128>().ok()?;
    let mut rendered = format!("{}.{:02}", cents / 100, cents % 100);
    if !fraction.is_empty() {
        rendered.push_str(fraction);
    }
    Some(rendered)
}

fn fix_tag(raw: &[u8], tag: &[u8]) -> Option<String> {
    raw.split(|byte| *byte == 1).find_map(|field| {
        let position = field.iter().position(|byte| *byte == b'=')?;
        let (key, tail) = field.split_at(position);
        let value = tail.get(1..)?;
        (key == tag).then(|| String::from_utf8_lossy(value).into_owned())
    })
}

fn raw_observation(observation: &TransportObservation) -> RawEvidence {
    match observation.raw_body.as_deref() {
        Some(bytes) => RawEvidence::from_bytes(
            if observation.protocol == "fix" {
                "application/fix"
            } else {
                "application/json"
            },
            bytes,
        ),
        None => RawEvidence::from_bytes(
            "application/json",
            &serde_json::to_vec(&observation.raw).unwrap_or_default(),
        ),
    }
}

async fn durable_not_attempted(
    sink: Option<&OrderEvidenceSink>,
    staged: &PreStaged,
    reason: impl Into<String>,
) -> Result<(), String> {
    let Some(sink) = sink else {
        return Ok(());
    };
    sink.record_not_attempted(NotAttempted {
        client_order_id: staged.client_order_id.clone(),
        market_ticker: staged.ticker.clone(),
        refusal_ts: MonotonicNs::now().0,
        reason: reason.into(),
    })
    .await
    .map(|_| ())
    .map_err(|error| error.to_string())
}

async fn durable_intent_evaluated(
    sink: Option<&OrderEvidenceSink>,
    staged: &PreStaged,
    ctx: &ExecCtx,
    intent: &DecisionIntent,
    keyword_id: u16,
    exec_ts: MonotonicNs,
    book_as_of_ts: u64,
    best_ask_cents: Option<u8>,
) -> Result<(), String> {
    let Some(sink) = sink else {
        return Ok(());
    };
    sink.record_intent_evaluated(IntentEvaluated {
        client_order_id: staged.client_order_id.clone(),
        market_ticker: staged.ticker.clone(),
        side: OrderSide::Yes,
        action: OrderAction::Buy,
        price_cents: u8::try_from(staged.price_cents).unwrap_or(u8::MAX),
        contract_count: staged.count,
        time_in_force: OrderTimeInForce::ImmediateOrCancel,
        tier: durable_tier(intent.tier),
        keyword_id,
        keyword: ctx
            .keyword_text
            .get(&keyword_id)
            .cloned()
            .unwrap_or_else(|| format!("keyword-{keyword_id}")),
        source_id: intent.source.0,
        audio_ts: intent.audio_ts.0,
        decision_ts: intent.decision_ts.0,
        exec_ts: exec_ts.0,
        book_as_of_ts,
        best_ask_cents,
        request_sha256: staged.request_sha256.clone(),
        decision_plan_sha1: ctx.decision_plan_sha1.clone(),
    })
    .await
    .map(|_| ())
    .map_err(|error| error.to_string())
}

async fn durable_observation(
    sink: Option<&OrderEvidenceSink>,
    staged: &PreStaged,
    endpoint_id: &str,
    observation: &TransportObservation,
) -> Result<(), String> {
    let Some(sink) = sink else {
        return Ok(());
    };
    let protocol = durable_protocol(observation.protocol)
        .ok_or_else(|| format!("unknown evidence transport {}", observation.protocol))?;
    let exchange_event_ts = observation
        .raw_body
        .as_deref()
        .filter(|_| observation.protocol == "fix")
        .and_then(|raw| fix_tag(raw, b"60").or_else(|| fix_tag(raw, b"52")));
    sink.record_exchange_observed(DurableExchangeObserved {
        client_order_id: staged.client_order_id.clone(),
        market_ticker: staged.ticker.clone(),
        protocol,
        endpoint_id: endpoint_id.to_owned(),
        observed_ts: observation.observed_ts.0,
        protocol_status: observation.protocol_status.clone(),
        exchange_accepted: observation.accepted,
        order_terminal: observation.order_terminal.unwrap_or(false),
        echoed_client_order_id: observation.echoed_client_order_id.clone(),
        order_id: observation.order_id.clone(),
        order_status: observation.order_status.clone(),
        exchange_event_ts: exchange_event_ts.clone(),
        fill: observation
            .fill
            .as_ref()
            .map(|fill| durable_fill(fill, exchange_event_ts)),
        raw: raw_observation(observation),
        parsed: Some(observation.raw.clone()),
        detail: observation.parse_error.clone(),
    })
    .await
    .map(|_| ())
    .map_err(|error| error.to_string())
}

/// An exchange order id is cancellable identity only when the same response
/// exactly echoes our client id. Preserve untrusted values in the raw bytes,
/// but do not promote them into the reducer or session registry.
fn retain_only_verified_correlation(
    staged: &PreStaged,
    mut observation: TransportObservation,
) -> TransportObservation {
    let exact_echo =
        observation.echoed_client_order_id.as_deref() == Some(staged.client_order_id.as_str());
    if !exact_echo
        && (observation.order_id.is_some() || observation.echoed_client_order_id.is_some())
    {
        let detail = "response did not carry the exact expected client_order_id; observed identity remains in raw/parsed evidence and is not promoted into the reducer";
        observation.parse_error = Some(match observation.parse_error.take() {
            Some(existing) => format!("{existing}; {detail}"),
            None => detail.into(),
        });
        observation.order_id = None;
        observation.echoed_client_order_id = None;
        // A terminal acceptance without verified exchange identity cannot be
        // adopted as terminal state: it cannot be cancelled or audited.
        if observation.accepted != Some(false) {
            observation.order_terminal = Some(false);
        }
    }
    observation
}

pub struct FixOrderEvidenceHook {
    sink: OrderEvidenceSink,
    ticker_by_client_id: HashMap<String, String>,
}

impl FixOrderEvidenceHook {
    pub fn new(sink: OrderEvidenceSink, prestaged: &PreStagedMap) -> Self {
        Self {
            sink,
            ticker_by_client_id: prestaged
                .values()
                .map(|order| (order.client_order_id.clone(), order.ticker.clone()))
                .collect(),
        }
    }
}

impl super::fix::transport::FixPreWriteHook for FixOrderEvidenceHook {
    fn before_write<'a>(
        &'a self,
        evidence: &'a super::fix::transport::FixPreWriteEvidence,
    ) -> super::fix::transport::FixPreWriteFuture<'a> {
        Box::pin(async move {
            let ticker = self
                .ticker_by_client_id
                .get(&evidence.client_order_id)
                .cloned()
                .ok_or_else(|| {
                    super::KalshiError::Validation(format!(
                        "FIX evidence hook has no prestaged ticker for {}",
                        evidence.client_order_id
                    ))
                })?;
            let cfg = super::fix::FixConfig {
                host: evidence.endpoint.host.clone(),
                port: evidence.endpoint.port,
                sender_comp_id: evidence.endpoint.sender_comp_id.clone(),
                target_comp_id: evidence.endpoint.target_comp_id.clone(),
                heartbeat_secs: 1,
                tls: evidence.endpoint.tls,
            };
            self.sink
                .record_invocation_prepared(InvocationPrepared {
                    client_order_id: evidence.client_order_id.clone(),
                    market_ticker: ticker,
                    protocol: TransportProtocol::Fix,
                    endpoint_id: fix_endpoint_id(&cfg),
                    prepared_ts: evidence.prepared_ts.0,
                    raw: RawEvidence::from_bytes("application/fix", &evidence.outbound_frame),
                })
                .await
                .map(|_| ())
                .map_err(|error| {
                    super::KalshiError::Validation(format!(
                        "durable FIX invocation-prepared evidence failed: {error}"
                    ))
                })
        })
    }
}

fn apply_transport_observation(record: &mut OrderRecord, observation: TransportObservation) {
    record.wire_state = OrderWireState::Observed;
    record.transport_protocol = Some(observation.protocol.to_string());
    record.protocol_status = Some(observation.protocol_status);
    record.exchange_accepted = observation.accepted;
    record.order_terminal = observation.order_terminal;
    record.order_id = observation.order_id;
    record.order_status = observation.order_status;
    record.exchange_observed_ts = Some(observation.observed_ts.0);
    record.hdrs_ts = Some(observation.observed_ts.0);
    record.ack_ts = observation.ack_ts.map(|ts| ts.0);
    record.parse_error = observation.parse_error;
    record.raw_response = Some(observation.raw);
    record.raw_response_body_base64 = observation
        .raw_body
        .map(|bytes| base64::engine::general_purpose::STANDARD.encode(bytes));
    record.fill = observation.fill;
}

fn record_attempt_edges(
    recorder: &Mutex<LatencyRecorder>,
    pending: &PendingEdges,
    exec_ts: MonotonicNs,
    intent: &DecisionIntent,
    record: &OrderRecord,
) {
    let Some(send_ts) = record.send_ts.map(MonotonicNs) else {
        pending.flush(recorder);
        return;
    };
    let mut rec = recorder.lock().unwrap();
    pending.record_into(&mut rec);
    rec.record("exec→send", send_ts.saturating_since(exec_ts));
    if let Some(observed_ts) = record.exchange_observed_ts.map(MonotonicNs) {
        rec.record("send→hdrs", observed_ts.saturating_since(send_ts));
        if let Some(ack_ts) = record.ack_ts.map(MonotonicNs) {
            rec.record("hdrs→ack", ack_ts.saturating_since(observed_ts));
        }
    }
    if let Some(ack_ts) = record.ack_ts.map(MonotonicNs) {
        rec.record("send→ack", ack_ts.saturating_since(send_ts));
        rec.record("decision→ack", ack_ts.saturating_since(intent.decision_ts));
        rec.record("capture→ack", ack_ts.saturating_since(intent.audio_ts));
    }
}

async fn handle_intent(
    ctx: &ExecCtx,
    recorder: &Mutex<LatencyRecorder>,
    intent: DecisionIntent,
) -> OrderRecord {
    let exec_ts = MonotonicNs::now();
    let mut pending = PendingEdges {
        // Aggregate: detect-thread tail + mpsc wake + task spawn.
        decision_to_exec: exec_ts.saturating_since(intent.decision_ts),
        // Isolated wake hop: only meaningful once the detect thread stamped it
        // (0 in unit tests / log-only paths that construct intents directly).
        handoff_to_exec: (intent.handoff_ts.0 > 0)
            .then(|| exec_ts.saturating_since(intent.handoff_ts)),
        ..Default::default()
    };

    let kw = intent.keyword.0;
    let staged = ctx
        .prestaged
        .get(&(kw, intent.tier, intent.tranche_contracts))
        .cloned();
    // A missing arm-time artifact is an assembly defect. Never manufacture a
    // different order on the live fire path: that would evade the reviewed
    // body, deterministic id, and evidence plan.
    let (client_order_id, ticker, price_cents, count) = match staged.as_ref() {
        Some(staged) => (
            staged.client_order_id.clone(),
            staged.ticker.clone(),
            staged.price_cents,
            staged.count,
        ),
        None => (
            super::rest::deterministic_client_order_id(&format!(
                "unstaged:{kw}:{:?}:{}",
                intent.tier, intent.tranche_contracts
            )),
            intent.market_ticker.clone(),
            intent.max_price_cents as u32,
            intent.tranche_contracts,
        ),
    };

    let mut record = OrderRecord {
        client_order_id,
        ticker,
        price_cents,
        count,
        tier: intent.tier,
        dry_run: ctx.dry_run,
        lost_race: false,
        refusal: None,
        order_status: None,
        order_id: None,
        audio_ts: intent.audio_ts.0,
        decision_ts: intent.decision_ts.0,
        exec_ts: exec_ts.0,
        wire_state: OrderWireState::NotAttempted,
        send_ts: None,
        hdrs_ts: None,
        ack_ts: None,
        transport_protocol: None,
        protocol_status: None,
        exchange_accepted: None,
        order_terminal: None,
        exchange_observed_ts: None,
        reconciliation_ts: None,
        reconciliation_status: None,
        transport_error: None,
        parse_error: None,
        raw_response: None,
        raw_response_body_base64: None,
        reconciliation_response: None,
        fill: None,
        best_ask: None,
        book_as_of_ts: None,
        book_age_ns: None,
        ev_at_ask_cents: None,
        verify_verdict: None,
        verify_wait_ns: None,
        verify_gated: None,
    };

    let Some(staged) = staged else {
        record.refusal =
            Some("missing arm-time prestaged order; live inline construction is forbidden".into());
        tracing::error!(
            kw,
            ?intent.tier,
            intent.tranche_contracts,
            "missing prestaged order — refusing before book/risk/transport"
        );
        pending.flush(recorder);
        return record;
    };

    // Fill-path verifier hold (D2). Only a FILL-priced Confirmed intent on an
    // opted-in ticker in gate mode ever waits; everything else proceeds and, if
    // a verdict is already there, records it as shadow evidence.
    if let Some(v) = ctx.verifier.as_ref() {
        let fill_priced = staged.price_cents > OBSERVE_PRICE_CENTS;
        let gated =
            fill_priced && intent.tier == Tier::Confirmed && v.gated_keyword(intent.keyword);
        record.verify_gated = Some(gated);
        if gated {
            let budget = std::time::Duration::from_millis(v.budget_ms() as u64);
            let already =
                std::time::Duration::from_nanos(exec_ts.saturating_since(intent.decision_ts));
            let deadline = tokio::time::Instant::now() + budget.saturating_sub(already);
            let wait_start = MonotonicNs::now();
            let outcome = v.await_verdict(intent.keyword, intent.tier, deadline).await;
            let waited = MonotonicNs::now().saturating_since(wait_start);
            record.verify_wait_ns = Some(waited);
            use crate::verifier::{Await, Verdict};
            let (label, veto) = match &outcome {
                Await::Ready(Verdict::Confirmed { .. }) => ("confirmed", false),
                Await::Ready(Verdict::Rejected) => ("rejected", true),
                Await::Ready(Verdict::Error { .. }) => ("error", false),
                Await::Timeout => (
                    "timeout",
                    v.on_timeout() == crate::config::VerifierOnTimeout::Hold,
                ),
                Await::Dropped => ("dropped", false),
                Await::NotRequested => ("not_requested", false),
            };
            record.verify_verdict = Some(label.into());
            tracing::info!(ticker = %staged.ticker, verdict = label, waited_ms = waited / 1_000_000,
                veto, "verifier hold resolved");
            if veto {
                let reason = if label == "timeout" {
                    "verifier_timeout"
                } else {
                    "verifier_veto"
                };
                record.refusal = Some(reason.into());
                if let Err(error) = durable_not_attempted(
                    ctx.order_evidence.as_ref(),
                    &staged,
                    "second-stage verifier refused the fill intent",
                )
                .await
                {
                    record.transport_error = Some(format!(
                        "order evidence verifier-veto termination failed: {error}"
                    ));
                }
                pending.flush(recorder);
                return record;
            }
        } else if let crate::verifier::Await::Ready(verdict) = v
            .await_verdict(intent.keyword, intent.tier, tokio::time::Instant::now())
            .await
        {
            // shadow evidence: a verdict that already exists is recorded, never waited for
            record.verify_verdict = Some(match verdict {
                crate::verifier::Verdict::Confirmed { .. } => "confirmed".into(),
                crate::verifier::Verdict::Rejected => "rejected".into(),
                crate::verifier::Verdict::Error { .. } => "error".into(),
            });
        }
    }

    // Local book read — never a blocking REST fetch (spec §5.6).
    let book = ctx.cache.read(&staged.ticker);
    let best_ask = book.as_ref().and_then(|b| b.best_ask());
    record.best_ask = best_ask;
    // Shadow evidence only — see `OrderRecord::ev_at_ask_cents`. Computed for
    // every intent that saw an ask, including ones that go on to be refused,
    // because the refused rows are exactly the counterfactual sample.
    record.ev_at_ask_cents = best_ask.map(|(ask, _)| {
        (100i64 - ask as i64) * staged.count as i64 - fee_cents(staged.count, ask) as i64
    });
    // seq 0 = the placeholder Book::default() (as_of 0) — no snapshot yet, so
    // an "age" would be time-since-boot noise.
    if let Some(b) = book.as_ref().filter(|b| b.seq > 0) {
        record.book_as_of_ts = Some(b.as_of.0);
        let age = exec_ts.saturating_since(b.as_of);
        record.book_age_ns = Some(age);
        pending.book_age = Some(age);
    }

    // Evaluation is evidence in its own right, including every no-send
    // outcome below. Persist it before any branch can return so a crash cannot
    // bias the performance sample toward only transport attempts.
    if let Err(error) = durable_intent_evaluated(
        ctx.order_evidence.as_ref(),
        &staged,
        ctx,
        &intent,
        kw,
        exec_ts,
        record.book_as_of_ts.unwrap_or(0),
        record
            .best_ask
            .and_then(|(price, _)| u8::try_from(price).ok()),
    )
    .await
    {
        let detail = format!("order evidence refused intent evaluation: {error}");
        record.refusal = Some(detail.clone());
        record.transport_error = Some(detail);
        pending.flush(recorder);
        return record;
    }

    // LOST_RACE: nothing offered at or under the cap. Primary edge-decay metric
    // — but only for fillable-tier caps. A 1¢ observe intent crossing nothing
    // is the designed outcome, never a race.
    let swept = match best_ask {
        Some((price, _)) if price <= staged.price_cents => false,
        _ => true,
    };
    if swept {
        record.lost_race = true;
        if staged.price_cents > OBSERVE_PRICE_CENTS {
            tracing::warn!(ticker = %staged.ticker, ?best_ask, cap = staged.price_cents,
                book_age_ms = record.book_age_ns.map(|a| a / 1_000_000), "LOST_RACE");
        } else {
            tracing::info!(ticker = %staged.ticker, ?best_ask,
                "observe non-crossing (1¢ cap, by design — not a lost race)");
        }
        if let Err(error) = durable_not_attempted(
            ctx.order_evidence.as_ref(),
            &staged,
            "book had no ask at or below the immutable price cap",
        )
        .await
        {
            record.transport_error = Some(format!(
                "order evidence lost-race termination failed: {error}"
            ));
        }
        pending.flush(recorder);
        return record;
    }

    // Fee/EV gate at the worst-case (fill entirely at the cap).
    let haircut = ctx.haircuts.get(&kw).copied().unwrap_or(0);
    if !ev_gate(
        staged.price_cents,
        staged.count,
        haircut,
        ctx.gate.cfg().min_ev_cents,
    ) {
        record.refusal = Some("EV gate".into());
        if let Err(error) = durable_not_attempted(
            ctx.order_evidence.as_ref(),
            &staged,
            "expected-value gate refused the evaluated intent",
        )
        .await
        {
            record.transport_error = Some(format!(
                "order evidence EV-gate termination failed: {error}"
            ));
        }
        pending.flush(recorder);
        return record;
    }

    // Shadow mode stops here: the record shows exactly what would have been
    // sent (spec §9.5 live-shadow). Risk gating below is exercised by unit
    // tests and the live drills.
    if ctx.dry_run {
        record.refusal = Some("dry_run".into());
        if let Err(error) = durable_not_attempted(
            ctx.order_evidence.as_ref(),
            &staged,
            "dry-run posture forbids transport invocation",
        )
        .await
        {
            record.transport_error = Some(format!(
                "order evidence dry-run termination failed: {error}"
            ));
        }
        pending.flush(recorder);
        return record;
    }

    // Risk gate (kill, armed, allowlist, staleness, caps, ledger).
    //
    // `book_age_ns` is the age of THIS TICKER's snapshot, not
    // `cache.feed_age_ns()`. Passing the latter was a live defect from the
    // gate's introduction until 2026-08-07: feed age is socket-wide liveness,
    // held under ~500 ms by the ping loop, so `Refusal::BookStale` could never
    // fire no matter how old the quote was. DASH-UBER on a 96.8 s snapshot was
    // refused twelve lines later by the then-existing per-event notional cap
    // (removed 2026-08-18), which proves BookStale had
    // not fired. Unknown age stays u64::MAX — unknown is stale (P3).
    let audio_age = MonotonicNs::now().saturating_since(intent.audio_ts);
    if let Err(refusal) = ctx.gate.check_order(
        &staged.ticker,
        staged.price_cents,
        staged.count,
        record.book_age_ns.unwrap_or(u64::MAX),
        audio_age,
    ) {
        record.refusal = Some(refusal.to_string());
        tracing::error!(ticker = %staged.ticker, "risk refusal: {refusal}");
        if let Err(error) = durable_not_attempted(
            ctx.order_evidence.as_ref(),
            &staged,
            format!("risk gate refused the evaluated intent: {refusal}"),
        )
        .await
        {
            record.transport_error = Some(format!(
                "order evidence risk-refusal termination failed: {error}"
            ));
        }
        pending.flush(recorder);
        return record;
    }

    let request = SendRequested {
        client_order_id: staged.client_order_id.clone(),
        market_ticker: staged.ticker.clone(),
        requested_ts: MonotonicNs::now().0,
    };
    if let Some(sink) = ctx.order_evidence.as_ref() {
        if let Err(error) = sink.record_send_requested(request).await {
            let detail = format!("order evidence refused send request: {error}");
            record.refusal = Some(detail.clone());
            tracing::error!(%detail, "order prevented before send boundary");
            pending.flush(recorder);
            return record;
        }
    }

    // Linearize the attempt against kill before any debit or transport call.
    // If kill wins first this client id can never enter the wire. If this call
    // wins first, kill sees the exact client id as pending and waits/reconciles
    // it even when no exchange order id has arrived yet.
    let attempt_permit = match ctx.registry.begin_attempt(staged.client_order_id.clone()) {
        Ok(permit) => permit,
        Err(detail) => {
            let reason = format!("send boundary refused: {detail}");
            if let Err(error) =
                durable_not_attempted(ctx.order_evidence.as_ref(), &staged, reason.clone()).await
            {
                tracing::error!(%error, "failed to terminate refused order evidence");
            }
            record.refusal = Some(reason);
            tracing::error!(ticker = %staged.ticker, %detail, "send boundary closed");
            pending.flush(recorder);
            return record;
        }
    };

    // The risk gate may have been killed between its earlier full policy check
    // and the registry boundary. Recheck only the kill/armed state now; a
    // failure resolves the pending client id as conclusively not attempted.
    if ctx.gate.is_killed() {
        ctx.registry.resolve_without_order(&staged.client_order_id);
        drop(attempt_permit);
        let reason = "kill switch engaged at final send boundary";
        if let Err(error) =
            durable_not_attempted(ctx.order_evidence.as_ref(), &staged, reason).await
        {
            tracing::error!(%error, "failed to terminate killed order evidence");
        }
        record.refusal = Some(reason.into());
        pending.flush(recorder);
        return record;
    }

    // Fire. Debit ledger BEFORE the send so an ambiguous timeout still counts
    // as spent. The transport owns its per-send machinery (REST: presigned
    // headers, RSA off the hot path).
    ctx.gate.record_send(staged.price_cents, staged.count);

    let mut reconcile = false;
    if matches!(staged.wire, WireStaged::Rest { .. }) {
        if let (Some(sink), WireStaged::Rest { body }) = (ctx.order_evidence.as_ref(), &staged.wire)
        {
            let prepared_ts = MonotonicNs::now();
            if let Err(error) = sink
                .record_invocation_prepared(InvocationPrepared {
                    client_order_id: staged.client_order_id.clone(),
                    market_ticker: staged.ticker.clone(),
                    protocol: TransportProtocol::Rest,
                    endpoint_id: ctx.rest_endpoint_id.clone(),
                    prepared_ts: prepared_ts.0,
                    raw: RawEvidence::from_bytes("application/json", body),
                })
                .await
            {
                ctx.registry.resolve_without_order(&staged.client_order_id);
                drop(attempt_permit);
                record.refusal = Some(format!(
                    "order evidence refused REST invocation preparation: {error}"
                ));
                record.transport_error = Some(error.to_string());
                pending.flush(recorder);
                return record;
            }
        }
    }
    match ctx.transport.submit_order(&staged).await {
        SubmitResult::NotAttempted { error } => {
            // Known pre-write refusal: no send timestamp and no status query.
            // The risk ledger remains conservative because it was debited
            // before asking the transport to enter its write boundary.
            record.transport_error = Some(error.to_string());
            record.refusal = Some(format!("transport did not attempt: {error}"));
            if let Err(journal_error) =
                durable_not_attempted(ctx.order_evidence.as_ref(), &staged, error.to_string()).await
            {
                record.transport_error = Some(format!(
                    "{error}; order evidence not_attempted failed: {journal_error}"
                ));
            }
            ctx.registry.resolve_without_order(&staged.client_order_id);
            pending.flush(recorder);
        }
        SubmitResult::Attempted(attempt) => {
            let super::transport::SendAttempt {
                protocol,
                send_ts,
                presign_age_ns,
                observations,
                outcome,
            } = attempt;
            record.wire_state = OrderWireState::AttemptedUnobserved;
            record.transport_protocol = Some(protocol.to_string());
            record.send_ts = Some(send_ts.0);
            pending.presign_age = presign_age_ns;
            let endpoint_id = if protocol == "fix" {
                match &ctx.transport {
                    OrderTransport::Fix { fix } => fix_endpoint_id(fix.config()),
                    _ => ctx.rest_endpoint_id.clone(),
                }
            } else {
                ctx.rest_endpoint_id.clone()
            };
            let observations: Vec<_> = observations
                .into_iter()
                .map(|observation| retain_only_verified_correlation(&staged, observation))
                .collect();
            let mut terminal_observed = false;
            // The transport has already returned these observations. Promote
            // verified cancellation identity before awaiting any journal fsync.
            // If storage stalls, the kill watcher can still cancel the order.
            for observation in &observations {
                terminal_observed |= observation.order_terminal == Some(true);
                apply_transport_observation(&mut record, observation.clone());
                if let Some(id) = &record.order_id {
                    ctx.registry
                        .register_for_client(&staged.client_order_id, id.clone());
                } else if record.exchange_accepted == Some(false)
                    && record.order_terminal == Some(true)
                {
                    ctx.registry.resolve_without_order(&staged.client_order_id);
                }
            }
            // `send_ts` comes from the transport at its actual invocation
            // boundary. It is intentionally persisted after the call returns:
            // if the process dies inside the call, InvocationPrepared remains
            // delivery-ambiguous and startup reconciles the exact client ID.
            if let Some(sink) = ctx.order_evidence.as_ref() {
                if let Some(protocol) = durable_protocol(protocol) {
                    if let Err(error) = sink
                        .record_wire_attempted(WireAttempted {
                            client_order_id: staged.client_order_id.clone(),
                            market_ticker: staged.ticker.clone(),
                            protocol,
                            endpoint_id: endpoint_id.clone(),
                            send_ts: send_ts.0,
                        })
                        .await
                    {
                        record.transport_error = Some(format!(
                            "order evidence wire-attempt receipt failed: {error}"
                        ));
                    }
                }
            }
            for observation in &observations {
                if let Err(error) = durable_observation(
                    ctx.order_evidence.as_ref(),
                    &staged,
                    &endpoint_id,
                    &observation,
                )
                .await
                {
                    record.transport_error =
                        Some(format!("order evidence observation failed: {error}"));
                }
            }
            match outcome {
                Ok(()) => {
                    if record.exchange_accepted == Some(false) {
                        record.refusal = Some(format!(
                            "exchange rejected: {}",
                            record
                                .protocol_status
                                .as_deref()
                                .unwrap_or("unknown status")
                        ));
                    } else if record.order_id.is_none() {
                        // A 2xx/malformed body is observed but still ambiguous:
                        // the exact bytes stay on the record and reconciliation
                        // determines whether an order exists.
                        reconcile = true;
                    }
                }
                Err(failure) => {
                    record.transport_error = Some(failure.error.to_string());
                    // A conclusive terminal exchange observation closes the
                    // reducer. Appending transport_failed afterward would be
                    // both false and an illegal post-terminal transition.
                    if !terminal_observed {
                        if let Some(sink) = ctx.order_evidence.as_ref() {
                            if let Some(protocol) = record
                                .transport_protocol
                                .as_deref()
                                .and_then(durable_protocol)
                            {
                                if let Err(error) = sink
                                    .record_transport_failed(TransportFailed {
                                        client_order_id: staged.client_order_id.clone(),
                                        market_ticker: staged.ticker.clone(),
                                        protocol,
                                        endpoint_id: endpoint_id.clone(),
                                        failure_ts: MonotonicNs::now().0,
                                        detail: failure.error.to_string(),
                                    })
                                    .await
                                {
                                    record.transport_error =
                                        Some(format!("{}; evidence: {error}", failure.error));
                                }
                            }
                        }
                    }
                    if record.exchange_accepted == Some(false) {
                        record.refusal = Some(format!(
                            "exchange rejected before response completed: {}",
                            record
                                .protocol_status
                                .as_deref()
                                .unwrap_or("unknown status")
                        ));
                    } else {
                        reconcile = true;
                    }
                }
            }

            if let Some(id) = &record.order_id {
                ctx.registry
                    .register_for_client(&staged.client_order_id, id.clone());
            } else if record.exchange_accepted == Some(false) && record.order_terminal == Some(true)
            {
                ctx.registry.resolve_without_order(&staged.client_order_id);
            }
            // One lock after the send boundary. Partial attempts still record
            // exec→send and any observation split that actually exists.
            record_attempt_edges(recorder, &pending, exec_ts, &intent, &record);
        }
    }

    if reconcile {
        // Ambiguous outcome: query by client id before assuming failure.
        // Never blind-retry. Preserve the original attempt/partial response
        // separately from this later REST observation.
        tracing::error!(
            transport_error = record.transport_error.as_deref().unwrap_or("none"),
            "ambiguous order attempt; querying by client order id"
        );
        match ctx
            .rest
            .order_by_client_id_evidence(&staged.client_order_id)
            .await
        {
            Ok(query) if query.matched_order.is_some() => {
                let order = query.matched_order.expect("guarded exact row");
                record.wire_state = OrderWireState::Reconciled;
                record.reconciliation_ts = Some(MonotonicNs::now().0);
                record.reconciliation_response = Some(order.clone());
                record.order_status = order["status"].as_str().map(String::from);
                record.reconciliation_status = record.order_status.clone();
                record.order_id = order["order_id"].as_str().map(String::from);
                if let Some(status) = record.order_status.as_deref() {
                    record.order_terminal = order_status_terminal(status);
                    record.exchange_accepted = match status {
                        "rejected" => Some(false),
                        "new" | "resting" | "pending" | "partial" | "filled" | "canceled"
                        | "expired" => Some(true),
                        _ => None,
                    };
                } else {
                    // Existence under the exact client id is itself proof the
                    // exchange observed the attempt, even when status is absent.
                    record.exchange_accepted = Some(true);
                }
                if let Some(fill) = rest_fill_from_order(&order) {
                    record.fill = Some(fill);
                }
                if let Some(sink) = ctx.order_evidence.as_ref() {
                    let durable_fill = record.fill.as_ref().map(|fill| {
                        durable_fill(fill, order["updated_time"].as_str().map(str::to_owned))
                    });
                    if let Err(error) = sink
                        .record_reconciled(Reconciled {
                            client_order_id: staged.client_order_id.clone(),
                            market_ticker: staged.ticker.clone(),
                            protocol: TransportProtocol::Rest,
                            endpoint_id: ctx.rest_endpoint_id.clone(),
                            observed_ts: query.observed_ts.0,
                            queried_client_order_id: query.queried_client_order_id,
                            echoed_client_order_id: staged.client_order_id.clone(),
                            order_id: record.order_id.clone(),
                            order_status: record
                                .order_status
                                .clone()
                                .unwrap_or_else(|| "unknown".into()),
                            exchange_accepted: record.exchange_accepted,
                            order_terminal: record.order_terminal.unwrap_or(false),
                            exchange_event_ts: order["updated_time"].as_str().map(str::to_owned),
                            fill: durable_fill,
                            raw: RawEvidence::from_bytes("application/json", &query.raw_body),
                            parsed: query.parsed,
                        })
                        .await
                    {
                        record.transport_error =
                            Some(format!("order reconciliation evidence failed: {error}"));
                    }
                }
                if let Some(id) = &record.order_id {
                    ctx.registry
                        .register_for_client(&staged.client_order_id, id.clone());
                } else if record.exchange_accepted != Some(false)
                    || record.order_terminal != Some(true)
                {
                    // An echoed client id without an exchange order id is not
                    // cancellable proof. Preserve the row but keep delivery
                    // ambiguous unless it is a conclusive terminal rejection.
                    record.wire_state = OrderWireState::AttemptedUnobserved;
                    record.refusal = Some(
                        "AMBIGUOUS exact-id reconciliation row had no order_id; do not retry"
                            .into(),
                    );
                }
                tracing::warn!("order WAS present despite ambiguous send outcome");
            }
            Ok(query) => {
                record.reconciliation_ts = Some(query.observed_ts.0);
                if let Some(sink) = ctx.order_evidence.as_ref() {
                    if let Err(journal_error) = sink
                        .record_reconcile_unresolved(ReconcileUnresolved {
                            client_order_id: staged.client_order_id.clone(),
                            market_ticker: staged.ticker.clone(),
                            protocol: TransportProtocol::Rest,
                            endpoint_id: ctx.rest_endpoint_id.clone(),
                            observed_ts: query.observed_ts.0,
                            queried_client_order_id: query.queried_client_order_id,
                            raw: Some(RawEvidence::from_bytes("application/json", &query.raw_body)),
                            parsed: query.parsed,
                            detail: query
                                .parse_error
                                .unwrap_or_else(|| "no exact echoed client ID".into()),
                        })
                        .await
                    {
                        record.transport_error =
                            Some(format!("order unresolved evidence failed: {journal_error}"));
                    }
                }
                let error = record
                    .transport_error
                    .as_deref()
                    .unwrap_or("ambiguous response");
                record.refusal = Some(format!(
                    "AMBIGUOUS attempted send ({error}); one exact-id query found no \
                     verifiable row — absence is not proven, do not retry"
                ));
            }
            Err(query_error) => {
                let observed_ts = MonotonicNs::now();
                record.reconciliation_ts = Some(observed_ts.0);
                if let Some(sink) = ctx.order_evidence.as_ref() {
                    if let Err(journal_error) = sink
                        .record_reconcile_unresolved(ReconcileUnresolved {
                            client_order_id: staged.client_order_id.clone(),
                            market_ticker: staged.ticker.clone(),
                            protocol: TransportProtocol::Rest,
                            endpoint_id: ctx.rest_endpoint_id.clone(),
                            observed_ts: observed_ts.0,
                            queried_client_order_id: staged.client_order_id.clone(),
                            raw: None,
                            parsed: None,
                            detail: format!(
                                "exact-id reconciliation failed before response headers: {query_error}"
                            ),
                        })
                        .await
                    {
                        record.transport_error = Some(format!(
                            "{}; unresolved-query evidence failed: {journal_error}",
                            record.transport_error.as_deref().unwrap_or("ambiguous response")
                        ));
                    }
                }
                let error = record
                    .transport_error
                    .as_deref()
                    .unwrap_or("ambiguous response");
                record.refusal = Some(format!(
                    "AMBIGUOUS attempted send ({error}); exact-id reconciliation query \
                     failed ({query_error}) — do not retry"
                ));
            }
        }
    }
    drop(attempt_permit);
    record
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A record shaped like whatever `handle_intent` returns at a given exit.
    fn rec() -> OrderRecord {
        OrderRecord {
            client_order_id: "c-1".into(),
            ticker: "KX-T".into(),
            price_cents: 97,
            count: 1,
            tier: Tier::Confirmed,
            dry_run: false,
            lost_race: false,
            refusal: None,
            order_status: None,
            order_id: None,
            audio_ts: 1,
            decision_ts: 2,
            exec_ts: 3,
            wire_state: OrderWireState::NotAttempted,
            send_ts: None,
            hdrs_ts: None,
            ack_ts: None,
            transport_protocol: None,
            protocol_status: None,
            exchange_accepted: None,
            order_terminal: None,
            exchange_observed_ts: None,
            reconciliation_ts: None,
            reconciliation_status: None,
            transport_error: None,
            parse_error: None,
            raw_response: None,
            raw_response_body_base64: None,
            reconciliation_response: None,
            fill: None,
            best_ask: None,
            book_as_of_ts: None,
            book_age_ns: None,
            ev_at_ask_cents: None,
            verify_verdict: None,
            verify_wait_ns: None,
            verify_gated: None,
        }
    }

    /// 2026-08-05: "23 orders placed" was reported when the true number was 0.
    /// Every non-fire exit returns a record that lands in `ExecReport.orders`,
    /// so the list length is an INTENT count. Only `send_ts` — stamped once,
    /// at `transport.submit_order`'s socket-write boundary — answers "did
    /// this reach Kalshi".
    #[test]
    fn no_early_return_counts_as_having_reached_the_wire() {
        // 1. non-crossing (LOST_RACE): the 26AUG05 morning's entire population.
        let mut non_crossing = rec();
        non_crossing.lost_race = true;
        // 2. EV gate.
        let mut ev = rec();
        ev.refusal = Some("EV gate".into());
        // 3. shadow mode.
        let mut dry = rec();
        dry.dry_run = true;
        dry.refusal = Some("dry_run".into());
        // 4. risk gate.
        let mut risk = rec();
        risk.refusal = Some("event notional would reach 194¢, cap 100¢".into());
        for (name, r) in [
            ("non-crossing", non_crossing),
            ("ev gate", ev),
            ("dry run", dry),
            ("risk gate", risk),
        ] {
            assert!(!r.reached_wire(), "{name} must NOT count as sent");
            assert!(!r.acked(), "{name} must NOT count as acked");
        }
    }

    /// The success path, and the ambiguous one: a send that timed out but was
    /// later proven on the book is acked even though the ack timestamps are
    /// absent — which is why `acked()` keys on `order_id`, not `ack_ts`.
    #[test]
    fn sent_and_acked_are_distinct_and_both_reachable() {
        let mut sent_only = rec();
        sent_only.send_ts = Some(10);
        assert!(sent_only.reached_wire());
        assert!(
            !sent_only.acked(),
            "a send without an order id is not acked"
        );

        let mut full = rec();
        full.send_ts = Some(10);
        full.hdrs_ts = Some(11);
        full.ack_ts = Some(12);
        full.order_id = Some("ord-1".into());
        full.order_status = Some("resting".into());
        assert!(full.reached_wire() && full.acked());

        // Ambiguous outcome: transport errored, status query found it resting.
        let mut recovered = rec();
        recovered.send_ts = Some(10);
        recovered.order_id = Some("ord-2".into());
        recovered.refusal = None;
        assert!(
            recovered.acked(),
            "a recovered order is on the book and must count"
        );
    }

    /// The report must carry the counts, not force every reader to re-derive
    /// them. `scripts/missed-arm-check.py` and `scripts/run-verdict.sh` both
    /// counted `len(orders)` and both therefore reported the 26AUG05 morning as
    /// 23 orders placed.
    #[test]
    fn run_json_counts_intents_sent_and_acked_separately() {
        let mut lost = rec();
        lost.lost_race = true;
        let mut refused = rec();
        refused.refusal = Some("event notional would reach 194¢, cap 100¢".into());
        let mut sent = rec();
        sent.send_ts = Some(10);
        let mut acked = rec();
        acked.send_ts = Some(10);
        acked.order_id = Some("ord-1".into());

        let report = ExecReport {
            orders: vec![lost, refused, sent, acked],
            lost_races: 1,
            observe_noncross: 0,
            recorder: crate::telemetry::LatencyRecorder::new(),
        };
        let json = crate::telemetry::build_run_json(
            "KX-EVENT",
            None,
            Some(&report),
            &crate::telemetry::ExitRecord::clean(),
        )
        .unwrap();

        assert_eq!(json["intents"], 4, "every processed intent is counted");
        assert_eq!(json["send_attempted"], 2);
        assert_eq!(json["sent"], 2, "only records with send_ts reached Kalshi");
        assert_eq!(
            json["acked"], 1,
            "only records with an order id were acknowledged"
        );
        assert_eq!(
            json["orders"].as_array().unwrap().len(),
            4,
            "records are still all present"
        );
    }

    #[test]
    fn fee_matches_verified_cases() {
        assert_eq!(fee_cents(1, 50), 2); // $0.0175 → ceil $0.02
        assert_eq!(fee_cents(100, 97), 21); // $0.2037 → ceil $0.21
        assert_eq!(fee_cents(1, 1), 1); // tiny but never zero when matched
        assert_eq!(fee_cents(0, 50), 0);
    }

    #[test]
    fn ev_gate_boundaries() {
        // cap 97, haircut 2 → gross 1¢/contract; fee eats it → fails at min_ev 2.
        assert!(!ev_gate(97, 100, 2, 2));
        // cap 90, no haircut → 10¢ gross/contract, fee ~0.7¢ → passes.
        assert!(ev_gate(90, 100, 0, 2));
        // cap + haircut ≥ 100 can never profit.
        assert!(!ev_gate(99, 100, 1, 0));
        // 1¢ probe order: gross 99¢, fee 1¢ → EV 98¢ ≥ 2¢. Passes.
        assert!(ev_gate(1, 1, 0, 2));
    }

    /// `ev_at_ask_cents` is shadow evidence, and its whole point is that it
    /// DISAGREES with the shipped gate: `ev_gate` scores every opportunity at
    /// the 97¢ cap, so a cheap ask is valued as if we paid 97¢ for it. Pin the
    /// gap so a future change that silently unifies them fails here.
    #[test]
    fn shadow_ev_prices_at_the_ask_not_the_cap() {
        let at = |ask: u32, count: u32| {
            (100i64 - ask as i64) * count as i64 - fee_cents(count, ask) as i64
        };

        // At the cap the two agree: 100 − 97 − 1 = 2¢, which is exactly the
        // min_ev_cents boundary risk.rs cites for why the ceiling is 97.
        assert_eq!(at(97, 1), 2);
        assert!(ev_gate(97, 1, 0, 2));

        // A 34¢ ask is worth 64¢ if the detection is right (66¢ gross − 2¢
        // fee), but the shipped gate never sees 34 — it evaluates the same
        // order at the cap and scores it 2¢. That 62¢ gap is the measurement
        // this field exists for.
        assert_eq!(fee_cents(1, 34), 2);
        assert_eq!(at(34, 1), 64);
        assert_eq!(at(34, 1) - at(97, 1), 62);

        // Fee scales with count, so the shadow value must too.
        assert_eq!(at(50, 2), 2 * 50 - fee_cents(2, 50) as i64);

        // THE LIMIT OF THIS FIELD, pinned as an assertion. Under p = 1 the
        // value is non-negative for every ask ≤ 100 and rises monotonically as
        // the ask falls, so price alone can NEVER make a gate refuse a
        // crossable order. Cheapness always looks better here — which is
        // exactly the reasoning that settlement refutes: measured P(YES | we
        // fired) is 29/29 at 95–100¢ and 5/19 at 20–59¢, so the cheap end of
        // this curve is where the money was actually lost. Refusing anything
        // requires the price-conditional P(wrong) term this does not carry.
        assert_eq!(at(100, 1), 0);
        assert_eq!(fee_cents(1, 100), 0); // P·(100−P) vanishes at the boundary
        for ask in 1..=100u32 {
            assert!(at(ask, 1) >= 0, "ask {ask} scored negative under p=1");
        }
        for ask in 2..=100u32 {
            assert!(at(ask - 1, 1) >= at(ask, 1), "not monotone at ask {ask}");
        }
        // 97 is the cap precisely because it is the last price clearing
        // min_ev_cents = 2 (risk.rs:44): 98 leaves 1¢, 99 leaves 0¢.
        assert_eq!((at(97, 1), at(98, 1), at(99, 1)), (2, 1, 0));
    }

    #[test]
    fn prestage_enumerates_all_tranches() {
        let kws = vec![crate::config::KeywordConfig {
            market_ticker: "KX-T".into(),
            phrase: "t".into(),
            variants: vec!["t".into()],
            max_price_cents: 97,
            budget_contracts: 200,
            policy: crate::config::Policy::TwoTier,
            rule_risk_haircut_cents: 2,
            fast_prefixes: vec![],
            fast_threshold: None,
            confirmed_threshold: None,
            boost: None,
            min_confidence_fast: 0.0,
            min_confidence_confirmed: 0.0,
        }];
        let map = prestage_orders("EV", &kws, StageWire::Rest);
        assert!(map.contains_key(&(0, Tier::Fast, 60)));
        assert!(map.contains_key(&(0, Tier::Confirmed, 140)));
        assert!(map.contains_key(&(0, Tier::Confirmed, 200)));
        let staged = &map[&(0, Tier::Fast, 60)];
        assert_eq!(
            staged.client_order_id,
            super::super::rest::deterministic_client_order_id("EV:KX-T:97:60:0:Fast")
        );
        // UUID-shaped and stable (idempotency).
        assert_eq!(staged.client_order_id.len(), 36);
        assert_eq!(staged.client_order_id.matches('-').count(), 4);
        let WireStaged::Rest { body } = &staged.wire else {
            panic!("StageWire::Rest must stage a REST body")
        };
        let v: serde_json::Value = serde_json::from_slice(body).unwrap();
        assert_eq!(v["price"], "0.97");
        assert_eq!(v["count"], "60.00");
    }

    #[test]
    fn prestage_matches_fast_only_policy() {
        let mut kw = crate::config::KeywordConfig {
            market_ticker: "KX-T".into(),
            phrase: "t".into(),
            variants: vec!["t".into()],
            max_price_cents: 1,
            budget_contracts: 1,
            policy: crate::config::Policy::FastOnly,
            rule_risk_haircut_cents: 0,
            fast_prefixes: vec![],
            fast_threshold: None,
            confirmed_threshold: None,
            boost: None,
            min_confidence_fast: 0.0,
            min_confidence_confirmed: 0.0,
        };
        let map = prestage_orders("EV", std::slice::from_ref(&kw), StageWire::Rest);
        // fast_only releases the FULL budget on the Fast tier.
        assert!(map.contains_key(&(0, Tier::Fast, 1)));
        assert_eq!(map.len(), 1);

        kw.policy = crate::config::Policy::ConfirmedOnly;
        let map = prestage_orders("EV", &[kw], StageWire::Rest);
        assert!(map.contains_key(&(0, Tier::Confirmed, 1)));
        assert_eq!(map.len(), 1);
    }
}
