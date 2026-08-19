//! Risk & controls (spec §5.8). Hard caps are consts — config can only
//! tighten downward, never exceed them. Checked before every send, in order:
//! killed? → armed? → staleness → caps → ledger.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};

use crate::clock::MonotonicNs;

/// Hard ceilings (cents). Not user-overridable upward at runtime (spec §11).
pub const HARD_MAX_ORDER_NOTIONAL_CENTS: u64 = 50_000; // $500
pub const HARD_DAILY_LOSS_CENTS: u64 = 100_000; // $1,000
/// M2 posture: cumulative real-money spend across ALL live drills/sessions,
/// enforced here and persisted across runs.
///
/// RAISED 100 → 2000 on 2026-08-10 (operator ruling, for the HIMS 26AUG10
/// board). The $1.00 figure was a per-EVENT intuition, but this ledger is
/// CUMULATIVE across every event and every posture — so with 19¢ already
/// spent it did not mean "$1 on this call", it meant 81¢ left in total,
/// forever. On a 19-market mention board that is not a per-word budget at
/// all: the first confirmed detection consumed the whole allowance and every
/// later word refused on `Refusal::ProbeLedger`, whichever word happened to
/// fire first. The operator's intent is $1 PER SUB-MARKET, so the ledger has
/// to clear 19 × `MENTION_FILL_CAP_CENTS` = 1843¢ plus what is already spent.
///
/// 2000 is the smallest round number above that (1862¢ needed). It is still a
/// real backstop — it is NOT the M4 "remove the clamps" step. Per-order price
/// stays bound by `MENTION_FILL_CAP_CENTS`; this constant only stops the
/// CUMULATIVE total from silently becoming the binding cap on a multi-market
/// board. There is NO per-event notional cap any more (operator ruling
/// 2026-08-18) — this ledger and the per-order bounds are what remain.
///
/// RAISED 2000 → 7000 → 13000 on 2026-08-11 (two operator rulings the same
/// day). The first sized FILL words by study ratio; the second replaced that
/// with TWO BANDS — $10 for high-accuracy words, $1 for low-accuracy ones —
/// via `posture-from-study.py --size-tiers "1.0:10" --low-band-budget 1`.
///
/// The second ruling is the expensive one, and not because of the $10: it made
/// the LOW band REAL. Sub-floor words used to carry `max_price_cents = 1`,
/// which is not "a 1¢ bet" — nothing rests at 1¢ on a mention board, so those
/// orders were non-crossing and took no position at all. They now cross at the
/// same 97¢ ceiling with 1 contract. CAVA 26AUG11 prices out at 12125¢ if every
/// word fires and fills (12 HIGH ×10 = 11640¢, plus 5 LOW ×1 = 485¢).
///
/// At 7000 the ledger would have refused that board's tail on
/// `Refusal::ProbeLedger` — the same silent-truncation failure the 100 → 2000
/// raise was meant to end, twice removed.
///
/// SIZE DOES NOT CHANGE THE SIGN. At a 97¢ ceiling a word must be right >97% of
/// the time to break even (10 contracts: correct = +30¢, wrong = −970¢), and
/// that ratio is identical at 1 contract. Raising this constant scales whatever
/// the precision already is; it does not improve it. Lifetime settled record
/// when this was raised: 3 YES / 1 NO, and the only large win (FIG +43¢) filled
/// at 57¢, where the break-even is 57%.
///
/// Scope note, because the number only makes sense with it: this ledger is
/// loaded from `state_dir.join("probe_ledger.json")` (see `RiskGate::new`), so
/// in practice it binds PER EVENT — each event dir starts its own file at zero.
/// 7000 is therefore a per-event ceiling, not a lifetime one. Whether that
/// matches the "cumulative across ALL live drills/sessions" intent stated above
/// is an OPEN question the operator deferred on 2026-08-11; do not read this
/// constant as a global spend bound until that is settled.
///
/// RAISED 13_000 → 20_000 on 2026-08-19 (operator ruling, event-prep for
/// TGT/TOL 08-19: "ramp up trading only for words we are confident in — use
/// the tiering system, double it: high confidence $20 and the ladder goes down;
/// no event caps, only per-word caps"). Sizes are now `--size-tiers
/// 3.0:20,2.0:10,1.0:4 --low-band-budget 2` at the 97¢ ceiling, so the per-word
/// cap is 20 × 97 = 1940¢ (`[risk].max_order_notional_cents = 1940` in those
/// configs) and a full board prices out above the old 13_000: TOL 08-19 worst
/// case 156 contracts = 15,132¢, TGT 114 = 11,058¢. 20_000 lets both boards
/// fill every tier without a `Refusal::ProbeLedger` tail; the sign argument
/// above is unchanged (break-even precision at 97¢ is 97 % at every size).
pub const PROBE_LEDGER_CAP_CENTS: u64 = 20_000;
/// Fillable mention posture (the deliberate step past the 1¢ no-fill probe):
/// a single order's price may cross a real ask up to this ceiling so the
/// executor can actually fill. This is a per-order PRICE bound only — real
/// spend stays clamped by the per-order notional cap AND the probe ledger
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
/// so each ORDER buys one ~97¢ contract. It no longer implies one order per
/// event: the per-event notional cap was removed on 2026-08-18, so an event
/// may place one order per FILL-posture keyword (the one-shot table is the
/// per-keyword bound).
pub const MENTION_FILL_CAP_CENTS: u64 = 97;

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RiskConfig {
    #[serde(default = "d_order")]
    pub max_order_notional_cents: u64,
    #[serde(default = "d_loss")]
    pub daily_loss_cap_cents: u64,
    #[serde(default = "d_book_stale")]
    pub book_stale_ms: u64,
    /// Whether `book_stale_ms` REFUSES, or is only recorded. Default `false`.
    ///
    /// Until 2026-08-07 the executor fed `BookCache::feed_age_ns()` — a
    /// socket-wide liveness clock the ping loop holds under ~500 ms — into the
    /// `book_age_ns` parameter, so `Refusal::BookStale` had never fired once.
    /// The real per-ticker age is now passed, which makes the gate live for
    /// the first time; enabling it in the same change would have been a large
    /// unreviewed trading change, so enforcement is opt-in.
    ///
    /// It defaults off because the evidence does not support refusing. Across
    /// 56 banked intents carrying a book age, a 2000 ms limit would refuse
    /// **43%**, including two of the three fills this system has ever made —
    /// DASH-AFFO on a 40.6 s snapshot and FIG-CODE on a 2.7 s one, both of
    /// which settled YES. P(YES) by age is 0.78 / 0.75 / 0.58 for
    /// ≤2 s / 2–30 s / >30 s, and the >30 s bucket's mean ask is 62.7¢ against
    /// 79.6¢ elsewhere, so most of that decline is the price effect, not the
    /// staleness. The cap already bounds what a stale quote can cost us: we
    /// send a marketable limit at `max_price_cents`, so worst case is the cap
    /// whether the book is 6 ms or 6 minutes old.
    #[serde(default)]
    pub book_stale_enforce: bool,
    #[serde(default = "d_audio_stale")]
    pub audio_stale_ms: u64,
    #[serde(default = "d_min_ev")]
    pub min_ev_cents: u32,
    #[serde(default)]
    pub allowlist_tickers: Vec<String>,
}

fn d_order() -> u64 {
    100
}
fn d_loss() -> u64 {
    100
}
fn d_book_stale() -> u64 {
    2000
}
fn d_audio_stale() -> u64 {
    1000
}
fn d_min_ev() -> u32 {
    2
}

impl Default for RiskConfig {
    fn default() -> Self {
        RiskConfig {
            max_order_notional_cents: d_order(),
            daily_loss_cap_cents: d_loss(),
            book_stale_ms: d_book_stale(),
            book_stale_enforce: false,
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
        self.max_order_notional_cents = self
            .max_order_notional_cents
            .min(HARD_MAX_ORDER_NOTIONAL_CENTS);
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
        // UNKNOWN age and KNOWN-BUT-OLD age are different failures and are
        // treated differently. Unknown means there is no snapshot at all, and
        // refusing on it is the P3 rule — refuse and page, never guess — which
        // `book_stale_enforce` does NOT relax. Known-but-old is a data-quality
        // signal whose enforcement is opt-in; see `book_stale_enforce`.
        let book_age_ms = book_age_ns / 1_000_000;
        if book_age_ns == u64::MAX {
            return Err(Refusal::BookStale {
                age_ms: book_age_ms,
                limit_ms: self.cfg.book_stale_ms,
            });
        }
        if book_age_ms > self.cfg.book_stale_ms {
            if self.cfg.book_stale_enforce {
                return Err(Refusal::BookStale {
                    age_ms: book_age_ms,
                    limit_ms: self.cfg.book_stale_ms,
                });
            }
            // Recorded, not refused. The age is already on the OrderRecord;
            // this makes it greppable in a live log without changing the run.
            tracing::warn!(
                ticker,
                age_ms = book_age_ms,
                limit_ms = self.cfg.book_stale_ms,
                "book snapshot older than book_stale_ms (not enforced)"
            );
        }
        let audio_age_ms = audio_age_ns / 1_000_000;
        if audio_age_ms > self.cfg.audio_stale_ms {
            return Err(Refusal::AudioStale {
                age_ms: audio_age_ms,
                limit_ms: self.cfg.audio_stale_ms,
            });
        }
        let notional = price_cents as u64 * count as u64;
        if notional > self.cfg.max_order_notional_cents {
            return Err(Refusal::OrderNotional {
                notional,
                cap: self.cfg.max_order_notional_cents,
            });
        }
        let ledger = self.store.ledger.lock().unwrap();
        let would_ledger = ledger.spent_cents + notional;
        if would_ledger > PROBE_LEDGER_CAP_CENTS {
            return Err(Refusal::ProbeLedger {
                would: would_ledger,
                cap: PROBE_LEDGER_CAP_CENTS,
            });
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

/// The external reason a run crossed its session-scoped kill boundary.
///
/// This value is deliberately small, copyable, and serializable so runtime
/// assembly can put the same reason in its final report/evidence without
/// inferring it from a process exit code.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum KillTrigger {
    StopFile,
    Sigint,
    Sigterm,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TeardownDisposition {
    Acknowledged,
    EventDropped,
}

/// Typed handoff from the risk kill boundary to runtime assembly.
///
/// Delivery happens only after the gate is killed, session cancellation has
/// completed, and the risk ledger is durably flushed. Runtime assembly owns
/// the remaining source/pipeline/report drain and calls [`Self::acknowledge`]
/// after those finalizers finish. Dropping this value without acknowledgement
/// is an explicit failure that releases (rather than strands) the watcher.
pub struct CoordinatedKill<T> {
    pub trigger: KillTrigger,
    pub kill_to_cancel_ms: u64,
    pub cancellation: T,
    acknowledgement: TeardownAcknowledgement,
}

impl<T> CoordinatedKill<T> {
    /// Confirm that runtime-owned source/pipeline/report teardown completed.
    /// The watcher is released even if it has already gone away. Returns the
    /// cancellation result intact so callers can retain unresolved IDs in
    /// their terminal classification after consuming the event.
    pub fn acknowledge(self) -> T {
        let CoordinatedKill {
            cancellation,
            mut acknowledgement,
            ..
        } = self;
        acknowledgement.acknowledge();
        cancellation
    }
}

struct TeardownAcknowledgement {
    sender: Option<tokio::sync::oneshot::Sender<TeardownDisposition>>,
}

impl TeardownAcknowledgement {
    fn acknowledge(&mut self) {
        if let Some(tx) = self.sender.take() {
            if tx.send(TeardownDisposition::Acknowledged).is_err() {
                tracing::error!("coordinated kill acknowledgement receiver disappeared");
            }
        }
    }
}

impl Drop for TeardownAcknowledgement {
    fn drop(&mut self) {
        if let Some(tx) = self.sender.take() {
            tracing::error!("coordinated kill event dropped without teardown acknowledgement");
            let _ = tx.send(TeardownDisposition::EventDropped);
        }
    }
}

/// Coordinated kill watcher for long-lived runtimes.
///
/// STOP/SIGINT/SIGTERM all follow the same ordered path:
///
/// 1. kill/disarm the gate;
/// 2. await the caller's session-scoped cancellation work;
/// 3. flush the money ledger;
/// 4. deliver a typed event carrying the cancellation result;
/// 5. wait for runtime teardown acknowledgement.
///
/// This API never exits the process. If the event receiver is gone, or the
/// delivered event is dropped without acknowledgement, the watcher records
/// the failure and terminates instead of hanging forever.
pub fn spawn_coordinated_kill_watcher<F, Fut, T>(
    gate: Arc<RiskGate>,
    stop_file: PathBuf,
    on_kill: F,
) -> (
    tokio::task::JoinHandle<()>,
    tokio::sync::oneshot::Receiver<CoordinatedKill<T>>,
)
where
    F: FnOnce() -> Fut + Send + 'static,
    Fut: std::future::Future<Output = T> + Send,
    T: Send + 'static,
{
    let (event_tx, event_rx) = tokio::sync::oneshot::channel();
    let watcher = tokio::spawn(async move {
        let mut tick = tokio::time::interval(std::time::Duration::from_millis(200));
        let ctrl_c = tokio::signal::ctrl_c();
        tokio::pin!(ctrl_c);
        // Install failure would leave SIGTERM at its default (instant death,
        // no cancel) — degrade loudly to STOP/SIGINT-only rather than
        // panicking the watcher (a dead watcher is a dead kill switch).
        let mut sigterm = match tokio::signal::unix::signal(
            tokio::signal::unix::SignalKind::terminate(),
        ) {
            Ok(s) => Some(s),
            Err(e) => {
                tracing::error!(
                        "SIGTERM handler install FAILED ({e}) — unattended kills will not cancel orders"
                    );
                None
            }
        };
        let trigger = loop {
            tokio::select! {
                _ = tick.tick() => {
                    if stop_file.exists() {
                        tracing::error!("STOP file detected — killing");
                        break KillTrigger::StopFile;
                    }
                }
                _ = &mut ctrl_c => {
                    tracing::error!("SIGINT — killing");
                    break KillTrigger::Sigint;
                }
                _ = async {
                    match sigterm.as_mut() {
                        Some(s) => { s.recv().await; }
                        None => std::future::pending::<()>().await,
                    }
                } => {
                    tracing::error!("SIGTERM — killing");
                    break KillTrigger::Sigterm;
                }
            }
        };

        let t0 = MonotonicNs::now();
        gate.kill();
        let cancellation = on_kill().await;
        // Kill is an emergency: make the ledger durable before handing
        // teardown to runtime assembly.
        gate.flush();
        let kill_to_cancel_ms = MonotonicNs::now().saturating_since(t0) / 1_000_000;
        tracing::info!(
            ?trigger,
            kill_to_cancel_ms,
            "kill boundary complete: cancelled, disarmed, ledger flushed; awaiting runtime teardown"
        );

        let (ack_tx, ack_rx) = tokio::sync::oneshot::channel();
        let event = CoordinatedKill {
            trigger,
            kill_to_cancel_ms,
            cancellation,
            acknowledgement: TeardownAcknowledgement {
                sender: Some(ack_tx),
            },
        };
        if let Err(event) = event_tx.send(event) {
            tracing::error!(
                ?trigger,
                "coordinated kill receiver disappeared before event delivery"
            );
            drop(event); // emits the explicit unacknowledged-event diagnostic
            return;
        }
        match ack_rx.await {
            Ok(TeardownDisposition::Acknowledged) => {
                tracing::info!(?trigger, "coordinated runtime teardown acknowledged");
            }
            Ok(TeardownDisposition::EventDropped) => {
                tracing::error!(
                    ?trigger,
                    "runtime dropped coordinated kill without acknowledgement"
                );
            }
            Err(_) => {
                tracing::error!(
                    ?trigger,
                    "coordinated teardown acknowledgement channel disappeared"
                );
            }
        }
    });
    (watcher, event_rx)
}

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
    let (coordinator, event_rx) = spawn_coordinated_kill_watcher(gate, stop_file, on_kill);
    tokio::spawn(async move {
        let Ok(event) = event_rx.await else {
            tracing::error!("compatibility kill watcher lost coordinated event");
            let _ = coordinator.await;
            return;
        };
        let terminated = event.trigger == KillTrigger::Sigterm;
        if terminated {
            on_terminate();
        }
        event.acknowledge();
        let _ = coordinator.await;
        if terminated && SIGTERM_EXITS.load(Ordering::SeqCst) {
            std::process::exit(143);
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

    /// 2026-08-07. The executor fed `cache.feed_age_ns()` — socket-wide
    /// liveness, pinned under ~500 ms by the ping loop — into `book_age_ns`,
    /// so this gate never fired once in the system's history. It now receives
    /// the per-ticker snapshot age, which splits into two cases that must not
    /// be collapsed.
    #[test]
    fn book_staleness_refuses_unknown_but_only_warns_on_old() {
        // Known-but-old, enforcement OFF (the default): passes, warns.
        let g = gate(permissive());
        g.arm();
        let old = 41_000_u64 * 1_000_000; // DASH-AFFO's real 40.6 s snapshot
        assert_eq!(g.check_order("T", 1, 1, old, 0), Ok(()));

        // Same age with enforcement ON: refuses.
        let g = gate(RiskConfig {
            book_stale_enforce: true,
            ..permissive()
        });
        g.arm();
        assert!(matches!(
            g.check_order("T", 1, 1, old, 0),
            Err(Refusal::BookStale { .. })
        ));

        // UNKNOWN age refuses either way — `book_stale_enforce` must never
        // relax P3 (refuse and page, never guess).
        for enforce in [false, true] {
            let g = gate(RiskConfig {
                book_stale_enforce: enforce,
                ..permissive()
            });
            g.arm();
            assert!(
                matches!(
                    g.check_order("T", 1, 1, u64::MAX, 0),
                    Err(Refusal::BookStale { .. })
                ),
                "unknown book age must refuse with enforce={enforce}"
            );
        }
    }

    /// 2026-08-18 operator ruling: there is NO per-event notional cap. Before
    /// this, `max_event_notional_cents = 100` against a 97¢ fill price let
    /// exactly ONE order per event reach the wire, and it went to whichever
    /// keyword fired FIRST. Measured on 26AUG18: the cheap, genuinely winnable
    /// entries (weather 34¢, profitability 56¢, tailwind 60¢, wimbledon 85¢)
    /// were all refused after an already-repriced 92–97¢ market consumed the
    /// event's whole allowance. Per-ORDER bounds and the probe ledger remain.
    #[test]
    fn many_orders_in_one_event_are_not_capped_by_the_event() {
        let g = gate(RiskConfig {
            allowlist_tickers: vec!["T".into()],
            ..Default::default()
        });
        g.arm();
        // Ten separate 97¢ orders in ONE event: 970¢ cumulative, which the old
        // 100¢ event cap refused at the second one.
        for i in 0..10 {
            assert_eq!(
                g.check_order("T", 97, 1, 0, 0),
                Ok(()),
                "order {i} must not be refused for a per-event reason"
            );
            g.record_send(97, 1);
        }
        // The per-ORDER cap is untouched and still binds.
        assert!(matches!(
            g.check_order("T", 97, 2, 0, 0),
            Err(Refusal::OrderNotional { .. })
        ));
    }

    #[test]
    fn config_cannot_exceed_hard_caps() {
        let cfg = RiskConfig {
            max_order_notional_cents: u64::MAX,
            daily_loss_cap_cents: u64::MAX,
            ..Default::default()
        }
        .clamped();
        assert_eq!(cfg.max_order_notional_cents, HARD_MAX_ORDER_NOTIONAL_CENTS);
        assert_eq!(cfg.daily_loss_cap_cents, HARD_DAILY_LOSS_CENTS);
    }

    #[test]
    fn refusal_order_and_reasons() {
        let g = gate(permissive());
        // Not armed yet.
        assert_eq!(g.check_order("T", 1, 1, 0, 0), Err(Refusal::NotArmed));
        g.arm();
        // Unknown staleness is stale.
        assert!(matches!(
            g.check_order("T", 1, 1, u64::MAX, 0),
            Err(Refusal::BookStale { .. })
        ));
        assert!(matches!(
            g.check_order("T", 1, 1, 0, u64::MAX),
            Err(Refusal::AudioStale { .. })
        ));
        // Allowlist.
        assert!(matches!(
            g.check_order("X", 1, 1, 0, 0),
            Err(Refusal::NotAllowlisted(_))
        ));
        // Clean pass.
        assert_eq!(g.check_order("T", 1, 1, 0, 0), Ok(()));
        // Kill dominates everything.
        g.kill();
        assert_eq!(g.check_order("T", 1, 1, 0, 0), Err(Refusal::Killed));
    }

    #[test]
    fn probe_ledger_caps_cumulative_spend() {
        // With the per-event cap removed (2026-08-18) the LEDGER is the only
        // cumulative bound left, so nothing has to be loosened for it to bind.
        // Both bounds are derived from PROBE_LEDGER_CAP_CENTS rather than
        // written as literals: this test hardcoded 99/100 until 2026-08-10,
        // so raising the constant broke it for no behavioural reason. What is
        // under test is "the ledger binds one cent below its cap", whatever
        // that cap currently is.
        let cap = PROBE_LEDGER_CAP_CENTS;
        let g = gate(RiskConfig {
            allowlist_tickers: vec!["T".into()],
            ..Default::default()
        });
        g.arm();
        // One cent below the cap: a 1¢ order still fits, a 2¢ order doesn't.
        for _ in 0..(cap - 1) {
            g.record_send(1, 1);
        }
        assert_eq!(g.ledger_spent_cents(), cap - 1);
        assert_eq!(g.check_order("T", 1, 1, 0, 0), Ok(()));
        assert!(matches!(
            g.check_order("T", 2, 1, 0, 0),
            Err(Refusal::ProbeLedger { .. })
        ));
        g.record_send(1, 1);
        assert!(matches!(
            g.check_order("T", 1, 1, 0, 0),
            Err(Refusal::ProbeLedger { .. })
        ));
    }

    #[test]
    fn corrupt_ledger_refuses_to_start() {
        // A truncated/garbage ledger must NEVER default to zero spend — that
        // would erase the cross-run money backstop (post-mortem 2026-07-15).
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("probe_ledger.json"), "{\"spent_c").unwrap();
        let err = RiskGate::new(permissive(), dir.path())
            .map(|_| ())
            .unwrap_err();
        assert_eq!(err.kind(), std::io::ErrorKind::InvalidData);
        // Missing file stays fine: fresh event starts at zero.
        let dir2 = tempfile::tempdir().unwrap();
        assert_eq!(
            RiskGate::new(permissive(), dir2.path())
                .unwrap()
                .ledger_spent_cents(),
            0
        );
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
        assert_eq!(
            names,
            vec!["probe_ledger.json"],
            "unexpected files: {names:?}"
        );
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
            Err(Refusal::OrderNotional {
                notional: 51,
                cap: 50
            })
        ));
    }

    #[test]
    fn kill_trigger_is_stable_serializable_evidence() {
        assert_eq!(
            serde_json::to_string(&KillTrigger::StopFile).unwrap(),
            r#""stop_file""#
        );
        assert_eq!(
            serde_json::from_str::<KillTrigger>(r#""sigterm""#).unwrap(),
            KillTrigger::Sigterm
        );
    }

    #[tokio::test]
    async fn coordinated_stop_kills_gate_and_finishes_cancel_before_delivery() {
        let dir = tempfile::tempdir().unwrap();
        let gate = Arc::new(RiskGate::new(permissive(), dir.path()).unwrap());
        gate.arm();
        gate.record_send(7, 1);
        let cancellation_finished = Arc::new(AtomicBool::new(false));
        let finished = cancellation_finished.clone();
        let stop = dir.path().join("STOP");
        let (watcher, event_rx) =
            spawn_coordinated_kill_watcher(gate.clone(), stop.clone(), move || async move {
                tokio::time::sleep(std::time::Duration::from_millis(20)).await;
                finished.store(true, Ordering::SeqCst);
                (3_u64, vec!["client-unresolved".to_owned()])
            });
        std::fs::write(&stop, "test").unwrap();
        let event = tokio::time::timeout(std::time::Duration::from_secs(2), event_rx)
            .await
            .expect("typed kill event timed out")
            .expect("typed kill event sender disappeared");
        assert!(gate.is_killed(), "gate must be killed before delivery");
        assert!(
            cancellation_finished.load(Ordering::SeqCst),
            "cancellation must complete before delivery"
        );
        assert_eq!(event.trigger, KillTrigger::StopFile);
        assert_eq!(
            event.cancellation,
            (3, vec!["client-unresolved".to_owned()])
        );
        let ledger = std::fs::read_to_string(dir.path().join("probe_ledger.json")).unwrap();
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&ledger).unwrap()["spent_cents"],
            7
        );
        event.acknowledge();
        tokio::time::timeout(std::time::Duration::from_secs(2), watcher)
            .await
            .expect("watcher did not finish after acknowledgement")
            .unwrap();
    }

    #[tokio::test]
    async fn coordinated_watcher_finishes_when_event_receiver_is_gone() {
        let dir = tempfile::tempdir().unwrap();
        let gate = Arc::new(RiskGate::new(permissive(), dir.path()).unwrap());
        gate.arm();
        let stop = dir.path().join("STOP");
        let (watcher, event_rx) =
            spawn_coordinated_kill_watcher(gate.clone(), stop.clone(), || async { 9_u8 });
        drop(event_rx);
        std::fs::write(stop, "test").unwrap();
        tokio::time::timeout(std::time::Duration::from_secs(2), watcher)
            .await
            .expect("watcher hung after event receiver disappeared")
            .unwrap();
        assert!(gate.is_killed());
    }

    #[tokio::test]
    async fn dropping_delivered_event_releases_watcher() {
        let dir = tempfile::tempdir().unwrap();
        let gate = Arc::new(RiskGate::new(permissive(), dir.path()).unwrap());
        gate.arm();
        let stop = dir.path().join("STOP");
        let (watcher, event_rx) =
            spawn_coordinated_kill_watcher(gate, stop.clone(), || async { () });
        std::fs::write(stop, "test").unwrap();
        let event = tokio::time::timeout(std::time::Duration::from_secs(2), event_rx)
            .await
            .expect("typed kill event timed out")
            .unwrap();
        drop(event); // deliberately omit acknowledge()
        tokio::time::timeout(std::time::Duration::from_secs(2), watcher)
            .await
            .expect("watcher hung on an unacknowledged dropped event")
            .unwrap();
    }
}
