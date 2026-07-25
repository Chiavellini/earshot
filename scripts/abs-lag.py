#!/usr/bin/env python3
"""Absolute lag of each capture leg behind the ENCODER, via segment PDT.

The measurement `capture-bakeoff` structurally cannot make: it compares legs to
each OTHER, so any cost every leg shares is invisible (docs/LATENCY-PROOF.md —
the 0 ms acquisition proof compared two ffmpeg legs and was blind to ffmpeg's
own cost). Here the reference is the CDN's own `EXT-X-PROGRAM-DATE-TIME`.

Method: download one segment whose PDT lands inside the capture window, decode
it, cross-correlate it into each leg's recording to find where that content sits,
then read the REAL `capture_ns` of the frame carrying it:

    absolute_lag = arrival_utc(content) - (segment_PDT + EXTINF)

i.e. how far behind the encoder our samples are — directly comparable to the
competitor's lead measured in docs/LATENCY-PROOF.md §THE GAP IS LOCATED.

Clock note: capture_ns is CLOCK_MONOTONIC_RAW (`src/clock.rs`), which chrony does
NOT slew. Anchor with CLOCK_MONOTONIC_RAW — a CLOCK_MONOTONIC anchor is wrong by
seconds on a long-uptime box.

Usage: scripts/abs-lag.py <m3u8> <record-dir> <leg> [<leg> ...]
"""

import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from datetime import datetime, timezone

import numpy as np

UA = "Mozilla/5.0 (X11; Linux x86_64) earshot-abs-lag/1"


def fetch(url: str) -> bytes:
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20
    ).read()


def resolve(url: str) -> str:
    body = fetch(url).decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" not in body:
        return url
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-STREAM-INF"):
            for c in lines[i + 1 :]:
                c = c.strip()
                if c and not c.startswith("#"):
                    if c.startswith("http"):
                        return c
                    if c.startswith("/"):
                        return re.match(r"(https?://[^/]+)", url).group(1) + c
                    return url.rsplit("/", 1)[0] + "/" + c
    return url


def segments(media_url: str):
    body = fetch(media_url).decode("utf-8", "replace")
    base = media_url.rsplit("/", 1)[0]
    out, pdt, dur = [], None, None
    for ln in body.splitlines():
        ln = ln.strip()
        if ln.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pdt = ln.split(":", 1)[1]
        elif ln.startswith("#EXTINF:"):
            dur = float(ln.split(":", 1)[1].rstrip(","))
        elif ln and not ln.startswith("#"):
            u = ln if ln.startswith("http") else (base + "/" + ln)
            if pdt and dur:
                out.append((pdt, dur, u))
            pdt = None
    return out


def decode(url: str) -> np.ndarray:
    raw = fetch(url)
    with tempfile.NamedTemporaryFile(suffix=".ts") as f:
        f.write(raw)
        f.flush()
        p = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", f.name, "-vn", "-f", "s16le",
             "-ac", "1", "-ar", "16000", "-"],
            capture_output=True,
        )
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32)


def load_leg(d, leg):
    with wave.open(f"{d}/{leg}.wav", "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    seq, ts = [], []
    with open(f"{d}/{leg}.jsonl") as f:
        for line in f:
            r = json.loads(line)
            seq.append(r["seq"])
            ts.append(r["capture_ns"])
    o = np.argsort(np.asarray(seq))
    return pcm, np.asarray(seq)[o], np.asarray(ts, dtype=np.int64)[o]


def find(needle: np.ndarray, hay: np.ndarray):
    n = 1 << int(np.ceil(np.log2(len(hay) + len(needle))))
    fa = np.fft.rfft(needle - needle.mean(), n)
    fb = np.fft.rfft(hay - hay.mean(), n)
    cc = np.fft.irfft(fb * np.conj(fa), n)[: len(hay) - len(needle) + 1]
    k = int(np.argmax(cc))
    seg = hay[k : k + len(needle)]
    den = np.linalg.norm(needle - needle.mean()) * np.linalg.norm(seg - seg.mean())
    return k, float(cc[k] / den) if den > 0 else 0.0


def main() -> None:
    m3u8, rec = sys.argv[1], sys.argv[2]
    legs = sys.argv[3:]
    raw = time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
    utc = time.time()

    def to_utc(ns):
        return utc + (ns / 1e9 - raw)

    media = resolve(m3u8)
    segs = segments(media)
    if not segs:
        sys.exit("no PDT segments in playlist")

    # probe leg lengths so we can pick a segment inside the capture window
    first = load_leg(rec, legs[0])
    t_lo, t_hi = to_utc(first[2][0]), to_utc(first[2][-1])
    cands = [
        s for s in segs
        if t_lo + 30 < datetime.fromisoformat(s[0].replace("Z", "+00:00")).timestamp() < t_hi - 30
    ]
    if not cands:
        sys.exit(f"no segment PDT inside capture window "
                 f"{datetime.fromtimestamp(t_lo, timezone.utc):%H:%M:%S}–"
                 f"{datetime.fromtimestamp(t_hi, timezone.utc):%H:%M:%S} "
                 f"(playlist spans {segs[0][0]}..{segs[-1][0]})")
    pdt_s, dur, url = cands[len(cands) // 2]
    pdt = datetime.fromisoformat(pdt_s.replace("Z", "+00:00")).timestamp()
    ref = decode(url)
    print(f"reference segment PDT={pdt_s} EXTINF={dur}s  decoded {len(ref)/16000:.2f}s")
    print(f"content ends at encoder time {datetime.fromtimestamp(pdt+dur, timezone.utc):%H:%M:%S.%f}\n")
    print(f"{'leg':>18} {'abs_lag_s':>10} {'peak':>6}")

    for leg in legs:
        pcm, sq, ts = load_leg(rec, leg)
        k, peak = find(ref, pcm)
        end_sample = k + len(ref)
        i = int(np.searchsorted(sq, end_sample // 320))
        i = min(i, len(sq) - 1)
        arrival = to_utc(int(ts[i]))
        print(f"{leg:>18} {arrival - (pdt + dur):10.3f} {peak:6.2f}"
              f"{'' if peak > 0.5 else '   (weak — not trustworthy)'}")
    print("\nabs_lag = when the segment's LAST audio reached our ring, minus when the "
          "encoder stamped its end.\nLower is fresher. Differences between legs are the "
          "client cost; the floor is the venue's own edge lag.")


if __name__ == "__main__":
    main()
