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
market validation, and order transport. Fanout labels must be unique.

## Output contracts

The node returns a `PipelineReport` and, when execution is attached, merges
`ExecReport` records into it. A graph has one shared decision table in fanout
mode and preserves the typed doors documented in `docs/ARCHITECTURE.md`.
The `runtime.source.twilio.listener_ready` door is the token file
`<state_dir>/listener.ready` (`{"port":<u16>,"pid":<u32>,"unix_ts":<int-seconds>}`,
via `fsio::write_atomic`), written by `main.rs::arm` ONLY after
`build_pipeline_input` has returned (detector fully constructed — the point
where the 2026-07-29 ORT hang lived, invisible to a port probe because the
listener binds before the detector builds) AND the pipeline thread is spawned.
`port` is the bound Twilio listener port surfaced by `build_pipeline_input`,
or `0` for configs without a Twilio source (the token is written
unconditionally on the arm path; the launcher only consumes it for phone
events). Replay never writes it; arm deletes any stale token at start. This
token is the only door that allows phone-session control to dial.

## Side effects and exclusive resources

It spawns source tasks, stage threads, detector instances, book/executor work,
and writes the requested report path. A live event exclusively owns its state
directory, report file, STOP marker, ports, and configured account/session.

## Failure contract

The required target contract is: validate and construct every fallible
dependency before activating the risk gate or exposing a live run. Stage
failures are returned and recorded; sibling fanout legs may continue. Assembly
must never substitute a missing dependency or silently select a different
graph.

**Every terminated run leaves a record.** `arm()` routes all post-listener exit
paths through one choke point, `finish_run(report_path, event_id, report, exec,
exit)`, which prints the summary and writes `telemetry::build_run_json(...)`.
The pipeline's outcome selects the `ExitReason` — it must never decide *whether*
a report exists:

| pipeline outcome | `ExitReason` | report |
|---|---|---|
| `Ok(Ok(report))` | `clean_drain` | full |
| `Ok(Err(e))`, single source | `source_error` | partial + `exit.detail` |
| `Ok(Err(e))`, fanout | `all_legs_died` | partial + `exit.detail` |
| join error / panic | `panic_or_join` | partial + `exit.detail` |
| SIGTERM (kill watcher) | `sigterm` | `telemetry::sigterm_stub_json`, never clobbers an existing report |

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

Exit-path coverage: `cargo test --test report_on_exit` (the MELI regression —
a dead source must still yield a run record) and `cargo test --test
kill_sigterm` (the SIGTERM path still runs `on_terminate` and flushes the
ledger).

## Migration status

The typed hot-path assembly exists, but `src/main.rs` is a legacy hub mixing
CLI dispatch, construction, live validation, execution setup, and operational
commands. Today `main::arm` calls `gate.arm()` before source/detector
construction, so the target ordering above is not yet guaranteed. Introduce an
all-fallible `RunSpec -> PreparedRun` phase, then activate it into
`BuiltRuntime -> RunResult`. The readiness door is now the
`<state_dir>/listener.ready` token (implemented 2026-07-29, replacing the
port-probe compatibility door); a typed in-process bound-endpoint event
remains the target once the run supervisor exists.
