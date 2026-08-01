# Node: `runtime.source.port`

## Job

Define the one media-ingress interface every source adapter must implement.

## Owns

`AudioSource`, `SourceDescriptor`, `SourceExit`, `SourceError`, `SourceKind`,
and `LatencyClass` in `src/source/mod.rs`, plus their compatibility rules and
the legacy shared source contract in `docs/layers/source.md`.

## Does not own

It does not decode a codec, open a socket or file, spawn a decoder, arbitrate
legs, assign configuration variants, or wire a source into the pipeline.

## Input contracts

Assembly provides `Box<dyn AudioSource>` and a bounded
`RingSender<AudioFrame>`. An implementation must declare its descriptor before
running and must stamp each frame at the earliest locally observable arrival.

## Output contracts

`run` emits ordered `AudioFrame` values and returns
`Result<SourceExit, SourceError>`. `capture_ts` is immutable, `source` identifies
the leg, and `seq` is dense within the adapter's declared stream.

## Side effects and exclusive resources

The port itself has none. Implementations declare their files, sockets,
children, or credentials in their own node contract.

## Failure contract

Protocol, decode, and I/O failures are typed and loud. Live adapters may drop
only at the ingress ring and must log/count it; lossless offline replay may
backpressure. End of stream and cancellation are not protocol errors.

## Parallel work boundary

Adapters may evolve independently while this trait and `AudioFrame` contract
stay fixed. Changing a trait method, descriptor meaning, error taxonomy, or
frame invariant is a port change requiring assembly and every adapter contract.

## Verification

Run `cargo test source::` and every adapter's focused integration test. The
cross-port seam is exercised by `tests/e2e_replay.rs`.

## Migration status

The Rust trait is already a clean door. Configuration construction still lives
in the `src/main.rs` assembly hub, while the legacy
`docs/layers/source.md` combines all adapters and arbitration in one document.
