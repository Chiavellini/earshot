# Pre-registration R2 — the INTRA-SWEEP arm

**Frozen 2026-08-07, before any holdout event has settled.** Amendments are
appended with a date and a reason; nothing is deleted (house rule, same as
round 1).

---

## R0 — What round 1 got wrong, and why this is a separate study

Round 1's FOLLOW arm bought the **residual after a repricing finished**. Its
headline — 94.8% of sweeps leave nothing liftable, and where something is left
it is because the sweeper was wrong — is measured correctly and stands **for
that entry point**.

It does not address buying **inside** the ladder. `EPISODE_MERGE_S = 120` glued
wave-1, wave-2 and wave-3 into one episode by design, so "after the sweep"
meant after the *last* wave. The population between waves was excluded by
construction, never scored, and round 1's audit brushed against it exactly once:
the `merge_s = None` row measured **+10.9¢**, the most favourable cell in the
study, and was dismissed in the scoreboard as a specification artifact.

It was not an artifact. It was a different trade.

---

## R1 — The claim under test

> After a competitor's first wave takes a mention market to the top of its
> ladder, the book gives some of it back before the decisive wave. A resting bid
> below the wave gets filled on that give-back, at a price the eventual
> settlement justifies.

**One arm. No others.** Round 1 ran four arms and paid for it in multiplicity;
this study tests one rule.

---

## R2 — The frozen rule

Every term is computable at the instant wave-1's last print lands. This is
enforced structurally, not by convention: `lib/wave_ladder.py::Wave` holds its
own prints and the resting price before it, carries no reference to the source
tape, and `qualifies()` accepts a `Wave` and no second data argument. The
self-test asserts both, so re-opening the hole fails the suite.

```
wave-1     consecutive taker-YES prints, inter-print gap < 2.0s
qualifies  top(wave-1) >= 95c
           AND top(wave-1) - resting_price(60s before onset) >= 15c
           AND (contracts(wave-1) >= 25 OR n_prints(wave-1) >= 3)
           AND onset inside the call window (lib/windows.py — resolve or refuse,
               never infer; round 1's density heuristic put INTC at 20:30Z and
               scored pre-call prints as sweeps)
action     rest a buy-YES bid at 70c, size 1 contract
cancel     on the first print >= top(wave-1), OR after 60s — whichever first
exit       hold to settlement
```

### Why these constants, given that each was also a winning cell

This is the researcher-degrees-of-freedom problem, stated before it can be
argued away. All of TRAIN has been inspected, so every threshold below was
chosen while its score was visible. Each therefore needs a reason that is not
its score.

| constant | ex-ante justification | what it scored (TRAIN) |
|---|---|---|
| **cap 70¢** | the user's own number, stated before any of this was measured | best of {70, 80, 90} at +21.0 / +18.6 / +10.5¢ |
| **wave-1 ≥ 95¢** | `COMPETITOR-ANATOMY` puts wave-2 at 95–98¢ and wave-3 at 98–99¢; round 1 measured **zero** takeable size below 90¢ after a 99¢ sweep | best of {90, 95, 98} at +13.7 / +21.0 / +14.3¢ |
| **burst gap 2.0 s** | inherited unchanged from round 1, where it was set from the same anatomy doc and validated against 7 documented onsets to the microsecond | not re-tuned here |
| **jump ≥ 15¢, size floor 25 ct / 3 prints** | inherited unchanged from round 1 | not re-tuned here |
| **cancel on recovery** | this is what "between the waves" *means*: once the price is back at wave-1's top, the decisive wave has arrived and we are no longer early | not a tuned constant |
| **60 s backstop** | deliberately **not** the best cell — a fixed window was the single largest lever found (30 s → +38.1¢ n=18, 60 s → +21.0¢ n=22, 300 s → +7.4¢ n=28), so the middle value is taken and the curve is reported | middle of three |

Because **cap** and **wave-1 threshold** were both best-scoring, promotion
carries an extra condition (§R5): the sign must hold across the whole declared
range of each, not only at the frozen value.

### Exit is settlement, and that was measured, not assumed

Selling into the decisive wave at 97¢ caps the trade at ~+30¢ where holding to
settlement pays +43.5¢. `lib/exit_model.py` models the sell side properly (a
taker-NO print at yes-price P proves a resting buy-YES bid at P, so we could
have sold at P) and reports both legs on every trade without choosing between
them per-trade — choosing per-trade is itself the lookahead. Holding wins, so
the frozen rule holds.

**The "riskless" framing is rejected on the evidence.** 5 of 22 TRAIN trades lost
32–64¢. `exit_model.adverse_excursion()` is reported on every trade for the same
reason: a round trip that finished green after marking 40¢ underwater was a
winning gamble, not a riskless one.

---

## R3 — TRAIN is contaminated and can never confirm anything

**TRAIN = every mention event banked through 2026-08-06** (39 events, 601
labelled markets). Per-trade tables have been inspected for all of them. TRAIN's
only jobs are to define the rule above and to report sensitivity. No TRAIN
number may be quoted as evidence for the arm.

Its reference values, recorded so a regression is detectable:

| | |
|---|---|
| frozen rule, TRAIN | **+21.0¢/trade**, 17/22 YES, 10 events, 6 days, Wilson [0.57, 0.90] |
| control: same rule with **no** wave-1 filter | **−6.4¢**, 48% YES, n=95 |
| LOEO worst case | +17.5¢ (dropping AMD-26AUG04) |
| fire rate | 22 fills from 335 qualifying wave-1 bursts ≈ **0.6 fills/event** |

The control matters as much as the headline: it is the round-1 population, and
it is negative. If a future change makes the filter stop separating the two,
the primitive has broken.

---

## R4 — The sacred holdout

**HOLDOUT = every mention event settling on or after 2026-08-08.** Scored
**once**, at close, under the rule exactly as frozen above.

- **Data collection is not scoring.** `fetch_tapes.py` banks holdout tapes as
  events settle. `score_r2.py --split holdout` is **refused** until the close
  condition is met; the guard is in code, not in discipline
  (`--close-the-holdout` must be passed explicitly and the event count checked).
- **No interim peeking.** This was chosen deliberately over the
  interim-reporting variant: seeing a running tally is what tempts re-tuning.
- **Editing the rule burns the holdout.** Any change to §R2 restarts the holdout
  from the date of the edit, and this file records the edit.

### Close condition, derived rather than picked

Round 1's audit finding A1.3 was that its bars were written without a power
analysis and were far too weak for the effect they gated on. Not repeating that.

With the TRAIN effect δ = +21¢ and the observed per-trade sd σ = 45¢, detecting
δ at 80% power and 5% two-sided needs

```
n >= 7.85 * sigma^2 / delta^2 = 7.85 * 2025 / 441 = 36 INDEPENDENT observations
```

Independence is at the **event** level, not the trade level. So:

**The holdout closes at 36 scored events, or 12 weeks (2026-10-30), whichever
comes first.** At ~5 events/week and 0.6 fills/event, 36 events is roughly 7
weeks and about **22 fills**.

The uncomfortable part, stated now: if the true effect is smaller than TRAIN
suggests, this study cannot see it. δ = +10¢ needs 159 events; δ = +5¢ needs
636. **A null result at close therefore means NOT-RESOLVED, never REFUTED** —
the same conclusion round 1's audit forced, pre-committed this time instead of
discovered afterwards.

---

## R5 — Decision bars

**PROMOTABLE** requires all of:

- per-trade EV after fees **≥ +5¢** on HOLDOUT, and
- event-clustered bootstrap 95% CI lower bound **> 0** (10,000 resamples,
  resampled by event), and
- **n ≥ 20** fills over **≥ 30 events** and **≥ 10 distinct days**, and
- survives leave-one-event-out with no sign flip, and
- **sign holds at wave-1 ∈ {90, 95, 98} and cap ∈ {60, 70, 80, 90}** — the
  multiplicity condition for the two constants that were chosen while their
  scores were visible, and
- beats the efficient-market null (`won ~ Bernoulli(price/100)`, under which a
  trade pays exactly −fee) at p < 0.05.

**REFUTED** requires HOLDOUT CI upper bound < 0 **and** n ≥ 20. Anything else is
**NOT-RESOLVED**, which is a real reportable outcome and not a failure to be
argued around.

---

## R6 — Pre-declared sensitivity (reported, never used to promote)

- **S1 — the two tuned constants.** Full curve over wave-1 ∈ {90, 95, 98} ×
  cap ∈ {60, 70, 80, 90}. Declared here because both were best-scoring cells.
- **S2 — cancel policy.** Recovery-cancel (frozen) vs fixed windows
  {15, 30, 60, 120, 300} s. The largest known lever; reported in full.
- **S3 — `merge_s`.** Round 1's audit found this drove its FOLLOW result and was
  missing from its grid. It is the parameter this whole study turns on, so the
  merged-vs-unmerged comparison is a required output, not a footnote.
- **S4 — burst gap** ∈ {1, 2, 4} s: does the wave boundary itself matter?
- **S5 — minimum printed size at entry** ∈ {0, 5, 25} contracts. Directly probes
  the fill assumption: if the edge lives only in 1-contract prints, it is dust
  we could not have captured at size.
- **S6 — window-free variant**, as in round 1.
- **S7 — adverse excursion** distribution, to keep "riskless" honest.
- **S8 — size-weighted P&L** using actual printed size. See §R7.

---

## R7 — What this can be worth, stated before it is measured

`PROBE_LEDGER_CAP_CENTS = 100` is a cumulative **$1 across all runs**, and
`MENTION_FILL_CAP_CENTS = 97` against `max_order_notional_cents = 100` admits
exactly one contract per order (`src/risk.rs`). At +21¢ × 0.6 fills/event this
arm earns roughly **13¢ per event** and is capped at $1 for its lifetime.

**So this can never make money at current caps, and that is not the point.** Its
only value is as evidence for or against an M4 cap raise. The number that
argument would rest on is the **size-weighted** figure using the size actually
printed at each entry (TRAIN entries ranged 1–200 contracts, median 6), so §S8
is a required deliverable, not an extra.

---

## R8 — The load-bearing assumption, named

Every fill in this study is credited because **somebody else took that offer**.
The public trades tape cannot distinguish "an offer rested there and we could
have had it" from "we would have lost the race for it".

With entries landing 2–26 s after wave-1 this is plausible — it is a
resting-limit problem, not a sub-second race, which is the whole reason this arm
is interesting when the phone lane is 534 ms behind. But plausible is not
measured, and no Kalshi orderbook tape exists for any event: `book-tape.rs` is
built and tested but has **never recorded a live session**
(`state/xvenue/*/norm.jsonl.meta.json` shows both banked cross-venue events
backfilled their Kalshi side from the trades archive, `feed_kind: "archive"`).

`record-book.sh` + `analyze_book.py` exist to close this. **If the book shows no
resting size at ≤70¢ at the trigger instants, this arm is dead regardless of
what the holdout says**, and that finding outranks the P&L.

---

## R9 — What would make me abandon the arm

Stated now so it cannot be rationalised later:

- The book tape shows resting YES size at ≤70¢ at fewer than half the triggers.
- The sign flips anywhere in the §R5 multiplicity range.
- The edge survives only at min-printed-size 0 (§S5) — i.e. it is dust.
- HOLDOUT CI upper bound < 0 at n ≥ 20.
- The control (§R3, no wave-1 filter) stops being negative — the primitive would
  then be measuring something other than what it claims.

---

## Amendments

### A1 — 2026-08-07, the cancel rule is dropped. **Zero holdout events had settled.**

§R2 froze a cancel-on-recovery rule: pull the bid the moment a print returns to
wave-1's top, on the argument that the decisive wave has then arrived and we are
no longer buying *between* the waves. Running it on TRAIN falsifies that
argument.

| | fills | P(YES) | per-trade |
|---|---|---|---|
| trades the cancel **removes** | 10 | **8/10 = 80%** | **+21.8¢** |
| trades it **keeps** | 12 | 9/12 = 75% | +20.2¢ |

If the mechanism were real the removed trades would be the *bad* ones. They are
marginally the good ones, and for an obvious reason once seen: a market that
recovers to 99¢ is a market whose sweep was **right**, so cancelling there
discards precisely the winners. The cancel was not selecting for anything — it
was halving the sample.

**The primary specification becomes the simpler rule:** a qualifying wave-1,
rest a bid at 70¢ for 60 s, hold to settlement. No cancel logic. On TRAIN:
**+21.0¢/trade, 17/22, 10 events, 6 days.**

Why this is a legitimate amendment and not tuning:

- It is made **before any holdout event has settled**. TRAIN's purpose is to
  specify the rule; the sin is changing it after opening the holdout, and the
  holdout is sealed (`score_r2.py --split holdout` refuses).
- It **removes** a mechanism rather than adding one. The resulting rule has
  strictly fewer invented parts, and is the rule that was actually reported when
  this study was proposed.
- It is not the best-scoring choice. 15 s (+37.0¢) and 30 s (+38.1¢) both score
  higher **and clear their CI where 60 s does not**; the burst gap at 1.0 s
  (+29.9¢) likewise. **None of those are taken.** The 60 s window and the 2.0 s
  burst gap stand exactly as frozen.

**This is the last specification change.** Any further edit to §R2 burns the
holdout and restarts it from the date of the edit.

The superseded variant stays in the code (`cancel_on_recovery=True`) and is a
required §S2 report, so the falsification above can be re-derived rather than
taken on trust.

### A2 — 2026-08-07, TRAIN is not significant, and that is not a problem yet

Recorded so it cannot be quietly forgotten at close: on TRAIN the primary rule's
event-clustered 95% CI is **[−3.3, +47.9]¢** — it straddles zero. The Wilson
interval on P(YES) is [0.57, 0.90] against an average fill of 54¢, so the
*proportion* clears break-even while the *P&L* does not; P&L variance is
dominated by the ±100¢ settle and needs far more observations for the same
precision.

So the arm is **not statistically significant even on the data it was tuned on.**
That is expected at n=22 given §R4's power calculation (36 independent
observations needed), and it is exactly why the holdout closes on an event count
rather than on a result. It does mean **no interim glance at the holdout could
ever be conclusive**, which is the second reason not to take one.

### A4 — 2026-08-07, a series probe ran. **The forward holdout is untouched and still sealed.**

A one-shot generalization test was declared in `PRE-REGISTRATION-R2-PROBE.md`
and scored once over **798 previously unseen events across 244 non-earnings
mention series**. Result: **+16.2¢/trade, 277/394, 208 events, 172 days**,
event-clustered CI [+9.7, +22.3], series-clustered [+12.1, +25.4], against a
control of **−13.2¢ on 7,713 fills**. Verdict **GENERALIZES**
(`out/scoreboard-r2-probe.md`).

What this changes here, and what it does not:

- **§R3's characterisation of the arm is now too narrow.** TRAIN is 43 earnings
  events plus one WNT; the arm is not an earnings-call artifact.
- **§R4's power problem is answered on the series axis, not the time axis.** The
  worry that the forward holdout might close NOT-RESOLVED for want of sample is
  unchanged — 394 fills off-earnings says nothing about whether the edge persists
  *forward in time*, which is a different failure mode (regime change, competitor
  adaptation).
- **§R8 is unchanged and now carries more weight, not less.** All 394 probe fills
  rest on the same unverified assumption: that an offer rested and we would have
  had it. No order-book tape exists for any of the 798 events either.
- **§R2 is NOT amended.** The probe cannot re-tune the rule and did not.
- **The holdout remains sealed** under §R4. `score_r2.py --split holdout` still
  refuses.

One correction to the mechanism recorded in A3: the timing placebo is **+8.2¢**
on the probe set where it was −22.6¢ on TRAIN. Off earnings calls, selection
(having had a wave-1 to ≥95¢) carries roughly half the edge and timing the other
half. The arm still beats both placebos, but it is a weaker *timing* signal
across venues than TRAIN implied.

### A3 — 2026-08-07, the quant-audit added two placebo gates. **Holdout still sealed.**

`audit_r2.py` runs the five gates. Two were not in §R6 and are added here as
required reports at close, because they are the only checks that separate this
arm from "buy cheap mention markets":

- **Placebo / timing.** Hold the market set fixed, draw the trigger instant at
  random from [first qualifying wave-1, window end], apply the identical rule.
  TRAIN: **−22.6¢ [−49.0, +1.2]** against the arm's +21.0¢.
- **Placebo / selection.** Same rule on the markets in the same events that
  never had a qualifying wave-1. TRAIN: **−23.5¢ [−33.2, −14.0]**.

The draw range matters and got it wrong once: drawing from the *whole* call
window lets the placebo fire before the sweep — buying at 40¢ a market it
already knows will reprice to 99¢ — which scored +31.9¢ and falsely appeared to
beat the arm. **A placebo must know exactly what the arm knows.** Both versions
stay in the code.

Also recorded: the Gate-1 shift curve (+21.0 / +21.0 / +9.3 / −2.4 / −21.8¢ at
0/1/5/15/60 s of added delay) says the edge is short-lived, agreeing with §S2
that the frozen 60 s window is too slow. **The rule is not changed** — A1
declared itself final and the mechanism agreeing with the score makes re-tuning
more tempting, not more legitimate. Consequence, pre-committed: the frozen
window is **conservative**, so a holdout pass is strong evidence and a holdout
failure does not rule out the tighter variant, which would become round 3 on
fresh data.
