# Bridge acquisition — the hunt playbook

`scripts/hunt-dial-in.py` automates the number hunt that `docs/DIAL-INS.md`
describes as manual work. This page records which sources actually work, the
query traps that cost real numbers, and where the hunt stops and a human or a
probe begins.

## Why this exists

Measured 2026-07-30: **47 open earnings-mention events, 47 distinct issuers,
39 with no registry entry at all, and exactly one venue in the whole registry
with `admission = "held"`.** We could phone-arm none of them. The recon
worklist had been flagging those 39 for weeks with an empty "start from"
column — correctly identifying the gap and offering no way to close it, because
closing it was a human errand nobody had time to run 39 times.

Meanwhile the prevalence work says **~85–90% of S&P 500 issuers run a real
bridge and only ~35–40% publish one** (two independent cohorts, n=19 and n=22).
So the modal earnings call is reachable by phone *only* via a hunt.

## Source verdicts (probed live 2026-07-30, not assumed)

| Source | Reachable | Carries numbers | Use |
|---|---|---|---|
| **SEC EDGAR full-text search** | yes (UA mandatory) | **yes, verbatim** | **primary — automated** |
| Wayback CDX + `id_` snapshots | flaky, needs backoff | yes | secondary, for rotators |
| Wayback `available` API | **429 always** | — | do not use |
| IR pages behind Cloudflare | **403 from laptop AND box** | n/a | browser only, human |
| IR pages not behind Cloudflare | yes | sometimes | opportunistic |
| Official transcripts | yes | **replay numbers only** | **never — wrong document** |
| Q4 `attendees.events.q4inc.com/rest/v1/event/<id>` | yes, no auth | no (speaker-side only) | schedule/status, absence proof |

Two corrections to older assumptions worth keeping: Apple's live IR page **still
publishes** its numbers, so Wayback was never load-bearing there; and the IR
blocking is a Cloudflare **bot challenge, not geo-fencing** — the laptop is
blocked identically to the box, so moving the fetcher cannot fix it.

**Transcripts are structurally the wrong document and always will be.** The
operator never reads the live dial-in aloud, because everyone hearing it has
already dialed in. Every number in our 15-transcript corpus is a replay or an
IR-contact line — e.g. AAPL's *"the number for the telephone replay is
866-583-1035."* Mining them for a bridge yields a recording.

## EDGAR query traps

Each of these cost a real number before it was fixed:

- **A descriptive `User-Agent` is mandatory.** An empty UA is a hard 403.
- **Numbers match literally.** `"8883176003"` → 0 hits; `"888-317-6003"` →
  3,234. Never query an unformatted number.
- **A bridge number is not an issuer key.** `888-317-6003` is a generic Chorus
  Call DID shared across hundreds of filers. Always scope by CIK plus a date
  window.
- **The passcode is the high-precision key.** `"3937444"` returns 3 hits in all
  of EDGAR — use it to confirm and dedup, never the number alone.

## Extraction traps

The classifier decides what each matched number *is*. Three failure modes, all
found by running it against real filings rather than by review:

- **Separator vocabulary.** Yum writes `800/715-9871`. A separator class of
  `[\s.-]` silently dropped its North America bridge while keeping the
  international one — a partial success that looks like a whole one.
- **Replay dominance.** *"To access the playback, dial +1/800-770-2030"* puts a
  bridge cue (`dial`) one character from the number and the replay cue fourteen
  characters further out. Nearest-cue alone promoted **both** of Yum's playback
  lines to bridges. A replay cue in the preceding text now governs the whole
  clause, because *"dial X for the replay"* is the standard construction.
- **Label vocabulary.** Wendy's writes `event ID 280384`, not `conference ID`.
  A bridge extracted without its code is an unenterable lane that still reports
  as a success.

Media-contact numbers share the paragraph with the bridge (Chipotle's EX-99.1
carries both), so classification is by *nearest* cue, and a number with no cue
in range stays `unknown` rather than being guessed into a class. Dialing a
company's press desk instead of its bridge is the failure that rule prevents.

## Absence is a result

Ford has 14 "conference call" filings since 2025 and **zero** containing
"dialing"; its last dial-in filing was 2024-04-24. That is a positive
`webcast-only` verdict, not an unresolved row. This mechanises the
"check the PRIOR quarter too" rule: one quarter tells you the state, two tell
you whether a missing number is a **drop** (Ford) or a **standing policy**.

## Digits never enter git

The hunt writes two artifacts and the split is the point:

- `state/hunt/<KEY>-<quarter>.json` — **gitignored**. Numbers, passcodes,
  filing URL, retrieved-at.
- `slate/hunt-status.json` — **committed**. Key, found, tier, verdict,
  `number_src`, evidence. No digits, ever.

`scripts/hunt-daily.sh` asserts the committed half is digit-free and **refuses
to commit** if anything phone-shaped appears. That is the enforcement the
digits policy in `docs/DIAL-INS.md` never had — seven registry entries already
carry leaked digits in prose fields.

## Where the hunt stops

**A number is not admission.** Paper establishes that a bridge exists; only
`scripts/bridge-probe.sh` establishes what it does to a silent listen-only
caller. MSFT published its digits and "no password required" and drops us in
42 seconds. The hunter never writes `admission` — that is THE MSFT RULE, and it
is the whole reason the registry has a probe-only field.

Two signals worth reading before probing: an *"ask to join …"* or
*"an operator will assist you"* phrasing in the filing predicts a Class B
operator screen, and a venue that no-answers off-hours is usually a per-event
reservation (Apple) rather than a dead number — re-probe it at T-90..T-30 on
event day.

## Not yet covered

- **Issuers who publish only on an IR page.** Apple is the type specimen: its
  8-Ks carry no dial-in, and its IR page is Cloudflare-challenged. Needs the
  Wayback lane or a browser step.
- **Non-issuer venues** (FOMC, hearings, pressers) have no CIK, so EDGAR does
  not apply, and `configs/dial-ins.toml` is issuer-keyed.
- **Foreign private issuers** file 6-K rather than 8-K; the form filter needs
  widening before the hunt covers them.
