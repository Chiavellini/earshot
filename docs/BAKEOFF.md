# M1 detector bake-off — protocol, corpus, and pre-registered criteria

Not a pipeline layer — this documents the benchmark harness (`src/bench/`),
the ground-truth corpus, and the evaluation protocol. The detector layer
contract lives in `docs/layers/detector.md`.

## Why the protocol is strict

A keyword detector tuned and evaluated on the same audio will look better
than it is; a latency number measured on lossless replay is fiction. Every
rule below exists to keep the reported numbers honest (quant-audit
discipline: pre-registered criteria, dev/holdout split, negative controls,
all grid cells reported).

## Corpus

```
corpus/
  raw/               # original downloads, any rate/codec — GITIGNORED
  telephony/         # processed 8 kHz mono s16 WAV (phone channel) — GITIGNORED
  wideband/          # processed 16 kHz mono s16 WAV (webcast channel) — GITIGNORED
  manifests/*.toml   # ground truth — COMMITTED
  manifests/*.toml.pending  # skeleton whose audio is NOT yet banked — see below
```

**`*.toml.pending` convention (2026-07-28):** `bench::manifest::load_dir`
loads every `manifests/*.toml` and hard-fails validation on unopenable
audio (`src/bench/manifest.rs:81`, `:96-102`) — so a pre-staged skeleton
pointing at an un-banked WAV kills every bench/screen run on that machine.
A manifest whose audio is not yet banked is named `*.toml.pending` (the
loader's extension filter skips it); renaming to `.toml` is step 0 of the
post-event fill. First instance: `f-q2-2026.toml.pending`.

- Sourced from **public earnings-call replays across the Kalshi
  earnings-mention universe** — mix of mega-cap and smaller issuers so
  accents, call quality, and operator intros match production, not just
  polished big-tech IR streams. Target ~5–10 h, ~100–300 keyword
  occurrences, plus ≥1 call containing **zero** target keywords (the
  false-alarm floor).
- Telephony processing (`scripts/corpus-process.sh`): mono → 300–3400 Hz
  band-limit → 8 kHz → G.711 µ-law round trip (matches the M3 SIP dial-in
  path). Engines are NEVER benchmarked on clean speech.
- Wideband processing (webcast channel, W5): mono → 16 kHz s16, NO
  band-limit — this IS the channel the webcast HLS legs deliver
  (`resample_to_hz = 16000`, 77 kbps AAC source), so no extra degradation is
  applied. Bench each engine on the channel it trades on: `telephony/` for
  phone-bridge events, `wideband/` for webcast events. The manifest's
  `audio` path selects the domain; the loader accepts mono s16 at 8 or
  16 kHz (`src/bench/manifest.rs`).
- Ground truth: forced alignment (faster-whisper word timestamps) on the
  **wideband originals**, keyword extraction per VERIFICATIONS.md Q4
  semantics, then human spot-check of a ~20 % random sample to measure
  aligner error (`scripts/corpus-verify.py`).

> ### ⚠ GROUND-TRUTH CORRECTION 2026-07-24 05:21 — READ BEFORE QUOTING ANY LMT NUMBER
>
> The wideband manifests were regenerated at **2026-07-24 05:21:50** (one scripted
> pass) after the aligner caught mentions its first pass had missed. **LMT went
> from 11 to 31 occurrences** — `government` ×13, `sikorsky` ×2, `dividend`,
> `nasa` and others, every one verified against `corpus/transcripts/lmt-q2-2026.words.json`
> by `scripts/verify-occurrences.py` (LATENCY-PROOF §manifest-divergence). This
> was **ground truth getting more correct**, not tuning on the answer.
>
> **Consequence:** every LMT — and therefore every pooled INTC+LMT *dev-split* —
> **recall** figure computed before that timestamp was scored against a
> denominator roughly **3× too small**, so it is not comparable with anything
> measured after the correction.
>
> ⚠⚠ **DIRECTION CORRECTED 2026-07-27 (§L1), and it is the OPPOSITE of what this
> block said this morning.** The original text asserted those numbers *"flattered
> the detector by not counting mentions it missed"* — i.e. optimistic. **That was
> an assumption about WHICH occurrences the aligner missed, and it is measured
> FALSE.** Re-scored on the armed model with both denominators taken from one
> run: **old 11-occ ⇒ recall 0.727; corrected 31-occ ⇒ recall 0.903.** All 20
> added occurrences are **caught** (`government` 13/13, `record` 3/3, `security`
> 3/3, `dividend` 1/1) and all 3 misses (`morfius`) were in the old manifest
> already. **Pre-correction LMT recall is PESSIMISTIC, not optimistic.**
>
> The non-comparability stands; only the direction was wrong, and it was
> asserted rather than measured.
>
> **Unaffected:** false-alarm counts and FA/h (no denominator), `emit_lag`/latency
> percentiles, and TP counts read as raw counts rather than as a fraction.
> **Affected:** anything of the form *TP / occurrences* — and per §L1 the effect
> on LMT is to make the OLD number too LOW.
>
> Figures measured **2026-07-25 or later** (D1b/D1c/D1d, D2/D2b, D1, D3/D3b) used
> the corrected manifests and need no adjustment.
>
> Individually marked instances carry the tag **`⚠ OPTIMISTIC-DENOMINATOR`**.

### Manifest format

```toml
[call]
id    = "AAPL-Q2-2026"
audio = "telephony/aapl-q2-2026.wav"   # relative to corpus root
split = "dev"                          # or "holdout" — PRE-COMMITTED

[[occurrences]]
phrase   = "recession"                 # must equal a config keyword phrase
onset_ms = 812345
end_ms   = 813420
```

`split` is chosen when the manifest is created, BEFORE any engine has run on
the call, and never flipped afterwards. Manifests are read only by the bench
scorer — never by the detector or config compiler.

## Harness

```
cargo run --release -- bench-detector \
  --config configs/<bench>.toml --corpus corpus/ \
  --engines stub,sherpa_kws --chunk-ms 80,160,320 \
  --split dev [--paced] [--calls id1,id2] --out bench-report.json
```

Two passes per grid cell:
1. **Accuracy** (default, replay speed=0 — lossless, hours of corpus in
   minutes): detections match occurrences when `audio_pos_ms ∈
   [onset−250 ms, end+1500 ms]` (post-window absorbs the ~0.4 s engine
   decode lag), greedy one-match-per-occurrence-per-tier. Yields per-tier
   recall, precision, FA/hour.
2. **Latency** (`--paced`, speed=1.0 — real-time, subset via `--calls`):
   onset→emit = `emit_ts − (first_capture_ts + onset_ms)`, reported
   p50/p95/max per tier. This is the number that matters for the trade.

Both passes also report **`lag50ms` / `lag95ms`** — `emit_lag_ms`, the
**audio-domain** lag `audio_pos_ms − occ.end_ms`: how much audio past the word's
end the engine consumed before firing. Signed, so prefix-style firing shows as
**negative**. Unlike the `p50ms`/`p95ms` columns it needs no clock, so the
LOSSLESS pass reports it too and run-to-run machine variance cannot move it.

It is the number to optimise, because on a live HLS feed audio arrives in
segment-sized bursts: this lag is free when the trailing audio is in the same
segment and costs a **whole segment** when it is not, with exposure `A/S`
(measured 19.2 % at a 2 s venue, 9.9 % at 4 s — `docs/LATENCY-PROOF.md`, E0).
The standing "~0.4 s engine decode lag" that sizes the 1500 ms post-window above
is an ONNX-metadata inference (`VERIFICATIONS.md` Q-M1 pin 3), never an observed
distribution; `lag50ms` is what finally measures it.

Self-test (no model, no corpus): `tests/bench_fixture.rs` — tone fixture +
stub detector + `FixtureSpec::occurrences()` ⇒ recall 1.0, FA 0, paced
latency within one chunk.

## Pre-registered holdout criteria (M1 sign-off)

Written BEFORE the holdout run; tuning happens on dev only; the holdout is
run ONCE with the frozen config. All grid cells are reported, not the best
one — a config that only wins at one chunk size is fragile, and that gets
said out loud.

| Metric | Bar |
|---|---|
| Confirmed recall | ≥ 0.90 |
| Confirmed false alarms | ≤ 1 / hour |
| Fast p95 onset→emit (paced) | ≤ 350 ms |
| Fast false alarms | ≤ 4 / hour (Fast tranche is a sized write-off, spec §5.4) |

Known headwind, stated up front: the gigaspeech KWS model is a
chunk-16-left-64 export (~320 ms algorithmic lookahead) and measured
detection lag on test_wavs is ~0.4–0.5 s after word END. The Fast prefix
mechanism claws back ~300–650 ms (measured: `▁LOVE LY` fires 640 ms before
the full word), but ≤350 ms p95 from word ONSET is tight for long keywords.
If the holdout misses the latency bar with everything else passing, the
pre-declared next step is the custom streaming-CTC prefix spotter
(0–80 ms lookahead) behind the same `Detector` trait — not a bar adjustment.

## Corpus v1 (splits pre-committed 2026-07-11, before any engine run)

Public earnings-call replays pulled from per-company earnings-call podcast
RSS feeds (casti.fyi, personal research use; YouTube replays were
bot-blocked for anonymous download). Target keywords chosen from words that
recur in live Kalshi earnings-mention markets (e.g. KXEARNINGSMENTIONNFLX
lists Acquisition / Partnership / Competition-style words): **recession,
guidance, tariff, acquisition, partnership**.

| call id | company | split | occurrences | rationale |
|---|---|---|---|---|
| AAPL-Q2-2026 | Apple | dev | 14 | |
| MSFT-Q3-2026 | Microsoft | dev | 8 | |
| QCOM-Q2-2026 | Qualcomm | dev | 13 | |
| WMT-Q1-2027 | Walmart | holdout | 17 | company never seen in dev |
| CAT-Q1-2026 | Caterpillar | holdout | 42 | company never seen in dev |
| GIS-Q3-2026 | General Mills | holdout | 12 | company never seen in dev |

Corpus v1 totals: **~5.5 h**, **106 occurrences** (tariff 55, guidance 42,
acquisition 5, partnership 4, recession 0). **"recession" is a pure
false-alarm control** — no call says it, so any recession detection is a
false alarm by construction (this substitutes for the zero-keyword call).

**Spot-check (2026-07-11, automated variant):** 12 random onsets re-checked
by independently re-transcribing 4 s telephony clips: 11/12 confirmed; the
single disagreement was the telephony re-transcription mishearing
"tariff-related" as "tear-related" (channel degradation, not an alignment
error — the wideband alignment is correct). Aligner trusted. A by-ear pass
(`scripts/corpus-verify.py`) can tighten this whenever a human has 20 min.

## Dev tuning log (2026-07-11, sherpa_kws, corpus v1 dev = 2.81 h)

Four accuracy rounds at speed=0. Chunk size (80/160/320) produced
bit-identical accuracy — the model's internal chunking governs decoding, so
`chunk_ms` is purely a latency dial; production uses 80 ms.
⚠ **On `sherpa_kws` that dial is INERT below 160 ms** (`docs/LATENCY-PROOF.md`
E0c, 2026-07-25): chunk 40/80/160 produce **byte-identical** output — same TP,
misses, FA, and all 53 false-alarm positions — at the same CPU cost. The engine's
emission grid is 160 ms (GCD of observed stream positions), so any chunk that
divides it lands on the same sample. Arming at 80 ms is output-identical to 160.

Round 1 (all fast_thresholds 0.08, conf 0.20): guidance Conf recall 1.00 but
Fast tier FA-heavy everywhere (recession control 6.4/h, partnership 6.05/h).
Round 2 (thresholds up): raising Fast 0.08→0.15 barely moved FAs — scores
cluster high under boost 2.0; tariff conf 0.10 + boost 3.0 lifted recall
0.56→0.78. Round 3 added FA-position dumps; **FA autopsy** (independent
re-transcription at every FA position) showed the false alarms are phonetic
neighbors, not noise:
- recession prefix `▁RE CE S` fired on "reception" and "a session" →
  lengthened to "recess" (`▁RE CE S S`): Fast FA 6.05/h → 1.78/h.
- guidance prefix `▁GU ID` fires on guiding/guided (constant on calls) and
  no token boundary exists before ANCE → **prefix firing is structurally
  unavailable for "guidance"**; Fast tier dropped (Confirmed releases the
  full budget when Fast never fired).
- partnership prefix "partner" collides with the standalone word → dropped.

This token-boundary + FA-autopsy + market-outcome procedure is generalized in
`docs/PREFIX-SAFETY.md` and tooled by `earshot compile-keywords` (Gate a: prints
each keyword's token-cut on any model's BPE). The cuts are model-specific — they
must be recomputed for the streaming-CTC model when it lands. (On the gigaspeech
BPE, `inflation` = `▁IN F L ATION` has no safe Fast prefix either: its only cut,
`▁IN F L` = "infl", collides with influence/influx/inflate.)

Frozen dev numbers (round 4): guidance Conf recall 1.00 / 1.07 FA/h;
tariff Conf 0.78 / 0.36 FA/h, Fast 0.67 / 0.36 FA/h; partnership Conf 0.75;
acquisition Conf 0.67 (n=3); recession (pure FA control, 0 positives) Fast
1.78/h, Conf 1.42/h. Known gaps, stated: recession thresholds are FA-tuned
only (no positive samples yet); telephony band costs tariff ~2/9 recall
(the aligner spot-check showed even wideband ASR mishears degraded
"tariff-related").

## Results log

| date | split | config | outcome |
|---|---|---|---|
| 2026-07-11 | dev ×4 rounds | configs/bakeoff.toml (frozen at r4) | tuning log above |
| 2026-07-11 | holdout (single run, frozen config) | chunk 80, sherpa_kws | accuracy below; paced latency on CAT below |
| 2026-07-18 (transcribed 07-20) | dev paced subset (AAPL, 1.02 h) | chunk 80, sherpa_kws vs streaming_ctc t=0.15 | streaming_ctc paced latency below — **no cut at 320 ms-quantum export; 350 ms bar fails ~4×**; follow-ons P1–P4 |

### Holdout accuracy (2.68 h, unseen companies: WMT / CAT / GIS)

| phrase | tier | recall | precision | FA/h | vs bar |
|---|---|---|---|---|---|
| acquisition | Confirmed | 1.00 (2/2) | 1.00 | 0.00 | ✓ |
| guidance | Confirmed | 0.96 (22/23) | 0.79 | 2.24 | ✗ FA bar (≤1/h) |
| tariff | Confirmed | 0.89 (41/46) | 0.98 | 0.37 | ✗ recall bar by ONE occurrence (needs 0.90) |
| tariff | Fast | 0.54 | 0.96 | 0.37 | (no Fast recall bar) |
| recession (FA control) | Confirmed | — | — | 0.37 | ✓ |
| recession (FA control) | Fast | — | — | 3.36 | ✓ (≤4/h) |

Aggregate Confirmed recall 65/71 = **0.915**.

**Holdout FA autopsy (post-hoc, reporting only — config unchanged):** the
FAs are phonetic neighbors or ground-truth errors, not noise: recession
fires on "re-cips" (CAT engine-product jargon, ×4), "receive your
question", operator boilerplate ("question and answer session"); guidance
on "the guide" (true FA per Q4) and on clips whose wideband transcript is
garbled; the single tariff "FA" sits on audio transcribed as "for the
**terrorists** introduced in the beginning" — i.e. the ALIGNER misheard
"tariffs", the detector was right, ground truth was wrong. Measured FA
rates are therefore upper bounds, and guidance's 2.24/h likely overstates.

### Holdout paced latency (CAT-Q1-2026, 1.05 h real-time, chunk 80)

| phrase | tier | n | p50 | p95 |
|---|---|---|---|---|
| tariff | Fast | 21 | 926 ms | 1066 ms |
| tariff | Confirmed | 34 | 961 ms | 1136 ms |
| guidance | Confirmed | 2 | 688 ms | 1386 ms |
| acquisition | Fast=Confirmed | 1 | 856 ms | 856 ms |

(Latency = word ONSET → detection emitted, wall clock, real-time replay.)

## M1 verdict against the pre-registered criteria (2026-07-11)

| criterion | bar | measured | verdict |
|---|---|---|---|
| Confirmed recall | ≥ 0.90 | 0.915 aggregate; tariff 0.89, guidance 0.96, acquisition 1.00 | **marginal** (tariff one occurrence short) |
| Confirmed FA | ≤ 1/h | tariff 0.37, acquisition 0.00, recession-control 0.37; guidance 2.24 (upper bound — autopsy shows ground-truth errors inside) | **guidance fails** |
| Fast p95 onset→emit | ≤ 350 ms | **1066 ms** (tariff, n=21) | **fails, decisively** |
| Fast FA | ≤ 4/h | 3.79 max (recession control) | ✓ |

Interpretation, as pre-declared: precision is production-grade (0.95–1.00)
and Confirmed accuracy is within touching distance of the bars on unseen
companies, but the latency bar is unreachable with this engine — the
gigaspeech KWS model's chunk-16-left-64 export bakes in ~320 ms lookahead
and ~0.4–0.5 s decode lag, and no threshold tuning changes that. The Fast
prefix mechanism works exactly as designed (fires before the full word,
40–300 ms earlier depending on how many tokens the prefix cuts) but it is
riding on top of a model whose floor is ~3× the bar.

**Pre-declared consequence (not a bar adjustment): the next detector
milestone is the custom low-lookahead streaming spotter** (Zipformer-CTC or
small-chunk transducer, 0–80 ms lookahead, own prefix-firing logic on frame
posteriors) behind the same `Detector` trait, benchmarked by this same
harness on this same corpus. Until then the sherpa_kws engine is usable in
Confirmed-weighted mode where ~1 s from word onset still beats human
listeners reacting to the same audio feed.

Secondary follow-ups: corpus v2 (more calls; positive "recession" samples;
by-ear verification pass), guidance FA re-scoring after fixing the aligner
misses, per-keyword `min_confidence` gates once an engine exposes scores.

## Recall-sweep finding (2026-07-14) — the tariff misses are NOT threshold-bound

Ran a threshold/boost sweep on `tariff` (harness `scratchpad/sweep.py`;
`bench-detector --split dev`). On AAPL the Confirmed result is **bit-identical
across `confirmed_threshold` = 0.10 / 0.07 / 0.03 AND `boost` = 3.0 / 5.0**:
7 tp, **2 miss**, 0 FA, recall 0.78, precision 1.00. The 2 missed occurrences
produce NO detector hypothesis at all — the KWS model emits nothing for them on
band-limited 8 kHz telephony audio, so lowering the threshold (even to 0.03) or
raising boost (to 5.0) recovers zero of them. This empirically confirms the
telephony band-limit hypothesis (`tariff ~2/9 recall cost`) and means:
- **Config tuning cannot close the tariff recall gap** — it is a model/acoustic
  ceiling, not an operating-point choice.
- Lowering thresholds is a NET LOSS in the fill posture (a false alarm now spends
  real money) for zero recall gain, so the **frozen thresholds stay** (tariff
  0.10, precision 1.00 — the right point for precision-critical fillable trading).
- The real recall lever is a **telephony-adapted / streaming-CTC model** (the
  M4-blocked detector work, `docs/CTC-MODEL-SOURCING.md`), or front-end work
  (loudness-normalization/AGC — none exists today), NOT thresholds.
- NB: full-corpus dev+holdout confirmation was not run to completion here (one
  accuracy pass >9.5 min on this box; the sweep harness is committed for a
  faster run). The single-call result is directional but consistent and strong.

## Fast / unclear speech robustness (2026-07-14)

Real calls are fast and sometimes slurred. Tooling: `scripts/corpus-degrade.sh`
(ffmpeg `atempo` time-compression = tempo change, no pitch shift = "speed-talking")
+ `scripts/manifest-rescale.py` (scale onsets by 1/T) → sibling `corpus-t125/` /
`corpus-t15/` roots benched with the existing harness.

**Finding 1 — speed halves SHORT-word recall; long words are robust; precision
holds (AAPL, tariff n=9).**

| tempo | tariff Conf recall | tariff precision | guidance Conf recall |
|-------|--------------------|------------------|----------------------|
| 1.00× | 0.78               | 1.00             | 1.00                 |
| 1.25× | 0.56               | 1.00             | 1.00                 |
| 1.50× | 0.33               | 1.00             | 1.00                 |

Short "tariff" (▁TA RI FF) collapses under compression; long "guidance"
(▁GU ID ANCE) is unaffected. Speed causes MISSES, never false alarms — so the
tiered fill posture stays money-safe (fast speech = missed trade, not a bad fill).
Recall-vs-length ⇒ the lever is a lower-lookahead / stronger MODEL
(streaming_ctc), not tuning.

**Finding 2 — naive per-chunk RMS AGC HURTS recall; front-end level-norm is NOT
the lever (rejected).**

| condition            | tariff Conf recall |
|----------------------|--------------------|
| normal, AGC off      | 0.78               |
| normal, AGC on       | 0.22  ⬅ regressed  |
| quiet (¼ level), off | 0.67               |
| quiet, AGC on        | 0.56  ⬅ worse      |

Per-80 ms RMS-to-target gain (target 0.08, max 8×) distorts the level the
gigaspeech model expects and destroys recall. Note quiet audio alone barely dents
recall (0.78→0.67 with AGC off) — the model is already fairly level-robust, so
there is little for gain to recover. AGC code was REVERTED (no dead knob). A
stateful/smoothed AGC is unproven and low-priority; the dominant lever is temporal
(model), per Finding 1.

## Miss-autopsy cell 1 — int8 vs fp32 encoder (2026-07-14): quantization IS a contributor

First (cheapest) rung of the miss-autopsy (`docs/TELEPHONY-DETECTION-SPEC.md` §10.5).
Identical corpus/config/keyword-graph/streaming; the ONLY variable is encoder+joiner
precision (shipped int8 vs fp32), via the new `[detector.sherpa_kws] fp32` toggle
(decoder is fp32 in both). tariff Confirmed recall:

| call | n | int8 | fp32 | precision (both) |
|------|---|------|------|------------------|
| AAPL-Q2-2026 (dev) | 9 | 0.78 (7/9) | **0.89 (8/9)** | 1.00 |
| CAT-Q1-2026 (holdout) | 39 | 0.87 (34/39) | **0.90 (35/39)** | 0.97 |

Fast tier also rose (AAPL 0.67→0.78; CAT 0.54→0.59). **fp32 recovers misses at ZERO
precision cost**, and the compute penalty is negligible — CAT wall-clock 329 s (fp32)
vs 324 s (int8), ~11× real-time, well inside `perf_detector_rtf` (>3×). So the
"int8 encoder for compute" tradeoff buys ~nothing on this workload while costing
recall.

**Conclusion:** part of the tariff recall gap is INT8 QUANTIZATION, not a pure
acoustic ceiling — the earlier "acoustic-bound" claim is partially refuted. A
residual gap remains (CAT 4/39 misses at fp32), so the remaining autopsy cells
(channel / beam / tokenization / streaming-offset / speed, via the offline ONNX
encoder harness) are still needed to isolate the rest. Cheap action: set
`[detector.sherpa_kws] fp32 = true` in the live configs (recall win, no precision
or meaningful latency cost). Samples are still modest (n=9, n=39) — confirm on full
dev+holdout before treating the magnitudes as final.

### Full-dictionary regression (all 6 calls, all 5 keywords, int8 vs fp32)

| keyword | tier | int8 recall/FA | fp32 recall/FA | Δ | live-traded |
|---------|------|----------------|----------------|---|-------------|
| tariff | Confirmed | 0.85 / 2 | **0.90 / 1** | recall +0.04, FA −1 | yes |
| recession (FA ctrl) | Confirmed | — / 4 | — / **3** | FA −1 | yes |
| guidance | Confirmed | 1.00 / 6 | 1.00 / **7** | FA **+1** | no |
| acquisition | Confirmed | 0.75 / 2 | 0.75 / **4** | FA **+2** | no |
| partnership | Confirmed | 0.75 / 1 | 0.75 / 1 | flat | no |

Aggregate Confirmed triggers 83 → 86 (+3, all on non-traded words). **Both
real-money keywords (tariff, recession) improved on BOTH axes** (tariff recall up +
FA down; recession FA down). fp32 regressed FA on two NON-traded keywords
(acquisition +2, guidance +1) — phonetic-neighbour false alarms the higher-fidelity
encoder surfaces. Per the deployment guard ("no FA regression on ANY keyword"), the
global live flip was initially HELD for review: fp32 is not universally FA-neutral,
so the untested live words (inflation + event-specific, no corpus positives) could
also see more FAs.

**Operator decision (recorded 2026-07-14, commit `9a163d3`): flip `fp32 = true`
in the live configs.** Rationale: both money-path keywords improve on both axes;
every FA regression sits on words the live configs hold at 1¢ observe-only
(`max_price_cents = 1`, non-crossing), where a false alarm costs nothing and is
itself useful validation data. Guard for future configs: before promoting any
observe-word to a fillable tier, re-check its fp32 FA rate on corpus — the
"no FA regression" bar applies at promotion time, not only globally.

## streaming_ctc first corpus results (2026-07-17, dev split — English model landed)

The Tier-0 hunt succeeded (`docs/CTC-MODEL-SOURCING.md`):
`sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18` (LibriSpeech,
`--causal 1 --use-ctc 1`, 320 ms decode quantum, ~130 ms right-context —
pins in `VERIFICATIONS.md`). First dev accuracy pass, chunk 80, int8,
CDMX laptop, vs sherpa_kws at its shipped config (threshold 0.20 boost 2.0):

| keyword (Confirmed) | sherpa_kws recall / FA | streaming_ctc t=0.15 recall / FA |
|---|---|---|
| guidance | 1.00 / 3 | **1.00 / 0** |
| recession (FA ctrl, 0 occurrences) | — / 4 | — / **0** |
| partnership | 0.75 / 1 | **1.00** / 1 |
| acquisition | 0.67 / 2 | 0.67 / 1 |
| tariff | 0.78 / 1 | **0.44** / 3 |
| **aggregate** | **0.89 recall, 3.9 FA/h** | **0.83 recall, 1.8 FA/h** |

Threshold evidence (dev-tuning surface): 0.5 and 0.35 identical; 0.15
recovers partnership 0.50→1.00 with zero FA cost; **tariff is pinned at 4/9
at every threshold and identical under the fp32 encoder** (quantization
ruled out, same AAPL call). Diagnosis: the telephony-domain gap the sourcing
doc pre-declared — a LibriSpeech-clean model on 300–3400 Hz band-limited
audio, on a word whose fricatives the band-limit destroys; sherpa also
benefits from keyword boost (2.0), which the CTC scorer does not implement.

Read this with two qualifiers: (1) these are telephony numbers — the
webcast lane feeds 16 kHz wideband, far closer to the model's training
domain (no wideband ground-truth corpus exists yet to bench it; the NFLX
07-16 capture is the seed material); (2) the model's PURPOSE is the latency
bar — the paced run (Fast p95 onset→emit vs sherpa's 1066 ms / the 350 ms
bar) is the decisive number, pending at time of writing. Follow-ons if
promoted: CTC keyword boosting in the scorer, telephony finetune (Tier 1),
chunk-16 re-export (160 ms quantum). Holdout stays UNRUN pending a frozen
config decision per protocol.

### streaming_ctc paced latency (dev subset: AAPL-Q2-2026, 1.02 h real-time, chunk 80; run 2026-07-18 00:24, transcribed 2026-07-20)

The decisive paced number arrived, and it is a **negative**: at the current
320 ms-quantum export, streaming_ctc delivers **no onset→emit latency cut**
over sherpa_kws. Raw artifacts: `state/bench/bench-paced.{json,log}`.

sherpa_kws (in-run comparator, same paced replay):

| phrase | tier | n | p50 | p95 |
|---|---|---|---|---|
| tariff | Fast | 6 | 976 ms | 1017 ms |
| tariff | Confirmed | 7 | 1017 ms | 1329 ms |
| guidance | Confirmed | 5 | 897 ms | 1785 ms |

streaming_ctc (t=0.15 config from the dev accuracy pass):

| phrase | tier | n | p50 | p95 |
|---|---|---|---|---|
| tariff | Fast | 6 | 984 ms | 1294 ms |
| tariff | Confirmed | 4 | 1016 ms | 1294 ms |
| guidance | Confirmed | 5 | 965 ms | 2107 ms |

(Latency = word ONSET → detection emitted, wall clock, real-time replay —
same convention as the holdout table above. guidance produced 0 Fast-tier
fires on BOTH engines, 5/5 Confirmed on both.)

**Verdict vs the bars:** streaming_ctc tariff Fast p95 **1294 ms** vs the
**350 ms bar** — fails by ~4×; vs sherpa's in-run 1017 ms (holdout incumbent
1066 ms) — no cut, slightly worse at p95, p50s equal (~980 ms). The
geometry hypothesis (0–80 ms lookahead ⇒ large cut) did NOT materialize:
the 320 ms decode quantum evidently dominates end-to-end emit timing just
as sherpa's internal chunking does (consistent with the chunk_ms 80→40
refutation in LATENCY-PROOF.md). Accuracy in-run was consistent with the
07-17 dev pass — tariff Confirmed 4/9 with 3 FAs (same three autopsy
timestamps at ~3352–3380 s of AAPL), i.e. the known telephony-domain gap,
not a new regression; tariff Fast stayed 6/9 with 0 FA.

**Caveats:** thin sample — one call, ~1.0 h, only two phrases produced
latency rows; laptop run (3.5× wall-clock variance caveat applies, though
paced replay anchors to audio onset so cross-engine comparison within the
same run is fair).

**Consequence:** the zero-GPU-spend promotion path is closed at this
export. Pre-declared follow-on ladder (RACE-CHECKLIST.md): **P1** chunk-16
re-export (160 ms quantum) + re-bench, **P2** scorer keyword boost, **P3**
prefix-fire diagnosis (below — the Fast tier IS the prefix tier and IS in
this table; the question was why it doesn't fire early), **P4** GPU
finetune go/no-go on the P1–P3 evidence. Holdout remains UNRUN per
protocol (no frozen config to one-shot).

### P1 RESULT — chunk-8 (160 ms quantum) RUN 2026-08-06: **−101 ms, but recall FAILS the gate**

**The export existed the whole time.** `models/streaming-ctc-en-20260723-chunk8` has been
staged on the us-east-1 box since 2026-07-23, alongside a matched
`streaming-ctc-en-20260723-chunk16` control from the same toolchain and checkpoint. Verified
geometry: chunk8 `T=29, decode_chunk_len=16` (160 ms) vs chunk16 `T=45, decode_chunk_len=32`
(320 ms). P1 was never blocked on producing a model — it was blocked on nobody running the
bench, and until 2026-08-05 the harness could not put two models in one run.

Run: `bench-detector --config configs/bakeoff-webcast-ctc16.toml --engines streaming_ctc
--chunk-ms 80 --model-dir <chunk16>,<chunk8> --split dev`, lossless, **interleaved in ONE
invocation** (both cells share machine state). us-east-1 box, 16 cores, idle. Dev split,
**5.15 h**, calls AXP/intc/lmt/VZ. 41 keywords. Exit 0.

**Latency — real, but ~60% of the predicted cut.** Per-phrase `consumed_lag_p50_ms`
(cross-engine-comparable), Confirmed tier, phrases scoring in both cells (n=10):

| | Δ |
|---|---|
| **TP-weighted mean** | **−101 ms** |
| median | −75 ms |
| best / worst | security −190 ms · nasa **+160 ms** (tp=1) |

Halving the quantum (320→160) returns ~100 ms, not 160 — consistent with the ~130 ms
right-context term being unchanged and part of the lag being phrase-alignment, not quantum.

**Accuracy — FAILS the pre-registered bar.** Headline recall is identical (0.818 both,
TP=63 / miss=14) but that is an artifact: `panther lake` carries **2,354 false alarms** and
its +3 TP masks losses elsewhere. Excluding that one pathological phrase:

| | chunk16 | chunk8 |
|---|---|---|
| recall | **0.842** | **0.789** |
| TP / miss | 48 / 9 | 45 / 12 |
| FA/h | 15.0 | 14.4 |

Per-phrase: `sikorsky` **2/2 → 0/2 (lost entirely)**, `security` 7→6. FA cost is nil.

**VERDICT: FAIL.** The gate was "−160 ms at unchanged recall/FA"; this is −101 ms *with* a
5.3-point recall drop. That is the "lag cut bought with a recall drop" case, called in
advance. Chunk-8 is below the trained 16/32/64 grid and the regression is exactly where that
predicts. **Do not promote.**

**Caveats, stated:** thin sample — 5.15 h, 63 TPs, and `sikorsky` is n=2, so the sharpest
single signal rests on two occurrences. Lossless run, so `consumed_lag` is audio-domain
(correct for a quantum question) and no wall-clock claim is made. Holdout remains UNRUN.

**Consequence:** −101 ms does not change a race that needs −340 ms to place an order at all
(`docs/RACE-STRATEGY.md`, TLN 26AUG05). Combined with the recall cost, quantum reduction *on
this checkpoint* is not the lever. The open path is a model **trained** for a small quantum
(G2 shortlist: NVIDIA Parakeet-Realtime-EOU-120M-v1, 80 ms), not a smaller export of this one.

### ⚠ P1 IS MISLABELLED EVERYWHERE — "chunk-16" is what we ALREADY run (2026-08-05)

`P1 chunk-16 re-export (160 ms quantum)` cannot deliver anything: **the armed
model already IS the chunk-16 export, and chunk-16 is 320 ms, not 160 ms.**
Evidence, all local and checkable:

- the encoder on disk is `models/sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18/`
  **`ctc-epoch-30-avg-3-chunk-16-left-128.onnx`** — the chunk size is in the filename;
- its metadata is `decode_chunk_len=32` → 320 ms (`VERIFICATIONS.md`), i.e. icefall's
  `decode_chunk_len = chunk_size × 2` in 10 ms fbank frames;
- upstream publishes only this one export for the checkpoint
  (`csukuangfj/icefall-asr-librispeech-streaming-zipformer-small-2024-03-18`).

Re-exporting at chunk-16 would reproduce the current artifact byte-for-byte.
**A 160 ms quantum requires `--chunk-size 8` (`decode_chunk_len=16`).** This doc's
own G2 section already had the convention right for the KWS lane ("chunk-8
(160 ms)"); the CTC lane text did not. Same correction is owed in
`docs/CTC-MODEL-SOURCING.md:15`, `docs/RACE-CHECKLIST.md:67`, and
`docs/RACE-STRATEGY.md:61`, which are other nodes' files.

**The catch that makes P1 a real experiment rather than a build step:** the
checkpoint's trained chunk grid is 16/32/64 — **8 is below it**. A chunk-8 export
runs the encoder at a chunk size it never saw in training, so quality is an open
question, not a given. That is precisely what the interleaved bench below is for;
do not promote a chunk-8 export on the latency number alone.

### How to run P1 once a chunk-8 export exists (harness ready 2026-08-05)

`bench-detector` grids engine × chunk_ms × **model_dir**. Sweep both exports
in ONE invocation so the cells share machine state — a latency delta measured
across two separate runs is not defensible on a laptop with 3.5× wall-clock
variance (the E0c method, `docs/LATENCY-PROOF.md`):

```
cargo run --release -- bench-detector \
  --config <cfg> --engines streaming_ctc --chunk-ms 80 --paced \
  --model-dir models/sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18,models/<chunk8-export> \
  --calls <one-call> --out bench-p1.json
```

Read `consumed_lag_p50_ms` — it is the cross-engine-comparable column;
`emit_lag_*` is engine-defined. The pre-registered expectation is a
**−160 ms** shift in decode lag (320 ms quantum → 160 ms) at unchanged
recall/FA; a lag cut bought with a recall drop is a FAIL, not a win. Because
chunk-8 is outside the trained grid, a recall drop is a live possibility and
the accuracy columns are the gate, not a formality.

Omitting `--model-dir` leaves the config's model alone and produces exactly
the pre-2026-08-05 grid and table. A `--model-dir` that cannot be applied
(engine `stub`, no matching `[detector.<engine>]` section, or a non-directory
path) REFUSES rather than silently running the configured model twice — two
identical cells would read as "the model made no difference," which is the
same shape as the genuine E0c null result and would be indistinguishable
from it.

**Harness validated 2026-08-05**, not the export (which does not exist yet):
sweeping the *same* model via two path spellings produced byte-identical
`rows` and `fa_autopsy`, and `--model-dir <the config's own path>` reproduced
the no-flag cell exactly. Determinism is the property the interleaved
comparison depends on, so it is checked rather than assumed.

### streaming_ctc prefix-fire diagnosis (P3, 2026-07-21) — prefix-firing is NOT the overtake lever for this vocabulary

The paced table above already contains the prefix number: streaming_ctc's
**Fast tier fires on the cut-prefix token target** (`ctc_scorer` Fast entry
= cut prefix, Confirmed = full word — same tier semantics as sherpa). So the
"unmeasured overtake mechanism" was in fact measured, and the diagnosis of
*why it doesn't overtake* is now definitive, from two evidence sources that
needed no new run:

1. **The measured prefix payoff is ~32 ms, not 250–400 ms.** tariff Fast p50
   984 ms vs Confirmed p50 1016 ms — the prefix fires 32 ms earlier, and at
   p95 they are identical (1294 ms). The overtake thesis (COMPETITOR-ANATOMY
   §"What BEATS them") needs the prefix ~250–400 ms before word-END; it
   delivers ~one output frame.

2. **Root cause — the safe prefixes only shave the FINAL BPE token** on this
   model (`compile-keywords --model-dir …-ctc-small-2024-03-18`):
   - tariff: fast `▁T AR I` vs confirmed `▁T AR I FF` — cut 3/4 (drops only `FF`)
   - recession: `▁RE CE S S` vs `▁RE CE S S ION` — drops only `ION`
   - acquisition: `▁A C QUI S` vs `▁A C QUI S ITION` — drops only `ITION`
   - guidance, partnership: **no safe prefix at all** (`fast_prefixes = []` —
     `▁G U ID` prefixes guiding/guided, `▁PART` collides with "partner";
     PREFIX-SAFETY §collision).
   The earliest *non-colliding* prefix for the earnings-mention vocabulary
   lands one token before the word end. So the acoustic head start is ~one
   phoneme (tens of ms), and the 320 ms decode quantum + ~130 ms right
   context swallows it whole.

**Verdict:** prefix-firing is correctly built and safe, but it **cannot be
the overtake move for these keywords** — their token structure affords no
early safe cut. The only remaining lever to beat a full-word competitor ASR
is a **lower-latency full-word detector** (smaller decode quantum via P1
chunk-16, or a faster model family — the new G2/model-breadth search).
This retires the "SIP + prefix-CTC beats wave-1" row of the stack math as
written; see RACE-STRATEGY §3. (chunk-16/P1 would cut the absolute floor
~150 ms but does not create an early prefix — it does not, by itself, make
prefix-firing overtake.)

### G2b — Parakeet-Realtime-EOU-120M measured (2026-07-21, dev call AAPL, telephony-upsampled): marginal, and RTF < 1 on laptop

The G2 "swap to a faster 2026 model" lever, benched via `parakeet-rs` (proven
featurizer + RNN-T decode; same ort 2.0-rc.12 as us) on the SAME dev call the
sherpa/CTC paced bench used. Isolated harness streams the 8 kHz telephony call
(linear-upsampled to the model's 16 kHz) in 160 ms chunks; records the AUDIO
position at which each keyword first appears in the RNN-T transcript.

| word | word-end (ms) | emit audio-pos (ms) | **decode-lag past word-end** | onset→emit |
|---|---|---|---|---|
| tariff (occ 1) | 63580 | 64000 | **420 ms** | 840 ms |
| guidance (occ 1) | 335520 | 335840 | **320 ms** | 580 ms |

> **⚠ CORRECTION 2026-07-21 (methodology): this run was NOT a fair test — re-open.**
> Finding #2 below (RTF 0.68×) is **WITHDRAWN**: it was measured on the CDMX **laptop
> (Apple M2)**, not the server, so it says nothing about the model on production
> hardware. (The current box, t3a.large / 2-vCPU EPYC 7571, is actually *slower* than
> the M2 — RTF is a hardware-provisioning choice, fixed by a GPU/high-CPU instance, not
> a model limit.) Finding #1 (decode-lag) is a valid CPU-independent number but under
> three unfair conditions — **default `blank_penalty=0`** (the emit-sooner knob, untuned),
> **telephony audio** (Parakeet is wideband-trained; webcast 16 kHz is its domain), and
> the **EOU** variant (vs the eager **TDT**). Corrected re-bench in progress (blank_penalty
> sweep × TDT × wideband, laptop; RTF on a real GPU instance). Treat the verdict below as
> preliminary/superseded.

**Verdict (PRELIMINARY — see correction above) — the STT swap is NOT the easy win the recon implied:**
- decode-lag **~320–420 ms past word-end** is only *marginally* better than
  sherpa's pinned ~400–500 ms — not the ~150–250 ms the 80 ms-quantum hope
  implied. (Reality check: the EOU export streams at **160 ms chunks**, not
  80 ms; and these audio-position numbers are the *optimistic* case — see RTF.)
- onset→emit ~580–840 ms beats sherpa/CTC's ~900–980 ms p50 modestly, but is
  nowhere near the ~350–500 ms needed to beat a 200–400 ms competitor ASR.
- **RTF = 0.68× on this laptop CPU (400 s audio took 586 s wall)** — the 120M
  model *cannot keep up with a live stream here*; in a real-time deployment it
  falls progressively behind, so the true wall-clock decode-lag is WORSE than
  the audio-position numbers above. It needs a materially faster CPU (the box,
  or more) just to run real-time, before any headroom.

**Caveats (why this is preliminary, not a final kill):** n=2 (thin); telephony
8→16 kHz linear upsample (Parakeet is wideband-trained — on true 16 kHz webcast
audio accuracy, and possibly latency, would differ); the **EOU** variant is
end-of-utterance-oriented and may emit at utterance boundaries (a different
Parakeet variant — TDT, or parakeet-ctc-0.6b — could have tighter emit
dynamics). Follow-ups: full-corpus run for n; a wideband (16 kHz) call; the TDT
variant. But the direction is clear: **decode-lag is stubbornly ~350–450 ms
across every model we've measured (sherpa, streaming_ctc, Parakeet-EOU)** — the
"faster off-the-shelf model" lever is weaker than hoped, which strengthens the
case that closing the detector gap may genuinely require the GPU finetune (P4)
or a wideband-native setup, not just a model swap. Artifacts: session scratch
`pk-bench/` + `parakeet-eou/`.

#### G2b experiment A.1 — blank_penalty sweep (2026-07-21): NO decode-lag improvement (lever spent)

Tuned the EOU emission-eagerness knob (`set_blank_penalty`) on the tariff 90 s window
(word-end 63580 ms): penalty 0/−1/−2 all emit tariff at **64000 ms (decode-lag 420 ms,
identical)**; penalty −3 breaks decoding (tariff no longer emitted). So the emit-sooner
lever does NOT reduce decode-lag — the ~420 ms is **structural** (the model needs ~2×
160 ms chunks of audio past word-end to emit, independent of eagerness). (Laptop RTF over
these identical-work runs ranged 0.28×–1.08× = confirmed noise; RTF is not measurable on
the laptop — experiment B, a real GPU host, is required for any RTF claim.)
Remaining A lever: **TDT variant** (different decode architecture). (The wideband test was
NOT blocked after all — the raw wideband source `corpus/raw/aapl-q2-2026.mp3` exists and the
manifest onsets were aligned on it; result in A.2 below.)

#### G2b experiment A.2 — WIDEBAND (native 16 kHz) domain: SAME decode-lag (structural)

The fairest test — Parakeet is wideband-trained, and the webcast lane is 16 kHz. Decoded the
wideband original (`corpus/raw/aapl-q2-2026.mp3`) to 16 kHz (manifest onsets are aligned on the
wideband source, so they apply directly) and re-ran tariff (word-end 63580 ms):
**tariff emits at 64000 ms → decode-lag 420 ms — IDENTICAL to the telephony 8 kHz result.**

**Consolidated A verdict (EOU variant): decode-lag ~420 ms is STRUCTURAL.** It is invariant across
the two levers that could plausibly move it — `blank_penalty` (0/−1/−2 identical) AND audio domain
(telephony 8 kHz == wideband 16 kHz). tariff pins to the chunk ending 64000 ms = **2× 160 ms chunks
past word-end**, every time. This is the model's inherent streaming lookahead (chunk + right-context
+ the word must complete in-chunk), the same shape as sherpa's chunk-16 (320 ms) + decode lag.
**The off-the-shelf model swap (EOU) does NOT beat sherpa on decode-lag, and the tuning levers don't
help.** Only the **TDT** variant (different decode) remains untested; the trend is strongly negative.

**Deeper implication (firming up):** decode-lag ~350–450 ms now looks like an *irreducible floor for
full-word streaming ASR* on this vocabulary — sherpa, streaming_ctc, and Parakeet-EOU all land there,
across domains and tuning. To break it you must fire on a partial word (prefix — retired, no safe
early prefix for this vocab) or predict ahead. So the detector may let us **MATCH** competitors (who
face the same physics) but not dramatically *beat* them; the beatable edge is in the OTHER terms —
audio transport (SIP) and execution (us-east-2 + FIX) — where we are currently behind but which ARE
closeable. **Consequence for the GPU RTF test (B): moot for EOU** — no point measuring real-time
viability of a model that doesn't improve decode-lag. B is gated on TDT reviving the case.

## Webcast fill-tier promotion gate (G4/FOMC) — PRE-REGISTERED 2026-07-23

Written BEFORE any webcast-corpus tuning run (handoff P1). Purpose: decide
whether FOMC 2026-07-29 may run fill posture. Fill posture remains
user-gated even on green; red means probe/observe only.

**Corpus (wideband, 16 kHz — §Corpus):** the three 07-23 tapes.
- **Dev = LMT + INTC** (arm tapes with scored per-word outcomes: false-fire
  prints APPL/TRUM/PERU/IRAN, misses ARIZ/DESK/GOVE/MOBI/VENT/ORIO).
  Per-keyword `fast_threshold`/`confirmed_threshold`/`boost`/variants are
  tuned here, AND generalized into written per-class policy rules
  (compound-phrase / OOV-proper-noun / short-common-word classes).
- **Holdout = AAL** (capture-only tape, 8 swept + 7 unswept words). The AAL
  keyword config is produced by applying the dev class-policy rules WITHOUT
  looking at AAL audio or truth. One run, frozen config, all cells reported.

Rationale: FOMC's keywords will be unseen, so the gate must test the tuning
PROCEDURE on an unseen event — per-keyword numbers memorized on dev prove
nothing about next week.

| Metric (Confirmed tier unless noted) | Bar |
|---|---|
| Dev: FA on the named 07-23 false-fire contexts (PERU, IRAN, APPL, TRUM, ULTR†) | 0 |
| Dev: FA rate overall | ≤ 0.5 / h |
| Dev: recall on the settlement-YES caught set (GOLD, MORF, EURO, NASA, CLI, FOUN, PANT, SECU) | ≥ untuned baseline (no trade of recall for precision on words we already catch) — **⚠ OPTIMISTIC-DENOMINATOR, see below** |
| Holdout (AAL): recall on the settlement-YES spoken words (ATH, BASI, CHIC, RECO, WEAT) | ≥ 4/5 |
| Holdout (AAL): FA rate | ≤ 1 / h |

⚠ **OPTIMISTIC-DENOMINATOR HAZARD ON THE DEV RECALL BAR (flagged 2026-07-27).**
This bar is *relative* — "≥ untuned baseline" — and these bars were pre-registered
**2026-07-23**, the day **before** the 2026-07-24 05:21 ground-truth correction
took LMT from 11 to 31 occurrences (§Corpus). A relative bar is only meaningful if
both sides are scored against the same denominator, so **any untuned-baseline
number recorded before that timestamp must NOT be used as this gate's reference** —
comparing a post-correction treatment against a pre-correction baseline silently
moves the bar. Three of the eight named dev keywords are LMT's (GOLD, MORF, EURO,
NASA), and `government`/GOVE — listed above as a *miss* — is exactly where the
correction added 13 occurrences.
**Required before this gate is scored: recompute the untuned baseline on the
corrected manifests, in the same run as the treatment.** The bar itself is
unchanged and is NOT being renegotiated — only the reference it is measured
against.

† ULTR pending classification: if the INTC transcript shows "ultra" was spoken,
it is a keyword→market semantics mismatch (phrase-design fix, excluded from the
threshold-FA set); if not spoken, it is a phonetic false fire (included).

Bars drafted 2026-07-23 and accepted by the user same day **as originally
drafted** (holdout bar then read "≥6/8 on the 8 swept words"). **AMENDED
2026-07-23 evening, BEFORE any bench run**, on settlement evidence: only 4 of
the 8 "swept" AAL words settled YES (WORL/CENT/IRAN/AT settled NO — release-
text basket bets, words never spoken; see COMPETITOR-ANATOMY AAL settlement
correction). The original denominator was factually wrong — a perfect detector
could score at most 4/8. Amended denominator = the settlement-YES spoken set
(5 words), bar ≥4/5. **Amendment ACKED by the user 2026-07-23 evening**
(offered stricter 5/5 and keep-original options; user chose the amended
≥4/5 bar). The holdout is unblocked once dev tuning converges; it still runs
exactly ONCE with the frozen class-policy config. Verdict lands here; gate
state mirrors to `docs/RACE-CHECKLIST.md`.

## Prefix-firing on the WEBCAST vocabulary (paced, 2026-07-24) — PARKED pending parity

Paced dev bench, sherpa_kws, chunk 160, INTC-Q2-2026 (0.98 h real-time, us-east
box), `configs/bakeoff-webcast-prefix.toml` (27 designed cuts, all accepted by
`compile-keywords`). Ran because the webcast vocabulary — 41 phrases of long
proper nouns and **multi-word compounds** — is structurally unlike the 5-word
earnings vocabulary the P3 diagnosis retired prefix-firing on.

| phrase | Fast p50 | Confirmed p50 | **lead** | Fast TP | Conf TP | Fast FA/h |
|---|---|---|---|---|---|---|
| client computing | 1489 ms | 2130 ms | **+641 ms** | 1 | 1 | 0 |
| panther lake | 676 ms | 1243 ms | **+567 ms** | 2 | 3 | 0 |
| security | 949 ms | 949 ms | **+0 ms** | 4 | 4 | **8.2** |
| foundry | — (**0 Fast fires**) | 1033 ms | — | 0 | **11** | 0 |

**Both P3 and the earlier estimate were right, about different word classes.**
Single words gain **nothing** (`security` +0 ms) — exactly P3's structural
result (§P3: safe cuts shave only the final BPE token). Multi-word phrases gain
**~0.6 s**, because the cut drops an entire *leading word* — a different
mechanism than P3 measured, and the first positive prefix result in the program.

**Verdict: PARKED, not promoted.** Three blockers: (a) n=3 paired words, on 1
and 2 true positives for the two large leads; (b) `foundry` — the most-spoken
keyword on the call — produced **0 Fast fires against 11 Confirmed**, so the
lever is absent exactly where recall is richest; (c) Fast-tier FA runs **~8/h**
(`europe`, `security`) against this doc's pre-registered G4 bars of ≤0.5/h dev
and ≤1/h holdout — over by ~16×.

Program reason for parking (operator directive 2026-07-24): prefix-firing is a
*beat*-the-competitor lever, and the measured gap is **not** decode — 81 % of it
is upstream of our audio (`LATENCY-PROOF.md` §THE GAP IS LOCATED). Reach parity
on the feed first; revisit this table after.

### Unpark attempt (2026-07-24 night) — two structural rules, −96 % FA, still FAILS the gate

Not tuning-by-memorization: both rules follow from the measured mechanism, which
is what the G4 gate asks for ("generalized into written per-class policy rules").

1. **Multi-word phrases only.** The Fast cut drops a whole *leading word*, which
   only exists for multi-word phrases; on a single word it shaves the final BPE
   token (P3's finding above). Attribution over the INTC bench: single-word
   phrases carried **25 of 29** Fast FAs and returned **+0 ms**. Prefixes removed
   from all 20 single-word phrases.
2. **Gate (b) at the word level.** Of the 7 surviving multi-word prefixes, two
   stopped at a word people say standalone — `venture`, `golden` — and those were
   the FA sources. Extended to `venture f` / `golden d`; `panther` kept (rare
   word). All 7 recompile with valid token-boundary cuts, zero rejects. The
   safety costs lead: `golden d` is a 5/6 cut where `golden` was 4/6.

**Result — dev split (INTC + LMT, 2.527 h, `sherpa_kws`):**

| tier | TP | FA | FA/h | G4 dev bar |
|---|---|---|---|---|
| Fast | 5 | 3 | **1.19/h** | ≤0.5/h — **FAIL** |
| Confirmed | 49 | 53 | 20.97/h | (untuned union config, see caveat) |

Residual Fast FAs: `panther lake`, `venture fund`, `venus aerospace` — one each.

**VERDICT: still PARKED.** The marginal FA cost of prefixes fell from 29.6/h to
**1.19/h (−96 %)** and both measured leads survive (`golden dome` now fires Fast
2/2 with 0 FA), but the pre-registered bar is a point estimate and 1.19/h misses
it by 2.4×. Per the gate as written, that is a fail — it is not re-negotiated
after seeing the number.

**Two honest caveats, in both directions:**
- *Against the fail:* 3 FA events over 2.527 h is thin. Against a true rate of
  0.5/h you would expect 1.26 events, and observing ≥3 has p ≈ 0.13 — so this is
  **not significantly above the bar**, merely above its point estimate. A longer
  corpus could land either side.
- *Against the pass:* only **5 Fast true positives** across the whole dev split,
  so the latency benefit is still resting on very few events.
- The absolute rates come from the untuned 41-phrase union bench config
  (Confirmed alone is 20.97/h), NOT an armed posture. The transferable number is
  the **marginal** +1.19/h that prefixes add, not the totals.

Next move if this is revisited: FA autopsy (`fa_autopsy` in `bench-report.json`)
on the three residual phrases, and more corpus hours to shrink the interval —
not threshold-fiddling on three events.

## Collision screen — detection quality is a KEYWORD-RISK problem (2026-07-24)

`scripts/collision-screen.py` answers the question a bench report cannot: not
*that* a keyword false-fires but **what the detector actually heard**. It joins
each `fa_autopsy` position (`src/bench/mod.rs`) to the word-level transcript
(`corpus/transcripts/<call>.words.json`) and classifies each fire.

Classification follows Q4 semantics (`VERIFICATIONS.md`): **plurals/possessives
ARE the same mention; substrings of closed compounds and verb forms are NOT.**
That distinction is the whole point — `tailwinds` is a true hit, `ultramaritime`
and `recorded` are not.

**Dev corpus taxonomy — 48 of 56 FAs are genuine, and they are NOT diffuse:**

| keyword | fires on | n | class |
|---|---|---|---|
| `trump` | "strong" | 8 | phonetic near-miss |
| `record` | "recorded" | 6 | **SUBSTRING** — verb form, not a mention |
| `weather` | "whether" | 4 | **TRUE HOMOPHONE — unwinnable** |
| `orion` | "invent**ory on**" | 2 | word-boundary artifact |
| `ultra` | "ultramaritime" | 2 | **SUBSTRING** — closed compound |
| `shutdown` | "shoot down" | 1 | phonetic near-miss |

Meanwhile the distinctive words are already near-perfect: `foundry` 11/0,
`government` 13/1, `morfius` 3/0, `panther lake` 5/1, `golden dome` 2/0.

**So "flawless detection" is not a global tuning target — it is a per-keyword
risk classification produced BEFORE arming.** Some collisions are
threshold-fixable; a true homophone is not, and a market on such a word is
simply not winnable on precision. Know which is which, then act only on the
words that are clean.

### Pre-arm screen of a real event's keywords (2026-07-28 candidates)

Ran the screen on the ACTUAL `earshot prepare` output for both Tuesday events,
against 2.527 h of unrelated calls (INTC + LMT) — so nearly every fire is a
collision by construction:

| event | total FA | worst offenders |
|---|---|---|
| **Ford** (`KXEARNINGSMENTIONF-26JUL28`) | **65** | `electric vehicle` 21, `ai` 17, `china` 7, `recall` 6 |
| **PayPal** (`KXEARNINGSMENTIONPYPL-26JUL28`) | **12** | `regulation` 5, `stripe` 4 (on "strike"/"outstrip"), `google` 2 |

**38 of Ford's 65 come from two-character variants**: `prepare` emitted
`ai → ["ai","ais","artificial intelligence"]` and
`electric vehicle → ["electric vehicle","ev","evs"]`. A 2-char keyword fires on
"ever", "than ever", even "2027". This is the generator bug (task #10) with
teeth — it also emits non-words (`autonomys`, `subsidys`, `recalleds`) which are
harmless, but the short variants are not.

**Consequences, decided before the event rather than after:**
- **PayPal is the materially safer arm** — its vocabulary is distinctive
  (bitcoin, blockchain, braintree, perplexity, biometric, stablecoin, agentic
  commerce all score zero FA).
- No event gets armed on an unscreened keyword set. Fold this into
  `scripts/preflight-event.sh`.

### Generator fix (task #10) — measured 65 → 29 FA

`prepare` now drops variants shorter than `MIN_VARIANT_CHARS` (3), **loudly** —
`tracing::warn` at generation plus a `# DROPPED ultra-short variant` comment in
the emitted config, so an operator who genuinely needs "EV" can re-add it and own
the precision cost. Silent dropping would have been its own bug.

Subtlety the regression test caught: a **generated plural must die with its
base**. `ev` is 2 chars and drops, but `prep_market`'s derived `evs` is 3 and
would have survived to fire on "ever" exactly as measured. Only plurals of a
dropped base are removed, so real 3-char market words (`ads`, `atc`) are
untouched. Tests: `ultra_short_variants_are_dropped_loudly_not_silently`,
`three_char_variants_are_kept` (`src/prepare.rs`).

Re-screened the regenerated Ford config on the same 2.527 h:

| keyword | before | after |
|---|---|---|
| `electric vehicle` (`ev`,`evs`) | 21 | **0** |
| `ai` (`ai`,`ais`) | 17 | **0** |
| `china` / `recall` / `apple` / `autonomy` | 21 | 21 (next tier) |
| **event total** | **65** | **29** |

**And the market was not lost, only made precise**: the phrase promoted to
`artificial intelligence`, which now fires **once, correctly**, on a real
utterance. Of the 29 remaining, **6 are GROUND-TRUTH** (`market share`,
`tailwinds`, `artificial intelligence`, `skunk works` — genuinely spoken, the
manifest missed them), so genuine collisions are **23**: 25.7/h → **9.1/h**.

Next tier for Ford if it is armed: `china` (7), `recall` (6), `apple` (4),
`autonomy` (4) — `autonomy` is the interesting one, since "auto" prefixes
"automotive"/"automatic", which are constant on a car maker's call.

### Ford 07-28 pre-arm screen (2026-07-28) — 1 genuine collision in 24 raw FAs, and the real risk is SEMANTIC

15 keywords over **5.15 h of NON-Ford audio** (AXP/INTC/LMT/VZ), armed engine,
chunk 160. **24 raw false alarms = 4.66/h** (PayPal: 15 = 2.91/h — Ford worse,
as its history predicted). The raw figure is again badly misleading.

Of the 14 the screen could explain (10 skipped — no AXP/VZ transcripts):

| keyword | FA | true verdict | what was spoken |
|---|---|---|---|
| `autonomy` | 7 | **6 × VARIANT-CORRECT**, 1 GROUND-TRUTH | "**autonomous** sea drone / weapon systems / manufacturing" |
| `market share` | 2 | GROUND-TRUTH | genuinely said (INTC, LMT) |
| `skunk works` | 2 | GROUND-TRUTH | genuinely said — on a **Lockheed** call |
| `recall` | 1 | GROUND-TRUTH ⚠ **SEMANTIC MISMATCH** | "as you **recall** Intel have a rich…" |
| `artificial intelligence` | 1 | GROUND-TRUTH | genuinely said (LMT) |
| `apple` | 1 | **PHONETIC** | "a little bit of an opportunity" |

⇒ **Exactly ONE genuine collision (`apple`) among 14 explainable fires.**

### ⚠ TOOL LIMITATION FOUND: `collision-screen.py` reads PHRASES, not VARIANTS
### → FIXED 2026-07-28 (same day): `--config` makes it variant-aware

`autonomy` carries `variants = ["autonomy", "autonomys", "autonomous"]`. Its six
"PHONETIC" verdicts are the detector **correctly matching a configured variant**
on calls where "autonomous" is genuinely spoken. The screen looked the *phrase*
up in the transcript, so a correct variant match was reported as a phonetic
false alarm. The reading rule ("read every `PHONETIC` verdict against the
keyword's `variants` list") is now a code fix: `collision-screen.py --config
<cfg>` classifies against variants and reports the new verdict class
**VARIANT-CORRECT** (self-test: `--check`). Re-run on this exact screen JSON:
autonomy's 7 fires → 6 VARIANT-CORRECT + 1 GROUND-TRUTH; `apple` remains the
sole genuine PHONETIC — matching the hand analysis above. Without `--config`
the old phrase-only behavior (and the reading rule) still applies.

### ⚠ THE REAL FORD RISK IS `recall`, AND NO COLLISION SCREEN CAN FLAG IT

`recall` fired on *"as you recall Intel have a rich…"* — the discourse verb, not
a product recall. The screen scores it **GROUND-TRUTH** because the word genuinely
was spoken, and it is right to. But the RECA market is about **vehicle recalls**,
and "as you recall" / "if you recall" is a filler phrase earnings executives use
constantly — Ford's included.

**This is a keyword→market SEMANTICS mismatch, the same class as the ULTR note**
(`ultra` a true mention on AAL, an unwinnable substring on LMT). It is invisible
to a false-alarm screen by construction: the detector is behaving perfectly and
the *market rule* is what disagrees. Expect RECA to fire early on filler and
resolve on the race rather than on a genuine recall mention. Posture is 1¢
non-crossing observe, so nothing is at risk but the detection log's readability.

**CLEAN (0 FA):** `china`, `electric vehicle`, `geely`, `headwind`, `iran`,
`oakville`, `tailwind`, `tariff`.

### ⚠ THE TOKEN-LENGTH RISK HEURISTIC HAS NOW MISPREDICTED TWICE

Before these screens I flagged, from the shortest-target rule alone, Ford's
`apple`/`china`/`iran`/`geely`/`subsidy` and PayPal's `stash`. Measured:

| predicted risky | actual |
|---|---|
| `stash` (2 tokens, "the board's highest risk") | **CLEAN, 0 fires in 5.15 h** |
| `china`, `iran`, `geely` | **CLEAN** |
| `apple`, `subsidy` | 1 FA each |
| **not predicted** | **`autonomy` 14** (variant behaviour), **`stripe` ← "strike"** (phonetic), **`recall`** (semantic) |

Short targets are cheap to *flag* and the flag carries almost no information.
The failures that actually occurred were phonetic near-misses no length rule
predicts, a variant firing correctly, and a semantic mismatch. **Screen; do not
guess from token counts.**

### ⛔ PREFIX-FIRING CANNOT FIRE IN OBSERVE POSTURE — a structural bound, found 2026-07-28

Asked to arm prefix-firing on near-certain-YES words for PayPal/Ford, and it
**cannot be done at all tomorrow** — for a reason that has nothing to do with the
FA bar, the token cuts, or the corpus. It is arithmetic in `src/decision.rs`.

    FAST_TRANCHE_PCT = 30                        (src/decision.rs:15)
    tranche = budget_contracts * 30 / 100        (u32 integer division)

Every keyword on every observe-posture board has **`budget_contracts = 1`** —
that IS the 1¢ non-crossing posture. So:

| policy | Fast hit at budget=1 | result |
|---|---|---|
| `confirmed_only` (what all boards use) | no `(ConfirmedOnly, Fast)` arm exists (`decision.rs:81`) — falls to `None` | **ignored outright** |
| `two_tier` | `1 * 30 / 100` = **0 contracts**, state → `FastFired`, Confirmed then still fires 1 | **strictly worse**: a 0-contract intent and a consumed one-shot state, for no gain |
| `fast_only` | fires the full budget on the PREFIX, never waiting for the full word | reckless — the prefix becomes the only evidence |

**The Fast tranche reaches 1 contract only at `budget_contracts ≥ 4.`** The
mechanism is designed for large budgets; `decision.rs`'s own test asserts
`fast.tranche_contracts == 60  // 30% of 200`.

⇒ **Prefix-firing and the 1¢ observe posture are mutually exclusive.** Making it
live requires raising budgets, i.e. real-size trading, which is **M4-gated**
(CLAUDE.md). This supersedes the token-cut analysis as the binding constraint:
even a perfect cut on a near-certain-YES word does nothing at budget 1.

⚠ **This also re-reads §D2/§D2b.** Those measured prefix *detection* on the
bench, where no decision table is involved — so their ~320 ms gain was never
wrong, it was simply never reachable through the armed path in observe posture.
Nothing in the ledger said so.

`configs/webcast-pypl-26jul28-arm.toml` keeps a **validated but INERT**
`fast_prefixes = ["e comm"]` on ECOM (cut `▁E ▁COM M`, 3 of 5 tokens,
compile-clean) as groundwork, with the arithmetic written beside it so nobody
reads it as a live capability.

### PayPal 07-28 pre-arm screen (2026-07-28) — `stash` is CLEAN, and 11 of 15 FAs are unexplainable

16 keywords over **5.15 h of NON-PayPal audio** (AXP/INTC/LMT/VZ), armed engine
`streaming_ctc`, chunk 160. **15 raw false alarms = 2.91/h** — and as with META
that raw figure must not be quoted bare.

| keyword | FA | verdict | actually spoken |
|---|---|---|---|
| `regulation` | 2 | **GROUND-TRUTH** | "federal acquisition **regulation**" (LMT) |
| `stripe` | 1 | **PHONETIC** | "conventional prompt **strike**" |
| `european payments council` | 1 | **PHONETIC** | — |
| `e commerce` | 5 | **UNEXPLAINED** | no AXP/VZ transcript |
| `agentic commerce` | 3 | **UNEXPLAINED** | " |
| `cash back` | 1 | **UNEXPLAINED** | " |

**Only 4 of 15 could be classified.** `collision-screen.py` skipped 11 because
**AXP and VZ have no transcripts** — the same gap §R1 hit (AXP's manifest carries
zero occurrences). Any screen run against those two calls can count fires but
cannot explain them.

**`stash` is CLEAN (0 FA).** The runbook has carried it as the board's
highest-risk target on the strength of its 2-token cut alone; measured, it did
not fire once in 5.15 h. The token-length heuristic flagged the wrong word here —
the actual phonetic collision was **`stripe` ← "strike"**, which no length rule
would have predicted.

Also clean: `ads id`, `anthropic`, `biometric`, `bitcoin`, `blockchain`,
`braintree`, `google`, `perplexity`, `stablecoin`.

### META 07-29 pre-arm screen (2026-07-28) — `sam` <- "SambaNova", and a raw FA/h that must NOT be quoted bare

18 keywords over **5.15 h of NON-Meta audio** (AXP/INTC/LMT/VZ), armed engine
`streaming_ctc`, chunk 160. Run BEFORE arming, as this section requires.

**Raw: 23 false alarms = 4.47/h. That number is misleading and should not be
quoted on its own.** Of the 11 the screen could explain:

| keyword | FA | verdict | what was actually spoken |
|---|---|---|---|
| `silicon` | 5 | **GROUND-TRUTH** | genuinely said — on an **Intel** call |
| `algorithm` | 2 | **GROUND-TRUTH** | "algorithms" (LMT) |
| `dividend` | 1 | **GROUND-TRUTH** | "dividends" (LMT) |
| **`sam`** | **2** | **PHONETIC** | **"samba nova"** ×2 |
| `cloud` | 1 | **PHONETIC** | "quad star seeker" |

**EIGHT of eleven are GROUND-TRUTH** — the word really was spoken and the *other*
call's manifest simply does not list it. Only **three** are genuine collisions.
12 further FAs are unmapped (AXP and VZ have no transcripts), so the true
collision count is bounded below by 3, not by 23.

⚠ **Methodological note worth keeping.** Screening a keyword set against other
issuers' calls silently conflates *"absent from this call's manifest"* with
*"false alarm"*. That is fine for issuer-SPECIFIC words (`llama`, `oakley`,
`nebius`) and **wrong for generic vocabulary** (`dividend`, `cloud`, `silicon`,
`algorithm`, `hiring`) — those are supposed to fire, because they are supposed
to be said. `collision-screen.py`'s GROUND-TRUTH verdict is what rescues the
method; the bare FA/h from `bench-detector` would have been read as a disaster.

**The actionable finding is `sam`.** It compiles to `▁SA M` — 2 tokens — and
fired on **SambaNova**, an AI-chip company, twice on one call. The predicted
*mechanism* (a short `▁SA M` target capturing a "Sam-" company name) held; the
predicted *words* ("Samsung", "sample", "same") did not. SambaNova is entirely
plausible on a Meta call discussing AI silicon, so this is a live risk rather
than a corpus artifact. Per the `stash` precedent: if it fires loose on the day,
**drop it rather than tune a threshold.**

⚠ **A prior claim of mine is RETRACTED here.** I flagged `orion` as an
already-proven collision, citing "orion <- inventory on" from
`collision-screen.py`'s own docstring (2026-07-24). **It did not reproduce** —
on this corpus with the armed engine, `orion` fired **zero** times. That earlier
finding came from a different engine/config and should not have been carried
forward as established.

**All 12 Meta-SPECIFIC words are CLEAN (0 FA):** advertising, age verification,
hyperion, llama, nebius, oakley, orion, prediction market, ray ban, tiktok,
united kingdom, virtual reality.

## Keyword arming blocklist — collisions that NO threshold can fix (2026-07-25)

From the collision screen on the webcast dev split (INTC+LMT, 2.53 h,
`sherpa_kws`, 56 false alarms across tiers = **22.16 FA/h**). These are
**keyword-risk** decisions taken before arming, not tuning knobs.

| keyword | FA | TP | collision | verdict |
|---|---|---|---|---|
| `record` | 14 | 5 | **"quarter over quarter"** (6 of its FAs) + "being recorded" (2) | **NEVER ARM.** "quarter over quarter" is the most common phrase on an earnings call and "this call is being recorded" is read aloud on every one. Precision 5/19 = **0.26**. |
| `weather` | 4 | 0 | **"whether"** — a true homophone | **NEVER ARM** on any call. No acoustic model separates these. |
| `ultra` | 3 | 0 | "ultramaritime" — substring of a closed compound | **NEVER ARM** unless the event's market text explicitly settles on substrings (Q4 says it does not). |
| `trump` | 8 | 0 | "strong" (8 of 8) | High risk; the phonetic distance is small and "strong" is ubiquitous on earnings calls. |

Removing `record` + `weather` + `ultra` alone: **56 → 35 FA, 22.16 → 13.85 FA/h
(−38 %)**.

⚠ **Two honest caveats on that headline.**

1. **This config arms 41 keywords spanning several events against two calls'
   audio.** Keywords belonging to other events cannot score true positives here,
   so a "0 TP" row does not mean the keyword is worthless in general — only that
   it was irrelevant to INTC/LMT. A real event arms one event's keywords, so
   **22.16 FA/h overstates single-event exposure.** Do not quote it as the armed
   figure.
2. **The structural collisions above are the exception** — they are properties of
   the *English*, not of this corpus. `record`/"quarter" and `weather`/"whether"
   will recur on every call where those keywords are armed, whatever the event.

**Consequence for the engine choice.** `sherpa_kws` exposes no per-hit
confidence (`VERIFICATIONS.md` Q-M1 pin 2: `Detection.confidence` is a constant
1.0), so there is no lever here except *drop the keyword*. `streaming_ctc` reads
raw CTC frame posteriors and produces real confidences, which is what a
negative-context rule ("suppress `record` when the frames also support
`quarter`") would need. That is a second, independent argument for the engine
swap — beyond its measured 38 % FA reduction.

## ⛔ RECALL GATE, 2026-07-29 — 12 of 52 market words are DEAD on clean studio TTS

Every screen above this line hunts **false** fires (collisions, prefix hits,
FA/h). `scripts/variant-study.sh` (new, born after WING lost CITR + AI to
flagged-but-unenforced gaps) asks the opposite question: **does the word fire at
all?** It synthesizes each configured variant with two macOS voices, splices
them 2.6 s apart, and replays through the config's OWN detector block.

Run against the four staged 07-29 configs. **A word scoring 0 on clean,
noise-free, studio-quality TTS will not fire on a live compressed webcast** —
this is a floor, not an estimate (LATENCY-PROOF D1g).

| event | DEAD (0 detections) | live-plausible? |
|---|---|---|
| SBUX | `matcha`, `protein`, `frappuccino`, `horchata` | **matcha + protein near-certain on a Starbucks call** |
| CMG  | `tariff`, `headwind`, `tailwind`, `autocado`, `chipotle kitchen`, `chipotle lane` | **tariff/headwind/tailwind are generic earnings language — near-certain** |
| META | `llama`, `ray ban` | **both near-certain on a Meta call** |
| MSFT | (not yet run) | — |

Detector-side facts, so nobody re-derives them:

- **It is not the harness.** Same runs, same configs, passing words emit
  normally (`orion` 4/4, `hyperion` 4/4, `inflation` 12/4, `advertising` 14/4).
  Failing words show the audio fully consumed and nothing emitted:
  `event DRYRUN-VARSTUDY-LLAM chunk 80ms frames 625 chunks 157 detections 0`.
- **It is not "multi-word phrases don't work."** `honey chicken`, `carne asada`,
  `al pastor`, `share purchase`, `supply chain`, `comparable sales` all pass
  while `ray ban` and `chipotle lane` fail.
- **It is not only rare brand words.** `tariff`, `protein`, `headwind` and
  `tailwind` are ordinary English and still score 0, so "out-of-vocabulary"
  does not explain the set.

⚠ **Do NOT paper over this by bolting on guessed pronunciation variants under
time pressure.** A false fire consumes the one-shot (`decision.rs:74`), so an
untested variant does not merely fail — it can forfeit the genuine mention it
was added to catch. Any added variant must be re-run through
`variant-study.sh` and land in this table with a number before it is trusted.
The 40 passing keywords are unaffected, and observe posture is 1¢, so a dead
word costs recall, never money.

**Reproduce:** `scripts/variant-study.sh configs/webcast-<slug>-26jul29-arm.toml`
(macOS only — uses `say`; exits 1 if ANY keyword scores zero).

### Autopsy of the 12 dead words (2026-07-29) — three buckets, three different fixes

The gate above says 12 words score zero. It could not say WHY, because
`ctc_scorer` computed each entry's confidence and then dropped it with a bare
`continue` — no log, no counter. `earshot detect-explain` (new) keeps that
number: per target it records the PEAK confidence reached *before* the threshold
gate, plus each BPE piece's best log-posterior. Pure observation — the firing
path does not read these fields, so behaviour is unchanged.

Run: `scripts/dead-word-autopsy.sh` over the variant-study TTS clips
(16 kHz studio, `say` Samantha+Daniel), armed CTC model, `default_threshold = 0.15`.

**CONTROLS FIRED — so the instrument is trustworthy.** A diagnostic that calls
everything broken has proven nothing, so the runner always includes known-good
words and voids the report if any fails:

| control | k | peak confidence |
|---|---|---|
| `inflation` | 4 | **0.975** |
| `advertising` | 6 | **0.952** |
| `loyalty` | 4 | **0.822** |
| `hyperion` | 5 | **0.712** |
| `orion` | 2 | **0.428** |

**The 12 dead words, bucketed:**

| bucket | word | k | peak | verdict |
|---|---|---|---|---|
| NEAR-MISS | `matcha` | 4 | **0.1489** | **short of 0.1500 by 0.0011** |
| NEAR-MISS | `tariff` | 4 | 0.1242 | short by 0.0258 |
| NEAR-MISS | `llama` | 4 | 0.1239 | short by 0.0261 |
| NEAR-MISS | `chipotle lane` | 7 | 0.0763 | short by 0.0737 |
| NO-EVIDENCE | `horchata` | 5 | 0.0013 | pieces `CH` (p=0.0003) + `AT` (p=0.0001) absent |
| NO-EVIDENCE | `chipotle kitchen` | 9 | 0.0549 | piece `OT` (p=0.0020) absent |
| WEAK | `ray ban` | 4 | 0.0579 | — |
| WEAK | `tailwind` | 6 | 0.0521 | — |
| WEAK | `frappuccino` | 8 | 0.0499 | — |
| WEAK | `autocado` | 6 | 0.0408 | — |
| WEAK | `headwind` | 4 | 0.0259 | — |
| WEAK | `protein` | 3 | 0.0145 | — |

**`matcha` is the headline.** Compiled `▁MA T CH A`, and the model hears the word
almost perfectly: `▁MA` p=0.998 @f161, `T` p=0.979 @f163, `CH` p=0.977 @f165 —
three consecutive frames, an unambiguous real path. It scores 0.1489 against a
0.1500 threshold and is therefore discarded. **A word near-certain to be spoken
on a Starbucks call was lost by 0.7% of a threshold that was never measured
against it.**

**The geometric mean is confirmed as the mechanism.** `chipotle kitchen` is the
clearest illustration: "kitchen" is essentially perfect (`▁K` 0.998, `IT` 0.998,
`CH` 0.9997, `EN` 0.991) while "chipotle" is not segmented the way we compile it
(`OT` p=0.0020, `LE` p=0.0055, `HI` p=0.026) — and one absent piece is enough to
sink all nine. `autocado` likewise: four strong pieces (0.77/0.89/0.83/0.97)
dragged to 0.041 by `UT` (0.11) and `C` (0.05).

⚠ **Two limits of this instrument, stated so nobody over-reads the table.**

1. **`piece_best_logp` is the best posterior ANYWHERE in the stream, not on the
   winning path.** It is therefore a per-piece UPPER BOUND, and the product of
   those bounds is an upper bound on confidence — not the confidence. `matcha`
   proves the gap matters: piece `A`'s best is p=0.270 at f17, which is *before*
   pieces 0-2 peak at f161-165, so no monotonic path can use it. A tiny
   best-anywhere value soundly proves a piece is absent everywhere (so
   NO-EVIDENCE is reliable); a healthy one does NOT prove the path is healthy.
   Tracking per-piece evidence along the argmax path is the obvious next
   increment.
2. **This is 16 kHz studio TTS, not 8 kHz telephony corpus audio.** In
   particular `tariff` peaking at 0.1242 here **does NOT overturn** the pinned
   finding at §"tariff threshold+boost sweep" that its corpus misses "produce NO
   detector hypothesis at all" and that config tuning cannot close its gap — that
   was measured on band-limited 8 kHz occurrences, a different channel. The two
   readings are about different inputs and are reconciled by a corpus run, not by
   assertion. `tariff`'s four pieces are all merely mediocre here
   (0.46/0.09/0.26/0.40) with none absent, which is consistent with the
   acoustic-ceiling reading rather than against it.

**No threshold has been changed on the strength of this table.** Lowering a
threshold is exactly the move that buys recall with false fires, and the
operator's standing bar for this work is *recall gains only if FA/h does not
worsen*. The NEAR-MISS bucket is the candidate, and the FA side has to be
measured per word before anything moves — with one methodological constraint
already pinned above: screening a keyword against other issuers' calls is valid
for ISSUER-SPECIFIC words (`matcha`, `llama`, `horchata`, `autocado`) and
**wrong for generic vocabulary** (`tariff`, `headwind`, `protein`), where absence
from a manifest does not mean absence from the audio.

**Reproduce:** `scripts/dead-word-autopsy.sh` (controls + buckets + per-piece
detail), or one word at a time:
`earshot detect-explain --config <cfg> --wav <clip> [--phrase matcha]`.

## ⛔ WNT RECALL GATE + AUTOPSY — 2026-07-30 (ABC World News Tonight, daily market)

Venue: `KXWORLDNEWSMENTION-26JUL29`, 16 strikes. Engine **`streaming_ctc`**
(`default_threshold = 0.15`) — the production engine, not `sherpa_kws`. This
matters: `prepare` emits `engine = "sherpa_kws"`, so the first pass at this
market benched an engine production no longer uses (19 of 30 arm configs are
`streaming_ctc`). The lane's daily job pinned the production engine after this
(WNT lane removed 2026-08-12; the pattern lives on in `scripts/slate-stage.sh`).

Two tooling fixes were required before the gate could run at all, both recorded
because each silently disabled a gate that existed:

- `scripts/variant-study.sh` extracted the `[detector]` block with
  `sed -n '/^\[detector\]/,/^\[arbiter\]/p'`. **`earshot prepare` output has no
  `[arbiter]` section**, so the range ran to EOF and the generated mini-config
  inherited `[[sources]]`/`[kalshi]`/`[[keywords]]` — invalid under
  `deny_unknown_fields`. The readiness gate therefore could not be run on a
  freshly prepared config, i.e. exactly when it is needed. Now an `awk` range
  that stops at the first non-`[detector...]` top-level section.
- `scripts/dead-word-autopsy.sh` hardcoded the 12 dead words and 5 controls from
  the 07-29 sbux/cmg/meta study, and assumed the per-slug
  `$STUDY/vs-<slug>/…` layout. Added `--auto`: derive dead words from the study
  dir's own `report.tsv` FAIL rows and **controls from that same run's
  top-5 OK rows by hit count** — a stronger control than a remembered list,
  since it is measured on the same clips by the same binary. Also supports the
  flat single-config layout.

### Readiness gate — 4 of 14 words DEAD on clean studio TTS

`ice`, `iraq`, `midterm`, `tariff` scored 0/4. (`tariff` was already dead in the
CMG study — same word, second venue, still unfixed.) Over-firing is the other
tail: `maintain` 19/8, `oil` 17/10, `circular` 12/4, `blanche` 11/4.

### Autopsy — all 5 controls FIRED, so the verdicts below are readable

| word | verdict | measured evidence | the fix, and only this fix |
|---|---|---|---|
| `iraq` | **NO-EVIDENCE** | piece `Q` best logP **−10.46** (p≈0.0000), `▁I` −6.15 — neither is emitted | **Unreachable at any threshold** (confidence is a geometric mean over k pieces). Needs a respelling whose pieces the model demonstrably emits, then re-gated. |
| `tariff` | NEAR-MISS | peak 0.1242 vs 0.150 — **short by 0.0258**; every piece emitted (0.09–0.46) | per-keyword `confirmed_threshold`, from the measured margin |
| `midterm` | WEAK | peak 0.0552; `TER` −3.72, `M` −2.92. Variant `midterms` reaches 0.0867 | threshold, but the margin is 0.063 — marginal |
| `ice` | NEAR-MISS | k=2, `CE` p=0.4352 → peak 0.1090 | ⚠ **do NOT lower this threshold.** `ice` produced 2 detections on 2026-07-29 on a strike that settled NO. A 2-piece word with a weak second piece is inherently ambiguous; dropping its threshold buys recall by buying false fires. Treat as not armable as spelled. |

### The finding that is NOT a vocabulary problem

`blanche` fires **strongly** on clean TTS — peak 0.836 and 0.949, every piece
`▁B LA N CH E` at p≈0.99 — and got **zero detections across all three legs** of
the live broadcast. So this is not reachability and not the operating point: it
is live audio. The likely cause is pronunciation (the broadcast says the
surname *Todd Blanche*; `say` reads the English word), which is the same class
as WING losing `CITR` to a Spanish pronunciation. It is also a **recurring**
word — 3 of 10 sampled days — so it is worth fixing rather than dropping.

### Standing consequence for this venue

Recall and precision, not latency, bound this market. On 2026-07-29 the latency
race was won by **+12.9 s** on the one pairable word while 4 of 6 tradeable
said-words went undetected, and 352¢ of board edge yielded 5¢ — the only strike
we'd have taken was already priced at 94¢. A threshold change must be validated
against **false alarms on real audio**, not just recall on TTS: the banked
2026-07-29 capture is a natural precision set, because 8 strikes settled NO and
any in-window detection of those words is a measured false alarm.

## D1 — `online_asr` (sherpa OnlineRecognizer + complete-word matcher) vs the armed `streaming_ctc` — PRE-REGISTERED 2026-08-18 23:15Z, before any corpus run

Why: the 26AUG18 scored baseline (`state/_day/DETECTION-SCORE-26AUG18.md`,
7 legs / 8.8 h, large-v3-turbo reference) put utterance precision at 79.5 %
(92 % ex-`a i`), recall at 68 %, and showed every false fire to be a
morphological/phonetic neighbour (`innovative`, `inflationary`, `storage`,
`Arc'teryx`) at the SAME confidence as the true fires — the piece-level scorer
cannot separate them; a word-level matcher on a real ASR rejects them by
construction. `online_asr` (`docs/nodes/detector-online-asr.md`) is that
matcher on any sherpa streaming model, with hotword biasing on transducers.

Corpus: wideband dev split = AXP / INTC / LMT / VZ + `dwnews-negctl` (FA
control), 41-phrase webcast keyword set (`configs/bakeoff-online-asr.toml`,
identical keywords/thresholds to `bakeoff-webcast.toml`); holdout = AAL, run
ONCE with the frozen config. Chunk 160 ms, lossless (speed=0). Cells:

- C0 `streaming_ctc` ctc-small-2024-03-18 int8 t=0.15 — the ARMED operating point (incumbent)
- C1 `online_asr` zipformer-en-2023-06-26 int8, modified_beam_search, hotwords ON (score 1.5)
- C2 `online_asr` same model, hotwords OFF (greedy) — isolates the biasing term
- (C3+ NeMo cache-aware 80/480 ms and other candidates as they land; same protocol)

Bars (Confirmed tier, per cell vs C0 in the same run or an audio-domain-only
comparison, which the interleaving rule permits for accuracy columns):

| bar | rule |
|---|---|
| A. false alarms | FA/h ≤ **50 %** of C0's, excluding `panther lake` (degenerate at C0's threshold; reported separately) |
| B. recall | ≥ C0 recall − **0.02** on the same manifests (same denominators) |
| C. latency | `consumed_lag` p50 and p95 ≤ C0 + **100 ms** (a null is expected; the transducer's decode floor is the same ~320 ms quantum + right context) |
| D. hotwords | C1 vs C2: no FA/h above bar A while recall on the proper-noun/brand phrases rises |
| E. box paced | on the event box, `chunk→detect` p95 < chunk_ms and RSS growth < 50 MB/h over a 90-min replay (a separate `--paced` run) |

Promotion = A ∧ B ∧ C on dev, then E, then the holdout once. A miss on any bar is
reported as such; the pre-declared consequence is the next candidate model or the
verifier (D2), not a bar adjustment. All cells are reported, including losers.

### D1 RESULT — run 2026-08-19 00:12–00:45Z on the us-east-1 box (`~/ear-bench`, release build of the D1 tree), dev split 4 event calls (AXP/INTC/LMT/VZ, 5.15 h), lossless, chunk 160

**⚠ Correction to the keyword set, found by this run.** `bakeoff-webcast.toml`'s
`panther lake` entry carried variants `["panther lake", "a", "as"]` — the word
"a" as a variant — so every engine "fired" on every "a" (~every 8 s in every
call, min gap = the 2 s refractory). That is the "degenerate at threshold /
2,354 FA" panther-lake reading of the P1 table above, and it was never the
phrase. `europe` carried `["europe","europes","european","europeans"]`;
`european` does not settle a `europe` market under Q4 semantics and the manifests
do not count it. `configs/bakeoff-online-asr*.toml` carry the corrected entries;
`bakeoff-webcast.toml` / `-ctc16.toml` (evidence.benchmark) still carry the old
ones and any FA/h quoted from them for `panther lake` or `europe` is inflated.
With the corrected entries `panther lake` is **2/20** on every engine (P1's
"16/4 TP" were `a`-fires landing inside match windows).

Cells (Confirmed tier, all 41 phrases, same corrected keyword file):

| cell | engine / knobs | TP | miss | FA | recall | FA/h | consumed_lag p50 / p95 |
|---|---|---|---|---|---|---|---|
| **C0** | `streaming_ctc` ctc-small int8 t=0.15 (ARMED) | 50 | 27 | 73 | **0.649** | **14.18** | **570 / 714 ms** |
| C1 | `online_asr` zipformer-en-2023-06-26 int8, mbs beam 4, hotwords 1.5 | 43 | 34 | 42 | 0.558 | 8.16 | 954 / 1218 ms |
| C2 | C1 with greedy, hotwords OFF | 33 | 44 | 27 | 0.429 | 5.25 | 967 / 1137 ms |
| C3 | C1 with hotwords 3.0, beam 8 | 44 | 33 | 80 | 0.571 | 15.54 | 936 / 1184 ms |

Per phrase (TP/miss/FA), the ones that moved: `government` C0 13/0/11 → C1
11/2/2; `record` 5/0/15 → 4/1/4; `security` 7/0/7 → 7/0/1; `dividend` 1/0/7 →
0/1/3; `sikorsky` 2/0/0 → **0/2/0**; `golden dome` 2/0/0 → 1/1/1; `foundry`
13/6/2 → 14/5/2; `morfius` 0/3/0 on every engine; `europe` FA 14 → 18 (the
hotword bias pulls "European" toward "EUROPE" — C2 without hotwords has 9).

**Verdict against the pre-registered bars: C1 FAILS.** A: FA/h −42 % (needs
−50 %). B: recall −9.1 pts (needs ≥ −2). C: `consumed_lag` +384 ms p50 (needs
≤ +100). D: hotwords are worth **+13 pts recall for +2.9 FA/h** (C2→C1);
pushing the score to 3.0 (C3) buys +1.3 pts for +7.4 FA/h — the knob is spent
at ~1.5. The off-the-shelf LibriSpeech transducer, driven as a text matcher,
does NOT beat the armed CTC spotter on this corpus: it is cleaner on the generic
words that false-fire (`government`, `record`, `security`) but loses proper
nouns (`sikorsky`, `golden dome`, `dividend`) — the same out-of-domain wall,
one model family over. Do not promote.

**What survives:** the engine + harness (`--engines online_asr --model-dir a,b`
now sweeps any sherpa online model in ~15 min per cell on the box), and the
direction: text-level matching removes the morphological false fires. The
pre-declared next rungs are (1) a streaming model trained on conversational
English — NeMo cache-aware FastConformer 80/480 ms (no hotwords) — through the
same engine, and (2) the fill-path verifier (D2), which attacks precision
without touching recall. **The `dwnews-negctl` (8 h zero-keyword) cells were
still running at the time of writing; the FA-control column lands with them.**

## D2 — fill-path verifier, V0 retrospective — 2026-08-19 00:36Z

`earshot verify-report` re-decoded the last 3.5 s before every Confirmed
detection of two banked 26AUG18 runs with
`sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8` (box, 4 threads) and
`scripts/verify-join.py` joined the verdicts to `detection-score.py`'s TP/FP
labels (large-v3-turbo reference):

| run | detections | FP rejected | FP confirmed (missed veto) | TP rejected (false veto) | decode p50 / p95 |
|---|---|---|---|---|---|
| AS (box, 2 legs) | 44 | **2/2** (`A terrorist`→tariff — the 96¢ FILL, both legs) | 0 | 4 (Wimbledon→"Winburton", Foot Locker→"Foot Walker", ×2 legs) | 365 / 397 ms |
| HD-box (2 legs) | 80 | **6/6** (`innovative`, `inflationary`×2, `storage`×2, `in-store`) | 0 | 2 empty decodes (now `Error`, not a veto) + 1 dup | 298 / 361 ms |

Read: every false fire that a text-level second look could catch, it caught;
the cost is proper nouns the offline model mishears — which is why gating is
per-ticker opt-in (`[verifier].tickers`) and starts with generic fill words.
Bars for promotion stay as pre-registered (V1 dev, V2 ≥3 shadow events, V3 one
gated event); the hold budget (500 ms) holds on the box at p95 < 400 ms.

**D1 negctl column (landed 01:10Z):** `dwnews-negctl` 8.00 h — C0 159 FA
(19.9/h), C1 119 (14.9/h), C2 92 (11.5/h). Read with care: DW News genuinely
SAYS europe/government/security/iran/trump, so for the generic words this
"control" counts true utterances as FA by construction; it is a control only
for the issuer-specific phrases (all engines: ≤ 3 FA in 8 h on those). The
cross-cell ordering (C0 > C1 > C2) matches the event calls.

**D2 addendum (01:20Z) — right-context pad.** On HD-laptop the verifier
initially vetoed a TRUE `weather` (conf 0.83): the spotter fires ~200 ms after
the word ends, so the word sat at the very end of the tail and Parakeet-TDT
never flushed it ("…given the softer start."). Appending 400 ms of silence to
the decode window (`TAIL_PAD_MS`, zero wall-clock cost) fixes it: HD-laptop
re-run = FP rejected 3/3, TP rejected 0/27 (one duplicate variant fire
rejected, never an intent). Three runs pooled (AS, HD-box, HD-laptop, 167
detections): **false fires vetoed 11/11, true intents falsely vetoed 0, true
non-intent detections falsely vetoed 4 (all proper nouns) + 2 empty decodes
(now `Error`).**
