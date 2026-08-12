# Node: `control.discover`

## Job

Enumerate upcoming tradeable mention events and publish their real call dates
with provenance.

## Owns

Public-event enumeration, pagination/backoff, family coverage, call-date
resolution, sweep-delta reporting, and the `slate/latest.json` schema in
`scripts/discover-events.py`. `scripts/slate-daily.sh` is its current scheduled
legacy orchestrator; it writes dated discovery deltas to `slate/changes/`,
whose accumulated history is archived at `docs/history/slate-changes/`
(evidence.measure) since 2026-08-12.

## Does not own

It does not judge venue access, prepare event configs, pin a binary, resolve a
media URL, launch an arm, or decide whether an event is ready.

## Input contracts

Inputs are public Kalshi event/series responses, an explicit horizon, and the
known recurring-series/date rules. Ticker suffixes are never accepted as call
dates unless an explicitly registered daily-series rule says so.

**Both sweeps query `status=open`.** That is a load-bearing property, not an
implementation detail: every event in the union is open by construction, so a
resolved call date lying in the past is a self-refuting pair and the node is
obliged to say so.

## Output contracts

`slate/latest.json` contains discovered events, resolved date plus source,
unresolved/disputed flags, and differences between broad and per-series sweeps.
Unknown dates remain explicit and are not silently dropped.

`stale_open_event` is true when an event came back OPEN with a call date that
has already passed. It marks the date as **untrustworthy** — never as a second
date source, the same discipline as `ticker_date_misleading`. A stale event is
reported outside the horizon filter and always earns the issuer-page
confirmation fetch, because re-deriving its real date is the only thing that
makes it actionable. DAILY_SERIES are exempt: their past events are genuinely
past, not rolled forward.

This exists because of PGR, 2026-08-04. Kalshi reuses ONE event ticker per
series for the standing "*What will <issuer> say during their next earnings
call?*" and does not re-date `sub_title` when the event rolls forward.
`KXEARNINGSMENTIONPGR-26JUL15` still read "On Jul 15, 2026" on Aug 4 while its
12 markets were open and trading — measured 14:33–14:54Z, during Progressive's
Aug 4 call. `today <= call_date` dropped it, so Progressive was never on the
slate at all and ~178,000 contracts traded on a board we were recording to WAV.
Note `ticker_misleads()` is structurally blind here for the HOOD reason: ticker
and stale `sub_title` are wrong *together*, so they agree.

## Side effects and exclusive resources

The core script performs read-only public HTTP calls and may atomically replace
the requested JSON path. The scheduled wrapper also writes `slate/changes/`,
regenerates a worklist, notifies, and commits `slate/`; those are legacy
cross-node effects.

## Failure contract

Pagination, rate-limit exhaustion, malformed responses, or a failed frozen
date regression return nonzero and preserve visibility. A partial sweep must
not masquerade as an empty valid slate.

## Parallel work boundary

Discovery logic and slate schema stay in `scripts/discover-events.py`. Venue
classification begins only after the slate artifact; scheduler/notification/
git behavior should be extracted from `scripts/slate-daily.sh`, not expanded.

## Verification

Run `python3 scripts/discover-events.py --self-test`, then a bounded live sweep
to a temporary output and validate its schema. Run
`scripts/slate-daily.sh --dry-run` for the current integration seam.

The self-test freezes three misdating classes as offline fixtures, and each one
is a real event that was nearly or actually lost: **BA** (ticker and sub_title
disagree), **HOOD** (both wrong together, only the settlement source carries the
truth), and **PGR** (sub_title stale on a still-open board). It also asserts the
four cry-wolf guards on the PGR detector — a call today, a future call, an
unresolved date, and a daily-series past event must none of them flag.

## Migration status

The discovery artifact is already structured. `scripts/slate-daily.sh` still
combines discovery, qualification, notification, and git publication; split it
into consumers of a versioned `Slate` result before adding more lifecycle work.
