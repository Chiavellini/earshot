# Dial-in access — which issuers can we listen to by phone?

Companion to `configs/dial-ins.toml` (the machine-readable registry) and
`scripts/mine-dial-in.py` (the classifier). Answers the question that decides
whether a `KXEARNINGSMENTION<T>` series is tradeable at all: **can we dial a
number and listen to this call live?**

It matters because EARSHOT is phone-FIRST: the bridge beats every webcast
listener. Since 2026-07-17 (branch `webcast-lane`) there is also a webcast
fallback — `src/source/pipe.rs` (ffmpeg/yt-dlp subprocess → PCM), launched via
`scripts/launch-webcast.sh` in capture/observe posture only. The router:
**tier `published`/`bridge_unpublished` with a number in hand → phone lane;
tier `none`, the webinar-shape `unknown`s, or a FAILED number hunt → webcast
lane (degraded-GO: corpus + observation, not racing).** No number no longer
means no audio; it means the slow lane.

## The finding

**Essentially every large issuer runs a real telephone bridge.** Of 18 issuers checked
against primary sources and verbatim operator transcripts (2026-07-16), exactly **two**
have no phone leg at all: **TSLA** and **NFLX**.

The intuition "most large companies offer a dial-in" is right. The trap is that it
answers the wrong question. **A bridge existing is not the same as us being able to
join it.** The real variable is whether the number is *published*:

| Access | Issuers |
|---|---|
| **Published, no registration** | JPM, GS, MS, C, MSFT, AAPL, OKLO |
| **Bridge confirmed, number NOT published** | GOOGL, AMZN, META, NVDA, DPZ, INTC, GEV, UAL, CAVA, RKLB |
| **No phone leg** | TSLA, NFLX |

Ten of eighteen sit in the middle tier — a real PSTN bridge that no press release
names. That is where the number hunt (`docs/RUNBOOK.md` Track A) earns its keep, and
it is the modal case, not the exception.

## The Apple rule

**`qa_mechanism` and `listen_access` are independent.** Conflating them is the one
error this whole design exists to prevent, and it was made and caught on 2026-07-16.

Apple's transcript contains zero star-1/keypad/listen-only language. The file starts at
the literal top of the call ("Good afternoon and welcome to the Apple Q2 Fiscal Year
2026 earnings conference call") — so the absence is real, not a podcast-trim artifact.
The reasoning "no DTMF ⇒ no phone access" is nevertheless **wrong**: Apple publishes a
dial-in on its IR earnings-call page. The queue is curated (the operator reads
pre-arranged analysts by name); the listen line is public.

That only means we cannot **ask** a question — which we never wanted to
(`docs/INGRESS-BAKEOFF.md:133`, "Listen-only, always"). **Apple is tradeable.**

So: a transcript can establish **only** `qa_mechanism`. `listen_access` is established
**only** by an IR page / press release / 8-K. A `curated` or `unknown` verdict is a
prompt to read the IR page — **never a no-go**.

## Two traps that will bite the next reader

**1. The UAL trap — 8-K silence is not evidence of absence.**
UAL's Q2 8-K says "A live, **listen-only webcast** … will be available at ir.united.com."
Tesla's says essentially the same thing. They mean opposite things: UAL's operator runs a
live phone Q&A ("press star, then the number one on your telephone keypad"); Tesla has no
phone leg whatsoever. Same phrasing, opposite fact. Grouping them — as
`docs/RUNBOOK.md` once did — forfeits a tradeable event.

The tell that actually separates them is not the 8-K wording. It is that **Tesla's call
has no operator at all**: IR moderates directly, analysts "raise hand" and "unmute".

**2. Motley Fool cannot be used for this signal.**
It redacts operator text to `[Operator Instructions]`, and dropped the preamble entirely
on INTC and GEV. It manufactures false negatives — the exact error mode that turns a
tradeable issuer into a NO-GO. Use **Insider Monkey** or **Investing.com**, which
preserve verbatim operator language. Any NO-GO derived from a Fool transcript is void.

## Method

Evidence order, strongest first:

1. **IR events page / earnings PR** — the only thing that establishes `listen_access`.
2. **8-K exhibit** — good for a *positive* number; worthless as a negative (see UAL).
3. **Official transcript operator preamble** — establishes `qa_mechanism`, and proves a
   bridge *exists* even when unpublished.

```
scripts/mine-dial-in.py --check --require-opening corpus/transcripts/*.txt
```

`--check` pins 6 corpus verdicts + 8 synthetic operator strings. It is a real
regression, not decoration: **it has caught two false negatives that review missed.**

- **QCOM** — "press star **then** the number one". A first-pass regex allowed only
  `star[, ]*(?:followed by)?`, so the connector missed.
- **GOOGL** — "you will need to press **\*1** on your telephone". Alphabet's own
  transcript uses the **glyph**, not the word "star". This was a silent `unknown` on
  the largest near-term event, and no corpus file uses the glyph — only the synthetic
  case could catch it.

Both bugs marked a tradeable issuer untradeable. That is the failure direction to fear:
a false positive costs one wasted dial, a false negative silently forfeits an event.

`--require-opening` refuses to report a negative from a transcript that does not contain
the call opening — a truncated file would otherwise read as "no phone". Our corpus is
faster-whisper ASR, not official text; real garbles include "All prayer" for "Operator"
and "To destroy your question" for "To withdraw your question". **Never rest a negative
on ASR alone.**

## Policy: what is and is not in the repo

`docs/layers/prepare.md` and `src/prepare.rs` keep dial-in **sourcing** out of the repo,
and that still holds. The registry threads it rather than reversing it:

- **In git:** `tier`, `qa_mechanism`, `listen_access`, the evidence *characterization*,
  `evidence_src`, `number_src`, `confidence`, `last_verified`. None of it secret; none of
  it per-call.
- **Not in git:** the digits. Numbers and passcodes rotate every quarter and stay in the
  gitignored per-event env files (`configs/*-autorun.env`). Fetch them at prep time from
  `number_src`.

This is also why the registry stores an access *pattern* and not a quote: a verbatim
quote of a dial-in paragraph contains the number, which would smuggle rotating digits
back into version control under the name "evidence".

## Board (2026-07-16)

Live `KXEARNINGSMENTION*` events, verified against the Kalshi API:

| Event | Date | Tier | Status |
|---|---|---|---|
| `…NFLX-26JUL02` | 07-16 | none | **NO-GO** — YouTube video interview only |
| `…PGR-26JUL15` | 07-15 | unknown | not researched |
| `…DPZ-26JUL20` | 07-20 | bridge_unpublished | GO only if a number lands by 07-19 |
| `…GEV-26JUL22` | 07-22 | bridge_unpublished | number hunt needed |
| `…GOOGL-26JUL22` | 07-22 | bridge_unpublished | **not previously tracked** — number hunt needed |
| `…TSLA-26JUL22` | 07-22 | none | **NO-GO** — no phone leg |
| `…AAL-26JUL23` | 07-23 | unknown | **not previously tracked** |
| `…INTC-26JUL23` | 07-23 | bridge_unpublished | number hunt needed |

Universe: **379** "Mentions" series, **135** of them `KXEARNINGSMENTION*` (live, 2026-07-16).
The registry deliberately covers only the open board plus already-verified issuers; the
remaining ~127 have no event for months.

## Prevalence beyond the board — the sp500-random cohort (2026-07-16)

The 18-issuer finding above is board-skewed. To make the prevalence claim
defensible for the S&P 500 broadly, a second cohort was sampled
**deterministically** (no cherry-picking possible): from the Wikipedia
S&P 500 constituents table (fetched 2026-07-16, 503 rows), the first two
tickers alphabetically per GICS sector not already in the registry —
22 issuers, 2 × 11 sectors. Same evidence rules; entries carry
`cohort = "sp500-random-2026-07-16"` in `configs/dial-ins.toml`.

| Rate | Board cohort (n=19 tiered*) | Random cohort (n=22) |
|---|---|---|
| Phone bridge confirmed | 17/19 (89%) | **19/22 (86%)** |
| Number published (incl. registration-link) | 7/19 (37%) | **8/22 (36%)** |
| Confirmed no phone leg | 2/19 (TSLA, NFLX) | **0/22** |
| Unresolved | 0 | 3 (all webinar-shape, below) |

\* the 4 ASR-corpus-only issuers (QCOM/GIS/WMT/CAT, tier `unknown`) excluded.

The two cohorts agree almost exactly, so the answer generalizes:
**~85–90% of S&P 500 issuers run a real telephone bridge, but only ~35–40%
publish a way in.** Both halves of the original intuition survive: "most
large companies offer a dial-in" is true of bridge *existence* and false of
bridge *access*. The modal S&P 500 earnings call is reachable by phone only
via a number hunt. Sector spread of the published tier is wide (Materials,
Real Estate, Utilities-adjacent, IT, Consumer Discretionary, Staples), so
publication is issuer culture, not a sector effect.

Two findings that change how the registry must be used:

**1. Tiers decay: the Ameren migration.** AEE ran a classic star-1 phone
bridge through Q3 2025, then moved to a webinar platform — Q4'25 and Q1'26
transcripts contain zero telephone language ("use the raise hand icon…at the
bottom of your webinar application"). A tier verified two quarters ago can be
stale. **Re-verify `last_verified` against the current quarter's PR before
declaring any event GO.**

**2. The webinar-with-operator shape (AEE, ALLE, CHTR) is a third thing** —
neither the TSLA "none" (those calls have NO operator) nor a classic bridge.
An operator is present but every queue instruction is on-screen
(Zoom-webinar style), and such platforms *can* carry unpublished dial-in
attendees. All three are recorded `tier = "unknown"` with likely-none notes,
NOT `none`: the TSLA tell (no operator at all) is absent. If one of these
ever gets a mention series, it needs a per-event probe, not a registry read.

Also worth recording: **ABNB was assumed untradeable-by-phone from memory
and is not** — its PR publishes a registration link that issues dial-in
details, and its operator runs a star-1 queue. The registry exists precisely
because remembered access facts are wrong at a meaningful rate.

## Open questions

- **`INGRESS-BAKEOFF.md:133` says "Listen-only, always … public dial-ins only."** Ten of
  eighteen issuers are bridge-unpublished, and Track A hunts a *participant* bridge while
  `launch-event.sh` passes `--dtmf` passcodes. As written, that rule forfeits most of the
  board. It needs an explicit decision, not silent drift.
- **The Tier A/B boundary for the bridge-unpublished set is unresolved.** No registration
  URL was found for any of them. Intel's webcast runs on `edge.media-server.com`
  (Notified), a platform that generically issues dial-in + PIN on registration — but no
  Intel-specific registration link exists, so that is platform-pattern inference, not
  evidence. Do not record a registration tier without an issuer-specific link.
- **The bank numbers have never been dialed.** All five are paper-verified only. MS is the
  sole wire-verified bridge (CallSid `CA5831c4…`, 3585 frames, 0 drops).
