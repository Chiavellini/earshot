# earshot

Audio-signal latency trader for Kalshi mention markets. The replay, detector,
bounded live-execution, and telephony-capture milestones are implemented; the
current posture and immediate work are tracked only in `docs/STATUS.md`.

New here (human or agent)? Read `AGENTS.md`, then `docs/NODES.md`. The runtime
data plane retains typed doors in `docs/ARCHITECTURE.md`; the same node/contract
discipline now covers discovery, preparation, launch, supervision, evidence,
and grading. External Kalshi/venue facts are pinned in `VERIFICATIONS.md`.

```
cargo test                                                    # unit + mock + e2e
scripts/doc-lint.sh                                           # node/docs/ownership contracts
cargo run --release -- gen-fixture --out configs/fixture.wav
cargo run --release -- replay --config configs/example-event.toml   # M0 pipeline
cargo run --release -- arm --config configs/m2-drill.toml           # + executor, DRY-RUN
cargo run --release -- book --ticker KXBTCD-... --secs 10           # live WS soak (read-only)
cargo run --release -- bench-detector --config configs/bakeoff.toml \
      --corpus corpus/ --engines sherpa_kws --split dev             # M1 harness (docs/BAKEOFF.md)
```

Live order paths (`arm --i-am-live`, `probe-order`, `kill-drill`) are gated:
allowlist + non-mention markets + 1¢ cap + $1 cumulative probe ledger. The
kill switch (`touch state/STOP` or SIGINT) cancels this session's orders
only — measured 140 ms STOP→cancelled.

Measured on production — **M2 acceptance run** (2026-07-10, from a laptop):
full chain fixture-audio → real fill in 231 ms — detection 137 ms
(chunk-size dominated; real speech via sherpa_kws adds ~1 s to word onset,
see the M1 verdict above), decision 8 µs, executor 71 µs, order
build+headers 352 µs (pre-signed/pre-staged), network send→ack 93.5 ms
(region move is M5).

```
edge (µs)             count        p50        p95        p99
capture→preproc          63        125        130        139
preproc→chunk            63     140247     142999     144965
chunk→detect             63        127        132        133
detect→decision           3          2          8          8
decision→exec             1         71         71         71
exec→send                 1        352        352        352
send→ack                  1      93519      93519      93519
capture→ack               1     231211     231211     231211
```

Secrets live in `~/.kalshi/` (env vars point at the PEM), never in the repo.
