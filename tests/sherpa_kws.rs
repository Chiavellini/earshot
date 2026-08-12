//! sherpa_kws engine integration tests against the model's bundled test_wavs
//! (16 kHz, known transcripts). Requires models/ on disk — SKIPs loudly when
//! absent so CI without models stays green (run scripts/fetch-models.sh).

use std::path::{Path, PathBuf};
use std::sync::Arc;

use earshot::clock::MonotonicNs;
use earshot::config::{KeywordConfig, SherpaKwsConfig};
use earshot::detector::sherpa_kws::SherpaKwsDetector;
use earshot::detector::Detector;
use earshot::types::{AudioChunk, Detection, SourceId, Tier};

const MODEL_DIR: &str = "models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01";

fn model_dir() -> Option<PathBuf> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join(MODEL_DIR);
    if dir.exists() {
        Some(dir)
    } else if std::env::var_os("EARSHOT_REQUIRE_MODELS").is_some() {
        panic!("EARSHOT_REQUIRE_MODELS set but {MODEL_DIR} missing — run scripts/fetch-models.sh");
    } else {
        eprintln!("==== SKIP sherpa_kws integration: {MODEL_DIR} missing — run scripts/fetch-models.sh ====");
        None
    }
}

fn keyword(phrase: &str, variants: &[&str], prefixes: &[&str]) -> KeywordConfig {
    keyword_with_threshold(phrase, variants, prefixes, 0.18)
}

fn keyword_with_threshold(
    phrase: &str,
    variants: &[&str],
    prefixes: &[&str],
    fast_threshold: f32,
) -> KeywordConfig {
    let toml = format!(
        r#"
market_ticker    = "KX-TEST"
phrase           = "{phrase}"
variants         = [{}]
max_price_cents  = 97
budget_contracts = 200
policy           = "two_tier"
fast_prefixes    = [{}]
fast_threshold   = {fast_threshold}
"#,
        variants
            .iter()
            .map(|v| format!("{v:?}"))
            .collect::<Vec<_>>()
            .join(", "),
        prefixes
            .iter()
            .map(|p| format!("{p:?}"))
            .collect::<Vec<_>>()
            .join(", "),
    );
    toml::from_str(&toml).unwrap()
}

fn sherpa_cfg(dir: &Path) -> SherpaKwsConfig {
    toml::from_str(&format!("model_dir = {:?}", dir.display().to_string())).unwrap()
}

fn read_wav_16k(path: &Path) -> Vec<i16> {
    let mut reader = hound::WavReader::open(path).unwrap();
    assert_eq!(reader.spec().sample_rate, 16_000);
    reader.samples::<i16>().map(|s| s.unwrap()).collect()
}

/// Feed a wav through the detector in 160 ms chunks, as the pipeline would.
fn run_detector(det: &mut SherpaKwsDetector, pcm: &[i16]) -> Vec<Detection> {
    let mut out = Vec::new();
    let chunk_samples = 16_000 * 160 / 1000;
    for (seq, window) in pcm.chunks(chunk_samples).enumerate() {
        let now = MonotonicNs::now();
        let chunk = AudioChunk {
            pcm: Arc::from(window),
            sample_rate_hz: 16_000,
            first_capture_ts: now,
            last_capture_ts: now,
            first_preproc_ts: now,
            chunk_ts: now,
            source: SourceId(0),
            seq: seq as u64,
        };
        out.extend(det.process(&chunk));
    }
    out
}

/// 0.wav: "… THE YELLOW LAMPS WOULD LIGHT UP HERE AND THERE …" — the phrase
/// "light up" sits at ≈3.0–3.2 s. Both tiers must fire, Fast strictly first,
/// with sane stream positions.
#[test]
fn fast_and_confirmed_both_fire_fast_first() {
    let Some(dir) = model_dir() else { return };
    let mut det = SherpaKwsDetector::new(
        &sherpa_cfg(&dir),
        &[keyword("light up", &["light up"], &["ligh"])],
    )
    .unwrap();

    let pcm = read_wav_16k(&dir.join("test_wavs/0.wav"));
    let dets = run_detector(&mut det, &pcm);

    let fast: Vec<&Detection> = dets.iter().filter(|d| d.tier == Tier::Fast).collect();
    let conf: Vec<&Detection> = dets.iter().filter(|d| d.tier == Tier::Confirmed).collect();
    assert_eq!(fast.len(), 1, "expected exactly one Fast hit, got {dets:?}");
    assert_eq!(
        conf.len(),
        1,
        "expected exactly one Confirmed hit, got {dets:?}"
    );
    assert_eq!(fast[0].keyword.0, 0);

    // The prefix must become actionable no later than the full phrase (equal
    // when both surface within the same 160 ms chunk).
    assert!(
        fast[0].emit_ts <= conf[0].emit_ts,
        "Fast emitted after Confirmed: {dets:?}"
    );
    assert!(
        fast[0].audio_pos_ms <= conf[0].audio_pos_ms,
        "Fast stream position after Confirmed: {dets:?}"
    );
    // Ground truth ≈3.0–3.2 s; audio_pos_ms marks the chunk where the hit
    // surfaced = word end + decode lag (~0.4 s).
    for d in &dets {
        assert!(
            (2_800..4_500).contains(&d.audio_pos_ms),
            "implausible audio_pos_ms: {dets:?}"
        );
    }
}

/// 1.wav has "LOVELY CHILD" (~5.4–6.0 s) and "FOR EVER" (~10.9–11.0 s):
/// two keywords, independent hits, positions in order.
#[test]
fn multiple_keywords_fire_independently() {
    let Some(dir) = model_dir() else { return };
    // NB: shorter prefixes need lower thresholds — ▁LOVE LY ▁CHI fires at
    // 0.05 but not 0.18, and a 1-token prefix (▁LOVE) never fires at any
    // threshold tried (VERIFICATIONS.md M1 pin). This interaction is the
    // per-keyword tuning dial of the bake-off.
    let mut det = SherpaKwsDetector::new(
        &sherpa_cfg(&dir),
        &[
            keyword_with_threshold("lovely child", &["lovely child"], &["lovely chi"], 0.05),
            keyword("forever", &["for ever"], &[]),
        ],
    )
    .unwrap();

    let pcm = read_wav_16k(&dir.join("test_wavs/1.wav"));
    let dets = run_detector(&mut det, &pcm);

    let kw0: Vec<&Detection> = dets.iter().filter(|d| d.keyword.0 == 0).collect();
    let kw1: Vec<&Detection> = dets.iter().filter(|d| d.keyword.0 == 1).collect();
    assert!(
        kw0.iter().any(|d| d.tier == Tier::Fast) && kw0.iter().any(|d| d.tier == Tier::Confirmed),
        "lovely child needs both tiers: {dets:?}"
    );
    assert_eq!(
        kw1.len(),
        1,
        "for ever fires once (no fast prefix): {dets:?}"
    );
    assert_eq!(kw1[0].tier, Tier::Confirmed);
    // "for ever" (~11 s) sits after "lovely child" (~6 s) on the timeline.
    assert!(kw1[0].audio_pos_ms > kw0[0].audio_pos_ms + 3_000);
}

/// Silence must never fire, and a detector armed with a keyword absent from
/// the audio must stay quiet (false-alarm floor of the self-test kind).
#[test]
fn silence_and_absent_keywords_stay_quiet() {
    let Some(dir) = model_dir() else { return };
    let mut det = SherpaKwsDetector::new(
        &sherpa_cfg(&dir),
        &[keyword(
            "recession",
            &["recession", "recessions"],
            &["reces"],
        )],
    )
    .unwrap();

    let dets = run_detector(&mut det, &vec![0i16; 16_000 * 5]);
    assert!(dets.is_empty(), "silence fired: {dets:?}");

    let pcm = read_wav_16k(&dir.join("test_wavs/0.wav"));
    let dets = run_detector(&mut det, &pcm);
    assert!(
        dets.is_empty(),
        "'recession' fired on audio that never says it: {dets:?}"
    );
}
