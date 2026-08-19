# Counting rules — the laws every trade-gap number obeys

Three verbatim trap statements from the tree, then the definitions. Violating
any of these has already produced a wrong grade at least once.

## 1. The `send_ts` trap (orders)

`scripts/post-event-report.sh`: "orders: count send_ts, NEVER `.orders|length`
(26AUG05 miscount)":

```python
o = j.get("orders") or []
sent = [x for x in o if x.get("send_ts") is not None]
# "orders ACTUALLY SENT : … <- the only number that means a real order"
```

`scripts/race-postmortem.py`: "It does not count `len(orders)` as orders
placed. The non-crossing posture short-circuits before `send_order()`, leaving
records with `send_ts=null`. Only `send_ts is not None` counts as reaching the
wire." Canonical equivalent: a `WireAttempted` transition in
`orders/order-evidence.jsonl`; run-audit's `attempted = wire_attempt is not
None`, `accepted = exchange_accepted is True`.

## 2. The `lost_races` trap (fanout)

`docs/history/chiavellini.md` §1/§7.6: the report aggregate `lost_races` is
broken on fanout runs — it reported 0 on three 2-leg runs whose records
carried `lost_race: true` on 11/12/5 orders. **Count order records, never the
aggregate.** (Correct on 1-leg runs; do not trust it anyway.)

## 3. The `edges{}` trap (latency)

`scripts/race-postmortem.py`: "It does not use `report.edges{}` for latency.
Measured 26AUG05 TLN: 371 of 534 ms was in-process, and a capture->decision
edge understates the real budget ~4x. Every latency number here is anchored to
an EXTERNAL reference (the tape) or to a full `audio_ts->ack_ts` span."

Decomposition recipe: per detection, `model_lag = audio_consumed_ms −
audio_pos_ms` and `inference = emit_ts − audio_ts` (from
`report.detections[]`); `edges{}` only for detect→decision, decision→exec,
exec→send, send→hdrs, hdrs→ack; sanity-check against the full audio_ts→ack
span. Also: histogram quantiles cannot be merged across runs — a cross-run
"p95" is a distribution of run-level p95s; execution edges are
success-conditioned (their absence is not speed).

## The funnel (definitions)

| stage | legacy (`report.json`) | canonical (`order-evidence.jsonl`) |
|---|---|---|
| detections | `detections[]` (dedupe by keyword+tier; note `suppression`) | same report |
| intents | `orders[]` records | `IntentEvaluated` |
| send requested | — | `SendRequested` |
| WIRE | `send_ts != null` OR `order_status == "executed"` | `WireAttempted` |
| accepted | ack fields | `ExchangeObserved.exchange_accepted == true` |
| filled | fill fields on the record | `FillEvidence` with positive fill |
| refused | `refusal`/reason fields per record | `NotAttempted{reason}` |

Outcome classes to report separately (26AUG06 CELH ruling): **filled /
lost-race / notional-refused / no-offers / observe-by-design**. A 1¢
non-crossing record is a DESIGNED observe (memory: "OBSERVE 1¢ is not a $1
bet"), not a lost race. An `EventNotional` refusal is arithmetic, not a bug —
but it can only appear in runs banked on or before 2026-08-18: the per-event
notional cap was removed that day (O8) precisely because its frequency WAS the
finding. Post-ruling, a notional refusal is per-ORDER or the probe ledger, and
you must say which.

## Evidence location (say which you read)

| artifact | canonical (E1: never yet live-populated) | legacy (today's reality) |
|---|---|---|
| orders | `var/runs/<E>/<R>/orders/order-evidence.jsonl` | `state/<E>/report.json` `orders[]` — **box first**: `state/<E>/box/report.json` |
| book | `var/runs/<E>/<R>/book/kalshi-tape.jsonl` | `state/<E>/capture/book-tape.jsonl` |
| audio | `var/runs/<E>/<R>/capture/` | `state/<E>/capture/call.wav` |
| grade | `var/runs/<E>/<R>/grade/verdict.json` | none (postmortem.json + precision-score.txt) |
| market tape | — | `state/<E>/tape-{precall,postcall}.json` (`fetch-tape.sh`) |

Always `scripts/opsctl.sh pull` before reading — `deploy-box.sh` excludes
`state/` AND `var/runs/` both directions (E3): a box run is invisible until
pulled, and HIMS 26AUG10 was graded "0 orders" with 2 wire orders because of
exactly this.

## Clock rules

- Two anchors (`scripts/clock-anchor.py`, start + end) or no sub-second
  cross-host claim. Box drift measured 23.889 ppm. An anchor is valid only
  within one awake session on one host.
- Kalshi batches ladder executions into a single microsecond timestamp
  (measured 08-10: 119 prints / 6 timestamps) — never sequence an intra-ladder
  race from print order; the give-back lives in the first ~20 ms.
- `race.executable_margin_ms` (system-analysis v1) requires two-anchor affine
  conversion — a single-anchor number does not qualify as an observation.

## Current tooling gaps (check before promising a number)

1. No book-tape analyzer exists — only run-audit's `verify_book_tape` +
   `depth_analysis` (cap-depth exhaustion only). The book↔`decision_ts` race
   join is named in `capture-sidecar.sh` and UNBUILT.
2. `drace-wallclock.py` / `abs-lag.py` / `leg-lag.py` need `<leg>.jsonl`
   (written only by `capture-bakeoff`) — they cannot run on an armed run.
3. `run-audit.py` hardcodes `precision: not_resolvable` until M3 (transcript
   score into RunGrade) closes.
4. `system-analysis.py` has ZERO registered metric validators — it can
   diagnose, never certify; top-level verdicts stay `not_resolvable`.
5. `daily_loss_cap_cents` is parsed and never enforced (O1); `settle-score.py`
   is the only realized-P&L computation and it is offline.
