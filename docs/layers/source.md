# Layer: source

## Role
Turns some external audio feed (WAV replay, SIP dial-in, HLS webcast) into a
uniform stream of `AudioFrame`s stamped at the moment bytes arrive. It is the
ONLY layer allowed to know where audio came from (spec P1). It is NOT
responsible for: decoding beyond its own codec, VAD, chunking, or any
detection/trading concern.

## Entry door
- **Type:** a `config::SourceConfig` variant (`Replay { path, speed }`,
  `TwilioStream { bind }`, `Sip {…}`, `Hls {…}`) plus the external media
  itself.
- **Mechanism:** constructor (e.g. `ReplaySource::from_wav(path, speed,
  SourceId)`, `TwilioWsSource::bind(addr, SourceId)`), then the pipeline
  calls `AudioSource::run(self: Box<Self>, tx: RingSender<AudioFrame>)`
  (async, `src/source/mod.rs`).
- **Cadence:** `run` is called once; it owns the feed until end-of-stream.

### Adapter #2: `TwilioWsSource` (`src/source/twilio_ws.rs`, M3)
We are the WS **server**; Twilio Media Streams connects to us after
`scripts/twilio-dial.sh` places the dial-in call (`<Start><Stream>` TwiML).
Message shapes and the capture path are pinned in `docs/TWILIO.md`. Each
`media` message: base64 G.711 μ-law, 8 kHz mono, nominally 20 ms → decoded
via a const LUT (`decode_ulaw`) into one `AudioFrame` stamped at arrival
(clock domain "network" — includes WS transit, excludes the PSTN leg).
`accept_timeout` (call never lands) and `idle_timeout` (dead call) both
fail loud. Twilio `sequenceNumber` gaps are warned (network loss
visibility); our own `seq` stays dense. μ-law decode lives HERE because
μ-law is this source's codec (the "decoding beyond its own codec"
exclusion refers to downstream resampling/VAD, which stay in preproc).

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
- 20 ms frames (`replay::FRAME_MS`); replay paces with absolute deadlines
  (`sleep_until(start + n·dur)`) so timer error never accumulates. Network
  sources emit at arrival cadence (Twilio nominally delivers 20 ms frames;
  frame length is whatever the message carried — the chunker accumulates by
  sample count, not frame count).
- Per-frame `Arc<[i16]>` allocation is accepted for now; the pool seam is
  noted in `replay.rs`.

## Failure policy
- Unreadable/malformed media → error from the constructor, before any frame
  flows (fail cheap). Twilio: a taken bind port refuses at construction.
- Mid-stream I/O error → return `Err(SourceError)`; the pipeline tears down.
- Never silently resample or downmix: wrong format = error (replay:
  constructor; Twilio: `SourceError::Protocol` on any `start.mediaFormat`
  other than audio/x-mulaw 8 kHz mono).

## How to replace or edit this layer
- New source = new file in `src/source/` implementing `AudioSource` + a new
  `SourceConfig` variant + a match arm where sources are constructed
  (`main.rs::build_source` today; the arbiter later). Nothing else changes.
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
- Twilio: μ-law LUT unit tests in `src/source/twilio_ws.rs`;
  `tests/twilio_source.rs` runs a synthetic in-process WS client speaking
  the pinned message protocol against a bound source. Live: the loopback
  rehearsal (docs/TWILIO.md — a second Twilio number `<Play>`s the fixture
  WAV into a real call).
