#!/usr/bin/env python3
"""Generate webcast arm configs for the 2026-08-04 afternoon events.

Takes the `earshot prepare` output (which emits sherpa_kws and a placeholder
starts_at) and produces the armed webcast config: production engine, real call
time, fanout source ladder reading $HOME/<slug>-stream.url, and the risk +
keyword sections copied verbatim so scripts/keywords-extract.py hashes the
armed set.

Two legs, not three: spec_edge's speculative prefetch is tuned for Q4/IVS
venues that publish #EXT-X-PREFETCH. None of these four venues is confirmed to,
and probing one that does not risks rate-limiting the vantage IP -- which takes
down every leg, not one (see configs/webcast-snap-26aug03-arm-wc.toml).
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

EVENTS = {
    "amd":  ("KXEARNINGSMENTIONAMD-26AUG04",  "2026-08-04T21:00:00Z", "configs/amd-26aug04-raw.toml"),
    "spcx": ("KXEARNINGSMENTIONSPCX-26AUG04", "2026-08-04T20:30:00Z", "configs/twilio-live-spcx.toml"),
    "tost": ("KXEARNINGSMENTIONTOST-26AUG04", "2026-08-04T21:00:00Z", "configs/twilio-live-tost.toml"),
    "zeta": ("KXEARNINGSMENTIONZETA-26AUG04", "2026-08-04T20:30:00Z", "configs/twilio-live-zeta.toml"),
}

HEADER = """# {ev} — webcast arm config, generated {gen} by scripts/stage-afternoon-26aug04.py
#
# VENUE: resolved at ~T-0 into $HOME/{slug}-stream.url. All four 2026-08-04
# afternoon events are `bridge_unpublished` (no dial-in in any scanned 8-K, and
# none published on the IR page), so this is the only lane. If a dial-in turns
# up, add a twilio_stream leg -- do not replace these.
#
# Posture: 1c mention-probe observe (non-crossing). Fill posture stays gated on
# the unscored Phase-4 precision verdict.
"""

BODY = """
[event]
id        = "{ev}-WEBCAST"
starts_at = "{starts_at}"

[pipeline]
chunk_ms       = 80
resample_to_hz = 16000

[detector]
engine = "streaming_ctc"

[detector.streaming_ctc]
model_dir         = "models/sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18"
num_threads       = 1
default_threshold = 0.15
refractory_ms     = 2000

# Kept so the engine reverts in a one-line change above.
[detector.sherpa_kws]
model_dir         = "models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
num_threads       = 1
max_active_paths  = 4
default_threshold = 0.20
default_boost     = 2.0
refractory_ms     = 2000
fp32              = true

[arbiter]
mode = "fanout"

[[sources]]
kind = "pipe"
label = "streamlink"
sample_rate_hz = 16000
cmd = [
  "/bin/sh", "-c",
  "streamlink --stdout --hls-live-edge 1 --hls-segment-stream-data --hls-playlist-reload-time segment \\"$(cat $HOME/{slug}-stream.url)\\" best | ffmpeg -loglevel warning -fflags nobuffer -flags low_delay -probesize 2000000 -analyzeduration 5000000 -i - -vn -f s16le -ac 1 -ar 16000 -",
]

[[sources]]
kind = "pipe"
label = "pipe_tuned"
sample_rate_hz = 16000
cmd = [
  "/bin/sh", "-c",
  "ffmpeg -loglevel warning -fflags nobuffer -flags low_delay -probesize 2000000 -analyzeduration 5000000 -live_start_index -1 -http_persistent 1 -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 2 -i \\"$(cat $HOME/{slug}-stream.url)\\" -vn -f s16le -ac 1 -ar 16000 -",
]

[kalshi]
series = "{series}"
env = "prod"

"""


def extract(text, start_pat, stop_pat):
    """Return the slice from the first start_pat match to the first stop_pat after it."""
    m = re.search(start_pat, text, re.M)
    if not m:
        return None
    rest = text[m.start():]
    m2 = re.search(stop_pat, rest[1:], re.M)
    return rest if not m2 else rest[: m2.start() + 1]


def main():
    gen = "2026-08-04"
    written = []
    for slug, (ev, starts_at, raw_path) in EVENTS.items():
        raw = (REPO / raw_path).read_text(encoding="utf-8")

        risk = extract(raw, r"^\[risk\]", r"^# .*\n\[\[keywords\]\]|^\[\[keywords\]\]")
        if risk is None:
            print(f"  {slug}: FAILED — no [risk] section in {raw_path}", file=sys.stderr)
            continue

        kw_start = re.search(r"^(# [^\n]*\n)?\[\[keywords\]\]", raw, re.M)
        if not kw_start:
            print(f"  {slug}: FAILED — no [[keywords]] in {raw_path}", file=sys.stderr)
            continue
        keywords = raw[kw_start.start():]

        series = ev.rsplit("-", 1)[0]
        out = (
            HEADER.format(ev=ev, gen=gen, slug=slug)
            + BODY.format(ev=ev, starts_at=starts_at, slug=slug, series=series)
            + risk.rstrip()
            + "\n\n"
            + keywords.rstrip()
            + "\n"
        )
        dest = REPO / f"configs/webcast-{slug}-26aug04-arm-wc.toml"
        dest.write_text(out, encoding="utf-8")
        n = out.count("[[keywords]]")
        written.append((slug, dest.name, n, starts_at))
        print(f"  {slug}: wrote {dest.name} ({len(out)} bytes, {n} keywords, starts {starts_at})")

    print()
    for slug, name, n, ts in written:
        print(f"{slug:5s} {n:3d} kw  {ts}  {name}")


if __name__ == "__main__":
    main()
