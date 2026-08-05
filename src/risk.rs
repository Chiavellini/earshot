//! Risk & controls (spec §5.8). Hard caps are consts — config can only
//! tighten downward, never exceed them. Checked before every send, in order:
//! killed? → armed? → staleness → caps → ledger.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};

use crate::clock::MonotonicNs;

/// Hard ceilings (cents). Not user-overridable upward at runtime (spec §11).
pub const HARD_MAX_ORDER_NOTIONAL_CENTS: u64 = 50_000; // $500
pub const HARD_MAX_EVENT_NOTIONAL_CENTS: u64 = 200_000; // $2,000
pub const HARD_DAILY_LOSS_CENTS: u64 = 100_000; // $1,000
/// M2 posture: cumulative real-money spend across ALL live drills/sessions
/// is capped at $1.00, enforced here and persisted across runs.
pub const PROBE_LEDGER_CAP_CENTS: u64 = 100;
/// Fillable mention posture (the deliberate step past the 1¢ no-fill probe):
/// a single order's price may cross a real ask up to this ceiling so the
/// executor can actually fill. This is a per-order PRICE bound only — real
/// spend stays clamped by the config notional caps AND the $1 probe ledger
/// (`PROBE_LEDGER_CAP_CENTS`), which remain the money backstops. Gated behind
/// `arm --i-am-live --allow-mention --fillable`; never reached by the default
/// probe posture.
///
/// RAISED 50 → 97 on 2026-08-05 (operator ruling). 50¢ was chosen to "cover the
/// cheap mention strikes (~20–30¢ asks)", but the 26AUG05 morning tape refuted
/// that premise: of 15 genuinely contested markets, the two we provably BEAT
/// were resting at 60¢ (UBER-ZOOX, +1.73 s) and 72¢ (SHOP-OPEN, +0.68 s) —
/// both above the old ceiling — while the only row a 50¢ cap would have crossed
/// was DIS-TRUM at 39¢, a word never said on the call, which settled NO. The
/// old ceiling excluded every win and admitted only the loss.
///
/// 97 is not arbitrary: it is where `ev_gate` cuts out on its own. At 1
/// contract, cap 97 leaves gross 3¢ − 1¢ fee = 2¢ = `min_ev_cents` (pass);
/// cap 98 leaves 1¢ (refused). Ceiling and EV gate now agree, so EV — not an
/// arbitrary price bound — is the binding constraint.
///
/// Still a per-order PRICE bound only. At 97¢ with `max_order_notional_cents`
/// = 100, `budget_contracts` MUST be 1 (97×2 = 194¢ → `Refusal::OrderNotional`),
/// so this buys ONE ~97¢ contract per event, not a larger position.
pub const MENTION_FILL_CAP_CENTS: u64 = 97;

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

/// Owns the persisted probe ledger and drives its disk writes OFF the fire
/// path. `record_send` only touches the in-memory `ledger` (the authoritative
/// in-session cap) and pokes `dirty`; a background persister (spawned in
/// `main.rs::arm`) coalesces those pokes into disk writes. Single-shot CLI
/// commands and tests spawn no persister — the `Drop` flush is what makes the
/// debit durable for them. Totals are monotonic, so any persist writes a value
/// no older than the last, keeping last-write/concurrent persists safe.
struct LedgerStore {
    path: PathBuf,
    ledger: Mutex<Ledger>,
    dirty: tokio::sync::Notify,
}

impl LedgerStore {
    /// Serialize the current total (brief lock) and write it ATOMICALLY
    /// (temp+rename — a kill mid-write must never truncate the money
    /// backstop; see `RiskGate::new`'s corrupt-file refusal). Never on the
    /// fire path. Preserves the `{"spent_cents": N}` on-disk shape.
    fn persist(&self) {
        let json = {
            let ledger = self.ledger.lock().unwrap();
            serde_json::to_string_pretty(&*ledger)
        };
        match json {
            Ok(json) => {
                if let Err(e) = crate::fsio::write_atomic(&self.path, json.as_bytes()) {
                    tracing::error!("probe ledger persist failed: {e}");
                }
            }
            Err(e) => tracing::error!("probe ledger serialize failed: {e}"),
        }
    }
}

impl Drop for LedgerStore {
    fn drop(&mut self) {
        // Final synchronous flush (teardown, not the hot path): guarantees the
        // last debit reaches disk even when no persister task ran.
        self.persist();
    }
}

pub struct RiskGate {
    cfg: RiskConfig,
    armed: AtomicBool,
    killed: AtomicBool,
    event_spent_cents: AtomicU64,
    store: Arc<LedgerStore>,
}

impl RiskGate {
    pub fn new(cfg: RiskConfig, state_dir: &Path) -> std::io::Result<Self> {
        std::fs::create_dir_all(state_dir)?;
        let ledger_path = state_dir.join("probe_ledger.json");
        // Missing file = fresh event, start at zero. A PRESENT file that does
        // not parse is refused loudly: defaulting a corrupt ledger to zero
        // would silently erase the cross-run $1 spend backstop.
        let ledger: Ledger = match std::fs::read_to_string(&ledger_path) {
            Ok(s) => serde_json::from_str(&s).map_err(|e| {
                std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    format!(
                        "corrupt probe ledger {} ({e}) — REFUSING to start: this file is the \
                         cumulative money backstop; inspect and restore it, never delete blindly",
                        ledger_path.display()
                    ),
                )
            })?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ledger::default(),
            Err(e) => return Err(e),
        };
        Ok(RiskGate {
            cfg: cfg.clamped(),
            armed: AtomicBool::new(false),
            killed: AtomicBool::new(false),
            event_spent_cents: AtomicU64::new(0),
            store: Arc::new(LedgerStore {
                path: ledger_path,
                ledger: Mutex::new(ledger),
                dirty: tokio::sync::Notify::new(),
            }),
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
        self.store.ledger.lock().unwrap().spent_cents
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
        let ledger = self.store.ledger.lock().unwrap();
        let would_ledger = ledger.spent_cents + notional;
        if would_ledger > PROBE_LEDGER_CAP_CENTS {
            return Err(Refusal::ProbeLedger { would: would_ledger, cap: PROBE_LEDGER_CAP_CENTS });
        }
        Ok(())
    }

    /// Debit worst-case notional the moment an order is sent (before the ack:
    /// an ambiguous timeout must still count as spent). The in-memory debit is
    /// synchronous and authoritative; the disk write is deferred to the
    /// background persister (or the `Drop` flush) so NO I/O sits on the fire
    /// path.
    pub fn record_send(&self, price_cents: u32, count: u32) {
        let notional = price_cents as u64 * count as u64;
        self.event_spent_cents.fetch_add(notional, Ordering::SeqCst);
        {
            let mut ledger = self.store.ledger.lock().unwrap();
            ledger.spent_cents += notional;
        }
        self.store.dirty.notify_one();
    }

    /// Spawn the background ledger persister: wake on each `record_send` and
    /// write the current total off the fire path (a burst coalesces into one
    /// write). Mirrors `Presigner::spawn_refresher`. Wired only in
    /// `main.rs::arm`; single-shot commands rely on the `Drop` flush instead.
    pub fn spawn_persister(&self, handle: &tokio::runtime::Handle) -> tokio::task::JoinHandle<()> {
        let store = Arc::clone(&self.store);
        handle.spawn(async move {
            loop {
                store.dirty.notified().await;
                store.persist();
            }
        })
    }

    /// Force a synchronous ledger write. Used at teardown (`main.rs::arm`) to
    /// guarantee the final total is durable before the persister is aborted.
    pub fn flush(&self) {
        self.store.persist();
    }
}

/// Test hook: the SIGTERM path ends in `process::exit`, which would kill the
/// test runner — `tests/kill_sigterm.rs` flips this off to observe the kill
/// sequence in-process. Always true in production.
#[doc(hidden)]
pub static SIGTERM_EXITS: AtomicBool = AtomicBool::new(true);

/// Kill-switch watcher: polls `state/STOP` every 200 ms and listens for
/// SIGINT and SIGTERM. On any trigger: flip the gate, run `on_kill`
/// (cancel-all), force a durable ledger flush.
///
/// STOP/SIGINT then let the process live (the pipeline winds down disarmed —
/// operator-driven, the supervisor keeps its report path). SIGTERM is the
/// UNATTENDED kill (launchd, `launch-event.sh`'s trap, the supervisor's
/// port cleanup): after the kill sequence it runs `on_terminate` (last-words
/// reporting) and exits the process with 143, because the sender expects
/// termination — swallowing SIGTERM would leak the process and wedge the
/// port. Returns the poller JoinHandle.
pub fn spawn_kill_watcher<F, Fut, G>(
    gate: std::sync::Arc<RiskGate>,
    stop_file: PathBuf,
    on_kill: F,
    on_terminate: G,
) -> tokio::task::JoinHandle<()>
where
    F: Fn() -> Fut + Send + Sync + 'static,
    Fut: std::future::Future<Output = ()> + Send,
    G: FnOnce() + Send + 'static,
{
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(std::time::Duration::from_millis(200));
        let ctrl_c = tokio::signal::ctrl_c();
        tokio::pin!(ctrl_c);
        // Install failure would leave SIGTERM at its default (instant death,
        // no cancel) — degrade loudly to STOP/SIGINT-only rather than
        // panicking the watcher (a dead watcher is a dead kill switch).
        let mut sigterm = match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(s) => Some(s),
            Err(e) => {
                tracing::error!("SIGTERM handler install FAILED ({e}) — unattended kills will not cancel orders");
                None
            }
        };
        let mut terminated = false;
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
                _ = async {
                    match sigterm.as_mut() {
                        Some(s) => { s.recv().await; }
                        None => std::future::pending::<()>().await,
                    }
                } => {
                    tracing::error!("SIGTERM — killing, then exiting");
                    terminated = true;
                    break;
                }
            }
        }
        let t0 = MonotonicNs::now();
        gate.kill();
        on_kill().await;
        // Kill is an emergency: make the ledger durable NOW — on SIGTERM
        // neither the persister nor the Drop flush will get another chance.
        gate.flush();
        tracing::info!(
            kill_to_cancel_ms = MonotonicNs::now().saturating_since(t0) / 1_000_000,
            "kill switch complete: orders cancelled, disarmed, ledger flushed"
        );
        if terminated {
            on_terminate();
            if SIGTERM_EXITS.load(Ordering::SeqCst) {
                std::process::exit(143);
            }
        }
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
    fn corrupt_ledger_refuses_to_start() {
        // A truncated/garbage ledger must NEVER default to zero spend — that
        // would erase the cross-run money backstop (post-mortem 2026-07-15).
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("probe_ledger.json"), "{\"spent_c").unwrap();
        let err = RiskGate::new(permissive(), dir.path()).map(|_| ()).unwrap_err();
        assert_eq!(err.kind(), std::io::ErrorKind::InvalidData);
        // Missing file stays fine: fresh event starts at zero.
        let dir2 = tempfile::tempdir().unwrap();
        assert_eq!(RiskGate::new(permissive(), dir2.path()).unwrap().ledger_spent_cents(), 0);
    }

    #[test]
    fn ledger_persist_is_atomic_no_temp_residue() {
        let dir = tempfile::tempdir().unwrap();
        let g = RiskGate::new(permissive(), dir.path()).unwrap();
        g.record_send(1, 30);
        g.flush();
        let names: Vec<_> = std::fs::read_dir(dir.path())
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .collect();
        assert_eq!(names, vec!["probe_ledger.json"], "unexpected files: {names:?}");
        // And the flushed file round-trips.
        let g2 = RiskGate::new(permissive(), dir.path()).unwrap();
        assert_eq!(g2.ledger_spent_cents(), 30);
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
