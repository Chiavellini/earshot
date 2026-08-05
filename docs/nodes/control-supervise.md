# Node: `control.supervise`

## Job

Watch one launched run, apply bounded recovery policy, and emit lifecycle events
until the run is terminal.

## Owns

Unattended lifecycle/retry logic in `scripts/event-autorun.sh`,
`scripts/wing-autorun.sh`, and `scripts/arm-when-url.sh`, plus mid-call phone
recovery in `scripts/redial-supervisor.sh` and event-timer installation in
`scripts/install-event-timer.sh`, wrapped for the remote host by
`scripts/box-event-install.sh`. It also owns the autorun environment
templates under `configs/*-autorun.env`, `docs/RUNBOOK.md`, and the current
phone-lane operational handoff in `PHONE-LANE-HANDOFF.md`.

`scripts/box-event-install.sh` schedules exactly one phone event on the
us-east-1 box and then **re-reads the loaded unit** rather than the file it
wrote — editing a unit file does not update the loaded job, and a stale job
missing a posture flag makes every order refuse with nothing said until the
call. It also pushes the gitignored digit store over stdin (never argv, never
git) because `scripts/deploy-box.sh` excludes `state/`, and refuses when the
config's `bind`, the env's `BIND_PORT`, and the `/e/<port>` in
`EARSHOT_STREAM_URL` disagree.

`scripts/event-autorun.sh` honors the event env's `BIND_PORT` (2026-08-05: it
was declared in every staged env and ignored, hardcoded to 8080 — so an event
binding elsewhere silently received no media on the tunnel-free box, and the
loop's stale-port cleanup could kill a concurrent event's listener). Optional
`DIAL_ATTEMPTS`, `STREAM_CONFIRM_SECS`, and `OPERATOR_HANDLER` env keys forward
to `scripts/launch-event.sh`; absent, behavior is unchanged.

## Does not own

It does not construct the runtime, invent a media endpoint, change trading
posture, broaden retry budgets, reinterpret source data, or decide PASS/FAIL
quality after termination.

## Input contracts

The node receives a `RunHandle`-equivalent, explicit deadlines/retry/redial
budgets, STOP semantics, and recovery requests such as the current atomic
`<state-dir>/redial.request` marker. `redial-supervisor.sh` consumes that
marker by CONTENT (never mtime — two drops in the same second must both
fire; whatever content exists at startup is the already-consumed baseline)
and parses its JSON `reason` field
(`stop|idle|socket_closed|close_frame|ws_error`). It also consumes launch's
`<state-dir>/listener.ready` token as a dial precondition, and takes a
`--call-end-grace <secs>` budget (default 2700) plus a `--dial-script <path>`
parameter (default `scripts/twilio-dial.sh`) so gated operator-venue dial
scripts can be substituted with identical pass-through args. A
`--hangup-script <path>` parameter (default `scripts/twilio-hangup.sh`)
exists for the same reason — stub tests must never reach the Twilio API. The
`reason` vocabulary also accepts `no_stream`: not a source drop but a
synthetic marker written by `control.launch`'s no-stream watchdog when a
placed call never produced a stream (2026-08-04, Gap Q). The supervisor needs
no special case — any non-`stop` reason redials and hangs up the deaf CallSid.

## Output contracts

The target `SupervisorEvent` stream records start, heartbeat, source drop,
recovery attempt, budget exhaustion, STOP, child exit, and terminal status.
Current outputs are per-event logs, marker files, exit codes, and notifications.

## Side effects and exclusive resources

It watches processes/files, may hang up and redial a call, may perform bounded
pre-run retries, writes supervisor logs, and stops children. Dial/hangup effects
are delegated to `control.phone_session`; one supervisor exclusively owns
recovery policy for one `RunHandle`.

## Failure contract

Budgets and deadlines are hard stops. Failed recovery is logged and terminal or
degraded according to explicit policy; it must not create an unbounded loop or
restart a traded run merely because grading later reports deafness.

Redial policy specifics (2026-07-29): a `reason=stop` marker consumed after
the `--call-end-grace` window is judged a genuine call end — the supervisor
logs the far-end call status for the old CallSid (best-effort curl to the
Twilio Calls API; `twilio-status.sh` has no per-call mode) and exits 0
without dialing; early stops and every abnormal reason still redial. The
first redial is immediate — the backoff sleep (1 -> 2 -> ... -> 30 s cap)
applies only AFTER a failed dial attempt and resets to zero on the next
success. If `<state-dir>/listener.ready` is missing the supervisor refuses
to dial (logged once per marker) but leaves the request unconsumed so it
retries each 0.25 s poll; a dial into a pipeline that cannot accept is the
2026-07-29 ORT-hang failure repeated. The zombie hangup of the previous
CallSid runs concurrently with the new dial (the source rejects zombie
streams by callSid, so ordering is safe).

**A dial is not a capture.** The dial script's exit code says Twilio accepted
the call-creation request; it cannot say the bridge admitted us or that a
single audio frame arrived. The in-band answer is the marker itself: the source
rewrites `redial.request` when it goes idle, so a fresh request arriving within
`--media-confirm-secs` (default 30 s, comfortably above the production
`idle_timeout_secs = 8` plus dial setup) means the previous redial landed and
produced nothing. That case logs `REDIAL-NO-MEDIA:` naming the attempt and the
elapsed time. It matters because the budget is small — production runs
`max_reconnects = 2` — so a bridge that admits and instantly re-drops would
otherwise consume every attempt behind two cheerful "redialed OK" lines. A
genuine second drop minutes later is not flagged. As of 2026-08-04
`REDIAL-NO-MEDIA` also PAGES via `${EARSHOT_NOTIFY:-scripts/notify.sh}` — a
log line nobody reads during a live call is the PYPL/BA silent-miss lesson
again.

Time budgets are supplied by `control.launch`, which derives them from the
event's call time; the defaults here are the fallback for a config with no
`starts_at` (see `control-launch.md`).

## Parallel work boundary

Recovery policy and lifecycle events stay here. Launch creates the handle,
Twilio source creates a recovery request, phone-session control performs the
dial/hangup, grade evaluates final artifacts, and notify delivers alerts.

## Verification

Run `bash -n` on the supervisor scripts and `cargo test --test
redial_supervisor` — the stub-driven suite this section prescribed since
2026-07-29 and that first existed on 2026-08-04. It drives the real script
with `--dial-script`/`--hangup-script` recording stubs in a scratch state dir
(no network; Twilio env removed, notify stubbed via `EARSHOT_NOTIFY`) and
proves: startup marker baselined not dialed, STOP halts without dialing,
redial budget respected, failed dial backs off and success resets,
stop-after-grace exits 0 without a dial, early stop still redials,
same-second double-marker both consumed, missing `listener.ready` refuses
then honors the pending request, `REDIAL-NO-MEDIA` flagged and paged, and the
synthetic `no_stream` marker redials and hangs up the deaf CallSid.

## Migration status

Supervision is split among event-specific shell hubs and file/process
conventions. Replace marker polling and implicit PIDs with typed
`RunHandle`/`SupervisorEvent` records before generalizing retry behavior.
