#!/usr/bin/env python3
"""Forced alignment for corpus ground truth (docs/BAKEOFF.md).

Runs faster-whisper word-level timestamps on the WIDEBAND original (better
ASR than the telephony-degraded 8 kHz file the detector sees — the manifest
is only read by the scorer, so this is not leakage), extracts occurrences of
the target phrases per VERIFICATIONS.md Q4 semantics (distinct standalone
word; plurals/possessives count; no stemming, no substrings of closed
compounds), and writes a draft manifest TOML to stdout.

Setup (once):   python3 -m venv .venv-corpus
                .venv-corpus/bin/pip install faster-whisper
Usage:          .venv-corpus/bin/python scripts/corpus-align.py \
                    --audio corpus/raw/aapl-q2-2026.m4a \
                    --id AAPL-Q2-2026 --split dev \
                    --phrases recession,guidance \
                    > corpus/manifests/aapl-q2-2026.toml

Review the draft (scripts/corpus-verify.py spot-checks onsets) before
committing. `split` is pre-committed here, BEFORE any engine run.
"""

import argparse
import re
import sys


def variants(phrase: str) -> set[str]:
    """Q4 semantics: the word itself, plural, and possessive forms."""
    last = phrase.split()[-1]
    out = {last, last + "s", last + "'s", last + "s'", last + "es"}
    return {v.lower() for v in out}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True, help="wideband original")
    ap.add_argument("--id", required=True)
    ap.add_argument("--split", required=True, choices=["dev", "holdout"])
    ap.add_argument("--phrases", required=True, help="comma-separated canonical phrases")
    ap.add_argument("--model", default="small.en", help="faster-whisper model size")
    args = ap.parse_args()

    from faster_whisper import WhisperModel  # deferred: import cost + optional dep

    model = WhisperModel(args.model, device="auto", compute_type="int8")
    segments, _info = model.transcribe(args.audio, word_timestamps=True, language="en")

    phrases = [p.strip().lower() for p in args.phrases.split(",") if p.strip()]
    # Collect the word stream once (segments is a generator).
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append((re.sub(r"[^\w']", "", w.word).lower(), w.start, w.end))
    print(f"# transcribed {len(words)} words from {args.audio}", file=sys.stderr)

    occurrences = []
    for phrase in phrases:
        parts = phrase.split()
        for i in range(len(words) - len(parts) + 1):
            # Interior words must match exactly; the last word admits Q4
            # plural/possessive variants (Q4 also allows variants on other
            # words in principle — extend if a phrase ever needs it).
            exact = all(words[i + j][0] == parts[j] for j in range(len(parts) - 1))
            if exact and words[i + len(parts) - 1][0] in variants(parts[-1]):
                occurrences.append((phrase, words[i][1], words[i + len(parts) - 1][2]))

    occurrences.sort(key=lambda o: o[1])
    print("[call]")
    print(f'id    = "{args.id}"')
    print(f'audio = "telephony/{args.id.lower()}.wav"')
    print(f'split = "{args.split}"')
    for phrase, start, end in occurrences:
        print()
        print("[[occurrences]]")
        print(f'phrase   = "{phrase}"')
        print(f"onset_ms = {int(start * 1000)}")
        print(f"end_ms   = {int(end * 1000)}")
    print(f"# {len(occurrences)} occurrences", file=sys.stderr)


if __name__ == "__main__":
    main()
