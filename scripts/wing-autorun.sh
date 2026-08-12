#!/usr/bin/env bash
# EARSHOT_RETIRED_ENTRYPOINT
# Historical WING-only resolver/arm. Generic access planning and typed
# supervision now own this lifecycle for every issuer.
echo "REFUSED: scripts/wing-autorun.sh is retired." >&2
echo "Use: scripts/opsctl.sh prepare-day --date YYYY-MM-DD" >&2
exit 64
