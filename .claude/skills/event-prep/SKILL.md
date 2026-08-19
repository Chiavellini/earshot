---
name: event-prep
description: Prepare upcoming earnings-call events end-to-end — phone bridge AND webcast lanes — from pasted dial-in/registration details or a target date. Scopes the Kalshi markets, qualifies admission, resolves call times, stages immutable RunPlans, studies keywords, sets the per-word betting posture from measured detectability, preflights, and schedules with loaded-job proof. Use when the user pastes dial-in or registration confirmations, gives webcast URLs, or asks to prepare, stage, probe, study, posture, preflight, or schedule events or an event date. It PREPARES only — it never arms a live run by hand and never changes risk caps.
---

# Event prep — from "here are the events" to green, scheduled, and proven

## Purpose

Turn a date (or pasted event details) into: every discoverable event on that
date staged as an immutable RunPlan, its keywords studied, its per-word posture
set from measured detectability, preflight green, and its launch job LOADED and
proven to fire in the future — or descoped on the record. The operator should
be needed only for decisions the system defines as human ones: call-time
evidence, access-mode attestation, ambiguous admission verdicts, and descopes.

## The incidents this exists to prevent

2026-08-06: all 5 staged events were silently lost because an unresolved call
time stages a midnight-UTC placeholder, so every T-75 job installed already in
the past — runs=0, never fired. 2026-08-11: both live events (CAVA, CRWV) died
in the operator queue because "Meeting ID" without a passcode parks you with an
operator who hangs up silent legs every ~75–90 s; the registry could not know
until a probe failed. 2026-08-12: CBRS was missed on preflight thrash — red at
20:42, green at 21:20:41Z for a 21:00Z call. **The failure is almost always the
timing of discovery, not the choice.** So: resolve times, bind access, and probe
admission EARLY; verify the LOADED job, never the file you wrote.
2026-08-19: TGT and TOL were BOTH lost with zero audio on two hosts. The box
legs waited 75 min on a url-file that had only ever been published on the
laptop (the push named one host); the laptop TOL leg armed on a "master" that
yielded zero frames (no PCM proof before publishing); TGT's venue (webinar.net)
never serves an HLS manifest at all (WebRTC) and nothing had proven it would;
and the only live supervision was a session wakeup that never fired. **A binding
is not "done" until PCM has been proven from that URL on every executing host,
the push is a LOADED launchd job naming every host, and a watchdog is loaded.**

## Scope guard — run this before anything else

1. Resolve the target date and event set: read `slate/latest.json` and
   `slate/worklist.md`; if the user pasted details for an issuer not on the
   slate, run `python3 scripts/discover-events.py` (TICKER DATE ≠ CALL DATE —
   the call date comes from `sub_title`, and even that can be stale:
   `stale_open_event`).
2. `python3 scripts/board-open.py --issuer <T> --json` per issuer. Exit codes
   ARE the answer: `0` open board, `1` none, `2` UNDETERMINED — **2 is never
   "no"**; a guard that fails open is how misses recur.
3. `git log --oneline -3` and `git status --short` — the nightly
   slate/hunt/stage jobs commit autonomously and can land mid-session; three
   reliability lanes may also be mid-work. Re-check before trusting any
   minutes-old read, and never commit files you did not create.
4. If no open or upcoming board exists for any named issuer, **say so and
   stop.**

## Phase 1 — SCOPE THE MARKETS

For each event, get the actual words the detector must hear and the markets
they resolve:

```sh
source ~/.kalshi/earshot_env
cargo run --release -- prepare --event-ticker KXEARNINGSMENTION<T>-<DATE>
```

`prepare` is read-only on Kalshi; it lists each market's `yes_sub_title` (the
word), live asks, and parsed variants — and it prints the dial-in as a TODO and
a placeholder `starts_at` (see Phase 3). Then screen resolution against the
LIVE board (the "iPhone 18" class: digits are stripped from variants, so a
16/16 study can still be unable to resolve the market):

```sh
python3 scripts/market-phrase-screen.py configs/<event>-arm.toml
```

Exit 1 = blocking PHRASE-MISMATCH → that market is a **descope question**, not
an ack.

## Phase 2 — INTAKE (phone lane)

When the user pastes a release/registration text:

```sh
pbpaste | scripts/dial-in-intake.py --key <TICKER> --redact
```

Exit codes are the verdict — act on them, never on vibes:

| exit | meaning | action |
|---|---|---|
| 0 | DTMF credential | emit the `numbers.env` suffix it prints into `state/hunt/numbers.env` (0600) |
| 3 | SPOKEN credential ("referencing…") | operator screen expected — prepare `[operator_handler]`, do not pretend DTMF |
| 4 | AMBIGUOUS verb | a human reads the source text; do not guess |
| 5 | no participant dial-in | hunt (below) or webcast-only |

No pasted text? Hunt SEC EDGAR (read-only, never dials):

```sh
scripts/hunt-dial-in.py --key <TICKER>          # or --from-slate slate/latest.json
scripts/registry-propose.py --slate slate/latest.json --days 12
```

Proposed blocks carry `admission = "untested"` by construction. Paste them into
`configs/dial-ins.toml` by hand; digits never enter argv, commits, or
`admission_detail` (not digit-safe — doc-lint scans for digit runs).

**THE MSFT RULE**: `admission` is established ONLY by a probe dial (Phase 6) —
never by a press release, never by the registry's optimism.
**THE APPLE RULE**: `qa_mechanism` and `listen_access` are independent facts.

## Phase 3 — CALL TIME (the midnight-UTC trap)

`src/prepare.rs` emits `starts_at = "2026-07-15T00:00:00Z"  # TODO` and
slate-stage keeps the TODO loud. An untimed event stages NEEDS-TIME, gets no
autorun env, and its fire-mechanism preflight check silently downgrades to a
WARN. Resolve the time with **two independent, non-Kalshi sources** and record
it:

```sh
$EDITOR slate/call-times.json     # {"<EVENT_TICKER>": {"call_time_utc":"YYYY-MM-DDTHH:MM:SSZ","source":"...","recorded_utc":"..."}}
python3 scripts/slate-time-merge.py --require-date <DATE>
```

Its refusals are law: a date-mismatched override needs a typed
`date_correction`; gcal outranks IR-page reading; `ESTIMATE …` sources are
accepted but printed loudly. `eventctl` refuses the placeholder and any
`starts_at` line containing TODO — that is the guard working, not an obstacle.

## Phase 4 — STAGE

```sh
scripts/opsctl.sh stage --date <DATE>
```

(Chains slate-time-merge → `slate-stage.sh` on the TIMED slate →
`eventctl.py init-date`, which compiles the immutable schema-3 RunPlans.)
Per-event outcomes and what each demands:

| outcome | action |
|---|---|
| STAGED / ALREADY-STAGED / RECONCILED | proceed |
| NEEDS-TIME | Phase 3, then re-stage |
| NO-PHONE-LANE | registry `admission != "held"` — Phase 6, then `scripts/slate-stage.sh --issuer <T>` to restage same-day |
| WINDOW-WRAP | call window crosses UTC midnight; set END/COMPLETE HHMMs by hand |
| ENV-STALE / ERROR | read the printed reason; fix or descope |

A config referenced by a published RunPlan is **immutable** — any byte change
means a new RunPlan attempt, never an in-place repair (`stage-reconcile.py`
enforces this).

## Phase 5 — ACCESS (webcast lane)

```sh
scripts/opsctl.sh access --date <DATE>
```

Every FAIL line prints the exact `bind-access` command to complete. Choose the
resolver from the venue:

| venue signal | resolver-kind / helper |
|---|---|
| Q4 Inc attendee URL (`events.q4inc.com`, numeric event id) | `q4-api` → `scripts/q4-resolve.sh --event-id <id> --slug <lc>` |
| Q4 guest-registration wall | `scripts/q4-register-resolve.py` (uses `~/.earshot/registration.env`) |
| Chorus Call Dynamic Media Frame (`webcastid`) | `choruscall` → `scripts/choruscall-resolve.py`; self-serve form → `scripts/chorus-register-resolve.py` |
| YouTube Live | `youtube-live` → `scripts/youtube-resolve.sh` — ⚠ the BOX is bot-walled at the segment layer (manifest 200s, media refused); laptop vantage only |
| **REGISTRATION WALL** (media-server MMC `edge.media-server.com/mmc/p/<hash>`, or ANY player asking name/email/company) | `browser-network` + **`registered-session`** — see the mandatory prep step below; NEVER bind these `public` |
| unknown venue page, no wall | `browser-network` → `scripts/browser-resolve.py` (headless, watches for the master m3u8; never logs in or passes walls — exit **5** + immediate page when a wall appears) |
| a stable public m3u8 | `static-public` |
| **webinar.net** (`app.webinar.net/<id>`, "Register now" / "Attend") | ⛔ **WebRTC — no HLS/DASH manifest is ever served** (TGT 2026-08-19: registered, Attend clicked, watched 12:00–12:17Z, nothing manifest-shaped). Not capturable by the pipe legs on any host → **descope on the record** unless a browser-audio capture leg exists. The registration "remembered" state needs `--click-text Attend` to reach `/live`. |
| **Notified MMC** (`edge.media-server.com/mmc/p/<hash>`) | registration wall → profile; pre-call the player serves a **15-min holding VOD master** and at call time a **tokenized live master that the pipe legs may not be able to ingest** (TOL 2026-08-19: resolved at 12:31Z, 0 frames, both legs dead at 12:46Z). Resolve with `--reject-endlist --reject-substr <holding-hash> --linger-secs 20` (the `metafile…` master itself yields 0 frames; the `media.main.pro2.mas…` MEDIA playlists the player fetches next ingest fine — 6 s → 192 000 B PCM measured 2026-08-19 — and `--linger-secs` is what gets them printed as `MANIFEST-SEEN`) and ONLY publish a URL that passed `pcm-gate.sh` on the executing host (`url-push-job.sh` tries every seen manifest and ships the first that yields audio). |

**THE AMAT RULE (2026-08-13, a full call lost):** if the player page shows or
can show a registration/login form, the binding is `registered-session` with a
browser profile the registration was done in AT PREP TIME — never `public`:

```sh
# 1. Register ONCE into a persistent profile (headed; standing identity;
#    tick "keep me logged in"; verify the player loads PAST the form):
python3 scripts/browser-resolve.py --slug <lc> --url <player-url> \
  --profile ~/.earshot/profiles/<lc> --headed --wait-secs 120 --no-notify
# 2. Bind with the profile in the resolver argv:
scripts/opsctl.sh bind-access --run-dir <run> --access-mode registered-session \
  --attested-by <operator> --session-ref "browser-profile:$HOME/.earshot/profiles/<lc>" \
  --session-expires-at-utc <call-end+2h> --resolver-kind browser-network \
  --resolver-argv-json '["python3","scripts/browser-resolve.py","--slug","<lc>","--url","<player-url>","--profile","'"$HOME"'/.earshot/profiles/<lc>","--out","$HOME/<lc>-stream.url","--click-play","--wait-secs","6600"]' \
  --url-file '$HOME/<lc>-stream.url'
```

Timing law for this venue family (measured: COHR 20:36Z for a 20:30Z call,
CBRS 20:58Z for 21:00Z, AMAT ~21:13Z): **the manifest is minted AT call
start**, so `--wait-secs` must span fire→start+15 min (T-75 fire ⇒ ≥ 6600).
The AccessPlan `deadline_utc` defaults to starts+15 (ceiling starts+30) and
gates the resolver's retry loop, not just its first attempt.

`bind-access` records a **non-secret** prerequisite and never resolves media
itself; session modes require `--attested-by` + `--session-ref`. AccessPlans
are IMMUTABLE — a binding mistake costs a whole new RunPlan attempt, so get
the mode and `--wait-secs` right the first time. Register for webcasts as
**bdelriom@actinver.com.mx** (standing identity), never the personal gmail.

### Browser-class venue on the BOX — the `preresolved` split (2026-08-14)

The box has no browser and no operator to pass a wall, so a browser-class venue
executing there splits the work: the **laptop resolves and pushes**, the **box
waits and arms**. Bind `preresolved`:

```sh
scripts/opsctl.sh bind-access --run-dir <run> --access-mode public \
  --vantage us-east1/<label> \
  --resolver-kind preresolved \
  --resolver-argv-json '["scripts/urlfile-wait.sh","$HOME/<lc>-stream.url"]' \
  --preresolve-kind browser-network \
  --preresolve-argv-json '<the browser-resolve argv that would have run here>'
```

Four things that bite if you do not know them:

- **`--vantage` is mandatory.** `bind` hard-refuses a defaulted vantage for
  `preresolved` (cross-host by construction) and for a plan compiled for the
  box while bind runs elsewhere. Pick a deliberate label — us-east capture must
  never pool its Δrace with CDMX history.
- **There is no `--expect-plan-sha256` flag.** `bind` synthesizes
  `--deadline-utc`, `--expect-plan-sha256` and `--fresh-secs 5400` into the
  resolver argv itself, so they are hash-covered. Do not pass them by hand; a
  contradicting value is refused.
- **Freshness is attempt identity, not mtime.** The url-file path comes from the
  pinned config, so every attempt of one event reuses it (the AMAT r1/r2 shape).
  `urlfile-wait.sh` keys on a `<url-file>.meta` sidecar naming
  `run_plan_sha256`; mtime is only a sanity bound.
- **The `$HOME/` spelling is literal.** `${HOME}/` and `~/` are refused, because
  `arm-on-url.sh` expands only that one form on the executing host.

Then push **inside the fire window** — not before, or the file ages past
`--fresh-secs` and the wait refuses it:

```sh
scripts/push-url-file.sh --run-dir <run> --resolve     # or --from-file <path>
```

It runs the plan's recorded `preresolve.argv` locally, then publishes the URL
bytes over SSH **stdin** (never argv — tokens are `ps`-visible), tmp + rename,
mode 0644, with a hash readback. It refuses once the run reaches
`capture_ready`, because the config `cat`s the url-file at runtime and a late
push would swap what the detector hears under a running arm.

**S5 is CLOSED (2026-08-19): the push is a SCHEDULED, PCM-PROVEN, MULTI-HOST job.**
Never push by hand and never rely on a session wakeup. For every preresolved
event install one job that (1) runs the plan's recorded preresolve leg, (2)
proves the minted URL yields PCM on this host (`scripts/pcm-gate.sh`, 6 s —
a holding VOD, an empty "metafile" master or a bot-walled manifest FAILS here
and is re-resolved), (3) publishes to EVERY executing host (box over ssh,
laptop natively with `--host local`), (4) pages on success and on final failure:

```sh
scripts/url-push-job.sh --install --slug <lc> --at-utc <start-2min or start+1min> --until-utc <start+14>   --run-dir var/runs/<E-BOX>/<R_BOX>   --run-dir var/runs/<E>/<R_LAPTOP> --host local
# prints INSTALLED + the LOADED launchd readback; disarm line included
```

Choose `--at-utc`: start−2 min when the venue mints the manifest at/just before
start (Q4, webinar-class), start+1 min when it only exists after the call
begins (Notified MMC, holding-VOD venues). `--until-utc` must be ≤ the
AccessPlan `deadline_utc` (start+15). Legs of one event on several hosts go in
ONE job (same url-file basename); different events get different jobs.

**Test the minting leg BEFORE the day** with the same flags the plan records
(e.g. `browser-resolve.py --click-text Attend`, `--reject-endlist
--reject-substr <hash>`): the wall-pass alone is NOT proof; a venue is proven
only when PCM came out of the URL it mints (a past replay on the same player
suffices for the ingest half: `pcm-gate.sh --kind hls --url <replay master>`).

## Phase 6 — ADMISSION (phone lane, Q-ADMIT)

Probe inside the venue's real window — default T-90..T-30; a
`reservation_window = true` venue only exists in its window, and an off-window
no-answer classifies NOTHING (AAPL is the type specimen):

```sh
scripts/bridge-probe.sh --to +1<number> --label <venue-date> [--dtmf 1234#] --secs 120 --promote --issuer <T>
```

- Only `held` auto-promotes, and only when held ≥95% of `--secs`. **Read the
  registry back, never stdout** — a promotion once silently failed on a
  launchd-PATH tomllib error.
- `dropped` / `operator_screened` / `passcode_required` are recorded by hand
  with `scripts/registry-admit.py --issuer <T> --admission <verdict> …`.
- `operator_screened` ⇒ the operator-handler branch: `[operator_handler]
  enabled = true` in the config, rehearse against the fake operator
  (+17622164408), record `operator_handler = "rehearsed-YYYY-MM-DD"`.
- NEVER probe >300 s against an open board (that is a call capture — the SPOT
  2026-08-04 335k-contract loss); never point `bridge-probe`/`chain-capture`
  at a tradeable call at all. Cover an event by ARMING it.
- After a promotion: `scripts/slate-stage.sh --issuer <T>` restages the phone
  lane same-day.

## Phase 7 — STUDY + POSTURE (the money step)

```sh
scripts/opsctl.sh study --date <DATE>        # laptop only — variant-study needs macOS `say`
```

Dead-word resolution is three-state, NO THIRD STATE: widen (proposal only —
adopting it changes `keywords_sha256` ⇒ re-study ⇒ new RunPlan attempt), ack
only measured residue (`configs/.acks/<base>-dead-word.ack` — auto-written,
refuses when ≥⅓ of the board is dead), or descope. "Dropping a dead word
forfeits ONE market; refusing the whole event forfeits ALL of them" (TTWO
2026-08-07: 12 markets / 69,658 contracts forfeited protecting 3 dead words).

Then the step **no automation runs** — set the per-word posture from the study:

```sh
python3 scripts/posture-from-study.py configs/<event>-arm.toml \
  --compile-with state/<EVENT_ID>/earshot-pinned \
  --model-dir models/<the config's model dir> [--low-band-budget N] [--dry-run]
```

- `ratio = confirmed/clips`: ≥ floor ⇒ FILL at 97¢; below ⇒ OBSERVE at 1¢
  (non-crossing — provably cannot fill) unless the operator granted a low-band
  budget. **ratio is not precision; there is deliberately no upper bound.**
- `--compile-with`/`--model-dir` are load-bearing: the short-token (k≤2)
  override cannot run without them and says so (SNAP "game" fired 3× and
  settled NO).
- Read back the printed WORST-CASE notional against `PROBE_LEDGER_CAP_CENTS`
  — that ledger is now the only cumulative bound. **There is no
  `[risk].max_event_notional_cents` any more** (removed 2026-08-18, O8): at a
  97¢ fill price against the old 100¢ cap exactly ONE order per event reached
  the wire, and it went to whichever keyword fired first rather than the
  best-priced one. An event may now place one order per FILL-posture keyword,
  so the worst case is real exposure — read it, do not just check it fits.
- Posture edits change config bytes ⇒ the preflight verdict is void ⇒ re-run
  Phase 8. Do posture BEFORE preflight, not the night the study finishes red.

## Phase 8 — PREFLIGHT + SCHEDULE + GREEN

```sh
scripts/opsctl.sh preflight --date <DATE>
export EARSHOT_LOCAL_EXECUTION_HOST=<stable-laptop-id>    # webcast scheduling refuses without it
export EARSHOT_BOX_EXECUTION_HOST=earshot-useast1         # default; the box side of the route
scripts/opsctl.sh schedule --date <DATE>
scripts/assert-ready.sh configs/<each-event-config>.toml
```

- **Webcast dispatch is host-directed (2026-08-13), not lane-directed.**
  `schedule-nightly` routes each webcast plan on its immutable
  `execution_host_id`: equal to `EARSHOT_LOCAL_EXECUTION_HOST` ⇒
  `install-t0-agents.sh` (launchd, unchanged); equal to
  `EARSHOT_BOX_EXECUTION_HOST` ⇒ `box-webcast-install.sh` (systemd user timer
  on the box, T-75); anything else ⇒ a loud refusal with no installer. Setting
  the two variables to the SAME value is itself a refusal (ambiguous host).
  A box event is compiled for the box at `init` — `execution_host_id` is inside
  `plan_sha256`, so you cannot move a run between hosts, only re-init it.

- Fire-time behavior worth knowing when reading logs (2026-08-13 fixes): the
  resolver RETRIES on a bounded schedule until the AccessPlan `deadline_utc`
  (a failure pages once, then retries every 30 s); `browser-resolve` exit 5 =
  registration wall, paged IMMEDIATELY and retried — an operator can register
  mid-window and the next attempt succeeds. The scheduler plist pins PATH
  (launchd's default python3 has no tomllib/playwright), and its launchctl
  readback accepts Darwin-25's quoted keys — `scripts/install-t0-agents.sh
  --selftest-readback` proves both renderings offline.
- Preflight pins the binary at `state/<E>/earshot-pinned`. **A rerun rebuilds
  from the CURRENT checkout and clobbers the pin in place** — `pinned.info` is
  the only provenance; on 26AUG12 a rerun swapped a green pin for one carrying
  ~2500 uncommitted lines. With three lanes editing this tree, check
  `git status` before any re-preflight and prefer a clean checkout.
- `schedule` installs only `preflight_passed` runs and proves the LOADED
  readback (`schedule-proof.json`). Independently verify the loaded descriptor
  fires in the FUTURE: `launchctl print gui/$(id -u)/<LABEL>` (laptop) or
  `systemctl --user list-timers` (box). A date with zero RunPlans is REFUSED,
  not a green no-op.
- **Box-primary / laptop-failover (operator ruling 2026-08-19: "priority is
  capturing in the box with orders sent from the box; if the box cannot ingest,
  capture and work from the laptop — one or the other sends actual trades,
  never both").** Stage the box leg as its own event id
  (`<EVENT>-BOX-WEBCAST`, config twin with `num_threads = 2` /
  `budget_ms = 500` under `[verifier]`, readiness JSON cloned — `keywords_sha256`
  identical so the study carries over; the HD 26AUG18 pattern), compile it with
  the box identity (`eventctl init --config <box cfg> --event-env <box env>
  --host-id earshot-useast1 --account-fingerprint <the box account sha>`), bind
  it `preresolved` with `--vantage us-east1/<lc>`, dispatch it
  (`box-webcast-install.sh --run-dir <box run> --dispatch-only`), **preflight ON
  the box** (`ssh box 'cd ~/ear && scripts/preflight-event.sh --config configs/<box cfg>
  --account ~/.earshot/<acct>.env --run-dir var/runs/<E-BOX>/<R>'`), pull
  authority back (`pull-box-state.sh --prelaunch --event <E-BOX>`), then
  `box-webcast-install.sh --run-dir <box run>` and read `systemctl --user
  list-timers` on the box. Then wrap the LAPTOP leg's loaded launchd job so it
  arms only if the box is not capturing by start+2:
  `ProgramArguments = ['/bin/bash', 'scripts/laptop-failover.sh', '--event', E,
  '--box-event', E_BOX, '--box-run', R_BOX, '--start-utc', 'HH:MM', '--', <original argv>]`
  (bootout/bootstrap, read back). It stops the box leg (STOP file + unit) before
  the laptop arms. `schedule-nightly` may silently drop a run that sorts after a
  box dispatch — **run `opsctl schedule` again, or `box-webcast-install.sh` the
  second box run directly, and read the timers back** (memory: ssh eats the
  manifest).
- **Live supervision is a LOADED job, never a session wakeup.** Install two
  watchdogs per event day that page when any leg on any host is not
  `capture_ready`: `scripts/event-watchdog.sh --install --slug <lc> --at-utc
  <start+5> --leg <E-BOX>:<R_BOX>:ubuntu@<box> --leg <E>:<R>:local` and again at
  `<start+35>`. Session-scheduled wakeups only fire while the session is idle
  and are worthless as a monitor (2026-08-19: four of them fired at 17:46Z,
  five hours after the calls). The reason is not only idleness: the 2026-08-19
  session was killed at 05:38Z by a model-safeguards flag on an ordinary
  ops turn and stayed dead 12 h with no human to `/model`-switch it — the
  failover install was the pending "next action" and never happened, and
  the pages that DID go out (10 mails, all delivered) had no receiver.
  **Before the agent goes unattended: every push / failover / watchdog is a
  LOADED OS job with a readback; nothing event-critical is left pending on
  the agent's next turn (finish the install before reporting the self-test);
  and the operator has named who receives the pages.**
- Snapshot the green SET so night-before edits cannot strand the event red:
  `mkdir -p state/<E>/green-snapshot && cp configs/<cfg>.toml state/<E>/green-snapshot/config.toml && cp configs/.readiness/<base>.json state/<E>/green-snapshot/readiness.json && cp state/<E>/preflight-result.json state/<E>/green-snapshot/` —
  then `scripts/green-guardian.sh --event <E> --config <cfg>` is the T-90
  restorer.

## Phase 9 — BOX BRANCH (phone events)

At PREP time, not T-60 (a stale box binary at T-60 cost the 26AUG05 race):

```sh
scripts/box-ready.sh --config configs/<event>.toml --account ~/.earshot/<acct>.env   # read-only GO/NO-GO
python3 scripts/phone-lane-derive.py --from configs/webcast-<x>-arm-wc.toml          # webcast twin → phone lane, keywords_sha256 preserved
scripts/box-event-install.sh --event-env configs/<ev>-phone-autorun.env \
  --run-dir var/runs/<EVENT_ID>/<RUN_ID>                                             # --run-dir is REQUIRED now; env-only form is refused
```

`box-event-install.sh` pushes digits over stdin (never argv), asserts the three
ports agree, and re-reads the LOADED unit. See `references/BOX-CHECKS.md` for
every gate and what it defends against.

## Phase 10 — REPORT

One table, one line per event: `READY | NEEDS-TIME | NEEDS-ACCESS |
NEEDS-ADMISSION | NEEDS-POSTURE | DESCOPED(reason)` with the exact next command
for every non-READY row. **READY means, and says, all of:** study green, posture
written, preflight PASS on every executing host, launch job(s) LOADED and read
back per host, push job LOADED naming every host, watchdog LOADED, and the
venue's URL→PCM path proven (live or on a replay of the same player) — anything
less is NEEDS-ACCESS with the missing proof named. Journal what you did per event
(`scripts/journal.sh --event <E> --actor event-prep --action stage|study|readiness|preflight|timer-install|note --outcome ok|fail|warn|refused --detail "..." || true`).
Descopes go on the record: `scripts/opsctl.sh descope <EVENT_TICKER> "<reason>"`.
**Fix, descope on the record, or leave the date command nonzero — silence is
not an outcome.**

## What this skill refuses to do

1. **Never changes risk caps, bands, ledger scope, or `src/risk.rs`** — the
   money-policy items in `docs/SYSTEM-STATE.md` §4 are operator rulings.
2. **Never sets posture by judgment** — only `posture-from-study.py` over a
   fresh, hash-matched study.
3. **Never probes an open board past 300 s and never points a recorder at a
   tradeable call** — arm it or descope it.
4. **Never writes digits** into argv, git, or `admission_detail` — digits live
   in `state/hunt/numbers.env` (0600) only.
5. **Never arms through a red gate** — no `EARSHOT_GREEN_OVERRIDE` unless the
   operator states the one-sentence reason themselves (see the inversion note
   in `references/TRAPS.md`).
6. **Never edits `scripts/`, `src/`, or governed docs** — those belong to the
   reliability lanes under AGENTS.md claims; this skill only invokes them.
7. **Never trusts a file it wrote** — it re-reads the installed job, the
   registry, the readiness artifact, the loaded unit.

## Reference

- `references/TRAPS.md` — every trap that cost real time, with dates, updated
  for the opsctl/RunPlan surface.
- `references/BOX-CHECKS.md` — the box gates and their incidents.
- `docs/runbooks/EVENT-LIFECYCLE.md` — the one procedure this skill drives.
- `docs/SYSTEM-STATE.md` — defect ledger (cite C/E/M/L/O IDs when blocked by a
  known defect).
