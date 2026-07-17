# Ingress bake-off — operational runbook

Resolves the Audio Ingress Path Selection work order: **which telephony ingress
path delivers earnings-call bridge audio into the detector with the lowest
median and tail latency.** This whole leg is worth 30–150 ms (the phone-vs-
webcast decision, already made, is worth 10–30 s) — calibrate effort
accordingly. Tail latency (p99) is the first priority: we may catch one word in
a 60-minute call, and a 300 ms stall on the wrong frame is a total loss of that
trade. Then median, then call reliability.

The code side (arbiter, signature validation, `latprobe`, `capture-bakeoff`) is
built and tested. This doc is the **field protocol** a human runs on the cloud
box — an agent cannot place live calls on carrier accounts.

## 0. What's built vs. what you run

| Deliverable | Status |
|---|---|
| Dual dial-in arbiter (failover + take-first) | ✅ `src/source/arbiter.rs` |
| `X-Twilio-Signature` validation (no tunnels) | ✅ `src/source/twilio_ws.rs` |
| Capture bake-off harness (jitter/p99/stall + cross-delta) | ✅ `earshot capture-bakeoff` |
| latprobe region/edge RTT | ✅ `earshot latprobe` |
| Raw SIP/RTP adapter | ⛔ **gated** until a winner is named (§5) |
| latprobe run → chosen region | ▶ you run it (§2) |
| Bake-off run → provider→lead table | ▶ you run it (§3) |
| Vendor recommendation w/ runner-up ms cost | ▶ from the data (§4) |

## 1. The options (recap)

- **A — Twilio Media Streams**: μ-law/8 kHz/20 ms over a WS **TCP/443** Twilio
  opens toward us. Native telephony fit; risk is TCP head-of-line blocking (the
  p99 hazard) and no buffer control. No Chicago edge. Built (`twilio_stream`).
- **B — SIP trunk → RTP on the box** (Telnyx / Bandwidth / Twilio Elastic SIP):
  UDP semantics (better tail), we own the jitter buffer, one fewer hop. Adapter
  gated behind this bake-off.
- **C — SIP URI direct to the bridge**: deletes a hop and a vendor; may
  negotiate wideband (G.722/Opus) → better ASR → fire on lower thresholds → an
  indirect latency win. **Check per event/provider** (§6).

## 2. FIRST: latprobe → region (blocking dependency)

The ingress vendor is coupled to the compute region. Do not finalize a vendor
until the region is chosen from numbers.

```
earshot latprobe \
  --targets <twilio-edge-a>:443,<twilio-edge-b>:443,<telnyx-pop>:5060,<bandwidth-pop>:5060,api.elections.kalshi.com:443 \
  --samples 50 --out latprobe.json
```

Reads p50/p95/**p99** TCP-handshake RTT + loss per target. Decision rule from
the work order:
- **us-east-1 (Ashburn)**: Twilio's ashburn edge is same-metro (~1–3 ms, near-
  zero loss) → the TCP objection is largely neutralized → Option A is defensible.
- **Chicago**: no Twilio Chicago edge → Ashburn→Chicago haul (+~20 ms, lossier)
  → Option B with a carrier that has a Chicago POP wins.

Record the chosen region and the measured numbers before proceeding.

**Measured — CDMX box baseline (2026-07-16, `state/latprobe-cdmx-2026-07-16.json`,
50 samples, TCP-handshake RTT):**

| target | loss% | p50 | p95 | p99 |
|---|---|---|---|---|
| api.twilio.com:443 | 0.0 | 32.7 ms | 181.3 ms | **266.4 ms** |
| media.twiliocdn.com:443 | **4.0** | 55.3 ms | 89.3 ms | 97.3 ms |
| sip.telnyx.com:5060 | 0.0 | 291.4 ms | 389.0 ms | 604.2 ms |
| api.elections.kalshi.com:443 | 0.0 | **54.7 ms** | 102.4 ms | 115.1 ms |

Reading: the Kalshi p50 (54.7 ms) is the network floor under the measured
102 ms warm send→hdrs (VERIFICATIONS Q-MSPM); Twilio API tail from Mexico is
poor (p99 266 ms) and the media CDN shows 4% loss — the p99-first decision
rule above is exactly what these numbers threaten. Region decision (us-east-1,
operator-chosen 2026-07-16) awaits the VPS-side latprobe for the "after"
column; the CDMX↔VPS *audio* differential should come from `capture-bakeoff`
cross-correlation on one call captured from both boxes — not from arithmetic
on these handshake numbers.

## 3. The bake-off that settles it

Do not decide from vendor docs. Run both paths into the same box against the
same live call and cross-correlate.

1. Configure ≥ 2 legs as `[[sources]]` (Twilio + a direct SIP leg once its
   adapter lands; until then, two WS-capable carriers). Distinct `carrier`
   labels feed the provider→lead table.
2. Dial the same live earnings call on every leg concurrently.
3. Capture + record + analyze:
   ```
   earshot capture-bakeoff --config <dual-leg.toml> --secs 5400 \
     --record-dir bakeoff/<call-id> --out bakeoff/<call-id>.json
   ```
   Per leg it reports frames, duration, inter-frame jitter p50/p95/**p99**,
   stalls > 100 ms; and the true cross-stream **delta_ms** (recovered by
   cross-correlation on our actual audio + first-frame arrival skew, so it's the
   real inter-vendor latency, not a spec number). `--record-dir` writes
   `<carrier>.wav` + `<carrier>.jsonl` (per-frame `capture_ts`) for offline
   autopsy — opt-in, non-production.
4. **Repeat across ≥ 5 calls and ≥ 2 conference providers** (Chorus Call,
   Notified, Q4, Zoom-backed…). Leads differ by provider — the table is
   per-provider.

Budget ≈ $10 of call minutes. That answers what a week of vendor blogs won't.

### Provider→lead table (fill from the JSON reports — feeds the arbiter)

| Conf provider | Path | median delta ms | jitter p95 | jitter p99 | stalls>100ms | loss events | codec |
|---|---|---|---|---|---|---|---|
| Chorus Call | Twilio | | | | | | μ-law 8k |
| Chorus Call | Telnyx SIP | | | | | | |
| Notified | Twilio | | | | | | |
| … | … | | | | | | |

## 4. Recommendation (state the runner-up's ms cost)

`capture-bakeoff` already ranks legs by p99 tail jitter and emits a
`recommendation` with `runner_up_cost_ms` — the explicit tradeoff. Write it up
as: **"Path X wins; the runner-up Y costs +N ms at p99 (and Δ reliability)."**
The tradeoff must be stated, not asserted.

Then, regardless of the single-vendor winner, **run the arbiter** (§5) — the
min of two independent latency draws beats either vendor alone and buys
failover.

## 5. Build regardless of the winner (already wired)

- **Dual dial-in arbiter** — two carriers, arbiter takes the first frame per
  audio position (`mode = "take_first"`) or fails over (`mode = "failover"`).
  Highest-ROI item on this leg; build it even after one vendor "wins".
  ```toml
  [[sources]]
  kind = "twilio_stream"
  bind = "127.0.0.1:8080"

  [[sources]]
  kind = "sip"          # gated today — see §6
  carrier = "telnyx"
  sip_uri = "sip:…"

  [arbiter]
  mode = "take_first"   # or "failover"
  switch_ms = 200       # failover stall threshold
  align_ms = 2000       # take-first cross-correlation window
  ```
- **No tunnels in production.** WS/RTP terminates directly on the box's public
  endpoint. Validate `X-Twilio-Signature` (set `TWILIO_AUTH_TOKEN`; on by
  default when present). ngrok is loopback-rehearsal only.
- **Jitter buffer is a tunable — sweep it.** Applies to the SIP/RTP path (we own
  the buffer; Twilio owns its own). Start at 40 ms, find the minimum stable
  value, log the choice. `capture-bakeoff --jitter-buffer-ms` is the sweep hook
  (no-op until the SIP adapter lands).
- **Listen-only, always.** No DTMF into the Q&A queue, public dial-ins only.

## 6. SIP path (Option B/C) — gated pre-flight

The raw SIP/RTP adapter is **deliberately not built** until this bake-off names
a winning carrier (a Rust RTP stack is the biggest lift here and the leg is
worth 30–150 ms — don't build it speculatively). `SourceConfig::Sip` parses and
slots into the arbiter; `build_leg` refuses to construct it with a pointer here.

Standing **per-event pre-flight** (Option C), before each call:
1. Does the target bridge expose a SIP URI? (Zoom/Teams/Webex backends often do.)
2. Which codec does it negotiate — narrowband μ-law, or wideband G.722/Opus?
   Wideband is a real, if indirect, latency win (better ASR → lower thresholds).
3. Does the chosen SIP carrier have a POP in the winning region; can RTP anchor there?
4. SRTP/TLS on the bridge — any forced re-encryption/transcoding cost?

When the winner is chosen, the SIP adapter is a fill-in behind `AudioSource`
(own jitter buffer, sweepable; see `docs/layers/source.md`) — switching is a
config change, not a rewrite.

## Deliverables checklist

- [ ] `latprobe.json` → chosen region, with numbers (§2)
- [ ] Per-call `bakeoff/<id>.json` across ≥ 5 calls, ≥ 2 providers (§3)
- [ ] Provider→lead table filled (§3)
- [ ] Written recommendation stating the runner-up's ms cost (§4)
- [ ] Arbiter config in production (dual dial-in) (§5)
- [ ] SIP-URI pre-flight run for the target event (§6)
