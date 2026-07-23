# Race execution checklist (durable, cross-session)

Companion to `RACE-STRATEGY.md` (why these items, in what order). One item =
one falsifiable outcome with one evidence destination.

**Rules**
- **No item is checked until its `Evidence →` destination is updated.** The
  check and the doc edit are one atomic act.
- Gates: `user` (operator decision/hands), `box` (needs the us-east or CDMX
  box), `venue` (depends on what an event's platform serves), `$-decision`
  (paid spend, operator-held), `measurement:<id>` (blocked on another item's
  result), `none` (executable now).
- Sessions work items top-to-bottom within their lane; cross-lane items first.
  Suitable for the `run-tasks` skill: point it at this file.

## Cross-lane (do first)

- [x] **X1.** *(done 2026-07-20 — BAKEOFF §"streaming_ctc paced latency"; raw archived `state/bench/`; verdict: NEGATIVE, no cut, bar fails ~4×)* Transcribe `bench-paced.{json,log}` into BAKEOFF.md (streaming_ctc
      paced results table + verdict vs the 350 ms Fast-p95 bar + the
      pre-declared P1–P3 follow-on ladder).
      Gate: none — **URGENT**: source files live in an ephemeral scratchpad,
      `/private/tmp/claude-501/-Users-bernardodelrio-Code-ear/ce50cb31-5422-4293-bb72-f31e8f7a76ea/scratchpad/`
      (copy into the repo's evidence convention as part of this item).
      Done when: BAKEOFF.md carries the full table and every
      "[measured, un-pinned — X1]" label in RACE-STRATEGY.md /
      COMPETITOR-ANATOMY.md is flipped to a BAKEOFF citation.
      Fallback if the files are gone: demote those numbers to "reported,
      evidence lost" and re-run the bench (becomes part of P1).
      Evidence →: BAKEOFF.md results section.

- [x] **X2.** *(done 2026-07-20 — outcome: capture NEVER RAN, box never started Sun/Mon; recorded RUNBOOK §DPZ outcome. Tape still fetched + analyzed tape-only → COMPETITOR-ANATOMY §Per-tape DPZ: verdict UNCONTESTED-to-THIN, one machine sweep all call, SOCC/PARM never raced. W1 becomes the first Δw sample.)* DPZ 2026-07-20: confirm whether the 4-leg capture ran; retrieve
      `~/dpz-capture/` artifacts (WAVs, jsonl, report.json,
      manifest-probe.txt) from the box; run the post-call analysis — xcorr
      leg deltas, manifest-probe verdict (cadence / LL-HLS / PDT lag), replay
      detection, and the VIRTUAL RACE vs the public Kalshi tape (Q-MSPM).
      Gate: box.
      Done when: first measured webcast-lag numbers are in RUNBOOK (its
      §DPZ post-call block is their single home) AND a DPZ subsection exists
      in COMPETITOR-ANATOMY §Per-tape results with an H1/H2/H3/uncontested
      verdict. If the capture never ran: record that plainly in RUNBOOK and
      W1 becomes the first sample.
      Evidence →: RUNBOOK.md (lag numbers) + COMPETITOR-ANATOMY.md §Per-tape
      results (race verdict). No double-homing.

## Phone lane

- [x] **PB0.** *(safety hardening done 2026-07-21)* Listen-only capture
      invariant made a hard, tested guarantee before any live participant-bridge
      dial: audio one-way by construction + WS receive-only (tested); DTMF
      passcode-entry only, `*` (raise-hand/unmute) hard-rejected in
      `twilio-dial.sh` + boundary scripts + `tests/twilio_listen_only.rs`;
      docs reconciled to one invariant (TWILIO/INGRESS-BAKEOFF/DIAL-INS).
      Evidence →: docs/TWILIO.md §"Listen-only capture invariant".

- [ ] **PB1.** Per-event participant-bridge hunt (the low-latency lane). Mine,
      in order: the **earnings press release / 8-K exhibit 99.1** (often carries
      the participant dial-in or a registration link — the user's insight), the
      **IR event-detail page**, then registration vendors (vevent / DiamondPass /
      media-server). Register truthfully (real name + Actinver) ONLY where a
      bridge requires it; listen-only; never queue a question; no analyst-coverage
      claim. Passcode must be `[0-9#]` (else PB0 refuses — skip a `*`-entry bridge).
      Gate: user (registration submits identity). Recurring per event.
      Done when: number/PIN + source recorded in `configs/dial-ins.toml` +
      `docs/DIAL-INS.md` with date + access class.
      Evidence →: configs/dial-ins.toml + docs/DIAL-INS.md.

- [ ] **P1.** chunk-16 re-export of the streaming-CTC model (160 ms decode
      quantum, per CTC-MODEL-SOURCING follow-on) + re-run the paced bench.
      **Re-scoped 2026-07-21:** a modest absolute-latency trim (~150 ms), NOT
      an overtake (P3 showed prefix can't fire early); lower priority than G2b.
      Gate: none (laptop-runnable; ~2 h detached real-time replay).
      Done when: new paced Fast p95 recorded with an explicit verdict vs the
      350 ms bar and vs sherpa's 1066 ms.
      Evidence →: BAKEOFF.md.

- [ ] **P2.** Scorer-side keyword boost (attacks the tariff telephony-domain
      gap) + re-bench accuracy on the dev split.
      Gate: none. Depends: X1 (baseline table pinned first).
      Done when: tariff recall movement measured at fixed FA/h, verdict
      recorded.
      Evidence →: BAKEOFF.md.

- [x] **P3.** *(done 2026-07-21 — VERDICT: prefix-firing is NOT the overtake
      lever for the earnings-mention vocabulary. The Fast tier already IS the
      prefix tier and was already in the paced bench; compiled cuts drop only
      the final BPE token → ~32 ms head start, not 250–400 ms; guidance/
      partnership have no safe prefix. Overtake now = lower-latency full-word
      detector, P1 + G2. BAKEOFF §"streaming_ctc prefix-fire diagnosis".)*
      Prefix-fire diagnosis: measure the prefix path + recompute token cuts.
      Evidence →: BAKEOFF.md + PREFIX-SAFETY.md.

- [ ] **P4.** GPU finetune go/no-go (Tier-1, CTC-MODEL-SOURCING).
      Gate: $-decision (user). Depends: P1–P3 (its inputs).
      Done when: operator decision recorded with the P1–P3 numbers it was
      based on.
      Evidence →: CTC-MODEL-SOURCING.md status header + LATENCY-PROOF.md.

- [x] **G2a.** *(done 2026-07-21)* Model-breadth recon beyond the zipformer
      family. Ranked shortlist pinned: (1) NVIDIA Parakeet-Realtime-EOU-120M
      (80 ms quantum, Rust+ort ref exists), (2) sherpa KWS chunk-8 (160 ms),
      (3) Nemotron-0.6b. Prefix-firing retired ⇒ RNN-T families now eligible.
      Evidence →: CTC-MODEL-SOURCING.md §"Beyond CTC".

- [~] **G2b.** *(RE-OPENED 2026-07-21 — first bench was UNFAIR, corrected re-run in
      progress.)* Parakeet-Realtime-EOU-120M first bench gave decode-lag ~320–420 ms
      / RTF 0.68×, but: RTF **WITHDRAWN** (measured on laptop not server — a GPU host
      runs a 120M model many× real-time); decode-lag under default `blank_penalty`,
      telephony audio, and the EOU (not TDT) variant. Corrected experiments: **(A)**
      blank_penalty sweep × TDT variant × wideband 16 kHz (laptop-valid, decode-lag
      is CPU-independent); **(B)** real RTF on a briefly-provisioned us-east-2 GPU
      (g5.xlarge), terminated after. Verdict PENDING fair numbers.
      Done when: decode-lag table {penalty×variant×domain} + a real-host RTF recorded.
      Evidence →: BAKEOFF.md §"G2b — Parakeet".

- [x] **E0.** *(done 2026-07-21)* Execution-arm audit + record correction. The
      "2.2 ms colo parity" was a CloudFront edge; engine is us-east-2, box is
      us-east-1 (~12–17 ms). Pinned VERIFICATIONS Q1; corrected LATENCY-PROOF +
      COMPETITOR-ANATOMY; RACE-STRATEGY §7. Evidence →: VERIFICATIONS Q1.

- [~] **E1.** Move the trading box us-east-1 → **us-east-2** (engine's region).
      **Network-RTT recovery MEASURED 2026-07-21** (throwaway us-east-2 probe,
      terminated): direct us-east-2 ELB ~2 ms / FIX host ~0.8 ms vs ~12–17 ms
      from us-east-1 → **~11–14 ms recovery confirmed** (VERIFICATIONS Q1,
      `state/latprobe-useast2-2026-07-21.json`). REMAINING: relocate the real
      trading box (t3a.large replica, full setup) + a 1¢ non-mention probe-order
      from us-east-2 to pin live send→ack. Reuse the kept `earshot-useast2` key
      pair + SG. Gate: user (full box + probe-order approval).
      Done when: us-east-2 probe-order send→ack pinned in LATENCY-PROOF.
      Evidence →: LATENCY-PROOF.md + VERIFICATIONS.

- [~] **E2.** FIX order-entry client — **OFFLINE BUILD DONE 2026-07-21**:
      `OrderTransport` seam extracted (`src/kalshi/transport.rs`; RiskGate/
      kill-switch untouched), hand-rolled FIXT.1.1 stack in `src/kalshi/fix/`
      (encoder + golden vectors, zero-copy parser, sans-io session machine,
      tokio TLS driver, mock-server + executor-through-FIX suites green),
      behind `[kalshi] transport = "fix"` (default rest). Facts + 8 open
      questions pinned in VERIFICATIONS §Q-FIX; layer doc
      docs/layers/fix-transport.md.
      **Remaining (live):** Gate: **Premier tier / FIX key** — user emails
      institutional@kalshi.com (draft prepared 2026-07-21). Then: demo logon
      (resolves OQ1–OQ8) → prod logon → 1¢ IOC via FIX under the $1 ledger →
      FIX-vs-REST send→ack A/B from the same box.
      Done when: that A/B is pinned. Evidence →: VERIFICATIONS §Q-FIX + LATENCY-PROOF.

- [ ] **E3.** AWS PrivateLink for the FIX session (Premier+). Depends: E2.
- [ ] **E4.** Rate-tier upgrade as volume grows (Basic→Advanced→…). Gate: volume.
- [ ] **E5.** REST µs residuals (only if REST stays for some markets): pre-encoded
      request frame (URL+headers as bytes), lock-free risk gate, deferred ack JSON
      parse. Dwarfed by E1/E2 — do last. Evidence →: src/kalshi/rest.rs, src/risk.rs.

- [ ] **P5.** Ingress bake-off: run the carrier candidates per
      INGRESS-BAKEOFF §3–6 and name the winning SIP carrier.
      Gate: user + venue (live calls).
      Done when: a carrier is named in INGRESS-BAKEOFF with measured jitter/
      latency evidence.
      Evidence →: INGRESS-BAKEOFF.md §5–6.

- [~] **P6.** SIP/RTP adapter. **MEDIA PLANE BUILT 2026-07-21** (`src/source/sip.rs`):
      UDP RTP receive + owned jitter buffer (sweepable, 40 ms) + µ-law decode +
      `AudioFrame` emit, behind the frozen `AudioSource` trait; `build_leg` unlocked
      for `SourceConfig::Sip` when `rtp_bind` set; `--jitter-buffer-ms` now real;
      12 tests green (loopback UDP + units). docs/layers/source.md Adapter #5.
      REMAINING (gated): SIP **signaling** (REGISTER/INVITE) needs the carrier
      (measurement:P5); then dual-leg capture-bakeoff xcorr for X_audio.
      Evidence →: docs/layers/source.md + LATENCY-PROOF.md.

## Webcast lane

- [~] **W1.** Week-of-07-20 webcast study captures + tape-anatomy discriminator —
      **DELIVERED 2026-07-22 on the designed samples**: TSLA captured + LIVE
      observe-arm → first measured Δrace (steady-state **−8.7 s mean**, verdict
      **H1/H3 co-listeners, winnable**); GOOGL captured (partial) → replay Δw
      incl. RECA at-or-ahead of the winning burst. Recall 14/18 + precision
      issues measured (both events). ALLY/GEV/ALK ended NO CAPTURE (tape-only,
      all CONTESTED). Evidence: COMPETITOR-ANATOMY §Per-tape 07-22 entries.
      Remaining for full W1 closure: AAL/INTC Thu + AXP/VZ Fri captures.
      NEW measured lever order (from the Δrace decomposition): edge-join at t0
      → tuned/streamlink leg → multi-leg OR'd detection → US vantage (X_audio
      still unmeasured) → compound/OOV recall (SAFE/TARA/TAIL/NVID class).
      **Levers 1–3 BUILT 2026-07-22 evening** (pending live verdicts): the
      fanout pipeline (`[arbiter] mode = "fanout"`, one detect chain per leg
      OR'd at the shared one-shot table — `pipeline::run_pipeline_fanout`,
      `tests/e2e_fanout.rs`), the armed edge-join template
      (`configs/webcast-fanout-arm.toml`: streamlink `--hls-live-edge 1`
      primary + `-live_start_index -1` tuned-ffmpeg backstop,
      `scripts/stage-webcast-arm.sh`), and the codified live gate
      (`launch-webcast.sh --wait-live`, RUNBOOK 07-22 post-mortems #1–#3).
      Verdict gates: **G2 box smoke ✅ PASSED 2026-07-23 04:01 UTC** (DW live,
      2 legs 0 drops, cross-leg dedupe live-proven — RUNBOOK §Monitor
      staging) → **G3 ✅ RUN 2026-07-23 (LMT, autonomous)**: (a) fanout full
      live event PASS (99 min, 0 drops, 1 intent/kw, dedupe 16/16);
      (b) OR-mechanism proven, no leg-differential word this event;
      (c) --wait-live production mechanism PASS (after 3 launcher/probe
      fixes — see RUNBOOK); (d) **event-venue ladder MEASURED: tuned
      −1.18 s, streamlink −0.21 s vs default — the reference-stream ladder
      COMPRESSES at short-window venues and the leg order REVERSES**;
      (e) Δrace GOLD −0.6 s / MORF −1.0 s / EURO −2.8 s (mean −1.5 s vs
      −8.7 s baseline: ~7 s recovered, sign NOT yet flipped; residual is
      decode+vantage-sized). LMT settlements: NASA YES (our true flag beat
      the market by 52 min — market-missed-word channel VALIDATED), PERU +
      IRAN NO (our false fires; precision 4/6). **INTC same-day: FIRST LIVE
      FILL (Q-FILL) from the us-east box arm** — APPL 1×26¢ filled in 9.9 ms
      decision→print; no-pump guard refused 2 repriced words; envelope held
      (28¢ of $1). CLI Δrace −1.0 s FROM US-EAST ⇒ vantage alone doesn't
      flip the sign — **decode is the remaining race lever**. INTC recall:
      4 swept words missed (ARIZ/DESK/GOVE/MOBI) + APPL false fire ⇒
      **detector quality (P1/P2/G2b + corpus) is now the top priority for
      G4**. FOMC bar unchanged.

- [ ] **W2.** Manifest-probe every event venue (cadence, LL-HLS tags, PDT
      absolute lag) and grow a per-venue table (venue/platform → cadence,
      LL-HLS?, PDT lag, bot-wall?).
      Gate: box (segment URLs can be requester-IP-locked); recurring per
      event.
      Done when: the table exists and each new event adds a row pre-call.
      Evidence →: RUNBOOK.md (venue table).

- [ ] **W3.** LL-HLS native sub-second fetcher (partial-segment /
      blocking-playlist-reload client).
      Gate: measurement:W2 — build only when a real event venue actually
      serves LL-HLS. Sequenced AFTER P6 (same src/source surface; SIP holds
      priority per RUNBOOK).
      Done when: ladder A/B on an LL-HLS stream shows the fetcher's delta vs
      streamlink; doc updated per layer-doc convention.
      Evidence →: docs/layers/source.md + RUNBOOK ladder evidence.

- [ ] **W4.** Channel recon — webcast platform transports (Notified / Q4 /
      Chorus / EQS player internals), caption websocket feeds (exist? what
      lag?), simulcast latency modes (YouTube latency classes, X, company
      players). Output: per-platform channel map + whether any channel beats
      live-edge HLS.
      Gate: user (web recon was explicitly deferred out of the 07-20
      session).
      Done when: findings + per-platform recommendations written; anything
      wire-verified pinned.
      Evidence →: RACE-STRATEGY.md §5 update + VERIFICATIONS.md for wire
      facts.

- [ ] **W5.** Wideband ground-truth corpus: label the NFLX 07-16 16 kHz
      capture (+ TSLA/GOOGL W1 captures) → bench streaming_ctc on wideband;
      tests the "telephony tariff gap shrinks at 16 kHz" hypothesis.
      Gate: none after W1.
      Done when: wideband dev table exists alongside the telephony one.
      Evidence →: BAKEOFF.md (corpus + results).

- [ ] **W6.** Vantage: corporate-player direct low-latency fetch trial from
      the us-east box (no bot-wall expected, RUNBOOK §W1-pre) and, for
      YouTube venues, the operator-cookies-on-VPS decision.
      Gate: user (AWS account; cookies are an operator ToS-gray call).
      Done when: an event-class stream fetched live-edge from us-east with
      measured stall profile; YouTube path decision recorded.
      Evidence →: RUNBOOK.md §Same-floor.

- [ ] **W7.** Fillable webcast posture unlock (lift `launch-webcast.sh`
      observe-only enforcement) for same-floor event classes.
      Gate: measurement:X2 + W1 — requires measured lag numbers AND a
      discriminator verdict of H1/H3/uncontested for the class.
      Done when: posture change + its evidence trail recorded; risk caps
      unchanged (config only tightens).
      Evidence →: RUNBOOK.md + docs/layers/source.md posture note.
