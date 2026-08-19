#!/usr/bin/env python3
"""verify-join.py — join verifier verdict rows with detection-score.py labels
→ precision-per-verdict (D2). Accepts either an `earshot verify-report` JSON or a
run report.json carrying live SHADOW rows (report["verifier"]["rows"]).
usage: verify-join.py <verify.json|report.json> <detection-score.json>"""
import json, sys
from collections import Counter
v = json.load(open(sys.argv[1])); d = json.load(open(sys.argv[2]))
if "verifier" in v and "rows" not in v:            # a run report with live shadow rows
    v = v["verifier"]
    for r in v["rows"]:                              # normalise the live row shape
        r["verdict"] = r["verdict"]["kind"] if isinstance(r["verdict"], dict) else r["verdict"]
        r.setdefault("confidence", 0.0)
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
