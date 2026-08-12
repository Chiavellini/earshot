//! SIGTERM is the PRODUCTION kill: launchd, `launch-event.sh`'s EXIT trap,
//! and the supervisor's port cleanup all deliver it (post-mortem 2026-07-15:
//! the STOP-file drill never exercised this path, and unhandled SIGTERM
//! skipped cancel/flush/report entirely). This test raises a real SIGTERM at
//! the process and asserts the full kill sequence runs: disarm, cancel,
//! durable ledger flush, on_terminate. Lives in its own integration-test
//! binary because the handler is process-global and the signal is real.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use earshot::risk::{spawn_kill_watcher, RiskConfig, RiskGate, SIGTERM_EXITS};

#[tokio::test]
async fn sigterm_cancels_flushes_and_reports() {
    // Keep the test runner alive past the kill sequence.
    SIGTERM_EXITS.store(false, Ordering::SeqCst);

    let state = tempfile::tempdir().unwrap();
    let gate = Arc::new(
        RiskGate::new(
            RiskConfig {
                allowlist_tickers: vec!["T".into()],
                ..Default::default()
            },
            state.path(),
        )
        .unwrap(),
    );
    gate.arm();
    // Debit exists in memory only — no persister spawned, no flush yet. The
    // kill path must make it durable.
    gate.record_send(7, 1);

    let cancelled = Arc::new(AtomicBool::new(false));
    let terminated = Arc::new(AtomicBool::new(false));
    let c2 = cancelled.clone();
    let t2 = terminated.clone();
    let watcher = spawn_kill_watcher(
        gate.clone(),
        state.path().join("STOP"),
        move || {
            let c = c2.clone();
            async move {
                c.store(true, Ordering::SeqCst);
            }
        },
        move || {
            t2.store(true, Ordering::SeqCst);
        },
    );

    // Let the watcher install its handler, then deliver a REAL SIGTERM.
    tokio::time::sleep(Duration::from_millis(300)).await;
    let status = std::process::Command::new("kill")
        .args(["-TERM", &std::process::id().to_string()])
        .status()
        .unwrap();
    assert!(status.success());

    watcher.await.unwrap();
    assert!(gate.is_killed(), "gate must disarm on SIGTERM");
    assert!(
        cancelled.load(Ordering::SeqCst),
        "cancel path must run on SIGTERM"
    );
    assert!(
        terminated.load(Ordering::SeqCst),
        "on_terminate must run on SIGTERM"
    );
    // The in-memory debit reached disk (the flush the old code never did).
    let ledger = std::fs::read_to_string(state.path().join("probe_ledger.json")).unwrap();
    let v: serde_json::Value = serde_json::from_str(&ledger).unwrap();
    assert_eq!(v["spent_cents"], 7);
}
