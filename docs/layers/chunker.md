# Layer: chunker

## Role
Accumulates 20 ms frames into fixed-duration `AudioChunk`s matched to the
detector's native input (spec §5.3). Chunk size is THE §3 stage-4
latency/accuracy dial. NOT responsible for: signal transformation, VAD,
detection.

## Entry door
- **Type:** `types::AudioFrame` (preproc-stamped).
- **Mechanism:** `ring::RingReceiver<AudioFrame>` (cap 256).

## Exit door
- **Type:** `types::AudioChunk` — `pcm: Arc<[i16]>` of `chunk_ms` worth of
  samples, `first/last_capture_ts`, `first_preproc_ts`,
  `chunk_ts: MonotonicNs` (stamped at seal), `source`, `seq` (chunk index).
- **Mechanism:** `ring::RingSender<AudioChunk>::send_blocking` (cap 64) to
  the detect thread.
- **Backpressure/drop:** interior stage — never drops.

## Obligations
- Chunk sample count = `sample_rate_hz × chunk_ms / 1000`, computed from the
  first frame of each chunk (`[pipeline] chunk_ms` in config, default 160).
- **The dial (latency side):** the detector cannot see a word until the chunk
  containing it seals — pure buffering of `chunk_ms/2` mean, `chunk_ms`
  worst-case, before any model time. For `sherpa_kws` this is a PURE latency
  dial (the model re-buffers internally; accuracy bit-identical across
  80/160/320 — BAKEOFF.md), so smaller is strictly better until the
  detector's per-call overhead eats the margin: the gate is the on-box
  `perf_detector_rtf` chunk ladder (160/80/40/20), judged on RTF margin —
  NOT per-call p95, which turns bimodal below the model's internal decode
  quantum (most calls near-zero, every Nth absorbs the decode burst; the
  burst tail is structural to sherpa at every chunk size). Prod is 80 ms;
  the 40 ms candidate is gated in RUNBOOK.md ("Fire-path latency pass").
- Timestamp propagation: `first_capture_ts`/`first_preproc_ts` from the
  chunk's FIRST frame, `last_capture_ts` from its last, `chunk_ts` stamped
  when sealed. The detection-latency math depends on these exact semantics.
- Flush the remainder at end-of-stream — trailing audio is never lost.

## Failure policy
- Upstream done → flush partial chunk, return chunk count.

## How to replace or edit this layer
- The whole layer is `pipeline::chunker::run(rx, tx, chunk_ms) -> u64` in
  `src/pipeline/chunker.rs`.
- Contract: `AudioChunk` field semantics (above), never-drop policy.
- Free: overlap/striding strategies, per-source chunk sizes — as long as the
  exit type's semantics hold (document any overlap in this file).

## How to test in isolation
- Unit test pattern already in the file: feed N synthetic frames through
  `ring::ring()`, assert chunk sizes, remainder flush, and ts propagation
  (`chunks_are_exact_and_remainder_flushes`).
