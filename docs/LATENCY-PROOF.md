# Latency proof ledger — measured verdicts, no claims without numbers

Rule (standing, per operator directive 2026-07-17): every latency/detection change ends here with
an interleaved A/B or a side-by-side measurement on the same hardware — or an explicit
`NOT RESOLVABLE ABOVE NOISE` / `INSTRUMENT-ONLY` verdict. Claims that don't survive measurement are
recorded as such, not dropped.

## Session 2026-07-17 — fire-path mechanical pass (commit `7c78863` vs baseline `8eed570`)

Box: CDMX dev laptop (macOS, thermally noisy — measured 3.5× run-to-run swing on uninterleaved
runs; all A/B below interleaved OLD/NEW ×5 or side-by-side in one process). Instruments:
`tests/perf_budget.rs` (`--release -- --ignored`). Raw logs: session scratchpad `ab_runs.log`.

| Change | Instrument | OLD | NEW | Verdict |
|---|---|---|---|---|
| Ring unpark-on-send | `perf_ring_wake_ab` — 500 parked pushes per impl, both impls in one process, push→recv-return | p50 **67 µs** p95 **127 µs** max 133 µs | p50 **4 µs** p95 **6 µs** max 42 µs | **PROVEN — ~17× p50 / ~21× p95 per ring hop.** Applies to every idle-wake crossing (frames arrive every 20 ms; consumers park between frames, so this is per-hop steady state, not a rare case) |
| Arm-time shadow warm-fire | `perf_cold_first_fire` — 4 fresh rigs [off,on,off,on], per-fire OrderRecord timestamps | process-cold rig, socket-warm only: first-fire premium **645 µs** | + signed-GET warm-fire: premium **0 µs** (later rigs 0–6 µs) | **PROVEN for the production case** — arm IS the process-cold state; the bare `/exchange/status` socket warm does NOT cover it (rig 1 had it and still paid 645 µs), the signed GET does. Caveat: wiremock/localhost — proves code-path warming, not network |
| No-spawn executor (FuturesUnordered) | `perf_executor_fire_path` interleaved ×5 — `decision→exec` p95 over a 6-deep PRE-QUEUED burst | ~**690 µs** (663–698, warm runs) | ~**763 µs** (721–836) | **MEASURED REGRESSION in this shape: ~+70 µs burst-intake p95.** The old code spread 6 queued intents across 2 workers; the new single-task drain serializes intake. The claimed win (single-intent wake, no spawn alloc) is NOT isolatable by this instrument — the racing case is the FIRST intent, which this p95-of-6 does not measure. Tracked live via the new `handoff→exec` edge (no pre-change baseline exists) |
| Deferred telemetry locks (PendingEdges) | same instrument — `exec→send` p95 | ~26–28 µs | ~27–39 µs | **NOT RESOLVABLE ABOVE NOISE** (predicted: ~ns of lock removal on a ~30 µs edge) |
| Fire-first ordering (detect thread) | `perf_full_chain_paced` interleaved ×5 — `capture→decision`/`capture→ack` p95 | 140.2–141.6 ms / 140.9–142.5 ms | 140.6–141.8 ms / 141.2–142.3 ms | **NOT RESOLVABLE ABOVE NOISE** — the edge is chunk-cadence-dominated (160 ms test chunks); the reordering removes ~µs of format/emit from the critical span. Tracked live via `handoff→exec` |
| `book_age` histogram fix | `telemetry` regression test + e2e all-edges assertion | silently dropped since 07-16 | recorded | **PROVEN (correctness)** — forensics-affecting, not latency |
| Cold-fire premium (characterization) | `perf_cold_first_fire` | — | first fire 320–675 µs vs steady 17–38 µs across runs | Instrument established; premium eliminated by warm-fire (row above) |
| `[perf]` pinning / SCHED_FIFO / spin, OS host tuning | `os-tune-check.sh`, on-box perf suite | — | — | **INSTRUMENT-ONLY, ON-BOX** — Linux-only; gates 1–4 in RUNBOOK "Fire-path latency pass". No laptop claim possible |
| chunk_ms 80→40 (recognition latency) | `bench-detector` paced, sherpa, QCOM-Q2-2026 dev call, chunk 80 vs 40 grid | *(running — appended below when complete)* | | Expected ~+20 ms mean onset→emit from halved chunk assembly; accuracy invariant per BAKEOFF |

### Net honest summary

The two mechanical wins that survive measurement on the laptop are the **ring wake (−60–120 µs per
idle hop)** and the **first-fire warm (−645 µs on the fire that decides the race)**. The executor
restructure is latency-neutral-to-slightly-negative in burst-intake p95 and unproven (though
plausible) for single-intent wake; if the on-box `handoff→exec` histograms don't vindicate it, the
`FuturesUnordered` change should be reconsidered. Everything else on this box is below the noise
floor, exactly as the pre-measurement audit predicted: the fire machinery was already µs-scale.
The dominant terms remain (a) detector model lag (sherpa ~320 ms lookahead + decode lag — blocked
on an English streaming-CTC model, see below), (b) chunk assembly (~20 ms, measurable now,
gated on-box for prod), (c) OS-tuning tail (on-box only).

## Race reconstruction — MS-RECE replayed under today's lanes (2026-07-17)

Question: with the us-east migration (+ this pass), how does the 07-15 MS-RECE loss replay?
Timeline anchors (post-mortem, RUNBOOK): competitor's first fill **13:27:10.274Z**; our
keyword chunk captured **≈10.672** (−398 ms at audio arrival); decision ≈10.763
(`capture→decision` 91.3 ms); would-be ack ≈**10.867** → net **~593 ms behind**.

| Leg | CDMX then (measured) | us-east now | Evidence class | Race effect |
|---|---|---|---|---|
| box→Kalshi RTT | p50 54.7 ms (floor under the 102 ms warm `send→hdrs`) | p50 **2.2 ms**, p99 7.2 | **MEASURED** (latprobes 07-16/07-17, INGRESS-BAKEOFF §2) | `decision→ack` 104 → **~15–20 ms PREDICTED**; pin via on-box probe-order |
| box→Twilio media | p50 55.3 ms + **4% loss** + ngrok relay | p50 **2.9 ms, 0% loss**, no ngrok | **MEASURED** (RTT/loss); end-to-end audio delta **UNMEASURED** | X_audio recovery: ~100–150 ms by p50 arithmetic; loss-tail removal may be worth much more on the worst chunk — handshake numbers cannot prove it |
| Twilio bridge→media-stream fork | unknown | unchanged | UNMEASURED | 0; the SIP bake-off exists to bypass it |
| `capture→decision` | 91.3 ms | ~same (chunk 80, sherpa) | measured then; unchanged | chunk-40 gate ≈ −20 ms when passed on-box |
| first-fire premium | unmeasured | −645 µs (warm-fire) | **PROVEN** (this ledger) | sub-ms |
| detector onset→emit | sherpa ~1 s class | unchanged | measured (BAKEOFF holdout) | **the remaining gap** — blocked on Tier-1 CTC |

**Replay verdict:** today's ack ≈ `+593 − ~85 (order leg) − X_audio` ms behind the same
competitor. At X_audio ≈ 100–200 ms (p50 arithmetic): **~+300–400 ms behind — still lost**, just
no longer by 600. Even a generous tail-case X only reaches ~even, and RECE swept 27→98¢ in 233 ms.
The migration converts transport from the *dominant* deficit to *recovered-or-nearly-so*; the
residual gap is detector-shaped (sherpa vs a competitor's streaming ASR). Races against slower
competition plausibly flip to winnable on transport alone; marquee instant-words do not until the
CTC engine lands.

**The two measurements that convert this reconstruction's estimates into proofs (operator-run):**
1. **Dual-box capture-bakeoff** — same live call captured simultaneously from the CDMX laptop and
   the us-east box (same Twilio account/carrier, one leg each), cross-correlated with the existing
   `capture-bakeoff` cross-delta machinery (INGRESS-BAKEOFF §3, dual-BOX variant of the dual-carrier
   protocol). Output: X_audio directly, with jitter/stall p99 per box. First natural slot: any
   Tier-A published-number call this week.
2. **On-box probe-order** (existing 1¢ non-mention envelope) — pins real `send→hdrs` from us-east,
   converting the "low-teens" prediction into a VERIFICATIONS-grade number.

## Word-recognition efficiency — status 2026-07-17

**Tier-0 probe re-run (per `docs/CTC-MODEL-SOURCING.md`): still no English streaming-CTC
checkpoint on any shelf.** The sherpa online-CTC model page lists five models as of the
2025-06-30/2025-04-01 releases — all Chinese. icefall's LibriSpeech RESULTS.md contains no
`--causal 1` + CTC-head entry (CR-CTC line is non-streaming only). English streaming zipformers
remain transducer-only. ⇒ **Tier 0 refuted; the several-hundred-ms detector win is blocked on
Tier 1 (GPU finetune of a CTC head onto a causal English checkpoint) — an operator GPU-budget
decision.** No provable English recognition-latency improvement is possible this session beyond the
chunk dial; recording that here per the rule above.

Sources: [sherpa online-CTC models](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-ctc/zipformer-ctc-models.html),
[icefall LibriSpeech RESULTS.md](https://github.com/k2-fsa/icefall/blob/master/egs/librispeech/ASR/RESULTS.md)
