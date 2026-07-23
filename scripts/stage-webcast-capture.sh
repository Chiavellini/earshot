#!/usr/bin/env bash
# stage-webcast-capture.sh <ticker-lc> <EVENT_TICKER> <START_ISO_UTC> [keyword]
# e.g.: stage-webcast-capture.sh alk KXEARNINGSMENTIONALK-26JUL22 2026-07-22T15:30:00Z guidance
# Instantiates a 4-leg webcast STUDY capture (config + run script) from the
# GEV template for a new event. Run ON THE BOX (or locally then scp). The
# morning m3u8 steps stay manual — see RUNBOOK §Webcast study monitors.
set -euo pipefail
LC="${1:?ticker-lc}"; EV="${2:?EVENT_TICKER}"; START="${3:?START_ISO_UTC}"; KW="${4:-guidance}"
UC=$(echo "$LC" | tr '[:lower:]' '[:upper:]')
EARDIR="${EARDIR:-$HOME/ear}"
TPL="$EARDIR/configs/webcast-gev-capture.toml"
CFG="$EARDIR/configs/webcast-$LC-capture.toml"
[[ -f "$TPL" ]] || { echo "template missing: $TPL"; exit 1; }
sed -e "s|gev-stream.url|$LC-stream.url|g" \
    -e "s|__GEV_URL__|__${UC}_URL__|g" \
    -e "s|KXEARNINGSMENTIONGEV-26JUL22-WEBCAST|$EV-WEBCAST|" \
    -e "s|starts_at = \".*\"|starts_at = \"$START\"|" \
    -e "s|KXEARNINGSMENTIONGEV|$(echo "$EV" | cut -d- -f1)|" \
    -e "s|KXEARNINGSMENTIONGEV-26JUL22-CAPT|$EV-CAPT|" \
    -e "s|electrification|$KW|g" \
    -e "s|run-gev-capture.sh|run-$LC-capture.sh|" \
    -e "s|gev-capture/|$LC-capture/|g" \
    "$TPL" > "$CFG"
cat > "$HOME/run-$LC-capture.sh" <<EOF
#!/usr/bin/env bash
# $UC webcast study capture. Run AFTER writing the stream URL:
#   echo "<m3u8>" > ~/$LC-stream.url
#   sed -i "s|__${UC}_URL__|<m3u8>|" $CFG
set -euo pipefail
[[ -s ~/$LC-stream.url ]] || { echo "REFUSING: ~/$LC-stream.url missing/empty"; exit 1; }
grep -q "__${UC}_URL__" "$CFG" && { echo "REFUSING: __${UC}_URL__ placeholder still in config"; exit 1; }
mkdir -p ~/$LC-capture
cd "$EARDIR"
nohup ./target/release/earshot capture-bakeoff \\
  --config "$CFG" \\
  --secs 6600 \\
  --record-dir ~/$LC-capture \\
  --out ~/$LC-capture/report.json > ~/$LC-capture/run.log 2>&1 &
echo "capture started (pid \$!) — 110 min; log: ~/$LC-capture/run.log"
EOF
chmod +x "$HOME/run-$LC-capture.sh"
echo "staged: $CFG + ~/run-$LC-capture.sh (URL placeholder __${UC}_URL__, url file ~/$LC-stream.url)"
