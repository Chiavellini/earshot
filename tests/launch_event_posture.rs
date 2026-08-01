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

use std::path::PathBuf;
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
