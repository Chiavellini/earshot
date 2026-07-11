# Layer: preproc

## Role
Per-frame signal conditioning between the source and the chunker. Today a
passthrough that stamps `preproc_ts`; this is the seam where G.711 decode,
resampling, and VAD land (spec §5.3) without any re-plumbing. NOT responsible
for: accumulation (chunker's job), detection, or dropping frames.

## Entry door
- **Type:** `types::AudioFrame` (with `preproc_ts == MonotonicNs(0)`).
- **Mechanism:** `ring::RingReceiver<AudioFrame>` from the source ring
  (cap 256). `recv()` returns `None` when the source is done → clean exit.
- **Cadence:** one frame per 20 ms at real-time speed; bursts in fast replay.

## Exit door
- **Type:** the same `AudioFrame`, with `preproc_ts` stamped (and, once real
  preproc exists, `pcm`/`sample_rate_hz` possibly transformed).
- **Mechanism:** `ring::RingSender::send_blocking` into the chunker ring
  (cap 256).
- **Backpressure/drop:** interior stage — NEVER drops. `send_blocking`
  applies backpressure upstream; a stalled downstream eventually surfaces as
  ingress drops at the source (the one loud drop point).

## Obligations
- Stamp `preproc_ts = MonotonicNs::now()` exactly once per frame.
- Preserve `capture_ts`, `seq`, `source` untouched.
- When VAD lands here: tune permissive — a missed speech onset is worse than
  wasted inference (spec §5.3). Gating = not forwarding a frame is a
  SEMANTIC change to the exit door; update this doc and the chunker doc.

## Failure policy
- Upstream sender dropped → drain and return frame count (normal shutdown).
- Downstream receiver dead → `send_blocking` parks forever in theory; in
  practice pipeline teardown joins this thread after the source ends. If you
  add mid-stream fallibility, keep it loud.

## How to replace or edit this layer
- The whole layer is `pipeline::preproc::run(rx, tx) -> u64` in
  `src/pipeline/preproc.rs` — edit the loop body freely.
- Contract: entry/exit types, `preproc_ts` stamping, never-drop policy.

## How to test in isolation
- Build `ring::ring()` pairs, feed hand-made `AudioFrame`s, run `run()` on a
  plain thread, assert stamps/counts (see chunker tests for the pattern).
