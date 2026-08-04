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
`corpus/bridge-probes/` and a classification: `held`, `dropped`,
`operator_screened`, or `passcode_required`, with detail and probe date.

**`--promote` writes that verdict into the registry itself**, via
`scripts/registry-admit.py`, instead of printing a snippet for a human to
retype. THE MSFT RULE is unchanged — admission is still established ONLY by a
probe dial — and this is that rule working as designed: the probe records the
answer it already got. `registry-propose.py` remains the propose-only,
human-audited path for PAPER evidence, which is a different thing.

Auto-promotion is deliberately narrow. Only **`held`** is inferable from
mechanical facts (the leg ran to `--secs` with frames flowing, within 5%).
`dropped`, `operator_screened` and `passcode_required` are transcript
readings: printed for a human, never auto-written, and `--auto` refuses them.

The proven digits are persisted alongside, to
`state/hunt/numbers-probed.env` (gitignored, 0600) — distinct from the
hunt-regenerated `numbers.env`. Without this, `admission = "held"` names a
bridge nobody can dial.

This closes the 2026-08-04 SPOT gap: a toll-free number and PIN admitted us to
the live Spotify call and held 598 s of clean audio, the snippet printed
correctly, nobody pasted it, `configs/dial-ins.toml` kept saying no-entry, and
an 18-market board traded 157,217 contracts without us.

**Long captures are refused.** A short probe against an open board is normal
and necessary — that is how admission is established on event day. A probe
longer than 300 s against an issuer with an open Kalshi mention board is not a
probe at all but an undetected call capture, and is refused unless
`--corpus-only` is passed. This node listens to the entry flow, not the call.

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
