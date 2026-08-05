# Node: `platform.deploy`

## Job

Move one reviewed source revision and platform configuration to the target host
without corrupting host-local runtime artifacts.

## Owns

The canonical rsync deployment in `scripts/deploy-box.sh`, TLS ingress setup in
`scripts/install-tls-ingress.sh`, model artifact installation in
`scripts/fetch-models.sh`, host readiness verification in `scripts/box-ready.sh`,
and deployed service configuration under `deploy/`, including
`deploy/caddy/Caddyfile`.

`scripts/box-ready.sh` answers one question — *can the target host run this
event, right now?* — and answers it at prep time rather than at T-60. It is
read-only except under `--repair`, which refuses while `src/`, `Cargo.toml`, or
`Cargo.lock` are dirty so the deployed binary always corresponds to a commit.

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

`scripts/box-ready.sh` emits the readiness half of that receipt today:
`state/<EVENT_ID>/box-ready.json` (schema 1) carrying `go`/`fails`/`warns`, the
box binary's sha256 prefix and build time, the box's compiled
`MENTION_FILL_CAP_CENTS`, the resolved `wss://` stream URL, and one record per
gate. Exit 0 is GO, 1 is NO-GO with the first blocker named on stdout. Consumers
should treat a missing or stale artifact as NO-GO rather than as absence of
evidence.

Three facts it exists to surface, all knowable at prep time and all missed on
2026-08-05: the deployed binary must be **newer than every source file** and its
compiled `MENTION_FILL_CAP_CENTS` must agree with the tree being armed
(`mention_fill_gate` refuses any higher cap); the config's bind port must appear
in the **installed** `/etc/caddy/Caddyfile`, since every unrouted path returns
404; and `state/` is rsync-excluded, so the digit store never arrives by deploy.

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
