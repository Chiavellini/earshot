#!/usr/bin/env python3
"""phrase_match.py — canonical word-normalization + occurrence matching for
transcript-based pack-lag joins.

Extracted as a PATTERN from scripts/wnt-pack-lag.py's `norm()`/
`alternatives()`/`within_one_edit()`/`nth_occurrence()` (production,
read-only reference — pattern-copied here, never imported from). This is
the canonical version: `tbpn/pack_lag_tbpn.py` and `rogan/pack_lag_rogan.py`
each independently reimplemented a PARTIAL copy of this logic (Rogan's
`norm()` had no possessive/plural stripping; TBPN's matcher had no fuzzy
fallback or Kalshi count-strike parsing) — both now import from here
instead, so the three venues' matching behavior stops drifting apart as
more venues get added.

Two call shapes are supported, matching what venues have actually needed:
  - Multi-word phrases with Kalshi's "(N+ times)" count-strike syntax and
    "/"-separated alternatives (WNT's shape: `alternatives()` parses a
    Kalshi `yes_sub_title` string directly).
  - Flat single-word alternative lists (TBPN/Rogan's shape: use
    `single_word_phrases()` to lift a flat list into the phrase format
    `occurrences()`/`nth_occurrence()` expect).
"""

from __future__ import annotations

import re

WORD_RE = re.compile(r"[a-z0-9]+")
COUNT_RE = re.compile(r"\((\d+)\+\s*times\)", re.I)


def norm(tok: str) -> str:
    """Lowercase, strip punctuation, collapse plural/possessive.

    Kalshi's payout criterion counts 'the exact phrase/word, or a plural or
    possessive form' but excludes other inflections, so 'tariffs' counts and
    'tariffing' does not. Stripping a trailing s/'s matches that rule; a
    general stemmer would be wrong in the permissive direction.
    """
    t = tok.lower()
    t = re.sub(r"['’]s$", "", t)
    t = "".join(WORD_RE.findall(t))
    return t[:-1] if len(t) > 3 and t.endswith("s") else t


def within_one_edit(a: str, b: str) -> bool:
    """True if a and b differ by at most one insertion/deletion/substitution."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = j = 0
    edited = False
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        if edited:
            return False
        edited = True
        if la == lb:
            i += 1
        j += 1
    return True


def alternatives(sub_title: str) -> tuple[list[list[str]], int]:
    """'Trump (5+ times)' -> ([['trump']], 5); 'Oil / Gas' -> two phrases, 1."""
    count = 1
    m = COUNT_RE.search(sub_title)
    if m:
        count = int(m.group(1))
        sub_title = COUNT_RE.sub("", sub_title)
    phrases = []
    for alt in sub_title.split("/"):
        toks = [norm(t) for t in alt.split()]
        toks = [t for t in toks if t]
        if toks:
            phrases.append(toks)
    return phrases, count


def single_word_phrases(alt_words: list[str]) -> list[list[str]]:
    """Lift a flat alternative-word list (TBPN/Rogan's shape) into the
    phrase-list format `occurrences()`/`nth_occurrence()` expect. Each word
    is normalized here (matching `alternatives()`'s own behavior) — the
    target side and the caption side must go through norm() identically,
    or a target that itself ends in 's' (e.g. "helios" -> "helio") would
    never match its own normalized transcript occurrence. Deduplicated
    after normalization: two alternatives that collapse to the same form
    (e.g. "robotic"/"robotics" both -> "robotic") must not produce a
    double-counted hit at the same offset."""
    seen, out = set(), []
    for w in alt_words:
        n = norm(w)
        if n not in seen:
            seen.add(n)
            out.append([n])
    return out


def occurrences(words: list[dict], phrases: list[list[str]], fuzzy: bool = True):
    """All occurrences (programme-offset seconds, "exact" or "fuzzy") of any
    alternative phrase, sorted earliest-first. `words` is a list of
    {"word": str, "end": float, ...} dicts (a caption/transcript word-level
    timing track). Exact pass first; fuzzy pass (one-edit-distance, tokens
    of 5+ characters only, per within_one_edit's own guard against
    ASR-spelling drift like 'Blanche'->'Blanch') only runs if the exact
    pass finds nothing AND fuzzy=True — a fuzzy hit is always labelled, never
    silently merged with an exact one.
    """
    normed = [norm(w["word"]) for w in words]

    def scan(match):
        found = []
        for phrase in phrases:
            k = len(phrase)
            for i in range(len(normed) - k + 1):
                if all(match(normed[i + o], phrase[o]) for o in range(k)):
                    found.append((words[i + k - 1]["end"], "exact" if match is _exact else "fuzzy"))
        found.sort()
        return found

    hits = scan(_exact)
    if hits or not fuzzy:
        return hits
    return scan(_fuzzy)


def _exact(a, b):
    return a == b


def _fuzzy(a, b):
    return a == b or (len(b) >= 5 and within_one_edit(a, b))


def nth_occurrence(words: list[dict], phrases: list[list[str]], n: int, fuzzy: bool = True):
    """Programme-time end offset of the nth utterance of any alternative
    phrase. Returns (offset_or_None, how) where how is "exact"/"fuzzy"/"absent".
    """
    hits = occurrences(words, phrases, fuzzy=fuzzy)
    if len(hits) >= n:
        off, how = hits[n - 1]
        return off, how
    return None, "absent"


def all_occurrences(words: list[dict], alt_words: list[str], fuzzy: bool = False) -> list[float]:
    """Convenience for the flat-single-word shape (TBPN's original call
    site): every occurrence of any alternative, offsets only, sorted.
    fuzzy defaults to False here to preserve TBPN's original exact-only
    behavior — pass fuzzy=True explicitly to opt into the WNT-style fallback.
    """
    return [off for off, _how in occurrences(words, single_word_phrases(alt_words), fuzzy=fuzzy)]


def first_occurrence(words: list[dict], alt_words: list[str], fuzzy: bool = False):
    """Convenience for the flat-single-word shape (Rogan's original call
    site): the START offset of the first occurrence of any alternative, or
    None. Uses 'start' not 'end' to match pack_lag_rogan.py's original
    convention (a VOD-publish-anchored offset, not a live-broadcast anchor —
    the two scripts made different, each internally-consistent, choices
    about which timestamp edge to anchor on; preserved here rather than
    silently unified, since changing it would change every already-banked
    Rogan result).
    """
    alts = {norm(a) for a in alt_words}
    for w in words:
        if norm(w["word"]) in alts:
            return w["start"]
    return None


def self_test():
    p = f = 0

    def chk(name, want, got):
        nonlocal p, f
        if want == got:
            print(f"  PASS  {name}")
            p += 1
        else:
            print(f"  FAIL  {name} (expected {want!r}, got {got!r})")
            f += 1

    print("phrase_match self-test:")

    chk("norm strips possessive", "blanche", norm("Blanche's"))
    chk("norm collapses plural on long word", "tariff", norm("Tariffs"))
    chk("norm leaves short word alone", "was", norm("was"))
    chk("norm strips punctuation", "spy", norm("spy,"))

    chk("within_one_edit: identical", True, within_one_edit("blanch", "blanch"))
    chk("within_one_edit: one substitution", True, within_one_edit("blanch", "blanche"[:-1]))
    chk("within_one_edit: one insertion (ASR drift, WNT's real 2026-07-29 case)",
        True, within_one_edit("blanch", "blanche"))
    chk("within_one_edit: iran/iraq ARE only one edit apart (raw check)",
        True, within_one_edit("iran", "iraq"))
    chk("...which is exactly why the length>=5 guard lives in _fuzzy(), "
        "not in within_one_edit() itself — 'iran' is under 5 chars",
        False, _fuzzy("iran", "iraq"))

    chk("alternatives parses a count-strike suffix",
        ([["trump"]], 5), alternatives("Trump (5+ times)"))
    chk("alternatives splits '/' into separate phrases",
        ([["oil"], ["gas"]], 1), alternatives("Oil / Gas"))
    chk("alternatives handles a multi-word phrase (norm()'s trailing-s "
        "strip applies to EVERY token, including 'Glass' -> 'glas' -- "
        "inherited unchanged from wnt-pack-lag.py's own norm(), not a bug "
        "introduced here)",
        ([["liquid", "glas"]], 1), alternatives("Liquid Glass"))

    words = [
        {"word": "So", "start": 0.0, "end": 0.2},
        {"word": "Anthropic", "start": 10.0, "end": 10.5},
        {"word": "later", "start": 20.0, "end": 20.3},
        {"word": "Claude", "start": 30.0, "end": 30.4},
        {"word": "Helios'", "start": 40.0, "end": 40.6},
        {"word": "robotics", "start": 50.0, "end": 50.7},
        {"word": "Blanch", "start": 60.0, "end": 60.4},  # ASR spelling of "Blanche"
    ]

    occs = all_occurrences(words, ["anthropic", "claude"])
    chk("TBPN shape: all_occurrences finds every alternative, sorted", [10.5, 30.4], occs)
    occs = all_occurrences(words, ["helios"])
    chk("TBPN shape: matches a possessive form via norm()", [40.6], occs)
    occs = all_occurrences(words, ["robot", "robotic", "robotics"])
    chk("TBPN shape: matches a plural alternative", [50.7], occs)
    occs = all_occurrences(words, ["openai", "chatgpt"])
    chk("TBPN shape: returns [] when nothing matches", [], occs)

    fo = first_occurrence(words, ["spy,".rstrip(",")])
    chk("Rogan shape: first_occurrence with punctuation-bearing target n/a here", None, fo)
    fo = first_occurrence(words, ["anthropic"])
    chk("Rogan shape: first_occurrence uses start not end", 10.0, fo)

    off, how = nth_occurrence(words, [["blanche"]], 1)
    chk("nth_occurrence falls back to fuzzy for an ASR-spelling miss", 60.4, off)
    chk("fuzzy hit is labelled, not silently merged with exact", "fuzzy", how)

    off, how = nth_occurrence(words, [["iran"]], 1)
    chk("nth_occurrence: absent when nothing plausibly matches", None, off)
    chk("...and says why", "absent", how)

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
