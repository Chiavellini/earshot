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
    ultra   <- "ultramaritime"       (2)   substring of a closed compound
    shutdown<- "shoot down"          (1)

(An earlier line here claimed `orion <- "inventory on"` — RETRACTED, did not
reproduce on the CTC engine; see BAKEOFF §META screen.)

while distinctive words were already near-perfect (foundry 11/0, government 13/1,
panther lake 3/0). So detection quality is a KEYWORD-RISK problem, not a global
tuning problem, and the useful output is a per-keyword verdict:

  CLEAN           no false fires on this corpus
  SUBSTRING       the keyword is contained in a longer spoken word. Under Kalshi
                  Q4 semantics plurals/possessives count but substrings of closed
                  compounds and verb forms do NOT, so these fire on things the
                  market will not settle YES on. Rarely fixable by threshold.
  GROUND-TRUTH    the keyword really was spoken there — the manifest missed it,
                  so this is a scoring artifact, not a detector error.
  VARIANT-CORRECT a configured NON-phrase variant was genuinely spoken (Ford
                  07-28: `autonomy` scored 6 PHONETIC that were the detector
                  correctly matching the variant "autonomous"). Detector right;
                  headline FA wrong. Requires --config to detect.
  PHONETIC        something else was said. Printed with context for review;
                  often threshold-fixable, but a true homophone is not.

Usage:
  scripts/collision-screen.py <bench-report.json> [--config <arm/screen cfg>]
                              [--transcripts corpus/transcripts] [--window 2.5]
  scripts/collision-screen.py --check      # embedded fixture self-test
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


def _spoken_match(term, toks):
    """The term (or its Q4-equivalent plural, single-word only) is present in
    the context tokens. Returns the matched surface form or None."""
    parts = term.lower().split()
    if all(p in toks for p in parts):
        return term.lower()
    if len(parts) == 1:
        root = parts[0]
        plurals = {root + "s", root + "es", root + "'s", root + "s'"}
        return next((w for w in toks if w in plurals), None)
    return None


def classify(phrase, ctx_words, variants=None):
    """Return (verdict, culprit). Mechanical classes only — phonetic collisions
    are surfaced for review rather than guessed at.

    Q4 semantics (VERIFICATIONS.md): plurals and possessives ARE the same
    mention; substrings of closed compounds and verb forms are NOT. So
    `tailwinds` is a true hit while `ultramaritime` and `recorded` are not — the
    distinction is the whole point of this classifier and must not be collapsed.

    `variants` (from --config): the keyword's configured variants list. The
    detector matches VARIANTS, not the phrase — classifying against the phrase
    alone mislabelled 6 correct `autonomous` matches as PHONETIC on the Ford
    07-28 screen (BAKEOFF §Ford pre-arm screen). Phrase is checked first so a
    plural-of-phrase stays GROUND-TRUTH; a non-phrase variant match is
    VARIANT-CORRECT.
    """
    toks = [w for w, _ in ctx_words]
    hit = _spoken_match(phrase, toks)
    if hit:
        return "GROUND-TRUTH", hit
    for var in variants or []:
        if var.lower() == phrase.lower():
            continue
        hit = _spoken_match(var, toks)
        if hit:
            return "VARIANT-CORRECT", hit
    root = phrase.lower().split()[0]
    plurals = {root + "s", root + "es", root + "'s", root + "s'"}
    for w in toks:
        if len(w) > len(root) and root in w and len(root) >= 4 and w not in plurals:
            return "SUBSTRING", w
    return "PHONETIC", None


def load_variants(cfg_path):
    """phrase → variants list from an arm/screen config's [[keywords]]."""
    import tomllib
    with open(cfg_path, "rb") as fh:
        cfg = tomllib.load(fh)
    return {k["phrase"]: (k.get("variants") or [k["phrase"]])
            for k in cfg.get("keywords", [])}


def check():
    """Embedded fixture self-test (recon-worklist convention)."""
    ctx = [("the", 0.0), ("autonomous", 1.0), ("vehicles", 2.0)]
    variants = ["autonomy", "autonomys", "autonomous"]
    v, c = classify("autonomy", ctx, variants)
    assert (v, c) == ("VARIANT-CORRECT", "autonomous"), (v, c)
    # phrase-only control: without --config the old behavior is preserved
    v, c = classify("autonomy", ctx)
    assert v == "PHONETIC", (v, c)
    # plural-of-phrase stays GROUND-TRUTH even when it is also a variant
    v, c = classify("tailwind", [("tailwinds", 0.0)], ["tailwind", "tailwinds"])
    assert (v, c) == ("GROUND-TRUTH", "tailwinds"), (v, c)
    # substring unaffected
    v, c = classify("ultra", [("ultramaritime", 0.0)], ["ultra", "ultras"])
    assert (v, c) == ("SUBSTRING", "ultramaritime"), (v, c)
    print("collision-screen --check: OK (variant-correct, phrase-only control, "
          "plural precedence, substring)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report", nargs="?")
    ap.add_argument("--config", help="arm/screen config TOML — enables "
                    "variant-aware classification (VARIANT-CORRECT)")
    ap.add_argument("--transcripts", default="corpus/transcripts")
    ap.add_argument("--window", type=float, default=2.5)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    if a.check:
        check()
        return 0
    if not a.report:
        ap.error("report required (or --check)")

    variants_by_phrase = load_variants(a.config) if a.config else {}
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
            verdict, culprit = classify(phrase, ctx, variants_by_phrase.get(phrase))
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
        elif all(v in ("GROUND-TRUTH", "VARIANT-CORRECT") for v in vs):
            verdict = (f"VARIANT-CORRECT — configured variant '{dom}' really "
                       "spoken; detector right, headline FA wrong")
        else:
            verdict = "PHONETIC — review context below"
            if not variants_by_phrase:
                verdict += " (no --config: variant matches score PHONETIC)"
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
