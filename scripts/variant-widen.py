#!/usr/bin/env python3
"""variant-widen.py — PROPOSE a wider variant set for a keyword the study killed.

WHY (2026-08-07, TTWO). TTWO's board traded 69,658 contracts and we placed
nothing. Three of twelve keywords scored confirmed:0 in the night's study
(`gta`, `playstation`, `tailwind`); scripts/study-nightly.sh paged and
CONTINUED, so scripts/preflight-event.sh never ran, scripts/assert-ready.sh
refused at T-75 with "no preflight verdict", the launchd `&&` short-circuited,
and scripts/arm-on-url.sh was never exec'd. Twelve markets forfeited to protect
three words that provably could not have fired.

`tailwind` is the indictment. The exact set that scores 66/36 had been sitting
in five other configs since 2026-08-05; TTWO and LLY still carried the bare
two-variant `prepare` output and scored 0/18. Widening had always been a human
copying a set they happened to remember. This script is that human, minus the
forgetting.

IT PROPOSES ONLY. IT NEVER DECIDES.
    Every candidate this emits is re-scored by scripts/variant-study.sh against
    the REAL detector before it can affect anything. A bad candidate comes back
    confirmed:0 and changes no behaviour. That is why proposing aggressively is
    safe: the gate is MEASUREMENT, not assertion. Nothing here asserts that a
    variant works — it asserts only that a variant is worth 18 seconds of `say`.

TWO SOURCES, IN THIS ORDER:
  1. configs/variant-lexicon.toml — curated sets with MEASURED provenance. A
     lexicon hit is the good case: it is a set that already moved a real study
     off zero on another issuer, so propagating it costs one study round.
  2. A rule generator, for phrases nobody has measured yet. The rules are the
     patterns the 26AUG05/06 nights actually produced, generalized:
       compound-split   the CTC emits the PIECES of an unspaced compound, so
                        the spaced form is what it hears (openai -> "open ai")
       prothetic vowel  English has no /nv/ onset, so the encoder hears a vowel
                        that was never spoken (nvidia -> "invidia"/"in vidia")
       letter-spelling  acronyms, as spaced letters AND as letter NAMES
                        (gta -> "g t a"; nfl -> "en ef el")
       homophone        orthographic swaps the vocab does carry
                        (phosphide -> fosfide; innolight -> innolite)
       plural           the cheap one `prepare` already does

THE ONE RISK THIS DOES NOT COVER. A variant can be heard perfectly and resolve
the WRONG market (the AAPL "iPhone 18" class: 16/16 on a word that could not
settle the contract). scripts/market-phrase-screen.py is that check and it runs
against the LIVE board. This script deliberately does not duplicate it —
widening a phrase can only ADD variants, never remove the ones that already
cover the market, so it cannot make a passing screen fail; but a widened set
that trades specificity for recall (`lyft` -> `lift`) is a precision cost the
1c OBSERVE posture pays for, and scripts/posture-from-study.py sets that cap
from the study ratio, not from anything here.

usage:
  variant-widen.py --phrase "nvidia" [--variants "nvidia|nvidias"] [--json]
  variant-widen.py --config <cfg.toml> [--only SUF,SUF] [--json]
  variant-widen.py --config <cfg.toml> --only SUF,SUF --write
  variant-widen.py --check          # offline self-test, no config, no network

--write rewrites each named keyword's `variants = [...]` in place and stamps a
dated comment. It changes keywords_sha256, which INVALIDATES the readiness
artifact on purpose: the widened set is unstudied until variant-study.sh
re-scores it, and preflight-event.sh must refuse it until then.
"""

import argparse
import json
import os
import re
import sys
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEXICON_PATH = os.path.join(REPO, "configs", "variant-lexicon.toml")

# Total variants per keyword after widening. Clips scale as
# variants x 3 voices x (1 bare + 2 carriers), so 8 variants is 72 `say` calls
# for ONE keyword — already minutes. The cap is a study-runtime budget, not a
# claim about detectability.
MAX_VARIANTS = 8

VOWELS = set("aeiou")

# Consonant pairs English DOES allow word-initially. A word starting with a
# pair outside this set has an onset the encoder has never been trained to emit
# bare, which is exactly the nvidia failure.
LEGAL_ONSETS = {
    "bl", "br", "by", "ch", "cl", "cr", "dr", "dw", "fl", "fr", "gl", "gn",
    "gr", "kn", "ph", "pl", "pr", "ps", "qu", "rh", "sc", "sh", "sk", "sl",
    "sm", "sn", "sp", "sq", "st", "sw", "th", "tr", "tw", "wh", "wr",
}

# Spoken names of the letters. CELH `nfl` -> "en ef el" is exactly this table
# and measured 10/27 -> 32/45 (2026-08-06).
LETTER_NAMES = {
    "a": "ay", "b": "bee", "c": "see", "d": "dee", "e": "ee", "f": "ef",
    "g": "gee", "h": "aitch", "i": "eye", "j": "jay", "k": "kay", "l": "el",
    "m": "em", "n": "en", "o": "oh", "p": "pee", "q": "cue", "r": "ar",
    "s": "es", "t": "tee", "u": "you", "v": "vee", "w": "double u",
    "x": "ex", "y": "why", "z": "zee",
}

# Orthographic swaps whose replacement the vocab demonstrably carries. Each one
# is here because a MEASURED set used it:
#   ph->f      AAOI "indium phosphide" -> "indium fosfide"   (10/36)
#   ight->ite  AAOI "innolight" -> "innolite"                (46/45)
HOMOPHONES = [("ph", "f"), ("ight", "ite"), ("ite", "ight"), ("ck", "k"),
              ("x", "ks"), ("oo", "u")]

# Compound components. Deliberately SMALL and domain-shaped rather than a
# dictionary: /usr/share/dict/words admits enough obscure words to split almost
# anything, and every junk split costs 9 `say` clips. Supplemented at runtime
# by the system dictionary under a length floor (see _system_words).
CORE_WORDS = {
    "ai", "air", "back", "base", "bench", "big", "bill", "black", "board",
    "book", "bot", "box", "buy", "call", "cap", "car", "card", "care", "cash",
    "cast", "cell", "chain", "chat", "check", "chip", "city", "class", "click",
    "cloud", "coin", "cost", "count", "cross", "cup", "cut", "data", "dead",
    "deal", "deep", "desk", "direct", "dish", "dock", "down", "drive", "edge",
    "end", "eye", "face", "fall", "fast", "feed", "field", "file", "fire",
    "first", "fit", "flow", "fold", "food", "foot", "force", "fort", "forward",
    "free", "front", "full", "game", "gate", "gear", "gift", "give", "go",
    "gov", "grid", "ground", "group", "half", "hand", "hard", "head", "health",
    "high", "hold", "home", "host", "house", "hub", "hyper", "ice", "in",
    "input", "key", "kit", "lake", "land", "large", "lead", "life", "light",
    "line", "link", "live", "load", "lock", "log", "long", "look", "low",
    "made", "mail", "main", "make", "map", "mark", "market", "mart", "mass", "master",
    "match", "media", "meta", "micro", "mid", "mind", "mini", "mix", "mobile",
    "money", "moon", "multi", "net", "network", "neo", "new", "news", "next",
    "night", "nite", "note", "off", "offline", "on", "one", "online", "open",
    "order", "out", "over", "pack", "page", "park", "part", "pass", "pay",
    "peak", "pick", "piece", "place", "play", "plus", "point", "port", "post",
    "power", "pre", "press", "price", "pro", "public", "pull", "push", "put",
    "quick", "rail", "rate", "real", "red", "ride", "right", "ring", "rise",
    "road", "robo", "rock", "roll", "room", "root", "round", "run", "safe",
    "sale", "scale", "scaler", "screen", "sea", "seat", "self", "sell",
    "service", "set", "share", "shop", "short", "shot", "side", "sign",
    "site", "sky", "slow", "small", "smart", "snap", "soft", "solar", "space",
    "speed", "spend", "sport", "spot", "stable", "stack", "staff", "stage",
    "stand", "star", "start", "state", "station", "stay", "step", "stock",
    "stop", "store", "storm", "stream", "street", "strong", "sub", "sun",
    "super", "supply", "swap", "switch", "table", "tag", "tail", "take",
    "talk", "tank", "tape", "task", "team", "tech", "ten", "test", "text",
    "thing", "think", "three", "tick", "ticket", "tide", "tie", "time", "tip",
    "today", "toll", "tool", "top", "touch", "town", "track", "trade",
    "traffic", "trail", "train", "trip", "triple", "truck", "true", "turn",
    "two", "type", "up", "use", "user", "value", "van", "video", "view",
    "voice", "volume", "wall", "war", "ware", "watch", "water", "watt", "wave",
    "way", "wear", "web", "week", "well", "west", "wide", "wind", "wire",
    "wise", "won", "wood", "word", "work", "world", "yard", "year", "zone",
}

# Short tokens that are ordinary words, NOT acronyms. Without this `pre` in
# "pre order" would be spelled out as "p r e".
NOT_ACRONYMS = {
    "the", "and", "for", "pre", "pro", "sub", "new", "old", "big", "top",
    "buy", "pay", "cut", "tax", "gas", "oil", "car", "job", "net", "run",
    "use", "one", "two", "ten", "own", "way", "day", "end", "eye", "air",
    "bot", "box", "cap", "fee", "fit", "gap", "key", "law", "low", "map",
    "mix", "put", "raw", "red", "set", "tie", "tip", "war", "win", "ai",
    "up", "us", "go", "no", "on", "in", "at", "to", "of", "or", "by", "do",
    "so", "it", "is", "be", "as", "an", "we", "he",
}

_SYS_WORDS = None


def _system_words():
    """/usr/share/dict/words, length-floored. Optional: absent on the box, and
    the self-test must not depend on it — CORE_WORDS carries every asserted
    case. Floor at 4 because a 2-3 letter dictionary hit ('ai' the sloth, 'ort',
    'tai') splits almost any brand name into noise."""
    global _SYS_WORDS
    if _SYS_WORDS is None:
        _SYS_WORDS = set()
        try:
            with open("/usr/share/dict/words", encoding="utf-8", errors="ignore") as f:
                for w in f:
                    w = w.strip().lower()
                    if len(w) >= 4 and w.isalpha() and w.isascii():
                        _SYS_WORDS.add(w)
        except OSError:
            pass
    return _SYS_WORDS


def _is_word(s, use_system=True):
    if s in CORE_WORDS:
        return True
    if not use_system:
        return False
    sysw = _system_words()
    if len(s) >= 4 and s in sysw:
        return True
    # web2 (/usr/share/dict/words) carries singulars only, so `borderlands`
    # found no split until the stem was tried: "border"+"lands" -> "land".
    return len(s) >= 5 and s.endswith("s") and s[:-1] in sysw


def _is_core(s):
    """Curated list ONLY. The one-sided split rule uses this instead of the
    system dictionary — measured 2026-08-07 on the real 08-07 boards, where the
    dictionary's obscure 4-letter entries produced `switch` -> "sw itch",
    `tariff` -> "ta riff", `alzheimer` -> "alzh eimer". Each junk split costs 9
    `say` clips, and the one-sided rule has no second half to constrain it."""
    return s in CORE_WORDS and len(s) >= 4


def load_lexicon(path=LEXICON_PATH):
    """{phrase: {variants, dead, ...}}. A missing/broken lexicon degrades to the
    rule generator rather than failing — never let a bad TOML file be the reason
    an event goes unwidened, which is the failure mode this whole change exists
    to remove."""
    try:
        import tomllib
        with open(path, "rb") as f:
            doc = tomllib.load(f)
    except Exception as e:  # noqa: BLE001 — degrade, never block
        print(f"variant-widen: lexicon unusable ({e}) — rules only", file=sys.stderr)
        return {}
    return {k.strip().lower(): v for k, v in (doc.get("phrases") or {}).items()}


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# ── rule generators ────────────────────────────────────────────────────────

def split_compound(word, use_system=True):
    """Spaced forms of an unspaced compound, best split first.

    Two rules, because brand compounds are usually half proper noun:
      BOTH halves known   playstation -> "play station", tailwind -> "tail wind"
      ONE half known      innolight   -> "inno light"  (only `light` is a word)
    Ranked by the length of the known part, so the split the encoder is most
    likely to actually emit comes first."""
    out = []
    if len(word) < 5 or not word.isalpha():
        return out
    for i in range(2, len(word) - 1):
        left, right = word[:i], word[i:]
        lw, rw = _is_word(left, use_system), _is_word(right, use_system)
        if lw and rw:
            # Both halves known: the two halves constrain each other, so the
            # broad dictionary is safe here.
            score = (2, min(len(left), len(right)))
        elif _is_core(right):
            score = (1, len(right))
        elif _is_core(left):
            score = (1, len(left))
        else:
            continue
        out.append((score, f"{left} {right}"))
    out.sort(key=lambda x: x[0], reverse=True)
    seen, ranked = set(), []
    for _s, v in out:
        if v not in seen:
            seen.add(v)
            ranked.append(v)
    return ranked[:2]


def prothetic(word):
    """English has no /nv/ onset, so the encoder hears a vowel nobody spoke.
    nvidia -> invidia / "in vidia" / "en vidia" — the set that went 0/18 -> 122/45
    on LYFT and placed a live order on UBER (2026-08-05)."""
    if len(word) < 5 or not word.isalpha():
        return []
    # `y` counts as a vowel HERE and nowhere else: `zynga` is a perfectly legal
    # English onset and was getting "izynga"/"in ynga" (measured 2026-08-07 on
    # the real TTWO board). It is not a vowel for the "has a vowel at all" test
    # below, which is about telling a word from an acronym.
    if word[0] in VOWELS or word[1] in VOWELS or word[1] == "y":
        return []
    if word[:2] in LEGAL_ONSETS:
        return []
    if not (set(word) & VOWELS):        # no vowel at all: that is an acronym
        return []
    return ["i" + word, f"in {word[1:]}", f"en {word[1:]}"]


def is_acronym(tok):
    if not tok.isalpha() or not (2 <= len(tok) <= 5) or tok in NOT_ACRONYMS:
        return False
    if not (set(tok) & VOWELS):         # gta, nfl, tsa — no vowel, must be letters
        return True
    # A 2-letter token WITH a vowel is a function word far more often than an
    # acronym: TTWO `de santa` was being spelled "d e santa"/"dee ee santa"
    # (measured 2026-08-07). Real 2-letter acronyms we care about (AI) already
    # arrive spaced from `prepare`.
    return 2 < len(tok) <= 3 and not _is_word(tok)   # iss, cpo


def letter_forms(phrase):
    """Spaced letters and letter NAMES, per acronym token, keeping the rest of
    the phrase intact. Both forms because the detector emits one or the other
    depending on the letter: CELH measured "n f l" AND "en ef el" (32/45), YOU
    measured "t s a precheck" (20/36)."""
    toks = phrase.split()
    idx = [i for i, t in enumerate(toks) if is_acronym(t)]
    if not idx:
        return []
    out = []
    for render in (lambda t: " ".join(t),
                   lambda t: " ".join(LETTER_NAMES.get(c, c) for c in t)):
        cand = list(toks)
        for i in idx:
            cand[i] = render(toks[i])
        v = " ".join(cand)
        if v != phrase:
            out.append(v)
    return out


def homophones(phrase):
    out = []
    for a, b in HOMOPHONES:
        if a in phrase:
            v = phrase.replace(a, b)
            if v != phrase:
                out.append(v)
    return out


def plurals(variants):
    out = []
    for v in variants:
        if v and not v.endswith("s") and v.isascii():
            out.append(v + "s")
    return out


def propose(phrase, existing=(), lexicon=None, max_variants=MAX_VARIANTS,
            use_system_dict=True):
    """-> (variants, source, notes). `existing` is NEVER dropped: a widening
    that removes a variant could remove the one covering the market, which is
    the market-phrase-screen failure mode we must not create."""
    phrase = norm(phrase)
    existing = [norm(v) for v in existing if norm(v)]
    lex = load_lexicon() if lexicon is None else lexicon
    notes = []

    out, seen = [], set()

    def add(v):
        v = norm(v)
        if v and v not in seen and len(out) < max_variants:
            seen.add(v)
            out.append(v)

    for v in existing:
        add(v)
    if phrase:
        add(phrase)

    entry = lex.get(phrase)
    if entry:
        source = "lexicon"
        if entry.get("dead"):
            notes.append("lexicon marks this phrase KNOWN-DEAD even widened — "
                         "expect confirmed:0 again and go straight to the ack")
        if entry.get("collision"):
            notes.append("lexicon marks this set COLLISION-PRONE — recall bought "
                         "with precision; only defensible at the 1c OBSERVE cap")
        for v in entry.get("variants", []):
            add(v)
        if entry.get("evidence"):
            notes.append(str(entry["evidence"]))
    else:
        source = "rules"
        single = phrase and " " not in phrase
        substantive = []
        if single:
            substantive += split_compound(phrase, use_system_dict)
            substantive += prothetic(phrase)
        substantive += letter_forms(phrase)
        substantive += homophones(phrase)
        for v in substantive:
            add(v)
        # Plural LAST and never counted as a fired rule: `prepare` already
        # emits it, so a set that gained only "<phrase>s" gained nothing.
        for v in plurals([phrase] if phrase else []):
            add(v)
        if not substantive:
            notes.append("no rule fired — this phrase has no compound seam, no "
                         "illegal onset, no acronym token and no homophone; "
                         "widening cannot help it and the autopsy bucket "
                         "(NEAR-MISS vs NO-EVIDENCE) is the only lead left")
    return out, source, notes


# ── config I/O ─────────────────────────────────────────────────────────────

BLOCK_RE = re.compile(r"\[\[keywords\]\](.*?)(?=\n\[\[|\n\[[a-z]|\Z)", re.S)
VARIANTS_RE = re.compile(r"^([ \t]*variants[ \t]*=[ \t]*)\[[^\]]*\][ \t]*(#.*)?$",
                         re.M)


def parse_config(text):
    """-> [(market_ticker, phrase, [variants])], in file order. Same block regex
    as scripts/keywords-extract.py so the two can never disagree about what a
    keyword is."""
    out = []
    for blk in BLOCK_RE.findall(text):
        tk = re.search(r'market_ticker\s*=\s*"([^"]+)"', blk)
        ph = re.search(r'phrase\s*=\s*"([^"]+)"', blk)
        if not (tk and ph):
            continue
        vs = re.search(r"variants\s*=\s*\[(.*?)\]", blk, re.S)
        variants = re.findall(r'"([^"]+)"', vs.group(1)) if vs else [ph.group(1)]
        out.append((tk.group(1), ph.group(1), variants))
    return out


def rewrite_config(text, ticker, variants, comment):
    """Replace ONE keyword block's variants array. Returns (text, changed).
    Rewrites in place rather than regenerating the config because the config
    carries hand-tuned caps, thresholds and postures that `prepare` would
    silently reset."""
    arr = "[" + ", ".join('"%s"' % v for v in variants) + "]"
    hit = [False]

    def block_sub(m):
        blk = m.group(1)
        if not re.search(r'market_ticker\s*=\s*"%s"' % re.escape(ticker), blk):
            return m.group(0)
        new, n = VARIANTS_RE.subn(
            lambda mm: f"{mm.group(1)}{arr}  # {comment}", blk, count=1)
        if n:
            hit[0] = True
            return "[[keywords]]" + new
        return m.group(0)

    return BLOCK_RE.sub(block_sub, text), hit[0]


def widen_config(path, only=None, write=False, max_variants=MAX_VARIANTS):
    text = open(path).read()
    lex = load_lexicon()
    only = {s.upper() for s in only} if only else None
    results, new_text = [], text
    for ticker, phrase, variants in parse_config(text):
        suffix = ticker.split("-")[-1]
        if only is not None and suffix not in only:
            continue
        proposed, source, notes = propose(phrase, variants, lexicon=lex,
                                          max_variants=max_variants)
        added = [v for v in proposed if norm(v) not in {norm(x) for x in variants}]
        rec = {"ticker": ticker, "suffix": suffix, "phrase": phrase,
               "was": variants, "proposed": proposed, "added": added,
               "source": source, "notes": notes, "written": False}
        if write and added:
            comment = (f"widened {date.today().isoformat()} by variant-widen.py "
                       f"({source}; study confirmed:0) — UNSTUDIED until "
                       f"variant-study.sh re-scores it")
            new_text, ok = rewrite_config(new_text, ticker, proposed, comment)
            rec["written"] = ok
        results.append(rec)
    if write and new_text != text:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(new_text)
        os.replace(tmp, path)
    return results


# ── self-test ──────────────────────────────────────────────────────────────

def check():
    """Offline self-test — no config, no network, no detector."""
    lex = load_lexicon()
    assert lex, f"lexicon did not load from {LEXICON_PATH}"

    # 1. THE LEXICON MUST REPRODUCE THE LYFT nvidia SET, exactly and in order.
    # That set went 0/18 -> 122/45 and placed a live order on UBER-26AUG05-NVID.
    # If this ever drifts, the propagation is propagating something else.
    lyft = ["nvidia", "nvidias", "invidia", "in vidia", "en vidia"]
    got, src, _ = propose("nvidia", ["nvidia", "nvidias"], lexicon=lex)
    assert got == lyft, f"lexicon nvidia set drifted: {got}"
    assert src == "lexicon", src
    # …and from a BARE phrase with no existing variants, too — study-nightly
    # may widen a keyword whose config never listed variants at all.
    got, _, _ = propose("nvidia", [], lexicon=lex)
    assert got == lyft, got

    # 2. THE GENERATOR MUST FIND "open ai" FOR openai WITH NO LEXICON.
    # SHOP 26AUG05: 0 bare -> 20/45. This is the rule the whole widener rests
    # on — the CTC emits the compound's PIECES, so the spaced form is what it
    # actually hears.
    got, src, _ = propose("openai", ["openai"], lexicon={})
    assert src == "rules", src
    assert "open ai" in got, f"generator lost the compound split: {got}"

    # 3. tailwind — the 2026-08-07 miss. Lexicon must carry the measured set.
    got, _, _ = propose("tailwind", ["tailwind", "tailwinds"], lexicon=lex)
    assert got[:4] == ["tailwind", "tailwinds", "tail wind", "tail winds"], got
    # and the generator must reach the same split unaided, so an unlisted
    # compound of the same shape is not lost.
    got, _, _ = propose("tailwind", ["tailwind"], lexicon={})
    assert "tail wind" in got, got

    # 4. acronyms: spaced letters AND letter names (TTWO `gta`, CELH `nfl`).
    got, _, _ = propose("gta", ["gta", "gtas"], lexicon={})
    assert "g t a" in got, got
    assert "gee tee ay" in got, got
    got, _, _ = propose("nfl", ["nfl"], lexicon={})
    assert "n f l" in got and "en ef el" in got, got
    # acronym expansion must respect the rest of the phrase (YOU 26AUG05)
    got, _, _ = propose("tsa precheck", ["tsa precheck"], lexicon={})
    assert "t s a precheck" in got, got
    # …and must not spell out ordinary short words
    got, _, _ = propose("pre order", ["pre order"], lexicon={})
    assert not any(v.startswith("p r e") for v in got), got

    # 5. prothetic vowel, generalized off nvidia. No lexicon.
    got, _, _ = propose("nvidia", ["nvidia"], lexicon={})
    for want in ("invidia", "in vidia", "en vidia"):
        assert want in got, f"{want} missing from {got}"
    # a legal English onset must NOT get one (`stablecoin` is not `istablecoin`)
    got, _, _ = propose("stablecoin", ["stablecoin"], lexicon={})
    assert not any(v.startswith("istable") for v in got), got
    assert "stable coin" in got, got

    # 6. compound split where only ONE half is a word (AAOI innolight, 46/45).
    got, _, _ = propose("innolight", ["innolight"], lexicon={})
    assert "inno light" in got, got
    assert "innolite" in got, got            # -ight/-ite homophone
    # ph->f, the AAOI indium phosphide swap
    got, _, _ = propose("indium phosphide", ["indium phosphide"], lexicon={})
    assert "indium fosfide" in got, got

    # 7. TTWO `playstation` — the second dead word of the 08-07 miss.
    got, _, _ = propose("playstation", ["playstation", "playstations"], lexicon={})
    assert "play station" in got, got

    # 7b. NOISE REGRESSIONS, all three measured 2026-08-07 by running this
    # script over the real TTWO and LLY boards. Every junk proposal costs 9
    # `say` clips and pushes a real candidate past the cap, so they are bugs,
    # not cosmetics.
    #   the system dictionary's obscure 4-letter entries drove the one-sided
    #   split rule into nonsense
    for phrase, junk in (("switch", "sw itch"), ("tariff", "ta riff"),
                         ("tariff", "tari ff"), ("alzheimer", "alzh eimer"),
                         ("ventyx", "vent yx")):
        got, _, _ = propose(phrase, [phrase], lexicon={})
        assert junk not in got, f"{phrase}: junk split {junk!r} back in {got}"
    #   …without losing the one-sided splits that are the whole point
    for phrase, want in (("innolight", "inno light"), ("lillydirect", "lilly direct"),
                         ("walmart", "wal mart"), ("govtech", "gov tech"),
                         ("headwind", "head wind"), ("clickhouse", "click house")):
        got, _, _ = propose(phrase, [phrase], lexicon={})
        assert want in got, f"{phrase}: lost {want!r} from {got}"
    #   the system dictionary is singulars-only, so a plural half needs its stem
    #   tried (TTWO `borderlands` found no split at all until it did)
    got, _, _ = propose("borderlands", ["borderlands"], lexicon={})
    assert "border lands" in got, got
    #   `zy` IS a legal English onset — zynga must not get a prothetic vowel
    got, _, _ = propose("zynga", ["zynga"], lexicon={})
    assert not any(v.startswith(("izynga", "in ynga", "en ynga")) for v in got), got
    #   a 2-letter token with a vowel is a function word, not an acronym
    got, _, _ = propose("de santa", ["de santa"], lexicon={})
    assert not any(v.startswith(("d e ", "dee ee ")) for v in got), got

    # 8. EXISTING VARIANTS ARE NEVER DROPPED. Dropping one could remove the
    # variant that covers the market (market-phrase-screen.py), turning a
    # detectability fix into a resolution bug.
    got, _, _ = propose("nvidia", ["nvidia", "hand tuned form"], lexicon=lex)
    assert got[0] == "nvidia" and "hand tuned form" in got, got

    # 9. the cap holds, and holds from the front (existing first).
    got, _, _ = propose("tsa precheck", ["a", "b", "c", "d", "e", "f", "g", "h"],
                        lexicon={}, max_variants=8)
    assert len(got) == 8 and got[0] == "a", got

    # 10. known-dead phrases must SAY SO — that is what lets study-nightly skip
    # a wasted study round and go straight to the ack (YOU `govtech`, 0/36 even
    # widened; TLN `shapiro`, 0/36 even widened).
    for deadword in ("govtech", "shapiro"):
        _g, _s, notes = propose(deadword, [deadword], lexicon=lex)
        assert any("KNOWN-DEAD" in n for n in notes), (deadword, notes)

    # 11. a phrase no rule can help must say so rather than pretend.
    _g, _s, notes = propose("vera rubin", ["vera rubin"], lexicon={})
    assert any("no rule fired" in n for n in notes), notes

    # 12. config rewrite round-trips, keeps the rest of the block byte-identical,
    # and is idempotent (a second widen must not stack comments).
    cfg = (
        '[event]\nid = "KXEARNINGSMENTIONTTWO-26AUG07"\n\n'
        '[[keywords]]\nmarket_ticker    = "KXEARNINGSMENTIONTTWO-26AUG07-TAIL"\n'
        'phrase           = "tailwind"\n'
        'variants         = ["tailwind", "tailwinds"]\n'
        'max_price_cents  = 1\n\n'
        '[[keywords]]\nmarket_ticker    = "KXEARNINGSMENTIONTTWO-26AUG07-SWIT"\n'
        'phrase           = "switch"\nvariants         = ["switch", "switchs"]\n'
        'max_price_cents  = 97\n'
    )
    new, ok = rewrite_config(cfg, "KXEARNINGSMENTIONTTWO-26AUG07-TAIL",
                             ["tailwind", "tailwinds", "tail wind", "tail winds"],
                             "widened 2026-08-07 by variant-widen.py")
    assert ok
    assert '"tail wind", "tail winds"]  # widened' in new, new
    assert 'variants         = ["switch", "switchs"]' in new, "wrong block touched"
    assert 'max_price_cents  = 1' in new, "cap must survive the rewrite"
    again, ok2 = rewrite_config(new, "KXEARNINGSMENTIONTTWO-26AUG07-TAIL",
                                ["tailwind", "tailwinds", "tail wind", "tail winds"],
                                "widened 2026-08-07 by variant-widen.py")
    assert ok2 and again == new, "rewrite is not idempotent"
    assert again.count("# widened") == 1, again
    # a ticker that is not in the file must report NOT changed, never silently pass
    _n, ok3 = rewrite_config(cfg, "KXEARNINGSMENTIONTTWO-26AUG07-NOPE", ["x"], "c")
    assert not ok3
    # a MULTI-LINE variants array must collapse correctly — some hand-edited
    # configs wrap, and a rewrite that only handled one line would silently
    # leave orphan quoted strings behind and produce invalid TOML.
    multi = ('[[keywords]]\nmarket_ticker    = "E-X"\nphrase           = "tailwind"\n'
             'variants         = [\n  "tailwind",\n  "tailwinds"\n]\n'
             'max_price_cents  = 1\n')
    out, ok4 = rewrite_config(multi, "E-X", ["tailwind", "tailwinds", "tail wind"], "c")
    assert ok4 and out.count("variants") == 1 and '"tail wind"]  # c' in out, out
    assert "max_price_cents  = 1" in out and '\n  "tailwinds"' not in out, out

    # 13. parse_config agrees with the shared extraction on what a keyword is.
    kws = parse_config(cfg)
    assert [k[0].split("-")[-1] for k in kws] == ["TAIL", "SWIT"], kws
    assert kws[0][2] == ["tailwind", "tailwinds"], kws

    # 14. a missing lexicon degrades to rules — it must never be the reason an
    # event goes unwidened, which is the exact failure this change removes.
    assert load_lexicon("/nonexistent/variant-lexicon.toml") == {}
    got, src, _ = propose("tailwind", ["tailwind"],
                          lexicon=load_lexicon("/nonexistent/x.toml"))
    assert src == "rules" and "tail wind" in got, got

    print("variant-widen --check: OK (lexicon reproduces the LYFT nvidia set "
          "exactly; generator finds 'open ai' for openai unaided; tailwind, "
          "gta/nfl/tsa letter-spelling, nvidia prothesis, innolight one-sided "
          "split, ph->f, playstation; existing variants preserved; cap; "
          "known-dead + no-rule-fired notes; idempotent in-place rewrite; "
          "lexicon-absent degradation)")
    return 0


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--phrase")
    ap.add_argument("--variants", default="",
                    help="current variants, '|' or ',' separated")
    ap.add_argument("--config")
    ap.add_argument("--only", default="",
                    help="comma-separated market-ticker SUFFIXes (e.g. NVID,TAIL)")
    ap.add_argument("--write", action="store_true",
                    help="rewrite the config's variants in place (invalidates "
                         "the readiness artifact on purpose)")
    ap.add_argument("--max", type=int, default=MAX_VARIANTS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    if a.check:
        return check()

    if a.phrase:
        cur = [v for v in re.split(r"[|,]", a.variants) if v.strip()]
        variants, source, notes = propose(a.phrase, cur, max_variants=a.max)
        if a.json:
            print(json.dumps({"phrase": a.phrase, "was": cur,
                              "proposed": variants, "source": source,
                              "notes": notes}, indent=1))
        else:
            print("|".join(variants))
            for n in notes:
                print(f"# {n}", file=sys.stderr)
        return 0

    if a.config:
        only = [s for s in a.only.split(",") if s.strip()]
        if a.write and not only:
            print("REFUSING --write without --only: widening the whole board "
                  "invalidates the readiness artifact for keywords that were "
                  "already confirmed. Name the dead suffixes.", file=sys.stderr)
            return 2
        res = widen_config(a.config, only=only or None, write=a.write,
                           max_variants=a.max)
        if a.json:
            print(json.dumps(res, indent=1))
        else:
            for r in res:
                mark = "WROTE" if r["written"] else ("+" if r["added"] else "=")
                print(f"{mark}\t{r['suffix']}\t{r['phrase']}\t{r['source']}\t"
                      + "|".join(r["proposed"]))
                for n in r["notes"]:
                    print(f"  # {n}")
        return 0

    ap.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
