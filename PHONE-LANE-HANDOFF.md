# HANDOFF — Robustify the phone lane: acquisition, admission, persistence

> ## ⚠ HISTORICAL as of 2026-08-01 — its work list is DONE, its state is WRONG
>
> This file was written 2026-07-29 ~21:45 UTC. Most of what it asks for was
> built the following night, so reading its §4 as a to-do list — or its §3 as a
> description of the code — will actively mislead you. Superseded items:
>
> | This file says | Actually |
> |---|---|
> | §4.1 build a bridge-entry probe | BUILT — `scripts/bridge-probe.sh`, four venues classified (Q-ADMIT) |
> | §4.2 `idle_timeout` is 30 s hardcoded, unreachable from config | FALSE — four per-source knobs in `src/config.rs`, validated |
> | §4.2 build an auto-redial supervisor | BUILT and live-pinned (Q-REDIAL): drop→audio 4.03 s |
> | §4.4 remove the one-event concurrency cap | DONE — TLS terminates on the box, Q-NGROK cap lifted (Q-TLS) |
> | §4.5 operator handler is design-only, needs sign-off | BUILT v1 and live-pinned (Q-OPERATOR-V1), fail-closed proven |
>
> **Current state lives in `docs/STATUS.md`. Current facts live in
> `VERIFICATIONS.md` (Q-TLS, Q-REDIAL, Q-OPERATOR-V1, Q-CONF, Q-UPDATE,
> Q-ADMIT).** What remains valuable here is the *reasoning* — why the phone
> lane is worth 4–8 s, and what the 07-29 failures actually were. Read it as
> the argument that motivated the work, not as the status of it.

**Written 2026-07-29 ~21:45 UTC, after a 6-event day. Read this whole file before touching anything.**

You are picking up EARSHOT (see `CLAUDE.md`, then `docs/ARCHITECTURE.md`). Your mission is
narrow and it is the highest-value work in the project right now:

> **Make the phone lane reliably ACQUIRE a dial-in, GET ADMITTED to the bridge, and STAY
> CONNECTED for the whole call — unattended.**

Not detector tuning. Not micro-latency. Those are already good enough to be irrelevant to
today's losses. The system loses because it either cannot get onto the fastest audio path,
or gets dropped off it within a minute.

---

## 1. Why this is the priority (measured today, not asserted)

**The phone lane is worth 4–8 seconds, measured on the same call, same detector, same
thresholds — transport was the only variable.** CMG 2026-07-29, both lanes armed
simultaneously:

| word  | phone lane   | webcast lane | phone ahead |
|-------|--------------|--------------|-------------|
| HONE  | 20:43:34.890 | 20:43:39.864 | 4.97 s |
| INFL  | 20:55:06.091 | 20:55:14.249 | 8.16 s |
| SUPP  | 21:07:39.692 | 21:07:43.788 | 4.10 s |
| CHIPL | 20:51:48.651 | never fired  | phone-only (recall win too) |

**And that margin is exactly the size of the gap to the competition.** The closest race of
the night, CMG `SUPP`: market's first ≥90¢ print at **21:07:39.058**, our phone fire at
**21:07:39.692** — **lost by 0.63 s**. The webcast lane lost the same word by 4.73 s.
Roughly: *phone ≈ photo finish, webcast ≈ never in it.*

Full day's race record (all scored against first ≥90¢ print on the event day, after call
start — use `scratchpad/wingmon/score.py`, and note the two scoring bugs called out in §7):

- **0 wins.** (An earlier claim of wins on SBUX MATC/TARI was WRONG — truncated trade
  window. A later "win" on META CLOU was also wrong — the market just didn't reprice for
  16 minutes. Do not repeat either error.)
- **Close speed losses:** CMG SUPP −0.63 s (phone), CMG SUPP −4.73 s / AL −5.46 s
  (webcast), SBUX ×6 in the −2.2 to −9.7 s band (mean ≈ −6.1 s, webcast-only venue).
- **Recall misses, NOT speed:** CMG HONE −606 s, CHIPL −215 s, INFL −737 s, META SILI
  −693 s. The market moved on an utterance we never detected; we fired on a later repeat.
  Different problem, tracked separately (see `keyword-readiness-gate` memory).

---

## 2. What actually happened today — the evidence

### 2.1 The lane was DEAD all day until 20:11 UTC (fixed, don't re-debug)

`scripts/launch-event.sh` never exported `ORT_DYLIB_PATH`. `streaming_ctc` dlopens
onnxruntime, so the **detector build hung**. Because `build_pipeline_input`
(`src/main.rs:971`) constructs the source — which BINDS the WS listener — *before* building
the detector (`src/main.rs:671-677`), the port was already open when the hang occurred.

Everything downstream then lied: `nc -z` passed, ngrok logged `join connections` from a
Twilio IP, calls connected and ran to completion. **Nothing ever `accept()`ed.** The tell
was `Recv-Q 3` on the listener (`ss -tlnp`) — three connections queued in the kernel
backlog. Same silent-hang class as RUNBOOK bug 1 (Ford 07-28 ORT dylib), whose guard
existed in `run-arm.sh`/`launch-webcast.sh` but had never been added to the TELEPHONY
launcher. **Fixed in commit `0e0e2bd`; rehearsed via the production mechanism.**

Lesson to carry: *when a lane is silent but the port is open, check the accept queue before
suspecting the protocol layer.*

### 2.2 Bridge admission — the ACTUAL open problem

After the fix, the WS/media path is provably healthy. Four live bridges, four different
outcomes:

| venue | CallSid | media started | media stopped | held | outcome |
|---|---|---|---|---|---|
| TTS rehearsal (+17627212978) | — | ✓ | on SIGTERM | full | **PASS** — sig verified, detection fired |
| **CMG** +18883176003 code `3937444#` | `CAeb4f8bbaa252ebb41dbbb2b7532c4eaf` | 20:17:37 | (killed by us 21:13) | **~55 min** | **WORKS** — 4 detections |
| **MSFT** +18774070666 (no code) | `CA9cceae1f2965be35a9e2849eb8178496` | 21:13:45 | 21:14:27 | **42 s** | dropped |
| **MSFT** retry | `CA1beb6db4156b390fe0af5cfa2b9fd84c` | 21:26:29 | 21:27:42 | **72 s** | dropped |
| **MSFT** retry, AFTER call start | `CAe3fa44c317294eb741f2286d63142efc` | 21:30:50 | 21:31:32 | **41 s** | dropped |
| **CVNA** +18007159871 code `3609424#` | `CA035fb931ffcaab46f1cdf7c0b51a7403` | 21:34:56 | 21:35:19 | **23 s** | dropped |
| **WING** published number | (Q-OPS probe 03:35) | — | — | 33 s | **operator-screened**, human asks "how can I help?" |

Twilio reports `status=completed` — the FAR END hung up, cleanly. `frames` count × 20 ms
gives the exact hold time. Note MSFT clusters at ~41 s / ~72 s / ~41 s and is
**independent of dial time** (16 min early, 3.5 min early, and *after* the call started all
failed identically).

**We have never listened to what those bridges actually SAY to us.** That is the single
biggest hole. We captured 2084 / 3620 / 2073 / 1161 frames of the bridge's own audio and
threw every one away. Fixing that is step one of your work (§4.1).

### 2.3 Environment facts you inherit

- Box: `ubuntu@3.212.174.94` (**Elastic IP attached today** — AWS moved the instance
  mid-session and the old IP `3.239.237.41` is dead; the EIP prevents recurrence).
  Key `~/.ssh/earshot-useast1.pem`. Instance `i-04c50d76572aa092e`, c6a.4xlarge, us-east-1.
- ngrok v3.39.10 at `~/bin/ngrok`, authtoken installed from the laptop.
- Real Twilio creds staged at `~/.earshot/ms.env` and `~/.earshot/intc.env` (two DIFFERENT
  Twilio accounts; Kalshi key paths were patched to the box's `~/.kalshi/earshot.pem`).
  ⚠ `~/.earshot/stub.env` is a PLACEHOLDER (`FROM=+15550000000`) — never use it.
- **Q-NGROK (pinned today): the free plan serves ONE static endpoint**
  (`sondra-unshivering-derelictly.ngrok-free.dev`). A second concurrent agent fails with
  `ERR_NGROK_334`. So **only one phone event can run at a time.** This forced a real
  sacrifice today: CMG's phone leg was killed at 21:13 to free the tunnel for MSFT.

---

## 3. Hard constraints — do not violate these

1. **Listen-only invariant.** DTMF charset is `[0-9#w]`; `*` is hard-rejected at three
   layers (`twilio-dial.sh:42`, `launch-event.sh:55`, `event-autorun.sh:85`) and asserted by
   `tests/twilio_listen_only.rs`. TwiML must stay `<Start><Stream>`; **never** emit
   `<Connect>`, `<Dial>`, `<Number>`, `<Sip>`. The WS handler never sends anything but
   `Pong`. If your design needs audio INTO a call (the operator-clip idea), it needs a
   SEPARATE, explicitly-gated TwiML path with its own invariant and its own test — never a
   loosening of this one. **Get user sign-off before writing that code.**
2. **Money/risk untouchable.** `src/risk.rs` caps are code constants; 1¢ non-crossing
   observes, `$1` probe ledger per state dir. Kill switch cancels ONLY `OrderRegistry` ids.
3. **Rehearsal rule (`CLAUDE.md`).** An unattended path counts as validated only when
   rehearsed with the *exact production mechanism* — via `scripts/launch-event.sh`, killed
   with real SIGTERM, under the launcher's own env. A manual drill proves nothing.
4. **Layer docs.** Any layer you change gets its `docs/layers/<layer>.md` updated in the
   same change. Wire facts go in `VERIFICATIONS.md` with dated evidence.
5. **Never dial a bridge to "test" during a live event you are also trading.** Use the TTS
   rehearsal number `+17627212978` (auto-answering, plays a keyword script; ~$1.15/mo).

---

## 4. The work, in priority order

### 4.1 FIRST: hear what the bridges say (unblocks everything else)

Build a **bridge-entry probe** that dials a number, records the first ~120 s of inbound
audio to a WAV, and transcribes it. Prior art to reuse, not reinvent:

- `earshot capture-bakeoff` (`src/main.rs:473-498`) already builds a leg and writes audio
  WITHOUT constructing a detector — this is how the WING Q-OPS probe captured
  `corpus/bridge-probes/wing-2026-07-29-bridge-entry.wav`.
- `scripts/launch-event.sh --no-dial` + `scripts/twilio-dial.sh` for the call itself.

Then answer, per venue, with a transcript pinned in `VERIFICATIONS.md`:
- What does the bridge ask for? (passcode? name recording? "press 1"? operator?)
- At what second does it hang up, and what does it say immediately before?
- Does it announce "listen-only mode" (good) or ask a question (needs a response)?

**Hypotheses for the MSFT/CVNA drops — test, don't guess:**
(a) prompt requires a keypress we never send (`w` waits exist but we send no digits for
MSFT); (b) name-record step; (c) silence-detection drop (we NEVER transmit audio —
`track="inbound_track"`, one-way — a bridge may treat a silent leg as dead); (d) operator
screen like WING; (e) Twilio answering-machine detection or carrier-side rejection;
(f) the `<Pause length="5400">` TwiML interacting badly. Hypothesis (c) is my prime
suspect: it would explain why CMG (which had continuous hold music and an active
conference) held for 55 minutes while quieter bridges dropped in 23–72 s.

### 4.2 Bridge admission + persistence

- **Auto-redial supervisor.** Today a drop ended the run — `docs/RUNBOOK.md:1910-1913`
  already flags this ("a 30 s audio stall or socket close ends the whole run; recovery is a
  full shell redial"). The source returns `EndOfStream`/idle error and the arm exits. Build
  a supervisor that re-dials and re-attaches WITHOUT tearing down the armed pipeline,
  bounded (e.g. N retries, backoff, hard stop at call end), and loudly logged.
- **Dial timing.** Bridges reject early callers; MSFT hung up on a 16-min-early dial and
  also on an at-start dial. Determine the real admission window per venue empirically and
  record it in `dial-ins.toml`.
- **`idle_timeout` is 30 s** (`src/source/twilio_ws.rs:139`), `accept_timeout` 900 s. Both
  are hardcoded defaults not reachable from config. Consider making them per-event config.

### 4.3 Acquisition — the dial-in registry itself

`configs/dial-ins.toml` (~90 entries) is the asset. Today it was **wrong in ways that cost
us**: MSFT is recorded `tier=published, listen_access=yes, "no password required"` and yet
the bridge will not hold us; WING was `tier=published` until a live probe found an operator
screen (now `listen_access="operator_screened"` — a fifth enum value the header comment at
L38-39 doesn't document; fix that).

- Add an **observed-admission** field set only by probe, never by reading a PR: e.g.
  `admission = "held_60min" | "dropped_42s" | "operator_screened" | "untested"`, with
  `admission_probed` date and the probe artifact path.
- Make `scripts/preflight-event.sh` REFUSE (or loudly WARN) an event-day phone arm whose
  venue has no successful admission probe. Today's failures were all knowable the night
  before with one 2-minute call each.
- Re-audit every `tier=published` entry with the §4.1 probe. Assume the registry is
  optimistic until proven otherwise.
- `scripts/mine-dial-in.py` exists for deriving `qa_mechanism` — check whether it can be
  extended for unpublished participant numbers (transcripts, 8-Ks, registration
  confirmations, legacy numbers). Standing user preference: **phone-first** — an
  unpublished bridge is worth real effort to find before declaring an event webcast-only.

### 4.4 Remove the concurrency cap

Q-NGROK means one phone event at a time — unacceptable on days like today (CMG 20:30 vs
MSFT+CVNA 21:30). Two paths, both already blessed:
- paid ngrok with multiple reserved domains (cheap, immediate), or
- the **no-ngrok migration** already approved in `docs/INGRESS-BAKEOFF.md` — TLS
  terminating on the box itself, which ALSO removes a network hop from the latency-critical
  lane. Prefer this if you can land it; it's strictly better.

### 4.5 (Design only, needs sign-off) operator-screened venues

For WING-class bridges, the user proposed and endorsed: a **clip-based operator handler** —
pre-recorded clips in the user's own voice (greeting/identity, name, spelled name, company,
spelled company, email, role, "listen-only, no questions", "please repeat", yes/no/thanks,
plus a per-event company-name clip), a small classifier on the operator's utterance, and a
**human-fallback leg** that dials the user's cell into the conference and pages them when
confidence is low. The fallback is load-bearing, not optional. This collides with the §3.1
invariant — spec it, get sign-off, do not implement unilaterally.

---

## 5. What "done" looks like

1. Every `tier=published` venue in `dial-ins.toml` carries a dated, probe-observed
   `admission` value with a transcript artifact.
2. A phone arm survives a full call unattended, including at least one induced mid-call
   drop that the supervisor recovers from — rehearsed via `launch-event.sh` + real SIGTERM.
3. Two concurrent phone events run cleanly (cap removed).
4. `cargo test` green, including `tests/twilio_listen_only.rs` unchanged in spirit.
5. `VERIFICATIONS.md` carries the per-venue admission pins; `docs/layers/source.md` matches
   any code you changed.

---

## 6. Key files

| path | why |
|---|---|
| `scripts/launch-event.sh` | telephony launcher; ORT guard added today at the `source "$ACCOUNT"` block; the weak `nc -z` dial gate was replaced later on 07-29 by the `${STATE_DIR}/listener.ready` readiness-token wait (arm writes it only after the detector is built and the pipeline is accepting) |
| `scripts/twilio-dial.sh` | TwiML (single line, ~L64), DTMF charset guard L42 |
| `src/source/twilio_ws.rs` | WS source; `run()` L238-365, signature L161-205, timeouts L138-139 |
| `src/main.rs` | `build_leg` L616-628 (bind), `build_pipeline_input` L671-677 (bind-then-detector ordering — the trap), pipeline start L989 |
| `configs/dial-ins.toml` | the registry; header contract L1-66, APPLE RULE L41-50 |
| `configs/twilio-{cmg,cvna,msft}-26jul29-arm.toml` | today's phone configs (generated from the webcast ones: swap `[[sources]]` to `twilio_stream`, drop `[arbiter]` — fanout needs ≥2 sources) |
| `VERIFICATIONS.md` | Q-M3 (Twilio pins), Q-OPS (operator screen), Q-CC (Chorus Call URL shape), Q-NGROK (added today) |
| `docs/TWILIO.md`, `docs/MULTI-EVENT.md` §8, `docs/INGRESS-BAKEOFF.md` | contracts + the no-ngrok plan |
| `scratchpad/wingmon/score.py` | race scorer — **read §7 first** |

---

## 7. Traps that already bit us today — do not repeat

1. **Race scoring.** `limit=8` on the trades endpoint truncates and makes losses look like
   wins (I published two false wins). Use `limit=100`, filter to the **event date** (not
   just time-of-day — cross-day trades leak), and take the first ≥90¢ print at/after call
   start. Also: a positive delta on an illiquid market is not a win — check whether the
   market simply hadn't repriced.
2. **Chorus Call live URL** is `httporiginlive/<STREAM-ID>_http/playlist.m3u8` where
   `<STREAM-ID>` is **not** reliably the webcastid (WING's was, SBUX's was numeric
   `6216819`). Wrong id ⇒ silent 404 forever ⇒ live gate never fires (Q-CC).
3. **`run-arm.sh` guard 8** refuses two arms on the same `[event].id`. To run phone+webcast
   on one call, give the webcast config an `[event].id` suffix (`-WEBCAST`) so it gets its
   own state dir — validated today (15 markets active, books live). Note each lane then has
   its own `$1` ledger.
4. **Stream URL files vanish.** `$HOME/<slug>-stream.url` disappeared twice today between
   write and arm; cause unidentified. The arm reads it at fire time via `cat`, so verify
   immediately before arming.
5. **Wall-clock.** FOMC (43 markets, the day's crown event) was **missed entirely** because
   the operator lost track of time while debugging. Set real alarms; never trust elapsed-time
   intuition during deep work.

---

## 8. First three actions

1. SSH the box, confirm it's alive, read the four `~/{cmg,msft,cvna}-phone*.log` files and
   `~/ear/state/KXEARNINGSMENTION*/` artifacts from today.
2. Build the §4.1 bridge-entry probe and run it against **MSFT +18774070666** and
   **CVNA +18007159871 (code 3609424#)**. Transcribe. That transcript decides the whole
   design.
3. Report findings + a concrete plan before writing production code.

**Do not start by tuning the detector. The detector is not why we lose.**
