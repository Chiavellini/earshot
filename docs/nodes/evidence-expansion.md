# Node: `evidence.expansion`

## Job

Survey adjacent mention-market surfaces (the full Kalshi Mentions census and
Polymarket mirrors) read-only, and rank where the capture→detect→fire infra is
reusable — phone-lane classes first.

## Owns

- `docs/EXPANSION.md` — the ranked target table and verdicts
  (CONTINUE / PROBE / PARKED / KILL / NOTE).
- `scripts/pm-capture.py` — read-only Polymarket book/trade capture sidecar
  (curl transport; Q-PM pin explains why).
- `scripts/pm-edge-report.py` — post-event join of PM capture against our
  detection intents and the Kalshi tape (not yet written; owned here so the
  boundary is fixed before it exists).
- `configs/pm-map-*.json` — per-date Kalshi-event → PM-token maps consumed by
  the capture sidecar.

## Does not own

- The Kalshi census tool and its baseline (`scripts/mentions-census.py`,
  `slate/census.json`) — `control.discover` owns discovery; this node only
  reads the census output for triage.
- Any order path, wallet, or venue credential — execution on a second venue is
  explicitly out of scope until an edge report justifies designing it
  (user decision 2026-07-30).
- Latency/competitor measurement methodology — `evidence.measure` owns the
  measurement doctrine and `VERIFICATIONS.md`; this node contributes evidence
  through it.

## Input contracts

- `control.events.discovered` — which events we armed (join keys for capture).
- `evidence.measurement.result` — detection/latency artifacts the edge report
  joins against (`state/<event>/report.json` decision intents, Kalshi tape).

## Output contracts

- `evidence.pm.capture` — `state/pm/<event>/pm-<date>.jsonl.gz`: one row per
  PM top-of-book change (deduped on the CLOB book `hash`) and per new trade;
  every row carries `wall_ns` + `mono_ns` (our clocks) and the venue timestamp
  (`server_ts_ms` for books, `server_ts_s` for trades). Gzip members are
  sync-flushed so a hard kill still leaves a valid stream.

## Side effects and exclusive resources

- Network: unauthenticated GETs/POSTs to `gamma-api.polymarket.com`,
  `clob.polymarket.com` (curl only — TLS-fingerprint filter, Q-PM),
  `data-api.polymarket.com`; ~1.1 req/s sustained during capture.
- Filesystem: `state/pm/**` on the box; `state/pm/pm-capture.{log,pid}`.
- No credentials, no locks, no ports.

## Failure contract

- Capture request failures are counted and heart-beaten
  (`pm-capture beat: … errors=N`), never fatal; a dead capture is visible as a
  stale log and a dead pid, both checked manually until the sidecar is wired
  into event autorun (only after two clean standalone runs).
- The edge report must refuse to emit a verdict without ≥3 overlapping armed
  events (provable-improvements rule) — "not-resolvable" is a valid verdict.

## Parallel work boundary

This node's files may change freely without coordinating with runtime nodes.
Wiring `pm-capture` into `event-autorun.sh` crosses into `control.supervise`
and requires a claim there.

## Verification

- `python3 -m py_compile scripts/pm-capture.py` and a bounded local run
  (`--until` now+15s) writing valid gzip rows with zero errors.
- Live deploy evidence 2026-07-30: capture on the box from 15:49Z for
  `KXEARNINGSMENTIONAAPL-26JUL30` + `KXEARNINGSMENTIONAMZN-26JUL30`.
- Wire facts pinned in `VERIFICATIONS.md` Q-PM (dated, with the exact
  endpoints exercised).

## Migration status

v1: standalone sidecar + hand-generated per-date map; edge report pending
first overlapping captures. Next structured artifact: `pm-edge-report.py`
output schema (per-keyword Δt and hypothetical-fill table) once ≥3 events are
banked. Execution-path design is gated on that report.
