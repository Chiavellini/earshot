#!/usr/bin/env python3
"""Call-structure metrics for the spike (docs/CALL-STRUCTURE.md).

Consumes the committed LLM section labels (corpus/structure/<id>.json), the
keyword-occurrence ground truth (corpus/manifests/<id>.toml), and the word
timestamps (corpus/transcripts/<id>.words.json), and computes the three
pre-registered make-or-break metrics:

  Q1  Section-order stability  -- median normalized edit distance of the
      section_type sequence across a company's quarters.   PASS if <= 0.30.

  Q2  Section -> keyword lift  -- P(keyword occurs in section S | call has S)
      minus base rate P(keyword occurs in call).           PASS if >= 0.25 for
      at least one (section, keyword) pair.

  Q3  Lead time                -- median seconds from a section's start (its
      boundary marker) to the first target-keyword occurrence inside it.
      PASS if >= 2 s.

Offline only; reads no live-path code. Thresholds are declared here, up front,
before looking at results (small-n discipline — see the note in the doc).

Usage:
    python3 scripts/structure-analyze.py --structure corpus/structure \
        --manifests corpus/manifests --transcripts corpus/transcripts
"""

import argparse
import glob
import json
import os
import statistics
import sys
import tomllib
from collections import defaultdict

# Pre-registered pass thresholds (see docstring / docs/CALL-STRUCTURE.md).
Q1_MAX_EDIT_DISTANCE = 0.30
Q2_MIN_LIFT = 0.25
Q3_MIN_LEAD_S = 2.0


def company_of(call_id: str) -> str:
    """AAPL-Q2-2026 -> AAPL."""
    return call_id.split("-", 1)[0].upper()


def levenshtein(a: list[str], b: list[str]) -> int:
    """Edit distance over two token sequences."""
    m, n = len(a), len(b)
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[n]


def load_manifest(path: str) -> list[dict]:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return data.get("occurrences", [])


def char_to_ms(words: list[dict], offset: int) -> float | None:
    """Map a transcript char offset to a wall-clock ms via the word timestamps."""
    if offset < 0 or not words:
        return None
    for w in words:
        if w["char_start"] <= offset < w["char_end"]:
            return w["start"] * 1000.0
    # Fall back to the nearest word start at/after the offset.
    after = [w for w in words if w["char_start"] >= offset]
    if after:
        return after[0]["start"] * 1000.0
    return words[-1]["start"] * 1000.0


def section_windows_ms(sections: list[dict], words: list[dict]) -> list[dict]:
    """Attach a start_ms to each located section and derive [start, next) windows."""
    located = [s for s in sections if s.get("char_offset", -1) >= 0]
    out = []
    for s in located:
        start = char_to_ms(words, s["char_offset"])
        if start is not None:
            out.append({**s, "start_ms": start})
    out.sort(key=lambda s: s["start_ms"])
    for i, s in enumerate(out):
        s["end_ms"] = out[i + 1]["start_ms"] if i + 1 < len(out) else float("inf")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--structure", default="corpus/structure")
    ap.add_argument("--manifests", default="corpus/manifests")
    ap.add_argument("--transcripts", default="corpus/transcripts")
    args = ap.parse_args()

    calls = {}
    for path in sorted(glob.glob(os.path.join(args.structure, "*.json"))):
        with open(path, encoding="utf-8") as f:
            struct = json.load(f)
        cid = struct["id"]
        words_path = os.path.join(args.transcripts, f"{cid.lower()}.words.json")
        words = []
        if os.path.exists(words_path):
            with open(words_path, encoding="utf-8") as f:
                words = json.load(f)
        manifest_path = os.path.join(args.manifests, f"{cid.lower()}.toml")
        occ = load_manifest(manifest_path) if os.path.exists(manifest_path) else []
        calls[cid] = {"struct": struct, "words": words, "occ": occ}

    if not calls:
        print("no corpus/structure/*.json found — run section-label.py first", file=sys.stderr)
        sys.exit(1)

    # ---- Q1: section-order stability across a company's quarters --------------
    by_company: dict[str, list[list[str]]] = defaultdict(list)
    for cid, c in calls.items():
        seq = [s["section_type"] for s in sorted(c["struct"]["sections"], key=lambda s: s["order"])]
        by_company[company_of(cid)].append(seq)

    q1_per_company = {}
    all_norm_dists = []
    for company, seqs in by_company.items():
        if len(seqs) < 2:
            continue
        dists = []
        for i in range(len(seqs)):
            for j in range(i + 1, len(seqs)):
                d = levenshtein(seqs[i], seqs[j]) / max(len(seqs[i]), len(seqs[j]), 1)
                dists.append(d)
        med = statistics.median(dists)
        q1_per_company[company] = {"n_quarters": len(seqs), "median_norm_edit": round(med, 3)}
        all_norm_dists.extend(dists)
    q1_median = round(statistics.median(all_norm_dists), 3) if all_norm_dists else None

    # ---- Q2 + Q3: assign occurrences to sections, compute lift and lead time --
    # base rate: fraction of calls in which each keyword occurs at all
    keywords = sorted({o["phrase"] for c in calls.values() for o in c["occ"]})
    n_calls_with_occ = sum(1 for c in calls.values() if c["occ"])
    base_rate = {}
    for kw in keywords:
        hits = sum(1 for c in calls.values() if any(o["phrase"] == kw for o in c["occ"]))
        base_rate[kw] = hits / n_calls_with_occ if n_calls_with_occ else 0.0

    # per (section_type, keyword): calls where kw appears in that section
    sec_kw_calls: dict[tuple[str, str], set[str]] = defaultdict(set)
    sec_present_calls: dict[str, set[str]] = defaultdict(set)
    lead_times_s: list[float] = []
    lead_detail = []

    for cid, c in calls.items():
        wins = section_windows_ms(c["struct"]["sections"], c["words"])
        for w in wins:
            sec_present_calls[w["section_type"]].add(cid)
        if not wins or not c["words"]:
            continue  # no timestamps -> can't place occurrences or measure lead
        for w in wins:
            # first occurrence of each keyword inside this section window
            firsts: dict[str, float] = {}
            for o in c["occ"]:
                onset = o["onset_ms"]
                if w["start_ms"] <= onset < w["end_ms"]:
                    sec_kw_calls[(w["section_type"], o["phrase"])].add(cid)
                    if o["phrase"] not in firsts or onset < firsts[o["phrase"]]:
                        firsts[o["phrase"]] = onset
            for kw, onset in firsts.items():
                lead = (onset - w["start_ms"]) / 1000.0
                if lead >= 0:
                    lead_times_s.append(lead)
                    lead_detail.append(
                        {"call": cid, "section": w["section_type"], "keyword": kw,
                         "lead_s": round(lead, 1)}
                    )

    q2_pairs = []
    for (sec, kw), call_set in sec_kw_calls.items():
        present = len(sec_present_calls.get(sec, set())) or 1
        p_kw_in_sec = len(call_set) / present
        lift = p_kw_in_sec - base_rate.get(kw, 0.0)
        q2_pairs.append(
            {"section": sec, "keyword": kw, "p_kw_in_section": round(p_kw_in_sec, 3),
             "base_rate": round(base_rate.get(kw, 0.0), 3), "lift": round(lift, 3),
             "n_calls": len(call_set)}
        )
    q2_pairs.sort(key=lambda x: x["lift"], reverse=True)
    q2_best = q2_pairs[0]["lift"] if q2_pairs else None
    q3_median = round(statistics.median(lead_times_s), 1) if lead_times_s else None

    result = {
        "n_calls": len(calls),
        "companies": {k: len(v) for k, v in by_company.items()},
        "thresholds": {
            "q1_max_edit_distance": Q1_MAX_EDIT_DISTANCE,
            "q2_min_lift": Q2_MIN_LIFT,
            "q3_min_lead_s": Q3_MIN_LEAD_S,
        },
        "q1_order_stability": {
            "median_norm_edit_distance": q1_median,
            "per_company": q1_per_company,
            "pass": (q1_median is not None and q1_median <= Q1_MAX_EDIT_DISTANCE),
        },
        "q2_section_keyword_lift": {
            "base_rate": {k: round(v, 3) for k, v in base_rate.items()},
            "pairs": q2_pairs,
            "best_lift": q2_best,
            "pass": (q2_best is not None and q2_best >= Q2_MIN_LIFT),
        },
        "q3_lead_time": {
            "median_lead_s": q3_median,
            "n_pairs": len(lead_times_s),
            "detail": lead_detail,
            "pass": (q3_median is not None and q3_median >= Q3_MIN_LEAD_S),
        },
    }
    json.dump(result, sys.stdout, indent=2)
    print()

    def verdict(p):
        return "PASS" if p else "FAIL"

    print(
        f"\n# calls={len(calls)}  companies={result['companies']}\n"
        f"# Q1 order stability : median edit {q1_median} (<= {Q1_MAX_EDIT_DISTANCE}) "
        f"-> {verdict(result['q1_order_stability']['pass'])}\n"
        f"# Q2 section->kw lift : best {q2_best} (>= {Q2_MIN_LIFT}) "
        f"-> {verdict(result['q2_section_keyword_lift']['pass'])}\n"
        f"# Q3 lead time        : median {q3_median}s over {len(lead_times_s)} pairs "
        f"(>= {Q3_MIN_LEAD_S}) -> {verdict(result['q3_lead_time']['pass'])}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
