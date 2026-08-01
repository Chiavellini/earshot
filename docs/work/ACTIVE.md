---
as_of: 2026-08-01
authority: architecture/claims.toml
---

# Active work

`architecture/claims.toml` is the machine-readable authority for all open
collaboration claims. This page is its human guide and compact projection, not
a second registry. If this page and the TOML disagree, stop and fix the TOML.

## Current projection

| Lane | Nodes | State |
|---|---|---|
| `/root` | project.architecture — the node registry and this claim file | active |
| `phone-lane` | runtime assembly/config/source port/Twilio; prepare, phone session, operator handler, supervision | active |
| `phone-lane + tls-box-lane` | launch integration (incl. the supervisor time budgets) | active |
| `tls-box-lane` | acquisition and deployment | active |
| `admission-evidence-lane` | qualification, admission probes, preflight's admission gate, capture, and measurement | active |
| `hunt-lane` | control.hunt — the SEC dial-in sweep and its two artifacts | active |
| `expansion-lane` | evidence.expansion (PM measure-only capture, EXPANSION.md) | active |

The 20 `complete_pending_commit` claims are gone: their paths were committed on
2026-08-01, which is the terminal transition that retires a claim. What remains
is 20 open claims, all `active`, all describing work that is genuinely
unfinished.

Two of those deserve a note, because "active" understates how differently they
are unfinished:

- `evidence.measure` additionally carries the **read-quantum lane**, cut
  mid-edit by the 2026-07-30 power loss. Its four known defects are recorded in
  the claim itself. Do not run `scripts/prefetch-gate.sh` and trust the result
  until they are fixed.
- `runtime.source.pipe` carries `scripts/hls-edge-fetch.py` from that same
  lane. The code is finished and `--selftest`-clean; what is unfinished is the
  gate that would let its default change.

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
