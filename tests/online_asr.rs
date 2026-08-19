//! online_asr engine integration tests against a sherpa streaming transducer's
//! bundled test_wavs (16 kHz, known transcripts). Requires the model dir on
//! disk — SKIPs loudly when absent (run scripts/fetch-models.sh); panics under
//! EARSHOT_REQUIRE_MODELS.
//!
//! Pins exercised here (VERIFICATIONS.md, detection-accuracy program D1):
//!   P1 hotwords buffer accepted by OnlineRecognizer::create (no C-side exit)
//!   P3 leading silence does not shift the fed-counter position (it is ours)
//!   P6 endpointing with the configured rules resets and does not re-fire

use std::path::{Path, PathBuf};
use std::sync::Arc;

use earshot::clock::MonotonicNs;
use earshot::config::{KeywordConfig, OnlineAsrConfig};
use earshot::detector::online_asr::OnlineAsrDetector;
use earshot::detector::Detector;
use earshot::types::{AudioChunk, Detection, SourceId, Tier};

const MODEL_DIR: &str = "models/sherpa-onnx-streaming-zipformer-en-2023-06-26";

fn model_dir() -> Option<PathBuf> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join(MODEL_DIR);
    if dir.exists() {
        Some(dir)
    } else if std::env::var_os("EARSHOT_REQUIRE_MODELS").is_some() {
        panic!("EARSHOT_REQUIRE_MODELS set but {MODEL_DIR} missing — run scripts/fetch-models.sh");
    } else {
        eprintln!("==== SKIP online_asr integration: {MODEL_DIR} missing — run scripts/fetch-models.sh ====");
        None
    }
}

fn keyword(phrase: &str, variants: &[&str]) -> KeywordConfig {
    let toml = format!(
        r#"
market_ticker    = "KX-{}"
phrase           = "{phrase}"
variants         = [{}]
max_price_cents  = 97
budget_contracts = 1
policy           = "confirmed_only"
"#,
        phrase.to_uppercase().replace(' ', "-"),
        variants
            .iter()
            .map(|v| format!("{v:?}"))
            .collect::<Vec<_>>()
            .join(", "),
    );
    toml::from_str(&toml).unwrap()
}

fn cfg(dir: &Path, extra: &str) -> OnlineAsrConfig {
    toml::from_str(&format!(
        "model_dir = {:?}\n{extra}",
        dir.display().to_string()
    ))
    .unwrap()
}

fn read_wav_16k(path: &Path) -> Vec<i16> {
    let mut reader = hound::WavReader::open(path).unwrap();
    assert_eq!(reader.spec().sample_rate, 16_000);
    reader.samples::<i16>().map(|s| s.unwrap()).collect()
}

fn run_detector(det: &mut OnlineAsrDetector, pcm: &[i16]) -> Vec<Detection> {
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

/// 0.wav: "AFTER EARLY NIGHTFALL THE YELLOW LAMPS WOULD LIGHT UP HERE AND THERE
/// THE SQUALID QUARTER OF THE BROTHELS". Each keyword fires exactly once,
/// Confirmed, at a sane stream position; absent keywords stay quiet.
#[test]
fn known_words_fire_once_absent_words_never() {
    let Some(dir) = model_dir() else { return };
    let kws = [
        keyword("quarter", &["quarter", "quarters"]),
        keyword("yellow lamps", &["yellow lamps"]),
        keyword("tariff", &["tariff", "tariffs"]),
        keyword("nightfall", &["nightfall", "night fall"]),
    ];
    let mut det = OnlineAsrDetector::new(&cfg(&dir, ""), &kws).unwrap();
    assert!(
        det.facts.hotwords_active,
        "transducer + mbs + bpe.model ⇒ hotwords on: {:?}",
        det.facts
    );
    let pcm = read_wav_16k(&dir.join("test_wavs/0.wav"));
    let secs = pcm.len() as f64 / 16_000.0;
    let dets = run_detector(&mut det, &pcm);
    let by = |k: u16| dets.iter().filter(|d| d.keyword.0 == k).collect::<Vec<_>>();
    assert_eq!(by(0).len(), 1, "quarter once: {dets:?}");
    assert_eq!(by(1).len(), 1, "yellow lamps once: {dets:?}");
    assert_eq!(by(2).len(), 0, "tariff is not in the wav: {dets:?}");
    assert_eq!(by(3).len(), 1, "nightfall once: {dets:?}");
    for d in &dets {
        assert_eq!(d.tier, Tier::Confirmed);
        assert_eq!(d.audio_pos_ms, d.audio_consumed_ms);
        assert!(d.audio_pos_ms as f64 / 1000.0 <= secs + 0.2, "{d:?}");
        assert!(d.confidence == 1.0);
    }
    // order on the timeline: nightfall < yellow lamps < quarter
    assert!(
        by(3)[0].audio_pos_ms < by(1)[0].audio_pos_ms
            && by(1)[0].audio_pos_ms < by(0)[0].audio_pos_ms,
        "{dets:?}"
    );
}

/// Greedy search, no hotwords, still hears the plain words (the matcher does
/// not depend on biasing) — and refuses nothing at build time.
#[test]
fn greedy_without_hotwords_still_matches() {
    let Some(dir) = model_dir() else { return };
    let kws = [keyword("quarter", &["quarter"])];
    let mut det = OnlineAsrDetector::new(
        &cfg(
            &dir,
            "decoding_method = \"greedy_search\"\nhotwords = false\n",
        ),
        &kws,
    )
    .unwrap();
    assert!(!det.facts.hotwords_active);
    let dets = run_detector(&mut det, &read_wav_16k(&dir.join("test_wavs/0.wav")));
    assert_eq!(dets.len(), 1, "{dets:?}");
}

/// P3: 5 s of leading silence must shift the reported position by exactly the
/// silence (fed counter), never by a decoder-internal rebase. P6: silence long
/// enough to endpoint between two copies of the wav resets the stream, and the
/// second copy fires again (refractory long expired), no double fire.
#[test]
fn leading_silence_and_endpoint_reset_behave() {
    let Some(dir) = model_dir() else { return };
    let kws = [keyword("quarter", &["quarter"])];
    let wav = read_wav_16k(&dir.join("test_wavs/0.wav"));
    let mut det = OnlineAsrDetector::new(&cfg(&dir, ""), &kws).unwrap();
    let base = run_detector(&mut det, &wav);
    assert_eq!(base.len(), 1);

    let mut det = OnlineAsrDetector::new(&cfg(&dir, ""), &kws).unwrap();
    let mut pcm = vec![0i16; 16_000 * 5];
    pcm.extend_from_slice(&wav);
    let shifted = run_detector(&mut det, &pcm);
    assert_eq!(shifted.len(), 1, "{shifted:?}");
    let delta = shifted[0].audio_pos_ms as i64 - base[0].audio_pos_ms as i64;
    assert!(
        (delta - 5000).abs() <= 320,
        "leading silence shifted the position by {delta} ms, want ~5000"
    );

    let mut det = OnlineAsrDetector::new(&cfg(&dir, ""), &kws).unwrap();
    let mut pcm = wav.clone();
    pcm.extend(std::iter::repeat(0i16).take(16_000 * 4)); // > rule1 2.4 s ⇒ endpoint
    pcm.extend_from_slice(&wav);
    let twice = run_detector(&mut det, &pcm);
    assert_eq!(
        twice.len(),
        2,
        "each copy fires once across the endpoint reset: {twice:?}"
    );
}
