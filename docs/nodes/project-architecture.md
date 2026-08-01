# Node: `project.architecture`

## Job

Keep one authoritative, checkable map from every project job to exactly one
node contract and implementation boundary.

## Owns

The node schema and topology in `architecture/nodes.toml`, this index in
`docs/NODES.md`, stable principles in `docs/SPEC.md`, the contract template in
`docs/nodes/TEMPLATE.md`, and architecture checks in `scripts/doc-lint.py` and
`scripts/doc-lint.sh`. It also owns the transition rules between the node map
and the legacy map in `docs/ARCHITECTURE.md`; repository guidance in
`AGENTS.md`, `CLAUDE.md`, and `README.md`; and the canonical current-state
documents `docs/STATUS.md`, `docs/ROADMAP.md`, and `docs/work/ACTIVE.md`.

## Does not own

It does not define a runtime algorithm, event posture, deployment target,
experiment conclusion, or the internals of any registered node.

## Input contracts

A node proposal supplies a stable ID, one job, one contract path, owned
implementation paths, dependencies, and verification commands. A boundary
change supplies updated producer and consumer contracts before registry edits.

## Output contracts

`architecture/nodes.toml` is the machine-readable ownership graph;
`docs/NODES.md` is its human projection; every registered contract uses the
headings in `docs/nodes/TEMPLATE.md`.

## Side effects and exclusive resources

Documentation and lint configuration only. This node is the exclusive writer
of node IDs, ownership assignments, and dependency edges; it must not rewrite
another node's behavioral contract as a side effect.

## Failure contract

Duplicate ownership, missing contracts, missing paths, dependency cycles, or
unresolved links are hard lint failures. An incomplete migration is allowed
only when the affected contract names the legacy hub under **Migration status**.

## Parallel work boundary

Agents may edit their claimed node contract independently. Changes to required
headings, node IDs, ownership paths, or cross-node edges belong here and must
land separately from implementation changes.

## Verification

Run `scripts/doc-lint.sh` and inspect `architecture/nodes.toml` against
`docs/NODES.md`. For legacy runtime topology, compare the result with
`docs/ARCHITECTURE.md`.

## Migration status

The current project has a typed runtime layer map in `docs/ARCHITECTURE.md` and
focused contracts in `docs/layers/`, but no complete lifecycle ownership map
was enforced. The node registry, index, and linter establish that missing
backbone without moving active implementation files.
