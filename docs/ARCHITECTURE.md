# EARSHOT Architecture — the layer map

Read this first. Every layer has exactly one **entry door** and one **exit
door** — a typed ring, channel, or trait. To work on a layer you need only:
this page, the layer's doc in `docs/layers/`, and the layer's source file.
Nothing else. If a change requires touching two layers' internals at once,
the doors are wrong — stop and fix the door design first.

## The chain

```
config.toml ──► config ──────────────────────────────┐ (arm-time wiring)
                                                     ▼
[source] ─ring─► [preproc] ─ring─► [chunker] ─ring─► [detector]+[decision]
 tokio            OS thread         OS thread          OS thread "detect"
                                                     │
                                          mpsc(64)   │ DecisionIntent
                                                     ▼
             [book-cache] ◄─WS── Kalshi        [executor] ──HTTPS──► Kalshi
              tokio task    (reads: ArcSwap)    tokio task
                                                     │ guarded by
                                                 [risk gate]
```

Cross-cutting planes (not layers — they touch everything by design):
**telemetry** (latency recorders + report) and **config** (arm-time only,
never consulted on the hot path).

Running two live events at once (two Kalshi + two Twilio accounts, one process
each) is isolated via per-event config credential binding (`[kalshi]`/`[twilio]`
sections), the `--state-dir` flag, and `scripts/launch-event.sh` — see
`docs/MULTI-EVENT.md` (invariant #5, per-session kill scope, is the anchor it
extends per event). The signing/credential layer is `docs/layers/auth.md`.

## Door table

| # | Layer | Entry door (exact type, mechanism) | Exit door (exact type, mechanism) | Thread | Source file |
|---|-------|-----------------------------------|-----------------------------------|--------|-------------|
| 1 | source | `SourceConfig` variant + media (WAV file, SIP leg, HLS URL) | `AudioFrame` → `ring::RingSender<AudioFrame>` (cap 256). Ingress `send()`: full ring = counted drop (paced) or blocking retry (speed=0 replay) | tokio (current-thread rt; inside `spawn_blocking` under `arm`) | `src/source/mod.rs` (trait), `src/source/replay.rs` |
| 2 | preproc | `ring::RingReceiver<AudioFrame>` | same `AudioFrame`, `preproc_ts` stamped → `send_blocking` into ring (cap 256) | OS thread `preproc` | `src/pipeline/preproc.rs` |
| 3 | chunker | `ring::RingReceiver<AudioFrame>` | `AudioChunk` (chunk_ms of samples, ts propagated) → `send_blocking` into ring (cap 64) | OS thread `chunker` | `src/pipeline/chunker.rs` |
| 4 | detector | `&AudioChunk` via `Detector::process(&mut self, &AudioChunk) -> SmallVec<[Detection; 2]>` (sync trait call) | `Detection` (return value, same call) | OS thread `detect` | `src/detector/mod.rs` (trait), `src/detector/stub.rs` |
| 5 | decision | `&Detection` via `DecisionTable::decide(&mut self, &Detection) -> Option<DecisionIntent>` | `DecisionIntent` → `tokio::sync::mpsc::Sender<DecisionIntent>` (cap 64), `try_send`, full = counted loud drop | OS thread `detect` (shared with 4) | `src/decision.rs` |
| 6 | executor | `tokio::sync::mpsc::Receiver<DecisionIntent>` | HTTPS orders on the wire + `OrderRecord`s in `ExecReport` (returned when sender drops) | tokio task | `src/kalshi/executor.rs` |
| 7 | book-cache | Kalshi WS frames (internal to `run_ws` task) | `BookCache::read(&ticker) -> Option<Arc<Book>>` (wait-free ArcSwap load) + `feed_age_ns()` | writer: tokio task; readers: any | `src/kalshi/book.rs` |
| 8 | risk | `RiskGate::check_order(..) -> Result<(), Refusal>` + `record_send(..)` calls | verdicts (return values) + `state/probe_ledger.json` + kill-watcher side effects | caller's thread (atomics; ledger mutex) | `src/risk.rs` |
| — | telemetry | `LatencyRecorder::record(edge, ns)` — each recorder is single-owner, merged at teardown via `merge()` | `PipelineReport` (`render_table()`, `write_json()`) | owning thread | `src/telemetry.rs` |
| — | config | per-event TOML file | `config::EventConfig` via `config::load(path)` | arm-time only | `src/config.rs` |

Wiring lives in exactly two places: `pipeline::run_pipeline` (doors 1–5)
and `main.rs::arm` (doors 6–8 + planes). Nothing else may connect layers.

## Invariants (hold across every layer — breaking one is a bug even if tests pass)

1. **Monotonic timestamp chain.** `capture_ts` is stamped once at the source
   ingress and never rewritten. Each stage appends its own stamp
   (`preproc_ts`, `chunk_ts`, `emit_ts`, `decision_ts`, `send_ts`, `ack_ts`).
   All are `clock::MonotonicNs` — never wall clock, never `std::time::Instant`.
2. **Drops happen only at ingress.** The source's ring `send()` may drop
   (counted, logged). Every interior hop uses `send_blocking`/backpressure or
   a counted-loud-drop channel. Exactly one drop point per pipeline; the
   report surfaces every counter.
3. **The hot path never blocks on the network or config.** Book reads are
   wait-free `ArcSwap` loads; order headers are pre-signed; bodies are
   pre-serialized `Bytes`; keyword→market lookup is a precompiled table.
4. **Fail cheap, fail loud (spec P3).** Unknown staleness is stale. A missing
   book at arm time refuses to arm. Ambiguous order outcomes are queried,
   never blindly retried.
5. **Kill switch cancels only this session's orders** (`OrderRegistry`).
   The account carries unrelated resting orders from other systems; an
   account-wide cancel is forbidden. Never widen this scope.
6. **Kalshi API facts come from `VERIFICATIONS.md`**, which records live pins
   with dates. Code comments cite it; nobody codes from memory of the docs.
7. **Money math is integer cents** (prices) and whole contracts (counts);
   wire formats are fixed-point strings built by `rest::cents_to_dollar_str`
   / `rest::count_to_fp_str`. Floats never touch prices.

## Benchmarking

The M1 detector bake-off (corpus design, dev/holdout protocol,
pre-registered sign-off criteria, `bench-detector` usage) is documented in
`docs/BAKEOFF.md`; the harness lives in `src/bench/`.

## Definition of done for a layer change

A layer change is complete when: (a) its unit tests pass, (b) the e2e replay
test passes, (c) **its `docs/layers/<layer>.md` matches the new code** —
types cited by exact name. New layers ship with a doc from the same template.
