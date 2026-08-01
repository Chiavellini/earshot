# Node: `control.admission`

## Job

Prove how a phone bridge treats a listen-only caller and publish an
evidence-backed admission classification.

## Owns

Probe sequencing and classification guidance in `scripts/bridge-probe.sh`,
staleness/batch planning in `scripts/bridge-probe-batch.sh`, event-window
probing in `scripts/admission-window-probe.sh`, and the
`AdmissionObservation` semantics handed to the qualification registry. The
registry file itself remains owned by `control.qualify`.

Two venue classes need different probes, and treating them alike produces
false verdicts:

- **Always-on bridges** answer whenever dialled. `bridge-probe-batch.sh` plans
  these; any quiet hour will do.
- **Reservation-window bridges** exist only near their call. AAPL's rang 55 s
  to `status=no-answer` with zero media at 03:40 UTC while its published digits
  had been stable in Wayback since 2024 — the bridge was not dead, it was not
  provisioned. `admission-window-probe.sh` derives the venue and call time from
  the event config, computes T-90..T-30, and **refuses to dial outside it**:
  a no-answer collected off-hours is not evidence, and recording it as
  `dropped` would poison the registry with a verdict no probe supports. Venues
  in this class carry `reservation_window = true` in `configs/dial-ins.toml`.

Numbers for both come from `control.bridge.numbers`, never from the registry —
digits rotate and stay gitignored. Neither tool prints them.

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

The candidate-number seam is closed (`control.bridge.numbers`), and the
event-window class now has a tool rather than a paragraph. What is still
scheduled by hand is the window probe itself: `--install-timer` prints its
scheduler line and never installs it, because a cron entry that places a real
phone call to a company's bridge is a human decision, not a side effect of a
nightly job.
