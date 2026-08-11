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
    timed = segs is not None and all("start" in s for s in (segs or []))

    first = rep.get("first_capture_ts")
    intents = [o for o in (rep.get("orders") or []) if (o.get("audio_ts") or o.get("decision_ts"))]
    if not intents:
        die(f"{rpath} has no orders carrying a timestamp — nothing to score")

    def occurrences(meta):
        """All occurrence times (timed) or a count (untimed), over ANY variant."""
        pats = [v.lower() for v in meta["variants"]] + [meta["phrase"].lower()]
        if timed:
            return sorted({s["start"] for s in segs if any(v in s["text"].lower() for v in pats)})
        return sum(full.count(v) for v in set(pats))

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
    cands = []
    for r in rows:
        if r["t"] is None or not r["occ"]:
            continue
        d = min(r["occ"], key=lambda x: abs(x - r["t"])) - r["t"]
        if abs(d) < 180:
            cands.append(d)
    if not cands:
        die("no candidate hits at all — transcript and report do not describe the same call", 3)
    off = statistics.median(cands)
    spread = statistics.pstdev(cands) if len(cands) > 1 else 0.0
    print(f"\n=== {a.event} ===\nreport: {rpath}")
    print(f"clock offset (transcript − decision): {off:+.1f}s   spread σ={spread:.1f}s   n={len(cands)}")
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
