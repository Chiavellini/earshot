---
as_of: 2026-08-13
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
  are live. Schedule-proof, run-verdict, redial supervision, and daily
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
