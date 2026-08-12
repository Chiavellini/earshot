#!/usr/bin/env bash
# EARSHOT_RETIRED_ENTRYPOINT
# Legacy slug launcher. The supported scheduler binds an exact schema-3
# RunPlan, preflight proof, capture-ready proof, and loaded-job readback.
echo "REFUSED: scripts/run-arm.sh is retired." >&2
echo "Use: scripts/opsctl.sh schedule --date YYYY-MM-DD" >&2
exit 64
