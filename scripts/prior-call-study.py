#!/usr/bin/env python3
"""prior-call-study.py — measure an ARMED config's per-word recall/precision on a
prior call of the SAME issuer (same CEO/CFO voices, same brand words), from real
audio instead of `say` TTS. The second, stronger readiness input the
detection-accuracy program (D3, 2026-08-19) adds next to variant-study.sh.

    prior-call-study.py --config configs/webcast-<slug>-arm-wc.toml \\
        --wav <prior-call 16 kHz mono s16 wav> [--words <words.json from run-transcribe.py|faster-whisper>] \\
        [--earshot target/release/earshot] [--out configs/.readiness/<base>.prior.json]

Steps: (1) reference transcript (faster-whisper large-v3-turbo via .venv-corpus,
skipped when --words is given), (2) a scratch corpus root with ONE manifest whose
occurrences are every armed keyword variant (Q4 inflections), (3)
`earshot bench-detector` with the config's own engine on that root, (4) per-word
recall / precision / FA-h into a `.prior.json` next to the TTS readiness artifact,
carrying `keywords_sha256` so a later keyword edit invalidates it exactly like the
TTS study. ADVISORY for now: posture-from-study.py does not read it yet — the
gate flip is a separate, reviewed change to the interlock.
"""
import argparse, hashlib, json, os, re, subprocess, sys, tempfile, tomllib, wave
from pathlib import Path

REPO = os.environ.get("EARSHOT_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")


def norm_words(t):
    return WORD.findall(t.lower())


def inflected(word, target):
    if word == target or word in (target + "s", target + "es", target + "'s"):
        return True
    return target.endswith("y") and word == target[:-1] + "ies"


def keywords_sha256(cfg_path):
    out = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "keywords-extract.py"), cfg_path],
                         capture_output=True, text=True, check=True).stdout
    return hashlib.sha256(out.encode()).hexdigest()


def transcribe(wav, model):
    from faster_whisper import WhisperModel
    m = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=6)
    segs, _ = m.transcribe(wav, language="en", word_timestamps=True, beam_size=1, vad_filter=False,
                           condition_on_previous_text=False)
    words = []
    for s in segs:
        for w in (s.words or []):
            words.append({"w": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3), "p": round(float(w.probability), 3)})
    return words


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--wav", required=True)
    ap.add_argument("--words")
    ap.add_argument("--earshot", default="target/release/earshot")
    ap.add_argument("--model", default="deepdml/faster-whisper-large-v3-turbo-ct2")
    ap.add_argument("--out")
    ap.add_argument("--chunk-ms", type=int, default=160)
    a = ap.parse_args()
    os.chdir(REPO)
    cfg = tomllib.load(open(a.config, "rb"))
    engine = cfg["detector"]["engine"]
    with wave.open(a.wav, "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 16000, "need 16 kHz mono s16"
        secs = w.getnframes() / 16000
    if a.words:
        words = json.load(open(a.words)); words = words.get("words", words)
    else:
        print(f"transcribing {a.wav} ({secs/60:.1f} min) with {a.model} …", file=sys.stderr)
        words = transcribe(a.wav, a.model)
    flat, idx = [], []
    for i, w in enumerate(words):
        for t in norm_words(w["w"]):
            flat.append(t); idx.append(i)
    occ = []
    for k in cfg["keywords"]:
        seen = set()
        for v in (k.get("variants") or [k["phrase"]]):
            vw = norm_words(v); L = len(vw)
            if not L: continue
            for s in range(0, len(flat) - L + 1):
                seg = flat[s:s + L]
                if seg[:-1] == vw[:-1] and inflected(seg[-1], vw[-1]):
                    on = int(round(words[idx[s]]["start"] * 1000)); en = int(round(words[idx[s + L - 1]]["end"] * 1000))
                    if on // 100 in seen: continue
                    seen.add(on // 100); occ.append((on, en, k["phrase"]))
    occ.sort()
    root = Path(tempfile.mkdtemp(prefix="prior-call-"))
    (root / "manifests").mkdir(); (root / "wideband").mkdir()
    os.symlink(os.path.abspath(a.wav), root / "wideband" / "prior.wav")
    man = ['[call]\nid    = "prior"\naudio = "wideband/prior.wav"\nsplit = "dev"\n']
    man += [f'[[occurrences]]\nphrase   = "{p}"\nonset_ms = {on}\nend_ms   = {en}\n' for on, en, p in occ]
    (root / "manifests" / "prior.toml").write_text("\n".join(man))
    out_json = root / "bench.json"
    env = dict(os.environ)
    if "ORT_DYLIB_PATH" not in env:
        for cand in Path("vendor/onnxruntime").rglob("libonnxruntime*"):
            if cand.suffix in (".dylib", ".so") or ".so." in cand.name:
                env["ORT_DYLIB_PATH"] = str(cand); break
    cmd = [a.earshot, "bench-detector", "--config", a.config, "--corpus", str(root), "--engines", engine,
           "--chunk-ms", str(a.chunk_ms), "--split", "dev", "--out", str(out_json)]
    print("bench:", " ".join(cmd), file=sys.stderr)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0 or not out_json.exists():
        sys.exit(f"bench-detector failed ({r.returncode}):\n{r.stderr[-2000:]}")
    rep = json.load(open(out_json)); cell = rep["cells"][0]
    per = {}
    for row in cell["rows"]:
        if row["tier"] != "Confirmed": continue
        per[row["phrase"]] = {"occurrences": row["true_positives"] + row["misses"], "tp": row["true_positives"],
                              "miss": row["misses"], "fp": row["false_alarms"], "recall": row["recall"],
                              "precision": row["precision"], "fa_per_hour": round(row["fa_per_hour"], 2),
                              "consumed_lag_p50_ms": row.get("consumed_lag_p50_ms")}
    base = os.path.basename(a.config)[:-5]
    result = {"config": base, "keywords_sha256": keywords_sha256(a.config), "engine": engine,
              "prior_wav": os.path.abspath(a.wav), "prior_minutes": round(secs / 60, 1),
              "reference": a.model if not a.words else "supplied", "occurrences_total": len(occ),
              "keywords": [{"ticker": k["market_ticker"], "phrase": k["phrase"], **per.get(k["phrase"], {"occurrences": 0, "tp": 0, "miss": 0, "fp": 0, "recall": None, "precision": None, "fa_per_hour": 0.0})}
                           for k in cfg["keywords"]]}
    out = a.out or f"configs/.readiness/{base}.prior.json"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    json.dump(result, open(out, "w"), indent=1)
    print(f"{'phrase':22} {'said':>4} {'tp':>3} {'miss':>4} {'fp':>3} {'recall':>7} {'prec':>6}")
    for k in result["keywords"]:
        print(f"{k['phrase']:22} {k['occurrences']:4} {k['tp']:3} {k['miss']:4} {k['fp']:3} {str(k['recall']):>7} {str(k['precision']):>6}")
    print(f"-> {out}   (advisory; not yet read by posture-from-study.py)")


if __name__ == "__main__":
    main()
