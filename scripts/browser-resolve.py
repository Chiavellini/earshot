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
nothing seen. 4 = page never loaded. 2 = usage.
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

        while time.time() < deadline and not any(found.values()):
            pg.wait_for_timeout(3000)
            if found:      # a variant is in hand; give the master a short grace
                grace = time.time() + 20
                while time.time() < grace and not any(found.values()):
                    pg.wait_for_timeout(2000)
                break

        try:
            (browser or ctx).close()
        except Exception:
            pass

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
