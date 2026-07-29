#!/usr/bin/env python3
"""Chorus Call (Dynamic Media Frame) → $HOME/<slug>-stream.url, browser-free.

Why this exists: WING fires at 14:00 UTC = 08:00 CDMX, squarely inside the
human-absent morning window that silently dropped PYPL and BA on 2026-07-28.
`run-arm.sh` refuses to start without a live master m3u8 in
$HOME/<slug>-stream.url (guard 2), and on every venue so far that file could
only be produced by a logged-in browser at T-20 — which is exactly the step a
sleeping operator cannot perform.

Chorus Call is the first venue that splits the problem in two:

  1. "has it started?"  — SOLVED, unauthenticated. getPolledStatus keyed by
     frameId returns a bare int (0 upcoming / 1 live / 3 archived) in ~93 ms
     from the us-east box with no cookies at all.  (VERIFICATIONS Q-CC)
  2. "give me the audio" — the m3u8 lives on the post-registration `frame`
     page.  Needs a credential.  See RESOLVE below.

Usage:
  choruscall-resolve.py --webcastid Cv4NzoL0 --slug wing \
      --access-url '<emailed confirmation link>' [--wait-secs 900]
  choruscall-resolve.py --webcastid Cv4NzoL0 --status-only
  choruscall-resolve.py --check          # offline self-test, no network

Exit 0 only when a validated master m3u8 has been written.  Every other path
exits non-zero and (unless --no-notify) pages via scripts/notify.sh, because
the failure mode this script exists to prevent is a SILENT one.
"""
import argparse, html, json, os, re, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request
from http.cookiejar import CookieJar

HOST = "https://event.choruscall.com"
PAGE = HOST + "/mediaframe/webcast.html?webcastid={wid}"
POLL = HOST + "/mediaframe/v2/dynamicframe/getPolledStatus?streamid={sid}"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

STATUS = {0: "upcoming", 1: "LIVE", 2: "in-progress/paused", 3: "archived"}


def _open(url, jar, data=None, timeout=20):
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with op.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace"), r.geturl()


# --- webcastConfig parsing ---------------------------------------------------
# The page assigns config in two styles: a `webcastConfig = { k: v, ... }`
# literal and a run of `webcastConfig.k = v;` lines.  Both matter; frameId and
# eventStatus arrive via the second.
def parse_config(page):
    cfg = {}
    for k, v in re.findall(r"webcastConfig\.(\w+)\s*=\s*([^;\n]+)", page):
        cfg.setdefault(k, v.strip())
    m = re.search(r"webcastConfig\s*=\s*\{", page)
    if m:
        blk = page[m.end():m.end() + 4000]
        for k, v in re.findall(r"(\w+)\s*:\s*(\"[^\"]*\"|\d+)", blk):
            cfg.setdefault(k, v)
    out = {}
    for k, v in cfg.items():
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = html.unescape(v[1:-1])
        out[k] = v
    return out


def parse_hidden_inputs(page):
    got = {}
    for tag in re.findall(r"<input[^>]*type=\"hidden\"[^>]*>", page):
        n = re.search(r"name=\"([^\"]+)\"", tag)
        v = re.search(r"value=\"([^\"]*)\"", tag)
        if n:
            got[n.group(1)] = html.unescape(v.group(1)) if v else ""
    return got


def event_status(sid, timeout=15):
    """Unauthenticated. VERIFICATIONS Q-CC — the one leg of this that is pinned."""
    body, _ = _open(POLL.format(sid=sid), CookieJar(), timeout=timeout)
    body = body.strip()
    if not re.fullmatch(r"-?\d+", body):
        raise RuntimeError(f"getPolledStatus returned non-integer: {body[:120]!r}")
    return int(body)


# --- m3u8 extraction ---------------------------------------------------------
# Deliberately generic: pull every absolute .m3u8 out of whatever page we were
# able to reach, then let validate() decide.  We do NOT invent a URL template —
# the shape of the stream URL on the `frame` page is explicitly UNPINNED in
# VERIFICATIONS Q-CC, and this repo does not code against remembered API facts.
M3U8_RE = re.compile(r"https?://[^\s\"'<>\\]{6,300}?\.m3u8(?:\?[^\s\"'<>\\]{0,200})?")


def extract_m3u8(page):
    seen, out = set(), []
    for u in M3U8_RE.findall(page):
        u = html.unescape(u)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def validate(url):
    """Mirror run-arm.sh guards 2+3 so we fail HERE, not at fire time."""
    if not re.match(r"^https?://", url):
        return f"not an http(s) URL: {url[:80]}"
    if ".m3u8" not in url:
        return f"does not look like an m3u8: {url[:80]}"
    # guard 3: a media/variant playlist can expire mid-call and the fetcher
    # cannot re-resolve it (LATENCY-PROOF §V6, 10,860 dead 404s).
    if "/v1/playlist/" in url:
        return ("VARIANT playlist (…/v1/playlist/<token>.m3u8) — the fetcher "
                "cannot re-resolve this; a master is required")
    return None


def write_url(path, url):
    """Atomic: run-arm.sh compares this file's mtime against the config, and a
    half-written file that still passes `-s` is a loaded gun between runs."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".ccresolve-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(url.rstrip("\n") + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def notify(subject, body, enabled=True):
    if not enabled:
        return
    sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify.sh")
    if os.path.exists(sh):
        try:
            subprocess.run([sh, subject, body, "--quiet"], timeout=60, check=False)
        except Exception as e:  # alerting must never mask the real error
            print(f"WARN: notify.sh failed: {e}", file=sys.stderr)


# --- self-test ---------------------------------------------------------------
FIXTURE = '''
  webcastConfig = { defaultLanguage: "ENG", frameVersion: "1.6.0", pageId: "register", mediaType: 0 };
  webcastConfig.accountName = "Wingstop";
  webcastConfig.eventDate = new Date("Wed Jul 29 10:00:00 EDT 2026");
  webcastConfig.eventStatus = 0;
  webcastConfig.frameId = 28574;
  webcastConfig.loginType = 2;
  webcastConfig.webcastId = "Cv4NzoL0";
  <input id="streamId" name="streamId" type="hidden" value="28574">
  <input id="loginType" name="loginType" type="hidden" value="2">
'''
FRAME_FIXTURE = '''
  var src = "https://cc-hls.example.net/live/28574/master.m3u8?token=abc";
  fallback: 'https://cc-hls.example.net/live/28574/index_1.m3u8'
'''


def selftest():
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + extra) if extra and not cond else ''}")
        ok = ok and cond

    print("parse_config:")
    c = parse_config(FIXTURE)
    chk("frameId", c.get("frameId") == "28574", repr(c.get("frameId")))
    chk("webcastId unquoted", c.get("webcastId") == "Cv4NzoL0", repr(c.get("webcastId")))
    chk("eventStatus", c.get("eventStatus") == "0", repr(c.get("eventStatus")))
    chk("loginType", c.get("loginType") == "2", repr(c.get("loginType")))
    chk("literal-block key", c.get("pageId") == "register", repr(c.get("pageId")))

    print("parse_hidden_inputs:")
    h = parse_hidden_inputs(FIXTURE)
    chk("streamId", h.get("streamId") == "28574", repr(h.get("streamId")))
    chk("frameId == hidden streamId", c.get("frameId") == h.get("streamId"))

    print("extract_m3u8:")
    u = extract_m3u8(FRAME_FIXTURE)
    chk("finds both", len(u) == 2, repr(u))
    chk("keeps query", u and u[0].endswith("token=abc"), repr(u[:1]))
    chk("none on register page", extract_m3u8(FIXTURE) == [])

    print("validate:")
    chk("accepts master", validate("https://x.net/a/master.m3u8") is None)
    chk("rejects variant", validate("https://x.net/v1/playlist/tok.m3u8") is not None)
    chk("rejects non-m3u8", validate("https://x.net/a.mpd") is not None)
    chk("rejects non-http", validate("wss://x.net/a.m3u8") is not None)

    print("write_url (atomic):")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "wing-stream.url")
        write_url(p, "https://x.net/a/master.m3u8")
        got = open(p).read()
        chk("trailing newline", got.endswith("\n"))
        chk("single line", got.strip() == "https://x.net/a/master.m3u8", repr(got))
        write_url(p, "https://x.net/b/master.m3u8")
        chk("overwrite clean", open(p).read().strip().endswith("b/master.m3u8"))
        chk("no temp litter", [f for f in os.listdir(d) if f.startswith(".ccresolve-")] == [])

    print("\n" + ("SELFTEST PASS" if ok else "SELFTEST FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--webcastid")
    ap.add_argument("--slug", help="writes $HOME/<slug>-stream.url unless --out")
    ap.add_argument("--stream-id", help="frameId; parsed from the page if omitted")
    ap.add_argument("--access-url", help="post-registration/emailed link that reaches the `frame` page")
    ap.add_argument("--out")
    ap.add_argument("--wait-secs", type=int, default=0, help="poll until status==1 (live)")
    ap.add_argument("--poll-secs", type=int, default=10)
    ap.add_argument("--status-only", action="store_true")
    ap.add_argument("--dump-frame", help="save the reached page here for inspection")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    if a.check:
        return selftest()
    if not a.webcastid:
        ap.error("--webcastid required (or --check)")
    notify_on = not a.no_notify

    jar = CookieJar()
    page, _ = _open(PAGE.format(wid=a.webcastid), jar)
    cfg = parse_config(page)
    hid = parse_hidden_inputs(page)
    sid = a.stream_id or cfg.get("frameId") or hid.get("streamId")
    if not sid:
        print("FAIL: no frameId/streamId on the webcast page", file=sys.stderr)
        return 2

    print(f"event   : {cfg.get('accountName','?')} — {cfg.get('eventTitle','?')}")
    print(f"when    : {cfg.get('eventDate','?')}  [{cfg.get('eventDateTimezone','?')}]")
    print(f"streamId: {sid}   loginType: {cfg.get('loginType','?')}   pageId: {cfg.get('pageId','?')}")

    st = event_status(sid)
    print(f"status  : {st} ({STATUS.get(st, 'unknown')})")
    if a.status_only:
        return 0

    deadline = time.time() + a.wait_secs
    while st != 1 and time.time() < deadline:
        time.sleep(a.poll_secs)
        st = event_status(sid)
        print(f"  … status {st} ({STATUS.get(st, 'unknown')})  {int(deadline - time.time())}s left")
    if st != 1:
        msg = f"{a.slug or a.webcastid}: still status {st} ({STATUS.get(st,'?')}) after {a.wait_secs}s"
        print("FAIL: " + msg, file=sys.stderr)
        notify("chorus-call not live", msg, notify_on)
        return 3

    # --- RESOLVE ------------------------------------------------------------
    # UNPINNED (VERIFICATIONS Q-CC): the stream URL's shape on the `frame` page.
    # We refuse to guess a template.  With --access-url we fetch whatever that
    # link reaches and scrape it; without one we stop LOUDLY rather than arm on
    # nothing.
    if not a.access_url:
        msg = (f"{a.slug or a.webcastid} is LIVE but no --access-url was given; the m3u8 "
               "is behind Chorus Call registration and its shape is unpinned. "
               "Surface the URL in a browser (VERIFICATIONS Q-OX recipe).")
        print("FAIL: " + msg, file=sys.stderr)
        notify("chorus-call LIVE, no credential", msg, notify_on)
        return 4

    frame, final = _open(a.access_url, jar)
    print(f"frame   : {len(frame)} bytes from {final[:110]}")
    if a.dump_frame:
        open(a.dump_frame, "w").write(frame)
        print(f"dumped  : {a.dump_frame}")

    cands = extract_m3u8(frame)
    good = [u for u in cands if validate(u) is None]
    if not good:
        msg = (f"{a.slug or a.webcastid}: reached the page but found no usable m3u8 "
               f"({len(cands)} candidate(s): {cands[:3]})")
        print("FAIL: " + msg, file=sys.stderr)
        for u in cands:
            print(f"  rejected: {u[:120]}  — {validate(u)}", file=sys.stderr)
        notify("chorus-call resolve found no m3u8", msg, notify_on)
        return 5

    url = good[0]
    if len(good) > 1:
        print(f"note    : {len(good)} usable candidates, taking the first:")
        for u in good:
            print(f"          {u[:140]}")

    out = a.out or (os.path.expanduser(f"~/{a.slug}-stream.url") if a.slug else None)
    if not out:
        print(url)
        return 0
    write_url(out, url)
    print(f"wrote   : {out}\n          {url[:160]}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.URLError as e:
        print(f"FAIL: network: {e}", file=sys.stderr)
        sys.exit(6)
