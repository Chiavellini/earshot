#!/usr/bin/env bash
# ONE-COMMAND morning launcher for the MS-26JUL15 live run (docs/RUNBOOK.md).
# Call is 6:30 AM CDMX; operator arrives 6:20. This does cleanup → sanity →
# launch → dial in one shot. Dress-rehearsed end-to-end 2026-07-14 night.
#
#   scripts/ms-morning.sh              # live run (the real thing)
#   scripts/ms-morning.sh --dry-run    # same chain, orders never sent
set -euo pipefail
cd "$(dirname "$0")/.."

LIVE_FLAGS="--i-am-live --allow-mention --fillable"
[[ "${1:-}" == "--dry-run" ]] && LIVE_FLAGS="--allow-mention"

# 1. No stale kill files (a leftover STOP kills the arm instantly).
rm -f state/KXEARNINGSMENTIONMS-26JUL15/STOP state/STOP

# 2. Fast market sanity: TARI book must be reachable and active.
source ~/.earshot/ms.env
cargo run --release --quiet -- book --ticker KXEARNINGSMENTIONMS-26JUL15-TARI --secs 3

# 3. Launch: tunnel → arm (validates all 14 markets, NTP, books) → dial.
#    launch-event.sh waits for the WS listener before dialing.
exec scripts/launch-event.sh \
  --config configs/twilio-live-ms.toml \
  --account ~/.earshot/ms.env \
  --bind-port 8080 \
  --to +18448862200 --dtmf "400700#" \
  $LIVE_FLAGS
