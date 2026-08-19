# Node contracts

This is the human index for EARSHOT's independently changeable units. A node
has one job, an explicit input and output, and a boundary that another agent
can rely on without reading its implementation. The machine registry is
`architecture/nodes.toml`; the prose contract for each entry is linked below.

The existing `docs/layers/` contracts still describe the original in-process
runtime spine. The node contracts extend that discipline across source
adapters, control-plane automation, platform operations, and evidence work.
Each node's **Migration status** says where a legacy script or hub still crosses
the intended boundary.

## How changes integrate

Use one node per change. Claim the node, read only its contract and cited
implementation paths, and keep edits inside its parallel-work boundary. If the
work needs another node, split it into a second change with an explicit handoff
artifact.

Integration is contract-first:

1. Change the producer or consumer contract document before changing a door.
2. Name the structured input/output artifact and its compatibility behavior.
3. Implement one side without reaching into the other node's internals.
4. Verify the node locally, then verify the connection at the nearest
   integration seam.
5. Update `architecture/nodes.toml` only when ownership or topology changes.

Shared shell environment, ad-hoc stdout parsing, `$HOME` URL files, and mutable
state directories are current compatibility surfaces, not implicit APIs. New
connections should prefer versioned TOML/JSON records with explicit paths and
failure states.

The operator surface is separately machine-declared in
`architecture/operations.toml` and rendered by `scripts/opsctl.sh surface`.
It is intentionally smaller than the node implementation map: public generic
commands call node-owned internal adapters; dated and issuer-specific scripts
are historical or hard-retired, never alternate production procedures.

Every registry contract declares three boundary facts:

- `kind`: `artifact` is a durable or immutable value, `event` is a lifecycle
  transition, `capability` is a callable constructor/service door, and
  `stream` is an ordered sequence with transport semantics.
- `maturity`: `implemented` means the named door exists as the stable
  boundary; `compatibility` means current scripts, stdout, environment, or
  files still approximate that boundary; `planned` reserves a reviewed target
  that consumers must not treat as available.
- `door`: the concrete type, file, command result, channel, or constructor at
  which producer responsibility ends and consumer responsibility begins.

The reusable skeleton is [`docs/nodes/TEMPLATE.md`](nodes/TEMPLATE.md).

## Runtime graph

`runtime.assembly` connects the hot path. Source adapters implement
`runtime.source.port`; market validation and order transport are independent
support nodes.

| Node ID | Contract |
|---|---|
| `runtime.foundation` | [runtime-foundation](nodes/runtime-foundation.md) |
| `runtime.assembly` | [runtime-assembly](nodes/runtime-assembly.md) |
| `runtime.config` | [existing config contract](layers/config.md) |
| `runtime.source.port` | [source-port](nodes/source-port.md) |
| `runtime.source.replay` | [source-replay](nodes/source-replay.md) |
| `runtime.source.twilio` | [source-twilio](nodes/source-twilio.md) |
| `runtime.source.sip` | [source-sip](nodes/source-sip.md) |
| `runtime.source.pipe` | [source-pipe](nodes/source-pipe.md) |
| `runtime.source.arbiter` | [source-arbiter](nodes/source-arbiter.md) |
| `runtime.preproc` | [existing preproc contract](layers/preproc.md) |
| `runtime.chunker` | [existing chunker contract](layers/chunker.md) |
| `runtime.detector.port` | [existing detector port contract](layers/detector.md) |
| `runtime.detector.keyword_compile` | [detector-keyword-compile](nodes/detector-keyword-compile.md) |
| `runtime.detector.sherpa` | [detector-sherpa](nodes/detector-sherpa.md) |
| `runtime.detector.streaming_ctc` | [detector-streaming-ctc](nodes/detector-streaming-ctc.md) |
| `runtime.detector.online_asr` | [detector-online-asr](nodes/detector-online-asr.md) |
| `runtime.verifier` | [verifier](nodes/verifier.md) |
| `runtime.decision` | [existing decision contract](layers/decision.md) |
| `runtime.market_validation` | [runtime-market-validation](nodes/runtime-market-validation.md) |
| `runtime.kalshi.auth` | [existing auth contract](layers/auth.md) |
| `runtime.kalshi.book` | [existing book-cache contract](layers/book-cache.md) |
| `runtime.kalshi.fix` | [existing FIX contract](layers/fix-transport.md) |
| `runtime.risk` | [existing risk contract](layers/risk.md) |
| `runtime.kalshi.executor` | [existing executor contract](layers/executor.md) |
| `runtime.order_transport` | [runtime-order-transport](nodes/runtime-order-transport.md) |
| `runtime.order_evidence` | [runtime-order-evidence](nodes/runtime-order-evidence.md) |
| `runtime.telemetry` | [existing telemetry contract](layers/telemetry.md) |

## Event control graph

The intended lifecycle has one parallel preparation fan-out:

```text
discover → qualify ─┬→ prepare ────┐
                    ├→ admission ──┼→ preflight → launch → [phone_session] → supervise → grade
                    └→ acquire ────┘
```

`phone_session` is present only for phone ingress. Its ordering is
load-bearing: launch/assembly first binds the Twilio listener and emits
`runtime.source.twilio.listener_ready`; only then may phone-session control
place the call. `control.phone.attached` is the later runtime event consumed by
supervision and telemetry. It is not an input needed to construct the Twilio
source adapter.

`prepare` already has a focused contract at
[`docs/layers/prepare.md`](layers/prepare.md), so it is not duplicated here. It
owns `src/prepare.rs` and the compatibility staging scripts
`scripts/stage-webcast-arm.sh` and `scripts/stage-webcast-capture.sh`. Those
scripts instantiate arm/capture configurations; runtime launch and evidence
capture remain behind their own contracts.

| Node ID | Contract |
|---|---|
| `control.discover` | [control-discover](nodes/control-discover.md) |
| `control.qualify` | [control-qualify](nodes/control-qualify.md) |
| `control.prepare` | [existing prepare contract](layers/prepare.md) |
| `control.admission` | [control-admission](nodes/control-admission.md) |
| `control.acquire` | [control-acquire](nodes/control-acquire.md) |
| `control.preflight` | [control-preflight](nodes/control-preflight.md) |
| `control.launch` | [control-launch](nodes/control-launch.md) |
| `control.phone_session` | [control-phone-session](nodes/control-phone-session.md) |
| `control.supervise` | [control-supervise](nodes/control-supervise.md) |
| `control.grade` | [control-grade](nodes/control-grade.md) |
| `control.operate` | [control-operate](nodes/control-operate.md) |

## Platform graph

| Node ID | Contract |
|---|---|
| `platform.runtime` | [platform-runtime](nodes/platform-runtime.md) |
| `platform.deploy` | [platform-deploy](nodes/platform-deploy.md) |
| `platform.notify` | [platform-notify](nodes/platform-notify.md) |

## Evidence graph

The intended evidence flow is:

`capture → corpus → benchmark`, with `measure` producing independent latency
and race measurements from captured artifacts.

| Node ID | Contract |
|---|---|
| `evidence.capture` | [evidence-capture](nodes/evidence-capture.md) |
| `evidence.corpus` | [evidence-corpus](nodes/evidence-corpus.md) |
| `evidence.benchmark` | [evidence-benchmark](nodes/evidence-benchmark.md) |
| `evidence.measure` | [evidence-measure](nodes/evidence-measure.md) |

## Project governance

| Node ID | Contract |
|---|---|
| `project.architecture` | [project-architecture](nodes/project-architecture.md) |
| `project.build` | [project-build](nodes/project-build.md) |
