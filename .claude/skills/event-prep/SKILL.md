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
| unknown venue page | `browser-network` → `scripts/browser-resolve.py` (headless, watches for the master m3u8; never logs in or passes walls) |
| a stable public m3u8 | `static-public` |

`bind-access` records a **non-secret** prerequisite and never resolves media
itself; session modes require `--attested-by` + `--session-ref`. Register for
webcasts as **bdelriom@actinver.com.mx** (standing identity), never the
personal gmail.

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
- Read back the printed WORST-CASE notional vs `[risk].max_event_notional_cents`
  and `PROBE_LEDGER_CAP_CENTS` — at cap 97 vs a 100¢ event cap exactly ONE
  order reaches the wire.
- Posture edits change config bytes ⇒ the preflight verdict is void ⇒ re-run
  Phase 8. Do posture BEFORE preflight, not the night the study finishes red.

## Phase 8 — PREFLIGHT + SCHEDULE + GREEN

```sh
scripts/opsctl.sh preflight --date <DATE>
export EARSHOT_LOCAL_EXECUTION_HOST=<stable-laptop-id>    # webcast scheduling refuses without it
scripts/opsctl.sh schedule --date <DATE>
scripts/assert-ready.sh configs/<each-event-config>.toml
```

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
for every non-READY row. Journal what you did per event
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
