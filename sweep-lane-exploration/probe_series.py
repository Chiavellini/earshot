#!/usr/bin/env python3
"""probe_series.py — is the intra-sweep arm an earnings artifact?

Declared in PRE-REGISTRATION-R2-PROBE.md, which was written BEFORE this script
fetched anything. That ordering is the probe's whole evidentiary basis and
`--verify-ordering` checks it.

    python3 probe_series.py --self-test        # the population filter, offline
    python3 probe_series.py --list             # population, BEFORE any fetch
    python3 probe_series.py --fetch            # public, no-auth, paced, resumable
    python3 probe_series.py --score            # ONE SHOT
    python3 probe_series.py --verify-ordering  # pre-registration older than tapes?

Fetching and scoring are separate subcommands on purpose: it must not be
possible to see a number as a side effect of banking data.

Read-only against production. Writes only into this lane's tapes/ and out/.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))
sys.path.insert(0, HERE)

import fetch_tapes as FT     # noqa: E402  (reused verbatim: _get, _paged, bank)

CENSUS = os.path.join(REPO, "slate", "census.json")
PREREG = os.path.join(HERE, "PRE-REGISTRATION-R2-PROBE.md")
TAPES = os.path.join(HERE, "tapes")
OUT = os.path.join(HERE, "out")
ROSTER = os.path.join(OUT, "probe-roster.json")

# --- Population (PRE-REGISTRATION-R2-PROBE.md §P1) ---------------------------
EARNINGS_PREFIX = "KXEARNINGSMENTION"
EXCLUDE_SERIES = {"KXWORLDNEWSMENTION"}      # its 26JUL29 event is in TRAIN


def in_population(series_ticker):
    """Is this series in the probe population? TICKER PREFIX ONLY.

    ⚠ NEVER filter on the census `class` field. 102 of the 153
    KXEARNINGSMENTION* series carry a class other than 'earnings-call'
    (KXEARNINGSMENTIONCRCL, ...RY, ...MCO are all 'unclassified'), so a
    class-based filter silently readmits earnings events and turns this
    generalization test into a partial replication. The self-test pins that
    exact case.
    """
    t = (series_ticker or "").upper()
    if not t or t in EXCLUDE_SERIES:
        return False
    return not t.startswith(EARNINGS_PREFIX)


def population(census_path=CENSUS):
    with open(census_path) as f:
        rows = json.load(f)["series"]
    return sorted(r["ticker"] for r in rows if in_population(r.get("ticker")))


# --- Enumeration --------------------------------------------------------------

def series_events(series):
    """[(event_ticker, n_settled, volume)] for one series. Public, no-auth."""
    url = (f"{FT.API}/events?series_ticker={series}&limit=200"
           "&with_nested_markets=true")
    out = []
    for e in FT._paged(url, "events"):
        ms = e.get("markets") or []
        settled = sum(1 for m in ms if m.get("result") in ("yes", "no"))
        vol = 0
        for m in ms:
            for k in ("volume", "volume_fp"):
                v = m.get(k)
                if v is not None:
                    try:
                        vol += int(float(v))
                    except (TypeError, ValueError):
                        pass
                    break
        out.append((e.get("event_ticker"), settled, vol, len(ms)))
    return [r for r in out if r[0]]


def build_roster():
    """Enumerate the whole population. Counting is not scoring."""
    pop = population()
    print(f"probe population: {len(pop)} series "
          f"(all mention series except {EARNINGS_PREFIX}* and {sorted(EXCLUDE_SERIES)})")
    roster, errs = [], 0
    for i, s in enumerate(pop, 1):
        try:
            evs = series_events(s)
        except Exception as e:                       # noqa: BLE001 — report, continue
            print(f"  [{i:>3}/{len(pop)}] {s:<32} ERROR {e}")
            errs += 1
            continue
        for ev, settled, vol, nm in evs:
            roster.append({"series": s, "event": ev, "settled_markets": settled,
                           "markets": nm, "volume": vol,
                           "eligible": bool(settled and vol)})
        if i % 25 == 0 or i == len(pop):
            print(f"  [{i:>3}/{len(pop)}] {len(roster)} events enumerated")
        time.sleep(FT.PACE_S)
    return roster, errs


def _write(path, obj):
    full = os.path.abspath(path)
    if not full.startswith(os.path.abspath(HERE) + os.sep):
        raise RuntimeError(f"refusing to write outside the lane: {full}")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        json.dump(obj, f, indent=1)


def prereg_witness():
    """Hash + mtime of the pre-registration, stamped INTO the roster.

    An mtime is not proof: appending a dated note to the pre-registration — which
    §Notes explicitly allows — rewrites it and destroys the evidence. That is not
    hypothetical; it happened on the first run of this probe at 17:05:10Z and
    took the ordering artifact with it. A content hash recorded in an immutable
    roster field survives amendment: it says what the document said WHEN the
    population was enumerated, which is the fact that matters.
    """
    import hashlib
    raw = open(PREREG, "rb").read()
    return {"prereg_sha256": hashlib.sha256(raw).hexdigest(),
            "prereg_bytes": len(raw),
            "prereg_mtime_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime(os.path.getmtime(PREREG)))}


def cmd_list():
    w = prereg_witness()          # BEFORE enumeration, before any fetch
    roster, errs = build_roster()
    _write(ROSTER, {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "population_series": len(population()), "errors": errs,
                    **w, "events": roster})
    elig = [r for r in roster if r["eligible"]]
    print(f"\n  events enumerated       : {len(roster)}")
    print(f"  with a settled market   : {sum(1 for r in roster if r['settled_markets'])}")
    print(f"  ...and non-zero volume  : {len(elig)}   <- the scored set")
    print(f"  series contributing     : {len({r['series'] for r in elig})}")
    print(f"  enumeration errors      : {errs}")
    print(f"\n  {'series':<30}{'events':>8}{'eligible':>10}")
    by = {}
    for r in roster:
        a, b = by.get(r["series"], (0, 0))
        by[r["series"]] = (a + 1, b + int(r["eligible"]))
    for s, (a, b) in sorted(by.items(), key=lambda kv: -kv[1][1])[:25]:
        if b:
            print(f"  {s:<30}{a:>8}{b:>10}")
    print(f"\n  roster -> {ROSTER}")
    return 0


def cmd_fetch(limit=None):
    if not os.path.exists(ROSTER):
        print("probe: no roster. Run --list first (it is the declared population).")
        return 2
    doc = json.load(open(ROSTER))
    # The first version of this guard refused whenever the pre-registration was
    # NEWER than the roster. That is wrong: §Notes allows dated amendments, so a
    # legitimate note silently blocked the fetch and a "resume" banked nothing.
    # Report a content change; never block on it. The roster's recorded hash is
    # what the population was declared against, and that is the fact to surface.
    recorded = doc.get("prereg_sha256")
    if recorded:
        import hashlib
        now = hashlib.sha256(open(PREREG, "rb").read()).hexdigest()
        if now != recorded:
            print(f"probe: NOTE — the pre-registration changed since the roster was\n"
                  f"       built ({recorded[:12]} -> {now[:12]}). Fetching anyway; the\n"
                  f"       roster records what was declared at enumeration time.")
    else:
        print("probe: NOTE — this roster predates the hash witness, so the\n"
              "       pre-registration -> roster ordering is not verifiable from\n"
              "       artifacts. See PRE-REGISTRATION-R2-PROBE.md defect D1.")
    roster = doc["events"]
    todo = [r["event"] for r in roster if r["eligible"]]
    if limit:
        todo = todo[:limit]
    print(f"fetching {len(todo)} eligible event(s) into tapes/  (public, no-auth)",
          flush=True)
    ok = fail = skip = 0
    for i, ev in enumerate(todo, 1):
        try:
            msg = FT.bank(ev)
            if msg.startswith("skip"):
                skip += 1
            else:
                ok += 1
            if i % 20 == 0 or i == len(todo) or not msg.startswith("skip"):
                # flush=True is load-bearing: Python block-buffers stdout when it
                # is redirected to a file, so the first run of this looked dead
                # for 70 minutes while it was working fine. A long fetch whose
                # progress is invisible is a fetch someone kills.
                print(f"  [{i:>3}/{len(todo)}] {ev:<44} {msg}", flush=True)
        except Exception as e:                       # noqa: BLE001
            print(f"  [{i:>3}/{len(todo)}] {ev:<44} FAILED: {e}", flush=True)
            fail += 1
    print(f"\ndone: {ok} fetched, {skip} already banked, {fail} failed", flush=True)
    return 1 if fail and not ok else 0


def scored_events():
    """Eligible roster events that actually banked a tape AND a settlement."""
    import data as D
    if not os.path.exists(ROSTER):
        return []
    out = []
    for r in json.load(open(ROSTER))["events"]:
        if not r["eligible"]:
            continue
        ev = r["event"]
        if D.load_tape(ev, ev) and D.load_labels(ev, ev):
            out.append(ev)
    return sorted(out)


def cmd_verify_ordering():
    """Was the population declared before the data was seen?

    Checked in two links, because only one of them is provable from artifacts:

      pre-registration -> roster   by the hash+mtime witness stamped into the
                                   roster at enumeration time (`prereg_witness`)
      roster           -> tapes    by the roster's immutable `generated_utc`
                                   field against every probe tape's mtime

    The pre-registration's own mtime is deliberately NOT used: amending it — which
    is allowed — rewrites it. That is exactly how the first run of this probe lost
    its ordering artifact.
    """
    fmt = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    doc = json.load(open(ROSTER)) if os.path.exists(ROSTER) else {}
    gen = doc.get("generated_utc")
    evs = scored_events()
    times = {e: os.path.getmtime(os.path.join(TAPES, e)) for e in evs
             if os.path.exists(os.path.join(TAPES, e))}

    print(f"roster generated_utc     : {gen}          (immutable field)")
    if times:
        print(f"earliest probe tape      : {fmt(min(times.values()))}")
    print(f"probe events banked      : {len(evs)}")

    rc = 0
    print("\nLINK 1  pre-registration -> roster")
    if doc.get("prereg_sha256"):
        import hashlib
        now = hashlib.sha256(open(PREREG, "rb").read()).hexdigest()
        same = now == doc["prereg_sha256"]
        print(f"  witness recorded in roster : {doc['prereg_sha256'][:16]} "
              f"({doc.get('prereg_mtime_utc')})")
        print(f"  pre-registration now       : {now[:16]}"
              + ("  unchanged" if same else "  AMENDED since (allowed; §Notes)"))
        print("  VERIFIED — the roster witnesses the document it was declared against.")
    else:
        print("  NOT VERIFIABLE — this roster predates the hash witness.")
        print("  The ordering is recorded in the session log (pre-registration")
        print("  16:52:36Z, roster 16:56:47Z) but cannot be checked from the")
        print("  filesystem, because appending notes N1/N2 at 17:05:10Z reset the")
        print("  pre-registration's mtime. Logged as defect D1; do not claim a")
        print("  clean ordering proof for this run.")
        rc = 1

    print("\nLINK 2  roster -> probe tapes")
    early = [e for e, t in times.items() if gen and fmt(t) < gen]
    if not gen:
        print("  NOT VERIFIABLE — no roster.")
        rc = 1
    elif early:
        print(f"  FAIL — {len(early)} tape(s) predate the roster: {early[:5]}")
        rc = 1
    else:
        print(f"  VERIFIED — all {len(times)} probe tapes were written after the")
        print(f"  population was enumerated. No event entered the roster because")
        print(f"  of anything seen in its tape.")
    return rc


# --- self-test ----------------------------------------------------------------

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

    print("probe_series self-test:")

    # THE TRAP. The census classes these 'unclassified', not 'earnings-call'.
    # A class-based filter would admit them and quietly make this a replication.
    for t in ("KXEARNINGSMENTIONCRCL", "KXEARNINGSMENTIONRY",
              "KXEARNINGSMENTIONMCO", "KXEARNINGSMENTIONNVDA"):
        chk(f"{t} EXCLUDED despite its census class", False, in_population(t))
    chk("KXWORLDNEWSMENTION excluded (26JUL29 is in TRAIN)",
        False, in_population("KXWORLDNEWSMENTION"))
    for t in ("KXFIGHTMENTION", "KXHEARINGMENTION", "KXTRUMPMENTION",
              "KXMRBEASTMENTION", "KXNBAMENTION", "KXFEDMENTION"):
        chk(f"{t} included", True, in_population(t))
    chk("lowercase is normalised", False, in_population("kxearningsmentionaal"))
    chk("empty ticker excluded", False, in_population(""))
    chk("None excluded", False, in_population(None))

    # The real census must partition exactly, with no earnings leakage.
    if os.path.exists(CENSUS):
        rows = json.load(open(CENSUS))["series"]
        pop = population()
        leak = [t for t in pop if t.upper().startswith(EARNINGS_PREFIX)]
        chk("no earnings series leaks into the population", [], leak)
        chk("population + earnings + excluded == census", len(rows),
            len(pop) + sum(1 for r in rows
                           if not in_population(r.get("ticker"))))
        misclassed = [r["ticker"] for r in rows
                      if r["ticker"].startswith(EARNINGS_PREFIX)
                      and r.get("class") != "earnings-call"]
        chk("the trap is real: earnings series ARE misclassed in the census",
            True, len(misclassed) > 50)
        chk("...and every one of them is still excluded", True,
            all(not in_population(t) for t in misclassed))

    # Writes are confined to the lane.
    try:
        _write("/tmp/should-not-happen.json", {})
        chk("refuses to write outside the lane", "refused", "WROTE IT")
    except RuntimeError:
        chk("refuses to write outside the lane", "refused", "refused")

    print()
    if f:
        print(f"SELFTEST FAIL ({f} failed, {p} passed)")
        return 1
    print(f"SELFTEST PASS ({p} checks)")
    return 0


def main(argv):
    if "--self-test" in argv:
        return self_test()
    if "--list" in argv:
        return cmd_list()
    if "--fetch" in argv:
        lim = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
        return cmd_fetch(lim)
    if "--verify-ordering" in argv:
        return cmd_verify_ordering()
    if "--score" in argv:
        import score_probe
        return score_probe.main(argv)
    print(__doc__.strip().split("\n\n")[1])
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
