# Node: `platform.runtime`

## Job

Provide and verify the host execution environment required by an event runtime.

## Owns

Runtime tuning hooks in `src/perf.rs`, Linux tuning checks/application in
`scripts/os-tune-check.sh` and `scripts/os-tune-apply.sh`, the scheduled
runtime profile under `configs/launchd/`, and `docs/OS-TUNING.md`.

`com.earshot.preflight-nightly` is the legacy label for the nightly completion
job. Its loaded command is now the canonical `opsctl prepare-day` facade for
the current UTC date, not a preflight-only fragment. Earlier slate/stage/study
jobs remain useful head starts; this last job repeats idempotent phases and is
the only scheduled arrow that reaches scheduling plus the final readiness
audit. Using UTC makes a missed firing on morning wake target the event day
rather than silently advancing to tomorrow.

## Does not own

It does not deploy project source, prepare an event, install event timers,
decide launch time, start the arm directly, implement pipeline algorithms, or
own event recovery policy.

## Input contracts

The target `HostRuntimeSpec` declares OS/platform, CPU/core assignments,
real-time policy, required tools/libraries, and safety limits. Unsupported
platforms remain explicit.

## Output contracts

Checks produce per-knob PASS/WARN/FAIL and nonzero on hard failure. Runtime
`PerfSection` defaults to no-op and reports degraded tuning rather than
pretending it was applied.

## Side effects and exclusive resources

Applying tuning may require root and changes sysctls, governors, IRQ affinity,
and limits. CPU cores and host-level knobs are exclusive shared resources and
require coordination across concurrent events.

## Failure contract

Wrong platform, missing privilege/tool/library, unsafe RT throttling, invalid
core map, disabled user lingering, an unverified timer, or a nightly completion
job whose loaded argv does not name `opsctl prepare-day` returns nonzero or loud
degradation. No script silently edits bootloader configuration.

## Parallel work boundary

Host requirements and tuning live here. Source deployment belongs to
`platform.deploy`; event readiness consumes these checks in
`control.preflight`; assembly only consumes the validated `PerfSection`.

## Verification

Run `scripts/os-tune-check.sh --cores <expected>` on the target Linux host and
`cargo test --test perf_budget`; verify default/no-privilege degradation and
the declared tuned core map.

## Migration status

Host checks exist but their outputs are human text and preflight repeats some
platform detection. Introduce a structured `HostRuntimeReport` and let
preflight reference it by host/spec digest rather than rerunning ad-hoc checks.
