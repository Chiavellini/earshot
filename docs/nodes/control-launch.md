# Node: `control.launch`

## Job

Start exactly one isolated event runtime from ready artifacts and return a
handle that can be supervised.

## Owns

Launch sequencing and isolation in `scripts/launch-event.sh`,
`scripts/launch-webcast.sh`, `scripts/run-arm.sh`, and
`scripts/ms-morning.sh`, with multi-session invariants in
`docs/MULTI-EVENT.md`.

## Does not own

It does not discover/qualify events, build a preflight verdict, resolve venue
internals, implement the runtime, choose retry/redial policy, grade outcomes, or
deliver notifications.

## Input contracts

Inputs are one successful immutable `PreflightReport`/ready token, its reviewed
event config and pinned runtime manifest, account binding, state directory,
explicit live/dry posture, and accepted `AccessPlan`. Every exclusive port,
account, and path is resolved before the runtime starts. A phone launch must not
receive permission to dial yet.

## Output contracts

The target `RunHandle` records event ID, process ID, state directory, report/log
paths, acquisition cleanup token, start time, and launch posture. For Twilio it
also emits `ListenerReady { bind_addr, stream_url, run_id }`; that token is the
phone-session entry door. Its current concrete form is the
`<state-dir>/listener.ready` file (`{"port":…,"pid":…,"unix_ts":…}`), written
by `earshot arm` only AFTER the detector is built and the pipeline is running
— NOT merely after the socket binds. `launch-event.sh` deletes any stale token
before starting the arm and gates the dial on the token appearing (120 s,
0.5 s poll, arm-liveness checked each iteration). A bare port check (`nc -z`)
is forbidden as the dial gate: the WS listener binds BEFORE the detector
build, so on 2026-07-29 the port answered all day while an ORT-less detector
build hung and the media socket sat unaccepted in the kernel backlog.

## Side effects and exclusive resources

It starts tunnels and the pinned arm process and writes per-event logs/state.
One event attempt exclusively owns its state directory, report path, bind port,
and stream endpoint. It hands listener readiness to phone-session control
rather than placing the call itself.

## Failure contract

Placeholder/stale config, missing pin/URL/model, double arm, port collision, or
child startup failure refuses without emitting `ListenerReady`. A partial
launch must clean up children and expose a nonzero result; no caller may dial
merely because a process was spawned.

## Parallel work boundary

Sequencing, `RunHandle`, and `ListenerReady` semantics belong here. Guards owned
by preflight, acquisition, or runtime nodes are consumed rather than
duplicated; phone dialing requires the ready token, and retry loops begin only
after the handle crosses to supervision.

Redial *policy* stays with `control.supervise`; what launch owns is the event's
**time frame**, because only launch knows the call this run is for. It derives
the supervisor's two budgets from `[event].starts_at` and passes them through:
`--call-end-grace` so a clean far-end stop counts as the end after 40 minutes
of real call, and `--max-minutes` so supervision runs to 150 minutes past the
call start — the same redial-window close `slate-stage.sh` already stages
against. Both anchor on the CALL, not on launch: the supervisor's own defaults
count from its own start, which is 10–20 minutes earlier, so its 2700 s grace
actually expired ~25 minutes into the call. `--max-minutes` and
`--call-end-grace` override the derivation; a config with no `starts_at` passes
neither and the supervisor keeps its historical defaults.

## Verification

Run `bash -n` on the owned launch scripts,
`cargo test --test launch_webcast_posture`, and a fully dry rehearsal with an
isolated state directory. Verify no orphan tunnel/decoder/arm remains.

## Migration status

The wrappers encode valuable refusal guards but overlap staging, acquisition,
phone dialing, notification, and process supervision. Consolidate them behind
`LaunchRequest -> RunHandle + ListenerReady`; preserve the current safe
ready-before-dial order (the `listener.ready` token, not a port probe) while
moving the dial itself to `control.phone_session`.
