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

## Does not own

It does not prepare market content, produce admission evidence, resolve access,
install host tuning, launch the event, supervise it, or loosen a failed gate.

## Input contracts

Inputs are the reviewed prepare output, fresh admission observation when the
venue requires it, resolved `AccessPlan`, account environment path, host
runtime/deployment receipts, optional event environment, and scheduled fire
time. Prepare, admission, and acquisition may run in parallel after
qualification; all must be terminal before preflight can pass.

Two evidence inputs are consumed by name, never produced here:

- **Keyword-readiness artifact** `configs/.readiness/<config-basename>.json`,
  written by `scripts/variant-study.sh` (node `evidence.benchmark`). The gate
  recomputes `keywords_sha256` from the config through the shared extraction
  `scripts/keywords-extract.py` — the same script the artifact writer used —
  so the two sides cannot drift.
- **Admission registry** `configs/dial-ins.toml` (node `control.qualify`),
  including the venue's `admission` verdict and, for operator-screened
  venues, the `operator_handler = "rehearsed-<YYYY-MM-DD>"` rehearsal pin
  (written only by a passed operator-handler rehearsal, per
  `docs/OPERATOR-HANDLER.md` §Registry interaction).

## Output contracts

Success pins `<state-dir>/earshot-pinned` and `pinned.info` with build
provenance/hash and emits a complete PASS/WARN checklist. The target
`PreflightReport` records every check, host identity, exact input digest, and
the prepare/admission/access artifact IDs it accepted.

## Side effects and exclusive resources

It builds release code, reads live credentials, performs read-only service
probes, creates the event state directory, copies the pinned binary, and may
notify. It must not start a traded arm or place an order.

## Failure contract

Missing or stale prepare/admission/access input, or any required tool, model
heartbeat, config/allowlist, credential, disk, schedule, or pin failure returns
nonzero. WARN is explicit degradation, never silently promoted to PASS; a
failed preflight cannot produce a ready token.

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

**Operator-screened admission downgrade.** `admission = "operator_screened"`
in `configs/dial-ins.toml` remains a hard FAIL ("NO-GO for a phone arm") by
default. It downgrades to PASS-with-note — "operator-screened venue armed via
gated human-first handler (rehearsed `<date>`)" — only when BOTH hold: (a) the
preflighted config carries `[operator_handler]` with `enabled = true`, and
(b) the venue's registry entry carries `operator_handler =
"rehearsed-<YYYY-MM-DD>"` (exact format; anything else does not downgrade).
Either side missing keeps the FAIL and names the missing side.

## Parallel work boundary

Checks and report semantics stay in the two scripts above. Preparation,
admission, access resolution, host provisioning, market validation internals,
notification delivery, and launch guards remain separate; consume their
outputs instead of reimplementing them.

## Verification

Run `bash -n scripts/preflight-event.sh scripts/allowlist-diff.sh`,
exercise `scripts/collision-screen.py` on frozen candidate schedules, and run
the checks with a rehearsal config/account. There is no side-effect-free full
self-test today; verify the produced binary hash and detector-smoke result.

The keyword-readiness gate IS side-effect-free in isolation:
`scripts/preflight-event.sh --config <cfg> --only-readiness` runs only that
gate (no build, no probes, no state dir, no mail) and exits nonzero on FAIL —
exercise the fixture matrix (missing / stale / hash-mismatch / confirmed=0 /
pass) against `configs/.readiness/` fixtures.

## Migration status

The checklist is comprehensive but is one large shell hub whose primary API is
console text plus files, and not every prepare/admission/access dependency is a
typed input today. Extract a structured `PreflightReport` and `PinnedRuntime`
manifest so launch consumes one immutable readiness artifact.
