# Node: `evidence.measure`

## Job

Evaluate one declared latency, arrival, or race hypothesis from observable
artifacts and publish an evidence-graded measurement without overstating what
the source bytes prove.

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
The versioned offline system-performance framework is
`scripts/system-analysis.py`; its spec, evidence-grade, pairing, uncertainty,
and gate contract is `docs/SYSTEM-ANALYSIS.md`.

## Does not own

It does not alter production acquisition posture, record canonical corpus
labels, select a benchmark winner, launch trades, or rewrite historical
evidence to make a hypothesis resolve.

## Input contracts

Each run declares the hypothesis, capture/arrival/race artifacts, clock domain,
host vantage, tool revision, parameters, comparison sign convention, and
declared decision bar. `system-analysis.py` consumes a hashed
`AnalysisSpec` that enumerates exact expected artifacts (including missing
ones), candidate/baseline pair ids, hashed controls, and gates. Shared
correlation helpers are consumed from their own runtime contract.
The hash binds what was declared; an external immutable commit or registry is
required to establish that the declaration preceded evidence collection.

## Output contracts

The target `MeasurementRecord` contains inputs/hashes, method, sample count,
estimate/distribution, units/sign, uncertainty, refusal reason, and verdict
(`supported`, `rejected`, or `not_resolvable`). Current tools variously emit
JSON, logs, and human tables documented in `docs/LATENCY-PROOF.md`.

`scripts/system-analysis.py` implements the first versioned aggregate door as
`SystemAnalysisRecord` (`record_type = "earshot.system_analysis"`,
`schema_version = 1`). It binds the spec/tool/input/control hashes, refusal
ledger, cohort distributions, paired comparisons, deterministic cluster-
bootstrap uncertainty, declared gate results, proof-required/exploratory
posture, same-input-lineage rung dependencies, and overall verdict. Generic
leaf measurements enter through `ObservationRecord`
(`record_type = "earshot.observation"`) as `provenance_bound`: their hashes and
structure are checked, but their scalar is not trusted as a derivation. Only an
analyzer-owned registered validator may emit `validator_recomputed`, the
default performance-gate grade. Current runtime and benchmark JSON are graded
`legacy_unbound`; all three current adapters can diagnose but cannot certify a
performance claim.

## Side effects and exclusive resources

Measurements may fetch public streams, run decoders/probes, write temporary or
large artifacts, and consume network/CPU. Live experiments use isolated output
paths and must not share production ports, URL files, or state directories.

## Failure contract

Clock mismatch, missing overlap, weak/ambiguous correlation, repeated content,
insufficient samples, inconsistent sign, or failed declared gate yields
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

For the system framework, run `python3 scripts/system-analysis.py --self-test`.
The planted controls cover missing/malformed denominators, partial-report
censoring, telemetry drop semantics, semantic clock anchors, provenance and
control refusal, strict JSON, absent target metrics, unpairable and below-grade
sample refusal, independent-cluster counting, Bernoulli/Wilson structure,
continuous-bootstrap cluster/tail-resolution floors, same-lineage rung enforcement,
unvalidated-scalar and other exploratory downgrades, symmetric diagnostic
verdicts, numeric overflow refusal, latency sign, and byte-identical reruns.

## Migration status

The project has unusually strong measurement logic but many one-off scripts,
stdout schemas, and long narrative ledgers. The system analyzer now provides a
versioned aggregate spec/record and a provenance-bound observation door.
Migration is not finished: existing measurement tools still need adapters;
the capture/corpus/benchmark producers still lack bound manifests and raw
per-opportunity traces; and analyzer-owned validators must be implemented to
recompute each proof metric from a registered raw schema. Until then no current
performance adapter can emit `validator_recomputed`. Narrative documents
should become indexes over immutable records rather than serving as the only
result store.
