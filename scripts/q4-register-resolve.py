#!/usr/bin/env python3
"""Q4 attendee platform: guest-register, then capture the player's manifest.

    q4-register-resolve.py --slug uber --event-id 806924129 [--wait-secs 2400]

WHY: the Q4 REST twin (attendees.events.q4inc.com/rest/v1/event/<id>) exposes
status/msToStart publicly but its `broadcastUrl` stays EMPTY even after the
event flips to STARTED (verified on Uber 2026-08-05 11:17Z and on Disney's
ENDED Q1 FY26 event). The manifest only exists inside the attendee player,
which sits behind a registration wall offering an anonymous path:
"Continue without a Q4 account" -> a guest form -> the player.

This walks that path with the STANDING registration identity from
~/.earshot/registration.env (EARSHOT_REG_FIRST/LAST/EMAIL/COMPANY — the same
values preflight round-trip-checks), then watches the network for the media
manifest exactly like browser-resolve.py, preferring a master over a variant.

Registration is the project's documented standing workflow for IR webcasts;
this script performs it non-interactively and touches nothing else. It never
creates an account, never stores a password, and submits only the identity
already recorded for this purpose.

Exit 0 wrote a URL / 3 no manifest before deadline / 4 registration failed.
Node: control.acquire.
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


def reg_identity():
    env = os.path.expanduser("~/.earshot/registration.env")
    vals = {}
    try:
        for ln in open(env):
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
    ap.add_argument("--event-id", required=True)
    ap.add_argument("--wait-secs", type=int, default=2400)
    ap.add_argument("--role", default="Analyst")
    ap.add_argument("--out", default=None)
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()

    first, last, email, company = reg_identity()
    if not (first and last and email):
        print("REG-IDENTITY-MISSING: ~/.earshot/registration.env lacks EARSHOT_REG_*", file=sys.stderr)
        return 4
    out = a.out or os.path.join(os.path.expanduser("~"), f"{a.slug}-stream.url")
    url = f"https://events.q4inc.com/attendee/{a.event_id}"

    from playwright.sync_api import sync_playwright
    found = {}
    deadline = time.time() + a.wait_secs
    profile = os.path.join("/tmp", f"q4-profile-{a.slug}")

    with sync_playwright() as p:
        # Persistent profile: the guest registration is stored client-side, so
        # the RELOAD loop below must keep it. The player itself does not exist
        # until T-15 ("can access the webcast up to 15 minutes before the start
        # time"), which is why a single page-load can never find the manifest.
        ctx = p.chromium.launch_persistent_context(
            profile, headless=not a.headed,
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"))
        b = None

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

        # ── registration wall (skipped automatically if already past it) ──
        try:
            guest = pg.get_by_text("Continue without a Q4 account").first
            if guest.count() and guest.is_visible():
                guest.click(timeout=8000)
                pg.wait_for_timeout(3500)
                print("CLICKED continue-as-guest", flush=True)
        except Exception as e:
            print(f"guest-path not present ({type(e).__name__}) — maybe already registered", flush=True)

        filled = 0
        for sel, val in (("#GuestRegistrationFirstNameInput", first),
                         ("#GuestRegistrationLastNameInput", last),
                         ("#GuestRegistrationEmailInput", email),
                         ("#GuestRegistrationInstitutionLookupInput", company),
                         ("#GuestRegistrationRoleFieldInput", a.role)):
            try:
                el = pg.locator(sel).first
                if el.count():
                    if "Institution" in sel:
                        # Typeahead: typing is not enough — the form reports
                        # "Company name required" unless an OPTION is chosen.
                        # Q4 offers a free-text option: Enter "<x>" as Company Name.
                        el.click()
                        el.type(val, delay=110)
                        pg.wait_for_timeout(3000)
                        picked = False
                        for opt in (f'Enter "{val}" as Company Name', val):
                            try:
                                o = pg.get_by_text(opt, exact=False).first
                                if o.count() and o.is_visible():
                                    o.click(timeout=5000)
                                    picked = True
                                    print(f"PICKED company option: {opt!r}", flush=True)
                                    break
                            except Exception:
                                continue
                        if not picked:
                            pg.keyboard.press("ArrowDown")
                            pg.keyboard.press("Enter")
                        pg.wait_for_timeout(900)
                    else:
                        el.fill(val, timeout=6000)
                        pg.wait_for_timeout(600)
                        if "Role" in sel:
                            try:
                                pg.keyboard.press("Enter")
                                pg.wait_for_timeout(500)
                            except Exception:
                                pass
                    filled += 1
            except Exception as e:
                print(f"field {sel} not filled: {type(e).__name__}", flush=True)
        print(f"FORM-FILLED {filled} field(s) as {first} {last} <{email}> / {company}", flush=True)

        if filled:
            for label in ("Register for this Event", "Register", "Submit"):
                try:
                    btn = pg.get_by_role("button", name=re.compile(label, re.I)).first
                    if btn.count() and btn.is_enabled():
                        btn.click(timeout=8000)
                        print(f"CLICKED {label!r}", flush=True)
                        pg.wait_for_timeout(6000)
                        break
                except Exception:
                    continue

        print("BODY-NOW:", pg.inner_text("body")[:200].replace("\n", " | "), flush=True)

        # try to start playback if a play control is present
        for sel in ["button[aria-label*='lay' i]", ".vjs-big-play-button", "video",
                    "text=/^(play|listen|join)/i"]:
            try:
                el = pg.locator(sel).first
                if el.count() and el.is_visible():
                    el.click(timeout=4000)
                    print(f"CLICKED play {sel}", flush=True)
                    pg.wait_for_timeout(2500)
            except Exception:
                pass

        # RELOAD LOOP: the attendee player opens ~T-15. Reload until it exists,
        # clicking any play control each pass. Registration persists via profile.
        rounds = 0
        while time.time() < deadline and not any(found.values()):
            rounds += 1
            for sel in ["button[aria-label*='lay' i]", ".vjs-big-play-button", "video",
                        "text=/^(play|listen|join|enter)/i"]:
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
            pg.wait_for_timeout(20000)
            if time.time() >= deadline:
                break
            try:
                pg.reload(timeout=45000, wait_until="domcontentloaded")
                pg.wait_for_timeout(4000)
                body = pg.inner_text("body")[:120].replace("\n", " ")
                print(f"RELOAD #{rounds} {time.strftime('%H:%M:%SZ', time.gmtime())} :: {body}", flush=True)
                # if the wall reappeared (new context/session), re-register
                g = pg.get_by_text("Continue without a Q4 account").first
                if g.count() and g.is_visible():
                    print("wall reappeared — re-registering", flush=True)
                    g.click(timeout=6000); pg.wait_for_timeout(3000)
            except Exception as e:
                print(f"reload failed: {type(e).__name__}", flush=True)
        try:
            ctx.close()
        except Exception:
            pass

    if not found:
        print(f"NO-MANIFEST: {a.slug} after registration", file=sys.stderr)
        page_alert(f"{a.slug} q4-register: NO MANIFEST",
                   f"Registered but saw no .m3u8 on {url}. Manual: open it, DevTools Network filter m3u8, "
                   f"then printf '%s' '<URL>' > {out}")
        return 3

    best = next((u for u, m in found.items() if m), None) or sorted(found, key=len)[0]
    tmp = out + ".tmp"
    open(tmp, "w").write(best)
    os.replace(tmp, out)
    print(f"URL-WRITTEN → {out}\n{best}")
    page_alert(f"{a.slug} URL RESOLVED (q4 register)", f"{best[:250]} → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
