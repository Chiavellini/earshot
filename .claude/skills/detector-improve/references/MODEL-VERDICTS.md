# Model verdicts — answered questions; do not re-run these

Every entry is a measured, dated verdict. Re-running one wastes an event cycle;
overturning one requires NEW evidence at least as strong as what closed it.

## P1 — chunk-8 export (160 ms quantum): FAIL, closed 2026-08-06

Interleaved, lossless, dev split, 5.15 h, 41 keywords, on the box: chunk-16
(320 ms) vs chunk-8 (160 ms). Excluding the pathological `panther lake`:
recall **0.842 → 0.789**, TP/miss 48/9 → 45/12, FA/h 15.0 → 14.4; `sikorsky`
2/2 → 0/2 lost entirely. Gate was "−160 ms at unchanged recall/FA"; result was
−101 ms WITH a 5.3-point recall drop. **Do not promote. Quantum reduction on
this checkpoint is closed** — chunk-8 is below the trained 16/32/64 grid and
the regression is exactly where that predicts. The open path is a model
TRAINED for a small quantum (G2 shortlist: NVIDIA Parakeet-Realtime-EOU-120M,
80 ms — note it is RNN-T, so `ctc_scorer` does not apply; a new decode path
would be required).

Also: the label "chunk-16 = 160 ms" was wrong in four docs — the armed model
already IS the chunk-16 export and chunk-16 is **320 ms**
(`decode_chunk_len=32` × 10 ms frames; icefall doubles the chunk size).

## P3 — prefix-firing: ~32 ms, not 250–400 ms; closed

On this model the safe prefixes only shave the FINAL BPE token (`tariff`
`▁T AR I` vs `▁T AR I FF`; `recession` drops only `ION`), and `guidance` /
`partnership` have **no safe prefix at all**. Prefix-firing is correctly built
and safe — it cannot be the overtake move for these keywords.

## The decomposition that frames all model work

For a full-word detector, `onset→emit ≈ word_duration + decode_lag`, and
word_duration is common to us and the competitor — it **cancels**. The entire
competitive question is whether our decode-lag beats their ~200–400 ms.
Current sherpa/CTC decode-lag ≈ 450 ms (320 ms quantum + ~130 ms
right-context). Measured on TLN 26AUG05: 371 of 534 ms was IN-PROCESS
(`model_lag` 360 ms) — never budget latency from `edges{}` (understates ~4×).

## fp32 vs int8 (sherpa_kws): fp32 recovers recall at zero precision cost

Measured in the miss-autopsy. `[detector.sherpa_kws] fp32 = true` is the knob.
For streaming_ctc, `precision = "fp32"` requires a non-int8 export on disk —
the detector refuses to fake it.

## G2b — Parakeet-Realtime-EOU-120M: marginal, methodology partially withdrawn

RTF < 1 on laptop but the measured gain did not survive scrutiny. Treat as
unproven, not promising.

## Sherpa confidence is a constant 1.0 sentinel

The KWS C API hides scores; `streaming_ctc` reports a real posterior
(geometric-mean per-token along the best CTC path, `default_threshold` 0.5).
Never pool the two engines' confidences — `PipelineReport.engine` exists so a
banked report can tell them apart.

## Ground-truth correction tag: ⚠ OPTIMISTIC-DENOMINATOR

The 2026-07-24 corpus correction (LMT 11→31 occurrences) invalidates any
pre-correction number of the form TP/occurrences — and the direction was
itself corrected on 07-27: pre-correction LMT recall was PESSIMISTIC (0.727 vs
corrected 0.903). Unaffected: FA counts, FA/h, emit_lag, latency percentiles,
raw TP counts. Check the tag before quoting any historical recall.

## TTS studies prove the path is WIRED, not that live recall is 100%

`variant-study.sh` synthesizes 3 voices × carriers through the real detector.
`ratio = confirmed/clips` is a wiring/readiness signal: it gates RECALL only,
has no upper bound by design (ratio > 1 = multi-variant multi-fire), and is
NOT precision (GRAB: a high ratio flagged `commission` but could not separate
`robot`/`game`, both live false positives).

## Venue behavior that bounds what detection can earn

Podcast/pack venues don't react live to utterances (TBPN batches near
show-end; Rogan runs on priors + description text). The real predictor of
exploitability is rare-discrete-event vs predictable-word, not venue format.
A better detector does not create a race where the pack never runs one.

## Pre-registered bars (quote, never move)

M1 holdout: confirmed recall ≥ 0.90; confirmed FA ≤ 1/h; fast p95 onset→emit
≤ 350 ms (paced); fast FA ≤ 4/h. G4 webcast fill-tier: dev FA ≤ 0.5/h, holdout
≤ 1/h, recall ≥ untuned baseline, plus written per-class policy rules —
scored per channel, never blended (`scripts/prefix-gate-score.py`). Holdout
runs ONCE with a frozen config; all grid cells reported; a missed bar's
consequence was pre-declared (build the next thing, not adjust the bar).


## D1 — `online_asr` zipformer-en-2023-06-26 (2026-08-19): FAIL, closed at this model

Off-the-shelf LibriSpeech streaming transducer via sherpa `OnlineRecognizer`
+ complete-word matcher + hotwords 1.5, vs the armed `streaming_ctc` on the
corrected 41-phrase dev set (5.15 h): recall 0.558 vs 0.649, FA/h 8.16 vs
14.18, consumed_lag +384 ms. Hotwords are worth +13 pts recall for +2.9 FA/h;
score 3.0 / beam 8 buys +1.3 pts for +7.4 FA/h — spent at 1.5. Cleaner on
generic false-firers (`government`, `record`, `security`), loses proper nouns
(`sikorsky` 0/2). Do not arm this model. The ENGINE stays: any sherpa online
model benches in ~15 min/cell on the box. Next rungs: NeMo cache-aware
FastConformer 80/480 ms; the fill-path verifier (D2).

## Correction (2026-08-19): the "degenerate `panther lake`" was the variant `"a"`

`bakeoff-webcast.toml` / `-ctc16.toml` list `panther lake` variants
`["panther lake","a","as"]`; every engine fired on every "a" (~every 8 s, in
every call). P1's "2,354 FA / 16 TP" for the phrase were `a`-fires; the real
phrase is 2/20 on every engine. `europe`'s `european` variant likewise inflates
its FA. Any FA/h quoted from those two files for those phrases is wrong;
`configs/bakeoff-online-asr*.toml` carry the corrected entries.

## D2 — verifier V0 retrospective (2026-08-19): 8/8 false fires vetoed, 0 missed

Parakeet-TDT-0.6b-v2 int8 on the last 3.5 s of AS + HD-box 26AUG18 (124
detections): rejected `A terrorist`→tariff (the AS fill), `innovative`,
`inflationary`×2, `storage`/`in-store`×3; confirmed all 83 true fires except
proper nouns it misheard (`Winburton`, `Foot Walker`) and 2 empty decodes (now
`Error`, not a veto). Decode p50 ~300–365 ms, p95 < 400 ms on the box (4
threads). Gate per-ticker opt-in, generic words first.
