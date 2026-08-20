#!/usr/bin/env python3
"""REGRESSION TEST — the deaf-leg failure of 2026-07-27, reproduced locally.

WHAT HAPPENED. A 90-min gate run against the AWS IVS demo channel delivered
1554 clean segments and then took 10,860 CONSECUTIVE poll 404s over 45 minutes.
The channel was still live; the leg's RESOLVED MEDIA PLAYLIST URL had died.
`hls-edge-fetch.py` resolved master->media once at startup and never again, so
the leg was totally deaf while every counter it reports stayed clean --
`dropped=0 short=0 seg_err=0`, no `seg LOST`. The runbook's own post-run health
check would have passed a leg that captured 52 minutes of a 70-minute call.

Run:  python3 scripts/test-deaf-leg.py [path-to-hls-edge-fetch.py]
Takes ~30 s, needs no network (binds 127.0.0.1 only).

Serves a master playlist that points at mediaA. At T_SWITCH the origin "rotates
its session": mediaA starts returning 404 and the master begins pointing at
mediaB. That is exactly what the AWS IVS demo channel did at 51.7 minutes.

The same local origin also exposes a ROST/CBRS-shaped media playlist: ordinary
reloads are frozen, while a fresh `_cb` query reaches an advancing origin.
The production cache-busting fetcher must emit multiple unique segments, use a
unique query every time, and carry `Cache-Control: no-cache`.

PASS requires both survivor contracts:
  1. segments flow before the switch
  2. the fetcher RE-RESOLVES and RECOVERS after it
  3. segments flow again afterwards, with no duplicate emission
  4. cache-busted reloads advance the frozen-CDN fixture without duplicates
"""
import http.server, threading, time, sys, subprocess, os, re
import urllib.parse

PORT = 18771
T_SWITCH = 10.0
SEG_MS = 500
t_start = time.time()
cache_queries = []
cache_controls = []


def playlist(prefix, n):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:1",
             f"#EXT-X-MEDIA-SEQUENCE:{max(0, n - 3)}"]
    for i in range(max(0, n - 3), n):
        lines.append("#EXTINF:0.5,")
        lines.append(f"{prefix}_{i}.ts")
    return "\n".join(lines) + "\n"


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        elapsed = time.time() - t_start
        switched = elapsed >= T_SWITCH
        n = int(elapsed * 1000 / SEG_MS)
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path == "/master.m3u8":
            tgt = "mediaB.m3u8" if switched else "mediaA.m3u8"
            body = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\n" + tgt + "\n")
        elif path == "/mediaA.m3u8":
            if switched:
                self.send_error(404); return
            body = playlist("segA", n)
        elif path == "/mediaB.m3u8":
            if not switched:
                self.send_error(404); return
            body = playlist("segB", n)
        elif path == "/cache-master.m3u8":
            body = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\ncache-media.m3u8\n"
        elif path == "/cache-media.m3u8":
            query = urllib.parse.parse_qs(parsed.query)
            cache_queries.append(query.get("_cb", [""])[0])
            cache_controls.append(self.headers.get("Cache-Control", ""))
            # Reproduce the Notified/CloudFront class: an ordinary reload is
            # frozen, while each unique cache-busted reload reaches origin.
            visible = n if query.get("_cb") else 1
            body = playlist("cache", visible)
        elif path.endswith(".ts"):
            data = (os.path.basename(path).encode() + b"\x00" * 200)
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        else:
            self.send_error(404); return

        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

script = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "hls-edge-fetch.py")
out = open("/tmp/deaf_out.bin", "wb")
err = open("/tmp/deaf_err.log", "w")
p = subprocess.Popen([sys.executable, "-u", script,
                      f"http://127.0.0.1:{PORT}/master.m3u8", "250", "2"],
                     stdout=out, stderr=err)
time.sleep(25)
p.terminate(); p.wait(timeout=10)
out.close(); err.close()
log = open("/tmp/deaf_err.log").read()
body = open("/tmp/deaf_out.bin", "rb").read()
segs = re.findall(rb"seg([AB])_(\d+)\.ts", body)
a_segs = [s for s in segs if s[0] == b"A"]
b_segs = [s for s in segs if s[0] == b"B"]
names = [s[0] + s[1] for s in segs]

print("--- fetcher stderr ---")
print(log.strip()[-1200:])
print("\n--- RESULT ---")
print(f"segments emitted BEFORE switch (segA): {len(a_segs)}")
print(f"segments emitted AFTER  switch (segB): {len(b_segs)}")
print(f"duplicate emissions: {len(names) - len(set(names))}")
ok1 = len(a_segs) > 0
ok2 = "RE-RESOLVED" in log and "RECOVERED" in log
ok3 = len(b_segs) > 0
ok4 = len(names) == len(set(names))
for label, ok in [("segments flowed before the switch", ok1),
                  ("fetcher re-resolved AND recovered", ok2),
                  ("segments flowed after the switch", ok3),
                  ("no duplicate segment emission", ok4)]:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

# ROST/CBRS-shaped frozen-CDN proof for the complementary cache-busting leg.
cache_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "cdn-cachebust-fetch.sh")
cache_url = f"/tmp/cachebust-url-{os.getpid()}.txt"
cache_out = f"/tmp/cachebust-out-{os.getpid()}.bin"
cache_err = f"/tmp/cachebust-err-{os.getpid()}.log"
with open(cache_url, "w") as handle:
    handle.write(f"http://127.0.0.1:{PORT}/cache-master.m3u8\n")
with open(cache_out, "wb") as out2, open(cache_err, "w") as err2:
    p2 = subprocess.Popen(["bash", cache_script, cache_url, "0.2"],
                          stdout=out2, stderr=err2)
    time.sleep(4)
    p2.terminate()
    try:
        p2.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p2.kill(); p2.wait(timeout=5)
with open(cache_out, "rb") as handle:
    cache_body = handle.read()
cache_segments = re.findall(rb"cache_(\d+)\.ts", cache_body)
cache_unique = len(set(cache_segments))
nonempty_queries = [query for query in cache_queries if query]
ok5 = cache_unique >= 3
ok6 = len(nonempty_queries) == len(set(nonempty_queries))
ok7 = bool(cache_controls) and all(value == "no-cache" for value in cache_controls)
print("\n--- CACHE-BUST RESULT ---")
print(f"unique advancing segments: {cache_unique}")
print(f"unique cache-busted reloads: {len(set(nonempty_queries))}/{len(nonempty_queries)}")
for label, ok in [
    ("frozen CDN origin emitted at least three advancing segments", ok5),
    ("every playlist reload used a fresh _cb value", ok6),
    ("every media-playlist reload requested no-cache", ok7),
]:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

srv.shutdown()
for path in (cache_url, cache_out, cache_err):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
all_ok = all([ok1, ok2, ok3, ok4, ok5, ok6, ok7])
print("\nOVERALL:", "PASS" if all_ok else "FAIL")
sys.exit(0 if all_ok else 1)
