# Node: `runtime.source.replay`

## Job

Convert one mono 16-bit WAV timeline into deterministic `AudioFrame` values for
offline and paced regression runs.

## Owns

WAV loading, 20 ms framing, replay pacing, synthetic capture timestamps, and
replay sequence behavior in `src/source/replay.rs`.

## Does not own

It does not define `AudioSource`, resample audio, interpret fixtures, run the
pipeline, score detections, or choose replay configuration.

## Input contracts

`ReplaySource::from_wav(path, speed, source_id)` accepts mono PCM16 WAV.
`speed = 0` means lossless fastest-possible replay; positive speed uses absolute
deadlines relative to the file timeline.

## Output contracts

The adapter emits 20 ms native-rate `AudioFrame`s, stamps `capture_ts` at emit,
uses dense sequence numbers from zero, and returns `SourceExit::EndOfStream`
after the final partial frame.

## Side effects and exclusive resources

It reads one WAV into memory and uses the runtime timer. It owns no network,
credential, port, subprocess, or persistent output.

## Failure contract

Unreadable WAV, stereo input, or non-16-bit input returns `SourceError`; no
frames are fabricated. Paced ring pressure is a visible ingress drop; fast
mode retries until every frame is delivered.

## Parallel work boundary

Changes stay in `src/source/replay.rs` and its focused tests. WAV schema,
`AudioFrame`, or assembly selection changes belong to their owning nodes.

## Verification

Run `cargo test source::replay` and `cargo test --test e2e_replay`. Confirm fast
mode emits all frames in order and paced mode preserves the port's drop policy.

## Migration status

The adapter is already isolated behind `AudioSource`. Its configuration
selection remains in `src/config.rs` and `src/main.rs::build_source`, and its
contract was previously embedded in `docs/layers/source.md`.
