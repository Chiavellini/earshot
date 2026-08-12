#!/usr/bin/env python3
"""Score a run's DETECTIONS against ground truth: was the word actually said?

    score-precision.py --event KXEARNINGSMENTIONHIMS-26AUG10-WEBCAST \
        --audio state/<E>/capture/call.wav [--window 20]
    score-precision.py --event <E> --transcript <file.json|file.txt>

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
import argparse, glob, json, os, re, statistics, sys

REPO = os.environ.get("EARSHOT_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AlignmentError(ValueError):
    pass


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


def phrases_for(event):
    """market_ticker -> phrase, from whichever config carries this event id.
    Never guess a phrase from the ticker suffix: SAME->'same store' and
    CATE->'catering' are not derivable, and a wrong phrase silently scores a
    true positive as false."""
    out = {}
    for p in glob.glob(os.path.join(REPO, "configs", "*.toml")):
        try:
            import tomllib
            cfg = tomllib.load(open(p, "rb"))
        except Exception:
            continue
        if (cfg.get("event") or {}).get("id") != event:
            continue
        for k in cfg.get("keywords") or []:
            tk, ph = k.get("market_ticker"), k.get("phrase")
            if tk and ph:
                out[tk] = {"phrase": ph, "variants": k.get("variants") or [ph],
                           "cap": k.get("max_price_cents"), "size": k.get("budget_contracts")}
    return out


def load_truth(args):
    """-> (segments|None, full_text_lower). segments carry .start when timed."""
    if args.transcript:
        raw = open(args.transcript).read()
        if args.transcript.endswith(".json"):
            segs = json.loads(raw)
            return segs, " ".join(s["text"] for s in segs).lower()
        return None, raw.lower()
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
    return segs, " ".join(s["text"] for s in segs).lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True)
    ap.add_argument("--audio")
    ap.add_argument("--transcript")
    ap.add_argument("--save-transcript")
    ap.add_argument("--model", default="small.en")
    ap.add_argument("--window", type=float, default=20.0,
                    help="max |residual| in seconds, AFTER offset correction, to call a hit (default 20)")
    a = ap.parse_args()
    os.chdir(REPO)

    rpath, rep = load_report(a.event)
    ph = phrases_for(a.event)
    if not ph:
        die(f"no config carries event id {a.event} — cannot map tickers to phrases")
    segs, full = load_truth(a)
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

    if not timed:
        print(f"\n=== {a.event}  PRESENCE-ONLY (transcript has no timestamps) ===")
        said = sum(1 for r in rows if r["occ"])
        for r in sorted(rows, key=lambda r: -r["occ"]):
            print(f"  {r['phrase']:22s} occurrences={r['occ']:<4d} "
                  f"{'FILLED' if r['sent'] else 'no fill':8s} {'SAID' if r['occ'] else '⛔ NEVER SAID'}")
        print(f"\n  said {said}/{len(rows)} — settlement-relevant, but this CANNOT confirm we")
        print("  fired at the right moment. Re-run with timed audio for true precision.")
        return 0

    # ── offset: the whole point of this script ────────────────────────────
    try:
        off, residuals, support, usable = estimate_offset(rows, a.window)
    except AlignmentError as exc:
        die(str(exc), 3)
    spread = statistics.pstdev(residuals) if len(residuals) > 1 else 0.0
    print(f"\n=== {a.event} ===\nreport: {rpath}")
    print(
        f"clock offset (transcript − decision): {off:+.1f}s   "
        f"spread σ={spread:.1f}s   support={support}/{usable}"
    )
    if spread > 30:
        print("⚠ residual spread is large — alignment NOT established; treat every verdict below as unproven.")

    print(f"\n{'phrase':22s} {'dec@min':>8s} {'said@min':>9s} {'resid':>8s} {'posture':>12s}  verdict")
    print("-" * 84)
    tp = fp = 0
    for r in sorted(rows, key=lambda r: (r["t"] if r["t"] is not None else 0)):
        post = f"{r['cap']}c x{r['size']}" if r["cap"] else "?"
        if r["t"] is None:
            continue
        if not r["occ"]:
            fp += 1
            print(f"{r['phrase']:22s} {r['t']/60:8.1f} {'--':>9s} {'--':>8s} {post:>12s}  ⛔ NEVER SAID")
            continue
        near = min(r["occ"], key=lambda x: abs(x - (r["t"] + off)))
        resid = near - (r["t"] + off)
        ok = abs(resid) <= a.window
        tp += ok
        fp += (not ok)
        print(f"{r['phrase']:22s} {r['t']/60:8.1f} {near/60:9.1f} {resid:+7.1f}s {post:>12s}  "
              f"{'TRUE POS' if ok else '⛔ no match'}")
    print("-" * 84)
    n = tp + fp
    print(f"PRECISION: {tp}/{n} = {100*tp/n:.0f}%   (window ±{a.window:.0f}s after offset correction)")
    filled = [r for r in rows if r["sent"]]
    if filled:
        bad = [r for r in filled if not r["occ"]]
        print(f"OF THE {len(filled)} THAT REACHED THE WIRE: {len(filled)-len(bad)} said, {len(bad)} never said"
              + (f" — {', '.join(r['phrase'] for r in bad)}" if bad else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
