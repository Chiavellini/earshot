#!/usr/bin/env python3
"""Keyword collision screen — WHY each false alarm fired, per keyword.

Run this on an event's keyword list BEFORE arming it. A bench report tells you
*that* a keyword false-fires; this tells you *what the detector actually heard*,
which is the difference between a fixable threshold and an unwinnable market.

Method: each `fa_autopsy` entry from `bench-detector` is
`"<phrase> <tier> <call>@<pos>ms"` (`src/bench/mod.rs`). We look that position up
in the call's word-level transcript (`corpus/transcripts/<call>.words.json`,
produced by scripts/corpus-align.py) and print the words that were really being
spoken there.

Measured on the webcast corpus 2026-07-24 — 48 of 56 false alarms were genuine,
and they were NOT diffuse. They collapsed to a handful of named collisions:

    trump   <- "strong"              (8)   phonetic near-miss
    record  <- "recorded"            (6)   morphological: verb form, not a mention
    weather <- "whether"             (4)   TRUE HOMOPHONE — unwinnable
    orion   <- "inventory on"        (2)   word-boundary artifact
    ultra   <- "ultramaritime"       (2)   substring of a closed compound
    shutdown<- "shoot down"          (1)

while distinctive words were already near-perfect (foundry 11/0, government 13/1,
panther lake 3/0). So detection quality is a KEYWORD-RISK problem, not a global
tuning problem, and the useful output is a per-keyword verdict:

  CLEAN        no false fires on this corpus
  SUBSTRING    the keyword is contained in a longer spoken word. Under Kalshi Q4
               semantics plurals/possessives count but substrings of closed
               compounds and verb forms do NOT, so these fire on things the
               market will not settle YES on. Rarely fixable by threshold.
  GROUND-TRUTH the keyword really was spoken there — the manifest missed it, so
               this is a scoring artifact, not a detector error.
  PHONETIC     something else was said. Printed with context for review; often
               threshold-fixable, but a true homophone (weather/whether) is not.

Usage:
  scripts/collision-screen.py <bench-report.json> [--transcripts corpus/transcripts]
                              [--window 2.5]
"""

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict

ENTRY = re.compile(r"^(?P<phrase>.+?) (?P<tier>Fast|Confirmed) (?P<call>\S+)@(?P<ms>\d+)ms$")


def load_transcripts(d):
    out = {}
    for p in glob.glob(os.path.join(d, "*.words.json")):
        call = os.path.basename(p)[: -len(".words.json")]
        try:
            out[call] = json.load(open(p))
        except Exception as e:
            print(f"warn: {p}: {e}", file=sys.stderr)
    return out


def window_words(words, t_s, win):
    return [
        (w["word"].strip(" .,!?;:\"'’“”").lower(), w["start"])
        for w in words
        if abs(w.get("start", -1e9) - t_s) <= win
    ]


def classify(phrase, ctx_words):
    """Return (verdict, culprit). Mechanical classes only — phonetic collisions
    are surfaced for review rather than guessed at.

    Q4 semantics (VERIFICATIONS.md): plurals and possessives ARE the same
    mention; substrings of closed compounds and verb forms are NOT. So
    `tailwinds` is a true hit while `ultramaritime` and `recorded` are not — the
    distinction is the whole point of this classifier and must not be collapsed.
    """
    toks = [w for w, _ in ctx_words]
    parts = phrase.lower().split()
    # Multi-word phrases need EVERY word present, not just the first — otherwise
    # "market share" scores a hit on a bare "market".
    if all(p in toks for p in parts):
        return "GROUND-TRUTH", phrase.lower()
    root = parts[0]
    plurals = {root + "s", root + "es", root + "'s", root + "s'"}
    if len(parts) == 1:
        hit = next((w for w in toks if w in plurals), None)
        if hit:
            return "GROUND-TRUTH", hit          # plural counts as the mention
    for w in toks:
        if len(w) > len(root) and root in w and len(root) >= 4 and w not in plurals:
            return "SUBSTRING", w
    return "PHONETIC", None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--transcripts", default="corpus/transcripts")
    ap.add_argument("--window", type=float, default=2.5)
    a = ap.parse_args()

    rep = json.load(open(a.report))
    tr = load_transcripts(a.transcripts)
    if not tr:
        print(f"no transcripts in {a.transcripts} — run scripts/corpus-align.py first")
        return 1

    hours = rep.get("corpus_hours") or 0.0
    per_kw = defaultdict(list)
    rows_by_kw = defaultdict(list)
    skipped = 0

    for cell in rep.get("cells", []):
        for e in cell.get("fa_autopsy", []) or []:
            m = ENTRY.match(e)
            if not m:
                continue
            call, ms, phrase = m["call"], int(m["ms"]), m["phrase"]
            if call not in tr:
                skipped += 1
                continue
            ctx = window_words(tr[call], ms / 1000.0, a.window)
            verdict, culprit = classify(phrase, ctx)
            per_kw[phrase].append((verdict, culprit))
            rows_by_kw[phrase].append((call, ms // 1000, verdict, culprit,
                                       " ".join(w for w, _ in ctx[:10])))

    # per-keyword recall/precision from the scored rows
    tp = defaultdict(int)
    for cell in rep.get("cells", []):
        for r in cell.get("rows", []):
            tp[r["phrase"]] += r.get("true_positives", 0)

    print(f"corpus_hours={hours:.3f}  calls={rep.get('calls')}  "
          f"transcripts={len(tr)}" + (f"  (skipped {skipped} FA with no transcript)" if skipped else ""))
    print()
    order = sorted(per_kw, key=lambda k: -len(per_kw[k]))
    print(f"{'keyword':<18}{'TP':>4}{'FA':>4}  verdict")
    for kw in order:
        vs = [v for v, _ in per_kw[kw]]
        culprits = defaultdict(int)
        for v, c in per_kw[kw]:
            if c:
                culprits[c] += 1
        dom = max(culprits.items(), key=lambda x: x[1])[0] if culprits else None
        # a keyword's verdict is its worst mechanical class, else PHONETIC
        if "SUBSTRING" in vs:
            verdict = f"SUBSTRING — fires on '{dom}' (Q4: not a mention)"
        elif all(v == "GROUND-TRUTH" for v in vs):
            verdict = f"GROUND-TRUTH — '{dom}' really spoken; manifest missed it"
        else:
            verdict = "PHONETIC — review context below"
        print(f"{kw:<18}{tp.get(kw,0):>4}{len(vs):>4}  {verdict}")

    print()
    print("── context for review ─────────────────────────────────────────────")
    for kw in order:
        print(f"\n{kw}  (TP={tp.get(kw,0)}, FA={len(per_kw[kw])})")
        for call, s, verdict, culprit, ctx in sorted(rows_by_kw[kw])[:8]:
            tag = verdict if verdict != "PHONETIC" else "phonetic"
            print(f"   {call.split('-')[0]:<5} {s:>5}s  {tag:<12} {ctx[:64]}")

    clean = [r["phrase"] for cell in rep.get("cells", []) for r in cell.get("rows", [])
             if r.get("false_alarms", 0) == 0 and r.get("true_positives", 0) > 0]
    if clean:
        print("\nCLEAN (TP>0, zero false fires): " + ", ".join(sorted(set(clean))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
