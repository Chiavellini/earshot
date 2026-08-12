//! Atomic file writes for money/audit state (probe ledger, report, rules).
//! `std::fs::write` in place can be interrupted mid-write (SIGTERM/SIGKILL/
//! power), leaving truncated JSON; for the probe ledger that would destroy
//! the cross-run spend backstop. Write-to-temp + rename is atomic on POSIX.

use std::io::Write;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

/// Write `contents` to `path` atomically: unique temp file in the same
/// directory (rename is only atomic within a filesystem), fsync, rename.
/// Never on the fire path — callers are teardown, the ledger persister, and
/// arm-time validation.
pub fn write_atomic(path: &Path, contents: &[u8]) -> std::io::Result<()> {
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let seq = SEQ.fetch_add(1, Ordering::Relaxed);
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(format!(".tmp.{}.{}", std::process::id(), seq));
    let tmp = std::path::PathBuf::from(tmp);
    let res = (|| {
        let mut f = std::fs::File::create(&tmp)?;
        f.write_all(contents)?;
        f.sync_all()?;
        std::fs::rename(&tmp, path)
    })();
    if res.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    res
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_content_and_leaves_no_temp_residue() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("ledger.json");
        write_atomic(&path, b"{\"spent_cents\": 7}").unwrap();
        assert_eq!(
            std::fs::read_to_string(&path).unwrap(),
            "{\"spent_cents\": 7}"
        );
        // Overwrite is atomic too.
        write_atomic(&path, b"{\"spent_cents\": 9}").unwrap();
        assert_eq!(
            std::fs::read_to_string(&path).unwrap(),
            "{\"spent_cents\": 9}"
        );
        let residue: Vec<_> = std::fs::read_dir(dir.path())
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .filter(|n| n != "ledger.json")
            .collect();
        assert!(residue.is_empty(), "temp residue: {residue:?}");
    }
}
