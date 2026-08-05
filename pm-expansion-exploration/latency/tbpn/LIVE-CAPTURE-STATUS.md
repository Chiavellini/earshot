# TBPN: watch + live-streaming capture kit — status

## Done (2026-08-05)

- **`poll_new_market.py` generalized and moved** to
  `pm-expansion-exploration/latency/poll_new_market.py` (was MLB-scoped
  under `mlb/`). Now watches `KXMLBMENTION`, `KXWNBAMENTION`, and
  `KXTBPNMENTION`. Self-tested (8 checks) and confirmed live: all three
  still `STILL_PAUSED` as of this run — TBPN baseline
  `KXTBPNMENTION-26JUL23` unchanged.
- **`configs/tbpn-live-capture.toml`** — 2-leg live-streaming capture
  config (default vs. low-latency ffmpeg tuning), modeled on
  `mlb/configs/knbr-giants-capture.toml` but adapted for a YouTube-live
  source via `yt-dlp -g` (WNT's proven two-step pattern: resolve once to
  `~/tbpn-stream.url`, both legs read it), not a static MP3 URL. Verified:
  `@TBPNLive`'s live URL resolves cleanly through `yt-dlp` (clean "channel
  is not currently live" error off-air, no bot-wall/format issues — same
  mechanism WNT already proved). Config parse-checked via a real
  `capture-bakeoff` run (both legs spawn correctly, fail only because no
  stream URL is resolved yet — exactly the expected shape while off-air).

## Why this venue, briefly

`pack_lag_tbpn.py`'s real result against `KXTBPNMENTION-26JUL23` (see
`../latency-scoreboard.md`): the join correctly refused (slope ≈0, not
≈1.0) because all 4 matched strikes repriced within the same ~9-minute
window near the show's end, regardless of whether the underlying word was
said 6 minutes or 104 minutes into the episode. That's inconsistent with
anyone racing individual mentions live — it looks like there's currently
**no** real-time audio-reactive pack on this market, only a later,
whole-show synthesis. If that holds, this is a better opportunity than
MLB/WNBA (which do show a real, if slow, competing pack) — there's nothing
to race, just an empty field.

## Blocked on (not forceable)

`KXTBPNMENTION`'s listing cadence is irregular by nature, not paused: 12
historical events since 2025-10-23, gaps ranging 4-98 days, no
day-of-week pattern, currently 0 open / 0 scheduled ahead. Re-run
`python3 pm-expansion-exploration/latency/poll_new_market.py --series KXTBPNMENTION`
periodically — exit code `3` means a new board appeared.

Separately, Polymarket runs TBPN mention markets in a different shape
(per-guest, e.g. "What will Jim Cramer say during TBPN on December 4?"),
none currently open — a different trigger condition (a notable-guest
booking, not a fixed schedule), not built out yet.

## Ready the moment a board opens

1. Confirm the show is about to air / is airing (weekdays 11am-2pm PT).
2. `yt-dlp -g -f 233/234/bestaudio "https://www.youtube.com/@TBPNLive/live" > ~/tbpn-stream.url`
3. `scripts/venue-probe.sh "$(cat ~/tbpn-stream.url)" --secs 15 --json` — now meaningful (was untestable off-air).
4. Check the live manifest for `#EXT-X-PROGRAM-DATE-TIME` tags — if present, use
   `scripts/pdt-log.py`/`scripts/abs-lag.py` (WNT's proven PDT bridge, more
   precise); if absent, fall back to
   `pm-expansion-exploration/latency/mlb/wallclock_anchor.py` (already
   proven, venue-agnostic). Don't assume either way — check first.
5. `ORT_DYLIB_PATH=$(pwd)/vendor/onnxruntime/onnxruntime-osx-arm64-1.27.1/lib/libonnxruntime.dylib target/release/earshot capture-bakeoff --config pm-expansion-exploration/latency/tbpn/configs/tbpn-live-capture.toml --secs <show_len+buffer> --record-dir <dir> --out <dir>/report.json`
6. Same replay→decision→compare-against-sweep sequence already proven end-to-end on KNBR/MLB (see `../mlb/LIVE-CAPTURE-STATUS.md` Part 3) — pull the open board's real strikes, build a keyword config for them, run `earshot replay` against the recorded WAV, bridge to UTC, compare against `lib/kalshi_sweep.py`'s sweep onset for that strike.
