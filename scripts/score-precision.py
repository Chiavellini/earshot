#!/usr/bin/env python3
"""Score a run's DETECTIONS against ground truth: was the word actually said?

    score-precision.py --event KXEARNINGSMENTIONHIMS-26AUG10-WEBCAST \
        --audio state/<E>/capture/call.wav [--window 20]
    score-precision.py --event <E> --transcript <file.json|file.txt>
    score-precision.py --run-dir var/runs/<E>/<R> --transcript <timed.json>

Typed `--run-dir` mode publishes the immutable pre-audit join artifact
`grade/precision-score.json`. Legacy `--event` mode remains report-only.

WHY. The readiness study measures RECALL on clean studio TTS. PRECISION — the
false-positive rate — is what actually gates position size, and until 2026-08-11
it was scored by hand, once, badly. On 26AUG11 five orders / $48.50 filled at
tranche 10 on a run that banked no audio at all, so nothing about them could be
checked. This script is the other half of `capture-sidecar.sh`: the sidecar
banks the evidence, this reads the verdict out of it.

THE TRAP THIS SCRIPT EXISTS TO AVOID. Detection timestamps are monotonic-clock
relative to `first_capture_ts`; transcript timestamps are relative to the START
OF THE WAV. Those two origins are NOT the same instant — on HIMS 26AUG10 they
differed by 58.8 s, because the capture began before the arm's first chunk.
Scoring naively against a shared zero graded HIMS at 7/9 = 78% and called two
TRUE positives false. Correcting for the offset gave 9/9, every hit within
±3.2 s. So: ESTIMATE THE OFFSET FIRST, from the median of all candidate hits,
then score residuals against it. A fixed offset across many words is signal; if
the residuals do not tighten, the alignment is wrong and the score is garbage —
which this script says out loud rather than printing a confident number.

PRESENCE-ONLY MODE. A published transcript (Seeking Alpha, IR site) usually has
no timestamps. These markets settle on WHETHER a word was said, not when, so
presence alone answers the settlement question — but it CANNOT distinguish "we
detected the real utterance" from "we fired at the wrong moment on a word that
happened to be said later". The output labels that weaker verdict as SAID
rather than TRUE POS, and never averages the two into one precision number.

Exit 0 scored / 2 usage or missing inputs / 3 alignment not established.
"""
import argparse, glob, hashlib, json, math, os, re, statistics, subprocess, sys
from pathlib import Path

REPO = os.environ.get("EARSHOT_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class AlignmentError(ValueError):
    pass


def canonical_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def regular_json(path, what):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{what} must be a regular non-symlink file: {path}")
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{what} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a JSON object")
    return value


def typed_context(run_raw):
    repo = Path(REPO).resolve()
    run = Path(run_raw)
    run = (run if run.is_absolute() else repo / run).resolve()
    root = (repo / "var" / "runs").resolve()
    try:
        relative = run.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"run is outside canonical {root}") from exc
    if len(relative.parts) != 2 or run.is_symlink() or not run.is_dir():
        raise ValueError("run must be var/runs/<EVENT_ID>/<RUN_ID>")
    verify = subprocess.run(
        [sys.executable, str(repo / "scripts" / "eventctl.py"),
         "verify", "--run-dir", str(run)],
        cwd=repo,
        env={**os.environ, "EARSHOT_REPO": str(repo)},
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if verify.returncode:
        raise ValueError(
            f"eventctl verify refused the run: {(verify.stderr or verify.stdout).strip()}"
        )
    plan_path = run / "control" / "run-plan.json"
    plan = regular_json(plan_path, "RunPlan")
    if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
        raise ValueError("typed precision requires a schema-3 RunPlan")
    if relative.parts != (plan.get("event_id"), plan.get("run_id")):
        raise ValueError("RunPlan identity differs from canonical run directory")
    config_raw = plan.get("config_path")
    if not isinstance(config_raw, str) or not config_raw:
        raise ValueError("RunPlan config_path is missing")
    config = (repo / config_raw).resolve()
    try:
        config.relative_to(repo)
    except ValueError as exc:
        raise ValueError("RunPlan config path escapes repository") from exc
    if config.is_symlink() or not config.is_file():
        raise ValueError("RunPlan config is not a regular non-symlink file")
    config_sha = sha256_file(config)
    if config_sha != plan.get("config_sha256"):
        raise ValueError("RunPlan config fingerprint differs from current bytes")
    report_relative = (plan.get("artifacts") or {}).get("runtime_report")
    if not isinstance(report_relative, str):
        raise ValueError("RunPlan lacks runtime_report artifact")
    report_path = (run / report_relative).resolve()
    try:
        report_path.relative_to(run)
    except ValueError as exc:
        raise ValueError("runtime report artifact escapes run") from exc
    report = regular_json(report_path, "runtime report")
    if report.get("event_id") != plan["event_id"]:
        raise ValueError("runtime report event_id differs from RunPlan")
    if report.get("run_id") not in (None, plan["run_id"]):
        raise ValueError("runtime report run_id differs from RunPlan")
    return {
        "repo": repo,
        "run": run,
        "plan": plan,
        "config": config,
        "config_sha256": config_sha,
        "report_path": report_path,
        "report": report,
        "report_sha256": sha256_file(report_path),
    }


def estimate_offset(rows, window):
    """Find one recorder-minus-detector offset across all timestamped intents.

    A recorder may start long before the detector.  Searching only occurrences
    near the detector-relative timestamp (the old +/-180 s shortcut) therefore
    rejects valid runs.  Every observed occurrence supplies a candidate offset;
    candidates are scored by how many *distinct intents* they align.  Tied
    candidates in one compact cluster are combined at their median, while
    disjoint equally-supported clusters are refused as ambiguous.
    """
    usable = [r for r in rows if r.get("t") is not None and r.get("occ")]
    if not usable:
        raise AlignmentError("no candidate hits at all — transcript and report do not describe the same call")

    candidates = sorted({float(occ) - float(r["t"]) for r in usable for occ in r["occ"]})

    def score(offset):
        residuals = []
        for row in usable:
            residual = min(
                (float(occ) - (float(row["t"]) + offset) for occ in row["occ"]),
                key=abs,
            )
            if abs(residual) <= window:
                residuals.append(residual)
        return len(residuals), sum(abs(value) for value in residuals), residuals

    scored = [(offset, *score(offset)) for offset in candidates]
    support = max(item[1] for item in scored)
    required = min(2, len(usable))
    if support < required:
        raise AlignmentError(
            f"alignment not established — best offset aligns {support}/{len(usable)} intents; "
            f"need at least {required}"
        )

    best_cost = min(item[2] for item in scored if item[1] == support)
    winners = [
        item[0]
        for item in scored
        if item[1] == support and abs(item[2] - best_cost) <= 1e-6
    ]
    if winners[-1] - winners[0] > 2 * window:
        raise AlignmentError(
            "alignment ambiguous — equally supported recorder offsets form disjoint clusters"
        )

    offset = statistics.median(winners)
    final_support, _cost, residuals = score(offset)
    if final_support < support:
        raise AlignmentError("alignment ambiguous — median offset loses supporting intents")
    return offset, residuals, support, len(usable)


def normalized_words(text):
    """Normalize ASR punctuation without collapsing distinct word tokens."""
    return " ".join(re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", text.lower()))


def die(msg, code=2):
    print(f"SCORE-ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def load_report(event):
    """Prefer a box-pulled report; deploy-box excludes state/ both ways, so a
    box-hosted run leaves nothing local until pull-box-state.sh runs."""
    for p in (f"state/{event}/box/report.json", f"state/{event}/report.json"):
        if os.path.exists(p):
            return p, json.load(open(p))
    die(f"no report.json for {event} (tried box/ then local — run scripts/pull-box-state.sh?)")


def phrases_in_config(event, path):
    try:
        import tomllib
        cfg = tomllib.load(open(path, "rb"))
    except Exception as exc:
        raise ValueError(f"cannot read pinned event config {path}: {exc}") from exc
    if (cfg.get("event") or {}).get("id") != event:
        raise ValueError("pinned config event id differs from scored event")
    out = {}
    for keyword in cfg.get("keywords") or []:
        ticker, phrase = keyword.get("market_ticker"), keyword.get("phrase")
        if ticker and phrase:
            out[ticker] = {
                "phrase": phrase,
                "variants": keyword.get("variants") or [phrase],
                "cap": keyword.get("max_price_cents"),
                "size": keyword.get("budget_contracts"),
            }
    return out


def phrases_for(event):
    """market_ticker -> phrase, from whichever config carries this event id.
    Never guess a phrase from the ticker suffix: SAME->'same store' and
    CATE->'catering' are not derivable, and a wrong phrase silently scores a
    true positive as false."""
    for p in glob.glob(os.path.join(REPO, "configs", "*.toml")):
        try:
            import tomllib
            cfg = tomllib.load(open(p, "rb"))
        except Exception:
            continue
        if (cfg.get("event") or {}).get("id") != event:
            continue
        return phrases_in_config(event, p)
    return {}


def load_truth(args):
    """-> (segments|None, full_text_lower, fingerprint metadata)."""
    if args.transcript:
        raw_bytes = open(args.transcript, "rb").read()
        raw = raw_bytes.decode("utf-8")
        truth = {
            "kind": "timed-transcript-json" if args.transcript.endswith(".json")
                    else "presence-only-transcript",
            "sha256": sha256_bytes(raw_bytes),
        }
        if args.transcript.endswith(".json"):
            segs = json.loads(raw)
            return segs, " ".join(s["text"] for s in segs).lower(), truth
        return None, raw.lower(), truth
    if not args.audio:
        die("need --audio or --transcript")
    if not os.path.exists(args.audio):
        die(f"audio not found: {args.audio}")
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        die("faster_whisper unavailable — use .venv-corpus/bin/python, or pass --transcript")
    print(f"transcribing {args.audio} ({args.model}) …", file=sys.stderr)
    m = WhisperModel(args.model, device="cpu", compute_type="int8")
    segs = [{"start": s.start, "end": s.end, "text": s.text} for s in m.transcribe(args.audio)[0]]
    if args.save_transcript:
        json.dump(segs, open(args.save_transcript, "w"))
        print(f"transcript saved -> {args.save_transcript}", file=sys.stderr)
    truth = {
        "kind": "audio-derived-timed-transcript",
        "sha256": sha256_file(args.audio),
        "transcript_sha256": sha256_bytes(canonical_bytes(segs)),
        "model": args.model,
    }
    return segs, " ".join(s["text"] for s in segs).lower(), truth


def score_rows(rows, timed, window):
    """Return one deterministic scoring object, including alignment proof."""
    if not timed:
        scored = []
        said = 0
        for row in rows:
            occurrences = int(row["occ"])
            verdict = "said" if occurrences else "never_said"
            said += bool(occurrences)
            scored.append({
                "ticker": row["ticker"],
                "phrase": row["phrase"],
                "occurrences": occurrences,
                "sent": row["sent"],
                "verdict": verdict,
            })
        return {
            "mode": "presence_only",
            "precision": {
                "status": "not_resolvable",
                "reason": "transcript_has_no_timestamps",
            },
            "settlement_presence": {
                "said": said,
                "total": len(scored),
            },
            "alignment": None,
            "rows": scored,
        }

    offset, residuals, support, usable = estimate_offset(rows, window)
    spread = statistics.pstdev(residuals) if len(residuals) > 1 else 0.0
    scored = []
    true_positive = false_positive = 0
    for row in sorted(rows, key=lambda item: (
            item["t"] if item["t"] is not None else float("inf"), item["ticker"])):
        item = {
            "ticker": row["ticker"],
            "phrase": row["phrase"],
            "detection_seconds": row["t"],
            "occurrence_count": len(row["occ"]),
            "cap_cents": row["cap"],
            "size_contracts": row["size"],
            "sent": row["sent"],
        }
        if row["t"] is None:
            item.update({"matched_seconds": None, "residual_seconds": None,
                         "verdict": "unscorable_timestamp"})
        elif not row["occ"]:
            false_positive += 1
            item.update({"matched_seconds": None, "residual_seconds": None,
                         "verdict": "false_positive_never_said"})
        else:
            near = min(row["occ"], key=lambda value: abs(value - (row["t"] + offset)))
            residual = near - (row["t"] + offset)
            matched = abs(residual) <= window
            true_positive += int(matched)
            false_positive += int(not matched)
            item.update({
                "matched_seconds": near,
                "residual_seconds": residual,
                "verdict": "true_positive" if matched else "false_positive_no_match",
            })
        scored.append(item)
    denominator = true_positive + false_positive
    return {
        "mode": "timed",
        "precision": {
            "status": "scored",
            "true_positive": true_positive,
            "false_positive": false_positive,
            "denominator": denominator,
            "ratio": true_positive / denominator if denominator else None,
        },
        "settlement_presence": None,
        "alignment": {
            "status": "established",
            "window_seconds": window,
            "offset_seconds": offset,
            "residual_spread_seconds": spread,
            "support": support,
            "usable_intents": usable,
            "support_residuals_seconds": residuals,
        },
        "rows": scored,
    }


def precision_artifact(context, truth, scoring):
    plan = context["plan"]
    value = {
        "schema": 1,
        "kind": "precision-score",
        "event_id": plan["event_id"],
        "run_id": plan["run_id"],
        "plan_sha256": plan["plan_sha256"],
        "inputs": {
            "config_path": plan["config_path"],
            "config_sha256": context["config_sha256"],
            "runtime_report_artifact": plan["artifacts"]["runtime_report"],
            "runtime_report_sha256": context["report_sha256"],
            "truth": truth,
        },
        "scoring": scoring,
    }
    value["score_sha256"] = sha256_bytes(canonical_bytes(value))
    return value


def verify_precision_artifact(context, value):
    """Consumer-side validation for the EVIDENCE grade join."""
    expected_top = {
        "schema", "kind", "event_id", "run_id", "plan_sha256",
        "inputs", "scoring", "score_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected_top:
        raise ValueError("precision score top-level fields differ from schema 1")
    plan = context["plan"]
    if value["schema"] != 1 or value["kind"] != "precision-score":
        raise ValueError("precision score schema/kind is unsupported")
    for field in ("event_id", "run_id", "plan_sha256"):
        if value[field] != plan[field]:
            raise ValueError(f"precision score {field} differs from RunPlan")
    claimed = value["score_sha256"]
    if not isinstance(claimed, str) or not HEX64.fullmatch(claimed):
        raise ValueError("precision score hash is invalid")
    unsigned = dict(value)
    unsigned.pop("score_sha256")
    if sha256_bytes(canonical_bytes(unsigned)) != claimed:
        raise ValueError("precision score hash mismatch")
    inputs = value["inputs"]
    if not isinstance(inputs, dict) or set(inputs) != {
        "config_path", "config_sha256", "runtime_report_artifact",
        "runtime_report_sha256", "truth",
    }:
        raise ValueError("precision score inputs differ from schema 1")
    expected_inputs = {
        "config_path": plan["config_path"],
        "config_sha256": context["config_sha256"],
        "runtime_report_artifact": plan["artifacts"]["runtime_report"],
        "runtime_report_sha256": context["report_sha256"],
    }
    for field, expected in expected_inputs.items():
        if inputs[field] != expected:
            raise ValueError(f"precision score {field} differs from bound input")
    truth = inputs["truth"]
    if not isinstance(truth, dict) or truth.get("kind") not in {
        "timed-transcript-json", "presence-only-transcript",
        "audio-derived-timed-transcript",
    } or not HEX64.fullmatch(str(truth.get("sha256", ""))):
        raise ValueError("precision score truth fingerprint is invalid")
    scoring = value["scoring"]
    if not isinstance(scoring, dict) or set(scoring) != {
        "mode", "precision", "settlement_presence", "alignment", "rows",
    } or not isinstance(scoring["rows"], list):
        raise ValueError("precision score scoring fields differ from schema 1")
    precision = scoring["precision"]
    if scoring["mode"] == "presence_only":
        if precision != {
            "status": "not_resolvable",
            "reason": "transcript_has_no_timestamps",
        } or scoring["alignment"] is not None:
            raise ValueError("presence-only score attempts to claim precision")
    elif scoring["mode"] == "timed":
        if not isinstance(precision, dict) or set(precision) != {
            "status", "true_positive", "false_positive", "denominator", "ratio",
        } or precision["status"] != "scored":
            raise ValueError("timed precision fields differ from schema 1")
        tp, fp, denominator = (
            precision["true_positive"], precision["false_positive"],
            precision["denominator"],
        )
        if any(type(item) is not int or item < 0 for item in (tp, fp, denominator)) \
                or denominator != tp + fp or denominator <= 0:
            raise ValueError("timed precision counts are inconsistent")
        ratio = precision["ratio"]
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) \
                or not math.isfinite(ratio) or abs(ratio - tp / denominator) > 1e-12:
            raise ValueError("timed precision ratio is inconsistent")
        alignment = scoring["alignment"]
        if not isinstance(alignment, dict) or set(alignment) != {
            "status", "window_seconds", "offset_seconds",
            "residual_spread_seconds", "support", "usable_intents",
            "support_residuals_seconds",
        } or alignment["status"] != "established":
            raise ValueError("timed alignment fields differ from schema 1")
        for field in ("window_seconds", "offset_seconds", "residual_spread_seconds"):
            number = alignment[field]
            if not isinstance(number, (int, float)) or isinstance(number, bool) \
                    or not math.isfinite(number):
                raise ValueError(f"timed alignment {field} is invalid")
        if alignment["window_seconds"] <= 0 or alignment["residual_spread_seconds"] < 0:
            raise ValueError("timed alignment bounds are invalid")
        if type(alignment["support"]) is not int or type(alignment["usable_intents"]) is not int \
                or not 0 < alignment["support"] <= alignment["usable_intents"]:
            raise ValueError("timed alignment support is invalid")
        residuals = alignment["support_residuals_seconds"]
        if not isinstance(residuals, list) or len(residuals) != alignment["support"] \
                or any(not isinstance(item, (int, float)) or isinstance(item, bool)
                       or not math.isfinite(item) for item in residuals):
            raise ValueError("timed alignment support residuals are invalid")
    else:
        raise ValueError("precision score mode is unsupported")
    return value


def publish_precision_artifact(context, value):
    verify_precision_artifact(context, value)
    run = context["run"]
    destination = run / "grade" / "precision-score.json"
    parent = destination.parent
    if parent.exists():
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("precision score parent must be a real directory")
    else:
        os.mkdir(parent, 0o755)
        fd = os.open(run, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    payload = (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise ValueError("existing precision score is not a regular file")
        if destination.read_bytes() != payload:
            raise ValueError("existing precision score differs from recomputed bytes")
        return destination, "existing-identical"
    temporary = parent / f".precision-score.{os.getpid()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        view = memoryview(payload)
        while view:
            wrote = os.write(fd, view)
            if wrote <= 0:
                raise ValueError("short write publishing precision score")
            view = view[wrote:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(temporary, destination)
    except FileExistsError:
        if destination.is_symlink() or not destination.is_file() \
                or destination.read_bytes() != payload:
            raise ValueError("concurrent different precision score appeared")
    finally:
        temporary.unlink(missing_ok=True)
    fd = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return destination, "published"


def render_score(event, report_path, scoring, window):
    if scoring["mode"] == "presence_only":
        print(f"\n=== {event}  PRESENCE-ONLY (transcript has no timestamps) ===")
        for row in sorted(scoring["rows"], key=lambda item: -item["occurrences"]):
            print(f"  {row['phrase']:22s} occurrences={row['occurrences']:<4d} "
                  f"{'FILLED' if row['sent'] else 'no fill':8s} "
                  f"{'SAID' if row['verdict'] == 'said' else '⛔ NEVER SAID'}")
        presence = scoring["settlement_presence"]
        print(f"\n  said {presence['said']}/{presence['total']} — settlement-relevant, but this CANNOT confirm we")
        print("  fired at the right moment. Re-run with timed audio for true precision.")
        return
    alignment = scoring["alignment"]
    print(f"\n=== {event} ===\nreport: {report_path}")
    print(
        f"clock offset (transcript − decision): {alignment['offset_seconds']:+.1f}s   "
        f"spread σ={alignment['residual_spread_seconds']:.1f}s   "
        f"support={alignment['support']}/{alignment['usable_intents']}"
    )
    print(f"\n{'phrase':22s} {'dec@min':>8s} {'said@min':>9s} {'resid':>8s} {'posture':>12s}  verdict")
    print("-" * 84)
    for row in scoring["rows"]:
        post = f"{row['cap_cents']}c x{row['size_contracts']}" if row["cap_cents"] else "?"
        detection = row["detection_seconds"]
        if detection is None:
            continue
        if row["matched_seconds"] is None:
            print(f"{row['phrase']:22s} {detection/60:8.1f} {'--':>9s} {'--':>8s} {post:>12s}  ⛔ NEVER SAID")
            continue
        print(f"{row['phrase']:22s} {detection/60:8.1f} {row['matched_seconds']/60:9.1f} "
              f"{row['residual_seconds']:+7.1f}s {post:>12s}  "
              f"{'TRUE POS' if row['verdict'] == 'true_positive' else '⛔ no match'}")
    print("-" * 84)
    precision = scoring["precision"]
    print(f"PRECISION: {precision['true_positive']}/{precision['denominator']} = "
          f"{100*precision['ratio']:.0f}%   (window ±{window:.0f}s after offset correction)")
    filled = [row for row in scoring["rows"] if row["sent"]]
    if filled:
        bad = [row for row in filled if row["verdict"] != "true_positive"]
        print(f"OF THE {len(filled)} THAT REACHED THE WIRE: {len(filled)-len(bad)} said, {len(bad)} never said"
              + (f" — {', '.join(row['phrase'] for row in bad)}" if bad else ""))


def main():
    ap = argparse.ArgumentParser()
    identity = ap.add_mutually_exclusive_group(required=True)
    identity.add_argument("--event")
    identity.add_argument("--run-dir")
    ap.add_argument("--audio")
    ap.add_argument("--transcript")
    ap.add_argument("--save-transcript")
    ap.add_argument("--model", default="small.en")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--window", type=float, default=20.0,
                    help="max |residual| in seconds, AFTER offset correction, to call a hit (default 20)")
    a = ap.parse_args()
    os.chdir(REPO)

    context = None
    if a.run_dir:
        try:
            context = typed_context(a.run_dir)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            die(str(exc))
        event = context["plan"]["event_id"]
        rpath, rep = str(context["report_path"]), context["report"]
        try:
            ph = phrases_in_config(event, context["config"])
        except ValueError as exc:
            die(str(exc))
    else:
        event = a.event
        rpath, rep = load_report(event)
        ph = phrases_for(event)
    if not ph:
        die(f"no config carries event id {event} — cannot map tickers to phrases")
    try:
        segs, full, truth = load_truth(a)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        die(str(exc))
    full_normalized = normalized_words(full)
    timed = segs is not None and all("start" in s for s in (segs or []))

    first = rep.get("first_capture_ts")
    intents = [o for o in (rep.get("orders") or []) if (o.get("audio_ts") or o.get("decision_ts"))]
    if not intents:
        die(f"{rpath} has no orders carrying a timestamp — nothing to score")

    def occurrences(meta):
        """All occurrence times (timed) or a count (untimed), over ANY variant."""
        pats = [normalized_words(v) for v in meta["variants"]] + [
            normalized_words(meta["phrase"])
        ]
        if timed:
            return sorted(
                {
                    s["start"]
                    for s in segs
                    if any(v in normalized_words(s["text"]) for v in pats)
                }
            )
        return sum(full_normalized.count(v) for v in set(pats))

    rows = []
    for o in intents:
        tk = o.get("ticker")
        meta = ph.get(tk)
        if not meta:
            continue
        t = ((o.get("audio_ts") or o.get("decision_ts")) - first) / 1e9 if first else None
        rows.append({"ticker": tk, "phrase": meta["phrase"], "t": t,
                     "occ": occurrences(meta), "cap": meta["cap"], "size": meta["size"],
                     "sent": bool(o.get("send_ts")) or o.get("order_status") == "executed"})
    try:
        scoring = score_rows(rows, timed, a.window)
    except AlignmentError as exc:
        die(str(exc), 3)
    if context is not None:
        artifact = precision_artifact(context, truth, scoring)
        try:
            output, publication = publish_precision_artifact(context, artifact)
        except (OSError, ValueError) as exc:
            die(str(exc))
        if a.json:
            print(json.dumps(artifact, indent=2, sort_keys=True, ensure_ascii=True))
        else:
            render_score(event, rpath, scoring, a.window)
            print(f"precision score {publication} -> {output}")
    elif a.json:
        print(json.dumps(scoring, indent=2, sort_keys=True, ensure_ascii=True))
    else:
        render_score(event, rpath, scoring, a.window)
    return 0


if __name__ == "__main__":
    sys.exit(main())
