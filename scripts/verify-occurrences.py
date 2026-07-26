import tomllib, json, os, sys
call = sys.argv[1]
added = set(sys.argv[2].split(","))
d = tomllib.load(open(f"corpus/manifests/{call}.toml", "rb"))
occ = [o for o in d.get("occurrences", []) if o["phrase"] in added]
tp = f"corpus/transcripts/{call}.words.json"
if not os.path.exists(tp):
    print(f"NO WORD TRANSCRIPT at {tp} — cannot verify from transcript")
    sys.exit(0)
w = json.load(open(tp))
words = w if isinstance(w, list) else w.get("words", [])
print(f"{len(words)} words in transcript")
for o in occ:
    t = o["onset_ms"]
    ctx = [x for x in words if abs(float(x.get("start", 0)) * 1000 - t) < 4000]
    txt = " ".join(str(x.get("word", "")).strip() for x in ctx)
    hit = o["phrase"].split()[0].lower() in txt.lower()
    print(f'{o["phrase"]:<10} @{t}ms  in-transcript={hit}  "{txt[:100]}"')
