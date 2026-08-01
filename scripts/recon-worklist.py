#!/usr/bin/env python3
"""slate ∩ dial-ins registry → the recon worklist.

The registry (configs/dial-ins.toml) is the durable record of per-issuer
venue/access recon. PYPL and F were both researched exhaustively the night
before their own events and neither was recorded — the recon leak. This
script makes the gap VISIBLE on a schedule instead of discovered at T−1:
for every slate event inside --days, flag

  NO-ENTRY   issuer absent from the registry entirely
  STALE(Nd)  last_verified older than --stale-days (tiers decay: Ford
             published a dial-in in earlier quarters, then moved to a
             login-gated webcast — see the [F] note)
  UNKNOWN    tier or listen_access recorded as "unknown"

It also folds in what `control.hunt` has already learned, so a row says where
to start rather than just that it is empty:

  HUNT-FOUND  the hunt found a published bridge for this issuer — a number is
              already on file in state/hunt/, so this row is a probe away from
              a phone lane rather than a research project
  HUNT-BLIND  the hunt COULD NOT ANSWER (no 8-K matched, or no CIK — foreign
              private issuers file 6-K). This is not the same as "no bridge",
              and collapsing the two is how a whole venue class stays invisible

The "start from" column falls back to the hunt's filing URL when the registry
has no provenance of its own. Before this, 27 rows carried a blank cell while
hunt-status.json held verdicts for 32 issuers.

Output: a prioritized markdown table (soonest call first) at --out, and
ESCALATE: lines on stdout for NO-ENTRY events ≤4 days out — slate-daily.sh
folds those into its alert. Exit 0 always unless the inputs are unreadable.

Self-test: --check runs embedded fixtures (mine-dial-in.py convention).
Usage:
  recon-worklist.py --slate slate/latest.json --registry configs/dial-ins.toml \
      [--hunt slate/hunt-status.json] [--days 10] [--stale-days 45] \
      [--out slate/worklist.md]
"""
import argparse, datetime, json, sys, tomllib
from pathlib import Path

ESCALATE_DAYS = 4


def build(slate, registry, days, stale_days, today, hunt=None):
    horizon = today + datetime.timedelta(days=days)
    hunt = hunt or {}
    rows = []
    for ev in slate.get("events", []):
        cd = ev.get("call_date")
        # ⚠ A DISPUTED event must be filtered on the date we BELIEVE, not the
        # date we distrust. HOOD (2026-07-29) is the whole reason: Kalshi said
        # 2026-07-17 for a call that was actually 2026-07-29, so the horizon
        # test below silently dropped the one event most needing attention.
        # Filtering on the disputed date would make the DATE-DISPUTED flag
        # inert — it would flag events it had already thrown away.
        disputed = bool(ev.get("call_date_disputed"))
        eff = ev.get("ir_page_date") if disputed else cd
        if not eff:
            continue
        d = datetime.date.fromisoformat(eff)
        if not (today <= d <= horizon):
            continue
        issuer = ev.get("issuer") or ""
        entry = registry.get(issuer)
        entry = entry if isinstance(entry, dict) else None
        flags = []
        if disputed:
            # Kalshi's metadata contradicts the issuer's own page. Staging
            # against the slate date would arm on the wrong day.
            flags.append(f"DATE-DISPUTED(kalshi={cd})")
        if entry is None:
            flags.append("NO-ENTRY")
        else:
            lv = str(entry.get("last_verified") or "")
            if not lv:
                flags.append("STALE(never verified)")
            else:
                try:
                    age = (today - datetime.date.fromisoformat(lv)).days
                    if age > stale_days:
                        flags.append(f"STALE({age}d)")
                except ValueError:
                    flags.append("STALE(unparsable last_verified)")
            if (entry.get("tier", "unknown") == "unknown"
                    or entry.get("listen_access", "unknown") == "unknown"):
                flags.append("UNKNOWN")

        # What the hunt already knows about this issuer. Only ever ADDS
        # information: the registry stays the authority on access, because it
        # is the only one of the two that carries probe history.
        h = hunt.get(issuer)
        h = h if isinstance(h, dict) else None
        if h is not None:
            if h.get("found"):
                flags.append("HUNT-FOUND")
            elif h.get("tier") == "unknown":
                # "We could not look here", not "there is nothing here". The
                # 6-K/foreign-filer blind spot lives in this branch.
                flags.append("HUNT-BLIND")

        if flags:
            rows.append({
                "call_date": eff,
                "days_out": (d - today).days,
                "issuer": issuer,
                "event_ticker": ev.get("event_ticker", ""),
                "flags": flags,
                # Registry provenance wins; the hunt's filing URL is the
                # fallback that turns an empty cell into a place to start.
                "src": (entry or {}).get("number_src")
                       or (entry or {}).get("evidence_src")
                       or (h or {}).get("number_src") or "",
            })
    rows.sort(key=lambda r: (r["call_date"], r["issuer"]))
    return rows


def render(rows, days, stale_days):
    # No date in the header, deliberately: the file must only differ when its
    # CONTENT differs, or slate-daily.sh would commit a no-op every day.
    lines = [
        f"# Recon worklist (horizon {days}d, staleness bar {stale_days}d; "
        "regenerated daily by slate-daily.sh)",
        "",
        "Work each row BEFORE its event's T−1 night: resolve the venue, record",
        "it in `configs/dial-ins.toml` (tier / listen_access / evidence /",
        "last_verified). A row disappears from this list only when the",
        "registry says so — that is the point.",
        "",
    ]
    if not rows:
        lines.append("**Nothing flagged — every event in the horizon has a "
                     "fresh registry entry.**")
    else:
        lines += ["| call date | days out | issuer | event | flags | start from |",
                  "|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r['call_date']} | {r['days_out']} | {r['issuer']} "
                         f"| {r['event_ticker']} | {', '.join(r['flags'])} "
                         f"| {r['src']} |")
    return "\n".join(lines) + "\n"


def check():
    today = datetime.date(2026, 7, 28)
    slate = {"events": [
        # NO-ENTRY, 1 day out → row + ESCALATE
        {"issuer": "AMD", "event_ticker": "KXEARNINGSMENTIONAMD-26JUL29",
         "call_date": "2026-07-29"},
        # fresh entry, no flags → absent
        {"issuer": "F", "event_ticker": "KXEARNINGSMENTIONF-26JUL28",
         "call_date": "2026-07-28"},
        # stale entry → STALE
        {"issuer": "WMT", "event_ticker": "KXEARNINGSMENTIONWMT-26AUG05",
         "call_date": "2026-08-05"},
        # unknown tier → UNKNOWN
        {"issuer": "DIS", "event_ticker": "KXEARNINGSMENTIONDIS-26AUG06",
         "call_date": "2026-08-06"},
        # outside horizon → absent
        {"issuer": "TGT", "event_ticker": "KXEARNINGSMENTIONTGT-26SEP01",
         "call_date": "2026-09-01"},
        # THE HOOD CASE (2026-07-29): Kalshi dates it into the PAST, so the
        # horizon test would drop it — yet the issuer says it is tomorrow. It
        # MUST appear, keyed on the issuer's date, with a fresh registry entry
        # (i.e. flagged for the dispute ALONE, nothing else).
        {"issuer": "HOOD", "event_ticker": "KXEARNINGSMENTIONHOOD-26JUL17",
         "call_date": "2026-07-17", "call_date_disputed": True,
         "ir_page_date": "2026-07-29"},
    ]}
    registry = {
        "F": {"tier": "bridge_unpublished", "listen_access": "no",
              "last_verified": "2026-07-28"},
        "WMT": {"tier": "published", "listen_access": "yes",
                "last_verified": "2026-05-01",
                "number_src": "https://ir.walmart.example/q1"},
        "DIS": {"tier": "unknown", "listen_access": "unknown",
                "last_verified": "2026-07-27"},
        "HOOD": {"tier": "none", "listen_access": "no",
                 "last_verified": "2026-07-29"},
    }
    hunt = {
        # A bridge is already on file — this row is a probe away, not research.
        "AMD": {"found": True, "tier": "published",
                "number_src": "https://www.sec.gov/Archives/edgar/data/2488/amd.htm"},
        # The hunt could not answer. NOT the same as "no bridge exists".
        "WMT": {"found": False, "tier": "unknown"},
        # A real negative in the scanned window — adds no flag.
        "DIS": {"found": False, "tier": "bridge_unpublished"},
    }
    rows = build(slate, registry, days=10, stale_days=45, today=today, hunt=hunt)
    got = {r["issuer"]: r["flags"] for r in rows}
    assert set(got) == {"AMD", "WMT", "DIS", "HOOD"}, f"wrong issuers flagged: {got}"
    assert got["AMD"] == ["NO-ENTRY", "HUNT-FOUND"], got
    assert got["WMT"] == ["STALE(88d)", "HUNT-BLIND"], got
    assert got["DIS"] == ["UNKNOWN"], \
        f"a bridge_unpublished hunt verdict must add no flag: {got}"
    # The empty "start from" cell is the thing this wiring exists to fill.
    amd = next(r for r in rows if r["issuer"] == "AMD")
    assert amd["src"] == hunt["AMD"]["number_src"], \
        f"hunt provenance must fill an empty start-from cell: {amd}"
    # ...but never override provenance the registry already has.
    wmt = next(r for r in rows if r["issuer"] == "WMT")
    assert wmt["src"] == "https://ir.walmart.example/q1", wmt
    # No hunt data at all must behave exactly as before.
    assert {r["issuer"]: r["flags"] for r in
            build(slate, registry, 10, 45, today)}["AMD"] == ["NO-ENTRY"]
    # Surfaced despite a Kalshi date in the past, keyed on the ISSUER's date.
    assert got["HOOD"] == ["DATE-DISPUTED(kalshi=2026-07-17)"], got
    hood = next(r for r in rows if r["issuer"] == "HOOD")
    assert hood["call_date"] == "2026-07-29", \
        f"disputed row must carry the BELIEVED date, got {hood['call_date']}"
    assert hood["days_out"] == 1, f"days_out must count from the believed date: {hood}"
    esc = [r for r in rows if "NO-ENTRY" in r["flags"]
           and r["days_out"] <= ESCALATE_DAYS]
    assert [r["issuer"] for r in esc] == ["AMD"], esc
    print("recon-worklist --check: OK (3 registry flag classes + 2 hunt flag "
          "classes + start-from fallback + escalation + horizon cut)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slate")
    ap.add_argument("--registry")
    ap.add_argument("--hunt", default="slate/hunt-status.json",
                    help="control.bridge.candidates status file; missing is fine")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--stale-days", type=int, default=45)
    ap.add_argument("--out")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        check()
        return 0
    if not (a.slate and a.registry):
        ap.error("--slate and --registry required (or --check)")

    slate = json.load(open(a.slate))
    registry = tomllib.load(open(a.registry, "rb"))
    # A missing or malformed hunt status degrades to the pre-hunt behaviour
    # rather than failing the nightly job — this is an enrichment, not an input.
    try:
        hunt = json.load(open(a.hunt)).get("issuers", {})
    except (OSError, json.JSONDecodeError):
        hunt = {}
    today = datetime.date.today()
    rows = build(slate, registry, a.days, a.stale_days, today, hunt=hunt)
    md = render(rows, a.days, a.stale_days)
    if a.out:
        Path(a.out).write_text(md)
    else:
        print(md)

    for r in rows:
        if "NO-ENTRY" in r["flags"] and r["days_out"] <= ESCALATE_DAYS:
            print(f"ESCALATE: {r['issuer']} {r['event_ticker']} calls in "
                  f"{r['days_out']}d with NO registry entry")
        # A disputed date escalates at ANY distance, not just inside
        # ESCALATE_DAYS: the whole failure mode is that the slate has the event
        # on the wrong day, so "how many days out" is exactly the number we
        # cannot trust to decide urgency with.
        dsp = next((f for f in r["flags"] if f.startswith("DATE-DISPUTED")), None)
        if dsp:
            kalshi = dsp[len("DATE-DISPUTED(kalshi="):-1]
            print(f"ESCALATE: {r['issuer']} {r['event_ticker']} CALL DATE DISPUTED "
                  f"— issuer says {r['call_date']}, Kalshi says {kalshi}; "
                  f"stage against the issuer date")
    n_flag = len(rows)
    print(f"worklist: {n_flag} flagged in {a.days}d horizon", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
