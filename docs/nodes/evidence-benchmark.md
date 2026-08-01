# Node: `evidence.benchmark`

## Job

Score declared detector/configuration candidates against a frozen corpus split
and publish comparable metrics.

## Owns

Manifest loading and scoring in `src/bench/`, frozen fixture generation in
`src/fixture.rs`, and benchmark-oriented study helpers:
`scripts/dead-word-autopsy.sh`, `scripts/miss-autopsy.py`,
`scripts/variant-study.sh`, `scripts/keywords-extract.py`,
`scripts/variant-audit.py`, `scripts/prefix-gate-score.py`, and
`scripts/verify-occurrences.py`.
The benchmark target in `benches/hot_path.rs` and protocol in
`docs/BAKEOFF.md` are part of the same gate.
`scripts/keywords-extract.py` is the shared `[[keywords]]` extraction (TSV +
`--sha256`) that both `variant-study.sh` and preflight's readiness gate call,
so the artifact hash and the gate's recomputed hash can never drift.

## Does not own

It does not create ground truth, change detector algorithms during a run,
promote a winning config automatically, capture live events, or redefine the
pre-registered acceptance bar after results are seen.

## Input contracts

The `bench-detector` command consumes an exact event/detector config, corpus
root/manifests, engine list, chunk sizes, split (`dev` or one-shot `holdout`),
pacing choice, and optional call filter.

## Output contracts

It writes a benchmark report containing candidate identity, corpus/split,
precision, recall, false alarms per hour, match details, and paced latency where
requested. Comparisons require identical corpus and scoring policy.

`scripts/variant-study.sh` additionally writes the keyword-readiness artifact
`configs/.readiness/<config-basename>.json` consumed by the BLOCKING gate in
`scripts/preflight-event.sh` (node `control.preflight`):

```json
{"config": "<basename>", "keywords_sha256": "<hex>", "date": "YYYY-MM-DD",
 "engine": "<detector engine>", "voices": ["Samantha", "Daniel", "Paulina"],
 "keywords": [{"ticker": "…", "phrase": "…", "variants": ["…"],
               "clips": N, "confirmed": M}]}
```

`keywords_sha256` is computed by `scripts/keywords-extract.py --sha256` over
the exact extracted `[[keywords]]` TSV (semantic content only — tickers,
phrases, variants — so retuning a price cap does not invalidate a study, but
any change to what the detector must hear does). The artifact is written even
when the study FAILs: a `confirmed: 0` entry is exactly what the gate must
see. `confirmed` is the raw detection-line count and can exceed `clips`
(multi-fires within one clip); the gate's only bar is zero vs nonzero. Artifacts under `configs/.readiness/` are generated evidence, excluded
from ownership governance (`architecture/nodes.toml` `excluded_globs`).

The study synthesizes every variant with THREE voices: Samantha + Daniel plus
a Spanish voice (Paulina, then Mónica) applied to ALL variants — WING lost
CITR live to a Spanish-accented pronunciation of a menu item, the exact class
a two-English-voice study cannot catch. When no Spanish voice is installed it
falls back to a third English voice and records that degradation in the
artifact as `voice_note` (the `voices` array always names what actually ran).

## Side effects and exclusive resources

It reads corpus/model files, constructs detector runtimes, consumes CPU/memory,
and writes only the requested report. Holdout evaluation is an exclusive
governance event and must not become an iterative tuning loop.

## Failure contract

Invalid manifests, unavailable models, unknown engines/splits, or incomplete
scoring returns nonzero and no passing verdict. Empty filters and incomparable
candidate reports are labelled, not summarized as wins.

## Parallel work boundary

Harness/scoring changes stay in `src/bench/` and benchmark helpers. Corpus
labels, detector implementation, and promotion decisions are separate changes;
schema/scoring changes invalidate comparison unless versioned.

## Verification

Run `cargo test bench::`, `cargo test --test bench_fixture`, and
`cargo bench --bench hot_path` when hot-path cost changes. Reproduce a small dev
report twice and compare deterministic non-wall-clock fields. For the readiness
path: `bash -n scripts/variant-study.sh`, then run it against a 2-keyword mini
config and check `configs/.readiness/<basename>.json` round-trips through
`scripts/keywords-extract.py --sha256` and through
`scripts/preflight-event.sh --config <cfg> --only-readiness`.

## Migration status

The Rust harness and JSON report exist, while study scripts emit additional
ad-hoc formats and the CLI dispatch lives in `src/main.rs`. Define one
`BenchmarkSpec`/`BenchmarkReport` schema and record config/corpus/tool hashes.
