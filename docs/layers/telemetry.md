# Layer: telemetry (cross-cutting plane)

## Role
Latency instrumentation, structured logging, and the per-session report
(spec §5.9). Every stage's timing obligations terminate here. NOT a pipeline
layer — it never sits between two stages.

## Entry door
- **Type/mechanism:** `LatencyRecorder::record(edge: &'static str, nanos:
  u64)` (`src/telemetry.rs`). Edge names are the fixed `EDGES` list —
  pipeline: `capture→preproc`, `preproc→chunk`, `chunk→detect`, `ring_wake`
  (detect-hop SPSC push→wake cost; absent under `[perf] spin_hot_rings`),
  `detect→decision`, `capture→decision`; execution: `handoff→exec` (mpsc
  wake hop, from the intent's `handoff_ts` stamp), `decision→exec` (the
  aggregate: detect-thread tail + wake), `book_age`, `presign_age`,
  `exec→send`, `send→hdrs`, `hdrs→ack`, `send→ack`, `decision→ack`,
  `capture→ack` (`send→hdrs`/`hdrs→ack` split `send→ack` at TTFB — see the
  executor doc).
- **Declared ⇒ populated:** an edge may only be added to `EDGES` together
  with the code that records it — `record()` on an undeclared edge is a
  silent no-op (this exact failure hid `book_age` until 2026-07-17), and
  `tests/e2e_full_chain.rs` asserts every declared edge is populated in a
  full-chain run.
- **Ownership rule:** each recorder is owned by exactly one thread/task (the
  detect thread owns the pipeline recorder; the executor owns its own) —
  recording is lock-free-by-ownership (~20 ns into a pre-allocated
  hdrhistogram). Teardown folds: fanout legs fold into the top-level
  recorder via `LatencyRecorder::merge(&other)` (hdrhistogram `add`);
  the arm-time pipeline↔executor fold in `main.rs::arm` is currently
  `report.edges.extend(exec_recorder.snapshot())`, which is
  histogram-correct ONLY because the two recorders record strictly
  disjoint edge sets — that partition is pinned by the
  `arm_fold_edge_sets_are_disjoint` unit test (an edge recorded on both
  sides would be silently replaced, not merged; switching that fold to
  `merge()` is a parked post-event seam change).
- Logging: `telemetry::init_logging(json)` — tracing with EnvFilter
  (`RUST_LOG`), JSON mode for machine ingestion. The writer is deliberately
  SYNCHRONOUS: hot-path code logs only AFTER the intent handoff / order send
  (see decision.md, executor.md), and a non-blocking appender
  (tracing-appender) can drop the final lines at SIGTERM — the post-mortem
  record (the MS RECE analysis was reconstructed from decision wall-times in
  the logs) outweighs the residual µs. Revisit only with a flush-on-signal
  guarantee.

## Exit door
- **Type:** `PipelineReport` — event id, frame/chunk counts,
  `first_capture_ts` (capture time of chunk seq 0 — anchors wall clock to
  the content timeline for the bench's paced onset→emit latency), per-edge
  `EdgeStats` (count + p50/p90/p95/p99/max in µs; zero-count edges omitted
  by `snapshot()`), `DetectionRecord`s (each carries `leg: String` — the
  fanout leg label; empty and JSON-omitted in single-source mode),
  `DropCounters`, and `per_leg: Option<Vec<LegSummary>>` — fanout only
  (`label`, `frames_in`, `chunks_in`, `detections`, per-leg `DropCounters`,
  `died: Option<String>`); `None`/omitted in single-source mode so those
  reports keep their exact shape. Fanout top-level numbers are folds:
  edges via `LatencyRecorder::merge`, frame/chunk/drop sums,
  `first_capture_ts` = min across legs. `main.rs::arm`
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
