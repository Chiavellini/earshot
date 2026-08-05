#!/usr/bin/env python3
"""Merge hand-verified call times into the slate → slate/latest-timed.json.

    slate/latest.json  +  slate/call-times.json  →  slate/latest-timed.json

Why: discovery only ever gets call_time_utc from a gcal link, and
slate-stage.sh refuses to write an autorun env (FIRE_HHMM, timers) without a
time. Editing slate/latest.json by hand is wrong — the nightly sweep
overwrites it. This is the merge layer; slate-stage consumes the output via
--slate slate/latest-timed.json.

call-times.json contract (keyed by event_ticker):
    { "KXEARNINGSMENTIONDIS-26AUG05": {
        "call_time_utc": "2026-08-05T12:30:00Z",
        "source": "IR page: 'August 5, 2026 5:30 AM PT' (<url>)",
        "recorded_utc": "2026-08-05T01:00:00Z" } }

Rules (all refusals are loud, none are silent):
  * an override whose DATE part != the row's call_date is REFUSED
    (the HOOD/PGR misdate class — a wrong date staged is worse than unstaged);
  * if discovery already carries a non-null gcal time that DISAGREES, FAIL —
    gcal sits above IR-page-reading in the evidence hierarchy;
  * --require-date YYYY-MM-DD exits 1 listing that date's still-untimed events;
  * times must be exactly YYYY-MM-DDTHH:MM:SSZ (what slate-stage parses);
  * an entry whose source starts with "ESTIMATE" is accepted but printed
    loudly — watcher ladders absorb ±30 min, silence absorbs nothing.

Exit 0 ok / 1 refusal or unmet --require-date / 2 usage.  --self-test covers
the refusal classes. Node: control.prepare (staging input shaping).
"""
import json
import re
import sys
from pathlib import Path

TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def merge(slate: dict, times: dict, require_date: str | None) -> tuple[dict, list[str], list[str]]:
    """Returns (merged_slate, errors, notes)."""
    errors: list[str] = []
    notes: list[str] = []
    by_ticker = {e.get("event_ticker"): e for e in slate.get("events", [])}

    for ticker, entry in times.items():
        t = entry.get("call_time_utc", "")
        row = by_ticker.get(ticker)
        if row is None:
            notes.append(f"note: {ticker} in call-times but not on the slate — ignored")
            continue
        if not TIME_RE.match(t):
            errors.append(f"REFUSED {ticker}: call_time_utc {t!r} is not YYYY-MM-DDTHH:MM:SSZ")
            continue
        if row.get("call_date") and t[:10] != row["call_date"]:
            errors.append(
                f"REFUSED {ticker}: override date {t[:10]} != slate call_date "
                f"{row['call_date']} (HOOD/PGR misdate class — resolve before staging)"
            )
            continue
        existing = row.get("call_time_utc")
        if existing and existing != t:
            errors.append(
                f"REFUSED {ticker}: discovery already has {existing} (gcal-sourced, "
                f"outranks IR reading) but call-times says {t} — reconcile"
            )
            continue
        src = str(entry.get("source", ""))
        if src.upper().startswith("ESTIMATE"):
            notes.append(f"⚠ ESTIMATE {ticker}: {t} ({src}) — ladders absorb ±30min; verify at first light")
        row["call_time_utc"] = t
        row.setdefault("call_time_evidence", src)

    if require_date:
        missing = [
            e["event_ticker"]
            for e in slate.get("events", [])
            if e.get("call_date") == require_date and not e.get("call_time_utc")
        ]
        for m in missing:
            errors.append(f"UNTIMED for {require_date}: {m} — resolve a time or descope-event.sh it")
    return slate, errors, notes


def self_test() -> int:
    base = {
        "events": [
            {"event_ticker": "T1", "call_date": "2026-08-05", "call_time_utc": None},
            {"event_ticker": "T2", "call_date": "2026-08-05", "call_time_utc": "2026-08-05T12:00:00Z"},
            {"event_ticker": "T3", "call_date": "2026-08-05", "call_time_utc": None},
        ]
    }
    # good merge
    s, errs, _ = merge(json.loads(json.dumps(base)), {"T1": {"call_time_utc": "2026-08-05T21:00:00Z", "source": "x"}}, None)
    assert not errs and s["events"][0]["call_time_utc"] == "2026-08-05T21:00:00Z", errs
    # wrong date refused
    _, errs, _ = merge(json.loads(json.dumps(base)), {"T1": {"call_time_utc": "2026-08-06T21:00:00Z"}}, None)
    assert errs and "misdate" in errs[0], errs
    # gcal disagreement refused
    _, errs, _ = merge(json.loads(json.dumps(base)), {"T2": {"call_time_utc": "2026-08-05T13:00:00Z"}}, None)
    assert errs and "outranks" in errs[0], errs
    # bad format refused
    _, errs, _ = merge(json.loads(json.dumps(base)), {"T1": {"call_time_utc": "2026-08-05 21:00"}}, None)
    assert errs and "not YYYY" in errs[0], errs
    # require-date names the untimed
    _, errs, _ = merge(json.loads(json.dumps(base)), {"T1": {"call_time_utc": "2026-08-05T21:00:00Z"}}, "2026-08-05")
    assert len(errs) == 1 and "T3" in errs[0], errs
    print("slate-time-merge: self-test PASS (5 cases)")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--self-test" in args:
        return self_test()
    slate_p = Path("slate/latest.json")
    times_p = Path("slate/call-times.json")
    out_p = Path("slate/latest-timed.json")
    require = None
    it = iter(args)
    for a in it:
        if a == "--require-date":
            require = next(it, None)
        elif a == "--slate":
            slate_p = Path(next(it, ""))
        elif a == "--times":
            times_p = Path(next(it, ""))
        elif a == "--out":
            out_p = Path(next(it, ""))
        else:
            print(f"unknown arg {a}", file=sys.stderr)
            return 2
    if not slate_p.exists():
        print(f"no slate at {slate_p}", file=sys.stderr)
        return 2
    times = json.loads(times_p.read_text()) if times_p.exists() else {}
    slate = json.loads(slate_p.read_text())
    merged, errors, notes = merge(slate, times, require)
    for n in notes:
        print(n)
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    tmp = out_p.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=1))
    tmp.rename(out_p)
    timed = sum(1 for e in merged.get("events", []) if e.get("call_time_utc"))
    print(f"wrote {out_p}: {timed}/{len(merged.get('events', []))} events timed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
