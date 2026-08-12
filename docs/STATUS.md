---
as_of: 2026-08-12
authority: current-project-state
---

# Current status

EARSHOT's Rust runtime remains the sound part of the system: typed sources,
preprocessing, detectors, persistent one-shot decisions, wait-free local books,
bounded risk, REST/FIX execution, and coordinated cancellation. The active
reliability program is replacing the improvised operational harness described
in `casino143.md`; it does not change price caps, sizing, detector thresholds,
or listen-only safety posture.

## Reliability baseline now implemented

- Every live attempt has one immutable schema-3 RunPlan and one append-only,
  hash-chained lifecycle ledger under
  `var/runs/<EVENT_ID>/<RUN_ID>/`. Config, environment, host, account identity,
  lane, posture, and all evidence destinations are pinned before launch.
- Both live composers require the RunPlan's in-process audio bank, exact raw
  Kalshi book tape, authoritative order-evidence journal, and (for FIX) raw
  observation tape. STOP/SIGTERM drains and finalizes these before the runtime
  publishes a success report; unresolved evidence makes the run fail loudly.
- Decision state and order accounting survive source reconnection and process
  restart. Order evidence is the accounting authority; report disagreement is
  an audit failure rather than a heuristic choice between counters.
- Phone streams renew a current-stream admission receipt on each CallSid and
  attempt. Unknown/untested venues switch to the operator topology on the first
  qualifying early drop; known screened venues start human-first; a route or
  page is not itself admission.
- Execution identity is a stable non-secret host/account fingerprint derived
  by the same signer selector as `arm`. Host-bound run trees cross through a
  prefix-checked, symlink-refusing boundary; box evidence is published locally
  only for completed/aborted/failed runs.
- Preflight is proof-bearing. Webcast access uses an immutable schema-2,
  non-secret AccessPlan with an explicit public, registered-session, or
  signed-in-session prerequisite and explicitly says current media is not yet
  proven. Phone uses a typed admission strategy (fresh held, rehearsed
  operator-first, first-drop fallback, or reservation fallback). A green
  report is durably written before `preflight_passed` can be recorded.
- The deterministic offline run audit independently validates order, book,
  report, and audio evidence. It reports observed cap-depth exhaustion and
  monotonic timing, never invents competitor/sweep causality. Local webcast
  evidence is consolidated in place; remote evidence retains explicit pull
  provenance. FIX runs independently replay the raw observation chain and REST
  proves the typed not-applicable posture. Precision remains unresolved until
  transcript scoring consumes the committed WAVs.
- Critical run failures have deterministic alert IDs, durable bounded delivery
  attempts/outcomes, a phone-required channel, and explicit human
  acknowledgement. The date audit reconciles every immutable attempt and its
  unresolved alerts while continuing siblings; invalid evidence or an
  unacknowledged alert keeps the date red.

## One operational surface

The supported operator entrypoint is `scripts/opsctl.sh`. Its generic
`prepare-day --date YYYY-MM-DD` chain runs stage, canonical access checks,
study, preflight, and typed schedule for every event on the date; problems
remain nonzero and loud while ready siblings continue. Missing public/session
access prints the exact generic `bind-access` action rather than inviting an
issuer-specific patch. Status, pull, single-run/date audit, alerts,
acknowledgement, and descope are exposed by the same facade.

`architecture/operations.toml` classifies the public commands, internal
adapters, retired live entrypoints, and historical scripts. Normal doc lint
invokes `scripts/ops-surface-check.py`; a new dated or unclassified
trade/schedule-capable top-level script fails. The old `run-arm.sh`,
`arm-when-url.sh`, `ms-morning.sh`, and `wing-autorun.sh` paths now refuse with
exit 64 and name their generic replacement.

The dated files beside `docs/runbooks/EVENT-LIFECYCLE.md` and the long
`docs/RUNBOOK.md` are historical evidence, not procedures. The lifecycle
runbook plus `opsctl.sh surface` are the current operational authority.

## Integration state

Typed schedule and fire-time supervision are implemented and offline-tested:
exact RunPlan jobs, loaded-manager proof, durable `capture_ready`, one launch,
launcher-PID-only shutdown, and no re-arm. The shared schedule proof binds plan
hash, stable host, fire time, loaded argv/environment, and (on systemd)
`TimeoutStopSec >= 150` plus `KillMode=mixed` before recording `scheduled`.

The 2026-08-12 COHR/CBRS live calls were the first attempted production
validation and they did **not** validate that canonical mechanism end to end.
Both calls were already live before their immutable plans could reach a loaded
scheduled job. COHR reached canonical preflight, but its late emergency launch
was dry-run and deliberately has no synthetic `scheduled`, `capture_ready`, or
`launched` receipt. Its old raw-book parser initially rejected valid one-sided
Kalshi snapshots whose empty side key was omitted; the shared parser now treats
an absent side as an empty ladder and the live regression passes. During the
subsequent optimized run the streamlink leg died on one HLS sequence gap while
the tuned ffmpeg sibling continued until venue end. An independent edge
fetcher and a primary WAV recorder preserved the call, and a separate raw book
tape closed at 39,354 frames with zero recorder drops.

COHR's emergency evidence is therefore useful but explicitly non-canonical:
63m48s primary audio, 16m32s recovery audio, transcript/word timestamps, raw
book and public-trade tapes, and a finalized runtime report are consolidated in
`state/KXEARNINGSMENTIONCOHR-26AUG12-WEBCAST/capture/`. The detector processed
101,207 frames with zero ring drops, produced 11 detections and two one-shot
intents, and sent zero orders under the existing 1-cent dry-run posture. Both
intents were audio-backed true positives. The China intent preceded observed
1-cent cap-depth exhaustion by about 15.7 seconds, but the single post-call
clock anchor cannot provide drift-corrected race timing and the book transition
does not establish competitor causality. This run proves that evidence can be
salvaged and compared; it does not prove autonomous production readiness.

## Governance state

The node registry remains structurally healthy: no ownership cycles,
dependency cycles, duplicate claims, broken contracts, heading errors, link
errors, or dial-in digit leaks are known. The shared worktree's pre-existing
event configs/evidence and formatter-only Rust churn are collision-protected by
exact claims, including eight explicit `complete_pending_commit`
formatter-only claims. No broad ownership or claim glob was added.

No deployment, real scheduler installation, phone call, page, or trade has
been performed by this reliability pass. The emergency webcasts contacted
their external media and market-data venues but did not send orders.
Production readiness still requires the focused/integration suites,
architecture test, doc lint, full Rust test matrix, and a future on-time
capture-only canonical fire from an immutable release.

## Deliberately unchanged or still open

- Risk ceilings, book-staleness policy, price caps, and depth-aware sizing are
  strategy decisions outside this reliability work.
- Transcript scoring is implemented for the legacy report path and COHR now
  has an audio-backed 2/2 actionable-intent result. The typed RunGrade still
  needs to ingest a bound transcript score before canonical run precision can
  be called complete.
- Remote status remains explicitly uncertain between dispatch and terminal
  pull (`STALE_PREFIX_POSSIBLE`); a future read-through may narrow that window
  but must not bless a stale laptop prefix.
- Deployment still needs an immutable commit/release artifact; operating a
  dirty shared checkout is forbidden.
- Historical event configs remain evidence. New events must be generated by
  the generic stage path, never cloned and patched before market.
