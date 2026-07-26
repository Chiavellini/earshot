#!/usr/bin/env python3
"""D2b — score the prefix gate PER CHANNEL, never as one blended number.

The dev split is mixed-channel: ~5.15 h of wideband (the 16 kHz webcast channel
the legs actually deliver) plus ~2.81 h of telephony (8 kHz, band-limited, G.711
round-tripped, for phone-bridge events). `docs/BAKEOFF.md` is explicit that an
engine is benched on the channel it trades — and we are arming a WEBCAST event,
so the decision-relevant false-alarm rate is the WIDEBAND subset. A single
blended FA/h mixes two channels and describes neither.

`fa_autopsy` lines carry the call id (`"<phrase> <tier> <call>@<pos>ms"`), which
is what makes the split possible at all.

Usage: prefix-gate-score.py <bench-report.json> [--corpus corpus]
"""
import argparse
import glob
import json
import os
import re
import sys
import tomllib

BAR_FA_PER_HOUR = 0.5          # PRE-REGISTERED. Not negotiable after the fact.

# Prefixed phrases plausible on a financial/telecom call: a Fast fire on any of
# these is NOT automatically a false alarm and must be checked against the audio.
NEEDS_VERIFY = {"venture fund", "world cup", "basic economy"}

# Per-call overrides: on a WORLD NEWS control, `golden dome` is a US
# missile-defence programme DW covers, so it joins the must-verify set there
# even though it is safe on an earnings call. Keyed by substring of the call id.
NEEDS_VERIFY_BY_CALL = {
    "DWNEWS": NEEDS_VERIFY | {"golden dome"},
}


def needs_verify(call_id):
    for key, phrases in NEEDS_VERIFY_BY_CALL.items():
        if key in call_id:
            return phrases
    return NEEDS_VERIFY


def domain_map(corpus):
    """call id -> (domain, audio path), read from the committed manifests."""
    out = {}
    for f in sorted(glob.glob(os.path.join(corpus, "manifests", "*.toml"))):
        with open(f, "rb") as fh:
            d = tomllib.load(fh)
        c = d["call"]
        dom = "wideband" if c["audio"].startswith("wideband") else "telephony"
        out[c["id"]] = (dom, os.path.join(corpus, c["audio"]))
    return out


def hours(path):
    """Audio hours from the WAV itself — never assumed."""
    try:
        import wave
        with wave.open(path) as w:
            return w.getnframes() / w.getframerate() / 3600.0
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--corpus", default="corpus")
    ap.add_argument(
        "--hours",
        default="",
        help="override measured hours when the WAVs are not local, "
             'e.g. --hours "wideband=5.15,telephony=2.81". Overrides are '
             "labelled in the output so an assumed number is never mistaken "
             "for a measured one.",
    )
    a = ap.parse_args()

    d = json.load(open(a.report))
    dm = domain_map(a.corpus)
    scored = d["calls"]

    # Hours per domain, measured from the WAVs that were actually scored.
    dom_hours, missing = {}, []
    for cid in scored:
        dom, path = dm.get(cid, (None, None))
        if dom is None:
            missing.append(cid)
            continue
        h = hours(path)
        if h is None:
            missing.append(cid)
            continue
        dom_hours[dom] = dom_hours.get(dom, 0.0) + h

    print(f"report      : {a.report}")
    print(f"calls scored: {len(scored)}  {scored}")
    overrides = {}
    for part in filter(None, (x.strip() for x in a.hours.split(","))):
        k, _, v = part.partition("=")
        try:
            overrides[k.strip()] = float(v)
        except ValueError:
            print(f"bad --hours term {part!r}", file=sys.stderr)
            return 2
    if overrides:
        # Explicit beats measured, but say which is which.
        for k, v in overrides.items():
            dom_hours[k] = v
        missing = [c for c in missing if dm.get(c, ("", ""))[0] not in overrides]

    print(f"corpus_hours: {d['corpus_hours']:.2f} h (from the report)")
    for dom, h in sorted(dom_hours.items()):
        src = "ASSUMED (--hours)" if dom in overrides else "measured from WAV"
        print(f"  {dom:<10}: {h:.2f} h   [{src}]")
    if missing:
        print(f"  ⚠ could not resolve hours for: {missing} — those calls are")
        print(f"    EXCLUDED from the per-domain rates below, not silently folded in.")
    print()

    for cell in d["cells"]:
        print(f"=== engine={cell['engine']} chunk={cell['chunk_ms']} ===")
        for tier in ("Fast", "Confirmed"):
            rows = [r for r in cell["rows"] if str(r["tier"]) == tier]
            tp = sum(r["true_positives"] for r in rows)
            miss = sum(r["misses"] for r in rows)
            fa = sum(r["false_alarms"] for r in rows)
            rec = tp / (tp + miss) if tp + miss else float("nan")
            print(f"  {tier:<10} tp={tp:<4} miss={miss:<4} fa={fa:<4} recall={rec:.3f}")

        # Split false alarms by call, hence by channel.
        per_dom, verify = {}, []
        for line in cell["fa_autopsy"]:
            m = re.match(r"^(.*?)\s+(Fast|Confirmed)\s+(\S+?)@(\d+)ms$", line)
            if not m:
                continue
            phrase, tier, cid, pos = m.group(1), m.group(2), m.group(3), int(m.group(4))
            dom = dm.get(cid, ("unknown", None))[0]
            per_dom.setdefault((tier, dom), []).append((phrase, cid, pos))
            if tier == "Fast" and phrase in needs_verify(cid):
                verify.append((phrase, cid, pos))

        print()
        print(f"  {'tier':<10}{'domain':<11}{'FA':>4}{'hours':>8}{'FA/h':>8}   verdict vs {BAR_FA_PER_HOUR}/h")
        for (tier, dom), items in sorted(per_dom.items()):
            h = dom_hours.get(dom)
            if not h:
                print(f"  {tier:<10}{dom:<11}{len(items):>4}{'?':>8}{'?':>8}   hours unknown")
                continue
            rate = len(items) / h
            verdict = ""
            if tier == "Fast":
                verdict = "**PASS**" if rate <= BAR_FA_PER_HOUR else "**FAIL**"
            print(f"  {tier:<10}{dom:<11}{len(items):>4}{h:>8.2f}{rate:>8.2f}   {verdict}")

        # A tier with zero false alarms produces no autopsy lines at all, so the
        # loop above prints nothing for it — say so rather than let silence read
        # as "not measured".
        for tier in ("Fast", "Confirmed"):
            for dom, h in sorted(dom_hours.items()):
                if (tier, dom) not in per_dom:
                    v = "**PASS**" if tier == "Fast" else ""
                    print(f"  {tier:<10}{dom:<11}{0:>4}{h:>8.2f}{0.0:>8.2f}   {v}")

        if verify:
            print()
            print("  ⚠ MUST VERIFY AGAINST AUDIO BEFORE COUNTING — these phrases are")
            print("    plausible on a financial/telecom call, so a fire may be a TRUE")
            print("    positive the zero-occurrence manifest cannot know about:")
            for phrase, cid, pos in verify:
                print(f"      {phrase:<20} {cid}@{pos}ms")

        wb = dom_hours.get("wideband")
        if wb:
            print()
            print(f"  DECISION-RELEVANT LINE (we arm WEBCAST): the wideband Fast row above.")
            print(f"  At {wb:.2f} h the {BAR_FA_PER_HOUR}/h bar permits {BAR_FA_PER_HOUR*wb:.1f} false alarms in total,")
            print(f"  so a PASS here bounds the rate loosely — it is not a tight estimate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
