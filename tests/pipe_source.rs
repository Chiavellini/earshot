//! `PipeSource` integration tests: framing from child stdout, EOF flush,
//! the dead-stream guard, and teardown. The children are plain shell — no
//! ffmpeg dependency, so these run everywhere.

use std::time::Duration;

use earshot::ring;
use earshot::source::pipe::PipeSource;
use earshot::source::{AudioSource, SourceError, SourceExit};
use earshot::types::SourceId;

const RATE: u32 = 16_000;
const FRAME_SAMPLES: usize = (RATE as usize * 20) / 1000; // 320
const FRAME_BYTES: usize = FRAME_SAMPLES * 2; // 640

fn sh(script: &str) -> Vec<String> {
    vec!["/bin/sh".into(), "-c".into(), script.into()]
}

#[tokio::test]
async fn frames_from_child_stdout_dense_seq_and_eof() {
    // 100 exact frames of zeros, then EOF.
    let src = PipeSource::new(
        sh(&format!("head -c {} /dev/zero", FRAME_BYTES * 100)),
        RATE,
        SourceId(0),
    )
    .unwrap();
    let (tx, mut rx) = ring::ring(256);
    let exit = Box::new(src).run(tx).await.unwrap();
    assert_eq!(exit, SourceExit::EndOfStream);

    let mut n = 0u64;
    while let Some(f) = rx.try_recv() {
        assert_eq!(f.seq, n, "seq must be dense");
        assert_eq!(f.sample_rate_hz, RATE);
        assert_eq!(f.pcm.len(), FRAME_SAMPLES);
        assert!(f.capture_ts.0 > 0, "capture_ts stamped at arrival");
        n += 1;
    }
    assert_eq!(n, 100);
}

#[tokio::test]
async fn trailing_partial_frame_is_flushed_at_eof() {
    // 2 full frames + 100 samples (200 bytes): expect 3 frames, last short.
    let bytes = FRAME_BYTES * 2 + 200;
    let src =
        PipeSource::new(sh(&format!("head -c {bytes} /dev/zero")), RATE, SourceId(0)).unwrap();
    let (tx, mut rx) = ring::ring(64);
    Box::new(src).run(tx).await.unwrap();

    let mut lens = vec![];
    while let Some(f) = rx.try_recv() {
        lens.push(f.pcm.len());
    }
    assert_eq!(lens, vec![FRAME_SAMPLES, FRAME_SAMPLES, 100]);
}

#[tokio::test]
async fn dead_stream_guard_kills_and_fails_loud() {
    // Child produces nothing and sleeps: the idle timeout must fire, the
    // child must be killed, and the error must be loud — a dead webcast
    // parked on silence must never look like a 90-minute success.
    let src = PipeSource::new(sh("sleep 30"), RATE, SourceId(0))
        .unwrap()
        .with_idle_timeout(Duration::from_millis(300));
    let (tx, _rx) = ring::ring(16);
    let started = std::time::Instant::now();
    let err = Box::new(src).run(tx).await.unwrap_err();
    assert!(
        started.elapsed() < Duration::from_secs(5),
        "guard must fire fast"
    );
    match err {
        SourceError::Protocol(msg) => assert!(msg.contains("idle"), "got: {msg}"),
        other => panic!("expected Protocol idle error, got {other:?}"),
    }
}

#[tokio::test]
async fn spawn_failure_is_a_cheap_constructor_style_error() {
    // Nonexistent binary: fails before any frame flows.
    let src = PipeSource::new(vec!["/nonexistent-decoder-xyz".into()], RATE, SourceId(0)).unwrap();
    let (tx, _rx) = ring::ring(16);
    match Box::new(src).run(tx).await {
        Err(SourceError::Io(_)) => {}
        other => panic!("expected Io spawn error, got {other:?}"),
    }
}

#[tokio::test]
async fn empty_command_refused_at_construction() {
    assert!(PipeSource::new(vec![], RATE, SourceId(0)).is_err());
    assert!(PipeSource::hls("", SourceId(0)).is_err());
}

// ── C2: leg respawn ─────────────────────────────────────────────────────────
//
// WHY THESE EXIST. COHR 2026-08-12: the arm exited MID-CALL at 21:35:44Z. Leg
// `streamlink` died at 21:15:48Z on an HLS `Sequence gap`, leg `pipe_tuned` at
// 21:35:44Z on `expired from playlists`, and a dead pipe leg never came back —
// so when the last one went, the run ended while the venue was still talking.
// Coverage was saved only by an out-of-band fetcher someone remembered to run.
//
// The budget mirrors twilio_ws's `max_reconnects`, including the property that
// makes it safe to ship: budget 0 reproduces the old returns exactly, so every
// replay/bake-off/probe caller is untouched and `dead_stream_guard_kills_and_
// fails_loud` above still pins the loud-failure contract, unmodified.

const BACKOFF: Duration = Duration::from_millis(20);

/// Drains a ring in the background so a long-running child cannot wedge on a
/// full ring, and hands back every frame it saw.
fn drain(
    mut rx: earshot::ring::RingReceiver<earshot::types::AudioFrame>,
) -> std::thread::JoinHandle<Vec<u64>> {
    std::thread::spawn(move || {
        let mut seqs = Vec::new();
        let deadline = std::time::Instant::now() + Duration::from_secs(20);
        while std::time::Instant::now() < deadline {
            match rx.try_recv() {
                Some(f) => seqs.push(f.seq),
                None => std::thread::sleep(Duration::from_millis(2)),
            }
        }
        seqs
    })
}

#[tokio::test]
async fn decoder_death_respawns_and_frames_resume() {
    // A child that emits one frame and exits. Under budget 0 this is a single
    // frame and EndOfStream; under a budget it must run again.
    let src = PipeSource::new(
        sh(&format!("head -c {FRAME_BYTES} /dev/zero")),
        RATE,
        SourceId(0),
    )
    .unwrap()
    .with_respawn_budget(3, BACKOFF);
    let (tx, mut rx) = earshot::ring::ring(256);

    let exit = tokio::time::timeout(Duration::from_secs(10), Box::new(src).run(tx))
        .await
        .expect("respawn loop must not hang")
        .expect("a budgeted leg ends cleanly once the budget is spent");
    assert_eq!(exit, SourceExit::EndOfStream);

    let mut seqs = Vec::new();
    while let Some(f) = rx.try_recv() {
        seqs.push(f.seq);
    }
    // 1 frame per spawn, 1 + 3 respawns.
    assert_eq!(seqs.len(), 4, "decoder must be respawned to the budget");
    assert_eq!(
        seqs,
        vec![0, 1, 2, 3],
        "seq must stay dense ACROSS respawns — a leg that restarted its \
         numbering would make every downstream gap statistic a lie"
    );
}

#[tokio::test]
async fn idle_timeout_respawns_instead_of_killing_the_leg() {
    // The COHR shape: the child is ALIVE but permanently silent, which is what
    // an HLS sequence gap does to ffmpeg/streamlink. First spawn goes quiet;
    // the marker makes the next one produce audio, proving the leg recovered
    // rather than merely being retried into the same silence.
    let dir = tempfile::tempdir().unwrap();
    let marker = dir.path().join("spawned");
    let src = PipeSource::new(
        sh(&format!(
            "if [ -f {m} ]; then head -c {n} /dev/zero; sleep 5; else touch {m}; sleep 5; fi",
            m = marker.display(),
            n = FRAME_BYTES * 10
        )),
        RATE,
        SourceId(0),
    )
    .unwrap()
    .with_idle_timeout(Duration::from_millis(300))
    .with_respawn_budget(1, BACKOFF);

    let (tx, rx) = earshot::ring::ring(256);
    let drained = drain(rx);
    let err = tokio::time::timeout(Duration::from_secs(15), Box::new(src).run(tx))
        .await
        .expect("respawn loop must not hang")
        .expect_err("budget of 1 is spent by the second silence");

    // The leg still fails loud once the budget is gone — respawn must not be a
    // way for a genuinely dead venue to look healthy.
    match err {
        SourceError::Protocol(msg) => assert!(msg.contains("idle"), "got: {msg}"),
        other => panic!("expected Protocol idle error, got {other:?}"),
    }
    let seqs = drained.join().unwrap();
    assert_eq!(
        seqs.len(),
        10,
        "the respawned decoder must deliver frames the first one never sent"
    );
}

#[tokio::test]
async fn budget_zero_is_the_historical_one_shot() {
    // The pin that makes this change safe to default-off: with no budget, a
    // child that exits is still exactly one spawn and one EndOfStream.
    let dir = tempfile::tempdir().unwrap();
    let counter = dir.path().join("spawns");
    let src = PipeSource::new(
        sh(&format!(
            "echo x >> {c}; head -c {n} /dev/zero",
            c = counter.display(),
            n = FRAME_BYTES
        )),
        RATE,
        SourceId(0),
    )
    .unwrap();
    let (tx, _rx) = earshot::ring::ring(64);
    let exit = tokio::time::timeout(Duration::from_secs(10), Box::new(src).run(tx))
        .await
        .expect("must not hang")
        .unwrap();
    assert_eq!(exit, SourceExit::EndOfStream);
    assert_eq!(
        std::fs::read_to_string(&counter).unwrap().lines().count(),
        1,
        "budget 0 must spawn the decoder exactly once"
    );
}

/// Frames one spawn of the killed-decoder fixture emits before the kill lands
/// (~400 ms at ~50 frames/s). Anything near this means the leg died there.
const FRAMES_BEFORE_KILL: u64 = 40;
/// Frames a full spawn emits if nothing kills it.
const FRAMES_PER_SPAWN: u64 = 250;

/// The work order's acceptance case, at pipeline altitude: kill the decoder
/// mid-stream with a REAL signal and assert the run keeps capturing.
///
/// This is the COHR 2026-08-12 shape end to end — `run_pipeline` drives a real
/// `PipeSource` and an external `kill -9` takes the decoder out from under it.
///
/// NOTE WHAT IS *NOT* THE ASSERTION. A SIGKILLed decoder closes the pipe, which
/// reaches the source as EOF, which returned `Ok(EndOfStream)` — so even
/// BEFORE this fix the run ended `Ok` with a clean record, just 20 minutes
/// early and with the venue still talking. Verified by running this fixture at
/// budget 0: `run_pipeline` returns Ok, `spawns=1`. That is precisely why
/// "the run returned Ok" proves nothing here, and why fanout never even
/// counted these as died legs. The real assertions are that the decoder came
/// BACK and that frames kept arriving after the kill.
#[test]
fn pipeline_survives_a_decoder_killed_mid_stream() {
    use earshot::decision::DecisionTable;
    use earshot::detector::stub::EnergyStubDetector;
    use earshot::pipeline::preproc::PreprocCfg;
    use earshot::pipeline::run_pipeline;

    let dir = tempfile::tempdir().unwrap();
    let pidfile = dir.path().join("decoder.pid");
    let spawns = dir.path().join("spawns");
    // Publishes its own PID, then paces out FRAMES_PER_SPAWN frames of silence
    // so the test has a window in which to kill it mid-stream.
    let src = PipeSource::new(
        sh(&format!(
            "echo x >> {s}; echo $$ > {p}; i=0; while [ $i -lt {frames} ]; do \
               head -c {n} /dev/zero; i=$((i+1)); sleep 0.02; done",
            s = spawns.display(),
            p = pidfile.display(),
            frames = FRAMES_PER_SPAWN,
            n = FRAME_BYTES
        )),
        RATE,
        SourceId(0),
    )
    .unwrap()
    .with_respawn_budget(1, Duration::from_millis(50));

    let killer = std::thread::spawn({
        let pidfile = pidfile.clone();
        move || {
            let deadline = std::time::Instant::now() + Duration::from_secs(10);
            while std::time::Instant::now() < deadline {
                if let Ok(text) = std::fs::read_to_string(&pidfile) {
                    let pid = text.trim().to_string();
                    if !pid.is_empty() {
                        std::thread::sleep(Duration::from_millis(400));
                        // A real `kill -9`, not a cooperative shutdown: the
                        // decoder gets no chance to clean up, which is what a
                        // hard venue/CDN failure looks like from in here.
                        let _ = std::process::Command::new("kill")
                            .args(["-9", &pid])
                            .status();
                        return true;
                    }
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            false
        }
    });

    let report = run_pipeline(
        "RESPAWN",
        160,
        PreprocCfg::default(),
        Box::new(src),
        Box::new(EnergyStubDetector::for_standard_fixture()),
        DecisionTable::from_keywords(&[]),
        None,
        Default::default(),
    )
    .expect("the pipeline must not fault out");

    assert!(
        killer.join().unwrap(),
        "test bug: never found the decoder pid"
    );
    let spawn_count = std::fs::read_to_string(&spawns).unwrap().lines().count();
    assert!(
        spawn_count >= 2,
        "the decoder was killed and never came back (spawns={spawn_count}) — \
         this is the COHR 21:35:44Z exit"
    );
    // The arithmetic that separates "recovered" from "died at the kill":
    // one killed spawn yields ~FRAMES_BEFORE_KILL; a recovered leg adds a
    // whole further spawn on top of it.
    assert!(
        report.frames_in as u64 > FRAMES_BEFORE_KILL * 2,
        "capture did not resume after the kill: frames_in={} (a leg that died \
         at the kill lands near {FRAMES_BEFORE_KILL})",
        report.frames_in
    );
}

#[tokio::test]
async fn respawn_budget_is_capped() {
    // A venue that kills a leg more than 20 times needs a different fix, not
    // more retries — the same ceiling config.rs puts on Twilio reconnects.
    let dir = tempfile::tempdir().unwrap();
    let counter = dir.path().join("spawns");
    let src = PipeSource::new(
        sh(&format!("echo x >> {c}", c = counter.display())),
        RATE,
        SourceId(0),
    )
    .unwrap()
    .with_respawn_budget(9_999, Duration::from_millis(1));
    let (tx, _rx) = earshot::ring::ring(16);
    tokio::time::timeout(Duration::from_secs(20), Box::new(src).run(tx))
        .await
        .expect("must not hang")
        .unwrap();
    assert_eq!(
        std::fs::read_to_string(&counter).unwrap().lines().count(),
        (earshot::source::pipe::MAX_RESPAWN_BUDGET + 1) as usize,
        "an over-large budget must clamp to MAX_RESPAWN_BUDGET"
    );
}

/// Detection parity with the replay path: the SAME fixture audio, fed once
/// via `ReplaySource` and once via `PipeSource` (raw s16le through `cat`),
/// must produce the same detections through the full pipeline (stub
/// detector — no models, no ffmpeg needed). This is the proof that the
/// webcast lane's frames are indistinguishable downstream of the source door.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn pipeline_detection_parity_with_replay() {
    use earshot::config::{KeywordConfig, Policy};
    use earshot::decision::DecisionTable;
    use earshot::detector::stub::EnergyStubDetector;
    use earshot::fixture::{Burst, FixtureSpec};
    use earshot::pipeline::preproc::PreprocCfg;
    use earshot::pipeline::run_pipeline;
    use earshot::source::replay::ReplaySource;

    let keywords = || {
        vec![KeywordConfig {
            market_ticker: "KX-PIPE-PARITY".into(),
            phrase: "tone".into(),
            variants: vec!["tone".into()],
            max_price_cents: 1,
            budget_contracts: 1,
            policy: Policy::FastOnly,
            rule_risk_haircut_cents: 0,
            fast_prefixes: vec![],
            fast_threshold: None,
            confirmed_threshold: None,
            boost: None,
            min_confidence_fast: 0.0,
            min_confidence_confirmed: 0.0,
        }]
    };
    let spec = FixtureSpec {
        sample_rate_hz: 8000,
        total_ms: 2000,
        tone_hz: 440.0,
        amplitude: 0.6,
        bursts: vec![Burst {
            onset_ms: 1000,
            dur_ms: 500,
        }],
    };
    let dir = tempfile::tempdir().unwrap();
    let wav = dir.path().join("f.wav");
    spec.write_wav(&wav).unwrap();
    // Same samples as raw s16le for the pipe lane.
    let raw = dir.path().join("f.s16le");
    let bytes: Vec<u8> = spec
        .samples()
        .iter()
        .flat_map(|s| s.to_le_bytes())
        .collect();
    std::fs::write(&raw, &bytes).unwrap();

    let run = |source: Box<dyn AudioSource>, kws: Vec<KeywordConfig>| async move {
        let detector = Box::new(EnergyStubDetector::for_standard_fixture());
        let table = DecisionTable::from_keywords(&kws);
        tokio::task::spawn_blocking(move || {
            run_pipeline(
                "PARITY",
                160,
                PreprocCfg::default(),
                source,
                detector,
                table,
                None,
                Default::default(),
            )
        })
        .await
        .unwrap()
        .unwrap()
    };

    let replay_report = run(
        Box::new(ReplaySource::from_wav(&wav, 0.0, SourceId(0)).unwrap()),
        keywords(),
    )
    .await;
    let pipe_report = run(
        Box::new(
            PipeSource::new(sh(&format!("cat {}", raw.display())), 8000, SourceId(0)).unwrap(),
        ),
        keywords(),
    )
    .await;

    assert!(
        !replay_report.detections.is_empty(),
        "fixture must fire the stub detector"
    );
    assert_eq!(
        replay_report.detections.len(),
        pipe_report.detections.len(),
        "pipe lane must detect exactly what replay detects"
    );
    assert_eq!(
        pipe_report.drops.total(),
        0,
        "unpaced local pipe must not drop"
    );
}
