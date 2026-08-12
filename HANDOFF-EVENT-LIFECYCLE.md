# EARSHOT reliability-systematization handoff

## Objective and scope

This is the continuation registry for the reliability program requested after
`casino143.md`. The goal is one repeatable event procedure that does not depend
on issuer/date shell patches or operator memory, and a durable evidence record
from which latency and accuracy can later be improved.

This pass did not change risk caps, order sizing, detector thresholds, price
policy, or listen-only safety. It did not deploy, install a real scheduler,
dial, page, trade, stage, or commit. The shared tree was already dirty; every
governed dirty path is now protected by an exact node claim.

## Current authorities

1. `AGENTS.md` and `architecture/claims.toml` — ownership/collision authority.
2. `architecture/nodes.toml` — node and typed-door topology.
3. `architecture/operations.toml` — canonical public/internal/retired surface.
4. `docs/STATUS.md` — current project state.
5. `docs/runbooks/EVENT-LIFECYCLE.md` — the procedure every event follows.
6. Node/layer contracts — implementation behavior at each door.
7. `casino143.md` — frozen 2026-08-11 diagnosis, not current status.

## Implemented reliability baseline

| Boundary | Result | Focused evidence |
|---|---|---|
| Run identity/lifecycle | Immutable schema-3 RunPlan, deterministic RunId, strict hash-chained/fsynced receipts, typed joins/terminals, no migration of old attempts | `tests/eventctl_lifecycle.py` 39/39 |
| Execution identity/host boundary | Stable non-secret signer+host identity; exact prefix transfer; symlink/divergence/collision refusal; terminal-only retrieval | profile 8/8; host boundary 6/6 |
| Decision continuity | Append-only one-shot decision journal with lease/replay and fail-closed health | focused decision tests |
| Audio evidence | Mandatory post-preprocessor raw/frame/WAV evidence and atomic commit; banked prefix equals detector-visible prefix | audio evidence 14/14 plus pipeline tests |
| Book evidence | Exact raw WebSocket frame tape, quiet-market heartbeats, bounded drain, committed terminal summary | WS tape/book tests |
| Order/FIX wire truth | Order-evidence v4 is accounting authority; raw REST/FIX observations retained; report disagreement is fatal | order and FIX focused suites |
| Runtime finalization | Audio/book/order and conditional FIX writers mandatory; STOP/SIGTERM coordinate cancel, drain and finalizers; one identity-bound report | runtime/e2e/report/kill suites |
| Admission | Current CallSid/attempt/challenge binding; fresh-held auto receipt; known screened human-first; unknown first-drop latches operator topology | held receipt tests; escalation 15/15 |
| Preflight | Immutable AccessPlan/typed phone strategy; durable report before truthful receipts; missing identity is failure | AccessPlan 8/8; preflight proof 10/10 |
| Scheduler/fire time | One exact RunPlan job; loaded launchd/systemd readback proof; T-75/T-25; durable PCM/capture proof; exactly one launch; launcher-PID-only teardown; no re-arm | scheduler boundary PASS; schedule 6/6; capture 5/5; launcher 26/26 + 21/21 |
| Descope | Exact market-event RunPlans prevalidated and closed with typed `descope` before legacy projection; ambiguous/launched/partial selections refuse | `tests/opsctl.py` 10/10 |
| Status | RunPlan+slate union; typed corruption is loud; scheduled active remote runs say `STALE_PREFIX_POSSIBLE` and return nonzero | lifecycle suite plus shell syntax |
| Audit | Deterministic order/book/audio/report validation; exact detection/wire/depth timing; immutable grade; local evidence consolidation vs explicit remote pull provenance | `tests/run_audit.py` 13/13 before FIX-tape follow-up |
| Operator surface | Every public invocation starts `scripts/opsctl.sh`; `prepare-day` aggregates stage→study→preflight→schedule; unsafe legacy paths are inert; surface checker is part of doc lint | ops surface 7/7; opsctl 10/10 |

## Canonical operation

```bash
scripts/opsctl.sh prepare-day --date YYYY-MM-DD
scripts/opsctl.sh status YYYY-MM-DD
```

Use `scripts/opsctl.sh surface` for the complete public command list. Backing
launchers, proof writers, and OS installers are internal adapters. The retired
`run-arm.sh`, `arm-when-url.sh`, `ms-morning.sh`, and `wing-autorun.sh` paths
exit 64 and must never be revived for one market.

One attempt lives under:

```text
var/runs/<EVENT_ID>/<RUN_ID>/
  control/run-plan.json
  control/receipts.jsonl
  control/{access-plan,preflight-proof,preflight-report,schedule-proof}.json
  control/{pcm-proof,capture-ready}.json
  capture/{audio.raw,audio.frames.jsonl,audio.manifest.json,audio.commit.json,wav/}
  book/kalshi-tape.jsonl
  orders/{order-evidence.jsonl,fix-observations.jsonl}
  report/report.json
  grade/verdict.json
```

## Live validation on 2026-08-12

The COHR and CBRS calls were an emergency test of this baseline, not a clean
acceptance run. Both events were already live before the canonical scheduler
could be installed. No receipt was invented to hide that fact, and no order
was sent. The existing event configs retained their one-cent observing caps
because no new execution price or event-loss authority was supplied.

COHR reached schema-3 preflight but never reached canonical scheduling. Its
emergency detector exposed a generic book-parser defect: Kalshi legitimately
omits the array for an empty side of a one-sided snapshot. The parser now maps
only an absent side to an empty ladder (a present non-array remains malformed),
with a live-shaped integration regression. The optimized dry run then processed
101,207 frames with zero ring drops. One streamlink leg died on an HLS sequence
gap; the tuned ffmpeg sibling continued to venue end. This is a run failure,
not a clean success, even though the redundant path retained coverage.

Independent evidence was consolidated under
`state/KXEARNINGSMENTIONCOHR-26AUG12-WEBCAST/capture/`:

- `call.wav`: 3,828.0185 s, 16 kHz mono PCM, SHA-256
  `5146355e55354c44a4fd13c206885dad2dc7508eccc52c18b6f1cfb4fa8c1e40`.
- `recovery-edge-part2.wav`: 991.9565 s, plus the exact segment-arrival/fetch
  logs. The fetcher closed after 248 segments with zero segment drops/errors;
  four transient poll failures recovered in a total six seconds of deaf time.
- `book-tape.jsonl`: 39,354 raw WebSocket frames, 14/14 full snapshots,
  monotonically increasing sequence, zero recorder drops, clean SIGINT close,
  SHA-256
  `e4a4d003a6260a838d948b9ed4a239096464c611c1ec8aa58ff6fef352a8b3d4`.
- `report.json`: 11 detections, two one-shot intents, zero send attempts and
  zero acknowledgements. Multi-rail detected in an empty/no-ask book. China
  detected with 86.970 ms detector latency while the last observed one-cent
  ask held 48 contracts; that cap depth disappeared 15.723 s later and the
  first next public trade printed 15.801 s after detection.
- `transcript.txt`, `transcript.words.json`, `precision-score.txt`, and
  `live-audit.json`: the two actionable intents both align to real utterances
  (2/2 within a +/-20 s adjudication window). The repaired precision scorer
  found the 2,208.6 s recorder-to-detector offset with 3.9 s residual spread.

The timing record is intentionally qualified: the emergency recorder missed
the required start clock anchor, so its one post-call anchor cannot correct
oscillator drift. The observed cap-depth transition is not labelled a
competitor sweep. The CBRS event and its generic control-plane fixes were
delegated exclusively to the `cerebras-live-lane`; use that lane's final
artifact report rather than inferring CBRS status from COHR.

## Reliability invariants now enforced

- A live launcher consumes exactly one verified schema-3 RunPlan.
- Every live run banks detector-path audio, the Kalshi book, and authoritative
  order evidence; FIX runs additionally bank raw FIX observations.
- Missing/unhealthy evidence is a run failure, never a warning.
- Order accounting comes from the order-evidence journal; derived report
  disagreement fails audit.
- Restarts do not reset one-shot decisions or overwrite the attempt namespace.
- A page/bridge/stream is not admission. Only current-stream proof opens it.
- Operator first-drop paging is bounded and the bridge is dialed only after the
  human joins; explicit assertion is still required.
- Scheduler proof comes from the loaded manager representation, not a file on
  disk. Fire-time capture proof precedes the only launch.
- STOP/TERM reaches the launcher first; scheduler/supervisor group kills and
  automatic re-arm are forbidden.
- A typed descope makes every selected pre-launch RunPlan terminal before any
  compatibility projection is written.
- Remote status uncertainty is explicit; a stale laptop prefix is nonzero.
- Book depth movement is never mislabeled a competitor sweep without evidence.

## Governance state

`scripts/doc-lint.sh` is green at the latest reconciliation: 48 nodes, 53
contracts, 47 exact open claims, 668 governed files, zero dependency cycles.
There are 36 active and 11 `complete_pending_commit` claims. Eight of the latter
isolate byte-for-byte formatter-only collateral by node; they carry no semantic
intent and remain only because this handoff is uncommitted.

Event-specific configs and captured evidence remain historical artifacts under
exact claims. They are not the template for a future event.

## Work still open

1. Complete independent offline validation of the raw FIX observation tape in
   `run-audit.py` and include its fingerprint/summary in RunGrade. Missing,
   truncated, mutated, or commit-mismatched FIX evidence must fail; REST must
   prove the not-applicable posture.
2. Bind the transcript score into typed RunGrade. The legacy scorer now handles
   arbitrarily early recorder starts, normalizes ASR punctuation, and refuses
   ambiguous offsets; COHR proves the standalone scorer, not the canonical
   grade join.
3. Make general dead-lane alerting proof-bearing. Operator first-drop calls are
   bounded/retried, but schedule, webcast, evidence-finalizer, and terminal
   failures still need durable `required/delivered/acknowledged` semantics and
   a bounded phone/SMS fallback.
4. Consider remote status read-through. Until then the current honest behavior
   is `STALE_PREFIX_POSSIBLE` nonzero after remote scheduling and before pull.
5. Add a date-level audit/index only after the single-run grade (including FIX)
   is complete. It must continue siblings and fail the date if any run is
   invalid without rewriting immutable grades.
6. Run the complete repository verification matrix and classify every failure.
7. Production readiness still requires an immutable commit/release, a dry
   rehearsal, operator-approved deployment, real loaded-scheduler proof, and a
   capture-only production fire. The 2026-08-12 emergency starts did not meet
   those acceptance conditions.

## Verification matrix for final reconciliation

```text
python3 tests/eventctl_lifecycle.py
tests/execution_profile.sh
python3 tests/run_host_boundary.py
python3 tests/access_plan.py
python3 tests/preflight_proof.py
python3 tests/schedule_proof.py
python3 tests/capture_ready.py
tests/typed_scheduler_boundary.sh
python3 tests/ops_surface.py
python3 tests/opsctl.py
python3 tests/run_audit.py
tests/operator_escalation.sh
cargo test --test launch_event_posture --test launch_webcast_posture
cargo test --test architecture_contract
cargo test --all-targets
cargo fmt --check
scripts/doc-lint.sh
git diff --check
```

Do not interpret an offline-green suite as production-mechanism validation.
Do not stage, commit, deploy, dial, page, or trade from this handoff without
the corresponding explicit authority.
