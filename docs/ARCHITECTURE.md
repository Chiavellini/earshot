# EARSHOT architecture

The unit of architecture is a node: one job, one owner, one input contract,
one output contract, and an independent verification path. The complete
machine-readable ownership map is `architecture/nodes.toml`; `docs/NODES.md`
is its human index.

The project contains three connected pipelines. They share versioned
artifacts, not implementation knowledge:

```text
CONTROL
discover → qualify ─┬→ prepare(EventConfig) ────┐
                    ├→ admission(ArmPermit) ────┼→ preflight → launch/bind
                    └→ acquire(SourceEndpoint) ─┘                 │
                                      listener_ready              ▼
                                                    [phone session] → phone_attached
                                                                         │
                                                              supervise + telemetry → grade

RUNTIME
source → preproc → chunker → detector.port → decision → executor → PipelineReport
                                 ▲
             keyword compiler → engine constructor
                                              ▲        ▲
                                           book     risk/auth

EVIDENCE
capture → curate corpus → benchmark/measure → reviewed verification pin
```

Qualification fans out into three independently owned control nodes. Preflight
is their explicit join. A phone session is optional and may dial only after
launch/assembly has bound the advertised listener and emitted
`runtime.source.twilio.listener_ready`; it is never part of source
acquisition. The later `control.phone.attached` event reports live attachment
to supervision and telemetry. It is not an adapter-construction dependency.

The detector boundary is likewise split by job: `runtime.detector.port` owns
the trait and single factory, `runtime.detector.keyword_compile` owns
model-token-aligned keyword compilation, and the Sherpa and streaming-CTC
nodes each expose an engine-constructor capability to the factory. Engine
work can proceed in parallel without changing the port.

Adapters translate external systems into a contract. Composers connect nodes
and own lifecycle; they do not own domain policy. A change that needs two
nodes' internals at once is a contract change and must be treated as such.

## Runtime data plane

Every runtime node has one **entry door** and one **exit door**—a typed ring,
channel, return value, or trait. To work on one, read its contract in
`docs/layers/` or `docs/nodes/` and the two adjacent contract definitions.

```
config.toml ──► config ──────────────────────────────┐ (arm-time wiring)
                                                     ▼
[source] ─ring─► [preproc] ─ring─► [chunker] ─ring─► [detector]+[decision]
 tokio            OS thread         OS thread          OS thread "detect"
                                                     │
                                          mpsc(64)   │ DecisionIntent
                                                     ▼
             [book-cache] ◄─WS── Kalshi        [executor] ──HTTPS──► Kalshi
              tokio task    (reads: ArcSwap)    own OS thread
                                                     │ guarded by
                                                 [risk gate]
```

**Fanout variant** (`[arbiter] mode = "fanout"`, `pipeline::run_pipeline_fanout`):
N sources each get their OWN full chain — `source-<i>` → `preproc-<i>` →
`chunker-<i>` → `detect-<i>`, one `Detector` instance per leg — and the flags
are OR-ed at door 5: every leg's detect thread calls `decide()` on ONE shared
`DecisionTable` behind an `Arc<Mutex<..>>`, so the one-shot per-keyword machine
is the dedupe point (first leg to hear a word wins; measured motivation:
07-22 TARI was heard on one leg and missed on another). Contrast with
`SourceArbiter`, which collapses N legs into one stream *before* the detector.
A leg whose source errors dies alone (`per_leg[i].died` in the report);
siblings keep racing; all-legs-dead returns the first error. Fanout refuses a
non-default `[perf]` section (core maps are single-chain).

```
[source-0] ─► [preproc-0] ─► [chunker-0] ─► [detector-0]┐   Arc<Mutex<
[source-1] ─► [preproc-1] ─► [chunker-1] ─► [detector-1]┼─► DecisionTable>> ─► mpsc
                                            (per leg)   ┘   (one-shot = dedupe)
```

Cross-cutting planes (not layers — they touch everything by design):
**telemetry** (latency recorders + report), **config** (arm-time only,
never consulted on the hot path), and **perf** (`src/perf.rs` + `[perf]`
config: per-thread core pinning/SCHED_FIFO and ring spin mode, applied at
thread spawn; default = no-op — docs/OS-TUNING.md).

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
| 6 | executor | `tokio::sync::mpsc::Receiver<DecisionIntent>` | HTTPS orders on the wire + `OrderRecord`s in `ExecReport` (returned when sender drops) | dedicated OS thread + current-thread rt (intents overlap via `FuturesUnordered`, no per-intent spawn) | `src/kalshi/executor.rs` |
| 7 | book-cache | Kalshi WS frames (internal to `run_ws` task) | `BookCache::read(&ticker) -> Option<Arc<Book>>` (wait-free ArcSwap load) + `feed_age_ns()` | writer: tokio task; readers: any | `src/kalshi/book.rs` |
| 8 | risk | `RiskGate::check_order(..) -> Result<(), Refusal>` + `record_send(..)` calls | verdicts (return values) + `state/probe_ledger.json` + kill-watcher side effects | caller's thread (atomics; ledger mutex) | `src/risk.rs` |
| — | telemetry | `LatencyRecorder::record(edge, ns)` — each recorder is single-owner, merged at teardown via `merge()` | `PipelineReport` (`render_table()`, `write_json()`) | owning thread | `src/telemetry.rs` |
| — | config | per-event TOML file | `config::EventConfig` via `config::load(path)` | arm-time only | `src/config.rs` |

Current runtime wiring lives in exactly two legacy composers:
`pipeline::run_pipeline` /
`run_pipeline_fanout` (doors 1–5; both entry points share the same stage code
in `src/pipeline/mod.rs`) and `main.rs::arm` (doors 6–8 + planes;
`build_pipeline_input` picks the entry point from `arbiter.mode`). Nothing
else may connect runtime layers. `main.rs::arm` is a documented legacy hub;
`docs/ROADMAP.md` replaces it with a prepared-run lifecycle without creating
a third composer.

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

## Definition of done for a node change

A node change is complete when its focused tests pass, affected contract/e2e
tests pass, its contract doc matches the code, external facts are pinned, and
`scripts/doc-lint.sh` passes. New nodes require an
`architecture/nodes.toml` entry and a doc based on
`docs/nodes/TEMPLATE.md`.
