# Node: `control.hunt`

## Job

Search SEC primary sources for an issuer's earnings-call dial-in bridge and
publish evidence-backed number candidates for admission to probe.

## Owns

Source selection and query construction in `scripts/hunt-dial-in.py`, the
scheduled sweep in `scripts/hunt-daily.sh` and its launchd unit, the hunting
playbook in `docs/HUNT.md`, and the two-artifact split that keeps rotating
digits out of version control: the gitignored `state/hunt/<KEY>-<quarter>.json`
carrying the numbers and passcodes, and the committed digits-free
`slate/hunt-status.json` carrying tier, verdict, and provenance.

## Does not own

It does not decide admission, place a call, edit the access registry, discover
events, prepare an event configuration, resolve a webcast stream, or store the
digits it finds anywhere a commit can reach. `configs/dial-ins.toml` and
`docs/DIAL-INS.md` remain owned by `control.qualify`; probing remains owned by
`control.admission`.

## Input contracts

`control.events.discovered` supplies the slate whose issuers define the hunt
horizon. `platform.notification` is the alert channel for a failed sweep. The
hunt itself needs no credentials: SEC full-text search and the Archives are
public, and a descriptive `User-Agent` is the only requirement.

## Output contracts

`control.bridge.candidates` — a per-issuer candidate record with classified
numbers (`bridge` / `replay` / `contact`), labelled passcodes, the filing URL
and file date that produced them, and a tier verdict of `published`,
`bridge_unpublished`, or `unknown`. Digits live only in the gitignored half;
the committed half carries provenance and is what fills the previously empty
"start from" column in `slate/worklist.md`.

## Side effects and exclusive resources

Read-only HTTPS to `efts.sec.gov` and `www.sec.gov`, paced below SEC's
published 10 req/s ceiling. Writes `state/hunt/**` and
`slate/hunt-status.json`, and its scheduled wrapper commits only the latter.
It places no telephone calls and touches no Twilio or Kalshi credential.

## Failure contract

A hunt that finds nothing is a SUCCESS carrying a verdict, not an error:
absence across two or more quarters is the evidence that distinguishes a
dropped dial-in from a standing webcast-only policy. Network and HTTP failures
are recorded per filing and never abort the sweep. Unreadable inputs exit 2.
A candidate number is never promoted to `admission` — only
`scripts/bridge-probe.sh` may do that, and the summary says so on every run.

## Parallel work boundary

Everything under `scripts/hunt-*`, `docs/HUNT.md`, `state/hunt/**`, and
`slate/hunt-status.json` changes here. Writing a discovered number into
`configs/dial-ins.toml` is a `control.qualify` change and requires coordination
with that node's open claim; adding a probe input is a `control.admission`
change.

## Verification

`python3 scripts/hunt-dial-in.py --self-test` re-derives four frozen control
cases offline with no network: CMG's bridge/international/media-contact
discrimination plus its passcode, YUM's slash-separated numbers and its
playback-versus-bridge dominance rule, a replay-only transcript that must yield
no bridge, and Ford's absence-is-a-verdict case. `scripts/hunt-daily.sh
--dry-run` prints every decision and commits nothing. Live evidence lands in
`VERIFICATIONS.md` under the EDGAR pin.

## Migration status

The EDGAR lane is implemented and verified against four control cases. Issuers
who publish only on an IR page — Apple is the type specimen, and its 8-Ks carry
no dial-in — are not yet covered, because those pages are Cloudflare
bot-challenged identically from the laptop and the box; the Wayback CDX lane
and a browser-assisted path are the next extraction. Non-issuer venues
(FOMC, hearings, pressers) have no CIK and need both a different source set and
a registry schema that is not issuer-keyed. The candidate record is written as
JSON but is not yet consumed programmatically by `bridge-probe-batch.sh`, whose
`--numbers` format still differs from the one file on disk that feeds it.
