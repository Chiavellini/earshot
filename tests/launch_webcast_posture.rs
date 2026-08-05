//! W7 posture regression — `scripts/launch-webcast.sh` must be able to arm LIVE,
//! must default to dry run, and must still refuse the fill posture.
//!
//! WHY THIS TEST EXISTS. Until 2026-07-27 the launcher hard-refused
//! `--i-am-live`, so `docs/RUNBOOK.md` step 5 — the command the operator is told
//! to run on the day — produced a **dry run**. The consequence was not "fewer
//! orders": dry runs never reach the risk gate, so the 07-26 fix that makes `arm`
//! refuse a partial allowlist (`config::allowlist_covers_keywords`) could not fire
//! on the documented path at all, and PayPal 07-28 — billed as the campaign's live
//! Δrace measurement — would have placed nothing.
//!
//! Both directions are asserted, because both are failures:
//!   - a launcher that cannot go live silently downgrades the event;
//!   - a launcher that goes live when not asked spends real money.
//!
//! The fill posture stays refused HERE and is asserted so: its gate (the G4
//! fill-tier verdict, docs/BAKEOFF.md) has never been scored, and W7's fillable
//! half is still blocked (docs/RACE-CHECKLIST.md W7).
//!
//! Method: run the real script with `--bin /bin/echo`, so the final `exec` prints
//! the argv that `arm` would have received instead of running it. cwd is a temp
//! dir because the script writes `state/<EVENT>/` relative to cwd.

use std::path::{Path, PathBuf};
use std::process::Command;

fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

/// Runs the launcher against the staged PayPal config with `arm` stubbed by
/// `/bin/echo`. Returns (stdout+stderr, exit-ok).
fn run_launcher(extra: &[&str]) -> (String, bool) {
    let repo = repo();
    let script = repo.join("scripts/launch-webcast.sh");
    let config = repo.join("configs/webcast-pypl-26jul28-arm.toml");

    // Scratch cwd: the script does `mkdir -p state/<EVENT>` relative to cwd and
    // writes a self-check file there. Keep that out of the repo.
    let cwd = std::env::temp_dir().join(format!(
        "earshot-launch-posture-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    std::fs::create_dir_all(&cwd).unwrap();
    let account = cwd.join("acct.env");
    std::fs::write(&account, "# empty test account env\n").unwrap();

    let mut args: Vec<&str> = vec![
        script.to_str().unwrap(),
        "--config",
        config.to_str().unwrap(),
        "--account",
        account.to_str().unwrap(),
        // Stub for `arm`: echoes its argv and exits 0.
        "--bin",
        "/bin/echo",
    ];
    args.extend_from_slice(extra);

    let out = Command::new("bash")
        .args(&args)
        .current_dir(&cwd)
        // The config is engine=streaming_ctc, so the launcher demands a readable
        // ORT dylib. Point it at any existing file — it only stats it here.
        .env("ORT_DYLIB_PATH", "/bin/echo")
        .output()
        .expect("failed to run launch-webcast.sh");

    let mut s = String::from_utf8_lossy(&out.stdout).to_string();
    s.push_str(&String::from_utf8_lossy(&out.stderr));
    let _ = std::fs::remove_dir_all(&cwd);
    (s, out.status.success())
}

#[test]
fn default_is_dry_run_and_does_not_pass_i_am_live() {
    let (out, ok) = run_launcher(&[]);
    assert!(ok, "launcher failed in default mode:\n{out}");
    assert!(
        out.contains("posture: DRY RUN"),
        "default run must announce DRY RUN:\n{out}"
    );
    assert!(
        !out.contains("--i-am-live"),
        "default run must NOT pass --i-am-live to arm:\n{out}"
    );
    assert!(
        out.contains("--allow-mention"),
        "mention posture is always passed:\n{out}"
    );
}

#[test]
fn i_am_live_reaches_arm_and_the_posture_line_says_so() {
    let (out, ok) = run_launcher(&["--i-am-live"]);
    assert!(ok, "launcher failed in live mode:\n{out}");
    assert!(
        out.contains("--i-am-live"),
        "W7 REGRESSION: --i-am-live did not reach arm, so the run would place NO \
         orders and would never exercise the risk gate:\n{out}"
    );
    assert!(
        out.contains("posture: LIVE"),
        "live run must announce LIVE, not 'observe' — a posture line that lies is \
         the defect this test exists for:\n{out}"
    );
}

/// W7's FILL half was lifted for the 2026-08-05 afternoon slate (operator
/// ruling): the webcast lane may now arm `--fillable`, because a 1¢ cap makes
/// `executor::handle_intent` short-circuit at the non-crossing branch before
/// `send_order` — that morning produced 30 "orders" and zero wire sends.
/// `--fill-all` stays refused: it overrides the vetted-word tier, which is a
/// separate decision nobody has taken.
#[test]
fn fill_all_is_still_refused_but_fillable_is_not() {
    let (out, ok) = run_launcher(&["--fill-all"]);
    assert!(!ok, "--fill-all must still be refused (tier override, undecided):\n{out}");
    assert!(out.contains("REFUSING"), "--fill-all refusal must name itself:\n{out}");
    assert!(
        !out.contains("--allow-mention"),
        "--fill-all must be refused BEFORE reaching arm:\n{out}"
    );
}

/// `--fillable` without `--i-am-live` is meaningless (arm refuses the pair) and
/// must fail HERE, at launch, rather than 8 minutes before a call.
#[test]
fn fillable_without_i_am_live_fails_early_and_says_why() {
    let (out, ok) = run_launcher(&["--fillable"]);
    assert!(!ok, "--fillable alone must not launch:\n{out}");
    assert!(
        out.contains("requires --i-am-live"),
        "the refusal must name the missing flag, not just fail:\n{out}"
    );
    assert!(
        !out.contains("--allow-mention"),
        "it must fail before reaching arm:\n{out}"
    );
}

/// The posture line must state what the run can actually DO. The 26AUG05
/// morning printed "orders WILL be sent" for a 1¢ cap that could not send one.
#[test]
fn live_fillable_reaches_arm_and_the_posture_line_says_fill() {
    let (out, _ok) = run_launcher(&["--i-am-live", "--fillable"]);
    assert!(
        out.contains("--fillable"),
        "W7-LIFT REGRESSION: --fillable did not reach arm, so the run could not \
         cross an ask and would repeat the 26AUG05 morning:\n{out}"
    );
    assert!(
        out.contains("mention-FILL"),
        "a fillable run must announce FILL, not probe:\n{out}"
    );
}

#[test]
fn unknown_flags_are_still_rejected() {
    let (out, ok) = run_launcher(&["--bogus"]);
    assert!(!ok, "unknown flag must be rejected:\n{out}");
    assert!(out.contains("unknown arg"), "{out}");
}

/// The launcher builds arm's argv with a bash array. Under `set -u`, expanding an
/// empty array as `"${A[@]}"` is an *unbound variable* error on bash < 4.4 — which
/// is what macOS ships (3.2). That would break the DEFAULT (dry-run) path only,
/// i.e. it would pass every live test and fail on the day. Assert the portable
/// `${A[@]+"${A[@]}"}` form is what is in the file.
#[test]
fn empty_arg_array_uses_the_bash3_safe_expansion() {
    let src = std::fs::read_to_string(repo().join("scripts/launch-webcast.sh")).unwrap();
    assert!(
        src.contains(r#"${LIVE_ARGS[@]+"${LIVE_ARGS[@]}"}"#),
        "LIVE_ARGS must use the ${{A[@]+\"${{A[@]}}\"}} form; the bare \"${{A[@]}}\" \
         aborts under `set -u` on bash 3.2 (macOS) when no --i-am-live is passed"
    );
    assert!(
        Path::new("/bin/bash").exists(),
        "sanity: this test assumes a POSIX box"
    );
}
