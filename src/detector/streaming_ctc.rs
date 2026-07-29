//! Custom low-lookahead streaming-CTC spotter (the detector latency milestone).
//!
//! Drives a streaming-CTC encoder ONNX **directly via `ort`** — unlike the
//! sherpa KWS engine (`sherpa_kws.rs`), whose C API exposes no frame scores — so
//! we read raw CTC frame posteriors and run our own prefix-firing decode
//! (`ctc_scorer`). Two wins over sherpa_kws: real per-hit confidence (not a
//! hard-coded 1.0), and latency set by the *model's* lookahead rather than the
//! gigaspeech KWS export's baked-in ~320 ms.
//!
//! `StreamingCtcModel` is the reusable inference core: kaldi fbank (pure-Rust,
//! HTK mel, 80-bin) → encoder forward over a fixed `T`-frame window advanced by
//! `decode_chunk_len` each step, with the 74 streaming cache tensors carried
//! across calls. **Init states and their shapes are read straight off the ONNX
//! signature** (batch dim `-1` → 1, zero-filled), so the same code drives any
//! sherpa/k2-style streaming-CTC export — the English model is a drop-in swap.
//!
//! `StreamingCtcDetector` compiles keyword text to this model's vocab ids
//! (reusing `kws_compile`'s tier/prefix logic), feeds each posterior frame to
//! the CTC scorer, and maps fires to `Detection`s.

use std::collections::{HashMap, HashSet};
use std::path::Path;

use kaldi_native_fbank::online::FeatureComputer;
use kaldi_native_fbank::{FbankComputer, FbankOptions, OnlineFeature};
use ort::session::Session;
use ort::value::{Tensor, TensorElementType, ValueType};
use smallvec::SmallVec;

use super::ctc_scorer::{CtcKeywordScorer, KeywordTarget};
use super::factory::DetectorBuildError;
use super::kws_compile::{compile, TokenTable};
use super::Detector;
use crate::clock::MonotonicNs;
use crate::config::{KeywordConfig, StreamingCtcConfig};
use crate::types::{AudioChunk, Detection};

const EXPECTED_RATE_HZ: u32 = 16_000;
/// kaldi fbank frame shift (10 ms). Output-frame duration is this × subsampling.
const FBANK_FRAME_SHIFT_MS: f32 = 10.0;

fn build_err(msg: impl std::fmt::Display) -> DetectorBuildError {
    DetectorBuildError::StreamingCtc(msg.to_string())
}

/// A streaming cache tensor carried across chunks (`new_X` output → `X` input).
enum State {
    F32(Vec<usize>, Vec<f32>),
    I64(Vec<usize>, Vec<i64>),
}

/// Reusable streaming-CTC inference core: PCM in, CTC log-posterior frames out.
pub struct StreamingCtcModel {
    session: Session,
    feat: OnlineFeature,
    states: HashMap<String, State>,
    /// Output names except `log_probs` (each maps to input `strip("new_")`).
    state_out_names: Vec<String>,
    /// State inputs that a `new_X` output overwrites every step. Their data is
    /// MOVED into the inference call (no per-step clone) because the output
    /// replaces it; any state input without an updating output (none in the
    /// sherpa/k2 exports we drive) falls back to cloning.
    updated_states: HashSet<String>,
    /// Reused f32 conversion buffer for `accept_pcm` (no per-call alloc).
    sample_scratch: Vec<f32>,
    /// Reused flat posterior buffer: the last `accept_pcm` call's output frames,
    /// frame-major, `frames × vocab` values (no per-frame alloc). Read via
    /// [`Self::posteriors`]; overwritten by the next call.
    posterior_scratch: Vec<f32>,
    vocab: usize,
    /// `T` — feature frames fed to `x` per step (ChunkLength).
    t: usize,
    /// `decode_chunk_len` — new frames advanced per step (ChunkShift).
    shift: usize,
    /// fbank frames consumed into the encoder so far.
    pos: usize,
    /// Total posterior frames emitted (global output-frame index).
    out_frames: u64,
    /// Output frames per encoder step (subsampling factor), measured on the
    /// first inference. `None` until then.
    out_per_chunk: Option<usize>,
}

impl StreamingCtcModel {
    pub fn new(model_dir: &Path, num_threads: u32) -> Result<StreamingCtcModel, DetectorBuildError> {
        let onnx = find_encoder_onnx(model_dir)?;
        let session = Session::builder()
            .map_err(build_err)?
            .with_intra_threads(num_threads as usize)
            .map_err(build_err)?
            .commit_from_file(&onnx)
            .map_err(|e| build_err(format!("load {}: {e}", onnx.display())))?;

        // T from the `x` input shape; vocab from the `log_probs` output shape.
        let mut t = 0usize;
        for i in session.inputs() {
            if i.name() == "x" {
                if let ValueType::Tensor { shape, .. } = i.dtype() {
                    t = shape.get(1).copied().unwrap_or(0).max(0) as usize;
                }
            }
        }
        let mut vocab = 0usize;
        for o in session.outputs() {
            if o.name() == "log_probs" {
                if let ValueType::Tensor { shape, .. } = o.dtype() {
                    vocab = shape.last().copied().unwrap_or(0).max(0) as usize;
                }
            }
        }
        if t == 0 || vocab == 0 {
            return Err(build_err("model missing `x` input or `log_probs` output"));
        }

        // decode_chunk_len (ChunkShift) from metadata. Scoped: ModelMetadata
        // holds a borrow on `session` (it has a Drop impl) until dropped.
        let shift = {
            let meta = session.metadata().map_err(build_err)?;
            meta.custom("decode_chunk_len")
                .and_then(|v| v.parse::<usize>().ok())
                .ok_or_else(|| build_err("model metadata missing integer `decode_chunk_len`"))?
        };
        if shift == 0 || shift > t {
            return Err(build_err(format!("nonsensical decode_chunk_len={shift} (T={t})")));
        }

        // Init all streaming states from the signature: batch dim (-1) → 1,
        // zero-fill; i64 for processed_lens, f32 otherwise.
        let mut states: HashMap<String, State> = HashMap::new();
        for inp in session.inputs() {
            let name = inp.name().to_string();
            if name == "x" {
                continue;
            }
            if let ValueType::Tensor { ty, shape, .. } = inp.dtype() {
                let dims: Vec<usize> =
                    shape.iter().map(|&d| if d < 0 { 1 } else { d as usize }).collect();
                let numel: usize = dims.iter().product();
                let st = if *ty == TensorElementType::Int64 {
                    State::I64(dims, vec![0i64; numel])
                } else {
                    State::F32(dims, vec![0f32; numel])
                };
                states.insert(name, st);
            }
        }
        let state_out_names: Vec<String> = session
            .outputs()
            .iter()
            .map(|o| o.name().to_string())
            .filter(|n| n != "log_probs")
            .collect();
        let updated_states: HashSet<String> = state_out_names
            .iter()
            .map(|n| n.strip_prefix("new_").unwrap_or(n).to_string())
            .filter(|t| states.contains_key(t))
            .collect();

        Ok(StreamingCtcModel {
            session,
            feat: new_fbank(),
            states,
            state_out_names,
            updated_states,
            sample_scratch: Vec::new(),
            posterior_scratch: Vec::new(),
            vocab,
            t,
            shift,
            pos: 0,
            out_frames: 0,
            out_per_chunk: None,
        })
    }

    pub fn vocab(&self) -> usize {
        self.vocab
    }

    /// Output frames per encoder step (subsampling factor), once measured.
    pub fn out_per_chunk(&self) -> Option<usize> {
        self.out_per_chunk
    }

    /// Feed PCM (i16 @ 16 kHz) and run the encoder over any newly complete
    /// feature windows. Returns the batch metadata (first global output-frame
    /// index + frame count); the flat log-posterior data itself is read via
    /// [`Self::posteriors`] — a reused buffer, so no per-frame allocation in
    /// steady state (the detector-layer obligation).
    ///
    /// On an inference `Err` the streaming cache state is left partially moved
    /// out — the model must not be fed again after an error (both production
    /// callers treat inference failure as fatal).
    pub fn accept_pcm(&mut self, pcm: &[i16]) -> Result<PosteriorBatch, ort::Error> {
        self.sample_scratch.clear();
        self.sample_scratch.extend(pcm.iter().map(|&s| s as f32 / 32768.0));
        self.feat.accept_waveform(EXPECTED_RATE_HZ as f32, &self.sample_scratch);

        self.posterior_scratch.clear();
        let first_frame = self.out_frames;
        let ready = self.feat.num_frames_ready();
        while self.pos + self.t <= ready {
            let mut xdata = Vec::with_capacity(self.t * 80);
            for f in self.pos..self.pos + self.t {
                xdata.extend_from_slice(self.feat.get_frame(f).unwrap());
            }
            let x = Tensor::from_array((vec![1usize, self.t, 80], xdata))?;

            let mut inputs: Vec<(String, ort::session::SessionInputValue)> = Vec::new();
            inputs.push(("x".to_string(), x.into()));
            let updated = &self.updated_states;
            for (name, st) in self.states.iter_mut() {
                // A state the model overwrites this step is moved, not cloned:
                // `mem::take` leaves an empty Vec that the matching `new_X`
                // output replaces below. Non-updated states (none in practice)
                // are cloned so they survive to the next step.
                let take = updated.contains(name);
                let v: ort::session::SessionInputValue = match st {
                    State::F32(shape, data) => {
                        let d = if take { std::mem::take(data) } else { data.clone() };
                        Tensor::from_array((shape.clone(), d))?.into()
                    }
                    State::I64(shape, data) => {
                        let d = if take { std::mem::take(data) } else { data.clone() };
                        Tensor::from_array((shape.clone(), d))?.into()
                    }
                };
                inputs.push((name.clone(), v));
            }

            let outputs = self.session.run(inputs)?;

            let (shape, data) = outputs["log_probs"].try_extract_tensor::<f32>()?;
            let tt = shape[1] as usize;
            self.out_per_chunk.get_or_insert(tt);
            self.posterior_scratch.extend_from_slice(&data[..tt * self.vocab]);
            self.out_frames += tt as u64;

            let mut updates: Vec<(String, State)> = Vec::new();
            for out_name in &self.state_out_names {
                let target = out_name.strip_prefix("new_").unwrap_or(out_name).to_string();
                let is_i64 = matches!(self.states.get(&target), Some(State::I64(..)));
                if is_i64 {
                    let (sh, d) = outputs[out_name.as_str()].try_extract_tensor::<i64>()?;
                    updates.push((target, State::I64(sh.iter().map(|&x| x as usize).collect(), d.to_vec())));
                } else {
                    let (sh, d) = outputs[out_name.as_str()].try_extract_tensor::<f32>()?;
                    updates.push((target, State::F32(sh.iter().map(|&x| x as usize).collect(), d.to_vec())));
                }
            }
            for (k, v) in updates {
                self.states.insert(k, v);
            }
            self.pos += self.shift;
        }
        Ok(PosteriorBatch { first_frame, frames: (self.out_frames - first_frame) as usize })
    }

    /// Flat log-posterior data for the LAST `accept_pcm` call's batch:
    /// `batch.frames × vocab` values, frame-major (iterate with
    /// `.chunks_exact(model.vocab())`). Overwritten by the next `accept_pcm`.
    pub fn posteriors(&self) -> &[f32] {
        &self.posterior_scratch
    }
}

/// Metadata for the posterior frames produced by one
/// [`StreamingCtcModel::accept_pcm`] call. The frame data lives in the model's
/// reused flat buffer ([`StreamingCtcModel::posteriors`]) — this struct only
/// carries where the batch sits in the global output-frame stream.
#[derive(Debug, Clone, Copy)]
pub struct PosteriorBatch {
    /// Global output-frame index of the first frame in this batch.
    pub first_frame: u64,
    /// Number of posterior frames produced (each `vocab` values long).
    pub frames: usize,
}

impl PosteriorBatch {
    pub fn len(&self) -> usize {
        self.frames
    }

    pub fn is_empty(&self) -> bool {
        self.frames == 0
    }
}

/// sherpa-matching kaldi fbank: 80-bin HTK mel, no energy, dither 0.
fn new_fbank() -> OnlineFeature {
    let mut opts = FbankOptions::default();
    opts.use_energy = false;
    opts.mel_opts.num_bins = 80;
    opts.mel_opts.low_freq = 20.0;
    opts.mel_opts.high_freq = 0.0;
    opts.frame_opts.dither = 0.0;
    opts.frame_opts.snip_edges = false;
    OnlineFeature::new(FeatureComputer::Fbank(
        FbankComputer::new(opts).expect("valid fbank opts"),
    ))
}

fn find_encoder_onnx(dir: &Path) -> Result<std::path::PathBuf, DetectorBuildError> {
    let rd = std::fs::read_dir(dir).map_err(|e| build_err(format!("read {}: {e}", dir.display())))?;
    let mut candidates: Vec<std::path::PathBuf> = rd
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().map(|x| x == "onnx").unwrap_or(false))
        .collect();
    // Prefer an int8 export (smaller/faster) when several exist.
    candidates.sort_by_key(|p| !p.to_string_lossy().contains("int8"));
    candidates
        .into_iter()
        .next()
        .ok_or_else(|| build_err(format!("no *.onnx in {} — run scripts/fetch-models.sh", dir.display())))
}

pub struct StreamingCtcDetector {
    model: StreamingCtcModel,
    scorer: CtcKeywordScorer,
    refractory_ms: u64,
    /// Total PCM samples handed to this detector. `audio_pos_ms` reports the
    /// SCORING FRAME, which is earlier than the audio the encoder actually
    /// needed by its right-context — fine for match scoring, wrong for
    /// cross-engine latency. This counter is the comparable quantity.
    samples_fed: u64,
}

impl StreamingCtcDetector {
    pub fn new(
        cfg: &StreamingCtcConfig,
        keywords: &[KeywordConfig],
    ) -> Result<StreamingCtcDetector, DetectorBuildError> {
        let model = StreamingCtcModel::new(&cfg.model_dir, cfg.num_threads)?;

        // Reuse the sherpa engine's tier/prefix token compilation, then map the
        // piece strings to THIS model's vocab ids via tokens.txt.
        let table = TokenTable::from_bpe_model(&cfg.model_dir.join("bpe.model"))
            .map_err(|e| build_err(e.to_string()))?;
        let entries = compile(keywords, &table, cfg.default_threshold, 1.0)
            .map_err(|e| build_err(e.to_string()))?;
        let piece_to_id = load_tokens(&cfg.model_dir.join("tokens.txt"))?;

        let mut targets = Vec::with_capacity(entries.len());
        for e in &entries {
            let mut ids = Vec::with_capacity(e.tokens.len());
            for piece in &e.tokens {
                let id = *piece_to_id
                    .get(piece)
                    .ok_or_else(|| build_err(format!("token piece {piece:?} not in tokens.txt")))?;
                ids.push(id);
            }
            targets.push(KeywordTarget { keyword: e.keyword, tier: e.tier, tokens: ids, threshold: e.threshold });
        }
        let scorer = CtcKeywordScorer::new(&targets, cfg.blank_id as usize);
        Ok(StreamingCtcDetector { model, scorer, refractory_ms: cfg.refractory_ms, samples_fed: 0 })
    }

    /// Construct directly from token-id targets, bypassing text compilation.
    /// Used to validate the engine against non-BPE stand-in models.
    pub fn from_targets(
        model: StreamingCtcModel,
        targets: &[KeywordTarget],
        blank_id: usize,
        refractory_ms: u64,
    ) -> StreamingCtcDetector {
        StreamingCtcDetector {
            model,
            scorer: CtcKeywordScorer::new(targets, blank_id),
            refractory_ms,
            samples_fed: 0,
        }
    }
}

impl Detector for StreamingCtcDetector {
    fn process(&mut self, chunk: &AudioChunk) -> SmallVec<[Detection; 2]> {
        // Wiring bug, not a runtime condition: preproc must feed 16 kHz.
        assert_eq!(
            chunk.sample_rate_hz, EXPECTED_RATE_HZ,
            "streaming_ctc requires 16 kHz input — set pipeline.resample_to_hz = 16000"
        );

        // Count BEFORE inference: by the time a frame scores, this whole chunk
        // has been handed to the encoder, so it is audio that had to arrive.
        self.samples_fed += chunk.pcm.len() as u64;
        let consumed_ms = self.samples_fed * 1000 / EXPECTED_RATE_HZ as u64;

        let batch = self
            .model
            .accept_pcm(&chunk.pcm)
            .expect("streaming_ctc encoder inference failed");
        let mut out = SmallVec::new();
        if batch.is_empty() {
            return out;
        }

        // Output-frame duration = fbank shift × subsampling (known after the
        // first inference); converts frame indices to stream-time and ms.
        let subsample = self.model.out_per_chunk().map(|n| self.model.shift / n.max(1)).unwrap_or(1);
        let frame_shift_ms = FBANK_FRAME_SHIFT_MS * subsample as f32;
        let refractory_frames = (self.refractory_ms as f32 / frame_shift_ms).round() as u64;

        let vocab = self.model.vocab();
        for (i, logpost) in self.model.posteriors().chunks_exact(vocab).enumerate() {
            let fidx = batch.first_frame + i as u64;
            for f in self.scorer.accept_frame(fidx, logpost, refractory_frames) {
                out.push(Detection {
                    keyword: f.keyword,
                    confidence: f.confidence,
                    tier: f.tier,
                    audio_ts: chunk.first_capture_ts,
                    emit_ts: MonotonicNs::now(),
                    audio_pos_ms: (fidx as f32 * frame_shift_ms) as u64,
                    // NOT the same as audio_pos_ms here: the scoring frame is
                    // earlier than the audio the encoder needed to produce it.
                    audio_consumed_ms: consumed_ms,
                    source: chunk.source,
                });
            }
        }
        out
    }
}

/// tokens.txt: `<piece> <id>` per line → `piece -> id`.
fn load_tokens(path: &Path) -> Result<HashMap<String, u32>, DetectorBuildError> {
    let txt = std::fs::read_to_string(path)
        .map_err(|e| build_err(format!("read {}: {e}", path.display())))?;
    let mut map = HashMap::new();
    for line in txt.lines() {
        let mut it = line.rsplitn(2, char::is_whitespace);
        let id = it.next().and_then(|s| s.parse::<u32>().ok());
        let piece = it.next();
        if let (Some(piece), Some(id)) = (piece, id) {
            map.insert(piece.to_string(), id);
        }
    }
    if map.is_empty() {
        return Err(build_err(format!("no tokens parsed from {}", path.display())));
    }
    Ok(map)
}

// Integration tests need a streaming-CTC model on disk; point EARSHOT_CTC_MODEL_DIR
// at one (e.g. the Chinese small-ctc stand-in) to run them.
#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{KeywordId, SourceId, Tier};
    use std::sync::Arc;

    fn model_dir() -> Option<std::path::PathBuf> {
        std::env::var_os("EARSHOT_CTC_MODEL_DIR").map(std::path::PathBuf::from)
    }

    fn read_wav_i16(path: &Path) -> Vec<i16> {
        let mut r = hound::WavReader::open(path).unwrap();
        match r.spec().sample_format {
            hound::SampleFormat::Int => r.samples::<i16>().map(|s| s.unwrap()).collect(),
            hound::SampleFormat::Float => {
                r.samples::<f32>().map(|s| (s.unwrap() * 32768.0) as i16).collect()
            }
        }
    }

    /// End-to-end: real model streams posteriors, and a known token subsequence
    /// present in 0.wav fires the scorer. Validates fbank + ort + cache + scorer.
    #[test]
    fn model_streams_and_known_subsequence_fires() {
        let Some(dir) = model_dir() else {
            eprintln!("SKIP: set EARSHOT_CTC_MODEL_DIR to a streaming-CTC model dir");
            return;
        };
        let mut model = StreamingCtcModel::new(&dir, 1).unwrap();
        let pcm = read_wav_i16(&dir.join("test_wavs/0.wav"));
        let batch = model.accept_pcm(&pcm).unwrap();
        assert!(!batch.is_empty(), "model produced no posterior frames");
        let vocab = model.vocab();
        assert_eq!(model.posteriors().len(), batch.frames * vocab);
        // out/chunk is export-specific (decode_chunk_len / subsampling): 16 for
        // the zh stand-in (640 ms quantum), 8 for ctc-small-en 2024-03-18
        // (320 ms quantum). The invariant is that the model measured one.
        let opc = model.out_per_chunk().expect("model never measured out frames/chunk");
        assert!(opc > 0, "nonsensical out frames/chunk {opc}");

        // Greedy-decode to find a real 3-token subsequence, then confirm the CTC
        // scorer fires on it (blank = 0).
        let mut prev = usize::MAX;
        let mut ids = Vec::new();
        for lp in model.posteriors().chunks_exact(vocab) {
            let best = lp.iter().enumerate().max_by(|a, b| a.1.partial_cmp(b.1).unwrap()).unwrap().0;
            if best != 0 && best != prev {
                ids.push(best as u32);
            }
            prev = best;
        }
        assert!(ids.len() >= 4, "greedy decode too short: {ids:?}");
        let target = KeywordTarget {
            keyword: KeywordId(0),
            tier: Tier::Confirmed,
            tokens: ids[1..4].to_vec(),
            threshold: 0.2,
        };
        let mut scorer = CtcKeywordScorer::new(&[target], 0);
        let mut fired = false;
        for (i, lp) in model.posteriors().chunks_exact(vocab).enumerate() {
            if !scorer.accept_frame(batch.first_frame + i as u64, lp, 100).is_empty() {
                fired = true;
            }
        }
        assert!(fired, "scorer did not fire on a greedy-present subsequence {:?}", &ids[1..4]);
    }

    /// Detector-trait path: feed 0.wav as AudioChunks, assert a Detection for a
    /// known-present target (via `from_targets`, bypassing text compilation).
    #[test]
    fn detector_emits_detection_for_present_keyword() {
        let Some(dir) = model_dir() else {
            eprintln!("SKIP: set EARSHOT_CTC_MODEL_DIR");
            return;
        };
        // First pass: discover a present subsequence.
        let mut m0 = StreamingCtcModel::new(&dir, 1).unwrap();
        let pcm = read_wav_i16(&dir.join("test_wavs/0.wav"));
        m0.accept_pcm(&pcm).unwrap();
        let mut prev = usize::MAX;
        let mut ids = Vec::new();
        for lp in m0.posteriors().chunks_exact(m0.vocab()) {
            let best = lp.iter().enumerate().max_by(|a, b| a.1.partial_cmp(b.1).unwrap()).unwrap().0;
            if best != 0 && best != prev {
                ids.push(best as u32);
            }
            prev = best;
        }
        let target = KeywordTarget {
            keyword: KeywordId(0),
            tier: Tier::Confirmed,
            tokens: ids[1..4].to_vec(),
            threshold: 0.2,
        };

        // Fresh model, driven through the Detector trait in 100 ms chunks.
        let model = StreamingCtcModel::new(&dir, 1).unwrap();
        let mut det = StreamingCtcDetector::from_targets(model, &[target], 0, 2000);
        let chunk_samples = (EXPECTED_RATE_HZ / 10) as usize; // 100 ms
        let mut detections = 0;
        for c in pcm.chunks(chunk_samples) {
            let chunk = AudioChunk {
                pcm: Arc::from(c.to_vec().into_boxed_slice()),
                sample_rate_hz: EXPECTED_RATE_HZ,
                first_capture_ts: MonotonicNs::now(),
                last_capture_ts: MonotonicNs::now(),
                first_preproc_ts: MonotonicNs::now(),
                chunk_ts: MonotonicNs::now(),
                source: SourceId(0),
                seq: 0,
            };
            detections += det.process(&chunk).len();
        }
        assert!(detections >= 1, "Detector emitted no Detection for a present keyword");
    }
}
