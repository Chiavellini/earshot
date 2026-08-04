//! Posture regression for `scripts/launch-event.sh` — the TELEPHONY launcher.
//!
//! WHY THIS TEST EXISTS. `launch-webcast.sh` has had a posture test since W7;
//! the phone launcher never did, and every guard it grew was added after the
//! failure it prevents had already cost a live event:
//!
//!   - **ORT_DYLIB_PATH** (commit `0e0e2bd`, 2026-07-29): `build_pipeline_input`
//!     binds the WS listener BEFORE building the detector, so with the dylib
//!     unset the port is open while the detector build hangs. Everything
//!     downstream then lies — `nc -z` passes, ngrok reports connections, Twilio
//!     dials in, and the media socket sits unaccepted in the kernel backlog.
//!     Cost: the whole phone lane for a day, diagnosed as a signature bug.
//!   - **listener.ready** (same night): the launcher must refuse to dial when
//!     `arm` is not actually accepting. A dial into a pipeline that cannot
//!     accept is that same failure with a real call attached.
//!   - **operator double-gate**: an unmanned silent leg presented to a live
//!     operator is the 21-73 s drop class (Q-ADMIT Class B).
//!   - **`--tls-host` / `--stream-url`**: Twilio signs the EXACT stream URL
//!     (Q-M3), so two sources of truth for it is a 403 waiting to happen.
//!
//! Method mirrors `launch_webcast_posture.rs`: run the real script with `--bin`
//! pointed at a stub so nothing is compiled, `--no-dial` so no call is ever
//! placed, and a temp cwd because the script writes `state/<EVENT>/` relative
//! to it. Every case here fails BEFORE the dial by construction.

use std::path::{Path, PathBuf};
use std::process::Command;

fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

/// Runs the telephony launcher with `arm` stubbed. `ort` controls whether
/// ORT_DYLIB_PATH is present, since its absence is one of the cases under test.
fn run_launcher(extra: &[&str], ort: Option<&str>) -> (String, bool) {
    run_launcher_inner(extra, ort, false)
}

fn run_launcher_inner(extra: &[&str], ort: Option<&str>, stale_ready: bool) -> (String, bool) {
    let repo = repo();
    let script = repo.join("scripts/launch-event.sh");
    let config = repo.join("configs/twilio-aapl-26jul30-arm.toml");

    let cwd = std::env::temp_dir().join(format!(
        "earshot-launch-event-posture-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    std::fs::create_dir_all(&cwd).unwrap();
    // The launcher sources the account env and hard-requires Twilio creds
    // before it reaches any of the guards under test. These are syntactically
    // valid placeholders that address nothing: --no-dial means no request is
    // ever made with them, and every case here exits before the dial anyway.
    let account = cwd.join("acct.env");
    std::fs::write(
        &account,
        "TWILIO_ACCOUNT_SID=ACtest00000000000000000000000000\n\
         TWILIO_AUTH_TOKEN=testtoken0000000000000000000000\n\
         TWILIO_FROM_NUMBER=+15550000001\n",
    )
    .unwrap();

    if stale_ready {
        // Leftover token from a previous run of the same event, exactly as it
        // would survive on disk after a crash.
        let state = cwd.join("state/KXEARNINGSMENTIONAAPL-26JUL30");
        std::fs::create_dir_all(&state).unwrap();
        std::fs::write(
            state.join("listener.ready"),
            r#"{"port":8081,"pid":1,"unix_ts":1}"#,
        )
        .unwrap();
    }

    let mut args: Vec<&str> = vec![
        script.to_str().unwrap(),
        "--config",
        config.to_str().unwrap(),
        "--account",
        account.to_str().unwrap(),
        // `arm` stub: exits 0 immediately, so it never writes listener.ready.
        "--bin",
        "/bin/echo",
        // Required by the launcher's usage check. The number is a reserved
        // never-assigned 555 line and, with --no-dial below, is never dialed.
        "--bind-port",
        "8081",
        "--to",
        "+15550000000",
        // Belt and braces: no case here should reach the dial, and if one ever
        // regresses far enough to try, this keeps it from placing a real call.
        "--no-dial",
    ];
    args.extend_from_slice(extra);

    let mut cmd = Command::new("bash");
    cmd.args(&args).current_dir(&cwd);
    match ort {
        Some(p) => {
            cmd.env("ORT_DYLIB_PATH", p);
        }
        None => {
            cmd.env_remove("ORT_DYLIB_PATH");
            // The guard falls back to searching $EARSHOT_ROOT/vendor before it
            // refuses. Point that at an empty dir so the test does not silently
            // pass just because this machine happens to have a vendored dylib.
            cmd.env("EARSHOT_ROOT", cwd.to_str().unwrap());
        }
    }

    let out = cmd.output().expect("failed to run launch-event.sh");
    let mut s = String::from_utf8_lossy(&out.stdout).to_string();
    s.push_str(&String::from_utf8_lossy(&out.stderr));
    let _ = std::fs::remove_dir_all(&cwd);
    (s, out.status.success())
}

#[test]
fn refuses_streaming_ctc_without_ort_dylib() {
    let (out, ok) = run_launcher(&["--stream-url", "wss://example.invalid/e/8081"], None);
    assert!(
        !ok,
        "launcher must REFUSE streaming_ctc with no ORT_DYLIB_PATH — this is the \
         2026-07-29 silent-hang that bound the port and then never accepted:\n{out}"
    );
    assert!(
        out.contains("ORT_DYLIB_PATH"),
        "the refusal must name ORT_DYLIB_PATH so the operator can act on it:\n{out}"
    );
}

#[test]
fn accepts_streaming_ctc_when_ort_dylib_is_present() {
    // The guard must not be a blanket refusal: with the dylib set it announces
    // the resolved path and proceeds. (It then dies waiting on listener.ready,
    // which is the next test's subject.)
    let (out, _ok) = run_launcher(
        &["--stream-url", "wss://example.invalid/e/8081"],
        Some("/bin/echo"),
    );
    assert!(
        out.contains("streaming_ctc: ORT_DYLIB_PATH=/bin/echo"),
        "with the dylib present the launcher must proceed and echo it:\n{out}"
    );
}

#[test]
fn refuses_to_dial_when_arm_dies_before_listener_ready() {
    // `/bin/echo` exits instantly, so listener.ready is never written. The
    // launcher must notice the arm is gone and refuse — NOT fall through to a
    // dial, and NOT block for the full 120 s bind timeout.
    let (out, ok) = run_launcher(
        &["--stream-url", "wss://example.invalid/e/8081"],
        Some("/bin/echo"),
    );
    assert!(!ok, "launcher must fail when arm dies before readiness:\n{out}");
    assert!(
        out.contains("listener.ready"),
        "the failure must name the readiness token, not a generic error:\n{out}"
    );
    assert!(
        !out.contains("— dialing"),
        "the launcher must never reach the dial without a ready listener:\n{out}"
    );
}

#[test]
fn stale_listener_ready_token_does_not_authorize_a_dial() {
    // The launcher deletes the token before arming precisely because a
    // leftover from a previous run would satisfy the wait instantly and hand a
    // real phone call to a dead pipeline. With `arm` stubbed the token can only
    // reappear if the launcher trusted the stale one.
    let (out, ok) = run_launcher_inner(
        &["--stream-url", "wss://example.invalid/e/8081"],
        Some("/bin/echo"),
        true,
    );
    assert!(
        !ok,
        "a stale listener.ready must NOT let the launcher proceed:\n{out}"
    );
    assert!(
        !out.contains("— dialing"),
        "the launcher reached the dial on a stale readiness token — this is the \
         2026-07-29 failure with a real call attached:\n{out}"
    );
}

#[test]
fn tls_host_and_stream_url_are_mutually_exclusive() {
    // Twilio signs the exact stream URL (Q-M3). Two ways to specify it is two
    // ways to disagree, and the disagreement surfaces as a 403 at dial time.
    let (out, ok) = run_launcher(
        &[
            "--tls-host",
            "media.example.invalid",
            "--stream-url",
            "wss://other.invalid/e/8081",
        ],
        Some("/bin/echo"),
    );
    assert!(!ok, "launcher must refuse both ingress flags at once:\n{out}");
    assert!(
        out.contains("mutually exclusive"),
        "the refusal must say why:\n{out}"
    );
}

#[test]
fn operator_handler_requires_the_config_to_opt_in() {
    // Half one of the double gate (docs/OPERATOR-HANDLER.md §Registry
    // interaction). The AAPL config carries no [operator_handler] block, so
    // asking for the operator path against it must refuse — presenting an
    // unmanned silent leg to a live operator is the Class B drop class.
    let (out, ok) = run_launcher(
        &[
            "--operator-handler",
            "--stream-url",
            "wss://example.invalid/e/8081",
        ],
        Some("/bin/echo"),
    );
    assert!(
        !ok,
        "--operator-handler must refuse a config that has not opted in:\n{out}"
    );
    assert!(
        out.contains("operator_handler"),
        "the refusal must name the missing config block:\n{out}"
    );
}

// ── Gap A / B / C / Q (2026-08-04) ──────────────────────────────────────────
// The first-dial retry, the one-shot persistence refusal, and the no-stream
// watchdog. Tests 11-12 fail (or echo) before the dial, so the simple harness
// works with an injected config. Tests 13-16 need the launcher to REACH the
// dial, so they use a stub `arm` that writes listener.ready and stays alive,
// spawn the launcher, poll for the artifact, then kill it.

use std::process::Child;
use std::time::Duration;

fn aapl_config_src() -> String {
    std::fs::read_to_string(repo().join("configs/twilio-aapl-26jul30-arm.toml")).unwrap()
}

/// Run the launcher against a caller-supplied config body, dying at the
/// listener.ready wait (arm stubbed with /bin/echo). Returns (output, ok).
fn run_launcher_with_config(config_src: &str, extra: &[&str]) -> (String, bool) {
    let cwd = std::env::temp_dir().join(format!(
        "earshot-launch-cfg-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    std::fs::create_dir_all(&cwd).unwrap();
    let config = cwd.join("event.toml");
    std::fs::write(&config, config_src).unwrap();
    let account = cwd.join("acct.env");
    std::fs::write(
        &account,
        "TWILIO_ACCOUNT_SID=ACtest00000000000000000000000000\n\
         TWILIO_AUTH_TOKEN=testtoken0000000000000000000000\n\
         TWILIO_FROM_NUMBER=+15550000001\n",
    )
    .unwrap();
    let script = repo().join("scripts/launch-event.sh");
    let mut args: Vec<&str> = vec![
        script.to_str().unwrap(),
        "--config", config.to_str().unwrap(),
        "--account", account.to_str().unwrap(),
        "--bin", "/bin/echo",
        "--bind-port", "8081",
        "--to", "+15550000000",
        "--no-dial",
    ];
    args.extend_from_slice(extra);
    let out = Command::new("bash")
        .args(&args)
        .current_dir(&cwd)
        .env("ORT_DYLIB_PATH", "/bin/echo")
        .output()
        .expect("run launch-event.sh");
    let mut s = String::from_utf8_lossy(&out.stdout).to_string();
    s.push_str(&String::from_utf8_lossy(&out.stderr));
    let _ = std::fs::remove_dir_all(&cwd);
    (s, out.status.success())
}

#[test]
fn refuses_one_shot_config_without_explicit_flag() {
    // Gap B/C: max_reconnects unset/0 (the Rust default) means a first drop
    // ends the run. The launcher must refuse unless the one-shot is named.
    let stripped = aapl_config_src()
        .lines()
        .filter(|l| !l.trim_start().starts_with("max_reconnects"))
        .collect::<Vec<_>>()
        .join("\n");
    let (out, ok) = run_launcher_with_config(&stripped, &[]);
    assert!(!ok, "no-persistence config must be refused:\n{out}");
    assert!(
        out.contains("max_reconnects") && out.contains("--one-shot-ok"),
        "refusal must name the knob and the escape hatch:\n{out}"
    );
    // With the flag it proceeds (to the listener.ready failure, like the ORT
    // test above): a refusal that cannot be overridden is a footgun.
    let (out2, _ok2) = run_launcher_with_config(&stripped, &["--one-shot-ok"]);
    assert!(
        out2.contains("listener ready") || out2.contains("listener.ready"),
        "--one-shot-ok must let it proceed past the persistence gate:\n{out2}"
    );
}

#[test]
fn derived_budgets_echo_is_reachable_and_starts_at_driven() {
    // The 7942a41 clock-derivation was previously unreachable from a test.
    // A FUTURE call time derives real budgets; AAPL's past date falls through
    // to the supervisor defaults. The echo distinguishes the two.
    let future = aapl_config_src().replace(
        "starts_at = \"2026-07-30T21:00:00Z\"",
        "starts_at = \"2035-01-01T21:00:00Z\"",
    );
    let (out, _ok) = run_launcher_with_config(&future, &[]);
    assert!(
        out.contains("supervisor budgets:") && out.contains("--call-end-grace"),
        "a future starts_at must derive budgets:\n{out}"
    );
    let (out2, _ok2) = run_launcher_with_config(&aapl_config_src(), &[]);
    assert!(
        out2.contains("supervisor budgets: (defaults)"),
        "a past starts_at must fall through to defaults:\n{out2}"
    );
}

/// Spawn the launcher with a LIVE stub arm (writes listener.ready, sleeps) and
/// a recording dial stub, so the dial path is actually reached. Returns the
/// cwd, the state dir, and the child (killed on drop).
struct DialRig {
    state_dir: PathBuf,
    child: Child,
    cwd: PathBuf,
}
impl Drop for DialRig {
    fn drop(&mut self) {
        // The launcher backgrounds an arm stub, a supervisor, and a watchdog;
        // killing the launcher bash orphans them. STOP makes the supervisor
        // exit on its own; the arm/watchdog stubs are short sleeps.
        let _ = std::fs::write(self.state_dir.join("STOP"), "");
        let _ = self.child.kill();
        let _ = self.child.wait();
        std::thread::sleep(Duration::from_millis(300));
        let _ = std::fs::remove_dir_all(&self.cwd);
    }
}
impl DialRig {
    fn dials(&self) -> usize {
        std::fs::read_to_string(self.state_dir.join("dial-invocations.log"))
            .map(|s| s.lines().count())
            .unwrap_or(0)
    }
    fn pages(&self) -> String {
        std::fs::read_to_string(self.state_dir.join("pages.log")).unwrap_or_default()
    }
    fn redial_request(&self) -> String {
        std::fs::read_to_string(self.state_dir.join("redial.request")).unwrap_or_default()
    }
    fn await_dials(&self, n: usize, secs: u64) -> usize {
        for _ in 0..(secs * 10) {
            if self.dials() >= n { break; }
            std::thread::sleep(Duration::from_millis(100));
        }
        self.dials()
    }
    fn await_pages(&self, needle: &str, secs: u64) -> bool {
        for _ in 0..(secs * 10) {
            if self.pages().contains(needle) { return true; }
            std::thread::sleep(Duration::from_millis(100));
        }
        false
    }
    fn await_redial(&self, needle: &str, secs: u64) -> bool {
        for _ in 0..(secs * 10) {
            if self.redial_request().contains(needle) { return true; }
            std::thread::sleep(Duration::from_millis(100));
        }
        false
    }
}

fn write_exec(path: &Path, body: &str) {
    std::fs::write(path, format!("#!/bin/bash\n{body}")).unwrap();
    use std::os::unix::fs::PermissionsExt;
    let mut p = std::fs::metadata(path).unwrap().permissions();
    p.set_mode(0o755);
    std::fs::set_permissions(path, p).unwrap();
}

/// dial_plan: exit code per successive dial. one_shot true adds --one-shot-ok
/// (no supervisor); false keeps the AAPL max_reconnects=2 supervisor running.
fn spawn_dialing_launcher(dial_plan: &[i32], one_shot: bool, extra: &[&str]) -> DialRig {
    let cwd = std::env::temp_dir().join(format!(
        "earshot-dialrig-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    std::fs::create_dir_all(&cwd).unwrap();
    let event = "KXEARNINGSMENTIONAAPL-26JUL30";
    let state_dir = cwd.join("state").join(event);
    std::fs::create_dir_all(&state_dir).unwrap();
    let config = cwd.join("event.toml");
    std::fs::write(&config, aapl_config_src()).unwrap();
    let account = cwd.join("acct.env");
    std::fs::write(
        &account,
        "TWILIO_ACCOUNT_SID=ACtest00000000000000000000000000\n\
         TWILIO_AUTH_TOKEN=testtoken0000000000000000000000\n\
         TWILIO_FROM_NUMBER=+15550000001\n",
    )
    .unwrap();

    // Stub arm: write listener.ready into --state-dir, then stay alive so the
    // launcher proceeds to (and blocks after) the dial.
    let arm = cwd.join("arm-stub.sh");
    write_exec(
        &arm,
        r#"sd=""
while [ $# -gt 0 ]; do case "$1" in --state-dir) sd="$2"; shift 2;; *) shift;; esac; done
echo "{\"port\":8081,\"pid\":$$,\"unix_ts\":1}" > "$sd/listener.ready"
sleep 30
"#,
    );
    // Dial stub: record, take Nth exit from the plan, write sid on success.
    let dial = cwd.join("dial-stub.sh");
    write_exec(
        &dial,
        &format!(
            r#"DIR="{}"
echo "$*" >> "$DIR/dial-invocations.log"
N=$(wc -l < "$DIR/dial-invocations.log" | tr -d ' ')
CODE=$(sed -n "${{N}}p" "$DIR/dial-exit-plan" 2>/dev/null); CODE=${{CODE:-0}}
if [ "$CODE" = "0" ]; then prev=""; for a in "$@"; do if [ "$prev" = "--sid-file" ]; then echo "CAdial$N" > "$a"; fi; prev="$a"; done; fi
exit "$CODE"
"#,
            state_dir.display()
        ),
    );
    let hangup = cwd.join("hangup-stub.sh");
    write_exec(&hangup, "echo \"$1\" >> \"$(dirname \"$0\")/hangup.log\"; exit 0\n");
    let notify = cwd.join("notify-stub.sh");
    write_exec(
        &notify,
        &format!("echo \"$1|$2\" >> \"{}/pages.log\"; exit 0\n", state_dir.display()),
    );
    let plan: String = dial_plan.iter().map(|c| format!("{c}\n")).collect();
    std::fs::write(state_dir.join("dial-exit-plan"), plan).unwrap();

    let script = repo().join("scripts/launch-event.sh");
    let mut args: Vec<String> = vec![
        script.to_string_lossy().into(),
        "--config".into(), config.to_string_lossy().into(),
        "--account".into(), account.to_string_lossy().into(),
        "--bin".into(), arm.to_string_lossy().into(),
        "--bind-port".into(), "8081".into(),
        "--to".into(), "+15550000000".into(),
        "--stream-url".into(), "wss://example.invalid/e/8081".into(),
        "--dial-script".into(), dial.to_string_lossy().into(),
        "--hangup-script".into(), hangup.to_string_lossy().into(),
    ];
    if one_shot {
        args.push("--one-shot-ok".into());
    }
    for e in extra {
        args.push((*e).into());
    }
    let child = Command::new("bash")
        .args(&args)
        .current_dir(&cwd)
        .env("ORT_DYLIB_PATH", "/bin/echo")
        .env("EARSHOT_NOTIFY", &notify)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .expect("spawn launch-event.sh");
    DialRig { state_dir, child, cwd }
}

#[test]
fn first_dial_retries_transient_failure_then_succeeds() {
    // Gap A: a transient 5xx at T-0 (exit 5) used to lose the event. The
    // launcher must retry and land the call.
    let rig = spawn_dialing_launcher(&[5, 0], true, &["--stream-confirm-secs", "120"]);
    assert_eq!(rig.await_dials(2, 8), 2, "transient failure must be retried");
}

#[test]
fn permanent_dial_rejection_does_not_retry_and_pages() {
    // Exit 4 (HTTP 4xx: bad number/auth) is permanent. Exactly one attempt,
    // and the operator is paged with a manual-dial command (the arm stays
    // warm — a manual dial can still save the event).
    let rig = spawn_dialing_launcher(&[4], true, &["--stream-confirm-secs", "120"]);
    assert_eq!(rig.await_dials(1, 8), 1, "the permanent-reject dial must happen once");
    assert!(
        rig.await_pages("FIRST DIAL FAILED", 4),
        "permanent rejection must page:\n{}",
        rig.pages()
    );
    // No retry: give a retry every chance to appear, then confirm it did not.
    std::thread::sleep(Duration::from_secs(2));
    assert_eq!(rig.dials(), 1, "permanent rejection must not retry");
}

#[test]
fn ambiguous_dial_failure_pages_and_never_blind_retries() {
    // Exit 6 (transport failed, recovery lookup inconclusive): a blind repeat
    // risks two legs in the bridge. One attempt, and a DIAL AMBIGUOUS page.
    let rig = spawn_dialing_launcher(&[6], true, &["--stream-confirm-secs", "120"]);
    assert_eq!(rig.await_dials(1, 8), 1, "the ambiguous dial must happen once");
    assert!(
        rig.await_pages("DIAL AMBIGUOUS", 4),
        "ambiguous failure must page:\n{}",
        rig.pages()
    );
    std::thread::sleep(Duration::from_secs(2));
    assert_eq!(rig.dials(), 1, "ambiguous failure must not blind-retry");
}

#[test]
fn no_stream_watchdog_writes_synthetic_marker_and_pages() {
    // Gap Q: the dial succeeds but no stream.connected ever appears (the stub
    // arm never writes it). The watchdog must page and, with a supervisor
    // running, write a synthetic no_stream redial request.
    let rig = spawn_dialing_launcher(&[0], false, &["--stream-confirm-secs", "1"]);
    assert_eq!(rig.await_dials(1, 8), 1, "the first dial must land");
    assert!(
        rig.await_pages("NO STREAM", 6),
        "the watchdog must page on no stream:\n{}",
        rig.pages()
    );
    assert!(
        rig.await_redial("no_stream", 6),
        "the watchdog must write a synthetic no_stream marker:\n{}",
        rig.redial_request()
    );
}
