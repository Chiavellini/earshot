#!/usr/bin/env python3
"""Minimal-edge HLS fetcher — S2 prototype.

Polls the media playlist aggressively (default 250ms) and streams each newly
published segment's bytes to stdout THE INSTANT it appears, instead of waiting
for a segment-duration reload interval (streamlink's --hls-playlist-reload-time
segment). Bytes → stdout → ffmpeg → PCM, same as our other legs.

The whole point: measure how much manifest-poll phase costs us. If we currently
reload every 2s (IVS) but poll here every 250ms, this leg should lead the
streamlink leg by ~0-1s (the avoidable poll-phase latency).

Usage: edge-fetch.py <media-playlist-url> [poll_ms]
Resolves one level of master playlist. Writes raw segment bytes to stdout.
"""
import sys, time, urllib.request, urllib.parse

def get(url, timeout=4):
    req = urllib.request.Request(url, headers={"User-Agent":"edge-fetch/1"})
    return urllib.request.urlopen(req, timeout=timeout).read()

def resolve_media(url):
    body = get(url).decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" in body:  # master → pick first variant
        base = url.rsplit("/",1)[0]
        for line in body.splitlines():
            line=line.strip()
            if line and not line.startswith("#"):
                return urllib.parse.urljoin(url, line)
    return url

def main():
    media = resolve_media(sys.argv[1])
    poll = (float(sys.argv[2]) if len(sys.argv)>2 else 250)/1000.0
    base = media.rsplit("/",1)[0] + "/"
    seen = set()
    out = sys.stdout.buffer
    first = True
    sys.stderr.write(f"edge-fetch: media={media} poll={poll*1000:.0f}ms\n"); sys.stderr.flush()
    while True:
        t0=time.time()
        try:
            body = get(media, timeout=poll+2).decode("utf-8","replace")
        except Exception as e:
            sys.stderr.write(f"poll err {e}\n"); time.sleep(poll); continue
        lines=body.splitlines()
        segs=[l.strip() for l in lines if l.strip() and not l.startswith("#")]
        # on first pass, only take the NEWEST segment (live edge), then track new ones
        new = [s for s in segs if s not in seen]
        for s in new: seen.add(s)
        if first:
            new = new[-1:]  # start at the newest only
            first=False
        for s in new:
            try:
                data = get(urllib.parse.urljoin(base, s), timeout=poll+3)
                out.write(data); out.flush()
            except Exception as e:
                sys.stderr.write(f"seg err {s} {e}\n")
        dt=time.time()-t0
        if dt<poll: time.sleep(poll-dt)

if __name__=="__main__":
    main()
