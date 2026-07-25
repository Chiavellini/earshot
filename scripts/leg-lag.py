#!/usr/bin/env python3
"""Steady-state arrival-lag between two capture legs — the honest leg A/B.

WHY THIS EXISTS (docs/LATENCY-PROOF.md): `capture-bakeoff`'s built-in
`delta_ms` is `start_skew + content_lag`, where arrival is EXTRAPOLATED from
frame 0 (`src/capture_bakeoff/mod.rs` cross_deltas). On a live HLS join, each
leg grabs a different backlog and drains it faster than real time, so the
whole-run delta measures JOIN LUCK, not steady-state client latency. That is
what produced the retracted "-6016 ms then 0 ms, both peak 1.0000" pair.

This measures the right thing: align content in several MID/LATE windows, then
compare the REAL `capture_ns` of the frames carrying that content.

    delta = arrival_B(content x) - arrival_A(content x)      [+ = B is later/behind]

Report is the median over windows plus the spread; a wide spread means the legs
were not in steady state and the number should not be quoted.

Usage:
  scripts/leg-lag.py <dir> <legA> <legB> [--windows 8] [--win-secs 4]
                     [--search-secs 30] [--start-frac 0.5]
  # <dir> holds <leg>.wav and <leg>.jsonl as written by capture-bakeoff --record-dir
"""

import argparse
import json
import sys
import wave

import numpy as np


def load_leg(d: str, leg: str):
    with wave.open(f"{d}/{leg}.wav", "rb") as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    seq, ts = [], []
    with open(f"{d}/{leg}.jsonl") as f:
        for line in f:
            r = json.loads(line)
            seq.append(r["seq"])
            ts.append(r["capture_ns"])
    order = np.argsort(np.asarray(seq))
    return pcm, sr, np.asarray(seq)[order], np.asarray(ts, dtype=np.int64)[order]


def arrival_ns(seqs, tss, sample_idx, spf):
    """capture_ns of the frame carrying `sample_idx` (spf = samples per frame)."""
    fr = int(sample_idx // spf)
    i = int(np.searchsorted(seqs, fr))
    if i >= len(seqs):
        i = len(seqs) - 1
    return int(tss[i])


def repetition_period(pcm: np.ndarray, sr: int) -> float | None:
    """Seconds between self-matches, or None if the audio is non-repetitive.

    Cross-correlation alignment silently assumes the content is DISTINCTIVE. On
    looping material every window matches at many offsets equally well, so the
    peak is arbitrary and every number downstream is fiction — measured
    2026-07-25 on a "stable" demo stream whose audio repeats every 40 s: it
    produced a physically impossible negative absolute lag and leg deltas of
    −21/−38/−59 s between legs that started simultaneously, all at peak 1.00.
    Refuse to measure rather than report that.
    """
    if len(pcm) < 60 * sr or pcm.std() < 50:
        return None
    probe = pcm[40 * sr : 50 * sr]
    if len(probe) < sr:
        return None
    n = 1 << int(np.ceil(np.log2(len(pcm) + len(probe))))
    cc = np.fft.irfft(
        np.fft.rfft(pcm - pcm.mean(), n) * np.conj(np.fft.rfft(probe - probe.mean(), n)), n
    )[: len(pcm) - len(probe) + 1]
    denom = np.linalg.norm(probe - probe.mean()) * np.std(pcm) * np.sqrt(len(probe))
    if denom <= 0:
        return None
    cc = cc / denom
    peaks: list[int] = []
    for i in np.argsort(cc)[::-1][:400]:
        if all(abs(int(i) - j) > 2 * sr for j in peaks):
            peaks.append(int(i))
        if len(peaks) >= 4:
            break
    strong = [p for p in peaks if cc[p] > 0.9]
    if len(strong) < 2:
        return None
    strong.sort()
    return min(b - a for a, b in zip(strong, strong[1:])) / sr


def fft_xcorr_lag(a: np.ndarray, b: np.ndarray) -> tuple[int, float]:
    """Best lag of `a` inside `b` (len(b) >= len(a)); returns (lag, normalized peak)."""
    n = 1 << int(np.ceil(np.log2(len(b) + len(a))))
    fa = np.fft.rfft(a - a.mean(), n)
    fb = np.fft.rfft(b - b.mean(), n)
    cc = np.fft.irfft(fb * np.conj(fa), n)[: len(b) - len(a) + 1]
    k = int(np.argmax(cc))
    # normalize against the matched slice so the peak is interpretable
    seg = b[k : k + len(a)]
    denom = np.linalg.norm(a - a.mean()) * np.linalg.norm(seg - seg.mean())
    peak = float(cc[k] / denom) if denom > 0 else 0.0
    return k, peak


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("legA")
    ap.add_argument("legB")
    ap.add_argument("--windows", type=int, default=8)
    ap.add_argument("--win-secs", type=float, default=4.0)
    ap.add_argument("--search-secs", type=float, default=30.0)
    ap.add_argument("--start-frac", type=float, default=0.5, help="ignore this fraction of the run (join transient)")
    a = ap.parse_args()

    pa, sr, sqa, tsa = load_leg(a.dir, a.legA)
    pb, srb, sqb, tsb = load_leg(a.dir, a.legB)
    if sr != srb:
        sys.exit(f"sample-rate mismatch {sr} vs {srb}")
    spf = 320 if sr == 16000 else sr // 50

    period = repetition_period(pa, sr)
    if period is not None:
        print(f"{a.legA} -> {a.legB}")
        print(f"\nVERDICT: NOT MEASURABLE — reference audio repeats every ~{period:.1f} s.")
        print("Cross-correlation cannot align looping content: many offsets match equally")
        print("well, so any lag reported here would be fiction. Use a stream with real,")
        print("non-repetitive audio (news/speech), not a demo loop.")
        return

    # Coarse global alignment on 100 Hz decimated envelopes. Probe with a MIDDLE
    # slice of A, not its head: legs join a live stream at different points, so
    # A's early content may simply not exist in B (and vice versa). A middle
    # probe is present in both regardless of which leg joined earlier, and lets
    # `coarse` come out negative when B joined later in content.
    dec = sr // 100
    da = np.abs(pa[: len(pa) // dec * dec]).reshape(-1, dec).mean(1)
    db = np.abs(pb[: len(pb) // dec * dec]).reshape(-1, dec).mean(1)
    p0 = len(da) * 3 // 8
    probe = da[p0 : p0 + max(len(da) // 4, 1)]
    if len(probe) >= len(db):
        sys.exit("legs too dissimilar in length for coarse alignment")
    k, cpeak = fft_xcorr_lag(probe, db)
    coarse = (k - p0) * dec  # content at A index i sits at B index i + coarse
    print(f"# coarse content offset: {coarse/sr:+.3f} s (peak {cpeak:.2f})", file=sys.stderr)

    win = int(a.win_secs * sr)
    margin = int(a.search_secs * sr)
    lo = int(len(pa) * a.start_frac)
    hi = len(pa) - win - 1
    if hi <= lo:
        sys.exit("run too short for the requested start-frac/win-secs")

    rows = []
    for p in np.linspace(lo, hi, a.windows).astype(int):
        seg = pa[p : p + win]
        b0 = max(0, p + coarse - margin)
        b1 = min(len(pb), p + coarse + margin + win)
        if b1 - b0 < win + 2:
            continue
        k, peak = fft_xcorr_lag(seg, pb[b0:b1])
        bidx = b0 + k
        da_ns = arrival_ns(sqa, tsa, p, spf)
        db_ns = arrival_ns(sqb, tsb, bidx, spf)
        rows.append((p / sr, (db_ns - da_ns) / 1e6, peak))

    good = [r for r in rows if r[2] > 0.5]
    print(f"{a.legA} -> {a.legB}   (+ = {a.legB} arrives LATER / is further behind)")
    print(f"{'win@s':>8} {'delta_ms':>10} {'peak':>6}")
    for t, d, pk in rows:
        print(f"{t:8.1f} {d:10.1f} {pk:6.2f}{'' if pk > 0.5 else '   (weak, ignored)'}")
    if not good:
        print("\nVERDICT: NOT RESOLVABLE — no window aligned above peak 0.5")
        return
    ds = np.array([r[1] for r in good])
    spread = float(ds.max() - ds.min())
    print(f"\nn={len(ds)}  median={np.median(ds):+.1f} ms  IQR={np.percentile(ds,75)-np.percentile(ds,25):.1f} ms  spread={spread:.1f} ms")
    if spread > 500:
        print("VERDICT: NOT STEADY — spread >500 ms across windows; do not quote a single number.")
    else:
        print(f"VERDICT: {a.legB} is {abs(np.median(ds)):.0f} ms {'BEHIND' if np.median(ds) > 0 else 'AHEAD OF'} {a.legA} in steady state.")


if __name__ == "__main__":
    main()
