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

STATUS = {0: "upcoming", 1: "LIVE", 2: "in-progress/paused", 3: "archived",
          4: "expired"}   # 4 observed live 2026-07-29 on a lapsed archive


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


# --- registration ------------------------------------------------------------
# LIVE-VERIFIED 2026-07-29 (VERIFICATIONS Q-CC): the form POSTs the four
# identity fields plus the page's hidden inputs, and the ENDPOINT DIFFERS BY
# EVENT STATE — `preregistration/submit` for an upcoming event (WING, status 0)
# vs `registration/submit` for a live/archived one (SBUX archive, status 3).
# Posting to the wrong one is how this silently no-ops, so derive it, never
# hardcode it.
def register(webcastid, page, ident, jar):
    hidden = parse_hidden_inputs(page)
    m = re.search(r"<form[^>]*action=\"([^\"]+)\"", page)
    action = m.group(1) if m else None
    if not action:
        raise RuntimeError("no registration form on the page")
    url = urllib.parse.urljoin(HOST + "/mediaframe/", action)
    fields = dict(hidden)
    fields.update({
        "firstName": ident["first"], "lastName": ident["last"],
        "email": ident["email"], "company": ident["company"],
    })
    body = urllib.parse.urlencode(fields).encode()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": PAGE.format(wid=webcastid),
    })
    with op.open(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace"), r.geturl()


# --- media extraction --------------------------------------------------------
# Deliberately generic: pull every absolute media URL out of whatever page we
# reached and let validate() rank them.  We do NOT invent a URL template — the
# shape of the LIVE stream URL is still UNPINNED in VERIFICATIONS Q-CC (the
# archive fixture serves a plain Akamai .mp4), and this repo does not code
# against remembered API facts.  Reporting the rejects matters as much as
# finding the winner: "no m3u8" plus a list of what WAS there is diagnosable at
# 07:40; a bare "nothing found" is not.
MEDIA_RE = re.compile(
    r"https?://[^\s\"'<>\\]{6,300}?\.(?:m3u8|mpd|mp4|mp3)(?:\?[^\s\"'<>\\]{0,200})?")


def extract_media(page):
    seen, out = set(), []
    for u in MEDIA_RE.findall(page):
        u = html.unescape(u)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def extract_m3u8(page):
    return [u for u in extract_media(page) if ".m3u8" in u]


# The player does not guess, and neither should we. frameScripts.js builds its
# videojs source list from ONE field — `webcastConfig.hlsSource` — and branches
# on its extension (live-read 2026-07-29, VERIFICATIONS Q-CC):
#     isValidURL(hlsSource,'http-hls') -> application/x-mpegurl   (.m3u8)
#     isValidURL(hlsSource,'http-mp4') -> video/mp4
#     isValidURL(hlsSource,'http-m4a') -> audio/mp4
# So read that field by name; the generic scrape stays only as a backstop for a
# page shape we have not seen.
def extract_hls_source(page):
    m = re.search(r"webcastConfig\.hlsSource\s*=\s*\"([^\"]+)\"", page)
    return html.unescape(m.group(1)) if m else None


PROGRESSIVE = (".mp4", ".m4a", ".mp3", ".aac")


def classify(url):
    """'hls' | 'progressive' | None — which arm path this URL needs."""
    base = url.split("?", 1)[0].lower()
    if base.endswith(".m3u8"):
        return "hls"
    if base.endswith(PROGRESSIVE):
        return "progressive"
    return None


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

    print("extract_m3u8 / extract_media:")
    u = extract_m3u8(FRAME_FIXTURE)
    chk("finds both", len(u) == 2, repr(u))
    chk("keeps query", u and u[0].endswith("token=abc"), repr(u[:1]))
    chk("none on register page", extract_m3u8(FIXTURE) == [])
    # Real archive fixture: the SBUX Q1 frame page served a plain Akamai mp4.
    # extract_media must SEE it (so the failure message can name it) while
    # extract_m3u8 must NOT offer it to the arm.
    arch = 'src: "http://vodchoruscall.akamaized.net/07452/starbucks/starbucks260128.mp4"'
    chk("media sees archive mp4", extract_media(arch) and extract_media(arch)[0].endswith(".mp4"))
    chk("m3u8 rejects archive mp4", extract_m3u8(arch) == [])

    print("extract_hls_source / classify (frameScripts.js contract):")
    real = ('webcastConfig.hlsSource = '
            '"http://vodchoruscall.akamaized.net/07452/starbucks/starbucks260128.mp4";')
    chk("reads hlsSource by name", extract_hls_source(real) ==
        "http://vodchoruscall.akamaized.net/07452/starbucks/starbucks260128.mp4")
    chk("absent -> None", extract_hls_source(FIXTURE) is None)
    chk("classify hls", classify("https://x.net/live/a/master.m3u8") == "hls")
    chk("classify hls w/ query", classify("https://x.net/a.m3u8?tok=1") == "hls")
    chk("classify progressive mp4", classify("http://x.net/a/b260128.mp4") == "progressive")
    chk("classify progressive m4a", classify("http://x.net/a.m4a") == "progressive")
    chk("classify unknown", classify("https://x.net/a.bin") is None)

    print("register endpoint selection (live-verified shapes):")
    up = '<form method="post" action="preregistration/submit">' + FIXTURE
    lv = '<form method="post" action="registration/submit">' + FIXTURE
    chk("upcoming -> preregistration",
        re.search(r'action="([^"]+)"', up).group(1) == "preregistration/submit")
    chk("live/archived -> registration",
        re.search(r'action="([^"]+)"', lv).group(1) == "registration/submit")
    chk("hidden inputs carried into POST", parse_hidden_inputs(up).get("streamId") == "28574")

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
    ap.add_argument("--register", action="store_true",
                    help="submit the registration form to reach the `frame` page")
    # Defaults come from the environment so the identity is never baked into a
    # unit file or a repo config. See memory/RUNBOOK: registrations use the
    # Actinver analyst identity, never the personal address.
    ap.add_argument("--first", default=os.environ.get("EARSHOT_REG_FIRST"))
    ap.add_argument("--last", default=os.environ.get("EARSHOT_REG_LAST"))
    ap.add_argument("--email", default=os.environ.get("EARSHOT_REG_EMAIL"))
    ap.add_argument("--company", default=os.environ.get("EARSHOT_REG_COMPANY"))
    ap.add_argument("--out")
    ap.add_argument("--wait-secs", type=int, default=0, help="poll until status==1 (live)")
    ap.add_argument("--poll-secs", type=int, default=10)
    ap.add_argument("--status-only", action="store_true")
    ap.add_argument("--allow-progressive", action="store_true",
                    help="accept a non-HLS hlsSource (.mp4/.m4a/.mp3) and mark it "
                         "kind=progressive; the caller must then arm the generic "
                         "ffmpeg leg instead of the HLS edge-fetch leg")
    ap.add_argument("--any-status", action="store_true",
                    help="skip the live gate — for validating the resolve chain "
                         "against an ARCHIVED event; never use on an arm path")
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
    if a.any_status and st != 1:
        print(f"  --any-status: proceeding on status {st} (VALIDATION ONLY)")
        st = 1
    while st != 1 and time.time() < deadline:
        time.sleep(a.poll_secs)
        st = event_status(sid)
        print(f"  … status {st} ({STATUS.get(st, 'unknown')})  {int(deadline - time.time())}s left")
    if st != 1:
        # Not fatal when we hold a credential: the media poll below is the real
        # gate, and the player can carry the stream URL before the status bit
        # trips. Only a credential-less run has nothing further to try.
        if not (a.register or a.access_url):
            msg = f"{a.slug or a.webcastid}: still status {st} ({STATUS.get(st,'?')}) after {a.wait_secs}s"
            print("FAIL: " + msg, file=sys.stderr)
            notify("chorus-call not live", msg, notify_on)
            return 3
        print(f"  status still {st}; falling through to media polling anyway")

    # --- RESOLVE ------------------------------------------------------------
    # Reach the `frame` page one of two ways: an emailed/access link, or by
    # registering (live-verified 2026-07-29 — the POST returns the frame page
    # directly for a live/archived event).
    # Already on the player? Then there is nothing to register. This happens for
    # real — the venue can serve `frame` straight away to a registrant it
    # recognises — and blindly POSTing produces "no registration form" instead
    # of just using the page we already hold.
    already_frame = (cfg.get("pageId") == "frame") or bool(extract_hls_source(page))
    if already_frame:
        print("note    : already on the player page; skipping registration")
        frame, final = page, PAGE.format(wid=a.webcastid)
    elif a.access_url:
        frame, final = _open(a.access_url, jar)
    elif a.register:
        ident = {"first": a.first, "last": a.last, "email": a.email, "company": a.company}
        missing = [k for k, v in ident.items() if not v]
        if missing:
            print(f"FAIL: --register needs {missing} (or EARSHOT_REG_* env)", file=sys.stderr)
            return 4
        frame, final = register(a.webcastid, page, ident, jar)
        # An upcoming-event POST lands on thankYou, not the player; re-fetch the
        # webcast page with the session now that we are registered.
        if "pageId = \"frame\"" not in frame and "pageId: \"frame\"" not in frame:
            frame, final = _open(PAGE.format(wid=a.webcastid), jar)
    else:
        msg = (f"{a.slug or a.webcastid} is LIVE but no credential was given: pass "
               "--access-url <emailed link> or --register with the identity fields.")
        print("FAIL: " + msg, file=sys.stderr)
        notify("chorus-call LIVE, no credential", msg, notify_on)
        return 4

    pid = re.search(r"pageId\s*[:=]\s*\"([^\"]*)\"", frame)
    print(f"frame   : {len(frame)} bytes, pageId={pid.group(1) if pid else '?'} from {final[:100]}")
    if a.dump_frame:
        open(a.dump_frame, "w").write(frame)
        print(f"dumped  : {a.dump_frame}")

    # Poll the FRAME PAGE for media, not just the status flip.  The status
    # integer is the venue's own "we have started" bit, but the player often
    # carries the stream URL before it trips — and every second we resolve
    # early is a second of lead the arm gets on the opening remarks.  Register
    # ONCE (a re-POST per poll would spam duplicate registrations) and re-fetch
    # with the same session.
    def pick(pg):
        """(url, kind, all_seen). hlsSource by name first, generic scrape after."""
        seen = extract_media(pg)
        named = extract_hls_source(pg)
        if named:
            seen = [named] + [u for u in seen if u != named]
        for u in seen:
            k = classify(u)
            if k == "hls" and validate(u) is None:
                return u, k, seen
        if a.allow_progressive:
            for u in seen:
                if classify(u) == "progressive":
                    return u, "progressive", seen
        return None, None, seen

    url, kind, media = pick(frame)
    while not url and time.time() < deadline:
        time.sleep(a.poll_secs)
        try:
            frame, _ = _open(PAGE.format(wid=a.webcastid), jar)
        except urllib.error.URLError as e:
            print(f"  … fetch failed ({e}); retrying", file=sys.stderr)
            continue
        url, kind, media = pick(frame)
        print(f"  … hlsSource={str(extract_hls_source(frame))[:70]!r} "
              f"({len(media)} media seen)  {int(deadline - time.time())}s left", flush=True)

    if not url:
        msg = (f"{a.slug or a.webcastid}: reached the player but no usable stream "
               f"({len(media)} media candidate(s) present)")
        print("FAIL: " + msg, file=sys.stderr)
        for u in media:
            print(f"  rejected: {u[:130]}  — kind={classify(u)} {validate(u) or ''}",
                  file=sys.stderr)
        if not media:
            print("  (webcastConfig.hlsSource empty — player still on a standby card?)",
                  file=sys.stderr)
        notify("chorus-call resolve found no stream", msg + f" | saw: {media[:3]}", notify_on)
        return 5

    if kind == "progressive":
        print(f"note    : NOT HLS — progressive media ({url.split('?')[0][-12:]}). "
              "The arm must use the generic-ffmpeg fallback leg; the HLS edge-fetch "
              "leg cannot read this.")

    out = a.out or (os.path.expanduser(f"~/{a.slug}-stream.url") if a.slug else None)
    if not out:
        print(url)
        return 0
    write_url(out, url)
    # The companion .kind file is how the launcher picks the arm path without
    # re-parsing the URL. Written AFTER the url file so it can never claim a
    # kind for a URL that was not written.
    write_url(out.replace(".url", ".kind") if out.endswith(".url") else out + ".kind", kind)
    print(f"wrote   : {out}  [kind={kind}]\n          {url[:160]}")
    return 0


if __name__ == "__main__":
    # NOTHING may escape as a traceback. This runs unattended at 13:25Z with the
    # operator asleep; a stack trace on stderr and exit 1 is indistinguishable
    # from a crash and pages nobody. Every abnormal exit gets a named code and,
    # unless --no-notify, an alert. (Learned the hard way 2026-07-29: an expired
    # fixture made register() raise and the process exited 1, silently.)
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except urllib.error.URLError as e:
        print(f"FAIL: network: {e}", file=sys.stderr)
        if "--no-notify" not in sys.argv:
            notify("chorus-call resolve: network failure", str(e))
        sys.exit(6)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL: unexpected: {type(e).__name__}: {e}", file=sys.stderr)
        if "--no-notify" not in sys.argv:
            notify("chorus-call resolve CRASHED",
                   f"{type(e).__name__}: {e} — the event was NOT armed.")
        sys.exit(7)
