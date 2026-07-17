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
    let src = PipeSource::new(sh(&format!("head -c {} /dev/zero", FRAME_BYTES * 100)), RATE, SourceId(0))
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
    let src = PipeSource::new(sh(&format!("head -c {bytes} /dev/zero")), RATE, SourceId(0)).unwrap();
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
    assert!(started.elapsed() < Duration::from_secs(5), "guard must fire fast");
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
        bursts: vec![Burst { onset_ms: 1000, dur_ms: 500 }],
    };
    let dir = tempfile::tempdir().unwrap();
    let wav = dir.path().join("f.wav");
    spec.write_wav(&wav).unwrap();
    // Same samples as raw s16le for the pipe lane.
    let raw = dir.path().join("f.s16le");
    let bytes: Vec<u8> = spec.samples().iter().flat_map(|s| s.to_le_bytes()).collect();
    std::fs::write(&raw, &bytes).unwrap();

    let run = |source: Box<dyn AudioSource>, kws: Vec<KeywordConfig>| async move {
        let detector = Box::new(EnergyStubDetector::for_standard_fixture());
        let table = DecisionTable::from_keywords(&kws);
        tokio::task::spawn_blocking(move || {
            run_pipeline("PARITY", 160, PreprocCfg::default(), source, detector, table, None)
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

    assert!(!replay_report.detections.is_empty(), "fixture must fire the stub detector");
    assert_eq!(
        replay_report.detections.len(),
        pipe_report.detections.len(),
        "pipe lane must detect exactly what replay detects"
    );
    assert_eq!(pipe_report.drops.total(), 0, "unpaced local pipe must not drop");
}
