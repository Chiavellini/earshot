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
| wideband | AXP(0), INTC(44), LMT(11), VZ(0) | 55 | **5.15** |
| **telephony** | **AAPL(14), MSFT(8), QCOM(13)** | **35** | **2.81** |

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
scored against a denominator that was ~3× too small — they flattered the detector
by not counting mentions it missed. Any LMT recall number predating 2026-07-24
05:21 should be treated as optimistic, not as evidence.

⚠ `ultra` is worth a note: on AAL it is a genuine mention (*"ultra premium"*),
while on LMT the collision screen classed `ultra` fires as SUBSTRING hits on
*"ultramaritime"*. **The same keyword is a true positive on one call and an
unwinnable collision on another** — which is exactly why keyword risk is
per-event, not global.

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
