# Pre-registration — post-sweep edge study

**Frozen 2026-08-06, before any TEST-set number existed.** Everything below was
written while only TRAIN data had been looked at. Amendments are appended with a
date and a reason; nothing is deleted (house rule).

---

## P0 — The claim under test

When a mention market is aggressively repriced, is there money in what happens
*next*, without winning the audio race?

Three arms, scored independently:

| arm | trigger | we buy |
|---|---|---|
| **FOLLOW** | a sweep episode ends | YES, at ≤ cap |
| **FADE** | a sweep episode ends **below** `STALL_MAX` and has not recovered after `STALL_WAIT_S` | NO, at ≤ cap |
| **MISSED** | our own detector fired on a word and the market never swept | YES, at ≤ cap |

---

## P1 — Prior (TRAIN only, and why it cannot count as confirmation)

A provisional pass over 9 events motivated this lane: FOLLOW at ≤70¢ measured
**−27.5¢/contract** (n=33) and FADE **+15.4¢** (n=77), with `P(YES | swept)`
= 64% overall but 33% for sweeps terminating at 60–69¢ and 97% for those
reaching 90–100¢.

Those numbers were derived on the same data they were computed from, so they
are a hypothesis, not evidence. **They are also known to be partly wrong**, for
two reasons found while building the primitive:

1. **The window was inferred from trade density**, which put INTC's window at
   20:30Z and scored **pre-call** trades as sweeps. Real call times resolve
   INTC to 21:00Z (`lib/windows.py`).
2. **"Swept to X" and "resting at X" were conflated.** `lib/acceptance.py`
   shows that the markets the repo calls wrong-way "basket sweeps" — AAL
   WORL/CENT/IRAN, INTC ARIZ/DESK/GOVE/MOBI — were already priced 55–97¢
   *before the call* and never repriced during it. CHIC was at 97¢ twelve
   minutes before its supposed 12:34:39Z "sweep". The provisional FADE number
   was therefore substantially measuring a **price-level** effect, not a
   **sweep** effect.

Because of (2), the level effect is promoted to its own arm rather than left
inside FADE:

| arm | trigger | we buy |
|---|---|---|
| **LEVEL** | a market is *resting* at price P during the call, however it got there | NO, at ≤ cap |

FADE and LEVEL are scored separately and their overlap is reported.

---

## P2 — The sweep primitive

Frozen in `lib/sweep_segment.py`. A **sweep episode** is a burst of taker-YES
prints where:

```
burst        consecutive taker-YES prints, inter-print gap < BURST_GAP_S
terminal     the burst's highest price      (where the sweep STOPPED)
entry        the resting price BASELINE_LOOKBACK_S before onset
             (fallback: nearest earlier print; then the burst's own first price)
jump         terminal - entry
qualifies    terminal >= MIN_TERMINAL and jump >= MIN_JUMP
             and (contracts >= MIN_CONTRACTS or n_prints >= MIN_PRINTS)
merge        legs within EPISODE_MERGE_S of the previous leg's end are ONE episode
```

| parameter | value | why |
|---|---|---|
| `BURST_GAP_S` | 2.0 | COMPETITOR-ANATOMY: wave-2 at +120–280 ms, wave-3 at ~+1.0–1.6 s |
| `MIN_TERMINAL` | 50 | below this the market made no directional claim |
| `MIN_JUMP` | 15 | a sweep must MOVE the price, not print at a level |
| `MIN_CONTRACTS` | 25.0 | measured: in-call bursts carry 18–914 ct, out-of-window 1–22 |
| `MIN_PRINTS` | 3 | a small multi-print walk still qualifies |
| `BASELINE_LOOKBACK_S` | 60 | a repricing arrives in legs; measuring each leg against the previous makes every leg look small |
| `EPISODE_MERGE_S` | 120 | **statistical, not cosmetic** — unmerged legs fire the arm several times on one correlated event and overstate every CI |
| `BASKET_WINDOW_S` | 2.0 | strikes sweeping this close = one release-text basket |

**Acceptance (already passed, `lib/acceptance.py`):** reproduces **7/7**
independently documented onsets to the microsecond — MS INVE/TAIL/RECE
(COMPETITOR-ANATOMY wave-1 table) and WNT IRAQ/BLAN/MAIN/MIDT (VERIFICATIONS
Q-WNT-CLOSE) — and correctly refuses to fire at the quoted times for **6/6**
pre-priced markets.

---

## P3 — Windows

Call times resolve from `slate/call-times.json`, then
`pm-expansion-exploration/call-times.json`, then a `configs/*.toml`
`starts_at` that is **not** the seeded placeholder `2026-07-15T00:00:00Z`.
Coverage is 47/51 events. The window is call − 45 min … call + 150 min.

Events whose call time does not resolve are **excluded from the primary run**
and reported as excluded. A window-free variant is a sensitivity check (§S2),
never the headline.

---

## P4 — Fills

Frozen in `lib/fill_model.py`. **A fill is credited only where a same-side
taker print at our price or better exists in the window.** A taker-YES print at
P proves a liftable YES offer at P; a taker-NO print at yes-price P proves NO
was buyable at (100−P). Resting size nobody hit is invisible, so this
understates the opportunity — the correct direction for an error to point.

Size is **1 contract**, not a free parameter: `MENTION_FILL_CAP_CENTS = 97`
against `max_order_notional_cents = 100` admits exactly one contract per order,
and `PROBE_LEDGER_CAP_CENTS = 100` is a cumulative $1 across all runs
(`src/risk.rs`).

Fees mirror `src/kalshi/executor.rs::fee_cents`, verified 300/300 against the
compiled Rust. The executor's `ev_gate` is applied, so an arm cannot claim a
trade the live system would refuse.

Every hypothetical trade is classified and none dropped:
`filled | no-offer | ev-gate-refused | unsettled`.

---

## P5 — Arm parameters

| parameter | value |
|---|---|
| `FOLLOW_CAP` | 70¢ (the idea as posed); also scored at the sweep terminal |
| `FOLLOW_WINDOW_S` | 30 |
| `STALL_MAX` | 89¢ — a sweep terminating at/below this is "stalled" |
| `STALL_WAIT_S` | 120 — wait this long before fading; if the market has reached ≥95¢ by then, **no trade** |
| `FADE_CAP` | 60¢ (a NO price) |
| `FADE_WINDOW_S` | 1800 |
| `LEVEL_BAND` | resting price 50–89¢ at call start |
| `LEVEL_CAP` | 60¢ (a NO price) |
| `LEVEL_WINDOW_S` | 3600 from call start |
| `MISSED_CAP` | 40¢ |
| `MISSED_WINDOW_S` | 3600 |

---

## P6 — TRAIN / TEST split

**TRAIN** (the 9 events the prior was derived on — can never confirm anything):
AAL 26JUL23, ALLY 26JUL21, AMD 26AUG04, AXP 26JUL24, GEV 26JUL22,
GOOGL 26JUL22, INTC 26JUL23, LMT 26JUL23, TSLA 26JUL22.

**TEST**: every other settled mention event. Scored **once**, after TRAIN is
finished and this file is frozen. If TEST is re-run after seeing its result,
that is recorded here as an amendment and the result is downgraded.

---

## P7 — Decision bars

An arm is **PROMOTABLE** only if, on TEST:

- per-trade EV after fees **≥ +5¢**, and
- bootstrap 95% CI lower bound **> 0** (10,000 resamples, resampled by EVENT
  not by trade — trades within an event are correlated), and
- **n ≥ 40** filled trades, spanning **≥ 8 events** and **≥ 4 distinct days**, and
- it survives leave-one-event-out (no single event flips the sign), and
- the shuffled-label null scores ≈0.

**REFUTED** if the TEST CI upper bound < 0.

Otherwise **NOT-RESOLVED** — and NOT-RESOLVED is a real, reportable outcome, not
a failure to be argued around. Verdict vocabulary follows
`pm-expansion-exploration/latency/mention-market-exploitability-scoreboard.md`.

---

## P8 — Sensitivity and robustness (all pre-declared)

- **S1 — Method sanity.** The 90–100¢ terminal bucket must price at ≈0 in both
  directions. If an arm shows edge *there*, the method is broken, not the
  market. This is a stop condition, not a footnote.
- **S2 — Window-free variant.** All arms re-scored with no call window, using
  the size floor alone. Reported alongside, never instead.
- **S3 — Parameter grid.** `MIN_TERMINAL` ∈ {40,50,60}, `MIN_JUMP` ∈ {10,15,25},
  `MIN_CONTRACTS` ∈ {10,25,50}, `STALL_WAIT_S` ∈ {60,120,300}. An arm that
  survives only at one cell is NOT-RESOLVED, not promotable.
- **S4 — Basket discriminator.** Report P(YES) split by `basket_n` = 1 vs ≥2.
  Pre-registered as exploratory: it may not be used to promote an arm in this
  round, only to generate a hypothesis for the next.
- **S5 — Settlement lag.** Report hold time from entry to settlement. Capital is
  tied up for hours (AAL swept 12:33Z, settled 17:08Z); an EV figure that
  ignores this overstates capital efficiency.
- **S6 — Overlap.** Report how often FADE and LEVEL fire on the same market, so
  their edges are not double-counted.

---

## P9 — What would make me abandon each arm

Stated now so it cannot be rationalised later:

- **FOLLOW** — abandon if TEST per-trade EV is negative with a CI upper bound
  below 0. The prior already expects this; the deliverable is a clean negative.
- **FADE** — abandon if the edge lives only in the 60–69¢ bucket **and** that
  bucket has fewer than 15 TEST trades, or if it fails S1.
- **LEVEL** — abandon if it is indistinguishable from FADE after S6 (i.e. the
  same trades wearing two names).
- **MISSED** — abandon if fewer than 10 TEST instances exist; the channel is
  settlement-confirmed only 3× to date (NASA, RECO, SIKO) and may simply be too
  rare to trade.

---

## Amendments

**A1 — 2026-08-06, after TEST was scored, from the `quant-audit` pass.**
Recorded, not folded silently into the rules above.

1. **§P8 S3 has a gap.** The grid covers `min_terminal`, `min_jump`,
   `min_contracts` and `STALL_WAIT_S` but **not `EPISODE_MERGE_S`**, which turns
   out to drive the FOLLOW result (+10.9¢ unmerged → −28.3¢ at 300 s; the sign
   flips). No cell is significant, so no verdict changes, but the −22.5¢ figure
   is weaker than it looks. `merge_s` belongs in the grid next round.

2. **§P8's shuffled-label null is invalid and was replaced.** For a single-side
   arm, `sum_pnl = 100·(#wins) − Σprice − Σfee` depends only on the *count* of
   wins, so permuting a fixed label multiset is constant — zero variance,
   observed on all three arms. Replaced with an efficient-market null
   (`won ~ Bernoulli(price/100)`, under which a trade pays exactly −fee). The
   replacement was designed after seeing that the original was degenerate, which
   is a researcher degree of freedom; it is disclosed here rather than presented
   as pre-registered.

3. **§P7's bars were written without a power analysis, and the study is
   underpowered for all of them.** Minimum detectable effect (80% power):
   FOLLOW ±34–48¢, FADE ±17–30¢, LEVEL ±6–16¢. The n≥40 / ≥8 events / ≥4 days
   bars are far too weak to detect the ≥+5¢ EV they gate on — roughly 500–600
   *independent* trades would be needed, and independence is at the event level.
   **Consequence: no arm may be reported as REFUTED on EV grounds.** The
   verdicts stand as NOT-RESOLVED. A future round must set n from a power
   calculation, not from a round number.

4. **§P8 S1 and S4 could not be run as designed.** S1's 90–100¢ bucket holds
   n=2 FOLLOW / n=0 FADE trades — precisely because that is where no residual
   exists — so it is uninformative rather than passed. S4 found zero baskets in
   TEST, so the discriminator is untested, not refuted.

Net effect: the EV verdicts are weaker than the first draft of the scoreboard
claimed, and the *proportion*-based findings (94.8% no-residual; the 21% vs 68%
adverse-selection gap, whose CIs do not overlap) are the study's durable
results. `out/scoreboard.md` was rewritten accordingly.

---

**A2 — 2026-08-07. Every arm here trades the END of a repricing. None trades the
middle, and the middle is a different result.**

§P0's three arms all fire once a repricing is over. That is forced by
`EPISODE_MERGE_S = 120` (§P2): merging exists so that one repricing arriving as
four legs fires an arm once rather than four times, which is correct — but it
also means FOLLOW's "residual" is what is left after the **last** wave, never
what is available between the first and the last.

Entry *during* a repricing was therefore never in scope, never scored, and is
not addressed by any number in this study or in `out/scoreboard.md`.

A1.1 above was the first sign and this file misread it. Unmerged, FOLLOW scored
**+10.9¢** — the most favourable cell anywhere in round 1 — and the scoreboard
called it a specification artifact on the grounds that "unmerged, the arm is no
longer buying leftovers — it is joining a sweep already in progress." That
sentence is a correct description of a **different trade**, not a reason to
discard it.

Measured properly (strict causality: a wave qualifies on its own prints alone,
`lib/wave_ladder.py`), resting a bid at 70¢ for 60 s after a wave-1 that reached
≥95¢ pays **+21.0¢/trade, 17/22, 10 events, 6 days** on the same tapes, against
**−6.4¢, 46/95** for the identical machinery with no wave-1 filter. Round 1's
own FOLLOW arm pays −22.5¢.

Nothing in round 1 is retracted. The scope of the word "sweep" in its headline
was too broad, and `out/scoreboard.md` now carries a correction to that effect.
The new question is round 2: `PRE-REGISTRATION-R2.md`, `out/scoreboard-r2.md`.
