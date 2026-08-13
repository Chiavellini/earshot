# Node: `control.supervise`

## Job

Watch one launched run, apply bounded recovery policy, and emit lifecycle events
until the run is terminal.

## Owns

Unattended lifecycle/retry logic in `scripts/event-autorun.sh` and
`scripts/webcast-autorun.sh`, plus mid-call phone
recovery in `scripts/redial-supervisor.sh` and event-timer installation in
`scripts/install-event-timer.sh`, wrapped for the remote host by
`scripts/box-event-install.sh`. It also owns the autorun environment
templates under `configs/*-autorun.env`. The historical run accumulator
(`docs/history/RUNBOOK.md`) and the phone-lane handoff
(`docs/history/PHONE-LANE-HANDOFF.md`) are frozen evidence owned by
evidence.measure since the 2026-08-12 docs/history/ move.

`scripts/schedule-proof.py monitor` is the continuous read-only observer for
one immutable run. `--supervise` keeps a small parent that restarts a killed
monitor child. The monitor never locks, repairs, appends, signals, or creates
anything below the run directory. Its only write-capable subprocess is
`platform.notify`, pointed at a standalone alert directory outside
`var/runs/`. Launch/schedule owners must install this command as a sibling;
this node does not insert it into their forbidden launcher paths.

The owned `scripts/wing-autorun.sh` and `scripts/arm-when-url.sh` paths are
retired inert facades that exit 64. They remain only to make stale invocations
fail deterministically and name the `opsctl` replacement.

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

**Operator escalation is derived and fail-closed (2026-08-12).** At fire time
`scripts/event-autorun.sh` reads both the venue's `admission` and its
`operator_handler` rehearsal pin from `configs/dial-ins.toml`. A venue recorded
as `operator_screened` is dialable only when its event config carries a usable
`[operator_handler] enabled=true` block (valid configured cell and conference
namespace), the operator scripts are executable, and the registry carries
`operator_handler = "rehearsed-YYYY-MM-DD"`. It then passes
`--operator-handler` on the **first** dial. A missing/invalid capability, a
missing rehearsal pin, or even an explicit `OPERATOR_HANDLER=0` refuses before
the pager or bridge is called. A known silent leg into a screener is not
coverage. Explicit enable for another venue is accepted only with the same
usable config and rehearsal evidence.

An admission-unknown path is different because requiring a per-venue rehearsal
would be circular: the first live drop is what reveals the venue's screener.
For an absent/`untested`/`unknown` admission verdict (including the
reservation-window class), autorun now requires a usable enabled
`[operator_handler]` capability but does **not** require a venue rehearsal pin.
It leaves the initial dial byte-for-byte on the plain path and exports the
exact config plus `scripts/operator-redial.sh` as a dormant run-scoped
fallback. `held` remains plain and `operator_screened` remains human-first
before the initial dial.

`redial-supervisor.sh` activates that fallback on the first recognized early
`stop|idle|socket_closed|close_frame|ws_error|no_stream` marker before the
call-end grace. The replacement that consumes that marker, and every later
replacement, use the operator wrapper; the switch is latched so a later leg
cannot silently revert to the screener path. A partial/unreadable/disabled
fallback is paged and refused at that boundary, never replaced by the default
dial. Listener readiness, STOP, redial count, call-end grace, and maximum-time
gates are evaluated in their existing order and retain their existing budgets.
An abnormal first drop after grace retains the existing plain-redial policy; a
leg that held that long is not auto-classified as a newly discovered screener.

This switch is recovery routing, not an admission classifier. It writes no
registry verdict or admission receipt. Page answer, bridge origination, and a
connected stream all leave decisions closed; only the explicit current
challenge+stream-bound `operator-page.sh --assert-admitted` action opens them,
and every replacement stream invalidates the prior receipt.

This closes HIMS 26AUG10: the venue was operator-screened, the handler had been
built and rehearsed, and the loop still spent **31m47s across 17 legs** cycling
DTMF variants of a credential that was never a passcode — every leg answered,
streamed hold audio, and was hung up by a screener waiting for a name. The
handler was engaged by hand 17 minutes into the live call, after which admission
took 10 seconds and the leg held 2817 s unbroken. The deciding fact was sitting
in the registry the whole time; nothing read it.

`PASSCODE` is required to be **defined but may be empty**. An operator-screened
bridge takes a reference spoken to a screener, so that venue class has no keyable
credential; requiring non-empty refused to launch exactly the events the operator
handler exists for. The generated env distinguishes three states — `KEY=+1…,pin#`
(DTMF), `KEY=+1…` (no keyable credential ⇒ empty), and an absent line (`TBD`,
which the placeholder guard still rejects).

**One event owns one Rust process.** `event-autorun.sh` invokes
`launch-event.sh` exactly once. Mid-call drops are consumed by the
`redial-supervisor.sh` that launch starts behind that same listener; the outer
scheduler never reconstructs the arm. A recorded live arm/ngrok PID or an
already-held bind port is a collision and is refused, never killed as cleanup.
This preserves the event-scoped decision journal, order journal, report and
evidence writers through every call-leg replacement. If the sole arm exits,
autorun pages and grades that terminal outcome; it does not overwrite the run
with another attempt.

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
The optional `--first-drop-dial-script` + `--first-drop-operator-config` pair
(or inherited `EARSHOT_FIRST_DROP_DIAL_SCRIPT` +
`EARSHOT_FIRST_DROP_OPERATOR_CONFIG`) arms the admission-unknown fallback; the
presence of either half is intent, so a missing other half fails closed rather
than silently disabling escalation. The config's `[event].id` must also equal
the selected state directory basename, preventing a stale handoff from paging
another event's configured cell.

The monitor consumes newline-complete lifecycle/journal prefixes without
locking their writers; an incomplete last line is ignored until the next
poll. E2 progress inputs are sidecars next to the RunPlan artifact using the
default `<artifact>.progress.json` name, or explicit
`--progress NAME=RUN_RELATIVE_PATH:growth|heartbeat`. The minimal consumer
schema is `schema=1`, `kind=evidence-progress|tape-progress`, nonnegative
`bytes` and `last_append_monotonic_ns`, and a positive bounded
`cadence_seconds`; optional run/plan identity, when present, must match. Audio
and book use `growth`; order evidence and FIX use writer-heartbeat posture
because an event can legitimately create no order/FIX append. The EVIDENCE
lane owns production of these records and their cadence; supervision only
reads them.

Optional typed `telemetry/live.jsonl` rows use `schema=1`,
`kind=live-telemetry`, the RunPlan identity, and a `legs` list with typed
`alive`/`died` state. The legacy event journal supplies redial timestamps until
that telemetry door carries them.

## Output contracts

The target `SupervisorEvent` stream records start, heartbeat, source drop,
recovery attempt, budget exhaustion, STOP, child exit, and terminal status.
Current outputs are per-event logs, marker files, exit codes, notifications,
and a non-authoritative terminal summary. Deterministic grading is a separate
`control.grade` operation over immutable evidence.

The monitor prints schema-1 `live-monitor-snapshot` JSON only to stdout. It
raises deterministic standalone critical alerts for: missing frames/progress
past the confirmation window, invalid progress/telemetry/journal input, audio
or book growth stalled for one published cadence, all typed legs dead, two or
more consecutive redial intervals inside the 60–105 second operator-queue
band, a watched launcher disappearing before a terminal receipt, and
failed/aborted lifecycle exit before the configured end. Repeated polling and
monitor restart preserve the same IDs; `alertctl repage-due` handles bounded
re-page until explicit acknowledgement.

The terminal exit code is part of the scheduler-facing output. After the
summary is written and notification attempted, `event-autorun.sh` returns the
sole `launch-event.sh` child's nonzero status unchanged. A clean child with
detector-path audio below the configured audit floor returns supervisor status
10. Summary delivery must never turn either condition into exit 0; launchd or
systemd must retain a machine-visible failed unit for operator status/alerting.

## Side effects and exclusive resources

It watches processes/files, may hang up and redial a call, may perform bounded
pre-run retries, writes supervisor logs, and stops children. Dial/hangup effects
are delegated to `control.phone_session`; one supervisor exclusively owns
recovery policy for one `RunHandle`.
The live monitor has no recovery side effect and holds no run-tree lock. Its
supervisor may terminate/restart only its own monitor child; it has no code
path that can signal the launcher or write STOP.

## Failure contract

Budgets and deadlines are hard stops. Failed recovery is logged and terminal or
degraded according to explicit policy; it must not create an unbounded loop or
restart a traded run merely because grading later reports deafness.

A launcher/finalizer failure or unauditable audio is terminal failure, not a
successful summary run. The supervisor still performs its best-effort summary
and notification cleanup, removes its own pidfile, and then exits nonzero.

Monitor input corruption is a pageable observation failure, never permission
to repair or terminate the run. A monitor-child crash returns to its supervisor
and restarts after a bounded delay. An acknowledged deterministic alert remains
suppressed across that restart.

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
genuine second drop minutes later is not flagged. `REDIAL-NO-MEDIA`, redial
failure/budget exhaustion, and operator-fallback refusal all raise deterministic
critical alerts through the standalone `platform.notify` ledger. Each requires
the phone channel, is eligible for bounded due re-page until acknowledgement,
and never writes into the live RunPlan tree. A log line nobody reads during a
live call is the PYPL/BA silent-miss lesson again.

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
synthetic `no_stream` marker redials and hangs up the deaf CallSid. The same
suite drives all six qualifying first-drop reasons through a stub operator
wrapper, proves the switch latches without double-consuming a marker, keeps the
known/default dial unchanged, refuses+pages an invalid fallback, preserves the
post-grace stop, and verifies that routing never creates admission evidence.

`tests/operator_escalation.sh` covers the escalation rule against the **real**
script: `EARSHOT_AUTORUN_DECIDE_ONLY=1` prints the decision and exits before any
dial, pidfile claim, STOP clear, or mail, so the cases test what ships
rather than a copy of its `sed` expressions. They assert: a screened venue with
a usable, rehearsed handler auto-escalates; missing config or missing rehearsal
refuses; a `held` venue is untouched; a screened venue cannot be forced off;
explicit first-dial enable still requires rehearsal; unknown/reservation-window
venues automatically arm the first-drop fallback without a circular venue
rehearsal; missing capability refuses; an **empty** `PASSCODE` still launches;
and a `TBD` one still refuses. Stubbed wrapper cases additionally prove the
configured cell is retried only after the failed page leg is closed, the bridge
is dialed once, ambiguous page origination is not retried, and no admission
receipt is auto-minted. A fixture registry carrying
`operator_screened` inside `admission_detail` prose guards the lookup against
reading prose as a verdict. The same offline launcher seam proves a nonzero
launcher status remains scheduler-visible after the summary and that a
zero-frame clean child exits with the distinct unauditable-audio status 10;
both cases launch exactly once, write their summary, and remove the supervisor
pidfile without touching a network.

`python3 tests/schedule_proof.py` also drives the monitor entirely in temporary
run trees. It proves mid-run startup, byte-for-byte run-tree non-mutation,
one-cadence progress stall, typed all-legs-dead, the repeated-drop cadence,
early terminal exit, and a killed child being replaced with a different PID.

## Migration status

Supervision is now generic one-RunPlan shell orchestration with one launch and
launcher-PID-only shutdown. The monitor consumer and its restart supervisor
are implemented and offline-green. The CAPTURE-owned phone/webcast launcher
and schedule hooks are deliberately untouched; they must install the monitor
as a sibling before M1 is production-complete.
Marker/PID typing remains compatibility debt:
replace file polling and implicit PID records with typed
`RunHandle`/`SupervisorEvent` records without reintroducing re-arm or group
signals.
