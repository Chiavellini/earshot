# Node: `evidence.expansion`

## Job

Survey adjacent mention-market surfaces (the full Kalshi Mentions census and
Polymarket mirrors) read-only, rank where the capture→detect→fire infra is
reusable, and run the standing cross-venue observatory that measures the
**time**, **volume** and **depth** difference between Kalshi and Polymarket on
every shared market.

Depth is a third axis, not a detail of volume. Volume asks how much *traded* —
which measures the competition. Depth asks how much rests at a price we could
*take* — which measures the opportunity. They diverge precisely where thin flow
lives, so a venue that trades almost nothing can still be worth executing on,
and volume alone cannot say.

## Owns

- `docs/EXPANSION.md` — the ranked target table and verdicts
  (CONTINUE / PROBE / PARKED / KILL / NOTE).
- **Recorders (raw tape layer).** `scripts/pm-tape.py` — Polymarket push
  recorder over the CLOB market WebSocket (Q-XVENUE-WS).
  `scripts/pm-capture.py` — the older 1 Hz REST poller, kept as the proven
  fallback and because ~39k rows of banked data are in its shape.
- **Normalizer.** `scripts/xvenue-normalize.py` — every tape shape into one
  canonical record at `XV_SCHEMA`.
- **Report.** `scripts/xvenue-report.py` — the written artifact (Δtime,
  Δvolume, verdict) plus `slate/xvenue-ledger.json`.
  `scripts/pm-edge-report.py` — the 2026-08-03 original, superseded and kept
  only so its result stays reproducible.
- **Lane.** `scripts/xvenue-autorun.sh` (per event, own timer),
  `scripts/xvenue-daily.sh` + `configs/launchd/com.earshot.xvenue-daily.plist`
  (daily health + coverage).
- `configs/pm-map-*.json` — Kalshi-event → PM-token maps
  (`scripts/pm-map-build.py`).

## Does not own

- The Kalshi book recorder itself. `examples/book-tape.rs` and the `FrameTap`
  seam belong to `runtime.kalshi.book` (`docs/layers/book-cache.md`); this node
  consumes its tape.
- The Kalshi census tool and its baseline (`scripts/mentions-census.py`,
  `slate/census.json`) — `control.discover` owns discovery.
- Any order path, wallet, or venue credential. Execution on a second venue
  stays out of scope until an edge report justifies designing it (user decision
  2026-07-30).
- Latency/competitor measurement doctrine — `evidence.measure` owns that and
  `VERIFICATIONS.md`; this node contributes evidence through it.

## Input contracts

- `control.events.discovered` — which events exist (join keys for capture).
- `evidence.measurement.result` — the Kalshi public trades tape
  (`state/<event>/tape-<date>.json`, `scripts/fetch-tape.sh`) used for the
  volume axis on events with no live book recording.

## Output contracts

- `evidence.pm.capture` — raw venue tapes. Newline-delimited JSON,
  **uncompressed while live** (a hard kill must leave a readable tape;
  `xvenue-autorun.sh` gzips on clean exit). Row 0 is a `session` header
  carrying `schema_version`, venue, `feed_kind`, endpoint, host and (Kalshi)
  `clock_offset_s`; the last row is a `close` carrying `rows` and `dropped`.
  Every frame row is the **verbatim** wire text plus `wall_ns` + `mono_ns`.
- `evidence.xvenue.report` — `state/xvenue/<event>/report-*.json` + `.md`, and
  one appended row in the committed `slate/xvenue-ledger.json`.

## Side effects and exclusive resources

- Network: unauthenticated reads to `gamma-api.polymarket.com`,
  `clob.polymarket.com` (curl only — TLS fingerprint, Q-PM),
  `data-api.polymarket.com`, and `wss://ws-subscriptions-clob.polymarket.com`
  (python TLS is fine there — Q-XVENUE-WS). Authenticated **market-data only**
  WS to Kalshi for the book tape.
- Filesystem: `state/xvenue/**`, `state/pm/**`, `slate/xvenue-ledger.json`.
- No orders, no `OrderRegistry`, no STOP file, no locks, no ports.

## Failure contract

- **The lane must never be able to fail an arm.** It runs on its own timer and
  is deliberately NOT wired into `event-autorun.sh`; wiring it there would
  cross into `control.supervise` and put it inside the arm's failure domain.
- The book tap uses `try_send` only; overflow is counted into the tape's
  `close` row. A truncated tape is visible as a number, never as silence.
- The normalizer refuses a `schema_version` it does not know rather than
  guessing at a field whose meaning may have changed.
- The report must emit `NOT_RESOLVABLE`, `UNSTABLE` or `VOID` rather than a
  number it cannot defend — specifically when a lag sits inside the joint
  measurement floor, when per-segment estimates disagree on sign or scale, or
  when the two series never overlap in time (the GRAB class).
- A session shorter than `MIN_LEDGER_SPAN_S` writes a report but **never** a
  ledger row: the ledger is what the ≥3-event gate is counted from, and a
  health-check-length run flipping that gate would be a fabricated result.
- No pooled cross-event verdict below ≥3 overlapping events
  (provable-improvements rule). "Not resolvable" is a valid verdict.

## Parallel work boundary

This node's files may change freely without coordinating with runtime nodes.
The `FrameTap` seam it depends on is owned by `runtime.kalshi.book`. Wiring the
lane into `event-autorun.sh` would cross into `control.supervise` and requires a
claim there — currently a deliberate non-goal.

## Verification

- `python3 scripts/pm-tape.py --self-test` (9 checks: schema pin, endpoint
  host, frame reassembly across five TCP fragmentation points, client masking).
- `python3 scripts/xvenue-normalize.py --self-test` (26 checks: exact decimal
  parsing incl. the 0.001 tick, NO→YES conversion, end-to-end over wire-exact
  fixtures for both venues, byte-identical re-run).
- `python3 scripts/xvenue-report.py --self-test` (15 checks: word pairing,
  NO-leg exclusion, poll-raises-the-floor, a **constructed 2 s Polymarket lead
  recovered exactly**, zero-lag refused rather than reported as a win, disjoint
  windows VOID, notional ratio, ledger eligibility).
- `scripts/xvenue-autorun.sh --check`, `scripts/xvenue-daily.sh --dry-run`.
- Live 2026-08-04: both recorders end-to-end on `KXEARNINGSMENTIONAMD-26AUG04`
  (Kalshi 17 markets / 0 dropped; Polymarket 479 rows / 40 s / 0 errors),
  normalized and reported with `feed | push | push`.
- Wire facts pinned in `VERIFICATIONS.md` **Q-PM** (+ its 2026-08-04
  amendment), **Q-XVENUE-WS**, **Q-XTAPE**.

## Migration status

v2 (2026-08-04): three separated layers — raw tape → versioned normalizer →
written artifact — with both venues on push feeds and a committed ledger.
Backfilled `pltr-26aug03` and the previously unanalysed `amd-26aug04`, giving
n=2 on the volume axis; the Δt axis starts at the first event recorded with
`book-tape`, because no Kalshi book tape exists for either backfilled event.

v2.1 (2026-08-05): the **Δdepth axis** (`XV_SCHEMA = 2`). Quote rows carry the
ladder (`bids_e6`/`asks_e6`, `book_levels`, `book_depth_kind`); the normalized
row schema is now versioned separately from the raw-tape envelope, so a row
bump cannot reject tapes already banked. `takeable_axis` reports time-weighted
p50 / max / `persisted_s` per edge tier, in windows anchored on the Kalshi
reprice, gated by a caller-supplied `--precision` with no default.

What that bought, and what it did not:

- The ladder was already on disk. `pm-capture.py` has banked 5 levels a side
  since 07-30 and the v1 normalizer read only `bb`/`ba`, emitting null sizes on
  every PM row. Recovering it made two closed events answerable retroactively.
- **Δdepth is NOT_RESOLVABLE at n=1 anchored word.** PLTR yields one
  (`ai slop`, $0.00 takeable below 90¢ for the full 300 s after the reprice);
  AMD yields none, because its PM capture ended ~24 h before the call.
- The binding constraint is capture coverage, not analysis. Neither banked
  event has a Polymarket tape that spans its own call, so this axis — like Δt —
  really starts at the first event recorded across the call on both venues.

**Open:** automated Kalshi↔Polymarket pair discovery. `configs/pm-map-*.json`
is still produced by `scripts/pm-map-build.py` from a hand-supplied PM slug, so
"every shared market" is currently "every shared market someone mapped". The
frozen registry (`slate/venue-pairs.json`: scored candidates, `--promote` to
confirm, recorded rejections, and a machine-enforced GRAB-class `endDate`
guard) is designed but not built.

**Open (raised 2026-08-05):** a capture-window guard at *planning* time. The
report now catches a non-overlapping capture after the fact (the AMD coverage
caveat), but the cheaper fix is refusing to schedule a Polymarket capture whose
market `endDate` precedes the Kalshi call — the same check that catches the
GRAB class. This belongs in `slate/venue-pairs.json` when it is built. Until
then, verify by hand that a PM capture spans the call: both banked events fail
that test, which is why the depth axis has n=1.
