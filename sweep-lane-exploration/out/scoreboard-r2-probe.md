# Series-generalization probe — scoreboard

**2026-08-07.** Scored **once**, per `../PRE-REGISTRATION-R2-PROBE.md`. Offline,
public no-auth data, no orders. 798 events across 244 declared series, none of
which had ever been looked at.

---

## Verdict: **GENERALIZES**

> **The intra-sweep edge is not an earnings-call artifact.** It holds across 39
> mention series and 208 events — World Cup football, MLB, congressional
> hearings, Trump speeches, fight nights, Love Island — none of which the study
> had touched, and none of which has an audio detector pointed at it.

| | triggers | fills | YES | Wilson P(YES) | avg buy | per-trade | 95% CI | events | days |
|---|---|---|---|---|---|---|---|---|---|
| **probe — frozen rule** | 8,807 | **394** | 277/394 | **[0.66, 0.75]** | 52¢ | **+16.2¢** | **[+9.7, +22.3]** | 208 | 172 |
| control — no wave-1 filter | 29,247 | 7,713 | 3260/7713 | [0.41, 0.43] | 54¢ | **−13.2¢** | [−16.4, −10.1] | 641 | 399 |
| *TRAIN baseline (window-free)* | — | 25 | 20/25 | — | 54¢ | *+24.5¢* | *[+0.3, +48.0]* | 11 | — |

Every §P3 condition is met: EV > 0, CI lower bound > 0, n ≥ 20 fills over ≥ 10
events, and the filter separates from its own control by **+29.5¢**.

**The control is the most valuable number here.** It is round 1's population —
buy the cheap print after *any* wave ≥ 50¢ — run through round 2's code on 7,713
fills, and it is **−13.2¢ with a CI nowhere near zero**. Round 1's
adverse-selection finding was measured on 95 fills; it now has 80× the data and
survives. Buying cheap after a sweep loses money. Buying cheap after a sweep
*that reached the top of the ladder* does not.

**Power is no longer the binding constraint.** Round 2's whole problem was n=22
and a CI straddling zero. This is n=394 across 208 events and 172 distinct days,
and both the event-clustered and the stricter series-clustered intervals clear
zero:

| clustering | interval | groups |
|---|---|---|
| by event (the declared §P3 bar) | [+9.7, +22.3] | 208 |
| by series (stricter, note N1) | **[+12.1, +25.4]** | 39 |

The series-clustered interval is the one that matters for a *generalization*
claim, because 141 of the 394 fills are World Cup. It clears zero comfortably.

---

## The placebos, and the one real complication

| | per-trade | 95% |
|---|---|---|
| **arm** | **+16.2¢** | [+9.7, +22.3] |
| placebo — random instant after wave-1, same markets | **+8.2¢** | [+3.3, +13.2] |
| placebo — same rule on markets that never triggered | **−24.8¢** | [−27.0, −22.5] |

**The timing placebo is positive here, and it was −22.6¢ on TRAIN.** That is a
genuine difference and it changes the mechanism story:

- **Selection carries about half the edge.** A market that had a wave-1 to ≥95¢
  is a market likely to settle YES, and buying it cheap at *any* later point
  pays +8.2¢. Against markets that never triggered (−24.8¢) the trigger is
  clearly selecting, by ~33¢.
- **Timing carries the other half.** Acting at wave-1 rather than at a random
  later instant is worth **+8.0¢**, and the arm sits above the timing placebo's
  upper bound.

On earnings calls timing was everything; across venues it is roughly half. Both
components are real, but the arm is a weaker *timing* signal off earnings than
round 2 implied. Reported, not smoothed over.

---

## By series — exploratory (§P3: may generate a hypothesis, may not promote)

39 series produced fills. **14 of the 15 series with ≥5 fills are positive**, and
7 of the 8 with ≥10.

| series | fills | YES | per-trade |
|---|---|---|---|
| KXWCMENTION (World Cup) | 141 | 95/141 | +10.5¢ |
| KXTRUMPMENTION | 56 | 37/56 | +11.2¢ |
| KXMLBMENTION | 23 | 20/23 | **+33.3¢** |
| KXFIGHTMENTION | 21 | 16/21 | +14.7¢ |
| KXNBAMENTION | 21 | 13/21 | +9.5¢ |
| KXHEARINGMENTION | 19 | 14/19 | +24.0¢ |
| KXTRUMPMENTIONB | 16 | 8/16 | **−5.6¢** |
| KXLOVEISLMENTION | 11 | 8/11 | +19.8¢ |
| KXMAMDANIMENTION | 9 | 8/9 | +34.0¢ |
| KXVANCEMENTION | 8 | 7/8 | +37.9¢ |

The 7 negative series hold 25 fills between them and 6 of the 7 have n ≤ 2.

**Concentration is the caveat**: World Cup is 36% of fills and the top three are
56%. The series-clustered CI is the answer to that and it holds.

**Worth flagging for the audio program**: MLB is +33.3¢ on 20/23, and hearings
+24.0¢. Those are venues where the *audio* lane was parked or failed — MLB/WNBA
pack-lag showed the market reacting late, and the cross-venue audio findings did
not generalize. This arm does not listen, so a venue being unlistenable does not
disqualify it. That is a hypothesis for round 3, not a result.

---

## What this does NOT establish

1. **It is not the sacred holdout.** These events are TRAIN-date-eligible:
   unseen, but not future. `PRE-REGISTRATION-R2.md` §R4's forward holdout is
   untouched and still sealed, and this changes nothing about it.
2. **§R8 is unresolved for all 798 events.** Every fill is credited because
   *somebody else* took that offer. No Kalshi order-book tape exists for any
   event here, so "an offer rested and we would have had it" is still an
   assumption — now an assumption carrying 394 fills instead of 22. §R9 makes it
   a kill switch regardless of this result.
3. **It says nothing about live detection.** Every trigger was found by
   replaying a finished tape. Detecting wave-1 in real time across hundreds of
   boards is a separate engineering problem this probe does not touch — though
   the round-2 shift curve says the bar is ~1 second, not milliseconds.
4. **It cannot re-tune the rule**, and it cannot be re-run. §P4.
5. **The money is still capped at $1.** `PROBE_LEDGER_CAP_CENTS = 100` is a
   lifetime cap on the lane. 394 fills × 16.2¢ is $63.83 at one contract each —
   unreachable today. What the probe buys is evidence, not P&L.

### Defect D1 — the ordering proof for this run is half-broken

The population was declared before any fetch, but I destroyed my own artifact:
appending notes N1/N2 to the pre-registration at 17:05:10Z, while the fetch ran,
reset its mtime. See D1 in `../PRE-REGISTRATION-R2-PROBE.md`.

| link | status |
|---|---|
| roster → tapes | **VERIFIED** — all 798 tapes written after `generated_utc` 16:56:47Z |
| pre-registration → roster | **NOT VERIFIABLE** from the filesystem |

So *no event entered the population because of anything seen in its own tape* is
provable. *This document predated the population* is true and in the session
record, but not independently checkable. Fixed for future runs: the roster now
stamps the pre-registration's SHA-256 at enumeration time.

---

## Method notes

Three defects were found and fixed **before** the shot was spent — each verified
by exact reproduction of prior numbers, never assumed:

1. **A quadratic in `_baseline`.** It rebuilt two filtered lists over the whole
   tape on every call, once per burst — O(prints × bursts) per market. Invisible
   at TRAIN's tape sizes; 83% of runtime on a 16,344-print World Cup event, and
   ~54 min for the probe. Replaced with a bisect in `wave_ladder._baseline_fast`,
   **fuzz-tested against the original over 7,200 randomised cases**. 19× faster.
2. **Placebos rescanned every tape on every draw.** Now compact parallel arrays
   plus bisect; reproduces the TRAIN placebos to the digit.
3. **Probe and control were two full passes over 2 GB**, with all 798 parsed
   tapes held in RAM. The `min_top=95` set is exactly the `min_top=50` set
   filtered to `top ≥ 95`, so it is one pass with the cache off.

The scoring run was killed twice mid-flight for these. Neither run wrote
`probe-result.json`, so no number had been seen and the one shot stayed unspent —
which is the only reason the fixes were legitimate.

## Reproduce

```
python3 probe_series.py --self-test        # population filter, incl. the census trap
python3 probe_series.py --list             # 244 series -> 798 eligible events
python3 probe_series.py --fetch            # ~2.0 GB, public no-auth, resumable
python3 probe_series.py --verify-ordering  # both links, honestly reported
python3 probe_series.py --score --smoke    # plumbing check on TRAIN
python3 probe_series.py --score            # THE ONE SHOT (already spent)
```
