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
