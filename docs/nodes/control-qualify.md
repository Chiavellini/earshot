# Node: `control.qualify`

## Job

Determine whether a discovered event has a sufficiently current, evidence-backed
listen venue and expose every unresolved access fact.

## Owns

Slate/registry reconciliation in `scripts/recon-worklist.py`, dial-in evidence
classification in `scripts/mine-dial-in.py`, and the durable access registry
schema in `configs/dial-ins.toml` documented by `docs/DIAL-INS.md`. It owns the
public say/Q&A evidence fetcher `scripts/fetch-say-qa.sh`, its prior-evidence
guide in `docs/SAY-QA-PRIOR.md`, and the reviewed `slate/worklist.md`.

**One writer outside this node, by contract.** `scripts/registry-admit.py`
(owned by `control.admission`) writes the `admission*` / `probe_artifact`
fields of `configs/dial-ins.toml`. It is owned there, not here, because
`control.qualify` already feeds `control.admission`, so owning the consumer of
`control.event.admitted` would close a dependency cycle that `doc-lint`
rejects. Writing another node's artifact is a data flow, not co-ownership.

This does not weaken THE MSFT RULE, it enforces it: admission may still be set
ONLY by a probe dial, and the probe is now the thing that writes it. What is
forbidden remains forbidden — paper evidence (a filing, an IR page, the hunt)
may never set `admission`, and every other field here stays human-audited. The
writer is comment-preserving by construction: it does surgical line edits and
never a `tomllib` parse-and-dump, because the rules and per-venue history in
that file are load-bearing documentation a round-trip would delete.

It also owns the intake of `control.hunt`'s findings, because the registry is
this node's file and the hunt is forbidden to write it:

- `scripts/registry-propose.py` turns hunt verdicts into paste-ready
  `[ISSUER]` blocks for issuers the registry does not yet carry. It **never
  writes** `configs/dial-ins.toml`. Every proposed block states
  `admission = "untested"` and `listen_access = "unknown"` — a filing can
  establish neither, and inferring them is exactly what THE MSFT RULE and THE
  APPLE RULE forbid.
- `recon-worklist.py` reads `slate/hunt-status.json` as enrichment, adding
  `HUNT-FOUND` (a bridge is already on file, so the row is a probe away) and
  `HUNT-BLIND` (the hunt could not look — no 8-K matched, or no CIK because
  the issuer files 6-K). `HUNT-BLIND` is deliberately distinct from a
  `bridge_unpublished` verdict, which is a real negative across a scanned
  window. A missing or malformed hunt file degrades to the pre-hunt behaviour
  rather than failing the nightly job.

## Does not own

It does not discover the event, store rotating phone secrets, prove live bridge
admission, resolve a day-of stream URL, generate market keywords, or launch.

## Input contracts

Inputs are `slate/latest.json`, `configs/dial-ins.toml`, a horizon, and a
staleness threshold. Registry facts include independent `listen_access`,
`qa_mechanism`, tier, evidence URL, verification date, and number source.

It also owns `scripts/board-open.py` — "does this issuer have an open Kalshi
mention board right now", one public unauthenticated GET. It is a read-only
market fact, which is this node's job, and it has two consumers outside it:
`evidence.capture`'s refuse-to-record guard and `control.grade`'s missed-arm
reconciliation. `--any-status` answers the retrospective form ("was there a
traded board"), which reconciliation needs because a board settles.

## Output contracts

The current output is `slate/worklist.md` plus `ESCALATE:` lines for
`NO-ENTRY`, `STALE`, `UNKNOWN`, and disputed-date cases. The target
`QualificationReport` must preserve those statuses and evidence provenance.

`STALE-DATE(open, kalshi=<date>)` marks an event whose board is OPEN but whose
only known call date has passed. It **bypasses the horizon filter entirely**,
and that is the whole point of it. The `DATE-DISPUTED` rescue does not reach
this class: that one substitutes the issuer's date for Kalshi's, and a stale
event frequently has no believed date at all (PGR's IR fetch returned HTTP
404). Such a row is shown with the stale date, flagged, and escalated — an open
board with no registry entry and no resolved date is the most urgent row the
worklist can hold, so its negative `days_out` is honest rather than a bug. A
past date WITHOUT the open-board contradiction stays filtered out; the horizon
must keep doing its job or every settled event returns.

Dropping this class is what left Progressive off the slate on 2026-08-04 while
its board traded ~178,000 contracts through the call.

## Side effects and exclusive resources

Reconciliation reads committed artifacts and writes the worklist. Research may
read public issuer pages/transcripts; rotating numbers and passcodes remain in
gitignored event environment files.

## Failure contract

Unreadable input or invalid dates return nonzero. Missing or stale evidence is
an explicit unresolved status, never inferred as no access. Transcript evidence
alone cannot prove `listen_access` or admission.

## Parallel work boundary

Registry schema/classification changes belong here. Admission probes, endpoint
resolution, prepared configs, and secret event values belong to later nodes and
must enter through named facts rather than edits to qualifier internals.

## Verification

Run `python3 scripts/recon-worklist.py --check` when available and
`python3 scripts/mine-dial-in.py --check` against its pinned fixtures; regenerate
a temporary worklist from the committed slate and registry.

## Migration status

The durable registry is a good single-purpose artifact, but the qualifier emits
Markdown/stdout rather than a machine result and the daily wrapper parses text.
Add a versioned JSON/TOML qualification result while retaining the human table.

Registry intake is proposal-only by design, not by omission: a human pastes
each block. That is the right posture while `admission` is the field
`slate-stage.sh` gates a phone arm on, and while it can only be established by
a real call. If registry writes are ever automated, the accepted-observation
artifact described in `control-admission.md` is the prerequisite.
