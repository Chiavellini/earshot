# Node: `runtime.source.pipe`

## Job

Turn a declared decoder subprocess's raw PCM stdout into a live webcast
`AudioFrame` stream.

## Owns

Decoder command spawning, stdout framing, stderr draining, idle detection,
child cleanup, and HLS `ffmpeg` command construction in `src/source/pipe.rs`,
plus the continuous HLS edge-fetch runtime adapter in
`scripts/hls-edge-fetch.py`, the cache-busting HLS segment adapter in
`scripts/cdn-cachebust-fetch.sh`, and their deaf-leg diagnostic in
`scripts/test-deaf-leg.py`.

## Does not own

It does not resolve venue URLs, decide which fetcher is fastest, supervise an
event, implement HLS/DASH codecs, process detector audio, or choose the command
for a production posture.

## Input contracts

`PipeSource::new(cmd, sample_rate_hz, source_id)` requires a non-empty argv and
nonzero rate; the command must emit mono signed 16-bit little-endian PCM on
stdout. `PipeSource::hls` is explicit 16 kHz `ffmpeg` sugar. When the edge
fetcher is in the argv chain, it consumes the preflight-approved master URL and
emits media bytes to the declared decoder; it does not resolve venue policy.

## Output contracts

PCM is emitted in 20 ms frames with dense sequence numbers and arrival-stamped
timestamps. EOF flushes a final whole-sample partial frame and returns
`EndOfStream`.

## Side effects and exclusive resources

It owns one child process and its stdio for the lifetime of the source.
`kill_on_drop` plus explicit termination prevents orphan decoders. Network and
file effects performed by the argv are declared by the acquisition plan.

## Failure contract

Empty commands and rates fail before spawn. Spawn/read errors, nonzero or dead
streams, and idle timeouts return loud source errors with a bounded stderr
tail. Ring pressure produces throttled, counted ingress-drop warnings.

## Parallel work boundary

Framing, child lifecycle, and continuous edge fetching stay in
`src/source/pipe.rs`, `scripts/hls-edge-fetch.py`,
`scripts/cdn-cachebust-fetch.sh`,
`scripts/test-deaf-leg.py`, and `tests/pipe_source.rs`. URL resolution and
command/posture selection belong to `control.acquire`; changing PCM format
requires the source-port contract.

## Verification

Run `cargo test --test pipe_source`, focused `cargo test source::pipe` tests,
and `python3 scripts/hls-edge-fetch.py --selftest`, including idle timeout,
child teardown, partial frame, and ring pressure.

## Migration status

The generic adapter is isolated, but production commands are still embedded in
TOML and staging shell scripts. Move them behind a structured
`AcquiredMedia { argv, pcm_format, provenance }` artifact instead of shared
environment and URL-file conventions.
