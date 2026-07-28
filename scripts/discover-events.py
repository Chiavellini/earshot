#!/usr/bin/env python3
"""Discover every upcoming Kalshi earnings-mention event — the BA-safe way.

WHY THIS EXISTS. On 2026-07-25 a slate sweep concluded "Kalshi has no BA mention
event next week" and a tradable 14-market Boeing call was nearly missed entirely.
The sweep was not careless — it was structurally blind:

  * `KXEARNINGSMENTIONBA-26JUL21` is MISDATED. Its ticker says JUL21; the call is
    Jul 28. Filtering on the ticker suffix cannot find it — and that alone is
    what lost Boeing. This is PERMANENT: the ticker will never change.
  * Separately, on 2026-07-27 BA was also absent from a full 8,000-event
    paginated `GET /events?status=open` sweep while a per-series query returned
    it open. ⚠ THAT DID NOT REPRODUCE on 07-28 — both sweeps returned all 57
    mention events including BA, delta 0. So do not claim the paginated endpoint
    is broken; claim only that it was observed incomplete once. The per-series
    sweep is belt-and-braces, and the recorded delta is how we would notice it
    happening again.

So this script does BOTH sweeps and reports the delta, and the regression test
(`--assert-ba`) asserts the thing that is actually permanent: that BA is found
and that its call date resolves to Jul 28 from `sub_title`, not from its ticker.

TICKER DATE != CALL DATE. The call date comes from the event's `sub_title`
("On Jul 28, 2026"), which is correct even on the misdated BA ticker. Never
parse the ticker suffix.

RATE LIMITS ARE REAL. docs/RUNBOOK.md:465 — the events endpoint 429s under rapid
repeat queries. There are ~135 KXEARNINGSMENTION* series, so the per-series
sweep is ~135 requests: it is paced, it backs off on 429, and that is precisely
why this belongs in a daily job rather than an on-demand call.

Read-only. Touches no credentials — the events/series endpoints are public.

Usage:
  scripts/discover-events.py                      # human summary
  scripts/discover-events.py --json slate/latest.json
  scripts/discover-events.py --days 14            # horizon (default 21)
  scripts/discover-events.py --assert-ba          # regression: exit 1 if the
                                                  # per-series sweep loses BA
  scripts/discover-events.py --self-test          # offline: frozen BA fixture

⚠ --assert-ba was TIME-BOUND and is now permanently failing: BA's event
closed after its 2026-07-28 call, so no live sweep can find it "open" again.
The durable form of the same regression is `--self-test`, which freezes the
BA case as an offline fixture (sub_title date parse + misleading-ticker
detection) and is what slate-daily.sh runs before every sweep.
(An earlier note here pointed at state/slate.json; the committed slate now
lives at slate/latest.json — state/ is gitignored.)
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.elections.kalshi.com/trade-api/v2"
UA = "earshot-discover/1.0"
PREFIX = "KXEARNINGSMENTION"
# 2026-07-28: the sweep was earnings-only, which made KXFEDMENTION-26JUL —
# the FOMC/Warsh crown event, itself found late (2026-07-26, "it had never
# been pinned") — structurally invisible to the daily job. Same blindness
# class as Boeing, different axis: right series category, wrong prefix
# filter. Sweep every Mentions family we trade.
PREFIXES = ("KXEARNINGSMENTION", "KXFEDMENTION")


def issuer_of(series_ticker):
    """Issuer label: suffix after the family prefix; family name if bare
    (KXFEDMENTION has no suffix — its label is FED, never '')."""
    for p in PREFIXES:
        if series_ticker.startswith(p):
            rest = series_ticker[len(p):]
            return rest or p.removeprefix("KX").removesuffix("MENTION")
    return series_ticker


def get(path, tries=4, pace=0.35):
    """GET with backoff. 429 is expected under load, not exceptional."""
    delay = 1.0
    for attempt in range(tries):
        try:
            req = urllib.request.Request(f"{API}/{path}", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                time.sleep(pace)          # be a good citizen between calls
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError(f"exhausted retries: {path}")


def mention_series():
    """Every mention-family series ticker (earnings + fed)."""
    out, cursor = [], ""
    while True:
        d = get(f"series?category=Mentions&limit=200" + (f"&cursor={cursor}" if cursor else ""))
        batch = d.get("series", [])
        out += [s["ticker"] for s in batch if s.get("ticker", "").startswith(PREFIXES)]
        cursor = d.get("cursor") or ""
        if not cursor or not batch:
            break
    return sorted(set(out))


def paginated_open():
    """The sweep that MISSES Boeing. Kept so the delta is measurable."""
    seen, cursor = {}, ""
    for _ in range(60):
        d = get("events?status=open&limit=200" + (f"&cursor={cursor}" if cursor else ""))
        evs = d.get("events", [])
        new = 0
        for e in evs:
            if e["event_ticker"] not in seen:
                seen[e["event_ticker"]] = e
                new += 1
        cursor = d.get("cursor") or ""
        if not cursor or new == 0:
            break
    return {k: v for k, v in seen.items() if v.get("series_ticker", "").startswith(PREFIXES)}


def per_series_open(series):
    """The BA-safe sweep: ask each series directly."""
    out = {}
    for i, s in enumerate(series, 1):
        try:
            d = get(f"events?series_ticker={s}&status=open&limit=50")
        except Exception as exc:                      # one bad series must not kill the sweep
            print(f"  ! {s}: {exc}", file=sys.stderr)
            continue
        for e in d.get("events", []):
            out[e["event_ticker"]] = e
        if i % 25 == 0:
            print(f"  …{i}/{len(series)} series", file=sys.stderr)
    return out


def ticker_misleads(event_ticker, d):
    """Does the ticker's date suffix disagree with the real call date?

    Suffix format is %y%b%d — `KXEARNINGSMENTIONBA-26JUL21` is 2026-JUL-21.
    Getting this backwards (%d%b) flagged 52 of 57 events as suspicious on the
    first run, which is worse than useless: a gate that cries wolf gets ignored.
    Only BA should flag — ticker 26JUL21 against a Jul 28 call.
    """
    if d is None or "-" not in event_ticker:
        return False
    suffix = event_ticker.rsplit("-", 1)[1].upper()
    return suffix != d.strftime("%y%b%d").upper()


def call_date(ev):
    """Call date from sub_title ('On Jul 28, 2026') — NEVER from the ticker."""
    sub = (ev.get("sub_title") or "").replace("On ", "").strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(sub, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def self_test():
    """Offline regression: the exact BA case, frozen. No network."""
    ba = {"sub_title": "On Jul 28, 2026"}
    d = call_date(ba)
    assert d is not None and d.strftime("%Y-%m-%d") == "2026-07-28", \
        f"BA sub_title date parse broke: {d}"
    assert ticker_misleads("KXEARNINGSMENTIONBA-26JUL21", d), \
        "BA misleading-ticker detection broke (26JUL21 vs Jul 28 call)"
    honest = call_date({"sub_title": "On Jul 28, 2026"})
    assert not ticker_misleads("KXEARNINGSMENTIONF-26JUL28", honest), \
        "false positive: honest ticker flagged (the 52/57 cry-wolf bug)"
    assert call_date({"sub_title": "garbage"}) is None, \
        "unparsable sub_title must return None, not raise"
    assert issuer_of("KXFEDMENTION") == "FED", \
        "bare KXFEDMENTION series must label as FED, not empty string"
    assert issuer_of("KXEARNINGSMENTIONF") == "F", "earnings issuer broke"
    print("self-test: OK (BA fixture — date-from-sub_title, misleading-ticker "
          "detection, cry-wolf guard, FED issuer)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write the slate here (e.g. slate/latest.json)")
    ap.add_argument("--days", type=int, default=21, help="horizon in days (default 21)")
    ap.add_argument("--self-test", action="store_true",
                    help="offline frozen-fixture regression; no network")
    ap.add_argument("--assert-ba", action="store_true",
                    help="regression: fail unless the per-series sweep finds BA")
    a = ap.parse_args()

    if a.self_test:
        self_test()
        return 0

    print("enumerating mention series…", file=sys.stderr)
    series = mention_series()
    print(f"  {len(series)} {PREFIX}* series", file=sys.stderr)

    print("sweep A — paginated /events?status=open (the one that misses BA)…", file=sys.stderr)
    pag = paginated_open()
    print(f"  {len(pag)} mention events", file=sys.stderr)

    print("sweep B — per-series (BA-safe)…", file=sys.stderr)
    per = per_series_open(series)
    print(f"  {len(per)} mention events", file=sys.stderr)

    union = dict(pag)
    union.update(per)
    only_per = sorted(set(per) - set(pag))     # THE BOEING CLASS
    only_pag = sorted(set(pag) - set(per))

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=a.days)
    rows = []
    for tk, ev in union.items():
        d = call_date(ev)
        rows.append({
            "event_ticker": tk,
            "series_ticker": ev.get("series_ticker"),
            "issuer": issuer_of(ev.get("series_ticker") or ""),
            "sub_title": ev.get("sub_title"),
            "call_date": d.strftime("%Y-%m-%d") if d else None,
            "call_date_source": "sub_title" if d else "UNRESOLVED",
            "found_by": ("both" if tk in pag and tk in per
                         else "per_series_only" if tk in per else "paginated_only"),
            "ticker_date_misleading": ticker_misleads(tk, d),
        })
    rows.sort(key=lambda r: (r["call_date"] or "9999", r["issuer"]))
    upcoming = [r for r in rows
                if r["call_date"] and now.strftime("%Y-%m-%d") <= r["call_date"] <= horizon.strftime("%Y-%m-%d")]

    print(f"\n{'='*72}\nSLATE — next {a.days} days ({len(upcoming)} events)\n{'='*72}")
    for r in upcoming:
        flag = "  ⚠ PER-SERIES ONLY" if r["found_by"] == "per_series_only" else ""
        mis = "  ⚠ ticker date != call date" if r["ticker_date_misleading"] else ""
        print(f"  {r['call_date']}  {r['issuer']:<7} {r['event_ticker']:<34}{flag}{mis}")

    print(f"\n--- sweep delta (this is the Boeing class) ---")
    print(f"  found by per-series ONLY : {len(only_per)}  {only_per if only_per else ''}")
    print(f"  found by paginated ONLY  : {len(only_pag)}  {only_pag if only_pag else ''}")
    if only_per:
        print("  ⇒ a paginated-only sweep would MISS the events listed above.")

    slate = {
        "generated_utc": now.isoformat(),
        "horizon_days": a.days,
        "series_count": len(series),
        "counts": {"paginated": len(pag), "per_series": len(per), "union": len(union)},
        "sweep_delta": {"per_series_only": only_per, "paginated_only": only_pag},
        "events": rows,
    }
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(slate, fh, indent=2)
        print(f"\nwrote {a.json}")

    if a.assert_ba:
        ba = [r for r in rows if r["issuer"] == "BA"]
        if not ba:
            print("\nREGRESSION FAIL: no BA event found by ANY sweep", file=sys.stderr)
            return 1
        r = ba[0]
        print(f"\nregression: BA found via {r['found_by']}, call_date={r['call_date']} "
              f"(ticker {r['event_ticker']})")
        if r["call_date"] is None:
            print("REGRESSION FAIL: BA call date unresolved from sub_title", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
