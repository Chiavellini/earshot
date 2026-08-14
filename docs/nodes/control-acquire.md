# Node: `control.acquire`

## Job

Bind one qualified venue's non-secret webcast access strategy and resolve it
into host-usable media without starting the event runtime or dialing.

## Owns

Venue resolution and probing in `scripts/choruscall-resolve.py`,
`scripts/youtube-resolve.sh`, `scripts/venue-probe.sh`, and
`scripts/manifest-probe.sh`, plus both ends of the resolver split —
`scripts/push-url-file.sh` (the host that mints) and `scripts/urlfile-wait.sh`
(the host that captures).

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

### The resolver split (`preresolved`)

Some venues cannot be resolved on the host that will capture them: the us-east
box has no browser and no operator to pass a registration wall, and THE AMAT
RULE puts the session on the laptop at prep time. The `preresolved` resolver
kind names that arrangement. Its resolver argv invokes
`scripts/urlfile-wait.sh` — it mints nothing and waits — and the plan
additionally records a `resolver.preresolve` block naming the kind and exact
argv of the leg that DOES mint the URL on the other host. That block is present
only for `preresolved`; every other kind's `resolver` object is byte-identical
to what it was before the kind existed, which matters because
`access_plan_sha256` is baked into the PCM proof and re-checked by
`capture-ready`.

`bind` derives the wait's bounds rather than trusting an operator to retype
them, appending to the argv: `--deadline-utc` equal to `resolver.deadline_utc`,
`--expect-plan-sha256` equal to the RunPlan's `plan_sha256`, and `--fresh-secs`.
An operator-supplied value that disagrees is refused. Because they live in argv
they are covered by the plan hash, so a fire-time environment variable can
narrow those bounds but never widen them. `preresolved` argv must also spell
URL files with a literal `$HOME/` prefix and must name exactly the URL files its
sources read: the runtime expands no other spelling, so `${HOME}/` would make
the wait address a path under the repository.

`preresolved` is deliberately the one kind whose capture vantage may not be
defaulted — see below.

### Capture vantage

`capture.vantage` is a deliberate label, not a copy of the execution host. A
defaulted vantage is indistinguishable in the artifact from a chosen one, so
capture from two different networks would pool silently — the hazard
`scripts/venue-probe.sh` names when it tells an operator to label a bot-walled
capture CDMX-vantage and never pool its Δrace with us-east. `bind` therefore
refuses to default the vantage for a `preresolved` plan (cross-host by
construction) or for a plan compiled for the capture box while binding runs
elsewhere. Every other cross-host default gets a loud WARN and still binds:
this node must not redden callers whose plans it knows nothing about. The
pooling guard itself belongs to `evidence.measure`, not here.

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

`scripts/push-url-file.sh` additionally reaches another host over SSH. The URL
bytes travel on **stdin** and never in argv — these URLs carry session tokens
and argv is `ps`-visible — so the only thing on the remote command line is the
url-file's basename, which is regex-validated before it reaches a shell. The
literal `$HOME` survives into the remote shell, because the destination is the
executing host's home and not the pusher's. Publication is a remote temp file
in the target's own directory, `chmod 0644`, then rename; the landed bytes are
proven by reading back their hash. Alongside the url-file it publishes a
sidecar `<url-file>.meta` carrying `run_plan_sha256`, `url_sha256`, and
`pushed_utc`. Each push appends one line to `state/<EVENT>/url-push.jsonl`
recording the hash and never the URL, so the journal outlives the token.

Neither script ever prints the URL. Everything a fire-time resolver writes
lands in `state/<EVENT>/arm-on-url-<RUN_ID>.log`, which `pull-box-state.sh`
copies back to the laptop; the url-sha is the durable identifier, following
`pcm-gate`.

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

`scripts/urlfile-wait.sh` exits 0 only when the url-file present on this host
belongs to THIS attempt; 3 = the resolver deadline passed with no such file;
2 = usage, including a missing `--deadline-utc`. It fails closed on that
missing deadline rather than inventing one, because `arm-on-url` runs a
resolver with no timeout and only tests the deadline after the child returns —
a wait that bounded itself would sail past the call. Acceptance requires a
regular non-symlink file whose first line is an `http(s)` endpoint, plus a
sidecar whose `run_plan_sha256` equals this attempt's and whose `url_sha256`
equals the file's. **Attempt identity, not mtime, is what refuses a prior
attempt's file**: the url-file path is derived from the pinned config, so every
attempt of one event reuses it — AMAT 2026-08-13 ran two attempts eleven
minutes apart against one `$HOME/amat-stream.url` with one `20:45:00Z`
deadline, and no freshness window wide enough to cover the fire window can
separate them. `--fresh-secs` remains a sanity bound (mtime not older than the
bound, not further ahead than tolerated clock skew) and defaults to the 90-min
fire window: T-75 scheduler lead plus the event-start+15-min deadline.

`scripts/push-url-file.sh` exits 0 only when the pushed bytes were read back
and hashed equal; 1 = refused; 2 = usage. It refuses a plan whose resolver kind
is not `preresolved` (a native resolver runs on the executing host and needs no
push), a terminal or archived RunPlan, and — importantly — a run that has
already reached `capture_ready`: real configs `cat` the url-file inside the
pipe source, so a late push would silently swap what the detector listens to
under a running arm. A pushed endpoint that does not look like an HLS playlist
is a loud WARN rather than a refusal, because the PCM gate requires HLS
continuity and a DASH manifest can never reach `capture_ready`.

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
`scripts/venue-probe.sh --check`, `scripts/urlfile-wait.sh --check`,
`scripts/push-url-file.sh --check`, and the source node's
`python3 scripts/hls-edge-fetch.py --selftest`. Live venue tests record vantage
and decoded-byte evidence.

`push-url-file.sh --check` exercises the transport against a stubbed `ssh`
(`EARSHOT_SSH`) that performs the remote command for real against a fake home,
so atomicity, mode, and the absence of a leftover temp file are demonstrated
rather than asserted. Its load-bearing checks are that no URL and no token
appears in any argv or on stdout, and that a transport failure publishes
nothing.

`bash tests/preresolved_arm.sh` is the end-to-end rig: a real
`push-url-file.sh` push over a stubbed ssh, then `arm-on-url` driving the real
wait through four cases — the pushed file arms exactly once with a PCM proof; a
PRIOR attempt's file is refused (and refused by the WAIT, proven by the absence
of a `pcm-proof.json`, not by a later gate); a stale file is refused; and a URL
that never arrives exhausts the deadline with exactly one page. It also pins
seam S-E (`install-t0-agents.sh` refuses only `static-public`, so a
`preresolved` plan is first-class to it) and asserts that the resolver child
can actually read `RESOLVER_DEADLINE_EPOCH`.

Run `python3 tests/access_plan.py` for isolated schema-3 RunPlan tests covering
legacy creation/validation, canonical public and registered-session binding,
explicit non-secret attestation, exact source and URL-file binding,
idempotence/collision refusal, tamper/config-drift refusal, lane enforcement,
date selection, phone-sibling continuation, legacy-plan refusal, and proof
that resolver argv is never executed. These tests use temporary repositories
only and make no network request.

## Migration status

### Token lifetime across vantages — measured 2026-08-14

How long a laptop-minted URL stays usable from a second vantage decides whether
a pushed URL can be trusted at fire time. Method: the AMAT 2026-08-13
browser-class token (`media-server.com`), minted on the laptop at
`2026-08-13T15:09:56Z`, was fetched at **14 h 02 m** of age from both the CDMX
laptop and the us-east-1 box, with the URL supplied on stdin via `curl -K -` so
it never entered argv.

| vantage | status | TTFB | body |
|---|---|---|---|
| laptop (CDMX) | 200 | 1.151 s | 8 B (`#EXTM3U`) |
| us-east-1 box | 200 | 0.105 s | 8 B (`#EXTM3U`) |

**Authorization lifetime is at least 14 h 02 m and is not vantage-bound** — the
token outlives the laptop session by many hours and is honoured from us-east.
**The media-bearing lifetime is `not_resolvable`** from this evidence: the
manifest is empty because the call ended, not because the token expired, and
separating the two requires a second live browser-class venue. It is recorded
as unresolved rather than guessed.

The consequence for `--fresh-secs` is that its default is NOT token-derived.
The push-to-arm gap is minutes and authorization outlives it by roughly two
orders of magnitude, so expiry is not the risk freshness guards; a prior
attempt's file is, and attempt identity handles that. The default is therefore
the fire window (5400 s = T-75 lead + start+15 min deadline), stated as a
sanity bound.

The versioned AccessPlan, explicit access-prerequisite gate, and preflight
consumer now exist. Resolvers still publish their compatibility
`$HOME/<slug>-stream.url` files, and launch/source adapters still consume those
files directly. Canonical operations must invoke `check-day` between RunPlan
compilation and preflight; venue classification remains a typed operator input
because this node cannot truthfully derive registration state from a pipe
command. This deliberately does not move phone dialing ahead of listener
readiness or substitute a plan for the later decoded-PCM proof.
