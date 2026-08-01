# Node: `evidence.measure`

## Job

Evaluate one declared latency, arrival, or race hypothesis from observable
artifacts and publish a provenance-complete measurement.

## Owns

Network probes in `src/latprobe/`; absolute/relative media timing tools in
`scripts/abs-lag.py`, `scripts/leg-lag.py`, `scripts/arrival-profile.py`,
`scripts/arrival-lead.py`, and `scripts/arrival-ab.sh`; physical/race bounds in
`scripts/physical-bound.py`; and focused venue probe experiments such as
`scripts/f2-probe.py`, `scripts/ivs-prefetch-probe.py`,
`scripts/spec-probe.py`, `scripts/prefetch-gate.sh`, and
`scripts/prefetch-verdict.py`. It also owns `scripts/drace-wallclock.py`,
`scripts/rival-decompose.py`, the settlement-truth accuracy scorer
`scripts/wnt-score.py`, the competitor-offset measurement
`scripts/wnt-pack-lag.py`, and experimental measurement code under
`scripts/experimental/`, together with the race/latency evidence documents
`HANDOFF-RACE-PROGRAM.md`, `VERIFICATIONS.md`,
`docs/COMPETITOR-ANATOMY.md`, `docs/INGRESS-BAKEOFF.md`,
`docs/LATENCY-PROOF.md`, and `docs/RACE-STRATEGY.md`.

## Does not own

It does not alter production acquisition posture, record canonical corpus
labels, select a benchmark winner, launch trades, or rewrite historical
evidence to make a hypothesis resolve.

## Input contracts

Each run declares the hypothesis, capture/arrival/race artifacts, clock domain,
host vantage, tool revision, parameters, comparison sign convention, and
pre-registered decision bar. Shared correlation helpers are consumed from their
own runtime contract.

## Output contracts

The target `MeasurementRecord` contains inputs/hashes, method, sample count,
estimate/distribution, units/sign, uncertainty, refusal reason, and verdict
(`supported`, `rejected`, or `not_resolvable`). Current tools variously emit
JSON, logs, and human tables documented in `docs/LATENCY-PROOF.md`.

## Side effects and exclusive resources

Measurements may fetch public streams, run decoders/probes, write temporary or
large artifacts, and consume network/CPU. Live experiments use isolated output
paths and must not share production ports, URL files, or state directories.

## Failure contract

Clock mismatch, missing overlap, weak/ambiguous correlation, repeated content,
insufficient samples, inconsistent sign, or failed pre-registered gate yields
an explicit refusal/not-resolvable result. No number is better than a
structurally invalid number.

## Parallel work boundary

Independent hypotheses and output directories can run in parallel. Method or
record-schema changes belong here; capture, acquisition, corpus, and production
config changes require separate nodes and cannot be hidden inside an experiment.

## Verification

Run tool self-checks where provided (`physical-bound.py --selfcheck`,
fetcher/probe self-tests), fixture-based sign/refusal tests, and reproduce the
same banked-artifact result. Record conclusions in `docs/LATENCY-PROOF.md` only
after the structured evidence exists.

## Migration status

The project has unusually strong measurement logic but many one-off scripts,
stdout schemas, and long narrative ledgers. Standardize `MeasurementSpec` and
`MeasurementRecord`, then make narrative documents index immutable records
instead of serving as the only result store.
