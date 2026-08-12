//! Listen-only capture invariant (docs/TWILIO.md): the system must be provably
//! incapable of putting noise on a live earnings call.
//!
//! Two hard guarantees, tested here so they cannot regress silently:
//!  1. AUDIO is one-way by construction — the dial TwiML is `<Start><Stream>`
//!     (a fork with no return-audio path) and NEVER `<Connect>`/`<Dial>`/
//!     `<Number>` (which could inject audio), and the WS handler only ever
//!     replies `Pong` (no media-send path).
//!  2. DTMF is passcode-entry ONLY — charset [0-9#w]; `*` (raise-hand/unmute)
//!     is hard-rejected, so the system can never enter a Q&A queue or unmute.

use std::path::PathBuf;
use std::process::Command;

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

/// Run scripts/twilio-dial.sh in --dry-run (builds+prints TwiML, places no call,
/// needs no credentials). Returns (exit_code, stdout).
fn dial_dry_run(dtmf: Option<&str>) -> (i32, String) {
    let mut cmd = Command::new("bash");
    cmd.arg("scripts/twilio-dial.sh")
        .arg("--to")
        .arg("+18005551234")
        .arg("--stream-url")
        .arg("wss://example.test/media")
        .arg("--dry-run")
        .current_dir(repo_root());
    if let Some(d) = dtmf {
        cmd.arg("--dtmf").arg(d);
    }
    let out = cmd.output().expect("run twilio-dial.sh");
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).into_owned(),
    )
}

#[test]
fn dtmf_star_is_rejected() {
    // `*1` / `*11` is the raise-hand / unmute key on operator bridges.
    for bad in ["*", "*1", "*11", "1234*", "pass*code"] {
        let (code, _) = dial_dry_run(Some(bad));
        assert_eq!(code, 3, "DTMF {bad:?} must be rejected with exit 3");
    }
}

#[test]
fn dtmf_non_passcode_chars_are_rejected() {
    for bad in ["abc", "12 34", "12;rm", "+1", "12,34"] {
        let (code, _) = dial_dry_run(Some(bad));
        assert_eq!(
            code, 3,
            "DTMF {bad:?} must be rejected (charset [0-9#w] only)"
        );
    }
}

#[test]
fn valid_passcodes_are_accepted() {
    for ok in ["1234#", "w1234", "0000", "1364784#"] {
        let (code, twiml) = dial_dry_run(Some(ok));
        assert_eq!(code, 0, "DTMF {ok:?} must be accepted");
        assert!(
            twiml.contains(&format!("<Play digits=\"wwwwwwww{ok}\"/>")),
            "TwiML: {twiml}"
        );
    }
}

#[test]
fn twiml_is_one_way_start_stream_never_bidirectional() {
    // With and without a passcode, the TwiML must fork inbound audio one-way and
    // never contain a bidirectional / audio-injecting verb.
    for dtmf in [None, Some("1234#")] {
        let (code, twiml) = dial_dry_run(dtmf);
        assert_eq!(code, 0);
        assert!(
            twiml.contains("<Start><Stream"),
            "must be a one-way fork: {twiml}"
        );
        assert!(
            twiml.contains("track=\"inbound_track\""),
            "inbound-only: {twiml}"
        );
        for forbidden in ["<Connect", "<Dial", "<Number", "<Sip"] {
            assert!(
                !twiml.contains(forbidden),
                "TwiML must never contain {forbidden}: {twiml}"
            );
        }
    }
}

#[test]
fn pause_secs_changes_only_the_hold_time() {
    // The bridge-probe path shortens <Pause>; nothing else in the TwiML may
    // move, and out-of-range values fail closed.
    let run = |pause: &str| {
        let out = Command::new("bash")
            .arg("scripts/twilio-dial.sh")
            .arg("--to")
            .arg("+18005551234")
            .arg("--stream-url")
            .arg("wss://example.test/media")
            .arg("--pause-secs")
            .arg(pause)
            .arg("--dry-run")
            .current_dir(repo_root())
            .output()
            .expect("run twilio-dial.sh");
        (
            out.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&out.stdout).into_owned(),
        )
    };
    let (code, twiml) = run("150");
    assert_eq!(code, 0);
    assert!(twiml.contains("<Pause length=\"150\"/>"), "TwiML: {twiml}");
    assert!(
        twiml.contains("<Start><Stream"),
        "one-way fork must survive: {twiml}"
    );
    assert!(
        twiml.contains("track=\"inbound_track\""),
        "inbound-only: {twiml}"
    );
    for forbidden in ["<Connect", "<Dial", "<Number", "<Sip"] {
        assert!(
            !twiml.contains(forbidden),
            "TwiML must never contain {forbidden}: {twiml}"
        );
    }
    for bad in ["0", "5401", "-5", "abc"] {
        let (code, _) = run(bad);
        assert_eq!(code, 2, "--pause-secs {bad:?} must be refused");
    }
    // Default (no flag) stays the pre-existing 90-min hold, byte-for-byte.
    let (code, twiml) = dial_dry_run(None);
    assert_eq!(code, 0);
    assert!(
        twiml.contains("<Pause length=\"5400\"/>"),
        "default hold changed: {twiml}"
    );
}

#[test]
fn ws_handler_only_ever_sends_pong() {
    // Structural regression guard: the Twilio WS source is receive-only. The
    // only `ws.send(` in the handler must be `Message::Pong` (keepalive). A
    // future `media`/audio send would trip this.
    let src = std::fs::read_to_string(repo_root().join("src/source/twilio_ws.rs"))
        .expect("read twilio_ws.rs");
    for line in src.lines() {
        let l = line.trim();
        if l.starts_with("//") {
            continue;
        }
        if l.contains("ws.send(") || l.contains(".send(Message::") {
            assert!(
                l.contains("Message::Pong"),
                "twilio_ws sends a non-Pong frame — listen-only invariant at risk: {l}"
            );
        }
    }
}
