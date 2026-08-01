# Node: `runtime.detector.streaming_ctc`

## Job

Run low-lookahead streaming CTC inference and convert frame posteriors into
tiered keyword detections with explainable confidence.

## Owns

`src/detector/streaming_ctc.rs`, `src/detector/ctc_scorer.rs`,
`docs/CTC-MODEL-SOURCING.md`, and
`scripts/export-english-streaming-ctc.md`: ONNX streaming state, fbank
extraction, vocabulary mapping, prefix-firing CTC scoring, diagnostics, and
model sourcing/export instructions.

## Does not own

Keyword text compilation, detector selection, the shared detector trait,
Sherpa KWS behavior, deployment of model assets, decision policy, or benchmark
acceptance.

## Input contracts

`runtime.detector.keyword_compiler`, validated `StreamingCtcConfig` and
`KeywordConfig` values supplied by the detector factory, a compatible model
directory containing ONNX/tokenizer assets, and mono 16-kHz `AudioChunk`
values. Dynamic inference also requires the configured ONNX Runtime library.
`StreamingCtcConfig.precision` (`"int8"` default | `"fp32"`, enforced by
config validation and re-parsed by `Precision::parse`) selects which encoder
export `select_encoder_onnx` loads from the model dir: `"int8"` prefers the
int8 export and falls back to a lone non-int8 one (the pre-knob behavior);
`"fp32"` requires a non-int8 export and returns a build error naming what was
found when only int8 exists. `KeywordConfig.boost` is NOT an input this engine
honors: the scorer implements no acoustic boost (`kws_compile::compile` is
invoked with a hardcoded `default_boost = 1.0`), and `config::validate` warns
loudly on streaming_ctc configs carrying keyword boosts.

## Output contracts

`runtime.detector.streaming_ctc_engine`: the
`StreamingCtcDetector::new` constructor and start-anytime Viterbi scoring capability
behind `Detector`. Emitted detections carry real CTC confidence,
model-timeline `audio_pos_ms`, engine-comparable `audio_consumed_ms`, source,
tier, and monotonic emission time. `explain` reports diagnostic peaks without
changing firing behavior.

## Side effects and exclusive resources

Construction reads model/token files and dynamically loads ONNX Runtime.
Each instance exclusively owns one inference session, streaming cache tensors,
online fbank state, and keyword scorer. It performs no network or mutable-file
I/O.

## Failure contract

Missing assets, invalid model signatures/metadata, vocabulary mismatches, or
session construction errors return `DetectorBuildError::StreamingCtc` and
refuse arming. Non-16-kHz input and inference failure are loud assertions;
the engine must never degrade into silent no-detections.

## Parallel work boundary

Inference and scorer internals may change together inside this node.
Tokenization changes coordinate with `runtime.detector.keyword_compile`;
constructor selection coordinates with `runtime.detector.port`; deployment and
corpus gates remain in their owning nodes.

## Verification

Run `cargo test detector::ctc_scorer` for model-free scoring checks, and
`cargo test detector::streaming_ctc` for the model-free encoder-selection and
precision-parsing tests. With `EARSHOT_CTC_MODEL_DIR` and `ORT_DYLIB_PATH`
set, the same filter also drives the real-model integration tests. Finish
with the declared corpus bake-off and `scripts/doc-lint.sh`.

## Migration status

The inference core and scorer are implemented as one engine node with an
explicit constructor capability. Model acceptance is still evidence-driven;
an exported model is not promoted merely because plumbing tests pass.

## Evidence — encoder precision A/B, int8 vs fp32 (2026-07-29)

Measured when the `precision` knob landed, per the provable-improvements
policy. Lossless (speed=0) accuracy passes, identical config
(`configs/bakeoff-webcast.toml` operating point: model
`sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18`, threshold 0.15,
chunk 80 ms, `--split dev`); the ONLY variable is which encoder export loads
(`ctc-….int8.onnx` vs `ctc-….onnx`). Metrics are audio-domain
(counts + `consumed_lag`), so run interleaving is not required.

Wideband cell (dev: INTC + LMT, 2.53 h, 77 Confirmed occurrences):

| Confirmed aggregate | int8 | fp32 | Δ |
|---|---|---|---|
| recall | 0.831 (64/77) | 0.844 (65/77) | +1 TP (panther lake 16→17) |
| false alarms | 1141 (451.5 FA/h) | 1149 (454.7 FA/h) | +8 FA |
| …excluding "panther lake" | 32 FA (12.7 FA/h), recall 0.842 | 35 FA (13.8 FA/h), recall 0.842 | +3 FA, recall identical |
| consumed_lag p50 (per-phrase median) | 530 ms | 540 ms | flat |

Every per-phrase row except panther lake (+1 TP / +5 FA), government (+2 FA)
and ultra (+1 FA) is BIT-IDENTICAL between the two arms. "panther lake" is a
degenerate shared artifact (1109/1114 FAs, firing near-continuously across
the INTC call at threshold 0.15) that dominates raw FA/h in both arms
equally; read the excluding-row for the honest precision delta.

Telephony cell (dev: AAPL-Q2-2026, 1.02 h): tariff Confirmed 4/9 recall,
3 FA, consumed_lag p50/p95 700/1020 ms — **bit-identical in both arms**,
re-confirming the 2026-07-17 pin that tariff is quantization-insensitive on
this engine (docs/BAKEOFF.md). Total FA 772 (int8) vs 769 (fp32); the bulk
("apple" 96/97, "panther lake" 600/597) is foreign-keyword-list artifact
shared by both arms, not an engine delta.

**Verdict: fp32 is a WASH on this engine** — +1 TP for +8 FA wideband, −3 FA
telephony, lag flat — unlike sherpa_kws, where `fp32 = true` was a recall
win at zero precision cost (BAKEOFF §Miss-autopsy cell 1). `precision`
stays default `"int8"`; the knob exists so the quantization arm of any
future miss-autopsy is a config flip instead of a code edit. Raw reports:
bench-{int8,fp32}.json + bench-aapl-{int8,fp32}.json (session scratchpad,
2026-07-29).
