# Sourcing an English streaming-Zipformer-CTC model

**Status:** TIER 0 SUCCEEDED (2026-07-17). The hunt found the one existing
English causal+CTC checkpoint — `csukuangfj/icefall-asr-librispeech-
streaming-zipformer-small-2024-03-18` (trained `--causal 1 --use-ctc 1`,
full LibriSpeech, 23.8 M params), ALREADY exported to the streaming-CTC ONNX
format as release asset `sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18`
(fetched by `scripts/fetch-models.sh`; sha + geometry evidence in
`VERIFICATIONS.md`: `decode_chunk_len=32` → 320 ms quantum, ~130 ms
right-context, `out_per_chunk=8`). Integration tests + `compile-keywords`
green against it on this box. REMAINING GATE: the corpus bake-off
(`bench-detector --engines streaming_ctc`, docs/BAKEOFF.md bars) — the CTC
head was an auxiliary loss (scale 0.2) with no published WER, so quality is
unproven until benched. Known latency-trim follow-on: re-export the HF
checkpoint at chunk 16 (160 ms quantum; trained chunk grid 16/32/64) via
`export-onnx-streaming-ctc.py` if the paced bench lands near the 350 ms bar.
Tier 1 (CTC-head finetune, GPU) stays the fallback ONLY if this model fails
the bars; recommended base: `Zengwei/icefall-asr-librispeech-streaming-
zipformer-2023-05-17`.

Historical scoping below (2026-07-14) — kept for the evidence trail.
See `docs/BAKEOFF.md` for why the engine
exists and `docs/layers/detector.md` §streaming_ctc for what it consumes.

## Why this is the blocker

`streaming_ctc` is the only engine on which prefix (Fast-tier) firing actually
pays off — it reads raw per-frame CTC posteriors with 0–80 ms lookahead, vs
sherpa_kws's ~320 ms baked-in lookahead. The engine is built, unit-tested, and
model-agnostic (init states/shapes read straight off the ONNX signature), so an
English model is a true drop-in. It is DARK only because no such model exists in
the repo.

## What the engine requires (the artifact spec)

From `src/detector/streaming_ctc.rs`: a **streaming (causal) zipformer2 encoder
ONNX** that

- carries the 74 streaming cache tensors as inputs/outputs
  (`cached_{key,nonlin_attn,val1,val2,conv1,conv2}`×12 + `embed_states` +
  `processed_lens`),
- exposes a `log_probs` output (the CTC head — one posterior distribution over
  the vocab per output frame),
- puts `decode_chunk_len` in the ONNX metadata,
- ships `tokens.txt` (+ `bpe.model` for keyword text→piece compilation).

This is exactly the format of the Chinese
`sherpa-onnx-streaming-zipformer-ctc-*` models the integration tests already run
against — so the format is proven; only an English instance is missing.

## The finding: the export tool exists, the checkpoint is the gap

The k2-fsa off-the-shelf release page lists **only Chinese** streaming-zipformer
CTC exports. Off-the-shelf English streaming zipformers are **transducers** with
no CTC head (`csukuangfj/sherpa-onnx-streaming-zipformer-en-*`), and off-the-shelf
English zipformer-**CTC** checkpoints (`Zengwei/...-ctc-attention-decoder-2024-07-08`,
`Zengwei/...-medium-cr-ctc-20241018`, `desh2608/icefall-asr-librispeech-zipformer-ctc`)
appear to be **non-streaming (non-causal)**. So neither shelf has the intersection
we need: *English × streaming × CTC head*.

But the **export script already exists**:
`icefall/egs/librispeech/ASR/zipformer/export-onnx-streaming-ctc.py` — it exports
a causal zipformer's CTC head to precisely the streaming-CTC ONNX format above.
No script authoring is required. The only missing input is a **checkpoint trained
with both `--causal 1` (streaming) and `--use-ctc 1` (CTC head)**.

## Effort tiers (drive the go/no-go)

- **Tier 0 — a causal+CTC English checkpoint already exists (hours, CPU-only).**
  If any published icefall/HF checkpoint was trained `--causal 1 --use-ctc 1`,
  the job is: `git lfs pull` the `pretrained.pt`, run `export-onnx-streaming-ctc.py`,
  drop the output dir into a config's `model_dir`. No GPU, no training. **This is
  the outcome to confirm or rule out first** (next probe below).
- **Tier 1 — finetune a CTC head onto an existing causal transducer (GPU-hours).**
  Published streaming English zipformer transducers exist; the CTC head is a
  single linear layer on the encoder. Resume from the causal checkpoint with
  `--use-ctc 1` for a partial epoch on LibriSpeech/GigaSpeech to train the head
  (encoder mostly frozen/warm). Modest GPU (single card, hours–day). Risk: head
  quality on telephony audio without domain finetuning.
- **Tier 2 — train from scratch `--causal 1 --use-ctc 1` (GPU-days).** Only if no
  usable causal checkpoint is found. Highest cost/uncertainty; effectively a
  research task. Avoid unless Tier 0/1 both fail.

## Recommended next probe (cheap, decides the tier)

1. Enumerate the HF `k2-fsa`, `Zengwei`, `csukuangfj`, `yfyeung`, `marcoyang`
   orgs for a LibriSpeech/GigaSpeech zipformer whose training command shows BOTH
   `--causal 1` and `--use-ctc 1` (read the model card's train invocation +
   `RESULTS.md`). The **CR-CTC** line (`Zengwei/...-cr-ctc`) is the most promising
   lead to chase for a streaming variant — CR-CTC gives strong CTC quality at low
   latency; confirm whether a *causal* CR-CTC English export exists or is planned.
2. If found → Tier 0: export with `export-onnx-streaming-ctc.py`, verify the ONNX
   has `log_probs` + `decode_chunk_len` + the cache tensors, `EARSHOT_CTC_MODEL_DIR`
   the integration tests green, pin URL/sha in `scripts/fetch-models.sh` +
   `VERIFICATIONS.md`, then run the ≤350 ms Fast-p95 bake-off.
3. If not found → decide Tier 1 vs Tier 2 with the user (GPU budget question).

## Domain caveat (applies to every tier)

The corpus is **8 kHz telephony**; LibriSpeech/GigaSpeech checkpoints are trained
on clean/16 kHz-ish audio. Whatever model is sourced must be benched on the
telephony corpus (`bench-detector … --engines streaming_ctc`) before trusting its
recall/FA — a clean-audio CTC model can degrade on band-limited call audio.
Telephony-domain finetuning may be a follow-on even after a model lands.

## Beyond CTC — 2026 low-latency streaming families (G2 model-breadth scan, 2026-07-21)

The project had only ever evaluated the sherpa/k2 **zipformer** family. Two things
changed the calculus: (a) prefix-firing is retired as the overtake lever (BAKEOFF
§prefix-fire diagnosis), so we no longer need CTC frame-posteriors — we need the
**fastest full-word emit**, which frees up RNN-T/transducer families; (b) the
competitive quantity is **decode-lag**, not the 350 ms onset→emit bar
(RACE-STRATEGY §2b). A read-only scan of the current landscape (sources below):

**The landscape moved in ~2026:** NVIDIA shipped cache-aware streaming models with
an **80 ms decode quantum** (vs our 320 ms), and sherpa-onnx merged support. There
is still **no newer English streaming zipformer-CTC** (2025 CTC exports are
Chinese-only), and our own KWS model is baked at chunk-16-left-64 (320 ms) with no
runtime chunk knob — so a smaller quantum requires a *different model*, not a config.

**Ranked bench shortlist (G2):**
1. **NVIDIA Parakeet-Realtime-EOU-120M-v1** — 80 ms quantum, 120M params,
   CPU-friendly, a working Rust+`ort` streaming reference exists (parakeet-rs). RNN-T
   emits per-frame tokens (fine now that prefix-firing is retired). Biggest
   structural decode-lag cut. Risks: community ONNX export needs verifying;
   sherpa-onnx support is an open issue (integrate via `ort` directly, new engine
   file behind the `Detector` trait); NVIDIA Open Model License (commercial OK).
2. **sherpa-onnx KWS zipformer at chunk-8 (160 ms)** — lowest-integration-risk
   (same stack), halves the quantum, arbitrary keywords via keywords-file, 3M params.
   Needs a chunk-8 English export (gigaspeech en is 2024-01-01 chunk-16; would need
   icefall re-export — same class as P1).
3. **Nemotron-Speech-Streaming-en-0.6b (80 ms mode)** — best accuracy, selectable
   80 ms latency, sherpa-onnx int8 export exists but only for the **560 ms** chunk;
   600M params on CPU is the risk. Bench only if 1–2 miss on accuracy.

**Not worth benching:** Whisper family (buffer-and-re-decode, ~seconds even on GPU);
Moonshine v2 streaming (impressive latency, MIT, but no published streaming ONNX and
autoregressive-revising output); Vosk/Kaldi (no ONNX path, 2020-era models); Silero
(utterance-level); Coqui (dead 2024). openWakeWord-class spotters emit per-frame at
80 ms but need synthetic-TTS training per keyword — wildcard only.

**Key nuance (do not skip):** our measured ~1000 ms onset→emit is ~3× the 320 ms
quantum, so a smaller quantum is **necessary but not sufficient** — the fixed
word_duration + model-lookahead terms dominate. Any candidate must be **benched on
the paced harness**, not assumed from its quantum. The bench target is decode-lag
(onset→emit − word_duration) ≤ ~200 ms, the competitive quantity.

### G2b Phase 1 — de-risk PASSED (2026-07-21): Parakeet-Realtime-EOU-120M ONNX obtained + loads

Availability resolved: NVIDIA ships Parakeet only as `.nemo` (460 MB, no ONNX) and this box has no
NeMo/torch to export — BUT a **pre-exported CPU ONNX exists**: `altunenes/parakeet-rs` →
`realtime_eou_120m-v1-onnx/{encoder.onnx (459 MB), decoder_joint.onnx (21 MB), tokenizer.json}`.
Both load cleanly under onnxruntime (⇒ fine under our ort 1.27). Also available if EOU disappoints:
`onnx-community/parakeet-ctc-0.6b-ONNX` (CTC, but 600M + likely non-streaming) and the packaged
Nemotron-0.6b ONNX. A **sherpa chunk-8 English KWS does NOT exist** (only chunk-16 gigaspeech), so
the easy same-engine drop-in is off the table — a new engine is required either way.

Measured ONNX signature (the integration contract):
- **encoder.onnx** (cache-aware streaming FastConformer, 17 layers): in `audio_signal[B,128,T]`
  (**128-mel** features — ours is 80), `length`, + 3 cache tensors (`cache_last_channel[*,1,70,512]`,
  `cache_last_time[*,1,512,8]`, `cache_last_channel_len`); out `outputs[B,512,T]`, `encoded_lengths`,
  + 3 `new_cache_*`.
- **decoder_joint.onnx** (RNN-T): in `encoder_outputs[*,512,*]`, `targets[int32]`, `target_length`,
  `input_states_1/2[1,*,640]` (2-layer LSTM prednet); out joint logits `[*,*,*,1027]` (1026 BPE + blank),
  `output_states_1/2`.
- tokenizer: BPE, 1026 tokens.

**Reality vs the "drop-in" hope:** this is **RNN-T, not CTC** — our `ctc_scorer` does not apply.
Phase 2 needs a NEW engine: (1) a **128-mel** featurizer matched to NeMo's preprocessor (the
correctness-critical part — wrong mel normalization ⇒ garbage output ⇒ misleading bench),
(2) the encoder streaming-cache loop (5 tensors, same pattern as `streaming_ctc.rs`), (3) an RNN-T
greedy decode (per encoder frame: joint→argmax over 1027; non-blank ⇒ emit + advance the LSTM
prednet; blank ⇒ next frame), (4) keyword BPE-subsequence matching on the emitted stream (prefix
retired ⇒ full-word match). Artifacts staged at the session scratchpad `parakeet-eou/`. Phase 2 is
a multi-hour, correctness-sensitive build — do it carefully (a subtly-wrong featurizer/decoder
produces a plausible-but-false bench).

## Sources
- [nvidia/parakeet_realtime_eou_120m-v1](https://huggingface.co/nvidia/parakeet_realtime_eou_120m-v1) · [parakeet-rs (Rust+ort streaming on CPU)](https://github.com/altunenes/parakeet-rs) · [sherpa-onnx issue #2805](https://github.com/k2-fsa/sherpa-onnx/issues/2805)
- [nvidia/nemotron-speech-streaming-en-0.6b](https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b) · [sherpa-onnx NeMo models (2026-04-25 int8)](https://k2-fsa.github.io/sherpa/onnx/nemo/index.html)
- [sherpa-onnx KWS pretrained models](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html)
- [icefall export-onnx-streaming-ctc.py (dir listing)](https://github.com/k2-fsa/icefall/tree/master/egs/librispeech/ASR/zipformer)
- [icefall export-onnx-ctc.py](https://github.com/k2-fsa/icefall/blob/master/egs/librispeech/ASR/zipformer/export-onnx-ctc.py)
- [Chinese streaming-zipformer-CTC (format reference)](https://huggingface.co/k2-fsa/sherpa-onnx-streaming-zipformer-ctc-multi-zh-hans-2023-12-13)
- [Zengwei/icefall-asr-librispeech-zipformer-medium-cr-ctc-20241018](https://huggingface.co/Zengwei/icefall-asr-librispeech-zipformer-medium-cr-ctc-20241018)
- [Zengwei/icefall-asr-librispeech-zipformer-ctc-attention-decoder-2024-07-08](https://huggingface.co/Zengwei/icefall-asr-librispeech-zipformer-ctc-attention-decoder-2024-07-08)
- [sherpa online-ctc pretrained models (Chinese only)](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-ctc/zipformer-ctc-models.html)
