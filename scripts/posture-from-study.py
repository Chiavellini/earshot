#!/usr/bin/env python3
"""Set per-keyword FILL vs OBSERVE posture from the keyword-readiness study.

WHY (2026-08-05, operator request): the 26AUG05 fill ruling put ONE cap on the
whole board — every keyword got max_price_cents = MENTION_FILL_CAP_CENTS. But
the board is not uniform. The same study that gates arming
(scripts/variant-study.sh -> configs/.readiness/<base>.json) already measures,
per keyword, how often the ARMED detector actually confirmed a synthesized
utterance of that word. Words that score badly there are the words most likely
to be missed live -- and, symmetrically, the words whose few detections are
least trustworthy. Paying real money on them is the worst risk-per-word on the
board.

So: spend only where the detector has DEMONSTRATED it hears the word.

    ratio = confirmed / clips        (per keyword, from the study artifact)

    ratio >= --floor   -> FILL    cap = --fill-cap   (default 97 = MENTION_FILL_CAP_CENTS)
    ratio <  --floor   -> OBSERVE cap = 1           (non-crossing; provably cannot fill)

An OBSERVE keyword is NOT dropped. It stays armed, still detects, still writes
decisions and latency evidence -- it just cannot take a position. That keeps the
recall/accuracy record for the next study instead of blinding us to the word.

WHAT THIS DOES AND DOES NOT PROVE. The study is clean studio TTS (say(1), three
voices), not live call audio over a webcast codec. A high ratio is therefore a
NECESSARY, not sufficient, condition for trusting a word: it proves the phrase
is encodable and reachable on the armed engine's vocab. It cannot prove the word
survives a noisy line, an accent, or a cross-talking operator.

Read the ratio carefully -- it is NOT precision. clips counts synthesized
utterances; confirmed counts detections during replay. ratio < 1 means the
detector MISSED utterances and is a straight recall deficit. ratio > 1 means it
fired more than once per utterance (multiple variants/paths matching the same
audio). A high ratio is therefore evidence of easy detectability, NOT evidence
of a false-positive problem: this file deliberately does NOT gate on an upper
bound, because preflight-event.sh records that the same top-decile ratio caught
GRAB "commission" but did NOT separate "robot"/"game", both live false
positives. Use --overfire-report to SEE the high end; there is no defensible bar
to enforce on it yet.

SHORT-TOKEN OVERRIDE (k<=2). A keyword any of whose variants compiles to <=2
BPE tokens is forced to OBSERVE regardless of how well it scored. This is the
one place where a HIGH study ratio is actively misleading: preflight-event.sh
records that SNAP "game" (k=2) fired at 0.587/0.932/0.953 and settled NO, and
that no decision-layer confidence gate can reach such a target -- raise the gate
past one leg and another fires, because fanout ORs at the decision. A k=2 target
assembles from PARTIAL audio, so "the detector hears it easily" and "the
detector is right" come apart completely. Those are precisely the words we must
not pay for. Pass --compile-with/--model-dir to enable this rule; without them
the rule cannot run and the script says so loudly rather than pretending.

Idempotent: re-running replaces the annotation rather than stacking it.
Refuses on a stale study (keywords_sha256 mismatch) -- the same hash the
blocking preflight gate recomputes, so this can never disagree with it.

usage:
  posture-from-study.py <config.toml> [--floor 1.0] [--fill-cap 97]
                        [--compile-with <earshot-bin> --model-dir <dir>]
                        [--dry-run] [--overfire-report]
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

OBSERVE_CAP = 1
MARK = "# posture:"


def study_path(cfg: str) -> str:
    return os.path.join("configs/.readiness", os.path.basename(cfg)[:-5] + ".json")


def tier_for(r: float, tiers) -> int | None:
    """budget_contracts for study ratio `r`; None when no tier matches or none given."""
    for lo, n in tiers:
        if r >= lo:
            return n
    return None


def keywords_sha(cfg: str) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    out = subprocess.run(
        [sys.executable, os.path.join(here, "keywords-extract.py"), cfg, "--sha256"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--floor", type=float, default=1.0,
                    help="min confirmed/clips ratio to earn FILL posture (default 1.0)")
    ap.add_argument("--fill-cap", type=int, default=97,
                    help="max_price_cents for FILL keywords (default 97 = MENTION_FILL_CAP_CENTS)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--overfire-report", action="store_true",
                    help="also list the highest-ratio words (calibration only, not a gate)")
    ap.add_argument("--compile-with", help="path to the pinned earshot binary (enables the k<=2 rule)")
    ap.add_argument("--model-dir", help="detector model dir for compile-keywords")
    ap.add_argument("--size-tiers", default="",
                    help='size FILL words by study ratio, e.g. "3.0:10,2.0:5,1.0:2" = '
                         "ratio>=3.0 gets budget_contracts 10, >=2.0 gets 5, >=1.0 gets 2. "
                         "Omit to leave budget_contracts untouched (every FILL word keeps "
                         "whatever the config already says).")
    ap.add_argument("--low-band-budget", type=int, default=0,
                    help="TRADEABLE low band: give sub-floor words max_price_cents = "
                         "--fill-cap and this many contracts, instead of the 1c "
                         "non-crossing OBSERVE default. 0 (default) keeps OBSERVE.")
    a = ap.parse_args()

    # ── size tiers (2026-08-11 operator ruling) ─────────────────────────────
    # A flat per-word size spends the same on the word the detector nails and
    # the word it barely clears the floor with. The study already ranks them,
    # so size follows the same evidence the FILL/OBSERVE split does.
    tiers = []
    if a.size_tiers:
        try:
            for part in a.size_tiers.split(","):
                lo, n = part.split(":")
                tiers.append((float(lo), int(n)))
        except ValueError:
            print(f"REFUSING: --size-tiers must look like '3.0:10,2.0:5,1.0:2', got {a.size_tiers!r}",
                  file=sys.stderr)
            return 2
        tiers.sort(key=lambda t: -t[0])
        if tiers[-1][0] < a.floor:
            print(f"REFUSING: lowest tier {tiers[-1][0]} is below --floor {a.floor} — a word "
                  f"under the floor is OBSERVE and cannot be sized at all.", file=sys.stderr)
            return 2

    cfg = a.config
    sp = study_path(cfg)
    if not os.path.exists(sp):
        print(f"REFUSING: no readiness study at {sp} — run scripts/variant-study.sh {cfg} first",
              file=sys.stderr)
        return 2
    study = json.load(open(sp))

    got, want = keywords_sha(cfg), str(study.get("keywords_sha256", ""))
    if got != want:
        print(f"REFUSING: study is STALE for this config.\n"
              f"  config keywords_sha256 = {got}\n"
              f"  study  keywords_sha256 = {want}\n"
              f"  The phrases/variants changed since the study ran. Re-run "
              f"scripts/variant-study.sh {cfg} (with ORT_DYLIB_PATH set) — a posture "
              f"derived from a stale study is exactly the false confidence this gate exists to stop.",
              file=sys.stderr)
        return 3

    score = {}
    for k in study.get("keywords", []):
        clips = k.get("clips", 0) or 0
        conf = k.get("confirmed", 0) or 0
        score[k["ticker"]] = (conf, clips, (conf / clips) if clips else 0.0)

    # ── k<=2 variant map (optional but strongly recommended) ────────────────
    short_variants = {}   # lowercased variant -> token count, for k<=2 only
    if a.compile_with:
        cmd = [a.compile_with, "compile-keywords", "--config", cfg]
        if a.model_dir:
            cmd += ["--model-dir", a.model_dir]
        ck = subprocess.run(cmd, capture_output=True, text=True)
        for m in re.finditer(r"confirmed \[([^\]]+)\]: .*?\((\d+) tokens\)", ck.stdout + ck.stderr):
            n = int(m.group(2))
            if n <= 2:
                short_variants[m.group(1).strip().lower()] = n
        print(f"   k<=2 variants seen: "
              + (", ".join(f"{v}({n})" for v, n in sorted(short_variants.items())) or "none"))
    else:
        print("   ⚠ --compile-with not given: the k<=2 SHORT-TOKEN rule did NOT run. "
              "A high-ratio k=2 target (SNAP \"game\" class) can still reach FILL.", file=sys.stderr)

    text = open(cfg).read()
    blocks = list(re.finditer(r"\[\[keywords\]\](.*?)(?=\n\[\[|\n\[[a-z]|\Z)", text, re.S))
    if not blocks:
        print(f"REFUSING: no [[keywords]] blocks in {cfg}", file=sys.stderr)
        return 2

    fill, observe, missing, short_forced = [], [], [], []
    out, last = [], 0
    for m in blocks:
        body = m.group(1)
        tk = re.search(r'market_ticker\s*=\s*"([^"]+)"', body)
        ph = re.search(r'phrase\s*=\s*"([^"]+)"', body)
        if not tk:
            continue
        ticker, phrase = tk.group(1), (ph.group(1) if ph else "?")
        if ticker not in score:
            missing.append(ticker)
            continue
        conf, clips, r = score[ticker]
        vs = re.search(r"variants\s*=\s*\[(.*?)\]", body, re.S)
        vlist = re.findall(r'"([^"]+)"', vs.group(1)) if vs else [phrase]
        hits = {v: short_variants[v.lower()] for v in vlist if v.lower() in short_variants}

        # TWO BANDS (2026-08-11 operator ruling): a HIGH band sized by the study
        # ratio, and a LOW band that still TRADES at a small budget rather than
        # sitting at 1¢ where it provably cannot fill.
        #
        # The distinction that matters: max_price_cents = 1 is NOT "a $0.01 bet".
        # Nothing rests at 1¢ on a mention board, so a 1¢ order is non-crossing
        # and takes NO position at all — it is pure instrumentation. Only
        # --low-band-budget makes the low band real money.
        low_trades = a.low_band_budget > 0
        is_fill = r >= a.floor
        if hits and is_fill:
            is_fill = False
            note = (f"{MARK} {'LOW BAND' if low_trades else 'OBSERVE'} (k<=2 OVERRIDE) — "
                    f"study {conf}/{clips} ratio {r:.2f} would have earned the HIGH band, but "
                    f"variant(s) {', '.join(f'{v}={n}tok' for v, n in sorted(hits.items()))} "
                    f"assemble from partial audio at near-ceiling confidence (SNAP \"game\" class). "
                    f"The study measures RECALL, not precision, so a high ratio here is exactly "
                    f"the misleading case — demoted to the small band")
            short_forced.append((phrase, r, hits))
        else:
            band = "HIGH" if is_fill else ("LOW BAND" if low_trades else "OBSERVE")
            note = (f"{MARK} {band} — study {conf}/{clips} "
                    f"ratio {r:.2f} {'>=' if is_fill else '<'} floor {a.floor:.2f}"
                    + (f"; also k<=2 ({', '.join(sorted(hits))})" if hits else ""))
        # A low-band word crosses at the SAME per-contract ceiling as a high-band
        # one; only the contract count differs. That is deliberate — the ceiling
        # is a price bound, and lowering it below the ask just recreates the
        # non-crossing case under a different name.
        cap = a.fill_cap if (is_fill or low_trades) else OBSERVE_CAP

        # NOTE: an unchanged body is the IDEMPOTENT case (posture already
        # correct), NOT a failure. Detect "line absent" by counting the match,
        # never by comparing strings — conflating the two silently dropped every
        # already-correct keyword out of the tally on the first re-run.
        pat = re.compile(r"^(max_price_cents\s*=\s*)(\d+)(?:\s+#.*?)?$", re.M)
        if not pat.search(body):
            missing.append(ticker + " (no max_price_cents line)")
            continue
        new = pat.sub(lambda mm: f"{mm.group(1)}{cap}  {note}", body, count=1)

        # Size follows the same study evidence as the FILL/OBSERVE split. An
        # OBSERVE word is always 1 contract: at a 1¢ non-crossing cap it cannot
        # fill, so a larger budget would be noise in the config, not exposure.
        if is_fill and tiers:
            size = tier_for(r, tiers)
        elif not is_fill and low_trades:
            size = a.low_band_budget
        elif tiers or low_trades:
            size = 1
        else:
            size = None
        if size is not None:
            bpat = re.compile(r"^(budget_contracts\s*=\s*)(\d+)(?:\s+#.*?)?$", re.M)
            if not bpat.search(new):
                missing.append(ticker + " (no budget_contracts line)")
                continue
            if is_fill:
                bnote = f"{MARK} size {size} — ratio {r:.2f} tier, max {cap*size}¢"
            elif low_trades:
                bnote = (f"{MARK} size {size} — LOW BAND, max {cap*size}¢ of REAL money "
                         f"(this word CAN fill; it is not the old 1¢ non-crossing observe)")
            else:
                bnote = f"{MARK} size 1 — OBSERVE cannot fill"
            new = bpat.sub(lambda mm: f"{mm.group(1)}{size}  {bnote}", new, count=1)
        contracts = size if size is not None else None
        (fill if is_fill else observe).append((phrase, conf, clips, r, contracts))
        out.append(text[last:m.start(1)]); out.append(new); last = m.end(1)
    out.append(text[last:])
    newtext = "".join(out)

    print(f"── {os.path.basename(cfg)}  (floor {a.floor:.2f}, fill cap {a.fill_cap}¢)")
    low_cap = a.fill_cap if a.low_band_budget else OBSERVE_CAP
    low_label = "LOW " if a.low_band_budget else "OBS "
    for phrase, c, cl, r, n in sorted(observe, key=lambda x: x[3]):
        print(f"   {low_label} {low_cap:2d}¢   {phrase:24s} {c:4d}/{cl:<4d} ratio {r:.2f}"
              + (f"  ×{n} = {low_cap * n:>5d}¢" if n else ""))
    for phrase, c, cl, r, n in sorted(fill, key=lambda x: x[3]):
        print(f"   HIGH   {a.fill_cap}¢   {phrase:24s} {c:4d}/{cl:<4d} ratio {r:.2f}"
              + (f"  ×{n} = {a.fill_cap * n:>5d}¢" if n else ""))
    if a.overfire_report and fill:
        top = sorted(fill, key=lambda x: -x[3])[:3]
        print("   [calibration only — NOT gated] highest ratios: "
              + ", ".join(f"{p} {r:.2f}" for p, _, _, r, _n in top))
    if short_forced:
        print("   k<=2 OVERRIDE (scored into the HIGH band, demoted to "
              + ("the LOW band — still real money, just small):" if a.low_band_budget
                 else "1¢ OBSERVE — no real money):"))
        for phrase, r, hits in short_forced:
            print(f"      {phrase:24s} ratio {r:.2f}  [{', '.join(f'{v}={n}tok' for v, n in sorted(hits.items()))}]")
    if missing:
        print(f"   ⚠ no study score for: {' '.join(missing)}", file=sys.stderr)
    print(f"   => {len(fill)} FILL / {len(observe)} OBSERVE of {len(fill)+len(observe)}"
          + (f"  ({len(short_forced)} forced down by the k<=2 rule)" if short_forced else ""))

    # The worst case the CONFIG must admit. Printed because the event cap going
    # unraised is a SILENT failure, not a loud one: on 26AUG06 CELH a 100¢
    # max_event_notional_cents against a 97¢ fill cap let exactly ONE order
    # reach the wire and refused the rest on EventNotional, so the board looked
    # like a detection problem when it was a config arithmetic problem.
    if tiers or a.low_band_budget:
        worst_hi = sum(a.fill_cap * n for _, _, _, _, n in fill if n)
        worst_lo = sum(low_cap * n for _, _, _, _, n in observe if n)
        worst = worst_hi + worst_lo
        print(f"   WORST-CASE event notional if every word fires and fills: {worst}¢ "
              f"(${worst/100:.2f})   [high band {worst_hi}¢ + low band {worst_lo}¢]")
        if a.low_band_budget:
            print(f"   NOTE: the low band is REAL money now ({len(observe)} words × "
                  f"{low_cap * a.low_band_budget}¢). At a {a.fill_cap}¢ ceiling a word must be "
                  f"right >{a.fill_cap}% of the time to break even, in EITHER band.")
        print(f"   -> [risk] max_event_notional_cents must be >= {worst}, and "
              f"PROBE_LEDGER_CAP_CENTS (src/risk.rs, per-event) >= {worst}, "
              f"or the tail of this posture silently refuses.")

    if a.dry_run:
        print("   (dry-run — nothing written)")
        return 0
    if newtext != text:
        open(cfg, "w").write(newtext)
        print(f"   written: {cfg}")
    else:
        print("   unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
