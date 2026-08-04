//! Stub-driven tests for `scripts/redial-supervisor.sh` — the mid-call redial
//! consumer.
//!
//! WHY THIS TEST EXISTS. Until 2026-08-04 NOTHING drove this script: its node
//! contract (docs/nodes/control-supervise.md §Verification) prescribed tests
//! for stale-marker / STOP / budget / failed-dial that did not exist, and all
//! four of its 2026-08-01 fixes (clock-derived budgets 7942a41, media-confirm
//! f67ac27, front-door probe 6fe1e30, posture suite 942deb0) were verified by
//! stubs run by hand. The supervisor is what stands between a mid-call drop
//! and a lost event; it gets the same treatment as the launcher.
//!
//! Method: run the real script with `--dial-script` / `--hangup-script`
//! pointed at recording stubs, drive it with real marker files, then read its
//! own `redial.log` and the stubs' invocation logs. No network: Twilio env is
//! removed (its far-end status probe short-circuits), notify goes to a stub
//! via `EARSHOT_NOTIFY`. Marker-poll cadence is 0.25 s, so every wait here is
//! ≥ 0.8 s.

use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::time::Duration;

fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

struct Rig {
    state_dir: PathBuf,
    child: Child,
    _tmp: tempfile::TempDir,
}

impl Rig {
    fn log(&self) -> String {
        std::fs::read_to_string(self.state_dir.join("redial.log")).unwrap_or_default()
    }
    fn dials(&self) -> usize {
        std::fs::read_to_string(self.state_dir.join("dial-invocations.log"))
            .map(|s| s.lines().count())
            .unwrap_or(0)
    }
    fn hangups(&self) -> String {
        std::fs::read_to_string(self.state_dir.join("hangup-invocations.log")).unwrap_or_default()
    }
    fn pages(&self) -> String {
        std::fs::read_to_string(self.state_dir.join("pages.log")).unwrap_or_default()
    }
    fn write_marker(&self, unix_ts: u64, old_sid: &str, attempt: u32, reason: &str) {
        // Same atomic write-then-rename the source uses; content differing
        // from the last consumed content is what triggers the supervisor.
        let body = format!(
            "{{\"unix_ts\":{unix_ts},\"old_call_sid\":\"{old_sid}\",\"attempt\":{attempt},\"reason\":\"{reason}\"}}\n"
        );
        let tmp = self.state_dir.join(".marker.tmp");
        std::fs::write(&tmp, body).unwrap();
        std::fs::rename(&tmp, self.state_dir.join("redial.request")).unwrap();
    }
    fn ready(&self) {
        std::fs::write(
            self.state_dir.join("listener.ready"),
            r#"{"port":8081,"pid":1,"unix_ts":1}"#,
        )
        .unwrap();
    }
    /// Poll until the dial count reaches `n` (or timeout). Dials land ~1 s
    /// after a marker (0.25 s poll cadence + concurrent hangup), so every
    /// dial expectation must wait, not sleep-then-assert.
    fn await_dials(&self, n: usize, secs: u64) -> usize {
        for _ in 0..(secs * 10) {
            if self.dials() >= n {
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        self.dials()
    }
    fn await_log(&self, needle: &str, secs: u64) -> bool {
        for _ in 0..(secs * 10) {
            if self.log().contains(needle) {
                return true;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        false
    }
    fn await_pages(&self, needle: &str, secs: u64) -> bool {
        for _ in 0..(secs * 10) {
            if self.pages().contains(needle) {
                return true;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        false
    }
    fn alive(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }
    /// Wait for the supervisor to exit on its own (STOP / grace / max-minutes).
    fn wait_exit(&mut self, secs: u64) -> Option<i32> {
        for _ in 0..(secs * 10) {
            if let Ok(Some(st)) = self.child.try_wait() {
                return st.code();
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        None
    }
}

impl Drop for Rig {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn write_stub(path: &Path, body: &str) {
    let mut f = std::fs::File::create(path).unwrap();
    writeln!(f, "#!/bin/bash").unwrap();
    f.write_all(body.as_bytes()).unwrap();
    drop(f);
    let mut perm = std::fs::metadata(path).unwrap().permissions();
    use std::os::unix::fs::PermissionsExt;
    perm.set_mode(0o755);
    std::fs::set_permissions(path, perm).unwrap();
}

/// Spawn the real supervisor with recording stubs. `dial_plan` gives the exit
/// code per successive dial invocation (missing entries = 0). `ready` writes
/// listener.ready before startup. `pre_marker` plants marker content BEFORE
/// startup (the baseline case).
fn spawn(
    extra: &[&str],
    dial_plan: &[i32],
    ready: bool,
    pre_marker: Option<&str>,
) -> Rig {
    let tmp = tempfile::tempdir().unwrap();
    let state_dir = tmp.path().join("state-ev");
    std::fs::create_dir_all(&state_dir).unwrap();

    // Dial stub: record the invocation, take the Nth exit code from the plan,
    // and on success write the sid file like the real dial script would.
    write_stub(
        &state_dir.join("dial-stub.sh"),
        r#"DIR="$(cd "$(dirname "$0")" && pwd)"
echo "$*" >> "$DIR/dial-invocations.log"
N=$(wc -l < "$DIR/dial-invocations.log" | tr -d ' ')
CODE=$(sed -n "${N}p" "$DIR/dial-exit-plan" 2>/dev/null)
CODE=${CODE:-0}
if [ "$CODE" = "0" ]; then
  prev=""
  for a in "$@"; do
    if [ "$prev" = "--sid-file" ]; then echo "CAstub$N" > "$a"; fi
    prev="$a"
  done
fi
exit "$CODE"
"#,
    );
    write_stub(
        &state_dir.join("hangup-stub.sh"),
        r#"DIR="$(cd "$(dirname "$0")" && pwd)"
echo "$1" >> "$DIR/hangup-invocations.log"
exit 0
"#,
    );
    write_stub(
        &state_dir.join("notify-stub.sh"),
        r#"DIR="$(cd "$(dirname "$0")" && pwd)"
echo "$1|$2" >> "$DIR/pages.log"
exit 0
"#,
    );
    let plan: String = dial_plan.iter().map(|c| format!("{c}\n")).collect();
    std::fs::write(state_dir.join("dial-exit-plan"), plan).unwrap();

    if ready {
        std::fs::write(
            state_dir.join("listener.ready"),
            r#"{"port":8081,"pid":1,"unix_ts":1}"#,
        )
        .unwrap();
    }
    if let Some(m) = pre_marker {
        std::fs::write(state_dir.join("redial.request"), m).unwrap();
    }

    let script = repo().join("scripts/redial-supervisor.sh");
    let mut cmd = Command::new("bash");
    cmd.arg(&script)
        .arg("--state-dir")
        .arg(&state_dir)
        .args(["--to", "+15550000000", "--stream-url", "wss://example.invalid/media"])
        .arg("--dial-script")
        .arg(state_dir.join("dial-stub.sh"))
        .arg("--hangup-script")
        .arg(state_dir.join("hangup-stub.sh"))
        .args(extra)
        .env("EARSHOT_NOTIFY", state_dir.join("notify-stub.sh"))
        .env_remove("TWILIO_ACCOUNT_SID")
        .env_remove("TWILIO_AUTH_TOKEN")
        .env_remove("TWILIO_FROM_NUMBER")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    let child = cmd.spawn().expect("spawn redial-supervisor.sh");

    Rig { state_dir, child, _tmp: tmp }
}

fn now_ts() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs()
}

fn settle() {
    std::thread::sleep(Duration::from_millis(900));
}

#[test]
fn startup_marker_is_baseline_not_a_dial() {
    // A marker left by a PREVIOUS run must be consumed as baseline: redialing
    // at startup would place a paid call for a drop that may be hours old.
    let mut rig = spawn(
        &[],
        &[],
        true,
        Some(r#"{"unix_ts":1,"old_call_sid":"CAold","attempt":1,"reason":"idle"}"#),
    );
    std::thread::sleep(Duration::from_secs(2));
    assert_eq!(rig.dials(), 0, "startup marker dialed:\n{}", rig.log());
    assert!(rig.alive(), "supervisor must keep watching after baselining");
}

#[test]
fn stop_file_halts_without_dialing() {
    let rig = spawn(&[], &[], true, None);
    std::fs::write(rig.state_dir.join("STOP"), "").unwrap();
    rig.write_marker(now_ts(), "CA1", 1, "idle");
    let mut rig = rig;
    let code = rig.wait_exit(5);
    assert_eq!(code, Some(0), "STOP must exit 0:\n{}", rig.log());
    assert_eq!(rig.dials(), 0, "dialed despite STOP:\n{}", rig.log());
    assert!(rig.log().contains("STOP"), "log must name STOP:\n{}", rig.log());
}

#[test]
fn redial_budget_is_respected() {
    let rig = spawn(&["--max-redials", "2"], &[0, 0], true, None);
    settle();
    rig.write_marker(now_ts(), "", 1, "idle");
    assert_eq!(rig.await_dials(1, 4), 1, "first request must dial:\n{}", rig.log());
    rig.write_marker(now_ts(), "", 2, "idle");
    assert_eq!(rig.await_dials(2, 4), 2, "second request must dial:\n{}", rig.log());
    rig.write_marker(now_ts(), "", 3, "idle");
    assert!(
        rig.await_log("budget exhausted", 4),
        "third request must log exhaustion:\n{}",
        rig.log()
    );
    assert_eq!(rig.dials(), 2, "budget 2 must cap dials:\n{}", rig.log());
}

#[test]
fn failed_dial_backs_off_and_success_resets() {
    let rig = spawn(&[], &[1, 0], true, None);
    settle();
    rig.write_marker(now_ts(), "", 1, "idle");
    assert!(
        rig.await_log("FAILED — backing off 1s", 4),
        "first failed dial must back off 1 s:\n{}",
        rig.log()
    );
    rig.write_marker(now_ts(), "", 2, "idle");
    assert_eq!(rig.await_dials(2, 4), 2, "second request must dial again:\n{}", rig.log());
    assert!(
        rig.await_log("redialed OK (#2)", 4),
        "second dial succeeds and resets backoff:\n{}",
        rig.log()
    );
}

#[test]
fn stop_after_grace_exits_zero_without_dial() {
    let rig = spawn(&["--call-end-grace", "1"], &[], true, None);
    std::thread::sleep(Duration::from_secs(2)); // pass the grace window
    rig.write_marker(now_ts(), "", 1, "stop");
    let mut rig = rig;
    let code = rig.wait_exit(5);
    assert_eq!(code, Some(0), "post-grace stop must exit 0:\n{}", rig.log());
    assert_eq!(rig.dials(), 0, "post-grace stop must not dial:\n{}", rig.log());
    assert!(
        rig.log().contains("treating as call end"),
        "log must name the call end:\n{}",
        rig.log()
    );
}

#[test]
fn early_stop_still_redials() {
    // An IVR hangup during joining is reason=stop too — before the grace
    // window it is a drop worth redialing (the 7942a41 regression's sibling:
    // a mis-anchored clock turned every early stop into a false "call over").
    let rig = spawn(&["--call-end-grace", "3600"], &[0], true, None);
    settle();
    rig.write_marker(now_ts(), "", 1, "stop");
    assert_eq!(rig.await_dials(1, 4), 1, "early stop must redial:\n{}", rig.log());
}

#[test]
fn missing_listener_ready_refuses_then_honors_pending_request() {
    let rig = spawn(&[], &[0], false, None);
    settle();
    rig.write_marker(now_ts(), "", 1, "idle");
    assert!(
        rig.await_log("refusing to dial", 4),
        "refusal must be logged:\n{}",
        rig.log()
    );
    assert_eq!(rig.dials(), 0, "dialed without listener.ready:\n{}", rig.log());
    // Readiness returns: the STANDING request must fire with no new marker —
    // the 2026-07-29 class (a dial into a pipeline that cannot accept) ends
    // the moment the pipeline can.
    rig.ready();
    assert_eq!(rig.await_dials(1, 4), 1, "pending request must fire once ready:\n{}", rig.log());
}

#[test]
fn same_second_double_marker_both_fire() {
    // The watch is on CONTENT, not mtime: two drops in the same second must
    // both dial (mtime granularity would silently eat the second one).
    let rig = spawn(&[], &[0, 0], true, None);
    settle();
    let ts = now_ts();
    rig.write_marker(ts, "CAa", 1, "idle");
    assert_eq!(rig.await_dials(1, 4), 1, "first same-second marker must dial:\n{}", rig.log());
    rig.write_marker(ts, "CAb", 2, "idle"); // same unix_ts, different content
    assert_eq!(rig.await_dials(2, 4), 2, "both same-second markers must dial:\n{}", rig.log());
}

#[test]
fn redial_no_media_flagged_and_paged() {
    // f67ac27: "a redial that lands is not a redial that captures". A fresh
    // idle marker right after a dial means the dial produced no audio — that
    // must be logged AND paged (2026-08-04: a log line nobody reads during a
    // live call is the PYPL/BA lesson again).
    let rig = spawn(&["--media-confirm-secs", "10"], &[0, 0], true, None);
    settle();
    rig.write_marker(now_ts(), "", 1, "idle");
    assert_eq!(rig.await_dials(1, 4), 1, "first redial must dial:\n{}", rig.log());
    // A second drop within the media-confirm window = the first redial made no
    // audio.
    rig.write_marker(now_ts(), "", 2, "idle");
    assert!(
        rig.await_log("REDIAL-NO-MEDIA", 4),
        "no-media redial must be flagged:\n{}",
        rig.log()
    );
    assert!(
        rig.await_pages("REDIAL-NO-MEDIA", 4),
        "no-media redial must PAGE (pages: {:?}):\n{}",
        rig.pages(),
        rig.log()
    );
}

#[test]
fn synthetic_no_stream_marker_dials_and_hangs_up_old_call() {
    // Gap Q wiring: the launcher's watchdog writes reason="no_stream" with
    // the deaf CallSid. The supervisor needs no special case — any non-stop
    // reason redials, and the old (deaf) call gets hung up concurrently.
    let rig = spawn(&[], &[0], true, None);
    std::fs::write(rig.state_dir.join("call.sid"), "CAdeaf\n").unwrap();
    settle();
    rig.write_marker(now_ts(), "CAdeaf", 0, "no_stream");
    assert_eq!(rig.await_dials(1, 4), 1, "no_stream must redial:\n{}", rig.log());
    assert!(
        rig.await_log("hung up previous call CAdeaf", 4),
        "the deaf call must be hung up (hangups: {:?}):\n{}",
        rig.hangups(),
        rig.log()
    );
}
