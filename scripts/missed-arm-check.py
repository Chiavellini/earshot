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
import datetime as dt
import glob
import json
import os
import re
import sys
import tomllib
import urllib.parse

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


# One issuer+date can carry several LANES (`…HIMS-26AUG10-WEBCAST` and
# `…-PHONE`). `descoped`/`wire_evidence` have stripped this suffix since
# 2026-08-05 to accept any lane's record; everything else now does too.
LANE_SUFFIX = re.compile(r"-(WEBCAST|PHONE|ACOUSTIC)$")

# `sweep-hft` is a SEPARATE project that shares only the Kalshi account. Its run
# dirs sit in the same state/ pile and its event ids can match the mention
# regex, so its fills must never be counted as this system's. Today's sweep
# reports carry `event_id: null` and a KXTRUMPMENTION series so nothing leaks —
# this makes the boundary explicit rather than incidental.
NON_MENTION_SUFFIX = re.compile(r"-SWEEP$")

# Venue/tool rehearsals live beside issuer captures but are not issuer boards.
# Keeping them in the denominator turns infrastructure QA into a fictitious
# KXEARNINGSMENTION issuer and, on network failure, an irrelevant undetermined.
NON_EVENT_PROBE_ISSUERS = {"TTSREHEARSAL", "Q4CHIME"}
# Mirrors scripts/eventctl.py:58. A config still carrying the placeholder
# start has NOT declared a call time, so it may never move a lane's date.
PLACEHOLDER_STARTS_AT = "2026-07-15T00:00:00Z"
BAD_VERDICTS = {"MISSED-ARM", "ARMED-DIED", "ARMED-NO-FILL", "ARMED-NO-RECORD"}


def _event_dirs(state_dir="state", stem=None):
    """Event state dirs for the whole pile, or for one issuer+date stem."""
    pat = (stem + "*") if stem else "*"
    return [d for d in sorted(glob.glob(os.path.join(state_dir, pat)))
            if os.path.isdir(d)
            and not NON_MENTION_SUFFIX.search(os.path.basename(d))]


def _artifact(d, name):
    """THE `<name>` for event dir `d` — canonical, or pulled from the box.

    A BOX-HOSTED run leaves NOTHING at `state/<E>/<name>` on the laptop. The
    documented pull (`.claude/skills/phone-event-prep`, and the convention
    `scripts/event-status.sh:35` already reads) lands it at
    `state/<E>/box/<name>`, so globbing one level deep made every box-hosted run
    invisible to this grader. That is how HIMS 26AUG10 was reported as
    "1 staged, 1 captured, 0 orders, 1c observing posture" on a day its PHONE
    lane put 2 orders on the wire and spent 194c: the only lane the grader could
    see was the laptop's webcast twin.

    Returns at most ONE path. `box/` exists only because a box run was pulled,
    so it wins when both are present and nothing is double-counted.
    """
    box = os.path.join(d, "box", name)
    if os.path.isfile(box):
        return box
    p = os.path.join(d, name)
    return p if os.path.isfile(p) else None


def _runs_dir_for(state_dir):
    """The canonical run root that pairs with `state_dir`.

    DERIVED, never passed: a self-test that isolates `state_dir` into a tempdir
    isolates the canonical tree with it, so `--check` can never silently read
    the real repo (the failure mode that already exists at `reconcile`).
    `state/` and `var/runs/` are siblings under one repo root by construction.
    """
    parent = os.path.dirname(os.path.abspath(state_dir))
    return os.path.join(parent, "var", "runs")


def _report(d):
    """THE terminal report for event dir `d` — canonical tree first, then legacy.

    E1 closed and INVERTED the problem: an armed run now writes its report into
    the immutable `var/runs/<EVENT_ID>/<RUN_ID>/report/` tree named by its own
    RunPlan, and writes NOTHING to `state/<E>/report.json`. On 2026-08-18 all
    four runs did exactly that, and this grader reported 3 x ARMED-NO-RECORD on
    a day that put three orders on the wire and filled all three — then paged
    the operator for it. An alarm that cries wolf on a good day is training the
    operator to ignore it on a bad one, which is the exact 26AUG04 failure this
    check exists to prevent.

    Selection is by ARTIFACT EXISTENCE, never by run-id ordering and never by
    receipt depth: AS-26AUG18 has four attempts, and the report-bearing one is
    neither last by name nor deepest by receipts.

    The report path comes from the plan's own `artifacts.runtime_report`, not a
    hardcoded constant — a published plan is immutable and authoritative. Legacy
    `state/<E>/{box/,}report.json` remains the fallback so older events, and
    box-pulled runs, keep grading exactly as before.

    Returns at most ONE path, like `_artifact`, so nothing is double-counted.
    """
    d = os.path.normpath(d)
    event_id = os.path.basename(d)
    state_dir = os.path.dirname(d) or "state"
    runs_dir = _runs_dir_for(state_dir)
    found = []
    for plan_path in sorted(glob.glob(os.path.join(
            runs_dir, event_id, "*", "control", "run-plan.json"))):
        try:
            plan = json.load(open(plan_path))
        except (OSError, ValueError):
            continue
        artifacts = plan.get("artifacts")
        if not isinstance(artifacts, dict):
            continue
        rel = artifacts.get("runtime_report")
        # Containment: a plan may not point outside its own run dir.
        if not isinstance(rel, str) or not rel or os.path.isabs(rel):
            continue
        if ".." in os.path.normpath(rel).split(os.sep):
            continue
        run_dir = os.path.dirname(os.path.dirname(plan_path))
        candidate = os.path.join(run_dir, rel)
        if os.path.isfile(candidate):
            found.append(candidate)
    if found:
        # Deterministic when several attempts each banked a report; RUN_ID
        # carries the UTC start prefix, so the last is the latest attempt.
        return found[-1]
    return _artifact(d, "report.json")


def _artifacts(d, name):
    """Every existing `<name>` for `d` (box first). For OR-style evidence only.

    Safe where the question is a boolean ("did any journal record arm-start?")
    and wrong where anything is summed — use `_artifact` for report.json.
    """
    return [p for p in (os.path.join(d, "box", name), os.path.join(d, name))
            if os.path.isfile(p)]


def _lane_arm_evidence(event_id, config_path, state_dir="state", runs_dir="var/runs"):
    """Return exact artifacts proving this config became an arm candidate.

    A filename or event-id match is intent, not an armed lane. Typed attempts
    bind the config path in RunPlan. Legacy attempts have no such binding, so
    only an exact event state report/journal can carry them.
    """
    typed = []
    legacy = []
    config_abs = os.path.abspath(config_path)
    config_rel = os.path.relpath(config_abs)
    for plan_path in sorted(glob.glob(os.path.join(
            runs_dir, event_id, "*", "control", "run-plan.json"))):
        try:
            plan = json.load(open(plan_path))
        except (OSError, ValueError):
            continue
        bound = plan.get("config_path")
        if not isinstance(bound, str):
            continue
        bound_abs = os.path.abspath(bound)
        if bound in (config_path, config_rel) or bound_abs == config_abs:
            typed.append(os.path.relpath(plan_path))
    event_dir = os.path.join(state_dir, event_id)
    for name in ("report.json", "journal.jsonl"):
        artifact = _artifact(event_dir, name)
        if artifact:
            legacy.append(os.path.relpath(artifact))
    return {"typed": typed, "legacy": legacy}


def event_board_activity(event_id, start_ts, end_ts, fetch=None):
    """Board volume at any point in one bounded UTC window.

    Hour candlesticks preserve activity that a later point-in-time open check
    cannot see. The event endpoint returns one candlestick array per market.
    """
    fetch = fetch or _bo.fetch
    event_ticker = LANE_SUFFIX.sub("", event_id)
    series_ticker = event_ticker.split("-", 1)[0]
    if not isinstance(start_ts, int) or not isinstance(end_ts, int) or start_ts >= end_ts:
        raise ValueError("invalid board-activity window")
    query = urllib.parse.urlencode({
        "start_ts": start_ts,
        "end_ts": end_ts,
        "period_interval": 60,
    })
    url = (
        f"{_bo.BASE}/series/{urllib.parse.quote(series_ticker, safe='')}"
        f"/events/{urllib.parse.quote(event_ticker, safe='')}/candlesticks?{query}"
    )
    payload = fetch(url)
    tickers = payload.get("market_tickers")
    series = payload.get("market_candlesticks")
    if not isinstance(tickers, list) or not isinstance(series, list):
        raise ValueError("event candlesticks missing market arrays")
    if len(tickers) != len(series) or any(not isinstance(rows, list) for rows in series):
        raise ValueError("event candlestick market arrays are misaligned")
    volume = 0.0
    active_markets = 0
    for rows in series:
        market_volume = 0.0
        for candle in rows:
            if not isinstance(candle, dict):
                raise ValueError("event candlestick row is not an object")
            value = candle.get("volume_fp", candle.get("volume")) or 0
            try:
                market_volume += float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"event candlestick volume is invalid: {value!r}") from exc
        if market_volume > 0:
            active_markets += 1
        volume += market_volume
    return {
        "traded": volume > 0,
        "series": event_ticker,
        "markets": len(tickers),
        "active_markets": active_markets,
        "volume": volume,
        "window_start_ts": start_ts,
        "window_end_ts": end_ts,
    }


def captures_for(date, probe_dir="corpus/bridge-probes", state_dir="state"):
    """issuer -> {'held_s': float, 'sources': [...]} for that date."""
    out = {}
    us = date.replace("-", "_")
    for p in sorted(glob.glob(os.path.join(probe_dir, f"*{us}*.json"))):
        base = os.path.basename(p)
        m = re.match(r"([A-Za-z0-9]+)_", base)
        if not m:
            continue
        issuer = m.group(1).upper()
        if issuer in NON_EVENT_PROBE_ISSUERS:
            continue
        try:
            held = json.load(open(p)).get("held_ms", 0) / 1000.0
        except Exception:
            continue
        if held < MIN_CAPTURE_S:
            continue
        e = out.setdefault(issuer, {"held_s": 0.0, "sources": []})
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
    for d in _event_dirs(state_dir):
        p = _report(d)
        if not p:
            continue
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
    for d in _event_dirs(state_dir):
        # The EVENT dir names the event — never the file's parent, which is
        # literally "box" for a pulled box run.
        ev = os.path.basename(d)
        m = re.match(r"KXEARNINGSMENTION([A-Z0-9]+?)-", ev)
        if not m:
            continue
        if suffix and suffix not in ev.upper():
            continue
        journals = _artifacts(d, "journal.jsonl")
        armed = False
        for p in journals:
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
        e["sources"].extend(os.path.relpath(p) for p in journals)
    return out


def staged_for(date, config_dir="configs", state_dir="state", runs_dir="var/runs",
               deferred=None):
    """issuer -> {'config','event_id','fill_posture','run_dir'} for the day's arm configs.

    THE DENOMINATOR (2026-08-07). Before this, `reconcile` iterated over
    CAPTURES, so an event that never armed at all had no row to be missing
    from — it was invisible rather than flagged. On 2026-08-06 ABNB and LYFT
    both died on a venue registration wall, produced no capture and no orders,
    and the checker reported **0 MISSED-ARM** for a day that missed three of
    four. A grader whose denominator is its own successes cannot report a
    total failure.

    Staging is the commitment: a config on disk for the date means we intended
    to cover that issuer. That is the honest population to grade against.
    """
    dp = _ticker_suffix(date).lower()
    if not dp:
        return {}
    out = {}
    # `*-{dp}-phone-arm.toml` is the name scripts/phone-lane-derive.py actually
    # writes (e.g. `hims-26aug10-phone-arm.toml`, 26AUG10). The old
    # `phone-*-{dp}-arm.toml` matched NOTHING, so the phone lane was never in
    # the denominator at all — HIMS 26AUG10 was graded entirely on its webcast
    # twin while the phone lane was the one that traded.
    patterns = (f"webcast-*-{dp}-arm-wc.toml", f"twilio-*-{dp}-arm.toml",
                f"*-{dp}-phone-arm.toml", f"phone-*-{dp}-arm.toml",
                f"acoustic-*-{dp}-arm.toml")
    seen_cfg = set()
    for pat in patterns:
        for p in sorted(glob.glob(os.path.join(config_dir, pat))):
            if p in seen_cfg:      # patterns overlap by design; count each once
                continue
            seen_cfg.add(p)
            try:
                with open(p, "rb") as fh:
                    cfg = tomllib.load(fh)
            except (OSError, tomllib.TOMLDecodeError):
                continue
            ev = (cfg.get("event") or {}).get("id") or ""
            m = re.match(r"KXEARNINGSMENTION([A-Z0-9]+?)-", ev)
            if not m:
                continue
            # THE CONFIG'S OWN START DATES THE COMMITMENT, not the Kalshi ticker
            # suffix. A call can move after the ticker is minted:
            # KXEARNINGSMENTIONTOL-26AUG18 carries a typed date_correction to
            # 2026-08-19T12:30Z, so on 08-18 this grader reported a MISSED-ARM
            # for a call that had not happened yet, on a board whose 10,428
            # contracts were pre-positioning.
            #
            # Only a PARSEABLE, non-placeholder start may move a lane. Missing,
            # malformed or placeholder keeps the filename date — a denominator
            # that shrinks on bad input is exactly the failure this function's
            # docstring exists to prevent, so deferral must fail visible.
            starts_at = (cfg.get("event") or {}).get("starts_at")
            if isinstance(starts_at, str) and starts_at != PLACEHOLDER_STARTS_AT:
                try:
                    when = dt.datetime.fromisoformat(
                        starts_at.replace("Z", "+00:00"))
                except ValueError:
                    when = None
                if when is not None:
                    declared = when.astimezone(dt.timezone.utc).date().isoformat()
                    if declared != date:
                        if deferred is not None:
                            deferred.append({
                                "issuer": m.group(1),
                                "event_id": ev,
                                "config": os.path.relpath(p),
                                "declared_date": declared,
                            })
                        continue
            kws = cfg.get("keywords") or []
            # POSTURE FROM THE CONFIG, never assumed. The old code justified an
            # ARMED-OBSERVING verdict with "1¢ non-crossing posture" wording
            # even for events armed at a 97¢ fill cap — it cited a posture the
            # config did not have. A single fillable keyword means silence on a
            # traded board is a real miss, not a designed decline.
            fill = any((k.get("max_price_cents") or 0) > 1 for k in kws)
            arm_evidence = _lane_arm_evidence(
                ev, p, state_dir=state_dir, runs_dir=runs_dir)
            lane = {"config": os.path.relpath(p), "event_id": ev,
                    "fill_posture": fill, "keywords": len(kws),
                    "armed": False,
                    "armed_evidence": arm_evidence["typed"] + arm_evidence["legacy"],
                    "typed_arm_evidence": arm_evidence["typed"],
                    "legacy_event_evidence": arm_evidence["legacy"]}
            # ONE ROW PER ISSUER, but every LANE kept. This used to be a plain
            # assignment, so a two-lane issuer silently discarded whichever
            # pattern matched first and graded the day on the survivor.
            e = out.setdefault(m.group(1), {"lanes": []})
            e["lanes"].append(lane)
            # Only a lane with exact run/report/journal evidence may influence
            # the armed posture. A never-armed raw/prepare config remains a
            # visible commitment, but it cannot turn a fillable miss into a
            # benign 1c observation (casino143 IV.5).
            event_counts = {}
            for item in e["lanes"]:
                event_counts[item["event_id"]] = event_counts.get(item["event_id"], 0) + 1
            for item in e["lanes"]:
                item["armed"] = bool(item["typed_arm_evidence"]) or (
                    event_counts[item["event_id"]] == 1
                    and bool(item["legacy_event_evidence"])
                )
                item["ambiguous_legacy_binding"] = bool(
                    item["legacy_event_evidence"]
                    and event_counts[item["event_id"]] > 1
                    and not item["typed_arm_evidence"]
                )
            armed_lanes = [item for item in e["lanes"] if item["armed"]]
            e["armed_lanes"] = armed_lanes
            representative = (armed_lanes or e["lanes"])[0]
            e["event_id"] = representative["event_id"]
            e["config"] = ", ".join(l["config"] for l in e["lanes"])
            e["armed_config"] = ", ".join(l["config"] for l in armed_lanes) or None
            e["fill_posture"] = (
                any(l["fill_posture"] for l in armed_lanes)
                if armed_lanes else None
            )
            e["declared_fill_posture"] = any(
                l["fill_posture"] for l in e["lanes"])
            e["keywords"] = max(l["keywords"] for l in e["lanes"])
            day = dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc)
            e["activity_window_start_ts"] = int(day.timestamp())
            e["activity_window_end_ts"] = int(
                (day + dt.timedelta(days=1)).timestamp())
    return out


def descoped(event_id, state_dir="state"):
    """Was this event deliberately dropped? → reason string, or None.

    THE MCD RULE: a descope must never be indistinguishable from a miss. This
    matters more now that the denominator is the staged set — NBIS-26AUG06 was
    staged, then descoped once its newsroom and the operator's own registration
    page both put the call on 2026-08-13, and grading the staged set naively
    reported it as a 74,308-contract miss.

    TWO forms are honoured, deliberately. `scripts/descope-event.sh` writes the
    canonical `DESCOPED.json`, but the NBIS descope exists only as a journal
    line written by an agent. Recognising just the canonical artifact would
    have this tool cry wolf on a correctly-handled event, which is the failure
    mode that gets a grader ignored.
    """
    # The record does NOT reliably live under the config's own event id. A
    # webcast twin is `…EBAY-26AUG05-WEBCAST` while descope-event.sh keyed
    # EBAY's record to the bare event ticker `…EBAY-26AUG05`, so an exact-path
    # lookup reported both of 2026-08-05's legitimate descopes (DUOL, EBAY) as
    # misses. Match on the issuer+date prefix and accept any lane's record.
    stem = LANE_SUFFIX.sub("", event_id)
    for d in _event_dirs(state_dir, stem):
        dsc = _artifact(d, "DESCOPED.json")
        if dsc:
            try:
                r = json.load(open(dsc))
                return (r.get("reason") or "DESCOPED.json")[:120]
            except (OSError, ValueError):
                return "DESCOPED.json"
        for jp in _artifacts(d, "journal.jsonl"):
            try:
                with open(jp) as fh:
                    for ln in fh:
                        try:
                            j = json.loads(ln)
                        except ValueError:
                            continue
                        if j.get("action") == "descope" and j.get("outcome") == "ok":
                            return (j.get("detail") or "journal descope")[:120]
            except OSError:
                continue
    return None


def wire_evidence(event_id, state_dir="state"):
    """(has_report, crossable_unsent) — was there a chance we declined to take?

    Distinguishes the two ways an armed run sends nothing:
      * every intent found NO ask at or under its cap. The market was gone or
        priced above us; declining is correct, not a defect. TLN-26AUG05's one
        intent saw a 99¢ ask against a 97¢ cap, and `ev_gate` refusing that is
        the design (1¢ gross − 1¢ fee against a 2¢ minimum).
      * at least one intent DID have a crossable ask and still nothing reached
        the wire. That is worth a human look.
    """
    stem = LANE_SUFFIX.sub("", event_id)
    has_report, crossable = False, 0
    for d in _event_dirs(state_dir, stem):
        rp = _report(d)
        if not rp:
            continue
        try:
            r = json.load(open(rp))
        except (OSError, ValueError):
            continue
        has_report = True
        for o in r.get("orders") or []:
            ask = (o.get("best_ask") or [None])[0]
            cap = o.get("price_cents") or 0
            if ask is not None and cap > 1 and ask <= cap and o.get("send_ts") is None:
                crossable += 1
    return (has_report, crossable)


def run_health(event_id, state_dir="state"):
    """('healthy'|'died'|'never-armed'|'unknown', detail) for one staged event.

    An arm that fired and then died is not an arm that observed. AAOI-26AUG06
    armed, ran 32 s, lost every leg to a dead stream, and left a partial report
    with `exit.reason = all_legs_died` and no frame count — which the checker
    read as benign observing. Liveness of the JOB is not liveness of the RUN.
    """
    d = os.path.join(state_dir, event_id)
    if not os.path.isdir(d):
        return ("never-armed", "no state dir")
    rp = _report(d)
    if not rp:
        return ("unknown", "armed, no report.json")
    try:
        r = json.load(open(rp))
    except (OSError, ValueError):
        return ("unknown", "report.json unreadable")
    frames = int(r.get("frames_in") or 0)
    exit_rec = r.get("exit") or {}
    reason = exit_rec.get("reason") or ""
    if r.get("partial_report") or reason == "all_legs_died" or frames == 0:
        return ("died", f"exit={reason or 'unknown'} frames_in={frames}")
    return ("healthy", f"exit={reason or 'unknown'} frames_in={frames}")


# Best outcome first: one healthy lane means the issuer was heard.
_HEALTH_RANK = {"healthy": 0, "died": 1, "unknown": 2, "never-armed": 3}


def best_health(lanes, health):
    """Health across an issuer's LANES, naming the lane that achieved it.

    An issuer covered on two lanes is covered if EITHER heard the call. Grading
    on one arbitrarily-chosen lane is how HIMS 26AUG10 read as a clean
    single-lane observe while its phone lane was the one on the wire.
    """
    best = ("never-armed", "no lane staged")
    for ln in lanes or []:
        state, detail = health(ln["event_id"])
        if _HEALTH_RANK.get(state, 9) < _HEALTH_RANK.get(best[0], 9):
            m = LANE_SUFFIX.search(ln["event_id"])
            best = (state, f"{m.group(1) if m else ln['event_id']}: {detail}")
    return best


def orders_by_issuer(state_dir="state"):
    """issuer -> orders that actually reached Kalshi, from every report.json.

    NOT `len(orders)`. `orders` is the list of INTENTS the executor processed,
    including every non-fire early return (non-crossing, EV gate, dry-run, risk
    refusal). Counting it reported "23 orders placed" on 2026-08-05 when the
    true number was 0 — every one had short-circuited before `send_order`.
    `send_ts` is stamped only around the wire send, so it is the honest count.

    Newer reports carry a top-level `sent`; older ones are derived from the
    records so this still grades an archived run correctly.
    """
    out = {}
    for d in _event_dirs(state_dir):
        p = _report(d)
        if not p:
            continue
        try:
            r = json.load(open(p))
        except Exception:
            continue
        m = re.match(r"KXEARNINGSMENTION([A-Z0-9]+?)-", r.get("event_id") or "")
        if not m:
            continue
        sent = r.get("sent")
        if sent is None:
            sent = sum(1 for o in (r.get("orders") or []) if o.get("send_ts") is not None)
        out[m.group(1)] = out.get(m.group(1), 0) + sent
    return out


def reconcile(captures, orders, board=None, staged=None, health=None):
    """→ list of findings, worst first. `board`/`health` are injectable for tests.

    The population is `staged | captures`, not `captures` alone — see
    `staged_for`. `staged` defaults to {} so existing callers and the offline
    self-test keep their behaviour.
    """
    board = board or _bo.any_board
    staged = staged or {}
    health = health or (lambda ev: run_health(ev))
    findings = []
    for issuer in sorted(set(captures) | set(staged)):
        cap = captures.get(issuer, {"held_s": 0.0, "sources": []})
        st = staged.get(issuer)
        placed = orders.get(issuer, 0)
        if placed:
            continue
        # A recorded descope is a decision, not a failure (the MCD rule).
        why = descoped(st["event_id"]) if st else None
        if why:
            findings.append({"issuer": issuer, "verdict": "DESCOPED",
                             "reason": why, "orders": 0,
                             "config": st.get("config"), **cap})
            continue
        try:
            # Prefer the EVENT board when the staged config names one. Grading
            # against the series sums every mention event that issuer has ever
            # had — DKNG-26AUG07 traded 0 while its series carried 135,341.
            b = (event_board_activity(
                    st["event_id"],
                    st["activity_window_start_ts"],
                    st["activity_window_end_ts"],
                ) if st and board is _bo.any_board else board(issuer))
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

            # THREE ways to place nothing, and only one of them is benign.
            if st:
                state, detail = best_health(
                    st.get("armed_lanes") or [], health)
            else:
                state, detail = ("unknown", "not staged")
            if not armed_run and state in ("never-armed", "unknown") and not cap.get("sources"):
                # Staged, board traded, and nothing on disk at all — the
                # ABNB/LYFT shape. Previously invisible for want of a row.
                verdict = "MISSED-ARM"
            elif state == "died":
                # Armed, then lost the stream. AAOI ran 32 s and was graded
                # benign. A run that could not hear the call did not "observe".
                verdict = "ARMED-DIED"
            elif armed_run and st and st.get("fill_posture"):
                has_report, crossable = wire_evidence(st["event_id"])
                if not has_report:
                    # Armed at a fillable cap and left no report at all, so
                    # whether it traded CANNOT be answered from disk. MELI
                    # 26AUG05 filled at 90¢ and its record died with the run.
                    # "Unknown" must not be reported as "fine".
                    verdict = "ARMED-NO-RECORD"
                elif crossable:
                    # A crossable ask existed and nothing reached the wire.
                    verdict = "ARMED-NO-FILL"
                else:
                    # Every intent was priced above its cap — correct decline.
                    verdict = "ARMED-OBSERVING"
            elif armed_run:
                verdict = "ARMED-OBSERVING"
            else:
                verdict = "MISSED-ARM"

            findings.append({
                "issuer": issuer, "verdict": verdict,
                "markets": b.get("markets", 0), "volume": b.get("volume", 0.0),
                "orders": 0, "intents": intents,
                "run_state": state, "run_detail": detail,
                "config": (st or {}).get("armed_config") or (st or {}).get("config"),
                "fill_posture": (st or {}).get("fill_posture"),
                "lanes": [l["event_id"] for l in (st or {}).get("armed_lanes") or []],
                "unarmed_lanes": [
                    l["event_id"] for l in (st or {}).get("lanes") or []
                    if not l.get("armed")
                ],
                **cap})
    # Worst first: total misses, then runs that died, then armed-but-silent at
    # a fillable cap, then designed observes.
    rank = {"MISSED-ARM": 0, "ARMED-DIED": 1, "ARMED-NO-FILL": 2,
            "ARMED-NO-RECORD": 3, "ARMED-OBSERVING": 4, "DESCOPED": 5,
            "UNDETERMINED": 6}
    findings.sort(key=lambda f: (rank.get(f["verdict"], 9), -f.get("volume", 0)))
    return findings


def findings_exit(findings):
    if any(f.get("verdict") in BAD_VERDICTS for f in findings):
        return 1
    if any(f.get("verdict") == "UNDETERMINED" for f in findings):
        return 2
    return 0


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
    assert findings_exit(f2) == 2, "UNDETERMINED must remain scheduler-visible"

    # short legs must not count as coverage (PGR legs 07..23 were ~12.3 s)
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        json.dump({"held_ms": 12300}, open(os.path.join(d, "pgr_2026_08_04_leg07.json"), "w"))
        json.dump({"held_ms": 598400}, open(os.path.join(d, "spot_2026_08_04.json"), "w"))
        json.dump({"held_ms": 600000}, open(os.path.join(d, "ttsrehearsal_2026_08_04.json"), "w"))
        c = captures_for("2026-08-04", probe_dir=d, state_dir=os.path.join(d, "none"))
        assert set(c) == {"SPOT"}, f"short/rehearsal probe counted as capture: {c}"

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
    # ── 2026-08-07: the denominator, the posture, and the descope ───────────
    # 2026-08-06 was graded "0 MISSED-ARM" on a day that missed ABNB and LYFT
    # outright and armed AAOI into a dead stream. Three separate defects.
    traded = lambda _i: {"traded": True, "markets": 12, "volume": 50000.0}

    with tempfile.TemporaryDirectory() as d:
        cdir, sdir = os.path.join(d, "configs"), os.path.join(d, "state")
        os.makedirs(cdir)

        def mkcfg(iss, fill):
            cap = 97 if fill else 1
            open(os.path.join(cdir, f"webcast-{iss.lower()}-26aug06-arm-wc.toml"),
                 "w").write(
                f'[event]\nid = "KXEARNINGSMENTION{iss}-26AUG06-WEBCAST"\n'
                f'[[keywords]]\nphrase = "x"\nmax_price_cents = {cap}\n')

        for iss, fill in (("ABNB", True), ("AAOI", True), ("NBIS", True),
                          ("OBSV", False)):
            mkcfg(iss, fill)
        st = staged_for("2026-08-06", config_dir=cdir, state_dir=sdir,
                        runs_dir=os.path.join(d, "runs"))
        assert set(st) == {"ABNB", "AAOI", "NBIS", "OBSV"}, st
        # Posture is READ, never assumed: the old code cited a "1¢ observe"
        # posture for events armed at a 97¢ fill cap.
        assert st["ABNB"]["declared_fill_posture"] is True
        assert st["OBSV"]["declared_fill_posture"] is False
        assert st["ABNB"]["fill_posture"] is None, \
            "a never-armed config must not supply armed posture"

        # AAOI: armed, then all legs died. Not "observing".
        os.makedirs(os.path.join(sdir, st["AAOI"]["event_id"]))
        json.dump({"event_id": st["AAOI"]["event_id"], "partial_report": True,
                   "exit": {"reason": "all_legs_died"}, "orders": []},
                  open(os.path.join(sdir, st["AAOI"]["event_id"], "report.json"), "w"))
        # NBIS: descoped by journal line only — no DESCOPED.json.
        os.makedirs(os.path.join(sdir, st["NBIS"]["event_id"]))
        open(os.path.join(sdir, st["NBIS"]["event_id"], "journal.jsonl"), "w").write(
            json.dumps({"action": "descope", "outcome": "ok",
                        "detail": "call is 2026-08-13"}) + "\n")

        st = staged_for("2026-08-06", config_dir=cdir, state_dir=sdir,
                        runs_dir=os.path.join(d, "runs"))
        assert not st["ABNB"]["armed_lanes"], st["ABNB"]
        assert st["AAOI"]["armed_lanes"], st["AAOI"]

        health = lambda ev: run_health(ev, state_dir=sdir)
        desc_ok = descoped(st["NBIS"]["event_id"], state_dir=sdir)
        assert desc_ok, "journal-only descope not recognised"
        assert descoped(st["ABNB"]["event_id"], state_dir=sdir) is None

        caps = {"AAOI": {"held_s": 0.0, "frames_in": 0,
                         "sources": [os.path.join(sdir, "x", "journal.jsonl")]}}
        f = {x["issuer"]: x for x in reconcile(caps, {}, board=traded,
                                               staged=st, health=health)}
        # ABNB was staged, never armed, left nothing on disk — previously
        # invisible because the loop iterated over captures.
        assert f["ABNB"]["verdict"] == "MISSED-ARM", f["ABNB"]
        assert f["AAOI"]["verdict"] == "ARMED-DIED", f["AAOI"]
        assert f["NBIS"]["verdict"] == "DESCOPED", f["NBIS"]
        # An issuer that placed orders is never a finding.
        f2 = reconcile(caps, {"ABNB": 1}, board=traded, staged=st, health=health)
        assert "ABNB" not in {x["issuer"] for x in f2}

    # ── 2026-08-11: TWO LANES, and the box-hosted one is where the trade was ──
    # HIMS 26AUG10 was graded "1 staged, 0 orders, 1c observing posture" on a day
    # its PHONE lane put 2 orders on the wire and spent 194c. Three defects, all
    # of which had to be true at once for the day to read clean.
    with tempfile.TemporaryDirectory() as d:
        cdir, sdir = os.path.join(d, "configs"), os.path.join(d, "state")
        os.makedirs(cdir)
        os.makedirs(sdir)
        WC = "KXEARNINGSMENTIONHIMS-26AUG10-WEBCAST"
        PH = "KXEARNINGSMENTIONHIMS-26AUG10-PHONE"
        # D1: the name phone-lane-derive.py ACTUALLY writes. The old glob
        # `phone-*-26aug10-arm.toml` matched nothing, so this lane was never in
        # the denominator.
        open(os.path.join(cdir, "webcast-hims-26aug10-arm-wc.toml"), "w").write(
            f'[event]\nid = "{WC}"\n[[keywords]]\nphrase = "x"\nmax_price_cents = 97\n')
        open(os.path.join(cdir, "hims-26aug10-phone-arm.toml"), "w").write(
            f'[event]\nid = "{PH}"\n[[keywords]]\nphrase = "x"\nmax_price_cents = 97\n')
        st = staged_for("2026-08-10", config_dir=cdir, state_dir=sdir,
                        runs_dir=os.path.join(d, "runs"))
        assert set(st) == {"HIMS"}, st
        # D2: both lanes kept, not one silently overwritten by the other.
        assert [l["event_id"] for l in st["HIMS"]["lanes"]] == [WC, PH], st["HIMS"]

        # The webcast lane ran on the LAPTOP: report at the canonical path.
        os.makedirs(os.path.join(sdir, WC))
        json.dump({"event_id": WC, "frames_in": 297398, "sent": 0,
                   "exit": {"reason": "clean_drain"}, "orders": []},
                  open(os.path.join(sdir, WC, "report.json"), "w"))
        # D3: the phone lane ran on the BOX, so its artifacts exist ONLY under
        # box/ after the documented pull. Globbing state/*/report.json one level
        # deep could never see it.
        os.makedirs(os.path.join(sdir, PH, "box"))
        json.dump({"event_id": PH, "frames_in": 140439, "sent": 2,
                   "exit": {"reason": "clean_drain"}, "orders": []},
                  open(os.path.join(sdir, PH, "box", "report.json"), "w"))
        open(os.path.join(sdir, PH, "box", "journal.jsonl"), "w").write(
            json.dumps({"action": "arm-start", "outcome": "info"}) + "\n")

        st = staged_for("2026-08-10", config_dir=cdir, state_dir=sdir,
                        runs_dir=os.path.join(d, "runs"))
        assert all(lane["armed"] for lane in st["HIMS"]["lanes"]), st["HIMS"]

        assert orders_by_issuer(state_dir=sdir) == {"HIMS": 2}, \
            orders_by_issuer(state_dir=sdir)
        c = captures_for("2026-08-10", probe_dir=os.path.join(d, "none"), state_dir=sdir)
        # Both lanes' audio counts toward the issuer.
        assert c["HIMS"]["frames_in"] == 297398 + 140439, c
        # Health is the BEST lane, and it names which one.
        assert run_health(PH, state_dir=sdir)[0] == "healthy"
        hb = best_health(st["HIMS"]["lanes"], lambda ev: run_health(ev, state_dir=sdir))
        assert hb[0] == "healthy" and "WEBCAST" in hb[1], hb
        # …and an issuer on the wire is never flagged.
        assert not reconcile(c, orders_by_issuer(state_dir=sdir), board=traded,
                             staged=st,
                             health=lambda ev: run_health(ev, state_dir=sdir))

        # A box report must be counted ONCE even when a stale canonical copy
        # also exists — box/ wins, and nothing double-counts.
        json.dump({"event_id": PH, "frames_in": 1, "sent": 99, "orders": []},
                  open(os.path.join(sdir, PH, "report.json"), "w"))
        assert orders_by_issuer(state_dir=sdir) == {"HIMS": 2}, \
            "box copy must win and must not double-count"

        # sweep-hft shares the account, never the numerator.
        sw = os.path.join(sdir, "KXEARNINGSMENTIONHIMS-26AUG10-SWEEP")
        os.makedirs(sw)
        json.dump({"event_id": "KXEARNINGSMENTIONHIMS-26AUG10-SWEEP", "sent": 40,
                   "frames_in": 5, "orders": []},
                  open(os.path.join(sw, "report.json"), "w"))
        assert orders_by_issuer(state_dir=sdir) == {"HIMS": 2}, \
            "sweep run leaked into mention-lane orders"

    # ── 2026-08-12 M5: only artifact-bound lanes supply armed posture ───────
    with tempfile.TemporaryDirectory() as d:
        cdir, sdir = os.path.join(d, "configs"), os.path.join(d, "state")
        os.makedirs(cdir)
        actual = "KXEARNINGSMENTIONRAWX-26AUG11-WEBCAST"
        # Deliberately the SAME event id: legacy state proves the event ran,
        # while only RunPlan can say which of two configs supplied it.
        prepared_only = actual
        actual_config = os.path.join(cdir, "webcast-rawx-26aug11-arm-wc.toml")
        raw_config = os.path.join(cdir, "twilio-rawx-26aug11-arm.toml")
        open(actual_config, "w").write(
            f'[event]\nid = "{actual}"\n[[keywords]]\nphrase = "x"\nmax_price_cents = 97\n')
        # This deliberately matches an arm-config filename pattern. Its 1c
        # posture must still be inert because it never produced run evidence.
        open(raw_config, "w").write(
            f'[event]\nid = "{prepared_only}"\n[[keywords]]\nphrase = "x"\nmax_price_cents = 1\n')
        os.makedirs(os.path.join(sdir, actual))
        open(os.path.join(sdir, actual, "journal.jsonl"), "w").write(
            json.dumps({"action": "arm-start", "outcome": "info"}) + "\n")
        plan_dir = os.path.join(d, "runs", actual, "fixture", "control")
        os.makedirs(plan_dir)
        json.dump({"config_path": actual_config},
                  open(os.path.join(plan_dir, "run-plan.json"), "w"))
        st = staged_for("2026-08-11", config_dir=cdir, state_dir=sdir,
                        runs_dir=os.path.join(d, "runs"))["RAWX"]
        assert [lane["config"] for lane in st["armed_lanes"]] == [
            os.path.relpath(actual_config)], st
        assert st["fill_posture"] is True, \
            "never-armed 1c prepare output changed the armed posture"
        assert next(l for l in st["lanes"] if l["config"] == os.path.relpath(raw_config))["armed"] is False
        loud = reconcile(
            {"RAWX": {"held_s": 0.0, "frames_in": 1,
                       "sources": [os.path.join(sdir, actual, "journal.jsonl")]}},
            {}, board=traded, staged={"RAWX": st},
            health=lambda _ev: ("healthy", "fixture"))
        assert loud[0]["verdict"] != "ARMED-OBSERVING", loud

    # ETOR shape: activity earlier in the audit window remains activity even
    # when the final hourly sample is zero and a point-in-time open check is no.
    requested = []
    def etor_history(url):
        requested.append(url)
        return {
            "market_tickers": ["ETOR-A", "ETOR-B"],
            "market_candlesticks": [
                [{"volume_fp": "26496"}, {"volume_fp": "0"}],
                [{"volume_fp": "0"}],
            ],
        }
    etor = event_board_activity(
        "KXEARNINGSMENTIONETOR-26AUG11-WEBCAST",
        1786406400, 1786492800, fetch=etor_history)
    assert etor["traded"] and etor["volume"] == 26496.0, etor
    assert etor["active_markets"] == 1 and "period_interval=60" in requested[0]

    print("missed-arm-check --check: OK (SPOT/PGR flagged, armed run silent, "
          "untraded board ignored, volume ranking, fail-loud, short-leg floor, "
          "staged denominator, posture from config, died≠observing, descope "
          "honoured, phone-lane config named, both lanes kept, box-hosted run "
          "visible + counted once, sweep excluded, never-armed config inert, "
          "traded-earlier-in-window retained)")


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
    deferred = []
    staged = staged_for(a.date, deferred=deferred)
    # No captures is NOT the same as nothing to grade. If configs were staged
    # for the date, a day with zero capture evidence is the loudest possible
    # finding, not an early return — that early return is how 2026-08-06's
    # never-armed events left no trace.
    if not caps and not staged:
        print(f"missed-arm-check: no capture evidence and no staged configs for {a.date}")
        return 0
    placed = orders_by_issuer()
    findings = reconcile(caps, placed, staged=staged)
    if a.json:
        print(json.dumps(findings, indent=1))
        return findings_exit(findings)

    missed = [f for f in findings if f["verdict"] == "MISSED-ARM"]
    died = [f for f in findings if f["verdict"] == "ARMED-DIED"]
    nofill = [f for f in findings if f["verdict"] == "ARMED-NO-FILL"]
    norec = [f for f in findings if f["verdict"] == "ARMED-NO-RECORD"]
    undet = [f for f in findings if f["verdict"] == "UNDETERMINED"]
    observing = [f for f in findings if f["verdict"] == "ARMED-OBSERVING"]
    desc = [f for f in findings if f["verdict"] == "DESCOPED"]
    # Lanes, not just issuers: one issuer covered on both webcast and phone is
    # TWO commitments, and reporting "1 staged" understated exactly the day
    # whose second lane was the one that traded.
    n_lanes = sum(len(v.get("armed_lanes") or []) for v in staged.values())
    committed_lanes = sum(len(v.get("lanes") or [1]) for v in staged.values())
    lane_note = f" ({n_lanes}/{committed_lanes} lanes armed)"
    print(f"missed-arm-check {a.date}: {len(staged)} staged{lane_note}, "
          f"{len(caps)} captured, "
          f"{len(missed)} MISSED-ARM, {len(died)} ARMED-DIED, "
          f"{len(nofill)} ARMED-NO-FILL, {len(norec)} ARMED-NO-RECORD, "
          f"{len(observing)} ARMED-OBSERVING, "
          f"{len(desc)} DESCOPED, {len(undet)} undetermined"
          + (f", {len(deferred)} deferred" if deferred else ""))
    for d in deferred:
        print(f"   deferred: {d['issuer']} — {d['config']} declares "
              f"{d['declared_date']}, graded on that date, not {a.date}")

    # An issuer that reached the wire produces NO finding (reconcile skips it),
    # so without this a fully successful day is indistinguishable from a day
    # where nothing was staged at all. The tool already prints the good news for
    # graded verdicts; this is the good news that never had a row.
    for issuer in sorted(set(staged) | set(caps)):
        n = placed.get(issuer, 0)
        if not n:
            continue
        lanes = [l["event_id"] for l in (staged.get(issuer) or {}).get("lanes") or []]
        where = ""
        if lanes:
            tags = [(LANE_SUFFIX.search(e).group(1) if LANE_SUFFIX.search(e) else e)
                    for e in lanes]
            where = f" across {len(lanes)} lane(s): {', '.join(tags)}"
        print(f"\n✓ ON THE WIRE: {issuer} — {n} order(s) with send_ts{where}")

    for f in desc:
        print(f"\n– DESCOPED: {f['issuer']} — {f['reason']}")
    # Print the good news too: an operator who only ever sees this tool shout
    # cannot tell a silent success from a silent failure.
    for f in missed + died + nofill + norec + observing:
        heard = (f"{f['held_s']:.0f}s of audio" if f.get("held_s")
                 else f"{f.get('frames_in', 0)} frames")
        board = (f"Board traded {f['volume']:.0f} contracts / "
                 f"{f['markets']} markets.")
        if f["verdict"] == "ARMED-OBSERVING":
            print(f"\n✓ ARMED-OBSERVING: {f['issuer']} — the arm ran and produced "
                  f"{f.get('intents', 0)} decision intent(s); 0 orders is the "
                  f"1c non-crossing posture declining asks above cap, not a miss. "
                  f"{board}")
            print(f"   posture read from {f.get('config') or 'no config'}: "
                  f"every keyword at a 1c observe cap")
            print(f"   evidence: {', '.join(f['sources'][:4])}")
        elif f["verdict"] == "ARMED-DIED":
            print(f"\n⛔ ARMED-DIED: {f['issuer']} — the arm fired and then lost "
                  f"the stream ({f.get('run_detail')}). It could not hear the "
                  f"call, so 0 orders is NOT observing. {board}")
            print(f"   evidence: {', '.join(f['sources'][:4])}")
        elif f["verdict"] == "ARMED-NO-RECORD":
            print(f"\n⛔ ARMED-NO-RECORD: {f['issuer']} — armed at a FILLABLE cap "
                  f"({f.get('config')}) and left NO report.json, so whether it "
                  f"traded cannot be answered from disk. {board}")
            print(f"   evidence: {', '.join(f['sources'][:4])}")
        elif f["verdict"] == "ARMED-NO-FILL":
            print(f"\n⛔ ARMED-NO-FILL: {f['issuer']} — armed at a FILLABLE cap "
                  f"({f.get('config')}), heard {heard}, produced "
                  f"{f.get('intents', 0)} intent(s) and sent nothing. {board}")
            print(f"   evidence: {', '.join(f['sources'][:4])}")
        else:
            where = (f"captured {heard}" if f.get("sources")
                     else "left NO capture, NO run dir and NO report")
            print(f"\n⛔ MISSED-ARM: {f['issuer']} — {where}, placed 0 "
                  f"orders, and its board traded {f['volume']:.0f} contracts "
                  f"across {f['markets']} markets.")
            if f.get("config"):
                print(f"   staged as {f['config']} — so this was intended coverage")
            if f.get("sources"):
                print(f"   evidence: {', '.join(f['sources'][:4])}"
                      + (f" (+{len(f['sources']) - 4} more)" if len(f["sources"]) > 4 else ""))
    for f in undet:
        print(f"\n?  UNDETERMINED: {f['issuer']} — {f['reason']}")
    if missed:
        print("\n⇒ A board traded and we put nothing on it. Either the arm path")
        print("  was never launched, or it launched and placed nothing.")
        print("  Check: is there a state/KXEARNINGSMENTION<ISSUER>-* run dir at all?")
    if died:
        print("\n⇒ An arm that dies mid-call reports the same silence as one that")
        print("  works. Check the stream URL and the leg exit in launch.log.")
    return findings_exit(findings)


if __name__ == "__main__":
    sys.exit(main())
