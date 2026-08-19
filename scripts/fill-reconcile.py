#!/usr/bin/env python3
"""Did anything trade on OUR markets that our own ledger does not know about?

WHY THIS EXISTS (2026-08-18). Every money number this project has ever
reported is SELF-REPORTED: `report.json → orders[]`, written by the arm process
about its own sends. `settle-score.py` scores those same records; the day study
and the trade-gap funnel read them too. Nothing has ever asked the exchange.

On 2026-08-18 that gap became measurable. Earshot filled three orders
(AS-TARI 96¢, KLAR-SWED 97¢, HD-INNO 92¢) and every grader reported "3 fills,
285¢". The account's own fills endpoint for the same UTC day lists FIVE fills
on earshot's own event markets — the two extra ones placed by `sweep-hft`,
a separate project that shares this Kalshi account (memory: "shares ONLY the
Kalshi + PM accounts"). One of them, `KXEARNINGSMENTIONHD-26AUG18-INFL`
bought at 49¢, settled NO. Earshot had itself detected `inflation` on that
market and been refused by the then-existing per-event notional cap (removed
that same day by operator ruling, SYSTEM-STATE O8) — so the position exists
on the account, the loss is real, and no earshot artifact mentions it.

The system already KNEW the account was shared — `CLAUDE.md` warns that the
kill switch must cancel only `OrderRegistry` ids because "this account carries
unrelated resting orders from other systems", and `missed-arm-check.py` keeps
sweep out of its numerator. What was missing is the other direction: nothing
ever looked at the account and asked whether OUR books explain it.

This asks exactly that, per UTC day, for the markets of the events we ran:

    for every account fill on one of our event's markets,
    is it an order WE placed, and does our evidence record it?

Three outcomes per fill:
  recorded   — order_id appears in one of our runs' order records. Normal.
  unrecorded — the order is OURS (its client_order_id is in our evidence) but
               no run recorded the fill. A LEDGER BUG: we traded and lost the
               receipt. This is the only condition that pages.
  foreign    — the order is not ours at all. Expected on a shared account, but
               it is account exposure on a market we also trade, so it is
               always printed and always written to the JSON. It never pages,
               because a nightly alarm for a known-legitimate condition trains
               the operator to ignore the alarm that matters.

It also reports, per board, our own notional beside the ACCOUNT's notional on
that board's markets. It reports; it never enforces — spend policy is the
operator's (SYSTEM-STATE §4). There is no per-event notional cap to compare
against any more: it was removed on 2026-08-18 (O8).

Read-only. Signs GET requests only (`/portfolio/fills`, `/portfolio/orders/…`);
it can neither place nor cancel anything. Writes only its JSON report.

Usage:
  scripts/fill-reconcile.py --date 2026-08-18
  scripts/fill-reconcile.py --date 2026-08-18 --json var/audits/fills-2026-08-18.json
  scripts/fill-reconcile.py --check          # offline self-test, no network

Exit: 0 reconciled (every fill on our markets is ours and recorded, or is a
      declared foreign fill), 1 at least one UNRECORDED fill of ours,
      2 could not determine (no credentials, or the account is unreachable).
"""
import argparse
import base64
import datetime as dt
import glob
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BASE = os.environ.get(
    "KALSHI_REST_BASE", "https://external-api.kalshi.com/trade-api/v2")
CRED_FILE = os.path.join(os.path.expanduser("~"), ".kalshi", "earshot_env")

# Event dirs that belong to the OTHER lane sharing this account. sweep-hft
# banks its runs in its own repo, but a stray dir here must never be read as
# an earshot event whose markets we then reconcile against.
FOREIGN_LANE_SUFFIX = re.compile(r"-(SWEEP|VEL\d*)$")

EXIT_OK, EXIT_UNRECORDED, EXIT_UNDETERMINED = 0, 1, 2


# ── credentials ─────────────────────────────────────────────────────────────
def load_credentials(env=None, cred_file=CRED_FILE):
    """(key_id, pem_path) from the environment, falling back to the operator's
    credential file — the same file `slate-stage.sh` sources when launchd fires
    it with no environment. Returns None when neither supplies both."""
    env = os.environ if env is None else env
    key_id = env.get("KALSHI_API_KEY_ID")
    pem = env.get("KALSHI_PRIVATE_KEY_PATH")
    if not (key_id and pem) and os.path.exists(cred_file):
        for line in open(cred_file):
            m = re.match(r"\s*(?:export\s+)?([A-Z_]+)=(.*)", line.strip())
            if not m:
                continue
            name, value = m.group(1), m.group(2).strip().strip('"').strip("'")
            if name == "KALSHI_API_KEY_ID" and not key_id:
                key_id = value
            elif name == "KALSHI_PRIVATE_KEY_PATH" and not pem:
                pem = value
    if not (key_id and pem):
        return None
    # The file is written to be `source`d by a shell, so its values may carry
    # `$HOME` or `~` verbatim. Reading it as text without expanding them is the
    # launchd-minimal-env failure class in miniature: found live on 2026-08-18,
    # where the pem path resolved to a literal '$HOME/.kalshi/earshot.pem'.
    return key_id, os.path.expanduser(os.path.expandvars(pem))


def make_getter(key_id, pem_path, base=DEFAULT_BASE, timeout=20):
    """A signed read-only GET. Mirrors `src/kalshi/auth.rs::headers`: RSA-PSS
    over SHA-256, salt length = digest length, canonical string
    `{ts_ms}{METHOD}{path}` where `path` starts at `/trade-api/` and NEVER
    includes the query string."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    with open(pem_path, "rb") as fh:
        key = serialization.load_pem_private_key(fh.read(), password=None)

    def get(endpoint, query=""):
        path = "/trade-api/v2" + endpoint
        ts = str(int(time.time() * 1000))
        sig = key.sign(
            (ts + "GET" + path).encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=hashes.SHA256.digest_size),
            hashes.SHA256())
        req = urllib.request.Request(base + endpoint + (("?" + query) if query else ""),
                                     headers={
                                         "KALSHI-ACCESS-KEY": key_id,
                                         "KALSHI-ACCESS-SIGNATURE":
                                             base64.b64encode(sig).decode(),
                                         "KALSHI-ACCESS-TIMESTAMP": ts})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    return get


# ── what WE ran, and what WE recorded ───────────────────────────────────────
def plan_date(plan):
    """The RunPlan's declared UTC date, or None. Never the RUN_ID string and
    never the ticker suffix — the 2026-08-18 TOL lesson (a `-26AUG18` ticker
    whose call was on the 19th)."""
    raw = plan.get("starts_at_utc")
    if not isinstance(raw, str):
        return None
    try:
        return dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").date().isoformat()
    except ValueError:
        return None


def our_boards(date, runs_dir=None, repo=REPO):
    """{market_event_ticker: board} for every Kalshi event our RunPlans declare
    on `date`.

    Keyed by the MARKET event, not by our event id, because the account's unit
    of exposure is the Kalshi board and more than one earshot lane can cover
    one board — on 2026-08-18 `KXEARNINGSMENTIONHD-26AUG18` carried both the
    laptop arm and the box 1¢ observe. Keying by lane put the laptop's own fill
    in the box lane's bucket and called it foreign.

    Order ids are the UNION over every attempt of every lane: a superseded
    attempt can still have reached the wire. The prefix comes from the plan's
    `market_event_ticker`, so a market we never touched is still in scope — a
    foreign fill on a market we passed on is exactly the exposure this exists
    to surface."""
    runs_dir = runs_dir or os.path.join(repo, "var", "runs")
    boards = {}
    for plan_path in sorted(glob.glob(
            os.path.join(runs_dir, "*", "*", "control", "run-plan.json"))):
        try:
            plan = json.load(open(plan_path))
        except (OSError, json.JSONDecodeError):
            continue
        event_id = plan.get("event_id") or ""
        if not event_id or FOREIGN_LANE_SUFFIX.search(event_id):
            continue
        if plan_date(plan) != date:
            continue
        prefix = plan.get("market_event_ticker")
        if not prefix:
            continue
        board = boards.setdefault(prefix, {
            "market_event_ticker": prefix, "lanes": {}, "order_ids": {},
            "client_order_ids": set(),
        })
        lane = board["lanes"].setdefault(event_id, {
            "event_id": event_id, "run_ids": [],
        })
        lane["run_ids"].append(plan.get("run_id"))
        run_dir = os.path.dirname(os.path.dirname(plan_path))
        report_rel = (plan.get("artifacts") or {}).get(
            "runtime_report", "report/report.json")
        collect_orders(os.path.join(run_dir, report_rel), board, event_id)
    return boards


def collect_orders(report_path, board, event_id):
    """Fold one run's order records into the board's ledger view, remembering
    WHICH lane placed each order — a fill is attributed to the lane that
    recorded its order id, never to whichever lane happens to sort first."""
    try:
        report = json.load(open(report_path))
    except (OSError, json.JSONDecodeError):
        return
    for order in report.get("orders") or []:
        client_id = order.get("client_order_id")
        if client_id:
            board["client_order_ids"].add(client_id)
        order_id = order.get("order_id")
        if not order_id:
            continue
        board["order_ids"][order_id] = {
            "event_id": event_id,
            "ticker": order.get("ticker"),
            "price_cents": order.get("price_cents"),
            "status": order.get("order_status"),
        }


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── the account's side ──────────────────────────────────────────────────────
def fetch_fills(date, get):
    """Every account fill in the UTC day. Kalshi pages with an opaque cursor;
    the query string is not signed, so paging needs no re-derivation."""
    day = dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    min_ts, max_ts = int(day.timestamp()), int((day + dt.timedelta(days=1)).timestamp())
    fills, cursor, pages = [], "", 0
    while True:
        query = f"limit=200&min_ts={min_ts}&max_ts={max_ts}"
        if cursor:
            query += "&cursor=" + cursor
        body = get("/portfolio/fills", query)
        fills += body.get("fills") or []
        cursor = body.get("cursor") or ""
        pages += 1
        if not cursor or pages > 50:
            break
    return fills


def classify(fills, boards, fetch_order=None):
    """Split the day's fills over our boards' markets into recorded / unrecorded
    / foreign.

    A fill is RECORDED when its order id is one we placed — that lookup also
    names the lane, so a two-lane board attributes each fill correctly. A fill
    Kalshi does not attribute to a known order id is resolved by fetching the
    ORDER, whose `client_order_id` is the only field that can say 'this was
    ours': the fill payload carries no client id. A lookup that fails leaves the
    fill FOREIGN — never silently promoted to ours."""
    rows = []
    for fill in fills:
        ticker = fill.get("ticker") or fill.get("market_ticker") or ""
        board = None
        for prefix, candidate in boards.items():
            if ticker.startswith(prefix + "-"):
                board = candidate
                break
        if board is None:
            continue                      # not one of our markets: not our business
        order_id = fill.get("order_id")
        contracts = _to_float(fill.get("count_fp")) or 0.0
        price_cents = round((_to_float(fill.get("yes_price_dollars")) or 0.0) * 100)
        row = {
            "market_event_ticker": board["market_event_ticker"],
            "ticker": ticker, "order_id": order_id,
            "contracts": contracts, "price_cents": price_cents,
            "cost_cents": contracts * price_cents,
            "created_time": fill.get("created_time"),
            "fee_cents": round((_to_float(fill.get("fee_cost")) or 0.0) * 100, 4),
        }
        placed_by = board["order_ids"].get(order_id)
        if placed_by is not None:
            row["classification"] = "recorded"
            row["event_id"] = placed_by["event_id"]
        else:
            client_id = None
            if fetch_order is not None and order_id:
                try:
                    client_id = ((fetch_order(order_id) or {}).get("order") or {}
                                 ).get("client_order_id")
                except Exception as exc:            # noqa: BLE001 — reported, not raised
                    row["order_lookup_error"] = str(exc)
            row["client_order_id"] = client_id
            row["event_id"] = None
            row["classification"] = ("unrecorded"
                                     if client_id and client_id in board["client_order_ids"]
                                     else "foreign")
        rows.append(row)
    return rows


def summarize(boards, rows):
    """Per-board ledger view: what we placed, what the account holds, and the
    difference. `foreign_notional_cents` is exposure on a board we trade that
    earshot did not place — reported, never enforced; spend policy is the
    operator's (SYSTEM-STATE §4). There is no per-event notional cap to compare
    an account total against any more (removed 2026-08-18, O8)."""
    per_board = {}
    for prefix, board in boards.items():
        mine = [r for r in rows if r["market_event_ticker"] == prefix
                and r["classification"] == "recorded"]
        unrecorded = [r for r in rows if r["market_event_ticker"] == prefix
                      and r["classification"] == "unrecorded"]
        foreign = [r for r in rows if r["market_event_ticker"] == prefix
                   and r["classification"] == "foreign"]
        our_cents = sum(r["cost_cents"] for r in mine + unrecorded)
        foreign_cents = sum(r["cost_cents"] for r in foreign)
        per_board[prefix] = {
            "lanes": [{"event_id": lane["event_id"],
                       "run_ids": lane["run_ids"]}
                      for lane in sorted(board["lanes"].values(),
                                         key=lambda l: l["event_id"])],
            "recorded_fills": len(mine),
            "unrecorded_fills": len(unrecorded),
            "foreign_fills": len(foreign),
            "our_notional_cents": round(our_cents, 2),
            "foreign_notional_cents": round(foreign_cents, 2),
            "account_notional_cents": round(our_cents + foreign_cents, 2),
            "foreign_exposure": bool(foreign),
        }
    return per_board


def reconcile(date, boards, get, fetch_order=None):
    fills = fetch_fills(date, get)
    rows = classify(fills, boards, fetch_order=fetch_order)
    per_board = summarize(boards, rows)
    return {
        "kind": "fill-reconcile",
        "schema": 1,
        "date": date,
        "boards": per_board,
        "fills": rows,
        "summary": {
            "account_fills_on_our_markets": len(rows),
            "recorded": sum(1 for r in rows if r["classification"] == "recorded"),
            "unrecorded": sum(1 for r in rows if r["classification"] == "unrecorded"),
            "foreign": sum(1 for r in rows if r["classification"] == "foreign"),
        },
    }


def _lane_label(prefix, event_id):
    """The lane's own suffix, so two lanes on one board are distinguishable —
    `…HD-26AUG18-BOX-WEBCAST` reads as `box-webcast`, not a second `webcast`."""
    tail = event_id[len(prefix) + 1:] if event_id.startswith(prefix + "-") else event_id
    return tail.lower() or event_id.lower()


def render(payload):
    out = []
    s = payload["summary"]
    for prefix, board in sorted(payload["boards"].items()):
        lanes = ", ".join(_lane_label(prefix, lane["event_id"])
                          for lane in board["lanes"])
        line = (f"  {prefix}: ours {board['recorded_fills']} fill(s) "
                f"{board['our_notional_cents']:.0f}¢, "
                f"foreign {board['foreign_fills']} fill(s) "
                f"{board['foreign_notional_cents']:.0f}¢, "
                f"account {board['account_notional_cents']:.0f}¢")
        out.append(line + (f"  [{lanes}]" if lanes else ""))
    for row in payload["fills"]:
        if row["classification"] == "recorded":
            continue
        label = ("ours, and NO run recorded it — the ledger lost a trade"
                 if row["classification"] == "unrecorded"
                 else "not ours — another system on this shared account")
        out.append(f"     {row['classification'].upper():<10} {row['ticker']} "
                   f"{row['contracts']:g} @ {row['price_cents']}¢ "
                   f"({row['created_time']}) — {label}")
    head = (f"fill-reconcile {payload['date']}: "
            f"{s['account_fills_on_our_markets']} account fill(s) on our markets — "
            f"{s['recorded']} recorded, {s['unrecorded']} UNRECORDED, "
            f"{s['foreign']} foreign")
    return "\n".join([head] + out)


# ── offline self-test ───────────────────────────────────────────────────────
def check():
    """Sanctioned offline self-test. No network, no credentials, no writes."""
    import tempfile

    def fake_get(fills):
        def get(endpoint, query=""):
            assert endpoint == "/portfolio/fills", endpoint
            assert "cursor" not in query, "single page fixture"
            return {"fills": fills, "cursor": ""}
        return get

    def write_lane(runs, event_id, run_id, orders, repo):
        run_dir = os.path.join(runs, event_id, run_id)
        os.makedirs(os.path.join(run_dir, "control"))
        os.makedirs(os.path.join(run_dir, "report"))
        cfg = os.path.join(repo, event_id + ".toml")
        open(cfg, "w").write("[risk]\nmax_order_notional_cents = 100\n")
        json.dump({"kind": "run-plan", "event_id": event_id, "run_id": run_id,
                   "market_event_ticker": "KXEARNINGSMENTIONHD-26AUG18",
                   "starts_at_utc": "2026-08-18T13:00:00Z", "config_path": cfg,
                   "artifacts": {"runtime_report": "report/report.json"}},
                  open(os.path.join(run_dir, "control", "run-plan.json"), "w"))
        json.dump({"event_id": event_id, "orders": orders},
                  open(os.path.join(run_dir, "report", "report.json"), "w"))

    BOARD = "KXEARNINGSMENTIONHD-26AUG18"
    LAPTOP, BOX = BOARD + "-WEBCAST", BOARD + "-BOX-WEBCAST"
    ours = {"ticker": BOARD + "-INNO", "order_id": "ours-1", "count_fp": "1.00",
            "yes_price_dollars": "0.9200",
            "created_time": "2026-08-18T13:05:31Z", "fee_cost": "0.0052"}
    theirs = {"ticker": BOARD + "-INFL", "order_id": "sweep-1", "count_fp": "1.00",
              "yes_price_dollars": "0.4900",
              "created_time": "2026-08-18T13:54:21Z", "fee_cost": "0.0175"}
    elsewhere = {"ticker": "KXTRUMPMENTION-26AUG18-XYZ", "order_id": "sweep-2",
                 "count_fp": "9.00", "yes_price_dollars": "0.5000",
                 "created_time": "2026-08-18T13:00:00Z", "fee_cost": "0.10"}

    with tempfile.TemporaryDirectory() as d:
        runs = os.path.join(d, "runs")
        # TWO earshot lanes on ONE Kalshi board — the 26AUG18 HD shape (laptop
        # arm + box 1¢ observe). The box lane sorts first and placed nothing.
        write_lane(runs, BOX, "20260818T130000Z-p3-box", [], d)
        write_lane(runs, LAPTOP, "20260818T130000Z-p3-laptop", [
                       {"ticker": BOARD + "-INNO", "order_id": "ours-1",
                        "client_order_id": "coid-1", "price_cents": 97,
                        "order_status": "filled",
                        "fill": {"cumulative_contracts": "1.00",
                                 "average_price": "0.9200"}},
                       {"ticker": BOARD + "-INFL", "order_id": None,
                        "client_order_id": "coid-2", "price_cents": 97,
                        "refusal": "event notional would reach 194¢"},
                   ], d)

        boards = our_boards("2026-08-18", runs_dir=runs, repo=d)
        assert set(boards) == {BOARD}, boards
        assert set(boards[BOARD]["lanes"]) == {LAPTOP, BOX}, boards[BOARD]["lanes"]
        # No per-event cap exists to read any more (O8, 2026-08-18).
        assert "cap_cents" not in boards[BOARD]["lanes"][LAPTOP], boards[BOARD]
        # A plan declaring another date is NOT this date's board.
        assert our_boards("2026-08-19", runs_dir=runs, repo=d) == {}

        # THE REGRESSION: keyed by lane, this fill landed in the box lane's
        # bucket (which placed nothing) and was reported FOREIGN. Keyed by
        # board and attributed by order id, it is ours, and it names the lane.
        p = reconcile("2026-08-18", boards, fake_get([ours, theirs, elsewhere]),
                      fetch_order=lambda oid: {"order": {"client_order_id": "sweep-coid"}})
        assert p["summary"] == {"account_fills_on_our_markets": 2, "recorded": 1,
                                "unrecorded": 0, "foreign": 1}, p["summary"]
        recorded = [r for r in p["fills"] if r["classification"] == "recorded"]
        assert recorded[0]["event_id"] == LAPTOP, recorded[0]
        b = p["boards"][BOARD]
        assert (b["our_notional_cents"], b["foreign_notional_cents"],
                b["account_notional_cents"]) == (92, 49, 141), b
        assert b["foreign_exposure"] is True, b

        # The SAME unmatched fill is UNRECORDED, not foreign, when the order's
        # client id is one of ours — we placed it and lost the receipt.
        p2 = reconcile("2026-08-18", boards, fake_get([theirs]),
                       fetch_order=lambda oid: {"order": {"client_order_id": "coid-2"}})
        assert p2["summary"]["unrecorded"] == 1, p2["summary"]
        assert p2["summary"]["foreign"] == 0, p2["summary"]

        # Clean day: only our own recorded fill, no foreign exposure.
        p3 = reconcile("2026-08-18", boards, fake_get([ours]))
        assert p3["summary"]["foreign"] == 0 and p3["summary"]["unrecorded"] == 0, p3
        assert p3["boards"][BOARD]["foreign_exposure"] is False, p3

        # An order lookup that fails must not crash the reconciliation, and an
        # unresolvable fill stays FOREIGN — never silently promoted to ours.
        def boom(_oid):
            raise urllib.error.URLError("network down")
        p4 = reconcile("2026-08-18", boards, fake_get([theirs]), fetch_order=boom)
        assert p4["summary"]["foreign"] == 1, p4["summary"]
        assert "order_lookup_error" in p4["fills"][0], p4["fills"][0]

        render(p)   # must not raise on any classification

    # A lane whose run dir carries a SUPERSEDED attempt still contributes its
    # order ids: a closed attempt can have reached the wire.
    with tempfile.TemporaryDirectory() as d:
        runs = os.path.join(d, "runs")
        write_lane(runs, LAPTOP, "20260818T130000Z-p3-first", [
                       {"ticker": BOARD + "-INNO", "order_id": "ours-1",
                        "client_order_id": "coid-1", "order_status": "filled",
                        "fill": {"cumulative_contracts": "1.00",
                                 "average_price": "0.9200"}}], d)
        os.makedirs(os.path.join(runs, LAPTOP, "20260818T130000Z-p3-second",
                                 "control"))
        os.makedirs(os.path.join(runs, LAPTOP, "20260818T130000Z-p3-second",
                                 "report"))
        json.dump({"kind": "run-plan", "event_id": LAPTOP,
                   "run_id": "20260818T130000Z-p3-second",
                   "market_event_ticker": BOARD,
                   "starts_at_utc": "2026-08-18T13:00:00Z",
                   "artifacts": {"runtime_report": "report/report.json"}},
                  open(os.path.join(runs, LAPTOP, "20260818T130000Z-p3-second",
                                    "control", "run-plan.json"), "w"))
        boards = our_boards("2026-08-18", runs_dir=runs, repo=d)
        p5 = reconcile("2026-08-18", boards, fake_get([ours]))
        assert p5["summary"]["recorded"] == 1, p5["summary"]

    # Credentials: a value written for a shell to expand must be expanded here
    # too, and a file supplying only one of the pair is not credentials.
    with tempfile.TemporaryDirectory() as d:
        cred = os.path.join(d, "earshot_env")
        open(cred, "w").write("export KALSHI_API_KEY_ID=abc\n"
                              "export KALSHI_PRIVATE_KEY_PATH=$HOME/.kalshi/earshot.pem\n")
        got = load_credentials(env={}, cred_file=cred)
        assert got is not None and got[0] == "abc", got
        assert "$HOME" not in got[1] and got[1].startswith(os.path.expanduser("~")), got
        open(cred, "w").write("export KALSHI_API_KEY_ID=abc\n")
        assert load_credentials(env={}, cred_file=cred) is None, "half a pair is not credentials"
        assert load_credentials(env={}, cred_file=os.path.join(d, "absent")) is None

    print("fill-reconcile --check: OK")
    return EXIT_OK


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="UTC event day, YYYY-MM-DD")
    ap.add_argument("--json", help="write the full reconciliation here")
    ap.add_argument("--check", action="store_true", help="offline self-test")
    args = ap.parse_args()

    if args.check:
        return check()
    if not args.date:
        print("fill-reconcile: --date or --check is required", file=sys.stderr)
        return EXIT_UNDETERMINED

    boards = our_boards(args.date)
    if not boards:
        print(f"fill-reconcile {args.date}: no run plan declares this date — "
              f"nothing to reconcile")
        return EXIT_OK

    creds = load_credentials()
    if creds is None:
        print(f"fill-reconcile {args.date}: UNDETERMINED — no Kalshi credentials "
              f"(KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH, or {CRED_FILE})",
              file=sys.stderr)
        return EXIT_UNDETERMINED
    try:
        get = make_getter(*creds)
        payload = reconcile(args.date, boards, get,
                            fetch_order=lambda oid: get("/portfolio/orders/" + oid))
    except Exception as exc:                        # noqa: BLE001 — fail loud, never "no"
        print(f"fill-reconcile {args.date}: UNDETERMINED — {exc}", file=sys.stderr)
        return EXIT_UNDETERMINED

    print(render(payload))
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
            fh.write("\n")
    return EXIT_UNRECORDED if payload["summary"]["unrecorded"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
