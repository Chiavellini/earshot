# Node: `<node-id>`

## Job

One sentence describing the node's single function in the pipeline.

## Owns

The behavior, implementation paths, and schema fields this node alone may
change.

## Does not own

Adjacent responsibilities that must remain behind another node's contract.

## Input contracts

Named types or versioned artifacts, required fields, validation, and ordering.

## Output contracts

Named types or versioned artifacts, guarantees, and compatibility behavior.

## Side effects and exclusive resources

Files, ports, processes, network calls, credentials, or locks; say `None` when
the node is pure.

## Failure contract

How failure is represented, what may be retried, and what must never fail
silently.

## Parallel work boundary

Paths this node may change and the contract changes that require coordination.

## Verification

Fast local checks, focused tests, integration seam, and evidence destination.

## Migration status

What is implemented now, which legacy hubs still cross the boundary, and the
next structured artifact or extraction required.
