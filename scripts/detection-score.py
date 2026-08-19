#!/usr/bin/env python3
"""detection-score.py — score EVERY detection of a run (not just intents) against a
per-leg word-timestamped reference transcript; count misses too.

WHY (2026-08-18, detection-accuracy program D0). `score-precision.py` grades the
one intent per keyword at a +/-20 s window; the money question is per-WORD
precision of the detector itself, and recall of the words the transcript proves
were said. This script consumes `scripts/run-transcribe.py` output (one
`<label>.words.json` per leg, times on the leg's fed-sample timeline == the
detector's `audio_pos_ms` domain, so no offset estimation is needed) and emits:

  per keyword x leg:  tp, fp, miss, occurrences, precision, recall,
                      confidence lists for tp/fp, fp contexts (what was said)
  per keyword merged (decision-layer view: OR of legs, first fire wins)
  pooled + a precision-at-threshold sweep of the engine confidence

Match rule (bench convention, docs/BAKEOFF.md): a detection matches an occurrence
of the same keyword when audio_pos_ms in [onset - pre_ms, end + post_ms]; greedy,
one detection per occurrence per leg. Everything else is a FALSE FIRE and its
+/-3 s transcript context is recorded so the collision word is visible.

Whisper limits, stated: word timestamps can be +/-0.3 s off; brand words are
sometimes misspelled ("Salomon"->"Solomon"). `--fuzzy` lets a single-word variant
of >= 6 chars match a transcript word at difflib ratio >= 0.85 and marks the
match `fuzzy` separately, never silently. A word the reference model itself does
not hear is a reference miss, not detector truth: rows carry occurrence counts so
"0 occurrences" (nothing to detect) is visible next to "0 detections".

Writes: <out>/<run_id>.json (+ .md table). Never touches the run dir.

usage:
  detection-score.py --run-dir var/runs/<E>/<R> --transcripts <dir>/<run_id> \
      --out state/<E>/detection-score [--pre-ms 500 --post-ms 2000] [--fuzzy]
"""
import argparse, difflib, json, os, re, sys, tomllib
from collections import defaultdict
from pathlib import Path

REPO = os.environ.get("EARSHOT_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")


def norm_words(text):
    return WORD.findall(text.lower())


def load_config_keywords(cfg_path, event_id):
    cfg = tomllib.load(open(cfg_path, "rb"))
    assert (cfg.get("event") or {}).get("id") == event_id, "config event id mismatch"
    det = cfg.get("detector") or {}
    eng = det.get("engine")
    default_thr = (det.get(eng) or {}).get("default_threshold")
    kws = []
    for i, k in enumerate(cfg.get("keywords") or []):
        variants = k.get("variants") or [k["phrase"]]
        kws.append({"index": i, "ticker": k.get("market_ticker"), "phrase": k["phrase"],
                    "variants": [norm_words(v) for v in variants],
                    "variant_text": variants,
                    "cap": k.get("max_price_cents"),
                    "threshold": k.get("confirmed_threshold", default_thr)})
    return kws, eng, default_thr


def inflected(word, target):
    """Kalshi Q4 semantics count plurals/possessives: `payments`~`payment`,
    `delinquencies`~`delinquency`, `tariff's`~`tariff`. Exact match first."""
    if word == target:
        return True
    if word in (target + "s", target + "es", target + "'s"):
        return True
    if target.endswith("y") and word == target[:-1] + "ies":
        return True
    return False


def find_occurrences(words, kw, fuzzy):
    """words: list of {w,start,end}. Returns [(onset_s, end_s, variant_text, fuzzy_bool)]."""
    toks = [norm_words(w["w"]) for w in words]
    # flatten multi-token words (rare: "U.S." -> ["u","s"]) while keeping the source index
    flat, idx = [], []
    for i, tk in enumerate(toks):
        for t in tk:
            flat.append(t); idx.append(i)
    occ = []
    n = len(flat)
    for vi, v in enumerate(kw["variants"]):
        L = len(v)
        if L == 0:
            continue
        for s in range(0, n - L + 1):
            seg = flat[s:s + L]
            if seg[:-1] == v[:-1] and inflected(seg[-1], v[-1]):
                occ.append((words[idx[s]]["start"], words[idx[s + L - 1]]["end"], kw["variant_text"][vi], False))
            elif fuzzy and L == 1 and vi == 0 and len(v[0]) >= 7 and len(seg[0]) >= 6:
                # only the canonical spelling (variant 0), never prepare's generated
                # plurals — "lumbers"~"numbers" scored 0.857 and manufactured misses
                if difflib.SequenceMatcher(None, seg[0], v[0]).ratio() >= 0.9:
                    occ.append((words[idx[s]]["start"], words[idx[s]]["end"], f"{kw['variant_text'][vi]}~{seg[0]}", True))
    # dedupe overlapping (e.g. "tail wind" and "tailwind" cannot both match, but plural+singular can)
    occ.sort()
    out = []
    for o in occ:
        if out and abs(o[0] - out[-1][0]) < 0.05:
            continue
        out.append(o)
    return out


def context(words, t, span=3.0):
    return " ".join(w["w"] for w in words if t - span <= w["start"] <= t + span)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--transcripts", required=True, help="dir holding <label>.words.json for this run")
    ap.add_argument("--out", required=True)
    ap.add_argument("--pre-ms", type=int, default=500)
    ap.add_argument("--post-ms", type=int, default=2000)
    ap.add_argument("--fuzzy", action="store_true")
    ap.add_argument("--dup-ms", type=int, default=400,
                    help="a same-keyword detection this close after another on the same leg is a DUPLICATE "
                         "(two variants firing on one utterance), counted separately from tp/fp")
    a = ap.parse_args()
    os.chdir(REPO)
    run = Path(a.run_dir)
    plan = json.load(open(run / "control" / "run-plan.json"))
    event_id, run_id = plan["event_id"], plan["run_id"]
    report = json.load(open(run / plan["artifacts"]["runtime_report"]))
    man = json.load(open(run / "capture" / "audio.manifest.json"))
    labels = {int(k): v for k, v in man.get("source_labels", {}).items()}
    kws, engine, default_thr = load_config_keywords(plan["config_path"], event_id)

    transcripts = {}
    for p in Path(a.transcripts).glob("*.words.json"):
        t = json.load(open(p))
        transcripts[int(t["source"])] = t
    if not transcripts:
        sys.exit(f"no transcripts under {a.transcripts}")

    dets = report.get("detections") or []
    by_leg = defaultdict(list)
    for r in dets:
        d = r["detection"]
        by_leg[int(d["source"])].append({"kw": int(d["keyword"]), "pos_ms": int(d["audio_pos_ms"]),
                                         "conf": float(d["confidence"]), "tier": d["tier"],
                                         "intent": bool(r.get("intent")), "leg": r.get("leg")})

    result = {"event_id": event_id, "run_id": run_id, "engine": engine, "default_threshold": default_thr,
              "pre_ms": a.pre_ms, "post_ms": a.post_ms, "fuzzy": a.fuzzy,
              "legs": {}, "keywords": []}
    per_kw_leg = defaultdict(dict)   # kw index -> leg -> stats
    for src, tr in sorted(transcripts.items()):
        words = tr["words"]
        label = tr.get("source_label") or labels.get(src, str(src))
        leg_dets = sorted(by_leg.get(src, []), key=lambda d: d["pos_ms"])
        result["legs"][str(src)] = {"label": label, "minutes": round(tr["total_samples"] / tr["sample_rate_hz"] / 60, 1),
                                    "words": len(words), "detections": len(leg_dets), "model": tr.get("model")}
        for kw in kws:
            occs = find_occurrences(words, kw, a.fuzzy)
            kd_all = [d for d in leg_dets if d["kw"] == kw["index"]]
            # split duplicate variant fires (same utterance) from real detections
            kd, dup = [], []
            for d in kd_all:
                if kd and d["pos_ms"] - kd[-1]["pos_ms"] <= a.dup_ms:
                    dup.append(d)
                else:
                    kd.append(d)
            used = [False] * len(occs)
            tp, fp = [], []
            for d in kd:
                hit = None
                for j, (on, en, vt, fz) in enumerate(occs):
                    if used[j]:
                        continue
                    if (on * 1000 - a.pre_ms) <= d["pos_ms"] <= (en * 1000 + a.post_ms):
                        hit = j; break
                if hit is not None:
                    used[hit] = True
                    on, en, vt, fz = occs[hit]
                    tp.append({"pos_ms": d["pos_ms"], "conf": d["conf"], "occ_end_ms": int(en * 1000),
                               "lag_ms": int(d["pos_ms"] - en * 1000), "variant": vt, "fuzzy": fz, "intent": d["intent"]})
                else:
                    fp.append({"pos_ms": d["pos_ms"], "conf": d["conf"], "intent": d["intent"],
                               "said": context(words, d["pos_ms"] / 1000.0)})
            miss = [{"onset_ms": int(on * 1000), "end_ms": int(en * 1000), "variant": vt, "fuzzy": fz,
                     "said": ("~" if fz else "") + context(words, on, 2.0)} for (on, en, vt, fz), u in zip(occs, used) if not u]
            per_kw_leg[kw["index"]][label] = {"occurrences": len(occs), "tp": tp, "fp": fp, "miss": miss,
                                              "dup": [{"pos_ms": d["pos_ms"], "conf": d["conf"]} for d in dup]}

    # merged (decision-layer view): a keyword is "detected" if any leg detected; occurrence set = max over legs
    for kw in kws:
        legs = per_kw_leg[kw["index"]]
        row = {"index": kw["index"], "ticker": kw["ticker"], "phrase": kw["phrase"], "cap": kw["cap"],
               "threshold": kw["threshold"], "legs": {}}
        tot_tp = tot_fp = tot_miss = tot_occ = 0
        for label, st in legs.items():
            ntp, nfp, nmiss = len(st["tp"]), len(st["fp"]), len(st["miss"])
            row["legs"][label] = {
                "occurrences": st["occurrences"], "tp": ntp, "fp": nfp, "miss": nmiss, "dup": len(st["dup"]),
                "precision": round(ntp / (ntp + nfp), 3) if (ntp + nfp) else None,
                "recall": round(ntp / st["occurrences"], 3) if st["occurrences"] else None,
                "tp_conf": [round(t["conf"], 3) for t in st["tp"]],
                "fp_conf": [round(f["conf"], 3) for f in st["fp"]],
                "tp_pos_ms": [t["pos_ms"] for t in st["tp"]],
                "fp_pos_ms": [f["pos_ms"] for f in st["fp"]],
                "dup_pos_ms": [d["pos_ms"] for d in st["dup"]],
                "tp_lag_ms": [t["lag_ms"] for t in st["tp"]],
                "fp_said": [f["said"] for f in st["fp"]][:12],
                "miss_said": [m["said"] for m in st["miss"]][:12],
                "fuzzy_tp": sum(1 for t in st["tp"] if t["fuzzy"]),
            }
            tot_tp += ntp; tot_fp += nfp; tot_miss += nmiss; tot_occ = max(tot_occ, st["occurrences"])
        # first-fire view: what the one-shot decision saw
        first = None
        for label, st in legs.items():
            for t in st["tp"]:
                if t["intent"]: first = ("TP", label, t["conf"], t["pos_ms"])
            for f in st["fp"]:
                if f["intent"]: first = ("FP", label, f["conf"], f["pos_ms"], f["said"])
        row["intent_verdict"] = first
        row["any_leg_detected"] = tot_tp + tot_fp > 0
        row["said_any_leg"] = tot_occ > 0
        row["pooled"] = {"tp": tot_tp, "fp": tot_fp, "miss": tot_miss, "occurrences_max_leg": tot_occ,
                         "precision": round(tot_tp / (tot_tp + tot_fp), 3) if (tot_tp + tot_fp) else None}
        result["keywords"].append(row)

    # pooled + threshold sweep
    all_tp = [t for kw in result["keywords"] for lg in kw["legs"].values() for t in lg["tp_conf"]]
    all_fp = [f for kw in result["keywords"] for lg in kw["legs"].values() for f in lg["fp_conf"]]
    sweep = []
    for thr in (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.70, 0.90):
        tp = sum(1 for c in all_tp if c >= thr); fp = sum(1 for c in all_fp if c >= thr)
        sweep.append({"thr": thr, "tp": tp, "fp": fp, "precision": round(tp / (tp + fp), 3) if tp + fp else None,
                      "tp_kept": round(tp / len(all_tp), 3) if all_tp else None})
    n_occ = sum(kw["pooled"]["occurrences_max_leg"] for kw in result["keywords"])
    n_dup = sum(lg["dup"] for kw in result["keywords"] for lg in kw["legs"].values())
    result["pooled"] = {"detections": len(dets), "dup": n_dup, "tp": len(all_tp), "fp": len(all_fp),
                        "precision": round(len(all_tp) / (len(all_tp) + len(all_fp)), 3) if dets else None,
                        "words_said": sum(1 for kw in result["keywords"] if kw["said_any_leg"]),
                        "words_detected_true": sum(1 for kw in result["keywords"] if kw["said_any_leg"] and any(l["tp"] for l in kw["legs"].values())),
                        "words_never_said_but_fired": sum(1 for kw in result["keywords"] if not kw["said_any_leg"] and kw["any_leg_detected"]),
                        "intents": {"tp": sum(1 for kw in result["keywords"] if kw["intent_verdict"] and kw["intent_verdict"][0] == "TP"),
                                    "fp": sum(1 for kw in result["keywords"] if kw["intent_verdict"] and kw["intent_verdict"][0] == "FP")},
                        "sweep": sweep}

    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    jp = out / f"{run_id}.json"
    json.dump(result, open(jp, "w"), indent=1)
    # markdown
    lines = [f"# detection score — {event_id} / {run_id}", "",
             f"engine {engine} default_threshold {default_thr}; window [onset-{a.pre_ms}, end+{a.post_ms}] ms; fuzzy={a.fuzzy}", ""]
    for s, lg in result["legs"].items():
        lines.append(f"- leg {s} `{lg['label']}`: {lg['minutes']} min, {lg['words']} words, {lg['detections']} detections ({lg['model']})")
    lines += ["", "| word | cap | thr | leg | said | tp | fp | dup | miss | prec | recall | tp conf | fp conf | intent |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for kw in result["keywords"]:
        for label, lg in kw["legs"].items():
            iv = kw["intent_verdict"]
            ivs = f"{iv[0]}@{iv[2]:.2f}" if iv and iv[1] == label else ""
            lines.append(f"| {kw['phrase']} | {kw['cap']} | {kw['threshold']} | {label} | {lg['occurrences']} | {lg['tp']} | {lg['fp']} | {lg['dup']} | {lg['miss']} | "
                         f"{lg['precision']} | {lg['recall']} | {' '.join(f'{c:.2f}' for c in lg['tp_conf'][:6])} | {' '.join(f'{c:.2f}' for c in lg['fp_conf'][:6])} | {ivs} |")
    p = result["pooled"]
    lines += ["", f"pooled: detections {p['detections']} (dup {p['dup']}), TP {p['tp']}, FP {p['fp']}, precision {p['precision']}; "
              f"words said {p['words_said']}, of which detected {p['words_detected_true']}; fired-but-never-said words {p['words_never_said_but_fired']}; "
              f"intents TP {p['intents']['tp']} FP {p['intents']['fp']}", "", "| thr | tp | fp | precision | tp kept |", "|---|---|---|---|---|"]
    for s in sweep:
        lines.append(f"| {s['thr']} | {s['tp']} | {s['fp']} | {s['precision']} | {s['tp_kept']} |")
    lines += ["", "## false fires — what was actually said", ""]
    for kw in result["keywords"]:
        for label, lg in kw["legs"].items():
            for said, c in zip(lg["fp_said"], lg["fp_conf"]):
                lines.append(f"- **{kw['phrase']}** ({label}, {c:.2f}): …{said}…")
    lines += ["", "## misses — said but not detected", ""]
    for kw in result["keywords"]:
        for label, lg in kw["legs"].items():
            for said in lg["miss_said"]:
                lines.append(f"- **{kw['phrase']}** ({label}): …{said}…")
    (out / f"{run_id}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:60]))
    print(f"\n-> {jp}")


if __name__ == "__main__":
    main()
