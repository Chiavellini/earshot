#!/usr/bin/env python3
"""Chorus Call mediaframe: register, then capture the player's manifest.

    chorus-register-resolve.py --slug you --webcastid NTtHOW8v [--wait-secs 1800]

WHY: scripts/choruscall-resolve.py solves "has it started?" anonymously
(getPolledStatus) but needs an EMAILED access-url for the media. Many Chorus
Call webcasts are instead gated by a plain self-serve registration form
(pageId=register, loginType=1): first/last/email/company -> Submit -> player.
That form is fillable without any emailed link, which is what YOU (Clear
Secure) used on 2026-08-05 while the operator was away.

Uses the STANDING identity in ~/.earshot/registration.env (the same values
preflight round-trip-checks) and watches the network for the media manifest,
preferring a master over a variant. Writes $HOME/<slug>-stream.url.

Exit 0 wrote a URL / 3 no manifest / 4 identity missing. Node: control.acquire.
"""
import argparse
import os
import re
import subprocess
import sys
import time

REPO = os.environ.get("EARSHOT_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_RE = re.compile(r"\.m3u8(\?|$)|\.mpd(\?|$)|/manifest|/playlist", re.I)


def page_alert(subject, body):
    try:
        subprocess.run([os.path.join(REPO, "scripts", "notify.sh"), subject, body, "--quiet"],
                       timeout=60, check=False)
    except Exception:
        pass


def identity():
    vals = {}
    try:
        for ln in open(os.path.expanduser("~/.earshot/registration.env")):
            ln = ln.strip()
            if ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return (vals.get("EARSHOT_REG_FIRST", ""), vals.get("EARSHOT_REG_LAST", ""),
            vals.get("EARSHOT_REG_EMAIL", ""), vals.get("EARSHOT_REG_COMPANY", ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--webcastid", required=True)
    ap.add_argument("--wait-secs", type=int, default=1800)
    ap.add_argument("--out", default=None)
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()

    first, last, email, company = identity()
    if not (first and last and email):
        print("REG-IDENTITY-MISSING", file=sys.stderr)
        return 4
    out = a.out or os.path.join(os.path.expanduser("~"), f"{a.slug}-stream.url")
    url = f"https://event.choruscall.com/mediaframe/webcast.html?webcastid={a.webcastid}"

    from playwright.sync_api import sync_playwright
    found = {}
    deadline = time.time() + a.wait_secs

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            f"/tmp/chorus-profile-{a.slug}", headless=not a.headed,
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"))

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
            # Amazon IVS (what Q4 serves) has two shapes and only ONE survives:
            #   playback.live-video.net/api/video/v1/<channel>.m3u8  = the stable
            #     CHANNEL master — re-resolvable for the whole call;
            #   playlist.live-video.net/v1/playlist/<token>          = a TOKENIZED
            #     variant that expires and cannot be re-resolved (RUNBOOK §V6:
            #     the leg dies mid-run and the fetcher can never recover).
            # Classify by URL shape FIRST; a 404-at-first-poll channel URL is
            # still the right answer (the channel 200s once the encoder starts).
            if re.search(r"playback\.live-video\.net/api/video/v1/", u, re.I):
                is_master = True
            elif re.search(r"playlist\.live-video\.net/v1/playlist/", u, re.I):
                is_master = False
            else:
                is_master = ("#EXT-X-STREAM-INF" in body) if body else bool(re.search(r"master|index", u, re.I))
                if body and "#EXTINF" in body and "#EXT-X-STREAM-INF" not in body:
                    is_master = False
            found[u] = is_master
            print(f"MANIFEST-SEEN {'master' if is_master else 'variant'} [{resp.status}] {u[:170]}", flush=True)

        ctx.on("response", on_response)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto(url, timeout=60000, wait_until="domcontentloaded")
        pg.wait_for_timeout(5000)
        print(f"PAGE-LOADED {url}", flush=True)

        rounds = 0
        while time.time() < deadline and not any(found.values()):
            rounds += 1
            # Fill + submit the registration form whenever it is present.
            try:
                if pg.locator("#firstName").count() and pg.locator("#firstName").first.is_visible():
                    pg.locator("#firstName").first.fill(first)
                    pg.locator("#lastName").first.fill(last)
                    pg.locator("#email").first.fill(email)
                    for sel in ("#company", "input[name=company]", "#companyName"):
                        try:
                            el = pg.locator(sel).first
                            if el.count():
                                el.fill(company)
                                break
                        except Exception:
                            continue
                    print(f"FORM-FILLED as {first} {last} <{email}> / {company}", flush=True)
                    for bsel in ("button:has-text('Submit')", "input[type=submit]", "#submitButton"):
                        try:
                            b2 = pg.locator(bsel).first
                            if b2.count() and b2.is_visible():
                                b2.click(timeout=6000)
                                print(f"SUBMITTED via {bsel}", flush=True)
                                pg.wait_for_timeout(6000)
                                break
                        except Exception:
                            continue
            except Exception as e:
                print(f"register step: {type(e).__name__}", flush=True)

            for sel in ["button[aria-label*='lay' i]", ".vjs-big-play-button", "video",
                        "text=/^(play|listen|launch)/i"]:
                try:
                    el = pg.locator(sel).first
                    if el.count() and el.is_visible():
                        el.click(timeout=3000)
                        print(f"CLICKED play {sel}", flush=True)
                        pg.wait_for_timeout(2500)
                except Exception:
                    pass

            if found:
                grace = time.time() + 25
                while time.time() < grace and not any(found.values()):
                    pg.wait_for_timeout(2000)
                break
            pg.wait_for_timeout(15000)
            if time.time() >= deadline:
                break
            try:
                pg.reload(timeout=45000, wait_until="domcontentloaded")
                pg.wait_for_timeout(4000)
                print(f"RELOAD #{rounds} {time.strftime('%H:%M:%SZ', time.gmtime())} :: "
                      f"{pg.inner_text('body')[:110]}", flush=True)
            except Exception as e:
                print(f"reload failed: {type(e).__name__}", flush=True)
        try:
            ctx.close()
        except Exception:
            pass

    if not found:
        print(f"NO-MANIFEST: {a.slug}", file=sys.stderr)
        page_alert(f"{a.slug} chorus-register: NO MANIFEST",
                   f"Registered on {url} but saw no .m3u8. Manual: open it, DevTools Network filter m3u8, "
                   f"then printf '%s' '<URL>' > {out}")
        return 3
    best = next((u for u, m in found.items() if m), None) or sorted(found, key=len)[0]
    tmp = out + ".tmp"
    open(tmp, "w").write(best)
    os.replace(tmp, out)
    print(f"URL-WRITTEN → {out}\n{best}")
    page_alert(f"{a.slug} URL RESOLVED (chorus register)", f"{best[:250]} → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
