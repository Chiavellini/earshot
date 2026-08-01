//! Operator-handler invariant (docs/OPERATOR-HANDLER.md v1, human-first):
//! the gated conference path must be provably OFF by default, double-gated
//! when on, and shaped so no audio-injection verb can ever reach a bridge.
//!
//! Guarded here so it cannot regress silently:
//!  1. The DEFAULT dial path (scripts/twilio-dial.sh) stays a one-way
//!     `<Start><Stream>` fork with no `<Connect>/<Dial>/<Number>/<Sip>` —
//!     asserted AGAIN in this file so the default stays frozen even if
//!     tests/twilio_listen_only.rs were ever touched.
//!  2. scripts/twilio-operator-dial.sh refuses without BOTH gates: the
//!     `--operator-i-am-paged` flag AND `[operator_handler] enabled = true`.
//!  3. The gated bridge-leg TwiML is `<Start><Stream track="inbound_track">`
//!     + `<Dial><Conference startConferenceOnEnter="false"
//!     endConferenceOnExit="true">` and NEVER `<Number>/<Sip>/<Connect>/
//!     <Say>/<Record>`.
//!  4. The pager leg (scripts/operator-page.sh) may `<Say>` (it targets the
//!     user's own phone) but its `<Dial>` contains ONLY `<Conference>`, its
//!     destination is EXACTLY the configured cell_number, and any other
//!     `--to` is refused.
//!  5. DTMF stays passcode-entry only: charset [0-9#w], `*` hard-rejected.

use std::path::PathBuf;
use std::process::Command;

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

const CELL: &str = "+525545436717";

/// Write a fixture event config into the target tmpdir and return its path.
/// Inline here (not checked into configs/) so the fixture can never be
/// mistaken for a real event config or trip the allowlist scanners.
fn fixture_config(name: &str, operator_section: &str) -> PathBuf {
    let dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join("operator-gated");
    std::fs::create_dir_all(&dir).expect("create fixture dir");
    let path = dir.join(name);
    let toml = format!(
        "[event]\nid = \"OPTEST-26JUL29\"\nstarts_at = \"2026-07-29T00:00:00Z\"\n\n{operator_section}\n"
    );
    std::fs::write(&path, toml).expect("write fixture config");
    path
}

fn enabled_config() -> PathBuf {
    fixture_config(
        "enabled.toml",
        &format!(
            "[operator_handler]\nenabled = true\ncell_number = \"{CELL}\"\nconference_prefix = \"earshot\""
        ),
    )
}

/// Run a script from the repo root; returns (exit_code, stdout).
fn run(script: &str, args: &[&str]) -> (i32, String) {
    let out = Command::new("bash")
        .arg(script)
        .args(args)
        .current_dir(repo_root())
        .output()
        .unwrap_or_else(|e| panic!("run {script}: {e}"));
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).into_owned(),
    )
}

fn operator_dial(config: &PathBuf, extra: &[&str]) -> (i32, String) {
    let cfg = config.to_str().unwrap();
    let mut args = vec![
        "--config",
        cfg,
        "--to",
        "+18774070666",
        "--stream-url",
        "wss://example.test/media",
        "--dry-run",
    ];
    args.extend_from_slice(extra);
    run("scripts/twilio-operator-dial.sh", &args)
}

// ── 1. the default path stays frozen ────────────────────────────────────────

#[test]
fn default_dial_path_is_still_one_way_start_stream() {
    for dtmf in [None, Some("1234#")] {
        let mut args = vec![
            "--to",
            "+18005551234",
            "--stream-url",
            "wss://example.test/media",
            "--dry-run",
        ];
        if let Some(d) = dtmf {
            args.extend_from_slice(&["--dtmf", d]);
        }
        let (code, twiml) = run("scripts/twilio-dial.sh", &args);
        assert_eq!(code, 0);
        assert!(twiml.contains("<Start><Stream"), "one-way fork: {twiml}");
        assert!(twiml.contains("track=\"inbound_track\""), "inbound-only: {twiml}");
        for forbidden in ["<Connect", "<Dial", "<Number", "<Sip"] {
            assert!(
                !twiml.contains(forbidden),
                "DEFAULT dial TwiML must never contain {forbidden}: {twiml}"
            );
        }
    }
}

// ── 2. the double gate ──────────────────────────────────────────────────────

#[test]
fn operator_dial_refuses_without_the_paged_flag() {
    let (code, _) = operator_dial(&enabled_config(), &[]);
    assert_ne!(code, 0, "config enabled but no --operator-i-am-paged must refuse");
}

#[test]
fn operator_dial_refuses_when_config_gate_is_closed() {
    // enabled = false
    let disabled = fixture_config(
        "disabled.toml",
        &format!("[operator_handler]\nenabled = false\ncell_number = \"{CELL}\""),
    );
    let (code, _) = operator_dial(&disabled, &["--operator-i-am-paged"]);
    assert_ne!(code, 0, "enabled = false must refuse even with the flag");

    // section missing entirely
    let missing = fixture_config("missing.toml", "");
    let (code, _) = operator_dial(&missing, &["--operator-i-am-paged"]);
    assert_ne!(code, 0, "missing [operator_handler] must refuse even with the flag");
}

// ── 3. the gated bridge-leg TwiML shape ─────────────────────────────────────

#[test]
fn gated_bridge_twiml_is_conference_only_with_inbound_fork() {
    let (code, out) = operator_dial(&enabled_config(), &["--operator-i-am-paged"]);
    assert_eq!(code, 0, "gated dry-run must succeed: {out}");
    let twiml = out.lines().next().expect("TwiML on first line");
    assert!(twiml.contains("<Start><Stream"), "fork present: {twiml}");
    assert!(twiml.contains("track=\"inbound_track\""), "inbound-only: {twiml}");
    assert!(twiml.contains("<Dial><Conference"), "conference dial: {twiml}");
    // Exact conference posture: bridge hears silence until the human enters;
    // the conference dies with the bridge leg.
    assert!(twiml.contains("startConferenceOnEnter=\"false\""), "{twiml}");
    assert!(twiml.contains("endConferenceOnExit=\"true\""), "{twiml}");
    assert!(!twiml.contains("startConferenceOnEnter=\"true\""), "{twiml}");
    assert!(!twiml.contains("endConferenceOnExit=\"false\""), "{twiml}");
    for forbidden in ["<Number", "<Sip", "<Connect", "<Say", "<Record"] {
        assert!(
            !twiml.contains(forbidden),
            "bridge-leg TwiML must never contain {forbidden}: {twiml}"
        );
    }
}

#[test]
fn gated_bridge_twiml_keeps_the_dtmf_play_shape() {
    let (code, out) = operator_dial(&enabled_config(), &["--operator-i-am-paged", "--dtmf", "1364784#"]);
    assert_eq!(code, 0, "{out}");
    let twiml = out.lines().next().unwrap();
    // Same padding construction as the default dial (8 × 'w' = 4 s).
    assert!(twiml.contains("<Play digits=\"wwwwwwww1364784#\"/>"), "{twiml}");
}

// ── 4. the pager leg ────────────────────────────────────────────────────────

#[test]
fn pager_dial_contains_only_conference() {
    let cfg = enabled_config();
    let (code, out) = run(
        "scripts/operator-page.sh",
        &["--config", cfg.to_str().unwrap(), "--operator-i-am-paged", "--dry-run"],
    );
    assert_eq!(code, 0, "pager dry-run must succeed: {out}");
    let twiml = out.lines().next().expect("TwiML on first line");

    // <Dial> wraps exactly one <Conference> element and nothing else.
    let start = twiml.find("<Dial>").expect("has <Dial>") + "<Dial>".len();
    let end = twiml.find("</Dial>").expect("has </Dial>");
    let inner = &twiml[start..end];
    assert!(inner.starts_with("<Conference "), "Dial wraps Conference only: {inner}");
    assert!(inner.ends_with("</Conference>"), "Dial wraps Conference only: {inner}");
    assert_eq!(
        inner.matches('<').count(),
        2,
        "no other element inside <Dial>: {inner}"
    );
    // Pager posture is the inverse of the bridge leg: media starts when the
    // human answers, and the human hanging up must not kill the conference.
    assert!(inner.contains("startConferenceOnEnter=\"true\""), "{inner}");
    assert!(inner.contains("endConferenceOnExit=\"false\""), "{inner}");
    for forbidden in ["<Number", "<Sip", "<Connect", "<Record", "<Play"] {
        assert!(
            !twiml.contains(forbidden),
            "pager TwiML must never contain {forbidden}: {twiml}"
        );
    }
    // No media fork on the pager leg — the user's audio never enters capture.
    assert!(!twiml.contains("<Stream"), "pager must not fork media: {twiml}");

    // Destination is EXACTLY the configured cell.
    assert!(
        out.lines().any(|l| l == format!("TO={CELL}")),
        "pager must target the configured cell {CELL}: {out}"
    );
}

#[test]
fn pager_refuses_any_other_destination() {
    let cfg = enabled_config();
    // The configured cell itself is accepted (tripwire passes)…
    let (code, _) = run(
        "scripts/operator-page.sh",
        &["--config", cfg.to_str().unwrap(), "--operator-i-am-paged", "--to", CELL, "--dry-run"],
    );
    assert_eq!(code, 0, "--to equal to the configured cell must pass");
    // …anything else is refused.
    for bad in ["+15550001111", "+18774070666", "+525545436718"] {
        let (code, _) = run(
            "scripts/operator-page.sh",
            &["--config", cfg.to_str().unwrap(), "--operator-i-am-paged", "--to", bad, "--dry-run"],
        );
        assert_ne!(code, 0, "pager --to {bad} must be refused");
    }
}

#[test]
fn pager_is_double_gated_too() {
    let cfg = enabled_config();
    let (code, _) = run(
        "scripts/operator-page.sh",
        &["--config", cfg.to_str().unwrap(), "--dry-run"],
    );
    assert_ne!(code, 0, "pager without --operator-i-am-paged must refuse");

    let disabled = fixture_config(
        "pager-disabled.toml",
        &format!("[operator_handler]\nenabled = false\ncell_number = \"{CELL}\""),
    );
    let (code, _) = run(
        "scripts/operator-page.sh",
        &["--config", disabled.to_str().unwrap(), "--operator-i-am-paged", "--dry-run"],
    );
    assert_ne!(code, 0, "pager with enabled = false must refuse");
}

// ── 5. DTMF charset guard (verbatim from the default path) ─────────────────

#[test]
fn operator_dtmf_star_and_junk_are_rejected() {
    for bad in ["12*34", "*", "*11", "1234*", "abc", "12 34", "12;rm"] {
        let (code, _) = operator_dial(&enabled_config(), &["--operator-i-am-paged", "--dtmf", bad]);
        assert_eq!(code, 3, "operator dial DTMF {bad:?} must be rejected with exit 3");
    }
}
