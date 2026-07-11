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

use bytes::Bytes;
use serde::Serialize;
use tokio::sync::mpsc;

use super::auth::Presigner;
use super::book::BookCache;
use super::rest::{OrderBodyV2, RestClient};
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

#[derive(Clone)]
pub struct PreStaged {
    pub body: Bytes,
    pub ticker: String,
    pub price_cents: u32,
    pub count: u32,
    pub client_order_id: String,
}

/// Key: (keyword index, tier, tranche count) — the decision table emits a
/// known, finite set of tranches (fast %, confirmed remainder, confirmed
/// full), all enumerable at arm time.
pub type PreStagedMap = HashMap<(u16, Tier, u32), PreStaged>;

pub fn prestage_orders(event_id: &str, keywords: &[KeywordConfig]) -> PreStagedMap {
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
            let client_order_id =
                super::rest::deterministic_client_order_id(&format!("{event_id}:{idx}:{tier:?}"));
            let body = OrderBodyV2::ioc_buy_yes(
                &kw.market_ticker,
                kw.max_price_cents as u32,
                count,
                client_order_id.clone(),
            );
            map.insert(
                (idx, tier, count),
                PreStaged {
                    body: body.to_bytes(),
                    ticker: kw.market_ticker.clone(),
                    price_cents: kw.max_price_cents as u32,
                    count,
                    client_order_id,
                },
            );
        }
    }
    map
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
    pub send_ts: Option<u64>,
    pub ack_ts: Option<u64>,
    /// Book snapshot at decision time (LOST_RACE forensics, spec §5.6).
    pub best_ask: Option<(u32, u32)>,
}

pub struct ExecCtx {
    pub rest: RestClient,
    pub presigner: Arc<Presigner>,
    pub cache: Arc<BookCache>,
    pub gate: Arc<RiskGate>,
    pub prestaged: PreStagedMap,
    pub haircuts: HashMap<u16, u32>, // keyword idx → rule_risk_haircut_cents
    pub dry_run: bool,
    /// Every acked order id lands here; the kill switch cancels exactly this
    /// set (never account-wide — other systems' orders share this account).
    pub registry: super::OrderRegistry,
}

pub struct ExecReport {
    pub orders: Vec<OrderRecord>,
    pub lost_races: u32,
    pub recorder: LatencyRecorder,
}

/// Runs until the intent sender is dropped. Intents are processed
/// concurrently (Powell burst case) — each spawns onto the runtime; results
/// aggregate under short-held mutexes that are never on the audio thread.
pub async fn run_executor(mut rx: mpsc::Receiver<DecisionIntent>, ctx: Arc<ExecCtx>) -> ExecReport {
    let orders = Arc::new(Mutex::new(Vec::<OrderRecord>::new()));
    let recorder = Arc::new(Mutex::new(LatencyRecorder::new()));
    let mut tasks = tokio::task::JoinSet::new();

    while let Some(intent) = rx.recv().await {
        let (ctx, orders, recorder) = (ctx.clone(), orders.clone(), recorder.clone());
        tasks.spawn(async move {
            let record = handle_intent(&ctx, &recorder, intent).await;
            orders.lock().unwrap().push(record);
        });
    }
    while tasks.join_next().await.is_some() {}

    let orders = Arc::try_unwrap(orders).ok().expect("sole owner").into_inner().unwrap();
    let lost_races = orders.iter().filter(|o| o.lost_race).count() as u32;
    let recorder = Arc::try_unwrap(recorder).ok().expect("sole owner").into_inner().unwrap();
    ExecReport { orders, lost_races, recorder }
}

async fn handle_intent(
    ctx: &ExecCtx,
    recorder: &Mutex<LatencyRecorder>,
    intent: DecisionIntent,
) -> OrderRecord {
    let exec_ts = MonotonicNs::now();
    recorder
        .lock()
        .unwrap()
        .record("decision→exec", exec_ts.saturating_since(intent.decision_ts));

    let kw = intent.keyword.0;
    let staged = ctx
        .prestaged
        .get(&(kw, intent.tier, intent.tranche_contracts))
        .cloned()
        .unwrap_or_else(|| {
            // Should never happen: tranches are enumerated at arm time.
            tracing::warn!(kw, ?intent.tier, intent.tranche_contracts, "no pre-staged body; building inline");
            let id = super::rest::deterministic_client_order_id(&format!("inline:{kw}:{:?}", intent.tier));
            let body = OrderBodyV2::ioc_buy_yes(
                &intent.market_ticker,
                intent.max_price_cents as u32,
                intent.tranche_contracts,
                id.clone(),
            );
            PreStaged {
                body: body.to_bytes(),
                ticker: intent.market_ticker.clone(),
                price_cents: intent.max_price_cents as u32,
                count: intent.tranche_contracts,
                client_order_id: id,
            }
        });

    let mut record = OrderRecord {
        client_order_id: staged.client_order_id.clone(),
        ticker: staged.ticker.clone(),
        price_cents: staged.price_cents,
        count: staged.count,
        tier: intent.tier,
        dry_run: ctx.dry_run,
        lost_race: false,
        refusal: None,
        order_status: None,
        order_id: None,
        audio_ts: intent.audio_ts.0,
        decision_ts: intent.decision_ts.0,
        exec_ts: exec_ts.0,
        send_ts: None,
        ack_ts: None,
        best_ask: None,
    };

    // Local book read — never a blocking REST fetch (spec §5.6).
    let book = ctx.cache.read(&staged.ticker);
    let best_ask = book.as_ref().and_then(|b| b.best_ask());
    record.best_ask = best_ask;

    // LOST_RACE: nothing offered at or under the cap. Primary edge-decay metric.
    let swept = match best_ask {
        Some((price, _)) if price <= staged.price_cents => false,
        _ => true,
    };
    if swept {
        record.lost_race = true;
        tracing::warn!(ticker = %staged.ticker, ?best_ask, cap = staged.price_cents, "LOST_RACE");
        return record;
    }

    // Fee/EV gate at the worst-case (fill entirely at the cap).
    let haircut = ctx.haircuts.get(&kw).copied().unwrap_or(0);
    if !ev_gate(staged.price_cents, staged.count, haircut, ctx.gate.cfg().min_ev_cents) {
        record.refusal = Some("EV gate".into());
        return record;
    }

    // Shadow mode stops here: the record shows exactly what would have been
    // sent (spec §9.5 live-shadow). Risk gating below is exercised by unit
    // tests and the live drills.
    if ctx.dry_run {
        record.refusal = Some("dry_run".into());
        return record;
    }

    // Risk gate (kill, armed, allowlist, staleness, caps, ledger).
    let audio_age = MonotonicNs::now().saturating_since(intent.audio_ts);
    if let Err(refusal) = ctx.gate.check_order(
        &staged.ticker,
        staged.price_cents,
        staged.count,
        ctx.cache.feed_age_ns(),
        audio_age,
    ) {
        record.refusal = Some(refusal.to_string());
        tracing::error!(ticker = %staged.ticker, "risk refusal: {refusal}");
        return record;
    }

    // Fire. Presigned headers (RSA off the hot path); debit ledger BEFORE the
    // send so an ambiguous timeout still counts as spent.
    let (headers, presign_age) = ctx.presigner.get();
    recorder.lock().unwrap().record("presign_age", presign_age);
    ctx.gate.record_send(staged.price_cents, staged.count);

    match ctx.rest.create_order_presigned(staged.body.clone(), headers).await {
        Ok((ack, send_ts)) => {
            record.send_ts = Some(send_ts.0);
            record.ack_ts = Some(ack.ack_ts.0);
            record.order_status = ack.order_status;
            record.order_id = ack.order_id;
            if let Some(id) = &record.order_id {
                ctx.registry.register(id.clone());
            }
            let mut rec = recorder.lock().unwrap();
            rec.record("exec→send", send_ts.saturating_since(exec_ts));
            rec.record("send→ack", ack.ack_ts.saturating_since(send_ts));
            rec.record("decision→ack", ack.ack_ts.saturating_since(intent.decision_ts));
            rec.record("capture→ack", ack.ack_ts.saturating_since(intent.audio_ts));
        }
        Err(e) => {
            // Ambiguous outcome: query by client id before assuming failure.
            // Never blind-retry (idempotent id makes a retry safe server-side,
            // but a query is cheaper and unambiguous).
            tracing::error!("order send error: {e}; querying status");
            match ctx.rest.order_by_client_id(&staged.client_order_id).await {
                Ok(Some(order)) => {
                    record.order_status = order["status"].as_str().map(String::from);
                    record.order_id = order["order_id"].as_str().map(String::from);
                    if let Some(id) = &record.order_id {
                        ctx.registry.register(id.clone());
                    }
                    tracing::warn!("order WAS accepted despite send error");
                }
                Ok(None) => record.refusal = Some(format!("send failed, not on book: {e}")),
                Err(q) => record.refusal = Some(format!("send failed ({e}); status query failed ({q})")),
            }
        }
    }
    record
}

#[cfg(test)]
mod tests {
    use super::*;

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
        let map = prestage_orders("EV", &kws);
        assert!(map.contains_key(&(0, Tier::Fast, 60)));
        assert!(map.contains_key(&(0, Tier::Confirmed, 140)));
        assert!(map.contains_key(&(0, Tier::Confirmed, 200)));
        let staged = &map[&(0, Tier::Fast, 60)];
        assert_eq!(
            staged.client_order_id,
            super::super::rest::deterministic_client_order_id("EV:0:Fast")
        );
        // UUID-shaped and stable (idempotency).
        assert_eq!(staged.client_order_id.len(), 36);
        assert_eq!(staged.client_order_id.matches('-').count(), 4);
        let v: serde_json::Value = serde_json::from_slice(&staged.body).unwrap();
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
        let map = prestage_orders("EV", std::slice::from_ref(&kw));
        // fast_only releases the FULL budget on the Fast tier.
        assert!(map.contains_key(&(0, Tier::Fast, 1)));
        assert_eq!(map.len(), 1);

        kw.policy = crate::config::Policy::ConfirmedOnly;
        let map = prestage_orders("EV", &[kw]);
        assert!(map.contains_key(&(0, Tier::Confirmed, 1)));
        assert_eq!(map.len(), 1);
    }
}
