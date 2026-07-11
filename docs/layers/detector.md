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

## How to test in isolation
- Feed synthetic `AudioChunk`s directly (see `stub.rs` tests: silence never
  fires, tone fires once, refractory respected).
- sherpa_kws: `tests/sherpa_kws.rs` runs the engine against the model's
  bundled `test_wavs` (known transcripts) — both tiers fire, Fast first,
  silence/absent keywords stay quiet. SKIPs loudly when `models/` is absent.
  Keyword compilation vectors are unit tests in `kws_compile.rs`.
- M1 engines: benchmark on the recorded-call corpus (8 kHz telephony), never
  on clean speech — latency distribution, precision, recall per chunk size.
