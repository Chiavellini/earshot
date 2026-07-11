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
  `emit_ts: MonotonicNs` (stamped by the detector at emission).
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
- New engine = new file in `src/detector/` implementing `Detector`, plus
  construction where detectors are built (`main.rs` today; engine selection
  becomes config in M1). Nothing else changes.
- Contract: the trait signature, `Detection` field semantics (especially the
  two timestamps), stable-partial emission.
- `EnergyStubDetector` (`stub.rs`) is permanent: it is the pipeline self-test
  with exact ground truth from `fixture::FixtureSpec`. Never delete it.

## How to test in isolation
- Feed synthetic `AudioChunk`s directly (see `stub.rs` tests: silence never
  fires, tone fires once, refractory respected).
- M1 engines: benchmark on the recorded-call corpus (8 kHz telephony), never
  on clean speech — latency distribution, precision, recall per chunk size.
