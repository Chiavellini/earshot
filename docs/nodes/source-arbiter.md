# Node: `runtime.source.arbiter`

## Job

Collapse multiple equivalent live source legs into one ordered source stream by
explicit failover or first-arrival arbitration.

## Owns

Leg task fan-in, standby draining, switch timing, cross-correlation alignment,
deduplication, output resequencing, and `ArbiterMode`/`ArbiterParams` in
`src/source/arbiter.rs`, plus the shared alignment primitive in `src/xcorr.rs`.

## Does not own

It does not acquire legs, decode their protocols, run full per-leg detectors,
choose event posture, implement the shared source port, or perform
decision-stage one-shot deduplication.

## Input contracts

`SourceArbiter::new` requires at least two `Box<dyn AudioSource>` legs carrying
the same call, explicit params, and an output source ID. Take-first assumes
correlatable content; failover assumes standbys can be drained continuously.

## Output contracts

The arbiter implements `AudioSource`, preserves the winning frame's
`capture_ts`, rewrites source and sequence to one dense output stream, and ends
only after all legs are drained. Weak alignment falls back to failover.

## Side effects and exclusive resources

It spawns one task and private bounded ring per inner source. Inner resources
remain owned by their adapters; the arbiter exclusively owns selection state
and the single downstream sender.

## Failure contract

Construction with fewer than two legs fails. One failed leg is logged and may
be survived; all legs failing before useful output returns a source error.
Untrusted correlation must never select by a guessed offset.

## Parallel work boundary

Arbitration changes stay in `src/source/arbiter.rs` and `tests/arbiter.rs`.
Source decoding stays with adapters; full-chain fanout stays with
`runtime.assembly`.

## Verification

Run `cargo test --test arbiter` and `cargo test --test e2e_fanout`. Cover leg
stall, leg end, weak correlation fallback, timestamp preservation, and dense
output sequence.

## Migration status

The arbiter is a focused adapter, while configuration and the choice between
pre-detector arbitration and full-chain fanout still live in
`src/main.rs::build_pipeline_input`. A future structured `SourceGraph` should
make that selection explicit.
