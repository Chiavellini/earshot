---
as_of: 2026-08-01
authority: current-project-state
---

# Current status

EARSHOT has a working typed runtime spine: replay/live sources, preprocessing,
chunking, pluggable detectors, one-shot decisions, local books, bounded risk,
live-validated REST execution, offline/mock-tested FIX execution, and latency
reporting. M0–M3 acceptance work is complete; M4 remains gated by the explicit
risk posture in `docs/SPEC.md`.

The fastest measured ingress is the phone lane, but its unattended acquisition,
bridge admission, reconnection, and multi-event ingress are active reliability
work. The webcast lane is a measured production fallback and research surface,
not merely a placeholder.

## Active structural state

- The executable node registry now gives every first-party file one owner and
  every connection a producer, consumers, kind, maturity, and concrete current
  door. Dependency cycles, uncovered paths, and unresolved contracts fail
  `cargo test`.
- The runtime leaf nodes mostly have useful typed doors and focused tests.
- `src/main.rs`, the flat event config, and several 200–540 line operational
  scripts are legacy hubs that combine construction, policy, lifecycle, and
  reporting.
- Operational state is still implicit across CLI flags, sourced env files,
  logs, and `state/<event>`. There is not yet a typed `RunPlan`, `RunId`, or
  structured preflight/session result.
- Append-only evidence is valuable but interleaved with current instructions.
  `docs/RUNBOOK.md` and `docs/LATENCY-PROOF.md` are historical accumulators,
  not current-state authorities.
- A completed performance sweep exists on
  `sweep/overnight-2026-07-29`; it is not silently part of this branch.
- The 2026-07-30 session ended in a power loss with 109 uncommitted paths. That
  work is now committed. One lane was cut mid-edit and is PARKED, not finished:
  the HLS read-quantum work in `docs/LATENCY-PROOF.md` §Q1. Its 90-minute
  drop-safety gate has never run, `scripts/prefetch-gate.sh` carries four
  recorded defects, and `--chunk-ms` therefore still defaults to 0. Do not read
  §Q1 as settled. The defects are listed in that lane's claim.

## Pre-event access: what we know before a call

This is the question the phone lane starts with, and it now has an end-to-end
answer rather than a human with a browser:

- `control.hunt` sweeps SEC 8-K exhibits nightly and produces a per-issuer
  verdict. `tier = "unknown"` means the hunt COULD NOT LOOK (no 8-K matched, or
  a foreign private issuer filing 6-K) and is deliberately distinct from
  `bridge_unpublished`, which is a real negative across a scanned window.
- Found numbers reach the prober automatically through
  `control.bridge.numbers` (gitignored, 0600); `bridge-probe-batch.sh` unions
  registry and hunt candidates, so a newly found bridge is visible the same
  night. `registry-propose.py` turns verdicts into paste-ready registry rows.
- Admission remains probe-only (THE MSFT RULE) and the registry stays
  human-audited. Reservation-window venues — whose bridge does not exist
  off-hours — are marked as such and probed at T-90..T-30 by
  `admission-window-probe.sh`, which refuses to dial outside that window.

The binding constraint is now coverage, not plumbing: `admission` is set on 10
of 64 registry venues and **three** are `held` (CMG, SPOT, PGR), so
`slate-stage.sh` still stages webcast-only for almost everything by
construction. Growing `held` coverage is the highest-value phone-lane work
available.

**2026-08-04 changed how a probe's verdict reaches the registry.** SPOT and PGR
were both `held` on the morning of their calls and neither was registered,
because `bridge-probe.sh` printed a paste-ready snippet and a human had to
retype it. `bridge-probe.sh --promote` now writes the verdict itself (via
`scripts/registry-admit.py`, owned by `control.admission`) and persists the
proven digits to `state/hunt/numbers-probed.env`. THE MSFT RULE is unchanged —
only a probe dial sets `admission`, and only `held` auto-writes. Full account
in `docs/POSTMORTEM-26AUG04.md`.

## Immediate outcomes

1. Grow probed `admission` coverage — the gate is correct and starving.
   Four hunted bridges (MELI, YOU, WEN, HIMS) are one probe each from a phone
   lane; the AAPL-class venues need an in-window probe on event day.
2. Introduce a compiled run plan and structured config inspection so shell
   scripts stop parsing TOML independently.
3. Introduce run-scoped identity/artifacts and converge attended/scheduled
   launch on one lifecycle.
4. Extract the legacy control-plane hubs behind compatibility facades.
5. Harden the runtime contracts listed below in independent workstreams.

Detailed sequencing and exit criteria are in `docs/ROADMAP.md`. Concurrent
claims are in `docs/work/ACTIVE.md`; neither README nor a runbook overrides
this status.

## Known architecture debts to keep visible

- Runtime environment selection is not consistently honored by all live
  connection paths.
- Risk authorization/debit is not yet one atomic permit.
- A saturated intent channel can consume one-shot decision state before the
  intent is durably accepted.
- Mixed-format source arbitration lacks an explicit stream-format contract.
- Telemetry edge names remain stringly typed.
- Deploying directly from a dirty shared checkout is unsafe and must be
  replaced by commit-addressed release artifacts.
