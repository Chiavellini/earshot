#!/usr/bin/env python3
"""Rule-based section splitter — no-API fallback for section-label.py.

section-label.py uses Claude for high-quality segmentation. When no API credit is
available, this heuristic splitter marks section boundaries by searching for
recurring transition phrases. It is deliberately cruder than the LLM path (fuzzy
CEO/CFO boundaries especially) and is meant to unblock the Q2/Q3 metrics, not to
replace the LLM labels. Output schema matches section-label.py so
structure-analyze.py consumes either interchangeably.

Usage:
    python3 scripts/section-label-rules.py \
        --transcript corpus/transcripts/cat-q1-2026.txt --id CAT-Q1-2026 \
        > corpus/structure/cat-q1-2026.json
"""

import argparse
import json
import re
import sys

# Canonical section order. We scan for each type's boundary *after* the previous
# section's offset (a moving cursor), which enforces sane structure and prevents
# e.g. the operator's upfront "press star one" instructions from being mistaken
# for the real Q&A. operator_intro is pinned to offset 0.
SECTION_ORDER = [
    "safe_harbor", "ceo_remarks", "cfo_remarks", "guidance_outlook", "qa", "closing",
]
MARKERS = {
    "safe_harbor": [
        "forward-looking statement", "forward looking statement", "safe harbor",
        "private securities litigation", "risks and uncertainties",
    ],
    "ceo_remarks": [
        "turn the call over to", "turn it over to", "hand the call over to",
    ],
    # CFO usually gets the *second* handoff; searching after the CEO cursor finds it.
    "cfo_remarks": [
        "turn the call over to", "turn it over to", "walk you through the financial",
        "review our financial", "turn to our financial", "moving to our financial",
    ],
    "guidance_outlook": [
        "turning to our outlook", "turning to the outlook", "our outlook",
        "for the full year", "full-year guidance", "full year guidance",
        "our guidance", "turning to guidance",
    ],
    # Real Q&A start = operator handing to the first analyst (not the upfront
    # "star one" instructions, which appear during operator_intro).
    "qa": [
        "first question comes from", "our first question", "first question is from",
        "we'll take our first question", "take our first question",
        "first question,", "line of",
    ],
    "closing": [
        "this concludes", "that concludes", "thank you for joining",
        "concludes our call", "concludes today", "concludes our",
    ],
}


def find_after(text_lc: str, phrases: list[str], start: int) -> tuple[int, str] | None:
    """Earliest match of any phrase at or after `start` (monotonic scanning)."""
    best = None
    for p in phrases:
        idx = text_lc.find(p, start)
        if idx != -1 and (best is None or idx < best[0]):
            best = (idx, p)
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--id", required=True)
    args = ap.parse_args()

    with open(args.transcript, encoding="utf-8") as f:
        text = f.read()
    text_lc = text.lower()

    found = []
    # operator intro always opens the call
    found.append({"section_type": "operator_intro", "char_offset": 0, "boundary_marker": ""})
    cursor = 0
    for sec_type in SECTION_ORDER:
        # search strictly after the previous accepted boundary to keep order sane
        hit = find_after(text_lc, MARKERS[sec_type], cursor + 1)
        if hit is not None:
            found.append(
                {"section_type": sec_type, "char_offset": hit[0], "boundary_marker": hit[1]}
            )
            cursor = hit[0]

    # already in canonical, monotonic order; opening_quote = ~10 words at the offset
    sections = []
    for order, s in enumerate(found):
        snippet = " ".join(text[s["char_offset"]:].split()[:10])
        sections.append(
            {
                "order": order,
                "section_type": s["section_type"],
                "title": s["section_type"].replace("_", " "),
                "boundary_marker": s["boundary_marker"],
                "opening_quote": snippet,
                "char_offset": s["char_offset"],
            }
        )

    out = {
        "id": args.id,
        "transcript": args.transcript,
        "char_len": len(text),
        "model": "rule-based-heuristic",
        "sections": sections,
    }
    json.dump(out, sys.stdout, indent=2)
    print()
    print(
        f"# {args.id}: {len(sections)} sections "
        f"({', '.join(s['section_type'] for s in sections)})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
