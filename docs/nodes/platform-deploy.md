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
For a webcast config, GO additionally proves that `ffmpeg`, `curl`,
`streamlink`, `yt-dlp`, and the Node runtime selected by yt-dlp are executable
on the box, the configured detector model and Linux ORT are present, and no
macOS dylib crossed the platform boundary. Caddy, TLS, `/e/<port>` routing, and
the phone digit store are phone-only gates; they are explicitly not applicable
to a webcast config rather than WARN degradations. Parentless webcast
fetcher/decoder processes are reported as WARN evidence but are never killed.

The source deploy excludes both runtime authorities: legacy `state/` and typed
`var/runs/`. Once a schema-3 run is dispatched, the target host owns the
receipt extension and evidence bytes; a source-tree `rsync --delete` must never
shorten or remove that history. `var/host-boundary-staging/` is excluded too,
so a concurrent source deploy cannot invalidate an incoming run snapshot
between its checksum-stability check and atomic publication.

## Does not own

It does not tune the host, build/pin an event binary, install event timers,
resolve venue media, launch an arm, manage secrets in git, or declare a run
ready.

## Input contracts

The target `DeploymentSpec` identifies source revision/worktree, destination
host/path, SSH identity, excluded host-local directories, and required
post-deploy invariants. Target identity must be resolved before mutation.
`scripts/deploy-box.sh` derives the full source commit and total dirty-path
count before transfer. After the transfer and Linux-ORT invariant succeed, it
atomically writes those facts plus deployment time to `DEPLOYED-FROM` in the
box repository root. The stamp is additive provenance; it is not a substitute
for a clean immutable release.

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
evidence. For webcast configs `stream_url` remains null and the phone-only
gates appear as PASS/not-applicable records, preserving schema 1. Multiline
diagnostics remain one gate record with continuation text in `msg`; every
`verdict` is exactly `PASS`, `WARN`, or `FAIL`.

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
`models/`, `corpus/`, `state/`, `var/runs/`, the host-boundary staging
namespace, credentials, and other declared host-local assets.

## Failure contract

Unknown target, failed transfer, wrong-platform library, missing TLS/runtime
asset, or post-deploy smoke failure returns nonzero. A partial deployment is
not ready and must not trigger launch automatically.

## Parallel work boundary

Transfer/exclusion policy and platform service files stay here. Runtime tuning,
event preflight, application behavior, and launch are separate changes and
consume only the deployment receipt.

## Verification

Run `bash -n scripts/box-ready.sh scripts/deploy-box.sh scripts/fetch-models.sh`.
In a network-authorized release flow, first check remote disk headroom, run
`scripts/deploy-box.sh --check-only`, inspect the explicit rsync delta, then
perform the real deploy and verify `DEPLOYED-FROM`, the Linux-only ORT/model
set, the webcast tool gates, and a real config's `box-ready.sh` GO output.

## Migration status

One guarded deployment path exists, but target defaults and receipts are shell
state/stdout, and TLS setup is adjacent rather than transactional. Add a
versioned `DeploymentSpec` and durable receipt before supporting more hosts.
