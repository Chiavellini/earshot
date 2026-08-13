# Work order C — OBSERVABILITY: we can watch it live and score it after

worker: observability-lane · branch: `webcast-lane` · as_of 2026-08-12
Defect IDs referenced here (M1–M6, C5 paging, E2 consumption) are defined in
`docs/SYSTEM-STATE.md` §3.

## Mission

During a run: one supervised, restartable monitor that sees leg health, tape
growth, detection flow, and drop cadence — and pages a phone (not an inbox)
within a bounded time of anything dying, including on the box. After a run:
the typed grade is complete (precision bound in, FIX fingerprint included),
the day has an audit index, and the missed-arm grader can no longer excuse a
real miss. The 08-11 pattern — a total failure emailed at 20:44 and noticed by
a human question at 20:57 — must be structurally impossible.

## What already exists — do not rebuild

- `scripts/journal.sh` (append-only per-event lifecycle journal, always exit
  0), `scripts/event-status.sh` (snapshot renderer over typed runs +
  journals), `scripts/opsctl.sh status`, `scripts/watcher-liveness.sh`
  (watches the watchers, page-only), `scripts/green-guardian.sh`.
- `scripts/alertctl.py` + `scripts/alert-phone.sh`: proof-bearing alerts —
  deterministic IDs, durable delivery attempts, phone-required channel,
  explicit human ack ("delivery is not acknowledgement").
- `src/telemetry.rs`: 16 latency edges, `PipelineReport` with per-leg `died`,
  drop counts, typed exit reasons, `sigterm_stub_json`.
- `scripts/score-precision.py` (alignment-safe; prints residual spread;
  regression `tests/score_precision.py`), `scripts/settle-score.py`,
  `scripts/race-postmortem.py`, `scripts/audit-day.py`,
  `scripts/missed-arm-check.py` (825 ln, has `--check` self-test),
  `scripts/operator-page.sh` (destination pinned to the config cell).

## Work items (priority order)

1. **M2 — all critical failures page a phone.** Migrate every legacy
   `notify.sh` call site that reports a run-threatening condition (schedule
   failures, webcast lane failures, evidence-finalizer failures, terminal
   failures, watcher-liveness diffs, missed-arm verdicts) onto
   `alertctl.py` with the phone-required channel
   (`docs/nodes/platform-notify.md` §migration is the worklist). An
   unacknowledged critical alert must re-page on a bounded schedule.
   `notify.sh` may remain for informational mail only.
2. **M1 — the live run monitor.** One supervised, auto-restarting monitor per
   armed event (launchd/systemd unit installed at schedule time alongside the
   T-0 agents): tails the journal, reads the EVIDENCE lane's tape progress
   records (E2 contract — consume it, don't implement writers), watches
   per-leg liveness from the report/telemetry stream and redial markers, and
   raises `alertctl` alerts on: no frames past confirm window, tape growth
   stalled one cadence, all-legs-dead, drop-cadence pattern (the ~75–90 s
   operator-queue signature), arm exit before scheduled end. It must survive
   its own death (supervised restart) and being started mid-run. Snapshot
   renderers stay; this is the thing that watches continuously so no human
   tails a log over ssh again (casino143 §IV.12).
3. **M4 — box read-through.** Give the monitor and `event-status.sh` a
   bounded remote read of the box journal/progress records during a run
   (read-only ssh pull on a cadence), so laptop status during a box-hosted
   run is live data with a staleness bound instead of `STALE_PREFIX_POSSIBLE`
   guesswork.
4. **M5 — the grader stops excusing misses.** `missed-arm-check.py`: a lane
   counts only if its config was actually armed (a run/report/journal
   artifact exists), never because a raw prepare output matched the event-id
   glob (casino143 §IV.5); and board-activity checks must consider whether
   the board traded at ANY point in the window, not at check time (§IV.10 —
   ETOR). Extend its self-test with both regression cases.
5. **M3 — precision into the typed grade.** Bind the transcript score
   (`score-precision.py` output, with its residual-spread alignment
   diagnostics) into the typed RunGrade join so run-audit stops reporting
   precision `not_resolvable` on scored runs. Grade schema fields live in the
   EVIDENCE lane's nodes — coordinate in claim comments; the join tooling and
   scorers are yours.
6. **M6 — the day audit index.** Once single-run grades are complete, make
   `opsctl.sh audit-day` produce the date-level index (every event that day:
   COVERED / DESCOPED / MISSED with evidence links), and schedule it after
   `missed-arm-check`'s daily run.
7. **C5 hook — host-contention paging.** Expose the alert the CAPTURE lane's
   host guard raises as a first-class alert type (they detect, you deliver).

## Owned nodes (claim under these; one open claim per node)

`runtime.telemetry`, `platform.notify`, `control.supervise`, `control.grade`,
`evidence.measure`, `evidence.benchmark`.

Files that implies (non-exhaustive): `src/telemetry.rs`, `scripts/alertctl.py`,
`scripts/alert-phone.sh`, `scripts/notify.sh` and its call sites,
`scripts/journal.sh`, `scripts/event-status.sh`, `scripts/watcher-liveness.sh`,
`scripts/operator-page.sh`, `scripts/missed-arm-check.py`,
`scripts/missed-arm-daily.sh`, `scripts/score-precision.py`,
`scripts/settle-score.py`, `scripts/audit-day.py`, `scripts/race-postmortem.py`,
`scripts/opsctl.sh` (status/alerts/audit-day verbs), their tests
(`tests/alertctl.py`, `tests/audit_day.py`, `tests/score_precision.py`,
`tests/operator_escalation.sh`).

## Forbidden zones

- `src/source/*`, launchers, redial supervisor, pcm-gate, HLS fetchers →
  CAPTURE lane.
- `src/main.rs`, `src/pipeline/mod.rs`, evidence writers
  (`src/audio_evidence.rs`, `src/kalshi/**`), `scripts/run-audit.py`,
  `scripts/deploy-box.sh`, `scripts/pull-box-state.sh` → EVIDENCE lane. Your
  monitor READS their progress records and run trees; it never writes into
  `var/runs/`.
- `src/risk.rs` and every §4 SYSTEM-STATE item — operator only.

## Rules

- Claim per `AGENTS.md`; contract docs match code before a claim closes.
- `docs/STATUS.md` / `docs/work/ACTIVE.md`: append your own section only.
- Alert drills page the operator's real cell only with the operator's consent
  in that session; otherwise use the rehearsal numbers. Alerts must be
  idempotent by deterministic ID — a monitor restart must not re-page an
  acknowledged alert.
- The monitor is read-only toward run state: observation must never be able
  to kill, stall, or mutate a live run (journal.sh's always-exit-0 discipline
  applies to the whole lane).

## Acceptance

- A rehearsal run with an induced mid-run failure produces a phone page
  within the bounded time, survives a monitor kill/restart without re-paging
  acked alerts, and the drill is scripted + repeatable.
- A box-hosted rehearsal shows live laptop status with a stated staleness
  bound.
- `missed-arm-check.py --check` covers the never-armed-config and
  traded-earlier-in-window regressions; both graders re-run green on the
  historical 26AUG04/26AUG11 fixtures.
- A scored run's typed grade contains precision + FIX fingerprint; `opsctl.sh
  audit-day` emits the day index.
- Verification: `./scripts/doc-lint.sh`, `python3 tests/ops_surface.py`,
  `python3 tests/alertctl.py`, `python3 tests/audit_day.py`,
  `python3 tests/score_precision.py`, `python3 scripts/missed-arm-check.py --check`.
