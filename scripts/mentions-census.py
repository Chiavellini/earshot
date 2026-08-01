#!/usr/bin/env python3
"""mentions-census.py — full census of Kalshi's Mentions category (Q-CENSUS).

Why this exists: a prefix sweep cannot discover a family it doesn't know
(three documented instances: BA, KXFEDMENTION, KXWORLDNEWSMENTION — see
VERIFICATIONS.md:1308-1328), and the taxonomy is inconsistent (two earnings
families, typo series, non-ticker names — Q-CENSUS pin, 2026-07-30). This
tool enumerates EVERY series in `category=Mentions` (no prefix filter),
samples each series' recent events, classifies, and diffs against a committed
baseline so blindness becomes a delta alert instead of a silent miss.

Two consumers:
  - W1.3 discovery: `--baseline slate/census.json` prints delta alerts
      NEW-FAMILY: <ticker> "<title>"            (series never seen before)
      OUT-OF-PREFIX-OPEN: <ticker> <event> …    (out-of-prefix series with an
                                                 open/unopened event in horizon)
    slate-daily greps these lines exactly like CHANGE:/UNSTAGED:.
  - W3 expansion: `--classify` adds audio-source class, cadence, and sampled
    liquidity per series for docs/EXPANSION.md triage.

Read-only. Request budget ≈ 1 + N(series) paced GETs (~400 today, ~3 min);
run it nightly-at-most (weekly wiring is the plan) — never inside an event
window.
"""

import argparse
import datetime
import json
import re
import sys
import time
import urllib.error
import urllib.request

API = "https://api.elections.kalshi.com/trade-api/v2"
UA = "earshot-census/1.0"

# Kept in sync with scripts/discover-events.py PREFIXES — the census exists to
# watch everything OUTSIDE this set, so it imports the concept, not the code.
PREFIXES = ("KXEARNINGSMENTION", "KXFEDMENTION", "KXWORLDNEWSMENTION")

CLASS_RULES = [
    ("earnings-call", re.compile(r"earnings call", re.I)),
    ("press-conference", re.compile(r"press (conference|briefing)|state of the union|joint (session|address)", re.I)),
    ("hearing", re.compile(r"hearing|testimony", re.I)),
    ("sports-tv", re.compile(r"announcers say|game|match|fight|race day", re.I)),
    ("speech-appearance", re.compile(r"say during (his|her|their)|rally|speech|announcement|meeting|summit|debate", re.I)),
    ("recurring-broadcast", re.compile(r"tonight|show|episode|SNL|newscast|front.?page|podcast|youtube|video|the view|morning", re.I)),
    ("recurring-person", re.compile(r"say (this week|in [A-Z][a-z]+|before)", re.I)),
]


def classify(title):
    for label, rx in CLASS_RULES:
        if rx.search(title or ""):
            return label
    return "unclassified"


def get(path, tries=4, pace=0.4):
    """GET with backoff. 429 is expected under load, not exceptional."""
    delay = 1.0
    for attempt in range(tries):
        try:
            req = urllib.request.Request(f"{API}/{path}", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                time.sleep(pace)
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError(f"exhausted retries: {path}")


def all_mention_series():
    out, cursor = [], ""
    while True:
        d = get("series?category=Mentions&limit=200" + (f"&cursor={cursor}" if cursor else ""))
        batch = d.get("series", [])
        out += [{"ticker": s.get("ticker", ""), "title": s.get("title", "")} for s in batch]
        cursor = d.get("cursor") or ""
        if not cursor or not batch:
            return out


def event_date_from_ticker(event_ticker):
    """Best-effort date from the -yyMONdd suffix. DISPLAY ONLY — the HOOD pin
    proves ticker dates lie; nothing downstream may schedule off this."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})[A-Z]?$", event_ticker or "")
    if not m:
        return None
    months = {m_: i for i, m_ in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
    mon = months.get(m.group(2))
    if not mon:
        return None
    try:
        return datetime.date(2000 + int(m.group(1)), mon, int(m.group(3))).isoformat()
    except ValueError:
        return None


def survey_series(ticker, sample_events=6):
    """Recent events for one series (any status), with nested markets where
    the API provides them. Failures return {'error': ...} and are COUNTED —
    never silently dropped (the discover-events.py :340 lesson)."""
    try:
        d = get(f"events?series_ticker={ticker}&limit={sample_events}&with_nested_markets=true")
    except Exception as exc:
        return {"error": str(exc)}
    evs = d.get("events", [])
    rows = []
    for e in evs:
        markets = e.get("markets") or []
        opens = [m.get("open_time") for m in markets if m.get("open_time")]
        vol = sum(m.get("volume") or 0 for m in markets)
        oi = sum(m.get("open_interest") or 0 for m in markets)
        statuses = {m.get("status") for m in markets}
        rows.append({
            "event_ticker": e.get("event_ticker"),
            "title": e.get("title"),
            "ticker_date": event_date_from_ticker(e.get("event_ticker")),
            "sub_title": e.get("sub_title"),
            "n_markets": len(markets),
            "statuses": sorted(s for s in statuses if s),
            "first_open_time": min(opens) if opens else None,
            "volume": vol, "open_interest": oi,
        })
    return {"events": rows}


def cadence_of(events):
    dates = sorted(d for d in (r.get("ticker_date") for r in events) if d)
    if len(dates) < 2:
        return "single-or-unknown"
    gaps = [(datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days
            for a, b in zip(dates, dates[1:])]
    med = sorted(gaps)[len(gaps) // 2]
    if med <= 2:
        return "daily"
    if med <= 10:
        return "weekly"
    if med <= 45:
        return "monthly"
    return "quarterly+"


def build_census(classify_mode):
    series = all_mention_series()
    print(f"census: {len(series)} Mentions series", file=sys.stderr)
    out, errors = [], 0
    for i, s in enumerate(series):
        t = s["ticker"]
        row = {
            "ticker": t,
            "title": s["title"],
            "in_prefixes": t.startswith(PREFIXES),
        }
        sv = survey_series(t)
        if "error" in sv:
            errors += 1
            row["survey_error"] = sv["error"]
        else:
            evs = sv["events"]
            row["n_events_sampled"] = len(evs)
            row["latest_event"] = evs[0]["event_ticker"] if evs else None
            row["latest_ticker_date"] = evs[0]["ticker_date"] if evs else None
            open_now = [r for r in evs
                        if any(st in ("active", "initialized") or st == "open"
                               for st in r["statuses"])]
            row["open_events"] = [r["event_ticker"] for r in open_now]
            if classify_mode:
                # series titles are terse ("Ackman Mention"); the event title
                # carries the venue ("What will X say during …?") — use both
                row["class"] = classify(s["title"])
                if row["class"] == "unclassified" and evs:
                    row["class"] = classify(evs[0].get("title"))
                row["cadence"] = cadence_of(evs)
                row["sampled_volume"] = sum(r["volume"] for r in evs)
                row["sampled_open_interest"] = sum(r["open_interest"] for r in evs)
        out.append(row)
        if (i + 1) % 50 == 0:
            print(f"census: {i + 1}/{len(series)} surveyed, {errors} errors", file=sys.stderr)
    return {
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "series_count": len(series),
        "survey_errors": errors,
        "prefixes": list(PREFIXES),
        "series": out,
    }


def horizon_dates(days):
    today = datetime.date.today()
    return {(today + datetime.timedelta(days=i)).isoformat() for i in range(days + 1)}


def diff_alerts(census, baseline, horizon_days):
    """The W1.3 delta contract. Returns printable alert lines."""
    known = {s["ticker"] for s in baseline.get("series", [])}
    prev_open = {s["ticker"]: set(s.get("open_events") or [])
                 for s in baseline.get("series", [])}
    horizon = horizon_dates(horizon_days)
    lines = []
    for s in census["series"]:
        t = s["ticker"]
        if t not in known:
            lines.append(f'NEW-FAMILY: {t} "{s.get("title", "")}"')
            continue
        if not s["in_prefixes"]:
            fresh = set(s.get("open_events") or []) - prev_open.get(t, set())
            for ev in sorted(fresh):
                d = event_date_from_ticker(ev)
                if d is None or d in horizon:
                    lines.append(
                        f'OUT-OF-PREFIX-OPEN: {t} {ev} '
                        f'(date-from-ticker {d or "UNKNOWN — check sub_title"}) '
                        f'"{s.get("title", "")}"')
    return lines


def self_test():
    baseline = {"series": [
        {"ticker": "KXOLDMENTION", "in_prefixes": False, "open_events": []},
        {"ticker": "KXEARNINGSMENTIONAAPL", "in_prefixes": True, "open_events": []},
    ]}
    today = datetime.date.today().isoformat().replace("-", "")
    census = {"series": [
        # never-seen series must alert
        {"ticker": "KXBRANDNEWMENTION", "title": "What will X say?", "in_prefixes": False,
         "open_events": []},
        # out-of-prefix series gaining an open event must alert (dateless suffix
        # falls inside horizon by the None rule)
        {"ticker": "KXOLDMENTION", "title": "Old", "in_prefixes": False,
         "open_events": ["KXOLDMENTION-26JUL30"]},
        # in-prefix series gaining events must NOT alert (the normal sweep owns it)
        {"ticker": "KXEARNINGSMENTIONAAPL", "title": "Apple", "in_prefixes": True,
         "open_events": ["KXEARNINGSMENTIONAAPL-26DEC01"]},
    ]}
    lines = diff_alerts(census, baseline, horizon_days=400)
    assert any(l.startswith("NEW-FAMILY: KXBRANDNEWMENTION") for l in lines), lines
    assert any("KXOLDMENTION-26JUL30" in l for l in lines), lines
    assert not any("AAPL" in l for l in lines), lines
    assert event_date_from_ticker("KXWORLDNEWSMENTION-26JUL29") == "2026-07-29"
    assert event_date_from_ticker("KXDEBATEMENTION-26AUG03B") == "2026-08-03"
    assert event_date_from_ticker("KXFEDMENTION-26JUL") is None
    assert classify("What will Apple say during their next earnings call?") == "earnings-call"
    assert classify("What will Powell say during June Press Conference?") == "press-conference"
    print("self-test OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="write census JSON here")
    ap.add_argument("--baseline", default=None, help="prior census to diff against")
    ap.add_argument("--horizon-days", type=int, default=21)
    ap.add_argument("--classify", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return

    census = build_census(args.classify)

    if args.baseline:
        try:
            with open(args.baseline) as f:
                baseline = json.load(f)
            for line in diff_alerts(census, baseline, args.horizon_days):
                print(line)
        except FileNotFoundError:
            print(f"census: no baseline at {args.baseline} — first run, no delta",
                  file=sys.stderr)

    if census["survey_errors"]:
        print(f"CENSUS-DEGRADED: {census['survey_errors']} of "
              f"{census['series_count']} series surveys failed")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(census, f, indent=1)
        print(f"census: wrote {args.out} ({census['series_count']} series, "
              f"{census['survey_errors']} errors)", file=sys.stderr)


if __name__ == "__main__":
    main()
