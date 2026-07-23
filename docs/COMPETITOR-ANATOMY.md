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

## Open verification items

1. Transcript check on OIL/MIDD (false-alarm confirmation) — needs the MS capture/transcript.
2. `our_then_transport` absolute measurement (dual-box capture-bakeoff, LATENCY-PROOF) tightens
   the their-loop bound from ±250 ms to ±50 ms.
3. Re-run this analysis on the next event's tape (DPZ/GEV/GOOGL) — one event = one sample of
   the competitor population; wave-2's existence suggests ≥2 fast players.
