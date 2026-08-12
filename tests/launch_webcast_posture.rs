//! Webcast launch posture regression — `scripts/launch-webcast.sh` must default
//! to dry run, and a LIVE arm must be bound to one immutable schema-3 RunPlan.
//!
//! WHY THIS TEST EXISTS. Until 2026-07-27 the launcher hard-refused
//! `--i-am-live`, so `docs/history/RUNBOOK.md` step 5 — the command the operator is told
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
//! The 2026-08-05 ruling allows `--fillable`; `--fill-all` remains refused
//! because it overrides the vetted-word tier.
//!
//! Method: dry posture tests run the real script with `--bin /bin/echo`.
//! Live tests compile a real temporary RunPlan/receipt chain with eventctl and
//! use an arm stub that emits the runtime-owned readiness, attachment, and
//! report doors. Every fixture is isolated under a temporary repository root.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

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
        .env("EARSHOT_TEST_DISABLE_TEE", "1")
        .output()
        .expect("failed to run launch-webcast.sh");

    let mut s = String::from_utf8_lossy(&out.stdout).to_string();
    s.push_str(&String::from_utf8_lossy(&out.stderr));
    let _ = std::fs::remove_dir_all(&cwd);
    (s, out.status.success())
}

const RUNPLAN_EVENT: &str = "KXEARNINGSMENTIONWEBCAST-26AUG12";
const RUNPLAN_HOST: &str = "webcast-box-test";
const RUNPLAN_FINGERPRINT: &str =
    "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";

fn write_exec(path: &Path, body: &str) {
    std::fs::write(path, format!("#!/usr/bin/env bash\nset -eu\n{body}")).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut permissions = std::fs::metadata(path).unwrap().permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(path, permissions).unwrap();
    }
}

struct WebcastRunPlanRig {
    root: PathBuf,
    run_dir: PathBuf,
    config: PathBuf,
    account: PathBuf,
    arm: PathBuf,
    alert: PathBuf,
    capture: PathBuf,
}

impl Drop for WebcastRunPlanRig {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

impl WebcastRunPlanRig {
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
            "earshot-webcast-runplan-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("configs")).unwrap();
        std::fs::create_dir_all(root.join("state")).unwrap();
        let config = root.join("configs/event.toml");
        std::fs::write(
            &config,
            format!(
                r#"[event]
id = "{RUNPLAN_EVENT}"
starts_at = "2026-08-12T21:00:00Z"

[[sources]]
kind = "pipe"
label = "fixture"
sample_rate_hz = 16000
cmd = ["true"]

[[keywords]]
market_ticker = "KXEARNINGSMENTIONWEBCAST-26AUG12-TEST"
phrase = "test"
max_price_cents = 97
budget_contracts = 1
"#,
            ),
        )
        .unwrap();
        let account = root.join("account.env");
        std::fs::write(
            &account,
            format!(
                "export EARSHOT_EXECUTION_HOST={RUNPLAN_HOST}\n\
                 export EARSHOT_KALSHI_ACCOUNT_FINGERPRINT={RUNPLAN_FINGERPRINT}\n"
            ),
        )
        .unwrap();
        let run_dir = root.join("var/runs").join(RUNPLAN_EVENT).join("run-test");
        let arm = root.join("arm-stub.sh");
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
printf '{"port":0,"pid":%s,"unix_ts":1}\n' "$$" > "$sd/listener.ready"
write_source_marker() {
  python3 - "$EARSHOT_RUN_PLAN" "$sd/source.attached" "$$" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1], encoding="utf-8"))
json.dump({"schema_version": 1, "run_id": plan["run_id"],
           "event_id": plan["event_id"], "plan_sha256": plan["plan_sha256"],
           "pid": int(sys.argv[3]), "unix_ns": 1, "accepted_frames": 1},
          open(sys.argv[2], "w", encoding="utf-8"))
PY
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
            "access_acquired",
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

    fn command_for_config(
        &self,
        config: &Path,
        mode: &str,
        exact_run_dir: bool,
        extra: &[&str],
    ) -> Command {
        let script = repo().join("scripts/launch-webcast.sh");
        let mut command = Command::new("bash");
        command
            .arg(script)
            .args(["--config", config.to_str().unwrap()])
            .args(["--account", self.account.to_str().unwrap()])
            .args(["--bin", self.arm.to_str().unwrap()])
            .arg("--i-am-live")
            .args(["--shutdown-grace-secs", "75"])
            .args(extra)
            .current_dir(&self.root)
            .env("EARSHOT_REPO", &self.root)
            .env("EARSHOT_EVENTCTL", repo().join("scripts/eventctl.py"))
            .env("EARSHOT_ALERTCTL", &self.alert)
            .env("RUNPLAN_CAPTURE", &self.capture)
            .env("RUNPLAN_ALERTS", self.root.join("alerts.log"))
            .env("ARM_STUB_MODE", mode)
            .env("EARSHOT_TEST_DISABLE_TEE", "1");
        if exact_run_dir {
            command.args(["--run-dir", self.run_dir.to_str().unwrap()]);
        }
        command
    }

    fn command(&self, mode: &str, exact_run_dir: bool, extra: &[&str]) -> Command {
        self.command_for_config(&self.config, mode, exact_run_dir, extra)
    }

    fn launch(&self, mode: &str, exact_run_dir: bool, extra: &[&str]) -> std::process::Output {
        self.command(mode, exact_run_dir, extra)
            .output()
            .expect("run launch-webcast with RunPlan")
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

    fn alerts(&self) -> String {
        std::fs::read_to_string(self.root.join("alerts.log")).unwrap_or_default()
    }
}

fn reached(status: &serde_json::Value, transition: &str) -> bool {
    status["state"]["reached"]
        .as_array()
        .unwrap()
        .iter()
        .any(|receipt| receipt["transition"] == transition)
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
    let rig = WebcastRunPlanRig::new();
    let output = rig.launch("good", false, &[]);
    let out = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        output.status.success(),
        "launcher failed in live mode:\n{out}"
    );
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
    assert!(
        !ok,
        "--fill-all must still be refused (tier override, undecided):\n{out}"
    );
    assert!(
        out.contains("REFUSING"),
        "--fill-all refusal must name itself:\n{out}"
    );
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
    let rig = WebcastRunPlanRig::new();
    let output = rig.launch("good", true, &["--fillable"]);
    let out = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
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
fn live_webcast_binds_schema3_runplan_paths_and_completes_typed_lifecycle() {
    let rig = WebcastRunPlanRig::new();
    let out = rig.launch("good", true, &[]);
    assert!(
        out.status.success(),
        "RunPlan-bound launch failed: {}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    let captured = std::fs::read_to_string(&rig.capture).unwrap();
    let fields: Vec<&str> = captured.trim().split('|').collect();
    assert_eq!(fields.len(), 6, "unexpected arm environment: {captured}");
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
    for transition in [
        "launched",
        "source_attached",
        "supervising",
        "draining",
        "completed",
    ] {
        assert!(
            reached(&status, transition),
            "missing {transition}: {status}"
        );
    }
    assert!(
        !reached(&status, "failed"),
        "healthy report must not fail: {status}"
    );
}

#[test]
fn malformed_or_unhealthy_report_records_failed_never_completed() {
    let rig = WebcastRunPlanRig::new();
    let out = rig.launch("bad-report", false, &[]);
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
    assert!(
        reached(&status, "failed"),
        "failed receipt missing: {status}"
    );
    assert!(
        !reached(&status, "completed"),
        "invalid report completed: {status}"
    );
    let alerts = rig.alerts();
    assert!(
        alerts.contains("raise")
            && alerts.contains("runtime_failed")
            && alerts.contains("lifecycle_terminal"),
        "terminal runtime failure did not cross the proof-alert boundary: {alerts}"
    );
}

#[test]
fn unresolved_cancellation_ids_never_complete_a_run() {
    let rig = WebcastRunPlanRig::new();
    let out = rig.launch("unresolved", false, &[]);
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
    assert!(reached(&status, "failed"), "{status}");
    assert!(!reached(&status, "completed"), "{status}");
}

#[test]
fn exit_before_detector_path_media_goes_straight_to_failed() {
    let rig = WebcastRunPlanRig::new();
    let out = rig.launch("no-attach", false, &[]);
    assert!(!out.status.success());
    let status = rig.status();
    assert!(
        reached(&status, "launched"),
        "listener readiness was not recorded: {status}"
    );
    assert!(
        reached(&status, "failed"),
        "pre-attachment failure missing: {status}"
    );
    assert!(
        !reached(&status, "source_attached"),
        "source was never attached: {status}"
    );
    assert!(
        !reached(&status, "draining"),
        "draining is illegal before supervising: {status}"
    );
    assert!(
        !reached(&status, "completed"),
        "pre-attachment exit completed: {status}"
    );
}

#[test]
fn mismatched_source_marker_cannot_advance_supervision() {
    let rig = WebcastRunPlanRig::new();
    let out = rig.launch("wrong-marker", false, &[]);
    assert!(!out.status.success());
    let status = rig.status();
    assert!(reached(&status, "launched"), "{status}");
    assert!(!reached(&status, "source_attached"), "{status}");
    assert!(!reached(&status, "supervising"), "{status}");
    assert!(reached(&status, "failed"), "{status}");
}

#[test]
fn live_webcast_refuses_host_mismatch_before_arm() {
    let rig = WebcastRunPlanRig::new();
    std::fs::write(
        &rig.account,
        format!(
            "export EARSHOT_EXECUTION_HOST=wrong-box\nexport EARSHOT_KALSHI_ACCOUNT_FINGERPRINT={RUNPLAN_FINGERPRINT}\n"
        ),
    )
    .unwrap();
    let out = rig.launch("good", true, &[]);
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
    assert!(!rig.capture.exists(), "arm ran despite host mismatch");
}

#[test]
fn live_webcast_refuses_account_fingerprint_mismatch_before_arm() {
    let rig = WebcastRunPlanRig::new();
    std::fs::write(
        &rig.account,
        format!(
            "export EARSHOT_EXECUTION_HOST={RUNPLAN_HOST}\n\
             export EARSHOT_KALSHI_ACCOUNT_FINGERPRINT=sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd\n"
        ),
    )
    .unwrap();
    let out = rig.launch("good", false, &[]);
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        !out.status.success(),
        "fingerprint mismatch must refuse: {combined}"
    );
    assert!(combined.contains("account fingerprint") && combined.contains("RunPlan"));
    assert!(
        !rig.capture.exists(),
        "arm ran despite fingerprint mismatch"
    );
}

#[test]
fn live_webcast_refuses_config_not_named_by_runplan() {
    let rig = WebcastRunPlanRig::new();
    let other_config = rig.root.join("configs/other.toml");
    std::fs::copy(&rig.config, &other_config).unwrap();
    let out = rig
        .command_for_config(&other_config, "good", true, &[])
        .output()
        .unwrap();
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        !out.status.success(),
        "config mismatch must refuse: {combined}"
    );
    assert!(
        combined.contains("not immutable RunPlan config"),
        "{combined}"
    );
    assert!(
        !rig.capture.exists(),
        "arm ran with a different config path"
    );
}

#[test]
fn live_webcast_refuses_run_dir_outside_canonical_event_root() {
    let rig = WebcastRunPlanRig::new();
    let outside = rig.root.join("outside-run");
    std::fs::create_dir_all(&outside).unwrap();
    let out = rig
        .command("good", false, &["--run-dir", outside.to_str().unwrap()])
        .output()
        .unwrap();
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        !out.status.success(),
        "outside run dir must refuse: {combined}"
    );
    assert!(combined.contains("outside"), "{combined}");
    assert!(
        !rig.capture.exists(),
        "arm ran from an outside run directory"
    );
}

#[test]
fn live_webcast_refuses_duplicate_launched_run_before_arm() {
    let rig = WebcastRunPlanRig::new();
    let record = rig.eventctl(&[
        "record",
        "--run-dir",
        rig.run_dir.to_str().unwrap(),
        "--transition",
        "launched",
        "--actor",
        "prior-attempt",
    ]);
    assert!(record.status.success());
    let out = rig.launch("good", true, &[]);
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        !out.status.success(),
        "duplicate run must refuse: {combined}"
    );
    assert!(combined.contains("duplicate activation") || combined.contains("pre-launch gate"));
    assert!(
        !rig.capture.exists(),
        "arm ran for an already-launched RunPlan"
    );
}

#[test]
fn live_webcast_refuses_terminal_run_before_arm() {
    let rig = WebcastRunPlanRig::new();
    let record = rig.eventctl(&[
        "record",
        "--run-dir",
        rig.run_dir.to_str().unwrap(),
        "--transition",
        "failed",
        "--actor",
        "prior-attempt",
    ]);
    assert!(record.status.success());
    let out = rig.launch("good", true, &[]);
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        !out.status.success(),
        "terminal run must refuse: {combined}"
    );
    assert!(combined.contains("terminal") || combined.contains("pre-launch gate"));
    assert!(!rig.capture.exists(), "arm ran for a terminal RunPlan");
}

#[test]
fn live_webcast_refuses_legacy_sidecar_escape_hatch() {
    let rig = WebcastRunPlanRig::new();
    let out = rig
        .command("good", false, &[])
        .env("EARSHOT_NO_SIDECAR", "1")
        .output()
        .unwrap();
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        !out.status.success(),
        "legacy evidence bypass must refuse: {combined}"
    );
    assert!(combined.contains("in-process audio evidence is mandatory"));
    assert!(!rig.capture.exists(), "arm ran with EARSHOT_NO_SIDECAR");
    let source = std::fs::read_to_string(repo().join("scripts/launch-webcast.sh")).unwrap();
    assert!(
        !source.contains("capture-sidecar.sh"),
        "launcher still starts a drifting feed"
    );
}

#[test]
fn launcher_term_asserts_stop_and_allows_runtime_finalizers_to_finish() {
    let rig = WebcastRunPlanRig::new();
    let mut child = rig
        .command("wait-stop", true, &[])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    let state = rig.root.join("state").join(RUNPLAN_EVENT);
    for _ in 0..200 {
        if state.join("source.attached").exists() && reached(&rig.status(), "supervising") {
            break;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    assert!(
        state.join("source.attached").exists(),
        "launcher never attached source"
    );
    assert!(
        reached(&rig.status(), "supervising"),
        "launcher had not accepted attachment before signal"
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
            "graceful TERM did not finish"
        );
        std::thread::sleep(Duration::from_millis(50));
    };
    assert_eq!(exit.code(), Some(143));
    assert!(
        state.join("STOP").exists(),
        "TERM must assert STOP before signaling arm"
    );
    assert!(
        state.join("arm-finalized").exists(),
        "launcher interrupted the runtime before its delayed evidence finalizer"
    );
    let status = rig.status();
    assert!(
        reached(&status, "completed"),
        "valid drained report must complete: {status}"
    );
}

#[test]
fn launcher_refuses_shutdown_budget_below_seventy_five_seconds() {
    let (out, ok) = run_launcher(&["--shutdown-grace-secs", "74"]);
    assert!(!ok);
    assert!(
        out.contains("shutdown-grace-secs") && out.contains(">= 75"),
        "{out}"
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
