//! Risk & controls (spec §5.8). Hard caps are consts — config can only
//! tighten downward, never exceed them. Checked before every send, in order:
//! killed? → armed? → staleness → caps → ledger.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

use crate::clock::MonotonicNs;

/// Hard ceilings (cents). Not user-overridable upward at runtime (spec §11).
pub const HARD_MAX_ORDER_NOTIONAL_CENTS: u64 = 50_000; // $500
pub const HARD_MAX_EVENT_NOTIONAL_CENTS: u64 = 200_000; // $2,000
pub const HARD_DAILY_LOSS_CENTS: u64 = 100_000; // $1,000
/// M2 posture: cumulative real-money spend across ALL live drills/sessions
/// is capped at $1.00, enforced here and persisted across runs.
pub const PROBE_LEDGER_CAP_CENTS: u64 = 100;

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RiskConfig {
    #[serde(default = "d_order")]
    pub max_order_notional_cents: u64,
    #[serde(default = "d_event")]
    pub max_event_notional_cents: u64,
    #[serde(default = "d_loss")]
    pub daily_loss_cap_cents: u64,
    #[serde(default = "d_book_stale")]
    pub book_stale_ms: u64,
    #[serde(default = "d_audio_stale")]
    pub audio_stale_ms: u64,
    #[serde(default = "d_min_ev")]
    pub min_ev_cents: u32,
    #[serde(default)]
    pub allowlist_tickers: Vec<String>,
}

fn d_order() -> u64 { 100 }
fn d_event() -> u64 { 100 }
fn d_loss() -> u64 { 100 }
fn d_book_stale() -> u64 { 2000 }
fn d_audio_stale() -> u64 { 1000 }
fn d_min_ev() -> u32 { 2 }

impl Default for RiskConfig {
    fn default() -> Self {
        RiskConfig {
            max_order_notional_cents: d_order(),
            max_event_notional_cents: d_event(),
            daily_loss_cap_cents: d_loss(),
            book_stale_ms: d_book_stale(),
            audio_stale_ms: d_audio_stale(),
            min_ev_cents: d_min_ev(),
            allowlist_tickers: Vec::new(),
        }
    }
}

impl RiskConfig {
    /// Clamp config to the hard ceilings — silently tightening is fine,
    /// loosening is impossible.
    pub fn clamped(mut self) -> Self {
        self.max_order_notional_cents =
            self.max_order_notional_cents.min(HARD_MAX_ORDER_NOTIONAL_CENTS);
        self.max_event_notional_cents =
            self.max_event_notional_cents.min(HARD_MAX_EVENT_NOTIONAL_CENTS);
        self.daily_loss_cap_cents = self.daily_loss_cap_cents.min(HARD_DAILY_LOSS_CENTS);
        self
    }
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum Refusal {
    #[error("kill switch engaged")]
    Killed,
    #[error("not armed for live orders")]
    NotArmed,
    #[error("ticker {0} not in allowlist")]
    NotAllowlisted(String),
    #[error("book stale: {age_ms} ms > {limit_ms} ms")]
    BookStale { age_ms: u64, limit_ms: u64 },
    #[error("audio stale: {age_ms} ms > {limit_ms} ms")]
    AudioStale { age_ms: u64, limit_ms: u64 },
    #[error("order notional {notional}¢ exceeds cap {cap}¢")]
    OrderNotional { notional: u64, cap: u64 },
    #[error("event notional would reach {would}¢, cap {cap}¢")]
    EventNotional { would: u64, cap: u64 },
    #[error("probe ledger would reach {would}¢, hard cap {cap}¢")]
    ProbeLedger { would: u64, cap: u64 },
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct Ledger {
    spent_cents: u64,
}

pub struct RiskGate {
    cfg: RiskConfig,
    armed: AtomicBool,
    killed: AtomicBool,
    event_spent_cents: AtomicU64,
    ledger_path: PathBuf,
    ledger: Mutex<Ledger>,
}

impl RiskGate {
    pub fn new(cfg: RiskConfig, state_dir: &Path) -> std::io::Result<Self> {
        std::fs::create_dir_all(state_dir)?;
        let ledger_path = state_dir.join("probe_ledger.json");
        let ledger: Ledger = std::fs::read_to_string(&ledger_path)
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default();
        Ok(RiskGate {
            cfg: cfg.clamped(),
            armed: AtomicBool::new(false),
            killed: AtomicBool::new(false),
            event_spent_cents: AtomicU64::new(0),
            ledger_path,
            ledger: Mutex::new(ledger),
        })
    }

    pub fn cfg(&self) -> &RiskConfig {
        &self.cfg
    }

    pub fn arm(&self) {
        self.armed.store(true, Ordering::SeqCst);
    }

    pub fn kill(&self) {
        self.killed.store(true, Ordering::SeqCst);
        self.armed.store(false, Ordering::SeqCst);
    }

    pub fn is_killed(&self) -> bool {
        self.killed.load(Ordering::SeqCst)
    }

    pub fn ledger_spent_cents(&self) -> u64 {
        self.ledger.lock().unwrap().spent_cents
    }

    /// Full pre-send check. `book_age_ns`/`audio_age_ns` are u64::MAX when
    /// unknown — unknown is stale (P3: refuse and page, never guess).
    pub fn check_order(
        &self,
        ticker: &str,
        price_cents: u32,
        count: u32,
        book_age_ns: u64,
        audio_age_ns: u64,
    ) -> Result<(), Refusal> {
        if self.killed.load(Ordering::SeqCst) {
            return Err(Refusal::Killed);
        }
        if !self.armed.load(Ordering::SeqCst) {
            return Err(Refusal::NotArmed);
        }
        if !self.cfg.allowlist_tickers.iter().any(|t| t == ticker) {
            return Err(Refusal::NotAllowlisted(ticker.to_string()));
        }
        let book_age_ms = book_age_ns / 1_000_000;
        if book_age_ms > self.cfg.book_stale_ms {
            return Err(Refusal::BookStale { age_ms: book_age_ms, limit_ms: self.cfg.book_stale_ms });
        }
        let audio_age_ms = audio_age_ns / 1_000_000;
        if audio_age_ms > self.cfg.audio_stale_ms {
            return Err(Refusal::AudioStale { age_ms: audio_age_ms, limit_ms: self.cfg.audio_stale_ms });
        }
        let notional = price_cents as u64 * count as u64;
        if notional > self.cfg.max_order_notional_cents {
            return Err(Refusal::OrderNotional { notional, cap: self.cfg.max_order_notional_cents });
        }
        let would_event = self.event_spent_cents.load(Ordering::SeqCst) + notional;
        if would_event > self.cfg.max_event_notional_cents {
            return Err(Refusal::EventNotional { would: would_event, cap: self.cfg.max_event_notional_cents });
        }
        let ledger = self.ledger.lock().unwrap();
        let would_ledger = ledger.spent_cents + notional;
        if would_ledger > PROBE_LEDGER_CAP_CENTS {
            return Err(Refusal::ProbeLedger { would: would_ledger, cap: PROBE_LEDGER_CAP_CENTS });
        }
        Ok(())
    }

    /// Debit worst-case notional the moment an order is sent (before the ack:
    /// an ambiguous timeout must still count as spent).
    pub fn record_send(&self, price_cents: u32, count: u32) {
        let notional = price_cents as u64 * count as u64;
        self.event_spent_cents.fetch_add(notional, Ordering::SeqCst);
        let mut ledger = self.ledger.lock().unwrap();
        ledger.spent_cents += notional;
        if let Ok(json) = serde_json::to_string_pretty(&*ledger) {
            if let Err(e) = std::fs::write(&self.ledger_path, json) {
                tracing::error!("probe ledger persist failed: {e}");
            }
        }
    }
}

/// Kill-switch watcher: polls `state/STOP` every 200 ms and listens for
/// SIGINT/SIGTERM. On trigger, runs `on_kill` (cancel-all) then flips the
/// gate. Returns the poller JoinHandle.
pub fn spawn_kill_watcher<F, Fut>(
    gate: std::sync::Arc<RiskGate>,
    stop_file: PathBuf,
    on_kill: F,
) -> tokio::task::JoinHandle<()>
where
    F: Fn() -> Fut + Send + Sync + 'static,
    Fut: std::future::Future<Output = ()> + Send,
{
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(std::time::Duration::from_millis(200));
        let ctrl_c = tokio::signal::ctrl_c();
        tokio::pin!(ctrl_c);
        loop {
            tokio::select! {
                _ = tick.tick() => {
                    if stop_file.exists() {
                        tracing::error!("STOP file detected — killing");
                        break;
                    }
                }
                _ = &mut ctrl_c => {
                    tracing::error!("SIGINT — killing");
                    break;
                }
            }
        }
        let t0 = MonotonicNs::now();
        gate.kill();
        on_kill().await;
        tracing::info!(
            kill_to_cancel_ms = MonotonicNs::now().saturating_since(t0) / 1_000_000,
            "kill switch complete: orders cancelled, disarmed"
        );
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gate(cfg: RiskConfig) -> RiskGate {
        let dir = tempfile::tempdir().unwrap();
        let g = RiskGate::new(cfg, dir.path()).unwrap();
        std::mem::forget(dir); // keep tempdir alive for the test process
        g
    }

    fn permissive() -> RiskConfig {
        RiskConfig {
            allowlist_tickers: vec!["T".into()],
            ..Default::default()
        }
    }

    #[test]
    fn config_cannot_exceed_hard_caps() {
        let cfg = RiskConfig {
            max_order_notional_cents: u64::MAX,
            max_event_notional_cents: u64::MAX,
            daily_loss_cap_cents: u64::MAX,
            ..Default::default()
        }
        .clamped();
        assert_eq!(cfg.max_order_notional_cents, HARD_MAX_ORDER_NOTIONAL_CENTS);
        assert_eq!(cfg.max_event_notional_cents, HARD_MAX_EVENT_NOTIONAL_CENTS);
        assert_eq!(cfg.daily_loss_cap_cents, HARD_DAILY_LOSS_CENTS);
    }

    #[test]
    fn refusal_order_and_reasons() {
        let g = gate(permissive());
        // Not armed yet.
        assert_eq!(g.check_order("T", 1, 1, 0, 0), Err(Refusal::NotArmed));
        g.arm();
        // Unknown staleness is stale.
        assert!(matches!(g.check_order("T", 1, 1, u64::MAX, 0), Err(Refusal::BookStale { .. })));
        assert!(matches!(g.check_order("T", 1, 1, 0, u64::MAX), Err(Refusal::AudioStale { .. })));
        // Allowlist.
        assert!(matches!(g.check_order("X", 1, 1, 0, 0), Err(Refusal::NotAllowlisted(_))));
        // Clean pass.
        assert_eq!(g.check_order("T", 1, 1, 0, 0), Ok(()));
        // Kill dominates everything.
        g.kill();
        assert_eq!(g.check_order("T", 1, 1, 0, 0), Err(Refusal::Killed));
    }

    #[test]
    fn probe_ledger_caps_cumulative_spend() {
        // Event cap loosened so the LEDGER is the binding constraint here.
        let g = gate(RiskConfig {
            max_event_notional_cents: 10_000,
            allowlist_tickers: vec!["T".into()],
            ..Default::default()
        });
        g.arm();
        // 99¢ spent: 1¢ order still fits, 2¢ doesn't.
        for _ in 0..99 {
            g.record_send(1, 1);
        }
        assert_eq!(g.ledger_spent_cents(), 99);
        assert_eq!(g.check_order("T", 1, 1, 0, 0), Ok(()));
        assert!(matches!(g.check_order("T", 2, 1, 0, 0), Err(Refusal::ProbeLedger { .. })));
        g.record_send(1, 1);
        assert!(matches!(g.check_order("T", 1, 1, 0, 0), Err(Refusal::ProbeLedger { .. })));
    }

    #[test]
    fn ledger_persists_across_gates() {
        let dir = tempfile::tempdir().unwrap();
        {
            let g = RiskGate::new(permissive(), dir.path()).unwrap();
            g.record_send(1, 30);
        }
        let g2 = RiskGate::new(permissive(), dir.path()).unwrap();
        assert_eq!(g2.ledger_spent_cents(), 30);
    }

    #[test]
    fn order_notional_cap() {
        let g = gate(RiskConfig {
            max_order_notional_cents: 50,
            allowlist_tickers: vec!["T".into()],
            ..Default::default()
        });
        g.arm();
        assert!(matches!(
            g.check_order("T", 51, 1, 0, 0),
            Err(Refusal::OrderNotional { notional: 51, cap: 50 })
        ));
    }
}
