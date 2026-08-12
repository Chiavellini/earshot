#![allow(dead_code)]

#[path = "../src/audio_evidence.rs"]
mod audio_evidence;
#[path = "../src/clock.rs"]
mod clock;
#[path = "../src/types.rs"]
mod types;

use std::fs::OpenOptions;
use std::io::{Read, Seek, SeekFrom, Write};
use std::sync::Arc;
use std::time::Duration;

use audio_evidence::{
    inspect_audio_evidence_raw, replay_audio_evidence, verify_audio_evidence, AudioEvidenceBank,
    AudioEvidenceConfig, AudioEvidenceError, AudioEvidenceIdentity, AudioEvidencePaths,
    AudioEvidenceTapError,
};
use clock::MonotonicNs;
use tempfile::TempDir;
use types::{AudioFrame, SourceId};

fn identity() -> AudioEvidenceIdentity {
    AudioEvidenceIdentity {
        run_id: "run-26aug12-001".into(),
        event_id: "KXEARNINGSMENTIONTEST-26AUG12".into(),
        plan_sha256: "a".repeat(64),
    }
}

fn paths(root: &TempDir, stem: &str) -> AudioEvidencePaths {
    let base = root.path().join(stem);
    AudioEvidencePaths {
        raw: base.join("audio.raw"),
        frames: base.join("audio.frames.jsonl"),
        manifest: base.join("audio.manifest.json"),
        commit: base.join("audio.commit.json"),
        lock: base.join("audio.lock"),
        wav_dir: base.join("wav"),
    }
}

fn config(root: &TempDir, stem: &str) -> AudioEvidenceConfig {
    let mut config = AudioEvidenceConfig::new(identity(), paths(root, stem));
    config.sync_every_frames = 1;
    config.producer_quiescence_timeout = Duration::from_millis(100);
    config.writer_join_timeout = Duration::from_secs(2);
    config
}

fn frame(source: u16, seq: u64, rate: u32, capture: u64, pcm: &[i16]) -> AudioFrame {
    AudioFrame {
        pcm: Arc::from(pcm),
        sample_rate_hz: rate,
        capture_ts: MonotonicNs(capture),
        source: SourceId(source),
        seq,
        preproc_ts: MonotonicNs(capture + 25),
    }
}

fn bank_frames(
    config: AudioEvidenceConfig,
    frames: &[AudioFrame],
) -> (audio_evidence::AudioEvidenceCommit, AudioEvidencePaths) {
    let paths = config.paths.clone();
    let (bank, tap) = AudioEvidenceBank::start_new(config).unwrap();
    for frame in frames {
        tap.try_record_accepted(frame).unwrap();
    }
    drop(tap);
    let commit = bank.finish(frames.len() as u64).unwrap();
    (commit, paths)
}

#[test]
fn exact_pcm_source_sequence_and_timestamps_round_trip() {
    let root = TempDir::new().unwrap();
    let input = vec![
        frame(7, 41, 8_000, 1_000, &[-32_768, -3, 0, 5, 32_767]),
        frame(7, 42, 8_000, 2_000, &[11, 12, 13]),
    ];
    let (commit, paths) = bank_frames(config(&root, "exact"), &input);
    assert!(commit.complete && commit.durable);
    assert_eq!(commit.frame_count, 2);
    assert_eq!(commit.sample_count, 8);

    let replay = replay_audio_evidence(&paths.raw).unwrap();
    assert_eq!(replay.len(), input.len());
    for (index, (actual, expected)) in replay.iter().zip(&input).enumerate() {
        assert_eq!(actual.record_index, index as u64);
        assert_eq!(actual.pcm.as_ref(), expected.pcm.as_ref());
        assert_eq!(actual.sample_rate_hz, expected.sample_rate_hz);
        assert_eq!(actual.capture_ts, expected.capture_ts);
        assert_eq!(actual.preproc_ts, expected.preproc_ts);
        assert_eq!(actual.source, expected.source);
        assert_eq!(actual.seq, expected.seq);
    }
    assert_eq!(
        verify_audio_evidence(&paths.manifest, &paths.commit).unwrap(),
        commit
    );
}

#[test]
fn bounded_full_queue_is_nonblocking_and_poisons_completion() {
    let root = TempDir::new().unwrap();
    let mut cfg = config(&root, "full");
    cfg.queue_capacity = 1;
    cfg.test_writer_start_delay = Some(Duration::from_millis(150));
    let paths = cfg.paths.clone();
    let (bank, tap) = AudioEvidenceBank::start_new(cfg).unwrap();
    tap.try_record_accepted(&frame(1, 0, 8_000, 1_000, &[1, 2]))
        .unwrap();
    let start = std::time::Instant::now();
    assert!(matches!(
        tap.try_record_accepted(&frame(1, 1, 8_000, 2_000, &[3, 4])),
        Err(AudioEvidenceTapError::Full)
    ));
    assert!(start.elapsed() < Duration::from_millis(50));
    let health = tap.health();
    assert!(health.poisoned);
    assert_eq!(health.dropped_frames, 1);
    drop(tap);
    let error = bank.finish(1).unwrap_err();
    assert!(matches!(error, AudioEvidenceError::Incomplete { .. }));
    assert!(!paths.commit.exists());
    let manifest: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&paths.manifest).unwrap()).unwrap();
    assert_eq!(manifest["complete"], false);
    assert_eq!(manifest["status"], "partial");
}

#[test]
fn asynchronous_writer_failure_is_visible_and_never_commits() {
    let root = TempDir::new().unwrap();
    let mut cfg = config(&root, "writer-fail");
    cfg.test_fail_after_frames = Some(1);
    let paths = cfg.paths.clone();
    let (bank, tap) = AudioEvidenceBank::start_new(cfg).unwrap();
    tap.try_record_accepted(&frame(2, 0, 16_000, 10_000, &[1, 2, 3]))
        .unwrap();
    tap.try_record_accepted(&frame(2, 1, 16_000, 20_000, &[4, 5, 6]))
        .unwrap();
    for _ in 0..100 {
        if tap.health().writer_error.is_some() {
            break;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    assert!(tap.health().poisoned);
    assert!(matches!(
        tap.try_record_accepted(&frame(2, 2, 16_000, 30_000, &[7])),
        Err(AudioEvidenceTapError::WriterFailed(_))
    ));
    drop(tap);
    assert!(matches!(
        bank.finish(2),
        Err(AudioEvidenceError::WriterFailed { .. })
    ));
    assert!(!paths.commit.exists());
}

#[test]
fn corruption_and_truncation_are_rejected() {
    let root = TempDir::new().unwrap();
    let input = vec![frame(3, 0, 8_000, 1_000, &[10, 20, 30, 40])];
    let (_, paths) = bank_frames(config(&root, "corrupt"), &input);

    let mut file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(&paths.raw)
        .unwrap();
    let len = file.metadata().unwrap().len();
    file.seek(SeekFrom::Start(len / 2)).unwrap();
    let mut byte = [0u8; 1];
    file.read_exact(&mut byte).unwrap();
    file.seek(SeekFrom::Current(-1)).unwrap();
    byte[0] ^= 0x55;
    file.write_all(&byte).unwrap();
    file.sync_all().unwrap();
    assert!(replay_audio_evidence(&paths.raw).is_err());
    assert!(verify_audio_evidence(&paths.manifest, &paths.commit).is_err());

    let (_, truncate_paths) = bank_frames(
        config(&root, "truncate"),
        &[frame(4, 0, 8_000, 1_000, &[1, 2, 3, 4, 5])],
    );
    let file = OpenOptions::new()
        .write(true)
        .open(&truncate_paths.raw)
        .unwrap();
    let len = file.metadata().unwrap().len();
    file.set_len(len - 9).unwrap();
    assert!(replay_audio_evidence(&truncate_paths.raw).is_err());
    assert!(verify_audio_evidence(&truncate_paths.manifest, &truncate_paths.commit).is_err());
}

#[test]
fn exclusive_lock_blocks_parallel_session_and_partial_restart_cannot_overwrite() {
    let root = TempDir::new().unwrap();
    let first_cfg = config(&root, "lease-a");
    let shared_lock = first_cfg.paths.lock.clone();
    let (bank, tap) = AudioEvidenceBank::start_new(first_cfg.clone()).unwrap();

    let mut parallel_cfg = config(&root, "lease-b");
    parallel_cfg.paths.lock = shared_lock.clone();
    assert!(matches!(
        AudioEvidenceBank::start_new(parallel_cfg.clone()),
        Err(AudioEvidenceError::Busy { .. })
    ));

    drop(tap);
    drop(bank);
    let mut restarted = false;
    for _ in 0..100 {
        match AudioEvidenceBank::start_new(parallel_cfg.clone()) {
            Ok((parallel_bank, parallel_tap)) => {
                drop(parallel_tap);
                drop(parallel_bank);
                restarted = true;
                break;
            }
            Err(AudioEvidenceError::Busy { .. }) => std::thread::sleep(Duration::from_millis(5)),
            Err(other) => panic!("unexpected restart error: {other}"),
        }
    }
    assert!(
        restarted,
        "lease was not released after the first bank dropped"
    );
    let restart = AudioEvidenceBank::start_new(first_cfg);
    assert!(
        matches!(
            restart,
            Err(AudioEvidenceError::ArtifactExists { .. }) | Err(AudioEvidenceError::Busy { .. })
        ),
        "partial original run must never reopen or overwrite"
    );
}

#[test]
fn fanout_rate_and_sequence_discontinuities_make_explicit_wav_segments() {
    let root = TempDir::new().unwrap();
    let mut cfg = config(&root, "segments");
    cfg.source_labels.insert(1, "phone-primary".into());
    cfg.source_labels.insert(2, "webcast-fallback".into());
    let input = vec![
        frame(1, 0, 8_000, 1_000, &[1, 2]),
        frame(2, 10, 16_000, 1_100, &[3, 4, 5]),
        frame(1, 1, 8_000, 2_000, &[6]),
        frame(1, 4, 8_000, 3_000, &[7, 8]),
        frame(1, 5, 16_000, 4_000, &[9, 10, 11]),
        frame(2, 11, 16_000, 4_100, &[12]),
    ];
    let (commit, paths) = bank_frames(cfg, &input);
    assert_eq!(commit.wav_segments.len(), 4);
    let source_one: Vec<_> = commit
        .wav_segments
        .iter()
        .filter(|segment| segment.source == 1)
        .collect();
    assert_eq!(source_one.len(), 3);
    assert_eq!(source_one[0].start_reasons, ["source_start"]);
    assert!(source_one[1].start_reasons[0].starts_with("sequence_discontinuity:"));
    assert!(source_one[2]
        .start_reasons
        .iter()
        .any(|reason| reason.starts_with("sample_rate_change:")));
    assert_eq!(source_one[0].source_label.as_deref(), Some("phone-primary"));
    assert_eq!(
        verify_audio_evidence(&paths.manifest, &paths.commit).unwrap(),
        commit
    );
}

#[test]
fn surviving_tap_prevents_complete_commit() {
    let root = TempDir::new().unwrap();
    let mut cfg = config(&root, "quiescence");
    cfg.producer_quiescence_timeout = Duration::from_millis(20);
    let paths = cfg.paths.clone();
    let (bank, tap) = AudioEvidenceBank::start_new(cfg).unwrap();
    tap.try_record_accepted(&frame(9, 0, 8_000, 1_000, &[1, 2, 3]))
        .unwrap();
    let survivor = tap.clone();
    drop(tap);
    let error = bank.finish(1).unwrap_err();
    let AudioEvidenceError::Incomplete { partial } = error else {
        panic!("expected incomplete evidence");
    };
    assert!(!partial.producer_quiesced);
    assert!(!paths.commit.exists());
    assert!(matches!(
        survivor.try_record_accepted(&frame(9, 1, 8_000, 2_000, &[4])),
        Err(AudioEvidenceTapError::Closed)
    ));
}

#[test]
fn wav_samples_are_bit_exact_with_raw_pcm() {
    let root = TempDir::new().unwrap();
    let samples = [-32_768, -1000, -1, 0, 1, 1000, 32_767];
    let (commit, paths) = bank_frames(
        config(&root, "wav-parity"),
        &[frame(11, 0, 44_100, 5_000, &samples)],
    );
    assert_eq!(commit.wav_segments.len(), 1);
    let mut reader = hound::WavReader::open(&commit.wav_segments[0].path).unwrap();
    assert_eq!(reader.spec().sample_rate, 44_100);
    assert_eq!(
        reader
            .samples::<i16>()
            .map(Result::unwrap)
            .collect::<Vec<_>>(),
        samples
    );
    assert_eq!(
        verify_audio_evidence(&paths.manifest, &paths.commit).unwrap(),
        commit
    );
}

#[test]
fn dropped_bank_leaves_inspectable_but_uncommitted_partial_tape() {
    let root = TempDir::new().unwrap();
    let cfg = config(&root, "crash-partial");
    let paths = cfg.paths.clone();
    let (bank, tap) = AudioEvidenceBank::start_new(cfg).unwrap();
    tap.try_record_accepted(&frame(5, 0, 8_000, 1_000, &[1, 2]))
        .unwrap();
    drop(tap);
    drop(bank);
    for _ in 0..100 {
        if matches!(replay_audio_evidence(&paths.raw), Ok(frames) if frames.len() == 1) {
            break;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    let inspection = inspect_audio_evidence_raw(&paths.raw).unwrap();
    assert_eq!(inspection.frame_count, 1);
    assert!(!inspection.sealed);
    assert!(!paths.manifest.exists());
    assert!(!paths.commit.exists());
}

#[test]
fn explicit_abort_seals_partial_artifacts_without_commit() {
    let root = TempDir::new().unwrap();
    let cfg = config(&root, "abort-partial");
    let paths = cfg.paths.clone();
    let (bank, tap) = AudioEvidenceBank::start_new(cfg).unwrap();
    tap.try_record_accepted(&frame(6, 0, 8_000, 1_000, &[3, 2, 1]))
        .unwrap();
    assert_eq!(bank.health().accepted_frames, 1);
    drop(tap);
    let partial = bank.abort("source_failed").unwrap();
    assert_eq!(partial.reason, "source_failed");
    assert_eq!(partial.accepted_frames, 1);
    assert_eq!(partial.written_frames, 1);
    assert!(partial.manifest_written);
    assert!(inspect_audio_evidence_raw(&paths.raw).unwrap().sealed);
    assert!(paths.manifest.exists());
    assert!(!paths.commit.exists());
    assert!(verify_audio_evidence(&paths.manifest, &paths.commit).is_err());
}

#[test]
fn invalid_frame_is_rejected_and_health_is_poisoned() {
    let root = TempDir::new().unwrap();
    let cfg = config(&root, "invalid");
    let paths = cfg.paths.clone();
    let (bank, tap) = AudioEvidenceBank::start_new(cfg).unwrap();
    let invalid = AudioFrame {
        pcm: Arc::from([1i16, 2]),
        sample_rate_hz: 8_000,
        capture_ts: MonotonicNs(10),
        source: SourceId(1),
        seq: 0,
        preproc_ts: MonotonicNs(9),
    };
    assert!(matches!(
        tap.try_record_accepted(&invalid),
        Err(AudioEvidenceTapError::InvalidFrame(_))
    ));
    assert_eq!(tap.health().invalid_attempts, 1);
    drop(tap);
    assert!(matches!(
        bank.finish(0),
        Err(AudioEvidenceError::Incomplete { .. })
    ));
    assert!(!paths.commit.exists());
}
