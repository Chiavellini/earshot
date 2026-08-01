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

## Output contracts

The current output is `slate/worklist.md` plus `ESCALATE:` lines for
`NO-ENTRY`, `STALE`, `UNKNOWN`, and disputed-date cases. The target
`QualificationReport` must preserve those statuses and evidence provenance.

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
