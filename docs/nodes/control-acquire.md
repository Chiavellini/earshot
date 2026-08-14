# Node: `control.acquire`

## Job

Bind one qualified venue's non-secret webcast access strategy and resolve it
into host-usable media without starting the event runtime or dialing.

## Owns

Venue resolution and probing in `scripts/choruscall-resolve.py`,
`scripts/youtube-resolve.sh`, `scripts/venue-probe.sh`, and
`scripts/manifest-probe.sh`.

## Does not own

It does not classify whether the event is worth staging, decode PCM into the
runtime port, assemble or launch the arm, create/hang up a phone session,
supervise reconnect policy, or judge the finished run.

## Input contracts

Inputs are the immutable schema-3 RunPlan, a qualified access target,
venue-specific public/secret material, deadline, host vantage, and requested
media/access parameters. A URL
must be tested for decoded audio from the actual capture host when venue policy
requires it. Resolution can run in parallel with prepare and admission; its
finished plan is consumed by preflight.

## Output contracts

`scripts/access-plan.py bind` publishes the canonical schema-2
`WebcastAccessPlan` at exactly
`var/runs/<EVENT_ID>/<RUN_ID>/control/access-plan.json`. The artifact is
canonical JSON with a self-hash and binds all of the following:

- the exact active schema-3 RunPlan event, run, and `plan_sha256`;
- the pinned config path/hash and every pipe source's exact argv;
- every portable `$HOME/...` URL-file reference found in those argv arrays;
- a typed resolver kind and exact resolver argv, deadline, publication mode,
  and endpoint-expiry semantics;
- the RunPlan's stable execution-host identity plus a named capture vantage;
- an explicit access prerequisite: either public access with no session, or a
  completed registration/signed-in session attested by a stable operator ID,
  named only by a non-secret `browser-profile:`/`external-session:` reference,
  and expected to remain valid beyond the resolver deadline.

It is explicitly non-secret: literal material supplied to token/password/
cookie/auth/key-shaped resolver options and URL user-info credentials are
refused; credentials remain environment or external-secret references. The
compiler does not execute resolver argv or perform a network request.

`bind` never registers, signs in, opens a browser, or tests the network. A
human-session mode is accepted only when the caller explicitly supplies the
operator ID, non-secret session reference, and UTC expiry after completing the
external action. Public access must be declared explicitly too. This stops an
ungated-looking URL from silently standing in for a registration/session fact.

The older `create` command remains a schema-1 compatibility constructor for
integrated fixtures and callers. Its artifact remains individually valid but
has no typed access prerequisite, so the canonical date gate refuses it and
requires a new immutable RunPlan attempt rather than overwriting history.
Consumers on the supported live path use `validate --canonical`, which applies
the same schema-2 requirement at the per-run door.

The distinction between **planned acquisition** and **acquired current media**
is load-bearing. Every AccessPlan says `current_media.proven = false`, carries
no PCM proof, and names `capture_ready` as the later owner of that fact.
Consequently schema-3 `access_acquired` means “the exact acquisition strategy
for this run is validated,” not “a T0 manifest currently decodes.” Launch-time
source acquisition and decoded PCM must still establish `capture_ready`.

Phone access remains outside this artifact. Its session parameters and
current CallSid belong to `control.phone_session`; phone admission strategy is
bound by preflight without putting numbers, passcodes, or a CallSid here.

`scripts/access-plan.py check-day --date YYYY-MM-DD` is the read-only daily
join consumed by canonical operations. It selects schema-3 RunPlans by their
immutable `starts_at_utc` (never a filename), verifies each ledger (chain
integrity always hard; pinned-input drift on a terminal/archived sibling is a
labeled soft pass — a superseded attempt outlives its bytes by design), skips
phone and terminal siblings explicitly, and requires every
active webcast run to have a valid schema-2 AccessPlan. It continues across
siblings but exits nonzero when any webcast is missing, legacy, corrupt, or
contradictory. A missing plan prints the generic `bind-access` next action and
the URL-file references discovered from the pinned source argv; it does not
guess venue kind, public-vs-session access, resolver argv, or credentials.

## Side effects and exclusive resources

It performs venue/network requests, may poll until live, write an endpoint
atomically, and run bounded decodability probes. Session-bound endpoint files
and resolver credentials are exclusive to one event attempt; continuous edge
fetching belongs to the runtime source and paid calls belong to
`control.phone_session`.

## Failure contract

Deadline, auth, non-media response, zero decoded PCM, rate limit, or unsupported
manifest/access plan returns nonzero and notifies through `platform.notify`
when requested. HTTP 200 alone is never proof of usable media.

`scripts/browser-resolve.py` exits 0 only when a manifest was observed and
written; 3 = deadline with nothing seen; 4 = page never loaded; **5 = a
registration/login wall is blocking the player** — detected in-loop across the
top frame and every child frame, paged IMMEDIATELY (never at the deadline;
AMAT 2026-08-13 watched a form in silence for 110 minutes), and left as the
human step it is. With `--profile` the same verdict means the persistent
session is missing or expired. While waiting it retries `--click-play`
(~30 s) and prints a heartbeat (~5 min) so a silent watch is visible in the
fire-time log. The wall decision (`is_wall`) is a pure function pinned by the
`--check` self-test.

The AccessPlan compiler/validator additionally refuses a phone or terminal
RunPlan, a config or RunPlan hash mismatch, unknown/non-pipe source shape,
source argv and declared URL-file disagreement, missing/wrong resolver
executable shape, unsafe expiry/deadline semantics, embedded secret material,
tampered/noncanonical JSON, or a different artifact already occupying the
immutable run path. Schema 2 also refuses an omitted session attestation, a
URL/cookie/token-shaped session reference, or a session whose asserted expiry
does not cover the resolver deadline. It never overwrites a semantically
different plan.

## Parallel work boundary

Resolvers and qualification probes stay here. Continuous media fetching and
source framing stay with source adapters; preflight validates the structured
result before launch; phone control consumes a phone plan only after launch
reports listener readiness; measurement experiments cannot silently alter
production posture.

## Verification

Run each resolver's offline `--check`/`--selftest` where provided,
`scripts/venue-probe.sh --check`, and the source node's
`python3 scripts/hls-edge-fetch.py --selftest`. Live venue tests record vantage
and decoded-byte evidence.

Run `python3 tests/access_plan.py` for isolated schema-3 RunPlan tests covering
legacy creation/validation, canonical public and registered-session binding,
explicit non-secret attestation, exact source and URL-file binding,
idempotence/collision refusal, tamper/config-drift refusal, lane enforcement,
date selection, phone-sibling continuation, legacy-plan refusal, and proof
that resolver argv is never executed. These tests use temporary repositories
only and make no network request.

## Migration status

The versioned AccessPlan, explicit access-prerequisite gate, and preflight
consumer now exist. Resolvers still publish their compatibility
`$HOME/<slug>-stream.url` files, and launch/source adapters still consume those
files directly. Canonical operations must invoke `check-day` between RunPlan
compilation and preflight; venue classification remains a typed operator input
because this node cannot truthfully derive registration state from a pipe
command. This deliberately does not move phone dialing ahead of listener
readiness or substitute a plan for the later decoded-PCM proof.
