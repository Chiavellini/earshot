#!/usr/bin/env python3
"""Headless-browser stream resolver — do what the sleeping operator would do.

    browser-resolve.py --slug uber --url <event-page> [--wait-secs 900]
                       [--out $HOME/<slug>-stream.url] [--click-play]
                       [--profile <persistent-dir>] [--no-notify]

WHY THIS EXISTS: on 2026-08-04 SPCX, ZETA and TOST were all lost the same way
— the watchers ran correctly and expired at `URL-MISSED` because the ONE step
that needed a human (open the IR page in a logged-in browser at T-15, read the
master .m3u8 out of DevTools, paste it into $HOME/<slug>-stream.url) had no
human. Every other resolver in this repo is venue-specific (choruscall,
youtube). This one is venue-agnostic because it works the way the operator
does: drive a real browser, let the page's own JavaScript mint whatever
tokens it wants, and WATCH THE NETWORK for the manifest the player fetches.

What it captures: the first response whose URL looks like a media manifest
(.m3u8 / .mpd, or a URL containing 'manifest'/'playlist'), preferring a MASTER
playlist over a media/variant one — handing the fetcher a variant is the §V6
failure (the media URL dies mid-run and the fetcher cannot re-resolve).
Requests made inside IFRAMES are seen too: that is the Ford/OpenExchange class
where the top frame never requests the m3u8 itself.

What it does NOT do: log in, accept terms, solve captchas, or click through a
registration wall. A venue that gates the player behind credentials needs
--profile pointing at a persistent context the operator logged into ONCE (the
session is then reused headlessly), or it stays a human step. It also never
invents a URL: if nothing is observed, it exits nonzero and pages, because the
failure this script exists to prevent is a SILENT one.

Exit 0 only when a manifest was observed AND written. 3 = deadline with
nothing seen. 4 = page never loaded. 5 = a registration/login wall is blocking
the player (paged IMMEDIATELY — on AMAT 2026-08-13 this script watched a
registration form in silence for 110 minutes; a wall is a page-worthy state
the second it is seen, and it stays a human step). 2 = usage.
Node: control.acquire (compatibility surface: $HOME/<slug>-stream.url).
"""
import argparse
import os
import re
import subprocess
import sys
import time

REPO = os.environ.get("EARSHOT_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_RE = re.compile(r"\.m3u8(\?|$)|\.mpd(\?|$)|/manifest|/playlist", re.I)
# A master lists variants (#EXT-X-STREAM-INF); a media playlist lists segments
# (#EXTINF). run-arm.sh guard 3 refuses a variant, so prefer the master.
MASTER_HINT = re.compile(r"master|index|playlist\.m3u8", re.I)


def page(subject, body, quiet=True):
    try:
        cmd = [os.path.join(REPO, "scripts", "notify.sh"), subject, body]
        if quiet:
            cmd.append("--quiet")
        subprocess.run(cmd, timeout=60, check=False)
    except Exception:
        pass


def looks_master(url, body):
    if body and "#EXT-X-STREAM-INF" in body:
        return True
    if body and "#EXTINF" in body:
        return False
    return bool(MASTER_HINT.search(url))


# In-page probe: visible form/input/media census for one frame. Kept dumb on
# purpose — the DECISION lives in is_wall(), which is pure and self-tested.
WALL_PROBE_JS = """() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const count = sel => Array.from(document.querySelectorAll(sel)).filter(vis).length;
  return {
    pw: count('input[type=password]'),
    email: count('input[type=email]'),
    text: count('form input[type=text], form input:not([type])'),
    forms: count('form'),
    media: document.querySelectorAll('video, audio').length,
  };
}"""


def is_wall(sig):
    """A visible password field, or a visible form asking for email/text with
    no media element on the page, is a registration/login wall (the AMAT
    2026-08-13 shape: First name / Last name / E-mail / Company, no player)."""
    return sig.get("pw", 0) > 0 or (
        sig.get("forms", 0) > 0
        and (sig.get("email", 0) > 0 or sig.get("text", 0) >= 2)
        and sig.get("media", 0) == 0
    )


def probe_wall(pg):
    """Merge the census across the top frame and every child frame (the
    Ford/OpenExchange class embeds the player — and its wall — in an iframe)."""
    total = {"pw": 0, "email": 0, "text": 0, "forms": 0, "media": 0}
    for frame in pg.frames:
        try:
            sig = frame.evaluate(WALL_PROBE_JS)
            for k in total:
                total[k] += int(sig.get(k, 0))
        except Exception:
            continue
    return total


def try_click_play(pg):
    for sel in ["text=/listen|play|launch|join|webcast|audio/i",
                "button[aria-label*='lay' i]", ".vjs-big-play-button", "video"]:
        try:
            el = pg.locator(sel).first
            if el.count() and el.is_visible():
                el.click(timeout=4000)
                print(f"CLICKED {sel}", flush=True)
                pg.wait_for_timeout(2500)
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--url", required=True, help="event/webcast page to open")
    ap.add_argument("--out", default=None)
    ap.add_argument("--wait-secs", type=int, default=900,
                    help="how long to keep the page open waiting for the player to start")
    ap.add_argument("--click-play", action="store_true",
                    help="try clicking obvious play/listen buttons")
    ap.add_argument("--profile", default=None,
                    help="persistent browser profile dir (for venues the operator logged into once)")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--check", action="store_true", help="offline self-test")
    a = ap.parse_args()

    if a.check:
        assert MANIFEST_RE.search("https://x/live/master.m3u8")
        assert MANIFEST_RE.search("https://x/a/manifest?tok=1")
        assert not MANIFEST_RE.search("https://x/style.css")
        assert looks_master("https://x/master.m3u8", "#EXT-X-STREAM-INF:BANDWIDTH=1")
        assert not looks_master("https://x/media.m3u8", "#EXTINF:6.0,")
        # Wall verdicts (AMAT 2026-08-13 fixtures): the registration form
        # (name/email/company, no player) IS a wall; a page with a form AND a
        # media element (a consent overlay next to a player) is NOT; a lone
        # password field always is.
        assert is_wall({"pw": 0, "email": 1, "text": 3, "forms": 1, "media": 0})
        assert is_wall({"pw": 1, "email": 0, "text": 0, "forms": 0, "media": 1})
        assert not is_wall({"pw": 0, "email": 1, "text": 1, "forms": 1, "media": 1})
        assert not is_wall({"pw": 0, "email": 0, "text": 1, "forms": 1, "media": 0})
        assert not is_wall({"pw": 0, "email": 0, "text": 0, "forms": 0, "media": 0})
        print("browser-resolve: self-test PASS")
        return 0

    out = a.out or os.path.join(os.path.expanduser("~"), f"{a.slug}-stream.url")
    from playwright.sync_api import sync_playwright

    found = {}   # url -> is_master
    deadline = time.time() + a.wait_secs

    with sync_playwright() as p:
        if a.profile:
            ctx = p.chromium.launch_persistent_context(a.profile, headless=not a.headed)
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            browser = None
        else:
            browser = p.chromium.launch(headless=not a.headed)
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"))
            pg = ctx.new_page()

        def on_response(resp):
            u = resp.url
            if not MANIFEST_RE.search(u) or u in found:
                return
            body = ""
            try:
                if resp.status == 200 and len(u) < 2000:
                    body = resp.text()[:4000]
            except Exception:
                pass
            found[u] = looks_master(u, body)
            print(f"MANIFEST-SEEN {'master' if found[u] else 'variant'} "
                  f"[{resp.status}] {u[:180]}", flush=True)

        ctx.on("response", on_response)

        try:
            pg.goto(a.url, timeout=60000, wait_until="domcontentloaded")
            print(f"PAGE-LOADED {a.url}", flush=True)
        except Exception as e:
            print(f"PAGE-LOAD-FAILED {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            if not a.no_notify:
                page(f"{a.slug} resolver: page did not load",
                     f"{a.url} — {type(e).__name__}. Manual paste needed: printf '%s' '<m3u8>' > {out}")
            return 4

        if a.click_play:
            try_click_play(pg)

        wall_hit = False
        started = time.time()
        last_wall_check = 0.0
        last_click = time.time()
        last_beat = time.time()
        while time.time() < deadline and not any(found.values()):
            pg.wait_for_timeout(3000)
            if found:      # a variant is in hand; give the master a short grace
                grace = time.time() + 20
                while time.time() < grace and not any(found.values()):
                    pg.wait_for_timeout(2000)
                break
            now = time.time()
            # A wall is a page-worthy state the moment it is seen — never a
            # quiet one (AMAT 2026-08-13: 110 silent minutes at a form).
            # First check right away, then every 60 s in case one is minted
            # later by the page.
            if last_wall_check == 0.0 or now - last_wall_check >= 60:
                last_wall_check = now
                if is_wall(probe_wall(pg)):
                    wall_hit = True
                    break
            if a.click_play and now - last_click >= 30:
                last_click = now
                try_click_play(pg)
            if now - last_beat >= 300:
                print(f"WAITING {int(now - started)}/{a.wait_secs}s — no manifest yet",
                      flush=True)
                last_beat = now

        try:
            (browser or ctx).close()
        except Exception:
            pass

    if wall_hit and not found:
        print(f"WALL-DETECTED: {a.slug} — a registration/login form is blocking "
              f"the player", file=sys.stderr)
        if not a.no_notify:
            if a.profile:
                page(f"{a.slug} RESOLVER BLOCKED BY WALL (profile session dead)",
                     f"{a.url} shows a registration/login form even with profile "
                     f"{a.profile} — the saved session is missing or expired. "
                     f"Re-register in that profile NOW, or manual: DevTools m3u8 "
                     f"→ printf '%s' '<URL>' > {out}")
            else:
                page(f"{a.slug} RESOLVER BLOCKED BY REGISTRATION WALL",
                     f"{a.url} gates its player behind a form and this AccessPlan "
                     f"has no session. Register NOW in a browser (standing identity), "
                     f"then manual: DevTools m3u8 → printf '%s' '<URL>' > {out}. "
                     f"For next time: bind registered-session + --profile.")
        return 5

    if not found:
        print(f"NO-MANIFEST: {a.slug} — nothing manifest-shaped in {a.wait_secs}s", file=sys.stderr)
        if not a.no_notify:
            page(f"{a.slug} RESOLVER FOUND NOTHING",
                 f"Browser opened {a.url} for {a.wait_secs}s and saw no .m3u8/.mpd. "
                 f"The player may be login-gated or not started. Manual: open the page, "
                 f"DevTools Network filter m3u8, then printf '%s' '<URL>' > {out}")
        return 3

    # master wins; else the single best variant (better than nothing — the
    # fetcher cannot re-resolve it, so the run is one-shot, but it hears).
    best = next((u for u, m in found.items() if m), None)
    kind = "master"
    if best is None:
        best = sorted(found, key=len)[0]
        kind = "VARIANT (no master seen — §V6 risk: cannot re-resolve mid-run)"

    tmp = out + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(best)
    os.replace(tmp, out)
    print(f"URL-WRITTEN {kind} → {out}\n{best}")
    if not a.no_notify:
        page(f"{a.slug} URL RESOLVED ({kind.split()[0]})", f"{best[:300]} → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
