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

The listener accepts until one connection COMPLETES the WS handshake
(deadline = `accept_timeout`): a stray scanner, health probe, or spoofed
dialer that fails the handshake is logged and dropped, and the listener
keeps waiting — a garbage TCP connect must never deny service while armed
on a public tunnel URL (live-found 2026-07-14; regression:
`tests/twilio_source.rs::stray_connection_does_not_kill_the_listener`).

`X-Twilio-Signature` is validated during the WS handshake (`accept_hdr_async`
callback → `valid_twilio_signature`, HMAC-SHA1 over the stream URL): a spoofed
dialer never becomes an audio source. The auth token comes from the config's
`[twilio]` section when present (per-account binding for concurrent events —
`build_leg` reads `auth_token_path` and pins it + `stream_url` via
`TwilioWsSource::with_auth`; see `docs/MULTI-EVENT.md` §8), otherwise from the
`TWILIO_AUTH_TOKEN` / `TWILIO_STREAM_URL` env pair (never in repo). With no
token set, validation is skipped with a loud warn — a dev/loopback-only
posture. Two distinct account tokens make this a cryptographic boundary between
two events. Scheme pinned in `VERIFICATIONS.md`.

### Adapter #4: `PipeSource` (`src/source/pipe.rs`) — the webcast lane

Generic subprocess audio: spawns a decoder command and reads **raw s16le mono
PCM** at a declared rate from its stdout, emitting one `AudioFrame` per 20 ms
of samples stamped at arrival (`clock_domain: "network"`, latency class
`Webcast` — tens of seconds behind the bridge, unmeasured; this lane is for
coverage/corpus/slow markets, never racing). Two config spellings:
`SourceConfig::Hls { url }` (sugar → `ffmpeg -i <url> -f s16le -ac 1 -ar
16000 -`) and `SourceConfig::Pipe { cmd, sample_rate_hz }` (the honest
primitive — YouTube via `sh -c "yt-dlp -o - <url> | ffmpeg -i - …"`).
Obligations kept here: dense `seq`; ring drop policy (send + warn);
dead-stream guard (`idle_timeout` 30 s, no bytes → `SourceError::Protocol`
with the decoder's stderr tail — "wall time ≠ audio"); child killed on every
exit path + `kill_on_drop` backstop (no orphan ffmpeg); trailing partial
frame flushed at EOF; empty cmd/url refused at construction (fail cheap).
The subprocess owns protocol/demux/codec; format is DECLARED to it, never
sniffed. 16 kHz default = wideband, an ASR-quality win over the phone path.
Launch: `scripts/launch-webcast.sh` (observe posture enforced — accepts no
fillable/live flags). Tests: `tests/pipe_source.rs`, incl. full-pipeline
detection parity vs `ReplaySource` on the same fixture.

### Adapter combiner: `SourceArbiter` (`src/source/arbiter.rs`)
Dual dial-in (spec §5.2). The arbiter *is itself* an `AudioSource`: it wraps
N inner sources (each run as a task feeding a private ring) and re-emits one
`AudioFrame` stream under a dense arbiter-local `seq`, so `run_pipeline` and
the `AudioFrame` contract are unchanged. Each forwarded frame keeps its own
`capture_ts` (the winning leg's true arrival — the quantity being minimized).
Two modes:
- **Failover** (default): forward the primary; on stall > `switch_ms` or leg
  death, switch to a live standby. A dropped call no longer kills the event.
- **TakeFirst**: cross-correlate the legs at startup (`xcorr::best_lag`) to
  recover the fixed inter-leg lag, then forward whichever copy of each audio
  position lands first, deduping the later. Falls back to Failover if the legs
  don't correlate. Configured via `[arbiter]` (`mode`, `switch_ms`, `align_ms`).

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
- Twilio: a bad/missing `X-Twilio-Signature` fails the WS handshake (403);
  the rejected connection is dropped and the listener keeps waiting (a
  spoofer or scanner must not kill the armed pipeline).
- Arbiter: needs ≥ 2 inners (constructor error otherwise). It returns
  `EndOfStream` once all legs end; only if *every* leg errors before producing
  a frame does it surface the first error (one dead leg must not tear down).

## How to replace or edit this layer
- New source = new file in `src/source/` implementing `AudioSource` + a new
  `SourceConfig` variant + a `main.rs::build_leg` match arm. One source →
  the direct path; ≥ 2 sources → `main.rs::build_source` wraps them in a
  `SourceArbiter`. Nothing downstream changes.
- **Raw SIP/RTP is gated** (Work Order deliverable #6): `SourceConfig::Sip`
  (`number`, `sip_uri`, `carrier`, `dtmf`, `dial_at`) parses and slots into the
  arbiter, but `build_leg` refuses to construct it until the ingress bake-off
  names a winning carrier (`docs/INGRESS-BAKEOFF.md`). When built, the SIP
  adapter owns its jitter buffer (sweepable, start ~40 ms — UDP/RTP is where we
  control buffering, unlike Twilio's WS); its own `AudioFrame`/drop obligations
  are identical to the adapters above.
- Contract (do NOT change without updating every layer doc): the
  `AudioSource` trait shape, `AudioFrame` field semantics, ingress drop
  policy.
- Free to change: internal buffering, pacing, codec handling, arbiter modes.

## How to test in isolation
- Unit tests in `src/source/replay.rs` drive `run()` on a current-thread
  tokio runtime with a `ring::ring()` pair and a fixture WAV from
  `fixture::FixtureSpec` — no pipeline needed.
- `cargo test source::` and the e2e replay test cover pacing fidelity
  (wall-clock ±5%).
- Twilio: μ-law LUT + `X-Twilio-Signature` (RFC 2202 HMAC-SHA1 known-answer +
  round-trip) unit tests in `src/source/twilio_ws.rs`; `tests/twilio_source.rs`
  runs a synthetic in-process WS client speaking the pinned message protocol
  against a bound source. Live: the loopback rehearsal (docs/TWILIO.md — a
  second Twilio number `<Play>`s the fixture WAV into a real call).
- Arbiter: `src/source/arbiter.rs` unit tests (failover switch, take-first
  dedup, single-source rejection) + `tests/arbiter.rs` (two ReplaySource legs
  → arbiter → real pipeline). `xcorr::best_lag` has its own known-lag tests.
- Ingress bake-off: `src/capture_bakeoff/` unit tests (known cross-delta,
  WAV/JSONL recording). Run live via `earshot capture-bakeoff`
  (docs/INGRESS-BAKEOFF.md); region choice via `earshot latprobe`.
