#!/usr/bin/env python3
"""Record a bridge-probe's OWN admission verdict into configs/dial-ins.toml.

WHY THIS EXISTS (2026-08-04, SPOT). A toll-free number and a numeric PIN
admitted us to Spotify's live Q2 call at 12:02Z and held 598 s of clean audio.
That proof existed only in a gitignored transcript header, because
`scripts/bridge-probe.sh` ended by PRINTING a paste-ready snippet for a human.
Nobody pasted it. `configs/dial-ins.toml` therefore had no `[SPOT]` section, so
`slate-stage.sh` read `admission = no-entry`, staged webcast-only, and never
derived a phone arm. An 18-market board traded 157,217 contracts while we
listened to the call and armed nothing.

THE MSFT RULE IS PRESERVED, NOT BENT. `admission` may still be set ONLY by a
probe dial — never from an IR page, never from the hunt. This script does not
create a new authority; it lets the probe write down the answer it already got
instead of asking a human to retype it. `registry-propose.py` remains the
propose-only path for PAPER evidence, and that stays human-audited.

AUTO-PROMOTION IS DELIBERATELY NARROW. Only `held` is safe to infer from
mechanical facts (the leg ran to --secs with frames flowing).
`operator_screened` and `dropped` are transcript readings — a human decides
those and passes --admission explicitly. Anything else writes NOTHING.

Comment-preserving by construction: this does surgical line edits, never
tomllib-parse-and-dump. The comments in dial-ins.toml (THE MSFT RULE, THE APPLE
RULE, per-venue history) are load-bearing documentation and a round-trip would
silently delete every one of them.

Usage:
  scripts/registry-admit.py --issuer SPOT --admission held \
      --detail "held 598.4s to the probe cap; 11 stalls; DTMF accepted" \
      --probed 2026-08-04 --artifact corpus/bridge-probes/spot_2026_08_04.wav \
      [--number-src URL] [--evidence TEXT] [--registry configs/dial-ins.toml] \
      [--dry-run]
  scripts/registry-admit.py --check      # offline self-test, no file touched
"""
import argparse
import datetime
import re
import sys

# Only these three are legal admission verdicts to write. `untested` is the
# absence of a probe and is never written by a probe; `passcode_required`
# (GRAB 2026-08-03) is a real fourth outcome that a human records by hand
# because it needs the transcript to distinguish from `dropped`.
VERDICTS = ("held", "dropped", "operator_screened", "passcode_required")
AUTO_OK = ("held",)

# Written into a NEWLY created section. Every existing entry carries all eight,
# so a new one must too or the next reader hits a missing key.
NEW_SECTION_DEFAULTS = {
    "tier": "published",
    "qa_mechanism": "unknown",
    "listen_access": "yes",
    "evidence": "",
    "evidence_src": "",
    "number_src": "",
    "confidence": "high",
    "last_verified": "",
}
# Field order inside a section: admission block sits after listen_access and
# before evidence, matching [YUM] / [GRAB].
ADMISSION_FIELDS = ("admission", "admission_detail", "admission_probed",
                    "probe_artifact")


def toml_str(s):
    """Escape a value for a TOML basic string. Never emits a multi-line form."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') \
                       .replace("\n", " ").replace("\r", " ") + '"'


def split_sections(lines):
    """[(key_or_None, start, end)] over the file, aware of \"\"\" blocks.

    A line starting with '[' inside a triple-quoted `notes` value is NOT a
    section header — [HOOD]'s notes contain bracketed prose, and a naive
    ^\\[ scan would cut its entry in half and corrupt the file.
    """
    spans, cur_key, cur_start, in_triple = [], None, 0, False
    for i, ln in enumerate(lines):
        if in_triple:
            if '"""' in ln:
                in_triple = False
            continue
        # opening triple quote that does not also close on the same line
        if '"""' in ln and ln.count('"""') % 2 == 1:
            in_triple = True
            continue
        m = re.match(r"^\[([A-Za-z0-9._-]+)\]\s*$", ln)
        if m:
            if cur_key is not None or i > cur_start:
                spans.append((cur_key, cur_start, i))
            cur_key, cur_start = m.group(1), i
    spans.append((cur_key, cur_start, len(lines)))
    return spans


def find_section(lines, key):
    for k, s, e in split_sections(lines):
        if k == key:
            return s, e
    return None


def _strip_fields(body, names):
    """Drop existing `name = ...` lines (single-line values only)."""
    pat = re.compile(r"^(" + "|".join(re.escape(n) for n in names) + r")\s*=")
    return [ln for ln in body if not pat.match(ln)]


def upsert(text, key, values, *, create_defaults=None):
    """Set `values` inside section `key`, creating the section if absent.

    Returns (new_text, action) where action is 'updated' | 'created'.
    """
    lines = text.splitlines(keepends=True)
    span = find_section(lines, key)

    block = [f"{n:<16} = {toml_str(values[n])}\n"
             for n in ADMISSION_FIELDS if values.get(n) not in (None, "")]

    if span is None:
        if create_defaults is None:
            raise KeyError(f"[{key}] absent and no defaults supplied")
        d = dict(NEW_SECTION_DEFAULTS)
        d.update({k: v for k, v in create_defaults.items() if v})
        d.setdefault("last_verified", values.get("admission_probed", ""))
        if not d["last_verified"]:
            d["last_verified"] = values.get("admission_probed", "")
        out = [f"\n[{key}]\n"]
        for n in ("tier", "qa_mechanism", "listen_access"):
            out.append(f"{n:<13} = {toml_str(d[n])}\n")
        out += block
        for n in ("evidence", "evidence_src", "number_src", "confidence",
                  "last_verified"):
            out.append(f"{n:<13} = {toml_str(d[n])}\n")
        body = "".join(out)
        if not text.endswith("\n"):
            text += "\n"
        return text + body, "created"

    s, e = span
    head, body, tail = lines[:s + 1], lines[s + 1:e], lines[e:]
    body = _strip_fields(body, ADMISSION_FIELDS)
    # Insert after listen_access when present (the [YUM] convention), else at
    # the top of the section so the verdict is never buried under prose.
    idx = next((i + 1 for i, ln in enumerate(body)
                if re.match(r"^listen_access\s*=", ln)), 0)
    body = body[:idx] + block + body[idx:]
    # A fresh probe is fresh verification.
    if values.get("admission_probed"):
        for i, ln in enumerate(body):
            if re.match(r"^last_verified\s*=", ln):
                body[i] = f"{'last_verified':<13} = " \
                          f"{toml_str(values['admission_probed'])}\n"
                break
    return "".join(head + body + tail), "updated"


PROBED_NUMBERS_HEADER = (
    "# control.bridge.numbers — PROBE-PROVEN digits, written by "
    "scripts/registry-admit.py.\n"
    "# Distinct from state/hunt/numbers.env (which hunt-dial-in.py regenerates\n"
    "# and must never be hand-edited): every line here is a number this project\n"
    "# actually DIALED and was admitted on, with the date it was proven.\n"
    "# Gitignored, 0600 — these digits rotate per call.\n"
    "# A number here is still NOT admission; configs/dial-ins.toml says that.\n"
)


def upsert_number(text, key, number, passcode, probed):
    """Set `KEY=+E164[,passcode#]` in a numbers.env-shaped file.

    Value stays comment-free: consumers parse it with a bare `^KEY=` grep and a
    trailing `# note` would land inside the dialed string.
    """
    val = number + (f",{passcode}" if passcode else "")
    line = f"{key}={val}\n"
    note = f"# {key} probed {probed}\n"
    lines = text.splitlines(keepends=True) if text else []
    out, replaced, skip_next = [], False, False
    for ln in lines:
        if skip_next:
            skip_next = False
            continue
        if re.match(rf"^#\s*{re.escape(key)}\s+probed\b", ln):
            continue
        if re.match(rf"^{re.escape(key)}=", ln):
            out += [note, line]
            replaced = True
            continue
        out.append(ln)
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out += [note, line]
    return "".join(out)


def check():
    """Offline self-test. Touches no real file."""
    sample = (
        '# header comment — MUST SURVIVE\n'
        '[AAA]\n'
        'tier          = "published"\n'
        'qa_mechanism  = "unknown"\n'
        'listen_access = "yes"\n'
        'evidence      = "e"\n'
        'evidence_src  = "u"\n'
        'number_src    = "n"\n'
        'confidence    = "high"\n'
        'last_verified = "2026-01-01"\n'
        'notes         = """multi-line note\n'
        'with a [BRACKETED] line that is NOT a section\n'
        'and more prose"""\n'
        '\n'
        '[BBB]\n'
        'tier          = "none"\n'
        'listen_access = "no"\n'
        'last_verified = "2026-02-02"\n'
    )
    # the [BRACKETED] line inside """ must not be read as a section
    keys = [k for k, _, _ in split_sections(sample.splitlines(keepends=True))]
    assert keys == [None, "AAA", "BBB"], f"triple-quote blindness: {keys}"

    v = {"admission": "held", "admission_detail": "held 598.4s",
         "admission_probed": "2026-08-04", "probe_artifact": "corpus/x.wav"}
    out, act = upsert(sample, "AAA", v)
    assert act == "updated"
    assert "# header comment — MUST SURVIVE" in out, "comments were destroyed"
    assert "[BRACKETED] line" in out, "multi-line notes were corrupted"
    assert 'admission        = "held"' in out
    # placed after listen_access, before evidence — the [YUM] convention
    assert out.index("listen_access") < out.index("admission ") < out.index("evidence "), \
        "admission block landed in the wrong place"
    assert 'last_verified = "2026-08-04"' in out, "probe must refresh last_verified"
    assert 'last_verified = "2026-02-02"' in out, "touched the wrong section"

    # idempotent: applying twice must not duplicate the block
    out2, _ = upsert(out, "AAA", v)
    assert out2.count('admission        = "held"') == 1, "not idempotent"
    assert out2 == out, "second apply changed the file"

    # creating an absent section
    out3, act3 = upsert(sample, "SPOT", v, create_defaults={
        "number_src": "https://example.invalid/ir"})
    assert act3 == "created"
    assert "\n[SPOT]\n" in out3 and 'admission        = "held"' in out3
    assert 'number_src    = "https://example.invalid/ir"' in out3
    assert 'last_verified = "2026-08-04"' in out3, "new section needs a date"
    import tomllib
    parsed = tomllib.loads(out3)
    assert parsed["SPOT"]["admission"] == "held"
    assert set(NEW_SECTION_DEFAULTS) <= set(parsed["SPOT"]), \
        f"new section missing required keys: {sorted(parsed['SPOT'])}"
    assert parsed["AAA"]["notes"].startswith("multi-line note")

    # a quote in the detail must not break the file
    v2 = dict(v, admission_detail='he said "hello" then hung up')
    tomllib.loads(upsert(sample, "AAA", v2)[0])

    # absent section with no defaults is an error, not a silent no-op
    try:
        upsert(sample, "ZZZ", v)
        raise AssertionError("must refuse to create without defaults")
    except KeyError:
        pass

    # ── proven-number persistence ────────────────────────────────────────────
    n = upsert_number("", "SPOT", "+18883302388", "570502021#", "2026-08-04")
    assert "SPOT=+18883302388,570502021#\n" in n, n
    assert "# SPOT probed 2026-08-04\n" in n
    # the dialable value must carry NO trailing comment — consumers grep ^KEY=
    val = [l for l in n.splitlines() if l.startswith("SPOT=")][0]
    assert "#" not in val.split("=", 1)[1].rstrip("#"), f"comment leaked: {val}"
    n2 = upsert_number(n, "PGR", "+18445430451", "1203363#", "2026-08-04")
    assert n2.count("SPOT=") == 1 and n2.count("PGR=") == 1
    # re-proving replaces in place rather than appending a second line
    n3 = upsert_number(n2, "SPOT", "+19995551234", "", "2026-09-01")
    assert n3.count("SPOT=") == 1, "duplicate number line"
    assert "SPOT=+19995551234\n" in n3 and "570502021#" not in n3
    assert "# SPOT probed 2026-09-01\n" in n3 and n3.count("# SPOT probed") == 1
    assert "PGR=+18445430451,1203363#\n" in n3, "clobbered a sibling"

    print("registry-admit --check: OK (comment preservation, triple-quote "
          "section split, YUM field placement, idempotence, section creation, "
          "quote escaping, last_verified refresh, proven-number upsert)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--issuer")
    ap.add_argument("--admission", choices=VERDICTS)
    ap.add_argument("--detail", default="")
    ap.add_argument("--probed", default=datetime.date.today().isoformat())
    ap.add_argument("--artifact", default="")
    ap.add_argument("--registry", default="configs/dial-ins.toml")
    ap.add_argument("--number-src", default="")
    ap.add_argument("--evidence", default="")
    ap.add_argument("--evidence-src", default="")
    ap.add_argument("--tier", default="")
    ap.add_argument("--listen-access", default="")
    ap.add_argument("--number", default="",
                    help="the E.164 number this probe actually dialed")
    ap.add_argument("--passcode", default="",
                    help="DTMF string that got us in, e.g. 570502021#")
    ap.add_argument("--numbers-out", default="state/hunt/numbers-probed.env")
    ap.add_argument("--auto", action="store_true",
                    help="mechanical promotion: refuse anything but `held`")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        check()
        return 0
    if not (a.issuer and a.admission):
        ap.error("--issuer and --admission required (or --check)")
    if a.auto and a.admission not in AUTO_OK:
        print(f"registry-admit: REFUSING to auto-write admission="
              f"{a.admission!r} — only {AUTO_OK} is inferable from mechanical "
              f"facts. Read the transcript and re-run without --auto.",
              file=sys.stderr)
        return 3

    key = a.issuer.upper()
    text = open(a.registry).read()
    values = {"admission": a.admission, "admission_detail": a.detail,
              "admission_probed": a.probed, "probe_artifact": a.artifact}
    defaults = {"number_src": a.number_src, "evidence": a.evidence,
                "evidence_src": a.evidence_src, "tier": a.tier,
                "listen_access": a.listen_access}
    try:
        out, action = upsert(text, key, values, create_defaults=defaults)
    except KeyError as exc:
        print(f"registry-admit: {exc}", file=sys.stderr)
        return 4

    import tomllib
    try:
        tomllib.loads(out)
    except Exception as exc:
        print(f"registry-admit: REFUSING — the edit would not parse as TOML: "
              f"{exc}", file=sys.stderr)
        return 5

    if a.dry_run:
        print(f"registry-admit: would have {action} [{key}] "
              f"admission={a.admission}"
              + (f"; would record proven number for {key}" if a.number else ""))
        return 0
    open(a.registry, "w").write(out)
    print(f"registry-admit: {action} [{key}] admission={a.admission} "
          f"probed={a.probed} → {a.registry}")

    # Persist the digits that actually worked. Without this, `admission=held`
    # points at a number nobody can find: SPOT's working number survived this
    # morning only inside a launchd plist and a gitignored transcript header.
    if a.number:
        import os
        os.makedirs(os.path.dirname(a.numbers_out) or ".", exist_ok=True)
        prev = ""
        if os.path.exists(a.numbers_out):
            prev = open(a.numbers_out).read()
        if not prev:
            prev = PROBED_NUMBERS_HEADER
        open(a.numbers_out, "w").write(
            upsert_number(prev, key, a.number, a.passcode, a.probed))
        os.chmod(a.numbers_out, 0o600)
        print(f"registry-admit: recorded proven number for {key} "
              f"→ {a.numbers_out} (0600, gitignored)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
