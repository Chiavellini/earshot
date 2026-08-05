# Live capture test: KNBR (free) vs. the Kalshi price pump — status

## Part 1 — built and shaken out end-to-end (2026-08-05), DONE

- **Free stream confirmed working**: `https://playerservices.streamtheworld.com/api/livestream-redirect/KNBRAM.mp3`
  (KNBR 680, SF Giants flagship, Triton Digital CDN). `venue-probe.sh` PASS
  (960,000/960,000 bytes at 30s). No auth, no login, no cost.
- **`configs/knbr-giants-capture.toml`** — 2-leg capture-only config
  (default vs. low-latency ffmpeg tuning against the same URL), modeled on
  `configs/webcast-abcwnt-capture.toml`. Live-run for 60s via
  `target/release/earshot capture-bakeoff`: both legs captured cleanly
  (3547/3461 frames), `tuned` leg recommended (lower p99 jitter). One real
  bug found and fixed along the way: `-http_persistent` is not a valid
  option on this ffmpeg build — removed from the tuned leg's flags.
- **`wallclock_anchor.py`** — built and self-tested. Generalizes
  `scripts/abs-lag.py`'s `(CLOCK_MONOTONIC_RAW, time.time())` anchor-pair
  technique for KNBR's plain-MP3 stream (no HLS, no PDT tags — the HLS
  variant 403s, confirmed in the stream-resolution research). `time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)`
  is available and working on this host.
- **`earshot replay` confirmed working end-to-end against the recorded
  audio** — this is the significant result: the REAL detection pipeline
  (streaming_ctc, ONNX) runs against KNBR's recorded WAV and produces a
  genuine `PipelineReport` (3461 frames, 866 chunks, 0 drops, real per-stage
  edge-latency percentiles). 0 detections in this shakedown clip is
  expected (the placeholder word "error" almost certainly wasn't said in
  this particular 60s window) — the point was confirming the mechanism
  works, not getting a real detection yet.
- **Real gotcha found and resolved**: `earshot replay` hung indefinitely
  (0.04s CPU over 2+ minutes wall-clock, no error) with `ORT_DYLIB_PATH`
  unset — this is a documented, known project trap (`scripts/run-arm.sh`,
  `scripts/wnt-daily.sh`, `scripts/launch-event.sh` all guard against it:
  "streaming_ctc dlopens onnxruntime; unset, the arm/launcher hangs/goes
  deaf"). Resolved: `export ORT_DYLIB_PATH="$(pwd)/vendor/onnxruntime/onnxruntime-osx-arm64-1.27.1/lib/libonnxruntime.dylib"`.
  **Any future run of `replay` or `capture-bakeoff` with a real detector
  engine in this investigation must set this first** — recorded here so it
  isn't rediscovered the hard way under real game-time pressure.
- **`poll_new_market.py`** — built, self-tested, and run live. As of
  2026-08-05T21:2x UTC, both `KXMLBMENTION` and `KXWNBAMENTION` are
  confirmed **still paused** (`STILL_PAUSED`, newest ticker still
  `KXMLBMENTION-26AUG01NYYCHC` / `KXWNBAMENTION-26AUG01NYPHX`). Since moved
  up to `pm-expansion-exploration/latency/poll_new_market.py` (shared, not
  MLB-scoped) to also watch `KXTBPNMENTION` — see
  `../tbpn/LIVE-CAPTURE-STATUS.md`.
- **Opportunistic Triton stream-mapping (Yankees/Mets, Cardinals, Cubs)**:
  attempted, failed. KNBR's resolution depended on finding the station's
  actual embedded Triton mount name (`KNBRAM`) via its SoCast player page —
  this does **not** follow a guessable `<CALLSIGN><BAND>` pattern (tried
  `WFANAM`/`WFANFM`/`WFAN`, `KMOXAM`/`KMOXFM`/`KMOX`, `WGNAM`,
  `670THESCORE`, `WSCRAM` — all 404). **Resolving a different team's stream
  needs the same per-station research pass KNBR got, not pattern-guessing.**
  Budget for this when Part 2 unblocks on a non-Giants game.

## Part 2 — blocked on Kalshi (not forceable)

No open `KXMLBMENTION`/`KXWNBAMENTION` market exists right now. Re-run
`python3 pm-expansion-exploration/latency/poll_new_market.py [--series KXWNBAMENTION]`
periodically. Exit code `3` means a new event was found (script prints it).
Historical cadence suggests ~2 days' lead before game date when Kalshi
resumes listing — could reappear any day.

## Part 3 — ready to execute the moment Part 2 unblocks

Every mechanical piece has been shaken out for real: the stream is
confirmed reachable, the capture config is confirmed working, the
wall-clock bridge is confirmed working, and the replay→real-detector step
is confirmed working (including the ORT_DYLIB_PATH trap, now already
resolved once). The only missing ingredient is a live market to compare
against. When `poll_new_market.py` reports `NEW_EVENT_FOUND`:

1. Identify the game (`gamePk` via `leg1_playbyplay.py`'s schedule pattern)
   and team. If SF, KNBR is ready now; otherwise resolve that team's stream
   (budget real research time, per the finding above).
2. `scripts/venue-probe.sh <url> --secs 30 --json` — fresh preflight.
3. `python3 wallclock_anchor.py --sample --out <dir>/anchor.json` right
   before starting.
4. `ORT_DYLIB_PATH=.../libonnxruntime.dylib target/release/earshot capture-bakeoff --config configs/knbr-giants-capture.toml --secs <game_len+buffer> --record-dir <dir> --out <dir>/report.json`
   (or a fresh per-team config, same shape).
5. In parallel, poll `leg1_playbyplay.py` for the live game's real play
   timestamps.
6. After the game: `ORT_DYLIB_PATH=... target/release/earshot replay --config <replay-cfg-pointed-at-the-real-strike-words> --report <dir>/decision-report.json` — real `decision_ts` from the actual detector.
7. Bridge `decision_ts` to UTC via the anchor; pull the market's trade tape
   via `lib/kalshi_sweep.py` for `sweep_onset`; compute the three deltas
   (our_decision vs. real_event, sweep_onset vs. real_event, our_decision
   vs. sweep_onset) — the last one is the number that actually answers the
   question asked.
