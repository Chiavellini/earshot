#!/usr/bin/env python3
"""Transcript acquisition for the call-structure spike (docs/CALL-STRUCTURE.md).

Produces, per call, a plain-text transcript plus a word-timestamp sidecar that
maps every character offset in the text back to a wall-clock time in the audio.
That sidecar is what lets structure-analyze.py turn a section's char offset into
*seconds*, so we can measure the lead time between a section marker and a target
keyword (the make-or-break Tier-B metric).

Two sources, both offline:

  --audio  : transcribe one of the 6 corpus recordings with faster-whisper
             (same dependency/config as scripts/corpus-align.py). Fully
             reproducible; gives text + timestamps in one pass. This is the
             Tier-B (timestamped depth) path.

  external : for Tier-A breadth (many quarters per company), drop a public
             transcript as corpus/transcripts/<id>.txt by hand (Motley Fool,
             IR sites). Those have no timestamps — they feed section-order
             stability and P(keyword|section) base rates only, not lead time.

Outputs (corpus/transcripts/ is gitignored — raw text stays local):
    corpus/transcripts/<id>.txt          plain text
    corpus/transcripts/<id>.words.json   [{word,start,end,char_start,char_end}]

Setup (once):
    python3 -m venv .venv-corpus
    .venv-corpus/bin/pip install faster-whisper

Usage:
    .venv-corpus/bin/python scripts/transcript-fetch.py \
        --audio corpus/raw/aapl-q2-2026.mp3 --id AAPL-Q2-2026
"""

import argparse
import json
import os
import sys

OUT_DIR = "corpus/transcripts"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True, help="wideband original, e.g. corpus/raw/<id>.mp3")
    ap.add_argument("--id", required=True, help="call id, e.g. AAPL-Q2-2026")
    ap.add_argument("--model", default="small.en", help="faster-whisper model size")
    args = ap.parse_args()

    from faster_whisper import BatchedInferencePipeline, WhisperModel  # deferred: optional dep

    model = WhisperModel(args.model, device="auto", compute_type="int8")
    batched = BatchedInferencePipeline(model=model)
    segments, _info = batched.transcribe(
        args.audio, word_timestamps=True, language="en", batch_size=8
    )

    # Reconstruct natural text from whisper word tokens (each includes its own
    # leading space), tracking the exact char span of every word so any char
    # offset in the .txt maps back to a timestamp.
    text_parts: list[str] = []
    words_out: list[dict] = []
    cursor = 0
    for seg in segments:
        for w in seg.words or []:
            tok = w.word
            char_start = cursor
            text_parts.append(tok)
            cursor += len(tok)
            words_out.append(
                {
                    "word": tok.strip(),
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "char_start": char_start,
                    "char_end": cursor,
                }
            )
    text = "".join(text_parts)

    os.makedirs(OUT_DIR, exist_ok=True)
    txt_path = os.path.join(OUT_DIR, f"{args.id.lower()}.txt")
    words_path = os.path.join(OUT_DIR, f"{args.id.lower()}.words.json")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)
    with open(words_path, "w", encoding="utf-8") as f:
        json.dump(words_out, f)

    print(
        f"# {args.id}: {len(words_out)} words, {len(text)} chars -> {txt_path}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
