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

## Exit door
- **Type:** `types::Detection` — `keyword: KeywordId` (index into the
  config keyword table), `confidence: f32`, `tier: Tier` (`Fast`/`Confirmed`),
  `audio_ts` (= `chunk.first_capture_ts` of the triggering audio),
  `emit_ts: MonotonicNs` (stamped by the detector at emission), and
  `audio_pos_ms: u64` — the triggering audio's position on the stream
  timeline (ms since first sample), wall-clock-free so bench scoring works
  in lossless speed=0 replays.
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
- Feed synthetic `AudioChunk`s directly (see `stub.rs` tests: silence never
  fires, tone fires once, refractory respected).
- sherpa_kws: `tests/sherpa_kws.rs` runs the engine against the model's
  bundled `test_wavs` (known transcripts) — both tiers fire, Fast first,
  silence/absent keywords stay quiet. SKIPs loudly when `models/` is absent.
  Keyword compilation vectors are unit tests in `kws_compile.rs`.
- M1 engines: benchmark on the recorded-call corpus (8 kHz telephony), never
  on clean speech — latency distribution, precision, recall per chunk size.
- streaming_ctc: `ctc_scorer.rs` unit tests cover the firing logic on synthetic
  posteriors (silence/wrong-token/low-confidence never fire; Fast prefix fires
  while Confirmed waits for the last token; CTC repeat-token rule; refractory) —
  no model needed. `streaming_ctc.rs` integration tests drive a real streaming-
  CTC model end-to-end: set `EARSHOT_CTC_MODEL_DIR` to a model dir (the Chinese
  `sherpa-onnx-streaming-zipformer-small-ctc` stand-in works — it exercises fbank
  + ort + cache + scorer identically) and `ORT_DYLIB_PATH` to the onnxruntime
  dylib; they SKIP loudly when unset.
