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

AND THE SUB_TITLE ITSELF CAN GO STALE (2026-08-04, PGR — the third misdating
class). Kalshi reuses ONE event ticker per series for the standing question
"What will <issuer> say during their NEXT earnings call?" and does not re-date
it when the event rolls forward. `KXEARNINGSMENTIONPGR-26JUL15` still carried
`sub_title = "On Jul 15, 2026"` on Aug 4 while its 12 markets were `status=open`
and trading — measured 14:33–14:54Z, DURING Progressive's Aug 4 call. The
`today <= call_date` worklist filter therefore dropped a live, liquid board, and
Progressive was never on the slate at all. See `stale_open_event()` below: this
is the one contradiction the sweep can detect for free, because BOTH sweeps
query `status=open` — so a past call_date on a returned event is self-refuting.

RATE LIMITS ARE REAL. docs/history/RUNBOOK.md:465 — the events endpoint 429s under rapid
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
import html as html_mod
import io
import json
import os
import re
import sys
import tempfile
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
#
# 2026-07-29: third instance of the same class. KXWORLDNEWSMENTION — "What
# will the reporters say during ABC World News Tonight?" — is a DAILY market
# (airs 6:30 PM ET = 22:30Z = 16:30 CDMX, ~250 sessions/yr), and this sweep had
# never once seen it. A daily market is worth more than any single earnings
# call because every fix amortises across every session; missing it is the
# most expensive version of this bug so far. Found only by hand-listing
# `GET /series?category=Mentions` (397 series) and reading the titles.
# ⇒ If you add a family here, also check whether it needs DAILY_SERIES below.
PREFIXES = ("KXEARNINGSMENTION", "KXFEDMENTION", "KXWORLDNEWSMENTION")

# ── dateless recurring series ────────────────────────────────────────────────
# call_date() reads the airdate out of sub_title and is right to refuse the
# ticker (see its docstring and commit 6be80aa: "HOOD was not a
# date-from-ticker bug — Kalshi's metadata lies in stereo"). But a recurring
# broadcast has no per-session sub_title date to read: KXWORLDNEWSMENTION's is
# "any host or reporter - ABC World News Tonight with David Muir", constant
# across every session. So these events resolve to UNRESOLVED and drop out of
# the horizon list and worklist.md — present in the JSON, invisible in practice.
#
# The narrow fix, deliberately NOT a general date-from-ticker inference:
# an explicit per-series opt-in, tagged `call_date_source: "ticker_daily"` so
# the provenance stays visibly weaker than the sub_title path. Value = the
# broadcast's daily air time (UTC), used only for reporting.
#
# ⚠ ticker_misleads() is MEANINGLESS for these — it compares the ticker suffix
# against a date derived FROM that suffix, so it always agrees with itself.
# That self-agreement is the exact HOOD failure mode, so the row reports None
# (unknown), never False (checked and clean).
DAILY_SERIES = {
    "KXWORLDNEWSMENTION": {"label": "ABC World News Tonight", "air_utc": "22:30"},
}


def issuer_of(series_ticker):
    """Issuer label: suffix after the family prefix; family name if bare
    (KXFEDMENTION has no suffix — its label is FED, never '')."""
    for p in PREFIXES:
        if series_ticker.startswith(p):
            rest = series_ticker[len(p):]
            return rest or p.removeprefix("KX").removesuffix("MENTION")
    return series_ticker


def get(path, tries=4, pace=0.35):
    """GET with bounded backoff for rate limits and transient network loss."""
    delay = 1.0
    for attempt in range(tries):
        try:
            req = urllib.request.Request(f"{API}/{path}", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                time.sleep(pace)          # be a good citizen between calls
                return json.load(r)
        except urllib.error.HTTPError as e:
            if (e.code == 429 or 500 <= e.code <= 599) and attempt < tries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            # Aug 18's one-shot DNS failure made the slate two days stale,
            # hid AAP/ROST, and also killed the failure email. Discovery is a
            # required morning input, so resolver/connectivity transients get
            # the same bounded retry discipline as 429s.
            if attempt < tries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError(f"exhausted retries: {path}")


# ── settlement-source call-date confirmation ────────────────────────────────
# Why this exists (2026-07-29, HOOD). Kalshi listed
# KXEARNINGSMENTIONHOOD-26JUL17 with event_ticker AND sub_title both saying
# Jul 17; the call was Jul 29 at 21:00Z. `ticker_misleads()` compares exactly
# those two fields, so when they are wrong TOGETHER it agrees with itself and
# stays silent — it can catch BA (fields disagree) and can NEVER catch HOOD.
# The only field carrying the truth was settlement_sources[0].url, which this
# sweep did not read. Second Kalshi misdating in eight days ⇒ a class.
#
# `settlement_sources` already ships in the /events payload, so confirming a
# date costs one fetch of the ISSUER's page — no extra Kalshi call.
#
# ⚠ THE CRY-WOLF CONSTRAINT, and the reason this is fussier than it looks.
# Only some settlement_sources are event-specific pages. Measured 2026-07-30
# slate: AMZN and RDDT point at one event; AAPL, RACE and YUM point at generic
# IR landing pages listing MANY events. Scraping "the first date" off a landing
# page invents a disagreement and pages the operator about nothing — and per
# this file's own history (52 of 57 events flagged on the first run of
# ticker_misleads) a gate that cries wolf gets ignored, which is worse than no
# gate. So: DISPUTE ONLY ON EXACTLY ONE UNAMBIGUOUS DATE. Many dates, or none,
# ⇒ "unconfirmable" (confirmed=None), never "disputed".
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
GCAL_RE = re.compile(r"dates=(\d{8}T\d{6}Z)")
TEXT_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),\s+(20\d\d)\b")
QUARTER_RE = re.compile(r"\b(q[1-4])[-_ ]?(20\d\d)\b", re.I)

# ── clock time on an IR page ────────────────────────────────────────────────
# The gcal link carries time of day; page text carries it too, and until
# 2026-08-07 this file read the date out of that text and threw the time away.
# That is what staged 9 of 10 events across 08-06/08-07 at midnight UTC, which
# install-t0-agents.sh then turned into launchd jobs dated the PREVIOUS day.
#
# ⚠ ANCHORED, never bare. A loose `\d{1,2}(:\d{2})?\s*[ap]m` fires on financial
# tables ("229.4 Amortization"), so a match REQUIRES an am/pm token, and a bare
# hour with no minutes still requires it.
# Three guards, each earning its place against a string that really occurs:
#   (?<![\d.:])  — "229.4 Amortization" otherwise matches as "4 AM". The digit
#                  after a decimal point is not an hour.
#   (?:-|to)…    — "8:00 - 9:00 a.m. EDT" prints the meridiem ONCE, after the
#                  range. Consume the range so the hour taken is 8, not 9.
#   (?![a-z])    — "Amortization" is not "a.m."; the m must end the token.
TIME_RE = re.compile(
    r"(?<![\d.:])(\d{1,2})(?::(\d{2}))?\s*"
    r"(?:(?:-|–|to)\s*\d{1,2}(?::(?:\d{2}))?\s*)?"
    r"([ap])\.?\s*m\.?(?![a-z])", re.I)
# 24-hour "at 13:00 UTC" — only accepted WITH an explicit zone, because a bare
# "13:00" on an IR page is as often a table cell as a call time.
TIME24_RE = re.compile(r"(?<![\d.:])([01]?\d|2[0-3]):([0-5]\d)\s*\(?(UTC|GMT|Z)\)?(?![\d\w])")
# A zone may sit a few characters past the time ("9:00am - 10:00am CDT"), so it
# is searched for separately rather than glued onto the time match.
TZ_RE = re.compile(r"\b(ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|PT|PST|PDT|UTC|GMT)\b")

# Abbreviation → IANA zone. We resolve DST from the zone ON THE CALL DATE and
# never trust the abbreviation's own S/D letter: PYPL's IR page rendered
# "08:00 AM EST" for a JULY call (configs/dial-ins.toml), and believing the
# "EST" would have put us an hour late, after the prepared remarks.
_TZ = {
    "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
    "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
    "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "UTC": "UTC", "GMT": "UTC", "Z": "UTC",
}


def parse_clock(text, on_date):
    """Earliest anchored clock time in `text` → (aware UTC datetime, tz_label).

    `on_date` is the confirmed call date (a date), needed because DST is a
    property of the instant, not of the abbreviation.

    Returns (None, None) when nothing anchored is found. A missing zone yields
    tz_label "ET?" — the caller marks that an ESTIMATE rather than silently
    assuming Eastern is right.
    """
    from zoneinfo import ZoneInfo

    m24 = TIME24_RE.search(text)
    m = TIME_RE.search(text)
    # Whichever comes first wins, so a 24-hour stamp later in the line cannot
    # shadow the am/pm time that actually labels the call.
    if m24 and (not m or m24.start() < m.start()):
        hh, mm, end = int(m24.group(1)), int(m24.group(2)), m24.end()
        label = m24.group(3).upper()
    elif m:
        hh, mm, end = int(m.group(1)), int(m.group(2) or 0), m.end()
        if not 1 <= hh <= 12:            # "13:00 PM" is not a time, it is noise
            return None, None
        if hh == 12:
            hh = 0
        if m.group(3).lower() == "p":
            hh += 12
        # Zone token within a short reach of the time, uppercase only so a
        # word like "Add" (from "Add to Calendar") cannot pose as one.
        z = TZ_RE.search(text[end:end + 24])
        label = z.group(1) if z else ""
    else:
        return None, None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None, None

    est = label not in _TZ
    zone = ZoneInfo(_TZ.get(label, "America/New_York"))
    local = datetime(on_date.year, on_date.month, on_date.day, hh, mm, tzinfo=zone)
    return local.astimezone(timezone.utc), (f"{label or 'ET'}?" if est else label)


def fetch_page(url, timeout=20):
    """Fetch an arbitrary issuer page as HTML text.

    `get()` cannot do this — it hardcodes the Kalshi base and sends
    User-Agent: earshot-discover/1.0, which IR hosts bot-wall. Mirrors the
    browser-UA shape already proven in scripts/choruscall-resolve.py.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _strip_html(html):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html_mod.unescape(t))


def ir_page_dates(html):
    """Every distinct call date this page asserts, strongest signal first.

    Returns (dates, source, quote) where `dates` is a sorted list of unique
    date strings. More than one ⇒ the page is a listing, not an event page.

    The Google-calendar "Add to calendar" link is by far the best target: it is
    machine-readable, already UTC, and carries TIME OF DAY — which call_date()
    throws away entirely (it returns midnight, so the slate has no notion of a
    21:00Z call). HOOD's page carried dates=20260729T210000Z.
    """
    stamps = GCAL_RE.findall(html)
    if stamps:
        uniq = sorted({s for s in stamps})
        days = sorted({s[:8] for s in uniq})
        if len(days) == 1:
            dt = datetime.strptime(uniq[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return [dt.strftime("%Y-%m-%d")], "gcal_link", f"dates={uniq[0]}", dt, "UTC"
        return [f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in days], "gcal_link_ambiguous", "", None, None

    text = _strip_html(html)
    hits = TEXT_DATE_RE.findall(text)
    if not hits:
        return [], "none", "", None, None
    seen, quote = [], ""
    for mon, day, year in hits:
        try:
            d = datetime.strptime(f"{mon} {day} {year}", "%B %d %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        s = d.strftime("%Y-%m-%d")
        if s not in seen:
            seen.append(s)
    if len(seen) == 1:
        m = TEXT_DATE_RE.search(text)
        i = max(0, m.start() - 70)
        quote = text[i:m.end() + 70].strip()
        # The time lives in this same window — "…Earnings Call August 7, 2026
        # 8:30 AM ET…". Search from the date match forward so a clock time
        # belonging to an earlier, unrelated line cannot be picked up.
        d = datetime.strptime(seen[0], "%Y-%m-%d").date()
        dt, tzl = parse_clock(text[m.end():m.end() + 70], d)
        return seen, "page_text", quote, dt, tzl
    return sorted(seen), "page_text_ambiguous", "", None, None


def confirm_call_date(ev, d, fetch=fetch_page):
    """Cross-check the sub_title date against the issuer's own page.

    Returns the extra row fields. `fetch` is injectable so self_test() stays
    network-free.
    """
    out = {
        "settlement_url": None,
        "call_date_confirmed": None,   # True / False / None(=unconfirmable)
        "call_date_disputed": False,
        "ir_page_date": None,
        "call_time_utc": None,
        "call_date_evidence": None,
    }
    srcs = ev.get("settlement_sources") or []
    url = next((s.get("url") for s in srcs if s.get("url")), None)
    if not url:
        out["call_date_evidence"] = "no settlement_sources"
        return out
    out["settlement_url"] = url

    q = QUARTER_RE.search(url)
    slug_q = f"{q.group(1).upper()}-{q.group(2)}" if q else None

    try:
        html = fetch(url)
    except Exception as e:                                  # noqa: BLE001
        out["call_date_evidence"] = f"fetch failed: {type(e).__name__}: {e}"
        return out

    dates, source, quote, dt, tz_label = ir_page_dates(html)
    if not dates or source.endswith("ambiguous"):
        out["call_date_evidence"] = (
            f"unconfirmable ({source}"
            + (f", {len(dates)} dates on page" if dates else "")
            + (f", slug says {slug_q}" if slug_q else "") + ")")
        return out

    page_date = dates[0]
    out["ir_page_date"] = page_date
    ours = d.strftime("%Y-%m-%d") if d else None
    agrees = (page_date == ours)
    slug_note = f" | slug {slug_q}" if slug_q else ""

    # A gcal stamp is machine-readable and authoritative, so it sets the time
    # even while DISPUTING the date — that is the HOOD case, and the whole
    # reason the dispute path exists.
    #
    # A page_text time may only be trusted when the page is describing OUR
    # call. URBN's page carries "5:00 PM EDT" against a 2026-05-20 page date
    # and a 2026-08-26 Kalshi date: a real time, belonging to a different
    # event. Taking it would schedule the arm confidently and wrongly, which
    # is worse than leaving it null and paging.
    if dt is not None and (source == "gcal_link" or agrees):
        out["call_time_utc"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        out["call_time_source"] = source
        # "ET?" means no zone was printed and Eastern was assumed. Carried so
        # slate-time-merge.py can shout rather than silently accept a guess.
        if tz_label:
            out["call_time_tz"] = tz_label
            if tz_label.endswith("?"):
                out["call_time_estimated"] = True

    # ⚠ ONLY A GCAL LINK MAY DISPUTE. Measured on the live 2026-07-29 slate:
    #   gcal_link disputes  1/1 correct  (HOOD — the real misdating)
    #   page_text disputes  0/3 correct  (META, AMD, NBIS — all false)
    # The page_text failures are not bad luck, they are systematic: a
    # settlement_source is often a PRESS RELEASE ANNOUNCING the call, and the
    # single date on such a page is the PR's OWN publication date, not the call
    # ("Meta to Announce Second Quarter 2026 Results — July 14, 2026" against a
    # July 29 call). "Exactly one date on the page" was not a strong enough
    # rule, because a PR page has exactly one date and it is the wrong one.
    #
    # A gcal link cannot make that mistake: it exists to put THE EVENT on a
    # calendar, so its timestamp is the event's by construction — and it is the
    # only signal that also carries time-of-day.
    #
    # Agreement is still worth recording from page_text: two independent
    # sources landing on the same date is real corroboration and costs nothing
    # if coincidental. Disagreement from page_text is simply not evidence.
    if agrees:
        out["call_date_confirmed"] = True
        out["call_date_disputed"] = False
        out["call_date_evidence"] = f"{source} agrees: {quote or page_date}{slug_note}"
    elif source == "gcal_link":
        out["call_date_confirmed"] = False
        out["call_date_disputed"] = True
        out["call_date_evidence"] = f"{source}: {quote or page_date}{slug_note}"
    else:
        # Disagreement we refuse to escalate. Recorded, never paged.
        out["call_date_confirmed"] = None
        out["call_date_disputed"] = False
        out["call_date_evidence"] = (
            f"page date {page_date} != Kalshi {ours}, NOT disputed — {source} "
            f"cannot distinguish a call date from a press-release date; only a "
            f"gcal link may dispute. quote: {quote or page_date}{slug_note}")
    return out


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


def stale_open_event(call_date_s, today_s, daily):
    """Is this an OPEN event whose call date has already passed?

    That combination is self-refuting, and it is free to detect: both sweeps
    query `status=open`, so every event in the union is open by construction.
    An open event whose sub_title date is in the past means the sub_title is
    STALE, not that the event is over — Kalshi reuses one ticker per series for
    the standing "next earnings call" and does not re-date it.

    PGR 2026-08-04 is the case: `KXEARNINGSMENTIONPGR-26JUL15`, sub_title
    "On Jul 15, 2026", 12 markets open, trades landing 14:33–14:54Z on Aug 4
    while we recorded that very call to WAV and armed nothing.

    Deliberately NOT a date source. This says "the date we have is not to be
    trusted", never "the date is X" — same discipline as `ticker_misleads()`.
    The operator (or the qualify lane) re-derives the real date from the
    issuer's page. False positives cost one look at a worklist row; the false
    NEGATIVE cost a whole liquid board.

    DAILY_SERIES are excluded: their events are per-session and a past one is
    genuinely past, not rolled forward.
    """
    if daily is not None or not call_date_s:
        return False
    return call_date_s < today_s


def call_date(ev):
    """Call date from sub_title ('On Jul 28, 2026') — NEVER from the ticker."""
    sub = (ev.get("sub_title") or "").replace("On ", "").strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(sub, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def daily_air_date(ev):
    """Air date+time for a DAILY_SERIES event, from its ticker suffix.

    ONLY for series listed in DAILY_SERIES, whose sub_title carries no date by
    design. Returns None for everything else — call_date() stays the sole
    date path for one-off events, and this is never a fallback for a
    *missing* sub_title date on a series we haven't opted in.
    """
    spec = DAILY_SERIES.get(ev.get("series_ticker") or "")
    if not spec or "-" not in (ev.get("event_ticker") or ""):
        return None
    suffix = ev["event_ticker"].rsplit("-", 1)[1].upper()
    hh, mm = spec["air_utc"].split(":")
    try:
        return datetime.strptime(suffix, "%y%b%d").replace(
            hour=int(hh), minute=int(mm), tzinfo=timezone.utc)
    except ValueError:
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

    # ── DAILY_SERIES: the dateless recurring broadcast (2026-07-29, WNT) ─────
    wnt = {"series_ticker": "KXWORLDNEWSMENTION",
           "event_ticker": "KXWORLDNEWSMENTION-26JUL29",
           "sub_title": "any host or reporter - ABC World News Tonight with David Muir"}
    assert call_date(wnt) is None, \
        "WNT sub_title carries no date — call_date must still refuse it"
    air = daily_air_date(wnt)
    assert air is not None and air.strftime("%Y-%m-%d %H:%M") == "2026-07-29 22:30", \
        f"WNT airdate from ticker suffix broke: {air}"
    assert issuer_of("KXWORLDNEWSMENTION") == "WORLDNEWS", \
        f"WNT issuer label broke: {issuer_of('KXWORLDNEWSMENTION')}"
    # the opt-in must be narrow: an unlisted series never gets a ticker date,
    # which is the whole HOOD lesson (commit 6be80aa).
    assert daily_air_date({"series_ticker": "KXEARNINGSMENTIONHOOD",
                           "event_ticker": "KXEARNINGSMENTIONHOOD-26JUL17",
                           "sub_title": "On Jul 17, 2026"}) is None, \
        "date-from-ticker leaked to a non-DAILY_SERIES event"
    assert daily_air_date({"series_ticker": "KXWORLDNEWSMENTION",
                           "event_ticker": "KXWORLDNEWSMENTION"}) is None, \
        "daily_air_date must refuse an event_ticker with no date suffix"
    assert call_date({"sub_title": "garbage"}) is None, \
        "unparsable sub_title must return None, not raise"

    # ── STALE sub_title on an OPEN event (2026-08-04, PGR) ──────────────────
    # Frozen from the live payload: KXEARNINGSMENTIONPGR-26JUL15 was returned by
    # the status=open sweep on Aug 4 carrying a Jul 15 sub_title, while its 12
    # markets traded 14:33–14:54Z through Progressive's Aug 4 call. The whole
    # event was absent from slate/worklist.md because `today <= call_date` is
    # false for it. This assertion is that miss, frozen.
    pgr = {"series_ticker": "KXEARNINGSMENTIONPGR",
           "event_ticker": "KXEARNINGSMENTIONPGR-26JUL15",
           "sub_title": "On Jul 15, 2026"}
    pgr_d = call_date(pgr)
    assert pgr_d is not None and pgr_d.strftime("%Y-%m-%d") == "2026-07-15", \
        f"PGR sub_title parse broke: {pgr_d}"
    assert stale_open_event("2026-07-15", "2026-08-04", None), \
        "PGR STALE-DATE detection broke — an open event with a passed call " \
        "date must flag (this is the 2026-08-04 miss)"
    # ticker_misleads CANNOT catch this one: suffix 26JUL15 agrees with the
    # (stale) Jul 15 sub_title, exactly the HOOD wrong-together shape. That
    # agreement is why a separate detector had to exist.
    assert not ticker_misleads("KXEARNINGSMENTIONPGR-26JUL15", pgr_d), \
        "PGR fixture assumption broke: ticker and stale sub_title must AGREE"
    # …and it must not cry wolf on the ordinary cases.
    assert not stale_open_event("2026-08-04", "2026-08-04", None), \
        "false positive: a call TODAY is not stale"
    assert not stale_open_event("2026-08-20", "2026-08-04", None), \
        "false positive: a future call is not stale"
    assert not stale_open_event(None, "2026-08-04", None), \
        "an unresolved date is UNRESOLVED, not stale"
    assert not stale_open_event("2026-07-29", "2026-08-04", air), \
        "DAILY_SERIES past events are genuinely past — must never flag"
    assert issuer_of("KXFEDMENTION") == "FED", \
        "bare KXFEDMENTION series must label as FED, not empty string"
    assert issuer_of("KXEARNINGSMENTIONF") == "F", "earnings issuer broke"

    # ── HOOD fixtures: the settlement-source cross-check. Network-free — the
    # fetcher is injected. Shapes are copied from the real 2026-07-29 pages.
    hood_page = """
      <div class="lfg-details">
        Robinhood Q2 2026 Earnings <span class="lfg_time">July 29, 2026 5:00 PM EDT</span>
        <a href="/node/16116/ics">Add to Outlook</a>
        <a href="https://www.google.com/calendar/render?action=TEMPLATE&amp;text=Robinhood
           &amp;dates=20260729T210000Z/20260729T210000Z&amp;details=Event">Add to Google Calendar</a>
        <a href="https://www.youtube.com/live/PpJSU4M87nw">Livestream</a>
      </div>"""
    hood_ev = {"sub_title": "On Jul 17, 2026", "settlement_sources": [
        {"name": "Robinhood",
         "url": "https://investors.robinhood.com/events/event-details/robinhood-q2-2026-earnings"}]}
    r = confirm_call_date(hood_ev, call_date(hood_ev), fetch=lambda u: hood_page)
    assert r["call_date_disputed"] is True, f"HOOD must be DISPUTED, got {r}"
    assert r["call_date_confirmed"] is False, f"HOOD confirmed must be False, got {r}"
    assert r["ir_page_date"] == "2026-07-29", f"HOOD real date wrong: {r['ir_page_date']}"
    # The gcal link is the only signal that recovers TIME OF DAY; call_date()
    # alone yields midnight and the runsheet needs T-20.
    assert r["call_time_utc"] == "2026-07-29T21:00:00Z", f"HOOD time wrong: {r}"

    # ── clock recovery from PAGE TEXT (2026-08-07) ──────────────────────────
    # Until this landed, only the gcal branch returned a time; page_text
    # returned None even though the ±70-char quote it hands back contains the
    # clock. 25 of 30 slate events were untimed, so slate-stage.sh staged them
    # at midnight UTC and install-t0-agents.sh wrote launchd jobs dated the
    # PREVIOUS day: runs=0, never fires. That is how DKNG/LLY/VST/WEN were all
    # lost on 08-07 and all 5 of the 08-06 slate before them.
    #
    # Strings are verbatim from slate/latest-timed.json's call_date_evidence.
    import datetime as _dt
    for name, txt, on, want, want_tz in [
        ("DKNG, no zone printed",
         "Earnings Call August 7, 2026 8:30 AM Add to Calendar",
         _dt.date(2026, 8, 7), "2026-08-07T12:30:00Z", "ET?"),
        ("VST, range with the zone after it",
         "Q2 2026 Results Call Friday, August 7, 2026 9:00am - 10:00am CDT",
         _dt.date(2026, 8, 7), "2026-08-07T14:00:00Z", "CDT"),
        ("WEN",
         "Earnings Conference Call August 7, 2026 8:30 AM ET",
         _dt.date(2026, 8, 7), "2026-08-07T12:30:00Z", "ET"),
        ("HIMS — corroborates the hand-typed 21:00Z",
         "Second Quarter 2026 Earnings Call August 10, 2026 5:00 PM ET",
         _dt.date(2026, 8, 10), "2026-08-10T21:00:00Z", "ET"),
        ("TGT, meridiem printed once AFTER the range",
         "Earnings Conference Call Wednesday, August 19, 2026 8:00 - 9:00 a.m. EDT",
         _dt.date(2026, 8, 19), "2026-08-19T12:00:00Z", "EDT"),
        ("NVDA, Pacific",
         "earnings call August 26, 2026 02:00 PM PT",
         _dt.date(2026, 8, 26), "2026-08-26T21:00:00Z", "PT"),
        ("TTWO, 24-hour with explicit UTC",
         "Take-Two earnings August 7, 2026 at 12:00 PM UTC",
         _dt.date(2026, 8, 7), "2026-08-07T12:00:00Z", "UTC"),
        # THE PYPL TRAP (configs/dial-ins.toml): the page says EST for a JULY
        # call. July is EDT. Believing the abbreviation puts the arm an hour
        # late, after the prepared remarks. DST is resolved from the DATE.
        ("PYPL, 'EST' printed in July",
         "PayPal call July 28, 2026 08:00 AM EST",
         _dt.date(2026, 7, 28), "2026-07-28T12:00:00Z", "EST"),
    ]:
        got, tzl = parse_clock(txt, on)
        assert got and got.strftime("%Y-%m-%dT%H:%M:%SZ") == want, \
            f"parse_clock({name}) = {got}, want {want}"
        assert tzl == want_tz, f"parse_clock({name}) tz = {tzl}, want {want_tz}"

    # Noise must yield NOTHING. A loose matcher reads "229.4 Amortization" as
    # "4 AM" and schedules the arm at 08:00Z with total confidence.
    for name, txt in [
        ("a financial table", "229.4 Amortization 412.8 333.8 Total"),
        ("a bare decimal", "Revenue rose 8.30 in the quarter"),
        ("a date with no clock", "Earnings Call August 7, 2026 Add to Calendar"),
    ]:
        got, _ = parse_clock(txt, _dt.date(2026, 8, 7))
        assert got is None, f"parse_clock({name}) invented {got}"

    # A page_text time is only trustworthy when the page is describing OUR
    # call. URBN's page carries a real 5:00 PM EDT against a May page date and
    # an August Kalshi date — a correct time for a DIFFERENT event.
    urbn_page = ("<div>Urban Outfitters Q1 2027 Earnings Call "
                 "May 20, 2026 5:00 PM EDT</div>")
    urbn_ev = {"sub_title": "On Aug 26, 2026", "settlement_sources": [
        {"name": "URBN", "url": "https://investor.urbn.com/events"}]}
    r = confirm_call_date(urbn_ev, call_date(urbn_ev), fetch=lambda u: urbn_page)
    assert r.get("call_time_utc") is None, \
        f"a time from a DISAGREEING page must be refused, got {r.get('call_time_utc')}"

    # A generic IR LANDING page lists many events. It must degrade to
    # "unconfirmable", NEVER to "disputed" — that is the cry-wolf guard, and the
    # measured majority case (AAPL/RACE/YUM on the 2026-07-30 slate).
    landing = """<ul>
        <li>Q2 2026 Earnings Call January 15, 2026</li>
        <li>Annual Meeting March 25, 2026</li>
        <li>Q3 2026 Earnings Call July 30, 2026</li></ul>"""
    land_ev = {"sub_title": "On Jul 30, 2026", "settlement_sources": [
        {"url": "https://investor.apple.com/investor-relations/default.aspx"}]}
    r2 = confirm_call_date(land_ev, call_date(land_ev), fetch=lambda u: landing)
    assert r2["call_date_disputed"] is False, f"landing page must NOT dispute: {r2}"
    assert r2["call_date_confirmed"] is None, f"landing page must be unconfirmable: {r2}"

    # An event page that AGREES confirms positively.
    ok_page = ('<a href="...&amp;dates=20260730T203000Z/20260730T213000Z">Add to Google Calendar</a>')
    ok_ev = {"sub_title": "On Jul 30, 2026", "settlement_sources": [
        {"url": "https://ir.aboutamazon.com/events/event-details/2026/Q2-2026/default.aspx"}]}
    r3 = confirm_call_date(ok_ev, call_date(ok_ev), fetch=lambda u: ok_page)
    assert r3["call_date_confirmed"] is True and not r3["call_date_disputed"], \
        f"agreeing event page must confirm: {r3}"

    # A PRESS-RELEASE settlement_source: one date on the page, and it is the
    # PR's own publication date, not the call. This shape produced 3 of 4
    # disputes on the first live run (META, AMD, NBIS) and every one was wrong.
    # It must record the disagreement and REFUSE to escalate it.
    pr_page = ("<html><body>Meta to Announce Second Quarter 2026 Results "
               "July 14, 2026 Download this Press Release MENLO PARK</body></html>")
    pr_ev = {"sub_title": "On Jul 29, 2026", "settlement_sources": [
        {"url": "https://investor.atmeta.com/investor-news/press-release-details/2026/x"}]}
    r6 = confirm_call_date(pr_ev, call_date(pr_ev), fetch=lambda u: pr_page)
    assert r6["call_date_disputed"] is False, \
        f"a press-release date must NOT dispute (the META/AMD false-positive class): {r6}"
    assert r6["call_date_confirmed"] is None, f"PR-page disagreement must be unconfirmable: {r6}"
    assert "only a gcal link may dispute" in (r6["call_date_evidence"] or ""), f"{r6}"
    # ...but page_text AGREEING is still recorded as corroboration.
    agree_page = "<html>Earnings call July 30, 2026 at 5:00 PM ET</html>"
    agree_ev = {"sub_title": "On Jul 30, 2026",
                "settlement_sources": [{"url": "https://x.com/events/e"}]}
    r7 = confirm_call_date(agree_ev, call_date(agree_ev), fetch=lambda u: agree_page)
    assert r7["call_date_confirmed"] is True and not r7["call_date_disputed"], \
        f"page_text agreement should confirm: {r7}"

    # No settlement_sources, and a fetch that raises, are both unconfirmable —
    # never disputed, and never an exception out of the sweep.
    r4 = confirm_call_date({"sub_title": "On Jul 30, 2026"}, call_date(ok_ev))
    assert r4["call_date_confirmed"] is None and not r4["call_date_disputed"], f"{r4}"

    def boom(_u):
        raise urllib.error.HTTPError("u", 403, "Forbidden", None, None)
    r5 = confirm_call_date(ok_ev, call_date(ok_ev), fetch=boom)
    assert r5["call_date_confirmed"] is None and not r5["call_date_disputed"], f"{r5}"
    assert "fetch failed" in (r5["call_date_evidence"] or ""), f"{r5}"

    # A transient resolver failure must not abort the daily census on its first
    # request. Keep this offline by replacing urlopen and sleep.
    attempts = []
    original_urlopen, original_sleep = urllib.request.urlopen, time.sleep
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_args): self.close()
    def flaky(_request, timeout=0):
        attempts.append(timeout)
        if len(attempts) < 3:
            raise urllib.error.URLError("temporary DNS failure")
        return Response(b'{"ok": true}')
    try:
        urllib.request.urlopen = flaky
        time.sleep = lambda _seconds: None
        assert get("offline-retry", tries=4, pace=0) == {"ok": True}
        assert len(attempts) == 3, attempts
    finally:
        urllib.request.urlopen, time.sleep = original_urlopen, original_sleep

    print("self-test: OK (BA fixture — date-from_sub_title, misleading-ticker "
          "detection, cry-wolf guard, FED issuer; HOOD fixture — "
          "settlement-source dispute, time-of-day recovery, landing-page "
          "no-cry-wolf, agreeing-page confirm, fetch-failure tolerance; "
          "PGR fixture — STALE-DATE open-board detection, ticker/sub_title "
          "agreeing-but-both-stale, 4 cry-wolf guards)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write the slate here (e.g. slate/latest.json)")
    ap.add_argument("--days", type=int, default=21, help="horizon in days (default 21)")
    ap.add_argument("--self-test", action="store_true",
                    help="offline frozen-fixture regression; no network")
    ap.add_argument("--assert-ba", action="store_true",
                    help="regression: fail unless the per-series sweep finds BA")
    ap.add_argument("--no-ir-check", action="store_true",
                    help="skip the settlement_sources call-date confirmation "
                         "(no issuer-page fetches; Kalshi metadata taken at face value)")
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
    today_s = now.strftime("%Y-%m-%d")
    horizon_s = horizon.strftime("%Y-%m-%d")
    rows = []
    for tk, ev in union.items():
        d = call_date(ev)
        src = "sub_title" if d else "UNRESOLVED"
        daily = None
        if d is None:
            daily = daily_air_date(ev)
            if daily is not None:
                d, src = daily, "ticker_daily"
        row = {
            "event_ticker": tk,
            "series_ticker": ev.get("series_ticker"),
            "issuer": issuer_of(ev.get("series_ticker") or ""),
            "sub_title": ev.get("sub_title"),
            "call_date": d.strftime("%Y-%m-%d") if d else None,
            "call_date_source": src,
            "found_by": ("both" if tk in pag and tk in per
                         else "per_series_only" if tk in per else "paginated_only"),
            # None, not False: for a ticker_daily date the check compares the
            # ticker against itself and can only ever agree. Reporting False
            # would claim "checked and clean" — the HOOD failure mode.
            "ticker_date_misleading": None if daily else ticker_misleads(tk, d),
            # Open + past = the sub_title is stale (PGR 2026-08-04). Recorded
            # as its own field, never folded into call_date_disputed: that one
            # means "the ISSUER's page contradicts Kalshi", which needs a fetch.
            # This needs nothing but the date we already have.
            "stale_open_event": stale_open_event(
                d.strftime("%Y-%m-%d") if d else None, today_s, daily),
        }
        if daily is not None:
            row["air_time_utc"] = daily.strftime("%H:%MZ")
            row["daily_label"] = DAILY_SERIES[ev["series_ticker"]]["label"]

        # Confirm against the issuer's own page — but only for events worth a
        # fetch. The union is ~56 events; confirming all of them means 56
        # requests to 56 different issuer domains every night, which is both
        # rude and slow. Two classes qualify:
        #   * inside the horizon (what the operator will actually act on), and
        #   * UNRESOLVED dates (no sub_title date at all — always worth a look).
        # HOOD is the reason the horizon test alone is not enough: its bogus
        # 2026-07-17 put it OUTSIDE the horizon, so a naive horizon gate would
        # skip exactly the event this check exists to catch. Widen the window
        # backwards by `days` so a misdated-into-the-past event still gets
        # confirmed.
        looks_recent = bool(row["call_date"]) and (
            (now - timedelta(days=a.days)).strftime("%Y-%m-%d") <= row["call_date"] <= horizon_s)
        # DAILY_SERIES are excluded from the IR cross-check on purpose. Their
        # settlement_sources are 14 news-org front doors (abcnews.go.com,
        # reuters.com, …) — generic landing pages, so ir_page_dates() can only
        # ever return "ambiguous"/"unconfirmable". Fetching them nightly buys
        # nothing and spends a request per session on a page that geo-blocks
        # this box anyway. The airdate is already ticker-derived and tagged.
        # A stale_open_event ALWAYS earns the fetch, even if its bogus date puts
        # it outside the widened window: re-deriving the real date from the
        # issuer is the only way it becomes actionable. Same reasoning that
        # widened the window backwards for HOOD, one step further.
        if (not a.no_ir_check and daily is None
                and (looks_recent or row["call_date"] is None
                     or row["stale_open_event"])):
            row.update(confirm_call_date(ev, d))
        rows.append(row)
    rows.sort(key=lambda r: (r["call_date"] or "9999", r["issuer"]))
    upcoming = [r for r in rows
                if r["call_date"] and now.strftime("%Y-%m-%d") <= r["call_date"] <= horizon.strftime("%Y-%m-%d")]

    # DISPUTED events are printed OUTSIDE the horizon filter on purpose. HOOD's
    # bogus 2026-07-17 put it outside every `today <= call_date` test, which is
    # exactly how a tradeable event stayed invisible while sitting in the JSON
    # the whole time. A dispute must never be hidden behind the date it disputes.
    disputed = [r for r in rows if r.get("call_date_disputed")]
    if disputed:
        print(f"\n{'='*72}\n⛔ CALL DATE DISPUTED — Kalshi metadata contradicts the "
              f"issuer ({len(disputed)})\n{'='*72}")
        for r in disputed:
            print(f"  {r['issuer']:<7} {r['event_ticker']:<34}")
            print(f"      Kalshi says : {r['call_date']}  (sub_title {r['sub_title']!r})")
            print(f"      issuer says : {r['ir_page_date']}"
                  + (f"  {r['call_time_utc']}" if r.get("call_time_utc") else ""))
            print(f"      evidence    : {(r.get('call_date_evidence') or '')[:150]}")
            print(f"      source      : {r.get('settlement_url')}")
        print("  ⇒ TRUST THE ISSUER. Stage against the issuer date, not the slate date.")

    # STALE-DATE events are printed OUTSIDE the horizon filter for the same
    # reason DISPUTED ones are, and it is not a hypothetical: PGR 2026-08-04 sat
    # in this JSON with call_date 2026-07-15 while its board traded through the
    # call. `today <= call_date` can only ever hide it. An open event with a past
    # date is a live event with a stale label until an issuer page says otherwise.
    stale = [r for r in rows if r.get("stale_open_event")]
    if stale:
        print(f"\n{'='*72}\n⚠ STALE CALL DATE — event is OPEN but its date has "
              f"passed ({len(stale)})\n{'='*72}")
        for r in stale:
            print(f"  {r['issuer']:<7} {r['event_ticker']:<34}")
            print(f"      Kalshi says : {r['call_date']}  (sub_title {r['sub_title']!r})")
            print(f"      issuer says : {r.get('ir_page_date') or '— unconfirmed —'}"
                  + (f"  {r['call_time_utc']}" if r.get("call_time_utc") else ""))
            print(f"      source      : {r.get('settlement_url')}")
        print("  ⇒ The board is OPEN, so the call has NOT happened yet or has just")
        print("    happened. Re-derive the date from the issuer before assuming it is over.")
        print("    PGR 2026-08-04: this exact shape hid a 12-market board that traded")
        print("    through the call while we recorded it and armed nothing.")

    print(f"\n{'='*72}\nSLATE — next {a.days} days ({len(upcoming)} events)\n{'='*72}")
    for r in upcoming:
        flag = "  ⚠ PER-SERIES ONLY" if r["found_by"] == "per_series_only" else ""
        mis = "  ⚠ ticker date != call date" if r["ticker_date_misleading"] else ""
        conf = {True: "", False: "  ⛔ DATE DISPUTED", None: "  (date unconfirmed)"}[
            r.get("call_date_confirmed")]
        t = f"  {r['call_time_utc'][11:16]}Z" if r.get("call_time_utc") else ""
        print(f"  {r['call_date']}{t}  {r['issuer']:<7} {r['event_ticker']:<34}{flag}{mis}{conf}")

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
        destination = os.path.abspath(a.json)
        directory = os.path.dirname(destination) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".slate.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(slate, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, destination)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
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
