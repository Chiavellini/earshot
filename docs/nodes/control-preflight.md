# Node: `control.preflight`

## Job

Prove before event day that one exact prepared configuration, admission result,
keyword-readiness evidence, resolved access plan, binary, account, host, and
schedule can launch safely — and refuse the event when any of them is
incomplete.

## Owns

The preflight checklist and binary pinning in `scripts/preflight-event.sh` and
live-board coverage comparison in `scripts/allowlist-diff.sh`, plus candidate
schedule collision screening in `scripts/collision-screen.py` and the operator
gate checklist in `docs/RACE-CHECKLIST.md`.

Also `scripts/posture-from-study.py`, which derives per-keyword money posture
from the readiness artifact — FILL at `MENTION_FILL_CAP_CENTS` where
`confirmed / clips` clears the floor, OBSERVE at 1¢ (provably non-crossing)
below it, and OBSERVE unconditionally for any keyword with a ≤2-BPE-token
variant. It refuses on a `keywords_sha256` mismatch, so it can never disagree
with this node's blocking readiness gate — which is why it lives here rather
than with the study that produces its input. It gates **recall, not
precision**: a high ratio means the phrase is easy to hear, not that it is
right, so there is deliberately no upper cut.

## Does not own

It does not prepare market content, produce admission evidence, resolve access,
install host tuning, launch the event, supervise it, or loosen a failed gate.

## Input contracts

Inputs are the reviewed prepare output, fresh admission observation when the
venue requires it, resolved `AccessPlan`, account environment path, host
runtime/deployment receipts, a pinned event environment for unattended
date-level operation (optional only for a direct focused invocation), and
scheduled fire time. Prepare, admission, and acquisition may run in parallel after
qualification; all required strategies must be terminal before preflight can
pass. Full preflight also requires **exactly one active schema-3 RunPlan** for
the config's event. `scripts/preflight-proof.py inspect` asks `eventctl` to
resolve that unique active run, verifies its full chain and pinned config, and
publishes a self-hashed run-scoped `preflight-inputs.json` binding. An explicit
`--run-dir` is only an assertion about which unique active run was expected; it
cannot select around ambiguity.

The date-level adapter `scripts/preflight-nightly.sh` derives its work set from
immutable schema-3 RunPlans whose `starts_at_utc` falls on the requested UTC
date. It never derives the event set from `configs/*-autorun.env`: an env with
no RunPlan is not an event, while an active RunPlan with no pinned event env is
a visible per-run failure. Every selected plan and ledger is verified before
its fields are trusted. Typed terminal runs are the only skip class; one
broken active run does not prevent ready siblings from being checked. Webcast
runs must already contain the run-scoped AccessPlan produced by
`control.acquire`; this node reports a missing plan but never creates one. The
live proof consumer invokes `access-plan.py validate --canonical`, so only a
schema-2 plan with an explicit satisfied public or external-session
prerequisite can advance. Legacy schema-1 plans remain readable historical
artifacts but cannot satisfy a new preflight.

For every selected RunPlan that passes that identity/ledger verification but
fails any later nightly gate, the adapter asks `platform.notify` to raise a
critical run-bound alert with code `preflight_failed` and dedupe key
`nightly_preflight`. Subject and body depend only on immutable run identity and
the requested UTC date, so a retry cannot silently change the same request.
An unverifiable plan cannot safely anchor an alert fact; it remains in the
legacy aggregate summary instead of borrowing an untrusted identity.

Evidence inputs are consumed by name, never produced here:

- **Keyword-readiness artifact** `configs/.readiness/<config-basename>.json`,
  written by `scripts/variant-study.sh` (node `evidence.benchmark`). The gate
  recomputes `keywords_sha256` from the config through the shared extraction
  `scripts/keywords-extract.py` — the same script the artifact writer used —
  so the two sides cannot drift.
- **Admission registry** `configs/dial-ins.toml` (node `control.qualify`),
  including the venue's `admission` verdict and, for operator-screened
  venues, the `operator_handler = "rehearsed-<YYYY-MM-DD>"` rehearsal pin
  (written only by a passed operator-handler rehearsal, per
  `docs/OPERATOR-HANDLER.md` §Registry interaction). Preflight converts these
  facts into a typed run-scoped `admission-strategy.json`; it does not mint a
  registry verdict or a current-stream admission receipt.
- **Webcast AccessPlan**
  `var/runs/<EVENT_ID>/<RUN_ID>/control/access-plan.json` (node
  `control.acquire`). Its canonical schema 2 binds source/resolver argv, URL
  files, deadline, expiry, capture host/vantage, config hash and RunPlan hash,
  plus an explicit satisfied public or externally attested session
  prerequisite, while leaving current media unproven. Here, schema-3
  `access_acquired` means that canonical strategy is validated. T0 decoded PCM
  remains the later `capture_ready` proof.
- **TLS ingress identity**, supplied as `EARSHOT_STREAM_URL` or
  `EARSHOT_TLS_HOST` (+ the config's `bind` port). The ingress is a *launch*
  argument, not a config field, so preflight must be told which endpoint this
  event will actually use. Given one, it probes the live front door for a phone
  arm: certificate validity and remaining days, and whether the per-event
  `/e/<port>` path is routed at all. An unrouted port falls through the
  Caddyfile's catch-all to `respond 404`, which is indistinguishable from a
  healthy host until Twilio is refused mid-event — so 404 is a FAIL and any
  other response (400/426/502) is the PASS. Given neither, it WARNs rather than
  passing silently, which is what it did before: the phone lane's production
  ingress had no preflight check of any kind.

## Output contracts

Success pins `<state-dir>/earshot-pinned` and `pinned.info` with build
provenance/hash and emits a complete PASS/WARN checklist. The schema-1
`PreflightReport` at `<state-dir>/preflight-result.json` remains compatible
with `assert-ready.sh` and additionally records the exact RunId, RunPlan hash,
preflight-binding path/hash, lane, and strategy mode. It always says
`current_media_proven = false` for webcast or
`current_stream_admitted = false` for phone; a preflight report never claims a
future source or bridge stream already exists.

On a full green checklist, `scripts/preflight-proof.py finalize` automatically
appends only the lifecycle receipts established by that run's exact evidence:

1. `prepared` for the pinned config that passed the complete checklist;
2. `access_acquired` for a validated webcast AccessPlan, or
   `admission_resolved` for one typed phone admission strategy;
3. `study_completed` only when the RunPlan requires it and the bound readiness
   artifact exists; and
4. `preflight_passed` only after the report itself is durably published.

Prerequisite receipts are safe to retain if a later publication step fails:
they assert only their own proven fact, are idempotent, and cannot satisfy the
launch join without `preflight_passed`.

## Side effects and exclusive resources

It builds release code, reads live credentials, performs read-only service
probes, creates the event state directory, copies the pinned binary, and may
request notification through `scripts/alertctl.py`. The date adapter's
`--no-mail` posture suppresses both delivery and alert-ledger mutation. It must
not start a traded arm or place an order.

## Failure contract

Missing or stale prepare/admission/access input, or any required tool, model
heartbeat, config/allowlist, credential, disk, schedule, or pin failure returns
nonzero. WARN is explicit degradation, never silently promoted to PASS; a
failed preflight cannot produce a ready token.

At date scope, zero matching schema-3 RunPlans is also nonzero because it
cannot distinguish a truly empty date from a failed staging/RunPlan join.
Missing event envs, missing webcast AccessPlans, contradictory pinned env
metadata, and per-run integrity failures are counted individually while the
sweep continues through all siblings.

Failure of proof-bearing alert delivery is reported separately but never
replaces, clears, or changes the originating preflight failure. The sweep
continues through siblings and retains its nonzero exit. Script-level crashes,
zero-plan dates, and corrupt/unverifiable plans continue to use the aggregate
legacy notifier because no trustworthy RunPlan ledger is available.

Result publication is fail-closed and crash-durable: the finalizer fsyncs a
temporary file, atomically replaces the report, and fsyncs its directory. A
would-be PASS whose report cannot be persisted returns nonzero and does not
append `preflight_passed`. If the final receipt append itself fails, the
published report is durably rewritten red. This replaces the former
warn-and-pass behavior for a failed `preflight-result.json` write.

**Keyword-readiness gate (BLOCKING, standing policy 2026-07-29).** Applies to
every config with `[[keywords]]`, phone and webcast lanes alike — WING lost
CITR and AI to gaps that were flagged in the config but enforced by nothing.
Decision table as implemented in `readiness_gate()`:

| condition | verdict |
|---|---|
| config has no `[[keywords]]` | WARN (capture-only; gate not applicable) |
| `configs/.readiness/<basename>.json` missing | FAIL — fix printed: `scripts/variant-study.sh <config>` (needs macOS `say`; laptop, not the box) |
| artifact unreadable / not JSON | FAIL |
| `keywords_sha256` ≠ recomputed config hash | FAIL (keywords changed since the study) |
| study `date` older than 7 days | FAIL (stale; exactly 7 days still passes) |
| any keyword `confirmed == 0` | FAIL, dead words listed (diagnose via `scripts/dead-word-autopsy.sh`) |
| otherwise | PASS, listing keyword count, study date, engine, voices |

**Phone admission strategies.** `admission_resolved` means the run's
fail-closed strategy is resolved, never that a current/future Twilio stream is
already admitted. The typed modes are deliberately non-interchangeable:

| Mode | Required preflight proof | Later current-stream proof |
|---|---|---|
| `fresh_probe_held` | exact registry `held`, real nonfuture probe date no older than 30 days, executable held-receipt producer | `held-admission-receipt.py` binds the live challenge + CallSid/attempt after `source_attached` |
| `rehearsed_operator_first` | registry `operator_screened`, usable enabled handler config, real nonfuture rehearsal pin, executable operator files with expected handoff contract markers and exact hashes | explicit human `operator-page.sh --assert-admitted` action |
| `first_drop_operator_fallback` | missing/unknown/untested or stale/malformed held fact plus the same structurally checked, hashed first-drop operator capability | explicit human assertion on the replacement stream; no drop means the gate stays closed |
| `reservation_window_first_drop_fallback` | same fallback capability plus a T-90..T-35 typed probe gate | explicit human assertion unless an independent fresh-held proof later replaces the strategy |

A stale/malformed/future `held` observation is recorded as that historical
fact but is never relabelled fresh held. `dropped`, `passcode_required`, an
unrehearsed operator path, or any unknown/reservation-window venue without the
complete fallback capability is a hard failure. For reservation-window events,
pending probe state explicitly has
`underwrites_admission_resolved = false`; the already-validated fallback is
the only thing that can underwrite the receipt. At T-35 the artifact changes
from `pending-until-t35` to `fallback-activated-at-t35`; no expiring pending
receipt is minted.

## Parallel work boundary

Checks and report semantics stay in the two scripts above. Preparation,
admission, access resolution, host provisioning, market validation internals,
notification delivery, and launch guards remain separate; consume their
outputs instead of reimplementing them. This node owns the preflight alert's
severity/code/dedupe policy; `platform.notify` exclusively owns request facts,
bounded delivery, retry, and explicit acknowledgement.

## Verification

Run `bash -n scripts/preflight-event.sh scripts/allowlist-diff.sh`,
exercise `scripts/collision-screen.py` on frozen candidate schedules, and run
the checks with a rehearsal config/account. There is no side-effect-free full
self-test today; verify the produced binary hash and detector-smoke result.

Run `python3 tests/preflight_proof.py` for isolated real-eventctl tests of all
four admission strategy modes, stale-held non-equivalence, missing capability
refusal, AccessPlan enforcement, successful receipt order/chain verification,
red-report behavior, report-persistence failure, RunPlan-driven nightly
selection, per-run missing-env/AccessPlan failures, canonical schema-2
enforcement (including legacy schema-1 refusal), sibling continuation, and
zero-RunPlan refusal. These fixtures use
temporary repositories and executable local stubs only—no build, network,
credential, dial, page, or trade occurs. Run `python3 tests/access_plan.py` for
the producer-side artifact matrix. The nightly fixtures also prove one alert
per verified failed run, no run-bound alert for corrupt identity, sibling
continuation when alert delivery itself fails, and zero alert mutation under
`--no-mail`.

The keyword-readiness gate IS side-effect-free in isolation:
`scripts/preflight-event.sh --config <cfg> --only-readiness` runs only that
gate (no build, no probes, no state dir, no mail) and exits nonzero on FAIL —
exercise the fixture matrix (missing / stale / hash-mismatch / confirmed=0 /
pass) against `configs/.readiness/` fixtures.

## Migration status

The checklist remains a large shell hub, but its lane inputs, structured report
publication, and schema-3 lifecycle advancement now cross a typed proof
boundary. Remaining migration work is to extract the individual legacy checks
and a `PinnedRuntime` manifest, make staging generate AccessPlans by default,
and retire legacy consumers that trust only `preflight-result.json` instead of
the RunPlan receipt join.
