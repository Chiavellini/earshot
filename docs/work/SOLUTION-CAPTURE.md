# Work order A — CAPTURE: the stream stays up

worker: capture-reliability-lane · branch: `webcast-lane` · as_of 2026-08-12
Defect IDs referenced here (C1–C6) are defined in `docs/SYSTEM-STATE.md` §3.

## Mission

Every armed event holds a continuous, decodable audio feed for the full call —
webcast or phone — surviving HLS sequence gaps, CDN cache freezes, operator
queues, and dropped legs, without a human touching anything mid-run. Detection
quality and latency are explicitly NOT this lane's job; staying connected is.

## What already exists — do not rebuild

- Fanout redundancy + arbiter failover (`src/pipeline/mod.rs`,
  `src/source/arbiter.rs` — 200 ms failover, TakeFirst xcorr align).
- Pipe idle guard (30 s default, `src/source/pipe.rs`), Twilio reconnect
  budget (`src/source/twilio_ws.rs`, `max_reconnects` ≤20 requires
  `EARSHOT_REDIAL_DIR`), redial supervisor for the phone lane
  (`scripts/redial-supervisor.sh` — marker-content driven, listener.ready
  gated), no-stream watchdog (`scripts/launch-event.sh`,
  `STREAM_CONFIRM_SECS=90`).
- `scripts/pcm-gate.sh` (decodable-bytes proof), `scripts/hls-edge-fetch.py`
  (phase-locked low-latency fetcher, never crashes the leg),
  `scripts/cdn-cachebust-fetch.sh` (per-reload cache-buster; header documents
  the CBRS four-tools-all-wrong failure), live gate held OUTSIDE the arm
  (`scripts/launch-webcast.sh`).
- First-drop operator escalation for unknown venues exists in the checkpointed
  program (see `docs/runbooks/EVENT-LIFECYCLE.md` §operator) — validate it,
  don't reinvent it.

## Work items (priority order)

1. **C1 — real liveness in `--wait-live`.** Replace the EXTINF grep
   (`scripts/launch-webcast.sh:556,569`) with the three-part test already
   specified in `scripts/cdn-cachebust-fetch.sh:22-26`: no `#EXT-X-ENDLIST`,
   an advancing manifest tail across two polls, and decodable bytes. Ship a
   VOD-manifest fixture test (a finished ENDLIST playlist must be refused; an
   advancing live one must pass) — this gate has no test today, which is why
   C1 survived.
2. **C2 — webcast legs respawn.** Give the pipe source a reconnect budget
   symmetrical to Twilio's: on child death or idle timeout, respawn the
   decoder command (fresh cache-buster URL where applicable) up to a
   configured budget with backoff, instead of returning a terminal
   `SourceError` (`src/source/pipe.rs:196-206`). The arm must never exit while
   the venue is live because every leg hit a sequence gap — COHR 2026-08-12 is
   the reproduction (arm exited 21:35:44Z mid-call; both legs died on
   `Sequence gap` / `expired from playlists`). Implement inside the source
   layer (your nodes); if you conclude `src/pipeline/mod.rs` must change, file
   a request to the EVIDENCE lane (it owns runtime.assembly) — do not edit it.
   Add a leg-respawn regression test (kill the decoder mid-stream; assert
   frames resume and the run survives).
3. **C2b — promote the survivors to first-class legs.** `hls-edge-fetch.py`
   and `cdn-cachebust-fetch.sh` saved COHR from outside the arm; make them
   selectable pipe-leg commands in the staged config the generic stage path
   emits, not emergency tools someone must remember.
4. **C3 — venue-derived redial policy + escalation without pre-knowledge.**
   Derive `--max-redials`/backoff/call-end-grace from the venue class in
   `configs/dial-ins.toml` instead of one global default (casino143 Tier-1
   #6). For unknown venues, validate the checkpointed first-drop escalation
   live-shaped (drill via `tests/operator_escalation.sh` + a rehearsal dial),
   and make one unanswered operator page retry on a bounded schedule rather
   than killing the lane.
5. **C4 — admission-probe window.** Probe venues whose bridge opens at T-30
   inside their actual open window; stop wasting the T-90…T-30 probes on them
   (casino143 §IV.8). The venue registry knows the class; use it.
6. **C5 — host-contention guard.** At pcm-gate/capture-ready time and
   periodically in-run, check load and frame-loss cadence; page (via the
   OBSERVABILITY lane's alert channel) when the host, not the venue, is the
   problem. FIG's 11.3% frame loss at load 150/8 cores must be detected as
   HOST, not stream death.
7. **C6 — recorder-tool guardrail.** `bridge-probe`/`chain-capture` against an
   issuer with an open board must refuse harder than a flag: verify the
   `--corpus-only` refusal path covers today's tooling and add the missing
   test.

## Owned nodes (claim under these; one open claim per node)

`runtime.source.port`, `runtime.source.pipe`, `runtime.source.twilio`,
`runtime.source.sip`, `runtime.source.replay`, `runtime.source.arbiter`,
`control.acquire`, `control.launch`, `control.phone_session`.

Files that implies (non-exhaustive): `src/source/*`, `scripts/launch-webcast.sh`,
`scripts/launch-event.sh`, `scripts/redial-supervisor.sh`,
`scripts/hls-edge-fetch.py`, `scripts/cdn-cachebust-fetch.sh`,
`scripts/pcm-gate.sh`, `scripts/arm-on-url.sh`, acquisition resolvers,
`tests/pipe_source.rs`, `tests/twilio_source.rs`, `tests/arbiter.rs`,
`tests/redial_supervisor.rs`, `tests/launch_event_posture.rs`,
`tests/launch_webcast_posture.rs`.

## Forbidden zones

- `src/main.rs`, `src/pipeline/mod.rs`, `src/audio_evidence.rs`,
  `src/kalshi/**`, `scripts/deploy-box.sh`, `scripts/pull-box-state.sh`,
  `scripts/run-audit.py` → EVIDENCE lane.
- `src/telemetry.rs`, `scripts/alertctl.py`, `scripts/alert-phone.sh`,
  `scripts/notify.sh`, `scripts/event-status.sh`, `scripts/journal.sh`,
  `scripts/missed-arm-check.py`, `scripts/score-precision.py`,
  `scripts/audit-day.py` → OBSERVABILITY lane. (Call `journal.sh`/`alertctl.py`;
  never edit them.)
- `src/risk.rs` money constants and every §4 item in SYSTEM-STATE — operator
  only.

## Rules

- Claim per `AGENTS.md` in `architecture/claims.toml` before touching a node;
  contract doc must match code before a claim closes.
- `docs/STATUS.md` / `docs/work/ACTIVE.md`: append your own section only.
- Any dial/page/live test uses rehearsal numbers (`+17627212978` TTS,
  `+17622164408` fake operator) — never a real venue outside an armed event.

## Acceptance

- VOD fixture refused + advancing-live fixture passes, in CI.
- A staged webcast arm survives a killed decoder leg (test) and a full
  rehearsal run with an induced mid-run leg kill records zero coverage gap
  beyond the respawn budget.
- Redial parameters visibly derived from the registry in the launch log; a
  drill against the fake operator shows page-retry instead of lane death.
- Verification: `./scripts/doc-lint.sh`, `python3 tests/ops_surface.py`,
  `cargo test --test pipe_source --test twilio_source --test arbiter --test
  launch_event_posture --test launch_webcast_posture`, plus the new tests.
