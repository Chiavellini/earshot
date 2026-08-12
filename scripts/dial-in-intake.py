#!/usr/bin/env python3
"""Dial-in intake lint: decide whether a published credential is DTMF or SPOKEN.

    scripts/dial-in-intake.py --key HIMS --file release.txt
    pbpaste | scripts/dial-in-intake.py --key HIMS
    scripts/dial-in-intake.py --check          # regression against the pinned table

WHY THIS EXISTS — HIMS, 2026-08-10, 17 minutes of a live call lost.

The release said:

    "...dialing +1 8XX-XXX-XXXX for U.S. participants ... **referencing**
     Meeting ID: 7XX XXX XXX"

That id went into state/hunt/numbers.env after the comma, i.e. as DTMF. It was
never a passcode. The bridge's own IVR split the two credentials explicitly —
"if you know your **unique passcode**, please enter it now; otherwise to speak
to the next available **operator**, stay on the line" — so every silent leg fell
through to a human screener, was asked for a name, heard nothing, and was hung
up. 17 legs, 31m47s of blind retrying, zero coverage until a human was paged in.

**The tell was one word, and it was in the release.** "Referencing" governs a
credential you SAY to a person. "Enter" governs a credential you KEY. This script
reads the governing verb and refuses to emit a DTMF suffix for the first class.

It runs on pasted text, places no calls, and costs nothing. Run it at intake,
BEFORE writing numbers.env — that is where 2026-08-10 should have been caught.

## What it does NOT do

It does not set `admission`. Only a real probe dial does that (the MSFT RULE,
.claude/skills/phone-event-prep). This emits a *prior* — what to expect and how
to prepare — never a verdict.

## Digit safety

Digits are PRINTED (that is the deliverable: the line you paste into the
gitignored store) but never written to any file. This script has no write path.
Use --redact when pasting output into a commit, an issue, or a doc.

Exit codes:
    0  DTMF path — credential is keyed; the numbers.env line carries a suffix
    3  SPOKEN path — operator screen expected; prepare [operator_handler]
    4  AMBIGUOUS — governing verb unclear or conflicting; a human must read it
    5  no participant dial-in found in the text
"""

import argparse
import re
import sys

# ── Credential nouns ──────────────────────────────────────────────────────
# Ordered longest-first so "conference id" wins over a bare "id".
CRED_NOUN = re.compile(
    r"\b("
    r"unique\s+passcode|personal\s+passcode|participant\s+(?:pass)?code|"
    r"conference\s+id(?:entification)?(?:\s+number)?|meeting\s+id|webcast\s+id|"
    r"event\s+id|access\s+code|entry\s+(?:code|number)|confirmation\s+(?:code|number)|"
    r"passcode|pass\s?code|pin\s+number|\bpin\b|conference\s+code|replay\s+(?:pass)?code"
    r")\b",
    re.I,
)

# Verbs that govern a credential you KEY into the phone.
DTMF_VERB = re.compile(
    r"\b(enter(?:ing)?|key(?:\s+in)?|input(?:ting)?|press(?:ing)?|type|punch|dial(?:ing)?\s+in\s+the)\b",
    re.I,
)
POUND = re.compile(r"followed\s+by\s+(?:the\s+)?(?:pound|hash|#|number\s+sign)", re.I)

# Verbs that govern a credential you SAY to a human. "reference" is the one that
# cost us HIMS; the rest are the same speech act.
SPOKEN_VERB = re.compile(
    r"\b(referenc\w*|quot\w+|cit\w+|mention\w*|ask(?:ing)?\s+for|"
    r"request\w*|provid\w+|giv\w+|stat\w+|say\w*|identify\w*)\b",
    re.I,
)

# Registration-gated: the credential does not exist until you sign up, and is
# usually per-registrant (so it is never publishable, and never shared).
REGISTRATION = re.compile(
    r"\b(register(?:ing|ed|ation)?|pre-?register\w*|upon\s+registering|sign\s+up)\b", re.I
)

# A "Meeting ID" sitting next to webcast language is the WEBCAST's id, not a
# bridge credential. Feeding it to the bridge is a guaranteed silent failure.
WEBCAST_CTX = re.compile(r"\b(webcast|web\s?cast|live\s+audio|listen[-\s]?only\s+webcast|streaming)\b", re.I)

# Replay/rebroadcast lines are not the live call. Dialing one captures nothing.
REPLAY_CTX = re.compile(r"\b(replay|rebroadcast|archive[d]?|available\s+(?:for|until))\b", re.I)

# ── Phone numbers ─────────────────────────────────────────────────────────
# The trailing guard must reject a CONTINUING number ("555-0110-99", "…0110.5")
# without rejecting a sentence-ending period — "may dial 1-888-555-0110." is the
# common shape in a press release and an earlier `(?![\d\-.])` silently dropped
# every number that ended a sentence.
PHONE = re.compile(
    r"(?<![\d\-.])(?:\+?1[\s.\-]?)?"          # optional US country code
    r"(?:\(\d{3}\)|\d{3})[\s.\-]\d{3}[\s.\-]\d{4}"
    r"(?![\d\-]|\.\d)"
    r"|"
    r"(?<![\d\-.])\+\d{1,3}[\s.\-]\d{1,4}[\s.\-]\d{3,4}[\s.\-]?\d{0,4}(?![\d\-]|\.\d)"
)

ROLE_HINT = [
    (re.compile(r"\btoll[-\s]?free\b", re.I), "toll-free"),
    (re.compile(r"\b(u\.?s\.?|domestic|north america|us/canada|canada)\b", re.I), "US/domestic"),
    (re.compile(r"\binternational\b", re.I), "international"),
    (re.compile(r"\breplay\b", re.I), "REPLAY (not the live call)"),
]


def _ctx(text: str, start: int, end: int, back: int = 70, fwd: int = 45) -> str:
    return text[max(0, start - back) : min(len(text), end + fwd)]


def _digits_near(text: str, end: int, window: int = 60):
    """Grab the digit group a credential noun introduces (spaces/dashes ok)."""
    m = re.search(r"[:\s#]*([0-9][0-9\s.\-]{3,})", text[end : end + window])
    if not m:
        return None
    raw = m.group(1)
    # Trim a trailing sentence tail: "782 611 911. A live audio webcast..."
    raw = re.split(r"[.,;)]\s|\s{2,}|\bor\b", raw)[0]
    digits = re.sub(r"\D", "", raw)
    return digits if 3 <= len(digits) <= 15 else None


def find_numbers(text: str):
    """Each number owns the text from itself up to the NEXT number.

    Role words follow the number they describe ("dial X for U.S. participants
    and Y for international participants"), so a fixed backward window bleeds
    the previous number's label onto this one — which labelled HIMS's
    international line "US/domestic, international".
    """
    ms = [m for m in PHONE.finditer(text) if len(re.sub(r"\D", "", m.group(0))) >= 7]
    out, seen = [], set()
    for i, m in enumerate(ms):
        raw = m.group(0).strip()
        digits = re.sub(r"\D", "", raw)
        e164 = "+" + digits if raw.startswith("+") else ("+1" + digits if len(digits) == 10 else "+" + digits)
        if e164 in seen:
            continue
        seen.add(e164)
        stop = ms[i + 1].start() if i + 1 < len(ms) else min(len(text), m.end() + 70)
        ctx = text[m.end() : stop]
        if i == 0:  # the first number can be introduced ("Toll-free: X")
            ctx = text[max(0, m.start() - 30) : m.start()] + " " + ctx
        roles = [lbl for rx, lbl in ROLE_HINT if rx.search(ctx)]
        out.append({"raw": raw, "e164": e164, "roles": roles})
    return out


def find_credentials(text: str):
    out = []
    for m in CRED_NOUN.finditer(text):
        noun = m.group(1).lower()
        digits = _digits_near(text, m.end())
        ctx = _ctx(text, m.start(), m.end())
        spoken = SPOKEN_VERB.search(ctx)
        dtmf = DTMF_VERB.search(ctx) or POUND.search(ctx)
        if spoken and not dtmf:
            klass = "spoken"
        elif dtmf and not spoken:
            klass = "dtmf"
        elif dtmf and spoken:
            klass = "conflict"
        else:
            klass = "unmarked"
        out.append({
            "noun": noun,
            "digits": digits,
            "class": klass,
            "verb": (spoken or dtmf).group(0) if (spoken or dtmf) else None,
            "registration": bool(REGISTRATION.search(ctx)),
            "webcast_ctx": bool(WEBCAST_CTX.search(ctx)),
            "replay_ctx": bool(REPLAY_CTX.search(ctx)),
            "ctx": " ".join(ctx.split()),
        })
    return out


def classify(text: str):
    nums = find_numbers(text)
    creds = find_credentials(text)
    live_nums = [n for n in nums if "REPLAY (not the live call)" not in n["roles"]]
    usable = [c for c in creds if c["digits"] and not c["replay_ctx"]]

    # Registration is checked BEFORE the no-number exit on purpose: a
    # registration-gated venue legitimately publishes NO number and NO code —
    # that absence is the finding, not a parse failure.
    if any(c["registration"] for c in creds) or (
        not live_nums and REGISTRATION.search(text) and CRED_NOUN.search(text)
    ):
        verdict = "registration_gated"
    elif not live_nums:
        return {"verdict": "no_dial_in", "numbers": nums, "creds": creds,
                "live_numbers": [], "exit": 5}
    elif not usable:
        verdict = "no_credential"
    elif any(c["class"] == "spoken" for c in usable):
        verdict = "spoken"
    elif any(c["class"] == "conflict" for c in usable):
        verdict = "ambiguous"
    elif all(c["class"] == "dtmf" for c in usable):
        verdict = "dtmf"
    else:
        verdict = "ambiguous"  # unmarked: the release never says what to do with it

    # A credential that only ever appears beside webcast language is the
    # webcast's id no matter which verb governs it.
    if usable and all(c["webcast_ctx"] and not c["class"] == "dtmf" for c in usable):
        verdict = "spoken" if verdict == "spoken" else "ambiguous"

    exits = {"dtmf": 0, "spoken": 3, "ambiguous": 4, "registration_gated": 4,
             "no_credential": 4, "no_dial_in": 5}
    return {"verdict": verdict, "numbers": nums, "creds": creds,
            "live_numbers": live_nums, "exit": exits[verdict]}


def pick_number(live_nums):
    """us-east-1 origin: US toll-free first, then US/domestic, then whatever."""
    for want in ("toll-free", "US/domestic"):
        for n in live_nums:
            if want in n["roles"]:
                return n
    return live_nums[0] if live_nums else None


def render(res, key, redact=False):
    def d(s):
        return re.sub(r"\d", "X", s) if redact else s

    L = []
    v = res["verdict"]
    banner = {
        "dtmf": "DTMF PATH — the credential is keyed into the phone",
        "spoken": "SPOKEN PATH — the credential is said to a HUMAN. Expect an operator screen.",
        "ambiguous": "AMBIGUOUS — a human must read this before any digits are stored",
        "registration_gated": "REGISTRATION-GATED — the credential is per-registrant and does not exist yet",
        "no_credential": "NO CREDENTIAL FOUND beside the number",
        "no_dial_in": "NO PARTICIPANT DIAL-IN in this text",
    }[v]
    L.append(f"verdict:  {v.upper()}")
    L.append(f"          {banner}")
    L.append("")

    if res["numbers"]:
        L.append("numbers found:")
        for n in res["numbers"]:
            roles = ", ".join(n["roles"]) or "unlabelled"
            L.append(f"  {d(n['e164']):<18} {roles}")
        L.append("")

    if res["creds"]:
        L.append("credentials found:")
        for c in res["creds"]:
            flags = []
            if c["registration"]:
                flags.append("registration")
            if c["webcast_ctx"]:
                flags.append("WEBCAST-ADJACENT")
            if c["replay_ctx"]:
                flags.append("replay")
            fl = f"  [{', '.join(flags)}]" if flags else ""
            verb = f"governed by '{c['verb']}'" if c["verb"] else "NO governing verb"
            L.append(f"  {c['noun']:<22} {d(c['digits'] or '—'):<14} {c['class']:<9} {verb}{fl}")
        L.append("")

    num = pick_number(res.get("live_numbers") or [])
    if num:
        L.append("state/hunt/numbers.env  (gitignored, mode 600 — never a tracked file):")
        if v == "dtmf":
            cred = next(c for c in res["creds"] if c["digits"] and c["class"] == "dtmf")
            L.append(f"  {key}={d(num['e164'])},{d(cred['digits'])}#")
        else:
            L.append(f"  {key}={d(num['e164'])}")
            L.append("  ^ NUMBER ONLY. No comma, no DTMF suffix.")
        L.append("")

    L.append("what to do next:")
    if v == "dtmf":
        L.extend([
            "  - probe in-window to establish `admission` (the MSFT RULE: only a real dial sets it)",
            "  - standard unattended path; no operator handler needed unless the probe says otherwise",
        ])
    elif v == "spoken":
        L.extend([
            "  - admission PRIOR: expect `operator_screened`. This is not a verdict — probe to confirm.",
            "  - the id is the reference you GIVE the screener; it is not a passcode. Do not send it as DTMF.",
            "  - prepare the operator path NOW, not as a fallback:",
            "      config:   [operator_handler] enabled = true + cell_number = \"+E164\"",
            "      registry: operator_handler = \"rehearsed-YYYY-MM-DD\" (set only by a passed rehearsal)",
            "  - there may ALSO be a separate per-registrant passcode that DOES key in. If you have one,",
            "    that is the only thing that belongs after the comma.",
            "  - be reachable at the fire minute: the page must be answered or the dial is refused.",
        ])
    elif v == "registration_gated":
        L.extend([
            "  - register first (docs/DIAL-INS.md; use the Actinver analyst identity, never personal gmail)",
            "  - the passcode is per-registrant: it is yours alone, and it goes in numbers.env only",
        ])
    else:
        L.extend([
            "  - do NOT write digits to numbers.env yet",
            "  - find the governing verb in the source release; if it is absent, treat as SPOKEN and",
            "    prepare the operator path — that failure mode is recoverable, a silent leg is not",
        ])
    return "\n".join(L)


# ── Regression pin ────────────────────────────────────────────────────────
# HIMS is VERBATIM from the 2026-08-10 earnings release (the case this script
# exists for), with digits altered — the classifier keys on words, not values.
# The rest are SYNTHETIC canonical phrasings, labelled as such; they encode the
# shapes, not observed releases.
PINNED = [
    ("hims-26aug10 (REAL)",
     "The conference call can be accessed by dialing +1 833-461-5787 for U.S. "
     "participants and +1 585-542-9983 for international participants, referencing "
     "Meeting ID: 782 611 911. A live audio webcast will be available at "
     "https://investors.hims.com and will be archived for one year",
     "spoken"),
    ("dtmf-passcode (SYNTHETIC)",
     "To join, dial 1-800-555-0199 (toll-free) or +1 212-555-0143 (international) "
     "and enter passcode 4471925 followed by the pound sign.",
     "dtmf"),
    ("dtmf-conference-id (SYNTHETIC)",
     "U.S. participants may dial (877) 555-0134; please enter conference ID 6620418 "
     "when prompted.",
     "dtmf"),
    ("registration (SYNTHETIC, phrasing from RACE FY25 instructions)",
     "Conference call participants must register in advance. Upon registering, each "
     "participant will be provided with Participant Dial-In Numbers and a Personal Passcode.",
     "registration_gated"),
    ("bare-number (SYNTHETIC)",
     "A live call will be held; U.S. participants may dial 1-888-555-0110.",
     "no_credential"),
]


def run_check() -> int:
    bad = 0
    for name, text, want in PINNED:
        got = classify(text)["verdict"]
        ok = got == want
        bad += not ok
        print(f"  {'OK  ' if ok else 'FAIL'} {name:<48} want={want:<19} got={got}")
    if bad:
        print(f"\nregression FAILED — {bad} of {len(PINNED)} mismatched", file=sys.stderr)
        return 1
    print(f"\nregression OK — {len(PINNED)} pinned intake verdicts "
          f"(1 real: hims-26aug10; 4 synthetic)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", default="TICKER", help="registry key for the numbers.env line")
    ap.add_argument("--file", help="file with the release / confirmation text (default: stdin)")
    ap.add_argument("--text", help="the text inline")
    ap.add_argument("--redact", action="store_true", help="mask digits in the output")
    ap.add_argument("--check", action="store_true", help="assert the pinned regression table")
    a = ap.parse_args()

    if a.check:
        return run_check()

    if a.text:
        text = a.text
    elif a.file:
        text = open(a.file, encoding="utf-8", errors="replace").read()
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        ap.error("give --text, --file, or pipe the release on stdin")

    res = classify(text)
    print(render(res, a.key, redact=a.redact))
    return res["exit"]


if __name__ == "__main__":
    sys.exit(main())
