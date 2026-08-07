# Round 2 — the intra-sweep arm — scoreboard

**2026-08-07.** Offline, no orders placed. Rules in `../PRE-REGISTRATION-R2.md`,
frozen before any holdout event settled.

> ## The holdout is SEALED. Nothing here is a result.
>
> Every number below is TRAIN — all 39 events banked through 2026-08-06, every
> one of which was inspected before the rule was written. TRAIN's job is to
> **specify** the arm; it cannot confirm it. `score_r2.py --split holdout`
> refuses to run until 36 forward events have settled or 2026-10-30, whichever
> comes first (§R4). That refusal is in code, not in discipline.
>
> Read this document as "here is the hypothesis and here is how sharp it is",
> never as "here is an edge".

---

## What round 1 missed, in one line

Round 1's arms all buy the residual **after** a repricing has finished, because
`EPISODE_MERGE_S = 120` glues the waves into one episode. This arm buys
**inside** it. Same tapes, same fill model, same fee mirror — opposite sign:

| entry point | per-trade | n |
|---|---|---|
| round 1 FOLLOW — after the last wave, merged | **−22.5¢** | 14 |
| unmerged, still firing at the episode end | +10.9¢ | 46 |
| **this arm — between the waves** | **+21.0¢** | 22 |
| control: same machinery, no wave-1 filter | **−6.4¢** | 95 |

The control is the load-bearing row. It is round 1's population run through
round 2's code, and it stays negative — so the wave-1 ≥95¢ filter is doing the
work, not the rewrite.

---

## Established by measurement (not contingent on the arm being profitable)

These are facts about market structure. They would still be true if the holdout
kills the arm, and they are what actually answers "is there a gap to step into".

| | |
|---|---|
| ≥95¢ repricings that arrive in **more than one wave** | **69%** (134/194) |
| gap from wave-1 to the next print | median **5.7 s**, min 2.0 s, p75 10.7 s, max 77 s |
| fill delay after wave-1's last print | median **14.9 s**, min **2.04 s**, max 56 s |
| fills that would have needed us to win a race (<534 ms) | **0 of 22** |

**This is not a latency trade.** The phone lane's measured decode deficit is
534 ms and a fillable price needs ≤194 ms — `chiavellini.md` §8 concludes "no
detector-only path reaches a fillable price". Every fill here is 2–56 seconds
after the competitor's wave. A resting limit order gets them; none requires
beating anyone. That is the whole reason this arm is worth a holdout: it is
**orthogonal to the ladder that gates everything else.**

What it is *not* is riskless. 5 of 22 lost 32–64¢, and the round trip was
modelled properly (`lib/exit_model.py`: a taker-NO print at yes-price P proves a
resting buy-YES bid at P, so we could have sold at P). Selling into the decisive
wave at 97¢ caps the trade at ~+30¢ where holding pays more, so the rule holds
to settlement and carries the settlement risk. Adverse excursion: 13 of 22 never
marked against us at all, but 3 of the 17 winners were ≥20¢ underwater on the
way and the worst touched **41¢** down.

---

## The arm on TRAIN (contaminated — specifies, cannot confirm)

| | triggers | fills | YES | Wilson P(YES) | avg buy | per-trade | 95% CI | events | days |
|---|---|---|---|---|---|---|---|---|---|
| **frozen rule** | 361 | 22 | 17/22 | [0.57, 0.90] | 54¢ | **+21.0¢** | [−3.3, +47.9] | 10 | 6 |
| control (no wave-1 filter) | 598 | 95 | 46/95 | [0.39, 0.58] | 53¢ | −6.4¢ | [−26.1, +15.5] | 24 | 9 |

- **The proportion clears break-even; the P&L does not clear zero.** Buying at
  54¢ needs P(YES) > 0.54 and the Wilson lower bound is 0.57 — but the
  event-clustered P&L CI straddles zero, because P&L variance is dominated by
  the ±100¢ settle. Both are true; quoting only the first would be misleading.
  Recorded as amendment A2 before the holdout opened.
- **Efficient-market null: p = 0.020, BEATS the null.** Drawing
  `won ~ Bernoulli(price/100)` gives −1.9¢ [−20.0, +16.4] against a realized
  +21.0¢. This is the one in-sample test the arm passes outright — and it is
  still in-sample.
- **LOEO: 0/10 sign flips**, range [+17.5¢ dropping AMD-26AUG04, +34.6¢ dropping
  GOOGL-26JUL22]. No single event carries it.
- **Fires rarely.** 22 fills from 361 triggers — a 6% fill rate, ~0.6 per event.

---

## Sensitivity (§R6)

**S1 — the two constants that were chosen while their scores were visible.**
Promotion requires the sign to hold across both ranges. On TRAIN it does:
**12 of 12 cells positive**, +1.1¢ to +37.0¢. Only the two tightest cells clear
their CI. Monotone in cap (cheaper is better) and peaked at wave-1 = 95¢.

**S2 — cancel policy, the largest known lever.** Reported in full because the
frozen value is *not* the best:

| variant | fills | YES | per-trade | 95% CI |
|---|---|---|---|---|
| 15 s window | 12 | 11/12 | +37.0¢ | **[+22.2, +55.0]** |
| 30 s window | 18 | 17/18 | **+38.1¢** | **[+25.9, +50.0]** |
| **60 s — FROZEN** | 22 | 17/22 | +21.0¢ | [−3.3, +47.9] |
| 120 s | 25 | 17/25 | +12.6¢ | [−13.1, +43.4] |
| 300 s | 28 | 17/28 | +7.4¢ | [−16.2, +33.6] |
| recovery-cancel (superseded, A1) | 12 | 9/12 | +20.2¢ | [−6.1, +49.4] |

**15 s and 30 s score nearly double the frozen rule and clear their CI where it
does not. They are not taken.** 60 s was chosen before any of this precisely
because it was not the best cell, and re-choosing on score after the fact is the
failure mode the pre-registration exists to prevent.

There is a mechanism behind it, which makes it *more* tempting and therefore
worth naming explicitly: **four of the five losers are the four latest fills**
(43.6 s, 51.0 s, 51.7 s, 56.1 s). A give-back that has not arrived within ~30 s
is not a give-back — it is the market genuinely repricing down because the sweep
was wrong. Plausible, invented after seeing the data, n=5. **Round-3 hypothesis,
not a result.**

**S4 — burst gap.** +23.9¢ / +21.0¢ / +11.8¢ at 1.0 / 2.0 / 4.0 s. Sign holds;
tighter is better. 2.0 s is inherited from round 1 unchanged and not re-tuned.

**S5 — is the edge dust?** No. It survives a 25-contract minimum printed size:
+23.6¢, 9/11. §R9's abandon-if-dust condition is **not** triggered.

**S6 — window-free.** +24.5¢, 20/25, CI [+0.3, +48.0]. Same conclusion without
call windows.

**S7 — adverse excursion.** Reported above. This is what keeps "riskless" out of
the vocabulary.

**S8 — what it could be worth.** At 1 contract per fill the entire TRAIN set
earns **$4.61 over 22 fills**, against a lane capped at $1 for its lifetime
(`PROBE_LEDGER_CAP_CENTS = 100`). At full printed size the ceiling is
**$480.95 over 1,636 contracts** — and that assumes we displace the actual taker
entirely, an assumption stacked on top of the one in §R8. **This arm cannot make
money at current caps. Its only value is as evidence for or against an M4 cap
raise**, and the second number is the one that argument would rest on.

---

## quant-audit — five gates, run on TRAIN (`../audit_r2.py`, `r2-audit.txt`)

**All five pass.** Which is also what a well-tuned overfit does on the data it
was tuned on — that is why the holdout is sealed, not why it is optional.

**Gate 3a — placebo, timing.** The framework's permutation test has no direct
analogue on binary settles, so the equivalent is: keep the markets the arm
selected and destroy **only the timing** — fire at a random instant drawn from
[wave-1, window end] instead of at wave-1.

| | per-trade | 95% |
|---|---|---|
| random instant after wave-1, same markets | **−22.6¢** | [−49.0, +1.2] |
| **the arm** | **+21.0¢** | above the placebo's upper bound |

Acting *at* wave-1 beats acting later on the identical market set. **The timing
is the signal**, and this is the check round 1 never ran.

**Gate 3b — placebo, selection.** Same rule on the 338 markets in the same
events that never had a qualifying wave-1: **−23.5¢ [−33.2, −14.0]**. So wave-1
is also selecting genuinely better markets, not merely cheap ones.

**Gate 1 — shift.** Delay every decision and watch it decay:

| offset | fills | YES | per-trade |
|---|---|---|---|
| 0 s (frozen) | 22 | 17/22 | **+21.0¢** |
| 1 s | 22 | 17/22 | +21.0¢ |
| 5 s | 18 | 12/18 | +9.3¢ |
| 15 s | 13 | 7/13 | −2.4¢ |
| 60 s | 9 | 3/9 | −21.8¢ |

A one-second shift changes nothing, so the arm is not reading the decision
instant itself — the classic lookahead signature is absent. Past that it decays
monotonically to negative, which is what a real, short-lived give-back looks
like.

**Gate 4 — cost.** +19.0¢ at **2× fees** (the hard gate), +17.0¢ at 3×. Fees are
tiny here (1–2¢), so the multiplier is standing in for slippage and for the
capital cost of holding to settlement, which nothing in this study models.

**Gate 4 — stability.** Positive on **5 of 6 days**, above the 60% bar. The
exception is 26JUL22 (GOOGL/TSLA/GEV) at −40.5¢ over 4 fills.

**Gate 5 — holdout.** Forward-only and refused in code, both without and with
`--close-the-holdout` while the condition is unmet.

### A fifth method error, found by this audit

The first version of Gate 3a drew the random instant from the **whole call
window**, which let the placebo fire *before* the market ever swept — buying at
40¢ a market it already knew would reprice to 99¢. It scored **+31.9¢** and
appeared to beat the arm, which reads as "wave-1 adds nothing" when it actually
reads "an oracle beats us". A placebo has to know exactly what the arm knows.
Both versions are kept in `audit_r2.py` so the error stays inspectable, joining
the four in round 1's scoreboard.

### The tension this audit creates, stated rather than resolved

Gate 1's decay curve and §S2's window sweep point the same way: **early matters
a lot, and the frozen 60 s window is too slow.** 15 s and 30 s score nearly
double and clear their CI. That the mechanism and the score now agree makes
re-tuning *more* tempting, not more legitimate, and amendment A1 already
declared itself the last specification change.

So the rule holds, with one consequence recorded now so it is not discovered at
close: **the frozen window is conservative.** A holdout pass at 60 s is strong
evidence; a holdout *failure* at 60 s does not rule out the tighter variant, and
would put it — pre-registered, on fresh data — as round 3.

---

## The assumption that outranks all of the above

Every fill here is credited because **somebody else took that offer**. The
public trades tape cannot distinguish "an offer rested at 68¢ for nine seconds"
from "one offer existed for 300 µs and we would have lost the race". Both print
identically.

`record-book.sh` + `analyze_book.py` close this, and both now work end to end:

- **The recorder ran for the first time, 2026-08-07.** `examples/book-tape.rs`
  had never recorded a live session — both banked cross-venue events backfilled
  their Kalshi side from the trades archive
  (`state/xvenue/*/norm.jsonl.meta.json`, `feed_kind: "archive"`). A 75-second
  smoke run against four live `KXWTIH` markets produced **119 frames, 0 dropped,
  clean `close` row**, clock offset 0.354 s.
- **The analyzer reconstructs a real ladder from it.** On one market: 63 book
  updates, resting size 50–300 contracts, liftable for 74.4 s of the 75 s tape.
- **The side conversion is the whole trick and is regression-tested.** To buy
  YES at ≤70¢ we need a resting YES *ask* at ≤70¢, and Kalshi's wire has no such
  leg — a **NO bid at ≥30¢ IS a YES ask at ≤70¢**. Reading `yes_dollars_fp`
  would measure YES *bids*, answer the wrong question, and look right doing it.

**No book tape exists for any mention event yet**, so §R8 is still open, and
§R9 makes it a kill switch: if fewer than half of triggers had resting size at
≤70¢, the arm is dead regardless of what the holdout P&L says. That measurement
outranks the P&L and should be taken on the next armed event.

---

## Verdict

**NOT-RESOLVED, and not resolvable from banked data.** The arm is a sharp,
falsifiable hypothesis with a sealed forward test and a named kill switch. It is
not an edge until the holdout closes.

What would change it, in order of decisiveness:

1. **A book tape on one armed event** — decides §R8, and could kill the arm
   outright without waiting for the holdout.
2. **36 forward events** (~7 weeks at 5/week, ~22 fills) — closes the holdout.
3. Neither is a re-analysis. There is nothing left to extract from these tapes.

## Reproduce

```
for m in windows sweep_segment fill_model data arms wave_ladder exit_model intra_arm; do
    python3 lib/$m.py; done
python3 lib/acceptance.py
python3 analyze_book.py --self-test
python3 score_r2.py --replicate --sensitivity     # this file's numbers -> out/r2-train.txt
python3 score_r2.py --split holdout               # must REFUSE
```
