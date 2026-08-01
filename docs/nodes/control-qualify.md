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
