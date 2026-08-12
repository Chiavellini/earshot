# System performance analysis

This is the measurement contract for turning banked EARSHOT evidence into a
claim about system performance. It is deliberately separate from live event
control and from any later trading-strategy study.

The executable is `scripts/system-analysis.py`. It reads a declared analysis
specification plus already-banked artifacts and writes one deterministic,
versioned `SystemAnalysisRecord`. It never launches a source, changes a config,
contacts a venue, or sends an order.

## What “proved” means

A system claim advances through this ladder in order:

1. **Evidence validity** — every expected run is enumerated; missing, partial,
   malformed, censored, and legacy-unbound artifacts remain in the denominator.
2. **Ingress integrity** — the intended source was present, audio arrived,
   sequence/drop/death accounting is complete, and coverage meets its gate.
3. **Detection quality** — ground truth is frozen and densely labelled;
   recall, precision, false alarms, and word-end-to-action latency meet their
   gates.
4. **Internal action path** — the detection can be joined through decision,
   accepted handoff, execution, and send attempt without a missing state.
5. **Race outcome** — a clock-valid observation joins the executable action to
   the competitor or price-moving event and reports a signed margin.

The analyzer enforces this ordering. A gate is assigned a rung only by a
built-in adapter or an analyzer-owned registered metric validator, never by an
arbitrary metric prefix. A downstream gate cannot support until at least one
proof-eligible gate exists and is supported at every prior rung over the exact
same declared `input_ids`; every other declared prior-rung gate on that lineage
must also support. This deliberately refuses a ladder assembled from unjoined
run sets. If an earlier rung on that evidence lineage is absent,
exploratory, rejected, or unresolved, the downstream result is
`not_resolvable`, not zero, not a pass, and not silently excluded. Its raw
result remains labelled `diagnostic_verdict`. A run denominator should
ultimately be explicit all the way through:

```text
expected → armed → attached → streamed → heard → detected → intent created
         → handoff accepted → dequeued → send attempted → acknowledged → filled
```

The current runtime report cannot populate every state in that chain. In
particular it has no stable detection-to-order trace id, structured dropped-
handoff outcome, authoritative fill outcome, or run clock envelope. The
analyzer therefore reports only what the artifact actually proves and refuses
causal joins it cannot make.

Exact input-set matching is run/artifact lineage, not yet an opportunity-level
join. Before the first performance validator is registered, its schema must
also bind a stable opportunity/denominator-set identity across rungs (or
jointly validate the full trace chain). Two metrics on the same file with
different word/sample identities may not be treated as one joined race.

## AnalysisSpec v1

Paths are resolved relative to the spec file. Expected artifacts are listed
one by one; globs are intentionally unsupported because a glob erases missing
runs and creates survivorship bias.

```json
{
  "schema_version": 1,
  "analysis_id": "ctc-candidate-dev-replay",
  "as_of": "2026-08-11",
  "hypothesis": "candidate reduces consumed-audio lag without losing recall",
  "notes": "Exploratory legacy-report diagnostic; not a proof record.",
  "inputs": [
    {
      "id": "base-aapl",
      "kind": "benchmark_report",
      "cohort": "baseline",
      "pair_id": "aapl-dev",
      "path": "artifacts/interleaved-bench.json",
      "selector": {
        "engine": "streaming_ctc",
        "chunk_ms": 80,
        "model_dir": "models/current",
        "tier": "fast"
      }
    },
    {
      "id": "candidate-aapl",
      "kind": "benchmark_report",
      "cohort": "candidate",
      "pair_id": "aapl-dev",
      "path": "artifacts/interleaved-bench.json",
      "selector": {
        "engine": "streaming_ctc",
        "chunk_ms": 80,
        "model_dir": "models/candidate",
        "tier": "fast"
      }
    }
  ],
  "gates": [
    {
      "id": "recall-noninferior",
      "scope": "comparison",
      "metric": "benchmark.recall",
      "stat": "paired_improvement_median",
      "op": ">=",
      "threshold": -0.01,
      "unit": "proportion",
      "min_samples": 1,
      "min_clusters": 1,
      "decision": "point",
      "minimum_provenance": "legacy_unbound"
    }
  ]
}
```

The example is intentionally runnable against today’s benchmark artifact and
therefore exploratory. A passing diagnostic is still top-level
`not_resolvable`; a performance proof needs analyzer-recomputed observations
from a registered validator, confidence-bound decisions, all expected inputs,
and the complete same-lineage rung ladder. No performance-metric validator is
registered in v1 yet, so current runtime, benchmark, and generic observation
artifacts can diagnose but cannot certify performance.
The spec hash proves which gates were declared to this analysis. Temporal
pre-registration requires an external immutable commit or registry entry made
before evidence collection; the analyzer does not infer that chronology.

Input kinds:

- `runtime_report` adapts the current `report.json`. A missing named path emits
  `runtime.report_file_present = 0`; it does not disappear from the analysis.
- `benchmark_report` selects exactly one cell and one tier from the current
  bake-off JSON. Ambiguous selectors are refused.
- `observation_record` consumes the provenance-bound generic record below.
  This is the door for acquisition, physical-bound, competitor, planted-control,
  and future opportunity-trace measurements.

An input may carry exact-file `controls`:

```json
"controls": [
  {"role": "audio", "path": "corpus/telephony/aapl.wav"},
  {"role": "manifest", "path": "corpus/manifests/aapl-q2-2026.toml"}
]
```

The analyzer hashes them. Candidate/baseline samples pair only when `pair_id`,
sample identity, context, and control hashes agree. Two cells selected from the
same benchmark report use that report hash as an automatic same-invocation
control. Separate reports require explicit controls. Paired
`observation_record` inputs additionally require the recognized control roles
`measurement_protocol` and `pair_manifest`; arbitrary shared labels are not a
proof control. Their `measurement_spec` and producer `tool` provenance hashes
also enter the comparison signature.

Each gate may name `input_ids`. When omitted, a cohort gate uses every input in
that cohort and a comparison gate uses all baseline and candidate inputs. This
is the explicit way to scope a gate in a heterogeneous spec; it cannot be used
to hide an omitted run after results are known.

## ObservationRecord v1

New measurement tools should emit this shape rather than an unversioned table:

```json
{
  "schema_version": 1,
  "record_type": "earshot.observation",
  "observation_id": "event-word-race-001",
  "cluster_id": "run-20260811-001",
  "status": "measured",
  "method": "word-end to first executable competitor sweep",
  "clock_domain": "utc_anchored",
  "host_vantage": "useast1-box",
  "provenance": [
    {"role": "measurement_spec", "path": "race-spec.json", "sha256": "<64 lowercase hex>"},
    {"role": "raw_evidence", "path": "opportunity-trace.jsonl", "sha256": "<64 lowercase hex>"},
    {"role": "clock_anchor", "path": "clock-anchor.json", "sha256": "<64 lowercase hex>"},
    {"role": "tool", "path": "race-measure.py", "sha256": "<64 lowercase hex>"}
  ],
  "metrics": [
    {
      "name": "race.executable_margin_ms",
      "value": 143.2,
      "unit": "ms",
      "direction": "higher_is_better",
      "uncertainty": 8.1,
      "sample_key": "event-word-race-001"
    }
  ],
  "attributes": {
    "outcome": "won",
    "run_id": "run-20260811-001",
    "host_boot_id": "linux-boot-id",
    "competitor_event_id": "kalshi-sweep-001",
    "clock_model": "two_anchor_affine",
    "clock_uncertainty_ms": 8.1,
    "sign_convention": "competitor_time_minus_our_executable_time",
    "exclusions": []
  }
}
```

Provenance paths resolve relative to the observation record, and the analyzer
re-hashes every referenced file. A stated hash without the exact local artifact
does not qualify as provenance. `raw_evidence` may repeat for multiple raw
artifacts; `measurement_spec`, `tool`, and (when required) `clock_anchor` are
singletons. The same role/path pair may not repeat.

`cluster_id` is the independent run/opportunity identity. Multiple words or
measurements from the same run share it and therefore never inflate
`min_clusters`. For a race observation it must equal `attributes.run_id`.
Repeating a `(metric name, sample_key)` inside one record is invalid.

`status` is `measured`, `censored`, or `invalid`. The latter two require a
`refusal_reason` and contribute no performance number, while remaining visible
in the output refusal ledger. Ratio metrics may carry `numerator` and
`denominator` without `value`; the analyzer derives the value and reports a
pooled ratio without averaging ratios. If all three are supplied, `value` must
agree with the counts.

`clock_domain` is one of `monotonic`, `source_relative`, `utc_anchored`, or
`not_applicable`. Any cited clock anchor is validated; `utc_anchored` requires
exactly one.

A `race.*` record is not valid merely because two UTC strings exist. It must
use `clock_domain = "utc_anchored"`, cite exactly one validated clock-anchor
artifact, and carry the run, host boot, competitor-event identity, sign
convention, uncertainty, and `clock_model = "two_anchor_affine"`. The clock
artifact is `ClockAnchor v1`:

```json
{
  "schema_version": 1,
  "record_type": "earshot.clock_anchor",
  "host_boot_id": "linux-boot-id",
  "host_vantage": "useast1-box",
  "clock_kind": "monotonic_raw",
  "anchors": [
    {"monotonic_ns": 1000000000, "utc_ns": 1700000000000000000},
    {"monotonic_ns": 2000000000, "utc_ns": 1700000001000000000}
  ],
  "fit": {
    "rate": 1.0,
    "offset_ns": 1699999999000000000,
    "max_residual_ns": 0
  }
}
```

The generic adapter verifies the schema, strictly increasing anchors, affine
fit, residual bound, host vantage, and boot identity. The observation’s
`clock_uncertainty_ms` may not be smaller than the validated maximum residual.
That is still not a race proof. A race validator must recover both event
timestamps from a versioned raw trace, bind the word/run/host/boot/competitor
identities, verify the anchor covers those timestamps (including
extrapolation and drift), compute `competitor_time - our_executable_time`, and
derive the uncertainty bound. Re-hashing a caller-provided scalar alongside an
unrelated valid anchor is insufficient. Current flat single-anchor conversions
are also insufficient for a sub-second cross-host claim.

The `race.*` assertion namespace is closed in v1:
`race.executable_margin_ms` is the only admitted race name, unit, direction,
and sign convention. It does not receive rung 5 merely from that name. Only a
registered validator can assign the rung and `validator_recomputed` grade;
none is registered in v1.

## Evidence grades

Every extracted sample has one of three grades:

- `legacy_unbound` — current runtime and benchmark JSON. These artifacts lack
  a schema version and hashes that bind config, model, binary, corpus, source,
  and clocks to the result. They are useful for diagnosis but are not proof.
- `provenance_bound` — a valid `earshot.observation` whose assertion, method
  label, and exact referenced bytes re-hash correctly and whose clock envelope
  is structurally valid when UTC anchored. It does not establish that the
  scalar was derived correctly from those bytes.
- `validator_recomputed` — the same byte binding plus a successful,
  analyzer-owned registered validator that parses the versioned method and raw
  schemas; reconstructs the expected sample denominator and identities;
  recomputes value, sign, unit, and uncertainty; and verifies any clock join.
  Only this grade is proof-eligible for a performance gate. A record cannot
  declare this grade for itself.

Missing or invalid inputs have no usable performance grade. Analyzer-derived
integrity zeroes always stay in their gate denominator, regardless of leaf
grade. Performance gates require `validator_recomputed` by default. Lowering
`minimum_provenance` to `provenance_bound` records
`unvalidated_scalar_allowed`; lowering it to `legacy_unbound` records
`legacy_provenance_allowed`. Either is an exploratory diagnostic, and the
downgrade is repeated in the gate and top-level ledgers. Likewise,
`input_validity = "metric_only"` and `decision = "point"` are exploratory for
performance gates. If every such diagnostic passes, top-level `verdict` still
remains `not_resolvable`, `posture` is `exploratory`, and only
`diagnostic_verdict` says `supported`.

With no exploratory downgrade, `posture = "proof_required"` describes the
declared analysis mode, not an achieved result. A missing registered validator
or weaker evidence still yields `not_resolvable`.

This rule is intentional. Although the architecture registry currently marks
the capture, corpus, and benchmark doors implemented, their node contracts
still identify the versioned `CaptureManifest`, hashed corpus catalog, and
hashed `BenchmarkSpec`/`BenchmarkReport` as migration work. The analyzer does
not upgrade those artifacts by assertion.

## Metrics from current adapters

The runtime adapter emits:

- report presence/parseability/completeness, exit observation/acceptability,
  ingress presence, frame/chunk/detection/intent/send/ack counts;
- the public telemetry-contract value
  `runtime.reported_drop_pct = 100 * total_drops / frames_in`, plus separate
  ingress, interior, and total drop counts. This is a drops-per-processed-frame
  percentage and can exceed 100 under sustained saturation; it is not treated
  as a bounded binomial proportion;
- dead-leg fraction and sent-to-ack ratio;
- raw per-detection capture-chunk-to-emit latency, labelled exactly as such;
- each serialized edge's count and run-level p50/p90/p95/p99/max.

Serialized histogram quantiles cannot be merged. Across runs, a metric such as
`runtime.edge.capture_to_decision.p95_ms` is therefore a distribution of
**run-level p95s**, never advertised as the p95 of all underlying events.
Execution edges are success-conditioned in current telemetry; their absence is
not converted to fast performance.

The benchmark adapter emits pooled TP/miss/false-alarm counts, recall,
precision, false alarms per hour, and distributions of the existing per-phrase
latency summaries. It does not repair sparse labels, manufacture raw latency
samples, or infer confidence from missing per-call matches. Current benchmark
metrics consequently remain `legacy_unbound`.

## Statistics and gates

- Cohort summaries retain sample count, independent cluster count, provenance
  counts, min/p25/p50/p75/p95/p99/max/mean, and pooled ratio counts where
  applicable.
- Uncertainty uses deterministic cluster bootstrap at the input/run level.
  Continuous percentile intervals require at least 20 independent clusters;
  this hard floor is not a substitute for a hypothesis-specific power study or
  a higher predeclared minimum. They also require at least 20 expected
  bootstrap draws in each confidence tail; raise `bootstrap_repetitions` or
  lower `confidence` when that resolution is unavailable. Repeated detections
  or phrase rows inside one run do not masquerade as independent experiments.
  Pair clusters are dependency-connected components of the immutable source-
  cluster identities, not caller-supplied pair labels. Thus `B-C1`, `B-C2`,
  and `B-C3` are one independent cluster because all reuse `B`. Declared
  measurement error widens the sampling interval conservatively; it is never
  combined as if independent. For `race.executable_margin_ms`, the clock error
  is a mandatory minimum uncertainty even if the metric omits a larger bound.
- Pooled ratios retain numerators and denominators. A confidence bound is
  available only when there is exactly one Bernoulli row (`0/1` over
  denominator `1`) per independent cluster. Only then does the analyzer use a
  Wilson interval. Multiple count trials inside a run or multiple nominal
  pairs inside one dependency component remain descriptive and cannot produce
  a confidence-bound verdict; pair-weighted `p` with component-count `n` is
  not treated as a binomial experiment.
- Comparisons are paired. The record reports candidate-minus-baseline deltas
  and direction-normalized improvements (positive is always better). Pair
  output preserves `pair_id` and `sample_key` as separate fields and adds a
  canonical `pair_key`; display delimiters never define identity.
- `decision = confidence_bound` is the default. A `>=` gate passes only when
  the lower bound clears the threshold and fails only when the upper bound is
  below it; overlap is `not_resolvable`. `point` decisions must be requested
  explicitly. `confidence` must be greater than `0.5` and at most `0.999999`;
  values closer to one cannot be represented safely by the Wilson transform.
- A gate below `min_samples` or `min_clusters`, without the requested metric,
  without sufficient provenance, without verified pairing, or without a
  usable interval is `not_resolvable`.
- Overall verdict is `rejected` if any proof-eligible gate is rejected,
  `supported` only if every gate is supported, proof-eligible, and
  rung-complete, otherwise `not_resolvable`. A rejected exploratory gate is
  reported only through `diagnostic_verdict`; it cannot issue exit 1. With no
  declared gates the result is always `not_resolvable`.

By default `input_validity = "all_expected"`: any missing, malformed,
censored, partial, duplicate, or otherwise ineligible relevant input blocks a
downstream performance verdict. A valid input that does not emit the gated
metric also blocks it. Every emitted expected sample of that metric must meet
the gate's minimum evidence grade; two recomputed survivors cannot hide a
third provenance-bound sample. For comparisons, expected tokens are examined
before provenance filtering. Any below-grade side, missing `pair_id`,
explicitly unpairable metric sample, missing counterpart, duplicate, control
mismatch, or other refused expected pair blocks the gate; surviving pairs are
not silently scored. Evidence-integrity gates (presence, parseability,
completeness/schema/exit) include every analyzer-produced zero so the failure
can be rejected rather than hidden by provenance filtering.
If one metric changes unit, direction, or scalar/ratio aggregation across
inputs, every affected input is marked ineligible, the metric is removed from
scoring, and the refusal ledger records `metric_identity_conflict` rather than
aborting record construction.
`input_validity = "metric_only"` is an explicit exploratory downgrade for a
focused diagnostic. A paired win rate is unresolved if a pair's measurement-
error interval crosses zero; dimensional error such as milliseconds is never
added to a proportion interval.

Supported cohort statistics are `pooled_ratio`, `mean`, `median`, `p95`,
`min`, and `max`. Supported paired statistics are
`paired_delta_mean`, `paired_delta_median`,
`paired_improvement_mean`, `paired_improvement_median`, and
`paired_win_rate`.

Rung assignment is deterministic: analyzer integrity metrics are rung 1;
known runtime ingress/drop/dead-leg and capture-to-detect processing metrics
are rung 2; known benchmark metrics plus runtime detection and
capture/detect-to-decision metrics are rung 3; and remaining known runtime
action/transport metrics are rung 4. Generic `ingress.*`, `detection.*`,
`action.*`, and `race.*` assertions remain rung 0 unless an analyzer-owned
validator registry entry fixes their metric id, unit, direction, rung, and
validator. A rung-0 diagnostic does not satisfy a ladder prerequisite. Every
prior rung must also use the exact same declared input set, so unrelated runs
cannot be spliced into an end-to-end claim.

## Running it

```sh
python3 scripts/system-analysis.py --self-test
python3 scripts/system-analysis.py --example
python3 scripts/system-analysis.py --spec path/to/spec.json --output path/to/result.json
```

Writes are atomic. The output path is refused if it aliases the spec, tool,
input, control, or observation-provenance evidence. Each input JSON is read
once into a byte snapshot used for both hashing and parsing. JSON is strict:
duplicate object keys and `NaN`/infinite constants are parse failures. With no `--output`,
the record is printed to stdout. The
record contains the spec hash, tool hash/revision, every input/control hash,
refusals, metric inventory, cohort summaries, paired comparisons, gates, and
the overall verdict. Identical files and spec produce byte-identical JSON.
Exit status is 0 for `supported`, 1 for `rejected`, 2 for `not_resolvable`, and
64 for an invalid invocation/spec or an output I/O failure; the record is
written before a verdict exit.

## Strategy boundary

This framework measures whether the system heard the right audio, recognized
the right words, preserved the action path, and acted before a declared rival
event. It does not evaluate pre-market inventory, contract flipping, position
sizing, expected value, or live posture. Those questions may consume a proven
measurement record later; they may not redefine its samples, timestamps,
labels, or gate after seeing the outcome.
