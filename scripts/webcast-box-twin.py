#!/usr/bin/env python3
"""webcast-box-twin.py — derive the BOX (primary, money) twin of a staged webcast arm.

Why this exists (2026-08-19). Operator ruling: "priority is capturing in the box
with orders sent from the box; if the box cannot ingest, capture and work from the
laptop — one or the other sends actual trades, never both". A webcast RunPlan is
compiled for ONE host (`execution_host_id` is inside `plan_sha256`) and eventctl
refuses two active runs for one event id, so the box leg needs its own event id
and its own config + env. On 2026-08-19 this twin was produced BY HAND at 05:20Z
for TGT/TOL (id suffix, verifier threads/budget, env label/port, readiness clone)
— the kind of night-before hand edit this program exists to remove. This script
is that derivation, done at prep time, checked, and re-runnable.

WHAT IT PRESERVES. Everything in the source config byte-for-byte except:
  * `[event].id`              <ID>-WEBCAST  ->  <ID>-BOX-WEBCAST
  * `[verifier].num_threads`  -> 2, `[verifier].budget_ms` -> 500 (the box decodes
    the 3.5 s tail p95 < 400 ms on 4 vCPU; the laptop budget is its own business)
  * a header comment naming the ruling and the twin
The `[[keywords]]` blocks are untouched, so `keywords_sha256` is identical and the
laptop's TTS readiness study + dead-word ack are cloned under the twin's basename
(assert-ready/preflight look them up by config basename).

WHAT IT WRITES.
  configs/webcast-<x>-box-arm-wc.toml
  configs/<x>-box-autorun.env       (EVENT_NAME += _BOX, CONFIG_PATH, LABEL
                                     com.earshot.<x>-box, BIND_PORT = src+10 unless
                                     taken by another env — then the next free)
  configs/.readiness/webcast-<x>-box-arm-wc.json   (clone, if the source study exists)
  configs/.acks/webcast-<x>-box-arm-wc-dead-word.ack (clone, if it exists)

WHAT IT REFUSES.
  * a source whose `[event].id` does not end in -WEBCAST
  * an existing box config whose bytes differ from what would be written now
    (a published RunPlan may pin it — supersede on purpose, never by accident);
    pass --force to overwrite
  * --init without a resolvable account fingerprint

  scripts/webcast-box-twin.py --from configs/webcast-<x>-arm-wc.toml
      [--host-id earshot-useast1] [--account '$HOME/.earshot/ms.env']
      [--init] [--force] [--dry-run] [--check]

--init runs `eventctl.py init --host-id <box>` on the twin (needs --account to
resolve the fingerprint through scripts/execution-profile.sh) and prints the run dir.
Exit 0 wrote (or would write); 1 refused; 2 could not run.
"""
import argparse
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys

REPO = os.environ.get("EARSHOT_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def die(msg, code=1):
    print(f"BOX-TWIN-ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def derive_config(text, src_base, host_id, now):
    m = re.search(r'^id\s*=\s*"([^"]+)"\s*$', text, re.M)
    if not m:
        die("source config has no [event] id line")
    src_id = m.group(1)
    if not src_id.endswith("-WEBCAST"):
        die(f"source [event].id {src_id!r} does not end in -WEBCAST — not a webcast arm")
    box_id = src_id[: -len("-WEBCAST")] + "-BOX-WEBCAST"
    out = text.replace(m.group(0), m.group(0).replace(src_id, box_id), 1)
    # verifier budget on the box: 2 threads / 500 ms (only when a [verifier] block exists)
    vm = re.search(r'^\[verifier\]\s*$', out, re.M)
    if vm:
        head, tail = out[: vm.end()], out[vm.end():]
        nxt = re.search(r'^\[', tail, re.M)
        block, rest = (tail[: nxt.start()], tail[nxt.start():]) if nxt else (tail, "")
        block = re.sub(r'^(num_threads\s*=\s*)\d+', r'\g<1>2', block, count=1, flags=re.M)
        block = re.sub(r'^(budget_ms\s*=\s*)\d+', r'\g<1>500', block, count=1, flags=re.M)
        out = head + block + rest
    header = (
        "# ═══════════════════════════════════════════════════════════════════════════\n"
        f"# BOX LEG (PRIMARY, MONEY) — derived {now} by scripts/webcast-box-twin.py from\n"
        f"# configs/{src_base}.toml on the operator ruling (2026-08-19): \"priority is capturing\n"
        "# in the box with orders sent from the box; if the box cannot ingest, capture and work\n"
        "# from the laptop — one or the other sends actual trades, never both\".\n"
        f"#   {box_id:<44} box (execution_host {host_id}), full posture  <- THIS, MONEY\n"
        f"#   {src_id:<44} laptop twin, same posture, armed ONLY by scripts/laptop-failover.sh\n"
        "#                      when the box run has not reached capture_ready by start+grace.\n"
        "# Distinct event id because execution_host_id is inside plan_sha256 and eventctl refuses\n"
        "# two active runs for one event id. keywords_sha256 identical -> the study carries over.\n"
        "# Verifier on the box: 2 threads / 500 ms (decode p95 < 400 ms measured 2026-08-18).\n"
        "# ═══════════════════════════════════════════════════════════════════════════\n"
    )
    return header + out, src_id, box_id


def taken_ports(exclude):
    ports = set()
    for p in glob.glob(os.path.join(REPO, "configs", "*.env")):
        if os.path.abspath(p) == os.path.abspath(exclude):
            continue
        for line in open(p, errors="replace"):
            mm = re.match(r'\s*BIND_PORT=(\d+)', line)
            if mm:
                ports.add(int(mm.group(1)))
    return ports


def derive_env(text, x, box_cfg_rel, env_out_path):
    src_port = re.search(r'^BIND_PORT=(\d+)', text, re.M)
    if not src_port:
        die("source env has no BIND_PORT")
    port = int(src_port.group(1)) + 10
    used = taken_ports(env_out_path)
    while port in used or port == 8099:
        port += 1
    out = text
    out = re.sub(r'^(EVENT_NAME=")([^"]+)(")', lambda m: m.group(1) + m.group(2) + "_BOX" + m.group(3), out, count=1, flags=re.M)
    out = re.sub(r'^(CONFIG_PATH=")[^"]+(")', lambda m: m.group(1) + box_cfg_rel + m.group(2), out, count=1, flags=re.M)
    out = re.sub(r'^(WEBCAST_CONFIG_PATH=")[^"]+(")', lambda m: m.group(1) + box_cfg_rel + m.group(2), out, count=1, flags=re.M)
    out = re.sub(r'^(LABEL=")[^"]+(")', lambda m: m.group(1) + f"com.earshot.{x}-box" + m.group(2), out, count=1, flags=re.M)
    out = re.sub(r'^BIND_PORT=\d+', f"BIND_PORT={port}", out, count=1, flags=re.M)
    if "-box" not in out.splitlines()[0]:
        out = f"# BOX twin of {x}-autorun.env — written by scripts/webcast-box-twin.py; port {port} = src+10 (first free)\n" + out
    return out, port


def kwsha(p):
    r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "keywords-extract.py"), p, "--sha256"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def self_check():
    src = ('[event]\nid        = "KXEARNINGSMENTIONX-26AUG20-WEBCAST"\nstarts_at = "2026-08-20T11:30:00Z"\n'
           '[verifier]\nmode = "gate"\nnum_threads = 4\nbudget_ms   = 1500\n[risk]\nmax = 1\n')
    out, sid, bid = derive_config(src, "webcast-x-26aug20-arm-wc", "earshot-useast1", "now")
    assert bid == "KXEARNINGSMENTIONX-26AUG20-BOX-WEBCAST", bid
    assert 'id        = "KXEARNINGSMENTIONX-26AUG20-BOX-WEBCAST"' in out
    assert "num_threads = 2\n" in out and "budget_ms   = 500\n" in out and "max = 1" in out
    env = 'EVENT_NAME="X"\nCONFIG_PATH="configs/webcast-x-26aug20-arm-wc.toml"\nWEBCAST_CONFIG_PATH="configs/webcast-x-26aug20-arm-wc.toml"   # informational\nLABEL="com.earshot.x-26aug20-autorun"\nBIND_PORT=8088\n'
    e, port = derive_env(env, "x-26aug20", "configs/webcast-x-26aug20-box-arm-wc.toml", "/nonexistent.env")
    assert 'EVENT_NAME="X_BOX"' in e and 'LABEL="com.earshot.x-26aug20-box"' in e and port >= 8098, (e, port)
    assert 'CONFIG_PATH="configs/webcast-x-26aug20-box-arm-wc.toml"' in e
    # refuses a non-webcast id
    try:
        derive_config('id = "KXEARNINGSMENTIONX-26AUG20"\n', "b", "h", "n"); raise SystemExit("should have refused")
    except SystemExit as ex:
        assert ex.code == 1
    print("webcast-box-twin: self-test PASS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src")
    ap.add_argument("--host-id", default="earshot-useast1")
    ap.add_argument("--account", default="$HOME/.earshot/ms.env", help="portable locator for the fingerprint (only with --init)")
    ap.add_argument("--init", action="store_true", help="compile the box RunPlan with eventctl init --host-id")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        self_check(); return 0
    if not a.src:
        ap.error("--from is required")
    os.chdir(REPO)
    src = a.src
    src_base = os.path.basename(src)[:-5]
    m = re.match(r'webcast-(.+)-arm-wc$', src_base)
    if not m:
        die(f"source basename {src_base!r} is not webcast-<x>-arm-wc.toml")
    x = m.group(1)
    env_src = f"configs/{x}-autorun.env"
    if not os.path.isfile(env_src):
        die(f"{env_src} missing — stage the laptop twin first (opsctl stage)")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    cfg_out = f"configs/webcast-{x}-box-arm-wc.toml"
    env_out = f"configs/{x}-box-autorun.env"
    text, src_id, box_id = derive_config(open(src).read(), src_base, a.host_id, now)
    env_text, port = derive_env(open(env_src).read(), x, cfg_out, env_out)

    if os.path.exists(cfg_out) and not a.force:
        old = open(cfg_out).read()
        # compare ignoring the derivation timestamp in the header
        strip = lambda t: re.sub(r'derived \S+ by', 'derived <ts> by', t)
        if strip(old) != strip(text):
            die(f"{cfg_out} exists with DIFFERENT bytes — a RunPlan may pin it. Supersede on purpose "
                f"(close the box attempt) then re-run with --force")
        print(f"{cfg_out}: unchanged")
    print(f"box twin  {box_id}  (host {a.host_id})")
    print(f"  config   {cfg_out}")
    print(f"  env      {env_out}  (BIND_PORT {port}, LABEL com.earshot.{x}-box)")
    if a.dry_run:
        print("--dry-run: nothing written"); return 0
    open(cfg_out, "w").write(text)
    open(env_out, "w").write(env_text)
    carried = []
    for s_, d_ in ((f"configs/.readiness/{src_base}.json", f"configs/.readiness/webcast-{x}-box-arm-wc.json"),
                   (f"configs/.acks/{src_base}-dead-word.ack", f"configs/.acks/webcast-{x}-box-arm-wc-dead-word.ack")):
        if os.path.isfile(s_):
            if s_.endswith(".json"):
                art = json.load(open(s_)); art["config"] = f"webcast-{x}-box-arm-wc"; art["derived_from"] = src_base
                json.dump(art, open(d_, "w"), indent=1)
            else:
                shutil.copyfile(s_, d_)
            carried.append(d_)
    ka, kb = kwsha(src), kwsha(cfg_out)
    if ka and ka == kb:
        print(f"  keywords_sha256 UNCHANGED ({ka[:16]}…) — study carries over; {len(carried)} artifact(s) cloned")
    else:
        die(f"keywords_sha256 CHANGED ({ka[:16]}… -> {kb[:16]}…) — derivation bug, refusing")
    if not a.init:
        print(f"  next: scripts/webcast-box-twin.py --from {src} --init   (or eventctl.py init --config {cfg_out} --event-env {env_out} --host-id {a.host_id} --account-fingerprint <fp>)")
        return 0
    r = subprocess.run(["bash", "scripts/execution-profile.sh", "resolve", "--account", a.account,
                        "--config", cfg_out, "--bin", "target/release/earshot"], capture_output=True, text=True)
    fp = ""
    try:
        fp = json.loads(r.stdout.strip().splitlines()[-1]).get("account_fingerprint", "") if r.stdout.strip() else ""
    except Exception:
        fp = ""
    if not re.match(r'^sha256:[0-9a-f]{64}$', fp or ""):
        die(f"could not resolve account fingerprint via execution-profile.sh resolve: {r.stdout[-300:]} {r.stderr[-300:]}", 2)
    cmd = [sys.executable, "scripts/eventctl.py", "init", "--config", cfg_out, "--event-env", env_out,
           "--host-id", a.host_id, "--account-fingerprint", fp]
    print("  init:", " ".join(cmd))
    r2 = subprocess.run(cmd, text=True)
    return r2.returncode


if __name__ == "__main__":
    sys.exit(main())
