#!/usr/bin/env python3
"""F1 — enumerate EVERY rendition in an HLS master playlist, not just the first.

`manifest-probe.sh` and `hls-edge-fetch.py` both follow the first
`EXT-X-STREAM-INF` and stop, so nothing in this system has ever looked at the
other renditions. That matters because a master playlist routinely carries:

  * variants with DIFFERENT segment durations (a shorter one is structurally
    closer to the live edge)
  * separate `EXT-X-MEDIA TYPE=AUDIO` tracks with their own timing — and we
    want audio only, so a video variant may be carrying dead weight
  * different packaging (MPEG-TS vs fMP4/CMAF); CMAF is the packaging LL-HLS
    partial segments require
  * per-variant `EXT-X-SERVER-CONTROL` — a venue can advertise low-latency
    capability on one rendition and not another

Read-only: one GET for the master, one per rendition. Reports a table and names
the rendition a latency-seeking client should prefer. It does NOT switch
anything — feeding the choice to the fetcher is a separate, deliberate step.

Usage: variant-audit.py "<master-m3u8-url>"
"""
import re
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = {"User-Agent": "earshot-variant-audit/1"}
TIMEOUT = 15


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def attrs(line):
    """Parse an HLS attribute list, respecting quoted commas."""
    out, buf, q = {}, "", False
    for ch in line.split(":", 1)[1] if ":" in line else "":
        if ch == '"':
            q = not q
        if ch == "," and not q:
            if "=" in buf:
                k, v = buf.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
            buf = ""
        else:
            buf += ch
    if "=" in buf:
        k, v = buf.split("=", 1)
        out[k.strip()] = v.strip().strip('"')
    return out


def parse_master(body, base):
    """Return [(kind, name, uri, attrs)] for every rendition in the master."""
    rends, lines = [], body.splitlines()
    for i, ln in enumerate(lines):
        ln = ln.strip()
        if ln.startswith("#EXT-X-STREAM-INF"):
            a = attrs(ln)
            uri = next((l.strip() for l in lines[i + 1:] if l.strip() and not l.startswith("#")), None)
            if uri:
                bw = a.get("BANDWIDTH", "?")
                res = a.get("RESOLUTION", "audio-only?")
                rends.append(("variant", f"{bw}bps {res}", urllib.parse.urljoin(base, uri), a))
        elif ln.startswith("#EXT-X-MEDIA:") and "URI=" in ln:
            a = attrs(ln)
            if a.get("URI"):
                nm = f"{a.get('TYPE','?')} {a.get('NAME', a.get('GROUP-ID',''))}"
                rends.append(("media", nm, urllib.parse.urljoin(base, a["URI"]), a))
    return rends


def describe(url):
    """Fetch a media playlist and pull out everything latency-relevant."""
    try:
        body = fetch(url)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        return {"error": str(e)}
    if "#EXT-X-STREAM-INF:" in body:
        return {"error": "nested master (not a media playlist)"}

    extinf = [float(m) for m in re.findall(r"^#EXTINF:([\d.]+)", body, re.M)]
    segs = [l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")]
    target = re.search(r"^#EXT-X-TARGETDURATION:(\d+)", body, re.M)
    sc = re.search(r"^#EXT-X-SERVER-CONTROL:(.*)$", body, re.M)
    parts = len(re.findall(r"^#EXT-X-PART:", body, re.M))
    fmp4 = bool(re.search(r"^#EXT-X-MAP:", body, re.M))
    ext = ""
    if segs:
        path = urllib.parse.urlparse(segs[-1]).path
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "target_s": int(target.group(1)) if target else None,
        "extinf_med": statistics.median(extinf) if extinf else None,
        "segments": len(segs),
        "window_s": sum(extinf) if extinf else None,
        "packaging": "fMP4/CMAF" if fmp4 else (ext.upper() or "?"),
        "server_control": sc.group(1).strip() if sc else "",
        "parts": parts,
        "pdt": body.count("#EXT-X-PROGRAM-DATE-TIME"),
    }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1]
    body = fetch(url)

    # The colons are load-bearing: "#EXT-X-MEDIA" is a substring of
    # "#EXT-X-MEDIA-SEQUENCE", which every media playlist carries, so the
    # bare prefix reports "master playlist" for literally every live stream.
    if "#EXT-X-STREAM-INF:" not in body and "#EXT-X-MEDIA:" not in body:
        print(f"NOT A MASTER PLAYLIST — {url} is already a media playlist.")
        print("Nothing to enumerate; the single-variant probe was never wrong here.")
        d = describe(url)
        print(f"  target={d.get('target_s')}s extinf={d.get('extinf_med')}s "
              f"packaging={d.get('packaging')} parts={d.get('parts')}")
        return 0

    rends = parse_master(body, url)
    print(f"master: {url}")
    print(f"renditions found: {len(rends)}   "
          f"(everything before today followed ONLY the first)\n")

    hdr = f"{'#':>2} {'kind':<8} {'name':<26} {'target':>7} {'extinf':>7} {'segs':>5} {'window':>7} {'pkg':<10} {'parts':>6} {'PDT':>4}"
    print(hdr)
    print("-" * len(hdr))
    rows = []
    for i, (kind, name, u, a) in enumerate(rends):
        d = describe(u)
        if "error" in d:
            print(f"{i:>2} {kind:<8} {name[:26]:<26}   ERROR: {d['error'][:50]}")
            continue
        rows.append((name, u, d, a))
        # Formatted in plain statements, not nested f-strings: same-quote
        # nesting is a 3.12+ feature and the box's python must not be assumed.
        s_target = ("%ds" % d["target_s"]) if d["target_s"] is not None else "-"
        s_extinf = ("%.2fs" % d["extinf_med"]) if d["extinf_med"] else "-"
        s_window = ("%.0fs" % d["window_s"]) if d["window_s"] else "-"
        print("%2d %-8s %-26s %7s %7s %5d %7s %-10s %6d %4d"
              % (i, kind, name[:26], s_target, s_extinf, d["segments"],
                 s_window, d["packaging"], d["parts"], d["pdt"]))

    print()
    if not rows:
        print("VERDICT: no rendition could be read — treat as NO CHANGE.")
        return 1

    for name, u, d, a in rows:
        if d["server_control"]:
            print(f"  SERVER-CONTROL on {name}: {d['server_control']}")
    if any(r[2]["parts"] for r in rows):
        print("  ⚠ EXT-X-PART present on at least one rendition — LL-HLS partial "
              "segments are available HERE even if the first variant hid it.")

    timed = [r for r in rows if r[2]["extinf_med"]]
    if timed:
        best = min(timed, key=lambda r: r[2]["extinf_med"])
        first = timed[0]
        gain = (first[2]["extinf_med"] - best[2]["extinf_med"])
        print()
        print(f"  default (first variant): {first[0]}  {first[2]['extinf_med']:.2f}s segments")
        print(f"  shortest segments:       {best[0]}  {best[2]['extinf_med']:.2f}s segments")
        if gain > 0.01:
            print(f"  => {gain:.2f}s shorter per segment.")
            print("     NOTE (E0): shorter segments do NOT reduce the expected")
            print("     lookahead cost — exposure is A/S, so a smaller S is hit MORE")
            print("     often for LESS each time. Same expected cost, different shape.")
            print("     The gain to measure is the EDGE lag, via spec-probe.py on this URI:")
            print(f"     {best[1]}")
        else:
            print("  => no rendition has shorter segments than the default. NULL on that axis.")

    audio = [r for r in rows if r[3].get("TYPE") == "AUDIO"
             or "audio-only" in r[0] or ("CODECS" in r[3] and "avc" not in r[3].get("CODECS", ""))]
    if audio:
        print()
        print("  audio-only renditions (we decode audio; video bytes are dead weight):")
        for name, u, d, a in audio:
            print(f"    {name}  {u}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
