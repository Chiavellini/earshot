#!/usr/bin/env python3
"""Score a WNT replay report against the market's own SETTLEMENT.

Why this exists. For a mention market, Kalshi's settlement IS the label: a
strike that settles ~99c means the word was said during the broadcast, ~1c
means it was not. So a banked capture plus its settled board is a free,
perfectly-labelled recall/precision set — no forced alignment, no hand marking,
no transcript. That makes every session a measurement, which is the point of
capturing daily.

    recall     = said-words with >=1 in-window detection / said-words
    false alarm = any in-window detection of a word that settled NO

⚠ WHAT THIS CANNOT TELL YOU. Settlement says a word occurred *somewhere in the
broadcast*, not when. So a "hit" here does not prove we detected the resolving
utterance — on 2026-07-29 `iraq` was detected 29 minutes from its pump. For
per-utterance timing you need a manifest with real onset_ms/end_ms
(scripts/transcript-fetch.py -> scripts/manifest-from-words.py) and
`earshot bench-detector`. Use this for the cheap daily signal and the FA count;
use bench-detector for latency-grade claims.

⚠ THE WINDOW IS APPROXIMATE. ABC News Live is a 24/7 channel and the market
resolves only on the 22:30-23:00Z block, but our feed runs seconds behind, so
the boundary in OUR audio is not the wall clock. `--window` is applied to
capture-derived wall time and deliberately padded. Detections outside it are
reported separately rather than silently dropped: on 2026-07-29 four of the
false firings were pre-show programming, which is a windowing bug, not a
detector bug, and conflating the two hides both.

Usage:
    scripts/wnt-score.py <replay-report.json> <config.toml> <EVENT_TICKER> \
        [--record-dir DIR --leg LABEL] [--window 22:30-23:05]
"""

import argparse
import collections
import datetime as dt
import json
import re
import sys
import urllib.request
from bisect import bisect_right


def keyword_index(cfg_path):
    """KeywordId (config order) -> strike suffix. Detections carry the index."""
    txt = open(cfg_path).read()
    out = []
    for b in re.findall(r"\[\[keywords\]\](.*?)(?=\n\[\[|\n\[[a-z]|\Z)", txt, re.S):
        m = re.search(r'market_ticker\s*=\s*"([^"]+)"', b)
        if m:
            out.append(m.group(1).rsplit("-", 1)[1])
    return out


def settlement(event):
    """suffix -> True(said) / False(not said). Uses last price, because the
    `result` field stays empty for a while after close."""
    u = (f"https://api.elections.kalshi.com/trade-api/v2/markets"
         f"?event_ticker={event}&limit=200")
    d = json.load(urllib.request.urlopen(u, timeout=30))
    out = {}
    for m in d["markets"]:
        px = m.get("last_price_dollars") or m.get("yes_bid_dollars") or "0"
        out[m["ticker"].rsplit("-", 1)[1]] = float(px) >= 0.90
    return out


def wall_mapper(record_dir, leg):
    """audio_pos_ms -> UTC epoch, via the leg's per-frame capture_ns and the
    clock anchor taken in the same awake session (see wnt-daily.sh)."""
    anchor = json.load(open(f"{record_dir}/clock_anchor.json"))
    cum, ns, total = [], [], 0
    with open(f"{record_dir}/{leg}.jsonl") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                cum.append(total)
                ns.append(r["capture_ns"])
                total += r["samples"]

    def to_utc(pos_ms):
        i = bisect_right(cum, int(pos_ms * 16)) - 1
        if i < 0 or i >= len(ns):
            return None
        return (anchor["wall_ns"] + (ns[i] - anchor["mono_ns"])) / 1e9

    return to_utc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("config")
    ap.add_argument("event")
    ap.add_argument("--record-dir")
    ap.add_argument("--leg", default="spec_edge")
    ap.add_argument("--window", default="22:30-23:05",
                    help="UTC HH:MM-HH:MM; padded on purpose, see module docs")
    a = ap.parse_args()

    kws = keyword_index(a.config)
    said = settlement(a.event)
    rep = json.load(open(a.report))

    to_utc = None
    if a.record_dir:
        try:
            to_utc = wall_mapper(a.record_dir, a.leg)
        except FileNotFoundError as e:
            print(f"note: no wall-clock mapping ({e}); window not applied\n")

    day = a.event.rsplit("-", 1)[1]
    d0 = dt.datetime.strptime(day, "%y%b%d").replace(tzinfo=dt.timezone.utc)
    lo_s, hi_s = a.window.split("-")
    lo = d0.replace(hour=int(lo_s[:2]), minute=int(lo_s[3:])).timestamp()
    hi = d0.replace(hour=int(hi_s[:2]), minute=int(hi_s[3:])).timestamp()

    inw, outw = collections.Counter(), collections.Counter()
    for x in rep.get("detections", []):
        det = x["detection"]
        suf = kws[det["keyword"]] if det["keyword"] < len(kws) else f"?{det['keyword']}"
        if to_utc is None:
            inw[suf] += 1
            continue
        w = to_utc(det["audio_pos_ms"])
        (inw if (w is not None and lo <= w <= hi) else outw)[suf] += 1

    print(f"event {a.event}   report {a.report}")
    print(f"window {a.window}Z  (approximate — see module docs)\n")
    print(f"{'strike':<6} {'settled':<8} {'in-win':>7} {'out-win':>8}  verdict")
    hits = misses = fa = 0
    fa_words, miss_words = [], []
    for suf in kws:
        s = said.get(suf)
        i, o = inw.get(suf, 0), outw.get(suf, 0)
        if s is True:
            v = "HIT" if i else "MISS"
            if i:
                hits += 1
            else:
                misses += 1
                miss_words.append(suf)
        elif s is False:
            v = "FALSE ALARM" if i else "ok (silent)"
            if i:
                fa += 1
                fa_words.append(suf)
        else:
            v = "unsettled"
        print(f"{suf:<6} {str(s):<8} {i:>7} {o:>8}  {v}")

    n_said = hits + misses
    rec = f"{hits}/{n_said}" + (f" = {hits/n_said:.0%}" if n_said else "")
    print(f"\nrecall (said-words with an in-window detection): {rec}")
    if miss_words:
        print(f"  missed: {','.join(miss_words)}")
    print(f"false alarms (words that settled NO, detected in-window): {fa}")
    if fa_words:
        print(f"  fired: {','.join(fa_words)}")
    outside = sum(outw.values())
    if outside:
        print(f"out-of-window detections: {outside} — NOT counted as FAs; these "
              f"are the windowing gap (24/7 channel), a separate fix.")


if __name__ == "__main__":
    main()
