# EARSHOT — Telephony Keyword-Detection Layer — MASTER SPEC (for expert review)

> Self-contained. An expert should be able to critique the design, accuracy, and
> latency from this document alone. All facts are from the live codebase
> (2026-07-14). Numbers are measured unless marked *estimated*.

---

## 0. What we want from the reviewer
Given the trading constraints (§1) and the measured reality (§5–6), tell us:
(a) the highest-leverage way to raise **single-utterance recall on band-limited
telephony**, (b) whether sub-~1 s **onset→detection latency** is reachable, and
(c) where our current conclusions (§7) are wrong. Be specific and ranked.

---

## 1. Trading context (why detection exists) — READ THIS FIRST, it drives everything

- **Product:** Kalshi "mention" markets. A market resolves **Yes** if a specific
  word is spoken by a company rep during a scheduled earnings call. Each event has
  ~12–14 such markets (a small per-event **dictionary**: tariff, recession, oil,
  buyback, guidance, "middle east", etc.).
- **The edge (why speed matters):** the market **closes/expires early the instant
  the word is uttered**; resolution is done by **Kalshi humans watching the call
  video** (they close the strike seconds→tens-of-seconds after the utterance). Our
  ingress is a **PSTN dial-in**, which runs **~10–30 s AHEAD of the public
  webcast**. The trade is: hear the word on the PSTN feed, detect it, and get a
  marketable order filled **before the human resolvers close the strike**. So both
  **recall** (did we catch it) and **latency** (did we beat the humans) are the
  product.
- **The distribution we actually trade (the crux):** we trade the words the market
  prices **low** — e.g. Morgan Stanley "tariff" implied **~14%**, United "tariff"
  **~19%** to be said **even once**. So on a tradeable market the word is said
  **0 or 1 times**, essentially never many. **Therefore per-occurrence recall ≈
  per-market catch probability.** (Earlier we reassured ourselves with corpus
  calls where "tariff" was said 7–39×, giving ~97% catch — that was **survivorship
  bias**: those were calls where tariff was the theme. The tradeable edge lives in
  the low-probability tail, where a word is said once and we get exactly one shot.)
- **Cost asymmetry / money:** orders are real (bounded). A **miss = lost edge**
  (the whole point for a rare word); a **false alarm = a real losing contract**
  (buys a Yes that resolves No). Current mitigation (§7) caps loss but a miss on a
  said-once word is a total loss of that market's edge.

## 2. End-to-end signal chain (exact)

```
PSTN call ─Twilio Media Streams─▶ WSS (we are the server)
   │ μ-law 8kHz 20ms frames
   ▼
[ingress] decode μ-law→i16, stamp capture_ts, validate X-Twilio-Signature
   ▼  AudioFrame { pcm: Arc<[i16]>, 8kHz, capture_ts, seq }   (ring, drop-on-full)
[preproc] 8→16kHz half-band FIR (16-tap), stamp preproc_ts   (backpressure, no drop)
   ▼
[chunker] accumulate frames → AudioChunk of chunk_ms (prod 80ms = 1280 samples@16k)
   ▼
[detector] sherpa_kws (16kHz i16→f32/32768) → Detection{keyword,tier,conf=1.0,pos_ms}
   ▼
[decision] one-shot Armed→Done state machine → DecisionIntent → executor
```
Threading: 3 dedicated OS threads (preproc, chunker, detect+decision) + source on a
current-thread tokio runtime; bounded SPSC rings (frame cap 256, chunk cap 64).
**Ingress is the only drop point** (ring-full drops a frame rather than block
capture); interior stages block (backpressure). PCM is **i16 end-to-end**.

### 2.1 Ingress (`src/source/twilio_ws.rs`)
- We are the **WS server**; Twilio dials in and connects. Audio is **G.711 μ-law,
  8 kHz, mono**, nominally **160 bytes = 20 ms per `media` message**. `mediaFormat`
  is validated fail-loud (rejects anything ≠ audio/x-mulaw/8000/1).
- μ-law→i16 via a 256-entry ITU-T G.711 LUT (max magnitude ±32124). Each payload →
  one `AudioFrame{ pcm: Arc<[i16]>, sample_rate_hz: 8000, capture_ts, seq }`.
- `capture_ts` is stamped at WS message arrival → **includes WS transit, NOT the
  PSTN leg**. `X-Twilio-Signature` = base64(HMAC-SHA1(authToken, url+sortedparams)),
  constant-time compared, checked in the handshake (403 on mismatch). No token ⇒
  validation disabled with a loud warn (dev/loopback only).

### 2.2 Preproc (`src/pipeline/preproc.rs`)
- **Only** operation: 8→16 kHz upsample via a **classical polyphase half-band FIR
  2× interpolator**, 16 taps, Hamming-windowed sinc, normalized to unity DC gain,
  group delay 1 ms. Even outputs = delayed input, odd = filtered midpoint. History
  carries across frame seams (continuous).
- This restores **sample rate, not bandwidth** — it adds no energy above the
  telephony band. **There is NO AGC / VAD / denoise / loudness-normalization /
  DC-offset removal anywhere in the live audio path** (grep-confirmed; the only hit
  is a comment: "the seam where G.711 decode and VAD land"). This is deliberate —
  a naive AGC was tested and reverted (§6).

### 2.3 Pipeline latency budget (`tests/perf_budget.rs`)
- `capture_ts → decision ≈ one chunk_ms + ~10 ms` (budget asserts capture→decision
  p95 < 170 ms at 160 ms chunk; ≈ 90 ms at the prod 80 ms chunk). Resampler runs
  >100× realtime; the latency recorder < 100 ns/op. **The pipeline is not the
  latency bottleneck — the model is (§5 lookahead).** NB: this ~90 ms **excludes the
  model's algorithmic emission delay** — the perf test drives a synthetic/stub
  detector; the real model adds §5's ~720–820 ms on this same interval (⚠ §10.3).

## 3. The acoustic domain (what the model actually sees)
- **Band-limit:** the corpus/telephony path (and the real PSTN leg) band-limits to
  **300–3400 Hz** and applies a **G.711 μ-law companding round-trip** (8-bit,
  ~fixed quantization noise), then 8→16 kHz upsample. So the model sees
  **narrowband, companded, 8 kHz-native** audio.
- **The mismatch:** the detector (§4) is a **gigaspeech model trained on WIDEBAND
  audio** with real energy to 8 kHz. Fricative/stop cues that disambiguate short
  words (the /t/ burst and /f/ frication in "tariff") live **above 3.4 kHz** and
  are simply absent. Hypothesis (from an earlier expert pass): this domain mismatch
  — not level, not pure "temporal resolution" — preferentially kills **short
  fricative words**, and is the through-line behind §6's findings.

## 4. Detector (`src/detector/sherpa_kws.rs`, `kws_compile.rs`)
- **Model:** `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01` — a **streaming
  Zipformer KWS transducer, 3.3M params, int8** (int8 encoder+joiner, fp32
  decoder). SentencePiece-unigram tokenization; keywords compiled by unigram
  Viterbi segmentation.
- **Streaming export = chunk-16-left-64 ⇒ ~320 ms algorithmic lookahead + ~0.4–0.5 s
  decode lag.** This is a **hard model floor** on latency, independent of chunk_ms.
- **No per-hit confidence:** the sherpa C API exposes no score, so every
  `Detection` carries a **hard-coded `confidence = 1.0`**. All gating happens inside
  sherpa via a **per-keyword threshold** written into the keywords list; the only
  acoustic knobs are `#threshold`, `:boost`, `max_active_paths` (beam width), and a
  `refractory_ms` (2000) dedupe window.
- **Keyword line format** (`render_keywords`): `TOK TOK … :BOOST #THRESHOLD @LABEL`
  (e.g. `▁TA RI FF :3 #0.10 @kw_conf`). Detections mapped back to keyword/tier by
  label.
- **Two tiers via two full spotters:** `Confirmed` (full word) and optional `Fast`
  (a **token-cut prefix** of the word, e.g. `▁TA RI` for "tari", fires earlier). A
  prefix must be a strict token-boundary prefix; several words have **no usable
  boundary** (guidance: no boundary before ANCE; partnership: "partner" collides).
- **Config defaults:** threshold 0.25, boost 2.0, beam 4, refractory 2000 ms,
  1 thread. **Live frozen (`twilio-live.toml`):** chunk 80 ms, resample 16 kHz,
  `confirmed_only` posture (no Fast — its latency isn't competitive); tariff
  threshold 0.10 / boost 3.0, recession 0.28, inflation at defaults.
- **Decision layer:** one-shot per-keyword `Armed→Done`; a `min_confidence` gate
  exists but is **dead for sherpa** (confidence is always 1.0). Tier→tranche split
  exists but the live posture releases the full (tiny) budget on the Confirmed hit.

### 4.1 Alternative engine (built, DARK): `streaming_ctc`
A custom low-lookahead spotter that drives a streaming-CTC ONNX **directly** and
reads **raw per-frame CTC posteriors**, giving **(1) real per-hit confidence**
(geometric-mean path posterior, enabling calibration + the dead gate) and **(2)
latency set by the model's lookahead (~0–80 ms), not the KWS export's ~320 ms**.
It is model-agnostic and unit-tested but **DARK**: no **English × streaming(causal)
× CTC-head** zipformer checkpoint exists on the public shelves (only Chinese
streaming-CTC, or English non-causal CTC, or English streaming transducers with no
CTC head). Unblocking it is a training/export task (finetune a CTC head onto a
causal transducer = GPU-hours), not a code task.

## 5. Latency stack (measured + *estimated*)
1. spoken word → `capture_ts`: **~300–600 ms** *estimated* (PSTN 200–400 + Twilio
   relay + WS 40–100), unmeasured on our path.
2. `capture_ts` → decision: **≈ chunk_ms (80 ms) + ~10 ms** (budgeted, measured).
3. **model floor:** ~320 ms lookahead + ~0.4–0.5 s decode.
4. **Measured onset→emit (real-time replay, chunk 80):** tariff **Confirmed p50
   961 ms / p95 1136 ms**; Fast p50 926 / **p95 1066 ms**. Bar was ≤350 ms (Fast) —
   **fails decisively**.
- Net utterance→fill ≈ **~1.2–1.7 s** *estimated*. The **10–30 s webcast lag** is
  the structural cushion vs human resolvers — but they watch the same PSTN-speed
  video, so the real race is tight.

## 6. Measured accuracy + findings
**Corpus:** 6 public earnings calls (~5.5 h), telephony-degraded (300–3400 Hz +
μ-law), whisper-forced-alignment ground truth; dev = AAPL/MSFT/QCOM, holdout =
WMT/CAT/GIS. Occurrences: **tariff 55, guidance 42, acquisition 5, partnership 4,
recession 0** (recession is a pure false-alarm control — nobody says it).
**Inflation and the other ~10 live dictionary words have ZERO corpus samples.**

**Holdout accuracy (unseen companies, Confirmed tier):**

| word | recall | precision | FA/h | vs bar (recall ≥0.90, FA ≤1/h) |
|------|--------|-----------|------|--------------------------------|
| acquisition | 1.00 (2/2) | 1.00 | 0.00 | ✓ (n tiny) |
| guidance | 0.96 (22/23) | 0.79 | 2.24 | ✗ FA |
| tariff | 0.89 (41/46) | 0.98 | 0.37 | ✗ recall by ONE occ |
| recession (FA ctrl) | — | — | 0.37 | ✓ |

Aggregate Confirmed recall 0.915. (FA autopsy: false alarms are phonetic neighbors
or aligner errors, so precision is an *upper-bound-limited* underestimate.)

**Key findings (this session):**
1. **Recall is not recovered by threshold/boost tuning** (⚠ §10.2 — "acoustic-bound"
   is NOT yet proven; cause remains unisolated). tariff recall is *bit-identical*
   at thresholds 0.10/0.07/0.03 and boost 3→5. "The model emits no hypothesis" is
   inferred from the black-box KWS API, which hides scores — it is equally
   consistent with the keyword search pruning a live hypothesis.
2. **Fast speech halves SHORT-word recall; long words robust; precision holds.**
   (ffmpeg atempo time-compression) tariff Confirmed **0.78→0.56→0.33** at
   1.0/1.25/1.5×; guidance **1.00 at all speeds**; precision stays 1.00 (misses,
   never false alarms). *Caveat:* atempo is WSOLA (may smear onsets = partly a
   simulation artifact); single-call; wide CIs.
3. **Front-end level-normalization is not the lever.** Naive per-80 ms RMS-AGC
   **hurt** recall (0.78→0.22); quiet audio alone (¼ level, AGC off) barely dented it
   (0.78→0.67) — the log-mel frontend is largely level-invariant. AGC reverted.
4. **Corpus is too small** to trust 0.78-vs-0.56-type deltas (few tariff instances;
   no positives for most live words) — some findings are inside the noise band.

## 7. Current mitigations + our working conclusions (challenge these)
- **Operational containment (shipped):** tiered fill — only **precision-vetted**
  words (tariff, recession) place real orders; the other ~12 observe at 1¢
  (non-crossing, never fill, never debit); a **$1 cumulative real-money ledger** +
  notional caps bound total loss. A **promotion loop** validates observe-words
  against public transcripts to graduate them. So a miss costs opportunity, a false
  alarm is bounded. **But for the said-once tradeable words, a miss forfeits the
  entire edge — which is why recall is the real problem, not just a footnote.**
- **Our current beliefs (want them stress-tested):** (a) the dominant recall killer
  is **band-limit domain mismatch** hurting short fricative words; (b) the durable
  fix is a **telephony-domain-matched / low-lookahead model** (stand up
  `streaming_ctc`, then **finetune on telephony+speed-augmented audio**), NOT
  front-end DSP or threshold tuning; (c) **latency** needs the streaming-CTC
  (~0–80 ms lookahead) to beat the ~320 ms KWS floor; (d) whisper/full-ASR is
  **rejected for the live path** (30 s latency + hallucinated keywords on
  band-limited audio = false alarms on order flow), kept only as an offline labeler.

## 8. Open questions for the reviewer (ranked)
1. **Single-shot recall on band-limited telephony** is the product for our (low-prob,
   said-once) markets. What actually moves it most: telephony finetune of the KWS?
   a telephony-native ASR/KWS model? bandwidth extension? pronunciation/lexicon
   expansion (multi-tokenization per word)? Rank by effort/payoff.
2. **Latency:** is sub-~1 s onset→emit realistic on telephony for short words? Is a
   causal streaming-CTC (0–80 ms lookahead) the right bet, or is there a better
   low-latency KWS/transducer path? What breaks first (accuracy? FA?)?
3. **No confidence today.** How much does moving to a real-posterior engine (for
   calibration + per-keyword ROC + online precision monitoring) actually buy us vs
   the recall/latency work?
4. **Evaluation for a 12-word dictionary with rare positives:** how do we get
   trustworthy per-word recall cheaply when most words have ~0 natural occurrences
   (synthesize? TTS-over-telephony? targeted clip mining?) without fooling ourselves?
5. **Is our "domain mismatch dominates" story right,** or are we mis-reading a
   chunking/lookahead/decoding artifact? What single experiment would you run to
   settle acoustic-vs-search (we can dump encoder posteriors)?
6. Anything structural we're missing (endpointing, streaming state hygiene, ensemble
   of complementary models, keyword-boundary tricks).

## 9. Status
Review artifact (2026-07-14), precedes the §8 decisions. No code changes proposed
here. Companion detail: `docs/BAKEOFF.md` (measured numbers), `docs/TWILIO.md`
(ingress/latency), `docs/CTC-MODEL-SOURCING.md` (the dark engine's blocker),
`docs/layers/detector.md` + `docs/layers/prepare.md`.

## 10. Review outcomes — two independent expert reviews (2026-07-14)

Two experts reviewed this spec and CONVERGED. Their corrections supersede the
load-bearing conclusions above; read this section as the current position.

### 10.1 THE correction — deployment base-rate precision (a real design flaw)
Corpus precision (tariff 0.98) does NOT transport to live, because the prior that a
tradeable word is said at all is low (~14% MS, ~19% United). First-order, with
p=0.14, recall 0.89, FA λ≈0.37/h over a 1 h call:
`P(≥1 FA|No)=1−e^-0.37≈0.31`, so
`P(real|trigger)= 0.14·0.89 / (0.14·0.89 + 0.86·0.31) ≈ 0.32` (≈0.40 at p=0.19).
**A "98% corpus-precision" detector is ~32–40% precise in deployment.**
- This does NOT mean negative EV (14¢ for ~32% Yes can still pay), but it means
  **"precision-vetted" must be defined ECONOMICALLY, not from corpus precision**:
  the production metric is `E = P(final Yes | trigger, word, company, price) −
  fill_price − fees/slippage`. `FA/h ≤ 1` is far too permissive; the acceptable FA
  rate is **price-dependent** (a 14¢ order tolerates far lower precision than 70¢).
- Consequence for shipped code: the tiered-fill *safety* holds, but the **promotion
  criterion** and `scripts/validate-from-log.py` must move from corpus
  precision/recall to `P(final Yes|trigger)` + per-price EV.

### 10.2 Rewritten conclusions (supersede §3, §6, §7)
- §6-1 "Recall is model/acoustic-bound, not tunable" → threshold/boost don't recover
  the misses; the cause is multi-factor. **UPDATE (miss-autopsy cell 1, 2026-07-14):
  INT8 QUANTIZATION is isolated as a real contributor — fp32 encoder recovers tariff
  Confirmed recall AAPL 0.78→0.89 and CAT holdout 0.87→0.90 at zero precision cost and
  ~1.5% compute cost (`docs/BAKEOFF.md`). So "acoustic-bound" is partially refuted.**
  A residual gap remains; acoustic representation, tokenization, beam, model capacity
  (3.3M), and streaming behavior are still UNISOLATED (remaining autopsy cells).
- §3/§7 "band-limit domain mismatch dominates; cues absent >3.4 kHz" → **leading
  hypothesis, not yet isolated;** usable cues remain below 3.4 kHz (humans + ASR
  read these words on the same audio) — **distribution shift, not erasure.**
- §7 "CTC is the durable fix" → a causal **scored** detector is required; streaming
  CTC is most implementation-ready, but a low-lookahead **biased RNN-T / joint
  CTC-transducer / two-stage candidate+verifier** stay in the bakeoff.
- §7 "full ASR rejected" → non-streaming Whisper rejected **for execution**;
  low-latency streaming ASR / contextual-keyword-biased decoding is a viable
  **candidate generator**.

### 10.3 Two factual inconsistencies to fix in the body
- **Latency (§2.3 vs §5):** capture→decision ≈90 ms **excludes the model's
  algorithmic emission delay** (the perf test drives a synthetic/stub detector);
  §5's ~720–820 ms is the model's own lookahead + decode on the SAME interval. Both
  true, different components.
- **Resolver race (§1 vs §5):** "PSTN 10–30 s ahead of webcast" and "resolvers watch
  PSTN-speed video" contradict and imply radically different races. **This is the
  most important unresolved fact and it is empirically measurable** (historical
  utterance→strike-close intervals + PSTN-vs-webcast lead distribution). If
  resolvers are on the webcast, latency barely matters and **recall is the whole
  game**; streaming-CTC's value collapses to its *posteriors*, not its latency.

### 10.4 Latency target — reframed
Drop the 350 ms-from-onset full-word bar (a short word can't be confirmed before its
discriminative phones occur). Use two metrics: `onset→candidate` and
`end-of-word→confirmed-emit`; optimize the latter. Measure the FULL chain
(onset→ACK→first-fill) with injected markers. Sub-1 s onset→emit is reachable.

### 10.5 Ordering (diagnostics-first — do NOT train yet)
1. Reframe the eval objective to the economic metrics in §10.1.
2. **Miss-autopsy** (no GPU): for each tariff hit/miss, sweep channel × {fp32,int8}
   × beam × tokenization × streaming-offset × speed and compute a **constrained
   keyword-path score** — isolates acoustic vs quantization vs capacity vs lexicon
   vs search vs streaming. Needs an offline ONNX-encoder harness (the KWS C API
   hides scores). fp32-vs-int8 is the cheapest check and comes first.
3. Hand-transcribe the telephony miss clips (info-in-band test).
4. Measure the resolver race, PSTN lead, and fill liquidity (§10.3, §5).
5. Only if an acoustic floor is confirmed: real clip-mined telephony data →
   telephony-finetune a causal scored detector (adapt the ENCODER). BWE = one
   bakeoff row, not a workstream.

**Traps (do NOT):** bandwidth extension as a workstream; more threshold tuning;
time-domain AGC (feature-domain CMVN if anything); training the CTC before the
miss-autopsy; chasing 350 ms; certifying words from TTS; corpus precision as the
promotion bar.
