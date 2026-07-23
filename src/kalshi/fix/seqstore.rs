//! Durable FIX sequence numbers.
//!
//! The default posture is KalshiNR (141=Y reset-on-logon), where persistence
//! is a no-op. A future KalshiRT session (retransmission, seqnums survive
//! reconnects — Kalshi never resets them at maintenance, F9) plugs in
//! `FileSeqStore`, which follows the `probe_ledger.json` pattern: in-memory
//! authority, atomic file write.

use std::path::PathBuf;

pub trait SeqStore: Send + Sync {
    /// (next outbound seq, next expected inbound seq); (1, 1) when fresh.
    fn load(&self) -> (u64, u64);
    fn save(&self, next_out: u64, next_in: u64);
}

/// KalshiNR: sequences reset on every logon — nothing to persist.
pub struct NoopSeqStore;

impl SeqStore for NoopSeqStore {
    fn load(&self) -> (u64, u64) {
        (1, 1)
    }
    fn save(&self, _next_out: u64, _next_in: u64) {}
}

/// KalshiRT: `state/fix_seq.json` via the atomic-write pattern.
pub struct FileSeqStore {
    path: PathBuf,
}

impl FileSeqStore {
    pub fn new(state_dir: &std::path::Path) -> Self {
        FileSeqStore { path: state_dir.join("fix_seq.json") }
    }
}

impl SeqStore for FileSeqStore {
    fn load(&self) -> (u64, u64) {
        let Ok(bytes) = std::fs::read(&self.path) else {
            return (1, 1);
        };
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap_or_default();
        (v["next_out"].as_u64().unwrap_or(1), v["next_in"].as_u64().unwrap_or(1))
    }

    fn save(&self, next_out: u64, next_in: u64) {
        let v = serde_json::json!({ "next_out": next_out, "next_in": next_in });
        if let Err(e) = crate::fsio::write_atomic(&self.path, v.to_string().as_bytes()) {
            tracing::error!("fix_seq.json write failed: {e}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn file_store_round_trips_and_defaults_fresh() {
        let dir = tempfile::tempdir().unwrap();
        let store = FileSeqStore::new(dir.path());
        assert_eq!(store.load(), (1, 1));
        store.save(42, 17);
        assert_eq!(store.load(), (42, 17));
        assert_eq!(NoopSeqStore.load(), (1, 1));
    }
}
