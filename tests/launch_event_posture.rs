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
    cmd.args(&args)
        .current_dir(&cwd)
        .env("EARSHOT_TEST_DISABLE_TEE", "1");
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
    assert!(
        !ok,
        "launcher must fail when arm dies before readiness:\n{out}"
    );
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
    assert!(
        !ok,
        "launcher must refuse both ingress flags at once:\n{out}"
    );
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
        "--config",
        config.to_str().unwrap(),
        "--account",
        account.to_str().unwrap(),
        "--bin",
        "/bin/echo",
        "--bind-port",
        "8081",
        "--to",
        "+15550000000",
        "--stream-url",
        "wss://example.invalid/e/8081",
        "--no-dial",
    ];
    args.extend_from_slice(extra);
    let out = Command::new("bash")
        .args(&args)
        .current_dir(&cwd)
        .env("ORT_DYLIB_PATH", "/bin/echo")
        .env("EARSHOT_TEST_DISABLE_TEE", "1")
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
            if self.dials() >= n {
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        self.dials()
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
    fn await_redial(&self, needle: &str, secs: u64) -> bool {
        for _ in 0..(secs * 10) {
            if self.redial_request().contains(needle) {
                return true;
            }
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
    write_exec(
        &hangup,
        "echo \"$1\" >> \"$(dirname \"$0\")/hangup.log\"; exit 0\n",
    );
    let notify = cwd.join("notify-stub.sh");
    write_exec(
        &notify,
        &format!(
            "echo \"$1|$2\" >> \"{}/pages.log\"; exit 0\n",
            state_dir.display()
        ),
    );
    let plan: String = dial_plan.iter().map(|c| format!("{c}\n")).collect();
    std::fs::write(state_dir.join("dial-exit-plan"), plan).unwrap();

    let script = repo().join("scripts/launch-event.sh");
    let mut args: Vec<String> = vec![
        script.to_string_lossy().into(),
        "--config".into(),
        config.to_string_lossy().into(),
        "--account".into(),
        account.to_string_lossy().into(),
        "--bin".into(),
        arm.to_string_lossy().into(),
        "--bind-port".into(),
        "8081".into(),
        "--to".into(),
        "+15550000000".into(),
        "--stream-url".into(),
        "wss://example.invalid/e/8081".into(),
        "--dial-script".into(),
        dial.to_string_lossy().into(),
        "--hangup-script".into(),
        hangup.to_string_lossy().into(),
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
        .env("EARSHOT_TEST_DISABLE_TEE", "1")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .expect("spawn launch-event.sh");
    DialRig {
        state_dir,
        child,
        cwd,
    }
}

#[test]
fn first_dial_retries_transient_failure_then_succeeds() {
    // Gap A: a transient 5xx at T-0 (exit 5) used to lose the event. The
    // launcher must retry and land the call.
    let rig = spawn_dialing_launcher(&[5, 0], true, &["--stream-confirm-secs", "120"]);
    assert_eq!(
        rig.await_dials(2, 8),
        2,
        "transient failure must be retried"
    );
}

#[test]
fn permanent_dial_rejection_does_not_retry_and_pages() {
    // Exit 4 (HTTP 4xx: bad number/auth) is permanent. Exactly one attempt,
    // and the operator is paged with a manual-dial command (the arm stays
    // warm — a manual dial can still save the event).
    let rig = spawn_dialing_launcher(&[4], true, &["--stream-confirm-secs", "120"]);
    assert_eq!(
        rig.await_dials(1, 8),
        1,
        "the permanent-reject dial must happen once"
    );
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
    assert_eq!(
        rig.await_dials(1, 8),
        1,
        "the ambiguous dial must happen once"
    );
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

// ── Immutable RunPlan activation (2026-08-12) ─────────────────────────────

const RUNPLAN_EVENT: &str = "KXEARNINGSMENTIONRUNPLAN-26AUG12";
const RUNPLAN_HOST: &str = "phone-box-test";
const RUNPLAN_FINGERPRINT: &str =
    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

struct RunPlanRig {
    root: PathBuf,
    run_dir: PathBuf,
    config: PathBuf,
    account: PathBuf,
    arm: PathBuf,
    dial: PathBuf,
    hangup: PathBuf,
    notify: PathBuf,
    alert: PathBuf,
    capture: PathBuf,
}

impl Drop for RunPlanRig {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

impl RunPlanRig {
    fn eventctl(&self, args: &[&str]) -> std::process::Output {
        Command::new("python3")
            .arg(repo().join("scripts/eventctl.py"))
            .args(args)
            .current_dir(&self.root)
            .env("EARSHOT_REPO", &self.root)
            .output()
            .expect("run eventctl")
    }

    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "earshot-launch-runplan-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("configs")).unwrap();
        std::fs::create_dir_all(root.join("state")).unwrap();
        std::fs::write(
            root.join("configs/dial-ins.toml"),
            "[RUNPLAN]\nadmission = \"untested\"\nadmission_probed = \"never\"\n",
        )
        .unwrap();
        let config = root.join("configs/event.toml");
        std::fs::write(
            &config,
            format!(
                r#"[event]
id = "{RUNPLAN_EVENT}"
starts_at = "2026-08-12T21:00:00Z"

[[sources]]
kind = "twilio_stream"
bind = "127.0.0.1:8081"
max_reconnects = 0
idle_timeout_secs = 8

[[keywords]]
market_ticker = "KXEARNINGSMENTIONRUNPLAN-26AUG12-TEST"
phrase = "test"
"#,
            ),
        )
        .unwrap();
        let account = root.join("account.env");
        std::fs::write(
            &account,
            format!(
                "export TWILIO_ACCOUNT_SID=ACtest00000000000000000000000000\n\
                 export TWILIO_AUTH_TOKEN=testtoken0000000000000000000000\n\
                 export TWILIO_FROM_NUMBER=+15550000001\n\
                 export EARSHOT_EXECUTION_HOST={RUNPLAN_HOST}\n\
                 export EARSHOT_KALSHI_ACCOUNT_FINGERPRINT={RUNPLAN_FINGERPRINT}\n"
            ),
        )
        .unwrap();
        let run_dir = root.join("var/runs").join(RUNPLAN_EVENT).join("run-test");
        let arm = root.join("arm-stub.sh");
        let dial = root.join("dial-stub.sh");
        let hangup = root.join("hangup-stub.sh");
        let notify = root.join("notify-stub.sh");
        let alert = root.join("alert-stub.sh");
        let capture = root.join("runtime-env.txt");
        write_exec(
            &arm,
            r#"sd=""; report=""
while [ $# -gt 0 ]; do
  case "$1" in
    --state-dir) sd="$2"; shift 2;;
    --report) report="$2"; shift 2;;
    *) shift;;
  esac
done
printf '%s|%s|%s|%s|%s|%s\n' "$EARSHOT_RUN_PLAN" "$EARSHOT_EXECUTION_HOST" "$EARSHOT_KALSHI_ACCOUNT_FINGERPRINT" "$EARSHOT_CANONICAL_STATE_ROOT" "$sd" "$report" > "$RUNPLAN_CAPTURE"
mkdir -p "$sd" "$(dirname "$report")"
printf '{"port":8081,"pid":%s,"unix_ts":1}\n' "$$" > "$sd/listener.ready"
write_source_marker() {
  python3 - "$EARSHOT_RUN_PLAN" "$sd/source.attached" "$$" <<'PY'
import json, sys, time
plan = json.load(open(sys.argv[1], encoding="utf-8"))
json.dump({"schema_version": 1, "run_id": plan["run_id"],
           "event_id": plan["event_id"], "plan_sha256": plan["plan_sha256"],
           "pid": int(sys.argv[3]), "unix_ns": time.time_ns(), "accepted_frames": 1},
          open(sys.argv[2], "w", encoding="utf-8"))
PY
}
write_admission_challenge() {
  python3 - "$EARSHOT_RUN_PLAN" "$sd" "$$" <<'PY'
import json, os, sys
plan = json.load(open(sys.argv[1], encoding="utf-8"))
state = os.path.abspath(sys.argv[2])
path = os.path.join(state, "admission.challenge.json")
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump({"schema_version": 1, "event_id": plan["event_id"],
               "nonce": "a" * 64, "pid": int(sys.argv[3]),
               "receipt_path": os.path.join(state, "admission.receipt.json"),
               "stream_connected_path": os.path.join(state, "stream.connected")}, fh)
os.replace(tmp, path)
PY
}
wait_for_receipt() {
  expected_sid="$1"; expected_attempt="$2"
  for _ in $(seq 1 240); do
    if python3 - "$sd/admission.receipt.json" "$expected_sid" "$expected_attempt" <<'PY' >/dev/null 2>&1
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if value.get("call_sid") == sys.argv[2]
                 and value.get("stream_attempt") == int(sys.argv[3]) else 1)
PY
    then
      return 0
    fi
    sleep 0.05
  done
  return 1
}
write_good_report() {
  python3 - "$EARSHOT_RUN_PLAN" "$report" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1], encoding="utf-8"))
json.dump({"run_id": plan["run_id"], "plan_sha256": plan["plan_sha256"],
           "finalized": True, "runtime_success": True,
           "required_evidence_healthy": True,
           "unresolved_cancellation_ids": [], "audio_evidence_healthy": True,
           "book_tape_healthy": True, "order_evidence_healthy": True,
           "fix_observation_tape_healthy": None},
          open(sys.argv[2], "w", encoding="utf-8"))
PY
}
case "${ARM_STUB_MODE:-good}" in
  good) write_source_marker; write_good_report;;
  bad-report) write_source_marker; printf '{"finalized":false}\n' > "$report";;
  unresolved)
    write_source_marker
    write_good_report
    python3 - "$report" <<'PY'
import json, sys
path = sys.argv[1]
value = json.load(open(path, encoding="utf-8"))
value["unresolved_cancellation_ids"] = ["client-order-unresolved"]
json.dump(value, open(path, "w", encoding="utf-8"))
PY
    ;;
  no-attach) write_good_report;;
  wrong-marker)
    write_source_marker
    python3 - "$sd/source.attached" <<'PY'
import json, sys
path = sys.argv[1]
value = json.load(open(path, encoding="utf-8"))
value["run_id"] = "different-run"
json.dump(value, open(path, "w", encoding="utf-8"))
PY
    write_good_report
    ;;
  symlink-report)
    write_source_marker
    write_good_report
    mv "$report" "$report.real"
    ln -s "$(basename "$report.real")" "$report"
    ;;
  redial-admission)
    write_admission_challenge
    write_source_marker
    wait_for_receipt CA11111111111111111111111111111111 0 || exit 9
    python3 - "$sd/stream.connected" "$sd/call.sid" <<'PY'
import json, os, sys, time
stream, sid_file = sys.argv[1:]
sid = "CA22222222222222222222222222222222"
tmp = stream + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump({"unix_ts": int(time.time()), "call_sid": sid, "attempt": 1}, fh)
os.replace(tmp, stream)
with open(sid_file, "w", encoding="utf-8") as fh:
    fh.write(sid + "\n")
PY
    wait_for_receipt CA22222222222222222222222222222222 1 || exit 9
    write_good_report
    ;;
  wait-ineligible)
    write_source_marker
    for _ in $(seq 1 400); do
      grep -q '^rc=3$' "$sd/admission-watcher.last" 2>/dev/null && break
      sleep 0.05
    done
    grep -q '^rc=3$' "$sd/admission-watcher.last" 2>/dev/null || exit 10
    write_good_report
    ;;
  wait-stop)
    write_source_marker
    while [ ! -f "$sd/STOP" ]; do sleep 0.05; done
    sleep 1
    write_good_report
    printf 'finalized\n' > "$sd/arm-finalized"
    ;;
esac
"#,
        );
        write_exec(
            &dial,
            r#"sid_file=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--sid-file" ]; then sid_file="$arg"; fi
  prev="$arg"
done
[ -n "$sid_file" ] || exit 4
sid="CA11111111111111111111111111111111"
printf '%s\n' "$sid" > "$sid_file"
printf '{"unix_ts":%s,"call_sid":"%s","attempt":0}\n' "$(date +%s)" "$sid" > "$(dirname "$sid_file")/stream.connected"
exit 0
"#,
        );
        write_exec(
            &hangup,
            r#"printf '%s\n' "$1" > "$(dirname "$0")/hung-up"
exit 0
"#,
        );
        write_exec(
            &notify,
            r#"printf '%s|%s\n' "$1" "$2" >> "$RUNPLAN_PAGES"
exit 0
"#,
        );
        write_exec(
            &alert,
            r#"printf '%s\n' "$*" >> "$RUNPLAN_ALERTS"
exit 0
"#,
        );

        let fixture = Self {
            root,
            run_dir,
            config,
            account,
            arm,
            dial,
            hangup,
            notify,
            alert,
            capture,
        };
        let init = fixture.eventctl(&[
            "init",
            "--config",
            "configs/event.toml",
            "--event",
            RUNPLAN_EVENT,
            "--host-id",
            RUNPLAN_HOST,
            "--account-fingerprint",
            RUNPLAN_FINGERPRINT,
            "--run-id",
            "run-test",
        ]);
        assert!(
            init.status.success(),
            "eventctl init failed: {}{}",
            String::from_utf8_lossy(&init.stdout),
            String::from_utf8_lossy(&init.stderr)
        );
        for transition in [
            "prepared",
            "admission_resolved",
            "study_completed",
            "preflight_passed",
            "scheduled",
            "capture_ready",
        ] {
            let out = fixture.eventctl(&[
                "record",
                "--run-dir",
                fixture.run_dir.to_str().unwrap(),
                "--transition",
                transition,
                "--actor",
                "launch-test",
            ]);
            assert!(
                out.status.success(),
                "record {transition} failed: {}{}",
                String::from_utf8_lossy(&out.stdout),
                String::from_utf8_lossy(&out.stderr)
            );
        }
        fixture
    }

    fn command(&self, mode: &str, host_override: Option<&str>, extra: &[&str]) -> Command {
        let script = repo().join("scripts/launch-event.sh");
        let mut command = Command::new("bash");
        command
            .arg(script)
            .args(["--config", self.config.to_str().unwrap()])
            .args(["--account", self.account.to_str().unwrap()])
            .args(["--bin", self.arm.to_str().unwrap()])
            .args(["--bind-port", "8081"])
            .args(["--to", "+15550000000"])
            .args(["--stream-url", "wss://example.invalid/e/8081"])
            .args(["--dial-script", self.dial.to_str().unwrap()])
            .args(["--hangup-script", self.hangup.to_str().unwrap()])
            .args(["--dial-attempts", "1"])
            .args(["--stream-confirm-secs", "3"])
            .args(["--shutdown-grace-secs", "75"])
            .arg("--one-shot-ok")
            .arg("--i-am-live")
            .args(extra)
            .current_dir(&self.root)
            .env("EARSHOT_REPO", &self.root)
            .env("EARSHOT_EVENTCTL", repo().join("scripts/eventctl.py"))
            .env("EARSHOT_NOTIFY", &self.notify)
            .env("EARSHOT_ALERTCTL", &self.alert)
            .env("RUNPLAN_CAPTURE", &self.capture)
            .env("RUNPLAN_PAGES", self.root.join("pages.log"))
            .env("RUNPLAN_ALERTS", self.root.join("alerts.log"))
            .env("ARM_STUB_MODE", mode)
            .env("EARSHOT_TEST_DISABLE_TEE", "1");
        if let Some(host) = host_override {
            // Environment inherited by launch-event.sh takes precedence until
            // the account file is sourced. Use a one-line alternate account
            // for the negative identity test instead.
            let bad = self.root.join("bad-account.env");
            std::fs::write(
                &bad,
                format!(
                    "export TWILIO_ACCOUNT_SID=ACtest00000000000000000000000000\n\
                     export TWILIO_AUTH_TOKEN=testtoken0000000000000000000000\n\
                     export TWILIO_FROM_NUMBER=+15550000001\n\
                     export EARSHOT_EXECUTION_HOST={host}\n\
                     export EARSHOT_KALSHI_ACCOUNT_FINGERPRINT={RUNPLAN_FINGERPRINT}\n"
                ),
            )
            .unwrap();
            command.args(["--account", bad.to_str().unwrap()]);
        }
        command
    }

    fn launch(&self, mode: &str, host_override: Option<&str>) -> std::process::Output {
        self.command(mode, host_override, &[])
            .output()
            .expect("run launch-event with RunPlan")
    }

    fn status(&self) -> serde_json::Value {
        let out = self.eventctl(&[
            "status",
            "--run-dir",
            self.run_dir.to_str().unwrap(),
            "--json",
        ]);
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        serde_json::from_slice(&out.stdout).unwrap()
    }

    fn set_admission(&self, admission: &str, probed: &str) {
        std::fs::write(
            self.root.join("configs/dial-ins.toml"),
            format!("[RUNPLAN]\nadmission = {admission:?}\nadmission_probed = {probed:?}\n"),
        )
        .unwrap();
    }

    fn set_fresh_held(&self) {
        let out = Command::new("date").args(["-u", "+%F"]).output().unwrap();
        assert!(out.status.success());
        self.set_admission("held", String::from_utf8(out.stdout).unwrap().trim());
    }

    fn state_dir(&self) -> PathBuf {
        self.root.join("state").join(RUNPLAN_EVENT)
    }

    fn pages(&self) -> String {
        std::fs::read_to_string(self.root.join("pages.log")).unwrap_or_default()
    }

    fn alerts(&self) -> String {
        std::fs::read_to_string(self.root.join("alerts.log")).unwrap_or_default()
    }
}

fn runplan_reached(status: &serde_json::Value, transition: &str) -> bool {
    status["state"]["reached"]
        .as_array()
        .unwrap()
        .iter()
        .any(|receipt| receipt["transition"] == transition)
}

#[test]
fn live_phone_launch_binds_runplan_report_and_records_runtime_receipts() {
    let rig = RunPlanRig::new();
    let out = rig.launch("good", None);
    assert!(
        out.status.success(),
        "RunPlan-bound launcher failed: {}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    let captured = std::fs::read_to_string(&rig.capture).unwrap();
    let fields: Vec<&str> = captured.trim().split('|').collect();
    assert_eq!(fields.len(), 6, "unexpected runtime capture: {captured}");
    let canonical_root = std::fs::canonicalize(&rig.root).unwrap();
    let canonical_run = std::fs::canonicalize(&rig.run_dir).unwrap();
    assert_eq!(
        fields[0],
        canonical_run
            .join("control/run-plan.json")
            .to_str()
            .unwrap()
    );
    assert_eq!(fields[1], RUNPLAN_HOST);
    assert_eq!(fields[2], RUNPLAN_FINGERPRINT);
    assert_eq!(fields[3], canonical_root.join("state").to_str().unwrap());
    assert_eq!(
        fields[4],
        canonical_root
            .join("state")
            .join(RUNPLAN_EVENT)
            .to_str()
            .unwrap()
    );
    assert_eq!(
        fields[5],
        canonical_run.join("report/report.json").to_str().unwrap()
    );

    let status = rig.status();
    for wanted in [
        "launched",
        "source_attached",
        "supervising",
        "draining",
        "completed",
    ] {
        assert!(
            runplan_reached(&status, wanted),
            "missing {wanted} receipt: {status}"
        );
    }
    assert!(!runplan_reached(&status, "failed"), "{status}");
}

#[test]
fn fresh_held_admission_opens_after_source_receipt_and_renews_on_redial() {
    let rig = RunPlanRig::new();
    rig.set_fresh_held();
    let out = rig.launch("redial-admission", None);
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(out.status.success(), "held launch failed: {combined}");
    assert!(
        combined.contains("current/open identity=CA11111111111111111111111111111111|0")
            && combined.contains("current/open identity=CA22222222222222222222222222222222|1"),
        "watcher did not consume both stream identities: {combined}"
    );
    assert_eq!(
        combined
            .matches("admission-watcher: current/open identity=")
            .count(),
        2,
        "watcher must invoke once per CallSid/attempt: {combined}"
    );

    let state = rig.state_dir();
    let receipt: serde_json::Value =
        serde_json::from_slice(&std::fs::read(state.join("admission.receipt.json")).unwrap())
            .unwrap();
    assert_eq!(receipt["call_sid"], "CA22222222222222222222222222222222");
    assert_eq!(receipt["stream_attempt"], 1);
    assert_eq!(receipt["assertion_method"], "probe_held_current_run");
    assert_ne!(receipt["assertion_method"], "explicit_operator_pass_cli");

    let audit = std::fs::read_to_string(state.join("admission.assertions.jsonl")).unwrap();
    let rows: Vec<serde_json::Value> = audit
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    let held: Vec<&serde_json::Value> = rows
        .iter()
        .filter(|row| row["kind"] == "held_admission_assertion")
        .collect();
    assert_eq!(held.len(), 2, "one assertion required per stream: {rows:?}");
    assert!(
        !rows
            .iter()
            .any(|row| row["kind"] == "operator_admission_assertion"),
        "watcher must never manufacture an operator assertion: {rows:?}"
    );
    assert_eq!(held[0]["stream_attempt"], 0);
    assert_eq!(held[1]["stream_attempt"], 1);

    let lifecycle = std::fs::read_to_string(rig.run_dir.join("control/receipts.jsonl"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap())
        .collect::<Vec<_>>();
    let source_hash = lifecycle
        .iter()
        .find(|row| row["transition"] == "source_attached")
        .unwrap()["receipt_sha256"]
        .clone();
    assert!(
        source_hash.is_string(),
        "missing durable source receipt: {lifecycle:?}"
    );
    assert!(
        held.iter()
            .all(|row| row["source_attached_receipt_sha256"] == source_hash),
        "held assertions were not bound to the accepted source receipt: {held:?}"
    );
    let status = rig.status();
    assert!(runplan_reached(&status, "completed"), "{status}");
    assert!(!runplan_reached(&status, "failed"), "{status}");
    assert!(
        !state.join("STOP").exists(),
        "healthy held flow asserted STOP"
    );
    assert!(
        rig.pages().is_empty(),
        "healthy held flow paged: {}",
        rig.pages()
    );

    let watcher_pid = std::fs::read_to_string(state.join("admission-watcher.pid")).unwrap();
    let alive = Command::new("kill")
        .args(["-0", watcher_pid.trim()])
        .status()
        .unwrap()
        .success();
    assert!(
        !alive,
        "admission watcher was not reaped: pid {watcher_pid}"
    );
}

#[test]
fn ineligible_admission_is_expected_closed_without_page_or_stop() {
    let rig = RunPlanRig::new();
    let out = rig.launch("wait-ineligible", None);
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(out.status.success(), "ineligible launch failed: {combined}");
    assert!(
        combined.contains("expected closed/ineligible")
            && combined.contains("INELIGIBLE held admission receipt"),
        "exit-3 posture was not observable: {combined}"
    );
    let state = rig.state_dir();
    assert!(
        !state.join("admission.receipt.json").exists(),
        "ineligible registry opened the gate"
    );
    assert!(
        !state.join("STOP").exists(),
        "expected ineligibility asserted STOP"
    );
    assert!(
        rig.pages().is_empty(),
        "expected ineligibility paged: {}",
        rig.pages()
    );
    let status = rig.status();
    assert!(runplan_reached(&status, "completed"), "{status}");
    assert!(!runplan_reached(&status, "failed"), "{status}");
}

#[test]
fn held_admission_integrity_fault_pages_stops_and_fails_run() {
    let rig = RunPlanRig::new();
    rig.set_fresh_held();
    // wait-stop publishes a valid source marker but deliberately omits the
    // runtime-owned admission challenge. Fresh held must therefore fail loud,
    // never silently degrade to a closed-but-healthy run.
    let out = rig.launch("wait-stop", None);
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(!out.status.success(), "integrity fault passed: {combined}");
    assert!(
        combined.contains("ADMISSION WATCHER FAILURE") && combined.contains("admission challenge"),
        "missing fail-loud detail: {combined}"
    );
    let state = rig.state_dir();
    assert!(
        state.join("STOP").exists(),
        "integrity fault did not assert STOP"
    );
    assert!(
        state.join("admission-watcher.failed").exists(),
        "integrity fault marker missing"
    );
    assert!(
        rig.pages().contains("ADMISSION RECEIPT FAILED"),
        "integrity fault did not page: {}",
        rig.pages()
    );
    assert!(
        !state.join("admission.receipt.json").exists(),
        "integrity fault published a receipt"
    );
    let status = rig.status();
    assert!(runplan_reached(&status, "failed"), "{status}");
    assert!(!runplan_reached(&status, "completed"), "{status}");

    let watcher_pid = std::fs::read_to_string(state.join("admission-watcher.pid")).unwrap();
    let alive = Command::new("kill")
        .args(["-0", watcher_pid.trim()])
        .status()
        .unwrap()
        .success();
    assert!(
        !alive,
        "failed admission watcher was not reaped: pid {watcher_pid}"
    );
}

#[test]
fn live_phone_launch_refuses_host_identity_mismatch_before_arm() {
    let rig = RunPlanRig::new();
    let out = rig.launch("good", Some("wrong-phone-box"));
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        !out.status.success(),
        "host mismatch must refuse: {combined}"
    );
    assert!(combined.contains("execution host") && combined.contains("RunPlan"));
    assert!(
        !rig.capture.exists(),
        "arm stub ran despite RunPlan host mismatch"
    );
}

#[test]
fn live_phone_launch_refuses_duplicate_launched_run_before_arm() {
    let rig = RunPlanRig::new();
    let recorded = rig.eventctl(&[
        "record",
        "--run-dir",
        rig.run_dir.to_str().unwrap(),
        "--transition",
        "launched",
        "--actor",
        "prior-launch",
    ]);
    assert!(recorded.status.success());
    let out = rig.launch("good", None);
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        !out.status.success(),
        "duplicate activation must refuse: {combined}"
    );
    assert!(combined.contains("duplicate activation") || combined.contains("pre-launch gate"));
    assert!(
        !rig.capture.exists(),
        "arm stub ran for an already-launched RunPlan"
    );
}

#[test]
fn unhealthy_runtime_report_records_failed_never_completed() {
    let rig = RunPlanRig::new();
    let out = rig.launch("bad-report", None);
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        !out.status.success(),
        "invalid report must fail launch: {combined}"
    );
    assert!(combined.contains("completion predicate"), "{combined}");
    let status = rig.status();
    assert!(runplan_reached(&status, "failed"), "{status}");
    assert!(!runplan_reached(&status, "completed"), "{status}");
    let alerts = rig.alerts();
    assert!(
        alerts.contains("raise")
            && alerts.contains("runtime_failed")
            && alerts.contains("lifecycle_terminal"),
        "terminal runtime failure did not cross the proof-alert boundary: {alerts}"
    );
}

#[test]
fn unresolved_cancellation_ids_never_complete_phone_run() {
    let rig = RunPlanRig::new();
    let out = rig.launch("unresolved", None);
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        !out.status.success(),
        "unresolved cancellation passed: {combined}"
    );
    assert!(
        combined.contains("unresolved_cancellation_ids"),
        "{combined}"
    );
    let status = rig.status();
    assert!(runplan_reached(&status, "failed"), "{status}");
    assert!(!runplan_reached(&status, "completed"), "{status}");
}

#[test]
fn stream_connected_without_runtime_source_marker_goes_straight_to_failed() {
    let rig = RunPlanRig::new();
    let out = rig.launch("no-attach", None);
    assert!(
        !out.status.success(),
        "stream.connected alone advanced lifecycle: {}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    let status = rig.status();
    assert!(runplan_reached(&status, "launched"), "{status}");
    assert!(runplan_reached(&status, "failed"), "{status}");
    for forbidden in ["source_attached", "supervising", "draining", "completed"] {
        assert!(
            !runplan_reached(&status, forbidden),
            "stream.connected manufactured {forbidden}: {status}"
        );
    }
}

#[test]
fn mismatched_runtime_source_marker_cannot_advance_phone_supervision() {
    let rig = RunPlanRig::new();
    let out = rig.launch("wrong-marker", None);
    assert!(!out.status.success());
    let status = rig.status();
    assert!(runplan_reached(&status, "launched"), "{status}");
    assert!(!runplan_reached(&status, "source_attached"), "{status}");
    assert!(!runplan_reached(&status, "supervising"), "{status}");
    assert!(runplan_reached(&status, "failed"), "{status}");
}

#[test]
fn symlink_runtime_report_is_not_completion_evidence() {
    let rig = RunPlanRig::new();
    let out = rig.launch("symlink-report", None);
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        !out.status.success(),
        "symlink report completed: {combined}"
    );
    assert!(combined.contains("regular non-symlink"), "{combined}");
    let status = rig.status();
    assert!(runplan_reached(&status, "failed"), "{status}");
    assert!(!runplan_reached(&status, "completed"), "{status}");
}

#[test]
fn live_phone_term_completes_only_after_stop_hangup_and_runtime_finalizers() {
    let rig = RunPlanRig::new();
    let mut child = rig
        .command("wait-stop", None, &[])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    let state = rig.root.join("state").join(RUNPLAN_EVENT);
    // Several RunPlan fixtures execute eventctl concurrently in this test
    // binary; leave enough wall-clock headroom for its durable fsync receipts.
    for _ in 0..800 {
        if state.join("call.sid").exists() && runplan_reached(&rig.status(), "supervising") {
            break;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    assert!(state.join("call.sid").exists(), "launcher never dialed");
    assert!(
        runplan_reached(&rig.status(), "supervising"),
        "launcher had not accepted runtime attachment before TERM"
    );
    let signal = Command::new("kill")
        .args(["-TERM", &child.id().to_string()])
        .status()
        .unwrap();
    assert!(signal.success());
    let started = std::time::Instant::now();
    let exit = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        assert!(
            started.elapsed() < Duration::from_secs(8),
            "graceful phone TERM did not finish"
        );
        std::thread::sleep(Duration::from_millis(50));
    };
    assert_eq!(exit.code(), Some(143));
    assert!(state.join("STOP").exists(), "TERM must assert STOP first");
    assert!(
        state.join("arm-finalized").exists(),
        "launcher interrupted delayed evidence/report finalizers"
    );
    assert_eq!(
        std::fs::read_to_string(rig.root.join("hung-up"))
            .unwrap()
            .trim(),
        "CA11111111111111111111111111111111"
    );
    let status = rig.status();
    assert!(runplan_reached(&status, "completed"), "{status}");
    assert!(!runplan_reached(&status, "failed"), "{status}");
}

#[test]
fn launcher_term_asserts_stop_hangs_up_and_allows_arm_to_finalize() {
    let cwd = std::env::temp_dir().join(format!(
        "earshot-launch-shutdown-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    std::fs::create_dir_all(&cwd).unwrap();
    let event = "KXEARNINGSMENTIONAAPL-26JUL30";
    let state = cwd.join("state").join(event);
    std::fs::create_dir_all(&state).unwrap();
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
    let arm = cwd.join("arm-graceful.sh");
    write_exec(
        &arm,
        r#"sd=""
while [ $# -gt 0 ]; do case "$1" in --state-dir) sd="$2"; shift 2;; *) shift;; esac; done
printf '{"port":8081,"pid":%s,"unix_ts":1}\n' "$$" > "$sd/listener.ready"
while [ ! -f "$sd/STOP" ]; do sleep 0.05; done
sleep 1
printf 'finalized\n' > "$sd/arm-finalized"
exit 0
"#,
    );
    let dial = cwd.join("dial.sh");
    write_exec(
        &dial,
        r#"sid_file=""; prev=""
for arg in "$@"; do if [ "$prev" = "--sid-file" ]; then sid_file="$arg"; fi; prev="$arg"; done
printf 'CA22222222222222222222222222222222\n' > "$sid_file"
exit 0
"#,
    );
    let hangup = cwd.join("hangup.sh");
    write_exec(
        &hangup,
        &format!(
            "printf '%s\\n' \"$1\" > '{}'; exit 0\n",
            state.join("hung-up").display()
        ),
    );
    let script = repo().join("scripts/launch-event.sh");
    let mut child = Command::new("bash")
        .arg(script)
        .args(["--config", config.to_str().unwrap()])
        .args(["--account", account.to_str().unwrap()])
        .args(["--bin", arm.to_str().unwrap()])
        .args(["--bind-port", "8081"])
        .args(["--to", "+15550000000"])
        .args(["--stream-url", "wss://example.invalid/e/8081"])
        .args(["--dial-script", dial.to_str().unwrap()])
        .args(["--hangup-script", hangup.to_str().unwrap()])
        .args(["--stream-confirm-secs", "120"])
        .args(["--shutdown-grace-secs", "75"])
        .arg("--one-shot-ok")
        .current_dir(&cwd)
        .env("ORT_DYLIB_PATH", "/bin/echo")
        .env("EARSHOT_TEST_DISABLE_TEE", "1")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    for _ in 0..100 {
        if state.join("call.sid").exists() {
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(
        state.join("call.sid").exists(),
        "launcher never reached dial"
    );
    let status = Command::new("kill")
        .args(["-TERM", &child.id().to_string()])
        .status()
        .unwrap();
    assert!(status.success());
    let started = std::time::Instant::now();
    let exit = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        assert!(
            started.elapsed() < Duration::from_secs(6),
            "launcher did not finish graceful TERM"
        );
        std::thread::sleep(Duration::from_millis(50));
    };
    assert_eq!(exit.code(), Some(143));
    assert!(state.join("STOP").exists(), "TERM must assert STOP first");
    assert_eq!(
        std::fs::read_to_string(state.join("hung-up"))
            .unwrap()
            .trim(),
        "CA22222222222222222222222222222222"
    );
    assert!(
        state.join("arm-finalized").exists(),
        "launcher killed arm before its delayed evidence finalizer completed"
    );
    let _ = std::fs::remove_dir_all(cwd);
}

#[test]
fn launcher_refuses_shutdown_budget_below_seventy_five_seconds() {
    let (out, ok) = run_launcher(
        &[
            "--stream-url",
            "wss://example.invalid/e/8081",
            "--shutdown-grace-secs",
            "74",
        ],
        Some("/bin/echo"),
    );
    assert!(!ok);
    assert!(
        out.contains("shutdown-grace-secs") && out.contains(">= 75"),
        "{out}"
    );
}
