#!/usr/bin/env python3
"""run-transcribe.py — word-timestamped reference transcript of a banked run, per source leg.

WHY (2026-08-18, detection-accuracy program D0). Every run banks its audio as
`capture/wav/source-<S>-segment-<N>-16000hz.wav` pieces (audio_evidence tape),
one series per ingress leg, split at sequence discontinuities. Detections carry
`audio_pos_ms` on the LEG's fed-sample timeline, and the banked frames ARE the
fed frames (banked prefix == detector-visible prefix by construction), so the
concatenation of a leg's segments in seq order is exactly the detector's
timeline: a word at t seconds in that concatenation is a word at
`audio_pos_ms ~= 1000*t` for that leg. No offset estimation is needed — unlike
`score-precision.py`'s legacy `--audio` path, which scores against a
separately-captured call.wav and must first solve for the origin shift.

Output: one `<out>/<run_id>/<source_label>.words.json` per leg:
  {"run_id","event_id","source","source_label","sample_rate_hz","total_samples",
   "model","segments":[{segment,first_seq,last_seq,sample_count,offset_samples,path}],
   "words":[{"w":str,"start":sec,"end":sec,"p":prob}], "text": str}
Times are seconds on the leg timeline. Nothing under the run dir is written.

usage:
  run-transcribe.py --run-dir var/runs/<E>/<R> --out <dir> [--model deepdml/faster-whisper-large-v3-turbo-ct2]
                    [--sources 0,1] [--threads 8] [--beam 1]
"""
import argparse, json, os, sys, time, wave
from pathlib import Path

import numpy as np


def load_leg(manifest, source):
    segs = [s for s in manifest["wav_segments"] if int(s["source"]) == source]
    segs.sort(key=lambda s: int(s["segment"]))
    chunks, meta, off = [], [], 0
    rate = None
    for s in segs:
        p = s["path"]
        if not os.path.exists(p):
            # manifests carry absolute paths from the host that wrote them; fall back to the run dir
            p = str(Path(RUN_DIR) / "capture" / "wav" / Path(p).name)
        w = wave.open(p, "rb")
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, p
        r = w.getframerate()
        rate = rate or r
        assert r == rate, (r, rate, p)
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        w.close()
        meta.append({"segment": s["segment"], "first_seq": s["first_seq"], "last_seq": s["last_seq"],
                     "sample_count": int(len(pcm)), "offset_samples": off, "path": os.path.basename(p)})
        off += len(pcm)
        chunks.append(pcm)
    if not chunks:
        return None, rate, meta
    return np.concatenate(chunks), rate, meta


def main():
    global RUN_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="deepdml/faster-whisper-large-v3-turbo-ct2")
    ap.add_argument("--sources", default=None, help="comma list of source ids (default: all with audio)")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--beam", type=int, default=1)
    a = ap.parse_args()
    RUN_DIR = a.run_dir
    man = json.load(open(os.path.join(a.run_dir, "capture", "audio.manifest.json")))
    run_id = man["identity"]["run_id"]; event_id = man["identity"]["event_id"]
    labels = {int(k): v for k, v in man.get("source_labels", {}).items()}
    sources = sorted({int(s["source"]) for s in man["wav_segments"]})
    if a.sources:
        want = {int(x) for x in a.sources.split(",")}
        sources = [s for s in sources if s in want]
    outdir = Path(a.out) / run_id
    outdir.mkdir(parents=True, exist_ok=True)
    from faster_whisper import WhisperModel
    t0 = time.time()
    m = WhisperModel(a.model, device="cpu", compute_type="int8", cpu_threads=a.threads)
    print(f"model {a.model} loaded in {time.time()-t0:.1f}s", file=sys.stderr)
    for src in sources:
        pcm, rate, meta = load_leg(man, src)
        label = labels.get(src, f"src{src}")
        if pcm is None:
            print(f"source {src} ({label}): no audio", file=sys.stderr); continue
        secs = len(pcm) / rate
        print(f"source {src} ({label}): {secs/60:.1f} min in {len(meta)} segments", file=sys.stderr)
        audio = pcm.astype(np.float32) / 32768.0
        if rate != 16000:
            raise SystemExit(f"leg sample rate {rate} != 16000; resample first")
        t1 = time.time()
        segs, info = m.transcribe(audio, language="en", word_timestamps=True, beam_size=a.beam,
                                  vad_filter=False, condition_on_previous_text=False)
        words, text = [], []
        for s in segs:
            text.append(s.text)
            for wd in (s.words or []):
                words.append({"w": wd.word.strip(), "start": round(wd.start, 3), "end": round(wd.end, 3),
                              "p": round(float(wd.probability), 3)})
        dt = time.time() - t1
        out = {"run_id": run_id, "event_id": event_id, "source": src, "source_label": label,
               "sample_rate_hz": rate, "total_samples": int(len(pcm)), "model": a.model,
               "wall_secs": round(dt, 1), "segments": meta, "words": words, "text": "".join(text).strip()}
        p = outdir / f"{label}.words.json"
        json.dump(out, open(p, "w"))
        print(f"  -> {p}  words={len(words)}  wall={dt:.0f}s  xRT={secs/dt:.1f}", file=sys.stderr)


if __name__ == "__main__":
    main()
