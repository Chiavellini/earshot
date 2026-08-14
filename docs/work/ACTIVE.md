---
as_of: 2026-08-14
authority: architecture/claims.toml
---

# Active work

`architecture/claims.toml` is the only machine authority. This page is a human
projection for coordination; if it disagrees with the TOML, fix this page.

The shared branch currently has 47 open claims: 36 `active` and 11
`complete_pending_commit`. Eight of the latter isolate verified formatter-only
collateral by node. A claim remains open until its paths are committed
or explicitly handed off; a green focused suite does not remove collision
protection by itself.

## Reliability integration lanes

| Lane(s) | Primary nodes | Current boundary |
|---|---|---|
| `event-lifecycle-lane` | `project.architecture`, `control.operate`, `control.grade` | Canonical operations manifest/facade, RunPlan ledger, lifecycle runbook, audit integration, and governance reconciliation |
| `typed-scheduler-lane` | `control.supervise`, `control.launch`, `control.operate`, `platform.deploy`, `control.grade` | Implemented and focused-green: exact RunPlan scheduling, loaded-job proof, capture-ready proof, single-launch supervision, terminal-only retrieval |
| `preflight-proof-lane` | `control.acquire`, `control.preflight` | Immutable AccessPlan, typed admission strategy, durable green report and truthful preparation receipts; implemented, claim still open |
| `run-audit-lane` | `control.grade` | Deterministic offline order/book/audio/report audit and local evidence consolidation; FIX tape replay is the remaining boundary |
| `execution-identity-lane` | `runtime.kalshi.auth`, `runtime.assembly`, `control.prepare`, `control.operate` | Stable host/account identity and exact signer selection; implementation claims are `complete_pending_commit` |
| `order-truth-lane` | executor, order transport/evidence | Append-only wire/accounting authority and report integration |
| `decision-reliability-lane` | `runtime.decision` | Persistent one-shot state across restart |
| `book-evidence-lane` | `runtime.kalshi.book` | Exact raw WebSocket tape and finalization |
| `fix-wire-truth-lane` | `runtime.kalshi.fix` | Independent raw FIX observation tape |
| `admission-evidence-lane` | admission, capture, measure, preflight, grade | Probe/current-stream admission, in-process audio evidence, historical measurement |
| `capture-reliability-lane` | `control.launch`, `runtime.source.pipe`, `runtime.source.port` | C1/C2 closed: real `--wait-live` liveness (no ENDLIST + advancing tail + decodable bytes) and a bounded pipe-leg respawn budget. C2b's template edit and C3–C6 sit outside these nodes — see the requests below |
| `box-resolve-lane` | `control.acquire`, `control.launch` | R1–R5 closed: the `preresolved` AccessPlan kind, `urlfile-wait.sh` / `push-url-file.sh`, deliberate capture vantage, and the exported resolver deadline. Δrace vantage pooling lives in `evidence.measure` — see the requests below |

Older phone, TLS, readiness, settlement, expansion, hunt, notification, build,
risk, detector, and source-pipe work remains represented by its existing claim.
Do not infer that those tasks belong to the reliability lane merely because
their paths share the dirty worktree.

## Known shared-tree hygiene

The event-specific configs/evidence and formatter-only Rust changes are now
covered by exact-file claims. Formatter collateral is isolated under eight
node-specific `complete_pending_commit` claims with no semantic intent. The
registry/doc graph is lint-clean; these claims remain until commit/handoff or a
deliberate formatting-only cleanup.

## Claim workflow

1. Read `docs/STATUS.md`, `docs/NODES.md`, the node contract, and the contracts
   at its input/output doors.
2. Inspect `git status` and `architecture/claims.toml`.
3. Extend the one existing open claim for that node with exact files, or create
   one claim if no claim exists. Never use globs or “related files.”
4. Record a stable worker/lane, branch, state, contract-change posture,
   verification commands, and evidence targets before editing.
5. Run focused tests, affected integration tests, `scripts/doc-lint.sh`, and
   the node's documented verification.
6. Remove the claim only after commit/handoff; otherwise leave it
   `complete_pending_commit` for collision protection.

No lane may stage, commit, deploy, dial, page, or trade merely to make a claim
look complete. External operations require their own explicit authorization.

## Evidence reliability lane — 2026-08-12

- E6 is repaired as test-intent drift: checkpoint `bccee1b` strengthened the
  deterministic client-id seed to immutable run identity plus the complete
  logical request, while `prestage_enumerates_all_tranches` still expected the
  earlier event/index/tier seed. The focused regression is green and the
  executor contract now matches the implementation.
- The complete verification matrix is green with required models and the
  sourced ORT dylib: 518 Rust tests passed, 8 ignored performance tests, 0
  failed; all 17 `tests/*.py` / `tests/*.sh` contract suites passed; format,
  doc lint, and diff checks passed.
- A later mandated pre-commit rerun observed one timing-only red outside this
  lane: `runtime.foundation`'s ring unpark test took 18.84 ms under load. The
  same test then passed 10/10 isolated reruns; `src/ring.rs` remains untouched
  under its existing foreign claim.
- A second loaded rerun timed out CAPTURE's new graceful-TERM launcher test;
  its other 24 tests passed and the exact test passed 3/3 isolated reruns. The
  launch implementation and test remain untouched under CAPTURE's claim.
- Next boundary: commit an immutable candidate, then exercise that exact
  artifact through launchd-fired SIGTERM rehearsal before any loaded scheduler
  or capture-only production proof. No deployment, dial, page, or trade has
  been performed by this lane.

## Capture reliability lane — 2026-08-13

Work order `docs/work/SOLUTION-CAPTURE.md`. Nodes claimed by extending the one
existing open claim per node (doc-lint permits no second): `control.launch`
(`phone_tls.launch`), `runtime.source.pipe` (`root.source_pipe_contract`, which
also gains `scripts/test-deaf-leg.py`), `runtime.source.port`
(`phone.source_port`, doc-only).

- **C1 CLOSED.** `--wait-live hls` now requires no `#EXT-X-ENDLIST`, an
  advancing segment tail across two cache-busted polls, and decodable bytes,
  bounded by `--wait-live-timeout-secs` with an `exit 7` refusal. `pcm-gate.sh`
  takes the ENDLIST half too, because `arm-on-url.sh` — the unattended path —
  gates there and never passes `--wait-live`. Evidence: the same
  `#EXT-X-ENDLIST` playlist served over real HTTP made the old gate print
  "stream is LIVE" and arm (exit 0); the new gate refuses (exit 7). Five new
  tests; the gate previously had none.
- **C2 CLOSED.** `PipeSource` carries a respawn budget symmetrical to
  `twilio_ws::max_reconnects` — default 0 (byte-for-byte the old behavior),
  cap 20, launchers export 20/2000 ms for an armed event. Covers the EOF branch
  as well as idle, because a decoder that merely exits returned
  `Ok(EndOfStream)`, which fanout never counted as a died leg. Five new tests
  including a real `kill -9` mid-stream at `run_pipeline` altitude.
- **C2b PARTIAL, by the operator's scope ruling.** The two survivor legs are
  documented as first-class recipes with their MEASURED complementary blind
  spots in `docs/nodes/source-pipe.md`. The template edit is not this lane's.
- **C3–C6 NOT WORKED.** See the boundary requests below.

### Boundary requests from this lane

1. **To EVIDENCE (`runtime.assembly`) + `runtime.config`** — give the respawn
   budget a config-native home: `SourceConfig::Pipe { max_respawns,
   respawn_backoff_ms }` in `src/config.rs` (bounded in the pipe arm of
   `validate`, as Twilio's is) plus two assignment lines in
   `main.rs::build_leg`, exactly the treatment `TwilioStream::max_reconnects`
   already gets. Until then the budget is env-only
   (`EARSHOT_PIPE_MAX_RESPAWNS`, `EARSHOT_PIPE_RESPAWN_BACKOFF_MS`) and an arm
   started outside `launch-webcast.sh` gets the old terminal behavior.
2. **To `control.prepare`** — add the two survivor legs as third and fourth
   `[[sources]]` blocks in `configs/webcast-fanout-arm.toml` and the generic
   stage path. Recipes are pinned verbatim in `docs/nodes/source-pipe.md`
   §"The two survivor legs". This is the remainder of C2b.
3. **C3/C4/C6 cross this lane's node boundary and were left unworked.** The
   work order's file list names paths owned by other nodes:
   `scripts/redial-supervisor.sh` and `tests/operator_escalation.sh`
   (`control.supervise`), `configs/dial-ins.toml` (`control.qualify`),
   `scripts/bridge-probe.sh` (`control.admission`), `scripts/chain-capture.sh`
   (`evidence.capture`). doc-lint refuses a claim naming another node's path,
   so these need a claim on the owning node or a reassignment.

No deployment, dial, page, schedule, or trade has been performed by this lane,
and no money policy was touched.

## Observability reliability lane — 2026-08-12

Work order `docs/work/SOLUTION-OBSERVABILITY.md`; node work stayed within
`platform.notify`, `control.supervise`, `control.grade`, and
`evidence.measure`.

- **M2:** the standalone proof ledger, bounded due re-page, and explicit ack
  are live. Schedule-proof, run-verdict, and daily
  reconciliation paths owned by these nodes page critical failures. Legacy
  callers in foreign nodes remain boundary work.
- **M1 / E2 consumer:** `schedule-proof.py monitor` is supervised,
  restartable, read-only toward run state, and consumes tape-progress,
  telemetry, journal, and redial evidence. It detects no frames, one-cadence
  tape stalls, all legs dead, queue-like drop cadence, and early arm exit.
  CAPTURE still owns installing it beside each launcher.
- **M4:** the monitor has a bounded, byte-capped read-only SSH snapshot with
  explicit staleness. Adding that read-through to `event-status.sh` remains
  with `control.operate`.
- **M5:** actual armed evidence—not a matching prepare/config filename—binds
  posture, and historical event candlesticks determine whether the board
  traded anywhere in the event window. Both regressions are in `--check`.
- **M3:** the scorer publishes and verifies a bound immutable
  `grade/precision-score.json`. The EVIDENCE-owned `run-audit.py` must consume
  that artifact before typed-grade precision is complete.
- **M6:** the day index emits one `COVERED`, `DESCOPED`, or `MISSED` row per
  dated event with evidence links, and the scheduled missed-arm wrapper always
  runs it for the same UTC date with an independent critical alert.
- **C5 delivery hook:** `alertctl.py host-contention` is a first-class
  critical phone request with deterministic host/guard identity. CAPTURE owns
  the guard and invocation.

All mandated offline gates are green. No deploy, push, schedule, real phone
page, or money-policy change was performed.

## Box webcast HOST lane — 2026-08-14 close-out

Work order `docs/work/BOX-HOST.md`; source changes stayed in
`platform.deploy` and `control.preflight`. `scripts/execution-profile.sh`
belongs to the foreign `control.operate` claim and was invoked but not edited.

- **H1 CLOSED.** Real-box `box-ready.sh` against
  `configs/webcast-amat-26aug13-arm-wc.toml` returned GO with 27 PASS, zero
  WARN, and zero FAIL gates. `ffmpeg`, `curl`, `streamlink`, `yt-dlp`, and
  `node` all resolve on the box; the configured streaming-CTC model and Linux
  ORT pass; phone-only Caddy/TLS `/e/<port>` and digit-store checks are
  explicit not-applicable PASSes. Schema 1 remains valid with `stream_url=null`;
  multiline diagnostics stay within one gate record. Evidence:
  `state/box-host-lane/box-ready-amat-go.log` and `.json` on the laptop.
- **H2 PARTIAL.** The owned host mechanism is proven: after a fresh deploy,
  real on-box AMAT preflight built release (`EXIT:0`), wrote
  `state/KXEARNINGSMENTIONAMAT-26AUG13-WEBCAST/earshot-pinned`, recorded
  `provenance=DEPLOYED-FROM`, commit `15285f9be58a`, dirty count 19 and the
  exact pin hash in `pinned.info`, compiled all keywords, and passed Linux ORT
  detector-smoke (`build_ms=706`, `total_ms=814`, `EXIT:0`). Overall preflight
  remained correctly red with exactly one failure: R lane has not yet
  published the required run-scoped AccessPlan. No false `access_acquired` or
  `preflight_passed` receipt was minted. Box evidence:
  `state/box-host-lane/preflight-amat-1.log` plus the event's
  `preflight-build.log`, `detector-smoke.log`, `pinned.info`, pin, and red
  `preflight-result.json`.
- **H3 PARTIAL.** Box `~/.earshot/ms.env` was atomically configured mode 0600;
  `execution-profile.sh resolve` returns `earshot-useast1` and
  `sha256:d13943b5cd4a799fcfb6c9d48b4c7f14a71dd4ff9883494521c18e7da1676399`.
  A new on-box AMAT plan verifies with that identity and
  `plan_sha256=83cb6f8942b7876cc03d621f1aeef9ff8c75a96c08009c0105bbe53cb223746f`.
  The launcher's exact host/fingerprint equality predicates both returned 0,
  and a real `launch-webcast.sh` invocation with `/bin/echo`, default DRY RUN,
  no `--wait-live`, and no `--i-am-live` exited 0. The script itself evaluates
  identity only inside its live branch, so entering that branch for a nominal
  "dry identity check" would violate this lane's no-arm rule. Box evidence:
  `state/box-host-lane/execution-profile.log`, `eventctl-init.log`, and
  `launch-dry-identity.log`.
- **H4 CLOSED.** `fetch-models.sh` ran on the box and skipped checksum-pinned
  artifacts already present. Both configured model directories exist; four
  `libonnxruntime*.so*` files and zero `*.dylib` files were asserted. The
  unparenthesized `find` expression is fixed. Box evidence:
  `state/box-host-lane/fetch-models.log`.
- **H5 CLOSED.** `box-ready.sh` reports parentless ffmpeg/streamlink/yt-dlp/
  cache-busting fetchers without killing them. The real-box result was none.

Two deploys were journaled after explicit dry-run review: 05:19Z at dirty
count 18 and 05:24Z at dirty count 19, both from unchanged HEAD `15285f9` and
with identical before/after source identity. Before any build the box had 8.7
GiB free; after model verification it still had 8.7 GiB. No timer was
installed, no URL was pushed, no dial or arm occurred, and no trade-capable
binary was launched. The real preflight performed its normal mailer test; no
phone page was requested.

Focused verification: shell syntax green; `python3 tests/preflight_proof.py`
15/15 green; `python3 tests/ops_surface.py` 8/8 green; `scripts/doc-lint.sh`
green. Because Deploy 2 also carried schedule-lane edits,
`python3 tests/schedule_proof.py` (15/15) and
`tests/typed_scheduler_boundary.sh` were green before that deploy.

### Boundary requests from HOST

1. **To R (`control.acquire`)** — land the master S-A `preresolved` AccessPlan
   plus `urlfile-wait.sh`/`push-url-file.sh`. Then fresh-deploy and rerun AMAT
   preflight once; it must advance the existing on-box plan from its one
   `plan_compiled` receipt through `preflight_passed`. Do not copy laptop
   `state/` or relax either deploy exclusion.
2. **To `control.launch`** — expose a read-only identity-check mode, or evaluate
   the host/fingerprint equality guard before the live-only branch, so the
   launcher's own guard can be exercised without `--i-am-live`, fake scheduler
   receipts, or an arm binary. HOST did not touch the forbidden launcher.

### Proposed `BOX-CHECKS.md` text (not applied; `.claude/skills/**` is forbidden)

> For a webcast config, `box-ready.sh` requires ffmpeg, curl, streamlink,
> yt-dlp, Node, the configured model, and Linux ORT; Caddy/TLS `/e/<port>` and
> the phone digit store are explicitly not applicable. A GO also reports any
> parentless webcast fetchers but never kills them. `deploy-box.sh` atomically
> writes root `DEPLOYED-FROM` (commit, dirty count, deployment UTC) after rsync
> and Linux-ORT verification. Run webcast preflight only on the box after a
> fresh deploy; its `pinned.info` must cite that stamp. Never push the laptop
> pin or relax the `state/` / `var/runs/` excludes.

## Box webcast SCHEDULE lane — 2026-08-13

Work order `docs/work/BOX-SCHEDULE.md`; changes stayed within
`control.operate` and `control.supervise`, plus the required registry/claim and
close-out surfaces.

- **S1 CLOSED.** `install-event-timer.sh` is lane-aware: phone remains T-25
  and byte-compatible, webcast is T-75 with exact `arm-on-url.sh --run-dir …
  --i-am-live --fillable` argv and `EARSHOT_ACCOUNT_ENV`, and webcast supports
  the same bounded `--fire-at-utc` override as launchd. Both lanes retain
  linger, `TimeoutStopSec=150`, and `KillMode=mixed`; manager readback now also
  requires the exact timer in `systemctl --user list-timers`.
- **S2 CLOSED.** New `box-webcast-install.sh` validates a canonical schema-2
  AccessPlan, refuses `static-public`, leaves typed non-static kinds including
  seam S-E's `preresolved` admissible, enforces the default/configured box host
  and pre-existing executable non-symlink box pin, then reuses private staged
  rsync → host-boundary reconcile → remote timer install → loaded readback →
  box-ledger verification. It has no phone digit, port, or Twilio path.
- **S3 CLOSED.** Schedule proof now keys expected environment on scheduler:
  webcast always requires `EARSHOT_ACCOUNT_ENV`; only launchd requires the
  Homebrew PATH pin. A systemd webcast readback is covered directly.
- **S4 CLOSED.** Webcast dispatch keys on immutable `execution_host_id`:
  local → unchanged launchd installer argv, box (default `earshot-useast1`) →
  box webcast installer, any other or ambiguous host → critical refusal with
  no installer. Phone routing is unchanged. The offline selftest proves all
  four outcomes and sibling continuation.
- **S5 NOT WORKED (stretch).** Lane R's `push-url-file.sh` has not landed in
  this checkout, so this lane did not invent or edit its transport. The
  `control.operate` contract records the required manual push step for
  `preresolved` box events until a later claimed change adds the laptop
  resolve-push launchd mode.

Focused and required gates are green: `tests/typed_scheduler_boundary.sh`,
`python3 tests/schedule_proof.py` (15 tests), `python3 tests/opsctl.py`,
`python3 tests/ops_surface.py`, `scripts/schedule-nightly.sh --check`, `bash
-n` on all three touched shell scripts, `scripts/doc-lint.sh`, and `git diff
--check`. The scheduler test includes a fully offline remote fixture whose
stubs exercise staging, host reconciliation, an existing drill pin, loaded
systemd unit/timer readback, and the durable `scheduled` receipt. No real
scheduler, deploy, network, URL push, dial, page, arm, trade, or event state
was touched.

### Boundary requests from this lane

1. **To RESOLVE (`control.acquire`)** — land and contract-test the
   `preresolved` AccessPlan kind plus `push-url-file.sh`; then S5 may claim
   `install-t0-agents.sh` and schedule that public transport at T-75. Until
   then the manual push step is mandatory.
2. **To the operator skill owner** — update the forbidden
   `.claude/skills/**/SKILL.md` scheduler note to export/configure
   `EARSHOT_BOX_EXECUTION_HOST` and to name the manual preresolved URL push
   during bring-up. This lane did not edit `.claude/skills/**`.
3. **To the integration operator** — after R and H land, rehearse an immutable
   drill artifact through the real box systemd user manager, verify the loaded
   unit/list-timers output, let it fire under minimal env, kill it with real
   SIGTERM, remove the drill unit, and journal both install/removal actions.
   This lane performed only offline fixtures, per the live-safety boundary.

## Box-resolve lane — 2026-08-14

Work order `docs/work/BOX-RESOLVE.md` (master `docs/work/BOX-WEBCAST.md`, seams
S-A/S-E/S-F). Nodes claimed by extending the one existing open claim per node
(doc-lint permits no second): `control.acquire` (`tls.acquire`, which also
gains `scripts/urlfile-wait.sh`, `scripts/push-url-file.sh`, and
`tests/preresolved_arm.sh`) and `control.launch` (`phone_tls.launch`, whose
paths already covered `scripts/arm-on-url.sh` and its contract doc).

**R1 CLOSED.** `preresolved` is a first-class resolver kind whose argv invokes
the new `scripts/urlfile-wait.sh`. Two design corrections came out of the
evidence rather than the plan:

- **Freshness by timestamp cannot satisfy seam S-A.** The url-file path is
  derived from the pinned config, so every attempt of one event reuses it. On
  disk today: `KXEARNINGSMENTIONAMAT-26AUG13-WEBCAST` attempts
  `…-2d71c29ad8ae` (created 18:36:55Z) and `…-2d71c29ad8ae-r2` (18:48:00Z)
  share `$HOME/amat-stream.url` **and** the `20:45:00Z` deadline. Any window
  wide enough to cover the T-75 fire window admits the earlier attempt's file.
  The wait therefore keys on **attempt identity** — a sidecar `<url-file>.meta`
  naming `run_plan_sha256`, cross-checked against `--expect-plan-sha256` that
  `bind` bakes into the hash-covered argv — and mtime demotes to a sanity
  bound. `tests/preresolved_arm.sh` case 2 is exactly this and fails against
  any mtime-only rule.
- **The wait must not invent its own deadline.** `arm-on-url.sh:163` runs the
  resolver with no timeout and only tests the deadline after the child returns
  (`:166`), and the TERM/INT trap is not installed until `:228`. A blocking
  resolver that bounded itself would sail past the call. `--deadline-utc` is
  required; `RESOLVER_DEADLINE_EPOCH` and `EARSHOT_URLFILE_FRESH_SECS` may only
  narrow what the immutable plan fixed, never widen it. Missing deadline is
  exit 2 at T-75, not a 90-minute silence.

Evidence: 12 `--check` cases plus live smoke — blocks then accepts on push
(exit 0), exhausts a 4 s deadline (exit 3), refuses missing deadline and
malformed attempt hash (exit 2), and never prints the URL.

**R2 CLOSED.** `scripts/push-url-file.sh` resolves locally — `--resolve` runs
the argv the plan records, `--from-file` takes an already-minted one — and
publishes to the executing host with the URL bytes on SSH **stdin**: remote tmp
in the target's own directory, `chmod 0644`, rename, then a hash readback that
proves the landed bytes. The AccessPlan gained a `resolver.preresolve` block
(kind + argv of the other host's minting leg) so the push is typed rather than
operator memory at T-75 — the C9 failure mode. It is emitted **only** for
`preresolved` and never as `null`: all five AccessPlans published on disk
re-validate byte- and hash-identically, which matters because
`access_plan_sha256` is baked into `pcm-gate`'s proof and re-checked by
`capture-ready`. A smuggled `preresolve` on any other kind is refused by a
conditional key gate. Evidence: 14 `--check` cases against a stubbed `ssh` that
performs the remote command for real — no URL and no token in any argv, stdout,
or journal; mode 0644; no leftover temp file; a transport failure publishes
nothing. Two refusals worth naming: a push is refused once the run has reached
`capture_ready` (configs `cat` the url-file at runtime, so a late push would
swap what the detector hears under a running arm), and a non-HLS endpoint is a
loud WARN because `pcm-gate.sh:329-334` can never certify it.

**R3 CLOSED (partly `not_resolvable`, deliberately).** Measured 2026-08-14: the
AMAT 26AUG13 `media-server.com` token, minted on the laptop at
`2026-08-13T15:09:56Z`, returned **HTTP 200 from both the CDMX laptop (TTFB
1.151 s) and the us-east-1 box (0.105 s)** at 14 h 02 m of age, with the URL on
stdin via `curl -K -` so it never entered argv. Authorization lifetime is
therefore **≥ 14 h 02 m and not vantage-bound**. The **media-bearing** lifetime
is recorded `not_resolvable`: the manifest is empty because the call ended, not
because the token expired, and separating the two needs a second live
browser-class venue. Consequently `--fresh-secs` is **not** token-derived — the
push-to-arm gap is minutes and authorization outlives it by ~350× — so its
default is the fire window (5400 s = T-75 lead + start+15 min deadline), stated
as a sanity bound. Method and numbers are in the `control.acquire` contract.

**R4 CLOSED, narrower than specified, on evidence.** `bind` refuses a defaulted
`--vantage` for a `preresolved` plan (cross-host by construction) or for a plan
compiled for the capture box while binding runs elsewhere; every other
cross-host default gets a loud WARN and still binds. The blanket refusal was
tried first and **measurably reddened a file this lane may not fix**:
`tests/typed_scheduler_boundary.sh:149-158` (lane S, `control.supervise`) binds
four plans for `earshot-test-host` before exporting an execution host at `:567`,
so an ambient `EARSHOT_EXECUTION_HOST` made lane S's suite fail — reproduced,
then designed out. That suite now passes under a plain environment, a
mismatching `EARSHOT_EXECUTION_HOST`, and both host variables exported.
`tests/access_plan.py` pins both identity variables so these rules cannot pass
or fail by ambient environment.

**R5 CLOSED — a real gap was found, so `control.launch` was touched.** The rig
was written first and went red on it: `arm-on-url.sh:118-121` prints `KEY=value`
lines that bash `eval`s, and `eval "FOO=bar"` creates a shell variable with **no
export attribute**, so `RESOLVER_DEADLINE_EPOCH` has never been visible to any
resolver the tool has ever launched. The fix is one line, `export
RESOLVER_DEADLINE_EPOCH`; no existing resolver reads that name
(`youtube-resolve.sh` takes its deadline positionally, `q4-resolve.sh` computes
its own, `choruscall-resolve.py` and `browser-resolve.py` use `--wait-secs`), so
nothing else changes. Everything else in `arm-on-url.sh` drives a preresolved
plan **unmodified**, which is what the rig proves. `tests/preresolved_arm.sh`
was created new rather than extending the scheduler rig, which lane S owns.

**Seam S-E verified, not assumed.** `install-t0-agents.sh:151` and
`box-webcast-install.sh:98` each refuse only `static-public`, so a
`preresolved` plan is first-class to both. The rig pins the shape of that gate
so a future change to it surfaces here. This answers the typed-scheduler lane's
boundary request #1: the kind and `push-url-file.sh` are landed and
contract-tested, so S5 may now schedule the push.

Gates, all green and each logged to a file with its exit code (never through
`head`/`tail`): `python3 tests/access_plan.py` (24 tests, was 12), `bash
tests/preresolved_arm.sh`, `scripts/urlfile-wait.sh --check`,
`scripts/push-url-file.sh --check`, `bash -n` on all three touched shell
scripts, `python3 tests/capture_ready.py`, `python3 tests/ops_surface.py`,
`python3 tests/opsctl.py`, `python3 tests/schedule_proof.py`,
`tests/typed_scheduler_boundary.sh`, and `scripts/doc-lint.sh` (532 governed
files). Neither new script matches `ops-surface-check.py`'s `TRADE_CAPABLE` or
`DATED` triggers, so no `architecture/operations.toml` entry is owed.

### Boundary requests from this lane

1. **To `evidence.measure` (`scripts/system-analysis.py`)** — R4 makes the
   capture vantage deliberate, but nothing yet *enforces* it: no analysis
   refuses to pool Δrace across vantages, and `capture.vantage` is validated
   for shape only, never compared against anything. Please add a pooling guard
   keyed on `capture.vantage` so a us-east box capture cannot be averaged with
   CDMX history (`scripts/venue-probe.sh:125-129` states the rule in prose
   today). This lane may not edit that file.
2. **To `control.launch`** — the resolver retries to the deadline but
   `pcm-gate.sh` runs exactly once (`arm-on-url.sh:197-203`, exit 4 with no
   retry). For preresolved events that asymmetry is sharp: a URL accepted at
   T-70 whose token has aged out fails PCM at T-69 and the arm is dead while
   the operator holds a good URL. `capture-ready.py`'s
   `MAX_PCM_AGE_SECONDS = 180` budgets the whole push→wait→pcm→ready chain.
   Consider a bounded PCM retry inside the resolver deadline.
3. **To `control.launch`** — `arm-on-url.sh:177,182` formats the deadline for
   its pages with BSD `date -u -r <epoch>`; on the Linux box `-r` takes a file,
   so the guard falls through and the page silently degrades to the literal
   string "the AccessPlan deadline". Cheap to route through `python3`.
4. **To the operator skill owner** (proposed text, not applied — this lane did
   not edit `.claude/skills/**`): `event-prep` needs (a) the browser-class
   box-event bind recipe — `--resolver-kind preresolved --resolver-argv-json
   '["scripts/urlfile-wait.sh","$HOME/<lc>-stream.url"]' --preresolve-kind
   browser-network --preresolve-argv-json '<the browser-resolve argv>'
   --vantage <deliberate label>` — noting that `bind` fills the deadline,
   attempt hash, and freshness bound itself; and (b) the push step
   `scripts/push-url-file.sh --run-dir <run> --resolve` inside the fire window,
   with the note that it refuses once `capture_ready` is reached.
5. **To the integration operator** — the token-lifetime measurement is
   `not_resolvable` for media-bearing life. The next live browser-class venue
   should be measured while the call is still running, from both vantages, to
   close it.

No deployment, dial, page, schedule, arm, order, or trade was performed by this
lane, and no money policy was touched. The only network activity was the R3
measurement: two read-only HTTP GETs of an already-finished call's manifest,
one from each vantage.

## Box-webcast integration rehearsal (§8) — 2026-08-14

Master `docs/work/BOX-WEBCAST.md` §8, run by the integrating operator session
after R/H/S landed (`1bb759d`, `b959c4e`, `144152c`). This lane wrote no
`src/`, no `scripts/`, and no money policy; it added a drill config + event env
under the existing `control.prepare` / `control.supervise` claims and appended
this section.

**Verdict: §8 PASSES through `capture_ready` and the launcher, and is PARTIAL
beyond it.** The break is not a defect in the migration — it is a property of
`arm` that no boardless rehearsal can get around (D4 below).

### §8.2 chain, item by item

| step | verdict | evidence |
|---|---|---|
| drill config, observe posture, no open board | PASS | `board-open.py --issuer BOXDRILL` exit 1 for open **and** `--any-status` |
| RunPlan compiled FOR the box | PASS | `execution_host_id=earshot-useast1` inside `plan_sha256` |
| `preresolved` AccessPlan bound | PASS | first published outside the rig; `state/box-drill-lane/access-bind*.log` |
| deploy | PASS | `DEPLOYED-FROM commit=c2f6f531f9b1 dirty_paths=0` |
| `box-ready` GO for a webcast config | PASS | 27 PASS / 0 WARN / 0 FAIL, `box-ready-drill2.log` |
| **preflight on the box** | PASS | 25/4/0; closes HOST boundary request #1 |
| `opsctl schedule` routes to the box | PASS | real `schedule-nightly` → `box-webcast-install`, `schedule-r5.log` |
| systemd timer LOADED readback | PASS | `TimeoutStopSec=150`, `KillMode=mixed`, no PATH line (S-D correct) |
| fires at a real future T-75 | PASS | timer-fired 17:04:00Z and 17:39:00Z, both natural `starts_at − 75` |
| laptop pushes the url-file | PASS | URL on SSH stdin, `URLFILE-FRESH` on attempt identity |
| PCM proof at fire time | PASS | 189952/192000 bytes, endlist absent, 7 distinct tails |
| `capture_ready` | PASS | receipt + `control/capture-ready.json` |
| `launch-webcast` arms | **PARTIAL** | identity gate, pinned binary and posture all correct; the Rust arm refused at market validation |
| SIGTERM kill drill mid-run | **NOT EXERCISED** | nothing ever attached to kill — see D4 |
| terminal receipts | PASS | `failed` recorded truthfully, no false success |
| `pull-box-state` → graders see it | PASS | 2 terminal runs published with `evidence_pulled` |
| `missed-arm-check` | PASS | exit 0, no drill row — inert by identity design |

§8.3 verification: `cargo test` 522 passed / 0 failed / 8 ignored (30 suites,
`EARSHOT_REQUIRE_MODELS=1` + ORT); `access_plan.py` (24), `schedule_proof.py`
(15), `opsctl.py`, `ops_surface.py`, `preflight_proof.py`, `capture_ready.py`,
`typed_scheduler_boundary.sh`, `schedule-nightly --check`, `urlfile-wait
--check` (12), `push-url-file --check` (14), `doc-lint.sh` (534 files) — all
green. `urlfile-wait`/`push-url-file` selftests ran on the box for the first
time. Every gate logged to `state/box-drill-lane/` with its exit code.

### Defects found

- **D1 — the box/laptop receipt boundary has no supported door, in EITHER
  direction.** Seam S-C mints `preflight_passed` on the box;
  `box-webcast-install.sh:104-114` reads it from the **laptop** ledger; and
  `pull-box-state.sh:634` refuses to publish a nonterminal run. The reverse
  also bites: closing a superseded attempt locally left the box with two active
  runs and on-box preflight correctly refused `ambiguous: 2 active runs`. A
  third instance appeared at grade time — a run whose terminal receipt existed
  only on the box failed local `verify` outright. `run-host-boundary.py:158-164`
  already accepts `incoming_prefix_destination`, so only the transport is
  missing. Worked around with plain `rsync` both ways, journaled as gaps; **no
  receipt was ever hand-written.** This blocks every box webcast event, not
  just browser-class ones, and it is the single biggest obstacle to a
  skill-navigated box event.
- **D2 — `tests/preresolved_arm.sh` case 2 is not load-proof.** Red once while
  `cargo test` saturated the machine (15-min load average 40), then 3/3 green
  isolated. Mechanism confirmed, not assumed: the case binds an 8-second
  deadline, and under load that deadline expires during `arm-on-url.sh`'s
  python-heavy preconditions, so `:79-80` refuses with "deadline has passed"
  (exit 1) instead of the expected resolver exhaustion (exit 3). Product
  behaviour is correct; the rig's budget is not.
- **D3 — the box path forces a live posture with no dry-run door.**
  `box-webcast-install.sh:88-89` refuses any plan that is not
  `execution_enabled`, and `install-event-timer.sh:201` hardcodes
  `--i-am-live --fillable`. There is no way to schedule a box webcast run that
  cannot trade. For this drill the absent board and the 1¢ non-crossing cap
  *were* the entire safety envelope. **Operator ruling wanted before a real box
  event.**
- **D4 — a boardless rehearsal can never exercise the arm.** `arm` validates
  every allowlist ticker at start-up and refuses twice over, measured:
  `api 404: not_found` for a non-existent ticker, and `status is "finalized",
  not active — refusing to arm` for a settled one. Only `status=active` starts
  it. So `source_attached`, a mid-run SIGTERM, and live-arm terminal receipts
  are unreachable under a no-open-board constraint. The operator ruled to accept
  §8 PARTIAL rather than point the drill at an open tradeable board. **SCHEDULE
  boundary request #3 therefore stays OPEN**; closing it needs either an
  active-market drill posture or a fake-Kalshi validation seam.
- **D5 — the failure path raised a critical phone alert.** Correct behaviour on
  a real event, surprising in a drill: `alertctl.py raise --severity critical`
  fired on the terminal `failed`. It was killed before writing any artifact and
  no page was delivered (verified: no alert files anywhere, no `alertctl`
  process).
- **D6 — the decision journal is keyed on the MARKET event ticker, not the run's
  event id.** Retargeting the allowlist made the box open
  `state/decision-prod-KXEARNINGSMENTIONAMAT-26AUG13.jsonl` under a drill whose
  event id is `BOXDRILL-26AUG14-WEBCAST`. It was created 0 bytes and removed; no
  real evidence was touched. A drill that borrows a real event's tickers will
  reach into that event's decision namespace.
- Known-and-hit: the CDMX→AWS SSH transport dropped once mid-chain
  (`ssh: Operation timed out`); a retry cleared it. An unattended box event has
  no retry loop around its operator steps.

### Teardown (readback-proven)

Both drill units removed from `~/.config/systemd/user/` and
`systemctl --user list-timers` reports **no earshot timers loaded**; the
synthetic stream is stopped and port 8099 closed; **zero parentless fetchers**
(the H5 class); `~/drill`, both url-files and the stray decision journal
removed. Drill artifacts kept deliberately: `state/BOXDRILL-26AUG14-WEBCAST/`
and its four `var/runs/` attempts, as the evidence behind this section.

No real event was touched. AS 26AUG18 has no config, plan, timer or state on
either host. No order reached any wire; no board was open at any point.
