# Post-sweep edge study — scoreboard

**2026-08-06.** Offline, read-only, no orders placed. Decision rules frozen in
`../PRE-REGISTRATION.md` before the TEST set was scored. TEST was scored **once**.
Audited afterwards against the `quant-audit` five-gate framework; **that audit
changed this document's conclusions and its corrections are folded in below.**

---

> ## ⚠ SCOPE CORRECTION — 2026-08-07
>
> **Everything in this document measures entry at the END of a repricing.** That
> is what `EPISODE_MERGE_S = 120` makes it mean: wave-1, wave-2 and wave-3 are
> glued into one episode, so "buy the residual" is "buy after the *last* wave".
>
> It therefore says nothing about buying **inside** the ladder, between the first
> wave and the decisive one. That population was excluded by construction, never
> scored, and it behaves differently — measured strictly-causally on the same
> tapes, a bid at 70¢ after a wave-1 that reached ≥95¢ pays **+20.2¢/trade**
> (9/12, 9 events) where this document's FOLLOW arm pays −22.5¢.
>
> The sign in audit finding 1 below (`merge_s = None` → +10.9¢) was the first
> sign of it, and this document called it a specification artifact. It was not an
> artifact; it was a different trade. See `../PRE-REGISTRATION-R2.md` and
> `scoreboard-r2.md`.
>
> **Nothing below is retracted.** The 94.8% no-residual rate and the
> adverse-selection gap are correctly measured *for end-of-episode entry*. Only
> the unqualified word "sweep" in the headline was too broad.

## Headline

**Buying what a sweep leaves behind once it has finished does not work, and the
reason is structural rather than statistical: 94.8% of the time there is nothing
left to buy, and the 5% of the time there is, it is because the sweeper was
wrong.**

The study's *strong* result is that mechanism — it rests on proportions with
large samples. The per-trade EV verdicts are **all underpowered nulls** and
should not be quoted as refutations. The audit forced that distinction.

Read "sweep" throughout as "completed repricing". Entry *during* a repricing is a
separate study (round 2) and is not covered by any number here.

---

## What is well established

| claim | measurement | 95% CI | n |
|---|---|---|---|
| A sweep leaves **nothing** liftable behind | **94.8%** | [91.5%, 96.9%] | 270 |
| P(sweep was right \| a residual existed) | **21%** | [8%, 48%] | 14 |
| P(sweep was right \| nothing was left) | **68%** | [62%, 74%] | 256 |

**The last two intervals do not overlap**, so the adverse-selection gap is real
even at n=14, and it replicates on TRAIN (29% vs 67%). The direction holds at
every value of every parameter tested.

**The presence of a residual is itself the signal that the sweep was wrong.**
When the competitor is right the book clears to 99¢ and there is nothing to buy.
You can only buy when you shouldn't. That is not fixable by being faster or
more selective — it is what "leftover" means here.

Nor is the residual cheap enough to compensate: the average TEST fill was **42¢
against a realized 21% YES rate** (TRAIN: 56¢ against 29%) — roughly double the
true probability.

---

## Arm verdicts (all underpowered — read the power column)

| arm | TEST per-trade | 95% CI | n | min. detectable effect | verdict |
|---|---|---|---|---|---|
| **FOLLOW** buy YES residual ≤70¢ | −22.5¢ | [−43.0, +0.6] | 14 | ±34–48¢ | **NOT-RESOLVED** (underpowered) |
| **FADE** buy NO on a stalled sweep | +0.9¢ | [−20.0, +15.9] | 55 | ±17–30¢ | **NOT-RESOLVED** |
| **LEVEL** buy NO on a market resting 50–89¢ | −0.5¢ | [−6.9, +5.9] | 137 | ±6–16¢ | **NOT-RESOLVED** (best-powered) |
| **MISSED** buy YES where the market never swept | 0 fills / 53 triggers | — | 0 | — | **NO-DATA** |

No arm is promotable under the pre-registered bars (P7). **No arm is formally
refuted either** — the audit's power analysis is explicit that this study could
not have detected a real ±5¢ edge in any arm.

All three are also **indistinguishable from an efficient market**:

| arm | realized | efficient-market null (95%) | p |
|---|---|---|---|
| follow | −22.5¢ | −1.9¢ [−22.5, +20.4] | 0.977 |
| fade | +0.9¢ | −1.8¢ [−13.7, +10.0] | 0.386 |
| level | −0.5¢ | −1.7¢ [−8.5, +5.4] | 0.399 |

---

## Audit findings (quant-audit, run after scoring)

**1. Gate 1 — a specification parameter drives the FOLLOW result, and it was
not in the pre-registered grid.** `merge_legs` extends an episode forward, and
FOLLOW fires at the episode end, so the merge window decides what counts as
"residual" versus "part of the sweep":

| `merge_s` | fills | fill rate | per-trade | 95% CI |
|---|---|---|---|---|
| None | 46 | 10% | **+10.9¢** | [−16.5, +33.5] |
| 30 | 16 | 5% | −13.6¢ | [−36.5, +14.1] |
| 120 *(pre-registered)* | 14 | 5% | −22.5¢ | [−43.0, +0.6] |
| 300 | 12 | 5% | −28.3¢ | [−44.9, −4.4] |

The sign flips. **S3 covered min_terminal/min_jump/min_contracts/wait_s but not
`merge_s` — a genuine gap in my own pre-registration.** No cell is significant
(every CI straddles zero except merge_s=300), so this does not rescue FOLLOW,
but it does mean the specific figure −22.5¢ carries less weight than it appears
to. Unmerged, the arm is no longer buying leftovers — it is joining a sweep
already in progress, on correlated observations, which is the double-counting
the merge exists to prevent.

The adverse-selection mechanism survives all four settings directionally
(residual-exists P(YES) 63/31/21/17% vs nothing-left 77/68/68/69%), though its
magnitude does not.

**2. Gate 3 — the pre-registered null was provably degenerate and was
replaced.** For a single-side arm, permuting settlements is mathematically
constant: `sum_pnl = 100·(#wins) − Σprice − Σfee` depends only on the *count* of
wins, not which trades won. All three arms returned identical min and max across
2,000 shuffles. Replaced with an efficient-market null (draw
`won ~ Bernoulli(price/100)`, under which a trade pays exactly −fee).
Documented in `robustness.py::null_test`.

**3. Power — the null claims were overstated in the first draft of this file.**
It claimed "roughly 4× the data would resolve a ±3¢ edge". Two SE estimates
bracket the truth: the cluster bootstrap gives a ±6.4¢ half-width; the
conservative `sd/√events` (which assumes perfect within-event correlation)
gives ±15.9¢. **The LEVEL null is trustworthy for effects above roughly 6–16¢,
not for a few cents.** Resolving a real +5¢ edge needs on the order of
500–600 independent trades — and independence is at the *event* level (26
events), not the trade level.

**4. Gates that could not be run as designed.** S1 (the 90–100¢ method-sanity
bucket) is under-powered to the point of being uninformative — n=2 FOLLOW and
n=0 FADE trades land there, precisely *because* that is the bucket where no
residual exists. It should not be quoted as passed. S4 (basket discriminator)
found **zero** baskets in TEST, so it is untested, not refuted.

---

## Reconciling `VERIFICATIONS.md` Q-WNT-CLOSE

Q-WNT-CLOSE says of the 26JUL29 WNT tape: *"Nothing is left after the sweep."*
A later reading of the same tape found more volume in the 60 s after the sweep
than in the sweep itself. Both cannot be right. Measured per episode:

| sweep terminal | episodes | takeable ≤90¢ in the next 60 s | settled |
|---|---|---|---|
| **99¢** | 7 (IRAN, IRAQ, BLAN, TRUM, MAIN, OIL, MIDT) | **0 contracts, every one** | all YES |
| **60–82¢** | 5 (AI ×3, TRUM ×1) | 624, 338, 336, 13, 4 contracts | all AI settled NO |

Event totals: **19,421 contracts in-sweep vs 3,525 in the next 60 s = 0.18×.**

- **Q-WNT-CLOSE is CONFIRMED, but only for sweeps terminating at 99¢.** For
  those, takeable size below 90¢ is exactly zero.
- **The "more volume after" reading is REFUTED at the event level** (0.18×, not
  >1×). It came from a looser sweep definition — first print ≥90¢ — that
  captures only the tail of the sweep and so makes the sweep look small.
- Sweeps that stall below 90¢ *do* leave takeable size, and here every such
  market settled NO — the same adverse-selection mechanism, on a second venue.

**Proposed pin amendment** (not written by this lane; `VERIFICATIONS.md` is
owned elsewhere): scope the sentence to *"Nothing is left after a sweep that
reaches 99¢; sweeps that stall below 90¢ leave takeable size, and on this tape
those markets settled NO."*

---

## Other robustness (`../robustness.py`)

- **S2 window-free** — same conclusions with no call window: follow −12.7¢
  (n=31), fade +0.7¢ (n=144), level −0.5¢ (n=137).
- **S3 parameter grid** — 81 cells (`grid.txt`). **0/81 cells meet the promotion
  EV+CI bar, for any arm.** No choice of `min_terminal` / `min_jump` /
  `min_contracts` / `wait_s` rescues anything. FOLLOW is negative in every cell
  (−5.5¢ to −34.6¢); FADE ranges −1.2¢ to +10.6¢ but is never significant, and
  its best cells are its smallest (n=25–30). LEVEL is invariant at −0.5¢ n=137
  across all 81 — the correct sanity result, since it does not depend on sweep
  segmentation at all.
- **S5 settlement lag** — not computed (timestamps banked for only 3 events).
  Order of magnitude is hours; every EV number here ignores capital cost.
- **S6 overlap** — 25 of 43 fade markets (58%) are also level markets. The arms
  are substantially the same trades; do not add their results.
- **LOEO** — follow 0/7 sign flips; fade 7/17; level 4/26. The fade and level
  nulls are genuinely centred on zero, not carried by one event.

---

## The provisional prior, and why it was wrong

`PRE-REGISTRATION.md` P1 recorded FOLLOW **−27.5¢** (n=33) and FADE **+15.4¢**
(n=77). `../replication.py` reproduces that method verbatim, then corrects one
component at a time (TRAIN only):

| step | FOLLOW | FADE |
|---|---|---|
| 0. provisional method, transcribed verbatim | −27.6¢ n=36 | +15.0¢ n=83 |
| 1. + real call windows (was: trade density) | −23.0¢ n=33 | +12.2¢ n=85 |
| 2. + corrected sweep primitive (was: first ≥50¢) | −45.8¢ n=11 | +62.5¢ n=26 |
| 3. + execution-realistic fills = **full pipeline** | −4.5¢ n=4 | −2.0¢ n=2 |

Step 0 reproduces the prior to within 0.4¢/trade — replication gate passes.

- **Step 2 destroys the sample.** Most "sweeps" were not sweeps: the provisional
  rule fired on markets already priced 55–97¢ *before the call* that never
  repriced during it.
- **Step 3 destroys the edge.** Requiring that an offer actually existed
  collapses n to 4 and 2. **The +15.4¢ fade edge was almost entirely the
  artifact of assuming a fill at a price nobody offered.**

---

## Four method errors this study found in itself

The reusable part:

1. **Density-inferred windows are circular** — inferring the call window from
   trade density put INTC's window at 20:30Z and scored pre-call trades as
   sweeps. Real call times give 21:00Z, matching the documented basket.
2. **An inclusive fill bound is lookahead** — the first fill model credited a
   fill on the sweep's *own final print*. Kalshi batches a ladder into one
   microsecond, so the bound must be strict.
3. **"Cheapest in the window" is lookahead** — taking the best price over 3600 s
   let the model buy NO at 1¢ because the market touched 99¢ sometime that hour.
   Fills must be the *earliest* qualifying print.
4. **A label-shuffle null is degenerate for single-side binary arms** — see
   audit finding 2.

Each of 1–3 inflated the result in the study's favour. All four are
regression-tested.

---

## Hypothesis generated by TEST — explicitly NOT a result

The mechanism implies its own mirror: if a residual's existence predicts ~79%
NO and the residual prices at 42¢, then *buying NO whenever a YES residual
appears* would have paid here.

**This is not a finding.** It is an arm invented after seeing TEST — exactly
what pre-registration exists to prevent — on n=14. It goes on the next round's
pre-registration and is tested on data not used here, or it is not quoted.

---

## What would actually move this

- **The orderbook, not the trade tape.** Every "no offer" here means *nobody
  took* an offer, not that none rested. **No Kalshi book tape exists for any
  armed event yet** — `examples/book-tape.rs` and the `FrameTap` seam are built
  and tested but have never been run during an arm. That single measurement
  decides both the 94.8% no-residual rate and the whole MISSED arm.
- **~500 independent trades** to resolve a ±5¢ edge, against 26 events today.
  At ~5 events/week that is a season of data, not a re-analysis.
- **A venue where baskets occur**, to test S4 at all.

## Data

39 of 44 mention events usable; **601 labelled markets**; 342 in-window sweep
episodes. Excluded: 5 for missing settlements (all 26AUG07, not yet settled),
2 for unresolvable call times (ALK 26JUL22, NFLX 26JUL02). Tapes and
settlements banked in `../tapes/` from the public, no-auth `GET /markets/trades`
and `GET /markets`.

**Reproduce:** `for m in windows sweep_segment fill_model data arms; do python3
lib/$m.py; done && python3 lib/acceptance.py && python3 replication.py &&
python3 score_arms.py --split test && python3 robustness.py --split test`
