# Sourcing an English streaming-Zipformer-CTC model

**Status:** scoping only (2026-07-14). No export run yet — this is the go/no-go
input for the M4 detector-latency work. See `docs/BAKEOFF.md` for why the engine
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

## Sources
- [icefall export-onnx-streaming-ctc.py (dir listing)](https://github.com/k2-fsa/icefall/tree/master/egs/librispeech/ASR/zipformer)
- [icefall export-onnx-ctc.py](https://github.com/k2-fsa/icefall/blob/master/egs/librispeech/ASR/zipformer/export-onnx-ctc.py)
- [Chinese streaming-zipformer-CTC (format reference)](https://huggingface.co/k2-fsa/sherpa-onnx-streaming-zipformer-ctc-multi-zh-hans-2023-12-13)
- [Zengwei/icefall-asr-librispeech-zipformer-medium-cr-ctc-20241018](https://huggingface.co/Zengwei/icefall-asr-librispeech-zipformer-medium-cr-ctc-20241018)
- [Zengwei/icefall-asr-librispeech-zipformer-ctc-attention-decoder-2024-07-08](https://huggingface.co/Zengwei/icefall-asr-librispeech-zipformer-ctc-attention-decoder-2024-07-08)
- [sherpa online-ctc pretrained models (Chinese only)](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-ctc/zipformer-ctc-models.html)
