#!/usr/bin/env python3
"""Extract the [[keywords]] blocks of an event config as a stable TSV, or hash it.

The ONE extraction shared by scripts/variant-study.sh (which writes the
keyword-readiness artifact configs/.readiness/<basename>.json) and
scripts/preflight-event.sh (which recomputes the hash for the blocking
readiness gate). Both call THIS file so the two sides can never drift: the
sha256 the gate recomputes is over the exact bytes this script prints.

Output (default): one line per keyword,
    MARKET_TICKER<TAB>PHRASE<TAB>VARIANT1|VARIANT2|...
--sha256: print only the sha256 hex digest over that exact TSV text
(including the trailing newline of each line). Deliberately hashes the
SEMANTIC keyword content — tickers, phrases, variants — not price caps or
thresholds, so retuning a cap does not invalidate a TTS study but any change
to what the detector must HEAR does.

usage: keywords-extract.py <config.toml> [--sha256]
"""

import hashlib
import re
import sys


def extract_tsv(text: str) -> str:
    blocks = re.findall(r"\[\[keywords\]\]\s*(.*?)(?=\n\[\[|\n\[[a-z]|\Z)", text, re.S)
    lines = []
    for b in blocks:
        tk = re.search(r'market_ticker\s*=\s*"([^"]+)"', b)
        ph = re.search(r'phrase\s*=\s*"([^"]+)"', b)
        vs = re.search(r"variants\s*=\s*\[(.*?)\]", b, re.S)
        if not (tk and ph):
            continue
        variants = re.findall(r'"([^"]+)"', vs.group(1)) if vs else [ph.group(1)]
        lines.append(f"{tk.group(1)}\t{ph.group(1)}\t" + "|".join(variants))
    return "".join(line + "\n" for line in lines)


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--sha256"]
    want_sha = "--sha256" in sys.argv[1:]
    if len(args) != 1:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    tsv = extract_tsv(open(args[0]).read())
    if want_sha:
        print(hashlib.sha256(tsv.encode("utf-8")).hexdigest())
    else:
        sys.stdout.write(tsv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
