#!/usr/bin/env bash
# EARSHOT_RETIRED_ENTRYPOINT
# Legacy URL watcher. It armed by slug without an immutable RunPlan or durable
# loaded-job proof and therefore is no longer an operational path.
echo "REFUSED: scripts/arm-when-url.sh is retired." >&2
echo "Use: scripts/opsctl.sh schedule --date YYYY-MM-DD" >&2
exit 64
