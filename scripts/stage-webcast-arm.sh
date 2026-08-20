#!/usr/bin/env bash
# stage-webcast-arm.sh [--speculative] <ticker-lc> <EVENT_TICKER> <START_ISO_UTC>
# e.g.: stage-webcast-arm.sh axp KXEARNINGSMENTIONAXP-26JUL24 2026-07-24T12:30:00Z
# Instantiates the FANOUT ARM config (streamlink + tuned-ffmpeg + re-resolving
# edge fetch + cache-busting fetch, multi-leg OR'd detection) from
# configs/webcast-fanout-arm.toml. Companion to
# stage-webcast-capture.sh (the 4-leg STUDY kit); this one ARMS.
#
# --speculative uses configs/webcast-fanout-arm-spec.toml instead: the
# speculative edge-fetcher REPLACES the streamlink leg (~4.5 s fresher, ~5x less
# join variance — docs/LATENCY-PROOF.md 2026-07-24 parts 1-3). It probes
# unannounced segment URLs, so a rate-limit would take EVERY leg on the box
# down. Default stays the proven non-speculative stack; FOMC runs the default.
#
# After staging you still MUST:
#   1. paste the `earshot prepare --event-ticker <EV>` keyword/risk sections
#      over the placeholders (grep __ to find leftovers — the launcher refuses
#      placeholder configs),
#   2. call-morning: echo "<m3u8>" > ~/<lc>-stream.url
#   3. launch attended from a PLAIN shell (post-mortem #3):
#      scripts/launch-webcast.sh --config configs/webcast-<lc>-arm.toml \
#        --account <env> --wait-live ~/<lc>-stream.url --wait-kind hls
set -euo pipefail
SPEC=0
if [[ "${1:-}" == "--speculative" ]]; then SPEC=1; shift; fi
LC="${1:?ticker-lc}"; EV="${2:?EVENT_TICKER}"; START="${3:?START_ISO_UTC}"
EARDIR="${EARDIR:-$HOME/ear}"
if (( SPEC )); then
  TPL="$EARDIR/configs/webcast-fanout-arm-spec.toml"
  # The fetcher is the leg here; refuse to stage a config whose fetcher is
  # broken rather than discovering it at arm time.
  python3 "$EARDIR/scripts/hls-edge-fetch.py" --selftest >/dev/null \
    || { echo "REFUSING: hls-edge-fetch.py --selftest failed"; exit 1; }
  echo "posture: SPECULATIVE (spec_edge + pipe_tuned) — check ~/spec_edge.err SUMMARY after the run"
else
  TPL="$EARDIR/configs/webcast-fanout-arm.toml"
  # These are production sources in the default template. Refuse staging when
  # either implementation is missing/broken; discovering that at fire time is
  # the TOL/ROST zero-frame failure class.
  python3 "$EARDIR/scripts/hls-edge-fetch.py" --selftest >/dev/null \
    || { echo "REFUSING: hls-edge-fetch.py --selftest failed"; exit 1; }
  bash -n "$EARDIR/scripts/cdn-cachebust-fetch.sh" \
    || { echo "REFUSING: cdn-cachebust-fetch.sh syntax check failed"; exit 1; }
fi
CFG="$EARDIR/configs/webcast-$LC-arm.toml"
[[ -f "$TPL" ]] || { echo "template missing: $TPL"; exit 1; }
SERIES=$(echo "$EV" | cut -d- -f1)
sed -e "s|__LC__|$LC|g" \
    -e "s|__EVENT_TICKER__|$EV|g" \
    -e "s|__START_ISO_UTC__|$START|" \
    -e "s|__SERIES__|$SERIES|" \
    "$TPL" > "$CFG"
echo "staged: $CFG"
echo "next: paste \`earshot prepare --event-ticker $EV\` keywords/risk over the CAPT placeholder,"
echo "      then on call morning: echo '<m3u8>' > ~/$LC-stream.url"
