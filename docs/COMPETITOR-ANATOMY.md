# Competitor anatomy — per-tape ledger (first tape: MS 2026-07-15)

Question answered here: **who is beating us, by how much, with what stack, and what would beat
them.** This doc is a ledger: each traded event's tape gets re-run through the same method
(§Open verification items, item 3) and appended under §Per-tape results. Every number below is from `state/KXEARNINGSMENTIONMS-26JUL15/report.json` (our 7
detections, wall-anchored via autorun.log) × the public Kalshi trades tape (fetched read-only
2026-07-17, raw JSON archived in the session scratchpad `ms_tape.json`). Method: our detection
wall-time − per-detection `capture→decision` − sherpa's pinned post-word-end decode lag
(400–500 ms, BAKEOFF.md) = the word's end **in our audio stream**; compare tape fills against it.

## The three true-positive races (markets settled ~99¢ = real mentions)

Competitor wave-1 fill vs word-end-in-our-stream (Δ):

| Word | Our decision (Z) | Word-end in our stream | Wave-1 fill | **Δ** | Wave-1 shape |
|---|---|---|---|---|---|
| INVE | 12:42:28.795 | 12:42:28.210–.310 | 12:42:28.287 | **−23 ms** | 10 fills same ms, 68→95¢ ladder |
| TAIL | 12:43:27.034 | 12:43:26.446–.546 | 12:43:26.504 | **−42 ms** | 2 fills, then wave-2 at +124 ms sweeping 84→98¢ |
| RECE | 13:27:10.763 | 13:27:10.172–.272 | 13:27:10.273 | **+2 ms** | 11 fills same ms, 27→95¢ ladder |

**Headline: the fast player's entire hear→decide→fill loop completes at the instant the word
finishes arriving in OUR stream — consistently, ±25 ms across three words 45 minutes apart.**
That consistency is machine timing, not a human. (Our own decision follows ~0.5 s later —
sherpa's decode lag — and our ack another ~0.1 s after that, in the old CDMX numbers.)

## Decomposition — what their Δ≈0 implies

`their_loop = our_then_transport + Δ ≈ our_then_transport − 20 ms`.

Our then-transport (bridge → Twilio conference fork → media-stream WS → ngrok → CDMX, 4% loss
path) is unmeasured but bounded ~300–800 ms. Therefore **their word-end→fill loop ≈ 300–800 ms,
most plausibly 400–600 ms**. That admits a perfectly ordinary architecture:

- **Phone-bridge participant leg** (webcast is tens of seconds behind — excluded), likely direct
  RTP/SIP or equivalent low-latency capture: ~50–150 ms;
- **Streaming full-word ASR** (Deepgram/RT-class or self-hosted small streaming model) emitting
  ~200–400 ms after word end — **NOT prefix-firing**: they land at/after word-end + their
  transport, never before;
- **Pre-staged IOC ladder sweep, colocated**: the 10–11 fills share one millisecond timestamp
  and walk the whole book in one order — exactly our own executor design; order flight ~5–20 ms.

Wave structure: **wave-2 at +120–280 ms** (INVE +261/+265 incl. an 812-lot at 96¢; TAIL +124;
RECE +122/+235) — a second tranche or a second, slightly slower fast player mopping 95–98¢.
**Wave-3 at ~+1.0–1.6 s** — the slow crowd at 98–99¢. The race is over ~300 ms after word-end
at the bridge.

## The two non-races (OIL, MIDD) — a finding about US, not them

At our OIL detection (12:51:50, best_ask 21¢) and MIDD detection (13:21:13, best_ask 37¢):
**no sweep ever came — from anyone.** OIL's next trade was 13 minutes later, mixed 26–67¢;
MIDD never traded again until 13:30, and **both markets printed at 1–5¢ YES near the close**.
Markets that end at pennies did not contain the mention. The parsimonious read: **those two
detections were sherpa false alarms** (or non-qualifying word forms under the market rules) —
the fast player didn't "skip" them; they were right and we were wrong. Open item: confirm
against the call transcript/capture. Consequence for strategy: the "win the words the fast
player skips" thesis is NOT supported by this event — on true mentions they showed up all
three times.

## Replicability matrix

| Component (inferred) | Their status | Our status | Verdict |
|---|---|---|---|
| Low-latency phone leg (RTP-class) | have | Twilio WS fork (+unmeasured fork latency); **SIP/RTP adapter approved & in build** | **Replicable — in progress** |
| Streaming ASR, ~200–400 ms after word-end | have | sherpa ~900 ms post-word-end (measured); streaming_ctc engine built, **model blocked on Tier-1 GPU — deferred by operator 2026-07-17** | **Replicable — deferred**; the single biggest term |
| Pre-staged IOC ladder sweep | have | have (proven µs-scale, warm-fired) | **Parity today** |
| Colo near Kalshi | have (likely us-east-2 + FIX) | ⚠ **NOT parity — REVISED 2026-07-21**: box is us-east-**1**, engine is us-east-**2** (~12–17 ms away); the "2.2 ms" was a CloudFront edge, not the engine (VERIFICATIONS Q1 measured pin) | **deficit ~12–17 ms** — fix = move box to us-east-2 + FIX (RACE-STRATEGY §exec) |
| Word-selection precision (no FA fires) | evidently good | 2 likely FAs of 7 on MS | Ours needs the validation loop / better detector confidence |

## What BEATS them (not just matches)

They fire on completed words. A safe **prefix fire** (PREFIX-SAFETY method; streaming_ctc reads
raw posteriors) triggers on "recess-" ~250–400 ms **before word-end** — landing our IOC while
their ASR is still waiting for the word to finish. Stack math (word-end-relative fill time):

| Stack | Est. fill vs word-end at bridge | vs their wave-1 (~+300–500 ms) |
|---|---|---|
| Today (Twilio+sherpa, us-east) | ~+1.0–1.3 s | lose by ~600–900 ms — matches the operator's 600 ms bar |
| + SIP/RTP (in build, ~100–250 ms recovery, to be measured) | ~+0.8–1.1 s | still lose — transport alone cannot close it |
| + streaming_ctc full-word (GPU, deferred) | ~+0.25–0.45 s | **tie / trade wave-1 with them** |
| + prefix firing (same engine, same GPU work) | **~−0.1–+0.1 s** | **beat wave-1 outright** |

**Bottom line: their strategy is ordinary and fully replicable; the one lever that overtakes them
(prefix-firing CTC) is the same GPU-gated detector work, and nothing else sums to the 600 ms bar.
SIP is worth building (this build) and necessary, but it is ~a third of the gap at best.**

> **Amendment 2026-07-20 — paced-bench update.** The "+ streaming_ctc full-word → tie" row is
> NOT achieved by the current 320 ms-quantum Tier-0 export. Paced dev bench (finished 07-18
> 00:24, read 07-20): tariff Fast p95 **1294 ms (ctc) vs 1017 ms (sherpa)** against the 350 ms
> bar; guidance Confirmed p95 2107 vs 1785; ctc tariff Confirmed regressed (recall 0.44, 3 FA).
> Status: **measured, pinned** — BAKEOFF.md §"streaming_ctc paced latency" (X1 done 2026-07-20).
> The tie/beat rows are contingent on the detector follow-on ladder (chunk-16 re-export,
> keyword boost, prefix-emit bench — RACE-CHECKLIST P1–P3); the GPU finetune (P4) is the
> fallback. Strategy synthesis: `RACE-STRATEGY.md` §2–3.
>
> **Amendment 2026-07-21 — the "prefix firing beats wave-1" row is RETIRED (P3 done).** The
> earnings-mention vocabulary has no safe early prefix: the compiled cuts drop only the final
> BPE token (tariff `▁T AR I` vs `▁T AR I FF`; recession drops `ION`; guidance/partnership
> have none), so the measured prefix head start is ~32 ms, not 250–400 ms (BAKEOFF
> §"streaming_ctc prefix-fire diagnosis"). Overtaking a full-word competitor now requires a
> LOWER-LATENCY full-word detector (P1 chunk-16 + the G2 faster-model search), not prefix-firing.

## Webcast lane — competitor hypotheses (pre-registered 2026-07-20, no tape yet)

On a webcast-ONLY event the bridge edge that produced Δ≈0 above cannot exist — the encode+CDN
wall is identical for every listener (RUNBOOK §Same-floor). Whatever wave-1 appears must derive
its lead elsewhere. Three hypotheses, pre-registered before the first webcast tape is read:

- **H1 — co-listeners.** They consume the fastest public feed with live-edge fetching
  (newest-segment join, in-progress segment streaming — the class of client we now match with
  tuned ffmpeg/streamlink) from a near-CDN vantage. Prediction: wave-1 fills cluster near
  **word-end in our best webcast leg**, spread by feed/fetch differences (sub-second to a few
  seconds). Consequence: winnable — fetch ladder + us-east + fast detector; prefix-firing
  overtakes them exactly as in the phone-lane analysis above.
- **H2 — hidden faster channel.** An unpublished dial-in (operator/press line), an in-room
  listener, a venue caption/text feed, or a faster simulcast we didn't map. Prediction: wave-1
  fills land **many seconds before** our webcast word-end, far outside any fetch-strategy
  spread. Consequence: a recon problem, not a latency problem — the phone-first bridge-hunt
  protocol + channel mapping (RACE-CHECKLIST W4), and the event class is re-rated (not
  same-floor).
- **H3 — same feed, better vantage/CDN position.** Prediction: same shape as H1 but a
  consistent sub-second-to-few-second lead that per-venue fetch experiments (W2/W6) can
  reproduce from a better vantage. Consequence: us-east placement + LL-HLS where served (W3).

**Discriminator (decision rule, pre-declared — these thresholds are rules, not measurements):**
for each true-mention word, compute Δw = wave-1 first fill − word-end in OUR best webcast leg
(same Q-MSPM alignment method as the MS analysis above).
- Δw within **±5 s** of our word-end, tracking our leg's lag class → **H1/H3** (same floor;
  race the fetch + detector).
- Δw earlier than our word-end by **more than ~5 s** (i.e. beyond anything a fetch strategy on
  the same feed explains) → **H2** (hidden channel; recon).
- No wave-1 at all + market settles YES → nobody fast is on this event class yet → the webcast
  floor is currently uncontested (fillable posture case strengthens, W7).

First subjects: DPZ 07-20 retroactive virtual race (X2) = sample #1; the Wed 07-22 tapes
(GOOGL attended, TSLA per same-floor W0) = the first designed samples.

### Per-tape results

*(append one subsection per analyzed tape: event, feed/leg used, per-word Δw table, verdict
H1/H2/H3/uncontested, posture consequence)*

#### AXP + VZ 2026-07-24 — FIRST LIVE ARM ON AMAZON IVS (AXP arm+capture; VZ capture-only)

Two simultaneous 8:30 ET calls, both webcast (no bridge). **New finding — venue
class matters for edge lag:** AXP is on **Q4 Inc → Amazon IVS**
(`*.playlist.live-video.net`, us-east-2, tokenized session URL, **2 s segments,
standard HLS, PDT edge lag ≈ 4 s**); VZ is Notified/CloudFront (known class,
4 s segments, **edge lag ≈ 10 s**). So an IVS-listening system sits ~6 s closer
to the room than a Notified consumer — a real venue-selection lever. AXP armed
in observe/dry-run posture (first arm ever on IVS), 488,306 frames, **0 drops**,
2-leg fanout perfectly balanced (19/19 detections), detect p50 ≈ 120 ms.

**AXP arm — 7 flagged words, settlement-graded:**
| word | our flag (UTC) | first ≥50¢ call-window sweep | settle | verdict |
|---|---|---|---|---|
| MILL (millennial) | 12:35:18 | 12:35:16 | YES | true; **Δrace −2 s** |
| NFL | 12:37:29 | 12:37:27 | YES | true; **Δrace −2 s** |
| LOUN (lounge) | 12:37:08 | 12:29:27 (pre-remarks basket) | YES | true (recall); no audio race |
| NII | (order journaled) | 12:28:07 (pre-remarks) | YES | true (recall) |
| AMAZ (amazon) | (order journaled) | 13:20:17 | YES | true (recall) |
| CURR (currency) | 12:31:01 | none | **NO** | **FALSE FIRE** |
| REGU (regulation) | 12:36:47 | 12:27:51 (pre-remarks) | **NO** | **FALSE FIRE** |

- **Recall 5/6** on settled-YES words (caught AMAZ/LOUN/MILL/NFL/NII; **missed
  OPEN** = "openai"/"chatgpt" — OOV-compound class, our known weak spot).
- **Precision 5/7** (false fires CURR, REGU — the same single/short-word
  hallucination class as 07-23's PERU/IRAN/APPL/TRUM/ULTR).
- ~~**Δrace on the two clean post-remarks audio races = −2 s each** (MILL, NFL)~~
  — **CORRECTED 2026-07-24 night, see below.** Envelope held exactly: 7 orders, 0
  LOST_RACE, all observe-non-crossing (dry-run) — $0 spend, by design.

> **⚠ CORRECTION 2026-07-24 (night) — re-scored at µs resolution
> (`LATENCY-PROOF.md` §THE GAP IS LOCATED).** The "−2 s each" was read off
> whole-second timestamps and scored against non-price-moving prints.
>
> - **MILL is NOT A RACE.** It traded 94–95¢ from **11:25**, over an hour before
>   the utterance; the 12:35:18.030 print is 127 contracts at a **flat 95¢**.
>   The doc's "wave-1" at 12:35:16 was a **single 1-contract** print. Pre-priced
>   words carry no rally to lose and are excluded from Δrace.
> - **NFL is the one genuine race: Δrace = −1.530 s** (our flag
>   12:37:29.042311 vs the price-moving sweep 12:37:27.512739 — 853 contracts,
>   30¢→72¢, 13 µs-identical fills).
> - **Decomposed (arm + capture on the same box): 1.241 s is FEED — the audio
>   had not yet reached us when they swept — and 0.289 s is our entire decode +
>   pipeline.** So the "pure software race" reading is wrong: 81 % of the gap is
>   upstream of our audio, and the venue-proximity inference below is superseded
>   (the feed gap is ~1 s at BOTH a 4 s- and a 9 s-edge-lag venue).

**VZ (capture-only, no arm):** 3 healthy legs banked for the wideband corpus;
Notified ladder confirmed (10 s edge lag). Settlements: YES = HEAD, HOME, LOYA,
RURA, SATE, SPEC, SYNE, TAIL; NO = 5G, C, OUTA, STARL. No race scoring (no arm);
value is corpus + venue-class data. Pre-remarks basket bets recur here too
(5G/C/LOYA/SPEC/SYNE swept 12:26–27, before 12:30 remarks) — same wrong-bet
pattern; several settled YES anyway (release-text-driven, coincidentally right).

**Program consequence:** the levers are unchanged and confirmed — **precision**
(false-fire class) and **decode speed** (the −2 s at a 4 s-lag venue proves
acquisition proximity alone won't win). Detector tuning (P1) + chunk-16 export
(P2, g5 pending) remain the path. Evidence:
`state/KXEARNINGSMENTION{AXP,VZ}-26JUL24/{report.json,tape-2026-07-24.json}`,
`~/{axp,vz}-capture/{report,manifest-probe}` (box).

#### DPZ 2026-07-20 — TAPE-ONLY (our capture never ran; box never started — RUNBOOK §DPZ outcome)

No Δw is computable (no word-end reference). Tape: all 15 markets, 5,244 trades, archived
`state/KXEARNINGSMENTIONDPZ-26JUL20/tape-2026-07-20.json`. Call 12:30–~14:15 UTC. Five YES
settlements: JOE, BEST, SAME, SOCC, PARM. Shape findings:

| Word | During-call behavior | Read |
|---|---|---|
| JOE (new CEO) | 92¢ by 12:27 (pre-call); NO-taker bursts at 90–91¢ after | pre-priced, no race possible |
| SAME (same-store) | 97¢ at 12:26 (pre-call) | pre-priced, no race possible |
| BEST | **10 fills at ONE µs-identical timestamp 12:49:34.999563Z, YES-taker ladder 79→85¢ from a 72¢ book (~118 contracts); no wave-2 in the next 45 s** | the ONLY machine-signature sweep of the whole call — same single-IOC-ladder shape as MS wave-1 |
| SOCC | sat 35–40¢ through the entire call; first ≥90¢ at **15:16:39** (≈1 h post-call), swept 50→97¢ in 1.5 s | never raced during the call; transcript-reader money |
| PARM | drifted 45–64¢ in Q&A (human-speed, fills spread over 20+ s); first ≥90¢ at **15:28:55** | never machine-raced |

**Verdict (tape-only, weaker than a Δw verdict): UNCONTESTED-to-THIN.** The MS-class wave-1
population did not show up on this small-cap event — one sweep all call (BEST, and with no
Δw reference its speed class is unknown: could be a fast machine or a ~20 s-lagged webcast
bot; the µs-identical ladder only proves machine execution, not fast hearing). Two YES words
sat at 35–73¢ through the whole call — **any listener at any latency class, including a
naive webcast player, could have printed on SOCC/PARM.** Strategy consequence [hypothesis,
one sample]: contestedness scales with event size — small/mid-cap mention events may be a
lightly-defended profit pool where even the un-upgraded webcast lane wins; strengthens the
W7 fillable case for this class once W1 confirms on a second tape. Open item: obtain the DPZ
transcript/recording to check whether SOCC/PARM were spoken late-call (which would shrink
the "sat uncontested" window) — and note BEST's sweep stopped at 85¢, leaving 85→99¢ on the
table, unlike MS's 95–98¢ ladders.

#### ALLY 2026-07-21 — TAPE-ONLY (no capture: the m3u8 surfacing step did not happen; no instruction reached the session in time)

Tape: 11 markets, archived `state/KXEARNINGSMENTIONALLY-26JUL21/tape-2026-07-21.json`. Call
13:00 UTC. **Our capture never ran** (no one surfaced the m3u8 / drove the box this morning —
box has no `~/ally-capture/`), so no Δw is computable. **Settled 2026-07-21** — YES: EV, HEAD,
OIL, TARI, VOLA; NO: AI, BASE, BUYB, CRED, RATE, TAIL. Shape findings — **the opposite of DPZ**:

- **All FIVE YES words had a machine-signature same-ms multi-fill sweep DURING the call:**
  VOLA 56 fills @ 13:00:20.841 (83¢, ~20 s after start); HEAD 16 @ 13:06:28.118 (89–95¢,
  wave-2 +472 ms); TARI 44 @ 13:37:47.821; OIL 36 @ 13:37:52.788; EV 23 @ 13:57:43.833 (→95¢).
- **TARI and OIL swept 5.0 s apart** (both settled YES) — a single sentence hitting two
  markets; whoever swept was **listening live**, not pre-positioning.
- The NO words also drew bursts (TAIL 17 @ 13:05 to 87–89¢, CRED 41 @ 13:45, AI/BUYB NO-taker
  sweeps) — fast money took positions that later unwound; i.e. the machines were *acting on
  the audio in real time*, right and wrong, not settling pre-priced markets.
- **Verdict (tape-only): CONTESTED** — a machine-speed population traded live on every YES
  word. [hypothesis, 2 samples now] Contestedness tracks **phone accessibility, not market
  cap**: ALLY published a self-serve dial-in registration (everyone's fast leg is cheap) and
  was heavily contested; DPZ's bridge was unpublished and its tape was nearly empty of fast
  money. If it holds, the exploitable pool is events with **no public dial-in** (unpublished-
  bridge or webcast-only), and ALLY-class open-dial-in events are the arms race to avoid.
  The Wed webcast-only Δw samples (TSLA/GOOGL, W1) are the direct test.

#### GEV 2026-07-22 — TAPE-ONLY (no capture: prep cues queued while the session was idle; box never driven)

Tape: 12 markets, 3,666 trades, `state/KXEARNINGSMENTIONGEV-26JUL22/tape-2026-07-22.json`.
Call 11:30–~12:45 UTC. FINALIZED — YES: ADMI, CHIN, DATA, ELEC, INFL, NUCL, ROBO, TARI;
NO: BLAD, IRAN, OIL, VIET. No Δw computable (no word-end reference).

- **7 of 8 YES words were pre-priced ≥83¢ before the call** (last pre-call prints: DATA 98,
  NUCL 97, TARI 95, INFL 91, ELEC 89, CHIN 87, ROBO 83) — GEV's script (nuclear/data-center/
  electricity/tariff/China) is predictable; the in-call bursts on those were 90s→99 top-ups.
- **The one unpriced YES word was machine-raced: ADMI, 36¢ pre-call.** Wave-1
  12:08:44.464 (13 same-µs fills, 36→85¢), wave-2 12:08:47.252 (+2.8 s, 13 fills 95→99¢).
  First ≥90¢ print 12:08:45 — same two-wave shape as MS/ALLY.
- **Wrong-way sweep proves live listening**: IRAN (settled NO, 75¢ pre-call) took a
  16-fill YES-taker burst 55→84¢ at 12:08:37.670 — 6.8 s BEFORE the ADMI wave — and later
  unwound. Machines were acting on the audio in real time, right and wrong.
- VIET sat 84¢ pre-call and settled NO (big human unwind; not a race).

**Verdict (tape-only): CONTESTED but pre-priced-thin** — the fast population showed up, yet
only one YES word offered a race at all. On events with predictable scripts the edge pool is
the un-obvious words, not the headline ones.

#### ALK 2026-07-22 — TAPE-ONLY (no capture: same failure mode, same morning)

Tape: 13 markets, 4,750 trades, `state/KXEARNINGSMENTIONALK-26JUL22/tape-2026-07-22.json`.
Call 15:30–~16:45 UTC. **Markets NOT settled at analysis time (18:00 UTC)** — sweep
directions are read from price action, not results; re-check post-settlement.

- Machine-signature same-µs bursts DURING the call: HEAD 15:33:22 (96→99¢), **AMAZ
  15:36:10.079 (66→95¢) with wave-2 15:36:18.442 (+8.4 s, 90→99¢)**, **ONE 15:56:16.245
  (47→84¢) and 15:56:16.330 — 85 ms apart — then 15:56:20.441 (95→99¢)**: either two
  distinct fast players or one split ladder; TAIL 16:04:34 (43→95¢) + 16:06:49 (94→99¢).
- OIL whipsawed 55→75¢ then crashed to the 20s under NO-taker bursts (16:09–16:28) — a
  speculative sweep unwinding in real time, same wrong-way class as GEV's IRAN.
- Pre-call the same population traded CARG/HEAD/ICEL bursts all morning (results filed
  Tuesday evening → partly pre-priced, as predicted in the prep note).

**Verdict (tape-only): CONTESTED.** *Settlement re-check (07-22 evening): YES = AMAZ,
BOEI, CARG, HEAD, ONE, STAR, TAIL; NO = AI, ICEL, IRAN, NEW, OIL, TARI. Every
machine-swept word settled YES ✓ and OIL settled NO ✓ (the whipsaw was wrong-way, as
read) — the intra-day sweep-direction reads were all correct.*

**Hypothesis update (4 samples): "contestedness tracks phone accessibility" is WEAKENED —
likely wrong as stated.** GEV and ALK are bridge-likely with NO published dial-in, yet both
drew the machine population in-call; DPZ (unpublished bridge, small-cap) stayed thin; ALLY
(public dial-in, liquid) was contested. Best current read: **contestedness tracks event
liquidity/prominence**, not dial-in publication. Consequence: the W7 fillable case narrows
to small/illiquid events until the GOOGL/TSLA Δw samples (the only same-floor DESIGNED
measurements) say how far behind the webcast lane actually is.

#### TSLA 2026-07-22 — THE FIRST MEASURED WEBCAST RACE (capture + live observe-arm + tape)

Artifacts: `~/tsla-capture/` (2 WAVs ~43 min + report), observe-arm
`state/KXEARNINGSMENTIONTSLA-26JUL22-OBSERVE/report3.json` (29 detections, dry-run) +
`observe-arm3.log` (wall-clock flags), final tape + settlements (same day). Call 21:30 UTC;
our arm's audio started 21:35:30 (see RUNBOOK ops post-mortem), joined BEHIND the live edge
and caught up at ~1.5× — pairs are labeled drain-phase vs steady-state accordingly.
Vantage: CDMX laptop, default yt-dlp edge join (NOT us-east, NOT edge-tuned).

**Δrace = wave-1 burst − our live Confirmed flag (same utterance):**

| Word | Their wave-1 | Our flag | Δrace | Phase |
|---|---|---|---|---|
| SEMI | 21:34:10.3 | 21:37:54.0 | −223.7 s | drain |
| REGU | 21:35:44.4 | 21:39:06.5 | −202.1 s | drain |
| SPAC | 21:42:22.4 | 21:44:37.7 | −135.2 s | drain |
| SCHG | 21:46:02.1 | 21:46:10.6 | **−8.5 s** | steady |
| COMP | 21:51:00.1 | 21:51:10.4 | **−10.3 s** | steady |
| AFFO | 21:55:58.2 | 21:56:05.5 | **−7.3 s** | steady |

**Headline: steady-state Δrace ≈ −8.7 s** (n=3). The fastest webcast racers beat our
un-tuned CDMX leg by single-digit-to-low-double-digit SECONDS, not minutes — and the drain
pairs prove most of any larger apparent gap is fetcher join-position, not competitor magic.
**Verdict per the pre-registered discriminator: H1/H3 (co-listeners on the same public
feed, better fetch position/vantage). WINNABLE.** Levers, in order: (1) join at the live
edge from t0 (drain artifact = 0 by construction), (2) edge-tuned leg (streamlink-class,
measured ~13 s faster than default on the box ladder), (3) US vantage (unmeasured residual;
X_audio), (4) detector decode (~0.4 s, small at this scale).

**Recall/precision (settled results = authority, capture replay = audio authority):**
- Raced settled-YES armed words 11: live flags 8 ✓ (SEMI REGU SPAC MEGA SCHG AFFO COMP
  ROBO), live misses 3: **TARI (leg-dependent — the capture leg's audio contains it and
  replay detects it ~4 s post-burst; the arm's own stream never yielded it), SAFE
  ("safety monitor" compound — missed in live AND replay), TARA ("terafab" — OOV-ish for
  the gigaspeech KWS; missed in both).**
- Live false fires 3/11: IRAN (×2, drain-phase), EURO (×4!), CHIN — all settled NO. CHIN
  is strange: live and replay agree something "china"-like at ~21:59, yet 91¢-pre-call CHIN
  settled NO (transcript check pending). EURO is a systematic confusion (4 live + 2 replay
  hits on a settled-NO word).
- **The precision win of the day: MERG.** The fast money swept MERG 27→85¢ at 21:57:40 on
  the "acquire or merge" Say question anticipation — and it UNWOUND to 7¢ within 5 min;
  settled NO. Our variant-exact detector never fired live. Speed without precision paid;
  our precision would have (correctly) refused the trade.
- Wrong-way sweep catalogue grows: MERG (TSLA), IRAN-late (TSLA, 12→58¢ 21:59 → NO),
  NANO/AUTO (GOOGL) — the fast population overtrades near-words; every unwind is edge
  for a precise listener.

Say-prior grading for this event: **FAIL** (1/5 vs both baselines) — full scoring +
post-mortem in docs/SAY-QA-PRIOR.md.

#### GOOGL 2026-07-22 — capture + tape (no arm; observe config not staged for GOOGL)

Capture 14:38–15:33 (missed 14:30–14:38 incl. CHRO's fire — operator-error post-mortem in
RUNBOOK). Settled YES: CHRO HEAD NVID RECA SEAR TAIL WIZ (7); the threshold/compound
markets (GEMI="Gemini 3", YOUT="YouTube Shorts", NANO="Nano Banana") all NO after heavy
whipsaw — the same overtrading signature.

**Replay adjudication (capture WAV through the same detector; wall times ±10 s — mapped
from capture start, no live arm ran on GOOGL):**
- **RECA — the near-win and the free lunch.** Replay detects "recall" at ≈21:08:57, i.e.
  AT-OR-BEFORE the 21:09:10.0 wave-1 that took RECA 10¢→99¢ (Δw ∈ [−13 s, +7 s] within
  timestamp uncertainty — from a CDMX capture leg, we were in the same second-class as the
  winners on this word). AND replay finds an EARLIER "recall" at ≈20:54:41 that the entire
  market ignored — RECA sat at 10¢ for ~14 minutes after it. Same class as GEV's ADMI:
  even on a mega-cap, single words go unraced for minutes. (Caveat: the 20:54 hit could be
  a near-word FP; the market's own 21:09 race + YES settlement confirms at least the later
  utterance. Transcript check open.)
- Recall on raced settled-YES words: CHRO ✓(×5, first at 20:58 — its original fire predates
  our capture), SEAR ✓, WIZ ✓, HEAD ✓, RECA ✓; **TAIL ✗, NVID ✗** → 5/7.
- Precision: YOUT ("YouTube Shorts") fired ×2 on a settled-NO market — compound near-word
  confusion, same failure family as TSLA's EURO. GEMI/NANO correctly silent.

**Cross-event scoreboard (first measured, 2 events, CDMX vantage):** detector recall on
raced settled-YES words 14/18 ≈ 78% (misses: TAIL, NVID, SAFE, TARA — compounds/OOV
skew); ~4 fired-but-settled-NO words (EURO, CHIN, YOUT, MERG-replay). Both axes — recall
on compounds and near-word precision — are now measured problems with a corpus (today's
WAVs) to tune against.

#### LMT 2026-07-23 — FIRST FANOUT ARM RACE (autonomous; live fanout arm + box 4-leg capture + tape)

First live run of the edge-join fanout posture (2 legs: streamlink `--hls-live-edge 1`
+ tuned ffmpeg `-live_start_index -1`, probesize 64K), fully autonomous (operator
absent), CDMX laptop, dry-run. Venue: Notified MMC → CloudFront, **audio-only 77 kbps,
4 s segments, ~40 s manifest window**, no LL-HLS, PDT edge lag ~9 s, no token/IP lock.
Arm: 99 min, 570,713 frames, **0 drops**; 16/16 detection events heard by BOTH legs at
identical stream positions; exactly one intent per keyword (cross-leg dedupe 16/16).

**Δrace (contested words; sweep = first ≥3-trade YES burst ≥50¢ within 2 s):**

| Word | Our flag (UTC) | Wave-1 sweep | Δrace | Note |
|---|---|---|---|---|
| GOLD ("golden dome" — compound, detected!) | 12:34:54.803 | 12:34:54.198 | **−0.6 s** | tuned leg |
| MORF ("morfius") | 12:57:10.364 | 12:57:09.337 | **−1.0 s** | tuned leg |
| EURO ("europe") | 12:42:03.136 | 12:42:00.316 | **−2.8 s** | tuned leg |

> **⚠ CORRECTION 2026-07-24 (night) — GOLD and EURO were scored against flat-price
> prints, not the price-moving sweep** (`LATENCY-PROOF.md` §THE GAP IS LOCATED):
>
> | word | as pinned | scored against | price-moving sweep | corrected Δrace |
> |---|---|---|---|---|
> | GOLD | −0.6 s | 12:34:54.198 (129 ct, flat 77¢) | 12:34:55.310 (730 ct, 77→95¢) | **+0.507 s — AHEAD** |
> | EURO | −2.8 s | 12:42:00.316 (156 ct, flat 64¢) | 12:42:01.112 (89 ct, 75→85¢) | **−2.024 s** |
> | MORF | −1.0 s | 12:57:09.337 | 12:57:09.336 (51 ct, 45→55¢) | −1.028 s (unchanged) |
>
> **GOLD flips sign — we beat the rally by 0.5 s.** Independently, the box capture
> puts the FEED term at **+1.112 / +1.125 / +0.754 s** for GOLD/EURO/MORF: the sweep
> lands before the word's audio reaches the box, at a venue 5 s *further* from the
> room than AXP's — which is what makes the feed gap venue-invariant.

Steady-state Δrace mean **−1.5 s** vs the −8.7 s TSLA baseline: the acquisition stack
recovered ~6–8 s but did NOT flip the sign at this venue. **Why: the ladder COMPRESSES
at short-window venues** — box 4-leg xcorr (same audio, same run): tuned −1.18 s vs
default, streamlink only −0.21 s (AAL capture replicates: −1.15/−0.19). A 40 s manifest
window means default joins near the edge anyway; the −11.7..−13.65 s reference-stream
ladder numbers DO NOT TRANSFER. **Leg order also reversed: tuned-ffmpeg beat streamlink**
(5/6 first-arrivals; streamlink won only NASA) — opposite of the Sky/DW reference runs.
The residual −0.6..−2.8 s is now decode-lag (~0.5 s) + CDMX vantage + wave-1 colocation
sized — the detector/vantage levers are material again at this gap size.

**Settlements ALL PINNED 2026-07-23 evening (market API, finalized 17:07:46Z;
evidence `state/KXEARNINGSMENTIONLMT-26JUL23/settlements-2026-07-23.json`;
transcript checks still pending):**
- **NASA settled YES ⇒ our 12:38:33 flag was a TRUE detection** that the market
  ignored for 52 min (sweep only at 13:30:24, ~20¢ available the whole window) —
  the market-missed-word channel is now settlement-confirmed, not just inferred.
- PERU (no sweep, post-flag max 36¢) — **settled NO ⇒ FALSE FIRE confirmed**;
  IRAN (max 48¢) — **settled NO ⇒ FALSE FIRE confirmed**.
- **LMT flag-precision final: 4/6** (GOLD, MORF, EURO, NASA true; PERU, IRAN false).

**FULL-EVENT SETTLEMENT GRADE (pinned 2026-07-23 evening; evidence
`state/KXEARNINGSMENTIONLMT-26JUL23/settlements-full-2026-07-23.json` + whisper
transcript, 13/13 transcript↔settlement concordance):** YES = EURO, GOLD, MORF,
NASA, **SIKO**; NO = all others **including ORIO and VENT**. Reclassifications:
- **ORIO and VENT were NOT recall misses** — "orion" appears nowhere in the
  88-min transcript and the speaker said "Lockheed Martin **Ventures**", never
  "venture fund"; both settled NO. Their sweeps (13:31:08 / 13:21:35) were
  wrong bets by other traders; **our silence was correct**.
- **SIKO is the real LMT recall miss**: "sikorsky" said 2×, settled YES, and
  never swept ≥50¢ — **market-missed mention #3**. We did not flag it either.
- MORF was caught even though whisper spells it "Morpheus" — the sherpa keyword
  "morfius" matched the audio phonetically.
**LMT settlement-graded scorecard: recall 4/5 (missed SIKO), precision 4/6.**
- **ORIO (13:31:08) and VENT (13:21:35) swept with NO flag from us — candidate recall
  misses** (VENT = "venture fund", compound-class).

Ops finding (cost ~4 min of pre-call scramble, fixed live): 77 kbps audio-only stream ×
1 MB ffmpeg probesize = ~100 s of silence before first output byte → every leg died on
the 30 s idle guard (arm + all box capture legs incl. defaults). Fixed to 64 KB probe;
the DW dress rehearsal could not have caught it (fat muxed stream). Mid-call caveat pin:
a truncated 100-trade tape pull misread GOLD as "+11.7 s ahead" — the full tape shows the
sweep ONSET pre-dated our flag; never score Δrace from a tail pull.

#### AAL 2026-07-23 — SECONDARY CAPTURE (no arm by design; box 4-leg capture + tape)

Same venue class as LMT (Notified→CloudFront, audio-only 4 s segments; PDT lag ~10 s),
captured concurrently with the LMT primary from the box, autonomously. 3 healthy legs
(~94 min each, hls sugar leg dead as expected). Event-venue ladder REPLICATES LMT:
pipe→pipe_tuned **−1.15 s**, pipe→streamlink **−0.19 s** (peaks 0.9997) — second
same-morning confirmation that short-window venues compress the ladder and reverse
the tuned/streamlink order. Tape: HEAVILY CONTESTED — 8/15 words swept ≥50¢ during
the call (WORL 12:33:13, CENT 12:33:00, IRAN 12:33:21, CHIC 12:34:39, WEAT 12:34:46,
AT 12:35:10, ATH 12:41:19, BASI 12:41:52); 7 no-sweep. WAVs banked on the box for
offline Δw replay vs these sweep stamps (Phase-4 corpus + Δw sample expansion).

**SETTLEMENT-GRADED CORRECTION (pinned 2026-07-23 evening; evidence
`state/KXEARNINGSMENTIONAAL-26JUL23/settlements-full-2026-07-23.json` + whisper
word-level transcript of the 87.5-min capture):** only **5 of 15 settled YES —
ATH, BASI, CHIC, RECO, WEAT** — and the transcript finds exactly those five
spoken (athens 1×, basic economy 3×, chicago 5×, record 5×, weather 1×): **15/15
transcript↔settlement concordance**. Consequences: (1) the 12:33Z basket sweeps
of WORL/CENT/IRAN (and AT at 12:35) were **wrong bets** — words never spoken,
settled NO; wave-1's early sweeps are release-text-driven baskets, NOT heard
mentions. (2) **RECO settled YES without ever sweeping ≥50¢ — market-missed
mention #2** (after LMT NASA). (3) Only 4 of the 8 "swept" words were real
(CHIC/WEAT/ATH/BASI) — Δw replay is valid only against those four stamps.
**Methodology rule going forward: sweeps ≠ mentions; recall/precision are scored
against settlements + transcript, never against tape sweeps alone.**

#### INTC 2026-07-23 — FIRST LIVE FILL RUN (fill-all ≤50¢, $1 envelope, ARM ON US-EAST BOX)

First mention-fill posture ever armed (VERIFICATIONS Q-FILL) and first arm run from
the us-east box. 76-min run, 455,828 frames, 0 drops, both legs clean EOF, 129/129
identical per-leg detection events. 7 intents → 4 observes (by design), 2 LOST_RACE
(TRUM 19¢>11¢ cap, CLI 34¢>27¢ — the frozen-cap no-pump guard refusing repriced
words), **1 SENT + FILLED: APPL 1×YES @26¢, decision→matched-print 9.9 ms** — the
order path at colo-class speed under live fire.

**Race + recall scoring (tape):** CLI Δrace **−1.0 s from us-east** — vantage alone
did not flip the sign; the residual second is decode-shaped (consistent with the
morning's diagnosis). SECU/ULTR pre-priced ≥80¢/repriced before flag (no real race;
ULTR's first utterance ~21:09 was MISSED, flagged only on a ~21:17 repeat).
**Recall misses (swept, never flagged): ARIZ (arizona), DESK (desktop), GOVE
(government), MOBI (mobileye)** — 4 clean misses, worse than LMT's 2. **APPL never
swept and last-traded 1¢ → our fill was a FALSE FIRE — SETTLED NO (finalized
2026-07-23T23:29:46Z, pinned 07-23 evening): realized P&L −26¢** (1×YES @26¢ → 0;
ledger had debited 28¢ worst-case). **TRUM also SETTLED NO (same timestamp) — the
frozen-cap refusal at ask 19¢ was a correct save (+19¢ counterfactual).** Evidence:
`state/KXEARNINGSMENTIONINTC-26JUL23/settlements-2026-07-23.json`. Combined
day-verdict: fill machinery + envelope + no-pump guard all validated exactly;
detector recall AND precision are now indisputably the binding constraint on
making money (Phase-4). Two-day realized program P&L: −26¢.

**FULL-EVENT SETTLEMENT GRADE (pinned 2026-07-23 evening; evidence
`state/KXEARNINGSMENTIONINTC-26JUL23/settlements-full-2026-07-23.json`) —
REVERSES the recall verdict above:** YES = **CLI, FOUN, PANT, SECU** only; NO =
everything else **including ARIZ, DESK, GOVE, MOBI and ULTR**. The four "clean
recall misses" never happened — arizona/desktop/government/mobileye settled NO;
their 21:01–21:03Z sweeps were release-text basket bets (same wrong-bet pattern
as AAL WORL/CENT/IRAN), not heard mentions. Our 7 intents by market:
PANT✓ SECU✓ FOUN✓ CLI✓ (all four real mentions flagged — **recall 4/4**) +
APPL✗ TRUM✗ ULTR✗ (false fires; ULTR settled NO despite trading ≥80¢ — if the
transcript shows "ultra"/"Core Ultra" was spoken, ULTR is a **keyword→market
semantics mismatch** (market required "Ultra 3 / Ultra Series 3"), a phrase-
design defect, not a threshold defect — classify when the replay transcript
lands). **INTC settlement-graded scorecard: recall 4/4, precision 4/7.**

**TWO-EVENT SETTLEMENT-GRADED SYNTHESIS (armed events, 2026-07-23): recall 8/9
(sole miss SIKO), precision 8/13 (false fires PERU, IRAN, APPL, TRUM, ULTR).
Detector PRECISION is the money leak; recall is near-ceiling. Market-missed
mentions settlement-confirmed 3× (NASA 52 min @~20¢, RECO never swept, SIKO
never swept) — a standing edge channel that requires no race win at all.**

Ops: INTC box capture recorded 0 frames — the probesize fix had been applied to the
LMT/AAL capture configs but not INTC's (staged pre-fix). Fixed in the TEMPLATE
(webcast-gev-capture.toml + fanout-arm template) so all future staging inherits it.
Audio for the 4 recall misses is recoverable from the Notified replay (through
08-06) for the Phase-4 corpus. Also: ssh-piped Monitor tails buffer without a tty —
use `ssh -tt` (cost: one silent hour mid-call).

## Open verification items

1. Transcript check on OIL/MIDD (false-alarm confirmation) — needs the MS capture/transcript.
2. `our_then_transport` absolute measurement (dual-box capture-bakeoff, LATENCY-PROOF) tightens
   the their-loop bound from ±250 ms to ±50 ms.
3. Re-run this analysis on the next event's tape (DPZ/GEV/GOOGL) — one event = one sample of
   the competitor population; wave-2's existence suggests ≥2 fast players.
