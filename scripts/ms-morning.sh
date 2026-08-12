#!/usr/bin/env bash
# EARSHOT_RETIRED_ENTRYPOINT
# Historical MS-26JUL15 launcher. Its issuer/date-specific config, credentials,
# dial digits, posture, and cleanup made it unsafe as a reusable operation.
echo "REFUSED: scripts/ms-morning.sh is retired." >&2
echo "Use: scripts/opsctl.sh prepare-day --date YYYY-MM-DD" >&2
exit 64
