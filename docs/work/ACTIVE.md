---
as_of: 2026-07-29
authority: architecture/claims.toml
---

# Active work

`architecture/claims.toml` is the machine-readable authority for all open
collaboration claims. This page is its human guide and compact projection, not
a second registry. If this page and the TOML disagree, stop and fix the TOML.

## Current projection

| Lane | Nodes | State |
|---|---|---|
| `phone-lane` | runtime assembly/config/source port/Twilio; prepare, phone session, operator handler, supervision | active |
| `phone-lane + tls-box-lane` | launch integration | active |
| `tls-box-lane` | acquisition and deployment | active |
| `admission-evidence-lane` | qualification, admission probes, capture, and measurement | active |
| `expansion-lane` | evidence.expansion (PM measure-only capture, EXPANSION.md); census/staging/autorun files folded into the open discover/preflight/prepare/supervise claims until the pending commit closes them | active |
| `/root` | architecture/build and remaining focused contract documents | complete pending commit; still collision-protected |

The detector implementation from commit `15d4ddb` is not an open claim. Its
new focused contract documents remain protected in `/root`'s
`complete_pending_commit` claims.

## Claim workflow

1. Inspect `architecture/claims.toml` and `git status`.
2. Add one `[[claim]]` for one known node before editing its paths.
3. Use a stable task/lane label in `worker`; do not guess a person's identity.
4. List normalized repository-relative files exactly—no directories, globs,
   brace expansion, or inferred “related files.”
5. Supply `branch`, `state`, `contract_change`, verification commands, and
   evidence destinations.
6. If `contract_change = true`, include the node's registered contract doc.
7. Run `scripts/doc-lint.sh`. It rejects unknown nodes, ownership mismatch,
   duplicate paths, multiple open claims for one node, and dirty governed paths
   without exactly one claim.

`active` and `blocked` claims describe unfinished work.
`complete_pending_commit` is also open: it keeps paths collision-protected
until they are committed or explicitly handed off. Remove a claim only after
that terminal transition.
