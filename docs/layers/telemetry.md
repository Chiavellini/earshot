# Layer: telemetry (cross-cutting plane)

## Role
Latency instrumentation, structured logging, and the per-session report
(spec §5.9). Every stage's timing obligations terminate here. NOT a pipeline
layer — it never sits between two stages.

## Entry door
- **Type/mechanism:** `LatencyRecorder::record(edge: &'static str, nanos:
  u64)` (`src/telemetry.rs`). Edge names are the fixed `EDGES` list —
  pipeline: `capture→preproc`, `preproc→chunk`, `chunk→detect`,
  `detect→decision`, `capture→decision`; execution: `decision→exec`,
  `presign_age`, `exec→send`, `send→hdrs`, `hdrs→ack`, `send→ack`,
  `decision→ack`, `capture→ack` (`send→hdrs`/`hdrs→ack` split `send→ack` at
  TTFB — see the executor doc).
- **Ownership rule:** each recorder is owned by exactly one thread/task (the
  detect thread owns the pipeline recorder; the executor owns its own) —
  recording is lock-free-by-ownership (~20 ns into a pre-allocated
  hdrhistogram). Recorders merge at teardown via
  `LatencyRecorder::merge(&other)` (hdrhistogram `add`).
- Logging: `telemetry::init_logging(json)` — tracing with EnvFilter
  (`RUST_LOG`), JSON mode for machine ingestion.

## Exit door
- **Type:** `PipelineReport` — event id, frame/chunk counts,
  `first_capture_ts` (capture time of chunk seq 0 — anchors wall clock to
  the content timeline for the bench's paced onset→emit latency), per-edge
  `EdgeStats` (count + p50/p90/p95/p99/max in µs; zero-count edges omitted
  by `snapshot()`), `DetectionRecord`s, `DropCounters`. `main.rs::arm`
  extends the JSON with `orders` (OrderRecords), `lost_races` (fillable-tier
  only), and `observe_noncross` (designed 1¢ non-crossing — see
  `docs/layers/executor.md`).
- **Mechanism:** `render_table()` (terminal) and `write_json(path)`
  (`report.json`) — the replay/regression artifacts (spec P4).

## Obligations
- New edges: add to `EDGES` (render order), record from the owning thread,
  merge at teardown. Never share one recorder across threads with a lock on
  the hot path.
- Histogram bounds are 1 ns – 60 s, 3 significant digits — a stalled stage
  saturates loudly at max rather than wrapping.
- Drop counters are part of the report contract: every bounded ring/channel
  that can drop must surface its counter here.

## How to replace or edit this layer
- Everything is `src/telemetry.rs`. Contract: edge names already emitted
  (dashboards/tests key on them), report JSON field names, the ownership
  rule.

## How to test in isolation
- Pure unit-testable: build a recorder, record, snapshot, assert stats.
  Covered indirectly by e2e (`tests/e2e_replay.rs` asserts populated edges,
  strict ts ordering, JSON round-trip) and `tests/exec_mock.rs` (exec edges).
