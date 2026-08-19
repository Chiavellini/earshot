#!/usr/bin/env python3
"""detection-score-pool.py — pool detection-score.py outputs into one per-word table.

usage: detection-score-pool.py <score.json>... [--md out.md]
Per (event, word): occurrences (max over legs), TP/FP/dup/miss summed over legs,
precision, recall, confidence spans, the intent verdict, and cap. Then a pooled
row and a precision-at-threshold sweep over every leg's TP/FP confidences.
"""
import argparse, json, sys
from collections import defaultdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scores", nargs="+")
    ap.add_argument("--md")
    a = ap.parse_args()
    rows, all_tp, all_fp = [], [], []
    tot = defaultdict(int)
    for p in a.scores:
        d = json.load(open(p))
        ev = d["event_id"].replace("KXEARNINGSMENTION", "").replace("-26AUG18-WEBCAST", "").replace("-WEBCAST", "")
        for kw in d["keywords"]:
            legs = kw["legs"]
            occ = max((l["occurrences"] for l in legs.values()), default=0)
            tp = sum(l["tp"] for l in legs.values()); fp = sum(l["fp"] for l in legs.values())
            dup = sum(l["dup"] for l in legs.values()); miss = sum(l["miss"] for l in legs.values())
            tpc = [c for l in legs.values() for c in l["tp_conf"]]; fpc = [c for l in legs.values() for c in l["fp_conf"]]
            all_tp += tpc; all_fp += fpc
            iv = kw["intent_verdict"]
            rows.append({"event": ev, "word": kw["phrase"], "cap": kw["cap"], "occ": occ, "tp": tp, "fp": fp, "dup": dup,
                         "miss": miss, "legs": len(legs),
                         "prec": round(tp / (tp + fp), 2) if tp + fp else None,
                         "recall": round(tp / (occ * len(legs)), 2) if occ else None,
                         "tp_conf": f"{min(tpc):.2f}-{max(tpc):.2f}" if tpc else "", "fp_conf": f"{min(fpc):.2f}-{max(fpc):.2f}" if fpc else "",
                         "intent": f"{iv[0]}@{iv[2]:.2f}" if iv else ""})
            for k in ("tp", "fp", "dup", "miss"):
                tot[k] += rows[-1][k]
    lines = ["| event | word | cap | said | tp | fp | dup | miss | prec | recall (per leg) | tp conf | fp conf | intent |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["occ"] == 0 and r["tp"] + r["fp"] == 0:
            continue  # never said, never fired — noise row
        lines.append(f"| {r['event']} | {r['word']} | {r['cap']} | {r['occ']} | {r['tp']} | {r['fp']} | {r['dup']} | {r['miss']} | {r['prec']} | {r['recall']} | {r['tp_conf']} | {r['fp_conf']} | {r['intent']} |")
    silent = [f"{r['event']}:{r['word']}" for r in rows if r["occ"] == 0 and r["tp"] + r["fp"] == 0]
    lines += ["", f"never said AND never fired ({len(silent)}): " + ", ".join(silent), "",
              f"POOLED: TP {tot['tp']}  FP {tot['fp']}  dup {tot['dup']}  miss {tot['miss']}  precision {tot['tp']/(tot['tp']+tot['fp']):.3f}  recall {tot['tp']/(tot['tp']+tot['miss']):.3f}", "",
              "| thr | tp | fp | precision | tp kept |", "|---|---|---|---|---|"]
    for thr in (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.70):
        tp = sum(1 for c in all_tp if c >= thr); fp = sum(1 for c in all_fp if c >= thr)
        lines.append(f"| {thr} | {tp} | {fp} | {tp/(tp+fp):.3f} | {tp/len(all_tp):.2f} |" if tp + fp else f"| {thr} | 0 | 0 | - | 0 |")
    out = "\n".join(lines) + "\n"
    print(out)
    if a.md:
        open(a.md, "w").write(out)


if __name__ == "__main__":
    main()
