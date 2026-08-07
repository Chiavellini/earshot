#!/usr/bin/env python3
"""vtt_parse.py — parse a broadcast-style scrolling-caption VTT file into a
words.json-shaped word-level timing track.

Fox News's video pages (extracted via yt-dlp's native `foxnews` extractor —
confirmed working 2026-08-06 against video.foxnews.com/v/6402195227112)
carry PROFESSIONAL closed captions, not ASR — short (1-3 word) scrolling
cues, speaker-labelled ("MARIA: ...", "RACHEL: ..."). This is higher-quality
ground truth than YouTube auto-captions (no ASR spelling drift), but VTT
cues are phrase-level, not strictly one-word-per-timestamp — words within a
cue are evenly interpolated across the cue's [start,end] span, which is a
reasonable approximation given cues are short (typically <3s).
"""

from __future__ import annotations

import re

TIME_RE = re.compile(r"(\d+):(\d+)\.(\d+)\s*-->\s*(\d+):(\d+)\.(\d+)")
SPEAKER_RE = re.compile(r"^[A-Z]+:\s*")
WORD_RE = re.compile(r"[A-Za-z0-9'’]+")


def parse_vtt(path: str) -> list[dict]:
    text = open(path, encoding="utf-8").read()
    blocks = re.split(r"\n\n+", text)
    words = []
    for b in blocks:
        m = TIME_RE.search(b)
        if not m:
            continue
        mm1, ss1, ms1, mm2, ss2, ms2 = m.groups()
        t0 = int(mm1) * 60 + int(ss1) + int(ms1) / 1000
        t1 = int(mm2) * 60 + int(ss2) + int(ms2) / 1000
        lines = b.split("\n")[1:]
        line = " ".join(l for l in lines if l.strip() and "-->" not in l)
        line = SPEAKER_RE.sub("", line)
        toks = WORD_RE.findall(line)
        if not toks:
            continue
        dur = (t1 - t0) / len(toks)
        for i, tok in enumerate(toks):
            words.append({
                "word": tok,
                "start": round(t0 + i * dur, 3),
                "end": round(t0 + (i + 1) * dur, 3),
            })
    return words


def self_test():
    import tempfile
    p = f = 0

    def chk(name, want, got):
        nonlocal p, f
        if want == got:
            print(f"  PASS  {name}")
            p += 1
        else:
            print(f"  FAIL  {name} (expected {want!r}, got {got!r})")
            f += 1

    print("vtt_parse self-test:")
    sample = (
        "WEBVTT\n"
        "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
        "00:00.533 --> 00:01.234 align:start line:69% position:10% size:75%\n"
        "MARIA: GOOD SUNDAY,\n\n"
        "00:01.234 --> 00:03.036 align:start line:69% position:10% size:65%\n"
        "NETANYAHU SAYS NUCLEAR\n\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".vtt", delete=False) as f_:
        f_.write(sample)
        path = f_.name
    words = parse_vtt(path)
    chk("parses both cues into individual words", 5, len(words))
    chk("strips the speaker label", "GOOD", words[0]["word"])
    chk("interpolates timing within a multi-word cue",
        True, words[2]["start"] < words[3]["start"] < words[4]["start"])
    chk("cue boundary respected", True, words[1]["end"] <= words[2]["start"] + 0.001)

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
