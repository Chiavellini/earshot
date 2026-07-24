#!/usr/bin/env python3
"""Phase-locked minimal-edge HLS fetcher — production source-leg (S2).

Polls the media playlist aggressively (default 250 ms) and streams each newly
published segment's bytes to stdout THE INSTANT it appears, instead of waiting
a full segment-duration reload interval (streamlink's
`--hls-playlist-reload-time segment`). Piped into ffmpeg it becomes a normal
`kind="pipe"` leg; the fanout arbiter OR's it with the other legs.

Measured basis: docs/LATENCY-PROOF.md 2026-07-24 — reload-per-segment holds
audio ~2.25 s staler than 250 ms phase-locked polling (PDT-anchored on DW).

Design invariants that make this a low-latency consumer (not just a downloader):
  * SKIP-TO-NEWEST catch-up — if downloads fall behind and >`max_backlog`
    fresh segments are pending, drop the stale ones and jump to the newest.
    A backlog would REVERSE the freshness win (drift behind live).
  * NEVER crash the leg — every network/parse error is logged to stderr and
    the loop continues. A crash kills the leg (fanout tolerates it, but we
    want it alive). The downstream ffmpeg carries `-fflags +discardcorrupt`
    to tolerate the TS discontinuity a skip introduces.
  * bounded memory (`seen` set is trimmed), keep-alive-free simple GETs.

Usage: hls-edge-fetch.py <media-or-master-url> [poll_ms] [max_backlog]
Writes raw segment bytes to stdout; diagnostics to stderr.
"""
import sys, time, urllib.request, urllib.parse

UA = {"User-Agent": "earshot-edge-fetch/1"}


def get(url, timeout=4):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def resolve_media(url, timeout=4):
    """Follow one level of master → first variant. Returns the media URL."""
    body = get(url, timeout).decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" in body:
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return urllib.parse.urljoin(url, line)
    return url


def parse_pdt(line):
    try:
        s = line.split(":", 1)[1].strip().replace("Z", "+00:00")
        import datetime
        return datetime.datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("usage: hls-edge-fetch.py <url> [poll_ms] [max_backlog]\n")
        return 2
    src = sys.argv[1]
    poll = (float(sys.argv[2]) if len(sys.argv) > 2 else 250) / 1000.0
    max_backlog = int(sys.argv[3]) if len(sys.argv) > 3 else 2

    # resolve master→media once, but tolerate startup failure with retries
    media = None
    for _ in range(20):
        try:
            media = resolve_media(src, timeout=poll + 2)
            break
        except Exception as e:
            sys.stderr.write(f"resolve retry: {e}\n"); sys.stderr.flush(); time.sleep(1.0)
    if media is None:
        sys.stderr.write("FATAL: could not resolve media playlist after retries\n")
        return 1
    base = media.rsplit("/", 1)[0] + "/"

    out = sys.stdout.buffer
    seen = set()
    seen_order = []
    first = True
    n_skip = 0
    sys.stderr.write(f"edge-fetch: media={media} poll={poll*1000:.0f}ms max_backlog={max_backlog}\n")
    sys.stderr.flush()

    while True:
        t0 = time.time()
        try:
            body = get(media, timeout=poll + 2).decode("utf-8", "replace")
        except Exception as e:
            sys.stderr.write(f"poll err: {e}\n"); sys.stderr.flush()
            time.sleep(poll); continue

        # pair each segment URI with the PDT that precedes it (diagnostics)
        pdt = None; pairs = []
        for l in body.splitlines():
            l = l.strip()
            if l.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                pdt = parse_pdt(l)
            elif l and not l.startswith("#"):
                pairs.append((l, pdt)); pdt = None

        new = [(u, p) for (u, p) in pairs if u not in seen]
        for u, _ in new:
            seen.add(u); seen_order.append(u)
        # bound memory: keep last ~500 segment keys
        if len(seen_order) > 600:
            for u in seen_order[:-500]:
                seen.discard(u)
            seen_order = seen_order[-500:]

        if first:
            new = new[-1:]  # start at the live edge (newest only)
            first = False
        elif len(new) > max_backlog:
            # SKIP-TO-NEWEST: we fell behind — drop stale, jump to newest.
            dropped = len(new) - 1
            n_skip += dropped
            u_last, p_last = new[-1]
            if p_last:
                sys.stderr.write(f"catch-up: dropped {dropped} stale seg(s) (total skipped {n_skip})\n")
                sys.stderr.flush()
            new = new[-1:]

        for u, p in new:
            try:
                data = get(urllib.parse.urljoin(base, u), timeout=poll + 3)
                out.write(data); out.flush()
                if p:
                    age = time.time() - p
                    sys.stderr.write(f"seg age={age:.2f}s\n"); sys.stderr.flush()
            except Exception as e:
                sys.stderr.write(f"seg err {u}: {e}\n"); sys.stderr.flush()

        dt = time.time() - t0
        if dt < poll:
            time.sleep(poll - dt)


if __name__ == "__main__":
    sys.exit(main() or 0)
