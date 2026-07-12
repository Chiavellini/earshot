# EARSHOT — agent guide

**Start at `docs/ARCHITECTURE.md`.** It maps every pipeline layer, the exact
typed door between each pair, and the system-wide invariants. Then read ONLY
the `docs/layers/<layer>.md` for the layer you're changing — the docs are
written so you can work on one layer without reading the rest of the system.

## Conventions (non-negotiable)

- **A layer change is not done until its `docs/layers/<layer>.md` matches
  the code.** Types are cited by exact name in the docs; keep them true.
  New layers ship with a doc from the same template (Role / Entry door /
  Exit door / Obligations / Failure policy / How to edit / How to test).
- **Kalshi API facts live in `VERIFICATIONS.md`** — dated, evidence-backed
  live pins. Never code against remembered API details; if you discover a
  new wire fact, pin it there (with evidence) in the same change.
- Layer wiring exists in exactly two places: `pipeline::run_pipeline`
  (audio chain) and `main.rs::arm` (execution side). Don't add a third.
- Money: integer cents + whole contracts internally; fixed-point strings
  only at the wire (`rest::cents_to_dollar_str` / `count_to_fp_str`).
  Latency: `clock::MonotonicNs` only.
- Risk hard caps in `src/risk.rs` are code constants; config only tightens.
  The kill switch cancels ONLY `OrderRegistry` ids — this account carries
  unrelated resting orders from other systems. Never widen that scope.
- Live testing: `probe-order`/`kill-drill` are hard-limited to 1¢ ×1
  contract, non-mention markets, $1 cumulative ledger
  (`state/probe_ledger.json`). `--allow-mention` (arm/probe-order) is the
  mention-probe posture: same envelope but Mentions-category permitted;
  probe orders must be non-crossing (guaranteed no fill). Real-size mention
  trading is gated until M4.

## Quick commands

```
cargo test                                   # unit + mock-integration + e2e
scripts/fetch-models.sh                      # M1 detector models → models/ (gitignored)
cargo run --release -- replay --config configs/example-event.toml
cargo run --release -- bench-detector --config <cfg> --corpus corpus/   # see docs/BAKEOFF.md
cargo run --release -- arm --config configs/m2-drill.toml            # dry-run
cargo run --release -- book --ticker <T> --secs 10                   # read-only
source ~/.kalshi/earshot_env                 # credentials (never in repo)
```

Milestone state: M0 ✅ M2 ✅ (live-validated) M1 ✅ (sherpa_kws engine +
corpus + bake-off run; holdout results and the pre-declared follow-on —
a custom low-lookahead spotter for the latency bar — in `docs/BAKEOFF.md`).
Next: M3 (Twilio Media Streams dial-in capture — `docs/TWILIO.md`), the
mention-probe live posture (1¢ verification run on a real call), then the
low-latency detector milestone. See the meta-spec for the roadmap.
