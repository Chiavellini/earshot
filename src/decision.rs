//! Decision engine (spec §5.6, M0 subset): precompiled keyword table with a
//! one-shot per-keyword state machine. O(1) lookup, no allocation on the
//! detection path except the intent itself.
//!
//! M2 adds here: local WS book read, sweep computation, fee/EV gate,
//! LOST_RACE branch. The M2 executor consumes `DecisionIntent`s from a ring.

use crate::clock::MonotonicNs;
use crate::config::{KeywordConfig, Policy};
use crate::types::{DecisionIntent, Detection, Tier};
use serde::{Deserialize, Serialize};
use sha1::{Digest, Sha1};
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock};

#[cfg(unix)]
use std::os::fd::AsRawFd;

/// Fraction of a keyword's budget released on a Fast-tier hit (spec §5.4:
/// small tranche sized to be an acceptable write-off). Public because the
/// executor pre-stages order bodies for exactly these tranche sizes.
pub const FAST_TRANCHE_PCT: u32 = 30;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum KeywordState {
    Armed,
    FastFired,
    Done,
}

impl KeywordState {
    fn as_str(self) -> &'static str {
        match self {
            Self::Armed => "armed",
            Self::FastFired => "fast_fired",
            Self::Done => "done",
        }
    }
}

struct Entry {
    market_ticker: String,
    max_price_cents: u8,
    budget_contracts: u32,
    policy: Policy,
    state: KeywordState,
    /// Per-tier confidence gates (config `min_confidence_fast`/`_confirmed`).
    /// 0.0 = no gating; engines without real scores emit confidence 1.0.
    min_conf_fast: f32,
    min_conf_confirmed: f32,
}

pub struct DecisionTable {
    entries: Vec<Entry>,
    journal: Option<FileDecisionJournal>,
    /// A persistence failure poisons the table for the rest of the process.
    /// Retrying after an ambiguous short write could emit an intent whose
    /// durable state is unknowable, so every later eligible hit refuses too.
    journal_fault: Arc<OnceLock<String>>,
}

/// Cloneable, read-only health door for the composer. The decision thread
/// publishes its first durable-journal fault here before refusing the intent;
/// assembly can then kill the session and write a structured fault receipt
/// without sharing or locking the mutable decision table.
#[derive(Clone)]
pub struct DecisionHealth {
    fault: Arc<OnceLock<String>>,
}

impl DecisionHealth {
    pub fn fault_detail(&self) -> Option<String> {
        self.fault.get().cloned()
    }
}

/// A duplicate of the journal's locked file description retained by assembly.
/// POSIX `flock` remains held until both the decision table and every duplicate
/// descriptor are dropped, so executor drain cannot overlap a second arm.
pub struct DecisionJournalLease {
    _file: File,
}

/// Failure to open, replay, or append the persistent one-shot journal.
///
/// A present-but-invalid journal is never treated as empty: doing so would
/// silently re-arm markets after a restart.
#[derive(Debug, thiserror::Error)]
pub enum DecisionJournalError {
    #[error("decision journal I/O at {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("invalid decision journal {path}: {detail}")]
    Invalid { path: PathBuf, detail: String },
    #[error("decision journal unavailable: {0}")]
    Unavailable(String),
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct DecisionReceipt {
    schema_version: u8,
    seq: u64,
    wall_unix_ns: u64,
    decision_plan_sha1: String,
    market_ticker: String,
    policy: String,
    max_price_cents: u8,
    budget_contracts: u32,
    tier: String,
    tranche_contracts: u32,
    next_state: String,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct DecisionPlanManifest {
    schema_version: u8,
    decision_plan_sha1: String,
}

struct FileDecisionJournal {
    path: PathBuf,
    file: File,
    next_seq: u64,
    decision_plan_sha1: String,
    #[cfg(test)]
    test_failure: TestAppendFailure,
}

#[cfg(test)]
#[derive(Clone, Copy, Default)]
enum TestAppendFailure {
    #[default]
    None,
    BeforeWrite,
    AfterPartialWrite,
    AfterWriteBeforeSync,
}

impl FileDecisionJournal {
    fn open(
        path: &Path,
        decision_plan_sha1: &str,
    ) -> Result<(Self, Vec<DecisionReceipt>), DecisionJournalError> {
        let parent = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .ok_or_else(|| DecisionJournalError::Invalid {
                path: path.to_path_buf(),
                detail: "journal path must have a pre-created event directory".into(),
            })?;
        if !parent.is_dir() {
            return Err(DecisionJournalError::Io {
                path: parent.to_path_buf(),
                source: std::io::Error::new(
                    std::io::ErrorKind::NotFound,
                    "event directory must exist before opening the decision journal",
                ),
            });
        }
        let (mut file, journal_created) = match OpenOptions::new()
            .create_new(true)
            .read(true)
            .append(true)
            .open(path)
        {
            Ok(file) => (file, true),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                let file = OpenOptions::new()
                    .read(true)
                    .append(true)
                    .open(path)
                    .map_err(|source| DecisionJournalError::Io {
                        path: path.to_path_buf(),
                        source,
                    })?;
                (file, false)
            }
            Err(source) => {
                return Err(DecisionJournalError::Io {
                    path: path.to_path_buf(),
                    source,
                });
            }
        };
        lock_exclusive_nonblocking(&file, path)?;
        // Do this unconditionally after winning the lock. An `exists()` test
        // cannot identify the creator safely: another process may create the
        // file, lose the lock, and exit before syncing the directory entry.
        // The eventual lock winner therefore persists both inode and parent
        // before accepting any decision, whether this open created the path
        // or recovered that race.
        durable_sync(&file, path)?;
        sync_directory(parent)?;
        if let Err(error) = pin_decision_plan(path, decision_plan_sha1, journal_created) {
            // If this call created a replacement journal while a manifest was
            // already present, never leave that empty inode behind: a retry
            // could otherwise see a matching pair and silently reset state.
            drop(file);
            if journal_created {
                let _ = std::fs::remove_file(path);
                let _ = sync_directory(parent);
            }
            return Err(error);
        }

        let mut raw = String::new();
        file.read_to_string(&mut raw)
            .map_err(|source| DecisionJournalError::Io {
                path: path.to_path_buf(),
                source,
            })?;
        // Every append is exactly one JSON object plus newline. A non-newline
        // tail is an interrupted/ambiguous write, not a harmless last record.
        if !raw.is_empty() && !raw.ends_with('\n') {
            return Err(DecisionJournalError::Invalid {
                path: path.to_path_buf(),
                detail: "truncated final line (missing newline); refusing to re-arm".into(),
            });
        }

        let mut receipts = Vec::new();
        for (line_no, line) in raw.lines().enumerate() {
            if line.is_empty() {
                return Err(DecisionJournalError::Invalid {
                    path: path.to_path_buf(),
                    detail: format!("blank line at {}", line_no + 1),
                });
            }
            let receipt: DecisionReceipt =
                serde_json::from_str(line).map_err(|e| DecisionJournalError::Invalid {
                    path: path.to_path_buf(),
                    detail: format!("line {} is not a receipt: {e}", line_no + 1),
                })?;
            if receipt.schema_version != 1 {
                return Err(DecisionJournalError::Invalid {
                    path: path.to_path_buf(),
                    detail: format!(
                        "line {} has unsupported schema {}",
                        line_no + 1,
                        receipt.schema_version
                    ),
                });
            }
            if receipt.decision_plan_sha1 != decision_plan_sha1 {
                return Err(DecisionJournalError::Invalid {
                    path: path.to_path_buf(),
                    detail: format!(
                        "line {} decision plan {} != current {}",
                        line_no + 1,
                        receipt.decision_plan_sha1,
                        decision_plan_sha1
                    ),
                });
            }
            let expected_seq = line_no as u64;
            if receipt.seq != expected_seq {
                return Err(DecisionJournalError::Invalid {
                    path: path.to_path_buf(),
                    detail: format!(
                        "line {} sequence {} != expected {}",
                        line_no + 1,
                        receipt.seq,
                        expected_seq
                    ),
                });
            }
            receipts.push(receipt);
        }
        let next_seq = receipts.len() as u64;
        Ok((
            Self {
                path: path.to_path_buf(),
                file,
                next_seq,
                decision_plan_sha1: decision_plan_sha1.to_owned(),
                #[cfg(test)]
                test_failure: TestAppendFailure::None,
            },
            receipts,
        ))
    }

    fn append(
        &mut self,
        entry: &Entry,
        tier: Tier,
        tranche_contracts: u32,
        next_state: KeywordState,
    ) -> Result<(), DecisionJournalError> {
        #[cfg(test)]
        let test_failure = std::mem::take(&mut self.test_failure);
        #[cfg(test)]
        if matches!(test_failure, TestAppendFailure::BeforeWrite) {
            return self.injected_failure("before write");
        }

        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default();
        let receipt = DecisionReceipt {
            schema_version: 1,
            seq: self.next_seq,
            wall_unix_ns: now.as_secs().saturating_mul(1_000_000_000)
                + u64::from(now.subsec_nanos()),
            decision_plan_sha1: self.decision_plan_sha1.clone(),
            market_ticker: entry.market_ticker.clone(),
            policy: policy_name(entry.policy).to_owned(),
            max_price_cents: entry.max_price_cents,
            budget_contracts: entry.budget_contracts,
            tier: tier_name(tier).to_owned(),
            tranche_contracts,
            next_state: next_state.as_str().to_owned(),
        };
        let mut encoded =
            serde_json::to_vec(&receipt).map_err(|e| DecisionJournalError::Invalid {
                path: self.path.clone(),
                detail: format!("receipt serialization failed: {e}"),
            })?;
        encoded.push(b'\n');

        #[cfg(test)]
        if matches!(test_failure, TestAppendFailure::AfterPartialWrite) {
            let split = encoded.len() / 2;
            let _ = self.file.write(&encoded[..split]);
            return self.injected_failure("after partial write");
        }

        // One O_APPEND write means concurrent actors cannot interleave JSON
        // fragments. A short write is ambiguous and poisons the table.
        let written = self
            .file
            .write(&encoded)
            .map_err(|source| DecisionJournalError::Io {
                path: self.path.clone(),
                source,
            })?;
        if written != encoded.len() {
            return Err(DecisionJournalError::Io {
                path: self.path.clone(),
                source: std::io::Error::new(
                    std::io::ErrorKind::WriteZero,
                    format!("short append: wrote {written} of {} bytes", encoded.len()),
                ),
            });
        }
        #[cfg(test)]
        if matches!(test_failure, TestAppendFailure::AfterWriteBeforeSync) {
            return self.injected_failure("after write before durable sync");
        }
        // Process restarts are the primary failure class, but sync_data also
        // makes the consumed state explicit before an intent can leave this
        // layer. There are at most two writes per market per event.
        durable_sync(&self.file, &self.path)?;
        self.next_seq += 1;
        Ok(())
    }

    fn lease(&self) -> Result<DecisionJournalLease, DecisionJournalError> {
        self.file
            .try_clone()
            .map(|file| DecisionJournalLease { _file: file })
            .map_err(|source| DecisionJournalError::Io {
                path: self.path.clone(),
                source,
            })
    }

    #[cfg(test)]
    fn injected_failure(&self, point: &str) -> Result<(), DecisionJournalError> {
        Err(DecisionJournalError::Io {
            path: self.path.clone(),
            source: std::io::Error::other(format!("injected append failure {point}")),
        })
    }
}

fn plan_manifest_path(journal_path: &Path) -> PathBuf {
    let mut path = journal_path.as_os_str().to_owned();
    path.push(".plan.json");
    PathBuf::from(path)
}

/// Pin the full plan even before the first eligible detection. Without this
/// companion manifest an empty journal carries no plan hash, so a differently
/// tuned source lane could acquire the same event lock later and silently arm
/// a drifted decision table as long as lane A had not fired yet.
fn pin_decision_plan(
    journal_path: &Path,
    decision_plan_sha1: &str,
    journal_created: bool,
) -> Result<(), DecisionJournalError> {
    let path = plan_manifest_path(journal_path);
    let expected = DecisionPlanManifest {
        schema_version: 1,
        decision_plan_sha1: decision_plan_sha1.to_owned(),
    };
    let mut encoded =
        serde_json::to_vec(&expected).map_err(|error| DecisionJournalError::Invalid {
            path: path.clone(),
            detail: format!("plan manifest serialization failed: {error}"),
        })?;
    encoded.push(b'\n');
    if journal_created {
        return match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut file) => {
                file.write_all(&encoded)
                    .map_err(|source| DecisionJournalError::Io {
                        path: path.clone(),
                        source,
                    })?;
                durable_sync(&file, &path)?;
                sync_directory(path.parent().expect("manifest has journal parent"))?;
                Ok(())
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                Err(DecisionJournalError::Invalid {
                    path,
                    detail: "plan manifest exists but journal was missing; refusing state reset"
                        .into(),
                })
            }
            Err(source) => Err(DecisionJournalError::Io { path, source }),
        };
    }

    let raw = match std::fs::read(&path) {
        Ok(raw) => raw,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Err(DecisionJournalError::Invalid {
                path,
                detail: "journal exists without its pinned plan manifest; refusing unpinned state"
                    .into(),
            });
        }
        Err(source) => {
            return Err(DecisionJournalError::Io { path, source });
        }
    };
    if raw.is_empty() || !raw.ends_with(b"\n") {
        return Err(DecisionJournalError::Invalid {
            path,
            detail: "truncated plan manifest (missing final newline)".into(),
        });
    }
    let actual: DecisionPlanManifest =
        serde_json::from_slice(&raw).map_err(|error| DecisionJournalError::Invalid {
            path: path.clone(),
            detail: format!("invalid plan manifest: {error}"),
        })?;
    if actual.schema_version != expected.schema_version
        || actual.decision_plan_sha1 != expected.decision_plan_sha1
    {
        return Err(DecisionJournalError::Invalid {
            path,
            detail: format!(
                "pinned decision plan {} != current {}",
                actual.decision_plan_sha1, expected.decision_plan_sha1
            ),
        });
    }
    Ok(())
}

fn durable_sync(file: &File, path: &Path) -> Result<(), DecisionJournalError> {
    file.sync_all().map_err(|source| DecisionJournalError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    #[cfg(target_os = "macos")]
    {
        // macOS fsync may leave data in volatile drive caches. F_FULLFSYNC is
        // the platform's explicit request to push through that boundary.
        // SAFETY: fcntl reads the valid descriptor and receives no pointer.
        let rc = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_FULLFSYNC) };
        if rc != 0 {
            return Err(DecisionJournalError::Io {
                path: path.to_path_buf(),
                source: std::io::Error::last_os_error(),
            });
        }
    }
    Ok(())
}

fn sync_directory(path: &Path) -> Result<(), DecisionJournalError> {
    let directory = File::open(path).map_err(|source| DecisionJournalError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    directory
        .sync_all()
        .map_err(|source| DecisionJournalError::Io {
            path: path.to_path_buf(),
            source,
        })
}

#[cfg(unix)]
fn lock_exclusive_nonblocking(file: &File, path: &Path) -> Result<(), DecisionJournalError> {
    // SAFETY: flock only reads the valid descriptor and does not retain a
    // pointer. The File owns the descriptor for the journal's full lifetime.
    let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc == 0 {
        Ok(())
    } else {
        Err(DecisionJournalError::Io {
            path: path.to_path_buf(),
            source: std::io::Error::last_os_error(),
        })
    }
}

#[cfg(not(unix))]
fn lock_exclusive_nonblocking(_file: &File, path: &Path) -> Result<(), DecisionJournalError> {
    Err(DecisionJournalError::Io {
        path: path.to_path_buf(),
        source: std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "persistent decision journal requires an exclusive file lock",
        ),
    })
}

fn tier_name(tier: Tier) -> &'static str {
    match tier {
        Tier::Fast => "fast",
        Tier::Confirmed => "confirmed",
    }
}

fn policy_name(policy: Policy) -> &'static str {
    match policy {
        Policy::FastOnly => "fast_only",
        Policy::ConfirmedOnly => "confirmed_only",
        Policy::TwoTier => "two_tier",
    }
}

fn parse_tier(value: &str) -> Option<Tier> {
    match value {
        "fast" => Some(Tier::Fast),
        "confirmed" => Some(Tier::Confirmed),
        _ => None,
    }
}

fn planned_transition(entry: &Entry, tier: Tier) -> Option<(u32, KeywordState)> {
    match (entry.state, entry.policy, tier) {
        (KeywordState::Done, _, _) => None,
        (KeywordState::FastFired, _, Tier::Fast) => None,
        (KeywordState::Armed, Policy::FastOnly, Tier::Fast) => {
            Some((entry.budget_contracts, KeywordState::Done))
        }
        (KeywordState::Armed, Policy::ConfirmedOnly, Tier::Confirmed) => {
            Some((entry.budget_contracts, KeywordState::Done))
        }
        (KeywordState::Armed, Policy::TwoTier, Tier::Fast) => Some((
            entry.budget_contracts * FAST_TRANCHE_PCT / 100,
            KeywordState::FastFired,
        )),
        (KeywordState::Armed, Policy::TwoTier, Tier::Confirmed)
        | (KeywordState::FastFired, Policy::TwoTier, Tier::Confirmed) => {
            let fast = if entry.state == KeywordState::FastFired {
                entry.budget_contracts * FAST_TRANCHE_PCT / 100
            } else {
                0
            };
            Some((entry.budget_contracts - fast, KeywordState::Done))
        }
        _ => None,
    }
}

#[derive(Serialize)]
struct DecisionPlanEntry<'a> {
    market_ticker: &'a str,
    phrase: &'a str,
    variants: &'a [String],
    max_price_cents: u8,
    budget_contracts: u32,
    policy: &'static str,
    rule_risk_haircut_cents: u8,
    fast_prefixes: &'a [String],
    fast_threshold_bits: Option<u32>,
    confirmed_threshold_bits: Option<u32>,
    boost_bits: Option<u32>,
    min_confidence_fast_bits: u32,
    min_confidence_confirmed_bits: u32,
}

pub fn persistent_plan_sha1(
    keywords: &[KeywordConfig],
    path: &Path,
) -> Result<String, DecisionJournalError> {
    let mut plan: Vec<_> = keywords
        .iter()
        .map(|kw| DecisionPlanEntry {
            market_ticker: &kw.market_ticker,
            phrase: &kw.phrase,
            variants: &kw.variants,
            max_price_cents: kw.max_price_cents,
            budget_contracts: kw.budget_contracts,
            policy: policy_name(kw.policy),
            rule_risk_haircut_cents: kw.rule_risk_haircut_cents,
            fast_prefixes: &kw.fast_prefixes,
            fast_threshold_bits: kw.fast_threshold.map(f32::to_bits),
            confirmed_threshold_bits: kw.confirmed_threshold.map(f32::to_bits),
            boost_bits: kw.boost.map(f32::to_bits),
            min_confidence_fast_bits: kw.min_confidence_fast.to_bits(),
            min_confidence_confirmed_bits: kw.min_confidence_confirmed.to_bits(),
        })
        .collect();
    plan.sort_by_key(|entry| entry.market_ticker);
    if let Some(duplicate) = plan
        .windows(2)
        .find(|pair| pair[0].market_ticker == pair[1].market_ticker)
    {
        return Err(DecisionJournalError::Invalid {
            path: path.to_path_buf(),
            detail: format!(
                "duplicate market_ticker {} has no stable persistent identity",
                duplicate[0].market_ticker
            ),
        });
    }
    let encoded = serde_json::to_vec(&plan).map_err(|error| DecisionJournalError::Invalid {
        path: path.to_path_buf(),
        detail: format!("decision plan serialization failed: {error}"),
    })?;
    Ok(format!("{:x}", Sha1::digest(encoded)))
}

impl DecisionTable {
    pub fn from_keywords(keywords: &[KeywordConfig]) -> Self {
        DecisionTable {
            entries: compile_entries(keywords),
            journal: None,
            journal_fault: Arc::new(OnceLock::new()),
        }
    }

    /// Construct a table whose consumed one-shot state survives process
    /// restarts. The journal is event-scoped (not attempt-scoped): a new arm
    /// must point at the same path for the same event.
    pub fn from_keywords_with_journal(
        keywords: &[KeywordConfig],
        journal_path: impl AsRef<Path>,
    ) -> Result<Self, DecisionJournalError> {
        let path = journal_path.as_ref();
        let decision_plan_sha1 = persistent_plan_sha1(keywords, path)?;
        let mut entries = compile_entries(keywords);
        let (journal, receipts) = FileDecisionJournal::open(path, &decision_plan_sha1)?;
        for (line_no, receipt) in receipts.iter().enumerate() {
            let tier = parse_tier(&receipt.tier).ok_or_else(|| DecisionJournalError::Invalid {
                path: path.to_path_buf(),
                detail: format!("line {} has unknown tier {:?}", line_no + 1, receipt.tier),
            })?;
            let entry = entries
                .iter_mut()
                .find(|entry| entry.market_ticker == receipt.market_ticker)
                .ok_or_else(|| DecisionJournalError::Invalid {
                    path: path.to_path_buf(),
                    detail: format!(
                        "line {} names market {} absent from the current config",
                        line_no + 1,
                        receipt.market_ticker
                    ),
                })?;
            let Some((_tranche, next_state)) = planned_transition(entry, tier) else {
                return Err(DecisionJournalError::Invalid {
                    path: path.to_path_buf(),
                    detail: format!(
                        "line {} repeats or violates the configured policy for {}",
                        line_no + 1,
                        receipt.market_ticker
                    ),
                });
            };
            let (expected_tranche, _) = planned_transition(entry, tier).expect("checked above");
            if receipt.policy != policy_name(entry.policy)
                || receipt.max_price_cents != entry.max_price_cents
                || receipt.budget_contracts != entry.budget_contracts
                || receipt.tranche_contracts != expected_tranche
            {
                return Err(DecisionJournalError::Invalid {
                    path: path.to_path_buf(),
                    detail: format!(
                        "line {} plan fields do not match the current config for {}",
                        line_no + 1,
                        receipt.market_ticker
                    ),
                });
            }
            if receipt.next_state != next_state.as_str() {
                return Err(DecisionJournalError::Invalid {
                    path: path.to_path_buf(),
                    detail: format!(
                        "line {} next_state {:?} != expected {:?}",
                        line_no + 1,
                        receipt.next_state,
                        next_state.as_str()
                    ),
                });
            }
            entry.state = next_state;
        }
        Ok(Self {
            entries,
            journal: Some(journal),
            journal_fault: Arc::new(OnceLock::new()),
        })
    }

    /// Read-only health handle for immediate composer-level fail-closed action.
    pub fn health(&self) -> DecisionHealth {
        DecisionHealth {
            fault: self.journal_fault.clone(),
        }
    }

    /// Duplicate the locked journal descriptor so assembly can hold the
    /// exclusivity boundary beyond pipeline teardown and through executor
    /// drain/report persistence. In-memory tables have no lease.
    pub fn journal_lease(&self) -> Result<Option<DecisionJournalLease>, DecisionJournalError> {
        self.journal
            .as_ref()
            .map(FileDecisionJournal::lease)
            .transpose()
    }

    #[cfg(test)]
    pub(crate) fn fail_next_append_before_write(&mut self) {
        self.journal
            .as_mut()
            .expect("test fault injection requires a persistent table")
            .test_failure = TestAppendFailure::BeforeWrite;
    }

    /// The first persistence fault, if any. Runtime health/reporting can use
    /// this to page and mark the run unauditable when assembly wiring lands.
    pub fn journal_error(&self) -> Option<&str> {
        self.journal_fault.get().map(String::as_str)
    }

    /// Checked form for callers that need to distinguish an ignored detection
    /// from a durability refusal.
    pub fn decide_checked(
        &mut self,
        det: &Detection,
    ) -> Result<Option<DecisionIntent>, DecisionJournalError> {
        let Some(entry) = self.entries.get(det.keyword.0 as usize) else {
            return Ok(None);
        };

        // Confidence gate before any state transition: a gated detection must
        // not consume the one-shot machine or append a receipt.
        let min_conf = match det.tier {
            Tier::Fast => entry.min_conf_fast,
            Tier::Confirmed => entry.min_conf_confirmed,
        };
        if det.confidence < min_conf {
            return Ok(None);
        }

        let Some((tranche, next_state)) = planned_transition(entry, det.tier) else {
            return Ok(None);
        };
        if let Some(fault) = self.journal_fault.get() {
            return Err(DecisionJournalError::Unavailable(fault.clone()));
        }

        if let Some(journal) = self.journal.as_mut() {
            if let Err(error) = journal.append(entry, det.tier, tranche, next_state) {
                let message = error.to_string();
                let _ = self.journal_fault.set(message.clone());
                return Err(DecisionJournalError::Unavailable(message));
            }
        }

        let market_ticker = entry.market_ticker.clone();
        let max_price_cents = entry.max_price_cents;
        // Durable receipt first, in-memory transition second, intent last.
        self.entries[det.keyword.0 as usize].state = next_state;
        Ok(Some(DecisionIntent {
            keyword: det.keyword,
            source: det.source,
            market_ticker,
            max_price_cents,
            tranche_contracts: tranche,
            tier: det.tier,
            audio_ts: det.audio_ts,
            decision_ts: MonotonicNs::now(),
            // Stamped at the ring boundary (pipeline::run_pipeline), not here.
            handoff_ts: MonotonicNs(0),
        }))
    }

    /// Compatibility door used by the current pipeline. Any journal failure
    /// is loud and fail-closed; assembly can adopt `decide_checked` when it
    /// adds a structured run-health receipt.
    pub fn decide(&mut self, det: &Detection) -> Option<DecisionIntent> {
        match self.decide_checked(det) {
            Ok(intent) => intent,
            Err(error) => {
                tracing::error!(error = %error, "decision journal failure — intent REFUSED");
                None
            }
        }
    }
}

fn compile_entries(keywords: &[KeywordConfig]) -> Vec<Entry> {
    keywords
        .iter()
        .map(|kw| Entry {
            market_ticker: kw.market_ticker.clone(),
            max_price_cents: kw.max_price_cents,
            budget_contracts: kw.budget_contracts,
            policy: kw.policy,
            state: KeywordState::Armed,
            min_conf_fast: kw.min_confidence_fast,
            min_conf_confirmed: kw.min_confidence_confirmed,
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::KeywordId;

    fn kw(policy: Policy) -> KeywordConfig {
        KeywordConfig {
            market_ticker: "KX-TEST".into(),
            phrase: "test".into(),
            variants: vec!["test".into()],
            max_price_cents: 97,
            budget_contracts: 200,
            policy,
            rule_risk_haircut_cents: 0,
            fast_prefixes: vec![],
            fast_threshold: None,
            confirmed_threshold: None,
            boost: None,
            min_confidence_fast: 0.0,
            min_confidence_confirmed: 0.0,
        }
    }

    fn det(tier: Tier) -> Detection {
        Detection {
            keyword: KeywordId(0),
            confidence: 1.0,
            tier,
            audio_ts: MonotonicNs::now(),
            emit_ts: MonotonicNs::now(),
            audio_pos_ms: 0,
            audio_consumed_ms: 0,
            source: crate::types::SourceId(0),
        }
    }

    #[test]
    fn two_tier_releases_tranche_then_remainder_then_nothing() {
        let mut table = DecisionTable::from_keywords(&[kw(Policy::TwoTier)]);
        let fast = table.decide(&det(Tier::Fast)).unwrap();
        assert_eq!(fast.tranche_contracts, 60); // 30% of 200
        let confirm = table.decide(&det(Tier::Confirmed)).unwrap();
        assert_eq!(confirm.tranche_contracts, 140);
        assert!(table.decide(&det(Tier::Fast)).is_none());
        assert!(table.decide(&det(Tier::Confirmed)).is_none());
    }

    #[test]
    fn confirmed_only_ignores_fast_hits() {
        let mut table = DecisionTable::from_keywords(&[kw(Policy::ConfirmedOnly)]);
        assert!(table.decide(&det(Tier::Fast)).is_none());
        let confirm = table.decide(&det(Tier::Confirmed)).unwrap();
        assert_eq!(confirm.tranche_contracts, 200);
        assert!(table.decide(&det(Tier::Confirmed)).is_none());
    }

    #[test]
    fn confirm_without_prior_fast_gets_full_budget() {
        let mut table = DecisionTable::from_keywords(&[kw(Policy::TwoTier)]);
        let confirm = table.decide(&det(Tier::Confirmed)).unwrap();
        assert_eq!(confirm.tranche_contracts, 200);
        assert!(table.decide(&det(Tier::Fast)).is_none());
    }

    fn det_conf(tier: Tier, confidence: f32) -> Detection {
        Detection {
            confidence,
            ..det(tier)
        }
    }

    #[test]
    fn confidence_gate_blocks_without_consuming_state() {
        let mut keyword = kw(Policy::TwoTier);
        keyword.min_confidence_fast = 0.6;
        keyword.min_confidence_confirmed = 0.3;
        let mut table = DecisionTable::from_keywords(&[keyword]);

        // Below-gate Fast is dropped AND the machine stays Armed…
        assert!(table.decide(&det_conf(Tier::Fast, 0.5)).is_none());
        // …so an above-gate Fast still releases the tranche afterwards.
        let fast = table.decide(&det_conf(Tier::Fast, 0.7)).unwrap();
        assert_eq!(fast.tranche_contracts, 60);
        // Per-tier independence: 0.4 fails the Fast gate but passes Confirmed.
        let confirm = table.decide(&det_conf(Tier::Confirmed, 0.4)).unwrap();
        assert_eq!(confirm.tranche_contracts, 140);
    }

    #[test]
    fn default_zero_gates_pass_everything() {
        let mut table = DecisionTable::from_keywords(&[kw(Policy::TwoTier)]);
        assert!(table.decide(&det_conf(Tier::Fast, 0.0)).is_some());
    }

    /// Fanout-mode dedupe proof: N detect threads race `decide()` for the
    /// same keyword through a shared mutex — the one-shot machine hands out
    /// exactly one intent no matter who wins the lock.
    #[test]
    fn concurrent_decides_yield_exactly_one_intent() {
        use std::sync::{Arc, Barrier, Mutex};
        let table = Arc::new(Mutex::new(DecisionTable::from_keywords(&[kw(
            Policy::ConfirmedOnly,
        )])));
        let barrier = Arc::new(Barrier::new(4));
        let fired: Vec<_> = (0..4)
            .map(|i| {
                let table = table.clone();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    let mut d = det(Tier::Confirmed);
                    d.source = crate::types::SourceId(i);
                    barrier.wait();
                    table.lock().unwrap().decide(&d)
                })
            })
            .collect::<Vec<_>>()
            .into_iter()
            .map(|h| h.join().unwrap())
            .filter(|r| r.is_some())
            .collect();
        assert_eq!(
            fired.len(),
            1,
            "one-shot must fire exactly once across racing legs"
        );
    }

    #[test]
    fn journal_rehydrates_done_and_prevents_restart_refire() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("decision.jsonl");
        {
            let mut table =
                DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &path)
                    .unwrap();
            assert!(table.decide(&det(Tier::Confirmed)).is_some());
            let rows: Vec<serde_json::Value> = std::fs::read_to_string(&path)
                .unwrap()
                .lines()
                .map(|line| serde_json::from_str(line).unwrap())
                .collect();
            assert_eq!(rows.len(), 1);
            assert_eq!(rows[0]["market_ticker"], "KX-TEST");
            assert_eq!(rows[0]["policy"], "confirmed_only");
            assert_eq!(rows[0]["budget_contracts"], 200);
            assert_eq!(rows[0]["tranche_contracts"], 200);
            assert_eq!(rows[0]["tier"], "confirmed");
            assert_eq!(rows[0]["next_state"], "done");
        }

        let mut restarted =
            DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &path).unwrap();
        assert!(restarted.decide(&det(Tier::Confirmed)).is_none());
        assert_eq!(std::fs::read_to_string(&path).unwrap().lines().count(), 1);
    }

    #[test]
    fn journal_rehydrates_two_tier_remainder_across_restart() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("decision.jsonl");
        {
            let mut first =
                DecisionTable::from_keywords_with_journal(&[kw(Policy::TwoTier)], &path).unwrap();
            assert_eq!(
                first.decide(&det(Tier::Fast)).unwrap().tranche_contracts,
                60
            );
        }
        {
            let mut second =
                DecisionTable::from_keywords_with_journal(&[kw(Policy::TwoTier)], &path).unwrap();
            assert_eq!(
                second
                    .decide(&det(Tier::Confirmed))
                    .unwrap()
                    .tranche_contracts,
                140
            );
        }
        let mut third =
            DecisionTable::from_keywords_with_journal(&[kw(Policy::TwoTier)], &path).unwrap();
        assert!(third.decide(&det(Tier::Fast)).is_none());
        assert!(third.decide(&det(Tier::Confirmed)).is_none());
        assert_eq!(std::fs::read_to_string(&path).unwrap().lines().count(), 2);
    }

    #[test]
    fn confidence_refusal_does_not_consume_or_journal() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("decision.jsonl");
        let mut keyword = kw(Policy::ConfirmedOnly);
        keyword.min_confidence_confirmed = 0.8;
        let mut table = DecisionTable::from_keywords_with_journal(&[keyword], &path).unwrap();
        assert!(table.decide(&det_conf(Tier::Confirmed, 0.7)).is_none());
        assert!(std::fs::read_to_string(&path).unwrap().is_empty());
        assert!(table.decide(&det_conf(Tier::Confirmed, 0.9)).is_some());
    }

    #[test]
    fn append_failure_poison_is_fail_closed_without_consuming_state() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("decision.jsonl");
        {
            let mut table =
                DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &path)
                    .unwrap();
            let health = table.health();
            table.journal.as_mut().unwrap().test_failure = TestAppendFailure::BeforeWrite;
            assert!(table.decide_checked(&det(Tier::Confirmed)).is_err());
            assert!(table.journal_error().is_some());
            assert!(health.fault_detail().is_some());
            // A poisoned process never guesses whether a later retry is safe.
            assert!(table.decide_checked(&det(Tier::Confirmed)).is_err());
            assert!(std::fs::read_to_string(&path).unwrap().is_empty());
        }
        // The injected failure happened before any bytes were appended, so a
        // clean reconstruction proves the in-memory state was not consumed.
        let mut restarted =
            DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &path).unwrap();
        assert!(restarted.decide(&det(Tier::Confirmed)).is_some());
    }

    #[test]
    fn partial_append_poison_leaves_a_refusing_tail_and_no_intent() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("partial.jsonl");
        {
            let mut table =
                DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &path)
                    .unwrap();
            table.journal.as_mut().unwrap().test_failure = TestAppendFailure::AfterPartialWrite;
            assert!(table.decide_checked(&det(Tier::Confirmed)).is_err());
            assert!(table.journal_error().is_some());
        }
        assert!(matches!(
            DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &path),
            Err(DecisionJournalError::Invalid { .. })
        ));
    }

    #[test]
    fn full_write_sync_failure_emits_no_intent_and_replay_stays_consumed() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("unsynced.jsonl");
        {
            let mut table =
                DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &path)
                    .unwrap();
            table.journal.as_mut().unwrap().test_failure = TestAppendFailure::AfterWriteBeforeSync;
            assert!(table.decide_checked(&det(Tier::Confirmed)).is_err());
            assert_eq!(std::fs::read_to_string(&path).unwrap().lines().count(), 1);
        }
        // If the ambiguous full write survived, reconstruction consumes it;
        // it must never risk a duplicate merely because no intent escaped.
        let mut restarted =
            DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &path).unwrap();
        assert!(restarted.decide(&det(Tier::Confirmed)).is_none());
    }

    #[test]
    fn corrupt_or_policy_incompatible_journal_refuses_to_rearm() {
        let dir = tempfile::tempdir().unwrap();
        let truncated = dir.path().join("truncated.jsonl");
        std::fs::write(&truncated, b"{\"schema_version\":1").unwrap();
        assert!(matches!(
            DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &truncated),
            Err(DecisionJournalError::Invalid { .. })
        ));

        let drift = dir.path().join("drift.jsonl");
        {
            let mut original =
                DecisionTable::from_keywords_with_journal(&[kw(Policy::TwoTier)], &drift).unwrap();
            assert!(original.decide(&det(Tier::Fast)).is_some());
        }
        assert!(matches!(
            DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &drift),
            Err(DecisionJournalError::Invalid { .. })
        ));

        let budget_drift = dir.path().join("budget-drift.jsonl");
        {
            let mut original =
                DecisionTable::from_keywords_with_journal(&[kw(Policy::TwoTier)], &budget_drift)
                    .unwrap();
            assert!(original.decide(&det(Tier::Fast)).is_some());
        }
        let mut changed = kw(Policy::TwoTier);
        changed.budget_contracts = 100;
        assert!(matches!(
            DecisionTable::from_keywords_with_journal(&[changed], &budget_drift),
            Err(DecisionJournalError::Invalid { .. })
        ));

        let mut extra = kw(Policy::ConfirmedOnly);
        extra.market_ticker = "KX-EXTRA".into();
        assert!(matches!(
            DecisionTable::from_keywords_with_journal(&[kw(Policy::TwoTier), extra], &budget_drift,),
            Err(DecisionJournalError::Invalid { .. })
        ));
    }

    #[test]
    fn persistent_constructor_refuses_duplicate_tickers_and_missing_parent() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("decision.jsonl");
        assert!(matches!(
            DecisionTable::from_keywords_with_journal(
                &[kw(Policy::ConfirmedOnly), kw(Policy::ConfirmedOnly)],
                &path,
            ),
            Err(DecisionJournalError::Invalid { .. })
        ));
        assert!(matches!(
            DecisionTable::from_keywords_with_journal(
                &[kw(Policy::ConfirmedOnly)],
                dir.path().join("missing/event/decision.jsonl"),
            ),
            Err(DecisionJournalError::Io { .. })
        ));
    }

    #[test]
    fn empty_journal_still_pins_the_complete_decision_plan() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("decision.jsonl");
        let first =
            DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &path).unwrap();
        drop(first);
        assert!(std::fs::read_to_string(&path).unwrap().is_empty());
        assert!(plan_manifest_path(&path).is_file());

        let mut drifted = kw(Policy::ConfirmedOnly);
        drifted.min_confidence_confirmed = 0.25;
        assert!(matches!(
            DecisionTable::from_keywords_with_journal(&[drifted], &path),
            Err(DecisionJournalError::Invalid { .. })
        ));
    }

    #[test]
    fn journal_and_plan_manifest_are_an_indivisible_state_pair() {
        let dir = tempfile::tempdir().unwrap();
        let missing_journal = dir.path().join("missing-journal.jsonl");
        drop(
            DecisionTable::from_keywords_with_journal(
                &[kw(Policy::ConfirmedOnly)],
                &missing_journal,
            )
            .unwrap(),
        );
        std::fs::remove_file(&missing_journal).unwrap();
        assert!(matches!(
            DecisionTable::from_keywords_with_journal(
                &[kw(Policy::ConfirmedOnly)],
                &missing_journal,
            ),
            Err(DecisionJournalError::Invalid { .. })
        ));
        assert!(!missing_journal.exists());
        assert!(matches!(
            DecisionTable::from_keywords_with_journal(
                &[kw(Policy::ConfirmedOnly)],
                &missing_journal,
            ),
            Err(DecisionJournalError::Invalid { .. })
        ));

        let missing_plan = dir.path().join("missing-plan.jsonl");
        drop(
            DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &missing_plan)
                .unwrap(),
        );
        std::fs::remove_file(plan_manifest_path(&missing_plan)).unwrap();
        assert!(matches!(
            DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &missing_plan,),
            Err(DecisionJournalError::Invalid { .. })
        ));
    }

    #[test]
    fn journal_lock_refuses_two_live_arms_for_one_event() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("decision.jsonl");
        let first =
            DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &path).unwrap();
        let second = DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &path);
        assert!(matches!(second, Err(DecisionJournalError::Io { .. })));
        drop(first);
        assert!(
            DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &path).is_ok()
        );
    }

    #[test]
    fn cloned_lease_holds_lock_after_pipeline_table_drops() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("decision.jsonl");
        let first =
            DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &path).unwrap();
        let lease = first.journal_lease().unwrap().unwrap();
        drop(first);
        assert!(matches!(
            DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &path),
            Err(DecisionJournalError::Io { .. })
        ));
        drop(lease);
        assert!(
            DecisionTable::from_keywords_with_journal(&[kw(Policy::ConfirmedOnly)], &path).is_ok()
        );
    }
}
