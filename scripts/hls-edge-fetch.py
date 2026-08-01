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

SPECULATIVE MODE (`--speculative`, measured 2026-07-24):
Segment URLs are numerically predictable, and on the reference stream a segment
is fetchable by predicted URL a median **+1.02 s BEFORE the manifest announces
it** (n=5, range −0.77..+2.89) — almost exactly the ~1.06 s lead the competitor
holds over us (docs/LATENCY-PROOF.md §THE GAP IS LOCATED). So instead of waiting
to be told a segment exists, we ask for it.

This is a MORE AGGRESSIVE consumer than a normal HLS client: it spends 404s to
buy latency. The politeness budget is not optional — a rate-limit or IP block
hits every leg on the box, not just this one:
  * probing starts only after `spec_start_frac` of a segment duration has passed
  * at most `spec_max_probes` requests per segment, at `spec_poll_ms` spacing
  * ANY 429/403 permanently disables speculation and falls back to
    manifest-driven for the rest of the run (loud on stderr)
  * the URL pattern is VERIFIED against consecutive real segments before
    speculation is enabled at all; an unpredictable pattern disables it

PREFETCH (added 2026-07-27):
Speculation above assumes numerically predictable URLs. **Amazon IVS — Q4 Inc's
platform, i.e. AXP and PayPal — uses opaque signed segment tokens**, so the
pattern check disables speculation and it buys ZERO there. What IVS ships
instead is `#EXT-X-PREFETCH`: the manifest NAMES segments that do not exist yet.
Measured on the AWS IVS demo channel (LATENCY-PROOF §V1): 4.18 s of announcement
lead, bodies chunked with no Content-Length. Never measured on a real issuer
channel. Two flags, deliberately separate:

  `--prefetch-report`  OBSERVE-ONLY. Times the two sightings of each prefetch
      URL and reports the lead. **Zero extra HTTP requests**, no audio-path
      change — safe to arm on a live event, and armed on the 07-28 legs.

  `--prefetch`         FETCHES prefetch URLs, buffered and serial, instead of
      waiting for announcement. Banks a flat **~0.92 s** per segment.
      UNVALIDATED — needs the 90-min drop-safety gate the speculative posture
      had to pass (LATENCY-PROOF S7) before any armed config uses it.

Why serial suffices for the buffered tier: a prefetch fetch occupies ~3.3 s from
when its URL appears (origin holds headers ~1.9 s while the segment is still
being written, then dribbles ~1.4 s) against a ~2 s URL cadence, which looks like
it needs ~1.65 concurrent connections. It does not — the segment COMPLETES ~3.3 s
after the URL appears regardless of when we ask, so asking early and blocking
costs nothing and the loop re-synchronises at one fetch per ~2 s. The remaining
~1.4 s (first byte at 1.9 s rather than complete at 3.3 s) DOES need concurrency
plus ordered emission, and is not attempted here.

Usage: hls-edge-fetch.py <media-or-master-url> [poll_ms] [max_backlog]
                         [--speculative] [--spec-start-frac 0.8]
                         [--spec-poll-ms 200] [--spec-max-probes 15]
                         [--prefetch-report] [--prefetch]
Writes raw segment bytes to stdout; diagnostics to stderr.
"""
import argparse, hashlib, re, sys, time, urllib.error, urllib.request, urllib.parse

UA = {"User-Agent": "earshot-edge-fetch/1"}

# A manifest-announced segment can 404 at the edge briefly (S3 negative-cache
# lag). Retry that many manifest polls (~250 ms apart) before declaring it lost.
SEG_RETRY_MAX = 12


def get(url, timeout=4):
    """Whole-body fetch — for the MANIFEST only (it must be parsed as a unit)."""
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


TS_PACKET = 188
CHUNK_BYTES_LEGACY = 32768      # the historical constant; still the default
CHUNK_BYTES_MIN = 10 * TS_PACKET   # 1880 B — small enough to be ~0 at any bitrate
CHUNK_BYTES_MAX = CHUNK_BYTES_LEGACY


def chunk_bytes_for(chunk_ms, bytes_per_sec):
    """How many bytes should one read block for, to cost at most `chunk_ms` of media?

    WHY THIS EXISTS (measured 2026-07-30, AWS IVS demo channel). `stream_to`'s
    read size is denominated in BYTES, and `r.read(chunk)` BLOCKS until `chunk`
    bytes have arrived. While the origin is still writing a segment it dribbles at
    roughly the media rate, so a byte-denominated read costs `chunk / bitrate`
    SECONDS of latency on every word — and that cost silently scales with the
    rendition:

        demo 230 kbps rendition (Q4-comparable): 17 bursts, median 2820 B, over
          1.848 s. First bytes at t=0.133 s; our 32768-byte read did not return
          until t=1.240 s. QUANTUM COST 1.107 s, against a predicted
          32768/28750 = 1.140 s.
        demo ~1.9 Mbit/s rendition: the origin's FIRST burst already exceeds
          32 KB, so the same constant costs ~0.001 s.

    Same code, same origin, same day — a 1000x difference decided purely by
    bitrate. That is why this is now expressed in MILLISECONDS OF MEDIA.

    It also defuses a latent trap: `pick_variant` deliberately prefers the
    cheapest / audio-only rendition, which is the LOWEST bitrate available — i.e.
    exactly the case where a fixed byte quantum is worst. Handing the fetcher a
    master (so `pick_variant` can run) while the quantum stayed byte-denominated
    would have quietly traded ~10 ms of transfer saving for ~0.7 s of added
    latency.

    `chunk_ms = 0` keeps the legacy 32768-byte behaviour exactly, so this is inert
    until switched on. Reads are floored to whole TS packets because a partial
    packet is what produces the `short` reads the ledger tracks.
    """
    if not chunk_ms:
        return CHUNK_BYTES_LEGACY
    if not bytes_per_sec or bytes_per_sec <= 0:
        # No bitrate estimate yet (first segment of a run). Start small: being
        # early costs nothing, and the estimate lands after one segment.
        return CHUNK_BYTES_MIN
    want = int(bytes_per_sec * chunk_ms / 1000.0)
    want = max(CHUNK_BYTES_MIN, min(CHUNK_BYTES_MAX, want))
    return max(TS_PACKET, (want // TS_PACKET) * TS_PACKET)


def stream_to(url, out, timeout=4, chunk=CHUNK_BYTES_LEGACY):
    """Stream a SEGMENT body to `out` as bytes arrive. Returns (written, short).

    Why not `get()` (measured 2026-07-24): buffering the whole segment before
    writing a byte leaves ~0.94 s of already-arrived audio sitting in memory —
    on the reference stream the body's first byte landed at 1.753 s and its last
    at 2.697 s. Writing progressively lets ffmpeg decode the head of a segment
    while its tail is still in flight, which is exactly the "~1 segment of
    waiting" term in docs/LATENCY-PROOF.md.

    A short read leaves a partial TS packet; downstream legs carry
    `-fflags +discardcorrupt` for precisely that. We report it rather than
    raising, because dropping the whole leg would cost far more than a glitch.

    Contract: this raises ONLY before any byte is written (connect/HTTP errors).
    A mid-body failure is swallowed and reported via `short`, because the caller
    has already committed those bytes to the stream — retrying would duplicate
    audio, which both confuses the detector and pushes us further behind live.
    """
    r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
    try:
        expect = int(r.headers.get("Content-Length") or 0)
    except ValueError:
        expect = 0
    written = 0
    t_first = None
    try:
        while True:
            buf = r.read(chunk)
            if not buf:
                break
            if t_first is None:
                t_first = time.time()   # audio starts flowing HERE, not at t_done
            out.write(buf)
            out.flush()      # hand each chunk to ffmpeg immediately — the point
            written += len(buf)
    except BrokenPipeError:
        # Our STDOUT is gone — ffmpeg exited. This is fatal, not a network
        # blip: the "never crash the leg" rule assumes a live consumer, and
        # without one the loop would spin forever hammering the CDN with
        # speculative 404s (observed: 43,812 in a single run, budget cap of 15
        # per segment bypassed entirely, because a dead pipe never advances the
        # segment cursor). Let it propagate.
        raise
    except Exception as e:
        sys.stderr.write(f"mid-body read failed after {written}B: {e}\n"); sys.stderr.flush()
        return written, True, t_first
    finally:
        try:
            r.close()
        except Exception:
            pass
    return written, bool(expect and written < expect), t_first


def fetch_segment(url, out, timeout, stream_body, chunk=CHUNK_BYTES_LEGACY):
    """Emit one segment. Whole-body read by DEFAULT.

    Progressive streaming was the obvious-looking win (~0.94 s of body sat in the
    buffer) and it was REFUTED: -0.197 s median vs a pre-registered >=0.3 s bar
    over 3 interleaved runs, while introducing 3/32/62 short reads per run
    against zero for the buffered path (docs/LATENCY-PROOF.md). Buffered stays
    the default; the path is kept behind --stream-body for a venue whose segment
    bodies genuinely arrive slowly."""
    if stream_body:
        return stream_to(url, out, timeout=timeout, chunk=chunk)
    data = get(url, timeout=timeout)
    out.write(data)
    out.flush()
    # Buffered: nothing reaches the decoder until the whole body is in, so the
    # audio-available instant IS t_done. None signals "use t_done".
    return len(data), False, None


def synth_pdt(first_seen_at, extinf):
    """Synthesise a segment's media-start instant for a venue that publishes no
    `EXT-X-PROGRAM-DATE-TIME`.

    A packager announces a segment once it has finished writing it, so the media
    that segment carries ENDED at ~the announcement instant, and therefore
    STARTED at `announce - EXTINF`.

    The result is biased LATE by the origin's publish + CDN propagation delay
    (measured 0.03 s at Q4/IVS, ≤0.17 s at AMZN — LATENCY-PROOF §Q1 — but never
    measured at a PDT-less venue, which is the point). So a lag computed against
    it is a **lower bound** on true staleness, never an encoder-referenced
    absolute. Callers tag these rows `-synth` for exactly that reason.

    Returns None when either input is missing, so the caller can fall back
    rather than fabricate a number.
    """
    if first_seen_at is None or not extinf or extinf <= 0:
        return None
    return first_seen_at - extinf


def join_key(uri):
    """A short, session-stable key for pairing the same segment across two legs.

    WHY COLUMN 1 IS NOT ENOUGH. `arrival-lead.py` joins two legs by EXACT STRING
    equality on field 0. Where the venue publishes PDT that is perfect — two
    independent sessions share PDTs to the millisecond (verified 75/75 pairs on
    the 2026-07-30 chunk-ms A/B). But a SYNTHESISED anchor is `t_first_seen -
    EXTINF`, and `t_first_seen` is *this leg's own poll instant*, so two legs
    differ by a uniform 0-250 ms and pair with probability ~1/250. The join would
    have returned under 30 pairs and printed NOT RESOLVABLE — a silent null,
    which is the worst possible failure for an A/B.

    So PDT-less rows carry the segment's own name as an extra column. It is
    byte-identical across legs by construction (both parse the same manifest
    text) and contains no spaces, so the reader can treat it as one field.

    Only the last path component, query stripped: enough to be unique within a
    stream, and it keeps the log small at venues whose full URLs are ~500-char
    signed tokens. ⚠ At Amazon IVS those tokens are PER-SESSION — a URL-keyed run
    there produced 0 paired segments — but IVS publishes real PDT and never takes
    this path.
    """
    if not uri:
        return "-"
    return uri.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or "-"


def arrival(log, pdt, t_done, t_first, fetch_secs, mode, key=None):
    """Record when a segment's audio became available, keyed by its PDT.

    ⚠ The obvious key — the segment URL — DOES NOT WORK ACROSS LEGS on IVS, and
    a run keyed that way produced 0 paired segments. Each client session resolves
    its OWN signed media playlist (…use12… vs …use15…) with per-session segment
    URLs. The V2 gate proved prefetch-URL == announced-URL *within* one leg; that
    is a different claim from *across* legs, and conflating them cost a run.

    `EXT-X-PROGRAM-DATE-TIME` is the encoder's timestamp for the media itself, so
    it IS stable across sessions — verified before rebuilding on it: two
    independent sessions shared **14 of 14 PDTs to the millisecond** while
    sharing **0 segment URLs**.

    t_first is when audio STARTS flowing (== t_done for a buffered fetch, which
    emits nothing until the body is complete).
    """
    if log is None:
        return
    # Column 6 is APPENDED, never substituted: column 1 stays a time so every
    # existing lag computation keeps working, and `arrival-lead.py`'s reader
    # already tolerates extra fields. Written only when column 1 is NOT a real
    # encoder PDT — i.e. exactly the rows that cannot otherwise be paired.
    tail = f" {key}" if key else ""
    log.write(f"{pdt:.3f} {t_done:.3f} {fetch_secs:.3f} {mode} {t_first:.3f}{tail}\n")
    log.flush()


def _pf_summary(pf_lead):
    """One-line prefetch verdict for the periodic SUMMARY. Median, not mean —
    the lead distribution is bimodal on poll phase."""
    if not pf_lead:
        return "  prefetch=NONE-SEEN"
    v = sorted(pf_lead.values())
    return f"  prefetch_n={len(v)} lead_med={v[len(v)//2]:.2f}s lead_min={v[0]:.2f}s lead_max={v[-1]:.2f}s"


def unclaim(seen, seen_order, uri):
    """Undo a pre-emptive `seen` claim so the manifest path can still fetch it."""
    seen.discard(uri)
    if seen_order and seen_order[-1] == uri:
        seen_order.pop()


def bump_uri(uri, delta=1):
    """Increment the LAST digit-run in a segment URI's path (query ignored).

    Covers the common shapes (`sequence_1234.ts`, `media_1234.ts`, `1234.ts`).
    Returns None when the URI carries no digits to bump.
    """
    path, sep, query = uri.partition("?")
    runs = list(re.finditer(r"\d+", path))
    if not runs:
        return None
    m = runs[-1]
    width = len(m.group())
    nxt = str(int(m.group()) + delta)
    if len(nxt) < width:                       # preserve zero padding
        nxt = nxt.rjust(width, "0")
    return path[: m.start()] + nxt + path[m.end():] + sep + query


def pattern_is_predictable(uris):
    """True iff bumping URI[i] by 1 reproduces URI[i+1] for every consecutive pair."""
    if len(uris) < 3:
        return False
    return all(bump_uri(a) == b for a, b in zip(uris[-3:-1], uris[-2:]))


def pick_variant(body):
    """Choose the CHEAPEST variant from a master playlist body, or None.

    We decode to 16 kHz mono — video bits are pure waste. Ford 2026-07-28: the
    old first-variant rule pulled the 5.86 Mbps 1080p rendition onto a
    1-physical-core box (two detect chains sharing it) while a 638 kbps 640x360
    rendition sat unused. Preference order:
      1. audio-only variants (no RESOLUTION attribute), lowest BANDWIDTH;
      2. else lowest BANDWIDTH overall;
      3. else (no parseable BANDWIDTH) the first variant, as before.
    """
    cands = []          # (has_video, bandwidth, order, uri)
    attrs = None
    order = 0
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = line.split(":", 1)[1]
        elif line and not line.startswith("#") and attrs is not None:
            m = re.search(r"(?:^|[,:])BANDWIDTH=(\d+)", attrs)
            bw = int(m.group(1)) if m else None
            has_video = "RESOLUTION=" in attrs
            cands.append((has_video, bw, order, line))
            attrs = None
            order += 1
    if not cands:
        return None
    if any(bw is None for _, bw, _, _ in cands):
        return cands[0][3]                      # unparseable master: old rule
    return min(cands, key=lambda c: (c[0], c[1], c[2]))[3]


def resolve_media(url, timeout=4):
    """Follow one level of master → cheapest variant. Returns the media URL."""
    body = get(url, timeout).decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" in body:
        uri = pick_variant(body)
        if uri is not None:
            return urllib.parse.urljoin(url, uri)
    return url


def parse_pdt(line):
    try:
        s = line.split(":", 1)[1].strip().replace("Z", "+00:00")
        import datetime
        return datetime.datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def selftest():
    """Pure-function regression check. `tests/` is Rust-only, so this ships here
    and can be called from scripts/preflight-event.sh."""
    assert bump_uri("a/sequence_297487012.ts") == "a/sequence_297487013.ts"
    assert bump_uri("media_0009.ts") == "media_0010.ts", "zero padding"
    assert bump_uri("live-audio_eng=64000-video=500000-929648094.ts") == \
        "live-audio_eng=64000-video=500000-929648095.ts", "last digit-run only"
    assert bump_uri("seg_41.ts?token=abc123") == "seg_42.ts?token=abc123", "query untouched"
    assert bump_uri("live.ts") is None, "no digits => not predictable"
    assert pattern_is_predictable(["s_1.ts", "s_2.ts", "s_3.ts"])
    assert not pattern_is_predictable(["s_1.ts", "s_2.ts", "s_9.ts"]), "gap must reject"
    assert not pattern_is_predictable(["s_1.ts", "s_2.ts"]), "too few to judge"
    # IVS shape: opaque signed tokens => speculation must refuse to engage.
    assert not pattern_is_predictable([
        "/v1/segment/CucCQjX4C-Pa2l0Xm8nl4YThczGSYTr8", "/v1/segment/CucCvif_NIU3UgQQ9i-QoJu2xeaw",
        "/v1/segment/CuECm7lTF_wRR1PfDZ4pamRtN0usQEZ"]), "IVS tokens are not predictable"
    assert _pf_summary({}) == "  prefetch=NONE-SEEN"
    assert "lead_med=4.18s" in _pf_summary({"a": 3.6, "b": 4.18, "c": 4.2})
    s, order = {"a", "b"}, ["a", "b"]
    unclaim(s, order, "b")
    assert s == {"a"} and order == ["a"], "unclaim removes the pre-emptive claim"
    unclaim(s, order, "zzz")            # unknown uri must be a no-op
    assert s == {"a"} and order == ["a"]
    # Rendition choice (Ford 2026-07-28): cheapest wins, audio-only beats video.
    ford = ("#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=5860800,RESOLUTION=1920x1080\nhi.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=638528,RESOLUTION=640x360\nlo.m3u8\n")
    assert pick_variant(ford) == "lo.m3u8", "lowest bandwidth, not first"
    audio = ford + "#EXT-X-STREAM-INF:BANDWIDTH=96000,CODECS=\"mp4a.40.2\"\naud.m3u8\n"
    assert pick_variant(audio) == "aud.m3u8", "audio-only preferred"
    nobw = "#EXTM3U\n#EXT-X-STREAM-INF:PROGRAM-ID=1\nfirst.m3u8\n#EXT-X-STREAM-INF:PROGRAM-ID=1\nsecond.m3u8\n"
    assert pick_variant(nobw) == "first.m3u8", "unparseable master falls back to first"
    assert pick_variant("#EXTM3U\n#EXT-X-TARGETDURATION:6\nseg1.ts\n") is None, "media playlist: no variant"
    # Read quantum (2026-07-30). The cost of a BYTE-denominated read is
    # chunk/bitrate seconds, so the knob is milliseconds of media and the result
    # must stay TS-aligned, bounded, and exactly inert when unset.
    assert chunk_bytes_for(0, 49000) == CHUNK_BYTES_LEGACY, "chunk_ms=0 keeps the legacy constant"
    assert chunk_bytes_for(40, None) == CHUNK_BYTES_MIN, "no estimate yet => start small"
    assert chunk_bytes_for(40, 0) == CHUNK_BYTES_MIN, "zero bitrate must not divide"
    assert chunk_bytes_for(40, -1) == CHUNK_BYTES_MIN, "negative bitrate must not underflow"
    # 40 ms at Q4/IVS production bitrate (~49 000 B/s) = 1960 B, floored to packets.
    assert chunk_bytes_for(40, 49000) == 1880, "TS-aligned floor"
    assert chunk_bytes_for(1, 49000) == CHUNK_BYTES_MIN, "never below 10 TS packets"
    assert chunk_bytes_for(200, 49000) % TS_PACKET == 0, "always whole TS packets"
    _hi = chunk_bytes_for(10_000, 5_000_000)
    assert CHUNK_BYTES_MAX - TS_PACKET < _hi <= CHUNK_BYTES_MAX, "clamped to the legacy max"
    assert _hi % TS_PACKET == 0
    # Synthetic PDT anchor for PDT-less venues (Chorus Call, webinar.net).
    # announce - EXTINF == the segment's media start.
    assert synth_pdt(1000.0, 6.006) == 1000.0 - 6.006
    assert synth_pdt(None, 6.0) is None, "no announcement => refuse to fabricate"
    assert synth_pdt(1000.0, None) is None, "no EXTINF => refuse to fabricate"
    assert synth_pdt(1000.0, 0) is None, "zero duration is not a duration"
    assert synth_pdt(1000.0, -2.0) is None, "negative duration must not shift forward"
    # A synthesised anchor must be EARLIER than the announcement it derives from,
    # by exactly one segment — that is what makes the lag it produces a bound.
    _a, _d = 1785446675.127, 2.0
    assert synth_pdt(_a, _d) < _a and abs((_a - synth_pdt(_a, _d)) - _d) < 1e-9
    # Cross-leg join key for PDT-less venues: last path component, query stripped.
    assert join_key("media_b96000_175.ts") == "media_b96000_175.ts", "webinar.net shape"
    assert join_key("media-u3j4zp9qm_435.ts") == "media-u3j4zp9qm_435.ts", "Chorus Call shape"
    assert join_key("https://h/a/b/seg_41.ts?token=abc123") == "seg_41.ts", "query stripped"
    assert join_key("/v1/segment/CucCQjX4C-Pa2l0Xm8nl4YThczGSYTr8") == \
        "CucCQjX4C-Pa2l0Xm8nl4YThczGSYTr8", "IVS token (never used — IVS has PDT)"
    assert join_key("") == "-" and join_key(None) == "-", "no uri => placeholder, not a crash"
    assert " " not in join_key("https://h/a/seg 1.ts".replace(" ", "%20")), "must stay one field"
    print("selftest OK")


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("url", nargs="?")
    ap.add_argument("--selftest", action="store_true", help="run pure-function checks and exit")
    ap.add_argument("poll_ms", nargs="?", type=float, default=250)
    ap.add_argument("max_backlog", nargs="?", type=int, default=2)
    ap.add_argument("--speculative", action="store_true",
                    help="fetch the next segment by predicted URL before the manifest announces it")
    ap.add_argument("--spec-start-frac", type=float, default=0.8,
                    help="wait this fraction of a segment duration before probing (politeness)")
    ap.add_argument("--spec-poll-ms", type=float, default=200)
    ap.add_argument("--spec-max-probes", type=int, default=15)
    ap.add_argument("--arrival-log", type=str, default=None,
                    help="append '<url-hash> <t_done> <fetch_secs> <mode>' per segment. "
                         "Join two legs' logs on the hash and difference t_done to get "
                         "the real arrival lead — no PDT, no clock alignment needed.")
    ap.add_argument("--prefetch", action="store_true",
                    help="FETCH #EXT-X-PREFETCH URLs (buffered, serial) instead of waiting "
                         "for the manifest to announce them. Banks a flat ~0.92 s per segment "
                         "on Amazon IVS. UNVALIDATED: needs the 90-min drop-safety gate that "
                         "the speculative posture had to pass (LATENCY-PROOF S7) before any "
                         "armed config may use it. Default OFF.")
    ap.add_argument("--prefetch-report", action="store_true",
                    help="OBSERVE-ONLY. Record what #EXT-X-PREFETCH would have bought, "
                         "without fetching anything and without touching the audio path. "
                         "Amazon IVS (Q4 Inc's platform) names segments that do not exist "
                         "yet; our numeric speculation buys ZERO there because IVS segment "
                         "URLs are opaque signed tokens. Measured 4.18 s of announcement "
                         "lead on the AWS IVS demo channel (LATENCY-PROOF §V1) but never on "
                         "a real issuer channel. This flag adds ZERO HTTP requests — it only "
                         "times manifest lines we already parse — so it is safe to arm.")
    ap.add_argument("--stream-body", action="store_true",
                    help="EXPERIMENTAL, REFUTED on DW 2026-07-24: emit segment bytes "
                         "progressively instead of reading the body whole. Measured "
                         "-0.197 s median against a pre-registered >=0.3 s bar, and it "
                         "introduced 3/32/62 short reads per run where the buffered path "
                         "had zero. Default OFF; kept for venues whose bodies arrive slowly.")
    ap.add_argument("--chunk-ms", type=float, default=0.0,
                    help="With --stream-body: size each read block to at most this many "
                         "MILLISECONDS OF MEDIA instead of a fixed 32768 bytes. 0 (default) "
                         "keeps the legacy byte constant exactly. WHY: r.read(chunk) blocks "
                         "until chunk bytes arrive, and an origin still writing a segment "
                         "dribbles at the media rate, so the fixed constant costs "
                         "chunk/bitrate SECONDS per word -- measured 1.107 s on a 230 kbps "
                         "IVS rendition vs ~0.001 s on the same channel's 1.9 Mbit/s "
                         "rendition (2026-07-30). At Q4/IVS production bitrate it predicts "
                         "0.669 s against a measured 0.713 s first-byte lag, i.e. nearly all "
                         "of it. 40 is a sane starting value. UNGATED: needs the 90-minute "
                         "drop-safety gate before an armed config uses it, because smaller "
                         "writes are exactly what produced the 3/32/62 short reads on DW.")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return 0
    if not a.url:
        ap.error("url is required (or pass --selftest)")
    src = a.url
    poll = a.poll_ms / 1000.0
    max_backlog = a.max_backlog
    spec_poll = a.spec_poll_ms / 1000.0

    # speculation state
    spec_on = a.speculative            # flips off permanently on 429/403
    spec_verified = False              # URL pattern proven predictable
    spec_target = None                 # next URI we expect (playlist-relative)
    spec_probes = 0
    spec_next_probe = 0.0
    last_seg_at = 0.0
    seg_dur = 6.0
    n_spec_hits = 0
    # Running bitrate estimate, so --chunk-ms can be honoured in BYTES. Seeded
    # from the first completed segment (bytes / EXTINF) and then EWMA'd, because
    # a single segment of a VBR rendition is a noisy sample and over-estimating
    # the bitrate is the direction that costs latency.
    bps_est = None

    def note_bitrate(written):
        nonlocal bps_est
        if not written or seg_dur <= 0:
            return
        sample = written / seg_dur
        bps_est = sample if bps_est is None else 0.7 * bps_est + 0.3 * sample

    def read_chunk():
        return chunk_bytes_for(a.chunk_ms, bps_est)
    # Health counters. The arm never sees this process's stderr (it is logged at
    # `debug` in src/source/pipe.rs), so the leg cmd redirects stderr to a file
    # and we emit a periodic summary — silent audio loss would otherwise be an
    # invisible recall bug.
    n_segs = n_short = n_seg_err = n_spec_404 = 0
    # --prefetch-report accounting (observe-only; no HTTP)
    pf_first_seen: dict[str, float] = {}
    pf_lead: dict[str, float] = {}
    arr_log = open(a.arrival_log, "a", buffering=1) if a.arrival_log else None
    # url -> (t_done, t_first, fetch_secs, mode), flushed to the log the moment
    # the manifest announces that url WITH its PDT (a prefetch fetch has none yet).
    arr_pending: dict[str, tuple] = {}
    # For the synthetic anchor at PDT-less venues: when a URI was first seen in a
    # manifest, and the EXTINF that belonged to it. Evicted alongside `seen`.
    first_seen: dict[str, float] = {}
    extinf_of: dict[str, float] = {}
    pf_on = a.prefetch          # flips off permanently on 429/403
    n_prefetch_hits = 0
    retry_n: dict[str, int] = {}
    next_summary = time.time() + 60.0

    # resolve master→media, tolerating startup failure with retries. NOT "once":
    # see the DEAF-LEG guard in the poll loop below.
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
    src_is_master = (media != src)
    if not src_is_master:
        sys.stderr.write(
            "⚠ the URL given IS a media playlist, not a master — if it dies "
            "mid-run this leg CANNOT self-heal. Prefer the master/channel URL.\n")
        sys.stderr.flush()

    # ── DEAF-LEG GUARD (2026-07-27) ────────────────────────────────────────
    # A media playlist URL can die MID-RUN while the channel stays live: IVS
    # signs them, and a channel-side session change invalidates them. Measured
    # this day on the AWS IVS demo channel — a 90-min gate run delivered 1554
    # clean segments and then took 10,860 CONSECUTIVE poll 404s over 45 minutes.
    #
    # Why that was so dangerous: the leg was TOTALLY DEAF, yet every counter it
    # reports stayed clean — dropped=0 short=0 seg_err=0, no `seg LOST` — so the
    # runbook's own post-run health check would have passed a leg that captured
    # 52 minutes of a 70-minute call. It is the S7 silent-audio-loss failure
    # class with a new mechanism, and a worse shape: S7 lost ~86 s in scattered
    # gaps, this loses EVERYTHING after the failure point.
    #
    # It is also a politeness bug: 4 requests/second at a dead URL for 45
    # minutes, per leg. Against a real issuer's CDN that is what earns the
    # rate-limit every other budget in this script exists to avoid.
    RERESOLVE_EVERY = 12        # consecutive failed polls (~3 s at 250 ms)
    DEAF_BACKOFF_AFTER = 40     # ~10 s ⇒ stop flooding, poll slowly
    DEAF_POLL_S = 1.0
    poll_fail = 0               # CONSECUTIVE failed polls
    n_poll_err = 0              # total, for SUMMARY
    n_reresolve = 0             # successful media-URL changes
    deaf_since = None
    deaf_secs = 0.0

    out = sys.stdout.buffer
    seen = set()
    seen_order = []
    event_type_seen = False
    first = True
    n_skip = 0
    sys.stderr.write(f"edge-fetch: media={media} poll={poll*1000:.0f}ms max_backlog={max_backlog}\n")
    sys.stderr.flush()

    while True:
        t0 = time.time()

        # ⚠ EMITTED AT THE TOP OF THE LOOP, DELIBERATELY. This used to sit after
        # the poll, so a leg that could not poll never wrote a SUMMARY at all —
        # during the 45-minute deaf window measured 2026-07-27 the last line in
        # the log was a perfectly clean `segs=1554 dropped=0 seg_err=0`. A health
        # line that goes silent exactly when the leg breaks is worse than none.
        if t0 >= next_summary:
            next_summary = t0 + 60.0
            deaf_now = deaf_secs + ((t0 - deaf_since) if deaf_since is not None else 0.0)
            sys.stderr.write(
                f"SUMMARY segs={n_segs} dropped={n_skip} short={n_short} "
                f"seg_err={n_seg_err} spec_hits={n_spec_hits} spec_404={n_spec_404} "
                f"spec={'on' if spec_on else 'OFF'}"
                f"  poll_err={n_poll_err} deaf_s={deaf_now:.0f} "
                f"reresolve={n_reresolve}{' DEAF-NOW' if deaf_since is not None else ''}"
                + (_pf_summary(pf_lead) if a.prefetch_report else "")
                + (f"  prefetch_fetched={n_prefetch_hits} pf={'on' if pf_on else 'OFF'}"
                   if a.prefetch else "")
                # The read quantum is a LATENCY term (chunk/bitrate seconds per
                # word), so it belongs in the health line next to the counters —
                # a run whose bitrate estimate drifted is otherwise invisible.
                + (f"  chunk={read_chunk()}B"
                   + (f" ({read_chunk() / bps_est * 1000:.0f}ms @ {bps_est / 1000:.0f}kB/s)"
                      if bps_est else " (no bitrate estimate yet)")
                   if a.stream_body and a.chunk_ms else "") + "\n")
            sys.stderr.flush()

        try:
            body = get(media, timeout=poll + 2).decode("utf-8", "replace")
        except Exception as e:
            n_poll_err += 1
            poll_fail += 1
            if deaf_since is None:
                deaf_since = t0
            # Loud on the first failure and then every 40th, so a real outage is
            # visible without writing 10,860 identical lines into the log.
            if poll_fail == 1 or poll_fail % DEAF_BACKOFF_AFTER == 0:
                sys.stderr.write(
                    f"poll err ({poll_fail} consecutive, deaf {t0 - deaf_since:.0f}s): {e}\n")
                sys.stderr.flush()
            # The playlist URL may simply have died. Re-derive it from the URL we
            # were originally given; if that was a master this recovers the leg.
            if poll_fail % RERESOLVE_EVERY == 0:
                if src_is_master:
                    try:
                        new_media = resolve_media(src, timeout=poll + 2)
                        if new_media != media:
                            media = new_media
                            base = media.rsplit("/", 1)[0] + "/"
                            n_reresolve += 1
                            sys.stderr.write(
                                f"RE-RESOLVED media playlist after {poll_fail} failed "
                                f"polls (recovery #{n_reresolve}): {media}\n")
                            sys.stderr.flush()
                    except Exception as e2:
                        sys.stderr.write(f"re-resolve failed: {e2}\n"); sys.stderr.flush()
                elif poll_fail == RERESOLVE_EVERY:
                    sys.stderr.write(
                        "⚠ CANNOT RE-RESOLVE — this leg was given a media playlist, "
                        "not a master. It stays deaf until restarted.\n")
                    sys.stderr.flush()
            time.sleep(poll if poll_fail < DEAF_BACKOFF_AFTER else DEAF_POLL_S)
            continue
        if poll_fail:
            deaf_secs += t0 - deaf_since
            sys.stderr.write(
                f"RECOVERED after {poll_fail} failed polls / {t0 - deaf_since:.1f}s deaf "
                f"(total deaf {deaf_secs:.1f}s)\n")
            sys.stderr.flush()
            poll_fail = 0
            deaf_since = None

        # pair each segment URI with the PDT that precedes it (diagnostics)
        pdt = None; pairs = []; prefetch_lines = []
        cur_dur = None
        for l in body.splitlines():
            l = l.strip()
            if l.startswith("#EXT-X-PLAYLIST-TYPE:") and "EVENT" in l and not event_type_seen:
                event_type_seen = True
                sys.stderr.write(
                    "playlist-type EVENT: manifest grows from seq 1 and never rolls off "
                    "(Ford 2026-07-28 venue class) — seen-cache scales with it\n")
                sys.stderr.flush()
            elif l.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                pdt = parse_pdt(l)
            elif l.startswith("#EXTINF:"):
                try:
                    seg_dur = float(l.split(":", 1)[1].rstrip(","))
                    cur_dur = seg_dur
                except Exception:
                    pass
            elif l.startswith("#EXT-X-PREFETCH:"):
                prefetch_lines.append(l.split(":", 1)[1].strip())
            elif l and not l.startswith("#"):
                pairs.append((l, pdt)); pdt = None
                # Per-URI duration, for the synthetic anchor below. `seg_dur` is
                # a running global (it is what the speculation budget uses); this
                # pins the value that belonged to THIS segment, because the last
                # segment of a winding-down stream is routinely short (Apple
                # 2026-07-30: 6.006, 6.006, 6.006, 2.736).
                if l not in extinf_of:
                    extinf_of[l] = cur_dur if cur_dur else seg_dur
                cur_dur = None
                # Announcement instant — the wall clock at which this URI first
                # appeared in a manifest we parsed. Only ever set once.
                if l not in first_seen:
                    first_seen[l] = t0

        # OBSERVE-ONLY prefetch accounting. Zero HTTP requests: a prefetch URL is
        # string-identical to the URL later announced as a normal segment (75 of
        # 77 matched on the IVS demo channel), so timing the two sightings gives
        # the lead this venue WOULD offer. This is how a live event measures the
        # lever without any risk to the audio path.
        if a.prefetch_report:
            for pu in prefetch_lines:
                if pu not in pf_first_seen:
                    pf_first_seen[pu] = t0
                    if len(pf_first_seen) == 1:
                        sys.stderr.write(
                            f"prefetch: venue publishes #EXT-X-PREFETCH "
                            f"({len(prefetch_lines)} ahead of the live edge)\n")
                        sys.stderr.flush()
            for u, _ in pairs:
                if u in pf_first_seen and u not in pf_lead:
                    pf_lead[u] = t0 - pf_first_seen[u]
                    sys.stderr.write(f"prefetch lead={pf_lead[u]:.3f}s\n")
                    sys.stderr.flush()

        # Verify the URL pattern once, against real consecutive segments.
        if spec_on and not spec_verified and len(pairs) >= 3:
            if pattern_is_predictable([u for u, _ in pairs]):
                spec_verified = True
                sys.stderr.write("speculative: URL pattern verified predictable\n")
            else:
                spec_on = False
                sys.stderr.write("speculative: DISABLED — segment URLs are not predictable\n")
            sys.stderr.flush()

        # Emit any parked arrival record now that this poll carries its PDT.
        #
        # Three tiers, and the distinction between them is load-bearing:
        #
        #   (no suffix) the venue published EXT-X-PROGRAM-DATE-TIME. Column 1 is
        #               the ENCODER's timestamp for the media. Absolute.
        #   "-synth"    the venue published none, but we saw the URI appear.
        #               A packager announces a segment once it has finished
        #               writing it, so the media it carries ENDED at about the
        #               announcement instant ⇒ it STARTED at announce - EXTINF.
        #               Biased LATE by the origin's own publish+CDN delay, so
        #               every lag computed against it is a LOWER BOUND on true
        #               staleness. That is still the only clock Chorus Call and
        #               webinar.net have ever had (both are PDT-less: SBUX wrote
        #               136 wall-clock-only rows, CMG 918, WING a 0-byte file).
        #   "-nopdt"    no PDT and no announcement either (we got here purely by
        #               prefetch/speculation). Column 1 is fetch wall-clock.
        #
        # ⚠ WHY THE TIERS MUST STAY DISTINGUISHABLE. The "-nopdt" fallback writes
        # t_done into the PDT column, so any consumer that treats column 1 as
        # encoder time is measuring our own fetch against itself and will report
        # a lag of ~0 forever. Tagging is what stops a synthetic anchor from
        # being quoted as an absolute one.
        if arr_log is not None and arr_pending:
            for _u, _p in pairs:
                rec = arr_pending.pop(_u, None)
                if rec is None:
                    continue
                if _p is not None:
                    arrival(arr_log, _p, rec[0], rec[1], rec[2], rec[3])
                    continue
                _synth = synth_pdt(first_seen.get(_u), extinf_of.get(_u, seg_dur))
                if _synth is not None:
                    arrival(arr_log, _synth, rec[0], rec[1], rec[2], rec[3] + "-synth",
                            key=join_key(_u))
                else:
                    arrival(arr_log, rec[0], rec[0], rec[1], rec[2], rec[3] + "-nopdt",
                            key=join_key(_u))
            if len(arr_pending) > 200:      # never grow without bound
                arr_pending.clear()

        new = [(u, p) for (u, p) in pairs if u not in seen]
        for u, _ in new:
            seen.add(u); seen_order.append(u)
        # Bound memory — but the bound MUST exceed the live manifest length.
        # Ford 2026-07-28 (bug 4): a fixed 500-key cap against a
        # #EXT-X-PLAYLIST-TYPE:EVENT manifest (grows from seq 1; 497→3000
        # entries) evicted still-listed URIs every poll; they re-entered `new`
        # as "fresh", SKIP-TO-NEWEST then fetched the newest of the RESURRECTED
        # block ≈ the whole manifest behind the edge. Sawtooth: seg age 11 s →
        # 3011 s → … → 12 s → 2508 s; 1 segment in 146 delivered, p50 arrival
        # lag 98 s. The bound now scales with the manifest so a listed URI is
        # never evicted; memory stays proportional to the playlist itself.
        keep = max(500, 2 * len(pairs))
        if len(seen_order) > keep + 100:
            for u in seen_order[:-keep]:
                seen.discard(u)
                retry_n.pop(u, None)
                # Same bound, same reason (Ford bug 4): these two must never
                # outlive `seen`, or an EVENT playlist leaks them without limit.
                first_seen.pop(u, None)
                extinf_of.pop(u, None)
            seen_order = seen_order[-keep:]

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
                _t0 = time.time()
                _wr, short, _tfb = fetch_segment(urllib.parse.urljoin(base, u), out, poll + 3,
                                                 a.stream_body, read_chunk())
                note_bitrate(_wr)
                _td = time.time()
                arr_pending[u] = (_td, _tfb if _tfb is not None else _td, _td - _t0, "manifest")
                retry_n.pop(u, None)
                n_segs += 1
                last_seg_at = time.time()
                if short:
                    n_short += 1
                    sys.stderr.write(f"short read on {u} (partial segment emitted)\n")
                if p:
                    age = time.time() - p
                    sys.stderr.write(f"seg age={age:.2f}s\n")
                sys.stderr.flush()
            except BrokenPipeError:
                raise
            except Exception as e:
                # A manifest-announced segment can still 404 at our CDN edge for
                # a moment — the same negative-cache lag that gives speculation
                # its lead (docs/LATENCY-PROOF.md S3). Treating that as permanent
                # LOSES the audio: the 90 min drop-safety run measured 17 such
                # 404s = ~86 s of missing call, as ~6 s gaps scattered through it,
                # any one of which could swallow a keyword. Un-claim so the next
                # manifest poll retries, bounded so a genuinely dead segment
                # cannot spin.
                tries = retry_n.get(u, 0) + 1
                retry_n[u] = tries
                if tries <= SEG_RETRY_MAX:
                    unclaim(seen, seen_order, u)
                    sys.stderr.write(f"seg retry {tries}/{SEG_RETRY_MAX} {u}: {e}\n")
                else:
                    n_seg_err += 1
                    sys.stderr.write(f"seg LOST after {tries} tries {u}: {e}\n")
                sys.stderr.flush()

        # PREFETCH FETCH (`--prefetch`). Take the FIRST prefetch URL we do not
        # already hold and fetch it buffered, exactly like a normal segment.
        #
        # Why serial is enough for THIS tier. A prefetch fetch occupies ~3.3 s
        # from the moment its URL appears (the origin holds headers ~1.9 s while
        # the segment is still being written, then dribbles the body ~1.4 s), and
        # new prefetch URLs appear every ~2 s, so it looks like it needs ~1.65
        # concurrent connections. It does not — the segment COMPLETES when the
        # origin finishes producing it, ~3.3 s after the URL appears, no matter
        # when we ask. Asking early and blocking costs nothing extra, and the
        # loop naturally re-synchronises: one fetch per ~2 s of wall clock.
        # Buffered therefore banks a flat ~0.92 s vs the manifest path, serially.
        #
        # The remaining ~1.4 s (first-byte-at-1.9 s instead of complete-at-3.3 s)
        # DOES need concurrency plus ordered emission, and is deliberately NOT
        # attempted here. See LATENCY-PROOF §V1b.
        #
        # Ordering: prefetch[0] is the segment immediately after the newest
        # announced one, and we take only ONE per poll, so emission order is
        # preserved without a reorder buffer. The `seen` claim makes the later
        # normal announcement of the same URL a no-op (they are byte-identical
        # strings — 75 of 77 matched when measured).
        if pf_on and prefetch_lines:
            target = next((p for p in prefetch_lines if p not in seen), None)
            if target is not None:
                seen.add(target); seen_order.append(target)
                try:
                    _t0 = time.time()
                    _wr, short, _tfb = fetch_segment(urllib.parse.urljoin(base, target), out,
                                             poll + 6, a.stream_body, read_chunk())
                    note_bitrate(_wr)
                    _td = time.time()
                    arr_pending[target] = (_td, _tfb if _tfb is not None else _td, _td - _t0, "prefetch")
                    n_prefetch_hits += 1
                    n_segs += 1
                    last_seg_at = time.time()
                    if short:
                        n_short += 1
                        sys.stderr.write(f"short read on prefetch {target[:60]}\n")
                    if n_prefetch_hits == 1:
                        sys.stderr.write("prefetch: FETCHING ahead of announcement\n")
                    sys.stderr.flush()
                except urllib.error.HTTPError as e:
                    unclaim(seen, seen_order, target)
                    if e.code in (403, 429):
                        pf_on = False
                        sys.stderr.write(
                            f"prefetch: DISABLED — HTTP {e.code} (rate-limited/forbidden); "
                            f"manifest-driven for the rest of the run\n")
                    else:
                        # Not yet publishable. Unlike speculation this is NOT a
                        # guess, so a 404 here is a venue quirk worth seeing.
                        sys.stderr.write(f"prefetch {e.code} on {target[:60]}\n")
                    sys.stderr.flush()
                except BrokenPipeError:
                    raise
                except Exception as e:
                    unclaim(seen, seen_order, target)
                    sys.stderr.write(f"prefetch err: {e}\n"); sys.stderr.flush()

        # Re-aim speculation at the segment AFTER the newest one we already hold.
        if spec_on and spec_verified and seen_order:
            t = bump_uri(seen_order[-1])
            if t and t != spec_target:
                spec_target, spec_probes = t, 0
                spec_next_probe = (last_seg_at or time.time()) + a.spec_start_frac * seg_dur

        # Idle window: at most ONE speculative probe per iteration, inside budget.
        if (spec_on and spec_verified and spec_target
                and spec_probes < a.spec_max_probes
                and time.time() >= spec_next_probe):
            spec_probes += 1
            probing = spec_target
            # Claim it BEFORE streaming — the body can take ~1 s to arrive and a
            # manifest poll landing mid-stream must not queue it a second time.
            # Every failure path below MUST un-claim it, or a 404 (the normal
            # case while the segment is still being published) would make us skip
            # that segment forever.
            seen.add(probing); seen_order.append(probing)
            try:
                _wr, short, _ = fetch_segment(urllib.parse.urljoin(base, probing), out, poll + 3,
                                              a.stream_body, read_chunk())
                note_bitrate(_wr)
                n_spec_hits += 1
                n_segs += 1
                last_seg_at = time.time()
                if short:
                    n_short += 1
                    sys.stderr.write(f"short read on {probing} (partial segment emitted)\n")
                sys.stderr.write(
                    f"speculative HIT {probing} on probe {spec_probes} (total {n_spec_hits})\n")
                sys.stderr.flush()
                nxt = bump_uri(probing)
                spec_target, spec_probes = nxt, 0
                spec_next_probe = last_seg_at + a.spec_start_frac * seg_dur
            except urllib.error.HTTPError as e:
                unclaim(seen, seen_order, probing)
                if e.code in (403, 429):
                    spec_on = False
                    sys.stderr.write(
                        f"speculative: DISABLED — HTTP {e.code} (rate-limited/forbidden); "
                        f"falling back to manifest-driven for the rest of the run\n")
                    sys.stderr.flush()
                else:
                    n_spec_404 += 1
                    spec_next_probe = time.time() + spec_poll   # not published yet
            except BrokenPipeError:
                raise
            except Exception:
                unclaim(seen, seen_order, probing)
                spec_next_probe = time.time() + spec_poll

        # Runaway trip (defence in depth behind the BrokenPipe fix): normal
        # operation is <=spec_max_probes 404s per emitted segment. A ratio this
        # far out means the cursor has stopped advancing and we are hammering
        # the origin — stop speculating rather than find out from a 429.
        if spec_on and n_spec_404 > 50 * max(1, n_segs):
            spec_on = False
            sys.stderr.write(
                f"speculative: DISABLED — runaway guard ({n_spec_404} 404s for "
                f"{n_segs} segments); falling back to manifest-driven\n")
            sys.stderr.flush()

        dt = time.time() - t0
        if dt < poll:
            time.sleep(poll - dt)


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except BrokenPipeError:
        # Downstream (ffmpeg) exited. Leave loudly and promptly rather than
        # spinning against a dead pipe — see stream_to().
        sys.stderr.write("FATAL: stdout closed (decoder exited) — stopping fetcher\n")
        sys.stderr.flush()
        sys.exit(3)
    except KeyboardInterrupt:
        sys.exit(130)
