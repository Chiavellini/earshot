#!/usr/bin/env python3
"""market-phrase-screen.py — do the armed variants actually resolve the market?

Why this exists (2026-07-30, AAPL "iPhone 18"). The keyword-readiness study
(scripts/variant-study.sh) answers exactly one question: CAN THE DETECTOR HEAR
OUR VARIANTS. It TTS-synthesizes each variant and confirms a Confirmed
detection. It cannot, by construction, notice that the variants are the WRONG
WORDS — a perfectly-heard wrong word passes it with a clean 16/16.

That is how this shipped live: `prepare` builds variants by splitting the
market's `yes_sub_title` on "/" and running each alternate through
`sanitize_variant`, whose alphabet is letters + spaces. DIGITS ARE STRIPPED.

    market "iPhone 18"      -> variants ["iphone", "iphones", "i fone"]
    market "Formula 1 / F1" -> variants ["formula", "formulas"]

Apple says "iPhone" on every call; the market resolves only on "iPhone 18" and
sat at 6c all night while our detector fired a Confirmed intent on it
(2026-07-30 21:02:20Z). `prepare` KNOWS these are compounds — it sets
`compound = word.chars().any(is_ascii_digit) || word.contains(' ')` and writes
a "review variants" comment — but a comment is not a gate, and nobody reviews
at T-20. Same shape as the WING CITR/AI loss: flagged, unenforced.

THE CHECK. For every [[keywords]] block, fetch the market's own
`yes_sub_title` and require that at least one variant COVERS at least one
"/"-alternative of it, where covering means every alphanumeric token of that
alternative appears in the variant (a trailing plural "s" is allowed). If no
variant covers any alternative, the keyword cannot resolve its market and the
screen FAILs.

This deliberately re-derives truth from the LIVE board rather than trusting
the config's own comments, so it stays correct however `prepare` evolves.

    python3 scripts/market-phrase-screen.py configs/<event>-arm.toml [EVENT_TICKER]
    python3 scripts/market-phrase-screen.py --self-test

Exit 0 = every armed keyword can resolve its market. Exit 1 = at least one
cannot (blocking). Exit 2 = could not run (bad args, board unreachable).
"""

import json
import re
import sys
import urllib.error
import urllib.request

API = "https://api.elections.kalshi.com/trade-api/v2"
UA = "earshot-phrase-screen/1.0"


def tokens(s):
    """Alphanumeric tokens, lowercased. '18' and 'iphone' both survive."""
    return [t.lower() for t in re.findall(r"[A-Za-z0-9]+", s or "")]


def covers(variant, alt):
    """Does `variant` contain every token the market alternative requires?"""
    vt = tokens(variant)
    if not vt:
        return False
    joined = " ".join(vt)
    for t in tokens(alt):
        # token present as a word, or as that word pluralized in the variant
        if re.search(rf"\b{re.escape(t)}s?\b", joined):
            continue
        # variant may glue the alternative together ("openai" for "Open AI")
        if t in joined.replace(" ", ""):
            continue
        return False
    return True


def screen(keywords, subtitles):
    """keywords: [(ticker, [variants])]; subtitles: {ticker: yes_sub_title}.
    Returns (failures, unknowns) — failures block, unknowns are WARNs."""
    failures, unknowns = [], []
    for ticker, variants in keywords:
        sub = subtitles.get(ticker)
        if sub is None:
            unknowns.append((ticker, variants))
            continue
        alts = [a for a in sub.split("/") if tokens(a)]
        if not alts:
            unknowns.append((ticker, variants))
            continue
        if not any(covers(v, a) for v in variants for a in alts):
            failures.append((ticker, sub, variants))
    return failures, unknowns


def parse_config(path):
    text = open(path).read()
    out = []
    for blk in re.findall(r"\[\[keywords\]\](.*?)(?=\n\[\[|\n\[[a-z]|\Z)", text, re.S):
        tk = re.search(r'market_ticker\s*=\s*"([^"]+)"', blk)
        va = re.search(r"variants\s*=\s*\[(.*?)\]", blk, re.S)
        if tk and va:
            out.append((tk.group(1), re.findall(r'"([^"]+)"', va.group(1))))
    return out, text


def event_ticker_of(config_text, override):
    if override:
        return override
    m = re.search(r"^\s*id\s*=\s*\"([^\"]+)\"", config_text, re.M)
    if not m:
        return None
    # webcast twins carry a lane suffix the Kalshi board does not know
    return re.sub(r"-(WEBCAST|CAPTURE|CAPT)$", "", m.group(1))


def _markets_raw(event_ticker):
    req = urllib.request.Request(
        f"{API}/markets?event_ticker={event_ticker}", headers={"User-Agent": UA}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    return d.get("markets", [])


def live_subtitles(event_ticker):
    return {m["ticker"]: (m.get("yes_sub_title") or "") for m in _markets_raw(event_ticker)}


def live_market_detail(event_ticker):
    """Like live_subtitles(), but keeps the full per-strike detail (not just
    yes_sub_title) needed to judge rare-discrete-event vs. predictable-word —
    the 2026-08-06 mention-market exploitability scoreboard's axis (its lane
    left the repo 2026-08-12).
    Not used by the phrase-screen gate itself; live_subtitles() above is
    unchanged and remains the one that gate calls."""
    out = {}
    for m in _markets_raw(event_ticker):
        out[m["ticker"]] = {
            "yes_sub_title": m.get("yes_sub_title") or "",
            "rules_primary": m.get("rules_primary") or "",
            "volume_fp": m.get("volume_fp"),
            "volume_24h_fp": m.get("volume_24h_fp"),
            "open_interest_fp": m.get("open_interest_fp"),
            "result": m.get("result"),
        }
    return out


def self_test():
    subs = {
        "E-IPHO": "iPhone 18",
        "E-F1": "Formula 1 / F1",
        "E-TARI": "Tariff",
        "E-OPEN": "OpenAI / Open AI",
        "E-FOLD": "Fold / Folding / Foldable",
        "E-GOOG": "Google (3+ times)",
    }
    kws = [
        ("E-IPHO", ["iphone", "iphones", "i fone"]),      # digit stripped -> FAIL
        ("E-F1", ["formula", "formulas"]),                 # digit stripped -> FAIL
        ("E-TARI", ["tariff", "tariffs"]),                 # ok
        ("E-OPEN", ["openai", "openais", "open ai"]),      # glued form covers
        ("E-FOLD", ["fold", "folds"]),                     # first alternative covers
        ("E-MISSING", ["whatever"]),                       # not on board -> unknown
    ]
    fails, unknowns = screen(kws, subs)
    ft = {f[0] for f in fails}
    assert ft == {"E-IPHO", "E-F1"}, f"unexpected failures: {ft}"
    assert [u[0] for u in unknowns] == ["E-MISSING"], unknowns
    # a variant that DOES carry the digits must pass
    ok, _ = screen([("E-IPHO", ["iphone eighteen", "iphone 18"])], subs)
    assert not ok, ok
    # plural tolerance must not let a missing token through
    bad, _ = screen([("E-IPHO", ["iphones"])], subs)
    assert bad, "plural must not satisfy a missing digit token"
    print("self-test OK")


def main():
    args = [a for a in sys.argv[1:]]
    if "--self-test" in args:
        self_test()
        return 0
    if not args:
        print(__doc__.strip().splitlines()[-4], file=sys.stderr)
        return 2
    cfg = args[0]
    override = args[1] if len(args) > 1 else None

    try:
        keywords, text = parse_config(cfg)
    except OSError as e:
        print(f"PHRASE-SCREEN-ERROR: cannot read {cfg}: {e}", file=sys.stderr)
        return 2
    if not keywords:
        print(f"PHRASE-SCREEN: no [[keywords]] in {cfg} — nothing to screen")
        return 0

    ev = event_ticker_of(text, override)
    if not ev:
        print(f"PHRASE-SCREEN-ERROR: no [event].id in {cfg}", file=sys.stderr)
        return 2
    try:
        subs = live_subtitles(ev)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"PHRASE-SCREEN-ERROR: live board for {ev} unreachable: {e}", file=sys.stderr)
        return 2
    if not subs:
        print(f"PHRASE-SCREEN-ERROR: live board for {ev} is empty — wrong ticker?", file=sys.stderr)
        return 2

    fails, unknowns = screen(keywords, subs)
    print(f"event {ev}: {len(keywords)} armed keywords screened against the live board")
    for tk, variants in unknowns:
        print(f"  PHRASE-UNKNOWN: {tk} not on the live board (variants {variants})")
    for tk, sub, variants in fails:
        need = " AND ".join(
            "+".join(tokens(a)) for a in sub.split("/") if tokens(a)
        ).replace(" AND ", "  OR  ")
        print(
            f"  PHRASE-MISMATCH: {tk}\n"
            f"      market resolves on : {sub!r}   (needs {need})\n"
            f"      armed variants     : {variants}\n"
            f"      -> these fire without resolving the market (false-positive by construction)"
        )
    if fails:
        print(f"PHRASE-SCREEN FAIL: {len(fails)} keyword(s) cannot resolve their market")
        return 1
    print("PHRASE-SCREEN PASS: every armed keyword can resolve its market")
    return 0


if __name__ == "__main__":
    sys.exit(main())
