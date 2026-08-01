# Node: `control.acquire`

## Job

Resolve one qualified venue into a host-usable media or phone access plan,
without starting the event runtime or dialing.

## Owns

Venue resolution and probing in `scripts/choruscall-resolve.py`,
`scripts/youtube-resolve.sh`, `scripts/venue-probe.sh`, and
`scripts/manifest-probe.sh`.

## Does not own

It does not classify whether the event is worth staging, decode PCM into the
runtime port, assemble or launch the arm, create/hang up a phone session,
supervise reconnect policy, or judge the finished run.

## Input contracts

Inputs are a qualified access target, venue-specific public/secret material,
deadline, host vantage, and requested media/access parameters. A URL
must be tested for decoded audio from the actual capture host when venue policy
requires it. Resolution can run in parallel with prepare and admission; its
finished plan is consumed by preflight.

## Output contracts

The desired output is an `AccessPlan`: `AcquiredMedia` contains endpoint or
argv, PCM format, expiry, vantage, and provenance; `PhoneAccessPlan` contains
the stream endpoint plus secret-reference/session parameters but no CallSid.
Today the handoff is usually `$HOME/<slug>-stream.url`, command argv in TOML,
or event environment variables.

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

## Migration status

Resolution is fragmented across scripts, URL files, environment, config argv,
and launch wrappers. Introduce a versioned `AccessPlan`; then make preflight,
`source.pipe`, launch, and phone-session control consume it without parsing
shared files or moving a dial ahead of listener readiness.
