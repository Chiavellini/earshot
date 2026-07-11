#!/usr/bin/env python3
"""Spot-check manifest onsets by ear (docs/BAKEOFF.md: ~20 % random sample).

Plays ±2 s around each sampled onset from the TELEPHONY wav (what the
detector hears) via afplay (macOS). Records verdicts and prints the measured
disagreement rate — if the sample is clean, trust the aligner for the rest.

Usage: python3 scripts/corpus-verify.py corpus/manifests/aapl-q2-2026.toml [--sample 0.2] [--seed 7]
Requires: python3 stdlib + afplay + ffmpeg (no venv needed).
"""

import argparse
import random
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--sample", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    path = Path(args.manifest)
    mf = tomllib.loads(path.read_text())
    corpus_root = path.parent.parent
    audio = corpus_root / mf["call"]["audio"]
    occurrences = mf.get("occurrences", [])
    if not occurrences:
        sys.exit("manifest has no occurrences")

    rng = random.Random(args.seed)
    k = max(1, round(len(occurrences) * args.sample))
    sample = sorted(rng.sample(range(len(occurrences)), k))
    print(f"{len(occurrences)} occurrences; spot-checking {k} (seed {args.seed})")

    verdicts = []
    for idx in sample:
        occ = occurrences[idx]
        onset_s = occ["onset_ms"] / 1000
        print(f"\n[{idx}] {occ['phrase']!r} @ {onset_s:.1f}s — playing ±2 s")
        with tempfile.NamedTemporaryFile(suffix=".wav") as clip:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(max(0.0, onset_s - 2)),
                 "-t", "4", "-i", str(audio), clip.name],
                check=True,
            )
            while True:
                subprocess.run(["afplay", clip.name], check=True)
                ans = input("  heard the word near the middle? [y/n/r=replay] ").strip().lower()
                if ans in ("y", "n"):
                    verdicts.append(ans == "y")
                    break

    bad = verdicts.count(False)
    print(f"\nspot-check: {len(verdicts) - bad}/{len(verdicts)} confirmed, {bad} disputed")
    if bad:
        print("disputed onsets — re-run alignment or fix the manifest by hand before committing")
        sys.exit(1)
    print("sample clean — aligner trusted for the remainder (record this in docs/BAKEOFF.md)")


if __name__ == "__main__":
    main()
