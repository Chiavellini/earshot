#!/usr/bin/env python3
"""LLM section segmentation for the call-structure spike (docs/CALL-STRUCTURE.md).

Feeds an earnings-call transcript to Claude and asks it to segment the call into
an ordered list of labeled sections, each anchored by a *verbatim* opening quote
so we can locate its character offset deterministically in Python (never trust
LLM-reported char indices). Also returns the section-boundary marker phrases the
model keyed on — those become candidate live "section markers" for the detector.

This is offline research only: it touches nothing in the live trading path.

Setup (once):
    .venv-corpus/bin/pip install "anthropic>=0.116"
    export ANTHROPIC_API_KEY=...        # or `ant auth login`

Usage:
    .venv-corpus/bin/python scripts/section-label.py \
        --transcript corpus/transcripts/aapl-q2-2026.txt \
        --id AAPL-Q2-2026 \
        > corpus/structure/aapl-q2-2026.json

Review a couple of outputs by hand against the raw text before scaling.
"""

import argparse
import json
import sys

import anthropic
from pydantic import BaseModel, Field

MODEL = "claude-opus-4-8"

# Shared taxonomy — keep in sync with docs/CALL-STRUCTURE.md and structure-analyze.py.
SECTION_TYPES = [
    "operator_intro",     # operator opens the call, reads participants
    "safe_harbor",        # forward-looking-statements / non-GAAP disclaimer
    "ceo_remarks",        # CEO prepared remarks (strategy, highlights)
    "cfo_remarks",        # CFO prepared remarks (financials)
    "business_segment",   # a named segment/product-line walk-through
    "guidance_outlook",   # forward guidance / outlook for next period
    "qa",                 # analyst Q&A
    "closing",            # closing remarks / operator ends the call
]


class Section(BaseModel):
    order: int = Field(description="0-based position of this section in the call")
    section_type: str = Field(description=f"one of: {', '.join(SECTION_TYPES)}")
    title: str = Field(description="short human label, e.g. 'CFO financial review'")
    opening_quote: str = Field(
        description="the first ~10 words of this section, copied VERBATIM from the "
        "transcript so it can be located by exact string match"
    )
    boundary_marker: str = Field(
        description="the short phrase that signals this section is starting "
        "(e.g. 'let me turn to the outlook', 'first question comes from'); "
        "empty string if the section has no explicit verbal marker"
    )


class CallStructure(BaseModel):
    sections: list[Section]


PROMPT = """You are analyzing the transcript of a quarterly earnings conference call.

Segment the ENTIRE call into an ordered list of sections. Use only these \
section_type values: {types}.

For each section:
- Copy `opening_quote` VERBATIM from the transcript (the first ~10 words of the \
section) so it can be located by exact string match. Do not paraphrase.
- Give `boundary_marker`: the recurring phrase a speaker uses to transition INTO \
this section (e.g. "let me turn to guidance", "moving to our outlook", "the first \
question comes from"). Use "" if the section has no explicit verbal marker.
- Keep sections in the order they occur; number `order` from 0.

Prepared remarks usually run: operator intro -> safe harbor -> CEO -> CFO \
(financials/segments) -> guidance/outlook, then analyst Q&A, then closing. Break \
prepared remarks into business_segment sections where the speaker walks through \
named segments or product lines.

Transcript:
---
{transcript}
---"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--id", required=True, help="call id, e.g. AAPL-Q2-2026")
    args = ap.parse_args()

    with open(args.transcript, encoding="utf-8") as f:
        transcript = f.read()

    client = anthropic.Anthropic()
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        output_format=CallStructure,
        messages=[
            {
                "role": "user",
                "content": PROMPT.format(
                    types=", ".join(SECTION_TYPES), transcript=transcript
                ),
            }
        ],
    )
    structure = resp.parsed_output
    if structure is None:
        print(f"# ERROR: model did not return valid structure for {args.id}", file=sys.stderr)
        sys.exit(1)

    # Locate each section's char offset by exact match on its verbatim opening
    # quote. Flag any quote we can't find so the reviewer can spot bad anchors.
    sections_out = []
    search_from = 0
    for s in structure.sections:
        offset = transcript.find(s.opening_quote, search_from)
        if offset == -1:
            offset = transcript.find(s.opening_quote)  # retry from the top
        if offset == -1:
            print(
                f"# WARN[{args.id}]: opening_quote not found for section "
                f"{s.order} ({s.section_type}): {s.opening_quote!r}",
                file=sys.stderr,
            )
        else:
            search_from = offset + 1
        sections_out.append(
            {
                "order": s.order,
                "section_type": s.section_type,
                "title": s.title,
                "boundary_marker": s.boundary_marker,
                "opening_quote": s.opening_quote,
                "char_offset": offset,  # -1 if not located
            }
        )

    out = {
        "id": args.id,
        "transcript": args.transcript,
        "char_len": len(transcript),
        "model": MODEL,
        "sections": sections_out,
    }
    json.dump(out, sys.stdout, indent=2)
    print()
    located = sum(1 for s in sections_out if s["char_offset"] != -1)
    print(
        f"# {args.id}: {len(sections_out)} sections, {located} located by quote",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
