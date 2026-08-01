# Node: `control.admission`

## Job

Prove how a phone bridge treats a listen-only caller and publish an
evidence-backed admission classification.

## Owns

Probe sequencing and classification guidance in `scripts/bridge-probe.sh`,
staleness/batch planning in `scripts/bridge-probe-batch.sh`, and the
`AdmissionObservation` semantics handed to the qualification registry. The
registry file itself remains owned by `control.qualify`.

## Does not own

It does not discover or qualify the venue generally, own Twilio media decoding,
store rotating numbers, trade during a probe, prepare event markets, or launch
the event arm.

## Input contracts

Inputs are a qualified dialable venue, secret phone/DTMF values supplied
outside git, probe duration, isolated Twilio account/stream endpoint, and proof
that no traded event is armed. This work can run in parallel with preparation
and access resolution after qualification; its accepted observation is a
preflight input.

## Output contracts

The probe produces WAV/JSONL/JSON/transcript evidence under
`corpus/bridge-probes/` and a proposed classification:
`held`, `dropped`, or `operator_screened`, with detail and probe date. A human
currently copies the audited fields into the registry.

## Side effects and exclusive resources

It places a real paid phone call, binds a temporary media endpoint, may start an
ngrok process, records bridge audio, transcribes it, and hangs up. The probe
exclusively owns its port, CallSid, and artifact label.

## Failure contract

It refuses while an arm is live unless explicitly overridden, caps probe
duration, and returns nonzero on bind/dial/capture failure. No audible evidence
means no admission classification; an IR page is never substituted.

## Parallel work boundary

Admission decision and probe orchestration stay here. Media capture mechanics
are consumed from `evidence.capture`/the `bridge-probe` CLI; Twilio dialing and
source decoding remain separate nodes.

## Verification

Run `bash -n scripts/bridge-probe.sh scripts/bridge-probe-batch.sh`, exercise
batch plan mode, and validate probe JSON/WAV consistency. Real calls are
intentional evidence runs, not routine unit tests.

## Migration status

The probe creates structured capture artifacts, but classification is printed
as a snippet and copied by hand, while the `BridgeProbe` command lives in the
`src/main.rs` hub. Add an auditable `AdmissionObservation` artifact and a
separate registry-accept step so preflight can consume the accepted result.
