# Export an English ≤80 ms streaming Zipformer-CTC (run on a GPU/PyTorch box)

The `streaming_ctc` engine (`src/detector/streaming_ctc.rs`) drives any
sherpa/k2 **zipformer2 streaming-CTC** ONNX with this signature:

- input `x [N, T, 80]` (80-bin fbank) + the per-layer cache tensors +
  `embed_states` + `processed_lens`
- output `log_probs [N, T', vocab]` (CTC frame log-posteriors)
- ONNX metadata `model_type=zipformer2`, `T=…`, **`decode_chunk_len=…`**

`export-onnx-streaming-ctc.py` from icefall produces exactly this. The only thing
that must change vs. the stock recipe is a **small `decode_chunk_len`** so the
chunk step is ≤80 ms (the Chinese stand-in used 64 → 640 ms; we want ≈8 → 80 ms).

This can't run on the EARSHOT box (no PyTorch/k2 + disk). Run it where a GPU +
`icefall`/`k2` + `torch` are installed, then copy the model dir back.

## 0. The one decision to resolve first — CTC head

The checkpoint **must have been trained with `--use-ctc 1`** (a CTC head).
Streaming transducer-only checkpoints (e.g. the common
`Zengwei/icefall-asr-librispeech-streaming-zipformer-2023-05-17`) do **not**
export a `log_probs` CTC output. Options, best first:

1. A pretrained **streaming zipformer trained with `--use-ctc 1`** (hybrid
   transducer+CTC) — check the icefall LibriSpeech zipformer HF model list for a
   streaming checkpoint whose training cmd includes `--use-ctc 1`. If found,
   skip to step 2 (no training).
2. **Fine-tune** an existing streaming checkpoint for a few epochs with
   `--use-ctc 1 --causal 1` to attach a CTC head (hours on a GPU, not a full run).
3. **Full train** the LibriSpeech streaming zipformer with `--use-ctc 1
   --causal 1` (last resort; days on a GPU).

> If none of 1–3 is acceptable, fall back to the NeMo FastConformer `[70,0]`
> path (different engine geometry — see the plan's model-sourcing options).

## 1. Setup (GPU box)

```bash
# k2 + icefall + torch per https://icefall.readthedocs.io (match CUDA/torch)
git clone https://github.com/k2-fsa/icefall
cd icefall/egs/librispeech/ASR
export PYTHONPATH=$PWD/../../../:$PYTHONPATH
pip install onnx onnxruntime kaldifeat sentencepiece
# place the CTC-head checkpoint at zipformer/exp/epoch-*.pt (+ its data/lang_bpe_500/)
```

## 2. Export to streaming ONNX with an ≤80 ms chunk

```bash
./zipformer/export-onnx-streaming-ctc.py \
  --exp-dir zipformer/exp \
  --tokens data/lang_bpe_500/tokens.txt \
  --causal 1 \
  --chunk-size 8 \                # -> decode_chunk_len ~8 frames -> ~80 ms step
  --left-context-frames 64 \      # left context (past) — free, no latency cost
  --use-ctc 1 \
  --epoch <EPOCH> --avg <AVG> --use-averaged-model 1
```

Produces `ctc-epoch-*-chunk-8-left-64.onnx` (+ variants). Rename the encoder to a
clear name and quantize if desired:

```bash
python -m onnxruntime.quantization.preprocess ...   # optional int8 for CPU RTF
```

## 3. Acceptance — must match the engine's expectations

Assemble a model dir `models/streaming-ctc-en-<date>/` containing:
`model[.int8].onnx`, `tokens.txt`, `bpe.model` (the LibriSpeech BPE-500).
Then verify on the EARSHOT box:

```bash
export ORT_DYLIB_PATH=…/libonnxruntime.1.27.1.dylib   # scripts/fetch-models.sh prints this
export EARSHOT_CTC_MODEL_DIR=$PWD/models/streaming-ctc-en-<date>
EARSHOT_REQUIRE_MODELS=1 cargo test --lib detector::streaming_ctc
```

The integration tests confirm the signature loads, streams, and the scorer
fires. Sanity-check the ONNX metadata `decode_chunk_len` is ~8 (≤80 ms) and the
`x` input is `[N, T, 80]`.

## 4. Wire it in

- `[detector] engine = "streaming_ctc"` + `[detector.streaming_ctc] model_dir = …`
  in the bakeoff/event config.
- BPE-500 vs gigaspeech: if this model's `bpe.model`/`tokens.txt` differ from the
  sherpa KWS gigaspeech vocab, re-derive the `kws_compile.rs` pinned test vectors
  against the new `bpe.model` (they assert the tokenizer's segmentation).
- Add the model to `scripts/fetch-models.sh` (host the dir; pin url+sha256 in
  `VERIFICATIONS.md`), then run the paced bake-off (Phase 4):
  `cargo run --release -- bench-detector --engines sherpa_kws streaming_ctc \
   --paced --calls CAT-Q1-2026 …` → the Fast p95 onset→emit ≤ 350 ms gate.
```
