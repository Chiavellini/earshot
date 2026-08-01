#!/usr/bin/env python3
"""hunt verdicts → paste-ready configs/dial-ins.toml entries. Writes nothing.

The hunt has scanned 32 issuers and created zero registry rows, because
`control.hunt` is forbidden from editing the registry (`control.qualify` owns
it) and nothing on the qualify side ever read the hunt's output. So a found
bridge stayed invisible to `slate-stage.sh`, whose phone-vs-webcast decision
reads exactly one registry field.

This closes that gap the way the registry's own rules require: by proposing,
not writing. A human reviews each block and pastes it in. Two invariants make
that non-negotiable —

  THE MSFT RULE   `admission` is established ONLY by a probe dial. Every
                  proposed block therefore carries admission = "untested",
                  never a value inferred from a filing. Published digits plus
                  "all lines have been placed on mute" looked automatable for
                  YUM; the live dial found an operator screen.
  THE APPLE RULE  `qa_mechanism` and `listen_access` are INDEPENDENT. The hunt
                  reads filings, and a filing cannot establish either, so both
                  are proposed as "unknown" rather than guessed.

A `tier = "unknown"` hunt verdict means the hunt COULD NOT LOOK — no 8-K
matched, or the key has no CIK (foreign private issuers file 6-K). That is a
different fact from `bridge_unpublished`, which is a real negative across a
scanned window, and the proposed note says which one it is. Collapsing them is
how a whole venue class stays invisible.

Usage:
  registry-propose.py                                  # every hunted issuer
  registry-propose.py --slate slate/latest.json --days 12   # only upcoming
  registry-propose.py --check                          # offline self-test
"""
import argparse, datetime, json, sys, tomllib
from pathlib import Path

# Mirrors scripts/hunt-dial-in.py's verdict vocabulary.
TIER_NOTE = {
    "published": (
        "PROPOSED from an SEC 8-K exhibit by scripts/hunt-dial-in.py — a "
        "number is on file in the gitignored state/hunt/. Nothing here is "
        "admission: run scripts/bridge-probe.sh before any phone arm."
    ),
    "bridge_unpublished": (
        "PROPOSED from an SEC 8-K sweep that found NO dial-in in the scanned "
        "window. This is a real negative for the ISSUER-PUBLISHED path only — "
        "per the UAL trap it means 'nothing is published to dial', NOT 'no "
        "phone leg exists'."
    ),
    "unknown": (
        "PROPOSED as UNRESOLVED: the hunt could not look here (no 8-K matched, "
        "or no CIK — foreign private issuers file 6-K, which the form filter "
        "excludes). Do NOT read this as webcast-only; it is unresearched. "
        "Needs the browser/IR path."
    ),
}


def propose(hunted, registry, issuers=None):
    """Blocks for hunted issuers absent from the registry, soonest key first."""
    out = []
    for key in sorted(hunted):
        if key in registry:
            continue  # never propose over a human-audited row
        if issuers is not None and key not in issuers:
            continue
        h = hunted[key]
        if not isinstance(h, dict):
            continue
        tier = h.get("tier", "unknown")
        src = h.get("number_src") or ""
        evidence = h.get("evidence") or h.get("verdict") or ""
        note = TIER_NOTE.get(tier, TIER_NOTE["unknown"])
        hunted_on = h.get("last_hunted") or ""
        block = [
            f"[{key}]",
            f'tier          = "{tier}"',
            '# APPLE RULE: a filing cannot establish either of these two.',
            'qa_mechanism  = "unknown"',
            'listen_access = "unknown"',
            f'evidence      = "{_esc(evidence)}"',
            f'evidence_src  = "{_esc(src)}"',
            f'number_src    = "{_esc(src)}"',
            # A machine-derived row is not high-confidence: nobody has looked
            # at the filing, and the tier is only as good as the form filter.
            'confidence    = "medium"',
            f'last_verified = "{_esc(hunted_on)}"',
            # MSFT RULE: stated, not inferred. slate-stage.sh reads this field
            # and stages webcast-only unless it says "held".
            'admission     = "untested"',
            f'notes         = "{_esc(note)} Hunted {_esc(hunted_on)}."',
        ]
        out.append((key, tier, "\n".join(block)))
    return out


def _esc(text):
    return str(text).replace("\\", "\\\\").replace('"', "'")


def slate_issuers(slate, days, today):
    """Issuers with a call inside the horizon, keyed on the believed date."""
    horizon = today + datetime.timedelta(days=days)
    keys = set()
    for ev in slate.get("events", []):
        eff = (ev.get("ir_page_date") if ev.get("call_date_disputed")
               else ev.get("call_date"))
        if not eff or not ev.get("issuer"):
            continue
        try:
            d = datetime.date.fromisoformat(eff)
        except ValueError:
            continue
        if today <= d <= horizon:
            keys.add(ev["issuer"].upper())
    return keys


def check():
    hunted = {
        "WEN": {"found": True, "tier": "published", "last_hunted": "2026-07-30",
                "number_src": "https://sec.gov/wen.htm", "evidence": "1 number + 1 code"},
        "GRAB": {"found": False, "tier": "unknown", "last_hunted": "2026-07-30",
                 "verdict": "no 8-K filings matched in the scanned window"},
        "SNAP": {"found": False, "tier": "bridge_unpublished",
                 "last_hunted": "2026-07-30", "verdict": "no dial-in in 2 filings"},
        "CMG": {"found": True, "tier": "published", "last_hunted": "2026-07-30"},
    }
    registry = {"CMG": {"tier": "published", "admission": "held"}}

    blocks = propose(hunted, registry)
    keys = [k for k, _, _ in blocks]
    assert keys == ["GRAB", "SNAP", "WEN"], keys
    assert "CMG" not in keys, "must never propose over a human-audited row"

    body = dict((k, b) for k, _, b in blocks)
    # THE MSFT RULE: a found bridge is still untested admission.
    assert 'admission     = "untested"' in body["WEN"], body["WEN"]
    assert "held" not in body["WEN"]
    # THE APPLE RULE: neither field is guessed from a filing.
    assert 'listen_access = "unknown"' in body["WEN"]
    assert 'qa_mechanism  = "unknown"' in body["WEN"]
    # "could not look" must not read as "nothing here".
    assert "could not look here" in body["GRAB"], body["GRAB"]
    assert "unresearched" in body["GRAB"]
    assert "real negative" in body["SNAP"], body["SNAP"]
    assert "could not look" not in body["SNAP"]
    # Every block must parse as TOML, or "paste-ready" is a lie.
    parsed = tomllib.loads("\n".join(b for _, _, b in blocks))
    assert set(parsed) == {"GRAB", "SNAP", "WEN"}, parsed
    assert parsed["WEN"]["admission"] == "untested"
    assert parsed["GRAB"]["tier"] == "unknown"

    # Horizon filter.
    slate = {"events": [
        {"issuer": "WEN", "call_date": "2026-08-07"},
        {"issuer": "GRAB", "call_date": "2026-09-30"},
    ]}
    inside = slate_issuers(slate, days=12, today=datetime.date(2026, 8, 1))
    assert inside == {"WEN"}, inside
    assert [k for k, _, _ in propose(hunted, registry, inside)] == ["WEN"]

    print("registry-propose --check: OK (MSFT rule, APPLE rule, "
          "unknown-vs-unpublished wording, TOML validity, horizon filter)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hunt", default="slate/hunt-status.json")
    ap.add_argument("--registry", default="configs/dial-ins.toml")
    ap.add_argument("--slate", help="restrict to issuers with a call in --days")
    ap.add_argument("--days", type=int, default=12)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        check()
        return 0

    try:
        hunted = json.load(open(a.hunt)).get("issuers", {})
    except (OSError, json.JSONDecodeError) as e:
        print(f"PROPOSE-ERROR: cannot read {a.hunt}: {e}", file=sys.stderr)
        return 2
    try:
        registry = tomllib.load(open(a.registry, "rb"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"PROPOSE-ERROR: cannot read {a.registry}: {e}", file=sys.stderr)
        return 2

    issuers = None
    if a.slate:
        try:
            issuers = slate_issuers(json.load(open(a.slate)), a.days,
                                    datetime.date.today())
        except (OSError, json.JSONDecodeError) as e:
            print(f"PROPOSE-ERROR: cannot read {a.slate}: {e}", file=sys.stderr)
            return 2

    blocks = propose(hunted, registry, issuers)
    if not blocks:
        print("PROPOSE-NONE: every hunted issuer in scope already has a "
              "registry entry.")
        return 0

    by_tier = {}
    for _, tier, _ in blocks:
        by_tier[tier] = by_tier.get(tier, 0) + 1
    print(f"# {len(blocks)} proposed entr(ies) for {Path(a.registry)} — "
          + ", ".join(f"{n} {t}" for t, n in sorted(by_tier.items())))
    print("# REVIEW EACH ONE, then paste. This script never writes the "
          "registry.")
    print("# A number is not admission: scripts/bridge-probe.sh decides that.")
    for _, _, block in blocks:
        print()
        print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
