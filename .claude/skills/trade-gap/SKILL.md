---
name: trade-gap
description: Analyze a finished event's trading performance against the implementation and target the actual code patches and latency work. Locates and audits the run evidence, builds the truthful execution funnel (detections through fills and refusals), joins settlement, measures the race against competitor prints, decomposes latency from primary sources, and emits a prioritized patch list with A/B protocols mapped to the defect ledger. Use after an event ends when the user asks how we traded, what lost the race, where the latency went, or what implementation gaps to fix. It PROPOSES patches with evidence — it never implements code changes and never touches money policy.
---

# Trade gap — from run evidence to targeted patches

## Purpose

Consume a finished event's evidence — order journal, report, book tape, market
tape, clock anchors — and produce: a truthful execution funnel, realized and
counterfactual P&L, a Δrace verdict per market, an in-process latency
decomposition from primary sources, and a prioritized list of code patches and
latency work, each with its evidence and an A/B protocol. Implementation goes
to the repo lanes under AGENTS.md claims; this skill's deliverable is the
targeting.

## The incident this exists to prevent

"23 orders placed" was reported when the true number was ZERO — the 1¢
non-crossing posture short-circuits before `send_order()`, leaving records
with `send_ts = null`. And the system's own `edges{}` understate in-process
latency ~4×: on TLN 26AUG05, 371 of the 534 ms race deficit was in-process
(`model_lag` 360 ms), invisible to a capture→decision edge. Every number this
skill reports is counted by the rules in `references/COUNTING-RULES.md` or is
labeled `NOT_RESOLVABLE` out loud.

## Scope guard — run this before anything else

1. **Pull first** (defect E3 — box runs are invisible until pulled):
   ```sh
   scripts/opsctl.sh pull --event <EVENT_ID>
   ```
2. Locate the evidence and **say which shape you read**:
   - Canonical: `var/runs/<EVENT_ID>/<RUN_ID>/{orders/order-evidence.jsonl, book/kalshi-tape.jsonl, capture/, report/, grade/}` — defect E1: this tree has never been populated by a live run yet; check anyway, it is the future.
   - Legacy (the reality today): `state/<EVENT_ID>/report.json` (+ `state/<E>/box/report.json` — box first), `state/<E>/capture/book-tape.jsonl`, `state/<E>/capture/call.wav`, `state/<E>/journal.jsonl`.
3. `git log --oneline -3` — automation and the reliability lanes commit
   mid-session; re-check before trusting minutes-old reads.
4. If neither shape has order records or a report, the event was likely never
   armed — that is a MISSED-ARM question for `scripts/missed-arm-check.py
   --date <D>`, not a trading-gap analysis. Say so and stop.

## Phase 1 — AUDIT (canonical runs)

```sh
scripts/opsctl.sh audit --run-dir var/runs/<EVENT_ID>/<RUN_ID>
```

Deterministic, offline; on validation failure it writes nothing. The grade
(`grade/verdict.json`) carries `accounting{intents,attempted,accepted,filled,
terminal,unresolved}`, per-order transitions, book `depth_analysis`
(`detection_to_exhaustion_ms`, `wire_to_exhaustion_ms` — always
`causality: not_resolvable`; a cap-depth transition is NEVER labeled a
competitor sweep), and evidence fingerprints. Note `audio_evidence.precision`
is hardcoded `not_resolvable` until M3 closes — precision comes from
`detector-improve`'s scoring, joined here by hand.

For legacy runs, apply `references/COUNTING-RULES.md` directly to
`report.json`.

## Phase 2 — COUNT TRUTHFULLY (the funnel)

Build this funnel and report every stage — refusal reasons are findings, not
noise:

```
detections → intents (one-shot table) → send-requested → WIRE-ATTEMPTED
→ exchange-accepted → filled | refused(reason) | lost_race | no-offers
```

- Wire truth: legacy = `orders[] | select(.send_ts != null)`; canonical = a
  `WireAttempted` transition. NEVER `len(orders)`.
- `lost_races` aggregate is broken on fanout runs (reports 0 while records
  carry `lost_race: true`) — count records.
- Separate the outcome classes the way the 26AUG06 CELH lesson demands:
  filled / lost-race / notional-refused (EventNotional at cap 97 vs event cap
  100 lets exactly ONE order through) / no-offers (`best_ask=None` — most of
  08-11) / observe-by-design (1¢ non-crossing is a designed observe, not a
  lost race).

## Phase 3 — SETTLE

```sh
python3 scripts/settle-score.py --event <EVENT_ID>
python3 scripts/settle-score.py --bands      # P(YES | fired, ask) by price band
python3 scripts/settle-score.py --replay     # actual vs ask_priced vs ev_gated policies
```

Realized P&L from actual fills; the corrected EV (`100p − price − fee`,
leave-one-event-out p). Note defect O4 — the shipped ev_gate has no P(wrong)
term and gates nothing (shadow) — **report it, never fix it here**; price
bands and caps are operator rulings (SYSTEM-STATE §4). Lifetime calibration:
6 wire orders ever; do not draw distributional conclusions from one event.

## Phase 4 — RACE

```sh
scripts/fetch-tape.sh <EVENT_TICKER> state/<E>/tape-postcall.json   # baseline should exist from pre-call
scripts/race-postmortem.py --event <EVENT_ID> \
  --call-start <YYYY-MM-DDTHH:MM:SSZ> --json state/<E>/postmortem.json
```

- Sign convention: **negative Δrace = we are behind.**
- Clock honesty: two anchors or no sub-second cross-host claim (single anchor
  = no drift correction; the box drifts ~23.9 ppm ≈ 0.13 s per 90 min). The
  tool prints its anchor quality — quote it.
- Without banked audio, "the market moved and we never fired" stays
  `NOT_RESOLVABLE(no-audio)` — never becomes "detector miss".
- The tool uses the public TRADES tape, not the book tape. The
  book-tape↔`decision_ts` millisecond join is a named, unbuilt gap (see Phase
  6) — do not fake it with hand arithmetic.

## Phase 5 — LATENCY DECOMPOSITION (primary sources only)

From `report.json → detections[]` per detection:

```
model_lag  = audio_consumed_ms − audio_pos_ms     # the 4× term edges{} cannot see
inference  = emit_ts − audio_ts
```

Then `edges{}` ONLY for the downstream hops: `detect→decision`,
`decision→exec`, `exec→send`, `send→hdrs`, `hdrs→ack` — and sanity-check the
sum against the full `audio_ts → ack` span from the order record. **Never
quote `capture→decision`** (TLN-measured ~4× understatement). Name the host
vantage in every number (decision→print was 9.9 ms from us-east-1 and
send→hdrs 102 ms from CDMX — same code). Kalshi batches ladders into single-
microsecond timestamps — any intra-ladder sequencing claim from print order
overstates fill rates.

## Phase 6 — GAP → PATCH LIST

Map every finding to a `docs/SYSTEM-STATE.md` defect ID, or propose a new one.
Each patch entry:

```
problem:        one sentence
evidence:       the number + artifact path from Phases 1–5
target:         file/function (proposal only)
expected gain:  quantified where honest, else "unblocks measurement of X"
A/B protocol:   how the fix will be proven (interleaved bench cells, rehearsal
                drill, or a system-analysis spec)
```

Check the standing high-value proposals FIRST — do not rediscover them:
1. **Book-tape ↔ `decision_ts` race join** — named in `capture-sidecar.sh`,
   unbuilt; the only path to per-market Δrace at book resolution on an armed
   run (the `<leg>.jsonl` tools cannot run on armed runs).
2. **`model_lag` as a first-class telemetry edge** — the one-line fix for the
   4× blind spot.
3. **Fanout `lost_races` fix** — the aggregate is wrong; records are right.
4. NTP offset into `journal.jsonl` at arm start — kills the clock caveat.

Where certifiable evidence is wanted, emit an AnalysisSpec + ObservationRecords
for `python3 scripts/system-analysis.py --spec … --output …` — stating plainly
that v1 has **zero registered validators**, so verdicts stay
`diagnostic`/`not_resolvable`; registering the first validator (most plausibly
`race.executable_margin_ms` on top of proposal 1) is itself a valid patch
proposal.

## Phase 7 — REPORT

Write `state/<EVENT_ID>/trade-gap-report.md` (gitignored evidence, beside
`postmortem.json`): the funnel table, settlement join, Δrace per market with
anchor quality, the latency decomposition, and the patch list ranked by
(evidence strength × expected gain). Summarize in chat. Journal it
(`scripts/journal.sh --event <E> --actor trade-gap --action grade --outcome ok
--detail "wire=N filled=M drace=..." || true`). Hand patches to the repo lanes
— implementation needs an AGENTS.md claim this skill never takes.

## What this skill refuses to do

1. **Never implements the patches** — proposals with evidence only; code goes
   through the lanes' claims.
2. **Never counts orders from `len(orders)` or `lost_races`** — the counting
   rules are law.
3. **Never quotes `edges{}` capture→decision as the latency story.**
4. **Never labels book movement a competitor sweep** — cap-depth exhaustion is
   an observation with `causality: not_resolvable`.
5. **Never converts `NOT_RESOLVABLE(no-audio)` into a detector verdict.**
6. **Never touches money policy** (caps, bands, ledger, ev_gate — SYSTEM-STATE
   §4 is the operator's).
7. **Never makes a sub-second cross-host claim on one clock anchor.**

## Reference

- `references/COUNTING-RULES.md` — the counting laws, funnel definitions,
  evidence-location table, and current tooling gaps.
- `docs/SYSTEM-STATE.md` — the defect ledger patches map onto.
- `docs/SYSTEM-ANALYSIS.md` — the AnalysisSpec/ObservationRecord contract.
- `docs/history/chiavellini.md` — the worked TLN latency decomposition.
