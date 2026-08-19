#!/usr/bin/env python3
"""verify-join.py — join `earshot verify-report` rows with detection-score.py labels
→ precision-per-verdict (D2 V0). usage: verify-join.py <verify.json> <detection-score.json>"""
import json, sys
from collections import Counter
v = json.load(open(sys.argv[1])); d = json.load(open(sys.argv[2]))
raw = d
tp_pos = {}; fp_pos = {}; dup_pos = {}
for kw in raw["keywords"]:
    for leg, lg in kw["legs"].items():
        key = (kw["index"], leg)
        tp_pos[key] = lg.get("tp_pos_ms", []); fp_pos[key] = lg.get("fp_pos_ms", []); dup_pos[key] = lg.get("dup_pos_ms", [])
table = Counter(); rows = []
for r in v["rows"]:
    key = (r["keyword"], r["leg"])
    lab = "unlabeled"
    for name, store in (("TP", tp_pos), ("FP", fp_pos), ("DUP", dup_pos)):
        if any(abs(p - r["audio_pos_ms"]) <= 50 for p in store.get(key, [])):
            lab = name; break
    table[(lab, r["verdict"])] += 1
    rows.append((lab, r["verdict"], r["phrase"], r["leg"], r["audio_pos_ms"], r["confidence"], r["transcript"][:80]))
print(f"{'label':6} {'verdict':10} n")
for (lab, ver), n in sorted(table.items()): print(f"{lab:6} {ver:10} {n}")
print("\nfalse vetoes (TP rejected):")
for row in rows:
    if row[0] in ("TP","DUP") and row[1] == "rejected": print("  ", row)
print("\ncorrect vetoes (FP rejected):")
for row in rows:
    if row[0] == "FP" and row[1] == "rejected": print("  ", row)
print("\nmissed vetoes (FP confirmed):")
for row in rows:
    if row[0] == "FP" and row[1] == "confirmed": print("  ", row)
