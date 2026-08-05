#!/usr/bin/env python3
"""Did we CAPTURE a call whose board was tradeable, and place nothing?

WHY THIS EXISTS (2026-08-04). Spotify and Progressive were both captured
cleanly — 598 s of intelligible Spotify audio, six held Progressive legs — and
both produced zero detections, zero decisions, zero orders, because the capture
ran on `earshot bridge-probe` (a WAV recorder with no detector) instead of
`earshot arm`. Their boards traded 157,217 and ~170,000 contracts respectively
while we listened. Nothing in the system noticed, and the question "why did
this morning fail?" had to be asked by a human hours later.

This is the detector for that entire failure class, and it is deliberately
INDEPENDENT of which upstream node broke. It does not care whether discover
lost the event, qualify lost the bridge, or someone pointed the wrong tool at
the call. It asks one question of the evidence on disk:

    we have capture audio for issuer X, X's board traded, we placed no orders.

Retrospective by design: it keys on `any_board` (traded, open or not), because
by the time it runs the board has often settled — SPOT was open at 12:02Z and
closed by 15:00Z. Keying on "open now" would clear the very miss it exists for.

Read-only. Public Kalshi endpoint, no credentials, writes nothing.

Usage:
  scripts/missed-arm-check.py --date 2026-08-04
  scripts/missed-arm-check.py --date 2026-08-04 --json
  scripts/missed-arm-check.py --check        # offline self-test

Exit: 0 clean, 1 at least one MISSED-ARM, 2 could not determine.
"""
import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from importlib.machinery import SourceFileLoader  # noqa: E402

_bo = SourceFileLoader(
    "board_open", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "board-open.py")).load_module()

# A leg shorter than this is an entry probe or a dead re-dial, not coverage of
# a call. PGR's legs 07..23 held ~12.3 s each after the conference ended; those
# must not each count as "we captured Progressive".
MIN_CAPTURE_S = 60


def _ticker_suffix(date):
    """'2026-08-04' -> '26AUG04', the Kalshi event-ticker date suffix.

    Used ONLY to date-filter local run dirs. Never to derive a call date —
    that is the BA/HOOD/PGR rule in scripts/discover-events.py, and it stays
    there.
    """
    import datetime
    try:
        d = datetime.date.fromisoformat(date)
    except ValueError:
        return ""
    return d.strftime("%y%b%d").upper()


def captures_for(date, probe_dir="corpus/bridge-probes", state_dir="state"):
    """issuer -> {'held_s': float, 'sources': [...]} for that date."""
    out = {}
    us = date.replace("-", "_")
    for p in sorted(glob.glob(os.path.join(probe_dir, f"*{us}*.json"))):
        base = os.path.basename(p)
        m = re.match(r"([A-Za-z0-9]+)_", base)
        if not m:
            continue
        try:
            held = json.load(open(p)).get("held_ms", 0) / 1000.0
        except Exception:
            continue
        if held < MIN_CAPTURE_S:
            continue
        e = out.setdefault(m.group(1).upper(), {"held_s": 0.0, "sources": []})
        e["held_s"] += held
        e["sources"].append(base)
    # An armed run banks its own evidence; count it as capture too, so a run
    # that heard the call but placed nothing still surfaces.
    #
    # ⚠ MUST be date-filtered on the EVENT, not just globbed. state/ is an
    # append-only pile of every run ever made, so an unfiltered scan reported
    # GEV's 2026-07-22 event as a miss on 2026-08-04 — a stale-evidence false
    # positive, and a gate that cries wolf gets ignored (the same lesson
    # discover-events.py learned at 52-of-57).
    suffix = _ticker_suffix(date)
    for p in sorted(glob.glob(os.path.join(state_dir, "*", "report.json"))):
        try:
            r = json.load(open(p))
        except Exception:
            continue
        ev = r.get("event_id") or ""
        m = re.match(r"KXEARNINGSMENTION([A-Z0-9]+?)-", ev)
        if not m or not r.get("frames_in"):
            continue
        if suffix and suffix not in ev.upper():
            continue
        e = out.setdefault(m.group(1), {"held_s": 0.0, "sources": []})
        e["frames_in"] = e.get("frames_in", 0) + int(r.get("frames_in") or 0)
        e["sources"].append(os.path.relpath(p))

    # JOURNAL EVIDENCE (2026-08-05). report.json is written only on a CLEAN
    # exit. A webcast that ends normally kills its legs ("pipe source idle past
    # 30s"), the arm exits via the error path, and no report is written — so a
    # COMPLETE 62-minute capture with live detections looked exactly like a
    # miss. YOU-26AUG05 captured its whole call and this checker still said
    # "no capture evidence for 2026-08-05". A grading tool that cannot see a
    # success is as dangerous as one that cannot see a failure.
    # state/<E>/journal.jsonl records arm-start regardless of how the run ends.
    for p in sorted(glob.glob(os.path.join(state_dir, "*", "journal.jsonl"))):
        ev = os.path.basename(os.path.dirname(p))
        m = re.match(r"KXEARNINGSMENTION([A-Z0-9]+?)-", ev)
        if not m:
            continue
        if suffix and suffix not in ev.upper():
            continue
        armed = False
        try:
            for ln in open(p):
                try:
                    j = json.loads(ln)
                except Exception:
                    continue
                if j.get("action") == "arm-start":
                    armed = True
        except OSError:
            continue
        if not armed:
            continue
        e = out.setdefault(m.group(1), {"held_s": 0.0, "sources": []})
        # An armed run that produced no report still HEARD; mark it so the
        # caller can distinguish "captured, report missing" from "never ran".
        e.setdefault("frames_in", 0)
        e["armed_no_report"] = "report.json" not in " ".join(e["sources"])
        e["sources"].append(os.path.relpath(p))
    return out


def orders_by_issuer(state_dir="state"):
    """issuer -> total orders placed, from every armed run's report.json."""
    out = {}
    for p in sorted(glob.glob(os.path.join(state_dir, "*", "report.json"))):
        try:
            r = json.load(open(p))
        except Exception:
            continue
        m = re.match(r"KXEARNINGSMENTION([A-Z0-9]+?)-", r.get("event_id") or "")
        if not m:
            continue
        out[m.group(1)] = out.get(m.group(1), 0) + len(r.get("orders") or [])
    return out


def reconcile(captures, orders, board=None):
    """→ list of findings, worst first. `board` is injectable for tests."""
    board = board or _bo.any_board
    findings = []
    for issuer, cap in sorted(captures.items()):
        placed = orders.get(issuer, 0)
        if placed:
            continue
        try:
            b = board(issuer)
        except Exception as exc:
            findings.append({"issuer": issuer, "verdict": "UNDETERMINED",
                             "reason": str(exc), **cap})
            continue
        if b.get("traded"):
            # An ARMED run that placed nothing is NOT a missed arm when the
            # posture is 1c non-crossing observe — declining an ask above the
            # cap is the design, not a loss ("observe non-crossing … not a lost
            # race"). Distinguish the two, or the tool cries wolf on exactly
            # the nights that went well and gets ignored on the nights that
            # did not. MISSED-ARM stays for audio captured with NO arm-start.
            armed_run = any("journal.jsonl" in src for src in cap.get("sources", []))
            intents = 0
            if armed_run:
                for src in cap.get("sources", []):
                    log = os.path.join(os.path.dirname(src), "launch.log")
                    try:
                        with open(log, errors="ignore") as fh:
                            intents += sum(1 for ln in fh if "decision intent" in ln)
                    except OSError:
                        pass
            findings.append({
                "issuer": issuer,
                "verdict": "ARMED-OBSERVING" if armed_run else "MISSED-ARM",
                "markets": b.get("markets", 0), "volume": b.get("volume", 0.0),
                "orders": 0, "intents": intents, **cap})
    findings.sort(key=lambda f: (f["verdict"] != "MISSED-ARM",
                                 -f.get("volume", 0)))
    return findings


def check():
    caps = {"SPOT": {"held_s": 598.4, "sources": ["spot_2026_08_04.json"]},
            "PGR": {"held_s": 3592.0, "sources": ["pgr_..._leg01.json"]},
            "GRAB": {"held_s": 3354.0, "sources": ["state/…/report.json"]},
            "QUIET": {"held_s": 120.0, "sources": ["quiet_2026_08_04.json"]}}
    orders = {"GRAB": 8}

    def board(issuer):
        return {"SPOT": {"traded": True, "markets": 18, "volume": 157217.0},
                "PGR": {"traded": True, "markets": 12, "volume": 170000.0},
                "GRAB": {"traded": True, "markets": 13, "volume": 5000.0},
                "QUIET": {"traded": False, "markets": 0, "volume": 0.0},
                }[issuer]

    f = reconcile(caps, orders, board=board)
    got = [(x["issuer"], x["verdict"]) for x in f]
    # ranked by contracts missed, worst first: PGR ~170k outranks SPOT 157,217
    assert got == [("PGR", "MISSED-ARM"), ("SPOT", "MISSED-ARM")], got
    # GRAB captured AND placed 8 orders — the system worked, so it must be silent
    assert not any(x["issuer"] == "GRAB" for x in f), "armed run must not flag"
    # a traded-nothing board is not a miss
    assert not any(x["issuer"] == "QUIET" for x in f)
    assert f[0]["volume"] == 170000.0 and f[1]["volume"] == 157217.0, f

    # a board lookup failure is UNDETERMINED, never silently clean
    def boom(issuer):
        raise RuntimeError("no route to host")
    f2 = reconcile({"X": {"held_s": 600, "sources": []}}, {}, board=boom)
    assert f2 and f2[0]["verdict"] == "UNDETERMINED", f2

    # short legs must not count as coverage (PGR legs 07..23 were ~12.3 s)
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        json.dump({"held_ms": 12300}, open(os.path.join(d, "pgr_2026_08_04_leg07.json"), "w"))
        json.dump({"held_ms": 598400}, open(os.path.join(d, "spot_2026_08_04.json"), "w"))
        c = captures_for("2026-08-04", probe_dir=d, state_dir=os.path.join(d, "none"))
        assert set(c) == {"SPOT"}, f"short leg counted as capture: {c}"

    # state/ run dirs MUST be date-filtered. Unfiltered, GEV's 2026-07-22 event
    # reported as a miss on 2026-08-04 — stale evidence crying wolf.
    assert _ticker_suffix("2026-08-04") == "26AUG04", _ticker_suffix("2026-08-04")
    assert _ticker_suffix("garbage") == ""
    with tempfile.TemporaryDirectory() as d:
        for ev in ("KXEARNINGSMENTIONGEV-26JUL22", "KXEARNINGSMENTIONAMD-26AUG04"):
            os.makedirs(os.path.join(d, ev))
            json.dump({"event_id": ev, "frames_in": 999, "orders": []},
                      open(os.path.join(d, ev, "report.json"), "w"))
        c = captures_for("2026-08-04", probe_dir=os.path.join(d, "none"), state_dir=d)
        assert set(c) == {"AMD"}, f"stale run dir leaked in: {c}"
    print("missed-arm-check --check: OK (SPOT/PGR flagged, armed run silent, "
          "untraded board ignored, volume ranking, fail-loud, short-leg floor)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD of the captures to reconcile")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        check()
        return 0
    if not a.date:
        ap.error("--date required (or --check)")

    caps = captures_for(a.date)
    if not caps:
        print(f"missed-arm-check: no capture evidence for {a.date}")
        return 0
    findings = reconcile(caps, orders_by_issuer())
    if a.json:
        print(json.dumps(findings, indent=1))
        return 1 if any(f["verdict"] == "MISSED-ARM" for f in findings) else 0

    missed = [f for f in findings if f["verdict"] == "MISSED-ARM"]
    undet = [f for f in findings if f["verdict"] == "UNDETERMINED"]
    observing = [f for f in findings if f["verdict"] == "ARMED-OBSERVING"]
    print(f"missed-arm-check {a.date}: {len(caps)} issuer(s) captured, "
          f"{len(missed)} MISSED-ARM, {len(observing)} ARMED-OBSERVING, "
          f"{len(undet)} undetermined")
    # Print the good news too: an operator who only ever sees this tool shout
    # cannot tell a silent success from a silent failure.
    for f in missed + observing + undet:
        heard = (f"{f['held_s']:.0f}s of audio" if f.get("held_s")
                 else f"{f.get('frames_in', 0)} frames")
        if f["verdict"] == "ARMED-OBSERVING":
            print(f"\n✓ ARMED-OBSERVING: {f['issuer']} — the arm ran and produced "
                  f"{f.get('intents', 0)} decision intent(s); 0 orders is the "
                  f"1c non-crossing posture declining asks above cap, not a miss. "
                  f"Board traded {f['volume']:.0f} contracts / {f['markets']} markets.")
            print(f"   evidence: {', '.join(f['sources'][:4])}")
            continue
        print(f"\n⛔ MISSED-ARM: {f['issuer']} — captured {heard}, placed 0 "
              f"orders, and its board traded {f['volume']:.0f} contracts "
              f"across {f['markets']} markets.")
        print(f"   evidence: {', '.join(f['sources'][:4])}"
              + (f" (+{len(f['sources']) - 4} more)" if len(f["sources"]) > 4 else ""))
    for f in undet:
        print(f"\n?  UNDETERMINED: {f['issuer']} — {f['reason']}")
    if missed:
        print("\n⇒ Audio was captured and nothing was traded on it. Either the")
        print("  arm path was never launched, or it launched and placed nothing.")
        print("  Check: is there a state/KXEARNINGSMENTION<ISSUER>-* run dir at all?")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
