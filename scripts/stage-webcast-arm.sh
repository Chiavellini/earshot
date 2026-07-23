#!/usr/bin/env bash
# stage-webcast-arm.sh <ticker-lc> <EVENT_TICKER> <START_ISO_UTC>
# e.g.: stage-webcast-arm.sh axp KXEARNINGSMENTIONAXP-26JUL24 2026-07-24T12:30:00Z
# Instantiates the FANOUT ARM config (streamlink edge-join + tuned-ffmpeg legs,
# multi-leg OR'd detection) from configs/webcast-fanout-arm.toml. Companion to
# stage-webcast-capture.sh (the 4-leg STUDY kit); this one ARMS.
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
LC="${1:?ticker-lc}"; EV="${2:?EVENT_TICKER}"; START="${3:?START_ISO_UTC}"
EARDIR="${EARDIR:-$HOME/ear}"
TPL="$EARDIR/configs/webcast-fanout-arm.toml"
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
