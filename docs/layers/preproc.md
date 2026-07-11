# Layer: preproc

## Role
Per-frame signal conditioning between the source and the chunker: stamps
`preproc_ts` and, when `pipeline.resample_to_hz` is set, upsamples 8 kHz
telephony to the 16 kHz that M1 ASR engines expect. This remains the seam
where G.711 decode and VAD land (spec §5.3) without any re-plumbing. NOT
responsible for: accumulation (chunker's job), detection, or dropping frames.

## Entry door
- **Type:** `types::AudioFrame` (with `preproc_ts == MonotonicNs(0)`).
- **Mechanism:** `ring::RingReceiver<AudioFrame>` from the source ring
  (cap 256). `recv()` returns `None` when the source is done → clean exit.
- **Cadence:** one frame per 20 ms at real-time speed; bursts in fast replay.

## Exit door
- **Type:** the same `AudioFrame`, with `preproc_ts` stamped. With
  `PreprocCfg { resample_to_hz: Some(16_000) }` an 8 kHz frame exits with
  `sample_rate_hz = 16_000` and `pcm.len()` doubled; a 16 kHz frame passes
  untouched; any other rate is a loud panic (wiring bug, config validation
  only permits 16 000).
- **Mechanism:** `ring::RingSender::send_blocking` into the chunker ring
  (cap 256).
- **Backpressure/drop:** interior stage — NEVER drops. `send_blocking`
  applies backpressure upstream; a stalled downstream eventually surfaces as
  ingress drops at the source (the one loud drop point).

## Obligations
- Stamp `preproc_ts = MonotonicNs::now()` exactly once per frame, AFTER any
  resampling so the capture→preproc edge includes the DSP cost.
- Preserve `capture_ts`, `seq`, `source` untouched.
- Resampling is `HalfBandUpsampler2x`: a 16-tap Hamming-windowed-sinc
  polyphase 2× interpolator with history carried across frames (frame
  boundaries are seamless; group delay 1 ms at 8 kHz). Classical DSP only —
  never neural bandwidth extension (hallucinated high band hurts ASR).
- When VAD lands here: tune permissive — a missed speech onset is worse than
  wasted inference (spec §5.3). Gating = not forwarding a frame is a
  SEMANTIC change to the exit door; update this doc and the chunker doc.

## Failure policy
- Upstream sender dropped → drain and return frame count (normal shutdown).
- Downstream receiver dead → `send_blocking` parks forever in theory; in
  practice pipeline teardown joins this thread after the source ends. If you
  add mid-stream fallibility, keep it loud.

## How to replace or edit this layer
- The whole layer is `pipeline::preproc::run(rx, tx, cfg: PreprocCfg) -> u64`
  in `src/pipeline/preproc.rs` — edit the loop body freely. `PreprocCfg` is
  built from `pipeline.resample_to_hz` at both wiring sites.
- Contract: entry/exit types, `preproc_ts` stamping, never-drop policy.

## How to test in isolation
- Build `ring::ring()` pairs, feed hand-made `AudioFrame`s, run `run()` on a
  plain thread, assert stamps/counts (see chunker tests for the pattern).
- Resampler unit tests in `preproc.rs`: passthrough when unconfigured, 2×
  length/rate, 16 kHz untouched, unity DC gain, 440 Hz tone fidelity vs an
  ideal 16 kHz sine, bit-identical output across frame-boundary splits.
