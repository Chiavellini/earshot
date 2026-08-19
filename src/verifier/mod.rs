//! Fill-path verifier (detection-accuracy program D2, 2026-08-18).
//!
//! WHY. Scored against a per-leg reference transcript, every false fire on
//! 26AUG18 was a morphological/phonetic neighbour of the keyword —
//! `innovative`→innovation fired the HD 92¢ fill, `Arc'teryx…quarter`→tariff
//! fired the AS 96¢ fill — at the same engine confidence as the true fires
//! (`state/_day/DETECTION-SCORE-26AUG18.md`). No threshold separates them; a
//! second, unbiased decode of the last few seconds does: the word is either in
//! the text or it is not.
//!
//! WHERE. The detect thread posts a `VerifyRequest` (the leg's last `tail_ms`
//! of PCM) AFTER the fire-first `try_send(intent)` — nothing new sits on the
//! critical span, and the 1¢ observe path never waits. The verifier thread
//! decodes and publishes a `Verdict` into a per-(keyword, tier) slot. The
//! executor, and only for a FILL-priced intent (`price > OBSERVE_PRICE_CENTS`)
//! whose ticker is opted in, `await_verdict`s up to `budget_ms` past
//! `decision_ts` when `mode = gate`; in `shadow` it never waits. Every request
//! produces a `VerdictRecord` row in the run report so precision-per-verdict
//! is scorable before anything is gated. `decision.rs` is untouched: the
//! one-shot table has already consumed the keyword; a vetoed fill leaves it
//! consumed (v1 — re-arm-on-veto is a later, decision-layer change).
//!
//! Fanout: exactly one intent per (keyword, tier) exists per run (the legs
//! share one one-shot table), so gating is once per keyword on the winning
//! leg's tail; other legs' detections are verified for evidence only when
//! `sample = all_confirmed`.

pub mod report;
pub mod tail;
pub mod transcriber;

use std::sync::atomic::{AtomicU64, AtomicU8, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Instant;

use serde::Serialize;

use crate::clock::MonotonicNs;
use crate::config::{
    KeywordConfig, VerifierMode, VerifierOnTimeout, VerifierSample, VerifierSection,
};
use crate::detector::text_match::VariantMatcher;
use crate::types::{KeywordId, SourceId, Tier};
use transcriber::{SherpaOfflineTranscriber, TailTranscriber};

pub use tail::PcmTail;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct VerifyTicket(pub u64);

pub struct VerifyRequest {
    pub keyword: KeywordId,
    pub tier: Tier,
    pub intent_bearing: bool,
    pub source: SourceId,
    pub leg: String,
    pub audio_pos_ms: u64,
    pub audio_consumed_ms: u64,
    pub pcm: Arc<[i16]>,
    pub sample_rate_hz: u32,
    pub decision_ts: Option<MonotonicNs>,
}

struct Queued {
    ticket: VerifyTicket,
    req: VerifyRequest,
    requested_ts: MonotonicNs,
    queued_at: Instant,
}

enum Msg {
    Req(Queued),
    /// Terminal marker: lets `join` finish even if a `VerifierClient` clone is
    /// still alive somewhere (an executor context, a watcher).
    Shutdown,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Verdict {
    Confirmed { variant: String },
    Rejected,
    Error { message: String },
}

#[derive(Debug, Clone, Serialize)]
pub struct VerdictRecord {
    pub ticket: u64,
    pub keyword: u16,
    pub phrase: String,
    pub tier: Tier,
    pub leg: String,
    pub source: u16,
    pub intent_bearing: bool,
    pub verdict: Verdict,
    pub transcript: String,
    pub tail_ms: u32,
    pub audio_pos_ms: u64,
    pub audio_consumed_ms: u64,
    pub queue_wait_us: u64,
    pub decode_us: u64,
    /// `verdict_ts − decision_ts` for intent-bearing requests: what the fill
    /// path would have waited.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub since_decision_us: Option<u64>,
    pub requested_ts: u64,
    pub verdict_ts: u64,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Await {
    Ready(Verdict),
    Timeout,
    Dropped,
    NotRequested,
}

const SLOT_IDLE: u8 = 0;
const SLOT_PENDING: u8 = 1;
const SLOT_READY: u8 = 2;
const SLOT_DROPPED: u8 = 3;

struct Slot {
    state: AtomicU8,
    verdict: Mutex<Option<Verdict>>,
    notify: tokio::sync::Notify,
}

impl Slot {
    fn new() -> Self {
        Slot {
            state: AtomicU8::new(SLOT_IDLE),
            verdict: Mutex::new(None),
            notify: tokio::sync::Notify::new(),
        }
    }
}

struct Inner {
    tx: mpsc::SyncSender<Msg>,
    slots: Vec<[Slot; 2]>,
    next_ticket: AtomicU64,
    opt_in: Vec<bool>,
    mode: VerifierMode,
    sample: VerifierSample,
    on_timeout: VerifierOnTimeout,
    tail_ms: u32,
    budget_ms: u32,
    dropped: AtomicU64,
}

/// Cheap clone handle used by the detect thread(s) and the executor.
#[derive(Clone)]
pub struct VerifierClient(Arc<Inner>);

fn tier_idx(tier: Tier) -> usize {
    match tier {
        Tier::Fast => 0,
        Tier::Confirmed => 1,
    }
}

impl VerifierClient {
    pub fn mode(&self) -> VerifierMode {
        self.0.mode
    }
    pub fn gates(&self) -> bool {
        self.0.mode == VerifierMode::Gate
    }
    pub fn on_timeout(&self) -> VerifierOnTimeout {
        self.0.on_timeout
    }
    pub fn tail_ms(&self) -> u32 {
        self.0.tail_ms
    }
    pub fn budget_ms(&self) -> u32 {
        self.0.budget_ms
    }
    /// Is this keyword's FILL intent gated (ticker opted in)?
    pub fn gated_keyword(&self, keyword: KeywordId) -> bool {
        self.gates()
            && self
                .0
                .opt_in
                .get(keyword.0 as usize)
                .copied()
                .unwrap_or(false)
    }
    /// Should the detect thread post a request for this detection?
    pub fn wants(&self, tier: Tier, intent_bearing: bool) -> bool {
        if tier != Tier::Confirmed || self.0.mode == VerifierMode::Off {
            return false;
        }
        match self.0.sample {
            VerifierSample::Intents => intent_bearing,
            VerifierSample::AllConfirmed => true,
        }
    }

    /// Detect-thread side. Non-blocking: a full queue drops the request and
    /// marks the slot Dropped so the executor never waits on nothing.
    pub fn request(&self, req: VerifyRequest) -> Result<VerifyTicket, ()> {
        let ticket = VerifyTicket(self.0.next_ticket.fetch_add(1, Ordering::AcqRel) + 1);
        let slot = &self.0.slots[req.keyword.0 as usize][tier_idx(req.tier)];
        let intent_bearing = req.intent_bearing;
        if intent_bearing {
            *slot.verdict.lock().unwrap() = None;
            slot.state.store(SLOT_PENDING, Ordering::Release);
        }
        let q = Queued {
            ticket,
            req,
            requested_ts: MonotonicNs::now(),
            queued_at: Instant::now(),
        };
        match self.0.tx.try_send(Msg::Req(q)) {
            Ok(()) => Ok(ticket),
            Err(_) => {
                self.0.dropped.fetch_add(1, Ordering::Relaxed);
                if intent_bearing {
                    slot.state.store(SLOT_DROPPED, Ordering::Release);
                    slot.notify.notify_waiters();
                }
                Err(())
            }
        }
    }

    /// Executor side: wait for the (keyword, tier) verdict until `deadline`.
    pub async fn await_verdict(
        &self,
        keyword: KeywordId,
        tier: Tier,
        deadline: tokio::time::Instant,
    ) -> Await {
        let slot = &self.0.slots[keyword.0 as usize][tier_idx(tier)];
        loop {
            match slot.state.load(Ordering::Acquire) {
                SLOT_IDLE => return Await::NotRequested,
                SLOT_DROPPED => return Await::Dropped,
                SLOT_READY => {
                    return slot
                        .verdict
                        .lock()
                        .unwrap()
                        .clone()
                        .map(Await::Ready)
                        .unwrap_or(Await::Dropped)
                }
                _ => {}
            }
            let notified = slot.notify.notified();
            // re-check after registering interest so a publish between the load
            // and the await is not lost (Notify stores one permit anyway)
            match slot.state.load(Ordering::Acquire) {
                SLOT_PENDING => {}
                _ => continue,
            }
            if tokio::time::timeout_at(deadline, notified).await.is_err() {
                return Await::Timeout;
            }
        }
    }

    pub fn dropped(&self) -> u64 {
        self.0.dropped.load(Ordering::Relaxed)
    }

    /// Ask the verifier thread to finish after draining what is queued. Call
    /// once the pipeline has ended; then `VerifierHandle::join`.
    pub fn shutdown(&self) {
        let _ = self.0.tx.send(Msg::Shutdown);
    }
}

pub struct VerifierHandle {
    thread: std::thread::JoinHandle<VerifierReport>,
}

impl VerifierHandle {
    /// Join AFTER the last `VerifierClient` clone that could still request is
    /// dropped (the pipeline is done); the thread exits when the queue closes.
    pub fn join(self) -> VerifierReport {
        self.thread.join().expect("verifier thread panicked")
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct VerifierReport {
    pub mode: VerifierMode,
    pub sample: VerifierSample,
    pub model_dir: String,
    pub engine: &'static str,
    pub tail_ms: u32,
    pub budget_ms: u32,
    pub rows: Vec<VerdictRecord>,
    pub dropped: u64,
    pub decode_p50_us: u64,
    pub decode_p95_us: u64,
}

pub struct VerifierBuild {
    pub client: VerifierClient,
    pub handle: VerifierHandle,
}

/// Build from config: constructs the sherpa offline recognizer (refuses on any
/// missing asset — never arm with a dead verifier).
pub fn build_verifier(
    section: &VerifierSection,
    keywords: &[KeywordConfig],
) -> Result<VerifierBuild, String> {
    let transcriber = SherpaOfflineTranscriber::new(
        &section.model_dir,
        &section.model_type,
        section.num_threads,
    )?;
    Ok(build_with_transcriber(
        section,
        keywords,
        Box::new(transcriber),
    ))
}

/// Test seam: any `TailTranscriber`.
pub fn build_with_transcriber(
    section: &VerifierSection,
    keywords: &[KeywordConfig],
    mut transcriber: Box<dyn TailTranscriber>,
) -> VerifierBuild {
    let (tx, rx) = mpsc::sync_channel::<Msg>(8);
    let all = section.tickers.iter().any(|t| t == "*");
    let opt_in: Vec<bool> = keywords
        .iter()
        .map(|k| all || section.tickers.iter().any(|t| t == &k.market_ticker))
        .collect();
    let inner = Arc::new(Inner {
        tx,
        slots: keywords
            .iter()
            .map(|_| [Slot::new(), Slot::new()])
            .collect(),
        next_ticket: AtomicU64::new(0),
        opt_in,
        mode: section.mode,
        sample: section.sample,
        on_timeout: section.on_timeout,
        tail_ms: section.tail_ms,
        budget_ms: section.budget_ms,
        dropped: AtomicU64::new(0),
    });
    let client = VerifierClient(inner.clone());
    let matcher = VariantMatcher::from_keywords(keywords);
    let phrases: Vec<String> = keywords.iter().map(|k| k.phrase.clone()).collect();
    let mode = section.mode;
    let sample = section.sample;
    let model_dir = section.model_dir.display().to_string();
    let tail_ms = section.tail_ms;
    let budget_ms = section.budget_ms;
    let engine = transcriber.name();
    let thread = std::thread::Builder::new()
        .name("verifier".into())
        .spawn(move || {
            let mut rows: Vec<VerdictRecord> = Vec::new();
            let mut decode_us: Vec<u64> = Vec::new();
            while let Ok(msg) = rx.recv() {
                let q = match msg {
                    Msg::Req(q) => q,
                    Msg::Shutdown => break,
                };
                let queue_wait_us = q.queued_at.elapsed().as_micros() as u64;
                let t0 = Instant::now();
                let (verdict, transcript) =
                    match transcriber.transcribe(&q.req.pcm, q.req.sample_rate_hz) {
                        // An EMPTY decode is not evidence of absence — Parakeet-TDT
                        // returned nothing for 2 of 80 real 3.5 s windows on HD-box
                        // 26AUG18 (both true fires). It is an instrument miss, never a veto.
                        Ok(text) if text.trim().is_empty() => (
                            Verdict::Error {
                                message: "empty transcript".into(),
                            },
                            text,
                        ),
                        Ok(text) => {
                            let v = match matcher.contains(q.req.keyword, &text) {
                                Some(vi) => Verdict::Confirmed {
                                    variant: variant_text(&phrases, &matcher, q.req.keyword, vi),
                                },
                                None => Verdict::Rejected,
                            };
                            (v, text)
                        }
                        Err(message) => (Verdict::Error { message }, String::new()),
                    };
                let dus = t0.elapsed().as_micros() as u64;
                decode_us.push(dus);
                let now = MonotonicNs::now();
                if q.req.intent_bearing {
                    let slot = &inner.slots[q.req.keyword.0 as usize][tier_idx(q.req.tier)];
                    *slot.verdict.lock().unwrap() = Some(verdict.clone());
                    slot.state.store(SLOT_READY, Ordering::Release);
                    slot.notify.notify_waiters();
                    slot.notify.notify_one();
                }
                let tail_len_ms =
                    (q.req.pcm.len() as u64 * 1000 / q.req.sample_rate_hz.max(1) as u64) as u32;
                tracing::info!(
                    keyword = q.req.keyword.0,
                    phrase = %phrases.get(q.req.keyword.0 as usize).cloned().unwrap_or_default(),
                    leg = %q.req.leg,
                    ?verdict,
                    decode_ms = dus / 1000,
                    intent_bearing = q.req.intent_bearing,
                    "verifier verdict"
                );
                rows.push(VerdictRecord {
                    ticket: q.ticket.0,
                    keyword: q.req.keyword.0,
                    phrase: phrases
                        .get(q.req.keyword.0 as usize)
                        .cloned()
                        .unwrap_or_default(),
                    tier: q.req.tier,
                    leg: q.req.leg.clone(),
                    source: q.req.source.0,
                    intent_bearing: q.req.intent_bearing,
                    verdict,
                    transcript,
                    tail_ms: tail_len_ms,
                    audio_pos_ms: q.req.audio_pos_ms,
                    audio_consumed_ms: q.req.audio_consumed_ms,
                    queue_wait_us,
                    decode_us: dus,
                    since_decision_us: q.req.decision_ts.map(|d| now.saturating_since(d) / 1000),
                    requested_ts: q.requested_ts.0,
                    verdict_ts: now.0,
                });
            }
            decode_us.sort_unstable();
            let pct = |p: f64| -> u64 {
                if decode_us.is_empty() {
                    0
                } else {
                    decode_us[((decode_us.len() - 1) as f64 * p) as usize]
                }
            };
            VerifierReport {
                mode,
                sample,
                model_dir,
                engine,
                tail_ms,
                budget_ms,
                dropped: inner.dropped.load(Ordering::Relaxed),
                decode_p50_us: pct(0.5),
                decode_p95_us: pct(0.95),
                rows,
            }
        })
        .expect("spawn verifier thread");
    VerifierBuild {
        client,
        handle: VerifierHandle { thread },
    }
}

fn variant_text(phrases: &[String], _matcher: &VariantMatcher, kw: KeywordId, vi: usize) -> String {
    format!(
        "{}#{vi}",
        phrases.get(kw.0 as usize).cloned().unwrap_or_default()
    )
}

#[cfg(test)]
mod tests {
    use super::transcriber::MockTranscriber;
    use super::*;
    use crate::config::Policy;
    use std::time::Duration;

    fn kw(phrase: &str, ticker: &str) -> KeywordConfig {
        KeywordConfig {
            market_ticker: ticker.into(),
            phrase: phrase.into(),
            variants: vec![phrase.into(), format!("{phrase}s")],
            max_price_cents: 97,
            budget_contracts: 1,
            policy: Policy::ConfirmedOnly,
            rule_risk_haircut_cents: 0,
            fast_prefixes: vec![],
            fast_threshold: None,
            confirmed_threshold: None,
            boost: None,
            min_confidence_fast: 0.0,
            min_confidence_confirmed: 0.0,
        }
    }

    fn section(mode: &str, tickers: &[&str], sample: &str) -> VerifierSection {
        toml::from_str(&format!(
            "mode = {mode:?}\nmodel_dir = \"/nonexistent\"\nsample = {sample:?}\ntickers = [{}]\n",
            tickers
                .iter()
                .map(|t| format!("{t:?}"))
                .collect::<Vec<_>>()
                .join(",")
        ))
        .unwrap()
    }

    fn req(kw: u16, intent: bool, pcm: &[i16]) -> VerifyRequest {
        VerifyRequest {
            keyword: KeywordId(kw),
            tier: Tier::Confirmed,
            intent_bearing: intent,
            source: SourceId(0),
            leg: "t".into(),
            audio_pos_ms: 1000,
            audio_consumed_ms: 1000,
            pcm: Arc::from(pcm),
            sample_rate_hz: 16_000,
            decision_ts: Some(MonotonicNs::now()),
        }
    }

    fn mock(text_for: impl Fn(&[i16]) -> String + Send + 'static) -> Box<dyn TailTranscriber> {
        Box::new(MockTranscriber(Box::new(move |p| Ok(text_for(p)))))
    }

    #[tokio::test]
    async fn verdict_ready_before_and_after_wait_resolves() {
        let kws = [kw("tariff", "T-A"), kw("weather", "T-B")];
        let b = build_with_transcriber(
            &section("gate", &["*"], "intents"),
            &kws,
            mock(|p| {
                if p.first() == Some(&1) {
                    "some tariffs ahead".into()
                } else {
                    "whether or not".into()
                }
            }),
        );
        assert!(b.client.gates() && b.client.gated_keyword(KeywordId(0)));
        // ready before wait
        b.client.request(req(0, true, &[1, 1, 1])).unwrap();
        std::thread::sleep(Duration::from_millis(50));
        let dl = tokio::time::Instant::now() + Duration::from_millis(500);
        assert_eq!(
            b.client
                .await_verdict(KeywordId(0), Tier::Confirmed, dl)
                .await,
            Await::Ready(Verdict::Confirmed {
                variant: "tariff#1".into()
            })
        );
        // wait before ready: post from another thread after a delay
        let c = b.client.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(60));
            c.request(req(1, true, &[2, 2])).unwrap();
        });
        let dl = tokio::time::Instant::now() + Duration::from_millis(1000);
        // slot is Idle until the request lands → NotRequested first; poll until pending/ready
        let mut got = Await::NotRequested;
        for _ in 0..50 {
            got = b
                .client
                .await_verdict(KeywordId(1), Tier::Confirmed, dl)
                .await;
            if got != Await::NotRequested {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        assert_eq!(got, Await::Ready(Verdict::Rejected));
        // not-requested keyword
        assert_eq!(
            b.client.await_verdict(KeywordId(0), Tier::Fast, dl).await,
            Await::NotRequested
        );
        b.client.shutdown();
        let rep = b.handle.join();
        assert_eq!(rep.rows.len(), 2);
        assert!(rep.rows.iter().all(|r| r.intent_bearing));
    }

    #[tokio::test]
    async fn slow_verifier_times_out_and_full_queue_drops() {
        let kws = [kw("tariff", "T-A")];
        let b = build_with_transcriber(
            &section("gate", &["T-A"], "all_confirmed"),
            &kws,
            Box::new(MockTranscriber(Box::new(|_| {
                std::thread::sleep(Duration::from_millis(200));
                Ok("x".into())
            }))),
        );
        b.client.request(req(0, true, &[0; 10])).unwrap();
        let dl = tokio::time::Instant::now() + Duration::from_millis(30);
        assert_eq!(
            b.client
                .await_verdict(KeywordId(0), Tier::Confirmed, dl)
                .await,
            Await::Timeout
        );
        // flood the bounded queue (cap 8) with non-intent requests → drops counted, never blocks
        let mut dropped = 0;
        for _ in 0..40 {
            if b.client.request(req(0, false, &[0; 10])).is_err() {
                dropped += 1;
            }
        }
        assert!(dropped > 0);
        assert_eq!(b.client.dropped(), dropped as u64);
        // wants(): all_confirmed accepts non-intent Confirmed, never Fast
        assert!(b.client.wants(Tier::Confirmed, false));
        assert!(!b.client.wants(Tier::Fast, true));
        b.client.shutdown();
        let rep = b.handle.join();
        assert!(rep.decode_p50_us >= 200_000);
        assert_eq!(rep.dropped, dropped as u64);
    }

    #[test]
    fn shadow_and_sampling_semantics() {
        let kws = [kw("tariff", "T-A")];
        let s = section("shadow", &[], "intents");
        let b = build_with_transcriber(&s, &kws, mock(|_| "tariff".into()));
        assert!(!b.client.gates());
        assert!(!b.client.gated_keyword(KeywordId(0)));
        assert!(b.client.wants(Tier::Confirmed, true));
        assert!(
            !b.client.wants(Tier::Confirmed, false),
            "intents sampling skips non-intent detections"
        );
        let off = section("off", &[], "intents");
        let b2 = build_with_transcriber(&off, &kws, mock(|_| "tariff".into()));
        assert!(!b2.client.wants(Tier::Confirmed, true));
    }
}
