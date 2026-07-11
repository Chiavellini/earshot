# Layer: source

## Role
Turns some external audio feed (WAV replay, SIP dial-in, HLS webcast) into a
uniform stream of `AudioFrame`s stamped at the moment bytes arrive. It is the
ONLY layer allowed to know where audio came from (spec P1). It is NOT
responsible for: decoding beyond its own codec, VAD, chunking, or any
detection/trading concern.

## Entry door
- **Type:** a `config::SourceConfig` variant (`Replay { path, speed }`,
  `Sip {…}`, `Hls {…}`) plus the external media itself.
- **Mechanism:** constructor (e.g. `ReplaySource::from_wav(path, speed,
  SourceId)`), then the pipeline calls
  `AudioSource::run(self: Box<Self>, tx: RingSender<AudioFrame>)` (async,
  `src/source/mod.rs`).
- **Cadence:** `run` is called once; it owns the feed until end-of-stream.

## Exit door
- **Type:** `types::AudioFrame` — `pcm: Arc<[i16]>` (mono), `sample_rate_hz`,
  `capture_ts: MonotonicNs`, `source: SourceId`, `seq: u64`,
  `preproc_ts` left as `MonotonicNs(0)`.
- **Mechanism:** `ring::RingSender<AudioFrame>` (cap 256), consumed by the
  preproc layer.
- **Backpressure/drop:** THE one legitimate drop point in the pipeline.
  Paced (live) mode: `send()` — full ring = counted drop + warn. Fast replay
  (`speed = 0`): `try_send` retry loop — lossless, blocks the replayer.
- **Completion:** returns `SourceExit::EndOfStream | Cancelled`; dropping the
  sender is the downstream shutdown signal.

## Obligations
- Stamp `capture_ts = MonotonicNs::now()` at the instant bytes arrive
  (replay: at emit — never backdated to the file timeline).
- `seq` strictly increasing per source; frames in capture order.
- 20 ms frames (`replay::FRAME_MS`); pace with absolute deadlines
  (`sleep_until(start + n·dur)`) so timer error never accumulates.
- Per-frame `Arc<[i16]>` allocation is accepted for now; the pool seam is
  noted in `replay.rs`.

## Failure policy
- Unreadable/malformed media → error from the constructor, before any frame
  flows (fail cheap).
- Mid-stream I/O error → return `Err(SourceError)`; the pipeline tears down.
- Never silently resample or downmix: wrong format = constructor error
  (mono 16-bit or bust, until a real decoder lands here).

## How to replace or edit this layer
- New source = new file in `src/source/` implementing `AudioSource` + a new
  `SourceConfig` variant + a match arm where sources are constructed
  (`main.rs::replay_source` today; the arbiter later). Nothing else changes.
- Contract (do NOT change without updating every layer doc): the
  `AudioSource` trait shape, `AudioFrame` field semantics, ingress drop
  policy.
- Free to change: internal buffering, pacing, codec handling.

## How to test in isolation
- Unit tests in `src/source/replay.rs` drive `run()` on a current-thread
  tokio runtime with a `ring::ring()` pair and a fixture WAV from
  `fixture::FixtureSpec` — no pipeline needed.
- `cargo test source::` and the e2e replay test cover pacing fidelity
  (wall-clock ±5%).
