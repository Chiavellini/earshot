#!/usr/bin/env bash
# Webcast-lane launcher: arm the pipeline on a config whose [[sources]] is
# `hls` or `pipe` (source::pipe decodes via ffmpeg/yt-dlp). No tunnel, no
# Twilio, no dial — the subprocess pulls the stream itself.
#
#   scripts/launch-webcast.sh --config configs/<event>.toml --account <env> [--bin <pinned>]
#
# POSTURE: capture/observe only — this script deliberately accepts no
# --fillable/--i-am-live. The webcast runs tens of seconds behind the phone
# bridge (unmeasured; docs/RUNBOOK.md), so fills here race nothing. Lift this
# only after the lag is measured and a policy exists.
#
# Abort: Ctrl-C (arm's kill watcher cancels session orders + writes the
# report), or `touch state/<event>/STOP`.
set -euo pipefail

CONFIG="" ACCOUNT="" BIN="${EARSHOT_BIN:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)  CONFIG="$2"; shift 2 ;;
    --account) ACCOUNT="$2"; shift 2 ;;
    --bin)     BIN="$2"; shift 2 ;;
    *) echo "unknown arg: $1 (this launcher takes no live/fillable flags by design)" >&2; exit 2 ;;
  esac
done
[[ -n "$CONFIG" && -n "$ACCOUNT" ]] || {
  echo "usage: $0 --config <cfg.toml> --account <env-file> [--bin <pinned>]" >&2; exit 2
}
[[ -f "$CONFIG" ]] || { echo "config not found: $CONFIG" >&2; exit 2; }
[[ -f "$ACCOUNT" ]] || { echo "account env not found: $ACCOUNT" >&2; exit 2; }

for tool in ffmpeg; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing tool: $tool" >&2; exit 1; }
done
# yt-dlp only matters for pipe-cmds that use it; warn, don't fail.
command -v yt-dlp >/dev/null 2>&1 || echo "note: yt-dlp not found — YouTube pipe configs will fail" >&2

EVENT_ID=$(sed -n '/^\[event\]/,/^\[/s/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
[[ -n "$EVENT_ID" ]] || { echo "no [event].id in $CONFIG" >&2; exit 2; }
STATE_DIR="state/${EVENT_ID}"
mkdir -p "$STATE_DIR"
REPORT="${STATE_DIR}/report.json"

# shellcheck disable=SC1090
source "$ACCOUNT"

echo "webcast lane: event=${EVENT_ID}  config=${CONFIG}  state=${STATE_DIR}  (observe posture)"
if [[ -n "$BIN" ]]; then
  [[ -x "$BIN" ]] || { echo "pinned binary not executable: ${BIN}" >&2; exit 2; }
  exec "$BIN" arm --config "$CONFIG" --state-dir "$STATE_DIR" --report "$REPORT" --allow-mention
else
  exec cargo run --release -- arm --config "$CONFIG" --state-dir "$STATE_DIR" --report "$REPORT" --allow-mention
fi
