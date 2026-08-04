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

Once the token appears, launch owns the FIRST dial (2026-08-04, Gap A) — a
classified retry, not the old fire-and-hope. `twilio-dial.sh` returns
0 (created), 4 (permanent 4xx: bad number/auth), 5 (transient 5xx / proven
no-call), or 6 (ambiguous: transport failed and a bounded `Calls.json`
recovery lookup could neither adopt an in-flight call nor prove none). Launch
retries only 5/1 (up to `--dial-attempts`, default 3, backoff 2 s then 5 s),
stops on 2/3/4, and on 6 pages `DIAL AMBIGUOUS` without a blind repeat (a
second leg into a live bridge is the 21–73 s operator-drop class). On final
failure it pages with the manual-dial command and KEEPS THE ARM ALIVE: the
900 s accept window is open and a hand dial can still save the event, so
tearing down would forfeit it. The redial supervisor is started BEFORE the
first dial (it is marker-driven and baselines existing content at startup),
so the post-dial window has a redial consumer from second zero.

Launch also owns the **no-stream watchdog** (Gap Q): a placed call is not a
stream — on 2026-07-29 calls ran to completion while nothing ever accept()ed.
The Twilio source writes `<state-dir>/stream.connected` on every accepted
`start`; if it does not appear within `--stream-confirm-secs` (default 90 s —
the stream forks at ANSWER, so real time-to-first-media is ~4–30 s), launch
pages `NO STREAM` and, with a supervisor running, writes a synthetic
`redial.request` with `reason=no_stream` and the deaf CallSid, which the
supervisor consumes to hang up the deaf leg and redial.

## Side effects and exclusive resources

It starts tunnels and the pinned arm process and writes per-event logs/state.
One event attempt exclusively owns its state directory, report path, bind port,
and stream endpoint. It hands listener readiness to phone-session control
rather than placing the call itself.

## Failure contract

Placeholder/stale config, missing pin/URL/model, double arm, port collision, or
child startup failure refuses without emitting `ListenerReady`. A partial
launch must clean up children and expose a nonzero result; no caller may dial
merely because a process was spawned. A phone config with no mid-call
persistence (`twilio_stream.max_reconnects` unset/0 — the Rust default) is
REFUSED unless `--one-shot-ok` names the one-shot as deliberate: a first drop
would otherwise end the run silently, and only slate-stage-derived configs
carry the Q-REDIAL production values (`max_reconnects = 2` /
`idle_timeout_secs = 8`). `preflight-event.sh` fails the same config the night
before. This is distinct from the arm-time Rust check (`config.rs
check_redial_dir_for_persistence`), which refuses if persistence is set but
`EARSHOT_REDIAL_DIR` is unset — the source would re-accept but nobody would be
asked to redial.

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

Run `bash -n` on the owned launch scripts (under both bash 3.2 and 5 — the box
runs 5, the laptop 3.2, and a bash-3.2 parse error has slipped in before),
`cargo test --test launch_webcast_posture`, `cargo test --test
launch_event_posture` (which now also covers the one-shot refusal, the
starts_at-derived budget echo, the classified dial retry, the ambiguous-dial
page, and the no-stream watchdog's synthetic marker), and a fully dry
rehearsal with an isolated state directory. Verify no orphan
tunnel/decoder/arm remains. The classified retry and watchdog are additionally
covered by the Q-CALLPATH live rehearsal (VERIFICATIONS.md).

## Migration status

The wrappers encode valuable refusal guards but overlap staging, acquisition,
phone dialing, notification, and process supervision. Consolidate them behind
`LaunchRequest -> RunHandle + ListenerReady`; preserve the current safe
ready-before-dial order (the `listener.ready` token, not a port probe) while
moving the dial itself to `control.phone_session`.
