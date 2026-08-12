# ARMING PLAN — 2026-07-30 (all times UTC)

Five events. Configs generated 2026-07-30 from `earshot prepare` against the
live Kalshi API; keyword readiness studied per config (artifacts in
`configs/.readiness/`, the blocking preflight gate reads them). Risk caps are
prepare's probe defaults everywhere: 1¢, non-crossing, budget 1.

Dial-in digits are NOT in this file or anywhere in the repo: they are staged in
the gitignored `configs/26jul30-numbers.env` (keys `YUM_TO`/`YUM_DTMF`/
`AAPL_TO`/`AAPL_DTMF` — in gitignored env staging, see research report).
Source it before the phone launch commands below.

Bind-port plan (MULTI-EVENT §8): YUM phone **8080**, AAPL phone **8081**. The
two phone calls do not overlap in time, but ports stay distinct anyway.
Account split: YUM phone on `~/.earshot/ms.env`, AAPL phone on
`~/.earshot/intc.env` — the two phone events never share a Twilio account.

| # | Event | Call (UTC) | Lanes | Configs | Bind | Account env |
|---|-------|-----------|-------|---------|------|-------------|
| 1 | YUM  | 12:15 | PHONE (operator-screened, Class B) + webcast fallback | `configs/twilio-yum-26jul30-arm.toml` / `configs/webcast-yum-26jul30-arm-wc.toml` | 8080 | phone: ms.env; webcast: intc.env |
| 2 | RACE | 13:30 | webcast only (phone is registration-gated, not available) | `configs/webcast-race-26jul30-arm-wc.toml` | — | ms.env |
| 3 | RDDT | 20:30 | webcast only | `configs/webcast-rddt-26jul30-arm-wc.toml` | — | ms.env |
| 4 | AAPL | 21:00 | PHONE (published dial-in; admission probe pending event-window) + webcast fallback | `configs/twilio-aapl-26jul30-arm.toml` / `configs/webcast-aapl-26jul30-arm-wc.toml` | 8081 | phone: intc.env; webcast: ms.env |
| 5 | AMZN | 21:00 | webcast only (positively no attendee phone leg) | `configs/webcast-amzn-26jul30-arm-wc.toml` | — | intc.env |

Phone twins carry the Q-REDIAL production values pinned 2026-07-29 night:
`max_reconnects = 2`, `idle_timeout_secs = 8`. Phone `[event].id` is
unsuffixed; each webcast twin runs as `<EVENT>-WEBCAST` so run-arm guard 8
sees two events (PHONE-LANE-HANDOFF.md §7.3).

## Launch commands

YUM phone (operator handler IS the admission path — Class B, probed 07-29
night; `[operator_handler]` enabled in the config, pager cell +52 55 4543 6717):

    source configs/26jul30-numbers.env
    scripts/launch-event.sh --config configs/twilio-yum-26jul30-arm.toml \
      --account ~/.earshot/ms.env --bind-port 8080 \
      --to "$YUM_TO" --dtmf "$YUM_DTMF" \
      --tls-host media.3-212-174-94.sslip.io \
      --operator-handler

AAPL phone:

    source configs/26jul30-numbers.env
    scripts/launch-event.sh --config configs/twilio-aapl-26jul30-arm.toml \
      --account ~/.earshot/intc.env --bind-port 8081 \
      --to "$AAPL_TO" --dtmf "$AAPL_DTMF" \
      --tls-host media.3-212-174-94.sslip.io

Webcast lanes (per event; write the morning-read HLS URL to the slug file
first — the leg cmds `cat` it):

    echo "<HLS-URL>" > ~/yum-stream.url    # likewise race/rddt/aapl/amzn
    scripts/launch-webcast.sh --config configs/webcast-<slug>-26jul30-arm-wc.toml \
      --account <env per table>

## Webcast URLs

- AMZN: https://events.q4inc.com/attendee/915225528 (known now).
- RDDT: **OPEN — morning browser-read, deadline 20:00Z.**
- RACE: **OPEN — morning browser-read, deadline 12:30Z.**
- YUM webcast fallback: **OPEN — morning browser-read, deadline 11:45Z.**
- AAPL webcast fallback: **OPEN — browser-read, deadline 20:30Z.**

## Open items (deadlines)

1. RACE webcast URL by **12:30Z**; RDDT by **20:00Z** (browser-reads; IR sites
   geo-block the box — read from the laptop, then stage the `.url` file).
2. AAPL bridge admission probe is **pending the event window**: run
   `scripts/bridge-probe.sh --to "$AAPL_TO" --label aapl-26jul30 --dtmf "$AAPL_DTMF"`
   when the bridge opens (target ≤ 20:40Z) — preflight's Q-ADMIT gate needs a
   fresh `held` in `configs/dial-ins.toml`.
3. `scripts/preflight-event.sh` per event before its window (pins binary,
   live-probes creds/mailer/disk; readiness artifacts already written).
4. Evening overlap 20:30–23:00Z: up to 4 concurrent arms (RDDT wc, AAPL
   phone+wc, AMZN wc) over 2 Kalshi accounts = 2 arms/account — same density
   as the 07-29 slate. Escalate only if capacity is exceeded (run-all-events
   standing policy).

## Keyword readiness (gate state at plan time)

Every config was studied with `scripts/variant-study.sh` (real detector,
streaming_ctc), dead words autopsied with `dead-word-autopsy.sh` /
`detect-explain`, and only measured fixes applied (respelling variants whose
pieces the model demonstrably emits; per-keyword `confirmed_threshold` at
0.7 × measured peak for clean NEAR-MISSes). Artifacts in `configs/.readiness/`.

**One word per venue is DEAD-ON-TTS and stays that way** (openai — the model
never emits ▁OPEN on any of 13 measured respellings; chatgpt also dead):

- `KXEARNINGSMENTIONRDDT-26JUL30-OPEN`
- `KXEARNINGSMENTIONAAPL-26JUL30-OPEN`
- `KXEARNINGSMENTIONAMZN-26JUL30-OPEN`

⚠ **Consequence: the RDDT, AAPL (both lanes), and AMZN configs will FAIL the
blocking readiness gate in `preflight-event.sh`** on that word. The words stay
in the configs per policy (dropping a market is a trading decision, not the
prep agent's). OPERATOR DECISION before the windows: either remove the OPEN
market from config+allowlist (accepting no trade there), or arm without a
preflight PASS on that gate. YUM and RACE pass clean.

## WNT (`KXWORLDNEWSMENTION-26JUL30`, 22:30Z) — MEASURE ONLY, NOT ARMED

Not one of the five events above and **no orders**, by operator decision
2026-07-30. It is listed here so a reader does not mistake its absence for an
oversight — it is a daily market and it *was* worked on last night.

Why it is not armed: the market resolves on the **linear ABC television
network** newscast, and we have no feed of it. Last night's capture recorded
ABC News Live, a different channel that does not carry WNT at 22:30Z
(`VERIFICATIONS.md` Q-WNT-SRC) — every accuracy and race number from it is
void. There is no US hardware available for an antenna, and every purchasable
live-TV route runs 30–90 s behind broadcast while the book is swept in **under
one second** (Q-WNT-CLOSE). Buying a subscription cannot fix that.

What runs instead, on free archived data, no live capture:

    scripts/wnt-vod-fetch.sh --settled       # real broadcast audio, 16 sessions
    scripts/wnt-pack-lag.py --json state/wnt-pack-lag.json

**RESULT (2026-07-30, `VERIFICATIONS.md` Q-WNT-PACK): the lane is CLOSED.**
Measured offset **−6.31 s median** over 6 independently resolvable sessions
(range −10.83 … −2.16 s, slopes 0.986–1.014). The pre-registered threshold was
< 5 s closes the lane / > 20 s reopens it; a pack on a 30–90 s streaming feed
would have landed at +30…+90 s. It did not. **Do not buy a live-TV
subscription for this market** — combined with the sub-second book sweep
(Q-WNT-CLOSE), a delayed feed does not arrive late, it arrives after the trade
no longer exists.

What survives needs no speed: the count strikes (`-TRUM` 5+, `-IRAN` 3+) and
pre-broadcast pricing. The tape is emphatic on the latter — **26 of 47
strike-notes across the 16 sessions are `pre-priced`** (already ≥85¢ before
air). This board is largely decided by the news day, not by the newscast, which
makes it a text/prediction problem rather than an audio-latency one.

`configs/launchd/com.earshot.wnt-daily.plist` stays **uninstalled**. Scheduling
it as written would bank the wrong programme every day.
