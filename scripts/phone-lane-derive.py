#!/usr/bin/env python3
"""phone-lane-derive.py — turn a staged webcast arm into a PHONE-ONLY arm.

Why this exists (2026-08-05, TLN). Talen published a participant dial-in, and
where one exists the webcast leg only adds load and false comfort: on 26JUL29
CMG the phone was 4.1-8.2 s AHEAD of the webcast on the SAME call, with one
extra recall win. The phone config that ran that night was produced by hand
from `configs/webcast-tln-26aug05-arm-wc.toml` at T-60 on the day of the event.
This script is that derivation, done at prep time and checked.

WHAT IT PRESERVES, AND WHY IT MATTERS. The `[[keywords]]` section is copied
BYTE-FOR-BYTE except for the three numbers that are posture, not meaning
(`max_price_cents`, `budget_contracts`, `min_confidence_confirmed`).
`scripts/keywords-extract.py` hashes only ticker/phrase/variants, so the derived
config keeps the twin's `keywords_sha256` — which is what lets it reuse the TTS
readiness study and its dead-word ack instead of re-running a laptop-only
`say`-based study on event morning. Verified: the 26AUG05 TLN pair share
`bcf1fd60…`.

WHAT IT REFUSES.
  * a source config with no `[[keywords]]` (nothing to arm)
  * a bind port `deploy/caddy/Caddyfile` does not route — the box's TLS ingress
    answers `respond 404` for everything else, so Twilio's handshake would never
    reach the listener. TLN's hand-made config bound 8093, which is NOT routed;
    that would have killed the box run independently of the stale binary.
  * caps that cannot trade: `max_price_cents > MENTION_FILL_CAP_CENTS` (read
    from src/risk.rs, never remembered) is refused by `mention_fill_gate`, and
    `max_price_cents * budget_contracts > [risk].max_order_notional_cents`
    yields `Refusal::OrderNotional` on EVERY order.

  scripts/phone-lane-derive.py --from configs/webcast-<x>-arm-wc.toml
      [--bind-port N] [--cap-cents auto|N] [--budget N]
      [--min-confidence-confirmed F] [--out <path>] [--event-env <path>]
      [--account ~/.earshot/ms.env] [--stream-host media.<domain>]
      [--dry-run] [--check]

Exit 0 wrote (or would write); 1 refused; 2 could not run.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import sys
import tomllib

REPO = os.environ.get("EARSHOT_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CADDYFILE = "deploy/caddy/Caddyfile"
RISK_RS = "src/risk.rs"
# bridge-probe.sh binds 8099 by default; never hand it to an event.
RESERVED_PORTS = {8099}


def die(msg, code=2):
    print(f"PHONE-DERIVE-ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def refuse(msg):
    print(f"REFUSED: {msg}", file=sys.stderr)
    sys.exit(1)


# ── facts read from code, never from memory ────────────────────────────────
def mention_fill_cap_cents(path=RISK_RS):
    m = re.search(r"^pub const MENTION_FILL_CAP_CENTS: u64 = (\d+);", open(path).read(), re.M)
    if not m:
        die(f"cannot read MENTION_FILL_CAP_CENTS from {path} — the cap contract is unreadable")
    return int(m.group(1))


def routed_ports(path=CADDYFILE):
    """Ports the TLS ingress actually reverse-proxies. Everything else 404s."""
    try:
        text = open(path).read()
    except OSError as e:
        die(f"cannot read {path}: {e}")
    return sorted({int(p) for p in re.findall(r"/e/(\d{4})\b", text)})


def ports_claimed_on(datepart, event_date, configs_dir="configs"):
    """Bind ports claimed by OTHER events on the SAME DAY.

    Scoped to the date on purpose. Two events collide on a port only if they run
    at once; a config from last month claiming 8080 is not a conflict, and
    treating it as one exhausts the routed range immediately. `*-raw.toml` is
    `earshot prepare` output kept as an unpatched audit copy — it is never
    launched, so it claims nothing.
    """
    claimed = {}
    for name in sorted(os.listdir(configs_dir)):
        p = os.path.join(configs_dir, name)
        if name.endswith("-raw.toml"):
            continue
        if name.endswith(".toml"):
            try:
                text = open(p, errors="replace").read()
            except OSError:
                continue
            if 'kind = "twilio_stream"' not in text and "twilio_stream" not in text:
                continue
            if f"-{datepart}" not in text:  # matches the [event].id date part
                continue
            m = re.search(r'^\s*bind\s*=\s*"[^"]*:(\d+)"', text, re.M)
            if m:
                claimed[int(m.group(1))] = name
        elif name.endswith("-autorun.env"):
            try:
                text = open(p, errors="replace").read()
            except OSError:
                continue
            if f'EVENT_DATE="{event_date}"' not in text:
                continue
            cp = re.search(r'^CONFIG_PATH="([^"]+)"', text, re.M)
            bp = re.search(r"^BIND_PORT=(\d+)", text, re.M)
            if not (cp and bp):
                continue
            # A webcast env's BIND_PORT is vestigial (launch-webcast.sh takes no
            # port); only a phone lane actually binds one.
            try:
                if "twilio_stream" in open(cp.group(1), errors="replace").read():
                    claimed[int(bp.group(1))] = name
            except OSError:
                continue
    return claimed


# ── verbatim section surgery (no tomllib round-trip: comments are evidence) ──
def split_at_keywords(text):
    """Head = everything before the keyword section; tail = the section itself.

    The split point is the top of the COMMENT BLOCK preceding the first
    `[[keywords]]`, not the marker itself: `prepare` writes each market's
    provenance ("Afford / Affordable … corpus-UNVALIDATED … review variants") on
    the lines above its block, and cutting at the marker silently drops the first
    market's provenance while keeping every other one.
    """
    lines = text.splitlines(keepends=True)
    first = next((i for i, l in enumerate(lines) if l.startswith("[[keywords]]")), None)
    if first is None:
        return text, ""
    start = first
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    return "".join(lines[:start]), "".join(lines[start:])


def take_sections(head, *names):
    """Extract `[name]` and every `[name.sub]` verbatim, in source order."""
    out, keep, cur = [], False, None
    for line in head.splitlines(keepends=True):
        m = re.match(r"^\[([^\[\]]+)\]\s*$", line)
        if m:
            cur = m.group(1)
            keep = any(cur == n or cur.startswith(n + ".") for n in names)
        elif line.startswith("[["):
            keep = False
        if keep:
            out.append(line)
    return "".join(out).rstrip("\n")


def set_kv(block, key, value):
    """Replace `key = <v>` in a keyword block, preserving the column alignment."""
    pat = re.compile(rf"^(\s*{re.escape(key)}\s*)=(\s*)\S.*$", re.M)
    if pat.search(block):
        return pat.sub(lambda m: f"{m.group(1)}={m.group(2)}{value}", block)
    return None


def rewrite_keyword_posture(tail, cap, budget, min_conf):
    """Rewrite ONLY the three posture numbers; phrase/variants stay byte-identical."""
    out, changed = [], 0
    for chunk in re.split(r"(?m)^(?=\[\[keywords\]\])", tail):
        if not chunk.startswith("[[keywords]]"):
            out.append(chunk)
            continue
        changed += 1
        for key, val in (("max_price_cents", cap), ("budget_contracts", budget)):
            new = set_kv(chunk, key, val)
            if new is None:
                die(f"a [[keywords]] block has no `{key}` — refusing to guess a posture")
            chunk = new
        if min_conf is not None:
            new = set_kv(chunk, "min_confidence_confirmed", min_conf)
            if new is None:
                # absent: insert after `policy`, where prepare/slate-stage put it
                new, n_sub = re.subn(
                    r"(?m)^(\s*policy\s*=\s*\"[^\"]+\")[ \t]*$",
                    rf"\1\nmin_confidence_confirmed = {min_conf}",
                    chunk,
                    count=1,
                )
                if not n_sub:
                    die("a [[keywords]] block has neither min_confidence_confirmed nor policy — "
                        "cannot place the threshold without guessing")
            chunk = new
        out.append(chunk)
    return "".join(out), changed


# ── the derived config ─────────────────────────────────────────────────────
HEADER = """\
# PHONE-ONLY arm — derived from {src} by scripts/phone-lane-derive.py on {date}.
#
# Where a participant dial-in exists, the phone lane IS the lane: 26JUL29 CMG
# measured the phone 4.1-8.2 s AHEAD of the webcast on the SAME call, with one
# extra recall win. The webcast legs are therefore REMOVED, not kept as a
# backstop — a second leg costs CPU and buys false comfort.
#
# THE CONSEQUENCE: there is NO webcast fallback for this event. The phone leg is
# the only capture path, so "refuse when unsure" is the WRONG default here —
# declining to arm is zero coverage, which is worse than arming on partial
# evidence. Where the evidence supports it, arm under an explicit
# EARSHOT_GREEN_OVERRIDE="<reason>", which PAGES (scripts/launch-event.sh).
#
# Keywords are copied BYTE-FOR-BYTE from the twin except the posture numbers, so
# keywords_sha256 is unchanged and configs/.readiness/{base}.json plus its
# dead-word ack carry over (scripts/keywords-extract.py hashes ticker/phrase/
# variants only, never caps or thresholds).
#
# Execution host: the us-east-1 box. Ingress wss://{stream_host}/e/{port}
# (Caddy terminates TLS; /e/<port> must be routed in deploy/caddy/Caddyfile).
# Verify the host BEFORE event day:  scripts/box-ready.sh --config {out}
{posture_note}
"""

SOURCE_BLOCK = """\
# NO [arbiter] section — deliberate. `fanout` requires >= 2 sources
# (src/config.rs:704) and this arm has exactly one, so the default single-source
# arbiter is correct. Adding [arbiter] here makes the config fail validation.

[[sources]]
# THE phone leg. max_reconnects/idle_timeout_secs are the Q-REDIAL production
# values; they require EARSHOT_REDIAL_DIR, which scripts/launch-event.sh exports
# (src/config.rs:588 refuses to arm otherwise). Without them a single mid-call
# drop ends the run, and preflight FAILs the config unless launched
# --one-shot-ok. Number + PIN are passed on the launcher command line and stay
# OUT of git.
kind = "twilio_stream"
bind = "127.0.0.1:{port}"
max_reconnects = 2
idle_timeout_secs = 8"""


def build_config(args, src_text, cap, budget, min_conf, port, stream_host, out_path):
    head, tail = split_at_keywords(src_text)
    if not tail.strip():
        refuse(f"{args.src} has no [[keywords]] — nothing to arm (capture-only configs are not a phone lane)")

    event = take_sections(head, "event")
    pipeline = take_sections(head, "pipeline")
    detector = take_sections(head, "detector")
    kalshi = take_sections(head, "kalshi")
    risk = take_sections(head, "risk")
    if not event:
        refuse(f"{args.src} has no [event] section")
    if not kalshi:
        refuse(f"{args.src} has no [kalshi] section")
    if not risk:
        refuse(f"{args.src} has no [risk] section — an arm with no allowlist cannot go live")

    tail, n_kw = rewrite_keyword_posture(tail, cap, budget, min_conf)

    posture = (
        f"#\n# Posture: MENTION-FILL — max_price_cents = {cap} (ceiling "
        f"MENTION_FILL_CAP_CENTS = {mention_fill_cap_cents()}), budget_contracts = {budget}.\n"
        f"# Orders MAY cross and fill. Launch needs --i-am-live --allow-mention --fillable\n"
        f"# (src/main.rs:1062-1067 refuses --fillable without both)."
        if cap > 1
        else "#\n# Posture: MENTION-PROBE — 1c non-crossing. This CANNOT fill by construction;\n"
        "# grade it on orders[].send_ts != null, never on a report's order count."
    )
    if min_conf is not None:
        posture += (
            f"\n# min_confidence_confirmed = {min_conf} — PROVENANCE: a single in-sample day\n"
            f"# (26AUG05 tape, n=26). NOT validated out of sample. Treat as a working\n"
            f"# default that has never been tested against a day it did not come from."
        )

    parts = [
        HEADER.format(
            src=args.src,
            date=datetime.date.today().isoformat(),
            base=os.path.basename(out_path)[:-5],
            stream_host=stream_host,
            port=port,
            out=out_path,
            posture_note=posture,
        ),
        "",
        event,
        "",
    ]
    if pipeline:
        parts += [pipeline, ""]
    if detector:
        parts += [detector, ""]
    parts += [SOURCE_BLOCK.format(port=port), "", kalshi, "", risk, "", tail.rstrip("\n"), ""]
    return "\n".join(parts), n_kw


# ── the autorun env (tracked; must never contain digits) ───────────────────
ENV_TMPL = """\
# Per-event autorun parameters — {name} ({event_ticker}). PHONE LANE.
# Written by scripts/phone-lane-derive.py on {date}. Sourced by
# scripts/event-autorun.sh via EARSHOT_EVENT_ENV (systemd user timer on the box,
# installed by scripts/box-event-install.sh). ALL HHMM TIMES ARE BOX-LOCAL = UTC.
#
# THIS FILE IS TRACKED IN GIT. Digits are read at source-time from the gitignored
# store, so preflight's dial-in gate can `source` this file and be satisfied
# without a PIN ever entering a commit.
#
# The store path is resolved, not hardcoded: the laptop repo is $HOME/Code/ear
# and the BOX repo is $HOME/ear, and a hardcoded laptop path silently yields an
# empty TO_NUMBER on the box (which event-autorun then reports as "event env
# incomplete" minutes before the call).
EVENT_NAME="{name}"
EVENT_TICKER="{event_ticker}"          # informational
CONFIG_PATH="{config}"
EARSHOT_NUMBERS="${{EARSHOT_NUMBERS:-$(for d in "${{EARSHOT_REPO:-}}" "$HOME/ear" "$HOME/Code/ear"; do [ -f "$d/state/hunt/numbers.env" ] && echo "$d/state/hunt/numbers.env" && break; done)}}"
TO_NUMBER="$(sed -n 's/^{key}=\\([^,]*\\).*/\\1/p' "$EARSHOT_NUMBERS" 2>/dev/null | head -1)"
PASSCODE="$(sed -n 's/^{key}=[^,]*,\\(.*\\)/\\1/p' "$EARSHOT_NUMBERS" 2>/dev/null | head -1)"
# Empty is NOT a safe default: preflight's dial-in gate only FAILs on a literal
# TBD, so an unresolved lookup would sail through as "resolved" with no number.
TO_NUMBER="${{TO_NUMBER:-TBD}}"
PASSCODE="${{PASSCODE:-TBD}}"
LABEL="com.earshot.{slug}"
EVENT_DATE="{event_date}"
END_HHMM_LIVE={end_hhmm}             # call + 150 min UTC
COMPLETE_EARLIEST_HHMM={complete_hhmm}     # call + 45 min UTC
ACCOUNT_ENV="{account}"
BIND_PORT={port}
FIRE_HHMM={fire_hhmm}                # timer fire = call - 25 min, zero-padded HHMM UTC
# Tunnel-free production ingress: launch-event.sh takes this verbatim and skips
# ngrok entirely (Q-NGROK/Q-TLS: p99 226 ms -> 20 ms, 61 stalls -> 0).
EARSHOT_STREAM_URL="wss://{stream_host}/e/{port}"
# The bridge ANSWERS while unprovisioned and then hangs up, so a too-early dial
# looks SUCCESSFUL to Twilio and dies (TLN 26AUG05: "the conference is not yet
# available", hung up at 13.1 s). Extra attempts plus the source's
# max_reconnects are what carry across the provisioning moment; the wider
# confirm window stops a legitimate redial being called a deaf call.
DIAL_ATTEMPTS=5
STREAM_CONFIRM_SECS=150
TICKERS="{tickers}"
"""


def hhmm(dt):
    return dt.strftime("%H%M")


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--from", dest="src")
    ap.add_argument("--bind-port", type=int)
    ap.add_argument("--cap-cents", default="auto")
    ap.add_argument("--budget", type=int)
    ap.add_argument("--min-confidence-confirmed", dest="min_conf", default="0.22")
    ap.add_argument("--out")
    ap.add_argument("--event-env")
    ap.add_argument("--account", default="$HOME/.earshot/ms.env")
    ap.add_argument("--stream-host", default=os.environ.get("EARSHOT_TLS_HOST", "media.3-212-174-94.sslip.io"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args()
    if args.help:
        print(__doc__.strip())
        return 0
    if args.check:
        return self_test()
    if not args.src:
        die("need --from <config.toml>  (see --help)")

    os.chdir(REPO)
    if not os.path.isfile(args.src):
        die(f"source config not found: {args.src}")
    src_text = open(args.src).read()
    try:
        src_cfg = tomllib.loads(src_text)
    except Exception as e:
        die(f"source config does not parse: {e}")

    event_id = src_cfg.get("event", {}).get("id", "")
    starts_at = src_cfg.get("event", {}).get("starts_at", "")
    m = re.match(r"KXEARNINGSMENTION([A-Z0-9]+)-(\d{2}[A-Z]{3}\d{2})", event_id)
    if not m:
        refuse(f"cannot derive a venue key from [event].id {event_id!r} — expected KXEARNINGSMENTION<KEY>-<DATE>")
    key, datepart = m.group(1), m.group(2)
    slug = f"{key.lower()}-{datepart.lower()}-phone"

    # Parsed here, not later: the fire time, the end window, the Q-ADMIT probe
    # window AND the same-day port-collision scope all derive from it, so an
    # unparseable timestamp must stop the run before anything is chosen.
    call = None
    if starts_at:
        try:
            call = datetime.datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
            if call.tzinfo is None:
                call = call.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            pass
    if call is None:
        refuse(
            f"[event].starts_at {starts_at!r} is unparseable — fire time, end window and the "
            f"T-90..T-30 admission window all derive from it"
        )
    call_date_for_ports = call.date().isoformat()

    # ── bind port ──────────────────────────────────────────────────────────
    routed = routed_ports()
    if not routed:
        die(f"{CADDYFILE} declares no /e/<port> routes — the TLS ingress cannot carry any event")
    out_path = args.out or f"configs/{key.lower()}-{datepart.lower()}-phone-arm.toml"
    claimed = ports_claimed_on(datepart, call_date_for_ports)
    claimed.pop(os.path.basename(out_path), None)  # a file does not collide with itself
    claimed = {p: n for p, n in claimed.items() if n != os.path.basename(out_path)}

    if args.bind_port:
        port = args.bind_port
    elif os.path.isfile(out_path) and (
        pm := re.search(r'^\s*bind\s*=\s*"[^"]*:(\d+)"', open(out_path, errors="replace").read(), re.M)
    ) and int(pm.group(1)) in routed:
        # Idempotence: regenerating an existing config keeps its port, so a re-run
        # never moves the ingress path out from under an installed timer or an
        # already-passed preflight (whose config_sha256 would then be stale anyway).
        port = int(pm.group(1))
        print(f"  (keeping bind port {port} already in {out_path})")
    else:
        free = [p for p in routed if p not in claimed and p not in RESERVED_PORTS]
        if not free:
            refuse(
                f"every routed port is claimed by another event on {call_date_for_ports} "
                f"(routed {routed}, claimed {sorted(claimed)}) — free one or widen {CADDYFILE}"
            )
        port = free[0]

    if port not in routed:
        refuse(
            f"bind port {port} is NOT routed by {CADDYFILE} (routed: {routed}). "
            f"Twilio's handshake would hit Caddy's `respond 404` catch-all and no media would ever arrive. "
            f"Pick a routed port, or add the route and re-run scripts/install-tls-ingress.sh on the box."
        )
    if port in claimed:
        refuse(f"bind port {port} is already claimed on {call_date_for_ports} by {claimed[port]} — two concurrent events cannot share one listener")

    # ── caps, derived from the code constants ──────────────────────────────
    ceiling = mention_fill_cap_cents()
    cap = ceiling if args.cap_cents == "auto" else int(args.cap_cents)
    if not 1 <= cap <= 99:
        refuse(f"max_price_cents must be 1..99 (config.rs validates a u8 in that range), got {cap}")
    if cap > ceiling:
        refuse(
            f"cap {cap}c exceeds MENTION_FILL_CAP_CENTS = {ceiling}c — mention_fill_gate "
            f"(src/kalshi/validator.rs:163) refuses the run before a single order is computed. "
            f"Raise the constant deliberately, not per-run."
        )
    order_notional = src_cfg.get("risk", {}).get("max_order_notional_cents", 100)
    if args.budget:
        budget = args.budget
    else:
        budget = max(1, order_notional // cap)
    if cap * budget > order_notional:
        refuse(
            f"{cap}c x {budget} = {cap*budget}c exceeds [risk].max_order_notional_cents = {order_notional}c — "
            f"every order would return Refusal::OrderNotional (src/risk.rs check_order). "
            f"Use --budget {max(1, order_notional // cap)} or raise the config's notional cap."
        )
    min_conf = None if args.min_conf in ("", "none") else args.min_conf

    text, n_kw = build_config(args, src_text, cap, budget, min_conf, port, args.stream_host, out_path)

    # ── the autorun env ────────────────────────────────────────────────────
    env_path = args.event_env or f"configs/{key.lower()}-{datepart.lower()}-phone-autorun.env"
    tickers = " ".join(sorted({k["market_ticker"].rsplit("-", 1)[-1] for k in src_cfg.get("keywords", [])}))
    env_text = ENV_TMPL.format(
        name=key,
        event_ticker=f"KXEARNINGSMENTION{key}-{datepart}",
        date=datetime.date.today().isoformat(),
        config=out_path,
        key=key,
        slug=slug,
        event_date=call.date().isoformat(),
        end_hhmm=hhmm(call + datetime.timedelta(minutes=150)),
        complete_hhmm=hhmm(call + datetime.timedelta(minutes=45)),
        account=args.account,
        port=port,
        fire_hhmm=hhmm(call - datetime.timedelta(minutes=25)),
        stream_host=args.stream_host,
        tickers=tickers,
    )

    # ── readiness + ack carry-over (same keywords_sha256 ⇒ same study) ─────
    src_base = os.path.basename(args.src)[:-5]
    out_base = os.path.basename(out_path)[:-5]
    carried = []
    ready_src = f"configs/.readiness/{src_base}.json"
    ready_dst = f"configs/.readiness/{out_base}.json"
    ack_src = f"configs/.acks/{src_base}-dead-word.ack"
    ack_dst = f"configs/.acks/{out_base}-dead-word.ack"

    print(f"venue {key} {datepart}   call {call.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print(f"  config     {out_path}   ({n_kw} keyword blocks, posture {cap}c x{budget})")
    print(f"  event env  {env_path}   (FIRE_HHMM {hhmm(call - datetime.timedelta(minutes=25))}Z)")
    print(f"  ingress    wss://{args.stream_host}/e/{port}   (routed: {routed})")
    print(f"  probe window (Q-ADMIT, T-90..T-30): "
          f"{(call - datetime.timedelta(minutes=90)).strftime('%H:%MZ')}.."
          f"{(call - datetime.timedelta(minutes=30)).strftime('%H:%MZ')} on {(call - datetime.timedelta(minutes=90)).date()}")

    if args.dry_run:
        print("\n--dry-run: nothing written. Config would be:\n")
        sys.stdout.write(text)
        return 0

    os.makedirs("configs/.readiness", exist_ok=True)
    os.makedirs("configs/.acks", exist_ok=True)
    with open(out_path, "w") as f:
        f.write(text)
    with open(env_path, "w") as f:
        f.write(env_text)

    # assert-ready.sh and preflight look up these artifacts by the config's
    # BASENAME under configs/.readiness and configs/.acks. A config written
    # outside configs/ (a scratch path, a test) can never be found there, so
    # carrying artifacts across for it just litters the real directories with
    # entries named after a temp file — which is exactly what a first test run
    # of this script did (configs/.readiness/x.json).
    if os.path.dirname(os.path.abspath(out_path)) != os.path.abspath("configs"):
        print(f"  (out is outside configs/ — readiness/ack carry-over skipped; "
              f"assert-ready.sh would not look for them under this basename)")
        ready_src = ack_src = os.devnull

    if os.path.isfile(ready_src) and not os.path.exists(ready_dst):
        art = json.load(open(ready_src))
        art["config"] = out_base  # the hand-made TLN artifact still named its twin
        art["derived_from"] = src_base
        json.dump(art, open(ready_dst, "w"), indent=1)
        carried.append(ready_dst)
    if os.path.isfile(ack_src) and not os.path.exists(ack_dst):
        shutil.copyfile(ack_src, ack_dst)
        carried.append(ack_dst)

    # Prove the carry-over is legitimate rather than asserting it.
    import subprocess

    def kwsha(p):
        r = subprocess.run([sys.executable, "scripts/keywords-extract.py", p, "--sha256"],
                           capture_output=True, text=True)
        return r.stdout.strip()

    a, b = kwsha(args.src), kwsha(out_path)
    if a and a == b:
        print(f"  keywords_sha256 UNCHANGED ({a[:16]}…) — the twin's TTS study still describes these words"
              + (f"; {len(carried)} artifact(s) carried over" if carried else ""))
    else:
        print(f"  WARNING: keywords_sha256 CHANGED ({a[:16]}… -> {b[:16]}…) — "
              f"re-run scripts/variant-study.sh {out_path}; the carried artifacts will NOT satisfy assert-ready.sh")
    for c in carried:
        print(f"  carried    {c}")

    print("\nnext:")
    print(f"  scripts/box-ready.sh --config {out_path} --account {args.account}")
    print(f"  scripts/admission-window-probe.sh --config {out_path}          # plan only")
    print(f"  scripts/preflight-event.sh --config {out_path} --account {args.account} --event-env {env_path}")
    print(f"  scripts/box-event-install.sh --event-env {env_path}")
    return 0


def self_test():
    """Offline checks of the parts that silently produce a wrong config."""
    src = (
        '[event]\nid = "KXEARNINGSMENTIONXYZ-26AUG05"\nstarts_at = "2026-08-05T20:30:00Z"\n\n'
        '[detector]\nengine = "streaming_ctc"\n\n[detector.streaming_ctc]\nmodel_dir = "m"\n\n'
        '[arbiter]\nmode = "fanout"\n\n'
        '[[sources]]\nkind = "hls"\nurl = "x"\n\n'
        '[kalshi]\nseries = "KXEARNINGSMENTIONXYZ"\n\n'
        '[risk]\nmax_order_notional_cents = 100\nallowlist_tickers = [\n  "A",\n]\n\n'
        '# a comment that must survive\n[[keywords]]\nmarket_ticker    = "KXEARNINGSMENTIONXYZ-26AUG05-A"\n'
        'phrase           = "alpha"\nvariants         = ["alpha", "alphas"]\n'
        'max_price_cents  = 1\nbudget_contracts = 3\npolicy           = "confirmed_only"\n'
    )
    head, tail = split_at_keywords(src)
    assert "[[keywords]]" not in head, "keyword section leaked into the head"
    assert "a comment that must survive" in tail, "the provenance comment was dropped"
    det = take_sections(head, "detector")
    assert "[detector.streaming_ctc]" in det and "model_dir" in det, "detector subsection lost"
    assert "[arbiter]" not in det, "take_sections over-collected"
    risk = take_sections(head, "risk")
    assert '"A",' in risk and "]" in risk, "multi-line allowlist truncated"
    new, n = rewrite_keyword_posture(tail, 97, 1, "0.22")
    assert n == 1
    assert "max_price_cents  = 97" in new, "cap rewrite lost the column alignment"
    assert "budget_contracts = 1" in new
    assert "min_confidence_confirmed = 0.22" in new, "threshold not inserted"
    assert 'variants         = ["alpha", "alphas"]' in new, "variants must be byte-identical"
    assert 'phrase           = "alpha"' in new
    # the hash inputs (ticker/phrase/variants) are untouched ⇒ study carries over
    for probe in ("market_ticker", "phrase", "variants"):
        assert new.count(probe) == tail.count(probe)
    assert 8099 in RESERVED_PORTS
    print("self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
