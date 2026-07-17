# NEXT EVENT — Monday 2026-07-20, Domino's (DPZ) Q2 call — PHONE-FIRST (number hunt)

> **2026-07-16, revised three times.** Per-issuer access now lives in
> `configs/dial-ins.toml` + `docs/DIAL-INS.md` — **that registry is the source of
> truth; this blockquote is a summary.**
>
> (1) DPZ 07-20, GEV 07-22, TSLA 07-22, INTC 07-23 — and UAL — publish NO public
> dial-in; announcements are webcast-only. (2) BUT the operator corrected the read:
> a participant phone bridge EXISTS for most — the analyst Q&A runs by phone.
> Verified 2026-07-16 against primary sources + verbatim transcripts:
> **DPZ + INTC** operators say "press star 11 on your telephone" (real PSTN bridge)
> — CONFIRMED. **TSLA alone** has NO phone leg (no operator at all; IR runs
> "raise hand"/"unmute") — CONFIRMED.
>
> Two corrections to the previous revision:
> - **GEV is NOT "a moderated bridge (analysts pre-cleared)"** — that was asserted
>   without evidence and is **refuted**. GEV runs the *same* self-service queue as
>   DPZ/INTC ("Please press 11 on your telephone"). There is a coordinator, but no
>   pre-clearance. Do not model GEV as structurally different.
> - **⚠ Do NOT group UAL with TSLA.** The old line "UAL/TSLA confirmed webcast-only
>   in their own 8-K exhibits" is literally true and **dangerously misleading**.
>   UAL's 8-K says "a live, listen-only webcast" — phrasing identical in kind to
>   Tesla's — yet UAL's operator runs a live phone Q&A ("press star, then the number
>   one on your telephone keypad"). **8-K silence is not evidence of absence.**
>   Acting on that grouping forfeits a tradeable event. The tell that actually
>   separates them: Tesla's call has **no operator at all**.
>
> Also newly tracked: **GOOGL 07-22** and **AAL 07-23** have open mention events that
> this runbook previously omitted. GOOGL is the largest near-term name — bridge
> confirmed from Alphabet's own transcript ("press \*1 on your telephone"), number
> unpublished, so it needs the same Track A hunt as DPZ.
>
> Phone is preferred — whoever holds the bridge number beats every webcast listener.
> ⚠ The "~18-30 s" webcast lag previously quoted here is **unsourced** (`:246` and
> `docs/TWILIO.md:66` both call it an unverified estimate; the "18" appears nowhere
> else). **Quote no number until it is measured.**
>
> **How to actually measure it (and why NFLX cannot).** The lag is a *difference*
> between two paths, so it needs a call carrying BOTH a phone bridge and a webcast,
> captured simultaneously and cross-correlated. **NFLX is exactly the wrong subject:
> it has no phone leg, so there is no reference to subtract from** — the 2026-07-16
> capture (`corpus/nflx-q2-2026.wav`, 39:25, verified speech throughout) is corpus
> material only, NOT a latency datapoint. An earlier revision of this line claimed it
> would yield "the first real measurement"; that was wrong.
> Run the measurement on a **Tier-A published-number** issuer instead (MSFT, or a
> bank — `configs/dial-ins.toml`), where both paths are reachable at once.
>
> The webcast (HLS) adapter is **NOT built** (`src/main.rs`: `SourceConfig::Hls =>
> Err("HLS adapter not built")`), and no design artifact exists — a previous revision
> claiming the design was "COMPLETE but DEFERRED" was an overclaim. Operator decision
> 2026-07-16: phone-only this week. **TSLA and NFLX are therefore untradeable** — not
> deferred by choice, but with no capture path at all.
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

## Track A execution kit (prepared 2026-07-16)

### 1. IR email — ready to send (operator's own account, TODAY)

To: `investorrelations@dominos.com` · Subject: **Dial-in access for the Q2
2026 earnings call (Monday, July 20)**

> Dear Domino's Investor Relations,
>
> I plan to follow Monday's Q2 2026 earnings conference call (July 20,
> 8:30 a.m. ET) live. The announcement lists the webcast, but streaming is
> unreliable where I will be that morning — is a telephone dial-in available
> to listen to the call? Listen-only access is all I need.
>
> Thank you very much,
> Bernardo Del Rio

Notes: no shareholder claim is made (we hold none); listen-only is stated
up front. If IR replies with a registration link instead of digits, register
immediately — platform-issued dial-in + PIN counts as a number landing.

### 2. Legacy-number probe — Thursday 2026-07-17, US business hours (ET)

⚠ USER-APPROVED, OPERATOR LIVE ON THE LEG. Do not run unattended; a human
answers. Two-leg bridge so the call works from Mexico and presents the US
Twilio caller ID: Twilio calls YOUR phone first; when you answer it bridges
you to the legacy number.

```bash
source ~/.twilio/earshot_env          # or the per-event account env
YOUR_PHONE="+52..."                   # the phone you will answer
LEGACY="+18884009978"                 # attempt 1; attempt 2: +17066344947
curl -s -X POST "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Calls.json" \
  --data-urlencode "To=${YOUR_PHONE}" \
  --data-urlencode "From=${TWILIO_FROM_NUMBER}" \
  --data-urlencode "Twiml=<Response><Dial callerId=\"${TWILIO_FROM_NUMBER}\">${LEGACY}</Dial></Response>" \
  -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}"
```

Say: *"Hi — I'm calling about the Domino's Pizza conference call on Monday,
July 20. Could you give me the dial-in details to listen to that call?"*

**OUTCOME 2026-07-17: 888-400-9978 is DEAD** — reassigned to an unrelated
marketing line (medical devices), no conference operator. Probed once via the
user-attended bridge, per protocol. Remaining: the +1-706-634-4947 probe, the
IR ask (email + the IR office phone from a current PR), the VPN page check,
and the call-morning player check.

Record, whichever way it goes: (a) does the number still route to a
conferencing operator at all, and which provider (greeting usually names it);
(b) participant number + passcode if offered → `configs/dpz-autorun.env`;
(c) "operator-assisted, ask for the call by name" → set `TO_NUMBER` to the
legacy number, `PASSCODE=""`, and note that the autorun DTMF path is unused —
an operator-assisted join needs a human on the morning leg, which demotes
the event from autonomous to attended; (d) dead/reassigned → step 3 is the
last chance. Hang up politely either way; one probe per number, no retries
(these are staffed lines).

### 3. Manual checks only the operator can do (geo-blocked box)

- **07-17/18, US VPN**: ir.dominos.com → Events → the Q2 2026 call entry.
  Look for any "listen by phone" / "telephone access" / registration link
  that the SEC-visible announcement lacks.
- **07-20 ~06:00–06:20 local**: open the live webcast player (US VPN) — the
  player chrome sometimes exposes a phone tab only once the event is live.
  If digits appear: update `configs/dpz-autorun.env`, then
  `launchctl kickstart` is NOT needed — the 06:05 fire reads the env at
  launch; before 06:05 an edit is safely picked up.
- After ANY number lands: fill `TO_NUMBER`/`PASSCODE`, run
  `scripts/preflight-event.sh --config configs/twilio-live-dpz.toml
  --account ~/.earshot/ms.env --fire-hhmm 0605` the night of 07-19.
  Preflight/supervisor FAIL-STOP on TBD is already validated.
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

## Same-floor W1-pre findings (2026-07-17 evening) — the YouTube access terrain

Three live-measured facts that shape the webcast lane's design:

1. **CGNAT breaks direct-manifest fetching from the CDMX box.** googlevideo
   segment URLs are IP-locked (the manifest bakes in `/ip/<requester>/`);
   this connection rotates egress IPs mid-session → ffmpeg-on-manifest dies
   with TLS/IO errors. yt-dlp (which re-resolves per session) worked: 80 s
   captured. Direct-manifest strategies REQUIRE a stable-IP host.
2. **YouTube bot-walls datacenter IPs.** From the us-east EC2 box, current
   yt-dlp gets "Sign in to confirm you're not a bot" — YouTube capture from
   AWS needs authenticated cookies (`--cookies`), a residential egress, or a
   non-YouTube simulcast. Also: Ubuntu 24.04's node 18 is not accepted as a
   JS runtime by yt-dlp 2026.07 — install deno on the VPS.
3. **Cadence probe methodology works** (runs Wednesday against TSLA's
   stream): resolve manifest → `EXT-X-TARGETDURATION`/`EXTINF`. Sky News
   reference: 5 s segments.

Implications: (a) W1's strategy race needs the operator's YouTube cookies on
the VPS (ordinary yt-dlp practice, ToS-gray — operator decision) or an owned
test stream + cookies; (b) non-YouTube corporate webcast players (Notified/
Q4/media-server HLS — INTC, most issuers) have no bot-wall and are the
friendlier same-floor venue for direct low-latency fetching; (c) Wednesday's
TSLA v0 runs from the CDMX box via yt-dlp (proven path) — proximity matters
less for a v0 whose goal is cadence + corpus.

## Webcast lane (branch `webcast-lane`, 2026-07-17 — MERGE ONLY AFTER DPZ)

`SourceConfig::Hls`/`Pipe` are now real (`src/source/pipe.rs`: ffmpeg/yt-dlp
subprocess → s16le PCM → AudioFrames; 6 tests incl. pipeline detection parity
vs replay). Launch: `scripts/launch-webcast.sh` — **observe posture enforced**
(no fillable/live flags accepted) until the webcast lag is measured. Purpose:
no-phone issuers (TSLA/NFLX class), webinar-shape unknowns, and ANY event
whose number hunt fails — degraded-GO instead of NO-GO. First target:
**GOOGL 07-22 (YouTube-only per Alphabet's PR), attended.** ⚠ The 07-19 DPZ
preflight pins from main HEAD — this branch stays unmerged until Monday
evening precisely so Monday runs exactly what was drilled.

Sequential, not concurrent (times pinned 2026-07-17 from issuer sources):
**GEV 7:30 AM ET = 5:30 CDMX** (gevernova.com PR) → fire 05:05, wake 05:00;
**GOOGL 4:30 PM ET = 2:30 PM CDMX** (abc.xyz IR page) → fire 14:05, operator
can attend. Same accounts, per-event state dirs. TSLA same day: NO-GO (no
phone leg). Prep state (2026-07-17):

- Configs generated via `prepare --fillable` + test-armed dry-run:
  `configs/twilio-live-gev.toml` (**12/12 markets active**),
  `configs/twilio-live-googl.toml` (**18/18 active**); keyword compile clean.
- ⚠ **Every word on both events is corpus-UNVALIDATED → all caps are 1¢
  observe.** As generated, Wednesday is observe-only. Making any word
  fillable requires the validation-loop promotion (or the risky `--fill-all`
  override) — **posture freeze Monday evening, after the DPZ outcome.**
- Env files staged with TBD numbers (`configs/gev-autorun.env`,
  `configs/googl-autorun.env`); preflight TBD gate governs go/no-go
  independently per event (preflights night of 07-21).
- Number hunts: GEV — IR ask to **investors@gevernova.com** and the direct
  IR line **+1 617 674 7568** (Michael Lapides, VP IR); GOOGL — IR ask to
  **investor-relations@abc.xyz**; plus registration/player checks per Track A.
- GOOGL expectation per VERIFICATIONS Q-MSPM: marquee-name hot words likely
  lose races at tight caps — value is detection data + any sitting asks.

## Re-validation rehearsal 2026-07-17 (after the Linux-seam script changes) — PASSED

The platform-seam changes to `event-autorun.sh` / `launch-event.sh` /
`preflight-event.sh` (us-east Phase 1 work) touched the DPZ live path, so the
launchd-fired TEST rehearsal was re-run the same day: kickstart 10:53:15 →
armed **configs/rehearsal-gev.toml** (fresh `prepare` output — the old
rehearsal config's NFLX markets had FINALIZED and arm correctly refused them;
lesson: **rehearsal configs go stale as events settle — re-prepare against a
live event each week**) → real TTS dial → 7129 frames / 1783 chunks / 0 drops
→ clean long-stream close → rc=0 → [TEST] SUMMARY. Caveat, stated exactly:
**0 detections, expected** — the TTS loopback speaks NFLX-tailored text and
this run armed GEV vocabulary; the detection leg is binary-level, unchanged
since its 3/3 TTS pass on 07-16. The changed (shell) surface is fully
re-validated.

## DPZ readiness scorecard (2026-07-16) — graded by evidence class

Classes, strongest first (the CLAUDE.md standard: *validated = rehearsed with
the exact production mechanism*):
**WIRE** = live evidence against the real external system ·
**PROD-REHEARSED** = launchd-fired / real signal / minimal env ·
**PAPER** = primary source read, never exercised ·
**ASSERTED** = no evidence — treat as false until shown.

| Claim | Class | Evidence |
|---|---|---|
| Twilio capture vs a real bridge | WIRE | MS bridge 07-14 (CA5831c4, 3585 frames, 0 drops) + 85-min live run 07-15 |
| Full unattended chain (launchd fire → dial → detect → order → email) | WIRE | MS run 07-15 completed rc-clean; SUMMARY sent (FAILED email was the self-bootout bug, fixed) |
| Kalshi mention-category order acceptance | WIRE | probe orders on TARI/RECE 07-14 + live RECE IOC ack 07-15 |
| Warm order path (~102 ms send→hdrs) | WIRE | MS run report.json; VERIFICATIONS Q-MSPM |
| Supervisor generalized to per-event env | PROD-REHEARSED | launchd-fired rehearsal 07-16 08:53 (env sourced, TTS dial, 7124 frames, 3 detections, rc=0) |
| SIGTERM kill path / STOP / TERM→KILL escalation / frames guard | PROD-REHEARSED | 07-15 dress rehearsal (SIGTERM 6 ms; STOP 152 ms; escalation fired for real) |
| DPZ config arms | PROD-REHEARSED | dry-run test-arm 14/14 vs pinned binary 07-16 |
| prepare encode-validation + preflight keyword compile | PROD-REHEARSED | follow-up 6 closed 07-16, force-fail tested |
| notify() retry | PROD-REHEARSED | force-fail verified 07-16 (both attempts logged) |
| Preflight TBD-number FAIL-STOP (`--event-env`) | PROD-REHEARSED | full real preflight run 07-16 vs the DPZ event: gate FAILed on TBD/TBD → NO-GO, exit nonzero, PREFLIGHT email sent. Same run also PASSed: pin, keyword compile, both cred probes, mailer; flagged missing pmset wake + battery power (both expected to be set 07-19) |
| Bank published dial-ins (JPM/GS/C) | PAPER | primary-source PRs read; **never dialed** (only MS is wire-verified) |
| AAPL listen-line confirmation code | PAPER | IR page renders all states at once; code not date-stamped — re-verify at call time |
| RKLB bridge evidence | PAPER (weak) | search summary only — re-derive before acting |
| **Detection-triggered mention FILL** | **ASSERTED** | **has NEVER happened.** One real fill ever = M2 plumbing 07-10 (non-mention 1¢). DPZ is this test |
| **DPZ dial-in number** | **ASSERTED** | TBD — event is NO-GO without it (Track A above; deadline 07-19) |
| Competitive fill at tight caps | **ASSERTED (evidence against)** | Q-MSPM: best competitor's whole loop beat our *audio arrival* by ~400 ms on MS-RECE. Until audio transport + detector chunking shrink, expect to lose sub-50¢ races on contested words |
| Mid-call stream recovery | ASSERTED | follow-up 1 open: a 30 s stall ends the run; recovery = full redial re-paying ~30 s warmup |
| Post-STOP arm SIGTERM immunity | KNOWN BUG (covered) | follow-up 5: TERM→KILL escalation covers it, fired for real 07-15 |
| Book freshness at decision | INSTRUMENTED, UNPROVEN | `book_age` added 07-16 after the MS RECE 20¢-vs-98¢ discrepancy; first data next live run |

Bottom line for DPZ: **plumbing is wire- or production-validated end to end;
the two things that have never been true remain the two go/no-go items — a
number to dial, and a mention fill.** The fill test itself carries a measured
headwind (the 400 ms audio deficit), so a DPZ zero-fill outcome with clean
detections would be *consistent with the known latency gap*, not a new
failure — judge it against `book_age` + the public tape, per Q-MSPM.

## Fills — precise status (do not over- or under-claim)

The system HAS filled one real order: the **M2 acceptance fill, 2026-07-10**
(1¢ × 1 IOC on a non-mention market, 231 ms round-trip, fee $0.0007 — wire
evidence pinned in `VERIFICATIONS.md` M2 live-drill pins). What has NEVER
happened is a **detection-triggered mention fill**: the full hear-keyword →
cross-ask → fill loop. The MS run sent 1 order (canceled unfilled, IOC).
Ledger `spent_cents` values prove sends, not fills (caps are debited on
send, never refunded — `src/risk.rs::record_send`). The next live event with
the fillable posture is that validation.

⚠ The widely-repeated "0 fills, **4 LOST_RACE**" headline for the MS run is
a misread (corrected 2026-07-16, post-mortem below): all 4 "LOST_RACE"
records were **1¢ observe-tier intents against 21–70¢ asks — the designed
non-crossing outcome**, not lost races. `ExecReport` now splits the counters
(`lost_races` vs `observe_noncross`, `src/kalshi/executor.rs`). The run had
exactly ONE genuine race (RECE), and its post-mortem is follow-up 2.

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
2. ~~Warm-path order latency: connection/presign reuse is a LOST_RACE lever~~
   **POST-MORTEM COMPLETE 2026-07-16 — the premise was wrong on every count.**
   Evidence: `state/KXEARNINGSMENTIONMS-26JUL15/report.json` + the public
   Kalshi trades tape for `…MS-26JUL15-RECE` (pinned in `VERIFICATIONS.md`
   Q-MSPM). Findings, in order of importance:
   - **The RECE race was lost upstream of everything tunable in the order
     path.** Tape: the first competitor fills printed **13:27:10.274Z**,
     sweeping RECE YES 27¢→95¢ (then to 98¢ by 10.507). Our audio chunk
     containing "recession" reached the pipeline **≈13:27:10.672** (decision
     wall-time minus the 91.3 ms capture→decision edge) — i.e. **the
     competitor's entire hear→fill loop finished ~400 ms before our audio
     even arrived**. Our internal 91 ms and the 104 ms decision→ack could
     both be zero and we still would not have filled at ≤26¢. The binding
     constraint is **audio-arrival latency** (bridge → Twilio media →
     ngrok → us), plus the detector's 80 ms chunking (~60 ms of the 91 ms
     internal edge is chunk assembly — `preproc→chunk` p50 60.5 ms).
     That sets the low-latency milestone's measured target: audio transport
     must shed hundreds of ms; the detector edge tens.
   - **`presign_age` 1.17 s is not a latency cost.** The presigner refreshes
     every 10 s and `get()` is a wait-free ArcSwap read; 1.17 s is merely
     time-since-refresh, far under the stale-fallback threshold (no "inline
     signing" warning in the run log). Nothing to fix.
   - **`send→hdrs` 102 ms is consistent with an already-warm h2 socket**
     (CDMX→Kalshi RTT + matching-engine time; a cold TLS handshake would
     read 250 ms+). The conn-warmer + presigner were live during the run
     (commit 8421741). Remaining order-path lever is geography (a us-east
     execution box), not code — deferred, likely moot given the 400 ms
     upstream deficit.
   - **Open forensic hole, now instrumented:** the decision-time book showed
     best_ask 20¢ at 10.763 although the tape had swept to 98¢ by 10.507 —
     either our WS book was ≥250 ms stale or an MM briefly re-quoted 20¢.
     Undecidable post-hoc then; the executor now records `book_age`
     (snapshot age at decision) per order so the next run settles it.
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
