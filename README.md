# earshot

Audio-signal latency trader for Kalshi mention markets. **M0** (replay
pipeline + latency chain) and **M2** (Kalshi execution layer) are complete
and live-validated; next milestones are M1 (detector bake-off) and M3
(SIP/HLS live capture).

New here (human or agent)? **Read `docs/ARCHITECTURE.md`** — every layer has
a documented typed door and can be edited in isolation. Conventions in
`CLAUDE.md`. All Kalshi API facts are pinned with evidence in
`VERIFICATIONS.md`.

```
cargo test                                                    # 48 tests: unit + mock + e2e
cargo run --release -- gen-fixture --out configs/fixture.wav
cargo run --release -- replay --config configs/example-event.toml   # M0 pipeline
cargo run --release -- arm --config configs/m2-drill.toml           # + executor, DRY-RUN
cargo run --release -- book --ticker KXBTCD-... --secs 10           # live WS soak (read-only)
```

Live order paths (`arm --i-am-live`, `probe-order`, `kill-drill`) are gated:
allowlist + non-mention markets + 1¢ cap + $1 cumulative probe ledger. The
kill switch (`touch state/STOP` or SIGINT) cancels this session's orders
only — measured 140 ms STOP→cancelled.

Measured on production (2026-07-10, from a laptop): full chain fixture-audio
→ real fill in 231 ms — detection 137 ms (chunk-size dominated, the M1
dial), decision 8 µs, executor 71 µs, order build+headers 352 µs
(pre-signed/pre-staged), network send→ack 93.5 ms (region move is M5).

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
