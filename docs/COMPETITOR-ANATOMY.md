# Competitor anatomy — MS 2026-07-15, from the public tape

Question answered here: **who is beating us, by how much, with what stack, and what would beat
them.** Every number below is from `state/KXEARNINGSMENTIONMS-26JUL15/report.json` (our 7
detections, wall-anchored via autorun.log) × the public Kalshi trades tape (fetched read-only
2026-07-17, raw JSON archived in the session scratchpad `ms_tape.json`). Method: our detection
wall-time − per-detection `capture→decision` − sherpa's pinned post-word-end decode lag
(400–500 ms, BAKEOFF.md) = the word's end **in our audio stream**; compare tape fills against it.

## The three true-positive races (markets settled ~99¢ = real mentions)

Competitor wave-1 fill vs word-end-in-our-stream (Δ):

| Word | Our decision (Z) | Word-end in our stream | Wave-1 fill | **Δ** | Wave-1 shape |
|---|---|---|---|---|---|
| INVE | 12:42:28.795 | 12:42:28.210–.310 | 12:42:28.287 | **−23 ms** | 10 fills same ms, 68→95¢ ladder |
| TAIL | 12:43:27.034 | 12:43:26.446–.546 | 12:43:26.504 | **−42 ms** | 2 fills, then wave-2 at +124 ms sweeping 84→98¢ |
| RECE | 13:27:10.763 | 13:27:10.172–.272 | 13:27:10.273 | **+2 ms** | 11 fills same ms, 27→95¢ ladder |

**Headline: the fast player's entire hear→decide→fill loop completes at the instant the word
finishes arriving in OUR stream — consistently, ±25 ms across three words 45 minutes apart.**
That consistency is machine timing, not a human. (Our own decision follows ~0.5 s later —
sherpa's decode lag — and our ack another ~0.1 s after that, in the old CDMX numbers.)

## Decomposition — what their Δ≈0 implies

`their_loop = our_then_transport + Δ ≈ our_then_transport − 20 ms`.

Our then-transport (bridge → Twilio conference fork → media-stream WS → ngrok → CDMX, 4% loss
path) is unmeasured but bounded ~300–800 ms. Therefore **their word-end→fill loop ≈ 300–800 ms,
most plausibly 400–600 ms**. That admits a perfectly ordinary architecture:

- **Phone-bridge participant leg** (webcast is tens of seconds behind — excluded), likely direct
  RTP/SIP or equivalent low-latency capture: ~50–150 ms;
- **Streaming full-word ASR** (Deepgram/RT-class or self-hosted small streaming model) emitting
  ~200–400 ms after word end — **NOT prefix-firing**: they land at/after word-end + their
  transport, never before;
- **Pre-staged IOC ladder sweep, colocated**: the 10–11 fills share one millisecond timestamp
  and walk the whole book in one order — exactly our own executor design; order flight ~5–20 ms.

Wave structure: **wave-2 at +120–280 ms** (INVE +261/+265 incl. an 812-lot at 96¢; TAIL +124;
RECE +122/+235) — a second tranche or a second, slightly slower fast player mopping 95–98¢.
**Wave-3 at ~+1.0–1.6 s** — the slow crowd at 98–99¢. The race is over ~300 ms after word-end
at the bridge.

## The two non-races (OIL, MIDD) — a finding about US, not them

At our OIL detection (12:51:50, best_ask 21¢) and MIDD detection (13:21:13, best_ask 37¢):
**no sweep ever came — from anyone.** OIL's next trade was 13 minutes later, mixed 26–67¢;
MIDD never traded again until 13:30, and **both markets printed at 1–5¢ YES near the close**.
Markets that end at pennies did not contain the mention. The parsimonious read: **those two
detections were sherpa false alarms** (or non-qualifying word forms under the market rules) —
the fast player didn't "skip" them; they were right and we were wrong. Open item: confirm
against the call transcript/capture. Consequence for strategy: the "win the words the fast
player skips" thesis is NOT supported by this event — on true mentions they showed up all
three times.

## Replicability matrix

| Component (inferred) | Their status | Our status | Verdict |
|---|---|---|---|
| Low-latency phone leg (RTP-class) | have | Twilio WS fork (+unmeasured fork latency); **SIP/RTP adapter approved & in build** | **Replicable — in progress** |
| Streaming ASR, ~200–400 ms after word-end | have | sherpa ~900 ms post-word-end (measured); streaming_ctc engine built, **model blocked on Tier-1 GPU — deferred by operator 2026-07-17** | **Replicable — deferred**; the single biggest term |
| Pre-staged IOC ladder sweep | have | have (proven µs-scale, warm-fired) | **Parity today** |
| Colo near Kalshi | have | have (us-east, 2.2 ms) | **Parity today** |
| Word-selection precision (no FA fires) | evidently good | 2 likely FAs of 7 on MS | Ours needs the validation loop / better detector confidence |

## What BEATS them (not just matches)

They fire on completed words. A safe **prefix fire** (PREFIX-SAFETY method; streaming_ctc reads
raw posteriors) triggers on "recess-" ~250–400 ms **before word-end** — landing our IOC while
their ASR is still waiting for the word to finish. Stack math (word-end-relative fill time):

| Stack | Est. fill vs word-end at bridge | vs their wave-1 (~+300–500 ms) |
|---|---|---|
| Today (Twilio+sherpa, us-east) | ~+1.0–1.3 s | lose by ~600–900 ms — matches the operator's 600 ms bar |
| + SIP/RTP (in build, ~100–250 ms recovery, to be measured) | ~+0.8–1.1 s | still lose — transport alone cannot close it |
| + streaming_ctc full-word (GPU, deferred) | ~+0.25–0.45 s | **tie / trade wave-1 with them** |
| + prefix firing (same engine, same GPU work) | **~−0.1–+0.1 s** | **beat wave-1 outright** |

**Bottom line: their strategy is ordinary and fully replicable; the one lever that overtakes them
(prefix-firing CTC) is the same GPU-gated detector work, and nothing else sums to the 600 ms bar.
SIP is worth building (this build) and necessary, but it is ~a third of the gap at best.**

## Open verification items

1. Transcript check on OIL/MIDD (false-alarm confirmation) — needs the MS capture/transcript.
2. `our_then_transport` absolute measurement (dual-box capture-bakeoff, LATENCY-PROOF) tightens
   the their-loop bound from ±250 ms to ±50 ms.
3. Re-run this analysis on the next event's tape (DPZ/GEV/GOOGL) — one event = one sample of
   the competitor population; wave-2's existence suggests ≥2 fast players.
