#!/usr/bin/env python3
"""data.py — load banked tapes and settlement labels. Read-only against state/.

Three things bite here and each has cost someone a wrong number before:

1. **Two tape schemas.** Newer banked tapes are
   `{event, fetched_utc, markets{...}, trades{ticker: [...]}}`; the oldest
   (MS 26JUL15) is a bare `{ticker: [...]}`. Assuming one shape raises
   KeyError on the other, which is the loud failure — the quiet one is
   assuming `markets` is always present when reading labels.

2. **State dirs are not Kalshi events.** `state/` carries our own run dirs
   suffixed `-WEBCAST`, `-PHONE`, `-OBSERVE`, `-ACOUSTIC`. Both
   `KXEARNINGSMENTIONAMD-26AUG04` and `KXEARNINGSMENTIONAMD-26AUG04-WEBCAST`
   exist and are ONE Kalshi event. Counting both double-counts every trade in
   it. The Kalshi event ticker is the unsuffixed one.

3. **`count_fp`, not `count`.** VERIFICATIONS Q-XTAPE: the plain `count` key
   is absent from the trades tape and reading it yields a silent zero
   notional. Handled in sweep_segment.normalize().

Settlement labels come from three places, in order of authority: a
`settlements*.json` in the event's state dir, then inline `markets[].result`
in the tape, then a live refetch (fetch_tapes.py). Labels are never guessed.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
STATE = os.path.join(REPO, "state")
TAPES = os.path.join(os.path.dirname(HERE), "tapes")

RUN_SUFFIXES = ("-WEBCAST", "-PHONE", "-OBSERVE", "-ACOUSTIC")


def kalshi_event(state_dir_name):
    """Our run-dir name -> the Kalshi event ticker it belongs to."""
    name = state_dir_name
    for suf in RUN_SUFFIXES:
        if name.endswith(suf):
            name = name[: -len(suf)]
    return name


def event_dirs():
    """Every mention-event state dir, deduped to one entry per Kalshi event.

    Prefers the unsuffixed dir when both exist, else the richest suffixed one.
    """
    by_event = {}
    for path in sorted(glob.glob(os.path.join(STATE, "KX*MENTION*"))):
        if not os.path.isdir(path):
            continue
        name = os.path.basename(path)
        ev = kalshi_event(name)
        prev = by_event.get(ev)
        if prev is None or (name == ev and prev != ev):
            by_event[ev] = name
    return by_event


def load_tape(event, run_dir=None):
    """{ticker: [trade,...]} for one Kalshi event, or None. Handles both schemas."""
    for base in (os.path.join(TAPES, event), os.path.join(STATE, run_dir or event)):
        paths = sorted(glob.glob(os.path.join(base, "tape-*.json")))
        if not paths:
            continue
        try:
            d = json.load(open(paths[-1]))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict) and isinstance(d.get("trades"), dict):
            return d["trades"]
        if isinstance(d, dict) and d and all(isinstance(v, list) for v in d.values()):
            return d
    return None


def load_labels(event, run_dir=None):
    """{ticker: 'yes'|'no'} settlement labels, most authoritative source first."""
    out = {}
    # 3rd: inline results carried in the tape's markets block.
    for base in (os.path.join(STATE, run_dir or event), os.path.join(TAPES, event)):
        for p in sorted(glob.glob(os.path.join(base, "tape-*.json"))):
            try:
                d = json.load(open(p))
            except (OSError, json.JSONDecodeError):
                continue
            for tk, m in (d.get("markets") or {}).items():
                if isinstance(m, dict) and m.get("result") in ("yes", "no"):
                    out[tk] = m["result"]
    # 2nd: a refetched labels file inside this exploration dir.
    for p in sorted(glob.glob(os.path.join(TAPES, event, "settlements*.json"))):
        for r in _rows(p):
            if r.get("result") in ("yes", "no"):
                out[r["ticker"]] = r["result"]
    # 1st (wins): a settlements file banked in state/ by a production run.
    for p in sorted(glob.glob(os.path.join(STATE, run_dir or event, "settlements*.json"))):
        for r in _rows(p):
            if r.get("result") in ("yes", "no"):
                out[r["ticker"]] = r["result"]
    return out


def _rows(path):
    try:
        d = json.load(open(path))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(d, list):
        return [r for r in d if isinstance(r, dict)]
    if isinstance(d, dict):
        if isinstance(d.get("markets"), list):
            return [r for r in d["markets"] if isinstance(r, dict)]
        return [{"ticker": k, **v} for k, v in d.items() if isinstance(v, dict)]
    return []


def event_day(event):
    """The YYMONDD token in an event ticker -> a sortable key, for day-level stats."""
    m = re.search(r"-(\d{2}[A-Z]{3}\d{2})$", event)
    return m.group(1) if m else event


def inventory():
    """What is available offline, per event: tape? labels? how many?"""
    rows = []
    for ev, run_dir in sorted(event_dirs().items()):
        tape = load_tape(ev, run_dir)
        labels = load_labels(ev, run_dir)
        rows.append({
            "event": ev,
            "run_dir": run_dir,
            "markets": len(tape) if tape else 0,
            "trades": sum(len(v) for v in tape.values()) if tape else 0,
            "labelled": len(labels),
            "usable": bool(tape) and bool(labels),
        })
    return rows


def self_test():
    p = f = 0

    def chk(name, want, got):
        nonlocal p, f
        if want == got:
            print(f"  PASS  {name}")
            p += 1
        else:
            print(f"  FAIL  {name} (expected {want!r}, got {got!r})")
            f += 1

    print("data self-test:")

    chk("run-dir suffix stripped to the Kalshi event",
        "KXEARNINGSMENTIONAMD-26AUG04", kalshi_event("KXEARNINGSMENTIONAMD-26AUG04-WEBCAST"))
    chk("unsuffixed name is unchanged",
        "KXEARNINGSMENTIONAAL-26JUL23", kalshi_event("KXEARNINGSMENTIONAAL-26JUL23"))
    chk("PHONE/ACOUSTIC suffixes also stripped",
        {"KXEARNINGSMENTIONGRAB-26AUG03"},
        {kalshi_event(n) for n in ("KXEARNINGSMENTIONGRAB-26AUG03-PHONE",
                                   "KXEARNINGSMENTIONGRAB-26AUG03-ACOUSTIC")})

    evs = event_dirs()
    chk("AMD appears exactly once despite two state dirs", 1,
        sum(1 for e in evs if e.startswith("KXEARNINGSMENTIONAMD-26AUG04")))
    chk("dedupe prefers the unsuffixed dir",
        "KXEARNINGSMENTIONAMD-26AUG04", evs.get("KXEARNINGSMENTIONAMD-26AUG04"))

    # Both tape schemas load.
    ms = load_tape("KXEARNINGSMENTIONMS-26JUL15")
    chk("bare {ticker: [...]} schema loads (MS 26JUL15)", True,
        isinstance(ms, dict) and len(ms) == 5)
    aal = load_tape("KXEARNINGSMENTIONAAL-26JUL23")
    chk("wrapped {trades:{...}} schema loads (AAL 26JUL23)", True,
        isinstance(aal, dict) and len(aal) == 15)
    chk("a missing event returns None", None, load_tape("KXEARNINGSMENTIONNOSUCH-26JAN01"))

    # Labels: state/ settlements outrank inline tape results.
    lab = load_labels("KXEARNINGSMENTIONAAL-26JUL23")
    chk("AAL labels load from state/ settlements", 15, len(lab))
    chk("AAL CHIC settled yes", "yes", lab.get("KXEARNINGSMENTIONAAL-26JUL23-CHIC"))
    chk("AAL WORL settled no", "no", lab.get("KXEARNINGSMENTIONAAL-26JUL23-WORL"))
    chk("labels are only yes/no, never blank", set(),
        {v for v in lab.values()} - {"yes", "no"})

    chk("day key extracted", "26JUL23", event_day("KXEARNINGSMENTIONAAL-26JUL23"))

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


if __name__ == "__main__":
    if "--inventory" in sys.argv:
        rows = inventory()
        print(f"{'event':<40}{'mkts':>6}{'trades':>8}{'labelled':>10}  usable")
        for r in rows:
            print(f"{r['event']:<40}{r['markets']:>6}{r['trades']:>8}{r['labelled']:>10}"
                  f"  {'YES' if r['usable'] else '-'}")
        u = [r for r in rows if r["usable"]]
        print(f"\n{len(u)} of {len(rows)} events usable offline "
              f"({sum(r['labelled'] for r in u)} labelled markets)")
        sys.exit(0)
    sys.exit(self_test())
