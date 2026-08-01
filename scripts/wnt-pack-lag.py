#!/usr/bin/env python3
"""How far behind the actual broadcast is the KXWORLDNEWSMENTION pack?

THE QUESTION THIS ANSWERS. The linear ABC feed is unreachable from here, and
every purchasable internet route (YouTube TV, Hulu + Live, Fubo, DirecTV
Stream) runs tens of seconds behind over-the-air. Whether that is fatal depends
entirely on a number nobody had measured: how far behind the broadcast the
*existing traders* already are. If the pack sits ~1 s back, no internet route
can compete and the live-race lane is closed. If it sits 40 s back — because
every participant is also watching a delayed stream — then a better delayed
feed still wins, and the lane is open.

Do NOT infer this from how TIGHT the repricing bursts are. Bursts are
sub-second, which says the leaders agree with each other; it says nothing about
their common offset from the studio. That distinction is the same one that made
the 2026-07-29 wrong-source capture look valid (VERIFICATIONS Q-WNT-SRC): legs
agreeing proves shared input, never correct input.

METHOD — two free public sources, joined:

  leg 1  the broadcast itself, as ABC's own same-night YouTube VOD, transcribed
         to word offsets            (scripts/wnt-vod-fetch.sh -> words.json)
  leg 2  the market's reaction, as the public Kalshi trade tape
         (GET /markets/trades)      -> the onset of the burst that reprices a
                                       strike to ~99c

  For strike i:   delta_i = sweep_onset_i - (22:30:00Z + programme_offset_i)

WHAT REPLACED THE AD MODEL, AND WHY. This was first written assuming the VOD
had commercials removed, so delta would equal the pack lag PLUS accumulated ad
time and would climb through the session. The 26JUL29 data refused that model
outright: delta came out flat (-6.75, -6.06, -4.06, -3.70, -7.37 s) instead of
climbing, and the caption timeline turned out to contain only 8.5 s of gaps in
1206 s — the VOD is continuous speech with no ad breaks in it, and wall-clock
sweep spacing tracks programme spacing 1:1 (0.999 over 748 s). So the estimator
is not a minimum over an ad-contaminated series; it is a straight line fit:

    sweep_wall_i  =  slope * programme_offset_i  +  offset

  * slope must come out at 1.0. That is the real alignment check — it says the
    market's clock and the programme's clock advance together, which a wrong
    VOD, a mis-parsed caption track, or news-driven repricing would all break.
  * offset is what we actually want, and it is a SUM of two things we cannot
    separate from this data alone: the pack's reaction lag, and however far the
    VOD's t=0 sits from 22:30:00Z. Both are small, and the sum is what decides
    the question, so the conflation is stated rather than hidden.

Reading the offset: a pack watching a 30-90 s delayed live-TV stream would put
offset at +30..+90 s. A pack at the broadcast puts it near zero. The measured
sign can even go slightly negative, which just means the VOD's t=0 aired a few
seconds before 22:30:00Z — it does not mean anyone traded before the words.

COUNT STRIKES ARE EXCLUDED from the fit. '-TRUM' (Trump 5+ times) and '-IRAN'
(Iran 3+ times) reprice on the Nth utterance, and Kalshi's N is not reproducible
from a caption track: possessives, plurals and anything the captioner dropped
all shift it. On 26JUL29 the IRAN sweep landed 615 s away from the caption's
third 'Iran'. They are reported separately, never fitted.

REFUSAL. If the slope is not 1.0, the alignment is wrong and this prints
NOT-RESOLVABLE for that session instead of a number. A wrong figure here would
send us to buy a subscription that cannot win, or abandon a lane that could.

Usage:
    scripts/wnt-pack-lag.py                     # every banked session
    scripts/wnt-pack-lag.py 26JUL29 26JUL28
    scripts/wnt-pack-lag.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.elections.kalshi.com/trade-api/v2"
CORPUS = Path("corpus/wnt")
SHOW_UTC = (22, 30)  # 6:30 PM ET

# A strike is "repriced" once YES trades at/above this and stays there.
SWEEP_PRICE = 0.90
SWEEP_HOLD = 0.80
# Trades closer together than this belong to one burst; the burst's first trade
# is the onset. Measured bursts on 2026-07-29 were 0.64-0.99 s wide end to end.
BURST_GAP_S = 5.0
# A strike already priced this high before air was decided by the news, not by
# the utterance, so it carries no timing information.
PREPRICED = 0.85
# The market clock and the programme clock must advance together. Caption word
# timings and burst onsets each jitter by a second or two, so the fit is judged
# on slope, which is insensitive to a constant error, rather than on residuals.
SLOPE_TOLERANCE = 0.02
# Below this the fit is a line through too few points to mean anything.
MIN_FIT_POINTS = 3
# Programme span the strikes must cover before a slope is trustworthy: three
# words inside one minute would fit any slope at all.
MIN_FIT_SPAN_S = 240.0

COUNT_RE = re.compile(r"\((\d+)\+\s*times\)", re.I)
WORD_RE = re.compile(r"[a-z0-9]+")


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


def get(url: str):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


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


def nth_occurrence(words: list[dict], phrases: list[list[str]], n: int):
    """Programme-time end of the nth utterance of any alternative phrase.

    Returns (offset, how) where how is "exact" or "fuzzy". The fuzzy pass only
    runs when the exact pass finds nothing, because word timings come from
    YouTube auto-captions, which carry ASR spellings: on 2026-07-29 the market
    word 'Blanche' is captioned 'Blanch' and would otherwise read as "settled
    YES but never said" — indistinguishable from a genuinely wrong VOD.

    It is deliberately narrow: one edit, tokens of 5+ characters only. At 4
    characters 'iran'/'iraq' are one edit apart, and both are live strikes on
    this board — a looser rule would swap them and produce a confident, wrong
    timestamp. Fuzzy hits are LABELLED, never silently merged into exact ones.
    """
    normed = [norm(w["word"]) for w in words]

    def scan(match) -> list[float]:
        found = []
        for phrase in phrases:
            k = len(phrase)
            for i in range(len(normed) - k + 1):
                if all(match(normed[i + o], phrase[o]) for o in range(k)):
                    found.append(words[i + k - 1]["end"])
        found.sort()
        return found

    hits = scan(lambda a, b: a == b)
    if len(hits) >= n:
        return hits[n - 1], "exact"

    hits = scan(lambda a, b: a == b or (len(b) >= 5 and within_one_edit(a, b)))
    if len(hits) >= n:
        return hits[n - 1], "fuzzy"
    return None, "absent"


def trades(ticker: str) -> list[dict]:
    out, cursor = [], ""
    while True:
        url = f"{API}/markets/trades?ticker={ticker}&limit=1000"
        if cursor:
            url += f"&cursor={cursor}"
        d = get(url)
        batch = d.get("trades", [])
        out.extend(batch)
        cursor = d.get("cursor") or ""
        if not cursor or not batch:
            break
    for t in out:
        t["_ts"] = datetime.fromisoformat(t["created_time"].replace("Z", "+00:00"))
        t["_yes"] = float(t["yes_price_dollars"])
    out.sort(key=lambda t: t["_ts"])
    return out


def sweep_onset(tr: list[dict], t0: datetime):
    """First trade of the burst that permanently reprices the strike to ~99c."""
    if not tr:
        return None, "no-trades"
    idx = None
    for i, t in enumerate(tr):
        if t["_yes"] >= SWEEP_PRICE and all(u["_yes"] >= SWEEP_HOLD for u in tr[i:]):
            idx = i
            break
    if idx is None:
        return None, "never-repriced"
    # Anything already this high before air was priced off the news, not the
    # utterance — 2026-07-29 FIFT sat at 0.93 six minutes before the broadcast.
    pre = [t for t in tr if t["_ts"] < t0 - timedelta(seconds=60)]
    if pre and pre[-1]["_yes"] >= PREPRICED:
        return None, "pre-priced"
    while idx > 0 and (tr[idx]["_ts"] - tr[idx - 1]["_ts"]).total_seconds() < BURST_GAP_S:
        idx -= 1
    onset = tr[idx]["_ts"]
    if not (t0 - timedelta(minutes=5) <= onset <= t0 + timedelta(minutes=60)):
        return None, "outside-window"
    return onset, "ok"


def session(slug: str) -> dict:
    d = CORPUS / slug
    meta = json.loads((d / "meta.json").read_text())
    words = json.loads((d / "words.json").read_text())
    ev = meta["event_ticker"]
    tick = meta["session"]
    day = datetime.strptime(tick, "%y%b%d").replace(tzinfo=timezone.utc)
    t0 = day.replace(hour=SHOW_UTC[0], minute=SHOW_UTC[1])

    markets = get(f"{API}/markets?event_ticker={ev}&limit=200")["markets"]
    rows, notes = [], []
    for m in markets:
        code = m["ticker"].rsplit("-", 1)[1]
        if m.get("result") != "yes":
            continue
        phrases, n = alternatives(m.get("yes_sub_title") or "")
        off, how = nth_occurrence(words, phrases, n)
        if off is None:
            notes.append(f"{code}: settled YES but the phrase is not in the VOD transcript")
            continue
        if how == "fuzzy":
            notes.append(f"{code}: matched FUZZY (caption spelling) — {m.get('yes_sub_title')!r}")
        onset, why = sweep_onset(trades(m["ticker"]), t0)
        if onset is None:
            notes.append(f"{code}: {why}")
            continue
        rows.append(
            {
                "strike": code,
                "phrase": m.get("yes_sub_title"),
                "count": n,
                "match": how,
                "programme_offset_s": round(off, 2),
                "sweep_onset_utc": onset.isoformat().replace("+00:00", "Z"),
                "delta_s": round((onset - (t0 + timedelta(seconds=off))).total_seconds(), 2),
            }
        )

    rows.sort(key=lambda r: r["programme_offset_s"])
    # Count strikes reprice on Kalshi's Nth utterance, which a caption track
    # cannot reproduce. Keep them visible, keep them out of the fit.
    fit_rows = [r for r in rows if r["count"] == 1]
    counted = [r for r in rows if r["count"] > 1]
    for r in counted:
        notes.append(
            f"{r['strike']}: count strike (n={r['count']}) — excluded from the fit "
            f"(delta {r['delta_s']:+.1f}s)"
        )

    result = {
        "session": tick,
        "event_ticker": ev,
        "video_id": meta.get("video_id"),
        "word_source": meta.get("word_source"),
        "fitted_strikes": len(fit_rows),
        "rows": rows,
        "notes": notes,
    }

    if len(fit_rows) < MIN_FIT_POINTS:
        result["verdict"] = "NOT-RESOLVABLE"
        result["reason"] = (
            f"needs >={MIN_FIT_POINTS} first-utterance strikes, have {len(fit_rows)}"
        )
        return result

    xs = [r["programme_offset_s"] for r in fit_rows]
    ys = [
        (
            datetime.fromisoformat(r["sweep_onset_utc"].replace("Z", "+00:00"))
            - day.replace(hour=SHOW_UTC[0], minute=SHOW_UTC[1])
        ).total_seconds()
        for r in fit_rows
    ]
    span = max(xs) - min(xs)
    if span < MIN_FIT_SPAN_S:
        result["verdict"] = "NOT-RESOLVABLE"
        result["reason"] = f"strikes span only {span:.0f}s of programme (need {MIN_FIT_SPAN_S:.0f}s)"
        return result

    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    intercept = my - slope * mx
    result["slope"] = round(slope, 4)
    result["span_s"] = round(span, 1)

    if abs(slope - 1.0) > SLOPE_TOLERANCE:
        result["verdict"] = "NOT-RESOLVABLE"
        result["reason"] = (
            f"slope {slope:.3f} != 1.00 — the market clock and the programme clock "
            "do not advance together, so the join is wrong"
        )
        return result

    deltas = [r["delta_s"] for r in fit_rows]
    result["verdict"] = "OK"
    # Reported as an OFFSET, not a lag: it is pack reaction time plus however
    # far the VOD's t=0 sits from 22:30:00Z, and this data cannot split them.
    result["offset_s"] = round(intercept, 2)
    result["offset_mean_s"] = round(sum(deltas) / len(deltas), 2)
    result["offset_spread_s"] = round(max(deltas) - min(deltas), 2)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="*", help="e.g. 26JUL29; default = all banked")
    ap.add_argument("--json", help="write the full result set here")
    args = ap.parse_args()

    slugs = [s.lower() for s in args.sessions] or sorted(
        p.name for p in CORPUS.iterdir() if (p / "words.json").exists()
    )
    if not slugs:
        print("no banked sessions — run scripts/wnt-vod-fetch.sh --settled first", file=sys.stderr)
        return 2

    results = []
    for slug in slugs:
        if not (CORPUS / slug / "words.json").exists():
            print(f"{slug.upper():>9}  SKIP (not banked)")
            continue
        try:
            r = session(slug)
        except Exception as exc:  # noqa: BLE001 - one bad session must not kill the sweep
            print(f"{slug.upper():>9}  ERROR {exc}")
            continue
        results.append(r)
        if r["verdict"] == "OK":
            print(
                f"{r['session']:>9}  offset {r['offset_s']:+7.2f}s  "
                f"spread {r['offset_spread_s']:5.2f}s  slope {r['slope']:.3f}  "
                f"({r['fitted_strikes']} strikes over {r['span_s']:.0f}s)"
            )
        else:
            print(f"{r['session']:>9}  NOT-RESOLVABLE — {r['reason']}")
        for n in r["notes"]:
            print(f"           · {n}")

    ok = [r for r in results if r["verdict"] == "OK"]
    print()
    if not ok:
        print("POOLED: NOT-RESOLVABLE — no session produced a slope-1.0 fit")
        return 1
    offs = sorted(r["offset_s"] for r in ok)
    median = offs[len(offs) // 2]
    worst = max(abs(o) for o in offs)
    print(f"POOLED over {len(ok)}/{len(results)} resolvable sessions:")
    print(f"  median offset  : {median:+7.2f}s")
    print(f"  range          : {offs[0]:+.2f}s .. {offs[-1]:+.2f}s")
    print(f"  all offsets    : {', '.join(f'{o:+.1f}' for o in offs)}")
    print()
    print("  offset = pack reaction lag + (VOD t=0 vs 22:30:00Z). Not separable here;")
    print("  a pack on a 30-90 s delayed live-TV feed would land at +30..+90.")
    print()
    # Pre-registered before any number was seen; restated against the offset,
    # which is what the refuted ad model was standing in for all along.
    if worst < 15:
        print("VERDICT: the pack is AT the broadcast. No obtainable internet feed can")
        print("         compete — close the live-race lane; what is left is the count")
        print("         strikes and pre-broadcast pricing, neither of which needs speed.")
    elif median > 20:
        print("VERDICT: the pack is materially delayed. A better delayed feed can win —")
        print("         a live-TV trial is now worth its cost; re-open the acquisition ladder.")
    else:
        print("VERDICT: MARGINAL. Decide on the distribution, not the median alone.")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
