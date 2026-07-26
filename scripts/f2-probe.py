#!/usr/bin/env python3
"""F2 — when a segment FIRST becomes fetchable, is it complete or still being written?

The ledger has carried "speculative chunked fetch — untested" for the whole
campaign, against S3's competing model that the object simply 404s until it is
complete. They imply opposite things about whether progressive consumption is
even possible, and nothing has separated them.

Discriminator, at the instant N+1 first returns 200:
  * Content-Length present AND body arrives in ~one RTT   => COMPLETE on publish.
    Progressive consumption is impossible here; --stream-body's refutation is
    explained rather than merely observed.
  * Transfer-Encoding: chunked AND the body dribbles in over ~a segment
    => the origin streams a segment being written. That IS a lever.
"""
import re, sys, time, urllib.request, urllib.parse, http.client

URL = open(sys.argv[1]).read().strip() if len(sys.argv) > 1 else sys.argv[1]
UA = {"User-Agent": "earshot-f2/1"}
BASE = URL.rsplit("/", 1)[0]
NET = urllib.parse.urlparse(URL).netloc

def bump(uri, k=1):
    p, sep, q = uri.partition("?")
    m = list(re.finditer(r"\d+", p))[-1]
    return p[: m.start()] + str(int(m.group()) + k) + p[m.end():] + sep + q

def manifest_last():
    b = urllib.request.urlopen(urllib.request.Request(URL, headers=UA), timeout=10).read().decode()
    segs = [l.strip() for l in b.splitlines() if l.strip() and not l.startswith("#")]
    ext = [float(x) for x in re.findall(r"^#EXTINF:([\d.]+)", b, re.M)]
    return segs[-1], (ext[-1] if ext else None)

last, seg_dur = manifest_last()
print(f"segment duration ~{seg_dur}s; chasing N+1 of {last.rsplit('/',1)[-1]}")
target = bump(last)
t0 = time.monotonic()
tries = 0
while time.monotonic() - t0 < (seg_dur or 6) * 2.5:
    tries += 1
    try:
        c = http.client.HTTPSConnection(NET, timeout=10)
        p = urllib.parse.urlparse(BASE + "/" + target)
        c.request("GET", p.path + (("?" + p.query) if p.query else ""), headers=UA)
        r = c.getresponse()
        if r.status == 200:
            hdr_t = time.monotonic()
            clen = r.getheader("Content-Length")
            tenc = r.getheader("Transfer-Encoding")
            body = r.read()
            done_t = time.monotonic()
            print(f"\n200 after {tries} probes, {hdr_t-t0:.2f}s into the chase")
            print(f"  Content-Length : {clen}")
            print(f"  Transfer-Encoding: {tenc}")
            print(f"  bytes received : {len(body):,}")
            print(f"  header->body   : {(done_t-hdr_t)*1000:.0f} ms")
            print()
            if tenc and "chunked" in tenc.lower() and (done_t - hdr_t) > (seg_dur or 6) * 0.25:
                print("VERDICT: STREAMED — the origin serves a segment while it is still")
                print("being written. Progressive consumption IS possible here.")
            else:
                print("VERDICT: COMPLETE ON PUBLISH — the object does not exist until it is")
                print("whole, then transfers in ~one RTT. Progressive consumption is")
                print("IMPOSSIBLE at this origin, which explains --stream-body's null.")
            c.close(); sys.exit(0)
        c.close()
    except Exception:
        pass
    time.sleep(0.2)
print(f"NOT RESOLVABLE — N+1 never returned 200 in {tries} probes")
