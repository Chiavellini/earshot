# M1 detector bake-off — protocol, corpus, and pre-registered criteria

Not a pipeline layer — this documents the benchmark harness (`src/bench/`),
the ground-truth corpus, and the evaluation protocol. The detector layer
contract lives in `docs/layers/detector.md`.

## Why the protocol is strict

A keyword detector tuned and evaluated on the same audio will look better
than it is; a latency number measured on lossless replay is fiction. Every
rule below exists to keep the reported numbers honest (quant-audit
discipline: pre-registered criteria, dev/holdout split, negative controls,
all grid cells reported).

## Corpus

```
corpus/
  raw/               # original downloads, any rate/codec — GITIGNORED
  telephony/         # processed 8 kHz mono s16 WAV — GITIGNORED
  manifests/*.toml   # ground truth — COMMITTED
```

- Sourced from **public earnings-call replays across the Kalshi
  earnings-mention universe** — mix of mega-cap and smaller issuers so
  accents, call quality, and operator intros match production, not just
  polished big-tech IR streams. Target ~5–10 h, ~100–300 keyword
  occurrences, plus ≥1 call containing **zero** target keywords (the
  false-alarm floor).
- Telephony processing (`scripts/corpus-process.sh`): mono → 300–3400 Hz
  band-limit → 8 kHz → G.711 µ-law round trip (matches the M3 SIP dial-in
  path). Engines are NEVER benchmarked on clean speech.
- Ground truth: forced alignment (faster-whisper word timestamps) on the
  **wideband originals**, keyword extraction per VERIFICATIONS.md Q4
  semantics, then human spot-check of a ~20 % random sample to measure
  aligner error (`scripts/corpus-verify.py`).

### Manifest format

```toml
[call]
id    = "AAPL-Q2-2026"
audio = "telephony/aapl-q2-2026.wav"   # relative to corpus root
split = "dev"                          # or "holdout" — PRE-COMMITTED

[[occurrences]]
phrase   = "recession"                 # must equal a config keyword phrase
onset_ms = 812345
end_ms   = 813420
```

`split` is chosen when the manifest is created, BEFORE any engine has run on
the call, and never flipped afterwards. Manifests are read only by the bench
scorer — never by the detector or config compiler.

## Harness

```
cargo run --release -- bench-detector \
  --config configs/<bench>.toml --corpus corpus/ \
  --engines stub,sherpa_kws --chunk-ms 80,160,320 \
  --split dev [--paced] [--calls id1,id2] --out bench-report.json
```

Two passes per grid cell:
1. **Accuracy** (default, replay speed=0 — lossless, hours of corpus in
   minutes): detections match occurrences when `audio_pos_ms ∈
   [onset−250 ms, end+1500 ms]` (post-window absorbs the ~0.4 s engine
   decode lag), greedy one-match-per-occurrence-per-tier. Yields per-tier
   recall, precision, FA/hour.
2. **Latency** (`--paced`, speed=1.0 — real-time, subset via `--calls`):
   onset→emit = `emit_ts − (first_capture_ts + onset_ms)`, reported
   p50/p95/max per tier. This is the number that matters for the trade.

Self-test (no model, no corpus): `tests/bench_fixture.rs` — tone fixture +
stub detector + `FixtureSpec::occurrences()` ⇒ recall 1.0, FA 0, paced
latency within one chunk.

## Pre-registered holdout criteria (M1 sign-off)

Written BEFORE the holdout run; tuning happens on dev only; the holdout is
run ONCE with the frozen config. All grid cells are reported, not the best
one — a config that only wins at one chunk size is fragile, and that gets
said out loud.

| Metric | Bar |
|---|---|
| Confirmed recall | ≥ 0.90 |
| Confirmed false alarms | ≤ 1 / hour |
| Fast p95 onset→emit (paced) | ≤ 350 ms |
| Fast false alarms | ≤ 4 / hour (Fast tranche is a sized write-off, spec §5.4) |

Known headwind, stated up front: the gigaspeech KWS model is a
chunk-16-left-64 export (~320 ms algorithmic lookahead) and measured
detection lag on test_wavs is ~0.4–0.5 s after word END. The Fast prefix
mechanism claws back ~300–650 ms (measured: `▁LOVE LY` fires 640 ms before
the full word), but ≤350 ms p95 from word ONSET is tight for long keywords.
If the holdout misses the latency bar with everything else passing, the
pre-declared next step is the custom streaming-CTC prefix spotter
(0–80 ms lookahead) behind the same `Detector` trait — not a bar adjustment.

## Corpus v1 (splits pre-committed 2026-07-11, before any engine run)

Public earnings-call replays (yt-dlp, personal research use). Target
keywords chosen from words that recur in live Kalshi earnings-mention
markets (e.g. KXEARNINGSMENTIONNFLX lists Acquisition / Partnership /
Competition-style words): **recession, guidance, tariff, acquisition,
partnership**.

| call id | company | split | rationale |
|---|---|---|---|
| TSLA-Q1-2026 | Tesla | dev | |
| SBUX-Q2-2026 | Starbucks | dev | |
| SNOW-Q2-FY26 | Snowflake | dev | |
| SBUX-Q1-2026 | Starbucks | holdout | same company, different call |
| SNOW-Q3-FY26 | Snowflake | holdout | same company, different call |
| GM-Q1-2026 | GM | holdout | company never seen in dev |

## Results log

| date | split | config | outcome |
|---|---|---|---|
| _(pending)_ | | | |
