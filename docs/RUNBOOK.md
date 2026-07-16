# NEXT EVENT — Monday 2026-07-20, Domino's (DPZ) Q2 call — PHONE-FIRST (number hunt)

> **2026-07-16, revised twice.** (1) UAL and every remaining July mention
> event (DPZ 07-20, GEV 07-22, TSLA 07-22, INTC 07-23) publish NO public
> dial-in — announcements are webcast-only (UAL/TSLA confirmed in their own
> 8-K exhibits). (2) BUT the operator corrected the read: a participant phone
> bridge EXISTS for most of these — the analyst Q&A runs by phone. Transcript
> evidence: DPZ + INTC operators say "press star-11" (real PSTN bridge); GEV
> is a moderated bridge (analysts pre-cleared); TSLA alone has NO phone leg
> (webcast-platform Q&A, "raise hand"/"unmute"). Phone is preferred — the
> webcast runs ~18-30 s behind, and whoever holds the bridge number beats
> every webcast listener. The webcast (HLS) adapter design is COMPLETE but
> DEFERRED (operator decision 2026-07-16: phone-only this week; TSLA is
> therefore untradeable this week).
>
> **DPZ number hunt (Track A) — the event is GO only if one lands by 07-19:**
> 1. IR ask: investorrelations@dominos.com (VP IR Greg Lemenchick) — operator
>    sends from their own account.
> 2. Legacy operator numbers from the 2015 8-K: **888-400-9978 /
>    +1-706-634-4947, "ask for the Domino's Pizza conference call"** — probe
>    via a user-approved Twilio bridge call (operator live on the leg), 07-17.
> 3. Call-morning: the live webcast player often shows a "listen by phone"
>    tab. ⚠ ir.dominos.com (and ir.united.com) GEO-BLOCK this box (Akamai,
>    Mexico) — needs a US VPN or the operator's manual check.
>
> - **Call**: Monday 2026-07-20, 8:30 AM ET = 6:30 AM CDMX local. Fire 06:05,
>   wake 06:00, END_HHMM 0740, COMPLETE_EARLIEST 0710.
> - **Markets**: `KXEARNINGSMENTIONDPZ-26JUL20` — 14 tradeable (prepared +
>   compile-keywords + dry-run test-arm 14/14 PASSED 2026-07-16; DoorDash
>   "5+ times" auto-skipped). Config: `configs/twilio-live-dpz.toml`
>   (tiered fillable: TARI only, cap 35¢). Env: `configs/dpz-autorun.env`
>   (TO_NUMBER/PASSCODE TBD — the supervisor refuses a live fire while TBD).
> - **Supervisor**: `scripts/event-autorun.sh` (generalized 2026-07-16,
>   follow-up 3 closed) driven by a per-event env file named in the launchd
>   plist via `EARSHOT_EVENT_ENV`; launchd-fired rehearsal PASSED 2026-07-16
>   08:53 (env sourced, TTS dial, 7124 frames, 3 detections, rc=0).

## Fills — precise status (do not over- or under-claim)

The system HAS filled one real order: the **M2 acceptance fill, 2026-07-10**
(1¢ × 1 IOC on a non-mention market, 231 ms round-trip, fee $0.0007 — wire
evidence pinned in `VERIFICATIONS.md` M2 live-drill pins). What has NEVER
happened is a **detection-triggered mention fill**: the full hear-keyword →
cross-ask → fill loop. The MS run sent 1 order (canceled unfilled, IOC) and
lost the race on 4. Ledger `spent_cents` values prove sends, not fills (caps
are debited on send, never refunded — `src/risk.rs::record_send`). The next
live event with the fillable posture is that validation.

# LIVE RUN — Wednesday 2026-07-15, Morgan Stanley Q2 call (COMPLETED)

> The JPM section below is retained as the original template; the JPM call
> (2026-07-14) passed without a live run — its markets are finalized. The MS
> run below was fully drilled the night of 2026-07-14 (all drills PASSED:
> Twilio hold probe, kill-drill 140 ms, mention probes on TARI/RECE, dry-run
> arm 14/14 markets, loopback rehearsal with 3/3 keyword detections, and an
> end-to-end `launch-event.sh` dress rehearsal against the real MS bridge).

Posture: **mention-fill, tiered** (`--i-am-live --allow-mention --fillable`) —
recession (cap 26¢) and tariff (cap 45¢; live ask was 38¢ on 07-14 evening,
the generated 14¢ cap could never fill) may cross the ask and FILL; the other
12 words observe at 1¢ non-crossing. Worst case 83¢, backstopped by the 100¢
event notional cap and the per-event $1 ledger (fresh:
`state/KXEARNINGSMENTIONMS-26JUL15/probe_ledger.json` = 0¢).

- **Call**: Wednesday 2026-07-15 **8:30 AM ET = 6:30 AM CDMX local**
  (results ~7:30 AM ET = 5:30 AM local).
- **Dial-in** (primary source morganstanley.com IR, verified 2026-07-14):
  **+1-844-886-2200** (intl +1-646-307-1096), passcode **400700#**.
- **Markets**: `KXEARNINGSMENTIONMS-26JUL15` — 14 active (verified via API
  2026-07-14); rules stored `state/KXEARNINGSMENTIONMS-26JUL15/rules/`.
- **Account env**: `~/.earshot/ms.env` (Twilio + Kalshi combined, created
  2026-07-14, mode 600).

## ⚡ AUTONOMOUS MODE — ARMED (operator asleep is OK)

`launchd` job `com.earshot.ms-autorun` fires **06:05 local** on 2026-07-15 and
runs `scripts/ms-autorun.sh` unattended: launch → dial → auto-redial on the
bridge's ~60 s pre-conference disconnect (DTMF wait variants cycle per
attempt) → redial window closes 07:40 → strike-close table fetched → SUMMARY
email to MAIL_TO. LAUNCHED and SUMMARY/FAILED emails bracket the run. All
output: `state/KXEARNINGSMENTIONMS-26JUL15/autorun.log` (+ summary + strike-close.json).
Verified the night before: mailer live-tested, full dry-run with a REAL
auto-redial against the bridge, and a launchd-fired dry-run (runs=1, exit 0).
- Abort before 06:05: `launchctl bootout gui/$(id -u)/com.earshot.ms-autorun`
- Abort mid-run: `touch state/KXEARNINGSMENTIONMS-26JUL15/STOP` (cancels
  orders + stops redials), then `scripts/twilio-hangup.sh <CallSid in log>`
- Requires: AC power + lid open (box never sleeps — resident caffeinates).
- Phase-0 note: the **webcast leg is skipped** (needs a human); the
  strike-close table (step 2) and warm-path report.json pin (step 4) are
  captured automatically.
- The job self-disarms (bootout) after the live run; a date guard in the
  script is the backstop. Self-disarm is the script's **final act** — booting
  out its own label SIGTERMs the script, so nothing may run after it. Before
  the 2026-07-15 fix this ran before `finished=1`, so a live run sent a
  spurious "FAILED (script died)" email right after the SUMMARY email (the
  2026-07-15 MS run itself completed fine).

### Hardening added 2026-07-15 (post-run audit — applies to EVERY future unattended event)

**DRESS-REHEARSED 2026-07-15 (production mechanisms, per the CLAUDE.md
convention).** Three launchd-fired runs against `configs/rehearsal-nflx.toml`
(active NFLX mention markets, dry-run, real Twilio calls to the dedicated
auto-answering TTS number **+1 762 721 2978** — voice webhook plays the
keyword script; bought 2026-07-15, ~$1.15/mo, reusable for every preflight).
Evidence in `state/KXEARNINGSMENTIONNFLX-26JUL02/autorun.log`:
- launchd fire + pinned binary (sha in `pinned.info`, clean commit) armed in
  <1 s — no fire-time compile;
- real 8 kHz μ-law stream, live detections (gaming/hollywood/acquisition),
  executor consulted the live book (centi-contract quantities visible);
- **SIGTERM mid-live-stream**: kill switch ran orders-cancel + disarm +
  ledger flush in 6 ms, exit 143, supervisor alerted + redialed;
- **STOP mid-attempt**: arm cancel/flush in 152 ms AND the supervisor watcher
  hung up the call leg and stopped the loop;
- **frames evidence**: two long zero-frame attempts were refused as "call
  complete" (no-audio alert + redial); the real 180 s / 7126-frame stream
  was accepted;
- **TERM→KILL escalation fired for real**: a post-STOP arm ignored SIGTERM
  (orphaned handler — follow-up 5 below) and was SIGKILLed by cleanup;
- rehearsal also caught `prepare` emitting a phrase the detector cannot
  BPE-encode ("warner bros." — the period): arm REFUSES the whole config.
  **Preflight/operator must test-arm any newly generated config** until
  prepare sanitizes phrases (follow-up 6).

- **Preflight is REQUIRED the night before**: `scripts/preflight-event.sh
  --config <cfg> --account <env> --fire-hhmm <HHMM>`. It builds and PINS the
  binary into the event state dir (`earshot-pinned` + `pinned.info` with
  commit/sha256), live-probes Twilio + Kalshi auth, checks disk ≥ 5 GiB,
  tools, timezone, a USER-scheduled `pmset` wake before fire time, and
  live-tests the mailer. The supervisor runs the pin — never a fire-time
  `cargo run` (a dirty tree / cold build / full disk at 06:05 is unrecoverable
  with nobody watching; the disk hit 100% TWICE this week).
- **Mid-window alerting**: every early-exit guard (wrong date, already
  running, window already closed) emails; the first failed attempt and every
  5th consecutive failure email WITH the log tail; a long call leg with no
  audio frames emails and keeps redialing ("wall time ≠ audio" — a dead
  tunnel parked on `<Pause>` looks like a 90-min success otherwise).
- **STOP now works mid-call**: a watcher hangs up the recorded CallSid
  (`state/<event>/call.sid`) and SIGTERMs the launcher; arm's kill watcher
  (which now handles SIGTERM — the supervisor's actual kill signal) cancels
  session orders, flushes the ledger, writes a last-words report, and exits.
- **Cleanup kills recorded PIDs only** (`arm.pid`/`ngrok.pid`, TERM→5 s→KILL),
  with a logged port-8080 sweep as last resort.
- **Test mode places no real call** by default (`--no-dial`);
  `EARSHOT_AUTORUN_TEST_DIAL=1` restores the full dial rehearsal.

Deferred follow-ups from the same audit (tracked, deliberately NOT done yet):
1. Mid-call stream recovery: a 30 s audio stall or socket close ends the whole
   run (`twilio_ws.rs` idle/EndOfStream); recovery is a full shell redial that
   re-pays ~30 s of warmup. Consider in-process re-accept within a call window.
2. Warm-path order latency: the one real send this run showed `send→hdrs`
   102 ms and `presign_age` 1.17 s — connection/presign reuse is a LOST_RACE
   lever; belongs to the low-latency detector milestone.
3. ~~Event windows hard-coded per event in `ms-autorun.sh`~~ DONE 2026-07-16:
   renamed `scripts/event-autorun.sh`, all event-specific values (name, config,
   number, passcode, label, date guard, `END_HHMM_LIVE`,
   `COMPLETE_EARLIEST_HHMM`, account env, tickers) come from a per-event env
   file (`configs/<event>-autorun.env`) named via `EARSHOT_EVENT_ENV` in the
   plist. Missing/incomplete env → refuse + email. `prepare`-generating the
   env file remains the follow-on.
4. ~~Test-mode rehearsals share the LIVE event's state dir~~ DONE 2026-07-15:
   `EARSHOT_AUTORUN_TEST_CONFIG` gives rehearsals their own event id/state dir.
5. After a STOP-triggered kill, the still-running disarmed arm is
   SIGTERM-immune (the kill watcher task returned; the installed handler's
   stream is orphaned, so TERM is swallowed). The supervisor's TERM→KILL
   escalation covers it (fired for real in the 2026-07-15 rehearsal), but the
   watcher should either keep draining signals or exit the process after a
   STOP kill.
6. ~~`prepare` can emit detector-unencodable phrases~~ DONE 2026-07-16:
   `prepare` now sanitizes variants into the detector alphabet AND
   encode-validates each against the real vocab, dropping (with comments) what
   cannot encode; `preflight-event.sh` additionally runs `compile-keywords`
   against the pinned binary. NOTE the second failure class found while
   fixing: the vocab contains `-`/`'` pieces, so "non-stop" ENCODES to tokens
   speech never produces — a silently dead entry that encode-checking alone
   passes; sanitization is what prevents it (see `docs/layers/prepare.md`).
7. ~~`notify()` sends are single-shot~~ DONE 2026-07-16: one retry with a
   30 s backoff (immediate retry would re-hit the send-rate throttle);
   still best-effort/never-fatal. Force-fail verified (both attempts logged).

## Morning checklist (manual fallback) — ⚠ call is **6:30 AM local (CDMX, UTC-6)** = 8:30 ET

This box runs America/Mexico_City (UTC-6, no DST). Operator arrives 6:20 →
**10-minute window**. ONE command does everything (cleanup → sanity → launch
→ dial); it was dress-rehearsed end-to-end the night before:

```
cd ~/Code/ear && scripts/ms-morning.sh        # at 6:20:00 sharp
```

Timeline: ~6:20 launch → arm validates 14 markets + books (~40 s) → tunnel +
dial → "stream started" by ~6:22 → bridge hold until the 6:30 call start.
The bridge IVR disconnects silent callers after ~60 s pre-conference
(measured); if that happens pre-6:25, the script's dial can be re-run —
`scripts/twilio-dial.sh` line is printed on exit. Do NOT dial before ~6:15.

Second device: public MS webcast open before 6:30 — the Phase-0 resolver-race
measurement below is REQUIRED this run (wall-clock notes only; no recording).

Kill (either leg, any time):
- `touch state/KXEARNINGSMENTIONMS-26JUL15/STOP`  → orders: cancel + disarm
- `scripts/twilio-hangup.sh <CallSid printed at dial>` → audio leg
Report after the call: `state/KXEARNINGSMENTIONMS-26JUL15/report.json`.

If the DTMF passcode isn't accepted (bridge prompt timing), hang up and
re-dial manually with more leading waits: `--dtmf "wwww400700#"` (each w =
0.5 s). The dress rehearsal confirmed the bridge answers and streams audio;
passcode acceptance can only be proven when the conference is open.

### Keyword rehearsal results (TTS through real Twilio, night of 07-14)

All 14 words produced ≥1 Confirmed detection via the live detector settings
(fp32, chunk 80 ms). Detect latency 64–127 ms past capture. Caveats to
expect live:
- **tariff** (fillable, cap 45¢): fired on mid-sentence "tariff"/"tariffs";
  MISSED sentence-initial "Tariff…" after silence, twice. Matches the known
  onset weakness (corpus recall 0.90). A missed first mention may be caught
  on a later mention — budget stays armed until filled.
- **tailwind** (observe 1¢): 2/3 phrasings fired; weakest of the 14.
- **recession** (fillable, cap 26¢): fired cleanly on TTS; still has zero
  corpus positives — live recall unproven.
- middle east/iran, oil (2nd mention), tailwind (2nd) correctly suppressed
  by refractory/budget after the first hit.

## Phase-0 measurement — resolver race + warm path (REQUIRED this run)

The JPM template's "thesis measurement" (below) stays in force. This section
adds the **webcast leg**, which answers the single most important open
question (TELEPHONY-DETECTION-SPEC §10.3): do Kalshi resolvers act at
webcast speed (~10–30 s behind our PSTN feed → recall is the product) or at
PSTN speed (→ every 100 ms matters)? The answer steers the next milestone
(docs/ROADMAP.md, Phase 2 vs 2.5).

**No call-audio recording on any leg** — the standing policy (item 5 of the
thesis measurement) applies to the webcast too. Everything below is
wall-clock notes + data the system already writes.

1. **Webcast leg (second device, required).** Before the call, open the
   public MS webcast player and confirm audio. During the call, for each
   armed keyword you hear ON THE WEBCAST, note wall-clock to the second
   (paper or a text file with `date` pasted per line). The arm log's
   `decision intent` tracing lines carry our detection wall-clock for the
   same words on the PSTN leg.
   → **PSTN lead ≈ webcast-heard time − detection-log time** (our detector
   adds ~1–1.2 s after the PSTN utterance; subtract that mentally). First
   real measurement of the structural cushion (TWILIO.md pins it at an
   unverified ~10–30 s research estimate).
2. **Strike-close interval.** Post-call (already thesis step 3): for every
   strike that fired or should have, `GET /markets/{ticker}` →
   `close_time`. Tabulate per keyword: webcast-heard t, detection t, our
   ack t (report.json edges are monotonic; use the log line wall-clock),
   strike close t.
3. **Decision rule.** If strike-close consistently trails our ack by
   **> 5 s**, resolvers are webcast-speed → the recall workstream leads the
   next milestone. If closes land within ~1–2 s of our ack, the race is
   PSTN-speed → latency work leads. Pin the table + verdict in
   VERIFICATIONS.md as a dated entry.
4. **Warm-path pin.** Keep `report.json`. Post-call, read `send→hdrs`,
   `hdrs→ack`, `send→ack`, `presign_age` — this run is the first warm-path
   sample from an armed process (cold CLI probes were 128–134 ms send→ack;
   VERIFICATIONS open item 6). Pin the numbers and, if `RUST_LOG` included
   `earshot::kalshi::rest=debug`, the negotiated `http_version` to the
   orders origin.

---

# First live run — Tuesday 2026-07-14 (JPMorgan Q2 call)

Posture: **mention-probe** — 1¢ IOC × 1 contract per keyword, $1 cumulative
ledger, Mentions category permitted via `--allow-mention`. The goal is a
timing + executionability verification, not P&L. Monday 07-13 is the full
dress-rehearsal day (no Kalshi mention calls exist that day — verified
against the open-events API 2026-07-11).

## Call targets (verified 2026-07-11 from issuer press releases + Kalshi API)

All five banks publish **static dial-ins, no registration**:

| Rank | Call (ET) | Kalshi event | Dial-in | Passcode |
|---|---|---|---|---|
| PRIMARY | JPMorgan 8:30 AM | `KXEARNINGSMENTIONJPM-26JUL14` (18 mkts) | 888-324-3618 / +1-312-470-7119 | `1364784#` |
| backup | Goldman 9:30 AM | `KXEARNINGSMENTIONGS-26JUL14` (13) | 1-800-330-6730 | `7042022` |
| backup | Wells Fargo 10:00 AM | `KXEARNINGSMENTIONWFC-26JUL14` (16) | 1-888-673-9782 | `8320644#` |
| backup | Citi 11:00 AM | `KXEARNINGSMENTIONC-26JUL14` (14) | 1-805-309-2350 | `7142026#` |
| skip | BofA 8:30 AM (conflicts with JPM) | `KXEARNINGSMENTIONBAC-26JUL14` (16) | 1-877-200-4456 | id `79795` |

Re-verify the JPM number on jpmorganchase.com IR Monday (sourced from a
syndicated reprint). A same-morning relay (JPM→GS→WFC→C) is possible later;
first run is JPM only. `configs/twilio-live.toml` carries the JPM tickers
(TARI corpus-tuned, RECE corpus-FA-tuned, INFL engine defaults).

## Prerequisites (user actions, do Saturday — external latency)

1. **Twilio**: create account, UPGRADE from trial (trial injects a voice
   preamble and restricts destinations), buy a voice-capable number, enable
   US outbound dialing. Put creds in `~/.twilio/earshot_env`:
   `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`.
2. **ngrok**: install; ideally reserve a stable domain (free hostnames
   rotate per session).
3. **Fee schedule**: fetch https://kalshi.com/docs/kalshi-fee-schedule.pdf
   in a real browser (bot-blocked), diff vs VERIFICATIONS §Q5 Feb-2026
   formula. Blocks trusting the EV gate live.

## Sunday drills (in order, each blocks the next; ~$0.03 total worst case)

Check ledger headroom first: `cat state/probe_ledger.json` (cap 100¢).

```bash
source ~/.kalshi/earshot_env
cargo build --release

# 1. Kill switch still <1 s, unrelated orders untouched (non-mention ticker)
cargo run --release -- kill-drill --ticker <liquid KXBTCD strike> --auto

# 2. Ack shape unchanged (1¢ IOC, non-mention)
cargo run --release -- probe-order --ticker <same>

# 3. NEW: mention-market order acceptance at $0 (refuses if 1¢ would cross)
cargo run --release -- probe-order --ticker KXEARNINGSMENTIONJPM-26JUL14-TARI --allow-mention

# 4. Dry-run arm against the real JPM markets: validator stores rules
cargo run --release -- arm --config configs/twilio-live.toml --allow-mention
#    → READ state/rules/KXEARNINGSMENTIONJPM-*.json and fix variants in the
#      config if rules_primary differs from ["tariff","tariffs"] etc.
#      Exclude any "said N times" strike (one-shot machine can't trade it).

# 5. Twilio loopback rehearsal (docs/TWILIO.md §Loopback):
#    ngrok http 8080 → dial a <Play>-fixture TwiML Bin → 3 tone detections,
#    measured capture latency, raw JSONL captured; then repeat with sherpa
#    + spoken keywords; finally --i-am-live (probe, non-mention 1¢).
```

## Tuesday timeline (ET)

- **T−60 m (7:30)**: `cargo build --release`; NTP sane (arm enforces ±10 s);
  start ngrok, note host; `book --ticker KXEARNINGSMENTIONJPM-26JUL14-TARI
  --secs 30` (live book sanity, note asks); confirm config allowlist =
  exactly the armed tickers.
- **T−20 m (8:10)**: `rm -f state/STOP`; then
  `cargo run --release -- arm --config configs/twilio-live.toml --i-am-live --allow-mention`
  (validator → gate summary → WS snapshots → presigner → executor idle).
- **T−15 m**: `scripts/twilio-dial.sh --to +18883243618 --dtmf "1364784#"
  --stream-url wss://<ngrok-host>/media` — hold music should start flowing:
  watch frame logs, seq-gap warnings, drop counters.
- **Live from the start.** The 1¢ × 1 posture IS the safety margin, and
  mention words can land in the CFO's first sentence. Only if the feed looks
  unhealthy at T−5 (drops climbing, gaps, silence) restart the call leg or
  fall back to a shadow session.

> **Two events at once (relay/parallel calls):** this timeline is single-event.
> To run two simultaneous calls on two separate Kalshi + Twilio accounts, use
> `scripts/launch-event.sh` once per event (each with its own config, account
> env file, `--bind-port`, and `--state-dir state/<id>`) instead of the manual
> arm/dial above — see `docs/MULTI-EVENT.md` §8 for the full setup + checklist.

## Event-prep + tiered fill (auto-config, M3+)

Instead of hand-cloning a config, generate it from the Kalshi event:

```
earshot prepare --event-ticker KXEARNINGSMENTION<ISSUER>-<YYMMMDD> --fillable
  # → configs/twilio-live-<issuer>.toml. TIERED: only precision-vetted words
  #   (tariff, recession — the tuned_library) fill at the live ask; every other
  #   word is 1¢ observe (logs fires, spends nothing, can't starve the $1 ledger).
  # Review: set [event].starts_at, research the dial-in, sanity-check variants.
```

Live: `arm --config <cfg> --i-am-live --allow-mention --fillable`. Fillable
orders cross the ask up to `MENTION_FILL_CAP_CENTS` (50¢), bounded by the config
notional caps + the $1 probe ledger. `--fill-all` makes EVERY word fillable —
deliberate only (false-alarm + ledger-starvation risk on unvalidated words).

Post-call, promote unvalidated words WITHOUT recording (respects "do not record"
below): `scripts/validate-from-log.py --log <arm.log> --config <cfg> --transcript
<public-transcript>` → per-word precision/recall from the observe-mode fire log.
A word that fires cleanly (~0 false alarms) graduates into `prepare::tuned_library`
and auto-fills next call. The rigorous path (record → corpus v2 → `bench-detector`)
is the durable follow-on for the recall ceiling.

## Kill criteria — any one → `touch state/STOP` then `scripts/twilio-hangup.sh <CallSid>`

- an order fires on a word you did not hear;
- frame drops / Twilio seq gaps climbing, or `feed_age` refusals appear;
- any refusal you don't understand;
- ledger at/near cap.

## The thesis measurement (capture during + after the call)

1. Note wall-clock time when you HEAR each keyword (your ear, rough).
2. Detection log line timestamp (tracing) + `report.json` (all 11 edges).
3. After the call: fetch each strike's actual close/settle time via REST —
   `close_time`/settlement on `GET /markets/{ticker}` — Kalshi humans close
   the market on utterance. **our-ack-time vs market-close-time is the
   measured edge**; positive margin = the strategy works.
4. `cargo run --release -- orders` + fills reconciliation; pin any new wire
   facts (mention-market ack on IOC, close behavior) in VERIFICATIONS.md
   with the evidence; write the run log.
5. Do NOT record call audio anywhere.

## Post-run

Verdict on: capture health (drops/gaps), detection correctness (per ear),
order acceptance (ack shapes), timing edge (ack vs close). THEN the M4 caps
conversation, with data.
