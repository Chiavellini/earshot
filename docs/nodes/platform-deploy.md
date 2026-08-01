# Node: `platform.deploy`

## Job

Move one reviewed source revision and platform configuration to the target host
without corrupting host-local runtime artifacts.

## Owns

The canonical rsync deployment in `scripts/deploy-box.sh`, TLS ingress setup in
`scripts/install-tls-ingress.sh`, model artifact installation in
`scripts/fetch-models.sh`, and deployed service configuration under `deploy/`,
including `deploy/caddy/Caddyfile`.

## Does not own

It does not tune the host, build/pin an event binary, install event timers,
resolve venue media, launch an arm, manage secrets in git, or declare a run
ready.

## Input contracts

The target `DeploymentSpec` identifies source revision/worktree, destination
host/path, SSH identity, excluded host-local directories, and required
post-deploy invariants. Target identity must be resolved before mutation.

## Output contracts

Success means the allowed source tree matches the reviewed input and
post-deploy checks prove target-platform runtime libraries/configuration remain
present. The target `DeploymentReceipt` records revision, host, time, excludes,
and verification results.

## Side effects and exclusive resources

It writes and may delete files inside the explicit remote deployment root,
connects over SSH, may download model artifacts, and may install/reload TLS
service configuration. The rsync path must preserve `vendor/`, `target/`,
`models/`, `corpus/`, `state/`, credentials, and other declared host-local
assets.

## Failure contract

Unknown target, failed transfer, wrong-platform library, missing TLS/runtime
asset, or post-deploy smoke failure returns nonzero. A partial deployment is
not ready and must not trigger launch automatically.

## Parallel work boundary

Transfer/exclusion policy and platform service files stay here. Runtime tuning,
event preflight, application behavior, and launch are separate changes and
consume only the deployment receipt.

## Verification

Run `scripts/deploy-box.sh --check-only`, inspect the explicit rsync delta, then
perform the real deploy only in an authorized release flow and verify the
remote platform-library/smoke checks plus TLS configuration.

## Migration status

One guarded deployment path exists, but target defaults and receipts are shell
state/stdout, and TLS setup is adjacent rather than transactional. Add a
versioned `DeploymentSpec` and durable receipt before supporting more hosts.
