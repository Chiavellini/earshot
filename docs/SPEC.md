# EARSHOT meta-spec

> Provenance: the original meta-spec cited throughout the repository was never
> committed. A reconstruction from surviving citations was committed on
> 2026-07-14 to the unmerged `refinements-m4-prep` branch; this reconciled
> version restores it to the active branch on 2026-07-29. Reconstructed
> sections say only what their citing code and contract docs require.
> Unrecovered slots remain explicit; numbering is frozen because code cites
> these anchors.

## Repository architecture doctrine

A node has one job, one owner, explicit input/output contracts, and an
independent verification path. Adapters translate external systems into a
contract. Composers connect nodes and own lifecycle but contain no domain
policy. Evidence records what happened; it does not silently become runtime
policy.

The machine-readable node and contract catalog is
`architecture/nodes.toml`; the human index is `docs/NODES.md`.

## Principles

### P1 — origin isolation

Only a source adapter may know where audio came from. Everything downstream
of the source port is origin-agnostic. A new feed is a new adapter, not a new
pipeline.

### P2 — unrecovered

No surviving citation states P2. The slot is reserved.

### P3 — fail cheap, fail loud

Unknown state is dangerous state: unknown staleness is stale, an unknown ask
is refused, and a missing book snapshot refuses to arm. Stalls, drops, and
refusals must be surfaced. Ambiguous order outcomes are queried, never blindly
retried. Failures should occur at the earliest cheap boundary: compile before
prepare, prepare before activate, activate before the hot path.

### P4 — deterministic replay regression

Replay-based end-to-end testing is first-class. A synthetic fixture through
the real pipeline must produce deterministic detections and a report artifact.
That acceptance path remains the regression floor for every runtime node.

## §1–§2 — thesis and market framing [unrecovered]

No direct citations survive. The operative thesis is maintained in
`docs/TELEPHONY-DETECTION-SPEC.md`; current priorities are in
`docs/ROADMAP.md`.

## §3 — end-to-end latency budget

The utterance-to-order path is measured as explicit stage edges:

- capture/ingress: spoken audio to `capture_ts`; measured per adapter and
  venue, never inferred from an unrelated feed;
- chunk accumulation: approximately one `chunk_ms`, the principal pipeline
  latency dial;
- detection: model-dependent evidence/lookahead delay plus compute;
- decision and order build: in-process, budgeted below network time;
- send to acknowledgement: transport/region dominated.

Every optimization names the edge it changes and preserves the timestamp
contract in `src/types.rs` / `src/telemetry.rs`.

## §4 — concurrency architecture

Hot runtime stages use dedicated ownership and bounded ports. Logical node
boundaries remain separate even when two nodes deliberately share a thread.
Thread placement is a deployment decision; it must not erase the typed door
between nodes. Control-plane concurrency uses run-scoped state and explicit
session identity rather than shared filenames.

## §5 — runtime node specifications

Exact contracts live in `docs/layers/` and `docs/nodes/`.

- **§5.1 — core types and clock.** One monotonic clock
  (`clock::MonotonicNs`) for intra-run timing; wall clock enters through an
  explicit report anchor only.
- **§5.2 — source adapter boundary.** An `AudioSource` produces captured
  frames. Replay, Twilio, SIP, pipe/webcast, and arbitration are independent
  implementations around that port.
- **§5.3 — preprocessing and chunking.** Preprocessing normalizes frames;
  chunking owns accumulation into detector-native windows.
- **§5.4 — detector.** Pluggable engines consume chunks and emit acoustic
  detections. They do not know markets, risk, or order policy.
- **§5.5 — per-event configuration.** TOML is deserialized and locally
  validated before runtime construction. Cross-node compatibility belongs in
  a compiled run plan, not in leaf factories.
- **§5.6 — decision.** A precompiled keyword/catalog view and one-shot policy
  transform detections into intents without network access.
- **§5.7 — execution and book cache.** Market data is projected into local
  snapshots; execution consumes intents plus market/risk decisions and emits
  durable outcomes.
- **§5.8 — risk and controls.** Hard ceilings are code constants; runtime
  configuration may only tighten them. Kill scope is session-local.
- **§5.9 — telemetry.** Typed stage observations are folded into the run
  report without becoming a business-policy dependency.

## §6–§8 — unrecovered

No surviving citations state these sections. Slots are reserved.

## §9 — acceptance regimen

- **§9.1 — detection latency fixture.** Synthetic and corpus fixtures measure
  the real pipeline and preserve the timestamp chain.
- **§9.2 / §9.4 — unrecovered.**
- **§9.3 — demo parity only.** Demo systems may prove signing and payload
  shape; they do not calibrate production liquidity, latency, or fills.
- **§9.5 — live shadow.** The full execution path computes and journals
  orders without sending them.

## §10 — kill criteria

The system stops when safety invariants fail or measured edge disappears.
Repeated lost races, stale inputs, dead ingress, dropped intents, or
unreconciled outcomes are explicit health/verdict states—not log trivia.
Event-specific stop procedure belongs in a runbook.

## §11 — risk hard-cap policy

Hard risk ceilings are not user-overridable upward at runtime. Raising one is
a reviewed code change. Configuration can only reduce exposure. The kill
switch cancels only orders registered to the current session.

## §12 — external verification ledger

External wire and venue facts are tracked by stable question/claim IDs in
`VERIFICATIONS.md`, with dates and evidence. Code must not depend on remembered
API behavior.

## Milestone definitions

| Milestone | Acceptance meaning |
|---|---|
| M0 | Deterministic replay pipeline and latency report |
| M1 | Detector corpus, bake-off protocol, and accepted engine |
| M2 | Live execution layer with bounded probes and scoped kill |
| M3 | Live telephony capture through the production launch mechanism |
| M4 | Reviewed transition beyond probe-size risk posture |
| M5 | Region/ingress/runtime latency posture accepted from measurements |

Current completion and priorities belong only in `docs/STATUS.md`.
