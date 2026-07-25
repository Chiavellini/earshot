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
```

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
| Dev: recall on the settlement-YES caught set (GOLD, MORF, EURO, NASA, CLI, FOUN, PANT, SECU) | ≥ untuned baseline (no trade of recall for precision on words we already catch) |
| Holdout (AAL): recall on the settlement-YES spoken words (ATH, BASI, CHIC, RECO, WEAT) | ≥ 4/5 |
| Holdout (AAL): FA rate | ≤ 1 / h |

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
