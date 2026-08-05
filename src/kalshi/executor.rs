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

use serde::Serialize;
use tokio::sync::mpsc;

use super::book::BookCache;
use super::rest::RestClient;
use super::transport::{OrderTransport, StageWire, WireStaged};
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
}

/// Key: (keyword index, tier, tranche count) — the decision table emits a
/// known, finite set of tranches (fast %, confirmed remainder, confirmed
/// full), all enumerable at arm time.
pub type PreStagedMap = HashMap<(u16, Tier, u32), PreStaged>;

pub fn prestage_orders(event_id: &str, keywords: &[KeywordConfig], wire: StageWire) -> PreStagedMap {
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
            map.insert(
                (idx, tier, count),
                PreStaged {
                    wire: wire.stage(
                        &kw.market_ticker,
                        kw.max_price_cents as u32,
                        count,
                        client_order_id.clone(),
                    ),
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
    /// Response-headers/TTFB timestamp (send→hdrs vs hdrs→ack forensics).
    pub hdrs_ts: Option<u64>,
    pub ack_ts: Option<u64>,
    /// Book snapshot at decision time (LOST_RACE forensics, spec §5.6).
    /// (price_cents, qty_centi_contracts) at decision time — forensic only.
    pub best_ask: Option<(u32, u32)>,
    /// Age of that snapshot (exec_ts − book.as_of). Distinguishes "stale book"
    /// from "fast competitor": the MS RECE loss showed best_ask 20¢ at decision
    /// while the public tape had already swept to 98¢ ~250 ms earlier — without
    /// this field the two explanations were indistinguishable post-hoc.
    pub book_age_ns: Option<u64>,
}

impl OrderRecord {
    /// Did this record actually reach Kalshi?
    ///
    /// THE distinction the 2026-08-05 morning lost. `ExecReport.orders` holds
    /// every intent the executor processed, including the five non-fire early
    /// returns (non-crossing, EV gate, dry-run, risk refusal, send-failed), so
    /// `orders.len()` is an INTENT count and calling it "orders placed"
    /// reported 23 when the true number was 0. `send_ts` is stamped at exactly
    /// one place — immediately around `transport.send_order` — so it is the
    /// only honest answer.
    pub fn reached_wire(&self) -> bool {
        self.send_ts.is_some()
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
    ExecReport { orders, lost_races, observe_noncross, recorder }
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
        .cloned()
        .unwrap_or_else(|| {
            // Should never happen: tranches are enumerated at arm time.
            tracing::warn!(kw, ?intent.tier, intent.tranche_contracts, "no pre-staged body; building inline");
            let id = super::rest::deterministic_client_order_id(&format!("inline:{kw}:{:?}", intent.tier));
            PreStaged {
                wire: ctx.transport.stage_wire().stage(
                    &intent.market_ticker,
                    intent.max_price_cents as u32,
                    intent.tranche_contracts,
                    id.clone(),
                ),
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
        hdrs_ts: None,
        ack_ts: None,
        best_ask: None,
        book_age_ns: None,
    };

    // Local book read — never a blocking REST fetch (spec §5.6).
    let book = ctx.cache.read(&staged.ticker);
    let best_ask = book.as_ref().and_then(|b| b.best_ask());
    record.best_ask = best_ask;
    // seq 0 = the placeholder Book::default() (as_of 0) — no snapshot yet, so
    // an "age" would be time-since-boot noise.
    if let Some(b) = book.as_ref().filter(|b| b.seq > 0) {
        let age = exec_ts.saturating_since(b.as_of);
        record.book_age_ns = Some(age);
        pending.book_age = Some(age);
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
        pending.flush(recorder);
        return record;
    }

    // Fee/EV gate at the worst-case (fill entirely at the cap).
    let haircut = ctx.haircuts.get(&kw).copied().unwrap_or(0);
    if !ev_gate(staged.price_cents, staged.count, haircut, ctx.gate.cfg().min_ev_cents) {
        record.refusal = Some("EV gate".into());
        pending.flush(recorder);
        return record;
    }

    // Shadow mode stops here: the record shows exactly what would have been
    // sent (spec §9.5 live-shadow). Risk gating below is exercised by unit
    // tests and the live drills.
    if ctx.dry_run {
        record.refusal = Some("dry_run".into());
        pending.flush(recorder);
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
        pending.flush(recorder);
        return record;
    }

    // Fire. Debit ledger BEFORE the send so an ambiguous timeout still counts
    // as spent. The transport owns its per-send machinery (REST: presigned
    // headers, RSA off the hot path).
    ctx.gate.record_send(staged.price_cents, staged.count);

    match ctx.transport.send_order(&staged).await {
        Ok(outcome) => {
            let (ack, send_ts) = (outcome.ack, outcome.send_ts);
            pending.presign_age = outcome.presign_age_ns;
            record.send_ts = Some(send_ts.0);
            record.hdrs_ts = Some(ack.hdrs_ts.0);
            record.ack_ts = Some(ack.ack_ts.0);
            record.order_status = ack.order_status;
            record.order_id = ack.order_id;
            if let Some(id) = &record.order_id {
                ctx.registry.register(id.clone());
            }
            // One lock, after the wire: the deferred pre-send edges plus the
            // TTFB split of send→ack (all recorded on the success path so
            // e2e's all-edges-populated assertion holds).
            let mut rec = recorder.lock().unwrap();
            pending.record_into(&mut rec);
            rec.record("exec→send", send_ts.saturating_since(exec_ts));
            rec.record("send→hdrs", ack.hdrs_ts.saturating_since(send_ts));
            rec.record("hdrs→ack", ack.ack_ts.saturating_since(ack.hdrs_ts));
            rec.record("send→ack", ack.ack_ts.saturating_since(send_ts));
            rec.record("decision→ack", ack.ack_ts.saturating_since(intent.decision_ts));
            rec.record("capture→ack", ack.ack_ts.saturating_since(intent.audio_ts));
        }
        Err(e) => {
            // Ambiguous outcome: query by client id before assuming failure.
            // Never blind-retry (idempotent id makes a retry safe server-side,
            // but a query is cheaper and unambiguous).
            pending.flush(recorder);
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
            send_ts: None,
            hdrs_ts: None,
            ack_ts: None,
            best_ask: None,
            book_age_ns: None,
        }
    }

    /// 2026-08-05: "23 orders placed" was reported when the true number was 0.
    /// Every non-fire exit returns a record that lands in `ExecReport.orders`,
    /// so the list length is an INTENT count. Only `send_ts` — stamped once,
    /// around `transport.send_order` — answers "did this reach Kalshi".
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
        // 5. send failed and the status query proved it never landed.
        let mut absent = rec();
        absent.refusal = Some("send failed, not on book".into());

        for (name, r) in [
            ("non-crossing", non_crossing),
            ("ev gate", ev),
            ("dry run", dry),
            ("risk gate", risk),
            ("send failed", absent),
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
        assert!(!sent_only.acked(), "a send without an order id is not acked");

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
        assert!(recovered.acked(), "a recovered order is on the book and must count");
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
        assert_eq!(json["sent"], 2, "only records with send_ts reached Kalshi");
        assert_eq!(json["acked"], 1, "only records with an order id were acknowledged");
        assert_eq!(json["orders"].as_array().unwrap().len(), 4, "records are still all present");
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
            super::super::rest::deterministic_client_order_id("EV:0:Fast")
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
