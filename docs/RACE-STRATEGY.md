# Race strategy — phone + webcast lanes (2026-07-20)

Question answered here: **how far behind are we on each lane, what are the fast
players doing there, and what sequence of work beats them.** This is the
cross-lane synthesis; the per-tape competitor evidence lives in
`COMPETITOR-ANATOMY.md`, the measurement ledger in `LATENCY-PROOF.md`, and the
executable task list in `RACE-CHECKLIST.md` (items cited here as X#/P#/W#).

Every number in this doc carries one of three labels:

- **[measured]** — pinned in a repo doc; the citation names it.
- **[hypothesis]** — a pre-registered claim awaiting its test.
- ~~[measured, un-pinned — X1]~~ — **retired 2026-07-20**: X1 completed;
  the paced-bench numbers are pinned in BAKEOFF.md §"streaming_ctc paced
  latency" and the raw artifacts archived at `state/bench/bench-paced.{json,log}`.

## 1. Standings snapshot — what is measured today

**Phone lane.** The MS 07-15 tape decomposition (COMPETITOR-ANATOMY
§Decomposition) puts the fastest competitor's word-end→fill loop at ~400–600 ms
[measured, bounded]. Our own post-migration replay verdict
(LATENCY-PROOF §"Race reconstruction — MS-RECE"): order path at parity
(us-east 2.2 ms RTT, warm-fire proven), residual deficit **~+300–400 ms,
detector-shaped** [measured + arithmetic estimate; X_audio still unmeasured].
sherpa's Fast p95 is 1066 ms against the 350 ms bar (BAKEOFF §holdout paced)
[measured].

**Webcast lane.** Absolute webcast lag is deliberately **unquoted** — the
earlier ~18–30 s figure was retracted as unsourced (RUNBOOK, top note), and the
standing rule is *quote no number until it is measured*. What IS measured
(RUNBOOK §Same-floor W1-pre, item 4): the acquisition ladder on a live
reference stream — tuned ffmpeg **−10.6 s** vs default, streamlink a further
**−1.1 s** with the best stall profile [measured]. The first real-venue
numbers were scheduled to come out of the DPZ 07-20 four-leg capture; as of
this writing its outcome is **unconfirmed** — retrieval + analysis is item X2
and is the single most valuable pending measurement in the program.

## 2. Paced-bench update — the current CTC export does NOT buy the tie

**[measured — BAKEOFF.md §streaming_ctc paced latency]** The detached paced
dev bench (real-time replay, AAPL-Q2-2026, ~1.0 h) completed 07-18 00:24 and
was first read 07-20:

- **streaming_ctc shows ~no onset→emit latency cut over sherpa_kws at the
  current 320 ms-quantum export.** tariff Fast p95: **1294 ms (ctc) vs
  1017 ms (sherpa)** against the **350 ms bar**; guidance Confirmed p95:
  2107 vs 1785; p50s ≈ equal (~970–1020 ms).
- streaming_ctc tariff **Confirmed regressed**: recall 4/9 (0.44), 3 FAs,
  precision 0.57 (Fast tier stayed 6/9, 0 FA).
- Thin basis: one call, ~1 hour, only two phrases produced latency rows.

**What this invalidates:** the "SIP + full-word CTC ties their wave-1 at zero
GPU spend" stack-math row. The model's accuracy profile (half sherpa's FAs on
the 07-17 dev pass, BAKEOFF) survives; its latency advantage does not exist
*as exported*. The geometry argument (0–80 ms lookahead vs sherpa's ~320 ms)
was not realized — plausibly because the 320 ms decode quantum dominates
exactly as sherpa's does.

**Pre-declared follow-on ladder** (each is a checklist item):

1. **P1** chunk-16 re-export → 160 ms decode quantum → re-run the paced bench.
2. **P2** scorer-side keyword boost (attacks the tariff telephony-domain gap).
3. **P3** prefix-fire diagnosis — **DONE 2026-07-21, negative**: prefix-firing
   is not the overtake lever for this vocabulary (§2b).
4. **P4** GPU finetune go/no-go — a $-decision; P1–P3 results are its inputs.
5. **G2** broaden the model family beyond zipformer (§2b) — the real lever.

## 2b. The reframe — what the latency actually decomposes into (2026-07-21)

Two results this session force a correction to the whole detector thesis:

- **P3 (done):** prefix-firing gives ~32 ms, not 250–400 ms — the earnings
  vocabulary has no safe early prefix (BAKEOFF §prefix-fire diagnosis). So we
  **cannot fire before word-end** on these words.
- **Every detector lands ~1000 ms onset→emit regardless of decode quantum**
  (sherpa and CTC both; chunk 80→40→ the 320 ms export all ≈ equal). The
  quantum is NOT the dominant term.

The decomposition that explains both: for a **full-word** detector,
`onset→emit ≈ word_duration + decode_lag`. "tariff" is ~500 ms of audio; add
~450 ms decode-lag (320 ms quantum + ~130 ms right-context) → ~950 ms. The
**word_duration term is irreducible without prefix-firing** — which is why the
350 ms onset→emit bar effectively *demands* prefix-firing and is likely
unreachable for this vocabulary by any full-word model.

**But beating the competitor does NOT require the 350 ms bar.** The competitor
also hears the same word and also fires at **word-end + their ~200–400 ms ASR
lag** (MS tape: Δ = −42..+2 ms vs word-end, never earlier — they do not
prefix-fire either). `word_duration` is **common to both of us**, so it
cancels: the race is decided by `decode_lag + transport`. We have already won
transport (2.2 ms colo vs their bridge+network). **So the entire competitive
question reduces to: is our full-word decode-lag shorter than their ~200–400 ms?**

- Current sherpa/CTC decode-lag ≈ 450 ms → we lose the detector leg by
  ~50–250 ms (consistent with the observed ~400 ms total deficit before colo).
- A 2026 **80 ms-quantum** streaming model (Parakeet-Realtime-120M /
  Nemotron-0.6b, G2) was HOPED to cut decode-lag to ~150–250 ms → tie-to-beat.
  **Fair re-bench DONE 2026-07-21 (BAKEOFF §A.1/A.2): decode-lag ~420 ms is STRUCTURAL.**
  Corrected the earlier unfair test — and Parakeet-EOU's decode-lag is invariant across
  `blank_penalty` (0/−1/−2 identical) AND audio domain (telephony 8 kHz == wideband 16 kHz):
  tariff pins to 2× 160 ms chunks past word-end every time. So the model swap does NOT beat
  sherpa on decode-lag, and the tuning levers don't help. (The RTF "too slow" claim was
  WITHDRAWN — laptop artifact.) Only the **TDT** variant is untested; trend strongly negative.
  **Firming conclusion: decode-lag ~350–450 ms looks like an irreducible floor for full-word
  streaming ASR on this vocabulary (sherpa, streaming_ctc, Parakeet-EOU all land there).** The
  detector likely lets us MATCH competitors (same physics), not dramatically beat them — the
  beatable edge is the OTHER terms (SIP audio transport, us-east-2 + FIX execution), where we
  are behind but which ARE closeable. This reshapes the program: stop chasing a magic detector;
  win the transport + execution races we've measured.

This is the answer to "can we beat the fast players on a level field": yes, and
the single lever is a **faster full-word detector** (G2), not prefix-firing and
not the 350 ms bar. Re-tune the internal bar to a **decode-lag target ≤ ~200 ms**
(the competitive quantity), measured onset→emit minus word_duration.

## 3. Phone lane — the winning stack, revised

The stack table stays in COMPETITOR-ANATOMY §"What BEATS them" (single source
of truth); a dated amendment box there now records the §2 result. Revised
reading:

- **SIP/RTP** (P5→P6, the approved task-12 build): recovers ~a third of the
  gap at best [measured-bounded, COMPETITOR-ANATOMY]. `build_leg` stays
  carrier-gated until the ingress bake-off names a winner
  (INGRESS-BAKEOFF §5–6). Necessary, not sufficient.
- **+ full-word streaming_ctc → "tie wave-1"** is now **CONTINGENT on P1–P3**;
  it is not available with the current export [measured, BAKEOFF §streaming_ctc
  paced latency].
- **+ prefix-firing → beat wave-1: RETIRED as the overtake lever (P3 done
  2026-07-21).** The mechanism is built and safe, but the earnings-mention
  vocabulary affords no early safe prefix — the compiled cuts drop only the
  final BPE token (tariff `▁T AR I`|`FF`; recession drops `ION`; guidance/
  partnership have no safe prefix at all), so the measured head start is
  ~32 ms, not the 250–400 ms the thesis needed [measured, BAKEOFF
  §"streaming_ctc prefix-fire diagnosis"]. Firing before word-end is not
  available for these words.
- **The remaining overtake lever is a LOWER-LATENCY full-word detector**, not
  prefix-firing: smaller decode quantum (P1 chunk-16, ~150 ms off the floor)
  and/or a faster model family (G2 model-breadth search — Parakeet/Moonshine/
  etc., benched behind the `Detector` trait). To beat a 200–400 ms full-word
  competitor ASR we need our own full-word emit at or below that, since we
  cannot fire mid-word on this vocabulary.
- Order path, colo, warm-fire: **parity, done** [measured].

Bottom line, sharpened: *the detector is still the only term that sums to the
600 ms bar, but the path is now "faster full-word detector" (P1 + G2), not
"prefix-firing" — that lever is measured out for this keyword set.*

## 4. Webcast lane — the gap inventory (what we lack on webcast-only events)

Frame (RUNBOOK §Same-floor): on webcast-only events the encode+CDN wall is
identical for every listener — the race is **relative** lag between webcast
listeners. Our deficit terms:

| # | Term | State | What closing it takes |
|---|---|---|---|
| 1 | Absolute floor (encoder→edge) | UNMEASURED (retracted estimate; Sky reference PDT ~36 s [measured, reference-only]) | X2 (DPZ data) + W2 (per-venue probe). Not itself competitive — but a **faster public simulcast lowers the floor for whoever finds it** [hypothesis → W4] |
| 2 | Client fetch | Ladder MEASURED: tuned ffmpeg −10.6 s, streamlink −11.7 s vs default (RUNBOOK §W1-pre #4); never yet measured on an event venue | X2/W1 confirm on real venues; remaining headroom = **LL-HLS partial segments** (sub-second class) — fetcher designed-not-built, venue-gated (W3) |
| 3 | Vantage | CDMX: CGNAT breaks direct-manifest fetch; YouTube bot-walls datacenter IPs (RUNBOOK §W1-pre #1–2) [measured] | us-east box: CDN proximity + no bot-wall on corporate players (Notified/Q4) (W6, user's AWS) |
| 4 | Detector | sherpa Fast p95 1066 ms [measured]; streaming_ctc = no cut as exported [measured, BAKEOFF §streaming_ctc paced latency] | Same P1–P4 ladder as the phone lane — **one detector program serves both lanes**. A competitor-class ASR is 200–400 ms post-word-end → this term alone is ~0.7–1.0 s |
| 5 | Fire/order | Parity (µs-scale, proven) [measured] | nothing |
| 6 | Posture | observe-only enforced (`launch-webcast.sh` accepts no fillable flags) until lag is measured | W7, gated on X2 + W1 numbers |

Webcast-specific assets we already hold: **16 kHz wideband audio** — the CTC
model's training domain, where the telephony tariff gap is expected to shrink
[hypothesis until W5: no wideband ground-truth corpus exists yet; NFLX 07-16
is the seed] — and the **dual-leg arbiter**, already built for the phone lane,
reusable for multi-feed racing.

## 5. Webcast lane — how a faster player operates there

On a webcast-only event the bridge edge that beat us on MS **does not exist**;
whatever wave-1 shows up must derive its lead from feed selection, fetch
strategy, vantage, or a channel we haven't mapped. The competitor model —
three pre-registered hypotheses (co-listeners / hidden faster channel / same
feed, better vantage) and the **tape-anatomy discriminator** that separates
them — lives in COMPETITOR-ANATOMY §"Webcast lane — competitor hypotheses",
with the decision rule pre-declared before the first webcast tape is read.

**MEASURED 2026-07-22 (TSLA live observe-arm vs tape — the discriminator has
its first real data): steady-state Δrace = −8.7 s mean (−7.3/−8.5/−10.3, n=3)**
from a CDMX laptop with a DEFAULT yt-dlp edge join. Verdict **H1/H3: the fast
population are co-listeners on the same public feed — the webcast race is
winnable.** Two structural artifacts dominated everything else: (a) fetcher
JOIN POSITION — joining behind the live edge cost 2–4 minutes until catch-up
(~1.5×); join-at-edge is worth more than every other lever combined; (b)
**recall/precision on webcast audio is the second axis**: live recall 8/11 on
raced YES words (TARI leg-dependent — different legs hear different audio;
SAFE/TARA true misses), precision 8/11 (EURO systematically confused). The
fast money's own precision is poor (MERG 27→85→7¢ unwind; NANO/AUTO on
GOOGL) — a precise detector monetizes their unwinds. Lever order for the
webcast lane is therefore: edge-join + tuned leg → multi-leg OR'd detection
→ US vantage (X_audio still unmeasured) → detector recall on
compounds/OOV words (SAFE/TARA class) → decode-lag (small at this scale).
Levers 1–3 were BUILT 2026-07-22 evening as the production armed posture —
`[arbiter] mode = "fanout"` (per-leg detect chains OR'd at the one-shot
decision table; NOT arbiter reuse — the arbiter collapses to one stream,
fanout keeps N), `configs/webcast-fanout-arm.toml` (streamlink edge-join +
tuned ffmpeg), `launch-webcast.sh --wait-live` (live gate outside the arm).
Live verdicts pending per RACE-CHECKLIST W1 gates (G3 Thu/Fri mixed-leg,
G4 FOMC 07-29 vs the −8.7 s baseline).
**G3 MEASURED (LMT 07-23, first fanout race, autonomous):** Δrace GOLD −0.6 s /
MORF −1.0 s / EURO −2.8 s (mean −1.5 s) — ~7 s recovered vs the −8.7 s baseline,
sign not yet flipped. Root cause named: **short-manifest-window venues (Notified/
CloudFront, 40 s window) compress the fetch ladder to −1.2 s and reverse the leg
order (tuned beats streamlink)** — replicated same-morning on AAL. The remaining
gap is decode-lag + vantage sized, so G4's levers are the US-vantage arm and
detector decode, not fetch flags. Full tables: COMPETITOR-ANATOMY §Per-tape LMT/AAL
07-23.
Full tables: COMPETITOR-ANATOMY §Per-tape TSLA/GOOGL 2026-07-22.
First subjects: the Wed 07-22 events (GOOGL attended; TSLA per the same-floor
W0 plan); DPZ's virtual race (X2) is retroactive sample #1.

**Replicate-then-beat sequence:**

1. **Match the floor** — fastest public feed per event (simulcast scan, W4
   recon), tuned-ffmpeg/streamlink ladder (done), LL-HLS native fetch where a
   venue serves it (W3), near-CDN vantage (W6), multi-feed min-lag racing
   through the arbiter.
2. **Beat the floor** — the detector: prefix-firing on wideband posteriors
   overtakes ANY co-listener, because their ASR (like the phone-lane
   competitors') waits for the word to finish. Same P1–P4 investment.
3. **Escape the floor** — if the discriminator says H2 (someone holds a
   faster channel), the answer is recon, not latency: the phone-first bridge
   hunt (registration links, IR asks, legacy 8-K numbers — the standing
   protocol) plus W4's caption/simulcast channel mapping.

## 6. Sequencing and gates

```
X1 (transcribe bench)  ──────────────►  P2, P4 read from it
X2 (DPZ retrieval)     ──────────────►  W7 (fillable unlock), venue table (W2)
P1 → P2 → P3 → P4 (detector ladder; serves BOTH lanes)
P5 (carrier bake-off)  ──────────────►  P6 (SIP build)  ──► W3 (fetcher: same src/source, build after)
W1 (07-22 tapes)       ──────────────►  discriminator verdict → W4 priority, W7
W5 (wideband corpus)   ──────────────►  wideband CTC bench (webcast detector claim)
W6 (us-east vantage)   — user's AWS account; prerequisite for real webcast races
```

User-held gates: P4 (GPU $), P5 (carrier choice), W4 (web recon), W6 (AWS),
W1 (attendance). Everything else is session-executable in order.

## 7. Execution arm — the measured region/CDN deficit (2026-07-21)

The standing belief "execution = colo parity today" is **falsified by measurement**
(VERIFICATIONS Q1 probe). Our client machinery (presigned headers, warm h2 socket,
pre-serialized body, single-IOC-at-cap sweep, warm-fire) is genuinely µs-scale and at
parity with the competitor's *design* — but it is siting-blind:

- The order host `api.elections.kalshi.com` is a **CloudFront edge**; the "2.2 ms" was a
  CDN pop, not the engine.
- The engine is in **us-east-2 (Ohio)**; our box is **us-east-1 (Virginia)** → true
  box→engine RTT **~12–17 ms** (measured handshake RTTs).
- **FIX** is the direct, CDN-free path to us-east-2 and Kalshi's intended HFT channel;
  we use REST ("REST-only at our tier", `rest.rs`).

Levers, by leverage (all operator-approved 2026-07-21): **E1** move box → us-east-2
(~10–15 ms, cheapest big win, independent) → **E2** FIX order-entry client behind the
`create_order_presigned` seam (structural best; Premier-tier-gated — email
institutional@kalshi.com) → **E3** PrivateLink (Premier+) → **E4** rate-tier upgrade →
**E5** REST µs residuals (pre-encoded frame, lock-free risk gate, deferred ack parse).
Honest ordering: E1+E2 are ~10 ms-class and dominate; E5 is µs. We had been polishing µs
while a region + a CDN from the engine. Full plan: the 2026-07-21 execution-arm plan file.

**E2 status (2026-07-21 evening): the offline build is DONE** — transport seam
(`src/kalshi/transport.rs`), full hand-rolled FIX stack (`src/kalshi/fix/`),
config-gated (`[kalshi] transport = "fix"`, default rest, REST behavior
byte-identical), all suites green incl. an executor-through-FIX mock and the
dual-wire ambiguous fallback. What's left is exactly the operator-gated live
ladder: FIX/Premier access → demo logon (resolves the 8 pinned open questions,
VERIFICATIONS §Q-FIX) → 1¢ probe → FIX-vs-REST send→ack A/B. Until that A/B
exists, FIX's latency win over REST-from-us-east-2 is a HYPOTHESIS (structural:
CDN-free + persistent session + no per-request TLS/signing; measured only as
host-level RTT ~0.8 ms vs ELB ~2 ms from us-east-2).
