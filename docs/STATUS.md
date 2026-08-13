---
as_of: 2026-08-13
authority: current-project-state
---

# Current status

EARSHOT's Rust runtime remains the sound part of the system: typed sources,
preprocessing, detectors, persistent one-shot decisions, wait-free local books,
bounded risk, REST/FIX execution, and coordinated cancellation. The active
reliability program is replacing the improvised operational harness described
in `docs/history/casino143.md`; it does not change price caps, sizing, detector thresholds,
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

The dated day runbooks (now archived in `docs/history/`) and the long
`docs/history/RUNBOOK.md` are historical evidence, not procedures. The lifecycle
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

## Evidence reliability lane — E6 verification checkpoint

E6 is closed in the working candidate as stale test intent, not an executor
identity defect. The checkpointed runtime intentionally scopes deterministic
client ids to immutable run identity and the full logical order request; the
unit assertion now pins that exact seed, and `docs/layers/executor.md` records
the same contract.

The 2026-08-12 complete matrix is green: `cargo test --all-targets
--no-fail-fast` with `EARSHOT_REQUIRE_MODELS=1` and the sourced
`ORT_DYLIB_PATH` passed 513 tests with 8 ignored performance tests and zero
failures; all 17 Python/shell contract suites passed; `cargo fmt --check`,
`scripts/doc-lint.sh`, and `git diff --check` passed. This is offline
validation only. A subsequent pre-commit matrix rerun had one classified
`runtime.foundation` timing flake (`unpark_wakes_parked_consumer_quickly`,
18.84 ms); the earlier full run was green and 10/10 isolated reruns passed.
The ordered production acceptance chain remains immutable
artifact, launchd-fired SIGTERM rehearsal, operator-approved deployment,
future loaded-scheduler proof, and capture-only canonical fire with green
run-audit.

## Capture reliability lane — C1 and C2 closed

Two defects that could each lose an event on their own are fixed, tested, and
their contract docs updated (`docs/work/SOLUTION-CAPTURE.md`).

**C1 — the live gate can no longer be fooled by pre-recorded audio.**
`--wait-live hls` decided liveness by grepping the manifest for `EXTINF`, which
a FINISHED VOD carries on every segment; the CBRS venue serves exactly such a
15-minute VOD from a sibling host. LIVE now requires no `#EXT-X-ENDLIST`, a
segment tail that advances between two cache-busted polls, and decodable bytes,
inside a bounded wait that refuses with `exit 7` instead of spinning forever.
Advancement is measured on the segment entry rather than
`#EXT-X-MEDIA-SEQUENCE`, because a CDN froze that header on a stream that was
still running — the opposite error, equally expensive. `pcm-gate.sh` enforces
the ENDLIST half as well, since `arm-on-url.sh` is the unattended path and
never passes `--wait-live`. Evidence: the same ENDLIST playlist served over
real HTTP made the old gate print "stream is LIVE" and arm (exit 0); the new
gate refuses (exit 7). The gate had no test at all before this; it now has
five.

**C2 — a webcast leg that dies mid-call comes back.** COHR 2026-08-12 exited
the arm at 21:35:44Z with both legs dead on HLS sequence gaps. `PipeSource`
now carries a respawn budget symmetrical to the phone lane's
`max_reconnects` — default 0, so every replay/bake-off/probe caller keeps the
old terminal behavior, and `scripts/launch-webcast.sh` turns it on for an armed
event and echoes the posture into the launch log. Sequence numbers stay dense
across respawns. The EOF branch is covered as well as the idle one: a decoder
that merely exited returned `Ok(EndOfStream)`, which fanout never counted as a
died leg, so that death was invisible as well as unrepaired.

Still open in this area: C2b's template edit belongs to `control.prepare`, and
C3–C6 name files owned by `control.supervise`, `control.qualify`,
`control.admission` and `evidence.capture` rather than this lane's nodes. All
four requests are recorded in `docs/work/ACTIVE.md`. The respawn budget is
env-configured until `runtime.config`/`runtime.assembly` give it a
`SourceConfig::Pipe` home, so an arm started outside the launchers still gets
the old behavior.

Offline validation only. No deployment, dial, page, schedule, or trade was
performed by this lane, and no money policy was touched.
