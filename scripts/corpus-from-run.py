#!/usr/bin/env python3
"""corpus-from-run.py — turn a banked run + its per-leg reference transcript into a
bake-off corpus entry: one 16 kHz wideband WAV (the leg's segments concatenated in
seq order == the detector's audio_pos_ms timeline) + a manifest with every
occurrence of every ARMED keyword (Q4 semantics: plurals/possessives count).

WHY (2026-08-18, detection-accuracy program D0). The bake-off corpus was 5.5 h
of other issuers' calls; the events we actually trade bank ~8 leg-hours a day of
real speakers, real venue codec, and the exact market words. Every model change
must be measured on THIS material. `split` is pre-committed at creation and
never flipped (docs/BAKEOFF.md §Corpus).

usage:
  corpus-from-run.py --run-dir var/runs/<E>/<R> --words <label>.words.json \\
      --id <call-id> --split dev|holdout [--corpus corpus] [--config <override.toml>]
"""
import argparse, json, os, re, sys, tomllib, wave
from pathlib import Path

REPO = os.environ.get("EARSHOT_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")


def norm_words(t):
    return WORD.findall(t.lower())


def inflected(word, target):
    if word == target or word in (target + "s", target + "es", target + "'s"):
        return True
    return target.endswith("y") and word == target[:-1] + "ies"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--words", required=True, help="run-transcribe.py output for ONE leg")
    ap.add_argument("--id", required=True)
    ap.add_argument("--split", required=True, choices=["dev", "holdout"])
    ap.add_argument("--corpus", default="corpus")
    ap.add_argument("--config", help="override the RunPlan config (e.g. a copy with obsolete keys stripped)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    os.chdir(REPO)
    run = Path(a.run_dir)
    plan = json.load(open(run / "control" / "run-plan.json"))
    cfg = tomllib.load(open(a.config or plan["config_path"], "rb"))
    tr = json.load(open(a.words))
    assert tr["run_id"] == plan["run_id"], "transcript is for another run"
    words = tr["words"]
    flat, idx = [], []
    for i, w in enumerate(words):
        for t in norm_words(w["w"]):
            flat.append(t); idx.append(i)
    occ = []
    for k in cfg["keywords"]:
        variants = k.get("variants") or [k["phrase"]]
        seen = set()
        for v in variants:
            vw = norm_words(v)
            L = len(vw)
            if not L:
                continue
            for s in range(0, len(flat) - L + 1):
                seg = flat[s:s + L]
                if seg[:-1] == vw[:-1] and inflected(seg[-1], vw[-1]):
                    on = int(round(words[idx[s]]["start"] * 1000)); en = int(round(words[idx[s + L - 1]]["end"] * 1000))
                    key = (on // 100)
                    if key in seen:
                        continue
                    seen.add(key)
                    occ.append((on, en, k["phrase"]))
    occ.sort()
    # audio: concatenate the leg's segments (paths relative to the run dir)
    out_wav = Path(a.corpus) / "wideband" / f"{a.id}.wav"
    out_man = Path(a.corpus) / "manifests" / f"{a.id}.toml"
    if out_man.exists():
        sys.exit(f"refusing to overwrite {out_man} (split is pre-committed; delete by hand if you mean it)")
    lines = [f'[call]\nid    = "{a.id}"\naudio = "wideband/{a.id}.wav"\nsplit = "{a.split}"\n',
             f'# generated {json.dumps({"run_id": tr["run_id"], "event_id": tr["event_id"], "leg": tr["source_label"], "reference": tr["model"]})}\n']
    for on, en, ph in occ:
        lines.append(f'[[occurrences]]\nphrase   = "{ph}"\nonset_ms = {on}\nend_ms   = {en}\n')
    print(f"{a.id}: {len(occ)} occurrences of {len({p for _,_,p in occ})} phrases over {tr['total_samples']/tr['sample_rate_hz']/60:.1f} min ({tr['source_label']})")
    if a.dry_run:
        return
    out_wav.parent.mkdir(parents=True, exist_ok=True); out_man.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(tr["sample_rate_hz"])
        total = 0
        for seg in tr["segments"]:
            p = run / "capture" / "wav" / seg["path"]
            r = wave.open(str(p), "rb")
            assert r.getframerate() == tr["sample_rate_hz"] and r.getnchannels() == 1
            n = r.getnframes(); assert n == seg["sample_count"], (p, n, seg["sample_count"])
            w.writeframes(r.readframes(n)); total += n; r.close()
    assert total == tr["total_samples"], (total, tr["total_samples"])
    out_man.write_text("\n".join(lines))
    print(f"  -> {out_wav} ({total/tr['sample_rate_hz']/60:.1f} min), {out_man}")


if __name__ == "__main__":
    main()
