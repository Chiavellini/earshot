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
  telephony/         # processed 8 kHz mono s16 WAV — GITIGNORED
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
