# Pre-registration — series-generalization probe

**Written 2026-08-07T16:52:36Z, BEFORE a single probe event was fetched.**

> ### ⚠ D1 — the ordering artifact for THIS run was destroyed by my own amendment
>
> The plan was: this file's mtime must precede every probe tape's. At
> **17:05:10Z**, while the fetch was running, I appended notes N1 and N2 —
> which §Notes explicitly permits — and that **rewrote the file and reset its
> mtime**, erasing the evidence it was supposed to be. An mtime is not a proof
> of authorship time for a document that is allowed to change.
>
> What survives, and what does not:
>
> | link | evidence | status |
> |---|---|---|
> | roster → tapes | roster's immutable `generated_utc` = **16:56:47Z**, earliest probe tape **16:57:46Z** | **VERIFIED** for all 689 tapes |
> | pre-registration → roster | this file's mtime | **DESTROYED**; rests on the session record only |
>
> So: no probe event entered the population because of anything seen in its own
> tape — that half is provable. That this document predated the population is
> *true* and recorded in the session log, but is **not independently checkable
> from the filesystem for this run**, and no claim to the contrary may be made.
>
> **Fixed for future runs:** `probe_series.py::prereg_witness()` now stamps this
> file's SHA-256 and mtime *into the roster* at enumeration time. A content hash
> in an immutable field survives amendment; an mtime does not.
>
> Same root cause broke the fetch: the original `--fetch` guard refused whenever
> this file was newer than the roster, so a legitimate dated note silently
> blocked a resume and it banked **zero** events while reporting success. The
> guard now reports a content change instead of blocking.

---

## P0 — The question

Round 2's intra-sweep arm measures **+21.0¢/trade** on TRAIN and passes all five
`quant-audit` gates. But **43 of its 44 events are earnings calls.** So the
result may be a property of *mention markets* or merely of *earnings calls*, and
nothing measured so far can tell those apart.

> **Is the intra-sweep edge a property of mention-market microstructure, or an
> artifact of earnings calls?**

This is answerable today and does not require the sealed forward holdout: every
past mention event is settled, and Kalshi's public `/markets/trades` is
historical. Round 1 only ever fetched from `state/` run dirs — events we armed,
all earnings plus one WNT — so **not one trade from any other series has ever
been looked at.**

The arm makes this test possible at all because **it uses no audio.** It triggers
on a competitor's sweep read from market data: no detector, no dial-in, no
webcast, no call time. It therefore applies to every mention board Kalshi lists,
not only to the ~5 events/week we can physically cover.

---

## P1 — Population, by ticker prefix

From `slate/census.json` (generated 2026-07-30, 398 series): every series whose
ticker does **not** start with `KXEARNINGSMENTION`, minus `KXWORLDNEWSMENTION`
(its 26JUL29 event is in TRAIN).

**244 series, 732 sampled events.**

⚠ **Filter on the ticker prefix. Never on the census `class` field.** 102 of the
153 `KXEARNINGSMENTION*` series carry a class other than `earnings-call` —
`KXEARNINGSMENTIONCRCL`, `...RY`, `...MCO` are all tagged `unclassified`. A
class-based filter would silently readmit earnings events and convert a
generalization test into a partial replication. `probe_series.py`'s self-test
asserts this exact case is excluded.

An event enters the scored set iff it has **≥1 market with `result ∈ {yes,no}`**
and **≥1 trade**. An event with no trades produces no triggers by construction,
so skipping it is a no-op, not a selection decision.

Every event enumerated is reported with its disposition — `scored`,
`no-settlement`, `no-trades`, `fetch-failed` — so nothing is silently dropped.

---

## P2 — The rule: frozen §R2 as amended by A1, unchanged, window-free

```
wave-1     consecutive taker-YES prints, inter-print gap < 2.0s
qualifies  top(wave-1) >= 95c
           AND top(wave-1) - resting_price(60s before onset) >= 15c
           AND (contracts(wave-1) >= 25 OR n_prints(wave-1) >= 3)
action     rest a buy-YES bid at 70c, size 1, for 60s
exit       hold to settlement
window     NONE
```

Not one constant is changed. `lib/wave_ladder.py` and `lib/intra_arm.py` are used
as they stand.

**Window-free is forced, not chosen.** `lib/windows.py` resolves a call window
from `configs/*.toml` or refuses; no probe event has a config, and inferring one
from trade density is the circular error round 1 documented (it put INTC at
20:30Z and scored pre-call prints as sweeps). Window-free is already pre-declared
as §S6 of `PRE-REGISTRATION-R2.md`.

**Consequence for the comparison, stated now:** the TRAIN baseline is the
**window-free** figure — **+24.5¢, 20/25 YES, 11 events, CI [+0.3, +48.0]** — not
the windowed +21.0¢. Comparing the probe against the windowed number would be
comparing two different rules and would flatter or damn the probe by accident.

---

## P3 — Outcomes, scored once

- **GENERALIZES** — all of:
  - per-trade EV > 0, and
  - event-clustered bootstrap 95% CI lower bound > 0 (10,000 resamples), and
  - **n ≥ 20 fills over ≥ 10 events**, and
  - the wave-1 filter separates from its own control (the identical rule with
    `min_top = 50`, same events) with non-overlapping point estimates.
- **DOES-NOT-GENERALIZE** — CI upper bound < 0 at n ≥ 20, **or** the wave-1
  filter fails to separate from its control.
- **NOT-RESOLVED** — anything else, including too few fills. A real reportable
  outcome, not a failure to be argued around.

**Required reports that are NOT promotion criteria:**

- Both placebos from `audit_r2.py` on the probe set — random timing after wave-1,
  and the same rule on markets that never triggered. These are what separate a
  real trigger from "buy cheap mention markets".
- A per-venue-class breakdown (fight / hearing / debate / broadcast / …), marked
  **exploratory**. It may generate a round-3 hypothesis; it may not promote or
  demote anything in this round.
- Average fill price against realized P(YES), so the Wilson bound is readable
  next to the price it has to beat.

---

## P4 — What this probe cannot do

Stated now so it cannot be stretched later.

1. **It cannot re-tune the rule.** Any edit to §R2 after seeing a probe number
   burns the probe as evidence, and this file records that it did.
2. **It is not the sacred holdout.** These events are TRAIN-date-eligible: unseen,
   but not future. I could in principle have looked at them and did not, which is
   weaker than data that did not exist. The forward holdout in
   `PRE-REGISTRATION-R2.md` §R4 stays sealed and is untouched by this work.
3. **It cannot be re-run.** A second scoring pass is an amendment to this file.
4. **A pass does not license trading.** `PROBE_LEDGER_CAP_CENTS = 100` still caps
   the lane at $1 lifetime, and §R8's assumption — that a resting offer existed
   and we would have got it — is unresolved for every event here, because no
   Kalshi order-book tape exists for any of them.
5. **It says nothing about live detection.** Every trigger here is found by
   replaying a finished tape. Detecting wave-1 in real time across hundreds of
   boards is a separate engineering question this probe does not touch.

---

## P5 — Why a negative result would be worth as much

If the arm does not generalize, that is the more useful outcome, because it
localises the edge to earnings calls — where we already have an audio program,
call-time discovery and armed events — and it removes a whole imagined roadmap of
covering 398 series. The cross-venue program has form here: the audio findings
did **not** generalise (TBPN batches near show-end, Rogan runs on base-rate
priors, MLB/WNBA pack reacts late). A clean "earnings only" answer would be
consistent with that history and would stop a scale-up that cannot pay.

---

## Notes added before any probe number existed

**N1 — 2026-08-07, a stricter interval is reported alongside the declared one.**
Recorded during the fetch, before a single probe result was computed.

§P3's bar is an **event**-clustered bootstrap CI, inherited from round 2 where
every event was a different company's call. The probe population is not shaped
like that: enumeration returned **798 eligible events across only 75 series**,
and two series dominate — `KXWCMENTION` contributes 104 events and
`KXMLBMENTION` 102, all the same venue with the same maker and the same crowd.
Treating those as 104 independent observations would understate the interval,
and a *generalization* claim is precisely a claim across venues.

So `score_probe.py::series_clustered_ci()` resamples by **series** and is
reported alongside. **The declared §P3 bar is unchanged and stays
event-clustered** — this is added conservatism, not a moved goalpost. If the two
disagree, the series-clustered interval is the honest one and the write-up must
say so.

**N2 — 2026-08-07, the fetch is ~7× larger than planned.** Enumeration found 798
eligible events / 15,396 markets / 322M contracts of volume — the non-earnings
mention boards are far more liquid than earnings ones. Projected `tapes/` growth
is **~2 GB over ~4 hours**, against a planned 200–300 MB / 15–30 min.

**Not truncated.** The population is part of the pre-registration; sampling it
down because it is slow is exactly the convenience deviation this file exists to
prevent. Disk was checked (26 GB free) and `tapes/` is now gitignored — it is
reproducible from the public API, so it does not belong in git history.

## Amendments

*(none yet)*
