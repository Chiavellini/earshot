#!/usr/bin/env python3
"""Miss-autopsy harness — offline transducer inspection for the recall diagnostic
(docs/TELEPHONY-DETECTION-SPEC.md §10.5).

The sherpa KWS C API hides scores (confidence is a constant 1.0), so to settle
"acoustic vs search/lexicon" we run the gigaspeech KWS transducer ONNX directly
via onnxruntime and read the actual per-frame evidence. This lets us compute a
CONSTRAINED keyword-path score on a specific miss clip:
  - strong sub-threshold mass on the keyword's token sequence  => search/lexicon
    problem (cheap fix: beam / tokenization / operating point);
  - flat/absent mass                                            => acoustic
    (justifies a telephony finetune).

REQUIRES onnxruntime (installed under /Library/Developer/CommandLineTools/usr/bin/
python3 3.9.6 on this box; the homebrew python3.14 has no wheel yet). Run e.g.:
  /Library/Developer/CommandLineTools/usr/bin/python3 scripts/miss-autopsy.py \
      --model-dir models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01 \
      --precision fp32 --signature

Increments:
  [1] --signature  : load encoder/decoder/joiner (fp32 or int8) and dump the ONNX
                     I/O signature (names/shapes/dtypes). Proof-of-harness; reveals
                     the interface the forced-decode needs.  <-- implemented here.
  [2] forced-decode: run a keyword's token sequence through the transducer over a
                     miss clip's encoder frames, return the path score.  <-- TODO,
                     built on the signature from [1].
"""
import argparse
import sys
from pathlib import Path

try:
    import onnxruntime as ort
except Exception as e:  # pragma: no cover
    sys.exit(f"onnxruntime import failed ({e}); use the interpreter that has it "
             f"(/Library/Developer/CommandLineTools/usr/bin/python3).")


def model_path(model_dir: Path, stem: str, precision: str) -> Path:
    suffix = ".int8.onnx" if precision == "int8" else ".onnx"
    hits = [p for p in model_dir.iterdir()
            if p.name.startswith(stem) and p.name.endswith(suffix)
            and (".int8." in p.name) == (precision == "int8")]
    if not hits:
        sys.exit(f"no {stem}*{suffix} in {model_dir}")
    return hits[0]


def dump_signature(sess: ort.InferenceSession, label: str) -> None:
    print(f"\n== {label} ==")
    for io, items in (("inputs", sess.get_inputs()), ("outputs", sess.get_outputs())):
        print(f"  {io}:")
        for t in items:
            print(f"    {t.name:28s} {str(t.shape):32s} {t.type}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True, type=Path)
    ap.add_argument("--precision", choices=["fp32", "int8"], default="fp32")
    ap.add_argument("--signature", action="store_true", help="dump ONNX I/O signature")
    a = ap.parse_args()

    encoder = model_path(a.model_dir, "encoder", a.precision)
    # decoder is fp32 in both live postures; joiner follows the precision flag.
    decoder = model_path(a.model_dir, "decoder", "fp32")
    joiner = model_path(a.model_dir, "joiner", a.precision)
    print(f"precision={a.precision}\n encoder={encoder.name}\n decoder={decoder.name}\n joiner={joiner.name}")

    opts = ort.SessionOptions()
    enc = ort.InferenceSession(str(encoder), opts, providers=["CPUExecutionProvider"])
    dec = ort.InferenceSession(str(decoder), opts, providers=["CPUExecutionProvider"])
    joi = ort.InferenceSession(str(joiner), opts, providers=["CPUExecutionProvider"])
    print("loaded encoder/decoder/joiner OK")

    if a.signature:
        dump_signature(enc, "ENCODER")
        dump_signature(dec, "DECODER")
        dump_signature(joi, "JOINER")
        # encoder metadata often carries chunk/context params we need for streaming.
        meta = enc.get_modelmeta().custom_metadata_map
        if meta:
            print("\n== ENCODER metadata ==")
            for k, v in meta.items():
                print(f"    {k} = {v}")

    # [2] forced-decode over a miss clip: TODO — build on the signature above.
    #   fbank(clip) -> encoder(frames, streaming cache) -> for the keyword token
    #   sequence, run decoder+joiner along the forced path and accumulate log-prob;
    #   compare fp32 vs int8 and vs the firing threshold.


if __name__ == "__main__":
    main()
