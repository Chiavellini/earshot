---
as_of: 2026-08-12
authority: architecture/claims.toml
---

# Active work

`architecture/claims.toml` is the only machine authority. This page is a human
projection for coordination; if it disagrees with the TOML, fix this page.

The shared branch currently has 47 open claims: 36 `active` and 11
`complete_pending_commit`. Eight of the latter isolate verified formatter-only
collateral by node. A claim remains open until its paths are committed
or explicitly handed off; a green focused suite does not remove collision
protection by itself.

## Reliability integration lanes

| Lane(s) | Primary nodes | Current boundary |
|---|---|---|
| `event-lifecycle-lane` | `project.architecture`, `control.operate`, `control.grade` | Canonical operations manifest/facade, RunPlan ledger, lifecycle runbook, audit integration, and governance reconciliation |
| `typed-scheduler-lane` | `control.supervise`, `control.launch`, `control.operate`, `platform.deploy`, `control.grade` | Implemented and focused-green: exact RunPlan scheduling, loaded-job proof, capture-ready proof, single-launch supervision, terminal-only retrieval |
| `preflight-proof-lane` | `control.acquire`, `control.preflight` | Immutable AccessPlan, typed admission strategy, durable green report and truthful preparation receipts; implemented, claim still open |
| `run-audit-lane` | `control.grade` | Deterministic offline order/book/audio/report audit and local evidence consolidation; FIX tape replay is the remaining boundary |
| `execution-identity-lane` | `runtime.kalshi.auth`, `runtime.assembly`, `control.prepare`, `control.operate` | Stable host/account identity and exact signer selection; implementation claims are `complete_pending_commit` |
| `order-truth-lane` | executor, order transport/evidence | Append-only wire/accounting authority and report integration |
| `decision-reliability-lane` | `runtime.decision` | Persistent one-shot state across restart |
| `book-evidence-lane` | `runtime.kalshi.book` | Exact raw WebSocket tape and finalization |
| `fix-wire-truth-lane` | `runtime.kalshi.fix` | Independent raw FIX observation tape |
| `admission-evidence-lane` | admission, capture, measure, preflight, grade | Probe/current-stream admission, in-process audio evidence, historical measurement |

Older phone, TLS, readiness, settlement, expansion, hunt, notification, build,
risk, detector, and source-pipe work remains represented by its existing claim.
Do not infer that those tasks belong to the reliability lane merely because
their paths share the dirty worktree.

## Known shared-tree hygiene

The event-specific configs/evidence and formatter-only Rust changes are now
covered by exact-file claims. Formatter collateral is isolated under eight
node-specific `complete_pending_commit` claims with no semantic intent. The
registry/doc graph is lint-clean; these claims remain until commit/handoff or a
deliberate formatting-only cleanup.

## Claim workflow

1. Read `docs/STATUS.md`, `docs/NODES.md`, the node contract, and the contracts
   at its input/output doors.
2. Inspect `git status` and `architecture/claims.toml`.
3. Extend the one existing open claim for that node with exact files, or create
   one claim if no claim exists. Never use globs or “related files.”
4. Record a stable worker/lane, branch, state, contract-change posture,
   verification commands, and evidence targets before editing.
5. Run focused tests, affected integration tests, `scripts/doc-lint.sh`, and
   the node's documented verification.
6. Remove the claim only after commit/handoff; otherwise leave it
   `complete_pending_commit` for collision protection.

No lane may stage, commit, deploy, dial, page, or trade merely to make a claim
look complete. External operations require their own explicit authorization.
