# Node: `runtime.assembly`

## Job

Turn one validated event configuration into one connected runtime graph and
return its consolidated run report.

## Owns

Crate/module exposure in `src/lib.rs`; graph wiring in `src/pipeline/mod.rs`;
source graph construction in
`src/main.rs::{build_source, build_pipeline_input}`; live attachment in
`src/main.rs::arm`; and selection between `run_pipeline` and
`run_pipeline_fanout`. It consumes `runtime.config`; it does not own
`src/config.rs`.

## Does not own

It does not implement a stage's algorithm, source protocol, market validation
rule, risk policy, order wire protocol, host tuning, or event scheduling.

## Input contracts

The entry is a validated `config::EventConfig`, resolved credentials and state
directory for live runs, and implementations of `AudioSource`, `Detector`,
market validation, and order transport. Fanout labels must be unique. An
omitted arm state path derives `state/<config event.id>`; the report defaults
under that lane directory. A live arm also consumes source-neutral durable
one-shot state at
`<state-root>/decision-<actual-kalshi-env>-<market-event-ticker>.jsonl` plus its
`.plan.json` manifest. PHONE and WEBCAST lane directories stay separate for
STOP, ledger, readiness, and reports, but map to the same decision journal when
they target the same market event. Dry-run/replay graphs use an in-memory table
and must not create or mutate live decision state.

Every live arm additionally requires an explicit absolute
`EARSHOT_RUN_PLAN` pointing to a schema-3 `control/run-plan.json`; assembly
never scans for a latest run. It verifies the plan's Python-compatible
canonical SHA-256, directory identity, exact config path/hash, event/lane/start
time, Kalshi environment, market-event ticker, stable account fingerprint,
stable execution-host ID, execution-enabled posture, and run-relative artifact
containment. `EARSHOT_EXECUTION_HOST` and
`EARSHOT_KALSHI_ACCOUNT_FINGERPRINT` must match the immutable plan. Schema-1
and schema-2 plans remain historical evidence but cannot activate execution.
The artifact map must contain exactly `receipts`, `status`, all six `audio_*`
destinations, `book_tape`, `order_evidence`, `fix_observation_tape`,
`runtime_report`, and `grade`. Every destination must be safe, run-relative,
have a pre-created parent, and resolve to a distinct path. Assembly never
synthesizes event-specific filenames or falls back to shared/latest evidence.

The read-only `earshot account-fingerprint [--config <path>]` adapter shares
the arm path's local credential selector (`load_signer_for`): a config-bound
key pair wins when present, otherwise the process environment is used. It
prints the `runtime.kalshi.auth` fingerprint and returns without constructing
a REST client, runtime, source, detector, book, or executor. In particular it
does not run the live `balance()` coherence probe; its only job is to provision
or verify the stable identity pinned by a RunPlan.

## Output contracts

The node returns a `PipelineReport` and, when execution is attached, merges
`ExecReport` records into it. A graph has one shared decision table in fanout
mode and preserves the typed doors documented in `docs/ARCHITECTURE.md`. For
a live graph, that table is constructed with
`DecisionTable::from_keywords_with_journal` before network setup or risk-gate
activation. Restarting the process—or switching source lane on the same
execution host—rehydrates `FastFired`/`Done`; it cannot re-arm an already
consumed market. The full decision plan is pinned before the first fire, so a
differently tuned failover lane refuses instead of silently changing policy.
Every live report also names `book_tape_path`, serializes the terminal
`BookTapeSummary` as `book_tape`, and sets `book_tape_healthy` only when the
writer produced a complete committed tape with no runtime fault. A failed
writer or missing terminal summary is therefore report data, not an inference
from an absent file. It likewise records terminal audio-bank and raw FIX
observation-tape accounting. `fix_observation_tape_healthy` is boolean for FIX
arms and JSON `null` for REST arms. `required_evidence_healthy` covers book,
order, audio, and (for FIX) receive evidence. `runtime_success` additionally
requires a clean drain or a coordinated STOP/SIGTERM with no unresolved
session cancellation IDs.
The `runtime.source.twilio.listener_ready` door is the token file
`<state_dir>/listener.ready` (`{"port":<u16>,"pid":<u32>,"unix_ts":<int-seconds>}`,
via `fsio::write_atomic`), written by `main.rs::arm` ONLY after
`build_pipeline_input` has returned (detector fully constructed — the point
where the 2026-07-29 ORT hang lived, invisible to a port probe because the
listener binds before the detector builds) AND the pipeline thread is spawned.
That spawned thread waits behind a one-use startup door: it cannot consume a
source frame or emit an intent until the readiness receipt is durable and the
live risk gate is armed. If the receipt write fails, assembly sends a refusal
through the door, the graph drops its sources and intent sender without
starting, and the common teardown writes `startup_error`.
`port` is the bound Twilio listener port surfaced by `build_pipeline_input`,
or `0` for configs without a Twilio source (the token is written
unconditionally on the arm path; the launcher only consumes it for phone
events). Replay never writes it. Arm removes a malformed/dead-owner stale token
before later validation, but preserves and refuses against a token whose PID
is alive. It removes stale `stream.connected` only after acquiring the live
decision-journal lease, so a racing arm cannot erase the current owner's
connected-stream evidence before losing that lease. This token is the only door
that allows phone-session control to dial.

The lifecycle `source_attached` fact is `<state_dir>/source.attached`, written
atomically only after the in-process audio bank reports its first accepted
detector-path frame. A bound port, connected call, or running source task is
not sufficient. Assembly removes a stale marker only after it owns the durable
decision lease and has verified the RunPlan, and rechecks the counter after the
pipeline/select race before finalization.

Live phone/SIP arms also create `admission.challenge.json` with a fresh
process nonce and remove any prior `admission.receipt.json`. The detector keeps
running and reports pre-admission hits, but the interlock sits before
`DecisionTable::decide_checked`: while closed, a `DetectionRecord` carries
`suppression="pre_admission"`, no intent is emitted, and the durable one-shot
state is not consumed. The phone supervisor opens the door only by atomically
writing schema-1 `admission.receipt.json` with the same event id, nonce, and
`status="admitted"`; Twilio receipts must additionally echo the current
`stream.connected` marker's `call_sid` and `attempt` as `call_sid` and
`stream_attempt`. A reconnect replaces that marker, so the previous receipt
closes automatically before any new-stream detection can consume one-shot
state. Malformed, stale, wrong-event, wrong-nonce, and wrong-stream receipts
remain closed. Replay, dry runs, and non-phone sources use an always-open door.

## Side effects and exclusive resources

It spawns source tasks, stage threads, detector instances, book/executor work,
and writes the requested report path. A live event exclusively owns its state
directory, report file, STOP marker, ports, configured account/session, and
canonical decision journal. A cloned journal lease remains held through
executor drain, ledger flush, and final report persistence. The decision
journal persists across process attempts for that market event; it is not a
run-attempt artifact.

Assembly opens and exclusively holds the RunPlan-scoped
`orders/order-evidence.jsonl` before activation. It replays the journal and
performs exact-client-ID REST recovery for every unresolved request; a known
order is cancelled session-scope first, and any absent, unknown, or
nonterminal result blocks activation. The journal lease remains held through
executor drain and report persistence. Any writer-health fault enters the
same fail-closed gate-kill and continuous cancellation path as a decision
journal fault.

For live runs assembly also opens the RunPlan-scoped raw Kalshi book tape
exclusively and durably writes its session header **before** starting the book
WebSocket. The same `run_ws_url_tapped` task both maintains the executor's
wait-free `BookCache` and offers byte-verbatim frames to the non-blocking
`FrameTap`; there is no second independently drifting feed. Assembly selects
on both the writer failure subscription and premature writer completion for
the entire activated run. Shutdown is ordered: request close intent, abort and
join the WebSocket producer so the final tap drops, then await and validate the
writer summary/commit before writing `report.json` on every in-process drain.
Dry runs retain the unrecorded reader and cannot claim a RunPlan book artifact.

The in-process audio bank is the last fallible recorder opened before ingress
activation. Every detector-visible frame crosses its mandatory tap and the
forwarding count is reconciled against the bank on shutdown. A healthy drain,
including a resolved coordinated STOP/SIGTERM, commits only when at least one
frame was published. Source, writer, accounting, or zero-frame failure leaves
explicit partial evidence and makes the run non-successful.

A live FIX arm opens the RunPlan-scoped raw receive tape before connecting and
passes its tap through the transport's mandatory-observer constructor. After
executor drain assembly requests close, performs bounded FIX Logout/shutdown,
drops the final transport producer, and requires a complete committed banker
summary. REST arms do not create or claim this artifact. Book and FIX
finalizers run concurrently; audio is finalized after their outcomes are known.

Live assembly enforces one absolute, canonical state root before journal
selection. The default is `<repository>/state`, found from the canonical config
path rather than the caller's working directory. An immutable RunPlan may pin
another absolute root in `EARSHOT_CANONICAL_STATE_ROOT`; the selected live
`--state-dir` must be its direct event child. Per-invocation alternate roots are
refused because they would create independent locks for the same market event.
Dry runs may still use arbitrary temporary roots.

The journal and `flock` fence only processes sharing one filesystem. The
operational contract therefore permits exactly **one live execution host per
Kalshi environment + market event**. Other hosts may capture or dry-run, but
may not arm execution. The immutable RunPlan must name that host and launchers
must enforce it. Cross-host active-active requires a centralized fenced state
service or a single centralized executor; local files must never be described
as solving it.

## Failure contract

The required target contract is: validate and construct every fallible
dependency before activating the risk gate or exposing a live run. Stage
failures are returned and recorded; sibling fanout legs may continue. Assembly
must never substitute a missing dependency or silently select a different
graph.

A corrupt, truncated, config-incompatible, or already-locked decision journal
refuses the live arm before any network setup or gate activation. Assembly
never falls back to a fresh in-memory table. Dry runs deliberately select the
in-memory constructor so rehearsal cannot consume production one-shot state.
The configured Kalshi environment is resolved to the actual REST and WebSocket
endpoints before journal selection; an unknown or FIX/non-prod mismatch is a
startup refusal, so a config string cannot select fresh state while still
trading prod. A live FIX arm also refuses a host or port other than the pinned,
verified production endpoint; dry connection experiments may override it but
cannot silently inherit production decision-state identity.

A mid-run append/sync failure publishes through `DecisionHealth`. Assembly
immediately kills the risk gate, removes readiness/stream markers, cancels the
current session registry, and writes archival plus latest decision-fault
receipts. While the source graph drains it polls for order ids registered after
the first snapshot and cancels each new id; it rechecks health after the
pipeline/select race and cancels the final registry again after executor drain.
The partial `PipelineReport` carries
`decision_journal_error`, and the terminal reason is `decision_state_error`,
never a benign ignored detection, source error, or panic.

A book-tape open/header error refuses before activation. A mid-run writer
failure, channel closure, premature completion, tap drop, accepted-but-
unwritten frame, incomplete terminal accounting, missing readiness proof, or
failed commit makes the run non-clean. Assembly immediately kills the risk
gate, closes the session registry, removes live attachment markers, cancels
session orders, and keeps cancelling late-registered ids while the pipeline
drains. The recorder producer is stopped immediately, but the writer is still
joined so its terminal accounting is preserved. The current telemetry enum
classifies this under `order_evidence_error`; `book_tape_error` and the full
`book_tape` summary identify the actual subsystem without ambiguity.

STOP, SIGINT, and SIGTERM use `risk::spawn_coordinated_kill_watcher`. The
watcher kills the gate, closes the session registry, reconciles and cancels
only this run's orders, flushes the ledger, and hands a typed event to assembly.
Assembly cancels every source leg, drains pipeline and executor, performs a
final bounded late-ID reconciliation, finalizes every recorder, and writes the
runtime report before acknowledging the watcher. Internal evidence containment
also kills the gate but is not misclassified as an external signal merely
because both share that safety boundary. If final report persistence fails,
assembly deliberately drops rather than acknowledges the typed event, waits
for the watcher's explicit `EventDropped` release, and returns a non-zero error;
a missing terminal report can never be certified as successful teardown.

**Every terminated run leaves a record.** `arm()` routes all post-listener exit
paths through one choke point, `finish_run(report_path, event_id, report, exec,
exit)`, which prints the summary and writes `telemetry::build_run_json(...)`.
The pipeline's outcome selects the `ExitReason` — it must never decide *whether*
a report exists:

| pipeline outcome | `ExitReason` | report |
|---|---|---|
| readiness/startup door refusal | `startup_error` | partial + `exit.detail`, source never released |
| `Ok(Ok(report))` | `clean_drain` | full |
| `Ok(Err(e))`, single source | `source_error` | partial + `exit.detail` |
| `Ok(Err(e))`, fanout | `all_legs_died` | partial + `exit.detail` |
| decision append/sync failure | `decision_state_error` | partial + `decision_journal_error` + fault receipt |
| book/audio/FIX tape failure or incomplete commit | `order_evidence_error` | full/partial + subsystem error + terminal accounting when available |
| resolved STOP/SIGINT | `stop_file` | finalized; success only with healthy mandatory evidence |
| resolved SIGTERM | `sigterm` | finalized; success only with healthy mandatory evidence |
| kill with unresolved IDs | `stop_file` / `sigterm` | finalized + `unresolved_cancellation_ids`; non-successful |
| join error / panic | `panic_or_join` | partial + `exit.detail` |

The failure this encodes: before 2026-08-05, `let report = pipeline_task.await??`
skipped every line below it, so an errored run wrote nothing. MELI armed, took 4
detections, sent one real order, then hit the pipe idle guard — and left no
evidence it had ever run. Adding a new early return that bypasses `finish_run`
re-opens that hole.

## Parallel work boundary

Only this node connects runtime nodes. Stage internals and wire behavior are
out of scope; a new connection starts with both endpoint contracts and lands
as a focused assembly change.

## Verification

Run `cargo test --test e2e_full_chain`, `cargo test --test e2e_replay`,
`cargo test --test e2e_fanout`, and `cargo test --test perf_budget`.
`pipeline_reconstruction_preserves_event_one_shot_state` runs the real
pipeline twice against one journal and proves that attempt two still hears the
fixture but emits no duplicate intent.
`assembly_boundary_shares_live_state_across_source_lane_ids` proves two
differently named lane directories resolve one canonical journal, dry-run does
not create it, the second live lane is locked out, and the arm-lifetime lease
survives pipeline-table teardown. Library tests prove a journal fault returns
partial evidence and an empty journal pins its plan. Binary assembly tests
prove safe default path derivation, canonical-root enforcement, and
ownership-aware readiness cleanup.
`cargo test --bin earshot book_feed_shutdown_drops_producer_before_joining_writer`
proves assembly sends close intent before abort/join drops the final tap and
that the writer returns terminal accounting rather than timing out. The
RunPlan identity test also proves the exact schema-3 artifact set and resolves
audio, book, order, and FIX evidence under the canonical run directory. Binary
tests additionally prove REST-null/FIX-boolean health diagnostics and that a
graceful kill requires resolved cancellation plus all mandatory live evidence.

Exit-path coverage: `cargo test --test report_on_exit` (the MELI regression —
a dead source must still yield a run record) and `cargo test --test
kill_sigterm` (the SIGTERM path still runs `on_terminate` and flushes the
ledger).

## Migration status

The typed hot-path assembly exists, but `src/main.rs` is a legacy hub mixing
CLI dispatch, construction, live validation, execution setup, and operational
commands. Startup now releases a prepared graph only after readiness is
durable and the gate is armed; the remaining simplification is an explicit
all-fallible `RunSpec -> PreparedRun` phase, then activation into
`BuiltRuntime -> RunResult`. The readiness door is now the
`<state_dir>/listener.ready` token (implemented 2026-07-29, replacing the
port-probe compatibility door); a typed in-process bound-endpoint event
remains the target once the run supervisor exists.
Live decision persistence, RunPlan identity/path verification, and
order-journal ownership are wired. Run-scoped audio, raw book, and FIX receive
evidence are composed in-process and finalized before the single terminal
runtime report on normal, fail-closed, STOP, and SIGTERM drains. Remaining
structural simplification is to extract this proven lifecycle from the legacy
CLI hub into explicit prepared/activated runtime types without changing these
contracts.
