#!/usr/bin/env python3
"""Log HLS manifest edge state continuously, for absolute PDT->ring lag.

The companion to `capture-bakeoff`: that tool measures legs against EACH OTHER,
which cannot see a cost every leg shares (docs/LATENCY-PROOF.md — the 0 ms
acquisition proof compared two ffmpeg legs and was blind to ffmpeg's own cost).
This records the CDN-side truth so a leg can be measured against the encoder:

    for each poll: wall_utc, media_sequence, last segment PDT, segment count

From that plus a leg's per-frame `capture_ns` (the `.jsonl` capture-bakeoff
writes), the absolute lag of that leg is
    (frame arrival wall clock) - (PDT of the segment carrying it + its EXTINF)
i.e. how far behind the encoder our samples actually are — the quantity the
competitor beats us on by ~1 s.

Read-only (one GET per poll). Usage:
    scripts/pdt-log.py <m3u8-url> [poll_ms=250] [secs=180] > pdt.jsonl
"""

import itertools
import json
import re
import sys
import time
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) earshot-pdt-log/1"

# CloudFront on the media-server venue class serves a FROZEN playlist body to
# any client that re-fetches the same URL string, even while the encoder keeps
# appending (CBRS 2026-08-12; AMAT 2026-08-13 — pcm-gate refused a live stream
# on this exact blindness). A unique query param per request is the defence;
# the counter keeps two same-nanosecond calls distinct.
_bust_counter = itertools.count()


def busted(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_cb={time.time_ns()}{next(_bust_counter)}"


def fetch(url: str) -> str:
    req = urllib.request.Request(
        busted(url),
        headers={
            "User-Agent": UA,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read().decode("utf-8", "replace")


def tail_segment(body: str) -> str | None:
    """Last non-comment, non-blank line = newest segment. The advance signal
    cdn-cachebust-fetch.sh and the launch-webcast live gate use — deliberately
    NOT #EXT-X-MEDIA-SEQUENCE, which this CDN class freezes while appending."""
    tail = None
    for ln in body.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            tail = ln
    return tail


def resolve_media(url: str) -> str:
    """Follow a master playlist to its first variant (same rule as manifest-probe.sh)."""
    body = fetch(url)
    if "#EXT-X-STREAM-INF" not in body:
        return url
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-STREAM-INF"):
            for cand in lines[i + 1 :]:
                cand = cand.strip()
                if cand and not cand.startswith("#"):
                    if cand.startswith("http"):
                        return cand
                    if cand.startswith("/"):
                        root = re.match(r"(https?://[^/]+)", url).group(1)
                        return root + cand
                    return url.rsplit("/", 1)[0] + "/" + cand
    return url


def main() -> None:
    url = resolve_media(sys.argv[1])
    poll_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 250
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 180.0

    print(json.dumps({"media_playlist": url, "poll_ms": poll_ms}), flush=True)
    deadline = time.time() + secs
    last_seq = None
    while time.time() < deadline:
        t0 = time.time()
        try:
            body = fetch(url)
        except Exception as e:  # transient CDN error must not end the log
            print(json.dumps({"t": t0, "error": str(e)[:120]}), flush=True)
            time.sleep(poll_ms / 1000.0)
            continue
        t1 = time.time()
        seq_m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", body)
        seq = int(seq_m.group(1)) if seq_m else None
        pdts = re.findall(r"#EXT-X-PROGRAM-DATE-TIME:(\S+)", body)
        extinfs = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", body)]
        nseg = len(extinfs)
        rec = {
            "t_req": round(t0, 6),
            "t_resp": round(t1, 6),
            "media_sequence": seq,
            "n_segments": nseg,
            "last_pdt": pdts[-1] if pdts else None,
            "last_extinf": extinfs[-1] if extinfs else None,
            "tail": tail_segment(body),
        }
        # Only interesting transitions need the full record; but a dense log is
        # cheap and lets you reconstruct exactly WHEN a new segment appeared.
        rec["new_segment"] = seq != last_seq
        last_seq = seq
        print(json.dumps(rec), flush=True)
        sleep = poll_ms / 1000.0 - (time.time() - t0)
        if sleep > 0:
            time.sleep(sleep)


if __name__ == "__main__":
    main()
