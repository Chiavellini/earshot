#!/usr/bin/env python3
"""Turn a YouTube json3 caption track into the project's word-timestamp sidecar.

Called by scripts/wnt-vod-fetch.sh. Emits the SAME shape that
scripts/transcript-fetch.py produces — `[{word, start, end, char_start,
char_end}]` — so `scripts/manifest-from-words.py` and every consumer of a
`.words.json` work against captions without knowing the difference.

Why captions rather than local ASR: the json3 track is word-level timed,
downloads in about a second, and costs no CPU. faster-whisper needed 25+ min
for one 20-minute session on a contended box, which makes a 16-session backfill
impossible to run alongside live event arms.

Two properties of json3 that this file exists to normalize:

  * There is a START per word and NO END. Each word's end is approximated with
    the next word's start (the last word keeps start == end). Downstream code
    that reads `end` — manifest-from-words.py builds occurrence spans from it —
    therefore gets a usable, slightly generous span rather than a zero-width
    one. Do not read these ends as measured word durations.
  * Non-speech cues are bracketed (`[music]`, `[applause]`). They are dropped:
    left in, they would create phantom occurrences and shift char offsets.

⚠ Captions carry ASR spellings ("Blanche" -> "Blanch", "Muir" -> "Mure" on
2026-07-29). Consumers matching market words must expect near-misses; this file
does not silently correct them, because a correction here would be invisible to
the thing doing the matching.

usage: wnt-captions-parse.py <track.json3> <words.json> <transcript.txt>
prints the word count; exits 1 if the track yields no words.
"""

import json
import sys


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__.strip().splitlines()[-2], file=sys.stderr)
        return 2
    cap, words_out, txt_out = sys.argv[1:4]

    doc = json.load(open(cap))
    words = []
    for ev in doc.get("events", []):
        base = ev.get("tStartMs", 0)
        for seg in ev.get("segs", []) or []:
            tok = (seg.get("utf8") or "").strip()
            if not tok:
                continue
            if tok.startswith("[") and tok.endswith("]"):
                continue
            start = (base + seg.get("tOffsetMs", 0)) / 1000.0
            words.append({"word": tok, "start": start, "end": start})

    if not words:
        return 1

    for i in range(len(words) - 1):
        words[i]["end"] = max(words[i]["start"], words[i + 1]["start"])

    pos = 0
    for w in words:
        w["char_start"] = pos
        pos += len(w["word"]) + 1
        w["char_end"] = pos - 1

    json.dump(words, open(words_out, "w"))
    open(txt_out, "w").write(" ".join(w["word"] for w in words))
    print(len(words))
    return 0


if __name__ == "__main__":
    sys.exit(main())
