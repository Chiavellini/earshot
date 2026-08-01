#!/usr/bin/env python3
r"""Slate content diff + staging gate — the extracted, testable form of the
inline python heredoc that lives in scripts/slate-daily.sh (lines ~47-91).

WHY EXTRACTED. Two defects shipped inside that heredoc precisely because a
heredoc cannot be unit-tested:

  1. FALSE UNSTAGED (2026-07-30). The staging check globbed
     `webcast-<lc>-*-arm.toml`, but the week's staged webcast configs are
     named `webcast-<lc>-*-arm-wc.toml`. The glob matched nothing, so the
     job false-fired UNSTAGED on ALL FIVE 07-30 events (AAPL, AMZN, RACE,
     RDDT, YUM) that were in fact fully staged. Fixed: the glob is now
     `webcast-<lc>-*-arm*.toml` (matches both suffixes), and --self-test
     pins the regression with touched files in a tmpdir so the pattern can
     never silently narrow again.

  2. NO SANITY FLOOR. discover-events.py's own docstring records 2026-07-27,
     when the paginated sweep was observed INCOMPLETE once (BA absent from an
     8,000-event sweep). Had a degraded API returned half a slate, the old
     block would have printed a wall of CHANGE: REMOVED lines and slate-daily
     would have COMMITTED the amputated slate as truth — every downstream
     consumer (recon-worklist, the changes/ archive, the humans reading the
     alert) would inherit the hole. Fixed: SANITY-FLOOR below.

  Also new here (not in the old block): UNSTAGED is now LANE-AWARE. The old
  single UNSTAGED line only knew about the webcast lane; phone-first capture
  (memory: ~20s faster when a bridge holds) had no staging check at all. An
  issuer whose dial-ins registry entry proved `admission = "held"` could sit
  unstaged on the phone lane forever without a peep.

CONTRACT WITH slate-daily.sh (for the wiring change, frozen until 23:30Z
2026-07-30 — this file is new and safe today):

  exit 0  normal report (possibly empty).
  exit 3  SANITY-FLOOR tripped. ALL other report lines are still printed
          first, then the SANITY-FLOOR: line. slate-daily.sh must still
          alert with the full report but SKIP the auto-commit of slate/ —
          a floored slate is evidence, never truth.
  exit 2  SLATE-DIFF-ERROR: printed to stderr — unreadable/invalid/malformed
          inputs, a --configs-dir that does not exist or is not a directory,
          `--new -` (stdin is only valid for --old), and ANY unexpected
          internal error: the traceback is condensed into the single tagged
          line (truncated ~200 chars), NEVER printed raw with exit 1. The
          wrapper does not touch SystemExit, so exit 3 is never masked.
          Lands in slate-daily's existing `|| fail` alert path.

GREP CONTRACT (stdout; every line greppable, no silent outcomes):
  ^(CHANGE|INITIAL|BOEING-CLASS|UNRESOLVED-DATE):   wording IDENTICAL to the
      old inline block — slate-daily's existing grep and the slate/changes/
      archive format keep working unchanged.
  ^UNSTAGED\(webcast\):  /  ^UNSTAGED\(phone\):
      NOTE: the old grep '^UNSTAGED:' will NOT match the lane-tagged form.
      When slate-daily.sh adopts this script it must grep '^UNSTAGED(' .
  ^SANITY-FLOOR:   refusal line, printed last, paired with exit 3.
  ^SLATE-DIFF-ERROR:   (stderr) input/operator failure or wrapped internal
      error — always a single line, paired with exit 2.

SANITY-FLOOR RULE (integer-exact, no float thresholds):
  prev_n = event count in --old, new_n = event count in --new,
  removed = number of CHANGE: REMOVED lines. If prev_n > 0 and
      removed >= max(5, ceil(0.30 * prev_n))   or   new_n < 0.5 * prev_n
  then print SANITY-FLOOR and exit 3. A normal heavy-turnover day does not
  trip it: 2026-07-29 was 56 -> 50 with 7 removals (threshold 17).

Usage:
  slate-diff.py --new slate/latest.json --old <path | - for stdin>
      [--configs-dir configs] [--registry configs/dial-ins.toml]
      [--horizon-days 7]
  slate-diff.py --self-test        # offline frozen fixtures, prints self-test OK

An EMPTY --old (what slate-daily passes on a repo with no committed slate)
means first run -> INITIAL. A MISSING --old file is treated as a bug (exit
2), never as a first run: masking a lost baseline as INITIAL would silently
re-baseline the diff.
"""
import argparse
import datetime
import json
import sys
import tomllib
import traceback
from pathlib import Path

EXIT_OK = 0
EXIT_INPUT = 2   # SLATE-DIFF-ERROR — distinct from the floor so slate-daily
EXIT_FLOOR = 3   # can tell "inputs broken" from "slate suspicious"


def die(msg):
    print(f"SLATE-DIFF-ERROR: {msg}", file=sys.stderr)
    sys.exit(EXIT_INPUT)


def evmap(slate):
    return {e["event_ticker"]: e for e in slate.get("events", [])}


def lane_unstaged(issuer, call_date, configs_dir, registry):
    """UNSTAGED lines for one in-horizon event, per capture lane.

    webcast: fires when NO `webcast-<lc>-*-arm*.toml` exists. The `-arm*`
    (not `-arm`) is the 2026-07-30 fix — it must match both `-arm.toml`
    and `-arm-wc.toml`.
    phone:   fires ONLY when the dial-ins registry proves the bridge held a
    silent caller (`admission = "held"`) AND no `twilio-<lc>-*-arm*.toml`
    exists. Any other admission value (operator_screened / untested / absent)
    means the phone lane is not expected to be staged, so no line — an
    UNSTAGED(phone) on an operator-screened bridge would be noise that
    trains people to ignore the tag.
    """
    lines = []
    lc = issuer.lower()
    cdir = Path(configs_dir)
    if not list(cdir.glob(f"webcast-{lc}-*-arm*.toml")):
        lines.append(
            f"UNSTAGED(webcast): {issuer} calls {call_date} — "
            f"no {configs_dir}/webcast-{lc}-*-arm*.toml"
        )
    entry = registry.get(issuer)
    entry = entry if isinstance(entry, dict) else None
    if entry is not None and entry.get("admission") == "held":
        if not list(cdir.glob(f"twilio-{lc}-*-arm*.toml")):
            lines.append(
                f"UNSTAGED(phone): {issuer} calls {call_date} — "
                f"no {configs_dir}/twilio-{lc}-*-arm*.toml (admission=held)"
            )
    return lines


def analyze(new, prev, *, today, configs_dir, registry, horizon_days):
    """Full report. Returns (lines, floor_tripped).

    Line wording for CHANGE/INITIAL/BOEING-CLASS/UNRESOLVED-DATE is byte-for-
    byte the old inline block's — do not 'improve' it, greps depend on it.
    """
    lines = []
    week = today + datetime.timedelta(days=horizon_days)
    nm = evmap(new)
    pm = evmap(prev) if prev is not None else {}
    removed = 0

    if prev is None:
        lines.append(f"INITIAL: first committed slate — {len(nm)} events")
    else:
        for tk in sorted(set(nm) - set(pm)):
            e = nm[tk]
            lines.append(
                f"CHANGE: ADDED {tk} ({e.get('issuer')}) call {e.get('call_date')}"
            )
        for tk in sorted(set(pm) - set(nm)):
            lines.append(f"CHANGE: REMOVED {tk}")
            removed += 1
        for tk in sorted(set(nm) & set(pm)):
            a, b = pm[tk].get("call_date"), nm[tk].get("call_date")
            if a != b:
                lines.append(f"CHANGE: DATE {tk} {a} -> {b}")

    for tk in new.get("sweep_delta", {}).get("per_series_only", []):
        lines.append(f"BOEING-CLASS: {tk} found by per-series sweep ONLY")

    for e in new.get("events", []):
        cd = e.get("call_date")
        tk = e["event_ticker"]
        if not cd:
            if tk not in pm:  # alert once, when it first appears — not every day
                lines.append(
                    f"UNRESOLVED-DATE: {tk} — sub_title did not parse; pin the date by hand"
                )
            continue
        try:
            d = datetime.date.fromisoformat(cd)
        except ValueError:
            die(f"event {tk} has unparseable call_date {cd!r} in --new")
        if today <= d <= week:
            issuer = e.get("issuer") or ""
            if issuer:
                lines.extend(lane_unstaged(issuer, cd, configs_dir, registry))

    # ── SANITY-FLOOR ────────────────────────────────────────────────────────
    # Integer-exact forms: ceil(0.30*n) == (3n+9)//10, new_n < 0.5*prev_n
    # <=> 2*new_n < prev_n. No floats: 0.30*prev_n in binary float lands a
    # hair above/below the true value and would move the boundary.
    prev_n = len(prev.get("events", [])) if prev is not None else 0
    new_n = len(new.get("events", []))
    floor = False
    if prev_n > 0:
        threshold = max(5, (3 * prev_n + 9) // 10)
        if removed >= threshold or 2 * new_n < prev_n:
            floor = True
            lines.append(
                f"SANITY-FLOOR: refusing commit — removed={removed} "
                f"prev={prev_n} new={new_n} (possible API degradation)"
            )
    return lines, floor


def load_slate(path, flag):
    if path == "-":
        raw = sys.stdin.read()
    else:
        p = Path(path)
        if not p.is_file():
            die(
                f"--{flag} {path} does not exist (an EMPTY file means INITIAL; "
                f"a MISSING one is a bug, not a first run)"
            )
        raw = p.read_text()
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        die(f"--{flag} {path} is not valid JSON: {exc}")


def load_registry(path):
    p = Path(path)
    if not p.is_file():
        die(f"registry {path} not found — cannot evaluate phone-lane UNSTAGED")
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        die(f"registry {path} failed to parse: {exc}")


# ── self-test: frozen fixtures, no network, no repo state ───────────────────
def self_test():
    import subprocess
    import tempfile

    today = datetime.date(2026, 7, 29)  # frozen — the 56->50 real day

    def ev(tk, issuer, cd):
        return {"event_ticker": tk, "issuer": issuer, "call_date": cd}

    def slate(events, per_series_only=()):
        return {"events": events, "sweep_delta": {"per_series_only": list(per_series_only)}}

    def run(new, prev, configs_dir, registry, horizon_days=7):
        return analyze(
            new, prev, today=today, configs_dir=configs_dir,
            registry=registry, horizon_days=horizon_days,
        )

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td)
        # CMG staged ONLY as -arm-wc.toml — the exact 07-30 false-fire shape.
        (cfg / "webcast-cmg-26jul29-arm-wc.toml").touch()
        # AAPL staged the classic way; also a staged phone config for CMG.
        (cfg / "webcast-aapl-26jul30-arm.toml").touch()
        (cfg / "twilio-cmg-26jul29-arm.toml").touch()

        registry = {
            "CMG": {"admission": "held"},               # held + phone staged
            "AAPL": {"admission": "held"},              # held + phone UNstaged
            "YUM": {"admission": "operator_screened"},  # not held -> never phone-flag
            # RDDT deliberately absent from the registry.
        }

        # 1. diff lines: added / removed / date-change / boeing / unresolved.
        prev = slate([
            ev("KXEARNINGSMENTIONA-26AUG01", "A", "2026-08-01"),
            ev("KXEARNINGSMENTIONB-26AUG02", "B", "2026-08-20"),
            ev("KXEARNINGSMENTIONC-26AUG03", "C", "2026-08-21"),
        ])
        new = slate(
            [
                ev("KXEARNINGSMENTIONA-26AUG01", "A", "2026-08-01"),
                ev("KXEARNINGSMENTIONB-26AUG02", "B", "2026-08-22"),
                ev("KXEARNINGSMENTIOND-26AUG04", "D", "2026-08-23"),
                ev("KXEARNINGSMENTIONE-26AUG05", "E", None),
            ],
            per_series_only=["KXEARNINGSMENTIONBA-26JUL21"],
        )
        lines, floor = run(new, prev, cfg, registry)
        assert "CHANGE: ADDED KXEARNINGSMENTIOND-26AUG04 (D) call 2026-08-23" in lines, lines
        assert "CHANGE: REMOVED KXEARNINGSMENTIONC-26AUG03" in lines, lines
        assert "CHANGE: DATE KXEARNINGSMENTIONB-26AUG02 2026-08-20 -> 2026-08-22" in lines, lines
        assert "BOEING-CLASS: KXEARNINGSMENTIONBA-26JUL21 found by per-series sweep ONLY" in lines, lines
        assert (
            "UNRESOLVED-DATE: KXEARNINGSMENTIONE-26AUG05 — sub_title did not parse; "
            "pin the date by hand" in lines
        ), lines
        assert not floor
        # UNRESOLVED-DATE fires once: with the dateless event already in prev, silence.
        lines2, _ = run(new, slate(new["events"]), cfg, registry)
        assert not any(l.startswith("UNRESOLVED-DATE:") for l in lines2), lines2

        # INITIAL wording (prev None).
        lines, floor = run(new, None, cfg, registry)
        assert lines[0] == "INITIAL: first committed slate — 4 events", lines
        assert not floor

        # 2. false-UNSTAGED regression + lane semantics, all in horizon.
        week_events = slate([
            ev("KXEARNINGSMENTIONCMG-26JUL29", "CMG", "2026-07-29"),
            ev("KXEARNINGSMENTIONAAPL-26JUL30", "AAPL", "2026-07-30"),
            ev("KXEARNINGSMENTIONYUM-26JUL30", "YUM", "2026-07-30"),
            ev("KXEARNINGSMENTIONRDDT-26JUL30", "RDDT", "2026-07-30"),
        ])
        lines, floor = run(week_events, slate(week_events["events"]), cfg, registry)
        assert not floor
        # CMG is staged as -arm-wc.toml: MUST NOT fire (the 07-30 regression).
        assert not any(l.startswith("UNSTAGED(webcast): CMG") for l in lines), lines
        # AAPL staged as plain -arm.toml: still matched by -arm*.toml.
        assert not any(l.startswith("UNSTAGED(webcast): AAPL") for l in lines), lines
        # YUM and RDDT have no webcast config at all: must fire, exact wording.
        assert (
            f"UNSTAGED(webcast): YUM calls 2026-07-30 — no {cfg}/webcast-yum-*-arm*.toml"
            in lines
        ), lines
        assert any(l.startswith("UNSTAGED(webcast): RDDT calls 2026-07-30") for l in lines), lines

        # 3. phone lane: only-when-held, and only-when-unstaged.
        phone = [l for l in lines if l.startswith("UNSTAGED(phone):")]
        assert any(l.startswith("UNSTAGED(phone): AAPL calls 2026-07-30") for l in phone), lines
        assert not any("CMG" in l for l in phone), lines   # held but twilio staged
        assert not any("YUM" in l for l in phone), lines   # operator_screened, not held
        assert not any("RDDT" in l for l in phone), lines  # no registry entry

        # Out-of-horizon events never fire UNSTAGED.
        far = slate([ev("KXEARNINGSMENTIONYUM-26SEP30", "YUM", "2026-09-30")])
        lines, _ = run(far, slate(far["events"]), cfg, registry)
        assert not any(l.startswith("UNSTAGED(") for l in lines), lines

        # 4. SANITY-FLOOR at the exact boundary. prev_n=10 -> threshold
        # max(5, ceil(3.0)) = 5.
        prev10 = slate([ev(f"KXEARNINGSMENTIONT{i}-26SEP01", f"T{i}", "2026-09-01")
                        for i in range(10)])
        keep5 = slate(prev10["events"][:5])   # removed=5 == threshold: trips
        lines, floor = run(keep5, prev10, cfg, registry)
        assert floor and lines[-1] == (
            "SANITY-FLOOR: refusing commit — removed=5 prev=10 new=5 "
            "(possible API degradation)"
        ), lines
        keep6 = slate(prev10["events"][:6])   # removed=4 < threshold: clean
        lines, floor = run(keep6, prev10, cfg, registry)
        assert not floor and not any(l.startswith("SANITY-FLOOR:") for l in lines), lines

        # new_n arm: prev_n=6, removed=4 (< 5) but new_n=2 < 0.5*6: trips.
        prev6 = slate(prev10["events"][:6])
        keep2 = slate(prev10["events"][:2])
        lines, floor = run(keep2, prev6, cfg, registry)
        assert floor, lines

        # 5. floor does NOT trip on the real 2026-07-29 shape: 56 -> 50,
        # 7 removed + 1 added (threshold is 17).
        prev56 = slate([ev(f"KXEARNINGSMENTIONX{i}-26SEP01", f"X{i}", "2026-09-01")
                        for i in range(56)])
        new50 = slate(prev56["events"][:49]
                      + [ev("KXEARNINGSMENTIONNEW-26SEP02", "NEW", "2026-09-02")])
        lines, floor = run(new50, prev56, cfg, registry)
        assert not floor, lines
        assert sum(1 for l in lines if l.startswith("CHANGE: REMOVED")) == 7, lines

        # 6. registry TOML loading path (same shape as configs/dial-ins.toml).
        reg_path = cfg / "dial-ins.toml"
        reg_path.write_text(
            '[CMG]\nadmission = "held"\n\n[YUM]\nadmission = "operator_screened"\n'
        )
        loaded = load_registry(reg_path)
        assert loaded["CMG"]["admission"] == "held"
        assert loaded["YUM"]["admission"] == "operator_screened"

        # 7. CLI exit contract (subprocess — the tag/exit pairs slate-daily
        #    greps on). Malformed/misused inputs must be ONE tagged stderr
        #    line + exit 2 — never a raw multi-line traceback with exit 1 —
        #    and the wrapper must not mask the floor's exit 3.
        script = str(Path(__file__).resolve())

        def cli(*argv, stdin_data=""):
            return subprocess.run(
                [sys.executable, script, *argv],
                input=stdin_data, capture_output=True, text=True,
            )

        common = ["--configs-dir", str(cfg), "--registry", str(reg_path)]
        ok_new = cfg / "cli-new.json"
        ok_new.write_text(json.dumps(week_events))

        # malformed --new (top-level list): AttributeError inside analyze
        # must come out as the tag, exit 2, single stderr line.
        bad_new = cfg / "cli-bad.json"
        bad_new.write_text('[{"event_ticker": "X"}]')
        r = cli("--new", str(bad_new), "--old", "-", *common)
        assert r.returncode == EXIT_INPUT, (r.returncode, r.stderr)
        assert r.stderr.startswith("SLATE-DIFF-ERROR:"), r.stderr
        assert len(r.stderr.strip().splitlines()) == 1, r.stderr

        # missing --configs-dir: operator error, not mass-UNSTAGED.
        r = cli("--new", str(ok_new), "--old", "-",
                "--configs-dir", str(cfg / "no-such-dir"),
                "--registry", str(reg_path))
        assert r.returncode == EXIT_INPUT, (r.returncode, r.stderr)
        assert r.stderr.startswith("SLATE-DIFF-ERROR:"), r.stderr
        assert "configs-dir" in r.stderr, r.stderr
        assert not any(l.startswith("UNSTAGED(") for l in r.stdout.splitlines()), r.stdout

        # --new - is rejected up front: stdin is only valid for --old.
        r = cli("--new", "-", "--old", str(ok_new), *common)
        assert r.returncode == EXIT_INPUT, (r.returncode, r.stderr)
        assert r.stderr.startswith("SLATE-DIFF-ERROR:"), r.stderr

        # exit 3 (SANITY-FLOOR) passes through the wrapper unmasked.
        p10 = cfg / "cli-prev10.json"
        k5 = cfg / "cli-keep5.json"
        p10.write_text(json.dumps(prev10))
        k5.write_text(json.dumps(keep5))
        r = cli("--new", str(k5), "--old", str(p10), *common)
        assert r.returncode == EXIT_FLOOR, (r.returncode, r.stdout, r.stderr)
        assert any(l.startswith("SANITY-FLOOR:") for l in r.stdout.splitlines()), r.stdout

    print("self-test OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--new", help="fresh slate JSON (slate/latest.json); a path only, '-' is not accepted")
    ap.add_argument("--old", help="previously committed slate JSON, or - for stdin; empty content = INITIAL")
    ap.add_argument("--configs-dir", default="configs")
    ap.add_argument("--registry", default="configs/dial-ins.toml")
    ap.add_argument("--horizon-days", type=int, default=7)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return EXIT_OK
    if not args.new or not args.old:
        die("--new and --old are both required (or run --self-test)")
    if args.new == "-":
        die("--new does not accept '-' — stdin is only valid for --old")

    # Everything below runs under the exit-2 contract: any unexpected failure
    # (bad JSON shape -> AttributeError/KeyError/TypeError, unreadable file ->
    # OSError/UnicodeDecodeError, ...) must land as one SLATE-DIFF-ERROR line
    # + exit 2, never a raw traceback with exit 1. `except Exception` does NOT
    # catch SystemExit (a BaseException), so die()'s exit 2 and the floor's
    # exit 3 pass through unmasked.
    try:
        if not Path(args.configs_dir).is_dir():
            die(
                f"--configs-dir {args.configs_dir} does not exist or is not a "
                f"directory — refusing to fake a mass-UNSTAGED report"
            )

        new = load_slate(args.new, "new")
        if new is None:
            die(f"--new {args.new} is empty — the sweep produced nothing; refusing to diff")
        prev = load_slate(args.old, "old")
        registry = load_registry(args.registry)

        lines, floor = analyze(
            new, prev,
            today=datetime.date.today(),
            configs_dir=args.configs_dir,
            registry=registry,
            horizon_days=args.horizon_days,
        )
        for line in lines:
            print(line)
        return EXIT_FLOOR if floor else EXIT_OK
    except Exception as exc:
        tb = " | ".join(traceback.format_exc().strip().splitlines())
        die(f"unexpected failure: {type(exc).__name__}: {exc} [{tb}]"[:200])


if __name__ == "__main__":
    sys.exit(main())
