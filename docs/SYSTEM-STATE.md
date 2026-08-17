# EARSHOT — state of the system

as_of: 2026-08-12 (post-cleanup; branch `webcast-lane`, checkpoint `bccee1b` + cleanup commits)

This is the standing answer to "what have we built, and what is causing issues."
Authorities it summarizes: `docs/STATUS.md` (current program state),
`docs/runbooks/EVENT-LIFECYCLE.md` (the one procedure),
`architecture/{nodes,claims,operations}.toml` (ownership + surface),
`docs/history/casino143.md` (the frozen 2026-08-11 diagnosis this program answers).
The three parallel work orders that consume this report live in
`docs/work/SOLUTION-CAPTURE.md`, `docs/work/SOLUTION-EVIDENCE.md`,
`docs/work/SOLUTION-OBSERVABILITY.md`.

## 1. What this system is

EARSHOT captures live earnings-call audio (phone bridge or webcast), detects
market keywords with a streaming detector, and places bounded orders on Kalshi
mention markets. Three pipelines: CONTROL (discover → qualify → prepare →
admission → preflight → launch → supervise → grade), RUNTIME (source → preproc
→ chunker → detector → decision → executor), EVIDENCE (capture → corpus →
benchmark → measure). 47 nodes, two composers (`pipeline::run_pipeline`,
`main.rs::arm`), four source lanes (twilio_ws, pipe, replay, sip). Money is
integer cents behind hard caps in `src/risk.rs`; the kill switch is scoped to
`OrderRegistry` ids only.

## 2. What is built and standing

- **The 2026-08-12 reliability program** (checkpoint `bccee1b`, built against
  casino143's diagnosis): crash-aware in-process audio evidence bank
  (`src/audio_evidence.rs` — banked prefix == detector-visible prefix by
  construction), raw in-arm book tape with commit receipts and verification
  (`src/kalshi/book.rs`), order-evidence v4 append-only journal as the
  accounting authority (`src/kalshi/order_evidence.rs`), FIX observation tape,
  persistent decision journal that survives restart (`src/decision.rs` — the
  fix for the 08-11 quadruple-buy), coordinated-kill teardown ordering
  (`src/risk.rs`), immutable schema-3 RunPlan + hash-chained lifecycle
  receipts under `var/runs/<EVENT>/<RUN>/`, proof-bearing alerts with a
  phone-required channel (`scripts/alertctl.py`), deterministic offline run
  audit (`scripts/run-audit.py`).
- **One operational surface**: `scripts/opsctl.sh` (15 commands: surface,
  prepare-day, stage, access, bind-access, study, preflight, schedule, status,
  pull, audit, audit-day, alerts, ack-alert, descope). Four legacy entrypoints
  are hard-retired tombstones (exit 64), enforced by `tests/ops_surface.py`.
- **Recurring discovery is live and autonomous**: slate-daily, hunt-daily and
  stage-nightly launchd jobs discover, hunt dial-ins, and stage new events,
  committing autonomously (observed staging YETI-26AUG13 mid-cleanup today).
- **Prep chain**: stage → readiness study → posture-from-study (per-word
  FILL/OBSERVE caps) → proof-bearing preflight with pinned binary → green
  gate + green-guardian → schedule with loaded-job readback proof.
- **Post-run scoring**: `scripts/score-precision.py` (alignment-safe PRECISION
  command), `settle-score.py`, `missed-arm-check.py`, `race-postmortem.py`.
- **Test floor**: 24 Rust integration suites + ~17 python/shell contract
  suites; doc-lint governance (ownership, claims, citations) green at 516
  governed files after cleanup.

### Proven live (evidence, not claims)

- COHR 2026-08-12 (salvaged, non-canonical): 101,207 frames / 0 ring drops;
  book tape 39,354 frames / 0 recorder drops; 11 detections; 2 one-shot
  intents, both audio-backed true positives; 0 orders under a 1¢ dry posture.
- HIMS 2026-08-10: 9/9 precision (regression-pinned in `tests/score_precision.py`).
- TLN 2026-08-05: phone lane decided 534 ms after first print vs a 3.23 s
  median webcast deficit (~6× compression); phone is 4.1–8.2 s ahead of the
  webcast on the same call (CMG 2026-07-29).
- Lifetime money record: 6 wire orders ever; 2026-08-11 filled $48.50 across 5
  orders **with no audio banked** — the event pair that produced casino143.

## 3. What is causing issues — open defect ledger

Cited; grouped by the three reliability areas plus lifecycle. IDs are stable —
the work orders reference them.

### Capture (stream stays up) — `SOLUTION-CAPTURE.md`

- **C1 CLOSED 2026-08-13** `--wait-live hls` accepted a finished VOD: the gate
  grepped EXTINF only; the trap was documented in
  `scripts/cdn-cachebust-fetch.sh:23-27` (no-ENDLIST + advancing tail +
  decodable bytes is the real liveness test). All three parts now enforced in
  `scripts/launch-webcast.sh`, inside a bounded wait that refuses with exit 7;
  `scripts/pcm-gate.sh` carries the ENDLIST half for the unattended
  `arm-on-url.sh` path. Proof: the same ENDLIST playlist over real HTTP armed
  the old gate (exit 0) and is refused by the new one (exit 7). Five tests
  where there were none. (Residual found 2026-08-13: the gate's decode legs
  were unbounded and wedged on AMAT — closed as **C7**.)
- **C2 CLOSED 2026-08-13** No webcast leg respawn: a leg that hit an HLS
  sequence gap died for good; when all legs died the arm exited. Live proof:
  COHR 2026-08-12 arm exited at 21:35:44Z mid-call, both legs dead on sequence
  gaps; coverage saved only by an out-of-band `hls-edge-fetch.py`.
  `PipeSource` now has a respawn budget symmetrical to twilio_ws's
  `max_reconnects` (default 0 = the old behavior; launchers enable it), and it
  covers the EOF branch too — a decoder that merely exited returned
  `Ok(EndOfStream)`, which fanout never counted as a died leg. Residual: the
  budget is env-configured until `SourceConfig::Pipe` gains the field
  (request filed, `docs/work/ACTIVE.md`), so an arm started outside the
  launchers keeps the old terminal behavior.
- **C3** Redial policy is not venue-derived (casino143 Tier-1 #6), and
  operator-queue escalation requires registry knowledge only the failure
  itself produces (casino143 §III.3); one unanswered operator page still
  kills the lane.
- **C4** Admission probe window is T-90…T-30 for venues whose bridge opens at
  T-30 (casino143 §IV.8).
- **C5** Host contention masquerades as capture failure: FIG lost 11.3% of
  frames at load 150 on 8 cores (`docs/history/POSTMORTEM-26AUG05.md`); COHR
  today showed preproc→chunk p99 4.08 s. No guard exists.
- **C6** Recorder-tool bypass: `bridge-probe` hardcodes `max_reconnects=0`
  and probe caps; pointing a recorder at a tradeable call bypasses every
  hardening (`docs/history/POSTMORTEM-26AUG04.md` — the 335k-contract miss).
- **C7 CLOSED 2026-08-13** Live-gate wedge: `live_decodes` piped
  `cdn-cachebust-fetch.sh` — which never exits by design — into a command
  substitution, and bash waits for every pipeline member; the gate's 3600 s
  deadline is a top-of-loop guard, so one unbounded child froze the gate at
  "need one advance" from 21:13Z until killed at 00:12Z while the AMAT stream
  demonstrably advanced (segments 1219→1585). Both decode legs now run under
  an own-process-group watchdog with unconditional group kill (no orphan may
  hold the venue open); per-leg budget clamps to the gate deadline. Live
  proof: AMAT 2026-08-13; the survived `.live-gate-url` temp file was the
  forensic tell. Three wedge regression tests stub the fetcher with the truth
  (a never-exiting loop) — the old suite's `exit 0` stub is exactly why C1's
  five tests missed this.
- **C8 CLOSED 2026-08-13** pcm-gate cache blindness + wrong predicate: its
  playlist poller (`scripts/pdt-log.py`) re-fetched the identical URL with no
  cache defence, so CloudFront served one frozen body all window ("541642
  bytes decoded but MEDIA-SEQUENCE never advanced" on a live stream) — and
  the predicate used the exact header this CDN freezes while appending, which
  `launch-webcast.sh` already rejects as a signal. Every playlist fetch now
  carries a unique `_cb` + no-cache headers, continuity counts distinct TAIL
  segments (sequence only as a no-segments fallback), and the decode leg got
  the same own-pgroup bound. Proof field `distinct_media_sequences` keeps its
  name (schema stability) and carries the distinct-advance count.
- **C9 CLOSED 2026-08-13** Resolver single-shot, deadline-blind, wall-silent:
  `browser-resolve` watched AMAT's registration form for 110 minutes with no
  page and no wall verdict; `arm-on-url` ran it once and exited 3 with the
  fix minutes away; the AccessPlan `deadline_utc` was parsed then discarded;
  the bound argv carried an unexpanded `$HOME/` token. Now: in-loop wall
  detection across all frames (exit 5, paged IMMEDIATELY, `is_wall` pinned by
  `--check`), heartbeat + click-play retry while waiting, `arm-on-url`
  retries every 30 s until `deadline_utc` (wall verdicts page their own
  message and keep retrying so an in-window human fix still arms), and
  `$HOME/` argv tokens expand exactly as url-files do. Live proof: the real
  AMAT player page now yields `WALL-DETECTED`/exit 5 in seconds. Residual:
  binding discipline is procedure — registration venues bind
  `registered-session` with a prep-time profile (event-prep skill, THE AMAT
  RULE); no code can retrofit a session at T-75.

### Evidence / storage (tape is banked) — `SOLUTION-EVIDENCE.md`

- **E1** The canonical `var/runs/` evidence tree has **never been populated by
  a live run** — COHR's run dir holds only `control/`; all real evidence
  landed in legacy `state/<E>/` paths (`docs/STATUS.md` calls it explicitly
  non-canonical).
- **E2** No growth-based tape liveness check exists anywhere in the tree; the
  "book-tape ran 2d21h past its deadline and went deaf with no close row"
  defect (casino143 §III.5) has a stronger close path in `examples/book-tape.rs`
  but no growth monitor. Rule: check tape growth, never process existence.
- **E3** `scripts/deploy-box.sh:51-54` excludes `state/` and `var/runs/` both
  directions with `--delete`: a box-hosted run is invisible to every grader
  until `scripts/pull-box-state.sh` runs. This has already produced a
  wire-orders-graded-as-zero incident (HIMS 2026-08-10).
- **E4** Phone-lane in-process audio parity is implemented but has never run
  live (casino143 Tier-0 #2).
- **E5** run-audit does not yet independently validate the FIX observation
  tape, and its fingerprint is not in the typed RunGrade
  (`HANDOFF-EVENT-LIFECYCLE.md` §open).
- **E6** Verification debt: `kalshi::executor::tests::prestage_enumerates_all_tranches`
  is red (client-id mismatch, `src/kalshi/executor.rs:1651`) — the program's
  full verification matrix has never been run and classified; the branch is
  240+ commits ahead of `main` with no immutable release artifact. The owed
  acceptance chain: immutable commit → dry rehearsal → operator-approved
  deployment → real loaded-scheduler proof → capture-only production fire.
- **E7 PARTIAL 2026-08-16 (box→laptop closed, laptop→box OPEN)** Pre-launch
  receipt boundary: box and laptop each keep
  their own hash-chained `control/receipts.jsonl`, and no supported transport
  moved a **pre-terminal** receipt between them — `pull-box-state.sh` published
  terminal runs only (`:619,633-635`). Measured three times in the §8 rehearsal:
  the box's `preflight_passed` could not reach the laptop installer; a
  laptop-side close could not reach the box (`ambiguous: 2 active runs`); a
  box-only terminal receipt failed local `verify`. Each was worked around with
  hand `rsync`, which no skill can do and which invites a hand-written receipt.
  Now: `pull-box-state.sh --prelaunch` publishes a run that has **not** reached
  `launched`, over the already-tested direction-agnostic
  `run-host-boundary.py reconcile` prefix primitive. Safe precisely because such
  a run has no `capture/`, `book/` or `orders/` content — only atomically
  written control proofs — so the two-growing-partial-authorities danger the
  terminal-only refusal guards against cannot exist yet. It records **no**
  receipt: `evidence_pulled` requires `completed|aborted|failed`
  (`eventctl.py:94-97`) and inventing a lifecycle transition is a schema change.
  With the prefix on the laptop, `schedule-nightly.sh` and
  `box-webcast-install.sh` pass unmodified; no gate was weakened.
  **Proved end-to-end 2026-08-16** on a fresh drill attempt: box
  `preflight_passed` → `pull-box-state.sh --prelaunch` (no rsync) →
  `opsctl.sh schedule --date` dispatched, installed and read back a LOADED
  systemd timer at the natural T-75.
  **The other direction is still open and is NOT cosmetic — it blocks a
  skill-navigated box webcast event.** Two measured refusals, both reproduced
  on 08-16:
  1. Box preflight hard-refuses without the laptop-bound AccessPlan
     (`REFUSED: AccessPlan is not a regular non-symlink file`), so the run dir
     must reach the box *before* preflight — but `deploy-box.sh` excludes
     `var/runs/` (E3) and `box-webcast-install.sh` only dispatches *after*
     preflight passes. Circular; today only hand `rsync` breaks it.
  2. A laptop-side close does not travel, so the box still sees the superseded
     attempt as active: `REFUSED: ambiguous: 2 active runs`.
  **And the run dir genuinely cannot be re-derived on the box instead of
  transported**: compiling the same config with the same identity on both hosts
  yields byte-different `run-plan.json` because `created_utc` sits inside
  `plan_sha256` (measured: laptop `eb7371cf…` vs box `1e70343c…`, diff = that
  one field). So transport is mandatory, not a convenience.
- **E10 OPEN 2026-08-16** Every re-preflight on the box makes the *next*
  `pull-box-state.sh` refuse the whole event: the box rewrites legacy
  `state/<E>/box/` artefacts (`detector-smoke.log` et al), the puller's
  `assert_compatible_legacy_state` sees genuine divergence, and refuses rather
  than merge in place (`:757`). The refusal itself is correct — it is the E3
  never-merge-unverified rule — but the operator is left hand-moving the stale
  snapshot aside, at exactly the moment (a red preflight being re-run before an
  event) when hands are scarcest. Hit twice in one drill. Needs a first-class
  supersession path for legacy box state, not a weaker check.
- **E8 CLOSED 2026-08-14** `tests/preresolved_arm.sh` was not load-proof **and**
  did not prove its own claim: case 2 asserted exit 3 for "a prior attempt's
  url-file is refused", but *absent* and *stale* also exit 3, and under load the
  8 s deadline expired during the nine `python3` preconditions, producing a
  fourth path to the same code (observed red once at load 40). Now the bind
  happens after setup, the budget is named (`EARSHOT_TEST_DEADLINE_SECS`,
  default 25), the assertions grep the reason (`URLFILE-WRONG-ATTEMPT` /
  `URLFILE-STALE` / `URLFILE-ABSENT`), and `assert_not_rig_timeout` fails
  **distinctly** when the rig lost its own race, so a load flake can never again
  be read as a product failure.
- **E9 CLOSED 2026-08-14** Green-over-broken selftest, found while patching E7:
  `pull-box-state.sh --check` passed with the new `--prelaunch` publication gate
  **deliberately disabled**. Root cause is bash 3.2 (the only bash macOS ships):
  a failing `[[ ... ]]` does **not** trip `set -e`, though a failing `[ ... ]`
  does — so ~15 assertions in that selftest, including pre-existing ones, were
  structurally incapable of failing. All are now `must` / `must_not` helpers
  that exit non-zero, proved red-then-green twice (once by breaking a
  pre-existing assertion that had never been able to fire). Same class as the
  standing rule *never pipe a gate through `tail`, it masks `$?`*. **Rule: in
  any repo shell gate, assert with `must`/`must_not`, never a bare `[[ ]]`.**

### Monitoring / grading (we can see it and score it) — `SOLUTION-OBSERVABILITY.md`

- **M1** There is no supervised, restartable, first-class live run monitor
  (casino143 §IV.12) — live visibility during an event is still ad-hoc tails;
  `event-status.sh` is a point-in-time snapshot renderer.
- **M2** Failure notification on legacy paths is email-first; the 08-11 total
  failure emailed at 20:44 and was noticed by a human question at 20:57.
  Proof-bearing phone-required alerting exists (`alertctl.py`) but schedule,
  webcast, evidence-finalizer, and terminal failures are not migrated onto it
  (`docs/nodes/platform-notify.md` §migration; HANDOFF open item 3).
- **M3** The transcript precision score is not bound into the typed RunGrade;
  audit reports precision `not_resolvable` (`docs/STATUS.md` §open).
- **M4** No live read-through to the box during a run: laptop status returns
  `STALE_PREFIX_POSSIBLE` between dispatch and terminal pull.
- **M5** `missed-arm-check.py` has excused a real miss by counting a
  never-armed prepare output as a lane (casino143 §IV.5) and treats
  point-in-time board checks as durable (§IV.10 — ETOR traded 26,496
  contracts, graded MISSED-ARM the wrong way).
- **M6** No date-level audit index until single-run grade (incl. FIX
  fingerprint) is complete.

### Lifecycle / process

- **L1** Preflight-thrash class: CBRS 2026-08-12 was missed — preflight went
  red at 20:42 through 21:16 and turned green at 21:20:41Z for a 21:00Z call;
  the launcher correctly refused to arm through red. Interrupted night-before
  edits leaving an event red is a recurring class (green-guardian mitigates,
  the class is not closed).
- **L2** An event with no resolved `starts_at` stages a midnight-UTC
  placeholder (`src/prepare.rs:363`, reattached by `scripts/slate-stage.sh`),
  so T-75 jobs schedule into the past — this lost all 5 events on 2026-08-06.
  Verify the LOADED descriptor is in the future, always.
- **L3** Both 2026-08-12 calls were live before their immutable plans could
  reach a loaded scheduled job — the canonical mechanism has not survived
  contact with a real event day.
- **L4** After the config purge, a future d-issuer event config would be
  ungoverned until the re-admit globs return (`architecture/nodes.toml`,
  control.prepare owns comment).
- **L5** The nightly slate/hunt/stage jobs commit autonomously and can land
  mid-work (observed twice during this cleanup) — any long-running repo
  operation must expect concurrent automation commits.
- **L6 CLOSED 2026-08-13** Darwin-25 readback rollback:
  `install-t0-agents.sh`'s launchctl-print regex expected bare calendar keys;
  Darwin 25 renders them quoted (`"Month" => 8`), so a CORRECTLY loaded job
  was torn down as a false mismatch — twice on AMAT day, each requiring a
  manual bootstrap + hand-verified `scheduled` receipt. The regexes now accept
  both renderings and `--selftest-readback` pins quoted + unquoted fixtures
  offline (the readback previously had zero test coverage).
- **L7 CLOSED 2026-08-13** No PATH in the scheduler plist: under launchd the
  fire path resolved python3 to Apple's 3.9 — no `tomllib` (imported
  unconditionally by eventctl/access-plan/capture-ready), no `playwright` —
  so the FIRST live firing of the typed scheduler died in its first seconds.
  The plist env now pins `PATH=/opt/homebrew/bin:...` in the build, the
  readback expectation, and `schedule-proof.py`'s webcast contract (phone
  lane schedules via systemd on the box, whose default PATH is healthy).
- **L8 CLOSED 2026-08-13** Pinned-input circularity + archived-run poisoning:
  `_check_pinned_inputs` refused the very terminal receipt its message
  demanded once a config drifted (three restore-the-bytes dances in one day);
  `verify` had no state awareness, so archived attempts failed forever and
  turned every day-walker row red; `study-nightly` counted archived plans as
  fatal date-wide collisions; `init-date` created successors without closing
  predecessors. Now: closing transitions + the post-terminal audit tail are
  pinned-exempt (chain integrity and the typed gate untouched — a tampered
  archived run still fails verify), `verify` soft-passes closed runs with a
  labeled `pinned inputs superseded` note, the study selector filters
  non-active attempts via the typed reducer, and `init-date` auto-closes
  never-launched drifted predecessors (`blocked`+`archived`, actor `stage`,
  idempotent; LAUNCHED predecessors are an explicit human refusal). Live
  proof: the three archived AMAT attempts now verify OK-with-note and the
  access day-walk reads `pass=1 terminal=3 fail=0` where it read `fail=2`.

### Deliberate behaviours that look like defects — DO NOT PATCH

Surfaced as apparent findings by the 2026-08-14 §8 box-webcast rehearsal and
triaged against the node contracts. Each is a safety property with a test
pinning it. A future agent "fixing" one of these regresses the system.

- **`arm` refuses a config whose markets are not active.** This is
  `runtime.market_validation`'s stated failure contract — *missing/inactive
  markets fail closed*. It is why the drill had to be retargeted at settled
  tickers rather than run against nothing. Fail-closed on market identity is
  the property that keeps a stale allowlist from arming at a live board.
- **The decision journal is keyed on the market event, not the process
  attempt or source lane.** `docs/layers/decision.md:32` — *scoped to the
  trading event, not to an individual process attempt or source lane*. This is
  the dedupe that stopped the 2026-08-11 quadruple-buy; two lanes covering one
  call share one journal **on purpose**. Pinned by
  `tests/e2e_full_chain.rs:294-320`.
- **A terminal run failure raises a critical page.** Correct for a real event;
  it only looked wrong because a drill is not an event.

## 4. Requires an operator ruling (deliberately NOT delegated to the work orders)

Money policy is out of scope for the reliability agents; these stay open until
the operator rules:

- **O1** `daily_loss_cap_cents` is parsed, clamped, and **never read** — there
  is no realized-P&L accumulator (`src/risk.rs:101,157,177`). The $1,000 daily
  backstop is a comment.
- **O2** `PROBE_LEDGER_CAP_CENTS = 13_000` is per-event, not cumulative,
  despite its doc comment (casino143 §IV.7): real exposure is $130 × events.
- **O3** `book_stale_enforce` defaults false (`src/risk.rs:159`): unknown book
  age refuses, known-but-old only warns; COHR showed book_age p90/max 60 s.
- **O4** `MENTION_FILL_CAP_CENTS = 97` sits exactly where depth has twice been
  observed (HIMS 99¢×523, CRWV 98¢×54) and exactly where ev_gate cuts;
  ev_gate has no P(wrong) term and gates nothing (shadow only).
- **O5** No pre-arm liquidity survey and no depth-aware sizing; on 08-11 most
  intents found `best_ask=None` or depth above the cap.
- **O6 OPEN 2026-08-14** The box webcast scheduling path **forces
  `--i-am-live --fillable`**: there is no non-trading scheduled posture. The one
  candidate, `capture_only`, requires zero keywords, so it cannot exercise the
  detector at all — meaning any rehearsal that proves the detector end-to-end on
  the box is, by construction, a fillable posture. The §8 drill was only made
  inert by config (1¢ non-crossing OBSERVE + a settled-market allowlist), not by
  the path. **A real box webcast event is a fillable posture until this is
  ruled.** Closing it spans 7 refusal sites and 5 test assertions across two
  nodes, so it is an operator decision, not a reliability patch.

## 5. Target operating model — the three skills

Everything above converges on a three-skill operator workflow. **Built
2026-08-12** as agent skills: `.claude/skills/event-prep/`,
`.claude/skills/detector-improve/`, `.claude/skills/trade-gap/` — pure
procedure over the existing surfaces (they orchestrate; the work-order lanes
still own the reliability floor they depend on):

1. **Event-prep skill** — input: dial-ins/webcast URLs for upcoming events.
   Scopes the markets, hunts/qualifies admission, stages configs via the
   generic path, runs the keyword readiness study, sets the per-word betting
   posture from measured detectability (`posture-from-study.py`), preflights,
   schedules, and proves the loaded jobs. Wraps: `opsctl.sh prepare-day →
   stage → access → study → preflight → schedule` + `dial-in-intake.py` /
   `bridge-probe.sh --promote`. Depends on: C-lane (liveness gates), L2/L3.
2. **Post-event detector-improvement skill** — input: a finished event.
   Consumes the banked audio tape + transcript + detection record, scores
   precision/recall, mines missed words and false fires into the corpus,
   re-benches the detector (`bench-detector`, `docs/BAKEOFF.md`), and updates
   thresholds/keyword variants at the model level. Depends on: E-lane (tapes
   must exist for every run — this is why per-event `state/` evidence is
   deliberately kept on disk).
3. **Post-event trading-gap skill** — input: a finished event. Consumes
   run-audit verdicts, order evidence, book tape, and race timing
   (`race-postmortem.py`, `drace-wallclock.py`, `settle-score.py`) and emits
   targeted code patches and latency work with A/B evidence. Depends on:
   M-lane (grades must be typed and complete) and E-lane (Δrace needs both
   tapes).

## 6. How work proceeds

Three agents work the three solution files in parallel under disjoint node
ownership, claiming via `architecture/claims.toml` per `AGENTS.md`. Shared
docs (`docs/STATUS.md`, `docs/work/ACTIVE.md`, this file) are append-only
per-agent sections. Money policy (§4) is untouched by all three.

The box-webcast migration program (2026-08-13) follows the same model:
master workflow `docs/work/BOX-WEBCAST.md`, consumed by three parallel
lanes `docs/work/BOX-RESOLVE.md`, `docs/work/BOX-HOST.md`,
`docs/work/BOX-SCHEDULE.md` under the same disjoint-ownership rules.
