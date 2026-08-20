# Node: `control.launch`

## Job

Start exactly one isolated event runtime from ready artifacts and return a
handle that can be supervised.

## Owns

Launch sequencing and isolation in `scripts/launch-event.sh` and
`scripts/launch-webcast.sh`, with multi-session invariants in
`docs/MULTI-EVENT.md`. The owned `scripts/run-arm.sh` and
`scripts/ms-morning.sh` paths are inert retained facades that exit 64 and point
to `opsctl`; they are not alternative launchers.

## Does not own

It does not discover/qualify events, build a preflight verdict, resolve venue
internals, implement the runtime, choose retry/redial policy, grade outcomes, or
deliver notifications.

## Input contracts

Inputs are one successful immutable `PreflightReport`/ready token, its reviewed
event config and pinned runtime manifest, account binding, state directory,
and explicit live/dry posture. Webcast additionally consumes an accepted
`AccessPlan`; phone consumes the typed admission strategy/proof. Every exclusive port,
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

`arm-on-url.sh` refuses `--i-am-live` and `--fillable` when the plan's pinned
config declares `[event] rehearsal = true` (O6, 2026-08-16), using the shared
`schedule-proof.py posture` derivation. The schedulers already build rehearsal
units without those flags; this closes the hand-invocation and hand-edited-unit
route around them, and is the last layer where it can be caught. Posture lives
in the config because the plan's field set is an exact-match schema, and
`config_sha256` already binds the config into `plan_sha256`.

A live phone or webcast launch (`--i-am-live`) consumes exactly one immutable
schema-3 RunPlan. `--run-dir` may name it exactly; otherwise `eventctl status
--json` must resolve one active run for the config's event. Before starting the
arm, launch runs `eventctl verify`, refuses a duplicate or terminal attempt,
requires the account/event environment to export `EARSHOT_EXECUTION_HOST` and
`EARSHOT_KALSHI_ACCOUNT_FINGERPRINT`, matches both to the plan, and exports
`EARSHOT_RUN_PLAN`. It never derives a fingerprint from private-key material
in shell. The selected plan must be execution-enabled, match the launcher's
lane and exact config, live under the canonical event/run root, and name a
runtime-report path contained by that run. Live state is the canonical
`<repo>/state/<event>` and `--report` is the RunPlan's
`artifacts.runtime_report`, not the legacy state report.

After the arm writes a `listener.ready` owned by that arm PID, webcast waits a
bounded 20 seconds for the runtime-owned `<state-dir>/source.attached` marker
before it records `launched`; listener readiness without detector-path audio is
a pre-launch failure, not a launched session. Phone retains its listener-first
ordering because that token is the gate that permits dialing. Both live
launchers validate the `source.attached` marker,
whose run/event/plan/PID identity must match and whose positive frame count
proves the in-process evidence stage durably accepted actual post-preprocessor
media. Only then do they record `source_attached` and `supervising`; process
existence, `stream.connected`, a live manifest, and decoder success are not
substitutes. A receipt append failure asserts STOP rather than running without
lifecycle evidence. On phone, `stream.connected` remains solely the
phone-session no-stream/redial health signal.

The multi-host webcast watchdog and laptop failover make the same distinction:
`capture_ready` alone never suppresses failover and never counts as a healthy
leg. Health requires both the run's `supervising` receipt and an active (or
systemd `activating` oneshot) launcher. Thus a URL-level PCM pass followed by a
zero-frame runtime decoder pages and reaches the backup at the bounded grace
point instead of standing it down.
Failover installation is idempotent only when the already loaded wrapper names
the same laptop run, box event/run, timing, grace, and host; any mismatch is a
hard refusal rather than a second layer of wrapping.
Its schema-1 install proof also binds the AccessPlan deadline. The read-only
`--verify-install` path requires the wrapper's tail to equal the original
schedule-proof launcher argv and preserves the scheduled environment/calendar;
this is the only valid schedule reconciliation after wrapping.

After phone launch has durably recorded `source_attached` and `supervising`, it
starts one held-admission watcher for the remaining arm lifetime. The watcher
consumes each atomic `stream.connected` `(CallSid, attempt)` identity exactly
once and invokes `scripts/held-admission-receipt.py` against the selected
config, immutable RunPlan, and canonical state directory. Helper exit 0 means
the current stream is open/current. Exit 3 is expected closed policy and does
not page or assert STOP: `operator_screened`, unknown, stale, and every other
non-fresh-held venue remain untouched for their owning admission path. Exit 8
or any unexpected exit is an integrity fault: launch writes STOP before paging
`ADMISSION RECEIPT FAILED`, so notification latency cannot delay the runtime's
drain. Every redial identity is a fresh invocation and therefore renews the
nonce/current-stream receipt; the watcher never mints an operator assertion.
Launch stops and reaps the watcher on normal exit and coordinated shutdown.

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

Webcast launch owns the **live gate** (`--wait-live <url-or-url-file>
--wait-kind youtube|hls`), which runs OUTSIDE the arm and decides when to hand
off. For `hls`, LIVE means all three of: the media playlist carries no
`#EXT-X-ENDLIST`; its last segment entry ADVANCES between two consecutive
cache-busted polls; and the source yields decodable audio. Any one alone is
insufficient — a finished VOD carries `#EXTINF` on every segment, which is why
the pre-2026-08-12 `EXTINF` grep declared the CBRS venue's 15-minute VOD live
and would have armed a FILL posture against pre-recorded audio.

Each poll is cache-busted (unique query parameter plus `Cache-Control:
no-cache`) and advancement is measured on the last segment ENTRY, never on
`#EXT-X-MEDIA-SEQUENCE`: a CDN froze that header on a stream that was still
running, so a sequence-based test reports a live venue dead. Decodability is
delegated to `scripts/pcm-gate.sh`; if that fails, the gate retries once
through `scripts/cdn-cachebust-fetch.sh` and, on success, names `cachebust` as
the leg the operator must arm. **Both decode legs run under an own-process-
group watchdog** (`WAIT_LIVE_PCM_SECS` + `EARSHOT_WAIT_LIVE_DECODE_GRACE_SECS`,
default 20, clamped to the gate's remaining deadline; the fetcher pipeline
writes PCM to a file and the whole group is killed unconditionally afterward —
no orphan may hold the venue open). This is the AMAT 2026-08-13 close: the
fetcher never exits by design, bash command substitution waits for every
pipeline member, and the gate's deadline is a top-of-loop guard — one
unbounded child wedged the gate for three hours while the stream ran. An
`#EXT-X-ENDLIST` verdict never breaks the
loop — a pinned URL can still flip to the live stream — but it pages once, so
watching the wrong URL surfaces as a page rather than a silent wait. The loop
is bounded by `--wait-live-timeout-secs` (default 3600) and refuses with
`exit 7` naming the last verdict; every verdict CHANGE is journalled. Liveness
must never be gated inside a source `cmd`: a gate that emits no bytes trips the
runtime's 30 s idle guard.

`scripts/pcm-gate.sh` enforces the same `#EXT-X-ENDLIST` refusal before it
decodes anything, because `scripts/arm-on-url.sh` — the unattended path — gates
on pcm-gate and never passes `--wait-live` at all. Since 2026-08-13 pcm-gate's
playlist fetches carry the same cache-buster, its decode leg runs under the
same own-pgroup watchdog (`PCM_GATE_DECODE_GRACE_SECS`, default 20), and its
continuity verdict counts distinct playlist TAIL segments from
`scripts/pdt-log.py` (which busts and no-caches every poll), falling back to
`#EXT-X-MEDIA-SEQUENCE` only when a playlist lists no segment lines. The proof
field `distinct_media_sequences` keeps its name for schema stability and
carries the distinct-advance count. `arm-on-url.sh` retries a failing
AccessPlan resolver on a bounded schedule until the plan's `deadline_utc`
(pages once per failure class; `browser-resolve` exit 5 — a registration
wall — pages its own message and keeps retrying so an operator fix inside the
window still arms), and expands portable `$HOME/` tokens in the resolver argv
before executing it. The **PCM gate is likewise retried on a bounded schedule**
until `deadline_utc` (`EARSHOT_PCM_RETRY_SECS`, default 15): a first refusal
pages once, and only exhausting the deadline pages `NOT ARMED — PCM proof
failed`. It was single-shot until 2026-08-19 — on AS 2026-08-18 the gate ran
once ~20 s after the URL landed, hit a transient IVS 404, and the unit EXITED
while the call streamed fine (the same URL 200'd a minute later); a venue that
mints its live master a few seconds before the first segment exists (Notified,
Q4) failed the first probe for the same reason. A refused attempt removes any
partial `control/pcm-proof.json` before retrying. It also **exports** `RESOLVER_DEADLINE_EPOCH` into the
resolver's environment: the value was computed and then held in a shell
variable created by `eval`, which carries no export attribute, so no resolver
could ever read it. A resolver that blocks rather than polls —
`scripts/urlfile-wait.sh`, the box side of the resolver split — needs to know
when to stop. The environment copy only narrows a bound the AccessPlan already
fixes in the resolver argv; the plan, not the environment, stays the authority.

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
rather than placing the call itself. Phone launch also writes the admission
watcher's PID, its last non-authoritative helper result, and any fail-loud
marker as `admission-watcher.pid`, `admission-watcher.last`, and
`admission-watcher.failed` in that event state directory. Admission authority
remains the receipt plus durable assertion journal, never the watcher marker.

## Failure contract

Placeholder/stale config, missing pin/URL/model, double arm, port collision, or
child startup failure refuses without emitting `ListenerReady`. A partial
launch must clean up children and expose a nonzero result; no caller may dial
merely because a process was spawned. A phone config with no mid-call
persistence (`twilio_stream.max_reconnects` unset/0 — the Rust default) is
REFUSED unless `--one-shot-ok` names the one-shot as deliberate: a first drop
would otherwise end the run silently. Newly derived/staged phone configs carry
the generic reliability posture (`max_reconnects = 20` /
`idle_timeout_secs = 8`); older checked-in event configs must be regenerated,
not patched before market. `preflight-event.sh` fails the same config the night
before. This is distinct from the arm-time Rust check (`config.rs
check_redial_dir_for_persistence`), which refuses if persistence is set but
`EARSHOT_REDIAL_DIR` is unset — the source would re-accept but nobody would be
asked to redial.

TERM/INT are coordinated shutdown requests. The launcher first creates the
run's STOP file and waits for the arm to finish its cancellation, evidence,
and report finalizers. The phone launcher also best-effort hangs up the current
CallSid and stops redial/watchdog/tunnel helpers. In both launchers,
`--shutdown-grace-secs` defaults to 90 and refuses values below 75; only after
that budget expires does launch send TERM, wait another 10 seconds, then KILL
an unresolved arm. A KILL is explicitly an unresolved-finalizer failure. A
normal arm exit does not manufacture STOP.

Webcast no longer starts `capture-sidecar.sh` or honors
`EARSHOT_NO_SIDECAR`: the separate decoder/source could drift from detector
input and its fail-open behavior made an armed run appear auditable when it
was not. Live audio evidence is the mandatory in-process RunPlan artifact.
After arm exit, either launcher accepts completion only when the runtime report
is a regular non-symlink file with matching `run_id` and `plan_sha256`,
`finalized=true`, `runtime_success=true`, `required_evidence_healthy=true`, and
exactly `unresolved_cancellation_ids=[]`. The runtime owns the aggregate
evidence policy and subsystem diagnostics; shell does not recompute it. A
supervised run that passes appends `draining` then `completed`. A missing,
malformed, identity-mismatched, unhealthy, symlinked, or unresolved report
appends `failed`; an arm that exited before the identity-bound
`source.attached` goes directly to `failed` because `draining` requires
`supervising`. An admission-watcher integrity fault also forbids `completed`
even if the runtime report otherwise passes, and records lifecycle `failed`.

After the durable `failed` receipt, both launchers raise one critical,
RunPlan-bound `runtime_failed` alert through `platform.notify`, deduplicated by
the `lifecycle_terminal` key. The alert request, delivery reservation, outcome,
and later human acknowledgement are append-only lifecycle facts; a successful
adapter return is not treated as acknowledgement. Definite phone-delivery
failure receives the notifier's bounded retry, while an ambiguous delivery is
never blindly repeated. Alert delivery failure is reported loudly but cannot
replace or conceal the launcher's original nonzero runtime result. Legacy runs
without a RunPlan retain best-effort notification only and cannot claim the
proof-bearing alert contract.

## Parallel work boundary

Sequencing, `RunHandle`, and `ListenerReady` semantics belong here. Guards owned
by preflight, acquisition, or runtime nodes are consumed rather than
duplicated; phone dialing requires the ready token, and retry loops begin only
after the handle crosses to supervision.

Supervisor callers signal only the launcher PID, then let this node own STOP
and escalation; signaling the whole launch process group is forbidden because
it bypasses the ordered drain. The typed autorun boundary enforces this and no
longer re-arms a completed/failed RunPlan. A new attempt requires a newly
compiled RunPlan rather than an automatic restart under the same run.

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
page, the no-stream watchdog's synthetic marker, immutable RunPlan binding,
PID/identity-bound source attachment, the final-report predicate, and graceful
STOP/hangup/finalizer ordering). It also covers fresh-held initial admission,
one renewal for each changed CallSid/attempt, exit-3 closed-without-page
posture, exit-8 STOP-before-page failure, and clean watcher reaping. Run a
fully dry rehearsal with an isolated state directory. The webcast posture
suite covers
automatic and exact `--run-dir` selection, host/account/path binding, marker
ownership, the final-report predicate, pre-attachment failure ordering, removal
of the sidecar escape hatch, terminal-failure proof-alert invocation, and
STOP-before-signal drain. It also covers the live gate's four verdicts —
finished VOD refused, advancing live manifest armed, live-shaped but frozen
tail timed out, and no-decodable-audio refused — against stubbed
`EARSHOT_CURL`/`EARSHOT_PCM_GATE`/`EARSHOT_CACHEBUST_FETCH`/`EARSHOT_FFMPEG`,
so a verdict is never decided by the network. `python3 tests/capture_ready.py`
covers pcm-gate's matching ENDLIST refusal. Verify no orphan
tunnel/decoder/arm remains. The classified retry and watchdog await a live
re-verify — the SCHEDULED Q-CALLPATH rehearsal (VERIFICATIONS.md); until it
runs, this path is stub-tested, not production-mechanism-validated.

## Migration status

The wrappers encode valuable refusal guards but overlap staging, acquisition,
phone dialing, notification, and process supervision. Consolidate them behind
`LaunchRequest -> RunHandle + ListenerReady`; preserve the current safe
ready-before-dial order (the `listener.ready` token, not a port probe) while
moving the dial itself to `control.phone_session`.
Webcast's compatibility STOP file is event-scoped, while execution is
attempt-scoped. At the verified fresh-attempt boundary, the launcher
quarantines a predecessor's regular STOP only when the prior `arm.pid` is not
alive; a live predecessor is an overlap refusal. This prevents a terminal
attempt's shutdown token from killing a new immutable attempt at frame zero.
