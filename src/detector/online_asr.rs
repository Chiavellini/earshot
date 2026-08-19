//! `online_asr` — any sherpa-onnx streaming ASR model behind the sync
//! `Detector` trait, matching keywords on the emitted word stream.
//!
//! WHY (2026-08-18, detection-accuracy program D1). The production spotter
//! (`streaming_ctc`) scores compiled BPE pieces on a 23.8 M-param LibriSpeech
//! model whose confidence does not separate right from wrong fires
//! (`docs/layers/detector.md`). The linked `sherpa-onnx` crate already drives
//! every sherpa streaming family — zipformer transducers, NeMo cache-aware
//! conformers (80/480/1040 ms), zipformer2-CTC — through one `OnlineRecognizer`
//! with `modified_beam_search` + hotword biasing. This engine turns any of them
//! into a `Detector` so `bench-detector --engines online_asr --model-dir a,b`
//! can sweep candidates interleaved on the same corpus.
//!
//! What it does per chunk: feed PCM → decode to exhaustion → rebuild words from
//! the partial's `▁`-marked tokens (`text_match::words_from_tokens`) → scan for
//! COMPLETE-word matches of any keyword variant past a watermark → emit one
//! `Confirmed` detection per match, deduped by the refractory table. On an
//! endpoint the trailing word is completed, matched, and the stream is `reset`
//! (encoder state survives; the decoder result and its counters restart).
//!
//! Positions: `audio_pos_ms` = fed-sample counter at the end of the chunk in
//! which the match surfaced (== `audio_consumed_ms`, exactly like
//! `sherpa_kws`). sherpa's own token timestamps are NOT trusted for position —
//! they rebase around silence (VERIFICATIONS M1 pin 3) and around our own
//! `reset` — but they are logged at debug level so a corpus study can pin
//! `sherpa_word_end − manifest.end` and promote them later.
//!
//! Confidence: the safe crate exposes no posteriors, so v1 emits the 1.0
//! sentinel like `sherpa_kws` (read alongside `engine_name`). Do not gate on it.
//! Hotwords: transducer + `modified_beam_search` + `modeling_unit = "bpe"` +
//! `bpe_vocab`; every file is validated in Rust first because sherpa's C++
//! `exit(-1)`s inside the FFI on a bad hotword config.

use sherpa_onnx::{OnlineRecognizer, OnlineRecognizerConfig, OnlineStream};
use smallvec::SmallVec;

use super::factory::DetectorBuildError;
use super::kws_compile::TokenTable;
use super::sherpa_files::{discover_online, ensure_bpe_vocab, DiscoveredOnline, OnlineModelFiles};
use super::text_match::{normalize_phrase, words_from_tokens, VariantMatcher};
use super::Detector;
use crate::clock::MonotonicNs;
use crate::config::{KeywordConfig, OnlineAsrConfig};
use crate::types::{AudioChunk, Detection, KeywordId, Tier};

const EXPECTED_RATE_HZ: u32 = 16_000;

pub struct OnlineAsrDetector {
    recognizer: OnlineRecognizer,
    stream: OnlineStream,
    matcher: VariantMatcher,
    /// Word index up to which the current segment has been scanned; matches must
    /// END at or after it, so a growing partial is scanned incrementally without
    /// re-emitting.
    watermark_word: usize,
    samples_fed: u64,
    last_fire_ms: Vec<Option<u64>>,
    refractory_ms: u64,
    f32_buf: Vec<f32>,
    /// Build facts, surfaced in the arm log and by `describe()`.
    pub facts: BuildFacts,
}

#[derive(Debug, Clone)]
pub struct BuildFacts {
    pub model_kind: &'static str,
    pub hotwords_active: bool,
    pub hotword_lines: usize,
    pub decoding_method: String,
    pub uppercase_vocab: bool,
}

/// One phrase per line, `TEXT :score`, in the vocabulary's case. Every keyword
/// variant becomes a hotword; a keyword `boost` overrides `default_score`.
pub fn render_hotwords(keywords: &[KeywordConfig], default_score: f32, uppercase: bool) -> String {
    let mut out = String::new();
    for k in keywords {
        let score = k.boost.unwrap_or(default_score);
        let mut variants: Vec<&String> = k.variants.iter().collect();
        if variants.is_empty() {
            variants.push(&k.phrase);
        }
        for v in variants {
            let words = normalize_phrase(v);
            if words.is_empty() {
                continue;
            }
            let text = words.join(" ");
            let text = if uppercase { text.to_uppercase() } else { text };
            out.push_str(&format!("{text} :{score:.2}\n"));
        }
    }
    out
}

impl OnlineAsrDetector {
    pub fn new(
        cfg: &OnlineAsrConfig,
        keywords: &[KeywordConfig],
    ) -> Result<Self, DetectorBuildError> {
        let err = |m: String| DetectorBuildError::OnlineAsr(m);
        let int8 = cfg.precision == "int8";
        let disc: DiscoveredOnline =
            discover_online(&cfg.model_dir, &cfg.model_type, int8).map_err(err)?;

        let mut rc = OnlineRecognizerConfig::default();
        rc.feat_config.sample_rate = EXPECTED_RATE_HZ as i32;
        rc.feat_config.feature_dim = 80;
        match &disc.files {
            OnlineModelFiles::Transducer {
                encoder,
                decoder,
                joiner,
            } => {
                rc.model_config.transducer.encoder = Some(encoder.display().to_string());
                rc.model_config.transducer.decoder = Some(decoder.display().to_string());
                rc.model_config.transducer.joiner = Some(joiner.display().to_string());
            }
            OnlineModelFiles::Zipformer2Ctc { model } => {
                rc.model_config.zipformer2_ctc.model = Some(model.display().to_string());
            }
            OnlineModelFiles::NemoCtc { model } => {
                rc.model_config.nemo_ctc.model = Some(model.display().to_string());
            }
        }
        rc.model_config.tokens = Some(disc.tokens.display().to_string());
        rc.model_config.num_threads = cfg.num_threads as i32;
        rc.model_config.provider = Some("cpu".into());
        rc.decoding_method = Some(cfg.decoding_method.clone());
        rc.max_active_paths = cfg.max_active_paths as i32;
        rc.enable_endpoint = true;
        rc.rule1_min_trailing_silence = cfg.endpoint_rule1_ms as f32 / 1000.0;
        rc.rule2_min_trailing_silence = cfg.endpoint_rule2_ms as f32 / 1000.0;
        rc.rule3_min_utterance_length = cfg.endpoint_rule3_ms as f32 / 1000.0;
        rc.blank_penalty = cfg.blank_penalty;

        // Hotwords: only the transducer beam search consults them, and sherpa
        // needs modeling_unit=bpe + bpe_vocab for BPE vocabularies. Resolve every
        // precondition here; never let the C++ discover a missing file.
        let mut hotwords_active = false;
        let mut hotword_lines = 0usize;
        if cfg.hotwords
            && disc.files.is_transducer()
            && cfg.decoding_method == "modified_beam_search"
        {
            let vocab = match (&disc.bpe_vocab, &disc.bpe_model) {
                (Some(v), _) => Some(v.clone()),
                (None, Some(m)) => {
                    let table = TokenTable::from_bpe_model(m).map_err(|e| err(e.to_string()))?;
                    Some(ensure_bpe_vocab(&cfg.model_dir, &table).map_err(err)?)
                }
                (None, None) => None,
            };
            match vocab {
                Some(v) => {
                    let buf = render_hotwords(keywords, cfg.hotwords_score, disc.uppercase_vocab);
                    hotword_lines = buf.lines().count();
                    if hotword_lines > 0 {
                        rc.model_config.modeling_unit = Some("bpe".into());
                        rc.model_config.bpe_vocab = Some(v.display().to_string());
                        rc.hotwords_score = cfg.hotwords_score;
                        rc.hotwords_buf = Some(buf.into_bytes());
                        hotwords_active = true;
                    }
                }
                None => tracing::warn!(
                    "online_asr: hotwords requested but {} ships neither bpe.vocab nor bpe.model — running unbiased",
                    cfg.model_dir.display()
                ),
            }
        } else if cfg.hotwords {
            tracing::warn!(
                "online_asr: hotwords requested but model kind {} / decoding {} cannot use them — running unbiased",
                disc.files.kind(),
                cfg.decoding_method
            );
        }

        let recognizer = OnlineRecognizer::create(&rc).ok_or_else(|| {
            err(format!(
                "OnlineRecognizer::create failed for {} (see stderr for the C-side error)",
                cfg.model_dir.display()
            ))
        })?;
        let stream = recognizer.create_stream();

        // Warm-up on a THROWAWAY stream — the real stream must never see synthetic
        // silence (leading silence corrupts the decoder's clock; sherpa_kws docs).
        let throwaway = recognizer.create_stream();
        throwaway.accept_waveform(
            EXPECTED_RATE_HZ as i32,
            &vec![0f32; 2 * EXPECTED_RATE_HZ as usize],
        );
        while recognizer.is_ready(&throwaway) {
            recognizer.decode(&throwaway);
        }

        let facts = BuildFacts {
            model_kind: disc.files.kind(),
            hotwords_active,
            hotword_lines,
            decoding_method: cfg.decoding_method.clone(),
            uppercase_vocab: disc.uppercase_vocab,
        };
        tracing::info!(
            model_dir = %cfg.model_dir.display(),
            kind = facts.model_kind,
            decoding = %facts.decoding_method,
            hotwords_active = facts.hotwords_active,
            hotword_lines = facts.hotword_lines,
            "online_asr detector built"
        );
        Ok(OnlineAsrDetector {
            recognizer,
            stream,
            matcher: VariantMatcher::from_keywords(keywords),
            watermark_word: 0,
            samples_fed: 0,
            last_fire_ms: vec![None; keywords.len()],
            refractory_ms: cfg.refractory_ms,
            f32_buf: Vec::new(),
            facts,
        })
    }

    /// Scan the current partial for new complete-word matches. Returns keyword ids
    /// (dedupe applied by the caller) and advances the watermark.
    fn scan_partial(&mut self, at_endpoint: bool, pos_ms: u64) -> SmallVec<[KeywordId; 4]> {
        let mut hits: SmallVec<[KeywordId; 4]> = SmallVec::new();
        let Some(res) = self.recognizer.get_result(&self.stream) else {
            return hits;
        };
        if res.tokens.is_empty() {
            return hits;
        }
        let words = words_from_tokens(&res.tokens, at_endpoint);
        if words.is_empty() {
            return hits;
        }
        let matches = self.matcher.scan(&words, self.watermark_word);
        let mut new_watermark = self.watermark_word;
        for m in matches.iter() {
            hits.push(m.keyword);
            if tracing::enabled!(tracing::Level::DEBUG) {
                let lo = m.first_word.saturating_sub(4);
                let ctx: Vec<&str> = words[lo..=m.last_word]
                    .iter()
                    .map(|w| w.text.as_str())
                    .collect();
                let sherpa_end_ms = match (&res.timestamps, res.start_time) {
                    (Some(ts), Some(st)) => ts
                        .get(words[m.last_word].last_tok)
                        .map(|t| ((st + t) * 1000.0) as u64),
                    _ => None,
                };
                tracing::debug!(
                    keyword = m.keyword.0,
                    pos_ms,
                    ?sherpa_end_ms,
                    words = words.len(),
                    watermark = self.watermark_word,
                    context = %ctx.join(" "),
                    "online_asr match (sherpa timestamp is diagnostic only)"
                );
            }
            new_watermark = new_watermark.max(m.last_word + 1);
        }
        // Complete words with no match may also be skipped: everything strictly
        // before the last complete word can never start a new match ending later
        // — except as the leading words of a multi-word variant, so keep a margin
        // of (max variant length − 1). We simply keep the watermark at the last
        // match end; rescanning a few completed words costs nothing.
        self.watermark_word = new_watermark;
        hits
    }
}

impl Detector for OnlineAsrDetector {
    fn engine_name(&self) -> &'static str {
        "online_asr"
    }

    fn process(&mut self, chunk: &AudioChunk) -> SmallVec<[Detection; 2]> {
        assert_eq!(
            chunk.sample_rate_hz, EXPECTED_RATE_HZ,
            "online_asr requires 16 kHz input — set pipeline.resample_to_hz = 16000"
        );
        self.f32_buf.clear();
        self.f32_buf
            .extend(chunk.pcm.iter().map(|&s| s as f32 / 32768.0));
        self.stream
            .accept_waveform(EXPECTED_RATE_HZ as i32, &self.f32_buf);
        self.samples_fed += chunk.pcm.len() as u64;
        let pos_ms = self.samples_fed * 1000 / EXPECTED_RATE_HZ as u64;

        let mut decoded = false;
        while self.recognizer.is_ready(&self.stream) {
            self.recognizer.decode(&self.stream);
            decoded = true;
        }
        let mut hits: SmallVec<[KeywordId; 4]> = SmallVec::new();
        if decoded {
            hits.extend(self.scan_partial(false, pos_ms));
            if self.recognizer.is_endpoint(&self.stream) {
                hits.extend(self.scan_partial(true, pos_ms));
                self.recognizer.reset(&self.stream);
                self.watermark_word = 0;
            }
        }

        let mut out = SmallVec::new();
        for kw in hits {
            let slot = &mut self.last_fire_ms[kw.0 as usize];
            if let Some(last) = *slot {
                if pos_ms.saturating_sub(last) < self.refractory_ms {
                    continue;
                }
            }
            *slot = Some(pos_ms);
            out.push(Detection {
                keyword: kw,
                // The safe crate exposes no posteriors: sentinel, like sherpa_kws.
                confidence: 1.0,
                tier: Tier::Confirmed,
                audio_ts: chunk.first_capture_ts,
                emit_ts: MonotonicNs::now(),
                audio_pos_ms: pos_ms,
                audio_consumed_ms: pos_ms,
                source: chunk.source,
            });
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Policy;

    fn kw(phrase: &str, variants: &[&str], boost: Option<f32>) -> KeywordConfig {
        KeywordConfig {
            market_ticker: format!("T-{}", phrase.to_uppercase()),
            phrase: phrase.into(),
            variants: variants.iter().map(|s| s.to_string()).collect(),
            max_price_cents: 1,
            budget_contracts: 1,
            policy: Policy::ConfirmedOnly,
            rule_risk_haircut_cents: 0,
            fast_prefixes: vec![],
            fast_threshold: None,
            confirmed_threshold: None,
            boost,
            min_confidence_fast: 0.0,
            min_confidence_confirmed: 0.0,
        }
    }

    #[test]
    fn hotwords_render_in_vocab_case_with_scores() {
        let kws = [
            kw("tariff", &["tariff", "tariffs"], None),
            kw("apple pay", &["Apple-Pay"], Some(2.5)),
        ];
        let up = render_hotwords(&kws, 1.5, true);
        assert_eq!(up, "TARIFF :1.50\nTARIFFS :1.50\nAPPLE PAY :2.50\n");
        let lo = render_hotwords(&kws, 1.5, false);
        assert!(lo.starts_with("tariff :1.50\n"));
        assert!(lo.contains("apple pay :2.50\n"));
    }
}
