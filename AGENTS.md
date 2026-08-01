# EARSHOT agent protocol

This repository is designed for parallel work. The unit of ownership is a
**node**, not a convenient set of files.

Before changing anything:

1. Read `docs/STATUS.md`, then `docs/NODES.md`.
2. Select one node from `architecture/nodes.toml`.
3. Check `architecture/claims.toml`, its guide in `docs/work/ACTIVE.md`, and
   `git status`. A dirty path is owned by somebody else unless the claim
   authority says otherwise.
4. Add one exact-file `[[claim]]` in `architecture/claims.toml` before editing.
   The claim names exactly one node, a stable worker/task lane and branch, its
   state, contract-change posture, verification commands, and evidence
   targets. Never use globs or “related files.”
5. Read only that node's contract doc plus the contracts at its input/output
   doors. Read broader history only when the contract points there.

## Change rules

- One change set has one primary node and one job.
- A node may depend on another node's public contract; it may not depend on
  the other node's internals.
- If two implementations must change together, change the shared contract
  first and create one claim per node. Do not hide cross-node coupling in a
  multi-node claim or drive-by edit.
- Composers wire nodes and own lifecycle. They do not own domain policy.
- External facts go to `VERIFICATIONS.md`; current project state goes to
  `docs/STATUS.md`; procedures go to runbooks; historical investigations stay
  historical.
- Do not deploy or stage the shared working tree. Deployment must identify an
  immutable commit or release artifact.
- Never weaken the risk caps, listen-only posture, or session-scoped kill
  boundary while doing structural work.

## Definition of done

A node change is done when:

- its focused tests pass;
- affected contract/integration tests pass;
- its node or layer contract matches the code;
- any changed external fact is pinned in `VERIFICATIONS.md`;
- `scripts/doc-lint.sh` passes;
- its machine claim is removed after commit/handoff, or remains
  `complete_pending_commit` while the paths still need collision protection.

`CLAUDE.md` contains runtime-specific safety conventions. This file is the
tool-neutral collaboration and ownership contract.
