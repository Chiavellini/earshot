# RUNBOOK — 2026-08-04 afternoon (SPCX/ZETA 20:30Z, AMD/TOST 21:00Z)

Written 2026-08-04 ~19:40Z. Everything below is copy-pasteable. Local = CST = UTC−6.

**Read this first if you are picking up cold:** this morning SPOT and PGR were
*recorded* instead of *armed* — 335,900 contracts traded, zero orders
(`docs/history/POSTMORTEM-26AUG04.md`). The whole point of this afternoon is that a
recorder is never pointed at a tradeable call again. `bridge-probe` and
`chain-capture` bank corpus and cannot trade. **Only `launch-event.sh` (phone)
and `arm-on-url.sh` / `launch-webcast.sh` (webcast) place orders.**

---

## Timeline

| Local | UTC | Do this |
|---|---|---|
| 14:20 | 20:20 | arm SPCX + ZETA (webcast, laptop) |
| **14:30** | **20:30** | SPCX / ZETA calls start |
| 14:40 | 20:40 | **re-probe the AMD bridge** (decides the AMD path) |
| 14:45 | 20:45 | arm AMD (box, phone) + TOST (webcast, laptop) |
| **15:00** | **21:00** | AMD / TOST calls start |
| ~17:00 | 23:00 | reconcile |

---

## Posture (all four events)

1¢ mention-probe, non-crossing, `confirmed_only`. **No fill is structurally
possible.** `--fillable` was explicitly declined for today. Each event has its
own `state/<event-id>/` and therefore its own $1 ledger.

---

## AMD — the phone lane

### What we know

The bridge arrived by analyst registration:

```
+1-877-407-8037   (int'l +1-201-689-8037)   passcode 13755754#
```

A T-98m admission probe (`corpus/bridge-probes/amd_2026_08_04.{wav,txt}`) held
only **55.6 s** and transcribed:

> "Thank you for calling InCom Conferencing. The next available conference
> specialist will be with you momentarily." (looping, then hung up on our silence)

So **the passcode does not self-admit** — it lands in a queue for a human.
`[AMD] admission = "operator_screened"` is recorded in `configs/dial-ins.toml`.
Because the probe ran 98 minutes early, "conference not open yet" is **not**
excluded — hence the T-20 re-probe below.

`configs/webcast-amd-26aug04-arm-wc.toml` is the AMD config. **Despite the
`webcast-` filename it is the phone lane**: leg 0 is `twilio_stream`, legs 1–2
are the webcast fanout siblings. The filename is kept because
`configs/.readiness/webcast-amd-26aug04-arm-wc.json` and
`configs/.acks/webcast-amd-26aug04-arm-wc-short-tokens.ack` are keyed to that
basename; renaming orphans both and forces a fresh `say`-based readiness study.

### Step 1 — re-probe at ~14:40 (T-20)

```bash
cd ~/Code/ear
scripts/bridge-probe.sh --to +18774078037 --dtmf "13755754#" \
  --label amd-2026-08-04-t20 --secs 120 --account ~/.earshot/ms.env \
  --promote --issuer AMD --yes
```

`--yes` is required because SPCX/ZETA will already be armed and `bridge-probe`
refuses (exit 4) while a `launch-event.sh` is live. `--secs 120` keeps it under
the 300 s open-board refusal — a short probe on event day is the sanctioned
workflow.

Then read `corpus/bridge-probes/amd_2026_08_04_t20.txt`:

- **Held ~120 s with conference/hold audio** → it auto-promoted to
  `admission = "held"`. Use **Step 2A**.
- **Still the InCom specialist queue / short hold** → it is Class B for real.
  Use **Step 2B**.

### Step 2A — bridge self-admits (no human needed)

```bash
ssh -i ~/.ssh/earshot-useast1.pem ubuntu@3.212.174.94
cd ~/ear && source ~/.cargo/env
export ORT_DYLIB_PATH=$(find ~/ear/vendor -name "libonnxruntime.so*" | head -1)
scripts/launch-event.sh \
  --config configs/webcast-amd-26aug04-arm-wc.toml \
  --account ~/.earshot/ms.env \
  --bind-port 8080 \
  --to +18774078037 --dtmf "13755754#" \
  --tls-host media.3-212-174-94.sslip.io \
  --bin state/KXEARNINGSMENTIONAMD-26AUG04-WEBCAST/earshot-pinned \
  --i-am-live --allow-mention
```

### Step 2B — operator ladder (bridge screens us; **your cell rings**)

Identical command **plus `--operator-handler`**:

```bash
scripts/launch-event.sh \
  --config configs/webcast-amd-26aug04-arm-wc.toml \
  --account ~/.earshot/ms.env \
  --bind-port 8080 \
  --to +18774078037 --dtmf "13755754#" \
  --tls-host media.3-212-174-94.sslip.io \
  --bin state/KXEARNINGSMENTIONAMD-26AUG04-WEBCAST/earshot-pinned \
  --operator-handler --i-am-live --allow-mention
```

What happens: it calls **+52 55 4543 6717**, puts you in an `earshot-` prefixed
conference, waits up to 60 s for you to join, and **only then** dials the
bridge. When the InCom specialist answers, say you are the registered Actinver
analyst (`bdelriom@actinver.com.mx`) for the AMD Q2 2026 call. After admission
the line goes listen-only and capture continues.

**Fail-closed:** if nobody joins the conference, the bridge is never dialed.
Answer the phone or AMD does not arm.

Config already carries:
```toml
[operator_handler]
enabled           = true
cell_number       = "+525545436717"
conference_prefix = "earshot"
```

### AMD webcast URL (optional, do not block on it)

AMD publishes only "via ir.amd.com" — no direct link until ~T-15. If you get an
m3u8, write it on the **box** (that is where AMD runs):

```bash
ssh ... 'echo "<m3u8-url>" > ~/amd-stream.url'
```

If it never resolves, the two pipe legs die and **the phone leg still carries the
event** — fanout is leg-resilient (`src/pipeline/mod.rs:304`). Do not delay the
arm waiting for a URL.

---

## SPCX / ZETA / TOST — webcast lanes (run on the LAPTOP)

The m3u8 must be resolved from the logged-in browser at ~T-15 and written to
`$HOME/<slug>-stream.url`. Registered/logged in already: SPCX, TOST.

- TOST attendee page: <https://events.q4inc.com/attendee/766087653> — states the
  webcast opens **15 minutes before** start.
- SPCX: <https://ir.spacex.com/updates/releases-details/2026/SpaceX-to-Post-Second-Quarter-2026-Results-and-Host-Webcast-on-August-4-2026-2026-g8layJlbFm/default.aspx>
- ZETA: <https://investors.zetaglobal.com/events-and-presentations/events/event-details/2026/Zeta-Q2-2026-Earnings-Call/default.aspx>

To capture the m3u8: open the player, DevTools → Network → filter `m3u8` → copy
the **master** playlist URL.

### ⚡ THE WATCHERS ARE ALREADY RUNNING — you only have to paste a URL

Three `arm-on-url.sh` watchers were started at 19:29Z and are polling for their
URL files right now. Each one will, on its own: detect the file, PCM-gate the
stream on this host, and **arm LIVE**. No further command is needed.

```bash
echo "<master-m3u8>" > ~/spcx-stream.url     # deadline 20:35Z, account aal.env
echo "<master-m3u8>" > ~/zeta-stream.url     # deadline 20:35Z, account intc.env
echo "<master-m3u8>" > ~/tost-stream.url     # deadline 21:05Z, account lmt.env
```

Watch them:

```bash
tail -f ~/spcx-armonurl-wrapper.log     # also ~/zeta-… ~/tost-…
```

They page via `notify.sh` and exit nonzero if the deadline passes with no URL,
or if the URL is reachable but yields no decodable PCM. There is no silent path
out — a missed event is acceptable, a deaf arm is not.

If a watcher died, restart it with the same line it was launched with, e.g.:

```bash
nohup scripts/arm-on-url.sh --slug spcx --kind hls \
  --config configs/webcast-spcx-26aug04-arm-wc.toml \
  --account $HOME/.earshot/aal.env --deadline-utc 20:35 --i-am-live \
  > ~/spcx-armonurl-wrapper.log 2>&1 &
```

`arm-on-url.sh` gates on `pcm-gate.sh` (actual decoded PCM), not HTTP 200 —
that is the check HOOD passed on 07-29 while capturing nothing.

**Account map (never reuse one across two live lanes):**
`ms.env` → AMD (box) · `aal.env` → SPCX · `intc.env` → ZETA · `lmt.env` → TOST

---

## Watching a live arm

```bash
# is it armed (not recording)?  Must show `arm` and `--state-dir`.
ps aux | grep -E "earshot (arm|bridge-probe)" | grep -v grep

# per-event
ls state/<event-id>/            # listener.ready → safe-to-dial; stream.connected → media landed
tail -f state/<event-id>/report.json
```

Kill an event with the STOP file, **never** by killing the pid:

```bash
touch state/<event-id>/STOP
```

Event ids: `KXEARNINGSMENTIONAMD-26AUG04-WEBCAST`,
`KXEARNINGSMENTION{SPCX,ZETA,TOST}-26AUG04-WEBCAST`.

**Red flag:** a `capture bake-off: max duration reached` line means a *recorder*
is running, not an arm. That is this morning's failure. Stop it.

---

## After the calls — the standing habit

```bash
# AMD ran on the box, so pull its state down FIRST or you get a false MISSED-ARM
rsync -av -e "ssh -i ~/.ssh/earshot-useast1.pem" \
  ubuntu@3.212.174.94:~/ear/state/KXEARNINGSMENTION\* state/

python3 scripts/missed-arm-check.py --date 2026-08-04
```

Exit 0 = clean. Exit 1 = MISSED-ARM. `com.earshot.missed-arm` also fires at
17:30 local, but on box event days that firing is expected-incomplete — the
post-rsync manual run is authoritative.

---

## Known state / caveats

- **Preflight has one accepted FAIL on AMD**: `market-phrase-screen.py` rejects
  MI45 because armed variants are spoken forms (`mi four fifty`) and the market
  string is `MI450 / MI455X`. The live rules resolve on what is **said**
  ("VIDEO ... primarily used"), so the spoken forms are correct and the textual
  screen is a false positive. Full reasoning is in the config next to the MI45
  keyword. Everything else on the box preflight passes.
- **Fixed today**: `preflight-event.sh:372` used BSD `stat -f %m`, which on GNU
  means `--file-system` and *succeeds* with the wrong output — preflight died on
  the box with `File: unbound variable`, skipping every later gate. Now
  OS-detected.
- **Do not run `cargo test`** while a tunnel is up — `tests/launch_event_posture.rs`
  spawns real ngrok agents and contends for the free-tier slot.
- The uncommitted arm-path work (`launch-event.sh`, `twilio-dial.sh`,
  `redial-supervisor.sh`, `src/`) is deliberately kept: `cargo check --tests`
  clean, 290 tests green, and it *is* the guardrails this morning lacked. One
  sharp edge: `twilio-dial.sh` exits `1` for "HTTP 2xx but no sid" and
  `launch-event.sh` retries on `1` — if the AMD dial retries, check the Twilio
  console for a second leg.
- `com.earshot.pgr-chain` (the recorder launchd job from this morning) is
  **unloaded and archived** to `~/Library/LaunchAgents/disabled-26aug04/`.
- Nothing is committed. Commit after reconciliation.
