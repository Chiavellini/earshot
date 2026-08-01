# EARSHOT — runtime and safety guide

**Start at `AGENTS.md`, `docs/STATUS.md`, and `docs/NODES.md`.** Claim one
node in `docs/work/ACTIVE.md`, then read that node's contract. For a runtime
layer, `docs/ARCHITECTURE.md` maps the exact typed doors and
`docs/layers/<layer>.md` gives the leaf contract.

## Conventions (non-negotiable)

- **A node change is not done until its contract doc matches the code.**
  Runtime types are cited by exact name; keep them true. New nodes ship with
  a registry entry and a contract from `docs/nodes/TEMPLATE.md`.
- **Kalshi API facts live in `VERIFICATIONS.md`** — dated, evidence-backed
  live pins. Never code against remembered API details; if you discover a
  new wire fact, pin it there (with evidence) in the same change.
- Current runtime wiring exists in `pipeline::run_pipeline` (audio chain) and
  `main.rs::arm` (execution side). Do not add a third composer; the roadmap
  migrates both behind an explicit run supervisor.
- Money: integer cents + whole contracts internally; fixed-point strings
  only at the wire (`rest::cents_to_dollar_str` / `count_to_fp_str`).
  Latency: `clock::MonotonicNs` only.
- Risk hard caps in `src/risk.rs` are code constants; config only tightens.
  The kill switch cancels ONLY `OrderRegistry` ids — this account carries
  unrelated resting orders from other systems. Never widen that scope.
- **An unattended path counts as validated only when rehearsed with the exact
  production mechanism**: launchd-fired (not a manual run), killed with the
  real signal (SIGTERM — what `kill`/launchd/traps send — not the STOP file),
  under launchd's minimal env. Equivalent-looking manual drills validated the
  wrong paths before the 2026-07-15 MS run (see `docs/RUNBOOK.md` hardening
  notes + the SIGTERM test `tests/kill_sigterm.rs`). Event runs REQUIRE
  `scripts/preflight-event.sh` the night before (pins the binary; live-probes
  creds, mailer, disk, scheduled wake).
- Live testing: `probe-order`/`kill-drill` are hard-limited to 1¢ ×1
  contract, non-mention markets, $1 cumulative ledger
  (`state/probe_ledger.json`). `--allow-mention` (arm/probe-order) is the
  mention-probe posture: same envelope but Mentions-category permitted;
  probe orders must be non-crossing (guaranteed no fill).
  `--fillable` (requires `--i-am-live --allow-mention`) is the mention-FILL
  posture: orders on Mentions markets may cross the ask up to
  `MENTION_FILL_CAP_CENTS` (50¢/order price bound); real spend stays clamped
  by the config notional caps AND the $1 ledger. Removing the ledger/notional
  clamps (real-size trading) stays gated until M4.

## Quick commands

```
cargo test                                   # unit + mock-integration + e2e
scripts/doc-lint.sh                          # node ownership + docs contracts
EARSHOT_REQUIRE_MODELS=1 cargo test          # model tests FAIL instead of skipping (use on boxes with models/)
cargo test --release -- --ignored perf_      # speed budgets (tests/perf_budget.rs); prints measured numbers
scripts/fetch-models.sh                      # M1 detector models → models/ (gitignored)
cargo run --release -- replay --config configs/example-event.toml
cargo run --release -- bench-detector --config <cfg> --corpus corpus/   # see docs/BAKEOFF.md
cargo run --release -- arm --config configs/m2-drill.toml            # dry-run
cargo run --release -- book --ticker <T> --secs 10                   # read-only
scripts/bridge-probe.sh --to +1… --label <venue-date> [--dtmf 1234#]  # admission probe → dial-ins.toml `admission` (Q-ADMIT; preflight requires a fresh `held`)
scripts/launch-event.sh --config <cfg> --account <env> --bind-port <p> --to +1…  # one isolated event; run per event for concurrent calls (docs/MULTI-EVENT.md §8)
source ~/.kalshi/earshot_env                 # credentials (never in repo)
```

Milestone definitions live in `docs/SPEC.md`; current completion and next
outcomes live only in `docs/STATUS.md` / `docs/ROADMAP.md`.
