# Post-mortem — PLTR + SNAP, 2026-08-03 21:00Z

Two webcast-only events, armed live at the 1¢ mention-probe posture
(`--i-am-live --allow-mention`, non-crossing, $1 ledger). Both captured to
completion, 0 drops on every queue. **9 orders placed, 0 fills by design.**

Neither issuer had a reachable phone bridge (`dial-ins.toml`: PLTR `unknown`,
SNAP `bridge_unpublished`), so both ran webcast-only. Two venue classes we had
never captured before: **YouTube Live** (PLTR) and **registration-gated Q4/IVS**
(SNAP).

| | PLTR | SNAP |
|---|---|---|
| armed | 21:02:29Z (T+2:29) | 21:03:58Z (T+3:58) |
| legs | ffmpeg_direct · ytdlp_primary · ytdlp_backstop | spec_edge · streamlink · pipe_tuned |
| frames | 359,481 (119,697 / 119,892 / 119,892) | 297,436 (99,096 / 99,068 / 99,272) |
| detections | 81 raw → 4 markets (20.2× dedup) | 35 raw → 5 markets (7.0× dedup) |
| drops | 0 | 0 |
| orders | SLOP SOVE MAVE REVE | GLAS MEMO STOR RETE GAME |

## Δrace — we lost all three genuine races, narrowly

"Genuine race" = market priced <80¢ pre-call (outcome actually uncertain).
Reference is the market's first print ≥95¢ after call start, i.e. the moment the
market became certain the word was said. Negative = market first.

| market | pre-call | our decision | market → 95¢ | Δrace |
|---|---|---|---|---|
| SNAP RETE | 68¢ | 21:21:47.833 | 21:21:47.181 | **−0.65 s** |
| PLTR SLOP | 73¢ | 21:03:13.804 | 21:03:10.784 | **−3.0 s** |
| PLTR SOVE | 50¢ | 21:06:45.548 | 21:06:40.763 | **−4.8 s** |

RETE was a photo finish. The other six ordered markets were pre-priced ≥80¢ —
no race existed to win.

**A first cut of this table used "first trade ≥50¢" as the reference and produced
−184 s to −621 s. That metric is invalid**: most of these markets opened above
50¢, so it timed ordinary trading rather than repricing. Recorded here because
the wrong number is more dangerous than no number.

## Two markets were never winnable by an audio lane

SNAP `MEMO` (55¢) repriced to 95¢ at **21:00:18** and `STOR` (78¢) at
**21:02:00** — both *before Q4's `broadcastUrl` existed at all* (empty until
21:03:22Z). Those moved on the **press release**, which Snap publishes ahead of
the call. No acquisition-latency work wins them; this is the text-lane reality
already recorded in the Ford 2026-07-28 post-mortem.

## Recall, decomposed by whether we were listening

Market verdict = repriced to ≥95¢ during the call.

**SETTLED 2026-08-04 00:02Z — all 34 markets finalized. Numbers below are final,
graded against settlement, not against the 95¢ proxy.**

| | market says SAID | we fired | in-coverage recall | precision |
|---|---|---|---|---|
| PLTR | 6 (INTE MAVE NVID REVE SLOP SOVE) | 4 | **4/5 = 80%** | **4/4 = 100%** |
| SNAP | 5 (GLAS MEMO REST RETE STOR) | 5 | **4/4 = 100%** | **4/5 = 80%** |

Combined precision **8/9 = 89%**. The single false positive is SNAP `GAME`
("Game"), which settled **NO**. Preflight had flagged exactly this class the same
night — `[WARN] SHORT token targets — highest false-alarm risk` — and `game` is a
short, high-frequency English word that appears inside ordinary speech
("gaming", "game plan", and as a substring of adjacent words). The warning fired
and nothing consumed it. That is the same shape as the NVID miss below: the
preflight already knows, and the knowledge dies in a WARN line.

The three misses are **not one failure class**, and lumping them into "67% / 80%
raw recall" hides the only actionable one:

- **PLTR INTE** — market→95¢ at 21:02:58, we armed 21:02:29. 13 variant-study
  confirmations, the *strongest* word on the board. Blind-window / edge miss,
  not a detector fault. Pre-priced 96¢ — no edge available regardless.
- **SNAP REST** — market→95¢ at 21:00:56, we armed 21:03:58. **Squarely inside
  the blind window.** Not a detector fault.
- **PLTR NVID — the one real detection miss.** Market→95¢ at 21:25:05, ~23 min
  into full coverage. Pre-priced **37¢**: genuine edge, and we never fired.

### NVID is a predicted failure the gate let through

`nvidia` carried **`confirmed = 2`** in the variant study — joint-lowest on the
PLTR board (with `commercial growth`, which also never fired). Every word with
≥3 confirmations that the market said, we caught.

`scripts/preflight-event.sh:119-121` FAILs only on `confirmed == 0`. A word with
`confirmed = 2` passes the gate silently and is materially likelier to be missed.
**Recommendation: WARN at `confirmed <= 2` and surface it in the preflight
summary** — not a FAIL, since dropping a keyword forfeits its market and that is
a trading decision, but it must not be invisible. One event is not proof of the
threshold; re-check against AMD 26AUG04 before hard-coding it.

## Coverage cost: 149 s and 238 s of blind window

Both arms started late, for different reasons, and neither was avoidable by
scheduling — **the stream URL did not exist earlier**:

- PLTR: YouTube flipped live 21:01:05Z. We armed 21:02:29Z. **~84 s of that gap
  was a bug in `scripts/pcm-gate.sh`** (below), not the venue.
- SNAP: Q4's `broadcastUrl` was empty until 21:03:22Z; armed 36 s later, fully
  automatic via `scripts/arm-on-url.sh`. That 36 s is gate + arm bring-up.

The launchd `FIRE_HHMM=2035` model assumes a URL is obtainable 25 min pre-call.
For both of these venue classes **it is not** — the URL materialises at or after
T-0. Arm-on-URL-availability is the correct trigger, not wall-clock.

## Bugs found and fixed (live)

1. **`ORT_DYLIB_PATH` resolved to a `.dSYM` debug bundle** —
   `…/libonnxruntime.1.27.1.dylib.dSYM/Contents/Resources/DWARF/libonnxruntime.1.27.1.dylib`
   has the same basename as the real library and `dlopen` HANGS on it, identically
   to a wrong-platform lib. Preflight's `detector-smoke` hung >90 s. Three
   resolvers had it, including `run-arm.sh:194` — **the actual arm path**.
   `wnt-daily.sh:138` was the only one that already excluded dSYM. Fixed in
   `run-arm.sh`, `preflight-event.sh`, `launch-webcast.sh`, `launch-event.sh`,
   plus an explicit dSYM refusal in each.
2. **`launch-webcast.sh` / `launch-event.sh` could never resolve ORT on macOS** —
   they searched `libonnxruntime*.so*` only and rooted at `$HOME/ear` (the box
   layout; the laptop is `$HOME/Code/ear`). On a Mac the find always came up
   empty and the REFUSE fired on a healthy tree. Now platform-aware and
   repo-rooted.
3. **`pcm-gate.sh` used yt-dlp watch-page semantics on a master manifest** — my
   own bug, shipped hours earlier. `youtube-resolve.sh` writes an HLS master, and
   `yt-dlp -f bestaudio` has no such format for a raw m3u8, so the gate FAILED on
   a perfectly good stream and `arm-on-url.sh` fail-closed exactly as designed.
   **Cost: ~84 s of PLTR coverage.** The gate was right to refuse; the gate was
   wrong about what it was testing. For a resolved master, use `--kind hls`.

## Leg ranking: my prediction was wrong

I added `ffmpeg_direct` to PLTR mid-flight because a 5 s bench showed it at
190,928/192,000 bytes (99%) vs the yt-dlp leg's 89,132/160,000 (56%). On the live
tape **`ytdlp_primary` won the SLOP decision at 21:03:13.804 and `ffmpeg_direct`
did not reach the decision until 21:03:16.842 — 3.04 s slower.** The bench was
startup-dominated and measured the wrong quantity. The extra leg cost nothing
(fanout ORs at the decision; duplicates are suppressed by the keyword state
machine) but the reasoning was unsound.

On SNAP the three legs landed within **204 frames of each other (0.4%)** and split
detections 12/12/11 — `spec_edge` did **not** out-detect `streamlink` on this
venue. Whether it won on *timing* is not answerable from counts and needs a
per-leg arrival join.

## Venue facts (candidates for VERIFICATIONS.md)

- **Q4 has a programmatic resolver.** `GET
  https://attendees.events.q4inc.com/rest/v1/event/<id>/broadcast-status` with an
  attendee bearer JWT (localStorage `prod.authToken-attendee`) returns
  `{"data":{"broadcastUrl":"…"}}` — empty pre-broadcast, populated on air. Every
  prior Q4 event (PYPL, BA, AXP, AMZN, RDDT) was a manual browser-read. Guest
  registration is still a human step; polling after that is automatable.
- **YouTube's datacenter block has escalated.** us-east-1 now fails at the
  *metadata* layer — `yt-dlp -J` returns "Sign in to confirm you're not a bot",
  where HOOD 2026-07-29 got a working manifest and failed at the segment layer.
  Cookie-free capture from the box is dead, not merely degraded.
- **Both venues' manifests are IP-bound.** YouTube embeds `/ip/<addr>/` in the
  master; IVS returns `USER-IP` session-data. A URL resolved on one host is not
  portable to another regardless of bot-walls — each vantage must self-resolve.
- **Kalshi REST now emits fixed-point dollar strings.** `yes_bid_dollars`,
  `yes_ask_dollars`, `last_price_dollars`, `volume_fp`, `open_interest_fp`; the
  integer-cent `yes_bid`/`volume` keys are **absent**. `src/kalshi/book.rs`
  already speaks the new WS shape (`price_dollars`, `yes_dollars_fp`), so the arm
  path was unaffected — but any script reading the old keys silently gets `None`.

## Polymarket: the extrapolability question, answered

**Kalshi traded 65× more often than Polymarket on the identical event.**

| | Kalshi | Polymarket |
|---|---|---|
| trades (event) | 7,918 | 121 |
| trades during call | 1,399 | 27 (YES side) |
| size | 224,142 contracts | $2,903 notional |
| markets | 19 | 16 |

Of the five word-level overlaps (SLOP, ANTH, HEAL, WARP, INTE), **only SLOP
produced a Δt at all** — Warp Speed, Healthcare and Anthropic had *zero* PM YES
trades after the call started. That single pair swung from **−6.2 s to −187.5 s**
on a 5¢ threshold change, which is proof it was not tracking the utterance.

**Verdict: NOT RESOLVABLE.** Not "Kalshi leads" — the instruments cannot answer
it. Using a PM pump as a Kalshi signal is a non-starter at 27 prints/hour.

The Q-PM claim that PM runs this class at 10–100× Kalshi volume holds for
Powell/Trump/NFLX boards; it is **false for earnings-mention markets**, where
Kalshi is the deeper and far tighter venue (1–2¢ spreads vs up to 17¢).

### What would make it answerable

A **Kalshi live book recorder**. `src/kalshi/book.rs` keeps an `ArcSwap` mirror
and never persists (`BookCache` :70, `run_ws` :236); `Command::Book` (`main.rs`
:1490) prints to stdout and stores nothing. PM book rows already carry
millisecond server timestamps — 2,669 YES rows captured tonight. Kalshi book (ms)
× PM book (ms) is a real measurement; trades × trades is not, because both venues
are second-granularity and PM barely trades.

## Say-Q&A prior — sample #2

Pre-registered `state/KXEARNINGSMENTIONPLTR-26AUG03/say-prereg-2026-08-03.md`,
frozen 19:52Z, 68 min pre-call, n=2 signal set declared underpowered in advance.

**GRADED ON SETTLEMENT — the pre-registered gate FAILS ON BOTH LIMBS.**

| | result |
|---|---|
| signal set {GOVE, COMM} | **0/2 = 0% YES** |
| (a) its own price-implied rate | ~24.8% → **0% does not beat it** |
| (b) control: no-signal, <80¢ (n=12) | **2/12 = 16.7%** (SLOP, NVID) → **0% does not beat it** |

Both signal markets were predicted **SAID (medium)** and both settled **NO**.

The pre-registration named its own failure mode in advance, and that is exactly
what happened: *"the risk is purely that they say 'government business/revenue'
and never the exact two-word phrase."* Palantir discussed government and
commercial business at length; the exact phrases "Government Contract" and
"Commercial Growth" were never uttered under the exact-phrase resolution rule.

**The Say prior is now 0 for 2, and both failures are the same failure.** Sample
#1 (TSLA 2026-07-22) was recorded as "acceptance model held, word-mapping broke."
Sample #2 breaks in the identical place. Two events is still not power, but the
*mechanism* is now repeated rather than merely observed: Say question traction
predicts **topic**, and Kalshi resolves on **exact phrase**, so the prior cannot
transfer without a phrase-level model. Nothing here promotes the prior into
`src/prepare.rs`.

Two details worth keeping, both of which the pre-registration got right by
excluding them: `SOVE` ("Sovereign AI") was explicitly recorded as weak/indirect
and **NOT counted in the signal set** — and it settled YES. Had it been counted,
the gate would have shown a spurious 1/3. The discipline of freezing the signal
set before the outcome is what kept that out.

The structural finding stands and remains more useful than the predictions: Say's
highest-traction topics (Dividends ×4, Buyback ×4, Competitiveness ×7, Scaling ×6,
AIP ×6) have **no corresponding Kalshi market**, while most of Kalshi's unpriced
pool draws zero Say questions.

## GRAB (2026-08-04 00:00Z) — first Zoom-class capture, taken acoustically

GRAB was staged by another agent as a **deliberate hard-block**: both
`[[sources]]` in `configs/webcast-grab-26aug03-arm-wc.toml` are
`echo 'GRAB ACCESS BLOCKED…' >&2; exit 64`, so a stale URL could never be
mistaken for the live call. That was the correct posture at the time and it is
why nothing armed by accident. Left alone, it would also have captured nothing.

### What the venue actually is

Established by joining it, not by inference:

- **A Zoom WEBINAR** ("2026 Q2 Earnings Livestream"), not a meeting. The web
  client admits an attendee **on name alone** — the passcode rides in the `wpk`
  link token — and shows a waiting room until the host opens.
- **No HLS anywhere.** The web client carries media over WebRTC (zero
  `<audio>`/`<video>` elements, no `m3u8` in the tab's network log), and Grab runs
  **no parallel YouTube livestream** (`@Grab` → "channel is not currently live").
  The only YouTube hit for the event is a retail commentator's reaction stream.
- **Three Singapore PSTN legs exist and are public**, decoded from
  `window.launchBase64` on `grab.zoom.us/j/<id>` — all one-touch `,<meeting-id>#`. The digits stay out of git by policy —
  re-derive them from `launchBase64` day-of.
- Grab **records the webinar** (REC indicator) and offers **live captions**.

### The phone lane got one step further than before, and stopped on a passcode

With Singapore outbound enabled by the account owner at ~23:35Z, the probe
**connected and held 35.6 s** (1790 frames) — the first time this venue has been
reached at all. It never got to conference audio:

> "Welcome to Zoom. Enter your meeting ID followed by hash. Please enter the
> meeting passcode followed by hash. You have not entered any numbers. Please
> re-enter the meeting passcode followed by hash."

The **meeting id was accepted**; entry is gated on a numeric telephone passcode
that is **absent from Zoom's own one-touch dial strings** (they end at
`,<meeting-id>#`), absent from `launchBase64`, absent from `MeetingConfig`
(`h323DeviceList` is the empty string), and not derivable from the web `pwd`
token. Recorded as a new `admission` value — **`passcode_required`** — which is
neither `held` nor `dropped`: the bridge never judged us. Preflight correctly
FAILs on it, and no live arm was attempted.

### So the capture was taken acoustically, and it worked

`configs/acoustic-grab-26aug03-arm.toml`: Zoom web client → laptop speakers →
laptop microphone → `ffmpeg -f avfoundation` → the ordinary detect chain. Keyword
set is **byte-identical** to the validated one (`keywords-extract.py` hashes both
configs to `dccf5151…`), so `configs/.readiness/phone-grab-26aug03-arm.json` was
transferred rather than re-run, with the transfer and its limits recorded in the
artifact itself.

Armed 23:58:31Z, ~90 s before the host opened the webinar. Measured, not assumed:

| t | RMS | reading |
|---|---|---|
| 0–160 s | 24–45 | room noise; nobody speaking |
| 170 s (≈00:01:40Z) | **243** | first speech |
| after raising output volume 55 → 80 | **787–922**, peak 8855 | strong signal, ample headroom (32767 full scale) |

Detections began ~2 min later and are **Grab-specific**, which is the strongest
available evidence the pipeline is hearing the real call: `SUPE` (superbank)
00:03:57, `COMM` (commission) 00:04:18, `FOOD` (foodpanda) 00:04:21.

**Posture: DRY RUN, deliberately.** The 1¢ mention-probe posture is inside
standing policy, and at 1¢ non-crossing an order cannot fill — so this was not a
money decision. It was that the acoustic path has **never been validated against
the detector**, and CLAUDE.md's rule is that an unattended path counts as
validated only when rehearsed with the exact production mechanism. Dry run yields
the decision timestamps that Δrace actually needs; the order leg would have added
~10 ms and no information, at the cost of restarting a working first-ever capture
mid-call. **This is a judgment call and it is the operator's to reverse.**

### Speed: on a Zoom-only venue we drew level — the first time that has happened

Call ran 00:01:40Z → ~00:55Z (RMS collapsed 1070 → ~20). **3,354 s captured, 8
markets fired, arm never died.**

Reference is the market's first print ≥95¢ after call start. **Kalshi trade
timestamps are second-granularity, so |Δ| < 2 s is a TIE, not a win** — the same
quantisation floor documented in `scripts/pm-edge-report.py`.

Two rows must be thrown out before reading anything: `SUPE` and `STAS` were both
**pre-priced at 97¢**, already above the 95¢ reference, so "first print ≥95¢"
times noise rather than repricing. (`STAS` "reaching" 95¢ at 00:01:13 — before
speech even began in our tape — is the tell.) That is the same trap that produced
the invalid −184 s table for PLTR; caught here before it was published.

**Genuine races — pre-call below 95¢, so a race actually existed:**

| market | pre-call | ours | market ≥95¢ | Δ | read |
|---|---|---|---|---|---|
| COMM | 77¢ | 00:04:18.132 | 00:19:16.840 | **+898.7 s** | we led ~15 min |
| BUYB | 84¢ | 00:26:48.880 | 00:26:49.343 | **+0.5 s** | tie |
| DELI | 19¢ | 00:14:39.443 | 00:14:40.738 | **+1.3 s** | tie |
| FOOD | 86¢ | 00:04:21.216 | 00:04:20.243 | **−1.0 s** | tie |
| HEAD | 81¢ | 00:10:52.977 | 00:09:55.202 | **−57.8 s** | we lagged ~1 min |

**3 ties, 1 led, 1 lagged — against PLTR/SNAP where we lost all three genuine
races** (−0.65 s, −3.0 s, −4.8 s). Same detector, same laptop, same night; the
venue changed and so did the outcome.

The mechanism is the point. GRAB is **Zoom-only**: no HLS, no YouTube simulcast,
and the PSTN legs are passcode-gated to registrants. Whatever buffering Zoom
imposes is therefore **common-mode** — it lands on every participant including
whoever is trading, so it cannot be arbitraged against us. PLTR (YouTube Live) and
SNAP (Q4/IVS) both have faster paths we do not control. This is the first direct
measurement supporting the same-floor thesis: **a venue with exactly one way in is
a level field, and a degraded capture path on a level field beats a good capture
path on an unlevel one.**

Do not over-read the two outliers. `COMM` at +899 s is real but rests on a thin
board — the market simply did not reprice until a later, more obvious mention;
that is Kalshi being slow, not us being fast, and it will not repeat on a liquid
board. `HEAD` at −58 s means we **missed the first "headwind" and caught a later
one** — a recall failure showing up in the timing column, not a latency failure.

**Provisional accuracy** (settlement due ~01:25Z; prices already decisive at
0.99/0.01): fired 8, of which 7 sit at 0.99 → **precision 7/8 = 87.5%**. One false
positive, `ROBO` ("robot", 24¢ pre-call, now 0.01). Markets heading to YES number
8; we caught 7 → **recall 7/8 = 87.5%**, the miss being `GRAB` ("grabmart").
`TAIL` ("tailwind") sits at 0.01 and we correctly never fired it, while `HEAD`
("headwind") went YES — the pair is a useful discriminator that the detector got
right.

### Polymarket release-clock for GRAB: NOT RESOLVABLE, for a structural reason

The plan was to use GRAB as the release-clock case since we could not hear it.
That is dead, and not because of a capture bug:

`grab-quarterly-earnings-gaap-eps-08-03-2026-0pt02` carries
**`endDate: 2026-08-03T21:00:00Z`** — the PM market ends **three hours before the
Kalshi call starts**. Our capture's last GRAB row is 21:03:45Z; the market now
reads `outcomePrices ["0.9995","0.0005"]`, i.e. it repriced to certainty in a
window where PM had already stopped trading and Kalshi's mention board had not yet
begun. **The two venues never price the same instant**: PM resolves on the press
release, Kalshi on words spoken in a call held hours later. There is no shared
clock to measure, so no lead/lag exists to find.

This strengthens, rather than weakens, the PLTR conclusion. Both attempts at a
cross-venue Δt now fail for *instrument* reasons — word-overlap markets are too
thinly traded on PM, and EPS-vs-mention markets do not share a repricing instant.

## Follow-ups

1. `scripts/preflight-event.sh` — WARN on `confirmed <= 2` (NVID above).
2. Build the Kalshi live book recorder; it is the blocker for every cross-venue
   latency question.
3. Promote the Q4 `broadcast-status` poll into a real resolver alongside
   `choruscall-resolve.py`, and pin Q-Q4 in `VERIFICATIONS.md`.
4. Re-pin Q-YOUTUBE: the block moved from segment layer to metadata layer.
5. Arm-on-URL-availability, not `FIRE_HHMM`, for venues whose URL appears at T-0.
6. Score per-leg *arrival* (not detection counts) to settle spec_edge vs
   streamlink on Q4/IVS.
