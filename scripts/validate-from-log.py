#!/usr/bin/env python3
"""Lightweight detection validation — promote observe-words without recording.

Compares a live `arm` run's detection log against the public call transcript to
estimate each keyword's real-world precision/recall on telephony audio, so a
vetted word can be promoted from observe (1¢) to fillable. Respects the RUNBOOK
"do not record" rule — it needs only the (already-emitted) detection log plus a
transcript you obtain separately (scripts/transcript-fetch.py on the public
webcast, or a pasted transcript).

Usage:
  scripts/validate-from-log.py --log arm.log --config configs/twilio-live.toml \
      --transcript corpus/transcripts/ms-q2-2026.txt [--words ms.words.json] \
      [--offset-ms 0] [--window-ms 3000]

Two modes:
  * COUNT mode (default, always available): per word, detector FIRES vs transcript
    OCCURRENCES. Robust, needs no time alignment. A word that fires ~= its
    occurrences with no wild over-firing is a promotion candidate.
  * TIME mode (when --words is a .words.json with per-word start times AND the log
    has a stream-start anchor): windowed matching → true precision/recall. Our
    dial audio and a public webcast differ by an offset (webcasts lag ~10-30 s);
    pass --offset-ms to align, or eyeball COUNT mode first.

Exit 0 always; this is a report, not a gate.
"""
import argparse, json, re, sys
from pathlib import Path

# ISO8601 ts prefix in tracing logs, e.g. 2026-07-15T13:00:01.234567Z
TS = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)Z")
TICKER = re.compile(r"ticker=\"?([A-Z0-9\-]+)\"?")

def parse_iso_ms(s):
    # seconds since a fixed epoch is enough for deltas; parse HMS + frac.
    m = re.search(r"T(\d{2}):(\d{2}):(\d{2})\.(\d+)", s)
    if not m: return None
    h, mi, se, frac = m.groups()
    frac_ms = int((frac + "000000")[:6]) / 1000.0
    return ((int(h) * 60 + int(mi)) * 60 + int(se)) * 1000.0 + frac_ms

def load_config_words(cfg_path):
    """market_ticker -> (phrase, [variants]) from an event config TOML."""
    try:
        import tomllib
        cfg = tomllib.loads(Path(cfg_path).read_text())
    except Exception as e:
        sys.exit(f"cannot read config {cfg_path}: {e}")
    out = {}
    for kw in cfg.get("keywords", []):
        out[kw["market_ticker"]] = (kw["phrase"], [v.lower() for v in kw.get("variants", [kw["phrase"]])])
    return out

def parse_log_fires(log_path, ticker_words):
    """Detector fires from the arm log: 'decision intent' and 'LOST_RACE' lines
    both mean the detector fired. Returns {phrase: [rel_or_abs_ms,...]} and the
    stream-start anchor (ms) if present."""
    fires = {}
    anchor = None
    text = Path(log_path).read_text(errors="replace") if log_path != "-" else sys.stdin.read()
    for line in text.splitlines():
        low = line.lower()
        if "stream started" in low and anchor is None:
            anchor = parse_iso_ms(line)
        is_fire = ("decision intent" in low) or ("lost_race" in low)
        if not is_fire:
            continue
        mt = TICKER.search(line)
        if not mt:
            continue
        phrase = ticker_words.get(mt.group(1), (mt.group(1),))[0]
        t = parse_iso_ms(line)
        fires.setdefault(phrase, []).append(t)
    return fires, anchor

def count_transcript(transcript_path, words):
    """Whole-word, case-insensitive occurrence counts for each phrase's variants."""
    text = Path(transcript_path).read_text(errors="replace").lower()
    counts = {}
    for phrase, variants in words.items():
        n = 0
        for v in variants:
            n += len(re.findall(r"\b" + re.escape(v) + r"\b", text))
        counts[phrase] = n
    return counts

def occurrence_times(words_json, words):
    """{phrase: [start_ms,...]} from a transcript-fetch .words.json."""
    data = json.loads(Path(words_json).read_text())
    variant_to_phrase = {}
    for phrase, variants in words.items():
        for v in variants:
            variant_to_phrase[v] = phrase
    out = {}
    for w in data:
        tok = str(w.get("word", "")).lower().strip(".,!?;:'\"")
        p = variant_to_phrase.get(tok)
        if p is not None:
            out.setdefault(p, []).append(float(w.get("start", 0.0)) * 1000.0)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--words", help=".words.json with per-word start times (enables TIME mode)")
    ap.add_argument("--offset-ms", type=float, default=0.0, help="add to transcript times to align to our audio")
    ap.add_argument("--window-ms", type=float, default=3000.0)
    a = ap.parse_args()

    words = {ph: var for (_, (ph, var)) in load_config_words(a.config).items()}
    ticker_words = load_config_words(a.config)
    fires, anchor = parse_log_fires(a.log, ticker_words)
    occ_counts = count_transcript(a.transcript, words)

    phrases = sorted(words.keys())
    print(f"{'word':16s} {'fires':>6s} {'occ':>5s}  COUNT-signal")
    for p in phrases:
        nf = len(fires.get(p, []))
        no = occ_counts.get(p, 0)
        if no == 0 and nf == 0:
            sig = "— (never said, never fired)"
        elif no == 0 and nf > 0:
            sig = "⚠ fired but transcript has 0 — likely FALSE ALARMS"
        elif nf == 0 and no > 0:
            sig = "✗ MISSED all — recall 0"
        elif nf <= no:
            sig = f"ok recall≈{nf/no:.2f}, no over-fire"
        else:
            sig = f"⚠ over-fired {nf}>{no} — {nf-no} likely false alarms"
        print(f"{p:16s} {nf:>6d} {no:>5d}  {sig}")

    # TIME mode: windowed matching for true precision/recall.
    if a.words and anchor is not None:
        occ_t = occurrence_times(a.words, words)
        print(f"\nTIME mode (anchor={anchor:.0f}ms, offset={a.offset_ms:.0f}ms, window=±{a.window_ms:.0f}ms):")
        print(f"{'word':16s} {'tp':>4s} {'fp':>4s} {'fn':>4s} {'prec':>6s} {'recall':>7s}")
        for p in phrases:
            fire_rel = sorted((t - anchor) for t in fires.get(p, []) if t is not None)
            occ = sorted(t + a.offset_ms for t in occ_t.get(p, []))
            used = [False] * len(occ)
            tp = 0
            for ft in fire_rel:
                for i, ot in enumerate(occ):
                    if not used[i] and abs(ft - ot) <= a.window_ms:
                        used[i] = True; tp += 1; break
            fp = len(fire_rel) - tp
            fn = len(occ) - sum(used)
            prec = tp / (tp + fp) if (tp + fp) else float("nan")
            rec = tp / (tp + fn) if (tp + fn) else float("nan")
            print(f"{p:16s} {tp:>4d} {fp:>4d} {fn:>4d} {prec:>6.2f} {rec:>7.2f}")
    elif a.words and anchor is None:
        print("\n(TIME mode skipped: no 'stream started' anchor in log)")

    print("\nPromotion rule: a word joins the vetted/fillable set (add to "
          "prepare::tuned_library) once it shows high precision / ~0 false alarms "
          "on a real call. COUNT mode is the first cut; confirm with TIME mode.")

if __name__ == "__main__":
    main()
