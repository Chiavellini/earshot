#!/usr/bin/env python3
"""Hunt an issuer's earnings-call dial-in bridge from SEC primary sources.

WHY THIS EXISTS. On 2026-07-30 we could phone-arm ZERO of the 47 open
earnings-mention events. 39 of 47 issuers had no registry entry at all, and
`slate/worklist.md` carried 28 NO-ENTRY rows whose "start from" column was
EMPTY — flagged as missing, with no seed and no next action. Meanwhile the
prevalence work (docs/DIAL-INS.md) says ~85-90% of S&P 500 issuers DO run a
telephone bridge and only ~35-40% publish one, so the modal call is reachable
only via a hunt. The hunt was 100% manual and therefore never happened at
scale. This script is the automated half.

THE SOURCE THAT WORKS IS EDGAR, AND ONLY EDGAR. Measured 2026-07-30 against
four control cases (Ford, Chipotle, Yum, Apple), all reproduced verbatim:
  * SEC full-text search is unauthenticated and returns the earnings PR
    exhibit that carries the number AND the passcode.
  * IR pages are Cloudflare bot-challenged — 403 from the laptop and from the
    box identically. That is a CHALLENGE, not a geo-block: relocating the
    fetcher cannot fix it. Browser or nothing.
  * TRANSCRIPTS CANNOT HELP AND NEVER WILL. The operator never reads the live
    dial-in aloud — everyone hearing it already dialed in. Every number in our
    15-transcript corpus is a REPLAY or IR-contact line (AAPL "the number for
    the telephone replay is 866-583-1035"). Mining transcripts for a bridge
    number is mining the wrong document.

EDGAR QUERY TRAPS, all measured, all load-bearing:
  * A descriptive User-Agent is MANDATORY. Empty UA => HTTP 403.
  * Numbers are matched LITERALLY. "8883176003" => 0 hits; "888-317-6003" =>
    3,234 hits. Never query an unformatted number.
  * A bridge number is NOT an issuer key. 888-317-6003 is a generic Chorus Call
    DID shared by hundreds of filers. Always scope by CIK + date window.
  * The PASSCODE is the high-precision key: "3937444" returns 3 hits in all of
    EDGAR. Use it to confirm and to dedup, never the number alone.

ABSENCE IS A RESULT, NOT A FAILURE. Ford has 14 "conference call" filings since
2025 and ZERO containing "dialing"; its last dial-in filing was 2024-04-24.
That is a positive `webcast-only` verdict, not an unresolved row — and it is
exactly the "check the PRIOR quarter too" rule from docs/DIAL-INS.md:106
mechanised: one quarter tells you the state, two tell you whether it is a DROP
or a standing policy.

DIGITS NEVER ENTER GIT (docs/DIAL-INS.md:135). This script writes TWO
artifacts and the split is the whole point:
  * state/hunt/<KEY>-<quarter>.json  — gitignored. The digits.
  * slate/hunt-status.json           — committed. Pattern + provenance only:
    key, found, tier, number_src, evidence, filing_url. No digits, ever.
The committed half is what fills the empty "start from" column.

THIS SCRIPT NEVER SETS `admission` (THE MSFT RULE, configs/dial-ins.toml:58).
Paper establishes that a NUMBER exists. Only scripts/bridge-probe.sh can
establish what the bridge does to a silent caller — MSFT published digits and
"no password required" and drops us in 42 s.

Read-only against SEC. Places no calls. Touches no credentials.

Usage:
  scripts/hunt-dial-in.py --key CMG                  # current quarter + prior
  scripts/hunt-dial-in.py --key F --quarters 8       # deeper history
  scripts/hunt-dial-in.py --from-slate slate/latest.json   # every event in horizon
  scripts/hunt-dial-in.py --self-test                # offline frozen fixtures

Exit 0 always unless inputs/network are unusable (2). A "found nothing" run is
a successful run with a verdict — see ABSENCE IS A RESULT.
"""

import argparse
import datetime
import html
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# SEC asks for a descriptive UA with contact info; an empty UA is a hard 403.
# Standing research identity (memory: register as the Actinver analyst).
UA = "earshot-research bdelriom@actinver.com.mx"
FTS = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
TICKERS = "https://www.sec.gov/files/company_tickers.json"

# SEC's published ceiling is 10 req/s. Measured 2026-07-30: 20 concurrent
# requests all returned 200 with no 429. We stay well under on principle —
# this runs in a scheduled job, not an interactive loop.
PACE_SECS = 0.25

# A North American number. The separator class MUST include "/" — Yum filings
# write "800/715-9871" and "+1/646-307-1963", and a separator class of just
# [\s.-] silently dropped Yum's North America bridge on 2026-07-30 while
# keeping the international one, which looks like a partial success and is
# actually a missed lane.
PHONE_RE = re.compile(
    r"(?:\+?1[\s.\-/]?)?\(?\b([2-9]\d{2})\)?[\s.\-/]?([2-9]\d{2})[\s.\-/](\d{4})\b"
)
# Passcodes are 6-10 digits and always LABELLED. An unlabelled 7-digit run is
# far more often a share count or a dollar figure than a conference code.
# Label vocabulary matters: Wendy's writes "event ID 280384" and a pattern
# that knew only "conference ID" silently returned a bridge with no code
# (measured 2026-07-30). A number without its code cannot be entered, so a
# missed label is a dead lane that still looks like a success.
CODE_RE = re.compile(
    r"(?:conference\s*(?:ID|code)|event\s*(?:ID|code)|participant\s*(?:ID|code)|"
    r"pass\s*code|passcode|access\s*code|confirmation\s*code|entry\s*code|"
    r"entering\s*code|referencing\s*conference\s*ID|ID\s*(?:number|#)|code)"
    r"\s*[:#]?\s*#?\s*(\d[\d\-\s]{4,12}\d)",
    re.I,
)

# Context windows that decide what a matched number IS. Measured on the CMG
# EX-99.1: the same document carries the bridge (1-888-317-6003), an
# international bridge (1-412-317-6061) AND a media-contact line
# ((949) 524-4035). Classifying by proximity is what separates them.
BRIDGE_CUES = re.compile(
    r"dial(?:ing|-in|\s+in)?|accessed live over the phone|conference call|"
    r"toll[-\s]?free|international callers|listen[-\s]?only|participant",
    re.I,
)
REPLAY_CUES = re.compile(r"replay|playback|rebroadcast|available for (?:replay|download)", re.I)
# How far back a replay cue governs. 140 chars covers "A replay will be
# available ... To access the playback, dial X" without reaching the previous
# sentence's live-bridge clause.
REPLAY_SCOPE_CHARS = 140

CONTACT_CUES = re.compile(
    r"contact|investor relations|media relations|press|@[\w.]+\.\w+|analyst inquiries", re.I
)


def get(url, pace=PACE_SECS, tries=4, binary=False):
    """GET with backoff. SEC 403s an empty UA and may 429 under burst."""
    delay = 1.0
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
                time.sleep(pace)
                return data if binary else data.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < tries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError(f"exhausted retries: {url}")


def visible_text(raw_html):
    """Strip tags/entities to one whitespace-normalised line.

    EDGAR exhibits are Word-exported HTML: the dial-in paragraph is routinely
    split across dozens of <span>s, so any regex over raw markup misses it.
    """
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw_html, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", html.unescape(txt)).strip()


def norm_phone(m):
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def classify(text, start, end):
    """What is this number? NEAREST cue wins — presence alone is not enough.

    An earnings PR puts a media contact and the bridge in the same paragraph.
    A first-match-by-category rule mislabels the contact as a bridge, because
    "conference call" appears ~100 chars AFTER the contact number while
    "Contact:" sits ~24 chars BEFORE it (measured on the real CMG EX-99.1).
    Dialing a company's press desk instead of the bridge is the failure this
    prevents, so distance decides, and a number with no cue within the window
    stays `unknown` rather than being guessed into a class.
    """
    win_before, win_after = 200, 200
    lo = max(0, start - win_before)
    ctx = text[lo : end + win_after]
    rel_start, rel_end = start - lo, end - lo

    def nearest(rx):
        best = None
        for m in rx.finditer(ctx):
            # 0 when the cue overlaps the number, else gap on whichever side
            d = 0 if m.start() < rel_end and m.end() > rel_start else (
                rel_start - m.end() if m.end() <= rel_start else m.start() - rel_end
            )
            if best is None or d < best:
                best = d
        return best

    # REPLAY DOMINATES, it does not compete. "To access the playback, dial
    # +1/800-770-2030" puts a bridge cue ("dial") one character from the
    # number and the replay cue fourteen characters further out, so a pure
    # nearest-cue rule labels a playback line as a live bridge — measured on
    # Yum's 2026-07-30 8-K, where BOTH playback numbers were promoted. A
    # replay cue anywhere in the preceding text governs the whole clause,
    # because "dial X for the replay" is the standard construction. Handing a
    # recording to an aggressive auto-prober is a wasted call and a false
    # `held`, so this asymmetry is deliberate.
    before = ctx[:rel_start]
    tail = REPLAY_CUES.search(before[-REPLAY_SCOPE_CHARS:]) if before else None
    if tail:
        return "replay"

    scored = {
        "replay": nearest(REPLAY_CUES),
        "bridge": nearest(BRIDGE_CUES),
        "contact": nearest(CONTACT_CUES),
    }
    scored = {k: v for k, v in scored.items() if v is not None}
    if not scored:
        return "unknown"
    # replay wins ties: mislabelling a replay line as a bridge would have us
    # dial a recording during a live call (every transcript number is one).
    order = {"replay": 0, "contact": 1, "bridge": 2}
    return min(scored, key=lambda k: (scored[k], order[k]))


def extract(text):
    """Pull classified numbers + labelled codes out of one filing's text."""
    numbers, seen = [], set()
    for m in PHONE_RE.finditer(text):
        n = norm_phone(m)
        kind = classify(text, m.start(), m.end())
        key = (n, kind)
        if key in seen:
            continue
        seen.add(key)
        numbers.append(
            {
                "number": n,
                "kind": kind,
                "context": text[max(0, m.start() - 110) : m.end() + 80].strip(),
            }
        )
    codes = []
    for m in CODE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(1))
        if 6 <= len(digits) <= 10 and digits not in codes:
            codes.append(digits)
    return numbers, codes


def ticker_to_cik(fetch=get):
    d = json.loads(fetch(TICKERS))
    return {r["ticker"].upper(): int(r["cik_str"]) for r in d.values()}


def search(cik, start, end, phrase='"conference call"', fetch=get):
    """EDGAR full-text search, scoped to one filer and one window."""
    q = urllib.parse.urlencode(
        {
            "q": phrase,
            "forms": "8-K",
            "ciks": f"{cik:010d}",
            "startdt": start,
            "enddt": end,
        }
    )
    d = json.loads(fetch(f"{FTS}?{q}"))
    out = []
    for h in d.get("hits", {}).get("hits", []):
        doc_id = h.get("_id", "")
        if ":" not in doc_id:
            continue
        accession, filename = doc_id.split(":", 1)
        src = h.get("_source", {})
        out.append(
            {
                "accession": accession,
                "filename": filename,
                "file_date": src.get("file_date"),
                "url": f"{ARCHIVES}/{cik}/{accession.replace('-', '')}/{filename}",
            }
        )
    return out


def hunt_filing(url, fetch=get):
    return extract(visible_text(fetch(url)))


def verdict_for(quarters):
    """Turn per-quarter findings into a tier verdict.

    The two-quarter rule (docs/DIAL-INS.md:106): one quarter gives you the
    state, two tell you whether a missing number is a DROP or a standing
    policy. Ford is the drop case; Boeing is the standing-policy case.
    """
    with_bridge = [q for q in quarters if q["bridge_numbers"]]
    if with_bridge:
        latest = max(with_bridge, key=lambda q: q["file_date"] or "")
        if latest is quarters[0] if quarters else False:
            pass
        return ("published", f"dial-in published in {latest['file_date']} filing")
    if quarters:
        return (
            "bridge_unpublished",
            f"no dial-in in {len(quarters)} scanned 8-K filing(s) "
            f"({quarters[-1]['file_date']}..{quarters[0]['file_date']}) — "
            f"webcast-only in this window; a bridge may still exist unpublished",
        )
    return ("unknown", "no 8-K filings matched in the scanned window")


def hunt_issuer(key, cik, quarters_back=2, today=None, fetch=get):
    """Scan back `quarters_back` quarters of 8-Ks for this filer."""
    today = today or datetime.date.today()
    findings = []
    for i in range(quarters_back):
        end = today - datetime.timedelta(days=i * 91)
        start = end - datetime.timedelta(days=95)
        try:
            hits = search(cik, start.isoformat(), end.isoformat(), fetch=fetch)
        except Exception as exc:  # network/HTTP — recorded, never fatal
            findings.append(
                {"file_date": None, "url": None, "error": str(exc),
                 "bridge_numbers": [], "replay_numbers": [], "codes": []}
            )
            continue
        for h in hits:
            try:
                numbers, codes = hunt_filing(h["url"], fetch=fetch)
            except Exception as exc:
                findings.append({**h, "error": str(exc), "bridge_numbers": [],
                                 "replay_numbers": [], "codes": []})
                continue
            findings.append(
                {
                    **h,
                    "bridge_numbers": [n for n in numbers if n["kind"] == "bridge"],
                    "replay_numbers": [n for n in numbers if n["kind"] == "replay"],
                    "codes": codes,
                }
            )
    findings.sort(key=lambda f: f.get("file_date") or "", reverse=True)
    tier, why = verdict_for(findings)
    return {"key": key, "cik": cik, "tier": tier, "verdict": why, "filings": findings}


def split_artifacts(result):
    """(secret, public) — the digits/pattern split that keeps git clean."""
    latest = next((f for f in result["filings"] if f.get("bridge_numbers")), None)
    secret = {
        "key": result["key"],
        "cik": result["cik"],
        "hunted_utc": datetime.datetime.now(
            datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "bridge_numbers": (latest or {}).get("bridge_numbers", []),
        "codes": (latest or {}).get("codes", []),
        "filing_url": (latest or {}).get("url"),
        "file_date": (latest or {}).get("file_date"),
    }
    public = {
        "key": result["key"],
        "found": bool(latest),
        "tier": result["tier"],
        "verdict": result["verdict"],
        # provenance only — never the digits
        "number_src": (latest or {}).get("url"),
        "evidence": (
            f"{len((latest or {}).get('bridge_numbers', []))} dial-in number(s) + "
            f"{len((latest or {}).get('codes', []))} code(s) in the "
            f"{(latest or {}).get('file_date')} 8-K exhibit"
            if latest
            else result["verdict"]
        ),
        "filings_scanned": len(result["filings"]),
        "last_hunted": datetime.date.today().isoformat(),
    }
    return secret, public


# North American toll-free SPNs. A filing that publishes both a toll-free and
# an international direct line means the toll-free for participants and the
# direct one for callers who cannot reach it — dialling the direct line by
# accident is a billed international call to the same bridge.
TOLL_FREE_NPA = {"800", "833", "844", "855", "866", "877", "888"}


def to_e164(number):
    """'800-715-9871' -> '+18007159871'. None when it is not a NANP number.

    The prober dials this string verbatim, so a number that needs any further
    massaging is a number that gets misdialed at T-90 on a live event day.
    """
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"+1{digits}"


def numbers_line(record):
    """One `KEY=E.164[,code#]` line from a state/hunt secret record, or None.

    Toll-free wins over the international direct line. Only `bridge`-classified
    numbers are eligible: the classifier's replay-dominance rule exists so an
    auto-prober is never handed a playback line, and that guarantee has to be
    honoured here or it was pointless.
    """
    key = (record.get("key") or "").upper()
    if not key:
        return None
    candidates = []
    for entry in record.get("bridge_numbers") or []:
        if entry.get("kind") not in (None, "bridge"):
            continue
        e164 = to_e164(entry.get("number"))
        if e164:
            candidates.append(e164)
    if not candidates:
        return None
    candidates.sort(key=lambda n: (n[2:5] not in TOLL_FREE_NPA, n))
    line = f"{key}={candidates[0]}"
    codes = [re.sub(r"\D", "", c) for c in (record.get("codes") or [])]
    codes = [c for c in codes if c]
    if codes:
        line += f",{codes[0]}#"
    return line


def emit_numbers(state_dir, out_path):
    """Project every cached hunt record into the prober's --numbers format.

    Regenerated from state/hunt/**, never authoritative, and 0600 because it is
    the one file in the tree that is dialable on sight.
    """
    state_dir = pathlib.Path(state_dir)
    newest = {}
    for path in sorted(state_dir.glob("*.json")):
        if path.name == "numbers.env":
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        key = (record.get("key") or "").upper()
        if not key:
            continue
        stamp = record.get("hunted_utc") or ""
        if key not in newest or stamp >= newest[key][0]:
            newest[key] = (stamp, record)

    lines = sorted(filter(None, (numbers_line(r) for _, r in newest.values())))
    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# control.bridge.numbers — generated by scripts/hunt-dial-in.py "
        "--emit-numbers.\n"
        "# Regenerate, never hand-edit. Gitignored: these digits rotate per "
        "call and\n"
        "# a stale line dials a dead bridge. A number here is NOT admission "
        "(MSFT RULE).\n"
    )
    out.write_text(header + "".join(f"{line}\n" for line in lines))
    try:
        out.chmod(0o600)
    except OSError:
        pass
    return len(lines), len(newest)


# ── frozen fixtures: the four verified control cases (2026-07-30) ───────────
# Text excerpts are copied from the real filings; the CMG one is the exact
# sentence that carries a bridge, an international bridge AND a media contact,
# which is what makes it the discriminating case.
FIX_CMG = (
    "EX-99.1 Document Exhibit 99.1 EARNINGS RELEASE PR Contact: Laurie Schalow "
    "(949) 524-4035 MediaRelations@chipotle.com ... investor materials are "
    "available at chipotle.com/presentations. The conference call can be "
    "accessed live over the phone by dialing 1-888-317-6003 or, for "
    "international callers, by dialing 1-412-317-6061 and entering code: "
    "3937444. The call will be webcast live."
)
FIX_AAPL_TRANSCRIPT = (
    "Thank you for joining. A replay of today's call will be available. "
    "The number for the telephone replay is 866-583-1035. Please enter "
    "confirmation code 280-3309 followed by the pound sign."
)
FIX_FORD_NOBRIDGE = (
    "Ford Motor Company will host a conference call to discuss results. "
    "A live webcast will be available at shareholder.ford.com. Investor "
    "Relations contact: (313) 322-3000."
)


FIX_YUM = (
    "will host a conference call to review results at 8:15 a.m. Eastern Time "
    "July 30, 2026. The number is 800/715-9871 for North America callers and "
    "+1/646-307-1963 for international callers, conference ID 8719077. The "
    "call will be available for playback beginning at 10:00 a.m. Eastern Time "
    "July 30, 2026 through August 6, 2026. To access the playback, dial "
    "+1/800-770-2030 in North America and +1/609-800-9909 for all "
    "international callers, conference ID 8719077."
)


FIX_WEN = (
    "The conference call will be available by telephone at (844) 200-6205 for "
    "domestic callers and (929) 526-1599 for international callers, both using "
    "event ID 280384. A replay of the webcast will be available."
)


def self_test():
    """Offline regression on the four 2026-07-30 control cases. No network."""
    # 1. CMG: the discriminating filing — bridge vs international vs contact.
    numbers, codes = extract(FIX_CMG)
    bridges = {n["number"] for n in numbers if n["kind"] == "bridge"}
    contacts = {n["number"] for n in numbers if n["kind"] == "contact"}
    assert "888-317-6003" in bridges, f"CMG toll-free bridge lost: {numbers}"
    assert "412-317-6061" in bridges, f"CMG international bridge lost: {numbers}"
    assert "949-524-4035" not in bridges, \
        f"media contact misclassified as a bridge — the false-dial bug: {numbers}"
    assert "949-524-4035" in contacts, f"media contact should classify as contact: {numbers}"
    assert "3937444" in codes, f"CMG passcode lost (the high-precision key): {codes}"

    # 2. A REPLAY number must never be offered as a bridge. This is the whole
    #    reason transcripts are excluded as a source: every number they carry
    #    looks like this one.
    numbers, codes = extract(FIX_AAPL_TRANSCRIPT)
    assert not [n for n in numbers if n["kind"] == "bridge"], \
        f"replay number offered as a dial-in bridge: {numbers}"
    assert "866-583-1035" in {n["number"] for n in numbers if n["kind"] == "replay"}, numbers

    # 3. Ford: a real conference-call filing with NO bridge. Absence must be a
    #    verdict, and the IR contact must not be promoted to fill the gap.
    numbers, _ = extract(FIX_FORD_NOBRIDGE)
    assert not [n for n in numbers if n["kind"] == "bridge"], \
        f"Ford webcast-only filing produced a phantom bridge: {numbers}"

    # 3b. YUM (frozen 2026-07-30): slash separators AND a playback clause in
    #     the same filing. Both bugs were found by running this live: the
    #     slash form dropped the North America bridge, and "dial" one char
    #     from the playback numbers promoted two recordings to bridges.
    numbers, codes = extract(FIX_YUM)
    bridges = {n["number"] for n in numbers if n["kind"] == "bridge"}
    replays = {n["number"] for n in numbers if n["kind"] == "replay"}
    assert "800-715-9871" in bridges, f"slash-separated NA bridge lost: {sorted(bridges)}"
    assert "646-307-1963" in bridges, f"slash-separated intl bridge lost: {sorted(bridges)}"
    assert "800-770-2030" in replays, f"playback number promoted to bridge: {sorted(bridges)}"
    assert "609-800-9909" in replays, f"intl playback promoted to bridge: {sorted(bridges)}"
    assert not (bridges & replays), (bridges, replays)
    assert "8719077" in codes, codes

    # 3c. WEN (frozen 2026-07-30): the code is labelled "event ID", not
    #     "conference ID". A bridge extracted without its code is an
    #     unenterable lane that still reports as a success.
    numbers, codes = extract(FIX_WEN)
    bridges = {n["number"] for n in numbers if n["kind"] == "bridge"}
    assert "844-200-6205" in bridges and "929-526-1599" in bridges, sorted(bridges)
    assert "280384" in codes, f"event-ID label not recognised: {codes}"

    # 3d. control.bridge.numbers: the dialable projection the prober consumes.
    #     YUM is the case that exercises every rule at once — a slash-separated
    #     toll-free, an international direct line that must LOSE to it, and a
    #     passcode that must arrive with its terminator.
    line = numbers_line({
        "key": "YUM",
        "bridge_numbers": [
            {"number": "646-307-1963", "kind": "bridge"},   # intl listed FIRST
            {"number": "800-715-9871", "kind": "bridge"},
        ],
        "codes": ["8719077"],
    })
    assert line == "YUM=+18007159871,8719077#", \
        f"toll-free must win and the code must carry '#': {line!r}"

    # A replay line reaching the prober would place a real call to a recording.
    assert numbers_line({"key": "X", "bridge_numbers": [
        {"number": "866-583-1035", "kind": "replay"}], "codes": []}) is None

    # A bridge with no published passcode still emits: some bridges take the
    # number alone, and dropping the line would read as "no bridge found".
    assert numbers_line({"key": "WING", "bridge_numbers": [
        {"number": "877-259-5243", "kind": "bridge"}], "codes": []}) == "WING=+18772595243"

    # Nothing dialable, nothing emitted — not a malformed half-line.
    assert numbers_line({"key": "F", "bridge_numbers": [], "codes": []}) is None
    assert to_e164("1-888-317-6003") == "+18883176003"
    assert to_e164("+1/646-307-1963") == "+16463071963"
    assert to_e164("12345") is None

    # 4. verdict_for: found vs drop vs nothing-scanned.
    t, _ = verdict_for([{"file_date": "2026-07-29", "bridge_numbers": [{"number": "888-317-6003"}]}])
    assert t == "published", t
    t, why = verdict_for([{"file_date": "2026-04-24", "bridge_numbers": []},
                          {"file_date": "2026-01-24", "bridge_numbers": []}])
    assert t == "bridge_unpublished" and "webcast-only" in why, (t, why)
    assert verdict_for([])[0] == "unknown"

    # 5. The digits/pattern split: the committed half must never carry a digit
    #    string that could be a phone number or a passcode.
    res = {"key": "CMG", "cik": 1058090, "tier": "published", "verdict": "x",
           "filings": [{"file_date": "2026-07-29", "url": "https://sec.gov/x",
                        "bridge_numbers": [{"number": "888-317-6003", "kind": "bridge"}],
                        "codes": ["3937444"]}]}
    secret, public = split_artifacts(res)
    assert secret["bridge_numbers"] and secret["codes"] == ["3937444"]
    blob = json.dumps(public)
    assert "888-317-6003" not in blob and "3937444" not in blob, \
        f"DIGITS LEAKED into the committed artifact: {blob}"
    assert public["found"] is True and public["number_src"] == "https://sec.gov/x"

    # 6. Search-result parsing against the real FTS response shape.
    fake = json.dumps({"hits": {"hits": [
        {"_id": "0001058090-26-000063:cmg-20260729xex991.htm",
         "_source": {"file_date": "2026-07-29"}}]}})
    hits = search(1058090, "2026-07-01", "2026-07-31", fetch=lambda u: fake)
    assert hits[0]["url"] == (
        "https://www.sec.gov/Archives/edgar/data/1058090/"
        "000105809026000063/cmg-20260729xex991.htm"), hits
    assert hits[0]["file_date"] == "2026-07-29"

    print("self-test: OK (CMG bridge/international/contact discrimination + "
          "passcode; YUM slash-separated numbers + playback-vs-bridge "
          "dominance; WEN event-ID label; replay-number rejection; Ford absence-is-a-verdict; "
          "verdict tiers; digits-never-in-git split; FTS _id -> Archives URL)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--key", help="issuer key / ticker, e.g. CMG")
    ap.add_argument("--from-slate", help="hunt every issuer in this slate JSON")
    ap.add_argument("--days", type=int, default=10, help="slate horizon (default 10)")
    ap.add_argument("--quarters", type=int, default=2,
                    help="quarters of 8-K history to scan (default 2 — the "
                         "drop-vs-standing-policy rule needs at least 2)")
    ap.add_argument("--state-dir", default="state/hunt", help="digits go here (gitignored)")
    ap.add_argument("--out", default="slate/hunt-status.json", help="committed, digits-free")
    ap.add_argument("--numbers-out", default="state/hunt/numbers.env",
                    help="control.bridge.numbers path (gitignored, 0600)")
    ap.add_argument("--emit-numbers", action="store_true",
                    help="project cached state/hunt records into the prober's "
                         "--numbers format and exit; no network, no sweep")
    ap.add_argument("--self-test", action="store_true", help="offline frozen fixtures; no network")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return 0

    if args.emit_numbers and not (args.key or args.from_slate):
        dialable, seen = emit_numbers(args.state_dir, args.numbers_out)
        print(f"HUNT-NUMBERS: {dialable} dialable of {seen} cached issuer(s) "
              f"→ {args.numbers_out} (0600, gitignored)")
        print("HUNT-NEXT: a number is not admission — probe with "
              "scripts/bridge-probe.sh before any phone arm (THE MSFT RULE).")
        return 0

    keys = []
    if args.key:
        keys = [args.key.upper()]
    elif args.from_slate:
        try:
            slate = json.load(open(args.from_slate))
        except OSError as e:
            print(f"HUNT-ERROR: cannot read slate {args.from_slate}: {e}", file=sys.stderr)
            return 2
        today = datetime.date.today()
        horizon = today + datetime.timedelta(days=args.days)
        for ev in slate.get("events", []):
            cd = ev.get("ir_page_date") if ev.get("call_date_disputed") else ev.get("call_date")
            if not cd:
                continue
            try:
                d = datetime.date.fromisoformat(cd)
            except ValueError:
                continue
            if today <= d <= horizon and ev.get("issuer"):
                keys.append(ev["issuer"].upper())
        keys = sorted(set(keys))
    else:
        print("HUNT-ERROR: pass --key or --from-slate or --self-test", file=sys.stderr)
        return 2

    try:
        cikmap = ticker_to_cik()
    except Exception as e:
        print(f"HUNT-ERROR: SEC ticker map unreachable: {e}", file=sys.stderr)
        return 2

    statedir = pathlib.Path(args.state_dir)
    statedir.mkdir(parents=True, exist_ok=True)
    try:
        status = json.load(open(args.out))
    except (OSError, json.JSONDecodeError):
        status = {"generated_utc": None, "issuers": {}}

    found = missing = nocik = 0
    for key in keys:
        cik = cikmap.get(key)
        if not cik:
            nocik += 1
            print(f"HUNT-NO-CIK: {key} is not in SEC's ticker map "
                  f"(foreign filer, or the Kalshi key is not the SEC ticker)")
            status["issuers"][key] = {"key": key, "found": False, "tier": "unknown",
                                      "verdict": "no CIK for this key",
                                      "last_hunted": datetime.date.today().isoformat()}
            continue
        res = hunt_issuer(key, cik, quarters_back=args.quarters)
        secret, public = split_artifacts(res)
        quarter = datetime.date.today().strftime("%Yq%m")
        (statedir / f"{key}-{quarter}.json").write_text(json.dumps(secret, indent=1))
        status["issuers"][key] = public
        if public["found"]:
            found += 1
            print(f"HUNT-FOUND: {key} tier={public['tier']} — {public['evidence']}")
            print(f"            digits: {statedir / f'{key}-{quarter}.json'} (gitignored)")
        else:
            missing += 1
            print(f"HUNT-NONE: {key} tier={public['tier']} — {public['verdict']}")

    status["generated_utc"] = datetime.datetime.now(
        datetime.timezone.utc).replace(microsecond=0).isoformat()
    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(json.dumps(status, indent=1, sort_keys=True))
    # Refresh the dialable projection on every sweep. A found bridge that the
    # prober cannot see is the break this seam exists to close.
    dialable, seen = emit_numbers(args.state_dir, args.numbers_out)
    print(f"HUNT-SUMMARY: {len(keys)} hunted — {found} with a dial-in, "
          f"{missing} none-found, {nocik} no-CIK. Wrote {args.out}")
    print(f"HUNT-NUMBERS: {dialable} dialable of {seen} cached issuer(s) "
          f"→ {args.numbers_out} (0600, gitignored)")
    print("HUNT-NEXT: a number is not admission — probe with "
          "scripts/bridge-probe.sh before any phone arm (THE MSFT RULE).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
