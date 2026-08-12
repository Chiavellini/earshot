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

An authoritative source may correct a stale discovery date, but only through
an explicit typed record.  Merely supplying a time on another date remains a
refusal:

    "date_correction": {
      "schema": 1,
      "kind": "call-date-correction",
      "from": "2026-08-06",
      "to": "2026-08-12",
      "source": "Issuer IR release and event registration page: ...",
      "recorded_utc": "2026-08-11T20:00:00Z"
    }

Rules (all refusals are loud, none are silent):
  * an override whose DATE part != the effective call_date is REFUSED unless
    the entry carries the exact typed/source-bearing correction above;
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
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CORRECTION_FIELDS = {"schema", "kind", "from", "to", "source", "recorded_utc"}


def valid_timestamp(value: str) -> bool:
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        return False
    try:
        import datetime
        datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def validate_correction(ticker: str, row: dict, entry: dict) -> tuple[dict | None, str | None]:
    """Return (validated correction, refusal).  Never mutates the slate row."""
    correction = entry.get("date_correction")
    if correction is None:
        return None, None
    if not isinstance(correction, dict) or set(correction) != CORRECTION_FIELDS:
        return None, (
            f"REFUSED {ticker}: date_correction must contain exactly "
            f"{sorted(CORRECTION_FIELDS)}"
        )
    if correction.get("schema") != 1 or correction.get("kind") != "call-date-correction":
        return None, (
            f"REFUSED {ticker}: date_correction must be schema=1, "
            'kind="call-date-correction"'
        )
    old, new = correction.get("from"), correction.get("to")
    try:
        if not isinstance(old, str) or not DATE_RE.fullmatch(old):
            raise ValueError
        if not isinstance(new, str) or not DATE_RE.fullmatch(new):
            raise ValueError
        import datetime
        datetime.date.fromisoformat(old)
        datetime.date.fromisoformat(new)
    except ValueError:
        return None, f"REFUSED {ticker}: correction from/to must be real YYYY-MM-DD dates"
    if old == new:
        return None, f"REFUSED {ticker}: correction from/to are identical ({old})"
    source = correction.get("source")
    if not isinstance(source, str) or not source.strip():
        return None, f"REFUSED {ticker}: date_correction source is empty"
    if source.strip().upper().startswith("ESTIMATE"):
        return None, f"REFUSED {ticker}: an ESTIMATE cannot authoritatively correct call_date"
    if not valid_timestamp(correction.get("recorded_utc")):
        return None, f"REFUSED {ticker}: date_correction recorded_utc is not a real UTC timestamp"
    current = row.get("call_date")
    prior = row.get("call_date_correction")
    if current == new and prior == correction:
        return correction, None  # idempotent merge of an already-corrected row
    if current != old:
        return None, (
            f"REFUSED {ticker}: date_correction.from {old} does not match slate "
            f"call_date {current!r}"
        )
    return correction, None


def merge(slate: dict, times: dict, require_date: str | None) -> tuple[dict, list[str], list[str]]:
    """Returns (merged_slate, errors, notes)."""
    errors: list[str] = []
    notes: list[str] = []
    by_ticker = {e.get("event_ticker"): e for e in slate.get("events", [])}

    for ticker, entry in times.items():
        if not isinstance(entry, dict):
            errors.append(f"REFUSED {ticker}: call-times entry is not an object")
            continue
        t = entry.get("call_time_utc", "")
        row = by_ticker.get(ticker)
        if row is None:
            notes.append(f"note: {ticker} in call-times but not on the slate — ignored")
            continue
        correction, correction_error = validate_correction(ticker, row, entry)
        if correction_error:
            errors.append(correction_error)
            continue
        effective_date = correction["to"] if correction else row.get("call_date")
        if not valid_timestamp(t):
            errors.append(f"REFUSED {ticker}: call_time_utc {t!r} is not YYYY-MM-DDTHH:MM:SSZ")
            continue
        if effective_date and t[:10] != effective_date:
            errors.append(
                f"REFUSED {ticker}: override date {t[:10]} != effective call_date "
                f"{effective_date} (a cross-date time needs a matching typed date_correction)"
            )
            continue
        existing = row.get("call_time_utc")
        if existing and existing != t:
            old_date = correction.get("from") if correction else None
            if not correction or existing[:10] != old_date:
                errors.append(
                    f"REFUSED {ticker}: discovery already has {existing} (gcal-sourced, "
                    f"outranks an untyped IR reading) but call-times says {t} — reconcile"
                )
                continue
        src = str(entry.get("source", ""))
        if src.upper().startswith("ESTIMATE"):
            notes.append(f"⚠ ESTIMATE {ticker}: {t} ({src}) — ladders absorb ±30min; verify at first light")
        if correction:
            old_date = row.get("call_date")
            row["call_date"] = correction["to"]
            row["call_date_source"] = "authoritative_correction"
            row["call_date_confirmed"] = True
            row["call_date_disputed"] = False
            row["ticker_date_misleading"] = True
            row["stale_open_event"] = False
            row["call_date_evidence"] = correction["source"]
            row["call_date_correction"] = dict(correction)
            notes.append(
                f"CORRECTED-DATE {ticker}: {old_date} -> {correction['to']} "
                f"({correction['source']})"
            )
        row["call_time_utc"] = t
        if src:
            row["call_time_evidence"] = src

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
    # wrong date refused without typed correction
    _, errs, _ = merge(json.loads(json.dumps(base)), {"T1": {"call_time_utc": "2026-08-06T21:00:00Z"}}, None)
    assert errs and "typed date_correction" in errs[0], errs
    # exact typed, source-bearing correction supersedes a stale date
    corrected = {
        "call_time_utc": "2026-08-06T21:00:00Z",
        "source": "Issuer event page: 5 PM ET",
        "date_correction": {
            "schema": 1,
            "kind": "call-date-correction",
            "from": "2026-08-05",
            "to": "2026-08-06",
            "source": "Issuer IR release plus event registration page",
            "recorded_utc": "2026-08-04T21:00:00Z",
        },
    }
    s, errs, notes = merge(json.loads(json.dumps(base)), {"T1": corrected}, None)
    assert not errs and s["events"][0]["call_date"] == "2026-08-06", errs
    assert s["events"][0]["call_date_source"] == "authoritative_correction"
    assert any("CORRECTED-DATE" in n for n in notes), notes
    # malformed/un sourced correction cannot change the date
    bad = json.loads(json.dumps(corrected))
    bad["date_correction"]["source"] = ""
    s, errs, _ = merge(json.loads(json.dumps(base)), {"T1": bad}, None)
    assert errs and s["events"][0]["call_date"] == "2026-08-05", (s, errs)
    # gcal disagreement refused
    _, errs, _ = merge(json.loads(json.dumps(base)), {"T2": {"call_time_utc": "2026-08-05T13:00:00Z"}}, None)
    assert errs and "outranks" in errs[0], errs
    # bad format refused
    _, errs, _ = merge(json.loads(json.dumps(base)), {"T1": {"call_time_utc": "2026-08-05 21:00"}}, None)
    assert errs and "not YYYY" in errs[0], errs
    # require-date names the untimed
    _, errs, _ = merge(json.loads(json.dumps(base)), {"T1": {"call_time_utc": "2026-08-05T21:00:00Z"}}, "2026-08-05")
    assert len(errs) == 1 and "T3" in errs[0], errs
    print("slate-time-merge: self-test PASS (7 cases)")
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
