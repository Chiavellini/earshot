//! sherpa-onnx streaming keyword spotter behind the sync `Detector` trait
//! (spec §5.4). TWO spotters — one holding only Fast (prefix) entries, one
//! holding only Confirmed (full) entries:
//!
//! Within a single decoder, a Fast entry and its Confirmed entry are mutually
//! exclusive per utterance — whichever path wins fires and the other never
//! does. And `create_stream_with_keywords` silently ignores its per-stream
//! list (both pinned empirically, VERIFICATIONS.md M1), so per-tier isolation
//! requires two full spotters, each with a config-level keyword list. The
//! 3.3M int8 model decodes a 160 ms chunk in low single-digit ms and its
//! weights are a few MB, so 2× spotter stays far inside the chunk budget.
//!
//! No stream resets: keywords re-fire without reset — the refractory table is
//! the dedupe, matching the detector contract.
//!
//! `audio_pos_ms` comes from our own fed-sample counter, NOT from sherpa's
//! per-token timestamps: those rebase unpredictably around leading/decoded
//! silence (pinned empirically — 5 s of leading silence shifted reported
//! times by 4.8 s; see VERIFICATIONS.md M1). The counter position marks the
//! chunk in which the hit surfaced, trailing the true word end by the decode
//! lag (~0.4 s with chunk-16-left-64) — well inside bench matching windows.

use std::collections::HashMap;

use sherpa_onnx::{KeywordSpotter, KeywordSpotterConfig, OnlineStream};
use smallvec::SmallVec;

use super::factory::DetectorBuildError;
use super::kws_compile::{compile, render_keywords, CompiledEntry, TokenTable};
use super::Detector;
use crate::clock::MonotonicNs;
use crate::config::{KeywordConfig, SherpaKwsConfig};
use crate::types::{AudioChunk, Detection, KeywordId, Tier};

const EXPECTED_RATE_HZ: u32 = 16_000;

/// One spotter + its stream, holding a single tier's keyword entries.
struct TierEngine {
    spotter: KeywordSpotter,
    stream: OnlineStream,
}

pub struct SherpaKwsDetector {
    /// `None` when no keyword configures `fast_prefixes`.
    fast: Option<TierEngine>,
    conf: TierEngine,
    label_map: HashMap<String, (KeywordId, Tier)>,
    /// Last fire per (keyword, tier) on the stream timeline; dedupe window.
    last_fire_ms: Vec<[Option<u64>; 2]>,
    refractory_ms: u64,
    samples_fed: u64,
    f32_buf: Vec<f32>,
}

impl SherpaKwsDetector {
    pub fn new(
        cfg: &SherpaKwsConfig,
        keywords: &[KeywordConfig],
    ) -> Result<Self, DetectorBuildError> {
        let dir = &cfg.model_dir;
        let table = TokenTable::from_bpe_model(&dir.join("bpe.model"))
            .map_err(|e| DetectorBuildError::SherpaKws(e.to_string()))?;
        let entries = compile(keywords, &table, cfg.default_threshold, cfg.default_boost)
            .map_err(|e| DetectorBuildError::SherpaKws(e.to_string()))?;
        let fast: Vec<&CompiledEntry> = entries.iter().filter(|e| e.tier == Tier::Fast).collect();
        let conf: Vec<&CompiledEntry> = entries
            .iter()
            .filter(|e| e.tier == Tier::Confirmed)
            .collect();
        let label_map: HashMap<String, (KeywordId, Tier)> = entries
            .iter()
            .map(|e| (e.label.clone(), (e.keyword, e.tier)))
            .collect();

        let conf_engine = TierEngine::create(cfg, &render_owned(&conf))?;
        let fast_engine = if fast.is_empty() {
            None
        } else {
            Some(TierEngine::create(cfg, &render_owned(&fast))?)
        };

        Ok(SherpaKwsDetector {
            fast: fast_engine,
            conf: conf_engine,
            label_map,
            last_fire_ms: vec![[None; 2]; keywords.len()],
            refractory_ms: cfg.refractory_ms,
            samples_fed: 0,
            f32_buf: Vec::new(),
        })
    }

    fn engines(&self) -> impl Iterator<Item = &TierEngine> {
        self.fast.iter().chain(std::iter::once(&self.conf))
    }

    /// Decode both engines to exhaustion; `on_hit(label, pos_ms)` per result,
    /// where `pos_ms` is the fed-sample position of the current chunk's end.
    fn decode_all(&self, mut on_hit: impl FnMut(&str, u64)) {
        let pos_ms = self.samples_fed * 1000 / EXPECTED_RATE_HZ as u64;
        for engine in self.engines() {
            while engine.spotter.is_ready(&engine.stream) {
                engine.spotter.decode(&engine.stream);
                if let Some(r) = engine.spotter.get_result(&engine.stream) {
                    if !r.keyword.is_empty() {
                        on_hit(&r.keyword, pos_ms);
                    }
                }
            }
        }
    }
}

impl TierEngine {
    fn create(cfg: &SherpaKwsConfig, keywords_buf: &str) -> Result<TierEngine, DetectorBuildError> {
        let dir = &cfg.model_dir;
        let find_model = |stem: &str, int8: bool| -> Result<String, DetectorBuildError> {
            let entries = std::fs::read_dir(dir).map_err(|e| {
                DetectorBuildError::SherpaKws(format!("read {}: {e}", dir.display()))
            })?;
            for f in entries.flatten() {
                let name = f.file_name().to_string_lossy().into_owned();
                if name.starts_with(stem)
                    && name.ends_with(".onnx")
                    && (name.contains(".int8.") == int8)
                {
                    return Ok(f.path().display().to_string());
                }
            }
            Err(DetectorBuildError::SherpaKws(format!(
                "no {stem}*.onnx (int8={int8}) in {} — run scripts/fetch-models.sh",
                dir.display()
            )))
        };

        let mut sc = KeywordSpotterConfig::default();
        // int8 encoder/joiner + fp32 decoder: the combination sherpa's own
        // examples ship; encoder dominates compute. `cfg.fp32` flips the
        // encoder/joiner to fp32 for the quantization arm of the miss-autopsy.
        let quant = !cfg.fp32;
        sc.model_config.transducer.encoder = Some(find_model("encoder", quant)?);
        sc.model_config.transducer.decoder = Some(find_model("decoder", false)?);
        sc.model_config.transducer.joiner = Some(find_model("joiner", quant)?);
        sc.model_config.tokens = Some(dir.join("tokens.txt").display().to_string());
        sc.model_config.num_threads = cfg.num_threads as i32;
        sc.max_active_paths = cfg.max_active_paths as i32;
        sc.keywords_threshold = cfg.default_threshold;
        sc.keywords_score = cfg.default_boost;
        sc.keywords_buf = Some(keywords_buf.to_string());

        let spotter = KeywordSpotter::create(&sc).ok_or_else(|| {
            DetectorBuildError::SherpaKws(format!(
                "KeywordSpotter::create failed for {} (see stderr for the C-side error)",
                dir.display()
            ))
        })?;
        let stream = spotter.create_stream();

        // Page in weights and prime ONNX Runtime on a THROWAWAY stream — the
        // real stream must never see synthetic silence (leading silence
        // corrupts the decoder's internal clock; module docs).
        let throwaway = spotter.create_stream();
        throwaway.accept_waveform(
            EXPECTED_RATE_HZ as i32,
            &vec![0f32; 2 * EXPECTED_RATE_HZ as usize],
        );
        while spotter.is_ready(&throwaway) {
            spotter.decode(&throwaway);
        }

        Ok(TierEngine { spotter, stream })
    }
}

fn render_owned(entries: &[&CompiledEntry]) -> String {
    let owned: Vec<CompiledEntry> = entries.iter().map(|e| (*e).clone()).collect();
    render_keywords(&owned)
}

impl Detector for SherpaKwsDetector {
    fn engine_name(&self) -> &'static str {
        "sherpa_kws"
    }

    fn process(&mut self, chunk: &AudioChunk) -> SmallVec<[Detection; 2]> {
        // Wiring bug, not a runtime condition: the preproc resampler (or a
        // 16 kHz-native source) must feed this engine. Loud per failure policy.
        assert_eq!(
            chunk.sample_rate_hz, EXPECTED_RATE_HZ,
            "sherpa_kws requires 16 kHz input — set pipeline.resample_to_hz = 16000"
        );

        self.f32_buf.clear();
        self.f32_buf
            .extend(chunk.pcm.iter().map(|&s| s as f32 / 32768.0));
        for engine in self.engines() {
            engine
                .stream
                .accept_waveform(EXPECTED_RATE_HZ as i32, &self.f32_buf);
        }
        self.samples_fed += chunk.pcm.len() as u64;

        let mut hits: Vec<(KeywordId, Tier, u64)> = Vec::new();
        let label_map = std::mem::take(&mut self.label_map);
        self.decode_all(|label, pos_ms| match label_map.get(label) {
            Some(&(kw, tier)) => hits.push((kw, tier, pos_ms)),
            None => tracing::error!(label, "sherpa_kws returned an unknown keyword label"),
        });
        self.label_map = label_map;

        let mut out = SmallVec::new();
        for (kw, tier, pos_ms) in hits {
            let slot = &mut self.last_fire_ms[kw.0 as usize][tier_idx(tier)];
            if let Some(last) = *slot {
                if pos_ms.saturating_sub(last) < self.refractory_ms {
                    continue;
                }
            }
            *slot = Some(pos_ms);
            out.push(Detection {
                keyword: kw,
                // The C API exposes no per-hit score (VERIFICATIONS.md M1 pin);
                // gating happens via per-entry thresholds in the keywords list.
                confidence: 1.0,
                tier,
                audio_ts: chunk.first_capture_ts,
                emit_ts: MonotonicNs::now(),
                audio_pos_ms: pos_ms,
                // Identical here by construction: this engine's position source
                // ALREADY is the fed-sample counter, so "where the evidence was"
                // and "how much audio had arrived" are the same number.
                audio_consumed_ms: pos_ms,
                source: chunk.source,
            });
        }
        out
    }
}

fn tier_idx(tier: Tier) -> usize {
    match tier {
        Tier::Fast => 0,
        Tier::Confirmed => 1,
    }
}

// Integration tests live in tests/sherpa_kws.rs (they need models/ on disk);
// keyword compilation and label parsing are unit-tested in kws_compile.rs.
