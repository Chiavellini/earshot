# Latency proof ledger — measured verdicts, no claims without numbers

Rule (standing, per operator directive 2026-07-17): every latency/detection change ends here with
an interleaved A/B or a side-by-side measurement on the same hardware — or an explicit
`NOT RESOLVABLE ABOVE NOISE` / `INSTRUMENT-ONLY` verdict. Claims that don't survive measurement are
recorded as such, not dropped.

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
