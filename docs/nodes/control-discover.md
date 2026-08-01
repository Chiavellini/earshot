# Node: `control.discover`

## Job

Enumerate upcoming tradeable mention events and publish their real call dates
with provenance.

## Owns

Public-event enumeration, pagination/backoff, family coverage, call-date
resolution, sweep-delta reporting, and the `slate/latest.json` schema in
`scripts/discover-events.py`. `scripts/slate-daily.sh` is its current scheduled
legacy orchestrator; dated discovery deltas under `slate/changes/` belong to
the same node.

## Does not own

It does not judge venue access, prepare event configs, pin a binary, resolve a
media URL, launch an arm, or decide whether an event is ready.

## Input contracts

Inputs are public Kalshi event/series responses, an explicit horizon, and the
known recurring-series/date rules. Ticker suffixes are never accepted as call
dates unless an explicitly registered daily-series rule says so.

## Output contracts

`slate/latest.json` contains discovered events, resolved date plus source,
unresolved/disputed flags, and differences between broad and per-series sweeps.
Unknown dates remain explicit and are not silently dropped.

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

## Migration status

The discovery artifact is already structured. `scripts/slate-daily.sh` still
combines discovery, qualification, notification, and git publication; split it
into consumers of a versioned `Slate` result before adding more lifecycle work.
