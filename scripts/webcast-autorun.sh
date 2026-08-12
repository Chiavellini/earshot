#!/usr/bin/env bash
# Compatibility name for the sole schema-3 webcast fire-time supervisor.
#
# Scheduling now targets arm-on-url.sh directly. This facade exists only for
# callers that still name webcast-autorun.sh; it accepts the same exact typed
# arguments and immediately replaces itself. It owns no resolver, deadline,
# report, retry, process group, TERM budget, or KILL path.
#
#   scripts/webcast-autorun.sh --run-dir var/runs/<EVENT>/<RUN> \
#     [--i-am-live --fillable]
set -euo pipefail

REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RUN_DIR="${EARSHOT_RUN_DIR:-}"
LIVE=0 FILLABLE=0 SECS=6
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir) RUN_DIR="${2:?}"; shift 2 ;;
    --i-am-live) LIVE=1; shift ;;
    --fillable) FILLABLE=1; shift ;;
    --secs) SECS="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "REFUSED: legacy webcast-autorun argument '$1'; use exact --run-dir" >&2; exit 2 ;;
  esac
done
[[ -n "$RUN_DIR" ]] || { echo "REFUSED: --run-dir is required" >&2; exit 2; }
[[ "$FILLABLE" == 0 || "$LIVE" == 1 ]] || {
  echo "REFUSED: --fillable requires --i-am-live" >&2; exit 2;
}
ARGS=(--run-dir "$RUN_DIR" --secs "$SECS")
[[ "$LIVE" == 1 ]] && ARGS+=(--i-am-live)
[[ "$FILLABLE" == 1 ]] && ARGS+=(--fillable)
exec "$REPO/scripts/arm-on-url.sh" "${ARGS[@]}"
