---
as_of: 2026-07-29
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

## Immediate outcomes

1. Finish the exactly claimed phone/TLS reliability work and preserve its
   evidence without crossing node boundaries.
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
