# Node: `control.admission`

## Job

Prove how a phone bridge treats a listen-only caller, publish an
evidence-backed admission classification, and bind an eligible `held` verdict
to the exact live run and stream that may consume it.

## Owns

Probe sequencing and classification guidance in `scripts/bridge-probe.sh`,
staleness/batch planning in `scripts/bridge-probe-batch.sh`, event-window
probing in `scripts/admission-window-probe.sh`, and the
`AdmissionObservation` semantics handed to the qualification registry. The
registry file itself remains owned by `control.qualify`.

`scripts/held-admission-receipt.py` is the run-time consumer for the one
mechanically reusable verdict. It converts a still-fresh, probe-established
`held` entry in the canonical `configs/dial-ins.toml` into the current phone
arm's decision-admission receipt. It neither changes the registry nor infers a
new verdict.

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
the event arm. It never asserts that a human passed an operator screen:
`operator_screened` remains owned by the explicit human assertion in
`control.operator`.

## Input contracts

Inputs are a qualified dialable venue, secret phone/DTMF values supplied
outside git, probe duration, isolated Twilio account/stream endpoint, and proof
that no traded event is armed. This work can run in parallel with preparation
and access resolution after qualification; its accepted observation is a
preflight input.

The held-receipt producer consumes only local, current evidence: the exact
event config; its immutable schema-3 phone RunPlan and verified hash-chained
lifecycle (including the plan's preparation receipts, `preflight_passed`,
`launched`, and `source_attached`); the canonical registry's exact issuer
entry; and the runtime's regular non-symlink `admission.challenge.json`,
`source.attached`, and `stream.connected` markers. The challenge PID must be
alive, its event and paths must name the selected state directory, the source
marker must match the RunPlan/event/PID and carry accepted detector-path
frames, and the stream marker must carry the current valid Twilio CallSid and
nonnegative attempt. The registry path is fixed at
`configs/dial-ins.toml`—there is no caller-selected evidence file.

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

For an already-probed `held` venue, the launcher may invoke the local producer
after both current runtime markers exist and after accepting the typed
`source_attached` lifecycle transition:

```sh
scripts/held-admission-receipt.py \
  --config <event.toml> \
  --run-plan var/runs/<EVENT_ID>/<RUN_ID>/control/run-plan.json \
  --state-dir state/<EVENT_ID>
```

Eligibility is exact: `admission` must equal `held`, `admission_probed` must be
a real non-future `YYYY-MM-DD`, and its UTC age must be at most 30 days.
`operator_screened`, `dropped`, `untested`, missing, `unknown`, and every other
value refuse without publishing a receipt. In particular, this path never
writes an `operator_admission_assertion` and never uses
`explicit_operator_pass_cli`.

Its machine exit contract lets a generic launcher distinguish posture from a
broken trust chain:

- exit `0`: published, recovered after an audit-first crash, or already open
  for this exact challenge/stream (including a preserved current human
  assertion);
- exit `3`: the canonical registry does not contain a fresh eligible `held`
  verdict for this issuer—an expected closed-gate posture for
  `operator_screened`, `dropped`, `untested`, `unknown`, missing, stale,
  future-dated, or malformed-freshness evidence;
- exit `8`: a nominally eligible invocation failed immutable RunPlan,
  lifecycle, config, registry-integrity, challenge, PID, path, source, stream,
  audit, or concurrent-change verification. This is a fail-loud/page outcome,
  not an ordinary closed gate. CLI usage errors retain argparse exit `2`.

A launcher-side watcher may call the helper once immediately **after** it has
accepted `source.attached` and durably recorded that typed transition, then
again once per atomically changed `stream.connected` marker. Do not call it
before that initial lifecycle ordering: an early call correctly exits `8`
because the current run is not yet proven attached. On each changed marker,
`0` means the current CallSid/attempt is covered, `3` means the automatic held
path is deliberately closed (leave operator/unknown handling to its own
policy), and `8` means page and assert the session-scoped STOP boundary. The
same live challenge plus a new valid CallSid/attempt is the sole stale-receipt
replacement case, so a long-lived watcher can safely rebind every redial
without weakening wrong-run/wrong-challenge refusals.

Once all identities agree, the producer takes a shared lifecycle lock and an
exclusive assertion-journal lock, appends and fsyncs a canonical hash-bearing
`held_admission_assertion` to `admission.assertions.jsonl`, fsyncs the state
directory, then atomically replaces and directory-fsyncs schema-1
`admission.receipt.json`. The assertion records the exact RunPlan/config,
canonical-registry hash and issuer/probe date, successful preflight and source
lifecycle receipt hashes, live challenge/PID, source marker, and current
CallSid/attempt. The smaller runtime receipt links back by
`assertion_sha256` and carries the event/run/plan, challenge nonce/PID, and
current CallSid/attempt. A repeated invocation is byte-idempotent; if a crash
left the durable assertion but not the receipt, the same assertion is reused.
A redial may replace only a recognizable prior receipt under the same live
challenge, binding a new assertion to the replacement CallSid/attempt. A
current explicit operator receipt is preserved, never relabelled.

**Long captures are refused.** A short probe against an open board is normal
and necessary — that is how admission is established on event day. A probe
longer than 300 s against an issuer with an open Kalshi mention board is not a
probe at all but an undetected call capture, and is refused unless
`--corpus-only` is passed. This node listens to the entry flow, not the call.

## Side effects and exclusive resources

The probe path places a real paid phone call, binds a temporary media endpoint,
may start an ngrok process, records bridge audio, transcribes it, and hangs up.
The probe exclusively owns its port, CallSid, and artifact label.

The held-receipt producer is local-only: it makes no network request, places no
call, sends no page or order, and changes no risk policy. Its only writes are
the append-only admission assertion journal and atomic current receipt in the
already-selected event state directory.

## Failure contract

The probe refuses while an arm is live unless explicitly overridden, caps
probe duration, and returns nonzero on bind/dial/capture failure. No audible
evidence means no admission classification; an IR page is never substituted.

The held-receipt producer fails closed before publication on an unverified,
inactive, terminal, wrong-lane, or mismatched RunPlan/lifecycle; config drift;
an absent/ineligible/stale/future registry verdict; a dead or mismatched PID;
wrong event/run/plan/path; malformed, stale, or replaced source/stream markers;
symlink/non-regular inputs; a corrupt assertion journal; or an incompatible
existing receipt. If anything changes after the durable audit append, it
leaves the interlock closed and publishes no receipt.

## Parallel work boundary

Admission decision and probe orchestration stay here. Media capture mechanics
are consumed from `evidence.capture`/the `bridge-probe` CLI; Twilio dialing and
source decoding remain separate nodes.

## Verification

Run `bash -n scripts/bridge-probe.sh scripts/bridge-probe-batch.sh`, exercise
batch plan mode, and validate probe JSON/WAV consistency. Real calls are
intentional evidence runs, not routine unit tests.

Run `python3 tests/held_admission_receipt.py` for isolated real-RunPlan tests of
held success/idempotence/recovery/redial plus every fail-closed identity,
freshness, lifecycle, marker, PID, path, and operator-boundary case.

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

The generic fresh-`held` receipt producer is implemented but intentionally not
wired here: `control.launch` owns `launch-event.sh` and calls this entry point
after it accepts the current source/stream attachment. Operator-screened
admission remains a separate, explicit human action.
