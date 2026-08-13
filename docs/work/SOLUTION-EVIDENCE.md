# Work order B — EVIDENCE: every run leaves a complete, canonical tape

worker: evidence-reliability-lane · branch: `webcast-lane` · as_of 2026-08-12
Defect IDs referenced here (E1–E6, L3) are defined in `docs/SYSTEM-STATE.md` §3.

## Mission

Every armed run — laptop or box, webcast or phone — terminates with a complete
evidence set in the canonical `var/runs/<EVENT>/<RUN>/` tree: audio bank, book
tape, order-evidence journal, (FIX) observation tape, report, grade — with
commit receipts, and loudly marked unauditable when it cannot record. This
lane also owns landing the checkpointed reliability program itself: it is the
lane that turns "implemented" into "validated".

## What already exists — do not rebuild

- `src/audio_evidence.rs` (bounded tap, banked prefix == detector-visible
  prefix, two-phase commit receipt, run-fatal on evidence failure),
  `src/kalshi/book.rs` book tape (try_send-only tap, heartbeats, commit +
  `verify_book_tape`), `src/kalshi/order_evidence.rs` v4 (append-only
  accounting authority), `src/kalshi/fix/observation_tape.rs`,
  `src/decision.rs` persistent journal, coordinated-kill teardown ordering.
- `scripts/run-audit.py` (deterministic offline audit),
  `scripts/pull-box-state.sh` (two-tree pull, ledger-prefix relation,
  terminal-only publication), the RunPlan/receipts layer in `src/main.rs`.
- Suites: `tests/audio_evidence.rs`, `tests/order_evidence.rs`,
  `tests/ws_tape.rs`, `tests/fix_observation_tape.rs`, `tests/run_audit.py`,
  `tests/run_host_boundary.py`, `tests/report_on_exit.rs`, `tests/kill_sigterm.rs`.

## Work items (priority order)

1. **E6 — fix the red test and run the matrix.**
   `kalshi::executor::tests::prestage_enumerates_all_tranches` fails on a
   deterministic client-id mismatch (`src/kalshi/executor.rs:1651`). Fix the
   defect (or the test if the intent changed — say which), then run and
   classify the complete verification matrix: `cargo test --all-targets
   --no-fail-fast` with `EARSHOT_REQUIRE_MODELS=1` and `ORT_DYLIB_PATH` set,
   `cargo fmt --check`, `./scripts/doc-lint.sh`, every `tests/*.py` and
   `tests/*.sh` contract suite. Never pipe a gate through `tail` — it masks
   the exit code (this repo has been burned twice).
2. **E1/L3 — validate the canonical mechanism end to end.** The owed
   acceptance chain, in order: immutable commit/release artifact → dry
   rehearsal through the REAL mechanism (launchd-fired, SIGTERM-killed,
   minimal env — a manual run validates the wrong path, see
   `tests/kill_sigterm.rs` history) → operator-approved deployment → real
   loaded-scheduler proof (`scripts/schedule-proof.py`; verify the LOADED
   descriptor, and that its fire time is in the future) → a capture-only
   production fire whose evidence lands complete in `var/runs/` with green
   `scripts/run-audit.py`. COHR 2026-08-12 left only `control/` in the run
   tree — that is the bug you are closing.
3. **E2 — make tape liveness observable.** Every writer (audio bank, book
   tape, order evidence, FIX tape) must expose growth in a machine-checkable
   place: a sidecar progress record (bytes + last-append monotonic ts,
   flushed on a bounded cadence) next to each artifact. The OBSERVABILITY
   lane's monitor consumes it — you provide the contract, document it in the
   owning node docs, and test that a wedged writer is visible within one
   cadence. "Check tape growth, never process existence."
4. **E3 — kill the box invisibility.** After a box run reaches terminal
   state, evidence must arrive on the laptop without operator memory:
   auto-invoke `scripts/pull-box-state.sh` from the box-side terminal
   transition (or a laptop-side watcher), and make every grader
   (`run-audit.py`, `missed-arm-check.py` via its existing box-first reads)
   fail loudly when a box marker exists but no pull has happened, rather than
   reading zero. `scripts/deploy-box.sh:51-54` stays exclude-both-ways; the
   fix is the pull, not the deploy.
5. **E4 — phone-lane parity, proven live-shaped.** Run the phone lane through
   a rehearsal dial (TTS number) with the in-process audio bank mandatory and
   assert the same evidence completeness the webcast lane gets. The 08-11
   events filled $48.50 with no audio banked; that must be structurally
   impossible in both composers.
6. **E5 — FIX tape into the audit.** `run-audit.py` independently validates
   the FIX observation tape (chain continuity, session events vs
   order-evidence transitions) and the tape fingerprint lands in the typed
   RunGrade.

## Owned nodes (claim under these; one open claim per node)

`runtime.assembly` (sole owner of `src/main.rs` + `src/pipeline/mod.rs` —
field cross-lane requests here), `runtime.kalshi.executor`,
`runtime.order_evidence`, `runtime.kalshi.book`, `runtime.kalshi.fix`,
`runtime.decision`, `evidence.capture`, `platform.deploy`.

Files that implies (non-exhaustive): `src/main.rs`, `src/pipeline/mod.rs`,
`src/audio_evidence.rs`, `src/kalshi/{book,executor,order_evidence,rest}.rs`,
`src/kalshi/fix/*`, `src/decision.rs`, `scripts/deploy-box.sh`,
`scripts/pull-box-state.sh`, `scripts/run-audit.py`, `scripts/box-ready.sh`,
`scripts/opsctl.sh` (pull/audit verbs), `examples/book-tape.rs`, their tests.

## Forbidden zones

- `src/source/*`, `scripts/launch-webcast.sh`, `scripts/launch-event.sh`,
  `scripts/redial-supervisor.sh`, `scripts/hls-edge-fetch.py`,
  `scripts/pcm-gate.sh` → CAPTURE lane.
- `src/telemetry.rs`, `scripts/alertctl.py`, `scripts/notify.sh`,
  `scripts/event-status.sh`, `scripts/journal.sh`,
  `scripts/missed-arm-check.py` internals, `scripts/score-precision.py`,
  `scripts/audit-day.py` → OBSERVABILITY lane (you may call, never edit; the
  RunGrade *schema* is yours where it lives in your nodes, the grade *join*
  tooling is theirs — coordinate in the claim comments).
- `src/risk.rs` money constants and every §4 SYSTEM-STATE item — operator
  only. The coordinated-kill code in risk.rs is frozen for this lane; if a
  teardown-order bug needs a risk.rs edit, flag the orchestrator first.

## Rules

- Claim per `AGENTS.md`; contract docs match code before a claim closes.
- `docs/STATUS.md` / `docs/work/ACTIVE.md`: append your own section only.
- No live trading to make a claim look complete: rehearsals and capture-only
  fires use 1¢ observe posture; `--fillable` stays untouched.
- Commits: the acceptance chain requires an immutable release — coordinate
  the actual merge/tag point with the operator; do not push without being
  asked.

## Acceptance

- Full matrix green (or every red classified with an owner) and recorded in
  `docs/STATUS.md`.
- One launchd-fired, SIGTERM-killed rehearsal + one capture-only production
  fire, both leaving complete `var/runs/` evidence, green run-audit, and a
  typed grade — laptop AND box variants.
- A wedged tape writer is detectable from the progress record within one
  cadence (test), and a terminal box run is pulled without human memory
  (drill).
- Verification: `./scripts/doc-lint.sh`, `python3 tests/run_audit.py`,
  `python3 tests/run_host_boundary.py`, `cargo test --all-targets
  --no-fail-fast`, `python3 tests/opsctl.py`.
