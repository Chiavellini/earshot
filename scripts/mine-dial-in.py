#!/usr/bin/env python3
"""Operator-preamble miner: classify how analysts reach the Q&A queue.

Feeds the dial-in access registry (configs/dial-ins.toml), which decides which
KXEARNINGSMENTION<T> series are tradeable on the phone-first capture path.

  ⚠ THIS SCRIPT DETERMINES `qa_mechanism` ONLY — NEVER `listen_access`.

That constraint is the whole design, so it is worth stating plainly. A transcript
tells you how analysts get into the *question queue*. It tells you nothing about
whether *we* can dial in and *listen*. Those are independent facts:

  AAPL — zero star-1/keypad language anywhere in its transcript, and the file
         starts at the literal top of the call, so the absence is real and not a
         podcast-trim artifact. Apple still publishes a dial-in on its IR page.
         The queue is curated; the listen line exists. Reading "no DTMF" as "no
         phone" was a real error made and caught on 2026-07-16.

So a `curated` or `none` verdict here is NEVER a no-go. It is a prompt to go read
the IR page. Only an IR page / press release / 8-K can establish `listen_access`.

Sourcing rules inherited from the registry (configs/dial-ins.toml header):
  - Prefer official transcripts (Insider Monkey / Investing.com).
  - ⛔ NEVER Motley Fool: it redacts operator text to "[Operator Instructions]"
    and dropped the preamble entirely on INTC and GEV -- it manufactures FALSE
    negatives, which is the one error mode this script must not launder.
  - Our own corpus/transcripts/*.txt are faster-whisper ASR, not official text.
    Real garbles observed: "All prayer" for "Operator", "To destroy your
    question" for "To withdraw your question". Hence --require-opening: never
    rest a NEGATIVE on ASR without first proving the file contains the call
    opening, or a truncated file reads as `none`.

Usage:
    scripts/mine-dial-in.py corpus/transcripts/*.txt          # report
    scripts/mine-dial-in.py --json corpus/transcripts/*.txt   # + corpus/structure/<id>.dial-in.json
    scripts/mine-dial-in.py --check corpus/transcripts/*.txt  # regression: assert the pinned table

Regression pin (--check). These six are the labeled ground truth; the classifier
must reproduce them exactly:
    msft, qcom, gis, wmt -> dtmf_star
    cat                  -> hybrid
    aapl                 -> curated      (must NOT be `none`)
"""

import argparse
import json
import pathlib
import re
import sys

# ── Patterns ──────────────────────────────────────────────────────────────────
#
# The star-digit VARIES, the connector varies, and the WORD "star" is sometimes
# not a word at all: "star 11" (DPZ/INTC), bare "press 11" (GEV), "star followed
# by the number one" (GIS), "star then the number one" (QCOM), "star zero" for
# operator assistance (MSFT/WMT), and the "*1" GLYPH (GOOGL).
#
# Two false negatives were found here by testing, not by review, and both would
# have silently marked a tradeable issuer untradeable:
#   - QCOM  "press star then the number one" -- a first pass allowed only
#           `star[, ]*(?:followed by)?`, so the "then" connector missed.
#   - GOOGL "you will need to press *1 on your telephone" -- Alphabet's own
#           transcript uses the glyph, not the word. This is the LARGEST
#           near-term event; the miner reported `unknown` for it.
# Both alternations below are load-bearing. --check and the synthetic cases in
# the header exist to keep them that way.
STAR_DTMF = re.compile(
    r"(?:\bstar\b|\*)[,\s]*(?:followed by|then)?[,\s]*(?:the\s+)?(?:number\s+)?"
    r"(zero|one|two|eleven|\d+)\b",
    re.I,
)
# GEV drops the word "star" entirely: "Please press 11 on your telephone."
BARE_KEYPAD = re.compile(r"\bpress\s+(?:the\s+)?(\d+)\b(?=[^.]{0,40}telephone)", re.I)
LISTEN_ONLY = re.compile(r"listen[-\s]?only mode", re.I)
KEYPAD = re.compile(r"telephone keypad|on your telephone", re.I)
HANDSET = re.compile(r"pick up your (?:handset|handsets)", re.I)
DIALED_IN = re.compile(r"if you (?:have )?dialed in", re.I)
RAISE_HAND = re.compile(r"raise (?:your )?hand", re.I)
UNMUTE = re.compile(r"unmute yourself", re.I)
# Curated queue: the operator reads a named analyst rather than opening a queue.
NAMED_ANALYST = re.compile(
    r"(?:first|next)\s+(?:question|one)[^.]{0,30}?from\s+([A-Z][a-z]+\s+[A-Z][a-z]+)", re.I
)
# Positive control for --require-opening: does this file contain the call's top?
OPENING = re.compile(
    r"(good (?:morning|afternoon|day)|greetings|welcome to|thank you for standing by)", re.I
)


def classify(text: str) -> dict:
    """Return the qa_mechanism verdict plus the signals behind it."""
    sig = {
        "star_dtmf": len(STAR_DTMF.findall(text)),
        "bare_keypad": len(BARE_KEYPAD.findall(text)),
        "listen_only": len(LISTEN_ONLY.findall(text)),
        "keypad": len(KEYPAD.findall(text)),
        "handset": len(HANDSET.findall(text)),
        "dialed_in": len(DIALED_IN.findall(text)),
        "raise_hand": len(RAISE_HAND.findall(text)),
        "unmute": len(UNMUTE.findall(text)),
        "named_analyst": len(NAMED_ANALYST.findall(text)),
        "has_opening": bool(OPENING.search(text[:400])),
    }

    # A DTMF queue needs a star/bare digit AND phone context -- the phone context
    # is what stops "star one" in prose from counting.
    phone_ctx = sig["keypad"] or sig["listen_only"] or sig["handset"]
    dtmf = (sig["star_dtmf"] or sig["bare_keypad"]) and phone_ctx

    if dtmf and (sig["dialed_in"] or sig["raise_hand"]):
        verdict = "hybrid"          # phone bridge AND webcast raise-hand (CAT)
    elif dtmf:
        verdict = "dtmf_star"
    elif sig["unmute"] or (sig["raise_hand"] and not phone_ctx):
        verdict = "webcast_hand"    # TSLA shape: no operator, raise-hand/unmute
    elif sig["handset"]:
        # META shape: "pick up your handset… if you are streaming, mute your
        # speakers" proves a phone leg exists (some listeners are on a phone,
        # some are not) but quotes no queue instruction. Distinct from `unknown`,
        # which means we found nothing at all -- and emphatically NOT dtmf_star,
        # which would claim a queue mechanism the text never showed.
        verdict = "phone_present"
    elif sig["named_analyst"]:
        verdict = "curated"         # AAPL shape: operator reads pre-arranged names
    else:
        verdict = "unknown"

    quote = ""
    m = STAR_DTMF.search(text) or BARE_KEYPAD.search(text) or NAMED_ANALYST.search(text)
    if m:
        s, e = max(0, m.start() - 70), min(len(text), m.end() + 70)
        quote = " ".join(text[s:e].split())

    return {"qa_mechanism": verdict, "signals": sig, "evidence_quote": quote}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcripts", nargs="+", type=pathlib.Path)
    ap.add_argument("--json", action="store_true", help="write corpus/structure/<id>.dial-in.json")
    ap.add_argument("--check", action="store_true", help="assert the pinned regression table")
    ap.add_argument(
        "--require-opening",
        action="store_true",
        help="refuse to emit a non-positive verdict unless the file contains the call opening "
             "(guards against a truncated transcript reading as `none`)",
    )
    args = ap.parse_args()

    PINNED = {"msft": "dtmf_star", "qcom": "dtmf_star", "gis": "dtmf_star",
              "wmt": "dtmf_star", "cat": "hybrid", "aapl": "curated"}

    # Synthetic cases: verbatim operator strings for issuers we hold no local
    # transcript for. These are NOT decoration -- the GOOGL case caught a live
    # false negative (the "*1" glyph) on the largest near-term event, which the
    # 6-file corpus regression could never have caught because no corpus file
    # uses the glyph. Every open-board issuer gets a case here.
    SYNTHETIC = [
        ("GOOGL *1 glyph", "dtmf_star",
         "At this time, all participants are in a listen only mode. To ask a question "
         "during the session, you will need to press *1 on your telephone."),
        ("GEV bare 'press 11'", "dtmf_star",
         "At this time, all participants are in a listen-only mode. Please press 11 on your telephone."),
        ("DPZ/INTC star 11", "dtmf_star",
         "all participants are in a listen-only mode. To ask a question during the session, "
         "you will need to press star 11 on your telephone."),
        ("UAL star-then-number", "dtmf_star",
         "If you would like to ask a question, please press star, then the number one on "
         "your telephone keypad."),
        ("NVDA star-then-number", "dtmf_star",
         "in order to ask a question, press star, then the number 1 on your telephone keypad."),
        ("AMZN star + handset", "dtmf_star",
         "please press star 1 on your keypad. We ask that when you pose your question, you "
         "pick up your handsets to provide optimum sound quality."),
        ("META handset, no star", "phone_present",
         "Please pick up your handset before asking your question to ensure clarity. If you "
         "are streaming today's call, please mute your computer speakers."),
        ("TSLA raise-hand/unmute", "webcast_hand",
         "welcome to Tesla's first quarter 2026 Q&A webcast. Please use the raise hand button "
         "to join the question queue, and unmute yourself when you're ready."),
    ]

    rows, failures = [], []
    for p in sorted(args.transcripts):
        if not p.exists():
            print(f"  MISSING {p}", file=sys.stderr)
            continue
        text = p.read_text(errors="replace")
        r = classify(text)
        stem = p.stem
        rows.append((stem, r))

        if args.require_opening and r["qa_mechanism"] in ("curated", "unknown", "webcast_hand"):
            if not r["signals"]["has_opening"]:
                print(f"  ⚠ {stem}: non-positive verdict but no call opening found — "
                      f"transcript may be truncated. Refusing to report a negative.", file=sys.stderr)
                r["qa_mechanism"] = "unknown"

        if args.json:
            out = pathlib.Path("corpus/structure") / f"{stem}.dial-in.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({
                "id": stem.upper(),
                "transcript": str(p),
                "model": "rule-based-operator-preamble",
                "qa_mechanism": r["qa_mechanism"],
                "listen_access": "unknown",  # NEVER derivable here — see module docstring
                "evidence_quote": r["evidence_quote"],
                "signals": r["signals"],
            }, indent=2) + "\n")

        if args.check:
            key = next((k for k in PINNED if stem.startswith(k)), None)
            if key and r["qa_mechanism"] != PINNED[key]:
                failures.append(f"{stem}: expected {PINNED[key]}, got {r['qa_mechanism']}")

    print(f"{'TRANSCRIPT':24s} {'VERDICT':13s} {'star':>4s} {'bare':>4s} {'lstn':>4s} "
          f"{'kpad':>4s} {'hand':>4s} {'dial':>4s} {'open':>4s}")
    for stem, r in rows:
        s = r["signals"]
        print(f"{stem:24s} {r['qa_mechanism']:13s} {s['star_dtmf']:>4d} {s['bare_keypad']:>4d} "
              f"{s['listen_only']:>4d} {s['keypad']:>4d} {s['raise_hand']:>4d} "
              f"{s['dialed_in']:>4d} {'yes' if s['has_opening'] else 'NO':>4s}")

    print("\nqa_mechanism only — listen_access is NOT determined here. A `curated`/`none`\n"
          "verdict is a prompt to read the IR page, never a no-go. (The Apple rule.)")

    if args.check:
        for name, expect, text in SYNTHETIC:
            got = classify(text)["qa_mechanism"]
            if got != expect:
                failures.append(f"synthetic {name}: expected {expect}, got {got}")
        if failures:
            print("\nREGRESSION FAILED:", file=sys.stderr)
            for f in failures:
                print(f"  {f}", file=sys.stderr)
            return 1
        print(f"\nregression OK — {len(PINNED)} pinned corpus verdicts + "
              f"{len(SYNTHETIC)} synthetic operator strings reproduced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
