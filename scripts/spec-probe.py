#!/usr/bin/env python3
"""Per-venue GO/NO-GO for the speculative fetcher, and the `L` term of the race.

Speculation works only where two things hold, and both are venue-specific:

  1. segment URLs are numerically PREDICTABLE (we must be able to name N+1), and
  2. a segment is FETCHABLE by that URL before the manifest announces it.

(2) is the whole lever — measured +1.02 s at 6 s segments and +1.40 s at 1.92 s,
i.e. a fixed origin property (most likely the manifest's CDN cache TTL) rather
than a fraction of a segment. It is also exactly the `L` in the race arithmetic:
our measured deficit was 1.530 s, so a venue's lead tells us how much of that
speculation can take back there (docs/LATENCY-PROOF.md).

Run it before arming a venue. NO-GO means stage the non-speculative template and
expect no improvement — better to know beforehand than to discover it from a
flat race.

Politeness (a block hits EVERY leg on the box, not just this probe): one probe
per 200 ms, bounded attempts per segment, and any 429/403 aborts immediately.

Usage: spec-probe.py <media-m3u8> [n_segments=5]
Called by scripts/manifest-probe.sh; also runnable standalone.
"""

import importlib.util
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request

UA = {"User-Agent": "earshot-spec-probe/1"}
PROBE_SPACING_S = 0.2
MAX_WAIT_PER_SEG_S = 25.0


def _load_fetcher_helpers():
    """Reuse the production fetcher's URL logic so the probe cannot disagree
    with the leg about what 'predictable' means."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hls-edge-fetch.py")
    spec = importlib.util.spec_from_file_location("_hef", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.bump_uri, mod.pattern_is_predictable


def get(url, timeout=8):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)


def media_segments(url):
    body = get(url).read().decode("utf-8", "replace")
    segs = [l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")]
    seq = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", body)
    return segs, (int(seq.group(1)) if seq else None)


def main() -> int:
    url = sys.argv[1]
    want = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    bump_uri, pattern_is_predictable = _load_fetcher_helpers()
    base = url.rsplit("/", 1)[0]

    try:
        segs, _ = media_segments(url)
    except Exception as e:
        print(f"SPECULATIVE: NO-GO (cannot read media playlist: {e})")
        return 0
    if len(segs) < 3:
        print("SPECULATIVE: NO-GO (playlist too short to judge URL pattern)")
        return 0

    if not pattern_is_predictable(segs):
        print(f"segment URI sample:  {segs[-1]}")
        print("SPECULATIVE: NO-GO (segment URLs are not numerically predictable)")
        return 0
    print(f"segment URI sample:  {segs[-1]}")
    print("URL pattern:         predictable (N+1 is nameable)")

    leads, aborted = [], None
    last = segs[-1]
    for _ in range(want):
        target = bump_uri(last)
        if not target:
            break
        t_fetch = t_ann = None
        t0 = time.time()
        while time.time() - t0 < MAX_WAIT_PER_SEG_S and (t_fetch is None or t_ann is None):
            if t_fetch is None:
                try:
                    r = get(base + "/" + target, timeout=4)
                    r.read(2048)
                    r.close()
                    t_fetch = time.time()
                except urllib.error.HTTPError as e:
                    if e.code in (403, 429):
                        aborted = e.code
                        break
                except Exception:
                    pass
            if t_ann is None:
                try:
                    if target in media_segments(url)[0]:
                        t_ann = time.time()
                except Exception:
                    pass
            time.sleep(PROBE_SPACING_S)
        if aborted:
            break
        if t_fetch and t_ann:
            leads.append(t_ann - t_fetch)
        last = target

    if aborted:
        print(f"SPECULATIVE: NO-GO (HTTP {aborted} — origin rate-limits/forbids direct probes)")
        return 0
    if len(leads) < 3:
        print(f"SPECULATIVE: NOT RESOLVABLE (only {len(leads)} clean samples)")
        return 0

    med = statistics.median(leads)
    print("fetchable-vs-announced: " + ", ".join(f"{x:+.2f}" for x in leads) + " s")
    print(f"median lead (L):     {med:+.2f} s   (n={len(leads)}, "
          f"range {min(leads):+.2f}..{max(leads):+.2f})")
    if med < 0.2:
        print(f"SPECULATIVE: NO-GO (lead {med:+.2f} s — nothing meaningful to win here)")
    else:
        print(f"SPECULATIVE: GO (lead ~{med:.2f} s)")
        # The reference deficit ALREADY contains our own 0.289 s path; do not add
        # it again (that double-count is the error this whole investigation began
        # by correcting — docs/LATENCY-PROOF.md §THE GAP IS LOCATED).
        proj = -(1.530 - med)
        verdict = "AHEAD" if proj >= 0 else "behind"
        print(f"  race arithmetic: reference deficit 1.530 s (AXP NFL, incl. our "
              f"0.289 s path) − L {med:.2f} s ⇒ Δrace ~ {proj:+.2f} s ({verdict})")
        print("  ⚠ venue-specific: this L applies to THIS origin, and the 1.530 s "
              "reference came from AXP/IVS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
