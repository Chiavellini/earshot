---
as_of: 2026-07-29
authority: current-sequencing
---

# Roadmap

This roadmap sequences architecture work around active event work. It does not
replace product evidence in `VERIFICATIONS.md` or node contracts in
`docs/nodes/` and `docs/layers/`.

## Phase A — restore the backbone

**State: complete on 2026-07-29.** The registry and its Cargo integration are
the acceptance mechanism; the remaining phases change implementations behind
the newly explicit doors.

Deliver:

- stable spec anchors (`docs/SPEC.md`);
- one current-state projection (`docs/STATUS.md`);
- machine-readable node, path, and contract ownership
  (`architecture/nodes.toml`);
- collision claims (`docs/work/ACTIVE.md`);
- documentation/ownership lint (`scripts/doc-lint.sh`).

Exit: every Rust and operational script has exactly one owning node; every
node has one contract doc; unresolved spec citations fail lint.

## Phase B — compile configuration into a run plan

Split deserialization from cross-node planning:

```text
TOML → RawEventConfig → PlanCompiler → RunPlan
```

`RunPlan` contains narrow subplans for ingress, audio format/topology, keyword
catalog, detector, decision, market data, risk, execution, and runtime
placement. Leaf factories accept only their subplan.

First compatibility API:

```text
earshot config inspect --config <path> --json
earshot config validate --config <path> --profile <profile>
```

Exit: shell code no longer parses TOML with `sed`/regex; demo/prod selection,
format compatibility, allowlists, and source topology are plan-compiler tests.

## Phase C — make lifecycle a node

Introduce explicit phases:

```text
compile → prepare → activate → run → drain
```

A `PreparedRun` owns successfully built detectors, bound ingress, live books,
transport, risk storage, and every background task handle. `activate()` is the
only operation that arms risk or exposes a live session. One supervisor owns
cancellation and teardown.

Exit: failed preparation proves nothing armed; STOP/SIGTERM/source death have
typed exit reasons; every spawned task is joined or deliberately cancelled.

## Phase D — type the operational doors

Introduce versioned artifacts before moving implementations:

- `event-plan.toml`
- `config-info.json`
- `build-manifest.json`
- `source-endpoint.json`
- `preflight-result.json`
- `run-meta.json`
- `session-result.json`
- `verdict.json`

Add a run identity independent of market event ID:

```text
var/runs/<event>/<run-id>/
  control/
  logs/
  report/
  provenance/
```

Exit: phone and webcast runs for one market event cannot share a STOP file,
ledger, PID, report, or log unless explicitly composed into one run.

## Phase E — extract control-plane hubs

Keep current script names as compatibility facades while extracting, in this
order:

1. config inspection;
2. notification;
3. ingress/tunnel allocation;
4. build/package and immutable deployment;
5. session start;
6. supervision/retry;
7. grading/archive;
8. scheduler adapters.

Attended and scheduled flows must call the same session-start, supervise, and
grade nodes. Event-specific scripts become data or thin presets.

Exit: no operational script owns more than one transformation; each supports
fixture-backed check/dry-run behavior and structured output.

## Phase F — harden runtime contracts

Parallel, contract-first workstreams:

- compile one stable `KeywordCatalog` rather than rebuilding positional
  keyword indices in detector, decision, executor, and arm;
- make `CapturedFrame → ProcessedFrame → AudioChunk` immutable doors with an
  explicit `StreamFormat`;
- commit decision state only after an intent dispatcher accepts the intent;
- replace risk `check` + later `record_send` with
  `reserve → AuthorizedOrder`;
- split transport/reconciliation from order policy;
- replace string telemetry edges with typed IDs.

Exit: each failure mode has a focused boundary test; full replay, fanout,
executor, kill, and performance suites remain green.

## Phase G — separate current truth from history

After active changes land, archive event narratives and superseded roadmaps
without deleting evidence. Procedures move to `docs/runbooks/`; incidents and
event narratives move to `docs/history/`; current conclusions stay in
`STATUS`, node contracts, and indexed verification claims.

Exit: a new agent can choose one node and begin work without reading a
multi-thousand-line event chronology.
