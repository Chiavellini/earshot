# Latency proof ledger — measured verdicts, no claims without numbers

Rule (standing, per operator directive 2026-07-17): every latency/detection change ends here with
an interleaved A/B or a side-by-side measurement on the same hardware — or an explicit
`NOT RESOLVABLE ABOVE NOISE` / `INSTRUMENT-ONLY` verdict. Claims that don't survive measurement are
recorded as such, not dropped.

## Session 2026-07-25 (E0) — the OURS term is not a number, it is a LOTTERY on where the word falls in the segment

**Campaign 2, item E0.** Goal was to explain ~200 ms that had never been
attributed: the AXP NFL race scored **289 ms** audio-arrival→decision, but the
arm's own `capture→decision` p50 was **88–91 ms**. The answer is that the
question was malformed — there is no single OURS value to attribute.

### Method — measured, not modelled

The detector cannot fire the instant a word ends; it must first consume some
audio *past* the word end (`sherpa_kws` ≈ 320 ms lookahead + 0.4–0.5 s decode
lag, `VERIFICATIONS.md` Q-M1 pin 3). On a smooth feed that costs exactly itself.
On a live HLS feed it does not, because audio does not arrive smoothly.

`scripts/arrival-profile.py` (new) asks one question of a **banked capture**, for
every frame position, with no model and no assumed segment duration:

    wait(i, A) = capture_ns[i + A/frame_ms] − capture_ns[i]

"Starting from the frame where a word ended, how long until `A` ms more audio had
physically arrived?" Inputs are the per-frame `capture_ns` logs already banked
from the live AXP 07-24 and LMT 07-23 arms — no new event, no network, no spend.

### Result 1 — audio arrives in bursts ~85–144× faster than real time

| capture | venue | burst period | audio per burst | delivered over | speed-up |
|---|---|---|---|---|---|
| AXP `hls` | Amazon IVS, 2 s seg | **2054 ms** | 2000 ms | **24 ms** | **84.6×** |
| LMT `streamlink` | Notified, 4 s seg | **4017 ms** | 4020 ms | **30 ms** | **132.8×** |
| LMT `pipe_tuned` | " | **4050 ms** | 4020 ms | **28 ms** | **144.0×** |

Cross-check: the burst count found here (2380 on AXP) matches the capture
bake-off's own independently-computed `stalls_over_100ms` (**2379**) — the
harness had been counting segment boundaries and calling them stalls.

### Result 2 — the wall cost of lookahead is BIMODAL, and the exposure is A/S

Wall cost of `A` ms of detector lookahead, over every frame position:

| capture | segment `S` | `A` | p50 | p90 | p95 | **pays a whole segment** |
|---|---|---|---|---|---|---|
| AXP | 2054 ms | 200 ms | 2.4 ms | 11.4 ms | 2030.6 ms | **9.6 %** |
| AXP | 2054 ms | **400 ms** | **5.0 ms** | 2033.2 ms | 2037.0 ms | **19.2 %** |
| LMT `streamlink` | 4017 ms | 400 ms | 3.0 ms | 14.6 ms | 3989.0 ms | **9.9 %** |
| LMT `pipe_tuned` | 4050 ms | 400 ms | 2.7 ms | 10.8 ms | 4022.2 ms | **9.8 %** |

**Four-fifths of the time the trailing audio the engine needs is ALREADY IN
HAND and lookahead is free (2.7–5.0 ms). One time in five at AXP it is not, and
the word costs a full ~2.03 s.**

The exposure fraction is predicted by `A/S` and lands within **0.3 percentage
points** on two venues and three independent legs:

| | predicted `A/S` | measured |
|---|---|---|
| AXP, A=400, S=2054 | 19.5 % | **19.2 %** |
| AXP, A=200, S=2054 | 9.7 % | **9.6 %** |
| LMT, A=400, S=4017 | 10.0 % | **9.9 %** |
| LMT, A=400, S=4050 | 9.9 % | **9.8 %** |

### What this changes

1. **The 289 ms was one draw, and a lucky one.** NFL fell in the ~80 % where
   lookahead was free. The ledger has been quoting a single sample of a
   two-peaked distribution as if it were the system's latency.
2. **On ~1 word in 5 at a 2 s venue we are ~2.0 s worse than the figure we have
   been racing against** — on top of the 1.241 s feed gap. Speculative fetch does
   **not** touch this: it changes *when* a segment arrives, not the fact that
   audio arrives in segment-sized lumps, and the trailing audio genuinely does
   not exist yet at the encoder.
3. **Shorter segments hit more often but hurt less** (19.2 % × 2.0 s vs 9.9 % ×
   4.0 s). Expected cost is `A` either way; only the shape differs. So "find a
   shorter-segment rendition" (F1) is **not** a fix for this term — worth knowing
   before F1 runs and gets over-credited.
4. **The lever is `A` itself, and it is linear.** Halving lookahead halves the
   exposure — AXP 19.2 % → 9.6 %. That is precisely what D1 (chunk-8) and D2
   (prefix-firing, which drives `A` *negative*) do, and it promotes both from
   "worth doing" to the largest identified remaining lever on our own side.

### Honest note on the pre-registered bar

The E0 bar was *"the sub-terms reconstruct the measured end-to-end within 10 ms,
and the largest single term is named with a number."* **I am not claiming a pass:
the bar was ill-posed and I wrote it.** It presumes OURS is a scalar; the
measurement's main finding is that it is not. Reconstructing "the" 289 ms to
±10 ms would mean fitting one draw of a bimodal variable, which would look
rigorous and mean nothing. What is delivered instead: the distribution, its
mechanism, the `A/S` law, and its confirmation on three legs at two venues.
A scalar reconstruction remains open and is now specified correctly in E0b below.

### E0b — `A` MEASURED against ground truth: 520–540 ms, ~30 % worse than the standing figure

First direct measurement of detector lookahead. `bench-detector` now reports
`emit_lag_ms = audio_pos_ms − occ.end_ms` per true positive (signed; lossless
speed=0, so no clock is involved). `sherpa_kws`, `chunk_ms=160`.

| split | calls | corpus | TP | per-phrase p50 range | min | max | **TP-weighted median of p50s** |
|---|---|---|---|---|---|---|---|
| dev | INTC + LMT | 2.53 h | 49 | 400–670 ms | 260 | 830 | **520 ms** |
| holdout | AAL | 1.51 h | 15 | 400–710 ms | 360 | 710 | **540 ms** |

*(The report serialises percentiles per phrase, not the raw vector, so the
aggregate is a TP-weighted median of per-phrase p50s — not a true pooled median.
Stated as what it is.)*

**The standing figure was 0.4–0.5 s, inferred from ONNX metadata
(`VERIFICATIONS.md` Q-M1 pin 3) and never observed. Measured, it is 520–540 ms**
— dev and holdout agree to 20 ms on independent calls, so this is a stable
property of the engine, not a corpus artifact.

**Recomputing E0's exposure with the measured `A` makes it worse, not better:**

| venue | `S` | old estimate (A=400) | **measured (A=530)** |
|---|---|---|---|
| AXP / IVS | 2054 ms | 19.2 % | **25.8 %** |
| LMT / Notified | 4017 ms | 9.9 % | **13.2 %** |

**One word in four at a 2 s venue pays a full ~2.05 s segment penalty.**

### E0b — the queue-drain term, and RTF

`RTF = 0.0693` (dev) and `0.0699` (holdout) — 14× real time, and the two agree to
1 %. So draining a whole 2 s AXP burst costs `2000 × 0.069 =` **138 ms**, and a
word's wait behind the audio ahead of it is ≈ `position_in_burst × RTF`, i.e.
**0–138 ms, mean ~69 ms**.

⚠ **That RTF is a single leg on an otherwise idle box.** Production fanout runs
2–3 legs on the same 2 vCPU, so the effective per-leg RTF — and this term — is
some multiple of it. Unmeasured; do not quote 138 ms as the armed figure.

**Reconstruction of the AXP NFL 289 ms.** The measured terms are: queue drain
0–138 ms, chunk quantisation 0–160 ms, lookahead wait 0 ms (NFL fell in the ~74 %
where trailing audio was already in hand) and ~90 ms of compute+decision. Those
span 289 ms comfortably. I am **not** claiming a ±10 ms fit: NFL's exact position
within its burst was never recorded, so the reconstruction bounds the value
rather than pinning it. Every term is now named with a number, which was the part
of the bar that was well-posed.

**Match window is still correct.** `MatchWindow::post_ms = 1500 ms` was sized to
absorb "~0.4 s decode lag". Largest lag actually observed is **830 ms**, so the
window remains adequate with room to spare — no change, and now for a measured
reason rather than an assumed one.

⚠ **`A` is not yet measured at the production chunk size.** The bake-off config
runs `chunk_ms=160`; production arms at **80**. Chunk quantisation is part of
what `audio_pos_ms` reports, so `A` at 80 may differ. The 520–540 ms figure is
pinned to chunk 160 until the sweep below runs — do not quote it as the armed
number.

### E0c — `chunk_ms` is an INERT dial on sherpa_kws: 40/80/160 are bit-identical

Interleaved in one run (dev, INTC+LMT, 2.53 h, `sherpa_kws`), so machine state
cannot differ between cells.

| chunk | tier | TP | miss | FA | recall | FA/h | `emit_lag` p50 |
|---|---|---|---|---|---|---|---|
| 40 | Confirmed | 49 | 28 | 53 | 0.636 | 20.97 | 520 ms |
| 80 | Confirmed | 49 | 28 | 53 | 0.636 | 20.97 | 520 ms |
| 160 | Confirmed | 49 | 28 | 53 | 0.636 | 20.97 | 520 ms |

**Not "a small effect" — the three cells are byte-identical.** `sha256` of the
serialised row set is `3a13cb27…` in all three, and of the sorted FA-autopsy
line list `7913bd65…` in all three, including all 53 false-alarm stream
positions. And they cost the same: 1880 s for three cells vs 630 s for the one
chunk-160 cell in E0b — **~627 s each, so the dial does not even trade CPU.**

**Mechanism — the emission grid is 160 ms, and every size tested divides it.**
Recovered directly from the data rather than assumed: the GCD of all 53
false-alarm stream positions is **160 ms**, and while every position is a
multiple of 40, 80 and 160, **none is a multiple of 320**. `sherpa_kws` stamps
`audio_pos_ms` from its own fed-sample counter at the moment a decode surfaces
(`src/detector/sherpa_kws.rs:92`), and decodes only when the model's internal
quantum is ready — so feeding it in 40, 80 or 160 ms pieces lands the same
absolute sample position every time.

**Consequence: `chunk_ms` cannot make emission finer than 160 ms on this
engine.** Production arming at 80 ms is output-identical to 160 ms. This
supersedes `docs/BAKEOFF.md`'s "chunk_ms is purely a latency dial" for
sherpa_kws — the dial is connected to nothing below the model's quantum. It also
replicates commit `2f78fc0` ("chunk_ms 80→40 REFUTED, measured ~0 ms") far more
sharply: not ~0 ms, but bit-identical.

**So `A` = 520 ms is confirmed as the armed number**, and E0b's caveat
("measured at 160, production arms at 80") is discharged — the two are the same
run in all but name. Exposure stands at **25.3 % (AXP)** and **12.9 % (LMT)**.

### E0d — the discriminating test: the override WORKS, and the grid is `max(chunk_ms, 160 ms)`

E0c's byte-identical cells had two explanations that fit equally well — "the
override works and 40, 80, 160 all divide a 160 ms model grid" versus "the
override is silently ignored by the plumbing" (which would be a real bug). 240
does not divide 160, so it separates them. Control and test interleaved in one
run, single call (INTC, 0.98 h) so the comparison is within-run.

| chunk | TP | miss | FA | **GCD of FA stream positions** | `emit_lag` p50 |
|---|---|---|---|---|---|
| 160 | 21 | 25 | 25 | **160 ms** | **460 ms** |
| 240 | 21 | 25 | 25 | **240 ms** | **550 ms** |

**Cells differ (`rows-sha` 60bfae6b… vs d31f9bce…), and the grid tracks
`chunk_ms` exactly. The override works — no plumbing bug.**

**Refined mechanism, now confirmed by two independent routes.** `sherpa_kws`
stamps `audio_pos_ms` at OUR chunk boundary (`sherpa_kws.rs:92`) but only decodes
when the model has a complete internal frame. The model quantum is **160 ms**.
So the effective emission grid is:

    grid = chunk_ms          when chunk_ms >= 160 ms   (our boundary is coarser)
    grid = 160 ms            when chunk_ms  < 160 ms   (the model's frame dominates)

Every E0c case (40, 80, 160) lands in the second branch — which is exactly why
they were byte-identical — and 240 lands in the first, which is why it moved.

**Operational verdict, and it cuts both ways:**

- **Below 160 ms, `chunk_ms` buys nothing.** Production arming at 80 ms is
  output-identical to 160 ms. Confirmed twice.
- **Above 160 ms it actively costs.** 160 → 240 is **+90 ms of `emit_lag`** for
  nothing (within-run, n=21 TPs). A future "let's use bigger chunks to save CPU"
  change would silently buy a 90 ms latency regression — and E0c already showed
  the CPU saving is nil, since all cells cost ~627 s.
- **160 ms is the knee**: the largest chunk that costs nothing. Production's 80 ms
  is safe but pointlessly fine-grained.

⚠ Cross-run caution: chunk-160's `emit_lag` p50 is 460 ms here vs 520 ms on the
full dev split — different call mix, not a contradiction. Only the within-run
460 → 550 comparison is valid, which is why control and test ran interleaved.

**E0 is complete.** `A` = 520 ms (armed), exposure 25.3 % at AXP / 12.9 % at LMT,
and the chunk dial is now fully characterised as a non-lever. Reducing `A`
requires changing the model quantum (D1) or firing before the word ends (D2).

> **⚠ Correction to my own first read of this run.** I initially pooled the Fast
> and Confirmed tiers and reported recall 0.318. That is wrong: `score_phrase`
> emits one `TierScore` per tier, each carrying `misses = occurrences − tp` for
> that tier, so pooling double-counts every occurrence. Per tier, Confirmed
> recall is **0.636** (49/77) at **20.97 FA/h**, and Fast records 0 TP / 0 FA.
>
> **⚠⚠ Second correction — and this one retracts an alarm I raised.** I wrote that
> the zero-firing Fast tier was "a latency lever silently switched off". **That is
> wrong and I should have checked before saying it.** `configs/bakeoff-webcast.toml`
> declares **no `fast_prefixes` at all** and marks all 41 keywords
> `policy = "confirmed_only"`. Fast entries are built only from `fast_prefixes`
> (`src/detector/kws_compile.rs:224`), so there is nothing to fire and zero
> firings is the *correct* result for this config. Not a bug, not a regression —
> a confirmed-only baseline behaving as declared. The prefix work lives in
> `configs/bakeoff-webcast-prefix.toml`, which does carry per-keyword prefixes
> (e.g. `"basic ec"`, `"client comp"`). D2 measures that config, not this one.

### Still open after E0b

- **`A` vs `chunk_ms`** — the lever's shape. `A` is the whole ballgame now
  (exposure is `A/S`, linear), and we have one point on the curve. Sweep
  chunk 40/80/160 interleaved in one run and find out whether smaller chunks
  buy lookahead or only cost CPU. Directly sizes D1/D2.
- **Effective RTF under armed fanout** — 0.069 is one leg on an idle box; the
  queue-drain term under 2–3 concurrent legs on 2 vCPU is unmeasured.
- **`A` for the streaming_ctc engines** — chunk-8 and chunk-16 exports exist on
  the box and neither has an `emit_lag` number. That is D1, and it is now a
  direct comparison on the metric that matters rather than a proxy.

## Session 2026-07-25 (D1f) — ARMED DRY-RUN of the Tuesday config: PASSES, and armed-fanout CPU matches prediction

The CTC engine had only ever run inside `bench-detector`. This is the first time
it has run inside a real **arm**, through the real launcher, under a deliberately
minimal env (`env -i HOME=… PATH=…`) to mimic launchd.

| check | result |
|---|---|
| `ORT_DYLIB_PATH` resolves under `env -i` | ✅ `Loaded ONNX Runtime dylib from …/vendor/…` |
| Kalshi validation of all 16 PayPal markets | ✅ `book snapshots live for all armed markets` |
| dry-run posture | ✅ `orders will be computed and journaled, never sent` |
| CTC detector built **per leg** | ✅ two ORT sessions initialised |
| fanout wiring | ✅ `fanout: per-leg detect chains, OR at decision legs=2` |
| both decoder subprocesses | ✅ `spec_edge` + `pipe_tuned` spawned and feeding |
| **SIGTERM** | ✅ **259 ms**, kill switch complete, orders cancelled, disarmed, ledger flushed, `report.json` written |
| orphaned processes | ✅ **0** |

### Armed-fanout CPU — D1e's arithmetic VALIDATED

D1e predicted 2 CTC legs at `2 × 0.1518 = 0.304` ⇒ **30.4 % of a 2-vCPU box**,
derived from single-leg RTF and never observed. Measured live: the arm sits at
**67.3 % of one core = 33.7 % of the box**. Prediction 30.4 %, measured 33.7 %.
**The burst-drain estimate rests on arithmetic that now has a live check under
it.** Headroom is ample; the concern was never duty cycle, it is burst drain.

### Two honest gaps

1. **Nothing fired.** Six minutes of DW World News produced **0 detections** with
   PayPal's keyword set — plausible (these are payments words on a news channel)
   but it means the **detection → decision path was not exercised end-to-end in
   this run**. Plumbing is proven; the firing path is inferred from the bench and
   from the earlier sherpa fanout smoke (`82739d3`). Closing it properly needs a
   replay leg carrying a known keyword.
2. **`WARN warm-fire id unexpectedly matched an order — ignoring`** at startup.
   Benign here (dry-run, zero orders) but it is a warning nobody has explained;
   worth chasing before a live-fill posture.

### One thing that looked like a problem and is not

The log takes a **286 KB burst of `ort::logging` INFO** at session init (2140 of
2155 lines) — arena allocations from both CTC sessions. It then stops: measured
**0 bytes of growth over 20 s** in steady state. Not a disk risk, which matters
because the box hit 100 % disk on 07-14. Startup noise, not a leak.

⚠ Also verified during the SIGTERM test: counting survivors **immediately** after
the arm exits reports 5 leg processes still alive. Five seconds later it is 0 —
they were mid-teardown. **Do not read an instantaneous post-kill process count as
an orphan leak;** the earlier 18-orphan incident was real and persistent, this is
not the same thing.

## Session 2026-07-26 — ⚠⚠ THE CORRECTION BELOW WAS ITSELF WRONG. RETRACTED.

The entry below "corrects" 5.15 h → 7.96 h. **It is wrong and the original 5.15 h
was right.** The bench scored exactly 4 calls — `AXP-Q2-2026`, `intc-q2-2026`,
`lmt-q2-2026`, `VZ-Q2-2026` — and reported `corpus_hours: 5.15`.

**Why:** the telephony manifests exist, but `corpus/telephony/` on the box holds
**zero WAVs** (`ls corpus/telephony/*.wav | wc -l` = 0). `manifest::load_dir`
skips a call whose audio is absent, so AAPL/MSFT/QCOM were never scored. I
counted **manifests** and assumed audio; the corpus is wideband-only on this box.

**The reasoning that produced the error is worth more than the error.** I saw
`grep -c "bench run"` = 4, believed dev had 7 calls, and concluded the run was
"57 % through" — then wrote a confident correction on that basis. It was already
finished. The tell I invented ("the runtime disagrees with my arithmetic") was
real; I just resolved it in the wrong direction, trusting my model of the corpus
over the machine's own report of what it loaded.

**Standing consequence:** `corpus_hours` in the bench report is the authority on
what was scored. Manifests describe intent; the report describes fact.

*(The mixed-channel ARGUMENT in the retracted entry still stands as a rule — if
telephony audio is ever present, it must be scored separately from wideband. It
simply was not present here. `scripts/prefix-gate-score.py` splits by channel
anyway, which is why D2b's number is unambiguous.)*

## Session 2026-07-25 — [RETRACTED, see above] CORRECTION: the dev split is ~7.96 h, not the 5.15 h I quoted

I stated "5.15 h" in `64aa07c` and in several loop prompts. That counted the
**wideband calls only**. The dev split is **7 calls**:

| domain | calls | occurrences | ≈ hours |
|---|---|---|---|
| wideband | AXP(0), INTC(44), **LMT(11 ⚠)**, VZ(0) | 55 ⚠ | **5.15** |
| **telephony** | **AAPL(14), MSFT(8), QCOM(13)** | **35** | **2.81** |

⚠ **OPTIMISTIC-DENOMINATOR (marked 2026-07-27).** The `LMT(11)` cell — and hence
the wideband total of 55 — is the **pre-correction** count. The manifests were
regenerated 2026-07-24 05:21:50 and **LMT is 31 occurrences**, so wideband is 75
and the dev total is 110, not 90. The hours are unaffected. This table is left as
written because the section it sits in is itself an evidence trail; the counts are
marked rather than silently updated. Any *recall* computed from the old
denominators is **not comparable** with a post-correction one — and per §L1 the
measured direction for LMT is **pessimistic** (old 0.727 → corrected 0.903), the
opposite of what this tag originally said. See `docs/BAKEOFF.md` §Corpus.

Two consequences, pulling in opposite directions:

1. **More power than claimed.** At ~8 h the ≤0.5 FA/h bar permits **~4** false
   alarms, not the 2.6 I stated.
2. **It is a MIXED-CHANNEL corpus, and that matters more.** The telephony calls
   are 8 kHz, band-limited and G.711 round-tripped. `docs/BAKEOFF.md` is explicit
   that an engine is benched on the channel it trades. **We are arming a WEBCAST
   event, so the decision-relevant false-alarm rate is the WIDEBAND subset** — a
   single blended FA/h mixes two channels and describes neither.

**Therefore D2b must be split by call id** (the `fa_autopsy` lines carry them)
and reported per domain, not as one number.

Caught by the running bench starting a 5th call when I had budgeted for 4 — the
**runtime disagreeing with my arithmetic** was the tell, not the numbers
themselves.

## Session 2026-07-26 — manifest-divergence flag RESOLVED: it was a ground-truth CORRECTION, not tuning

I flagged the box/repo manifest divergence as "provenance unknown, needs the
operator". It was checkable, and checking it clears the concern.

**Timestamps.** Repo versions committed 2026-07-23 (*"pre-committed before any
tuning run"*). Box versions all modified **2026-07-24 05:21:50** — the same
second for all three, so one scripted regeneration, not hand edits.

**The decisive test is not WHEN but FROM WHAT.** Occurrences derived from the
word transcripts are a legitimate aligner fix; occurrences derived from detector
output would be circular. `scripts/verify-occurrences.py` (new) looks each added
occurrence up in `corpus/transcripts/<call>.words.json`:

| call | added phrase | transcript context |
|---|---|---|
| AAL | `boeing` | *"new deliveries of **Boeing** 787-9"* |
| AAL | `ultra` | *"**ultra** premium config"* |
| AAL | `europe` | *"secondary cities in **Europe**"* |
| LMT | `government` ×13 | *"the U.S. **government** awarded us…"*, *"our **government** and our allies"*, … |
| LMT | `sikorsky` ×2 | *"production ramps at **Sikorsky**"* |
| LMT | `dividend` | *"$796 million to shareholders through **dividends**"* |
| LMT | `nasa` | *"our partners at **NASA**"* |

**Every added occurrence is a word genuinely spoken in the transcript.** The
regeneration was the aligner catching mentions the first pass missed —
transcript-derived, detector-blind. **This is ground truth getting MORE correct,
which is the opposite of tuning on the answer.**

### What still stands from the finding

The reproducibility break was real and worth fixing: the committed manifests did
not match what every measurement was scored against, so a fresh checkout would
have reproduced none of the numbers. That is now repaired (`4dd121a`).

**And the direction of the correction matters for reading old numbers.** LMT went
11 → 31 occurrences, so recall figures computed against the *old* manifest were
scored against a denominator that was ~3× too small. Any LMT recall number
predating 2026-07-24 05:21 is therefore **not comparable** with one measured
after it.

> ⚠⚠ **DIRECTION RETRACTED 2026-07-27 — see §L1.** This paragraph originally
> continued: *"they flattered the detector by not counting mentions it missed …
> should be treated as optimistic."* **Measured, that is backwards.** Re-scored
> on the armed model with both denominators from one run: **old 11-occ ⇒ 0.727,
> corrected 31-occ ⇒ 0.903.** All 20 added occurrences are caught; all 3 misses
> were already in the old manifest. **Pre-correction LMT recall is PESSIMISTIC.**
> The non-comparability stands; the direction was asserted, not measured.

**Acted on 2026-07-27** (operator ruling: annotate, do not delete — the ledger
keeps evidence trails). The rule is now stated in full at `docs/BAKEOFF.md`
§Corpus, individual instances carry the tag **`⚠ OPTIMISTIC-DENOMINATOR`**, and
the G4 promotion gate's *relative* dev-recall bar carries an explicit warning
that its untuned baseline must be recomputed on the corrected manifests before
the gate is scored. Scope of the damage, stated precisely: **only quantities of
the form `TP / occurrences` are affected.** False-alarm counts, FA/h, latency
percentiles and raw TP counts have no denominator and stand as measured.
Everything benched 2026-07-25 or later already used the corrected manifests.

⚠ `ultra` is worth a note: on AAL it is a genuine mention (*"ultra premium"*),
while on LMT the collision screen classed `ultra` fires as SUBSTRING hits on
*"ultramaritime"*. **The same keyword is a true positive on one call and an
unwinnable collision on another** — which is exactly why keyword risk is
per-event, not global.

## Session 2026-07-27 (V2) — `--prefetch` PASSES the 90-min drop-safety gate

Pre-registered bar (`scripts/prefetch-gate.sh`, same bar the speculative posture
had to pass after silently losing ~86 s in S7): **zero audio deficit vs the plain
manifest-driven leg.** Two legs against the AWS IVS demo channel, 90 minutes,
each → ffmpeg → 16 kHz WAV.

### Result — PASS

| | prefetch leg | plain leg |
|---|---|---|
| segments | **2673** | 2669 |
| `dropped` / `short` / `seg_err` | **0 / 0 / 0** | **0 / 0 / 0** |
| segments obtained via prefetch | **2672 of 2673 (99.96 %)** | — |
| `lead_med` | **4.00 s** (min 0.25, max 6.04) | — |
| error lines of ANY kind | **none** | **none** |

Wall-anchored deltas, 75 samples over the final 37 min:
**min +0.00 s** (never behind), max +8.19 s, final +0.37 s, second-half drift
+0.37 s. The +8.19 s excursions are exactly one 256 KB ffmpeg write block —
quantization, not lead.

**This also settles the URL-identity question.** If prefetch URLs were not
byte-identical to the URLs later announced, each such segment would be fetched
twice and `segs` would run far above `prefetch_fetched`. Measured: **2673 vs
2672.** The earlier probe's "75 of 77 matched" was a boundary artifact of a short
run, not a 2.6 % mismatch rate.

⚠ **Coverage, stated honestly:** the counters cover all 90 minutes; the
wall-anchored samples cover **09:26 → 10:03 only** (the sampler was added
mid-run, after I noticed the original verdict logic was unsound), plus one
exact-equality spot check at 09:26. The first 53 minutes rest on counters alone.

### ⚠ This gate proves SAFETY. It does NOT measure the LEAD.

The byte-delta sampler resolves one ffmpeg write block — **262144 bytes = 8.19 s**
of 16 kHz mono. It cannot see a 0.92 s difference at all; the observed 0 ↔ 8.19 s
oscillation is that quantum toggling, and the +0.37 s "final" is a fraction of one
block, i.e. noise. It was the right instrument for the question it was built for
(does the prefetch leg silently lose audio? no) and it is the **wrong** instrument
for the question the race arithmetic needs.

So **the ~0.92 s buffered gain remains MODELLED, not measured.** It is derived
from the §V1 probe timings (headers held ~1.9 s, body ~1.4 s, announcement at
4.18 s), not from audio observed arriving earlier at our decoder. Nothing in this
session moved it from model to measurement, and the race-arithmetic table is
model-based to exactly that extent.

What a real measurement needs is a **PDT-anchored** comparison: the plain leg
already prints `seg age=` per segment (wall clock minus that segment's
`EXT-X-PROGRAM-DATE-TIME`), but the prefetch path does not, because a prefetch
URL arrives without a PDT — the manifest has not announced it yet. Its PDT is
derivable (prefetch[0] is the segment after the newest announced one, so
`PDT_last + EXTINF`), and adding that is the next piece of work. Until then the
honest statement is: **prefetch is proven safe and proven to obtain segments
4.00 s before announcement; the audio-arrival gain at our decoder is inferred.**

### Armed-path validation, same session

Dry-run arm of `configs/webcast-pypl-26jul28-arm.toml` on the rebuilt binary,
against a live IVS stream:

- `all keyword markets active … markets=16` ⇒ **the populated allowlist passes
  `allowlist_covers_keywords`**, which is what this run existed to check.
- `fanout: per-leg detect chains, OR at decision legs=2`, 2 leg processes.
- **`--prefetch-report` works end-to-end through the arm**: `prefetch lead=4.002s
  / 4.252s` in `$HOME/spec_edge.err`, matching the standalone probe.
- `order query returned 100 order(s), NONE match` — the hardened
  `order_by_client_id` correctly refusing to adopt any of the ~100 resting orders
  this account carries from other systems. The kill-switch scope fix, live.

### Two traps paid for, neither a product defect

1. **A missing `ORT_DYLIB_PATH` makes `arm` HANG SILENTLY**, not fail. My first
   dry-run sat for 5 minutes at `book snapshots live for all armed markets` with
   no pipeline threads, no leg processes, and **no error line** — main thread in
   `futex_do_wait`, Kalshi API responsive in 0.2 s, sockets idle. Setting
   `ORT_DYLIB_PATH` fixed it immediately. `scripts/launch-webcast.sh` sets it and
   carries a guard, so the launcher path is safe — but an arm started by hand
   looks perfectly healthy while never listening.
2. **`$HOME/<ev>-stream.url` is a loaded gun between runs.** I pointed
   `~/pypl-stream.url` at the AWS IVS demo channel for this test. Left in place,
   Tuesday's arm would have captured **the demo channel instead of PayPal** and
   looked entirely normal doing it. Removed; the runsheet writes it fresh on the
   morning, and it should not exist before then.

## Session 2026-07-27 (V8) — the streamed tier PASSES its 90-minute gate, on the third attempt

`--prefetch --stream-body`, 90 minutes against the AWS IVS demo channel, on the
deaf-leg-fixed fetcher (§V6). **Pre-registered bar, unchanged since S7: zero
audio deficit vs the plain leg.**

```
=== 16:07:03 UTC GATE START   === 17:37:04 UTC capture done
prefetch duration=5399.079625s   0 lost, 0 short, 0 catchup
plain    duration=5399.033188s   0 lost, 0 short, 0 catchup
both legs: dropped=0 short=0 seg_err=0   poll_err=0 deaf_s=0 reresolve=0
samples=179  min=+0.00s  max=+8.19s  final=+8.19s  second-half drift=+0.00s
GATE: PASS — no audio deficit and no duplication
```

**2671 of 2672 segments obtained via prefetch**, `lead_med=4.00 s`.

### Why this is a pass, stated with its limits

Two *independent* checks, and only the second is precise:

1. **Byte-delta sampler: never behind, no drift.** ⚠ **It quantizes to 8.192 s**
   — one 256 KB ffmpeg write block at 16 kHz mono s16 (262144/32000). So
   `min=+0.00 s` means *"never behind by as much as one block"*, and the
   `max=+8.19 s` is **one write block ahead, not 8 s of real lead**. This
   instrument could not have seen a deficit smaller than 8.19 s at any single
   sample. What it *can* see is accumulation, and `second-half drift = +0.00 s`
   says there was none. (Note for S7's failure mode — ~86 s lost as ~6 s gaps —
   individual 6 s gaps are **below** this resolution; only the cumulative drift
   would have caught it. That is worth knowing about this gate generally.)
2. **Final durations, ffprobe-measured and NOT quantized: 5399.080 s vs
   5399.033 s — 46 ms apart, prefetch ahead.** This is the tight check.

Together with `dropped=0 short=0 seg_err=0` on both legs, the pass is sound.

`short=0` is specifically the counter that matters here, and it holds across
**every** `SUMMARY` line in the run, not just the last (`grep -o 'short=[0-9]*'
… | sort -u` returns the single value `short=0`). That is the number that
**refuted** `--stream-body` on DW/Akamai, where the same flag cost **3 / 32 / 62
short reads per run** against 0/0/0 buffered. The DW null was venue-scoped, and
this is the other side of that scope.

### ⚠ What this run did NOT test: the §V6 deaf-leg fix

`reresolve=0` and `deaf_s=0` mean **no media playlist rotated during these 90
minutes, so the re-resolve path never executed.** The fix that §V6 exists for is
therefore still evidenced only by `scripts/test-deaf-leg.py` against a **local
synthetic origin** — never against a real CDN mid-call. A green gate here is not
a test of it, and the failure it guards was intermittent (1 run in 3 that day),
so its absence is expected rather than reassuring.

### V4's "unexplained ~4 s gap" did not reproduce

V4 was called INCONCLUSIVE partly on a 2.16 s shortfall (prefetch 5396.92 vs
plain 5399.08). Here the same comparison is **+0.046 s, prefetch AHEAD.** I am
**not** claiming the instrument fixes caused that — the gate script's `rm -rf`,
the both-legs-exit sampling and the deaf-leg fix all changed between runs, and a
startup effect remains an equally good explanation. **It did not reproduce; that
is all that is claimed.**

### Third attempt — the two that failed were both instrument, not prefetch

| run | window | outcome |
|---|---|---|
| V4 | 11:59 → 13:29 | INCONCLUSIVE — stale-file poisoning + unexplained gap |
| V6 | 14:20 → died 15:12 | **deaf at 51.7 min** — media playlist URL died, fetcher never re-resolved |
| **V8** | **16:07 → 17:37** | **PASS** |

Neither failure was a property of `--prefetch --stream-body`. Both were the
harness measuring itself.

### Consequence

Per the operator decision taken this session (arm the streamed tier, strictly
conditional on this gate): **`--prefetch --stream-body` goes on armed leg 1 of
`configs/webcast-pypl-26jul28-arm.toml`.**

⚠ Read this pass for what it is: **a SAFETY clearance, not a latency result.**
It says the streamed tier does not lose audio over 90 minutes. What it is worth
is §V3/§V5 (arrival) as corrected by **§V7** — p50 *fire-time* gain ≈ 0.6 s, not
the 1.121 s arrival figure. And it is still **one venue, one channel, the AWS
demo** — Tuesday is the first real-venue test. The day-of decision table in
`docs/RUNBOOK.md` step 4b remains the final authority and can still downgrade
the tier if the probe shows PayPal's channel behaves differently.

### V8 follow-on — the armed streamed tier smoke-tested END TO END through `arm`

Dry-run arm of `configs/webcast-pypl-26jul28-arm.toml` with the new leg-1 flags,
against the IVS demo stream (110 s, `timeout`-killed, exit 124 as expected):

- `all keyword markets active … markets=16` — allowlist coverage holds.
- `book snapshots live for all armed markets`
- `fanout: per-leg detect chains, OR at decision legs=2`, both legs spawned,
  leg 1 carrying `--speculative --prefetch --stream-body --prefetch-report`.
- **`ort::logging` allocating continuously ⇒ `streaming_ctc` was running
  inference on both legs**, which only happens if audio frames actually flowed.
  (`ORT_DYLIB_PATH` prints no banner; the proof it built is that the arm got
  past `book snapshots` instead of hanging there — the documented failure mode.)
- `SIGTERM — killing, then exiting` — clean teardown.

Leg-1 fetcher, from `~/spec_edge.err`:

```
prefetch: venue publishes #EXT-X-PREFETCH (2 ahead of the live edge)
speculative: DISABLED — segment URLs are not predictable
prefetch: FETCHING ahead of announcement
SUMMARY segs=31 dropped=0 short=0 seg_err=0 spec=OFF
        poll_err=0 deaf_s=0 reresolve=0
        prefetch_n=28 lead_med=4.01s  prefetch_fetched=30 pf=on
```

**`speculative: DISABLED` is the designed self-disable on IVS's opaque signed
URLs** — the exact reading RUNBOOK step 4 now tells the operator to expect and
NOT act on. 30 of 31 segments came via prefetch. The §V6 deaf-leg fields are
live in the armed path and clean.

⚠ **The dry-run required writing the IVS demo URL into `$HOME/pypl-stream.url`
— the "loaded gun" the runbook warns about. It was deleted immediately after and
absence verified.** Confirmed again here: that file must be written FRESH on
Tuesday, and RUNBOOK step 3 deletes it first regardless.

## Session 2026-07-27 (A3) — the join lottery does NOT exist at IVS, and the A4 bound is now computable

Two results from **banked data only** — the 90-minute §V8 gate logs, re-read. No new
capture, no network, no spend.

### A3a — absolute lag at IVS: 2.62 s, and it does not wander

`~/pfgate4-evidence/plain.err`, `seg age` = PDT → fetch-complete, **n = 2697**
(every segment of the 90-minute run):

| | value |
|---|---|
| p50 | **2.62 s** |
| p10 – p90 | 2.52 – 2.72 s |
| min / max | 2.45 / 3.80 s |
| samples > 3.0 s | **1 of 2697** |
| drift, first-200 vs last-200 mean | 2.63 → 2.61 s — **none** |

**This retires a standing worry for the venue class we actually trade.** §S6
characterised a **7.22 s** join lottery on `prod_tuned` (n=21, DW) and warned that "a
bad draw currently costs up to 6.5 s **for the entire event**". At IVS the whole
distribution is **0.20 s wide at p10–p90 with zero drift over 90 minutes.**

The mechanism is not luck: a 2 s cadence against ~16 ms fetches never lets a backlog
form, so there is nothing for a lottery to draw from. **Scope: this is a property of
short-segment venues, not a refutation of S6** — S6's 6 s-segment reference streams
still behave as measured there. It means the lottery is not a lever *here*, and the
`prod_tuned` fallback leg's lottery costs redundancy rather than races.

Cross-check: §A1's armed-leg smoke put the *complete* lag at **2.393 s p50** (n=29,
60 s). Two different instruments, different runs, different n — **agreeing to 227 ms**
on an absolute quantity that had never been measured at this venue at all.

### A3b — what the 2.62 s is made of, and why prefetch is the only lever that touches it

```
2.62 s  =  2.00 s   the segment itself — cannot be complete before it exists
        +  0.60 s   origin write-completion + CDN propagation
        +  0.016 s  our own fetch  (V3 measured)
```

Only the 0.62 s tail is reachable by any client-side change, and **streamed prefetch
attacks both terms** by consuming the segment while the origin is still writing it.
That is why its lead is phase-dependent (**+2.073 s** at the head, **+0.168 s** at the
tail): it converts a phase-dependent 0.62–2.62 s lag into a near-constant one.
§A2's fire-lag table shows the same thing from the other side — streamed spans
**0.944–1.013 s (69 ms)** against plain's **1.516–3.016 s**.

⚠ The 0.60 s term is **inferred by subtraction**, not measured directly, and §V1's
chunked-response profile that motivates it is **n=3 on the demo channel**. Treat it as
the loosest number here — which is exactly why the A4 instrument below evaluates every
verdict at both ends of it.

### A3c — `scripts/physical-bound.py`: the first ABSOLUTE race instrument

Every other race instrument in this campaign is comparative, and this ledger already
names why that class is blind: *"Any buffering shared by both legs cancels exactly and
is invisible to that instrument."* This one's reference is the encoder's own PDT.

    earliest_possible(word_end) = PDT + u + origin_flush

where `u` is the word-end's offset inside its segment. If the competitor's sweep
precedes that instant they did not get the word from this feed — they fired on a
prefix, or they are on a channel we have not found. **Either way no fetch or transport
work can ever beat that race.** A race is reported DECIDED only when the verdict holds
at *both* `origin_flush = 0` and `= 0.60 s`.

**⚠ A first draft of this script was wrong and the planted control caught it** — it
derived `u` from when the audio reached us. Under a buffered fetch the whole segment
lands at once, so arrival is `PDT + our_lag` for **every** word in the segment and
carries no information about `u` at all. `u` comes only from forced alignment joined to
the segment boundaries. That is the fourth self-consistent-but-wrong instrument this
campaign has produced (after S4's keep-alive −250 ms, E0c, and D1c's 350 ms artifact),
and the first caught before it printed a number anyone believed.

Threshold mode, run on AXP NFL as measured (no PDT banked for that race):

```
we were 1.241 s behind the sweep
IMPOSSIBLE iff u > u*,  u* = 0.779 .. 1.379 s   (flush 0.60 .. 0)
a uniformly-placed word exceeds u* about 31% of the time
⇒ Not settled. Do not quote a direction from this run.
```

So **roughly a third of races would be provably unreachable on-feed even on the most
generous flush assumption** — and the instrument correctly refuses to call this one,
because `u` was not banked when AXP ran. §A1 has since switched `--arrival-log` on for
the armed leg, so the next event converts this from a threshold into a per-race binary.

`--selfcheck` plants three races with known verdicts (IMPOSSIBLE / POSSIBLE /
UNDECIDED) and reproduces `u* = 1.379 s` for AXP NFL.

## Session 2026-07-27 (A2) — ⚠ §V7 OVER-CORRECTED AND I RETRACT IT: the gain is 1.117 s, not 0.605 s

This morning I "corrected" the campaign's headline downward, claiming the
arrival-domain +1.121 s handed back ~516 ms before reaching the decision point.
**That was wrong. Measured properly, the fire-time gain is 1.117 s** — within
**4 ms** of the original arrival figure. The correction was the error.

### Both legs now measured the same way, on the same channel

`--arrival-log` on each leg against the IVS demo channel, 60 s each:

| leg | first-byte lag behind PDT | complete lag | span | n |
|---|---|---|---|---|
| plain (manifest-driven) | **2.516 s** | **2.516 s** | **0.000 s** | 35 |
| `--prefetch --stream-body` | **0.484 s** | 2.393 s | 1.909 s | 29 |

The plain leg's span of **exactly zero** is the buffered path proving itself:
nothing is emitted until the body is complete.

### Fire lag — when audio through `word_end + A` actually arrives

`A = 0.530 s` (E0b measured). Plain includes the segment-**spill** discontinuity
that E0 identified; streamed does not spill because it tracks the encoder.

| word position `f` | plain | streamed | streamed wins by |
|---|---|---|---|
| 0.00 head | 2.516 s | 0.990 s | 1.526 s |
| 0.25 | 2.016 s | 0.967 s | 1.049 s |
| 0.50 | 1.516 s | 0.944 s | 0.572 s |
| **0.75** | **3.016 s** ← spill | 1.013 s | **2.003 s** |
| 1.00 tail | 2.516 s | 0.990 s | 1.526 s |
| **mean** | **2.087 s** | **0.970 s** | **1.117 s** |

**Streamed fire lag is 0.944–1.013 s — a 69 ms spread — against plain's
1.516–3.016 s.** The variance collapse is the bigger result: streaming's largest
win (2.003 s) lands exactly on the segment-spill case E0 identified as where we
bleed.

### Why V7 was wrong, stated so it is not repeated

The gain is a **difference between two legs over the same audio**:

    gain(f) = lag_plain(f) − lag_streamed(f)
            = (arr_plain − t) − (arr_stream − t) = arr_plain − arr_stream

**The speech time cancels exactly.** So the arrival-domain gain *is* the
fire-time gain, identically — and subtracting a lookahead penalty from it
**double-counts**, because the arrival measurement already prices when audio for
a given moment shows up. The lookahead term is a property of the detector and
applies to **both** legs (and to the competitor).

⚠ **§V7's own caveat named this exact failure** — *"treats arrival gain and
lookahead cost as additive at the same percentile when the two are correlated
through segment phase"* — and I wrote the number anyway. Flagging a risk is not
the same as heeding it.

**What §V7 got RIGHT and is NOT retracted:** the burst-structure change (449
bursts of 2000 ms → 5741 of 160 ms) and the per-leg lookahead cost profile
(4.3 ms → 520 ms p50) are real, measured, and reproduced here. What is retracted
is using them to discount the gain.

### Consequences — the original race table stands, and §P1's EV table was wrong

`Δrace` for the AXP NFL reference case returns to ≈ **+0.413 s behind**, not the
0.925 s I wrote this morning. And §P1's lever arithmetic, which used V7's
0.605 s, must be redone:

| race | today | **+FEED (1.117)** | +FEED+prefix |
|---|---|---|---|
| LMT GOLD | +0.507 | +1.624 | +1.944 |
| **LMT MORF** | −1.028 | **+0.089 — FLIPS** | +0.409 |
| AXP NFL | −1.530 | −0.413 | **−0.093** |
| LMT EURO | −2.024 | −0.907 | −0.587 |
| **races won** | **1 of 4** | **2 of 4** | **2 of 4** |

**The streamed tier alone flips MORF and takes NFL to within 93 ms with
prefix-firing.** §P1's "0 of 4 flipped" was an artifact of the wrong FEED figure;
its *sequencing* conclusion survives and is now sharper — prefix-firing becomes
decisive exactly when FEED has pulled races into its band, and NFL at −93 ms is
the first race that would sit there.

⚠ Demo channel, 60 s per leg, n=29/35, and `A` and the phase model are inputs.
Four instruments now agree on the phase-dependent gain (§V3 byte-join n=600,
§V5 cross-correlation, §A1 armed arrival log, §A2 both-leg arrival logs):
head ≈ 2.03–2.07 s, tail ≈ 0.12–0.17 s, mean ≈ 1.12 s.

## Session 2026-07-27 (A1) — PDT banking armed, and it immediately gives the first ABSOLUTE lag from the armed path

§R1 flagged that its strongest available test — *could ANY consumer of the
public feed have held this audio at the instant the competitor swept?* — was
**not computable**, because no capture had ever recorded the encoder's
`EXT-X-PROGRAM-DATE-TIME`. That is a physical **bound**, not another
correlation, and it was blocked purely on instrumentation.

**It turned out to need no code at all.** `hls-edge-fetch.py --arrival-log`
already writes `{pdt} {t_done} {fetch_secs} {mode} {t_first}` per segment. It
was simply never switched on for an armed leg. Added to leg 1 of
`configs/webcast-pypl-26jul28-arm.toml`; **zero extra HTTP**, one local line per
~2 s.

PDT is the correct key and that is verified rather than assumed: two independent
IVS sessions shared **14 of 14 PDTs to the millisecond while sharing ZERO segment
URLs** (`arrival()` docstring — a run keyed on URL produced 0 paired segments).
Being the encoder's own stamp for the media, it is stable across sessions, which
is what makes it usable against a competitor's timestamps too.

### First absolute-lag measurement from the ARMED configuration

60 s smoke on the IVS demo channel, leg-1 command verbatim, n=29 prefetch-mode
segments (the one startup `manifest` segment excluded — it has no prefetch lead):

| | p50 | IQR | min | max |
|---|---|---|---|---|
| **first-byte lag behind encoder** | **0.484 s** | [0.474, 0.517] | 0.449 | 1.849 |
| **complete lag behind encoder** | **2.393 s** | — | 2.349 | 2.437 |

The campaign has reasoned in *relative* leads throughout; this is the first
**absolute** position behind the encoder measured from the armed path. For
scale, `manifest-probe` put the naive IVS edge position at **~4 s** behind PDT
(AXP 07-24) — the armed leg now takes first bytes at **0.484 s**.

### A third instrument, and it lands on the V7 model to 1 ms

§V7 modelled the streamed gain as phase-dependent, `lead(f) = 2.073 − f × 1.905`.
The span between first byte and complete arrival is exactly that slope:

**median (complete − first byte) = 1.904 s, against the model's 1.905 s — 1 ms.**

That is a byte-timestamp join (§V3), a cross-correlation (§V5) and now the armed
leg's own arrival log all agreeing on the same segment-phase span. The
distribution is real and it is not an artifact of any one instrument.

⚠ Demo channel, 60 s, n=29, and the complete-lag figure is tight (2.349–2.437 s)
precisely because the demo channel is well-behaved. Q4's channel on Tuesday is
the real test — and it will now be logged.

## Session 2026-07-27 (P1) — prefix-firing RE-OPENED as instructed, and the re-opening says it is not the binding constraint yet

Operator ruling 2026-07-27: **re-open prefix-firing generally**, on the strength
of §V7 (its latency value converts at ~1.00 ms/ms under streamed arrival vs
~0.008 buffered) and §R1 (~half the competitor's lead may be earlier *firing*).
Re-opened. Three things were asked for. Two are answerable now; one is not, and
the reason it is not is itself the finding.

### (c) RE-MEASURING THE GAIN IS IMPOSSIBLE ON THE BANKED CORPUS

`n=3` was the objection. It cannot be improved, and this is structural, not a
matter of running more:

| prefixed phrase | true occurrences, **entire** banked corpus |
|---|---|
| `panther lake` | 20 (INTC) — **excluded by D2b's own a-priori rule**, `panther` is a standalone word |
| `basic economy` | 3 — and they are in **AAL, the HOLDOUT split** |
| `golden dome` | 2 (LMT) |
| `client computing` | 1 (INTC) |
| `venture fund` / `venus aerospace` / `world cup` | **0 anywhere** |

**Dev, after the a-priori rule: 3 occurrences across 2 phrases** — exactly the
n=3 that made D2 too weak. **3 of the 7 prefixed phrases have no true occurrence
anywhere in the corpus at all.** A bench re-run would spend 20 minutes
reproducing n=3. Not run, deliberately.

This is D2's own conclusion — *"the binding constraint is the corpus, not the
technique"* — now quantified exactly.

### (b) THE A-PRIORI RULE WOULD PASS THE BAR, AND THAT PASS WOULD NOT MEAN ANYTHING

D2b's rule (*a `fast_prefix` must not be a complete standalone word*) drops
`panther`, removing **2 of the 4** Fast false alarms ⇒ 2 FA / 5.15 h =
**0.388/h, under the ≤0.5 bar.**

**I am not calling that a pass.** Two reasons, and the first is D2's own:
1. The rule was proposed *after* seeing that `panther` caused those 2 FAs.
   Principled in form, post-hoc in origin — and validating it on the data that
   motivated it is circular. D2 rejected exactly this move for the collision
   variant and that rejection applies to me here.
2. The surviving evidence is **2 false alarms and ~3 true positives.** A bar
   cleared on that is cleared by having almost no data, not by being safe.

### (a) THE EXPECTED-VALUE RE-DERIVATION — AND IT CHANGES THE REASON FOR PARKING

Asked for: express the bar as expected value rather than a bare FA/h number.
Doing that makes the FA side almost irrelevant, because **the gain side is
currently zero.** Against every race we have actually measured:

| race | today | +prefix (320 ms) | +FEED+prefix (925 ms) |
|---|---|---|---|
| LMT GOLD | **+0.507** | +0.827 | +1.432 |
| LMT MORF | −1.028 | −0.708 | −0.103 |
| AXP NFL | −1.530 | −1.210 | −0.605 |
| LMT EURO | −2.024 | −1.704 | −1.099 |

**⚠ THIS TABLE USED §V7's 0.605 s FEED FIGURE, WHICH IS RETRACTED (§A2).**
Redone with the measured **1.117 s**: FEED alone takes MORF to **+0.089 (a win)**
and, with prefix-firing, NFL to **−0.093** — races won go **1 of 4 → 2 of 4**.
So prefix-firing does not flip a race *on its own*, but it is no longer firing
into empty space: after FEED lands, NFL sits 93 ms outside the band, which is
exactly where a 320 ms lever decides. **The sequencing conclusion below survives
and is sharpened; the "0 of 4" figure does not.**

⇒ **A false-alarm bar is the wrong question while the lever converts no races.**
The FA cost is real (up to `MENTION_FILL_CAP_CENTS` 50¢ on a market that settles
NO); the benefit at today's Δrace distribution is **nil**.

### VERDICT — parked, on a NEW and better-founded reason

Prefix-firing stays parked. **Not** on the 0.78 FA/h bar this time, but because
**it is not the binding constraint at the current Δrace distribution.** It
becomes decision-relevant only once races sit within ~320 ms of parity, which
requires the `FEED` lever to land first. That is a *sequencing* result, and it
inverts this morning's read that prefix-firing was the priority lever.

⚠ n=4 races. The 4 are draws from a distribution with ~±2 s of segment-phase
spread (§E0), so "0 of 4 flipped" is a statement about the draws we hold, not a
probability. What it does establish is that **no race we have measured is close
enough for this lever to matter.**

### What would make it answerable — and it is already scheduled

Tuesday's PayPal board carries **five multi-word phrases** (`agentic commerce`,
`european payments council`, `cash back`, `e commerce`, `ads id`) — precisely
the class D2 measured a ~+0.6 s gain on, and the class the banked corpus lacks.
**Capturing Tuesday supplies the corpus this question needs**, at no extra cost,
and the same tape answers §R1's word-duration slope test. No new work is
required to unblock it; it is a by-product of an event already armed.

## Session 2026-07-27 (L1) — ⚠ I GOT THE DIRECTION BACKWARDS THIS MORNING: the LMT correction RAISED recall, 0.727 → 0.903

Operator ruling was *annotate **and** re-score*. The annotation went in this
morning; here is the re-score, and **it refutes the inference the annotation
carried.**

### The claim I made, and it was wrong

`docs/BAKEOFF.md` §Corpus and this ledger both say the pre-correction LMT recall
figures were **optimistic** — that the too-small denominator *"flattered the
detector by not counting mentions it missed."* That is an assumption about
**which** occurrences the aligner had missed. **Measured, it is false.**

### The exact comparison — old manifest recovered from git, not reconstructed

`git show 5ecd5a0:corpus/manifests/lmt-q2-2026.toml` is the pre-correction
ground truth, so the diff is exact:

| phrase | old | corrected | added |
|---|---|---|---|
| `government` | 0 | 13 | **+13** |
| `record` | 0 | 3 | +3 |
| `security` | 0 | 3 | +3 |
| `dividend` | 0 | 1 | +1 |
| `europe` / `golden dome` / `morfius` / `nasa` / `sikorsky` | 3/2/3/1/2 | same | 0 |
| **total** | **11** | **31** | **+20** |

Re-scored on the **ARMED** model (`sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18`,
`streaming_ctc`, chunk 160, LMT only, 1.548 h), one run, both denominators
scored from **that same run** so only the ground truth varies:

| ground truth | TP | miss | occ | **recall** |
|---|---|---|---|---|
| old (11-occ) | 8 | 3 | 11 | **0.727** |
| **corrected (31-occ)** | **28** | **3** | **31** | **0.903** |

**All 20 added occurrences are CAUGHT** (`government` 13/13, `record` 3/3,
`security` 3/3, `dividend` 1/1). **All 3 misses are `morfius`, and they were in
the OLD manifest already.** So the mentions the first aligner pass missed were
mentions we *detect* — the small denominator made recall look **worse**, not
better.

⇒ **Pre-correction LMT recall figures are PESSIMISTIC, not optimistic.** The
`⚠ OPTIMISTIC-DENOMINATOR` tags placed this morning are corrected in the same
change; the tag name is kept but its text now states the measured direction.

**What survives unchanged:** you still cannot compare a number scored against
the 11-occurrence manifest with one scored against the 31-occurrence manifest.
The reproducibility break was real. Only the *direction* of the bias was
asserted without measurement, and it was asserted by me.

*(Scope: this isolates the DENOMINATOR effect, holding the detector fixed. It is
not a claim about what any historical run reported — those used whatever engine
and thresholds were current at the time.)*

### Incidental, and it confirms a live hazard

Run #1 of this used `configs/bakeoff-webcast-ctc16.toml`, which points at
`models/streaming-ctc-en-20260723-chunk16` — the **D3 control export, not the
model we arm.** My error, caught before publishing. Re-run on the armed model:

| | `panther lake` FA | total FA | FA/h |
|---|---|---|---|
| chunk-16 **control** export | **527** | 553 | **357.31** |
| **armed** model | **0** | 25 | **16.15** |

**527 false alarms from one keyword, on a Lockheed call that cannot contain an
Intel product name** — and zero from the shipped model on identical audio. This
replicates §D3b's fragile-keyword finding call-by-call: `panther lake` is a
0.3 %-weight-difference tripwire, and it is why that keyword must never be armed
without a per-model FA check. It is in `webcast-intc-arm{,-fill}.toml` and in no
staged 07-28/29 config.

Armed-model FA/h of **16.15** on LMT is well above the 5.30 the AAL holdout gave
(D1b). Drivers are the newly-added common words themselves — `government` 11 FA,
`record` 6 FA. One call; not a bar verdict.

## Session 2026-07-27 (B1) — our four legs receive audio IDENTICALLY; two of my own instrument constants were lying

Operator asked whether the competition is on a *cleaner* feed. First half of the
answer, on the acquisition side: **our own legs are not meaningfully different
from each other**, and the two apparent differences I found first were both
artifacts of `arrival-profile.py`'s constants.

12 legs, 4 events, 2 venue classes, from banked captures — no new capture, no
spend. **AXP and VZ were captured CONCURRENTLY on one host** (12:17:21 and
12:19:52 on 07-24), and LMT/AAL likewise (both 12:02:54 on 07-23), so venue is
isolated from host and network by construction.

| event | venue | leg | p50 cost of A=530 ms | **spill past burst** | stalls |
|---|---|---|---|---|---|
| AXP | IVS 2.054 s | `hls` / `pipe` / `pipe_tuned` | 7.0 / 7.3 / 7.0 ms | 25.0 % | 17 / 20 / 19 |
| AXP | " | `streamlink` | 5.7 ms | 26.0 % | 2 |
| VZ | Notified 4.02 s | `pipe_tuned` / `streamlink` | 6.2 / 5.0 ms | 12.6 / 12.9 % | 6 / 1 |
| LMT | Notified 4.02 s | `pipe` / `pipe_tuned` / `streamlink` | 3.6 / 3.6 / 3.9 ms | 12.7 / 12.7 / 12.9 % | 2 / 2 / 1 |
| AAL | Notified 4.02 s | `pipe` / `pipe_tuned` / `streamlink` | 3.9 / 3.9 / 3.3 ms | 12.7 / 12.7 / 12.9 % | 2 / 2 / 1 |

### What this confirms

**E0's `A/S` exposure law, on 12 independent legs at two venues simultaneously:**

| venue | predicted `A/S` (530 ms) | **measured** |
|---|---|---|
| IVS, S=2054 ms | 25.8 % | **25.0–26.0 %** |
| Notified, S=4020 ms | 13.2 % | **12.6–12.9 %** |

**And §V7's buffered baseline is NOT a demo-channel artifact.** The claim that
under buffered arrival detector lookahead is nearly free at p50 was measured on
the AWS IVS demo loop (4.3 ms). At four *real* venues it is **3.3–7.3 ms**. The
V7 comparison's baseline holds on real audio.

⇒ **No leg has an acquisition advantage worth having.** Whatever the competitor
is doing, it is not "picked a better one of the clients we already run".

### ⚠ Two instrument constants that produced impressive nonsense

Both were mine, both are fixed in `scripts/arrival-profile.py`, and both are the
same failure: **a constant that silently interacts with segment duration.**

1. **`STALL_NS = 3 s` flat.** At any venue whose segments are ≥3 s, *every
   segment boundary* reads as a stall. Raw output: **1090–1384 "stalls" per run**
   at the Notified venues against 17–20 at IVS — an apparent **60× venue
   difference** that is entirely the constant. With the threshold derived from
   the measured burst period (`max(3 s, 1.5 × period)`) the same runs report
   **1–6**. I nearly published the 60×.
2. **`max_gap_s` included teardown.** The 26.4–26.7 s "worst-case delivery gap"
   at every Notified venue occurs at **99–100 % of the run** — it is the capture
   shutting down. Excluding the final gap: 8.1–14.0 s. **During the calls, every
   leg at every event had ZERO gaps > 10 s.**

### What did NOT replicate — and I am reporting it as such

My first read of AXP alone was *"streamlink is 10× more stable (2 stalls vs
17–20) — and it is the leg we dropped"*. Tested on the other three events, the
**direction** holds (streamlink is lowest in 4 of 4) but the **magnitude does
not**: 1 vs 2–6 at the Notified venues. At counts of 1–19 with n=4 this is
**NOT RESOLVABLE**, and it is not a basis for changing a leg. The AXP-only
reading would have been a one-event finding dressed as a law.

### Still open on the quality axis

This settles *arrival*. It does not touch whether the **decoded audio** differs
per leg or whether **detection** differs per leg — the 07-22 TARI observation
that different legs catch different words. That needs the detector over the
banked leg WAVs and is queued behind the running gate.

## Session 2026-07-27 (R1) — ⚠ ROUGHLY HALF OF THE COMPETITOR'S "FEED ADVANTAGE" MAY NOT BE FEED AT ALL

**Question asked by the operator: what are they doing that we aren't, and are
they on a cleaner/faster feed?** Answering it turned up a confound that the
campaign has carried as a one-line caveat and never computed — and it is large
enough to change what the program should be spending on.

### The answer space is already bounded — three settled facts

1. **There is no alternate transmission of an earnings call.** Recording/API
   services are downstream of the same webcast (closed on architecture,
   2026-07-26, never-revisit).
2. **They are on the same segmented HLS we are** — batching-covariance slope
   **−0.084** vs **+1.00** predicted for a continuous feed.
3. **Not a hidden channel.** The pre-registered discriminator says Δw earlier by
   >~5 s ⇒ H2/hidden. At ~1 s we are firmly in **H1/H3: same feed, better
   vantage or better fetch.**

So it must be **earlier bytes**, **fewer bytes needed**, **closer vantage**, or
**faster decode**. This entry separates the first two.

### The confound

The pinned "FEED gap" is the interval between their price-moving sweep and the
instant the **word-END** audio reached our box. It is anchored on word-end
because that is when *our* detector can fire. **A competitor firing on
word-START never needed the word-end audio** — and the audio for the word's
start reached our box earlier, by exactly the word's duration:

    earlier_audio = FEED_gap − word_duration     (they fire at word start)
    earlier_audio = FEED_gap                     (they fire at word end)

The word durations come from the same forced alignment that produced the
word-end anchors and are already banked in `corpus/manifests/`. No new
measurement was needed. `scripts/rival-decompose.py` (new) does it.

### Which utterance was which race — cross-validated, not eyeballed

The only place bias could enter is picking *which* utterance a race refers to
when a phrase recurs. That is fixed here by a clock offset Δ between the corpus
WAV and the arm capture, fitted independently per race:

| race | candidate utterances ⇒ implied Δ | chosen |
|---|---|---|
| GOLD | **126.960 s**, −324.450 s | 1924.67–1925.31 s |
| EURO | **128.046 s**, 115.486 s, −946.014 s | 2352.08–2352.52 s |
| MORF | **127.927 s**, −1305.073 s, −1491.273 s | 3259.57–3259.87 s |

**Three independent races agree on Δ ≈ 127 s to within 1.1 s**; every rival
pairing is 324–1491 s away. The identification is unambiguous.

### Result

| race | venue | FEED gap | word | word duration | **residual = genuine earlier AUDIO** |
|---|---|---|---|---|---|
| AXP NFL | Amazon IVS | 1.241 s | `nfl` | **unknown — not banked** | **NOT COMPUTABLE** |
| LMT GOLD | Notified/CDN | 1.112 s | `golden dome` | 0.640 s | **0.472 s** |
| LMT EURO | Notified/CDN | 1.125 s | `europe` | 0.440 s | **0.685 s** |
| LMT MORF | Notified/CDN | 0.754 s | `morfius` | 0.300 s | **0.454 s** |

On the same three races: raw gap spread **0.371 s** → residual spread
**0.231 s**, and `corr(word duration, raw gap) = +0.792`.

**If the word-start model is right, their true feed advantage is ~0.45–0.69 s,
not ~1.1 s — and the other ~0.5 s is a DETECTOR difference, not a transport
one.** That is the same ~0.5 s that prefix-firing addresses, and it means the
campaign has been spending on `FEED` to close a gap that may be half `OURS`.

### ⚠ VERDICT: NOT RESOLVED — and the counter-evidence is real

**I am not claiming the word-start model.** Against it:

- **The MS phone-lane analysis concluded the opposite**, explicitly: *"NOT
  prefix-firing: they land at/after word-end + their transport, never before"*
  (`COMPETITOR-ANATOMY` §Decomposition, n=3 words, ±25 ms). If the same actor
  behaves the same way on webcast, the residual is the **full** gap and their
  feed advantage really is ~1.1 s.
- `corr = +0.792` is **n=3**. Significance at n=3 needs |r| > 0.997. **This is
  `NOT RESOLVABLE ABOVE NOISE`** and the direction is all that may be reported.
- The spread reduction 0.371 → 0.231 s is descriptive, on 3 points, and could
  easily be chance.
- `europe` is the *keyword*; the speaker likely said "European", so the banked
  occurrence span may be shorter than the word actually uttered — which would
  **understate** the correction for EURO specifically.
- All three computable races are the **same venue class**. NFL — the only IVS
  race — has no banked word duration, so **the venue-invariance question cannot
  be asked of the residual at all.**

### What resolves it, and it is nearly free

**A designed test on Tuesday's PayPal board.** The two models make opposite
predictions about the *same* event:

- **word-START firing** ⇒ the apparent feed gap **scales with word duration**.
- **word-END firing** ⇒ the apparent feed gap is **independent of word duration**.

PayPal's vocabulary is unusually well spread for this: `stash` (2 tokens) against
`agentic commerce`, `european payments council`, `stablecoin`. Score Δrace per
market **against that market's word duration** rather than pooling, and the slope
answers it. This costs nothing beyond scoring the tape we were already going to
pull.

### Instrumentation gaps this exposed, both cheap to close

1. **No banked PDT.** The A4 physical-bound test — *could any consumer of the
   public feed have held this audio when they swept?* — is the strongest test
   available and is **not computable**, because neither `~/axp-capture/run.log`
   nor the capture `jsonl` records per-segment `EXT-X-PROGRAM-DATE-TIME`. The
   fetcher already computes `seg age=` and `--arrival-log` exists; the armed legs
   should bank PDT so future races can be bounded rather than only compared.
2. **No AXP ground truth.** `corpus/manifests/axp-q2-2026.toml` carries **zero
   occurrences**, so the one IVS race cannot be decomposed. Aligning the banked
   `~/axp-capture/*.wav` would recover it and is queued.

## Session 2026-07-27 (V7) — ⚠ THE HEADLINE IS AN *ARRIVAL* NUMBER: `--stream-body` hands back ~516 ms of it at the decision point

**This is the most important correction of the campaign and it cuts the headline
gain roughly in half at p50.** Every prefetch number to date — V1, V3, V5 — is
**when audio ARRIVES**. The race is decided by **when the detector can FIRE**,
and those differ by the lookahead term E0 measured. Nobody had priced the
interaction, and it is large.

Measured with `scripts/arrival-profile.py` on the **banked V5 legs** — one
`capture-bakeoff` run, both legs interleaved on the same machine, so the standing
"never across runs" rule is satisfied. No new capture, no network, no spend.

| | `plain` (buffered) | `pfstream` (`--prefetch --stream-body`) |
|---|---|---|
| bursts over 900 s | 449 | **5741** |
| burst period / payload | 2002 ms / 2000 ms | **154 ms / 160 ms** |
| delivered-over | 16 ms (126.9× real time) | 1 ms |
| **A=200 ms, p50 wall cost** | **1.6 ms** | **200.6 ms** |
| **A=400 ms, p50** | **3.3 ms** | **400.4 ms** |
| **A=530 ms, p50** | **4.3 ms** | **520.1 ms** |
| A=530 ms, p90 | 1991.2 ms | **670.6 ms** |
| A=530 ms, p95 | 1994.8 ms | **716.8 ms** |
| A=530 ms, max | 2247.0 ms | 2174.9 ms |
| spills past its own burst | 26.0 % | 99.7 % |

### The mechanism, and why it was invisible

`--stream-body` does exactly what it advertises: it stops delivering audio in
2-second lumps and delivers it **near real time**. E0's central finding — that
four-fifths of the time the trailing audio the engine needs is ALREADY IN HAND,
so lookahead is free — **is a property of buffered arrival, not of the system.**
Under streamed arrival that property is gone: the detector's ~530 ms lookahead
costs its full face value, essentially always.

**The conversion factor between an offline latency win and a real one:**

| arrival mode | p50 wall cost per ms of lookahead |
|---|---|
| buffered (today's production leg) | **≈ 0.008 ms/ms** — cutting `A` buys ~nothing at p50 |
| streamed | **≈ 1.00 ms/ms** — cutting `A` buys its full face value |

The streamed column is 200.6/200, 400.4/400, 520.1/530 — the identity line, to
within 2 %.

### What this does to the race table

| state | arrival gain | **est. p50 FIRE-time gain** |
|---|---|---|
| `--prefetch` buffered | +0.168 s | **+0.168 s** (arrival profile unchanged) |
| `+ --stream-body` | +1.121 s avg | **≈ +0.605 s** (1.121 − 0.516) |

⚠⚠ **THIS PROJECTION IS RETRACTED — see §A2.** It read: *"the projected Δrace
for the streamed tier is ≈ 0.9 s behind at p50, not the +0.409 s the
arrival-domain table claims."* **Measured with both legs' arrival logs, the
fire-time gain is 1.117 s — within 4 ms of the arrival figure — so +0.409 s was
right and this correction was the error.** The gain is a DIFFERENCE between two
legs over the same audio, so the speech time cancels and subtracting a lookahead
penalty double-counts. The burst-structure and lookahead-profile measurements
below STAND; only this subtraction is withdrawn. **Streamed is still the best
option on the board** (0.9 < 1.36 < 1.53) and its *tail* is dramatically better
(p90 671 ms vs 1991 ms) — but the size was overstated, and the overstatement came
from measuring arrival when the race is settled at fire time.

### It also RE-VALUES the two levers this campaign closed

Both were closed on **precision**, neither on latency:

- **D2 prefix-firing** measured **+320 ms** median `emit_lag` gain — a pure
  `A`-reduction, benched offline where segment boundaries do not exist. Under
  today's buffered arrival that converts to ≈ **3 ms** of real p50 gain, which is
  why it has always looked worthless in production. **Under streamed prefetch it
  converts to ≈ 320 ms.** Parked at 1.19 FA/h against a ≤0.5 bar.
- **The `streaming_ctc` swap** costs **≈ +300 ms** under armed fanout (+50 ms
  `consumed_lag` p50 plus +249 ms burst drain, D1c as revised by D1i-b), bought
  for −69 % false alarms. Reverting is a one-line edit to `engine =`.

And E0's own conclusion — *"the lever is `A` itself, and it is linear"* — was
**right about the mechanism and wrong about the coefficient under the arrival
profile we actually ran**. It also retrospectively explains D1: chunk-8's −80 ms
of quantum reduction produced no visible win because at p50 the term was already
free. (D1 stays rejected on its other costs: +263 ms drain and −3 of 57
detections.)

### Caveats, stated rather than buried

- The `pfstream` leg carried **both** `--prefetch` and `--stream-body`, so
  attributing the 154 ms burst structure to `--stream-body` is **by mechanism,
  not by a third arm**. A 3-leg capture (plain / `--prefetch` / `--prefetch
  --stream-body`) would settle it and has not been run.
- One 900 s run per leg, AWS IVS demo channel, not Q4 Inc's.
- `A = 530 ms` is E0b's measured `sherpa_kws` lookahead; D1c put `streaming_ctc`
  `consumed_lag` p50 at the same 540 ms, so it is a fair proxy for both.
- **`1.121 − 0.516` is an ESTIMATE, not a measurement.** It treats arrival gain
  and lookahead cost as additive at the same percentile when the two are
  correlated through segment phase. The two distributions above are the honest
  result; the single number is a projection and is labelled as one. Measuring the
  fire-time gain directly needs a detector on both legs against keyworded audio —
  which is what Tuesday's control legs plus the armed run can finally provide.

## Session 2026-07-27 (V6) — the gate did NOT complete: a leg can go TOTALLY DEAF with every counter clean

**Verdict: the streamed-tier gate ran 51.7 of its 90 pre-registered minutes and
is NOT a pass.** What killed it is a defect worth more than the gate.

> ⚠ **SUPERSEDED ON THE GATE QUESTION 2026-07-27 by §V8** — the 16:07 re-run on the
> fixed fetcher completed all 90 minutes and **PASSED** (179 samples, never behind,
> `short=0`). This section stands unchanged as the record of the *defect*, which is
> the part worth keeping: the deaf-leg failure and its fix are not retracted, and
> **§V8 did not exercise them** (`reresolve=0`, nothing rotated that run).

### What happened

`prefetch-gate.sh` started 14:20:40 UTC. At **15:12** — 3105 s in — both WAVs
froze at 99,352,576 B (3104.8 s of audio) and never grew again. The processes
stayed alive for another 46 minutes.

| leg | segments delivered | then | other error kinds |
|---|---|---|---|
| `plain` | 1554 | **10,860 consecutive poll 404s** | **none** |
| `prefetch` | 1555 | 10,939 consecutive poll 404s | none |

The channel was **still live** — its master playlist returned HTTP 200 throughout
(verified from the laptop at 15:58). What had died was each leg's **resolved
media playlist URL**.

`scripts/hls-edge-fetch.py` resolved master→media **once at startup** and never
again, so both legs polled a dead URL at 4 requests/second for 45 minutes.

### Why this is worse than the gate it broke

**Every health signal the FETCHER emits stayed clean.** The last SUMMARY before
the flood reads `segs=1554 dropped=0 short=0 seg_err=0`, there is no `seg LOST`,
and — the sharpest part — **the SUMMARY line was emitted only after a successful
poll, so once the leg went deaf it stopped reporting entirely.** The final state
of the log is a perfect health line followed by silence. `docs/RUNBOOK.md`'s own
post-run check ("`seg LOST` must be 0") **passes** on that log.

### ⚠ Severity is NOT the same under the arm — checked rather than assumed

My first write-up of this said the armed run would have lost 18 minutes silently.
**That is wrong and is corrected here.** `PipeSource` carries a **30 s
dead-stream guard** (`src/source/pipe.rs:59`, `idle_timeout`): no bytes for 30 s
⇒ `SourceError::Protocol` with the decoder's stderr tail. So:

| context | what a deaf fetcher does |
|---|---|
| bare shell pipeline (`prefetch-gate.sh`, `fetcher \| ffmpeg > wav`) | **totally silent** — observed here, 45 min |
| **armed run / `capture-bakeoff`** (through `PipeSource`) | leg **errors loudly after 30 s**, `SourceArbiter` keeps the surviving leg ("one dead leg must not tear down") |

So Tuesday would not have lost audio silently — it would have **lost the racing
leg permanently**, 30 s after the failure, with `pipe_tuned` (12–19 s absolute
lag) carrying detection alone for the remainder. Every race after that point is
forfeit, but the detections survive and the failure is visible.

**That is the honest value of the fix: not "prevents silent loss" under the arm,
but "3 s of recovery instead of losing the racing leg for the rest of the
call".** It *does* prevent silent loss in the bare-pipeline instruments, which is
what broke this gate and what every future long capture depends on.

Related to the S7 silent-audio-loss class but distinct in shape: S7 lost ~86 s in
scattered gaps within a live leg; this ends the leg outright — and on an earnings
call the tail is the Q&A, where keyword density is highest.

It is also a **politeness** failure: ~21,800 requests across the two legs at a
dead URL. Against a real issuer's CDN that is precisely what earns the rate-limit
every other budget in that script exists to prevent.

### Frequency — it is intermittent, which is worse than deterministic

Three 90-minute runs against the same channel this day:

| run | window | outcome |
|---|---|---|
| V2 (buffered) | 08:33 → 10:03 | completed, 5399.4 s |
| V4 (streamed) | 11:59 → 13:29 | completed, 5396.9 / 5399.1 s |
| this one | 14:20 → died 15:12 | **deaf at 51.7 min** |

So it is not a fixed TTL — **1 in 3 today**. A rate that high cannot be accepted
on a leg that has to survive a 70-minute call.

⚠ It also offers a *candidate* mechanism for V4's "unexplained" 2.16 s duration
shortfall (5396.92 vs 5399.08). **Candidate, not conclusion** — a brief poll-404
window would produce exactly that shape, but it was not observed and is not being
claimed.

### The fix, and it is TESTED rather than asserted

`scripts/hls-edge-fetch.py`:
- on every **12 consecutive** failed polls, **re-resolve the original URL**; if it
  was a master this recovers the leg. Recoveries are counted and logged loudly.
- if the leg was handed a **media** playlist rather than a master it **cannot**
  self-heal — it now says so once, at startup and again on failure. ⇒ **day-of
  rule: write the MASTER/channel m3u8 into the `.url` file, not a variant.**
  (`~/axp-stream.url` from 07-24 holds a `/v1/playlist/<token>.m3u8` variant, so
  the AXP arm had no recovery path either.)
- back off to 1 s polling after ~10 s deaf, killing the request flood.
- `SUMMARY` now carries **`poll_err=`, `deaf_s=`, `reresolve=`** and a
  **`DEAF-NOW`** marker, and is **emitted at the TOP of the loop** so a leg that
  cannot poll still reports. A health line that goes silent exactly when the leg
  breaks is worse than no health line.

`scripts/test-deaf-leg.py` (new) reproduces the failure against a local origin
that rotates its session mid-run, and is a real A/B:

| fetcher | segments before switch | segments after | re-resolved | duplicates |
|---|---|---|---|---|
| pre-fix (`HEAD`) | 19 | **0** — deaf forever | no | 0 |
| fixed | 19 | **25** | yes, in **3.1 s** | 0 |

The control matters: the same test **fails** on the unpatched fetcher, so it is
measuring the fix and not the weather.

**Gate re-launched 16:07 UTC with the fixed fetcher, ending ~17:37 UTC.** The
51.7 min of data collected before the failure showed deltas of only `0` or
`+262144` (one 256 KB block, prefetch AHEAD) and never a deficit — **but 52
minutes is not 90, and the bar is not being re-negotiated after the fact.**
Evidence preserved in `~/pfgate3-evidence/` before the re-run wiped the directory.

## Session 2026-07-27 (V5) — the prefetch lead SURVIVES ffmpeg, and the "not steady" spread IS the segment-phase distribution

The biggest open unknown: every prefetch number was ARRIVAL at the fetcher's
stdout. Measured now at the **PIPE SOURCE — after ffmpeg** — via
`configs/pcm-lead-bakeoff.toml` (two legs differing in one flag) +
`scripts/leg-lag.py`, which aligns on audio CONTENT and compares `capture_ns`.

```
plain -> pfstream   (+ = pfstream LATER;  negative = pfstream AHEAD)
   win@s   delta_ms
   450.0    -1476.3      704.9    -1030.6
   513.7    -1664.5      768.6    -1309.6
   577.5     -241.4      832.3    -1687.5
   641.2     -493.6      896.0     -170.3
n=8  median=-1170.1 ms  spread=1517.1 ms
VERDICT: NOT STEADY — do not quote a single number.
```

**The guard is right, and the reason it fired is the finding.** This quantity is
not a constant — it is the segment-phase distribution. A word at the head of a
segment gains the full streamed lead; a word at the tail gains only what the
buffered tier gives, because the last byte lands at the same instant either way.
So windows sampling different phases MUST scatter, and a single number would
misrepresent it.

### The two instruments agree, and they were built independently

| | arrival domain (§V3, n=600, PDT-keyed) | PCM domain (this run, n=8, content-keyed) |
|---|---|---|
| tail of segment | **+0.168 s** | **+0.170 s** (min observed) |
| average | **+1.121 s** | **+1.170 s** (median) |
| head of segment | +2.073 s | +1.688 s (max observed) |

The tail figures agree to **2 ms** and the averages to **49 ms**, from a
byte-timestamp join on one side and a cross-correlation on the other. Every
observed window falls inside the arrival-domain range [0.168, 2.073]. The head
figure is under-sampled — 8 windows of 4 s did not happen to land at a segment
boundary — which is exactly the direction a small n biases.

**Conclusion: the streamed prefetch lead reaches the decoder essentially intact.
ffmpeg and the pipe do not eat it.** The race table's `+1.121 s` row is no longer
arrival-only.

### Instrument change — the loop guard is now a condition, not a blanket refusal

`leg-lag.py` refused outright on detecting ANY repetition, because a 40 s loop
once produced −21/−38/−59 s of fiction at peak 1.00. That is right when the
repeat is REACHABLE: 40 s of loop against a ±30 s search leaves the repeat inside
the ±60 s ambiguity band. It is wrong for the AWS IVS demo channel, which loops
every **180.2 s** — six times the search range away.

The honest test is not "does it loop" but **"is the alignment ambiguous where we
look"**. Verified on this data before relying on it: main peak **0.956 at
0.00 s**, highest rival inside ±30 s **0.226** — a ratio of **0.24**. The guard
now refuses when `period ≤ 2 × search_secs` (which still catches the 40 s case)
or when an in-window sidelobe exceeds 0.5 of the main peak, and prints both
numbers when it proceeds.

⚠ Still one venue, one channel — the AWS IVS demo, not Q4 Inc's. And n=8 windows
is small; the agreement with an independently-built instrument is what carries
this, not the sample size.

## Session 2026-07-27 (V4) — the streamed-tier gate is INCONCLUSIVE, and I am not calling it a pass

`--prefetch --stream-body`, 90 minutes against the AWS IVS demo channel, same
pre-registered bar as V2: **zero audio deficit vs the plain leg.**

**The verdict script said FAIL.** It should not be believed, and neither should a
pass be claimed from it.

### Why the FAIL is not real

`min = −5399.04 s` in a 5400 s run is physically impossible, and the candidate
leg's own counters contradict it: `segs=2672 dropped=0 short=0 seg_err=0`,
0 mid-body failures, 2671 of 2672 segments via prefetch, `duration=5396.9 s`.
Finding the row rather than arguing with the aggregate:

```
11:59:19 prefetch=0 plain=172769140 delta=-172769140     <- sample 1 of 180
11:59:49 prefetch=786432  plain=786432  delta=0
```

`plain=172769140` is **the previous gate run's finished file**, 5399 s of audio,
read 2 seconds into a live capture before ffmpeg truncated it — against a
`prefetch.wav` that did not exist yet. The gate script never wiped its output
directory. Of the other 179 samples: **162 exactly 0, 17 at +262144** (one
256 KB write block, i.e. the prefetch leg AHEAD). Never behind.

### Why I am still not calling it a PASS

A second discrepancy is **unexplained**: final durations are prefetch 5396.92 s
vs plain 5399.08 s, and the candidate had ~2 s MORE wall time (it launches
first). So it produced ~4 s less audio than its window allowed while plain
produced essentially all of its. That is 0.075 % over 90 minutes and plausibly a
startup effect — the candidate blocks ~2 s on its first prefetch fetch before any
byte flows — but "plausibly" is not a measurement, and the byte sampler stopped
when the *first* leg exited, so it cannot see the shutdown window either.

One poisoned input plus one unexplained gap is not a basis for changing the audio
path. **Verdict: INCONCLUSIVE. Re-run on the fixed instrument.** Excluding the
invalid datum is legitimate — it is provably not a measurement of this run — but
I only examined it because I disliked the answer, and that is exactly when to
demand a clean re-run instead of a rescued one.

### Instrument fixes (both codified, not applied by hand)

- `prefetch-gate.sh` now `rm -rf`s its output directory, skips samples until
  **both** WAVs exist and carry audio, and samples until **both** legs exit
  (`||` not `&&`) so the shutdown window is covered.
- `prefetch-verdict.py` drops degenerate rows where either leg reads <1000 bytes
  and **prints how many it dropped** — a verdict that silently discards rows
  which would flip it is worse than one that fails.

Trap 28: **`rm -rf` the output directory, or a previous run's artifacts become
this run's data.** The stale file was the right size to look like a catastrophic
deficit rather than obvious garbage.

## Session 2026-07-27 (V3 — RACE ARITHMETIC, now MEASURED) — prefetch is worth ~1.12 s, not the 1.6 s I modelled, and it is still not parity

> Tagged **V3** in the header 2026-07-27: three other sections and one config cite
> "§V3" and there was no such header to land on. §V1b and §V1c are likewise cited
> but are not headers — they are the two `⚠` correction blocks inside §V1 below
> (V1b = observe-only `--prefetch-report` + the over-strong V1 claim; V1c = the
> buffered tier needs no concurrency).

Superseding the model-based table. Measured by `scripts/arrival-ab.sh` +
`arrival-lead.py` on a live IVS channel, **20 minutes, n=600 paired segments per
leg, 0 lost / 0 short on all three legs**:

| leg | LEAD (complete) | LEAD (first byte) |
|---|---|---|
| `--prefetch` (buffered) | **+0.168 s** IQR [+0.114, +0.220] | +0.168 s (identical by construction) |
| `--prefetch --stream-body` | +0.168 s | **+2.073 s** IQR [+2.014, +2.129] |

Internal consistency check that the instrument is sound: both legs show the
**same** complete-lead, which is exactly right — streaming cannot change when the
origin finishes writing the segment. And 600 segments in 1200 s is precisely the
2 s cadence with zero backlog, so the serial prefetch loop keeps up.

### Two of my own numbers were wrong

1. **The buffered tier is worth +0.168 s, not the ~0.92 s I modelled** — off by
   5.5×. I had already written down why ("the segment completes when the origin
   finishes producing it, no matter when we ask") and then credited 0.92 s
   anyway. A 250 ms phase-locked poller sees the announcement almost instantly
   and fetches in **16 ms**, so almost nothing remains to win on completion. The
   4.18 s announcement lead does **not** convert into audio-arrival lead.
2. **The streamed tier needs NO concurrency.** `--prefetch --stream-body` earns
   its ~2 s serially: the fetch blocks ~1.98 s while the origin writes, at a 2 s
   cadence, which is exactly one in flight. My "~2 concurrent fetches with
   ordered emission" was wrong; it is a **flag**, not a threading rewrite.

### The race table, measured

The streamed gain is phase-dependent between two measured endpoints —
`lead(f) = 2.073 − f × 1.905` for a word at fraction `f` of a segment — so it
averages **+1.121 s** over uniform word position.

> ⚠ **EVERY ROW BELOW IS THE AXP NFL RACE, NOT "THE GAP"** (corrected
> 2026-07-27 on an operator challenge). NFL is the reference case because it is
> the only race with a full FEED/OURS decomposition on one box — **not** because
> it is typical. The four corrected Δrace figures we hold, scored on the
> price-moving sweep, are **LMT GOLD +0.507 s (AHEAD)**, LMT MORF −1.028 s,
> **AXP NFL −1.530 s**, LMT EURO −2.024 s: median **−1.28 s**, mean **−1.02 s**,
> range **+0.5 … −2.0 s**. **We already win some races**, and the race is a
> distribution — the same conclusion E0 reached about `OURS` and §V7 reached
> about the streamed tier. Do not quote 1.530 s as the system's deficit.
> (Sign warning: `COMPETITOR-ANATOMY` uses negative = behind; this table uses
> positive = behind.)

| state | FEED | OURS | **Δrace (AXP NFL reference case)** |
|---|---|---|---|
| today | 1.241 | 0.289 | **+1.530 s behind** |
| `--prefetch` buffered (**measured +0.168**) | 1.073 | 0.289 | **+1.362 s behind** |
| `+ --stream-body` (**measured, +1.121 avg**) | 0.120 | 0.289 | **+0.409 s behind** |
| …best case, word at a segment's head (+2.073) | −0.832 | 0.289 | −0.543 s (ahead) |
| …worst case, word at a segment's tail (+0.168) | 1.073 | 0.289 | +1.362 s behind |

**This is still not parity.** The best available configuration leaves us ~0.41 s
behind on average, and where a word falls inside its segment swings the outcome
by ±0.95 s. Nothing here reaches the terminal condition, and the remaining 0.41 s
has no identified lever — `OURS` was closed by D1 and `FEED` is now down to what
the origin's own write speed allows.

### ⚠ What these numbers still are not

- **Arrival, not detection.** Measured at the fetcher's stdout with output to
  `/dev/null`. Whether ffmpeg converts earlier BYTES into earlier PCM — and
  whether the pipe's backpressure changes the picture — is **not measured**. That
  instrument does not exist yet and is the next thing to build.
- **One venue, one channel.** The AWS IVS demo channel, not Q4 Inc's.
- `--stream-body` has **not** passed a drop-safety gate on IVS. Its DW refutation
  (−0.197 s, 3/32/62 short reads) was venue-scoped, and the runs above show 0
  short reads over 600 segments — but that is 20 minutes, and S7 is the reason
  the bar is 90.

## Session 2026-07-27 (D1 — CLOSED) — chunk-8 REJECTED: −80 ms bought for +263 ms, and it loses 3 of 57 detections

D1 proper, finally run against a validated control. Pre-registered as a possible
net loss (e81a4ac); it is one, and by a wider margin than the pre-registration
allowed for, because the accuracy cost was not anticipated at all.

### Measured

| | SHIPPED / chunk-16 | **chunk-8** |
|---|---|---|
| `consumed_lag_p50` (paired, n=10) | — | **−80 ms** (IQR [−120, +0], min −190, max +160) |
| `emit_lag_p50` (paired, n=10) | — | **+0 ms** (IQR [−10, +0]) |
| wall, dev split 5.15 h | 2126 s | **3351 s = 1.577×** |
| recall, **excluding `panther lake`** | **0.842** (TP 48 / miss 9) | **0.789** (TP 45 / miss 12) |
| FA/h, excluding `panther lake` | 14.96 | 14.38 |

Both splits reported as pre-declared. *Including* `panther lake` the two are
indistinguishable (0.818 recall, ~470 FA/h) because that phrase is pathological
in both — see D3b.

### Netting it

`consumed_lag` is the cross-engine-comparable metric, and **−80 ms is exactly
what theory predicts once the arithmetic is right**: halving the decode quantum
320 → 160 ms saves *half* the reduction on average, not all of it. My
pre-registered "≈160 ms quantum" was 2× too high; the measurement and the
corrected theory agree to the millisecond.

Cost, via D1i-b's measured contention:

| | solo | 2 legs @ 1.50× |
|---|---|---|
| chunk-16 drain (2 s burst) | 304 ms | 456 ms |
| chunk-8 drain @ 1.577× RTF | 479 ms | **719 ms** |
| **penalty** | +175 ms | **+263 ms** |

**Net at the armed 2-leg configuration: −80 ms gained, +263 ms paid = +183 ms
NET LOSS**, and a 5.3-point recall regression on top.

⚠ **The exposure term cannot be credited, and it would not save this.** E0's
`A/S` exposure model predicts a further ~160 ms from shrinking the window
450 → 290 ms. **This bench cannot measure it** — `bench-detector` replays a WAV
as fast as backpressure allows, so there are no segment boundaries and no
segment-phase lottery to win. Even granting the model in full, chunk-8 lands at
roughly break-even on latency while losing 3 of 57 detections. There is no leg
count at which it wins.

### VERDICT: REJECT. And that closes the last identified lever on `OURS`.

`configs/bakeoff-webcast-ctc{8,16}.toml` stay for the record; nothing is
promoted. The exports remain on the box.

**This matters beyond D1.** `OURS` (0.289 s, 19 % of the gap) had exactly one
identified lever — the model quantum — and it is now measured and rejected.
`chunk_ms` was already inert (E0d), `num_threads` and `spin_hot_rings` are dead
ends (D1i-b), and threading is bounded by a 3-stage design that already uses
~2 vCPU per leg. **Every remaining speed lever is on `FEED`**, which is the
81 % term anyway — and on the venue class that actually matters that means
`#EXT-X-PREFETCH` (§V1/V1b/V1c), not the speculative fetcher we shipped.

## Session 2026-07-27 (D3b) — the control's 31× gap was ONE keyword, not quantization (and my first explanation was wrong)

The pre-registration said: export chunk-16 too, and if it does not reproduce the
shipped model, chunk-8's number means nothing. It did not reproduce, and stopping
to ask why was the whole value of running it.

| tier | model | TP | miss | FA | recall | FA/h |
|---|---|---|---|---|---|---|
| Confirmed | SHIPPED | 50 | 27 | 77 | 0.649 | **14.96** |
| Confirmed | ours, chunk-16 | 62 | 15 | **2425** | 0.805 | **471.19** |

`emit_lag` medians were identical (**+0 ms**, n=11), so the geometry was right —
the model was simply firing at a wildly different rate.

**Cause: we were comparing int8 against fp32.** `find_encoder_onnx`
(`src/detector/streaming_ctc.rs:236`) sorts `int8` first — *"Prefer an int8
export (smaller/faster) when several exist"* — and the shipped directory carries
**both** `…-left-128.int8.onnx` and `…-left-128.onnx`. My collect step filtered
int8 out, so the export dirs held fp32 only. The "control" was int8 vs fp32:
trap 8 again, an instrument comparing two different quantities.

### ⚠ CORRECTION — the int8 explanation above was WRONG

I re-ran with int8 present in both directories, and the gap barely moved:
**FA 2425 (fp32) → 2431 (int8)**. Quantization explains **nothing** here. The
"fp32 gives 31× the false alarms" claim is retracted.

What survives: `find_encoder_onnx` does prefer int8 and the shipped directory
does carry both, so every `streaming_ctc` figure in this campaign **is** an int8
figure. That part stands. It simply is not the cause of the control failure.

### The real cause: ONE phrase, and the control otherwise passes EXACTLY

| | SHIPPED | ours, chunk-16 |
|---|---|---|
| all rows | TP 50 · miss 27 · FA 77 (**14.96/h**) | TP 63 · miss 14 · FA 2431 (**472.35/h**) |
| **excluding `panther lake`** | TP 48 · miss 9 · **FA 77 (14.96/h)** | TP 48 · miss 9 · **FA 77 (14.96/h)** |

**79 of 82 rows are byte-identical in TP and FA**, and the three that move are
`panther lake` (FA 0 → 2354, TP 2 → 15), `ultra` (1 → 2) and `atc` (3 → 2). One
phrase carries **2354 of the 2354** extra false alarms — 457/h on its own, a fire
roughly every 8 seconds — and the entire apparent recall "gain" (0.649 → 0.818)
is that same phrase.

**So the export IS faithful.** The control passes everywhere it can be read.

### What actually differs between the two models

Checked rather than assumed, cheapest question first:

| check | result |
|---|---|
| `tokens.txt`, `bpe.model` md5 | **identical** — not a vocabulary mismatch |
| initializer count / total parameters | **641 / 22,457,594 — identical** |
| graph nodes | 7353 vs 7167 — different tracing, same math |
| weight values (595 tensors, size+mean+std) | **0 identical**; differences in the 3rd–4th significant figure |

Same architecture, same parameter count, **weights ~0.3 % apart** — the signature
of a different checkpoint averaging (`--use-averaged-model` / `--avg` semantics),
not of a broken export.

### The finding worth keeping

**A 0.3 % weight perturbation moved one keyword from silent to firing every 8
seconds, and left 79 others bit-identical.** That is not model fragility in
general — it is a *fragile keyword*. `panther lake` sits in a regime where this
model family is unstable in both directions at once: the shipped model catches
2 of 17 occurrences, ours catches 15 but fires 2354 times.

⚠ **`panther lake` is carried by `configs/webcast-intc-arm.toml` and
`webcast-intc-arm-fill.toml`** at `confirmed_only`. It is **not** in any of the
three staged 07-28/29 configs (PayPal, Ford, FOMC — checked), so it is not a risk
this week. It is a live false-alarm hazard for any future INTC arm, and the
shipped model being on the quiet side of that instability is luck, not margin.

Trap 27: **when a pooled metric moves 31×, find the row before naming the
cause.** I had a plausible mechanism (int8 vs fp32), it fit the aggregate, and it
was wrong. One `GROUP BY` would have pointed at `panther lake` immediately.

**Consequence for D1:** chunk-8 IS interpretable, with `panther lake` reported
separately — declared here BEFORE the treatment run finishes, not after.

## Session 2026-07-27 (V1) — IVS hands us the future. Our speculative fetcher buys ZERO there, and `#EXT-X-PREFETCH` buys ~2.3 s.

**The premise under Tuesday's whole plan was wrong.** `docs/RUNBOOK.md` said PayPal
is "the venue class where `L` was measured three times (1.40/1.50/1.51 s)". It is
not. Those three readings were all taken on **Unified Streaming's demo server**
(`demo.unified-streaming.com`, 1.92 s segments, standard HLS — see the S3/S6
entries below). That is a different origin that happens to have similar segment
duration. **Segment duration is not venue class**, and nothing about `L` had ever
been measured on Amazon IVS.

### Why our speculative leg buys nothing on IVS

`hls-edge-fetch.py --speculative` earns its lead by NUMERICALLY predicting
segment N+1 (`pattern_is_predictable`). IVS segment URLs are opaque signed
tokens:

```
/v1/segment/CucCQjX4C-Pa2l0Xm8nl4YThczGSYTr8vaWr4t9XDaQHuwrrCik4Scz4oBy3TqN9…
```

so the fetcher hits its own `speculative: DISABLED — segment URLs are not
predictable` guard and the `spec_edge` leg silently degrades to a plain edge
fetcher. **Two independent sources**, per the never-close-a-venue-question-on-one
rule: the live AWS IVS demo channel, and **the real AXP production stream
captured 07-24** (`1fe4bb1f4543.use21.playlist.live-video.net/v1/playlist/…`,
Q4 Inc — the same platform PayPal uses). Same URL shape on both.

### What IVS ships instead — and the instrument that could not see it

IVS publishes **`#EXT-X-PREFETCH`** (Twitch LHLS): the manifest *names the URLs
of segments that do not exist yet*. No prediction, no 404 probing, no CDN
politeness exposure — the venue volunteers them.

`scripts/manifest-probe.sh` greps only for RFC 8216bis tags
(`EXT-X-PART|PART-INF|SERVER-CONTROL|PRELOAD-HINT`). On 07-24 it therefore
reported **"LL-HLS capability: (none — standard-latency HLS)"** for AXP. That was
a **false negative for the mechanism that venue actually ships.** Patched: the
probe now reports `EXT-X-PREFETCH` separately.

### Measured — `scripts/ivs-prefetch-probe.py`, 286 polls / 150 s, AWS IVS demo channel

**Q1 — how far ahead of announcement is a prefetch URL?**

| | |
|---|---|
| prefetch URLs matched to a later normal announcement | **75 of 77** |
| `L_prefetch` median | **4.181 s** |
| range | 0.521 – 4.260 s (tight cluster 3.65–4.26) |

At 2.002 s segments that is ~2 segments of lead, consistent with the 2 prefetch
URLs the manifest carries.

**Q2 — is the body complete on publish, or produced live?**

```
200  hdr=1943ms  body=1139ms  bytes=60536  reads=4  clen=None  tenc=chunked
200  hdr=1842ms  body=1375ms  bytes=54520  reads=4  clen=None  tenc=chunked
200  hdr=2145ms  body=1446ms  bytes=60160  reads=4  clen=None  tenc=chunked
```

`Transfer-Encoding: chunked`, **no `Content-Length`**, headers held ~1.9 s, body
dribbling over a further ~1.4 s in 4 reads. Control: the one already-complete URL
in the set returned `hdr=41ms body=104ms` — the same 60 KB in one RTT. **The
origin serves the segment WHILE IT IS BEING WRITTEN.**

**F2's "segments are COMPLETE ON PUBLISH" does not hold here.** F2 scoped itself
to DW/Akamai and said to re-ask per venue; re-asked, and IVS answers the other
way. Progressive consumption — refuted on DW — is real on IVS.

### What it is actually worth, stated conservatively

The 4.18 s is the announcement lead, **not** the wall-clock gain. Netting the
~1.9 s header block:

| | when audio is in hand |
|---|---|
| manifest-driven client | segment announced at `T+4.18 s`, fetched in ~1 RTT ⇒ **~4.22 s** |
| prefetch, last byte | **~3.3 s** ⇒ complete **~0.9 s earlier** |
| prefetch, FIRST bytes | **~1.9 s** ⇒ start-of-segment audio **~2.3 s earlier** |

⚠ **Sharpened 07-27, because the first phrasing here ("more than enough to
close it") was too strong.** The gain is *phase-dependent within the segment*,
and the two modes are not interchangeable:

- **Buffered** (read the prefetch body to completion, then emit): a flat
  **~0.92 s**, every word. That does **NOT** close a 1.241 s deficit on its own.
- **Streamed** (emit bytes as they arrive): a word at the *start* of a segment
  gains ~2.3 s; a word at the *end* gains the same ~0.9 s, because the last byte
  lands at the same moment either way. Averaged over uniform word position,
  **~1.6 s** — which does close 1.241 s on average, but not on every word.

**And it is not sustainable serially.** A prefetch fetch takes ~3.3 s from the
moment its URL appears, while new prefetch URLs appear every ~2 s. Fetching them
one at a time falls behind ~1.3 s per segment and compounds. A correct
implementation needs **~2 concurrent in-flight fetches with strictly ordered
emission** — which is exactly the number of prefetch URLs the manifest carries,
so the venue is sized for it. That concurrency is the real work here; it is not a
flag flip.

Unlike numeric speculation it costs **no 404 traffic** — the URLs are given.

**The progressive machinery already exists.** `--stream-body` was built and
REFUTED on DW (−0.197 s against a ≥0.3 s bar, plus 3/32/62 short reads). That
refutation was venue-scoped for the same reason F2 was: on DW the body arrives in
one RTT, so there was nothing to stream. On IVS the body genuinely dribbles over
~1.4 s. **The null does not transfer** — but neither does the validation, so
`--stream-body` on IVS is untested and its short-read behaviour there is unknown.

### ⚠ Correction to the "needs concurrency" claim above

The claim that prefetch needs ~2 concurrent in-flight fetches is right for the
*streaming* tier and **wrong for the buffered one**. The reasoning was: a fetch
occupies ~3.3 s while URLs appear every ~2 s, so 3.3/2 ≈ 1.65 connections. That
treats the 3.3 s as work. It is not — **~1.9 s of it is the origin holding the
connection while the segment is still being written.** The segment completes when
the origin finishes producing it, ~3.3 s after its URL appears, *no matter when
we ask*. So asking early and blocking costs nothing, and the loop
re-synchronises at one fetch per ~2 s of wall clock.

| tier | concurrency | gain |
|---|---|---|
| **buffered, serial** | **none needed** | flat **~0.92 s** every word |
| streamed, ordered | ~2 in flight | ~2.3 s (start of segment) … ~0.9 s (end), ~1.6 s avg |

That splits the lever into a small change worth 74 % of the `FEED` deficit and a
larger change worth the rest.

### Shipped from this session — observe-only, safe to arm

`hls-edge-fetch.py --prefetch-report`: times the two sightings of each prefetch
URL (as `#EXT-X-PREFETCH`, then as a normal segment) and reports
`prefetch_n / lead_med / lead_min / lead_max` in the periodic SUMMARY. It issues
**zero extra HTTP requests** — it only times manifest lines the fetcher already
parses — and does not touch the audio path. **This is how Tuesday measures the
lever on the real Q4 Inc channel without risking the run.** Selftest covers the
IVS token shape refusing the predictability check.

### ⚠ What this does NOT establish

1. **Measured on the AWS IVS demo channel, not Q4 Inc's PayPal channel.** IVS has
   standard and low-latency channel types and prefetch may not be enabled on
   theirs. AXP's production stream is the same IVS product at 2 s segments with
   ~4 s edge lag — consistent with a low-latency channel — but **no raw AXP
   manifest was saved**, so I cannot confirm prefetch was present on it. The
   patched `manifest-probe.sh` settles it day-of, before arming.
2. **We do not implement prefetch.** This is a source-layer code change, not a
   config flip. Nothing ships from this session.
3. A prefetch fetch **occupies a connection for ~3.3 s**, and two are in flight
   at once. Whether that disturbs the normal path is unmeasured.
4. It measures ARRIVAL only. The decode and detector terms are untouched.

Trap 26: **a capability probe that greps for one standard reports "(none)" for a
venue that implements a different one** — and the miss is invisible, because a
false negative on a capability looks exactly like the capability being absent.

## Session 2026-07-27 (D3) — "chunk-8 needs a GPU box" was WRONG. It needed five pip packages.

`A` (detector lookahead, 520 ms) is the only identified lever on the `OURS` term,
and `OURS` becomes 100 % of the race gap the moment speculative fetch closes
`FEED`. The queue had it filed as **⛔ BLOCKED — needs a GPU box
(PyTorch/icefall/k2)** since 07-24. That reading came from one line of
`~/ctc-export.log`:

```
ModuleNotFoundError: No module named '_k2'
```

which was taken to mean "k2 needs CUDA". Checked instead of assumed:

1. `_k2.cpython-312-x86_64-linux-gnu.so` **is present** in the venv, and
   `import k2` succeeds on the box today (`k2 1.24.4.dev20250714+cpu.torch2.7.1`
   against `torch 2.7.1+cpu`). The 07-24 failure was a stale torch/k2 pairing
   (the log's own header shows `torch: 2.13.0+cpu`), since repaired.
2. k2 is referenced in `export-onnx-streaming-ctc.py` in **exactly one place** —
   `k2.SymbolTable.from_file(params.tokens)`, i.e. reading `tokens.txt`. Even
   with k2 genuinely broken, that is a 20-line pure-python shim, not a GPU.
   (The shim is in `~/k2shim` as a fallback; it was not needed.)
3. The real blockers were **pure-python pip packages** on icefall's import graph
   — `lhotse`, then `urllib3`, then `kaldialign`, `pypinyin`, `tensorboard` —
   plus an empty checkpoint glob in the 07-24 script (`cp: cannot stat ''`).
   Runner `~/ctc-export-retry.sh` installs on `ModuleNotFoundError` and retries.

**Both exports produced, CPU-only, in 49 s, at zero AWS cost.** Verified from
the ONNX metadata rather than from the file existing:

| model | `x` shape | `decode_chunk_len` | window | shift |
|---|---|---|---|---|
| SHIPPED `…-2024-03-18` | `[N,45,80]` | 32 | 450 ms | 320 ms |
| **ours, chunk 16** (control) | `[N,45,80]` | 32 | 450 ms | 320 ms |
| **ours, chunk 8** (treatment) | `[N,29,80]` | **16** | **290 ms** | **160 ms** |

The control is byte-for-byte equivalent in geometry to the shipped model, which
is what makes the treatment interpretable. No Rust change is required:
`StreamingCtcModel::new` reads `T` from the `x` input shape and `decode_chunk_len`
from the ONNX metadata — the chunk geometry was never hardcoded.

### ⚠ Pre-registered, before the bench runs

Chunk 16→8 halves the emission quantum (320 → 160 ms) and shrinks the window by
160 ms, **but doubles encoder invocations per second of audio**, so RTF roughly
doubles and D1i's burst drain lands on the critical path. This can be a **net
loss**: ~160–320 ms of quantum bought against a drain increase of the same
order. `~/bench-lookahead.sh` runs SHIPPED / ours-16 / ours-8 on the dev split
and decides it. Whatever it says gets recorded, including a loss.

Trap 25: **"needs a GPU" is a diagnosis, and this one was never made** — a
`ModuleNotFoundError` on a native extension was converted straight into a
hardware requirement, and it parked the only lever on `OURS` for three days.

## Session 2026-07-27 (D1i-b) — re-measured on 2 physical cores: contention 2.34× → 1.50×, and the threading refutation did NOT survive

D1i's whole table was taken on `t3a.large` — **1 physical core**. The box was
resized to `t3a.xlarge` (4 vCPU / **2 physical cores**, `AMD EPYC 7571`,
`Thread(s) per core: 2`, `Core(s) per socket: 2`) and the identical script re-run
on the identical call (INTC, 0.98 h):

| run | old box (1 phys core) | **new box (2 phys cores)** | |
|---|---|---|---|
| A — 1 proc, `num_threads=1` | 538 s | **394 s** | −27 % |
| B — 1 proc, `num_threads=2` | 762 s (**+42 % slower**) | **363 s** (**−8 % vs A**) | direction REVERSED |
| C — 2 procs concurrent | 1259 s (**2.34×**) | **592 s** (**1.50×**) | contention nearly halved |

**Contention factor is 1.50×, not 2.34×.** Corrected burst drain (2 s burst,
2 legs):

| engine | solo drain | 2 legs @ **1.50×** | (D1i said, @2.34×) |
|---|---|---|---|
| `sherpa_kws` | 138 ms | **207 ms** | 323 ms |
| `streaming_ctc` | 304 ms | **456 ms** | 711 ms |
| **CTC penalty** | | **+249 ms** | +388 ms |

So the engine trade is **−69 % false alarms for +249 ms**, not +388 ms. The
operator has now been given three numbers for this term (+166 → +388 → +249);
the first was arithmetic, the second was measured on hardware we no longer run,
this one is measured on the hardware that will run the events.

**`num_threads=2` is no longer refuted.** It was 42 % slower on one physical
core because both threads landed on the same core; on two it is ~8 % faster.
⚠ That 8 % is **one sequential run each** and this box shows large run-to-run
variance — the *direction reversal* is the finding, the magnitude is not.
`num_threads` stays at 1 in every armed config until an interleaved A/B says
otherwise; the point is that the door D1i closed is open again.

### Why C is 1.50× and not 1.0× — chased, and it is NOT a lever

Each `num_threads=1` process runs at **185–203 % CPU**, which looked like ORT
ignoring `with_intra_threads(1)`. It is not. `top -H` on a live bench process:

```
4282 earshot  R  99.9%      <- main: ReplaySource WAV read + send_blocking
4285 detect   R  99.9%      <- inference
4284 chunker  S  10.0%
4283 preproc  R   0.0%
```

**One leg is ~2 vCPU by design** — `spawn_stages` runs preproc → chunker →
detect as three threads (`src/pipeline/mod.rs:93-118`) plus the source on main.
Two legs therefore want all 4 vCPU = both physical cores, and SMT delivers ~1.5×
not 2×. The 1.50× is explained, and there is no free vCPU to reclaim.

Two things this rules out, so nobody re-chases them:
- **ORT threading is not the dial.** `num_threads` moves one of four threads.
- **`spin_hot_rings` is not a latency lever here.** It replaces a park/unpark
  (~µs) with a busy-spin (~100 ns) per chunk. Against a **249 ms** drain that is
  noise, and it is refused in fanout mode by config validation anyway.

⚠ **Caveat on the contention factor itself.** In an unpaced bench the source
thread reads the WAV as fast as backpressure allows; on the armed path it is
bounded by network and decoder even during an 84.6× burst. So the bench's source
thread works *harder* than production's — **1.50× is a conservative (pessimistic)
bound on real contention**, which is the right direction for a safety number but
means it should not be quoted as the production figure.

## Session 2026-07-26 (D1i) — the box has ONE physical core. D1e's drain was understated by 2.3×, and threading makes it worse.

> ⚠ **SUPERSEDED for the hardware in use** — every number in this section was
> measured on `t3a.large` (1 physical core). The box is `t3a.xlarge` since
> 2026-07-27; see D1i-b directly above for the re-measured contention factor
> (1.50×) and the reversal of the threading refutation. The *reasoning* here
> stands; the constants do not.

D1e computed "+166 ms burst drain at 2 legs" from single-leg RTF and division by
2 cores. **Never measured.** Measured now — three runs, one call (INTC, 0.98 h):

| run | wall | vs baseline |
|---|---|---|
| A — 1 proc, `num_threads=1` | **538 s** | — |
| B — 1 proc, `num_threads=2` | **762 s** | **+42 % SLOWER** |
| C — 2 procs concurrent, `num_threads=1` | **1259 s** | **2.34×** |

### Root cause: `t3a.large` is 1 physical core, not 2

```
CPU(s): 2   Thread(s) per core: 2   Core(s) per socket: 1
/sys/.../cpu0/topology/thread_siblings_list = 0-1
```

**"2 vCPU" is one AMD EPYC core with SMT.** Two compute-bound legs do not run in
parallel — they contend for a single core. Credit throttling was ruled out first:
`CPUCreditBalance` is pinned at **864** (the maximum) across every datapoint and
the instance is in **`unlimited`** mode, so it never throttled.

### Two corrections to the record

**1. The burst drain is ~2.3× worse than reported.** Using the measured 2.34×
contention factor rather than assumed parallelism:

| engine | solo drain (2 s burst) | **2 legs, measured contention** |
|---|---|---|
| `sherpa_kws` | 138 ms | ~323 ms |
| `streaming_ctc` | 304 ms | **~711 ms** |
| **CTC penalty** | | **+388 ms**, not the +166 ms in D1e |

**So the engine trade is materially worse than I stated: −69 % false alarms for
+388 ms**, not +166 ms. It is still defensible — a false fire buys YES on a
market that settles NO — but the operator was given a number half the true size.

**2. "Armed-fanout CPU 33.7 % of the box" was wrong.** The arm measured 67.3 % of
*one vCPU*; with both vCPUs on a single physical core that is **~67 % of the real
compute**, not 34 %. There is far less headroom than D1f implied — and FOMC runs
43 keywords, the largest set staged.

### What this does to the speed plan

**Step 2b survives but changes shape.** Threading is refuted — `num_threads=2`
is 42 % *slower*, because both threads land on the same physical core. The lever
is **physical cores**: `t3a.xlarge` is 4 vCPU = **2 physical cores**, doubling
real compute for ~$0.15/h vs $0.075/h. On event days only, that is negligible
against the $60/month cap and it directly attacks a term now measured at +388 ms.

⚠ Same failure family as trap 9 and trap 10: a resource figure (`2 vCPU`) taken at
face value and converted into a latency claim without measuring. **Trap 24: vCPU
count is not core count — check `lscpu` before dividing by it.**

## Session 2026-07-26 (A3c) — corpus sourcing: BUY is $129/mo, SCRAPE is dead, and the answer is neither

A3b killed the DW plan (0/6 windows finance/policy). Before declaring the corpus
blocked, three sources checked:

| source | result |
|---|---|
| **DW World News**, 8 h capture | **0 %** finance/policy vocabulary in 6 sampled windows — documentary programming, not news |
| **Apple quarterly-earnings podcast RSS** (`rss.art19.com/apple-quarterly-earnings-call`) — the documented corpus-v1 method | **1 episode, 16 seconds, and it is a PSA.** The "available for two weeks" note is literal; right now the archive is empty |
| **EarningsCall API** (earningscall.biz) | **REJECTED ON ARCHITECTURE, not price** — it is downstream of the same webcast, so it can never be faster than our own leg. (For the record: audio only from the $129/month tier; transcripts below that, which cannot test an acoustic detector anyway.) |

**⛔ CLOSED ON ARCHITECTURE — do not revisit, for ANY purpose (operator, 2026-07-26).**
A recording service is **downstream of the same webcast we already consume**: it
ingests that feed, transcodes, stores, then serves. It is slower than our own leg
by construction. **You cannot buy your way upstream of the origin.**

That is not a pricing judgement, it is the same finding the batching-covariance
test already produced: slope **−0.084** against **+1.00** predicted for a
continuous feed ⇒ **the competitor is on the SAME segmented HLS we are, ~1 s
ahead.** There is no alternate transmission of an earnings call. **All edge comes
from consuming that one feed better**, which is what this entire campaign
measures.

Recorded here only so the source is never re-investigated. (Secondary point, now
moot: $129/month would also be more than double the standing $60/month AWS cap.)

### The answer costs nothing: we already generate the corpus by operating

**Every event we run banks ~1–1.5 h of real earnings-call audio on the exact
channel we trade.** That is where the current 5.15 h came from — AXP, VZ, INTC,
LMT were all captured from live arms, not downloaded. The corpus is a *by-product
of running events*, and it is the highest-quality adversary available: same codec,
same bitrate, same speakers, same acoustics.

Projected from the staged slate alone:

| | adds | running total |
|---|---|---|
| today | — | **5.15 h** |
| Tue 07-28 PayPal + Ford | ~2.5 h | ~7.7 h |
| Wed 07-29 FOMC (+ META/CVNA/WING if staged) | ~1.5–4 h | ~9–12 h |
| Thu 07-30 AMZN/RDDT/YUM/RACE | ~2–4 h | **~11–16 h** |

**~20 h is reachable in two to three weeks of ordinary operation, with no
purchase and no scraping.** The corpus blocker is not a blocker — it is a
*schedule*.

**Consequence for D2:** prefix-firing stays parked, and the honest reason is now
"the evidence to judge it will exist in ~2–3 weeks", not "we cannot get evidence".
The `panther`-is-a-standalone-word hypothesis waits for that corpus, where it can
be tested on data it was not derived from.

⚠ Do NOT re-run the sources above. Apple RSS is empty, EarningsCall needs a
purchase, DW is off-domain at this hour. **Capture every event instead.**

## Session 2026-07-26 (A3b) — the DW negative-control plan does NOT work: 0/6 windows are finance/policy speech

I wrote the prerequisite into the manifest myself — *no FA rate may be quoted
from this corpus until the news-vs-other split is known.* Measured it. The answer
kills the plan.

Six 60 s windows sampled evenly across the first 2.02 h of capture, transcribed
and scored against 27 finance/policy terms (market, economy, inflation, bank,
rate, dollar, tariff, trade, stock, earnings, growth, recession, budget, tax,
debt, fed, government, president, minister, election, policy, oil, gas, energy,
billion, million, percent):

| window | domain terms | content |
|---|---|---|
| 1 | **0** | *"Crises, conflicts, and their histories… brings maps to life"* |
| 2 | **0** | personal narrative |
| 3 | **0** | *"whoever I'll marry will be good and he'll be kind"* |
| 4 | **0** | peacekeeping / schools |
| 5 | **0** | architecture, *"stylised columns reminiscent of the 1…"* |
| 6 | **0** | *"That's all from Art Unveiled"* |

**0 of 6 windows, 0 domain terms across 604 words.** DW at this hour is
documentary programming, not news.

### The consequence: A3-via-DW does not deliver an adversary

`docs/BAKEOFF.md` §A3 specifies news/talk audio so the prefix gate faces *"a
realistic adversary"*. **The stream named "DW World News" is, at this hour, not
news.** A false-alarm rate measured on documentary narration about photography
and architecture says almost nothing about false alarms on an earnings call —
it would be flatteringly low and **must not be reported as a prefix-gate result**,
and never compared with the 0.78 FA/h measured on 5.15 h of real calls.

**This invalidates the plan, not just the number.** The capture continues (it is
free and disk is fine), and the full 8 h spans 06:12–14:12 UTC = 08:12–16:12 CEST,
which should include actual bulletins — so re-sample across the WHOLE capture
before writing it off entirely. But on the evidence so far the yield is low, and
**the corpus blocker is not solved by more DW.**

### What the corpus problem actually needs

Earnings-call audio, which is a **sourcing project rather than a loop iteration**:
~15 h more to make ≤0.5 FA/h distinguishable from 0.78. Per-company
earnings-call podcast RSS exists (the corpus-v1 method, `docs/BAKEOFF.md`) but one
company yields ~4 calls/year. **Flagged as blocked on sourcing, not on effort.**

⚠ Generalises past this campaign: **a negative control's validity is a property of
what is IN it, not of what the source is called.** Two separate checks tonight —
D1h's zero-detection scare and this one — came back to the same root: I assumed
the content of a stream from its name. Transcribe a sample first.

## Session 2026-07-26 (D1h) — FOMC config dry-run PASSES, and the zero-detection scare was resolved by transcript, not by debugging

The Wednesday config uses **`streamlink` legs**, a combination never run with the
CTC engine (PayPal's dry-run exercised `spec_edge`). Validated the same way:
`env -i`, real launcher, real event.

| check | result |
|---|---|
| `ORT_DYLIB_PATH` under `env -i` | ✅ |
| Kalshi validation, **all 43 markets** | ✅ `book snapshots live for all armed markets` |
| CTC detector per leg | ✅ arm at **67.3 % CPU**, matching PayPal's dry-run |
| fanout wiring | ✅ `OR at decision legs=2` |
| streamlink + ffmpeg chains | ✅ both alive, no stalls/drops/errors |
| SIGTERM | ✅ kill switch complete, ledger flushed, **0 survivors** |

### The scare, and why the cheap test beat the debugging instinct

**0 detections in ~3 minutes of DW with 43 Fed keywords** — including `president`,
`tax`, `oil`, `dollar`, `trump`. Those are near-certain in three minutes of world
news, so I called it a red flag and prepared to hunt a detection bug. **I was
wrong, and one transcript proved it.**

Transcribing the same stream over the same window (the negative-control capture is
recording it) shows DW was airing an **art-photography programme, a China
documentary trailer, a segment about hair, and a sports promo** — *"In Lutson's
photographs… 24 hours in Young China… let's talk about hair… Life driven by
sport."* **Not one Fed keyword was spoken.** Zero detections was the CORRECT
answer. It also retro-validates the PayPal dry-run's zero, which I had explained
away as rare vocabulary — the explanation was right for the wrong reason.

### ⚠ The finding that actually matters: the news corpus is a WEAKER adversary than its manifest claims

`corpus/manifests/dwnews-negctl-20260726.toml` says news audio gives the prefix
gate *"a realistic adversary"*. **DW World News is not continuous news.** In the
sampled window it ran documentary, lifestyle and sports programming. An art
documentary about photography is not adversarial for earnings-call keywords in
any meaningful sense.

So the 8 h capture is **not 8 h of dense financial speech**; an unknown fraction is
vocabulary nowhere near the target domain. **A false-alarm rate measured on it will
be optimistically low, and must not be compared like-for-like against the 5.15 h
earnings-call figure of 0.78 FA/h.** Same failure family as the zero-occurrence
caveat: the validity of a negative control depends on what is actually *in* it,
not on what the source is called.

**What to do about it:** when the capture lands, transcribe a sample of windows
and estimate the news-vs-other split before quoting any rate — or restrict scoring
to windows verified as news. Reporting a blended figure would be manufacturing a
better number and calling it evidence.

## Session 2026-07-26 (D2b) — prefix gate FAILS at 0.78 FA/h on 2× the corpus. Prefix-firing stays parked.

`configs/bakeoff-prefix-only.toml` (the 7 keywords carrying a `fast_prefix`) over
the doubled wideband dev corpus, scored per channel by
`scripts/prefix-gate-score.py`.

| tier | TP | miss | FA | hours | **FA/h** | bar ≤0.5/h |
|---|---|---|---|---|---|---|
| **Fast** | 5 | 18 | **4** | 5.15 | **0.78** | **FAIL** |
| Confirmed | 6 | 17 | 0 | 5.15 | 0.00 | — |

| Fast false alarm | call | assessment |
|---|---|---|
| `venture fund` | INTC | must-verify — plausible on an Intel call |
| `venus aerospace` | INTC | real |
| **`panther lake`** | **LMT** | real |
| **`panther lake`** | **VZ** | real |

**The verdict does not depend on the ambiguous one.** Crediting `venture fund` as
a true positive gives 3 FA = **0.58/h** — still a fail. Passing needs ≤2 FAs in
total. **Prefix-firing stays parked, as pre-registered. The bar is not
renegotiated.**

Worth noting the direction: **1.19/h at 2.53 h → 0.78/h at 5.15 h.** Doubling the
corpus moved the estimate down by a third, which says the earlier figure was
noisy-high — exactly why the bar could not be judged at 2.53 h. It is still a
fail, and now on twice the evidence.

### The failure is CONCENTRATED, and that suggests a rule

**`panther` alone accounts for 2 of the 4 false alarms**, firing on both LMT and
VZ — calls that cannot contain an Intel product name. Look at the seven prefixes:

| prefix | standalone English word? |
|---|---|
| `basic ec`, `client comp`, `golden d`, `venture f`, `venus a`, `world c` | no — fragments |
| **`panther`** | **yes** |

**Candidate a-priori rule: a `fast_prefix` must not be a complete standalone
word.** A fragment cannot be uttered by itself; a whole word can, and then the
prefix fires on a legitimate use of an unrelated word.

⚠ **I am NOT claiming this rescues the gate, and it must not be applied to this
corpus as a pass.** The rule is checkable without looking at outcomes — but I
formed it *after* seeing which prefix failed, which is the same
selection-on-the-outcome trap that made "keep only the zero-FA prefixes" invalid
in D2. Dropping `panther` here would leave 2 FAs = 0.39/h and *look* like a pass
**by construction**. The rule is a hypothesis for the NEXT corpus, not a verdict
on this one.

### What would actually settle it

More audio the rule was not derived from. At 5.15 h the bar permits 2.6 false
alarms in total, so even a pass would be a loose bound. The corpus needs to reach
~20 h of **wideband** for 0.5/h to be distinguishable from 0.78/h with any power.

## Session 2026-07-25 (F3) — the multi-IP vantage race is REFUTED BY ARCHITECTURE, not by measurement

F3 was written as *"resolve all A records for the CDN host, fetch the same
predicted segment from N distinct edge IPs concurrently, take first."* DNS
answers whether that is even possible, before any probing:

| host | CDN | A records |
|---|---|---|
| `dwamdstream102.akamaized.net` | Akamai | **2** (`189.216.110.16`, `.24`) — same `/24` |
| `manifest.prod.boltdns.net` | **Fastly** | **1** (`199.232.158.27`) |
| `fastly-signed-us-east-1-prod.brightcovecdn.com` | **Fastly** | **1** (`146.75.94.27`) |

**Fastly is anycast.** One address, and which POP you actually reach is decided
by BGP from your network location — not by which IP you dial. There is no second
IP to race. Both Brightcove hosts (the FOMC venue) are Fastly, so the experiment
has nothing to vary there. Akamai returns two addresses but they sit in the same
`/24`, i.e. two machines in one POP, not two POPs.

**⇒ You cannot choose a POP from a single vantage point on these CDNs.** The
"N distinct edge IPs" design cannot work, and no amount of probing would have
made it work — this is a property of how the CDNs are addressed.

**What survives is the expensive version:** reach a *different POP* by being in a
different network location, i.e. a **second-region box**. That is in scope under
the $60 cap as provision → run → terminate, but it is a different, costlier
experiment than the one queued, and it should be justified before it is spent —
the bar (≥200 ms median spread) is unchanged.

⚠ **Scope:** resolved from the CDMX laptop. Akamai and Fastly both return
location-appropriate answers, so the box in us-east-1 will see *different
addresses* — but the **counts** are the load-bearing part, and Fastly's single
anycast address is architectural, not locational. Worth re-running from the box
to confirm the Akamai count; it would not change the Fastly conclusion.

## Session 2026-07-25 (F2) — RESOLVED: segments are COMPLETE ON PUBLISH. Progressive consumption is impossible here.

The ledger has carried *"speculative **chunked** fetch — untested"* for the whole
campaign, against S3's competing cache-TTL model which predicts the object simply
404s until it is whole. Opposite consequences, never separated. `scripts/f2-probe.py`
separates them at the instant N+1 first returns 200.

| run | `Content-Length` | `Transfer-Encoding` | verdict |
|---|---|---|---|
| 1 | **714 776** | *(none)* | complete |
| 2 | **523 580** | *(none)* | complete |
| 3 | **654 428** | *(none)* | complete |

**Every run returns a full `Content-Length` and no chunked encoding.** A response
that is still being written cannot declare its length — so the segment exists
only once whole, and the header alone is decisive. This is categorical, not
statistical: three replications guard against a fluke, not against sampling
error.

### What this settles

1. **The "untested candidate" is CLOSED as impossible** at this origin class.
   There is no in-progress segment to stream, so a chunked speculative fetch has
   nothing to fetch.
2. **`--stream-body`'s null is now EXPLAINED, not merely observed.** It was
   refuted empirically weeks ago and defaulted off; the reason is that the bytes
   it wanted to stream do not exist until the whole object does.
3. **S3's cache-TTL model wins over the chunked-transfer hypothesis.** That model
   already made one correct out-of-sample prediction (S5's N+2 null); this is the
   second, and it was the discriminating case.

⚠ **Scope: DW/Akamai only.** Amazon IVS (PayPal's venue class) and
Brightcove/boltdns (FOMC) have **not** been probed this way — `f2-probe.py` takes
a manifest URL, so run it day-of alongside `manifest-probe.sh`. An origin that
*did* stream partials would be a genuine lever worth a segment, so this is worth
re-asking per venue rather than assuming the answer generalises.

⚠ The `header→body` timings (884 ms for 715 KB) are CDMX-laptop link speed, not
an origin property, and carry no information about streaming. Only the presence
of `Content-Length` does.

## Session 2026-07-25 (D1g) — FIRING GAP CLOSED: 6/6 keywords → correct decision intents

D1f proved the plumbing but nothing fired, so detection → decision had never
been exercised under fanout + CTC. Closed by planting known keywords in the
audio: a 19.2 s TTS clip speaking six of PayPal's actual market words inside
plausible earnings-call sentences, added as a **third replay leg** alongside the
two live pipe legs, with the real event and real Kalshi markets.

| spoken | market ticker | decision intent |
|---|---|---|
| stablecoin | `…-STAB` | ✅ |
| blockchain | `…-BLOC` | ✅ |
| bitcoin | `…-BITC` | ✅ |
| regulation | `…-REGU` | ✅ |
| braintree | `…-BRAI` | ✅ |
| agentic commerce | `…-AGEN` | ✅ |

**6 planted, 6 detected, 6 routed to the correct market**, all `tranche=1
tier=Confirmed`, plus 2 `detection ignored (keyword state machine)` — the
one-shot dedupe working as designed. Shutdown clean; journal shows
`ledger_spent_cents: 0`, `session_order_ids: []`, `terminated: SIGTERM`.

**What this validates that nothing else did:** keyword text → CTC BPE targets →
frame scorer → detection → decision table → market ticker → intent, under the
real arm, real launcher, real Kalshi markets, with two other legs competing.
Every stage between the config and the order path is now exercised.

**What it does NOT validate:** TTS is clean studio speech, not a compressed
77 kbps webcast of a CFO on a bad line. It proves the path is *wired*, not that
recall on real audio is 6/6 — the holdout (D1d, recall 0.833) is the honest
recall figure.

## Session 2026-07-25 (D1d) — HOLDOUT: the CTC accuracy win GENERALISES, and is bigger than dev

Unseen audio (AAL, 1.51 h), run **once**, config frozen — BAKEOFF discipline.
With the latency case refuted in D1c, accuracy was the entire argument, so this
was the decisive test.

| metric | `sherpa_kws` | `streaming_ctc` | delta |
|---|---|---|---|
| true positives | 15 | **15** | **0** |
| misses | 3 | **3** | **0** |
| recall | 0.833 | **0.833** | **0.000** |
| **total detections fired** | **41** | **23** | **−18** |
| **FA/h** | **17.21** | **5.30** | **−69 %** |
| `consumed_lag` p50 | 540 ms | **540 ms** | **0 ms** |
| RTF | 0.0688 | 0.1524 | 2.2× |

### It catches exactly the same words and fires 18 fewer times

**Identical true positives, identical misses, identical recall — and 18 fewer
firings.** There is no recall cost at all on unseen audio: the entire difference
is false alarms. `−69 %` on holdout against `−38 %` on dev, so the effect is not
only reproduced, it is **larger on the split that counts**.

### The dev latency regression did NOT reproduce

D1c measured `consumed_lag` +50 ms on dev. On holdout it is **+0 ms** (540 vs
540). Two splits, two different answers ⇒ **the +50 ms is call-dependent noise,
not a property of the engine.** Honest statement: **no consistent per-detection
latency penalty; bound it at ≤+50 ms rather than claiming zero.** n = 15 TPs on
holdout, so this is a small sample and should not be over-read either way.

### What still costs, and it is the only thing that does

The `2.2× RTF` is unchanged and real, and via D1e it converts to burst drain:

| legs | `sherpa_kws` drain | `streaming_ctc` drain | penalty |
|---|---|---|---|
| 1 | 138 ms | 304 ms | **+166 ms** |
| 3 (armed fanout) | 207 ms | 455 ms | **+249 ms** |

> ⚠ The multi-leg row here was **assumed** parallelism, not measured. D1i
> measured it (2.34× on 1 physical core) and **D1i-b re-measured it on the
> hardware now in use** (`t3a.xlarge`, 2 physical cores): contention **1.50×**,
> 2-leg drain **207 / 456 ms**, penalty **+249 ms**. The current number is
> D1i-b's; that this row lands on the same +249 ms is a coincidence of a
> different leg count and a different contention factor, not a confirmation.

**That is now the whole cost of the swap** — and it is partly a *configuration*
choice, not a fixed property: the fanout ORs at decision, so leg count is a dial.

### Recommendation

**Adopt `streaming_ctc`**, on this reasoning:

1. **It wins on the axis that loses money.** A false alarm buys YES on a market
   that settles NO — a direct loss every time. 17.21 → 5.30 FA/h is ~26 → ~8
   false fires on a 1.5 h call. A slow fire costs a race; a false fire costs
   premium, and there are far more of them.
2. **It costs no recall whatsoever** on unseen audio — same 15 catches, same 3
   misses.
3. **Its latency cost is bounded and mostly configurable**: ≤+50 ms per detection
   (not consistently present), plus 166–249 ms of burst drain that shrinks with
   leg count.
4. **It unlocks the only fix for the structural collisions.** `sherpa_kws` has no
   per-hit confidence (Q-M1 pin 2: constant 1.0), so `record` ← "quarter over
   quarter" can only be handled by forfeiting the market. `streaming_ctc` exposes
   frame posteriors, which is what a negative-context rule requires.

⚠ **Against it, stated plainly:** the drain penalty lands on the critical path in
exactly the 25 % of words that E0 showed already pay a full segment, so the worst
case gets worse. If the operator's priority is the race above all else, staying on
`sherpa_kws` is defensible — but then the 17 FA/h stays too.

## Session 2026-07-25 (F1) — first variant audit on a REAL venue class: segment-duration axis NULL, but 3 AUDIO-ONLY renditions exist

`scripts/variant-audit.py` had never run against a venue we actually trade.
Ran it on **Brightcove/boltdns — the FOMC venue** (Wed 07-29, the crown target),
using the archived 2026-06-17 presser (same account `66043936001`; rendition
structure is a property of the encoding profile, so it transfers even though the
archive is VOD).

**10 renditions where our tools have only ever read 1:**

| kind | name | segment | packaging |
|---|---|---|---|
| **media** | **AUDIO en (Main)** ×3 | 9.92 s | TS |
| media | SUBTITLES en | 30.0 s | VTT |
| variant | 559 900 – 2 336 400 bps, 480x270 → 1280x720 (×6) | 10.01 s | TS |

### Verdict on the pre-registered bar: NULL on the axis it was written for

The bar was *"≥250 ms median advantage of the best rendition over the default"*.
**Every rendition here is ~10 s — no rendition is shorter, so that axis is NULL
at this venue.** No `EXT-X-PART`, no `SERVER-CONTROL`, no PDT: standard HLS, and
the first-variant probe was not missing anything on cadence.

### But the audit found a different lever the bar did not anticipate

**Three audio-only renditions exist, and we decode audio only.** Today's legs
pull a *video* variant — 560–2336 kbps of video bytes fetched, decoded, and
discarded. The audio rendition carries the same speech at a fraction of the size.

That is **not** a segment-duration win and must not be scored as one. It is a
*download-duration* win: E0 measured segments arriving as bursts (a 2 s segment
delivered in 24 ms at AXP), and burst duration scales with bytes. Smaller segment
⇒ shorter burst ⇒ audio in the ring sooner, additively on the critical path.
**Size unmeasured — this is a new sub-experiment (F1b), not a result.**

### F1b — the audio-only rendition is 5–19× smaller, MEASURED

Same 10 s of the same press conference, first segment of each, byte-exact:

| rendition | bytes | bitrate | vs audio-only |
|---|---|---|---|
| **AUDIO en (Main)** | **177 848 B** | 143 kbps | — |
| video 559 900 bps | 914 808 B | 731 kbps | **5.1×** |
| video 1 455 300 bps | 2 108 984 B | 1686 kbps | **11.9×** |
| video 2 336 400 bps | 3 363 320 B | 2688 kbps | **18.9×** |

**Why we are paying this today.** The webcast legs point at the *master*, and
ffmpeg resolves a master by selecting a **video** variant — which in turn
references the audio group, so the leg fetches video segments *and* audio
segments and throws every video byte away after decode. Pointing the leg at the
**audio rendition playlist directly** removes 5–19× of the bytes with **no change
to the audio** — the same encoded track either way.

### F1c — MEASURED from us-east: the byte saving buys ~10 ms. My framing was wrong.

Same two segments, fetched three times each from the box against this CDN:

| rendition | bytes | **TTFB** | **total** | transfer (total−TTFB) |
|---|---|---|---|---|
| audio-only | 177 848 | 51.1 ms | **54.8 ms** | 3.7 ms |
| video variant | 1 359 992 | 55.3 ms | **65.1 ms** | 9.8 ms |
| **saving** | **7.6× fewer bytes** | 4.2 ms | **≈ 10 ms** | 6.1 ms |

**A 7.6× byte reduction buys ~10 ms, not the hundreds I implied.** The fetch is
**round-trip-dominated, not bandwidth-dominated**: TTFB is ~52 ms and the box
pulls 18–22 MB/s, so 1.36 MB transfers in under 10 ms of actual wire time. Bytes
were never the constraint on this link.

**Verdict: the audio-rendition lever is REAL but SMALL (~10 ms, ~0.5 % of a 2 s
segment).** Still worth taking — it is a one-line config change with identical
audio and it also spares ffmpeg the work of demuxing and discarding video, which
matters more now that D1e showed detector CPU lands on the critical path — but it
is **not** a latency lever of consequence and must not be sold as one.

This is why F1b claimed the ratio and refused the millisecond figure. Had I
converted bytes to time by assuming throughput was the constraint, I would have
reported a ~150 ms win that does not exist.

### F1c — and the fastly_token is NOT IP-bound (Wednesday go/no-go)

The token was minted for the **laptop's** session in CDMX; both fetches returned
**HTTP 200 from the us-east box**. So a URL surfaced on the logged-in laptop can
be handed to the box and consumed there. **This clears the FOMC staging path**
that the runbook flagged as unknown — the box does not need its own session, only
a freshly-surfaced URL. Token *lifetime* is still unmeasured and remains the
day-of risk.

⚠ **Superseded framing below — kept because the reasoning error is instructive.**

⚠ **The time this saves is NOT measured and must not be assumed.** Bytes convert
to milliseconds only through throughput, and the measurement above was taken from
the CDMX laptop, not the us-east box whose link is far better. E0 showed segments
arriving in bursts whose duration scales with bytes (2 s delivered in 24 ms at
AXP), so the effect is real in direction and additive on the critical path — but
its size needs a box-side timing test against this CDN. **Claim the ratio; do not
claim a latency number yet.**

This is a lever that was invisible for the whole campaign purely because nothing
ever enumerated the renditions, and it is a one-line config change rather than
new machinery.

### Operationally the most valuable part — a faster FOMC surfacing path

The runbook lists two ways to get Wednesday's tokenized master: the Playback API
(needs `policy_key` from player JS) or a Chrome network capture of the m3u8. A
third, found here and faster than both: **the Brightcove metrics beacon carries
the full tokenized master URL in its `media_url` query parameter** —
`metrics.brightcove.com/v2/tracker?…&media_url=https%3A%2F%2Fmanifest.prod.boltdns.net%2F…master.m3u8%3Ffastly_token%3D…`
— and it fires on `video_impression`, i.e. as soon as the player initialises,
before segment fetching settles. It also carries `video_duration`, the video id,
and the event title, so the right asset can be identified unambiguously.

⚠ **VOD caveat:** this is the `/10s/` archive path. The LIVE presser may publish a
different cadence and profile. **Re-run `variant-audit.py` + `manifest-probe.sh`
day-of** — this establishes the venue's *shape*, not Wednesday's numbers.

## Session 2026-07-25 (D1c) — the CTC latency win was NOT REAL: it REVERSES to −50 ms on the comparable metric

Head-to-head re-run with `audio_consumed_ms` (D1c instrument), dev split,
2.53 h, chunk 160, same config — only the engine varies.

| metric | `sherpa_kws` | `streaming_ctc` | delta |
|---|---|---|---|
| `emit_lag` p50 — **engine-defined, NOT comparable** | 520 ms | 220 ms | *−300 ms* |
| **`consumed_lag` p50 — COMPARABLE** | **520 ms** | **570 ms** | **+50 ms** |
| **`consumed_lag` p95 — COMPARABLE** | **640 ms** | **755 ms** | **+115 ms** |
| true positives | 49 | **50** | +1 |
| total detections fired | 102 | **83** | **−19** |
| recall | 0.636 | **0.649** | +0.013 |
| FA/h | 20.97 | **13.06** | **−38 %** |
| RTF | 0.0689 | 0.1518 | **2.2×** |

### The headline: the sign flips

**`emit_lag` said CTC was 300 ms faster. The comparable metric says it is 50 ms
SLOWER.** The entire apparent win — and 50 ms more — was instrument.

**Artifact size: 350 ms**, all of it on the CTC side (`emit_lag` 220 vs
`consumed_lag` 570). `sherpa_kws` reads 520/520, identical by construction since
its position source already *is* the fed-sample counter — which is the internal
validation that the new metric behaves as designed.

⚠ **My pre-registered estimate of the artifact was 210 ms** (≈80 ms half-chunk +
≈130 ms right-context from `VERIFICATIONS.md` Q-M1). **Measured, it is 350 ms.**
So the ONNX-metadata right-context figure understates real consumption by ~140 ms:
this model consumes ~350 ms of audio beyond the frame it scores on, not ~130.
Correcting analytically would have produced "CTC wins by ~90 ms" — still wrong,
and wrong in the flattering direction. **The measurement was necessary; the
estimate was not sufficient.**

### What survives, and what does not

- **REFUTED: the CTC latency advantage.** It is a 50 ms regression at p50 and
  115 ms at p95. Against E0's law this *raises* segment-penalty exposure at a 2 s
  venue from `520/2054 = 25.3 %` to `570/2054 = 27.7 %`.
- **STANDS: the accuracy advantage**, untouched by any of this. CTC fires **19
  fewer times** and catches **one more** — raw counts, window-independent. FA/h
  **20.97 → 13.06 (−38 %)** with recall slightly up.
- **STANDS: the 2.2× CPU cost.**

### The CPU cost is NOT just CPU — it lands on the critical path (never priced before)

`2.2× RTF` sounds like a capacity question. Under **armed 3-leg fanout** it is a
latency question, because E0 showed audio arrives in bursts and the detector must
drain each burst before a word late in it can fire (E0b's queue-drain term).

| engine | CPU per leg per burst | 3 legs on 2 cores | **drain (wall)** |
|---|---|---|---|
| `sherpa_kws` | 138 ms | 413 ms | **207 ms** |
| `streaming_ctc` | 304 ms | 911 ms | **455 ms** |

**+249 ms of extra drain**, worst case (word at the end of a burst), on top of the
measured `consumed_lag` regression. Total added latency for the swap under armed
fanout: **≈ +300 ms at p50**, not the +50 ms the bench alone suggests.

Assumptions stated so they can be attacked: `num_threads = 1` per leg (as
configured), perfect parallelism, no other load, AXP-class 2 s bursts. **Real
drain can only be worse than this.** Averaged over a call the duty cycle is
comfortable (23 % of 2 vCPU) — it is the *burst* drain that lands on the critical
path, and averages hide it completely.

### The trade, restated honestly

Adopting `streaming_ctc` now buys **−38 % false alarms and +0.013 recall** and
costs **+50 ms latency (p50), +115 ms (p95), and 2.2× CPU**. It is an
accuracy-for-latency trade, **not** the free win the raw numbers implied.

**This changes the basis of a decision already taken.** The operator approved
pursuing the engine swap on accuracy and CPU grounds (the latency figure was
correctly withheld as confounded at the time) — that premise survives, but the
newly measured latency *cost* is adverse information that did not exist when the
decision was made, and is surfaced here rather than absorbed silently.

⚠ **Third instrument-artifact catch of this campaign**, after S4's keep-alive
−250 ms and E0c's byte-identical chunk cells. All three were self-consistent,
plausible, and would have been believed.

## Session 2026-07-25 (D1b) — streaming_ctc BEATS sherpa_kws on accuracy outright; the latency win is real but PARTLY CONFOUNDED  [LATENCY CLAIM SUPERSEDED BY D1c ABOVE — it reversed]

First run of the `streaming_ctc` engine on this box against real corpus audio,
using the pinned base export `sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18`.
Same corpus/split/chunk/config as the `sherpa_kws` baseline (dev, INTC+LMT,
2.53 h, chunk 160).

| metric | `sherpa_kws` | `ctc-small` | delta |
|---|---|---|---|
| `emit_lag` p50 | 520 ms | **220 ms** | **−300 ms** |
| `emit_lag` min / max | 260 / 830 | **60 / 400** | |
| recall (Confirmed) | 0.636 | **0.649** | +0.013 |
| FA/h | 20.97 | **13.06** | **−38 %** |
| true positives | 49 | **50** | +1 |
| **total detections fired** | **102** | **83** | **−19** |
| RTF | 0.0693 | 0.1519 | **2.2× the CPU** |

### The accuracy result is CLEAN and it is the important one

**`ctc-small` fired 19 fewer times in total and still caught one more true
positive.** That is window-independent — a raw count of firings against a raw
count of hits — so it cannot be explained by position precision. Strictly better
precision at equal-or-better recall, on the very axis (`20.97 FA/h`) that has
been the detector's standing weakness.

### The latency result is CONFOUNDED — do not quote −300 ms

The two engines do not report the same quantity, and the difference favours CTC:

| engine | `audio_pos_ms` source | measured grid | bias |
|---|---|---|---|
| `sherpa_kws` | `samples_fed` at decode (`sherpa_kws.rs:92`) | **160 ms** (GCD of FA positions) | **overstates** by 0–160 ms (mean ~80) |
| `streaming_ctc` | `fidx × frame_shift` (`streaming_ctc.rs:334`) | **40 ms** (GCD of FA positions) | **understates** by the encoder's right-context (~130 ms, Q-M1) |

Net artifact ≈ **80 + 130 = 210 ms in CTC's favour**, so the honest reading of the
300 ms is roughly:

> **`A` improves by ~90 ms, bounded ~90–300 ms. NOT RESOLVABLE more tightly
> without an instrument fix.**

~90 ms is still worth having — at AXP's 2054 ms segments it moves exposure from
25.3 % to ~21 % — but it is **not** the campaign-winning 300 ms the raw table
suggests. Recorded this way deliberately: S4 (keep-alive) already produced a
confident, self-consistent, *wrong* −250 ms from exactly this failure mode, an
instrument comparing two different quantities.

**Instrument fix that would resolve it (D1c):** make both engines report a
comparable "audio consumed when the fire happened" — i.e. have `streaming_ctc`
also stamp the chunk's cumulative sample position alongside `fidx`, keeping the
frame-precise value for match scoring. Until then the accuracy comparison stands
and the latency comparison is a bound.

### Cost

RTF 0.152 vs 0.069 — **2.2× the CPU**. Three concurrent legs would put the box at
~0.46 aggregate RTF on 2 vCPU: feasible, but the headroom that made leg fanout
free is halved. Worth pricing before arming CTC in a fanout posture.

**This is the BASE export.** The chunk-8/16 re-exports D1 proper is blocked on
target the *quantum* specifically, so they attack the term this one did not.

## Session 2026-07-25 (D1) — the chunk-8/chunk-16 exports DO NOT EXIST

The campaign queue said *"`models/streaming-ctc-en-20260723-chunk8` is on the box,
never benched"*. **It is an empty directory.** So is chunk-16:

| dir | files | size |
|---|---|---|
| `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01` | 15 | 20 M |
| `streaming-ctc-en-20260723-chunk16` | **0** | 4.0 K |
| `streaming-ctc-en-20260723-chunk8` | **0** | 4.0 K |

Created 2026-07-24 02:18 and never populated. The base export
`sherpa-onnx-streaming-zipformer-ctc-small-2024-03-18` that
`configs/bakeoff-webcast.toml` points at is **also absent** — the box has no
streaming-CTC model at all. `streaming_ctc` has therefore never run on this box
against real corpus audio, and the plan's cost estimate for D1 ("bench it") was
wrong by a whole workstream.

**What D1 actually costs.** `scripts/export-english-streaming-ctc.md` states
plainly: *"This can't run on the EARSHOT box (no PyTorch/k2 + disk). Run it where
a GPU + icefall/k2 + torch are installed, then copy the model dir back."* So the
chunk-8/16 comparison is a GPU export project, not a bench — in scope under the
raised $60 cap as a provision→run→terminate burst, but not a loop iteration.

**What is measurable today (D1b, running).** The base English streaming-CTC
export is pinned with a sha256 in `scripts/fetch-models.sh` and is CPU-only. It
is **not** a foregone conclusion against `sherpa_kws`, because `A` is quantum
*plus* acoustic lookahead and the two models trade them differently:

| engine | decode quantum | lookahead / right-context | `A` |
|---|---|---|---|
| `sherpa_kws` gigaspeech | **160 ms** (measured, E0d) | ~320 ms (Q-M1) | **520 ms** (measured) |
| `ctc-small` 2024-03-18 | 320 ms (ONNX meta, Q-M1) | ~130 ms (Q-M1) | **?** |

A coarser quantum with a much shorter right-context could land either side of
520 ms. That is the number D1b is after.

## Session 2026-07-25 (D2) — prefix-firing buys ~320 ms, FAILS its precision bar, and is BLOCKED ON CORPUS

`configs/bakeoff-webcast-prefix.toml`, `sherpa_kws`, chunk 160, dev split — same
corpus/split/chunk as E0c so the Confirmed rows compare directly. Two spotters
(Fast + Confirmed), hence 1229 s vs the 630 s single-spotter baseline.

### The speed side — measured, and real

Within-run, same-phrase Fast-vs-Confirmed `emit_lag`. This is the honest metric:

| phrase | prefix | Fast `emit_lag` | Confirmed `emit_lag` | **gain** |
|---|---|---|---|---|
| client computing | `client comp` | **−100 ms** | 540 ms | **+640 ms** |
| panther lake | `panther` | 240 ms | 560 ms | **+320 ms** |
| golden dome | `golden d` | 290 ms | 400 ms | **+110 ms** |

**Median gain +320 ms (n=3, range +110..+640)** — consistent with the 300–500 ms
this was parked at, now measured rather than estimated. `client computing` went
**negative**: it fired *before the phrase finished*.

⚠ I had been saying "a win means `emit_lag` goes negative". Corrected **before**
seeing this: a prefix fire still pays the engine's ~520 ms lookahead after the
*prefix* ends, so negative requires the skipped tail to exceed 520 ms. Two of the
three stayed positive and were still wins. Had I kept the wrong criterion I would
have scored two real wins as failures.

### The precision side — FAIL, replicating the prior number exactly

| tier | TP | FA | **FA/h** | bar | verdict |
|---|---|---|---|---|---|
| Fast | 5 | 3 | **1.19** | ≤0.5 | **FAIL** |
| Confirmed | 49 | 53 | 20.97 | — | **no regression** vs E0c (49/53 identical) |

**1.19/h reproduces the parked figure to the digit.** The two-spotter isolation
is clean — Confirmed is bit-for-bit what it was without prefixes.

### Why the obvious remedy is NOT honest here

Collision screen on the real transcripts: the three Fast false alarms are
`panther lake`, `venture fund`, `venus aerospace`, all classed **PHONETIC** —
not `SUBSTRING`. So the screen gives **no principled a-priori reason** to drop
them; it says "review, often threshold-fixable". It does not rescue D2.

Applying "eligible only if no collision" would keep `basic economy`,
`client computing`, `golden dome`, `world cup` → Fast FA **0**, 0.00/h, a pass.
**That pass would be an artifact.** Those four are *defined* as the ones with no
false alarms on this corpus; scoring them on the same corpus is selection on the
outcome. It passes by construction.

### The binding constraint is the corpus, not the technique

| problem | number |
|---|---|
| prefixed phrases with **zero occurrences** in dev | **4 of 7** (basic economy, venture fund, venus aerospace, world cup) |
| `venture fund` / `venus aerospace` | produced false alarms while having **no true occurrences at all** |
| true Fast recall over prefixed phrases | **5/23 = 0.217** (the 0.065 headline counts all 77 occurrences including 34 keywords that have no prefix — meaningless) |
| what the bar permits at 2.53 h | **1.26 false alarms total** |

At this corpus size **≤0.5/h cannot be distinguished from 0.8/h**. And the
holdout would not fix it: of the four eligible prefixes only `basic economy`
plausibly occurs on AAL, so a holdout "pass" would mostly measure three prefixes
having no chance to fire. Burning the once-only holdout on that would spend it
for nothing.

**Verdict: D2 FAILS its pre-registered bar (1.19/h vs ≤0.5/h) and prefix-firing
stays parked — as pre-registered, no renegotiation.** But the reason it cannot
yet be rescued is that the false-alarm corpus is too small and too poorly matched
to the prefixed phrases to resolve the bar at all. **A3 (expand the FA corpus) is
therefore promoted from housekeeping to a blocking dependency for D2.** The
+320 ms is real and says the technique is worth unblocking.

## Session 2026-07-25 (S6) — join-lottery characterised at n=16–21

Ten consecutive 150 s runs pooled with every earlier `abs-lag.py` observation.
Characterisation only — no pre-registered bar.

| leg | n | median | IQR | min | max | **spread** | stdev |
|---|---|---|---|---|---|---|---|
| prod_tuned | 21 | 17.06 s | 1.98 | 12.01 | 19.23 | **7.22 s** | 1.92 |
| prod_streamlink | 19 | 17.28 s | 1.55 | 13.39 | 19.23 | **5.84 s** | 1.50 |
| **spec_edge** | 16 | **12.39 s** | **0.71** | 11.71 | 13.01 | **1.30 s** | **0.40** |

**`spec_edge` is 4.67 s fresher at the median and 5.6× tighter in spread**
(stdev 0.40 s vs 1.92 s). Both production legs carry a 6–7 s join lottery that
persists for a whole event; the speculative leg does not, because it continuously
re-aims at the newest segment rather than living with wherever it happened to
join.

> **⚠ CORRECTION.** On the n=6 sample I wrote that `spec_edge`'s *worst* draw beat
> the production legs' *best-ever* draw "by a hair". At n=16 that is **false**:
> worst `spec_edge` is 13.01 s, best `prod_tuned` is 12.01 s — **the
> distributions overlap.** The separation is large and consistent in the medians,
> but it is not the clean no-overlap dominance the small sample suggested. Ninth
> claim of this campaign corrected by a larger measurement.

Consistent across sampling windows: within the 10-run batch alone, spreads were
`spec_edge` 1.30 s, `prod_tuned` 2.78 s, `prod_streamlink` 5.23 s.

## Session 2026-07-25 (S7 re-validation) — PASSES: 46 transient 404s, all recovered, zero loss

Second 90-minute run with the retry fix (10:20 → 11:50Z, DW, 3 legs).

| leg | audio emitted | vs 5400 s wall clock |
|---|---|---|
| prod_tuned | 5345.9 s | **−54.1 s** |
| prod_streamlink | 5398.4 s | −1.6 s |
| **spec_edge** | **5410.4 s** | **+10.4 s** (join backlog drained) |

| fetcher counter | before fix | after fix |
|---|---|---|
| `seg_err` (permanent loss) | 17 | **0** |
| `seg retry` (recovered) | — | **46** |
| `seg LOST` | — | **0** |
| `dropped` (catch-up) | 0 | **0** |
| `short` | 0 | 0 |

**BOTH bar conditions met: `seg_err=0` AND no audio deficit vs `prod_tuned`.**
**S7 IS GREEN** — the drop-safety gate that blocked arming the speculative
posture is cleared.

**46 transient 404s — ~276 s of call audio — were recovered.** Under the old code
every one would have been a permanent ~6 s gap. The 404 rate is variable (17 in
the first run, 46 in the second, same stream and duration), which is itself worth
knowing: the exposure is not a fixed small number.

**No duplication risk from the retry.** In the default buffered path
`fetch_segment` calls `get()` to completion *before* writing anything, so a 404
raises before a single byte is emitted — a retry cannot double-emit. (The
`--stream-body` path can write partially, but mid-body failures return `short`
rather than raising, so they are not retried.)

**Unexpected result worth flagging: `spec_edge` was the only leg to emit full
wall-clock audio.** `prod_tuned` — the *fallback* leg in the armed spec template
— came up **54 s short** with 829 stalls. That is the same leg already retracted
from "deterministic joiner" to "largest join lottery of the three" (7.22 s
spread). It is a backstop for *diversity*, not for reliability, and this run is a
third independent mark against treating it as the safe option.

## Session 2026-07-25 (S7 first run) — 90-min drop-safety FAILED: ~86 s of audio silently lost

The gate that blocks arming the speculative posture. **90 minutes, DW, 3 legs**
(08:46:50 → 10:17:08Z).

| leg | audio emitted | vs `prod_tuned` |
|---|---|---|
| prod_tuned | 5387.9 s | — |
| prod_streamlink | 5391.9 s | +4.0 s |
| **spec_edge** | **5302.0 s** | **−85.9 s** |

**The thing the gate was written to test PASSED: `dropped=0`.** The
skip-to-newest catch-up — the known-lossy path — never fired once in 90 minutes.

**But the leg lost ~86 s of audio anyway, by a different mechanism nobody had
modelled.** `seg_err=17`, and every one is an **HTTP 404 on the
MANIFEST-DRIVEN path**: the manifest announces a segment, we fetch it
immediately, and our CDN edge does not have it yet — the *same* negative-cache
lag that gives speculation its lead (S3). The fetcher marked the segment `seen`
before fetching and **never retried**, so each 404 permanently lost that
segment. 17 × 6 s ≈ 102 s against an 85.9 s observed deficit.

**Why this matters more than the number:** the loss is **~14–17 gaps of ~6 s
each, scattered through the call**, and any one of them can swallow a keyword.
It is a recall bug wearing a latency bug's clothes. **Every 150 s run showed
`seg_err=0`** — only the full-length run surfaced it, which is exactly why the
gate is 90 minutes.

**VERDICT: FAILS the pre-registered bar** ("zero audio deficit vs `pipe_tuned`").
The speculative posture stays un-armable until re-validated.

**Fix:** a manifest-path fetch failure now **un-claims the segment so the next
poll retries it**, bounded at `SEG_RETRY_MAX = 12` polls (~3 s, comfortably over
the ~1–1.5 s negative-cache window S3 measured) before declaring it lost and
counting it. Directly implied by the S3 model: the segment is not missing, we
asked too early.

## Session 2026-07-25 (S5) — N+2 speculation NULL, exactly as the S3 model predicted

**Question:** speculation aims one segment ahead. If the origin published in
bursts, aiming TWO ahead could jump a whole segment.

**S3's cache-gating model predicted a null** — if discovery is gated by CDN
negative-cache expiry *per URL*, then N+2's gate simply opens one segment
duration after N+1's, buying nothing. This is the model's first out-of-sample
prediction, so it is a test of the model as much as of the lever.

Two probers off one shared manifest poller, one chasing N+1 and one N+2 from the
same manifest state:

| gen | N+2 fetchable, relative to N+1 |
|---|---|
| 1 | −4.46 s |
| 4 | −4.81 s |
| 8 | −5.05 s |

**median −4.81 s against a 6.0 s segment duration ⇒ NULL.** N+2 arrives roughly a
full segment later; there is no jump to take. n=3 paired (N+2 only completes on
generations where it can wait out an extra segment), but the three samples agree
to within 0.6 s and the direction is unambiguous.

**The model held.** S3's negative-cache hypothesis made a falsifiable prediction
and it was confirmed — modest evidence, but it is the first thing that has
distinguished that model from "the CDN is just slow".

## Session 2026-07-25 (S4) — keep-alive REFUTED: the handshake is real but NOT on the critical path

**Hypothesis:** every probe opens a fresh TCP+TLS connection, including the
winning one, so a persistent connection removes 1–2 RTT per segment.

**The handshake cost is real and large.** Ranged 1 KB GETs to the same segment,
interleaved, n=15 each: fresh **55.1 ms** (51.2–56.5) vs reused **4.4 ms**
(4.0–6.8) ⇒ a **50.7 ms** ceiling, comfortably over the ≥30 ms bar. So the lever
was worth measuring properly rather than dismissing.

**Realised gain in the race: none.** Same paired-race instrument as S3 — two
probers, same 200 ms cadence, same shared manifest poller, differing ONLY in
connection handling:

| | median | range | n | verdict |
|---|---|---|---|---|
| fresh → keep-alive | **+0.3 ms** | −55.6 .. +57.7 | 9 | **REFUTED** vs ≥30 ms |

**Why a 50.7 ms cost yields a 0 ms gain** — it follows directly from S3. Because
we *poll repeatedly* rather than issuing one perfectly-timed request, the
handshake is absorbed into the waiting window instead of adding to the critical
path. A fresh-connection probe is **in flight for ~55 ms**, which gives it a
longer window to catch the CDN's release instant; the keep-alive probe returns in
4 ms and can miss that instant, then waits a full 200 ms interval. The advantage
cancels — and the symmetric **±55 ms** spread is exactly the signature of that
cancellation.

⚠ **Untested sub-case:** this holds for the *speculative* (polling) path, which
is what we deploy. On a manifest-driven fetch of a segment already known to
exist, the handshake IS on the critical path and keep-alive should still pay.
Not measured, and low priority since S3 showed manifest-driven is the slower
path anyway.

### ⚠ Instrument bug caught — the first S4 run said −250 ms and was WRONG

The first attempt reported keep-alive **250 ms slower**, remarkably consistently
(−226..−460), and a plausible story was available (fresh connections re-roll
which CDN edge node answers, so connection churn could be a *feature*). It was
an artefact: `probe_fresh` timestamped after reading **1 KB**, while the
keep-alive path must drain the **entire ~500 KB segment** to keep its socket
reusable, and timestamped after that. First-byte was being compared against
full-download.

Both paths now timestamp at **response headers**, before any body read. Recording
this because the wrong number was self-consistent, mechanistically explicable,
and would have been believed.

## Session 2026-07-25 (S3) — probe interval REFUTED at 50 ms, and 200 ms is measured to be at the knee

**Hypothesis:** a segment becomes fetchable at an instant; probing every P ms
discovers it P/2 late, so 200→50 ms should return ~75 ms.

**Instrument — deliberately NOT the audio pipeline.** A ~75 ms effect is far
below `leg-lag`'s ~2.5 s noise on DW, so a capture A/B could never resolve it.
Instead two probers race for the **same** predicted segment, driven by one shared
manifest poller, comparing first-success times: paired per segment, sub-ms,
no decode and no cross-correlation. Bounded probes per segment, any 429/403
aborts (that outcome would itself be the answer).

| comparison | median | range | n | verdict |
|---|---|---|---|---|
| 200 ms → **50 ms** (faster) | **−0.1 ms** | −124 .. +86 | 10 | **REFUTED** vs ≥50 ms bar |
| 200 ms → **500 ms** (slower) | **+45.4 ms LATER** | −34 .. +239 | 10 | real cost |

No 429/403 at 4× the request rate — politeness was not the limiting factor; the
effect simply is not there.

**Why faster buys nothing — and what it says about `L`.** Six of the ten 50 ms
deltas land within **±3 ms**. Independently-timed pollers cannot agree that
tightly unless the 404→200 transition is **externally gated rather than
probe-gated** — i.e. both probers are released at the same instant by something
upstream. The natural candidate is **CDN negative-cache expiry on the 404**.

That unifies with the `L` measurements: it explains why the fetchable-lead is a
**fixed ~1.0–1.5 s per origin regardless of segment duration** (+1.07 s at 6 s,
+1.51 s at 1.92 s) — **`L` would simply BE the negative-cache TTL.** Stated as a
hypothesis: it is strongly consistent with three independent observations, but
not directly proven (that would need origin-side cache headers).

**Operational verdict — keep 200 ms, and this argues AGAINST more aggression.**
Faster costs 4× the requests for nothing; slower costs ~45 ms median (up to
239 ms). The current setting is at the knee, validated from both directions.
Good news for the CDN-politeness guardrail: the safe choice is also the fast one.

Overnight loop, item S2: measure `L` and the speculation A/B at **our venue class**
(1.92 s segments) rather than DW's 6 s.

### `L` — MEASURED, and the tightest reading yet

| stream | segment | median `L` | range | n |
|---|---|---|---|---|
| DW (Akamai) | 6.0 s | +1.07 s | +1.01..+3.96 | 5 |
| Unified Streaming | **1.92 s** | **+1.51 s** | **+0.93..+1.54** | **7** |

Third independent reading at this venue (1.40, 1.50, 1.51) and by far the
tightest distribution. Confirms `L` is a fixed origin property, not a fraction of
a segment. Projection against the 1.530 s reference deficit: **Δrace ≈ −0.02 s**.

### Absolute lag — `NOT RESOLVABLE` on this stream (its PDT is not wall-clock)

The A/B produced **physically impossible negative lags** (−7.0 / −8.4 / −8.0 s):
audio cannot reach our ring before the encoder stamped it. Diagnosed rather than
patched around — the reference segment *is* inside the leg's window and
correlates at **peak 1.00**, yet its PDT claims encoding 7 s *after* arrival.

**The demo stream's encoder clock runs fast.** `manifest-probe.sh` already
carries the caveat ("encoder-clock-bounded"); this is the first time it bit.
Consequence: `abs-lag.py`'s absolute number is only as good as the venue's PDT
discipline, and this venue has none. DW's numbers are unaffected (per-segment
PDT, sane 16–19 s readings).

`abs-lag.py` now **refuses to print a negative lag**, reporting `IMPOSSIBLE` with
the reference PDT, the arrival, and the leg window — so a physically impossible
figure can never be quoted, and the next run starts from the diagnostic.

**Relative leg deltas survive** — the PDT offset is common to every leg, so
differences are unaffected. From the one usable run: `spec_edge` **0.97 s
fresher** than `prod_tuned`, and 0.44 s *behind* `prod_streamlink`.

### Why only one usable run — decoder startup fails at short segments

`spec_edge` recorded **0 frames in 2 of 3 runs**. The new BrokenPipe guard worked
exactly as designed (clean `FATAL: stdout closed (decoder exited)` instead of the
runaway), so the fetcher is not at fault — **ffmpeg exited during startup probe**.
Working hypothesis, untested: the fetcher emits only the newest segment on join,
and 1.92 s of media is too little for `-probesize 65536 -analyzeduration 1000000`
to settle, where DW's 6 s segments always sufficed. That did not surface on any
6 s-segment run.

**S2 verdict: `L` MEASURED and strong; the leg A/B is `NOT RESOLVABLE` at n=1.**

### S2 continued — the A/B is not resolvable on this stream AT ALL: it is a 40 s loop

Chased the two defects; both fixed, and then the real blocker surfaced.

**1. Decoder startup — fixed.** ffmpeg exited before emitting a sample:
`Could not find codec parameters for stream 0 (Audio: aac …): unspecified sample
rate`. `probesize 65536 / analyzeduration 1000000` is too little for this
stream's AAC signalling. At **200000 / 3000000** the leg captures cleanly in
4 runs of 4. Corrections to my own reasoning, both found by running the command
instead of theorising:
- I predicted ffmpeg needed *less* probe data. It needed **more**.
- I attributed it to pipe-vs-URL. **Wrong** — the direct-URL `prod_tuned` leg
  fails identically. Only `streamlink` survives, because it demuxes first.
- It is **NOT** a general short-segment law: **AXP/IVS at 2 s segments captured
  488,306 frames with 0 drops at 65536.** Stream-specific.

**2. The blocker — the reference content repeats every 40 s.** With all legs
finally alive, every window comparison returned `NOT STEADY` with absurd medians
(−21 / −38 / −59 s) between legs that started simultaneously. Autocorrelation of
the captured audio explains it: near-perfect self-matches (**0.999**) at 0 s,
40 s, 80 s, 120 s.

**Cross-correlation cannot align looping content** — many offsets match equally
well, so the peak is arbitrary. That retroactively explains the impossible −7 s
absolute lag from the earlier attempt: a spurious match at a loop multiple, at
peak 1.00. The PDT was likely never the problem; **the earlier
"encoder clock runs fast" explanation is superseded by this simpler one.**

`scripts/leg-lag.py` now **refuses to measure** when it detects repetition,
naming the period. Verified to discriminate: DW news (real speech) passes
through to measurement; this demo stream refuses at ~40.0 s.

**Consequence:** `L` (+1.51 s) stands — `spec-probe.py` times HTTP requests and
never cross-correlates, so looping content cannot affect it. **The leg A/B at
our venue class remains OPEN**, blocked on finding a 2–4 s-segment stream with
real non-repetitive audio. S3/S4 are unaffected and can proceed on DW.

## Session 2026-07-25 — ⚠ ORPHANED-FETCHER INCIDENT: a runaway, and a contamination caveat on part 3/4

**Found 18 orphaned `hls-edge-fetch.py` processes running on the box.** Killing a
`capture-bakeoff` orphans its fetcher children, and the fetcher's "NEVER crash
the leg" invariant assumed a live consumer: with stdout dead it looped forever,
never advancing the segment cursor, so the `spec_max_probes` budget (15/segment)
was bypassed entirely — **44,108 speculative 404s and 5,186 broken-pipe writes in
a single leg's counters**. Exactly the CDN-politeness failure the guardrails were
written to prevent, arrived at through a path nobody had modelled.

**Fixes (`scripts/hls-edge-fetch.py`):**
- `BrokenPipeError` on stdout is now **fatal** at all three write sites and exits
  the process. A dead decoder means the leg is over; looping is never right.
- **Runaway trip** as defence in depth: >50 × 404s per emitted segment disables
  speculation and falls back to manifest-driven. Normal operation is ≤15.
- `scripts/abs-lag.py` no longer crashes on a leg that captured zero frames — it
  reports `LEG DEAD — n frames` and measures the surviving legs.

**⚠ Contamination caveat, stated because it cuts against our own results.** The
orphans accumulated across the session and were competing for a 2-vCPU box
*while the part-3/part-4 A/Bs were being measured*. Load was 1.7–2.1 with 18
strays; 0.88 once reaped. Two mitigations mean the headline is probably intact
but NOT clean:
- every A/B was **interleaved in a single process**, so contention hit all legs
  in that run together rather than biasing one;
- the −4.56 s speculative result **replicated across two independent sessions**
  (−4.560 s paired median, twice).

Still: the absolute lag figures and the leg-spread statistics were taken under
unknown contention and should be re-measured on the now-quiet box before being
quoted as final.

> **Correction, same session:** the sentence here originally read "`spec_buffered`
> dying with 0 frames in two consecutive runs is itself most likely a contention
> symptom, not a code defect." **That was wrong.** The leg died because the rig
> still passed `--buffered`, a flag renamed to `--stream-body` when progressive
> streaming was refuted and the default inverted — argparse rejected it and the
> process exited before reading a byte. A stale config flag, diagnosed by running
> the command rather than reasoning about the load average. The contention
> caveat above stands on its own evidence (18 strays, load 1.7–2.1 → 0.88); this
> particular attribution did not.

**Operational rule added to the loop pre-flight:** reap strays before every
experiment, and verify with `ps -eo cmd | grep -c "[h]ls-edge-fetch"` — `pgrep
-cf` self-matches through the ssh command line and will report a false non-zero.

## Session 2026-07-24 (night, part 4) — progressive body streaming REFUTED; speculative fetch REPLICATED and shipped

### REFUTED — streaming the segment body buys nothing and costs stability

The reasoning looked airtight: `hls-edge-fetch.py` read each segment whole
(`urlopen(...).read()`) before writing a byte, and the reference probe showed
first-byte at 1.753 s vs last-byte at 2.697 s — **~0.94 s of already-arrived
audio sitting in a buffer.** Emitting progressively should have handed ffmpeg the
head of a segment while its tail was still in flight.

Implemented behind a flag so it was a pure A/B (identical fetcher, identical
speculation, only the body read differs), 3 interleaved runs, all peaks 1.00:

| run | spec_buffered | spec_stream | Δ |
|---|---|---|---|
| 1 | 11.709 s | 11.512 s | −0.197 |
| 2 | 11.466 s | 11.541 s | **+0.075** |
| 3 | 12.731 s | 12.486 s | −0.245 |

**Median −0.197 s against a pre-registered ≥0.3 s bar → REFUTED.** And it is not
free: `spec_stream` logged **3 / 32 / 62 short reads** per run against **0 / 0 / 0**
for buffered (many-small-writes hit a closing ffmpeg pipe where one large write
does not), with jitter p99 12–18 ms vs ~1 ms. **Default reverted to buffered**;
the path survives behind `--stream-body` for a venue whose bodies genuinely
arrive slowly. The ~0.94 s of "wasted" buffer was never on the critical path —
ffmpeg was already draining the pipe faster than the body arrived.

*Process note: the first attempt at this A/B produced a leg with 0 frames because
the box was running a pre-flag copy of the script. That is a broken instrument,
not a result — re-run, do not interpret.*

### REPLICATED — the speculative fetch number holds

Same runs, `spec_buffered` vs `prod_tuned`: paired **−0.632 / −4.560 / −6.496 s,
median −4.560 s** — landing on the **−4.56 s** measured independently the run
before, on different runs against a moving live stream. The headline stands.

### Shipped

- `scripts/hls-edge-fetch.py` — `--speculative`, `--selftest` (pure-function
  regression checks; `tests/` is Rust-only), periodic `SUMMARY` health line
  (segments, catch-up drops, short reads, spec hits, 404 rate), and a
  pre-emptive `seen` claim that is **un-claimed on every failure path** — without
  that, a 404 (the normal case while a segment is still publishing) would make
  the leg skip that segment forever.
- `configs/webcast-fanout-arm-spec.toml` — armed template where `spec_edge`
  **replaces** `streamlink` (leg count stays 2, CPU flat on the 2-vCPU box).
- `scripts/stage-webcast-arm.sh --speculative` — selects it, and refuses to stage
  if `--selftest` fails.
- `configs/webcast-fanout-arm.toml` — **unchanged as the proven default** (FOMC
  runs it); its stale NOTE claiming "+0 ms" and "the gap is decode, not
  acquisition" is corrected in place with the evidence trail.

## Session 2026-07-24 (night, part 3) — SPECULATIVE SEGMENT FETCH: −4.56 s median, and the join lottery collapses

**PROVEN, and it clears the pre-registered ≥0.5 s bar by ~9×.** Follows part 2
(the client term is ~1 segment of waiting to be told a segment exists).

### The idea and its measured basis

Segment URLs are numerically predictable. On the reference stream a segment is
fetchable by **predicted URL** a median **+1.02 s before the manifest announces
it** (n=5, range −0.77..+2.89) — almost exactly the ~1.06 s lead the competitor
holds. So `scripts/hls-edge-fetch.py` gained `--speculative`: aim at `N+1`, ask
for it directly, retry 404s inside a politeness budget.

Safety rails, because this is a more aggressive consumer than a normal client
and a block would hit **every leg on the box**: probing starts only after 80 % of
a segment duration, ≤15 probes/segment, and **any 429/403 permanently disables
speculation** and falls back to manifest-driven. The URL pattern is verified
against real consecutive segments before speculation is enabled at all.

### Result — 6 runs, DW reference stream, all peaks ≥0.98

Absolute lag behind the encoder (`abs-lag.py`), 4 legs in ONE process per run so
each run is a true interleaved A/B:

| leg | r1 | r2 | r3 | r4 | r5 | r6 | **median** | **spread** |
|---|---|---|---|---|---|---|---|---|
| prod_streamlink | 18.59 | 13.65 | 17.32 | 16.78 | 17.05 | 18.03 | 17.18 | 4.94 |
| prod_tuned | 16.54 | 13.88 | 12.01 | 17.37 | 17.81 | 18.54 | 16.95 | 6.53 |
| edge_poll (no spec) | 17.17 | 13.67 | 12.26 | 14.63 | 12.57 | 13.47 | 13.57 | 4.92 |
| **spec_edge** | **12.00** | **12.85** | **12.25** | **12.79** | **11.94** | **12.76** | **12.50** | **0.91** |

Paired per-run differences (median):

| comparison | isolates | median |
|---|---|---|
| spec_edge − prod_tuned | the whole change | **−4.56 s** |
| spec_edge − edge_poll | **speculation alone** | **−0.76 s** |
| edge_poll − prod_tuned | phase-locked poll + catch-up | −1.48 s |

**Two distinct wins, and the second is the bigger one:**

1. **Speculation is worth ~0.76 s**, consistent with the +1.02 s
   fetchable-before-announced measurement that motivated it.
2. **The join lottery collapses.** `spec_edge` spread is **0.91 s** against
   4.9–6.5 s for every other leg. Because it always pulls the newest segment the
   instant it exists, it converges to the edge regardless of where it joined —
   it reliably achieves what the production legs reach only on a lucky draw
   (their best-ever draw was 12.01 s; spec_edge's *worst* was 12.85 s). For
   racing this matters more than the mean: a bad draw currently costs up to
   6.5 s **for the entire event**.

Speculation health across the instrumented runs: **65 hits, 0 rate-limit or
disable events**, URL pattern verified. The 131 `seg err` entries are all
`Broken pipe` at shutdown (ffmpeg exiting first) — benign.

### Drop-safety — checked, not assumed

The fetcher's skip-to-newest catch-up **drops audio by design**, which would cost
recall. It fired once per run (2–3 segments). Two checks say those drops drained
the *startup backlog* rather than mid-call content:

- `spec_edge` never carried **less** audio than the production baseline in any
  run (r4 8093 vs 8095 frames; r5 7795 vs 7795; r6 7788 vs 7494).
- Windowed `leg-lag.py` across a whole run oscillates within one segment
  duration (−2.6..−5.9 s) with **no persistent 12–18 s step**, which a mid-run
  drop of 2–3 segments would have produced.

⚠ **Still a deployment gate:** the lossy path exists, was exercised, and has only
been observed over 3×150 s. Before arming, validate over a full ~90 min event and
surface a drop counter in the report.

### The fetchable-lead does NOT scale with segment duration (2026-07-24, follow-up)

The open worry was that the +1.02 s lead was a fraction of DW's 6 s segment and
would shrink to ~0.33 s at our 2 s venues, making speculation useless where it
matters. **Refuted.** Measured on a **1.92 s-segment** stream (Unified Streaming
demo, `demo.unified-streaming.com`, standard HLS, no LL-HLS — same class as
AXP/IVS):

| stream | segment duration | median lead | range | n |
|---|---|---|---|---|
| DW (Akamai) | 6.0 s | +1.02 s | −0.77 .. +2.89 | 5 |
| Unified Streaming demo | **1.92 s** | **+1.40 s** | **+1.36 .. +1.96** | 8 |

**The lead is a fixed origin property, not a fraction of a segment** — it is
*larger* at short segments, and far tighter (range 0.6 s vs 3.7 s). The likely
mechanism is the **manifest's CDN cache TTL**: the playlist is served stale for
~1–1.5 s while the segment object itself is already fetchable. That predicts a
roughly constant lead per origin, which is what we see across two independent
CDNs.

**Consequence for the race:** the AXP feed gap is **1.241 s**. A lead of
1.0–1.4 s at 2 s segments means **speculation alone is of the right size to close
the whole measured gap** — where before we could only claim a 6 s-segment number.
⚠ Still origin-specific: neither test used Amazon IVS (AXP) or Brightcove (FOMC),
and the lead must be re-measured per venue before it is claimed for an event.

### Caveats

- The −4.56 s A/B headline is a **6 s-segment number and does not transfer**;
  `COMPETITOR-ANATOMY.md` records that the ladder compresses at short-window
  venues. The transferable claims are the *fetchable-lead* (measured at both 6 s
  and 1.92 s above) and the variance collapse.
- `scripts/abs-lag.py` assumes a PDT tag per segment. The 1.92 s stream carries a
  **single PDT anchor** plus cumulative `EXTINF` (a common packager choice), so
  the script needs to derive per-segment PDT from the anchor before it can run an
  absolute A/B there. Open.
- 6 runs × 150 s, one venue, one CDN (Akamai). Other origins may 404 until
  announce, in which case speculation degrades to exactly `edge_poll`.
- **Not deployed for FOMC 07-29** (operator decision): prove it off-FOMC first,
  because a rate-limit would take the production legs down with it.

## Session 2026-07-24 (night, part 2) — Step 2: ffmpeg flags REFUTED; the client term is ~1 SEGMENT and the join is stochastic

Follows the entry below (the gap is upstream of our audio). Two new instruments,
one refuted hypothesis, one located mechanism.

### New instruments (the old ones structurally could not see this)

- **`scripts/abs-lag.py`** — absolute lag of a leg behind the **encoder**, by
  downloading a segment with a known `EXT-X-PROGRAM-DATE-TIME`, decoding it, and
  cross-correlating it into the leg's recording to read the real `capture_ns` of
  the frame carrying it. `capture-bakeoff` compares legs to *each other*, so any
  cost every leg shares is invisible to it — that is why the 0 ms acquisition
  proof (below) could not see ffmpeg's own cost. This measures against the CDN's
  own clock instead.
- **`scripts/leg-lag.py`** — steady-state leg-vs-leg arrival delta from the
  **real** frame timestamps over several mid/late windows, with a spread-based
  NOT-STEADY guard. `capture_bakeoff::cross_deltas` extrapolates arrival as
  `ref_t0 + i/sr` from frame 0, so a live-HLS join backlog (each leg drains a
  different one faster than real time) makes its whole-run `delta_ms` measure
  **join luck** — the defect behind the retracted "−6016 ms then 0 ms, both peak
  1.0000" pair. Coarse alignment probes with a MIDDLE slice, since a
  later-joining leg simply does not contain the other's early content.
- **`scripts/pdt-log.py`** — continuous manifest edge log (PDT, media sequence,
  segment count) so segment-appearance can be tied to ring arrival.
- Rig: `configs/client-latency-bakeoff.toml` — 4 legs, one run, so each pair is a
  true interleaved A/B.

### REFUTED — the ffmpeg probe/buffer flags are worth ~0 ms

Hypothesis: our legs pay `-analyzeduration 1000000` (a full second) plus AVIO
output buffering. Tested `-probesize 32768 -analyzeduration 0 -flush_packets 1
-avioflags direct` against production flags, both client chains, 2 replicate
runs on DW (6 s segments, PDT on every segment), all peaks 1.00:

| pair | leg-lag (steady state) | abs-lag run 1 | abs-lag run 2 |
|---|---|---|---|
| streamlink: prod → fast | **−14.7 ms** (IQR 27.9, spread 60.6) | −24 ms | −20 ms |
| tuned-ffmpeg: prod → fast | **+6.7 ms** (IQR 87.0, spread 294) | +1 ms | −31 ms |

**Verdict: NOT RESOLVABLE ABOVE NOISE — the flag set buys nothing.** Replicated.
Do not spend more time on ffmpeg input flags.

### LOCATED — the client term is ~1 segment, and streamlink's join is erratic

Absolute lag behind the encoder (`abs-lag.py`, DW, segment-end reference):

| leg | run 1 | run 2 | run-to-run spread |
|---|---|---|---|
| prod_streamlink | 19.232 s | 16.261 s | **2.97 s** |
| prod_tuned | 16.750 s | 16.620 s | **0.13 s** |

Two findings, both new:

1. **The leg ordering FLIPS between runs** — streamlink was 2.48 s *behind*
   tuned in run 1 and 0.36 s *ahead* in run 2. This is the same instability seen
   live (streamlink won on AXP, tuned-ffmpeg won 5/6 on LMT). It is not a
   property of the client, it is the **stochastic join point**.
2. ~~**`-live_start_index -1` joins deterministically (0.13 s spread)**~~ —
   **RETRACTED, see correction below.** What survives: for racing, variance is as
   costly as mean, because a bad draw persists for the whole event — a live
   stream plays at 1× from wherever you joined.

> **⚠ CORRECTION 2026-07-24 (night, part 5) — "deterministic joiner" was a
> 2-sample artifact.** The 0.13 s spread above is the only two observations that
> existed when it was written. Pooling every `abs-lag.py` observation since:
>
> | leg | n | min | max | **spread** |
> |---|---|---|---|---|
> | prod_tuned | 11 | 12.006 s | 19.227 s | **7.22 s** |
> | prod_streamlink | 9 | 13.653 s | 19.232 s | ~4.9 s |
> | spec_edge | 6 | 11.935 s | 12.847 s | **0.91 s** |
>
> **`-live_start_index -1` is NOT deterministic — it carries the largest lottery
> of the three.** Both production legs draw badly on a bad day; only the
> speculative fetcher is stable, because it continuously re-aims at the newest
> segment instead of living with wherever it happened to join. Consequence: the
> `pipe_tuned` fallback in `configs/webcast-fanout-arm-spec.toml` is a *fallback*,
> not a *reliable* one — the fanout's OR-at-decision is what protects us, and it
> only protects us while the speculative leg is actually working. That is what
> makes a per-venue GO/NO-GO probe worth having.

**Mechanism.** DW's manifest edge lag was 16 s to the newest segment's *start*,
so its freshest content-end is ~10 s old; our best leg sits 16.3 s behind that
content — i.e. **~6.3 s ≈ one segment duration behind the freshest available
audio.** That is structural for standard HLS: you join at the newest *complete*
segment and play from its beginning, so you are permanently ~1 segment back.

Scaled to our real venues this predicts a client term of **~2 s at AXP (2 s
segments)** — and the competitor's measured lead is **~1.06 s**, i.e. roughly
half a segment. Consistent with them consuming the segment *progressively* while
we wait for it to complete. This matches the batching-covariance result below
(they share our segment cadence but receive it ~1 s earlier).

**The lever is therefore partial/progressive segment consumption, not flags and
not the venue.** Note "just skip forward to the live edge" is NOT the fix: being
one segment back means we process all audio ~1 segment late, whereas skipping
would *drop* speech and cost recall. The competitor is not skipping — they are
getting the bytes earlier. Since these venues advertise no LL-HLS, the open
candidate is a **speculative chunked fetch of the in-progress segment** (request
N+1 before it is announced; origins that serve chunked-transfer stream it as it
is produced). Untested.

### Caveats

- DW has 6 s segments; our venues run 2–4 s, and `docs/COMPETITOR-ANATOMY.md`
  already records that the acquisition ladder **compresses at short-window
  venues**. The ~6.3 s client term is DW-specific; the *≈1 segment* shape is the
  transferable claim, and it is untested at 2 s.
- n=2 runs. The flag refutation is replicated; the join-variance figure is two
  draws and should be treated as "≥3 s of variance exists", not a distribution.

## Session 2026-07-24 (night) — THE GAP IS LOCATED: ~1 s of it is UPSTREAM OF OUR AUDIO, not decode

**The competitor moves the market before the word's audio reaches our box.** Measured on
4 races across 2 venue classes, from artifacts already banked (no new event, no GPU).
This supersedes the "the gap is DECODE" consequence written in the entry below.

### Method

Per-race, all timestamps reduced to one UTC timeline:

1. **Our audio-arrival** — word-end located in the box's recorded capture leg with
   faster-whisper word timestamps (`scripts/corpus-align.py` method), then the *frame*
   carrying that word-end looked up in the leg's `.jsonl` (`seq`,`capture_ns`) to get the
   instant the audio actually reached the pipeline (includes client buffering — which is
   the point).
2. **Monotonic→UTC** — `clock::MonotonicNs` is `CLOCK_MONOTONIC_RAW` (`src/clock.rs:17`),
   which chrony does NOT slew, so it drifts against UTC at the box's oscillator error.
   Two *contemporaneous* anchor pairs (arm `decision_ts` ↔ its own log wall-clock line) at
   INTC 07-23 21:09 and AXP 07-24 12:37 fit the map at **6.47 ppm** — matching chrony's
   independently-reported 6.899 ppm. Anchor cross-validated on a second log line (MILL)
   to **774 µs**. ⚠ A naive `CLOCK_MONOTONIC` anchor is wrong by ~2.1 s over this box's
   3.7-day uptime; do not use one.
3. **Wave-1** — µs-resolution Kalshi tape, scored on the **price-moving** sweep (see
   scoring correction below). Box clock is disciplined to **1.6 µs** RMS vs AWS Time Sync.

### Result — the feed term dominates and is venue-invariant

| event | venue (PDT edge lag) | word | word-end audio **@ our box** | price-moving sweep | **FEED gap** |
|---|---|---|---|---|---|
| AXP 07-24 | Amazon IVS, 2 s seg (**~4 s**) | NFL | 12:37:28.754 | 12:37:27.513 | **+1.241 s** |
| LMT 07-23 | Notified/CDN, 4 s seg (**~9 s**) | golden dome | 12:34:56.421 | 12:34:55.310 | **+1.112 s** |
| LMT 07-23 | " | european | 12:42:02.236 | 12:42:01.112 | **+1.125 s** |
| LMT 07-23 | " | morfius | 12:57:10.089 | 12:57:09.336 | **+0.754 s** |

Full decomposition of the one race with a live arm on the same box (AXP NFL):

| term | value | share |
|---|---|---|
| **FEED** — sweep → word-end audio arrives at us | **1.241 s** | **81 %** |
| **OURS** — audio arrival → order decision (decode + pipeline + decision) | **0.289 s** | 19 % |
| **total Δrace** | **1.530 s** | |

**Verdict — PROVEN: the dominant term is upstream of our audio.** A detector with *zero*
latency would still have flagged NFL 1.241 s after the competitor had already swept 853
contracts 30¢→72¢. **No model, quantum, or pipeline work can close this gap.** Our own
end-to-end path measured **289 ms** live — better than the ~510 ms the corpus benches
imply, and not the problem.

**Venue-invariance is the discriminating finding.** The feed gap is ~1 s at a 4 s-edge-lag
venue AND at a 9 s-edge-lag venue. So the competitor is **not** on a fixed-latency
independent source (a phone bridge would make the 9 s venue far worse — it isn't); they
co-listen on the same feed class with a systematic ~1 s advantage. That points at
**segment consumption / client buffering**, not venue selection and not the detector.

⚠ **Why the 0 ms acquisition proof below did not see this.** That experiment compared our
streamlink leg against a phase-locked fetcher by cross-correlating **two recorded WAVs —
both produced through an ffmpeg decode**. Any buffering shared by both legs cancels
exactly and is invisible to that instrument. It proved "we poll our chosen manifest
optimally"; it did **not** and could not test "our `streamlink | ffmpeg` chain adds ~1 s
before the samples reach the ring." That remains open and is now the prime suspect.

### Scoring correction — wave-1 must be the PRICE-MOVING sweep

Re-scoring the tapes at µs resolution found the previously-pinned Δrace figures were
scored against **flat-price prints**, not the sweep that causes the rally:

| word | doc figure | scored against | price-moving sweep | corrected |
|---|---|---|---|---|
| AXP MILL | −2 s | a **single 1-contract** print 12:35:16.500 | — none — | **NOT A RACE** |
| AXP NFL | −2 s | 12:37:27 (whole-second) | 12:37:27.513 (853 ct, 30→72¢) | **−1.530 s** |
| LMT GOLD | −0.6 s | 12:34:54.198 (129 ct, flat 77¢) | 12:34:55.310 (730 ct, 77→95¢) | **+0.507 s (AHEAD)** |
| LMT EURO | −2.8 s | 12:42:00.316 (156 ct, flat 64¢) | 12:42:01.112 (89 ct, 75→85¢) | **−2.024 s** |

**AXP MILL was never a race** — it traded 94–95¢ from 11:25, over an hour before the
utterance; the 12:35:18.030 print is 127 contracts at a *flat* 95¢. Pre-priced words must
be excluded from Δrace entirely: there is no rally to lose.

**On LMT GOLD, read "+0.507 s" precisely: we beat the sweep that moved the price, but a
machine print preceded us.** 12:34:54.198 took 129 contracts at a flat 77¢ in 3
µs-identical fills — a machine signature that did *not* move the market, 0.605 s before
our flag. Whether that or the 12:34:55.310 sweep is "wave-1" is a definitional choice, and
the two give opposite signs. This ledger scores the **price-moving** sweep (the operator's
framing: the competitor who causes the first rally), so GOLD reads as ahead — but the
honest statement is *"we beat the rally, and someone machine-traded ahead of us without
moving price."* Both numbers are given above so the choice stays visible.

### Caveats

- **n = 4 races, 2 events.** AXP contributes the only full decomposition (the only race
  with a live arm on the same box as the capture); LMT's arm ran on the CDMX laptop, so
  LMT contributes the FEED term only.
- faster-whisper word boundaries are ±~100 ms; the finding is 7–12× that. `morfius` was
  rendered "Smorpheus" and is the least reliable of the four.
- If the competitor fires on word-*start* rather than word-end (prefix-style), credit them
  ~0.3–0.5 s of the gap — GOLD "Golden"/"Dome" differ by 300 ms, EURO "European" spans
  460 ms. Even then ~0.6–0.8 s of feed advantage survives.
- INTC 07-23 could not be used: its capture bake-off recorded **0 frames on all 4 legs**
  (`~/intc-capture/run.log`) — a silent capture failure worth its own follow-up.

### Are they even on our feed? — batching-covariance says YES

Two tests, and the weaker one is the famous one:

- **Segment-phase (Rayleigh) test — underpowered, do not cite alone.** Phase of
  each machine-signature price-moving sweep relative to our segment-arrival
  boundaries. All sweeps: n=68, R=0.072, p=0.71. Audio-triggered first-rally
  only: n=14, R=0.229, **p=0.49** — at n=14 significance needs R>0.46, so this
  cannot distinguish clustered from uniform. Also note pooling across events is
  itself wrong: each capture joins at an arbitrary phase, so a real per-event
  cluster would wash out.
- **Batching-covariance test — decisive.** If they were on a *continuous* feed,
  their delay would be constant and our measured gap would track OUR
  within-segment batching 1:1 (slope +1.00). If they share our segment cadence,
  their delay absorbs the same batching and the slope is ~0.

| race | our batching excess | feed gap |
|---|---|---|
| AXP NFL | 0.360 s | 1.241 s |
| LMT GOLD | 2.800 s | 1.112 s |
| LMT EURO | 1.380 s | 1.125 s |
| LMT MORF | 1.880 s | 0.754 s |

**Observed slope −0.084 (r² 0.17) against +1.00 predicted for a continuous
feed.** Our batching swings **2.44 s** across these races while the gap moves
only **0.49 s**. ⇒ **They are on the same segmented feed, receiving it ~1.06 s
before us.** (n=4 — but the effect size is unambiguous: a continuous feed would
have made the gap vary by 2.4 s.)

### What this makes next

Not decode, and not the venue. The question is why the same segment reaches us
~1 s later than a co-listener — pursued in the Step 2 entry above, which refutes
the ffmpeg-flag explanation and localizes it to ~1 segment of
wait-for-complete-download. Evidence: box `~/{axp,lmt}-capture/`,
`state/KXEARNINGSMENTION{AXP-26JUL24,LMT-26JUL23}/`, `~/axp-arm-launch.log`.

## Session 2026-07-24 (evening) — ACQUISITION LEVER REFUTED: our streamlink leg is already at the live edge

**Supersedes the morning S1/S2 entry below.** Deployed the phase-locked fetcher
as a real leg (`scripts/hls-edge-fetch.py`, `edge_poll`) and measured it
leg-vs-leg against our ACTUAL streamlink leg via `capture-bakeoff` + windowed
xcorr on the recorded WAVs (DW, 6 s segments, us-east box).

- `capture-bakeoff` `delta_ms` was start-skew-dominated NOISE: run 1
  `streamlink→edge_poll −51.6 ms`, run 2 `−603.5 ms` (streamlink stalled/started
  late that run, 4479 vs 4793 frames). Both peak 1.00 — the CONTENT aligns
  perfectly; only the leg-startup timing differs. `delta_ms = start_skew +
  content_lag` and start_skew is not a steady-state term.
- **Steady-state content lag (the real number), windowed xcorr on 5 independent
  mid-stream 15 s windows: +0 ms, peak 1.000 in EVERY window.** edge_poll and
  streamlink deliver the same audio at the same instant.

**Verdict — the acquisition poll-phase lever is ~0 ms against our real leg.**
streamlink `--hls-segment-stream-data --hls-live-edge 1` already streams the
in-progress segment download at the live edge; aggressive 250 ms polling adds
nothing. The morning ~2.25 s figure was a `poll-age.py` proxy comparing against
a NAIVE reload-per-segment consumer we do not run — it does NOT describe our
streamlink leg. **Consequence: acquisition is not the −2 s; the gap is DECODE
(prefix-firing, chunk-8, FastEmit spotter) and/or the competitor's feed.** The
`edge_poll` leg was NOT deployed (0 benefit, +1 leg CPU on a 2-vCPU box); script
+ `configs/edge-bakeoff.toml` kept as the experiment record.

## Session 2026-07-24 — ACQUISITION: manifest-poll-interval is a real latency lever (S1/S2)  [SUPERSEDED — see above; proxy vs a naive consumer, not our real leg]

Box: us-east-1 (`i-04c50d76572aa092e`), reference stream DW News HLS
(`dwamdstream102.akamaized.net/…/stream01`, 6 s segments, PDT on every segment).
Motivation: Friday AXP/VZ arm measured −2 s vs wave-1 on IVS (2 s segments); the
internal pipeline is only **88 ms** (`capture→decision` p50 from the AXP arm
report `edges`), so the −2 s is acquisition + the shared ~1 s decode floor, not
our pipeline.

**⚠ RETRACTED first attempt (method error, recorded per the rule):** an xcorr of
a streamlink leg vs a fast-poll fetcher gave −6016 ms (run 1) then 0 ms (run 2),
both at peak 1.0000. Whole-window xcorr of two independently-started HLS
consumers measures **start-segment alignment, not steady-state receive latency** —
the two runs began at different segments. NOT a valid latency measurement.

**Clean measurement (PDT-anchored absolute content-age = wall_time_received −
segment_PDT, two poll modes run simultaneously against DW, 90 s):**

| Poll mode | n | mean age | median | min | p95 |
|---|---|---|---|---|---|
| reload-per-segment (our streamlink `--hls-playlist-reload-time segment`) | 15 | **17.86 s** | 17.82 | 17.81 | 17.86 |
| 250 ms phase-locked poll | 16 | **15.61 s** | 15.61 | 13.13 | 18.02 |

**Verdict — PROVEN lever: phase-locked sub-500 ms manifest polling holds audio
~2.25 s fresher on average (up to ~4.7 s at best) than reloading once per
segment**, on DW's 6 s segments. The effect is poll-phase latency: a
reload-per-segment client notices a freshly published segment up to a full
segment-duration late; a 250 ms poller catches it near-instantly. The absolute
age (~15–18 s) is DW's deep DVR and not transferable; the ~2.25 s DIFFERENCE is
the transferable lever and scales with segment duration. **On IVS 2 s segments
this bounds to ≤2 s — consistent with the entire Friday −2 s gap.**
Caveats before promotion: (a) this models our streamlink reload behavior with a
matched-config python proxy, not streamlink instrumented directly; (b) needs
validation on a real 2 s-segment (IVS) stream to confirm it delivers ~2 s there;
(c) the recovered audio still faces the ~1 s decode floor. Next: build the
phase-locked fetcher as a real source-leg (S2) and re-measure on IVS.
Evidence: box `~/poll-age.py`, `~/age-{slow,fast}.txt`; scratchpad `edge-fetch.py`.

## Session 2026-07-17 — fire-path mechanical pass (commit `7c78863` vs baseline `8eed570`)

Box: CDMX dev laptop (macOS, thermally noisy — measured 3.5× run-to-run swing on uninterleaved
runs; all A/B below interleaved OLD/NEW ×5 or side-by-side in one process). Instruments:
`tests/perf_budget.rs` (`--release -- --ignored`). Raw logs: session scratchpad `ab_runs.log`.

| Change | Instrument | OLD | NEW | Verdict |
|---|---|---|---|---|
| Ring unpark-on-send | `perf_ring_wake_ab` — 500 parked pushes per impl, both impls in one process, push→recv-return | p50 **67 µs** p95 **127 µs** max 133 µs | p50 **4 µs** p95 **6 µs** max 42 µs | **PROVEN — ~17× p50 / ~21× p95 per ring hop.** Applies to every idle-wake crossing (frames arrive every 20 ms; consumers park between frames, so this is per-hop steady state, not a rare case) |
| Arm-time shadow warm-fire | `perf_cold_first_fire` — 4 fresh rigs [off,on,off,on], per-fire OrderRecord timestamps | process-cold rig, socket-warm only: first-fire premium **645 µs** | + signed-GET warm-fire: premium **0 µs** (later rigs 0–6 µs) | **PROVEN for the production case** — arm IS the process-cold state; the bare `/exchange/status` socket warm does NOT cover it (rig 1 had it and still paid 645 µs), the signed GET does. Caveat: wiremock/localhost — proves code-path warming, not network |
| No-spawn executor (FuturesUnordered) | `perf_executor_fire_path` interleaved ×5 — `decision→exec` p95 over a 6-deep PRE-QUEUED burst | ~**690 µs** (663–698, warm runs) | ~**763 µs** (721–836) | **MEASURED REGRESSION in this shape: ~+70 µs burst-intake p95.** The old code spread 6 queued intents across 2 workers; the new single-task drain serializes intake. The claimed win (single-intent wake, no spawn alloc) is NOT isolatable by this instrument — the racing case is the FIRST intent, which this p95-of-6 does not measure. Tracked live via the new `handoff→exec` edge (no pre-change baseline exists) |
| Deferred telemetry locks (PendingEdges) | same instrument — `exec→send` p95 | ~26–28 µs | ~27–39 µs | **NOT RESOLVABLE ABOVE NOISE** (predicted: ~ns of lock removal on a ~30 µs edge) |
| Fire-first ordering (detect thread) | `perf_full_chain_paced` interleaved ×5 — `capture→decision`/`capture→ack` p95 | 140.2–141.6 ms / 140.9–142.5 ms | 140.6–141.8 ms / 141.2–142.3 ms | **NOT RESOLVABLE ABOVE NOISE** — the edge is chunk-cadence-dominated (160 ms test chunks); the reordering removes ~µs of format/emit from the critical span. Tracked live via `handoff→exec` |
| `book_age` histogram fix | `telemetry` regression test + e2e all-edges assertion | silently dropped since 07-16 | recorded | **PROVEN (correctness)** — forensics-affecting, not latency |
| Cold-fire premium (characterization) | `perf_cold_first_fire` | — | first fire 320–675 µs vs steady 17–38 µs across runs | Instrument established; premium eliminated by warm-fire (row above) |
| `[perf]` pinning / SCHED_FIFO / spin, OS host tuning | `os-tune-check.sh`, on-box perf suite | — | — | **INSTRUMENT-ONLY, ON-BOX** — Linux-only; gates 1–4 in RUNBOOK "Fire-path latency pass". No laptop claim possible |
| chunk_ms 80→40 (recognition latency) | `bench-detector` paced, sherpa, QCOM-Q2-2026 dev call (45 min), chunk 80 vs 40 grid | onset→emit p50 837 / p95 916 ms (acquisition), 916/1076 (guidance) | p50 838 / p95 916; 915/1077. Accuracy identical | **REFUTED — worth ~0 ms on sherpa** (expected ~20). The model's internal chunk-16 quantum absorbs chunker timing: earlier delivery just waits in sherpa's internal buffer. The dial only pays on a small-quantum engine (streaming_ctc). RUNBOOK gate 2 closed as WON'T-DO for sherpa |

### Net honest summary

The two mechanical wins that survive measurement on the laptop are the **ring wake (−60–120 µs per
idle hop)** and the **first-fire warm (−645 µs on the fire that decides the race)**. The executor
restructure is latency-neutral-to-slightly-negative in burst-intake p95 and unproven (though
plausible) for single-intent wake; if the on-box `handoff→exec` histograms don't vindicate it, the
`FuturesUnordered` change should be reconsidered. Everything else on this box is below the noise
floor, exactly as the pre-measurement audit predicted: the fire machinery was already µs-scale.
The dominant terms remain (a) detector model lag (sherpa ~320 ms lookahead + decode lag — blocked
on an English streaming-CTC model, see below), (b) chunk assembly (~20 ms, measurable now,
gated on-box for prod), (c) OS-tuning tail (on-box only).

## Race reconstruction — MS-RECE replayed under today's lanes (2026-07-17)

Question: with the us-east migration (+ this pass), how does the 07-15 MS-RECE loss replay?
Timeline anchors (post-mortem, RUNBOOK): competitor's first fill **13:27:10.274Z**; our
keyword chunk captured **≈10.672** (−398 ms at audio arrival); decision ≈10.763
(`capture→decision` 91.3 ms); would-be ack ≈**10.867** → net **~593 ms behind**.

| Leg | CDMX then (measured) | us-east now | Evidence class | Race effect |
|---|---|---|---|---|
| box→Kalshi RTT | p50 54.7 ms (floor under the 102 ms warm `send→hdrs`) | p50 2.2 ms ⚠ **= CloudFront edge, NOT the engine** | ⚠ **RE-READ 2026-07-21** (VERIFICATIONS Q1 probe) | the latprobe host `api.elections.kalshi.com` is CloudFront; the engine is in **us-east-2** and our box is us-east-**1** → **true box→engine RTT ~12–17 ms**. **MEASURED 2026-07-21 from a us-east-2 probe**: direct us-east-2 ELB ~2 ms, FIX host ~0.8 ms → **box move recovers ~11–14 ms** (`state/latprobe-useast2-2026-07-21.json`). Real send→ack still needs the relocated box + a probe-order |
| box→Twilio media | p50 55.3 ms + **4% loss** + ngrok relay | p50 **2.9 ms, 0% loss**, no ngrok | **MEASURED** (RTT/loss); end-to-end audio delta **UNMEASURED** | X_audio recovery: ~100–150 ms by p50 arithmetic; loss-tail removal may be worth much more on the worst chunk — handshake numbers cannot prove it |
| Twilio bridge→media-stream fork | unknown | unchanged | UNMEASURED | 0; the SIP bake-off exists to bypass it |
| `capture→decision` | 91.3 ms | ~same (chunk 80, sherpa) | measured then; unchanged | chunk-40 gate ≈ −20 ms when passed on-box |
| first-fire premium | unmeasured | −645 µs (warm-fire) | **PROVEN** (this ledger) | sub-ms |
| detector onset→emit | sherpa ~1 s class | unchanged | measured (BAKEOFF holdout) | **the remaining gap** — blocked on Tier-1 CTC |

**Replay verdict:** today's ack ≈ `+593 − ~85 (order leg) − X_audio` ms behind the same
competitor. At X_audio ≈ 100–200 ms (p50 arithmetic): **~+300–400 ms behind — still lost**, just
no longer by 600. Even a generous tail-case X only reaches ~even, and RECE swept 27→98¢ in 233 ms.
The migration converts transport from the *dominant* deficit to *recovered-or-nearly-so*; the
residual gap is detector-shaped (sherpa vs a competitor's streaming ASR). Races against slower
competition plausibly flip to winnable on transport alone; marquee instant-words do not until the
CTC engine lands.

**The two measurements that convert this reconstruction's estimates into proofs (operator-run):**
1. **Dual-box capture-bakeoff** — same live call captured simultaneously from the CDMX laptop and
   the us-east box (same Twilio account/carrier, one leg each), cross-correlated with the existing
   `capture-bakeoff` cross-delta machinery (INGRESS-BAKEOFF §3, dual-BOX variant of the dual-carrier
   protocol). Output: X_audio directly, with jitter/stall p99 per box. First natural slot: any
   Tier-A published-number call this week.
2. **On-box probe-order** (existing 1¢ non-mention envelope) — pins real `send→hdrs` from us-east,
   converting the "low-teens" prediction into a VERIFICATIONS-grade number.

## Competitor bound (2026-07-17) and the 600 ms roadmap decision

`docs/COMPETITOR-ANATOMY.md` (public-tape × our MS artifacts): the fast player's wave-1 fill lands
**−42 to +2 ms vs the word's end in our own stream** (3 true-positive words, ±25 ms machine-tight),
implying a word-end→fill loop ≈ our-then-transport ≈ **400–600 ms**: phone leg + full-word
streaming ASR + colo'd IOC ladder — ordinary and replicable. OIL/MIDD saw no sweep from anyone and
closed at pennies → likely OUR false alarms, not their misses. Operator decisions 2026-07-17:
**Tier-1 CTC GPU work: NOT NOW** → the 600 ms bar is **not reachable this cycle** (the detector is
~500–700 ms of it; prefix-firing on that engine is also the only move that *beats* wave-1 rather
than tying it). **SIP/RTP direct ingress: approved, in build** (~100–250 ms, provable only by the
dual-leg capture-bakeoff).

## Word-recognition efficiency — status 2026-07-17

**Tier-0 probe re-run (per `docs/CTC-MODEL-SOURCING.md`): still no English streaming-CTC
checkpoint on any shelf.** The sherpa online-CTC model page lists five models as of the
2025-06-30/2025-04-01 releases — all Chinese. icefall's LibriSpeech RESULTS.md contains no
`--causal 1` + CTC-head entry (CR-CTC line is non-streaming only). English streaming zipformers
remain transducer-only. ⇒ **Tier 0 refuted; the several-hundred-ms detector win is blocked on
Tier 1 (GPU finetune of a CTC head onto a causal English checkpoint) — an operator GPU-budget
decision.** No provable English recognition-latency improvement is possible this session beyond the
chunk dial; recording that here per the rule above.

Sources: [sherpa online-CTC models](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-ctc/zipformer-ctc-models.html),
[icefall LibriSpeech RESULTS.md](https://github.com/k2-fsa/icefall/blob/master/egs/librispeech/ASR/RESULTS.md)
