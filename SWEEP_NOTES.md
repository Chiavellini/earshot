# Overnight sweep notes — branch `sweep/overnight-2026-07-29`

One section per hourly sweep. Read this first; it is written for the next
sweep. Base commit: f29976c (webcast-lane). Rules of the night: no pushes to
main/webcast-lane, no risk/kill/ledger/launch-script/config/state changes, no
live network side effects, no detector threshold/model changes.

## Sweep 1 — 2026-07-29 (commit 5720c02 + this notes commit)

Test status: `EARSHOT_REQUIRE_MODELS=1 cargo test` fully green in the
worktree (179 lib tests + every integration suite, 0 failed) after all
changes. Perf evidence from `cargo test --release -- --ignored` on the dev
laptop (~3.5x run variance — two full release runs, interleaved A/B rounds
inside each run).

### What changed

1. **Phone-lane μ-law ingress decode: single allocation** — VERIFIED.
   `twilio_ws.rs` + `sip.rs` decoded each 20 ms frame via
   `Arc::from(decode_ulaw(bytes).as_slice())`: Vec alloc + fill, then a
   second alloc + memcpy into the `Arc<[i16]>`. New `decode_ulaw_arc()`
   collects the LUT map straight into the `Arc` (TrustedLen → one
   allocation). Numbers (`perf_ingress_decode_ab`, per 160-sample frame,
   5 interleaved rounds × 2 release runs): old 165–353 ns vs new 110–239 ns
   — new won **all 10 interleaved rounds**, ~30–40% less per-frame decode
   work. Absolute magnitude is tiny (~60 ns/frame) but the direction is
   unambiguous; it also removes one of the two per-frame heap churn points
   on the capture thread.

2. **Webcast pipe ingress: decode straight off the accumulation buffer** —
   NOT RESOLVABLE (kept as an obviously-safe simplification).
   `pipe.rs::emit` used `buf.drain(..n).collect::<Vec<u8>>()` then decoded
   that temporary into the `Arc<[i16]>`. Now decodes from `buf[..n]` and
   drains after — one less alloc + copy per frame by construction, but the
   A/B rounds interleave with the μ-law bench and did NOT separate from
   noise (old 137–288 ns vs new 136–259 ns, winner alternates by round).
   Honest verdict: not measurable here; kept because it strictly removes
   work and is behavior-identical (EOF torn-byte path unchanged, verified
   by the existing pipe tests).

3. **Twilio `capture_ts` stamped at WS message arrival** — UNVERIFIED-LOW-RISK
   (measurement-honesty fix, not a speedup). The layer doc promised
   "stamped at message arrival"; the code stamped AFTER JSON parse + base64
   + μ-law decode, silently excluding that work from every capture→X edge.
   Now `arrival_ts` is taken right after `ws.next()` yields. Effect: phone
   lane capture→preproc edge will read a few µs LARGER and truthful from
   now on — do not misread that as a regression in tomorrow's report.
   `docs/layers/source.md` updated to match (per CLAUDE.md doc rule).

4. **Preproc resample path reuses a scratch buffer** — UNVERIFIED-LOW-RISK.
   `preproc::run` now calls new `HalfBandUpsampler2x::process_into` with one
   reused `Vec` (one less alloc per 8 kHz frame); `process()` is a wrapper
   with identical semantics (continuity + tone tests still pass;
   `perf_preproc_rtf` sanity: 8128× / 9625× realtime across the two runs —
   nowhere near the 100× floor).

5. **`kws_compile::prefix_cut` whitespace off-by-N** — VERIFIED (unit test).
   A fast_prefix containing a doubled space / tab counted every whitespace
   char, but the token stream renders any inter-word gap as exactly one `▁`
   (encode() splits on split_whitespace) — so the Fast cut landed one token
   too far (later Fast fire, or spurious PrefixNotShorter). Now whitespace
   runs count as one char. Behavior for well-formed configs is bit-identical
   (all pinned vectors + compile tests pass); new model-free test
   `prefix_cut_collapses_whitespace_runs` pins the edge cases.

6. **New perf harness: `perf_ingress_decode_ab`** in `tests/perf_budget.rs` —
   interleaved old-vs-new decode A/B in one binary, report-only prints plus a
   loose 2× drift alarm. Rerunnable by later sweeps.

### Inspected and deliberately left alone

- `executor::handle_intent` clones a `PreStaged` (2 short Strings + Bytes)
  per intent — on the fire path but ~100 ns and once per fire, not per
  frame; an `Arc<PreStaged>` map value would save it. Left: not worth
  touching the executor the night before a live run.
- Twilio media loop parses each WS message into `serde_json::Value` (~µs,
  50/s). A typed borrow-deserialize struct would cut it, but the message
  shapes are pinned evidence in docs/TWILIO.md and the parse is not the
  bottleneck; too much risk-per-gain tonight.
- `chunker.rs`: a chunk seals only when `buf.len() >= chunk_samples` and
  ships the WHOLE buffer (can exceed chunk_ms when frames are large) —
  looks intentional (never splits a frame); did not touch.
- `preproc` upsampler inner loop (`copy_within` history shift, 16-tap MAC):
  8128× realtime — nothing to win.
- `ring.rs`, `decision.rs`, detect_loop fire-first ordering, executor
  `FuturesUnordered` single-task design: all already tuned by the 07-17
  latency pass; found nothing safe to improve.
- `arbiter.rs` align/dedupe path `Arc::from(pcm.as_slice())` — off the
  default racing path (fanout is the racing config), skipped.

### Suggested next steps for sweep 2

- Run `cargo test --release -- --ignored perf_` for the FULL ladder (I ran
  only preproc + the new A/B; detector RTF ladder + executor fire path +
  ring wake untouched by my changes but a full baseline in SWEEP_NOTES
  would help later sweeps).
- `streaming_ctc.rs` / `ctc_scorer.rs` hot loops were NOT audited (time).
  They are the webcast-lane detector milestone target; look for per-chunk
  allocations there (tensors, token vecs) — but respect the no-model/
  no-threshold rule.
- Consider `Arc<PreStaged>` in `PreStagedMap` (see above) if you want an
  executor-side micro-win with a clean seam.
- `twilio_ws` typed message deserialize (borrowed `&str` payload) if you
  can pin it against docs/TWILIO.md shapes — saves the `Value` tree per
  20 ms message.
- The e2e/full-chain perf tests (`perf_full_chain_paced`) were not rerun
  tonight; if you touch anything on the chunk path, run them.

## Sweep 2 — 2026-07-29 (commit 597ae63 + this notes commit)

Test status: `EARSHOT_REQUIRE_MODELS=1 cargo test` fully green (179 lib +
every integration suite, 0 failed) — run WITH `EARSHOT_CTC_MODEL_DIR`
pointed at `models/sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18`
and `ORT_DYLIB_PATH` at the main checkout's
`vendor/onnxruntime/onnxruntime-osx-arm64-1.27.1/lib/libonnxruntime.1.27.1.dylib`
(read-only; the worktree has no vendor/), so the two model-gated
streaming_ctc integration tests actually RAN (they silently SKIP without
those vars — sweep 3: keep setting them). Full release perf ladder
(now 11 tests) green.

### Full perf baseline ladder (sweep 1's ask) — 2026-07-29, this laptop

`cargo test --release --test perf_budget -- --ignored --nocapture
--test-threads=1`. NOTE: numbers print ONLY with `--nocapture` (sweep 1's
background run swallowed them). Two runs, pre/post-change values where the
test is untouched by tonight's changes (all inside run variance):

- preproc 2x upsampler: 11485× / 11767× realtime
- sherpa_kws detector: 21.8–22.2× realtime; per-chunk p50 279/265 µs
  (160 ms), 122/120 (80), 59/59 (40), 30/30 (20); p95 ~14–14.6 ms at every
  size (the structural decode-burst bimodality, see test comment)
- executor fire path: presigner.get() 140/141 ns; decision→exec p95
  476/630 µs; exec→send p95 14/25 µs
- full chain paced: capture→decision p95 140.1/142.0 ms, capture→ack p95
  140.8/142.6 ms (paced replay — dominated by chunk cadence, not our code)
- cold first fire: process-warm premium 0 µs both postures
- ring idle-wake: old p50 75–77 µs vs new (unpark-on-send) p50 4 µs
- ingress decode A/B (sweep 1's change): ulaw old ~145–152 vs new
  ~100–109 ns/frame (new won all rounds again); pipe old≈new (as sweep 1
  reported: not resolvable)
- recorder 28–29 ns/op; risk record_send 26–27 ns/op
- NEW streaming_ctc encoder: 23.7–24.1× realtime, per-100ms-call p50
  219–227 µs, p95 ~13 ms (same bimodal burst shape as sherpa — inference
  runs every 320 ms quantum)

### What changed

1. **`ctc_scorer::accept_frame`: per-entry scratch buffer reuse** —
   VERIFIED. The Viterbi update allocated a fresh `next` Vec per entry per
   posterior frame (~25–50 fps × entries); now it writes every slot of a
   reused `EntryState.next` unconditionally and swaps with `alpha`.
   Numbers (`perf_ctc_scorer_ab`, 24 entries × L=9, 5 interleaved rounds ×
   2 release runs + 1 ladder run): current-code side ~1250 ns/frame before
   → 745–769 ns/frame after, winning ALL 15 interleaved rounds, while the
   inline old-algorithm anchor held at ~1126–1218. ≈40% less per-frame
   scorer work on the streaming_ctc fire path. Semantics bit-identical
   (all 7 scorer unit tests + both model-gated integration tests pass).
2. **`streaming_ctc::accept_pcm`: cache tensors moved, not cloned** — NOT
   RESOLVABLE end-to-end / strictly-less-work by construction. Every
   encoder step cloned all 74 streaming cache tensors (shape + data Vec)
   into `Tensor::from_array`; now data Vecs are `mem::take`-moved (the
   `new_X` outputs replace them; a state input without an updating output —
   none in sherpa/k2 exports — falls back to clone via the `updated_states`
   set computed at init). Also the f32 sample-conversion buffer is reused
   across calls. RTF 24.1× before vs 23.8–23.9× after, p50 219→226/227 µs:
   inside this laptop's noise, verdict NOT RESOLVABLE (inference dominates).
   Consequence documented in code + detector.md: an inference `Err` now
   poisons the streaming state; both production callers already treat
   inference failure as fatal (`process()` panics loudly per P3).
3. **New perf harnesses** — `perf_ctc_scorer_ab` (model-free, inline
   old-algorithm anchor, loose 2× drift alarm) and `perf_streaming_ctc_rtf`
   (model-gated, report-only RTF + per-call percentiles, SKIPs loudly
   without the env vars). Both rerunnable by later sweeps.
4. **docs/layers/detector.md** updated to match: cache move semantics +
   error poisoning, scorer steady-state no-alloc (this closes part of the
   pre-existing gap vs the layer obligation "No allocation in steady state
   beyond the returned SmallVec" — see below for what still violates it).

### Inspected and deliberately left alone

- `accept_pcm` still allocates per step (`xdata` feature window handed to
  ort by value; the returned `Vec<(u64, Vec<f32>)>` posterior frames — one
  vocab-sized Vec per output frame) and ort's output map per run. Fixing
  the posterior Vecs means a callback/flat-buffer API change to a public
  method; deferred, see next steps.
- Executor `Arc<PreStaged>` and Twilio typed WS deserialize: same verdict
  as sweep 1 (risk-per-gain the night before a live event) — untouched.
- sherpa_kws hot path: behind the C FFI, nothing safe to shave.
- `perf_full_chain_paced` p95 ~140 ms is chunk-cadence-dominated (paced
  replay); not a code smell, left as-is.

### Suggested next steps for sweep 3

- Rerun the ladder with `--nocapture` (else you get green but no numbers)
  and the CTC env vars set (else the model-gated tests silently skip).
- If appetite remains on streaming_ctc: flat-buffer/callback posterior
  delivery in `accept_pcm` (removes one vocab-sized Vec per output frame,
  ~2–6 KB each at BPE-500) — needs the two model-gated tests + bench users
  updated; still no-model/no-threshold territory.
- The remaining obligations-vs-code gap in detector.md ("No allocation in
  steady state") for streaming_ctc is now only the accept_pcm internals
  above — worth one more pass, then the doc line is fully true for both
  engines.
- Consider pinning a tiny `vendor/` symlink note or env-var helper for the
  worktree so future sweeps run the model-gated CTC tests by default.

## Sweep 3 — 2026-07-29 (commits 2d38400, 149e56f, bc40052 + this notes commit)

Test status: `source scripts/ctc-test-env.sh && EARSHOT_REQUIRE_MODELS=1
cargo test` fully green after all changes (179 lib tests + every integration
suite, 0 failed everywhere); the two model-gated streaming_ctc tests
confirmed RUNNING (2.5 s of real inference), not skipping. Perf A/Bs below
were run binary-vs-binary: the pre-change release `perf_budget` binary was
copied aside before editing, then OLD/NEW alternated in one shell loop —
true interleaving across the change, same machine, same session.

### What changed

1. **`streaming_ctc::accept_pcm`: flat-buffer posterior delivery** —
   NOT RESOLVABLE end-to-end / strictly-less-work by construction (sweep 2's
   #1 handoff item). The public API changed from
   `Result<Vec<(u64, Vec<f32>)>>` to `Result<PosteriorBatch>` (Copy struct:
   `first_frame` + `frames`, with `len()`/`is_empty()`) + a
   `posteriors() -> &[f32]` accessor over a reused frame-major buffer —
   removes one vocab-sized Vec (~2 KB at BPE-500) per output frame (~25/s)
   plus the outer Vec per call. This closes the detector-layer "no
   steady-state allocation" gap for this engine: the only remaining per-step
   allocs are the `x` feature tensor handed to ort by value and ort's own
   output map, both inside the inference call. A/B `perf_streaming_ctc_rtf`,
   3 interleaved rounds: old 23.7/24.2/24.1× vs new 24.0/24.3/24.1×, p50
   220–222 vs 219–221 µs, identical 1496 frames out — inside noise
   (inference dominates), as predicted. All callers updated (detector
   `process`, both model-gated tests, `perf_streaming_ctc_rtf` needed no
   change thanks to `PosteriorBatch::len`). detector.md updated.

2. **`RestClient`: orders URL pre-parsed at construction** — VERIFIED.
   `create_order_presigned` did `format!("{base}{ORDERS_ENDPOINT}")` + a
   full reqwest/url parse on EVERY send, on the exec→send edge. Now a
   `reqwest::Url` is parsed once in `with_base_and_timeout` (a bad base now
   fails at construction with `KalshiError::Validation` instead of at first
   send — strictly earlier, strictly better) and cloned per send (internal
   memcpy, no parse). A/B `perf_executor_fire_path`, 5 interleaved rounds
   (old binary vs new): exec→send p95 old {356, 25, 24, 827, 29} µs vs new
   {38, 9, 9, 209, 10} µs — new won ALL 5; in the quiet rounds the edge
   drops 24–29 → 9–10 µs. (Rounds 1/4 are the known first-run/system-noise
   outliers on both sides; the ordering never flips.) Order-path suites
   (exec_mock, exec_fix_mock, validator_mock, e2e_full_chain) all green.
   executor.md updated.

3. **`scripts/ctc-test-env.sh`** — closes sweep 2's last handoff item. New
   sourceable helper (bash + zsh; find -L so the worktree's `models`/`vendor`
   symlinks resolve) exporting `EARSHOT_CTC_MODEL_DIR` + `ORT_DYLIB_PATH`,
   so the model-gated CTC tests and `perf_streaming_ctc_rtf` stop silently
   skipping: `source scripts/ctc-test-env.sh && EARSHOT_REQUIRE_MODELS=1
   cargo test`. Also symlinked `vendor` → main checkout's vendor in THIS
   worktree (untracked, gitignored) so the helper resolves here. Doc pointer
   in detector.md's test section. Touches no launch/preflight/autorun script.

### Inspected and deliberately left alone

- **`Arc<PreStaged>` in `PreStagedMap`** (executor fire path, third sweep in
  a row): still ~100 ns once per fire (2 String clones + a Bytes refcount).
  Same verdict as sweeps 1–2 — not touching `handle_intent`'s structure the
  night before a live run for a sub-µs win. If sweep 4+ wants it anyway,
  note `OrderRecord` construction clones the same two Strings again, so do
  both or neither.
- **Twilio typed WS deserialize**: unchanged verdict (message shapes are
  pinned evidence in docs/TWILIO.md; parse ~µs at 50/s, not the bottleneck).
- `create_order_presigned`'s response side (`resp.json()` into `Value`,
  `raw.clone()` into the ack): after the wire, off the send edge; the TTFB
  split (`hdrs→ack`) already accounts for it. Left alone.
- `cancel_order` / `order_by_client_id` still format! URLs per call —
  non-hot-path by design (kill switch, ambiguous-outcome query); left.
- `xdata` feature-window Vec per encoder step: handing ort a borrowed view
  (`TensorRef`) would drop it, but that's an ort-API-shape change on the
  inference call the night before a live event; deferred with a note.

### Suggested next steps for sweep 4

- The perf ladder was NOT rerun in full tonight (only preproc-adjacent
  suites via cargo test + the two A/B targets above). A full
  `cargo test --release --test perf_budget -- --ignored --nocapture
  --test-threads=1` drift record against sweep 2's baseline table would be
  cheap and useful.
- exec→send is now ~9–10 µs p95 in the mock harness; the next real lever on
  the order path is upstream (`decision→exec` p95 ~650–930 µs in the same
  harness — mpsc wake + task polling; sweep 2's ring unpark-on-send pattern
  may transplant, but that's executor-structure territory: post-event).
- Phone-lane hot path beyond ingress decode is still the least-audited area
  (chunker → detect handoff on the 8 kHz lane).
- If the live event ran by sweep-4 time: check `state/`-side telemetry for
  the widened exec→send claim against real wire numbers before believing
  the mock-harness magnitude.
