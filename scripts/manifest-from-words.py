#!/usr/bin/env python3
"""Build a bench ground-truth manifest from an existing word-timestamp
transcript (corpus/transcripts/<id>.words.json, produced by
transcript-fetch.py) instead of re-running alignment.

Companion to corpus-align.py (which aligns from audio); this variant is for
calls whose transcript already exists — e.g. the webcast wideband corpus
(docs/BAKEOFF.md §Corpus, W5). Occurrence semantics match corpus-align.py:
an occurrence spans the matched variant's first→last word timestamps.

Matching: words are normalized (lowercase, '&'→'and', punctuation stripped);
multi-word variants match consecutive word sequences; a squashed comparison
(no spaces) also matches single tokens like "AT&T" against "at and t".

Usage:
  manifest-from-words.py --id lmt-q2-2026 --split dev \
      --keywords '[{"phrase":"nasa","variants":["nasa"]}, ...]' \
      [--corpus corpus] [--audio wideband/lmt-q2-2026.wav]
Writes corpus/manifests/<id>.toml (refuses to overwrite unless --force).
"""
import argparse, json, re, sys
from pathlib import Path

def norm(w: str) -> str:
    w = w.lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]", "", w)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--split", required=True, choices=["dev", "holdout"])
    ap.add_argument("--keywords", required=True, help="JSON [{phrase,variants}]")
    ap.add_argument("--corpus", default="corpus")
    ap.add_argument("--audio", default=None, help="relative to corpus root")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    corpus = Path(a.corpus)
    words = json.load(open(corpus / "transcripts" / f"{a.id}.words.json"))
    audio = a.audio or f"wideband/{a.id}.wav"
    out = corpus / "manifests" / f"{a.id}.toml"
    if out.exists() and not a.force:
        sys.exit(f"refusing to overwrite {out} (use --force)")

    toks = [(norm(w["word"]), w["start"], w["end"]) for w in words if norm(w["word"])]
    occs = []
    for kw in json.loads(a.keywords):
        phrase = kw["phrase"]
        variants = kw.get("variants") or [phrase]
        for var in variants:
            seq = [norm(x) for x in var.split() if norm(x)]
            if not seq:
                continue
            squash = "".join(seq)
            n = len(seq)
            for i in range(len(toks)):
                # consecutive-sequence match
                if n > 1 and i + n <= len(toks) and [t[0] for t in toks[i:i+n]] == seq:
                    occs.append((phrase, toks[i][1], toks[i+n-1][2]))
                # single-token match (covers squashed compounds like "at&t")
                elif toks[i][0] == squash:
                    occs.append((phrase, toks[i][1], toks[i][2]))
    # dedupe overlapping matches of the same phrase (variant overlap), sort by onset
    occs.sort(key=lambda o: (o[1], o[0]))
    dedup = []
    for ph, s, e in occs:
        if dedup and dedup[-1][0] == ph and s < dedup[-1][2]:
            continue
        dedup.append((ph, s, e))

    with open(out, "w") as f:
        f.write(f'[call]\nid    = "{a.id}"\naudio = "{audio}"\nsplit = "{a.split}"\n')
        for ph, s, e in dedup:
            f.write(f'\n[[occurrences]]\nphrase   = "{ph}"\nonset_ms = {int(s*1000)}\nend_ms   = {int(e*1000)}\n')
    counts = {}
    for ph, _, _ in dedup:
        counts[ph] = counts.get(ph, 0) + 1
    print(f"wrote {out}: {len(dedup)} occurrences  {json.dumps(counts, sort_keys=True)}")

if __name__ == "__main__":
    main()
