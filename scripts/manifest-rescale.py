#!/usr/bin/env python3
"""Rescale a corpus manifest for a time-compressed (fast-speech) audio variant.

Time-compressing audio by tempo T (ffmpeg atempo) moves every occurrence's
absolute stream position to 1/T of its original ms. This rewrites onset_ms /
end_ms by 1/T so the manifest still lines up with the compressed WAV; the audio
path (relative to the corpus root) and everything else are preserved.

Usage: manifest-rescale.py <in.toml> <tempo> <out.toml>
"""
import re
import sys

if len(sys.argv) != 4:
    sys.exit("usage: manifest-rescale.py <in.toml> <tempo> <out.toml>")
inp, tempo, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]
if tempo <= 0:
    sys.exit("tempo must be > 0")

rescaled = []
for line in open(inp).read().splitlines():
    m = re.match(r"(\s*(?:onset_ms|end_ms)\s*=\s*)(\d+)\s*$", line)
    if m:
        rescaled.append(f"{m.group(1)}{round(int(m.group(2)) / tempo)}")
    else:
        rescaled.append(line)
open(out, "w").write("\n".join(rescaled) + "\n")
print(f"rescaled {inp} by 1/{tempo} -> {out}")
