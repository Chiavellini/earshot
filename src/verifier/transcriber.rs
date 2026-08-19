//! The second opinion: an OFFLINE recognizer over a short PCM window. No
//! hotwords by design — an unbiased decode is the point of a verifier.

use std::path::Path;

use sherpa_onnx::{OfflineRecognizer, OfflineRecognizerConfig};

use crate::detector::sherpa_files::{list_dir, pick_model};

pub trait TailTranscriber: Send {
    fn transcribe(&mut self, pcm: &[i16], rate_hz: u32) -> Result<String, String>;
    fn name(&self) -> &'static str;
}

pub struct SherpaOfflineTranscriber {
    rec: OfflineRecognizer,
    f32_buf: Vec<f32>,
}

impl SherpaOfflineTranscriber {
    /// `model_type`: `nemo_transducer` (Parakeet-TDT / NeMo RNN-T),
    /// `nemo_ctc`, or `transducer` (icefall). Every path is checked in Rust
    /// before sherpa sees it (its C++ exits the process on a missing file).
    pub fn new(model_dir: &Path, model_type: &str, num_threads: u32) -> Result<Self, String> {
        if !model_dir.is_dir() {
            return Err(format!(
                "verifier.model_dir {} is not a directory",
                model_dir.display()
            ));
        }
        let names = list_dir(model_dir)?;
        let pick = |stem: &str| -> Result<String, String> {
            pick_model(&names, stem, true)
                .or_else(|| pick_model(&names, stem, false))
                .map(|n| model_dir.join(n).display().to_string())
                .ok_or_else(|| format!("no {stem}*.onnx in {}", model_dir.display()))
        };
        let tokens = model_dir.join("tokens.txt");
        if !tokens.is_file() {
            return Err(format!("{}: tokens.txt missing", model_dir.display()));
        }
        let mut cfg = OfflineRecognizerConfig::default();
        cfg.feat_config.sample_rate = 16_000;
        cfg.feat_config.feature_dim = 80;
        match model_type {
            "nemo_transducer" | "transducer" => {
                cfg.model_config.transducer.encoder = Some(pick("encoder")?);
                cfg.model_config.transducer.decoder = Some(pick("decoder")?);
                cfg.model_config.transducer.joiner = Some(pick("joiner")?);
                if model_type == "nemo_transducer" {
                    cfg.model_config.model_type = Some("nemo_transducer".into());
                }
            }
            "nemo_ctc" => {
                cfg.model_config.nemo_ctc.model = Some(pick("model")?);
            }
            other => return Err(format!("unknown verifier model_type {other:?}")),
        }
        cfg.model_config.tokens = Some(tokens.display().to_string());
        cfg.model_config.num_threads = num_threads as i32;
        cfg.model_config.provider = Some("cpu".into());
        cfg.decoding_method = Some("greedy_search".into());
        let rec = OfflineRecognizer::create(&cfg).ok_or_else(|| {
            format!(
                "OfflineRecognizer::create failed for {} (see stderr for the C-side error)",
                model_dir.display()
            )
        })?;
        let mut t = SherpaOfflineTranscriber {
            rec,
            f32_buf: Vec::new(),
        };
        // warm-up: page weights in before the first real request
        let _ = t.transcribe(&vec![0i16; 16_000], 16_000);
        Ok(t)
    }
}

/// Silence appended after the tail so an offline TDT/RNN-T decoder has the
/// right-context it needs to EMIT the last word: on HD 26AUG18 the spotter fired
/// ~200 ms after "weather" ended and Parakeet decoded "...given the softer
/// start." — the word was in the window but never flushed (a false veto). Zeros
/// cost no wall-clock, unlike waiting for more live audio.
pub const TAIL_PAD_MS: u32 = 400;

impl TailTranscriber for SherpaOfflineTranscriber {
    fn transcribe(&mut self, pcm: &[i16], rate_hz: u32) -> Result<String, String> {
        self.f32_buf.clear();
        self.f32_buf.extend(pcm.iter().map(|&s| s as f32 / 32768.0));
        self.f32_buf
            .extend(std::iter::repeat(0f32).take((rate_hz as usize * TAIL_PAD_MS as usize) / 1000));
        let stream = self.rec.create_stream();
        stream.accept_waveform(rate_hz as i32, &self.f32_buf);
        self.rec.decode(&stream);
        Ok(stream.get_result().map(|r| r.text).unwrap_or_default())
    }
    fn name(&self) -> &'static str {
        "sherpa_offline"
    }
}

/// Test double: any closure from PCM to text.
pub struct MockTranscriber(pub Box<dyn FnMut(&[i16]) -> Result<String, String> + Send>);

impl TailTranscriber for MockTranscriber {
    fn transcribe(&mut self, pcm: &[i16], _rate_hz: u32) -> Result<String, String> {
        (self.0)(pcm)
    }
    fn name(&self) -> &'static str {
        "mock"
    }
}
