# Node: `runtime.detector.sherpa`

## Job

Construct and run the Sherpa-ONNX streaming keyword-spotting engine behind the
shared synchronous detector port.

## Owns

`src/detector/sherpa_kws.rs`, `tests/sherpa_kws.rs`, and
`examples/kws_probe.rs`: Sherpa model discovery, tier-isolated spotters,
stream warm-up, label mapping, per-tier refractory behavior, and
engine-focused verification.

## Does not own

Keyword token compilation, detector selection, the `Detector` trait,
streaming CTC inference/scoring, decision policy, or model deployment.

## Input contracts

`runtime.detector.keyword_compiler`, validated `SherpaKwsConfig` and
`KeywordConfig` values supplied by the detector factory, shared runtime types,
and a readable Sherpa model directory. Runtime chunks must be mono 16 kHz.

## Output contracts

`runtime.detector.sherpa_engine`: the
`SherpaKwsDetector::new` constructor returning a `Detector` implementation.
Each `process` call returns tiered `Detection` values whose source and capture
timestamp come from the input chunk and whose emission timestamp is monotonic.

## Side effects and exclusive resources

Construction reads model/token files, initializes Sherpa/ONNX Runtime state,
and warms throwaway decoder streams. Each detector instance exclusively owns
its fast/confirmed spotters and live streams; it makes no network calls.

## Failure contract

Missing or incompatible model assets and compilation/FFI construction errors
return `DetectorBuildError::SherpaKws` and refuse arming. A non-16-kHz chunk is
a loud wiring assertion. Unknown labels are logged as errors and never mapped
to a guessed keyword.

## Parallel work boundary

Sherpa internals and their focused tests may change independently while the
constructor and `Detector` behavior stay compatible. Token rules belong to
`runtime.detector.keyword_compile`; factory selection belongs to
`runtime.detector.port`.

## Verification

Run `cargo test detector::sherpa_kws` and
`cargo test --test sherpa_kws`. The integration test skips loudly when model
assets are absent; set `EARSHOT_REQUIRE_MODELS=1` when their presence is a
required gate. Finish with `scripts/doc-lint.sh`.

## Migration status

The engine implementation is separate and its constructor is now an explicit
capability. `src/detector/factory.rs` remains the single consumer that selects
it for runtime use.
