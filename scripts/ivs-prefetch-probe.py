#!/usr/bin/env python3
"""IVS venue probe — the venue class PayPal (07-28) runs on.

Why this exists. Our speculative fetcher wins its lead by NUMERICALLY predicting
segment N+1 (`hls-edge-fetch.py:pattern_is_predictable`). IVS segment URLs are
opaque signed tokens:

    /v1/segment/CucCQjX4C-Pa2l0Xm8nl4YThczGSYTr8vaWr4t9XDaQHuwrrCik4Scz4...

so that fetcher self-disables on IVS ("segment URLs are not predictable") and
buys exactly ZERO. Every projection of the race residual assumed it buys `L`.

But IVS publishes `#EXT-X-PREFETCH` (Twitch LHLS): the manifest HANDS US the URLs
of segments that do not exist yet. No prediction, no 404 probing, no CDN
politeness risk. If those URLs are fetchable before the same segment is announced
normally, that interval IS `L` for this venue — by a better mechanism than the
one we built.

Two questions, both open for IVS and both answered here:
  Q1  L_prefetch = t(URL announced as a normal segment) - t(URL first fetchable)
  Q2  Is the body COMPLETE on publish (Content-Length, one RTT) or does it
      DRIBBLE (chunked, ~a segment long)? F2 answered "complete" for DW/Akamai
      and explicitly scoped it there. A dribbling origin means progressive
      consumption is possible HERE even though it is not on DW.

Politeness: one playlist poll per interval, at most one prefetch fetch per new
URL, hard cap on total fetches, abort on any 429/403.

Usage: ivs-prefetch-probe.py <media-m3u8-url> [seconds=180]
"""
import re, sys, time, threading, urllib.request, urllib.error

URL = sys.argv[1]
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0
POLL_S = 0.5
MAX_FETCHES = 40

UA = {"User-Agent": "earshot-venue-probe/1.0"}
t0 = time.monotonic()
now = lambda: time.monotonic() - t0

lock = threading.Lock()
first_seen = {}      # prefetch url -> t when it first appeared as EXT-X-PREFETCH
announced = {}       # url -> t when it first appeared as a NORMAL segment
fetch_rec = {}       # url -> dict of fetch timings
aborted = threading.Event()
n_fetches = 0


def get_playlist():
    req = urllib.request.Request(URL, headers=UA)
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read().decode("utf-8", "replace")


def fetch_prefetch(url):
    """Fetch a prefetch URL and time the body. Answers Q2."""
    rec = {"t_req": now()}
    try:
        req = urllib.request.Request(url, headers=UA)
        r = urllib.request.urlopen(req, timeout=20)
        rec["status"] = r.status
        rec["t_hdr"] = now()
        rec["clen"] = r.headers.get("Content-Length")
        rec["tenc"] = r.headers.get("Transfer-Encoding")
        n, chunks, first_body = 0, 0, None
        while True:
            b = r.read(16384)
            if not b:
                break
            if first_body is None:
                first_body = now()
            n += len(b)
            chunks += 1
        rec["t_end"] = now()
        rec["t_first_body"] = first_body
        rec["bytes"] = n
        rec["reads"] = chunks
    except urllib.error.HTTPError as e:
        rec["status"] = e.code
        rec["t_end"] = now()
        if e.code in (429, 403):
            aborted.set()
            rec["abort"] = "rate-limited/forbidden — probe stopped"
    except Exception as e:
        rec["status"] = f"ERR {type(e).__name__}: {e}"
        rec["t_end"] = now()
    with lock:
        fetch_rec[url] = rec


threads = []
polls = 0
while now() < DURATION and not aborted.is_set():
    try:
        body = get_playlist()
    except Exception as e:
        print(f"[{now():6.1f}] playlist error: {e}")
        time.sleep(POLL_S)
        continue
    polls += 1
    t = now()
    segs = [l.strip() for l in body.splitlines()
            if l.strip().startswith("http") and "EXT-X-PREFETCH" not in l]
    pres = [l.split(":", 1)[1].strip() for l in body.splitlines()
            if l.startswith("#EXT-X-PREFETCH")]

    with lock:
        for s in segs:
            announced.setdefault(s, t)
        new = [p for p in pres if p not in first_seen]
        for p in pres:
            first_seen.setdefault(p, t)

    for p in new:
        if n_fetches >= MAX_FETCHES:
            break
        n_fetches += 1
        th = threading.Thread(target=fetch_prefetch, args=(p,), daemon=True)
        th.start()
        threads.append(th)

    time.sleep(POLL_S)

for th in threads:
    th.join(timeout=25)

print(f"\n=== IVS PREFETCH PROBE — {polls} polls over {now():.0f} s, "
      f"{len(first_seen)} distinct prefetch URLs, {n_fetches} fetched")
if aborted.is_set():
    print("!! ABORTED on 429/403 — treat all numbers below as partial")

print("\n--- Q1: L_prefetch = t(announced as normal segment) - t(first seen as prefetch)")
Ls = []
for u, ts in sorted(first_seen.items(), key=lambda kv: kv[1]):
    if u in announced:
        L = announced[u] - ts
        Ls.append(L)
print(f"  matched {len(Ls)} of {len(first_seen)} prefetch URLs to a later announcement")
if Ls:
    Ls.sort()
    print(f"  L median = {Ls[len(Ls)//2]:.3f} s   min {Ls[0]:.3f}   max {Ls[-1]:.3f}")
    print(f"  all: {[round(x,2) for x in Ls]}")
else:
    print("  NOT RESOLVABLE — no prefetch URL was later seen as a normal segment")
    print("  (either the run was too short, or IVS rewrites the token per manifest)")

print("\n--- Q2: complete-on-publish, or does the body dribble?")
for u, r in sorted(fetch_rec.items(), key=lambda kv: kv[1]["t_req"])[:12]:
    if not isinstance(r.get("status"), int) or r["status"] != 200:
        print(f"  [{r['t_req']:6.1f}] status={r.get('status')}")
        continue
    hdr = r["t_hdr"] - r["t_req"]
    body = r["t_end"] - (r.get("t_first_body") or r["t_hdr"])
    print(f"  [{r['t_req']:6.1f}] 200  hdr={hdr*1000:6.0f}ms  body={body*1000:7.0f}ms  "
          f"bytes={r['bytes']:>7}  reads={r['reads']:>3}  "
          f"clen={r['clen']}  tenc={r['tenc']}")
print("\n  READ: body >> one RTT with tenc=chunked and no Content-Length => the")
print("  origin streams a segment WHILE IT IS BEING WRITTEN. That is progressive")
print("  consumption, which F2 ruled out for DW/Akamai and never tested here.")
print("\n=== IVS-PROBE-DONE")
