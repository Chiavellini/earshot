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

It also owns **credential-kind classification** in `scripts/dial-in-intake.py`:
given published release or confirmation text, which digits (if any) may be keyed
into the phone, and which are spoken to a human. The governing verb decides it —
"enter / dial / followed by #" is a DTMF credential; "**referencing** / quote /
cite / provide" is a reference said to a screener. Only the first may be written
after the comma in `state/hunt/numbers.env`; a line with no comma means the venue
has no keyable credential at all, which `configs/*-autorun.env` resolves to an
empty `PASSCODE` rather than `TBD`.

## Does not own

It does not decide admission, place a call, edit the access registry, discover
events, prepare an event configuration, resolve a webcast stream, or store the
digits it finds anywhere a commit can reach. `dial-in-intake.py` emits an
admission **prior** — "expect an operator screen, prepare the handler" — and
never a verdict; THE MSFT RULE stands, and only a real probe dial sets
`admission`. `configs/dial-ins.toml` and
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

`control.bridge.numbers` — the dialable projection of the same record, written
by `--emit-numbers` to a gitignored file (default `state/hunt/numbers.env`,
mode 0600) in the one format `scripts/bridge-probe-batch.sh --numbers`
consumes:

```
# KEY=E.164[,passcode#]        one issuer per line, comments and blanks ignored
YUM=+18007159871,8719077#
```

Rules the producer guarantees, so the consumer can dial without re-deciding:

- **E.164, always.** `800/715-9871` in a filing becomes `+18007159871`. A
  number the dialer has to reformat is a number that gets misdialed.
- **Toll-free preferred.** When a filing publishes both a North American
  toll-free and an international direct line, the toll-free wins; the direct
  line is a fallback, not a peer.
- **`bridge` only.** `replay` and `contact` classifications never appear here.
  The classifier's replay-dominance rule exists precisely so an auto-prober is
  never handed a playback line.
- **Passcode carries its terminator.** The `#` is appended once, here, rather
  than in each caller.
- **A key with no passcode still emits.** Some bridges take a number alone;
  omitting the line would look like "no bridge found", which is a different
  and wrong verdict.

This is a projection, not a new source of truth: it is regenerated from
`state/hunt/**` and may be deleted at any time. It confers no admission —
every consumer still owes a probe dial.

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

`python3 scripts/dial-in-intake.py --check` asserts five pinned intake verdicts.
One is real and is the reason the script exists: the HIMS 2026-08-10 release
("...**referencing** Meeting ID..."), whose id was stored as DTMF and cost the
first 17 minutes of that call. The other four are labelled synthetic and encode
the DTMF-passcode, DTMF-conference-id, registration-gated, and bare-number
shapes. Exit codes are the machine surface: `0` DTMF, `3` spoken, `4` ambiguous
or registration-gated, `5` no dial-in.

## Migration status

The EDGAR lane is implemented and verified against four control cases. Issuers
who publish only on an IR page — Apple is the type specimen, and its 8-Ks carry
no dial-in — are not yet covered, because those pages are Cloudflare
bot-challenged identically from the laptop and the box; the Wayback CDX lane
and a browser-assisted path are the next extraction. Non-issuer venues
(FOMC, hearings, pressers) have no CIK and need both a different source set and
a registry schema that is not issuer-keyed.

The candidate-to-prober seam is closed: `--emit-numbers` writes
`control.bridge.numbers` and `bridge-probe-batch.sh` reads that path by
default. What remains manual on purpose is the registry write — a found number
becomes a `configs/dial-ins.toml` row only after a human reviews the proposal,
because `control.qualify` owns that file and admission is still probe-only.
