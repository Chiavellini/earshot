# Node: `runtime.detector.online_asr`

## Job

Drive any sherpa-onnx streaming ASR model (zipformer transducer, NeMo
cache-aware conformer, zipformer2-CTC) through sherpa's `OnlineRecognizer` and
turn its emitted complete-word stream into `Confirmed` keyword detections, with
optional hotword biasing toward the configured variants.

## Owns

`src/detector/online_asr.rs` (`OnlineAsrDetector`, `render_hotwords`,
`BuildFacts`), `src/detector/text_match.rs` (`normalize_word`,
`normalize_phrase`, `words_from_tokens`, `VariantMatcher::{scan,contains}` —
the word matcher shared with the fill-path verifier), `src/detector/sherpa_files.rs`
(`discover_online`, `pick_model`, `find_model`, `ensure_bpe_vocab` — the
model-directory discovery every sherpa-backed engine uses; `sherpa_kws`
delegates its `find_model` here), `tests/online_asr.rs`.

## Does not own

Keyword text→BPE compilation (`runtime.detector.keyword_compile`; this node
only reads `TokenTable::pieces()` to synthesize `bpe.vocab`), engine selection
and the `Detector` trait (`runtime.detector.port`), `EngineKind::OnlineAsr` /
`OnlineAsrConfig` and their validation (`runtime.config`), the bench grid
(`evidence.benchmark`), decision policy, model fetching (`scripts/fetch-models.sh`,
`platform.*`), and the pre-registered promotion bars (`docs/BAKEOFF.md`).

## Input contracts

Validated `OnlineAsrConfig` (`[detector.online_asr]`: `model_dir`,
`model_type = auto|transducer|zipformer2_ctc|nemo_ctc`, `precision = int8|fp32`,
`num_threads`, `decoding_method = modified_beam_search|greedy_search`,
`max_active_paths`, `hotwords`, `hotwords_score`, `blank_penalty`,
`endpoint_rule{1,2,3}_ms`, `refractory_ms`) and `KeywordConfig` values from the
factory; mono 16 kHz `AudioChunk`s (any other rate is an assertion). Config
validation refuses `fast_prefixes` and `policy = "fast_only"` under this engine
(it emits no Fast tier) and `hotwords = true` without `modified_beam_search`.

Model directory: `encoder/decoder/joiner*.onnx` (transducer) | `ctc-*.onnx`
(zipformer2 CTC) | `model*.onnx` (NeMo CTC), `tokens.txt`, and for hotwords
`bpe.vocab` or `bpe.model` (the vocab is synthesized next to the model when
only the protobuf ships). Every path is resolved and checked in Rust before
`OnlineRecognizer::create` because sherpa's C++ `exit(-1)`s inside the FFI on a
missing hotword asset.

## Output contracts

`runtime.detector.online_asr_engine`: `OnlineAsrDetector::new` behind
`Detector`; `engine_name() = "online_asr"`. Detections are `Tier::Confirmed`
only, one per keyword occurrence (refractory-deduped), with
`audio_pos_ms == audio_consumed_ms` = the fed-sample position of the chunk in
which the match surfaced (sherpa token timestamps are logged at debug level as
`sherpa_word_end_ms`, diagnostic only — they rebase around silence and around
our own `reset`). `confidence` is the **1.0 sentinel** (the safe crate exposes
no posteriors) — read it alongside `engine_name`, never gate on it.

**Completion rule (contract):** a match ends only at a COMPLETE word — a later
`▁`-initial token exists or the stream is at an endpoint. The trailing partial
word never matches (`ai` inside "airline"), so prefix-style false fires are
structurally impossible, at roughly one following token of latency.

**Endpointing:** on `is_endpoint` the trailing word is completed and matched,
then the stream is `reset` (encoder state kept, decoder result restarted) and
the match watermark returns to 0.

**Hotwords:** active iff transducer + `modified_beam_search` + a BPE vocab;
buffer = one line per keyword variant, `TEXT :score`, in the vocabulary's case
(`uppercase_vocab` detected from `tokens.txt`); a keyword `boost` overrides
`hotwords_score`. `BuildFacts { model_kind, hotwords_active, hotword_lines,
decoding_method, uppercase_vocab }` is logged at build and exposed on the
detector.

## Side effects and exclusive resources

Construction reads the model directory and may WRITE `<model_dir>/bpe.vocab`
once (hotword prerequisite). Each instance owns one `OnlineRecognizer` + one
`OnlineStream` (+ a throwaway warm-up stream at build). No network I/O.

## Failure contract

Unclassifiable / incomplete model dirs, an fp32 request with only int8 exports,
missing `tokens.txt`, an unwritable `bpe.vocab`, or `OnlineRecognizer::create`
returning null → `DetectorBuildError::OnlineAsr(..)` naming the directory;
arming refuses. Non-16 kHz input is a loud assertion. Hotwords silently
unavailable is NOT allowed to be silent: the engine logs a `warn!` and
`hotwords_active=false` in `BuildFacts`.

## Parallel work boundary

This node may change the three source files and its test freely. Adding a
config knob touches `runtime.config`; a new sherpa model family that needs a
new `EngineKind` touches `runtime.detector.port` and `evidence.benchmark`.
The `VariantMatcher` is a shared contract with the fill-path verifier — change
its semantics only with both consumers' tests green.

## Verification

`cargo test --lib detector` (text_match / sherpa_files / render_hotwords unit
tests, no models), `cargo test --test online_asr` (SKIPs loudly without
`models/sherpa-onnx-streaming-zipformer-en-2023-06-26`; panics under
`EARSHOT_REQUIRE_MODELS`; pins P1 hotword buffer accepted, P3 leading-silence
position = fed counter, P6 endpoint reset without double fire), and the
interleaved corpus bench
`bench-detector --engines streaming_ctc,online_asr --model-dir a,b` scored
against the pre-registered bars in `docs/BAKEOFF.md`.

## Migration status

Built 2026-08-18 (detection-accuracy program D1) and green on the zipformer-en
2023-06-26 test_wavs. Not yet armed anywhere; not yet benched on the corpus.
Open increments: (v1.1) raw sherpa result JSON (`ys_probs`, `context_scores`)
for a real per-word confidence + `boost_share`; a sherpa-timestamp
promotion study for `audio_pos_ms`; NeMo cache-aware 80 ms candidates on the
box (paced RTF gate).
