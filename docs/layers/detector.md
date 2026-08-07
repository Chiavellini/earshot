# Layer: detector

## Role
Binary detection of a small closed set of keywords in audio chunks — NOT
transcription (spec §5.4). This is the core IP and the M1 bake-off surface:
engines (grammar-restricted Vosk, sherpa-onnx zipformer, KWS models) compete
behind this trait. NOT responsible for: trading decisions, market mapping,
sizing — a `Detection` is a pure acoustic event.

## Entry door
- **Type:** `&types::AudioChunk`.
- **Mechanism:** synchronous trait call on the dedicated `detect` OS thread:
  `Detector::process(&mut self, chunk: &AudioChunk) -> SmallVec<[Detection; 2]>`
  (`src/detector/mod.rs`). Engines with internal async runtimes hide them
  behind this synchronous surface (internal thread + channel).
- **Cadence:** one call per chunk (every `chunk_ms` at real-time speed). The
  call may take up to ~chunk_ms before it becomes the bottleneck.
- **Identity:** `Detector::engine_name(&self) -> &'static str` returns the name
  as it appears in `[detector].engine`. Both composers in `pipeline` read it
  before the detector moves into its thread and stamp it on `PipelineReport`;
  a fanout run reports the distinct set joined by `+`, because nothing forces a
  config's legs to agree. This exists because `Detection::confidence` is NOT
  comparable across engines (see Exit door), roughly half the arm configs run
  each engine, and no banked report recorded which one produced its detections
  — the first settlement study had to infer it from the sentinel value.

## Exit door
- **Type:** `types::Detection` — `keyword: KeywordId` (index into the
  config keyword table), `confidence: f32`, `tier: Tier` (`Fast`/`Confirmed`),
  `audio_ts` (= `chunk.first_capture_ts` of the triggering audio),
  `emit_ts: MonotonicNs` (stamped by the detector at emission),
  `source: SourceId` (winning ingress leg), `audio_pos_ms: u64` (the
  triggering position on the stream timeline), and
  `audio_consumed_ms: u64` (engine-independent audio fed before emission).
  The two audio-position fields are wall-clock-free so bench scoring works in
  lossless speed=0 replays.
- **`confidence` is engine-relative and is NOT a probability of correctness.**
  sherpa_kws emits a constant 1.0 sentinel; streaming_ctc emits
  `exp(score / k)`, a geometric mean over BPE pieces. Neither predicts whether
  the word was really said: joined against settlement over every banked run
  (`scripts/settle-score.py`), mean confidence on *wrong* fires below a 60¢ ask
  was **higher** than on right ones (0.411 vs 0.331), while 0.50-confidence
  fires against a 95–100¢ book were right 29 times out of 29. Read it as a
  detector-internal gate input only, and always alongside `engine_name`.
- **Mechanism:** function return value; the detect-thread loop feeds each
  `Detection` straight into the decision layer.

## Obligations
- Stamp `emit_ts = MonotonicNs::now()` when the hypothesis becomes actionable
  — for streaming engines that is the STABLE-PARTIAL moment, never
  end-of-utterance endpointing (spec §5.4 hard requirement).
- `audio_ts` must reference the triggering audio's capture time (chunk's
  `first_capture_ts` until finer alignment exists).
- Word-boundary-safe matching against the config `variants` list only —
  plurals/possessives count, other inflections don't (VERIFICATIONS Q4 /
  NEWMENTION terms). No stemming, no substring matches.
- Emit at most one Detection per keyword per utterance-ish window
  (refractory); the decision layer's state machine is the backstop, not the
  primary dedupe.
- No allocation in steady state beyond the returned SmallVec (inline cap 2).
- Copy `chunk.source` into `Detection.source` (leg attribution). Detectors
  stay otherwise leg-ignorant: in fanout mode each leg gets its OWN detector
  instance — model memory is deliberately duplicated (the 3.3M KWS model is
  a few MB per instance; sharing one sherpa recognizer across threads via
  the FFI would be an unforced thread-safety risk for no meaningful
  savings). N legs ⇒ N × per-chunk decode CPU; 2–3 legs is trivial.
- Note on gates: sherpa_kws exposes no per-hit score (`confidence` is a
  constant 1.0), so the decision layer's `min_confidence_*` gates are INERT
  on the production engine — per-keyword `fast_threshold` /
  `confirmed_threshold` / `boost` (compiled into the spotter) are the real
  precision knobs. `boost` is sherpa_kws-ONLY: the streaming_ctc scorer
  ignores it (config validation warns; see that engine's section below).

## Failure policy
- An engine failure (FFI error, model crash) must surface as a loud log +
  detector replacement/abort — never silent no-detections (P3: an armed
  session with a dead detector must page, not idle).

## How to replace or edit this layer
- New engine = new file in `src/detector/` implementing `Detector`, plus a
  match arm in `detector::factory::build_detector` (the single construction
  point; both `main.rs` wiring sites call it) and an `EngineKind` variant in
  `src/config.rs` (`[detector] engine = "..."` selects it). Nothing else
  changes.
- Factory failure = refuse to arm (`DetectorBuildError`), never a fallback
  engine.
- Contract: the trait signature, `Detection` field semantics (especially the
  two timestamps), stable-partial emission.
- `EnergyStubDetector` (`stub.rs`) is permanent: it is the pipeline self-test
  with exact ground truth from `fixture::FixtureSpec`. Never delete it.
  Its `audio_pos_ms` = start of the firing chunk (own sample counter).

## Engine: sherpa_kws (`sherpa_kws.rs` + `kws_compile.rs`)
- sherpa-onnx streaming keyword spotter (Zipformer transducer, 16 kHz —
  requires `pipeline.resample_to_hz = 16000` for telephony sources; a wrong
  input rate is an assert, not a silent degrade).
- **Two spotters, not one**: Fast (prefix) entries and Confirmed (full)
  entries are mutually exclusive within one decoder, and per-stream keyword
  lists are silently ignored by the C API — so each tier gets its own
  spotter + stream (VERIFICATIONS.md M1 pins 5–6).
- **Keyword compilation** (`kws_compile.rs`): config `variants` →
  SentencePiece **unigram Viterbi** token sequences (pinned vectors;
  `bpe.model` is unigram despite the name). `fast_prefixes` are CUT from
  the full variant's token sequence at the first boundary covering the
  prefix text — never independently encoded (mid-word token mismatch
  otherwise). Labels `kw{i}_fast{p}` / `kw{i}_conf{v}` round-trip through
  sherpa's `@label`; `parse_label` inverts.
- **Tuning**: per-keyword `fast_threshold` / `confirmed_threshold` / `boost`
  (defaults from `[detector.sherpa_kws]`). Shorter prefixes need lower
  thresholds; 1-token prefixes never fire — keep ≥2 tokens (pin 7).
- **`audio_pos_ms`** = fed-sample position of the chunk in which the hit
  surfaced (≈ word end + ~0.4 s decode lag). sherpa's token timestamps are
  NOT used — they rebase around silence (pin 3).
- No stream resets; refractory (`refractory_ms`, per keyword × tier on the
  stream timeline) is the dedupe. `confidence` is constant 1.0 (the C API
  exposes no score, pin 2) — decision-layer gates compose regardless.
- Warm-up runs on a throwaway stream so real streams never see synthetic
  leading silence.
- Smoke test without a corpus: `say -v Samantha -o s.aiff "... recession ..."`,
  `ffmpeg -i s.aiff -ac 1 -ar 8000 -af "highpass=f=300,lowpass=f=3400"
  configs/kws-smoke.wav`, then
  `cargo run -- replay --config configs/m1-kws-example.toml`.

## Engine: streaming_ctc (`streaming_ctc.rs` + `ctc_scorer.rs`)
The custom low-lookahead spotter — the detector latency milestone (BAKEOFF.md:
sherpa_kws's gigaspeech export floors at ~1 s onset→emit, ~3× the ≤350 ms bar).
Drives a streaming-CTC encoder ONNX **directly via `ort`** (16 kHz; wrong input
rate is an assert), so it reads raw CTC **frame posteriors** — which the sherpa
KWS C API does not expose. Two wins over sherpa_kws: real per-hit `confidence`
(not a constant 1.0), and latency set by the *model's* lookahead, not a baked-in
~320 ms.
- **`StreamingCtcModel`** — reusable inference core: pure-Rust kaldi fbank
  (`kaldi-native-fbank`, 80-bin HTK mel, no energy, dither 0, snip_edges false —
  matches sherpa's feature config) → encoder forward over a fixed `T`-frame
  window advanced by `decode_chunk_len` each step. The 74 streaming cache tensors
  (`cached_{key,nonlin_attn,val1,val2,conv1,conv2}`×12 + `embed_states` +
  `processed_lens`) are carried across calls: each `new_X` output → next call's
  `X` input. **Init states + shapes are read straight off the ONNX signature**
  (batch dim `-1` → 1, zero-filled; i64 for `processed_lens`), so the same code
  drives any sherpa/k2 zipformer2-style streaming-CTC export — the English model
  is a drop-in. `T` from the `x` input shape, `decode_chunk_len` from ONNX
  metadata, `vocab` from the `log_probs` output.
- **Prefix-firing** (`ctc_scorer.rs`): one start-anytime **Viterbi** forward
  variable per compiled entry over the CTC lattice `[blank, t0, blank, t1, …]`;
  fires when the best path reaches the final token (`max(alpha[L-2], alpha[L-1])`)
  with `confidence = exp(score / k)` ≥ threshold — the frame the last token
  peaks, never endpointing. Fast entries carry the *cut prefix* token target, so
  Fast fires earlier than Confirmed from the SAME single posterior stream (no
  second decoder — unlike sherpa_kws's two spotters).
- **Keyword compilation**: reuses `kws_compile::compile` (same tier/prefix token
  logic) for piece strings, then maps them to this model's `tokens.txt` ids.
  Thresholds come from `[detector.streaming_ctc]` / per-keyword overrides
  (CTC-confidence scale, tuned in Phase 4 — NOT sherpa's threshold scale).
  ⚠ **Keyword `boost` is IGNORED here**: the CTC scorer implements no acoustic
  boost — `compile` is called with a hardcoded `default_boost = 1.0` and the
  per-entry value is never read. `config::validate` warns loudly when a
  streaming_ctc config carries keyword boosts (they are sherpa-scale numbers,
  e.g. prepare's tuned_library), so they can no longer be mistaken for applied
  tuning.
- **Encoder precision** (`[detector.streaming_ctc].precision`, default
  `"int8"`): picks which `*.onnx` export in `model_dir` is loaded
  (`Precision` + the pure `select_encoder_onnx` — unit-tested on synthetic
  file lists). `"int8"` prefers the int8 export and falls back to whatever
  single export exists; `"fp32"` requires a non-int8 export and refuses to
  build when only int8 is present (naming what was found). Counterpart of
  `sherpa_kws.fp32` — but NOT the same result: the 2026-07-29 A/B (dev,
  chunk 80) measured fp32 as a WASH on this engine (+1 TP / +8 FA wideband,
  bit-identical tariff on telephony, consumed_lag flat), unlike sherpa_kws
  where fp32 won recall for free. Default stays int8; full table in
  `docs/nodes/detector-streaming-ctc.md` §Evidence.
- **`audio_pos_ms`** = output-frame index × (fbank shift × subsampling), the
  subsampling measured from the first inference (`out_per_chunk`). Refractory
  (`refractory_ms`) converted to frames.
- **BUILD/RUNTIME — `ORT_DYLIB_PATH` required.** `ort` is configured
  `load-dynamic`: it dlopens onnxruntime (1.27) at runtime rather than static-
  linking it, because `sherpa-onnx` statically links onnxruntime 1.24.2 and two
  static copies clash (linker picks one → the other's API request SIGSEGVs; see
  VERIFICATIONS streaming_ctc pin). Only this engine needs the dylib (loaded
  lazily on first Session) — sherpa_kws-only runs are unaffected. `scripts/
  fetch-models.sh` fetches the dylib and prints the `export ORT_DYLIB_PATH=…`.

## How to test in isolation
- **Inspect keyword token-cuts** with `earshot compile-keywords --config <cfg>
  [--model-dir <dir>]`: prints each keyword's Confirmed tokens per variant and,
  per `fast_prefixes` candidate, the cut tokens or the rejection reason. Reads
  `bpe.model` only (no inference); `--model-dir` analyzes another model's
  tokenizer. This is Gate (a) of the prefix-safety method (`docs/PREFIX-SAFETY.md`)
  and reuses `kws_compile::{compile, prefix_cut, render_keywords, TokenTable}`.
- Feed synthetic `AudioChunk`s directly (see `stub.rs` tests: silence never
  fires, tone fires once, refractory respected).
- sherpa_kws: `tests/sherpa_kws.rs` runs the engine against the model's
  bundled `test_wavs` (known transcripts) — both tiers fire, Fast first,
  silence/absent keywords stay quiet. SKIPs loudly when `models/` is absent.
  Keyword compilation vectors are unit tests in `kws_compile.rs`.
- M1 engines: benchmark on the recorded-call corpus matching the production
  channel (8 kHz telephony for phone events, 16 kHz wideband for webcast
  events — `docs/BAKEOFF.md` §Corpus), never on clean speech — latency
  distribution, precision, recall per chunk size.
- streaming_ctc: `ctc_scorer.rs` unit tests cover the firing logic on synthetic
  posteriors (silence/wrong-token/low-confidence never fire; Fast prefix fires
  while Confirmed waits for the last token; CTC repeat-token rule; refractory) —
  no model needed. `streaming_ctc.rs` integration tests drive a real streaming-
  CTC model end-to-end: set `EARSHOT_CTC_MODEL_DIR` to a model dir and
  `ORT_DYLIB_PATH` to the onnxruntime dylib; they SKIP loudly when unset.
  Two models qualify (both in `scripts/fetch-models.sh`): the ENGLISH
  `sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18` (landed 2026-07-17 —
  LibriSpeech BPE-500, 320 ms decode quantum, `out_per_chunk` 8; quality
  gated on the corpus bake-off, see `docs/CTC-MODEL-SOURCING.md`) and the
  Chinese `sherpa-onnx-streaming-zipformer-small-ctc` stand-in (plumbing
  only). The out-frames-per-chunk figure is export-specific — tests assert
  the model reports one, not a fixed number.
