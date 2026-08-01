# Node: `project.build`

## Job

Define the reproducible Rust workspace, dependency resolution, feature
surface, and repository build exclusions used by every executable target.

## Owns

`Cargo.toml`, `Cargo.lock`, and `.gitignore`. This node alone changes crate
targets, dependency versions and features, build profiles, or which generated
build surfaces Git excludes.

## Does not own

Runtime assembly, deployment packaging, host tuning, application
configuration, or the behavior of any crate module.

## Input contracts

None. Dependency source availability and the Rust toolchain are external
prerequisites, not inputs from another project node.

## Output contracts

`project.build.workspace`: the locked Cargo dependency graph and declared
library, binary, example, benchmark, test, and feature targets consumed by
runtime assembly, deployment, and benchmarking.

## Side effects and exclusive resources

Cargo may populate its local registry and `target/`; both are generated
surfaces outside node ownership. Updating `Cargo.lock` is an intentional,
reviewed side effect of a dependency change.

## Failure contract

An unresolved dependency, incompatible feature set, duplicate target, or
unlocked manifest change fails Cargo metadata/build checks. Consumers must not
work around the workspace with private dependency declarations.

## Parallel work boundary

Changes confined to crate implementation stay in the owning runtime,
control, platform, or evidence node. Any new dependency, target, feature, or
repository exclusion coordinates through this node as a separate contract
change.

## Verification

Run `cargo metadata --locked --no-deps`, `cargo check --locked`, and
`scripts/doc-lint.sh`.

## Migration status

The root workspace and lockfile are implemented and now have one explicit
owner. Platform-specific binary/model delivery remains behind
`platform.deploy`; it is not part of the Cargo workspace contract.
