# earshot

Audio-signal latency trader for Kalshi mention markets. See the meta-spec for
architecture; **M0 (skeleton)** is complete: a replayed WAV flows through the
ring-buffer pipeline (source → preproc → chunker → detector → decision) with a
full monotonic timestamp chain and a per-stage latency report.

```
cargo run --release -- gen-fixture --out configs/fixture.wav
cargo run --release -- replay --config configs/example-event.toml
cargo test
```

Sample report (M1's real detectors replace the energy stub; `preproc→chunk` is
chunk accumulation — the §3 stage-4 latency dial, 160 ms chunks here):

```
event AAPL-Q3-2026  chunk 160 ms  frames 500  chunks 63  detections 3  drops 0

edge (µs)             count        p50        p90        p95        p99        max
capture→preproc          63        150        154        154        205        205
preproc→chunk            63     139984     143654     146800     150601     150601
chunk→detect             63        151        157        158        159        159
detect→decision           3          1          8          8          8          8
capture→decision          3     138805     139853     139853     139853     139853
```

Layout: `src/source/` (AudioSource adapters — replay now; SIP/HLS in M2/M3),
`src/pipeline/` (stages), `src/detector/` (M1 engine seam), `src/decision.rs`
(keyword state machine), `src/kalshi/` (M2 execution seam), `src/telemetry.rs`
(latency histograms + report.json).

**VERIFICATIONS.md** records all resolved `[VERIFY]` items against live Kalshi
docs/API (order transports, rate limits, signing scheme incl. the measured
±30 s skew window, mention-market early-close mechanics, fees, demo parity).
Kalshi code must cite that file. Re-run the probe with:

```
source ~/.kalshi/env
cargo run --example sign_probe --features probe -- live   # read-only GETs + WS subscribe
```

Secrets live in `~/.kalshi/` (never in the repo). Drop policy: only the
ingress ring drops (loudly, counted); interior stages apply backpressure.
