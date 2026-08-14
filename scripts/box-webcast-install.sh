#!/usr/bin/env bash
# Transfer one exact schema-3 webcast RunPlan to its execution box and install
# the box's systemd-user timer. This is a dispatch boundary only: it does not
# resolve media, build a binary, create a pin, dial, open ports, or move phone
# credentials.
#
#   scripts/box-webcast-install.sh --run-dir var/runs/<EVENT_ID>/<RUN_ID>
#       [--fire-at-utc UTC] [--verify-only]
#
# A successful return proves the canonical schema-2 AccessPlan is non-static,
# the plan is bound to EARSHOT_BOX_EXECUTION_HOST, the box already has its
# executable preflight pin, and the LOADED systemd unit passed schedule-proof.
set -euo pipefail

REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPO=$(cd "$REPO" && pwd -P)
cd "$REPO"

RUN_DIR_RAW="" VERIFY_ONLY=0 FIRE_AT_UTC=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir)     RUN_DIR_RAW="${2:?}"; shift 2 ;;
    --fire-at-utc) FIRE_AT_UTC="${2:?}"; shift 2 ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    -h|--help)     sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$RUN_DIR_RAW" ]] || {
  echo "usage: $0 --run-dir var/runs/<EVENT_ID>/<RUN_ID> [--fire-at-utc UTC] [--verify-only]" >&2
  exit 2
}

EXPECTED_HOST="${EARSHOT_BOX_EXECUTION_HOST:-earshot-useast1}"
EARSHOT_INSTANCE_ID="${EARSHOT_INSTANCE_ID:-i-04c50d76572aa092e}"
if [[ -z "${EARSHOT_BOX:-}" ]] && command -v aws >/dev/null 2>&1; then
  _ip="$(aws ec2 describe-instances --instance-ids "$EARSHOT_INSTANCE_ID" \
           --query 'Reservations[].Instances[].PublicIpAddress' --output text 2>/dev/null || true)"
  [[ "$_ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]] && EARSHOT_BOX="ubuntu@$_ip"
fi
BOX="${EARSHOT_BOX:-ubuntu@3.212.174.94}"
KEY="${EARSHOT_BOX_KEY:-$HOME/.ssh/earshot-useast1.pem}"
BOX_REPO="${EARSHOT_BOX_REPO:-~/ear}"
SSH_BIN="${EARSHOT_SSH:-ssh}"
RSYNC_BIN="${EARSHOT_RSYNC:-rsync}"
SSH=("$SSH_BIN" -o ConnectTimeout=15 -o BatchMode=yes -i "$KEY" "$BOX")

RUN_DIR=$(python3 - "$REPO" "$RUN_DIR_RAW" <<'PY'
from pathlib import Path
import sys
repo = Path(sys.argv[1]).resolve()
raw = Path(sys.argv[2])
run = (raw if raw.is_absolute() else repo / raw).resolve()
try:
    rel = run.relative_to(repo / "var" / "runs")
except ValueError:
    raise SystemExit("run directory is outside canonical var/runs")
if len(rel.parts) != 2:
    raise SystemExit("run directory must be var/runs/<EVENT_ID>/<RUN_ID>")
print(run)
PY
) || { echo "REFUSED: unsafe --run-dir '$RUN_DIR_RAW'" >&2; exit 2; }

EARSHOT_REPO="$REPO" python3 scripts/eventctl.py verify --run-dir "$RUN_DIR" >/dev/null || {
  echo "REFUSED: local RunPlan/ledger verification failed" >&2
  exit 1
}
EARSHOT_REPO="$REPO" python3 scripts/access-plan.py validate \
  --run-dir "$RUN_DIR" --canonical >/dev/null || {
    echo "REFUSED: webcast RunPlan has no canonical schema-2 AccessPlan" >&2
    exit 1
  }

PLAN_EXPORTS=$(python3 - "$REPO" "$RUN_DIR" "$EXPECTED_HOST" <<'PY'
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

repo, run, expected_host = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
plan = json.loads((run / "control/run-plan.json").read_text())
access = json.loads((run / "control/access-plan.json").read_text())
if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
    raise SystemExit("schema-3 RunPlan required")
if plan.get("lane") != "webcast":
    raise SystemExit(f"box-webcast-install received lane={plan.get('lane')!r}")
if plan.get("execution_enabled") is not True or plan.get("capture_only") is not False:
    raise SystemExit("box webcast scheduling requires an execution-enabled RunPlan")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", expected_host):
    raise SystemExit("EARSHOT_BOX_EXECUTION_HOST is not a stable host ID")
if plan.get("execution_host_id") != expected_host:
    raise SystemExit(
        f"RunPlan host {plan.get('execution_host_id')!r} != box host {expected_host!r}"
    )
resolver_kind = access.get("resolver", {}).get("kind")
if resolver_kind == "static-public":
    raise SystemExit(
        "static-public AccessPlan cannot produce the exact endpoint-bound PCM proof yet"
    )
if not isinstance(resolver_kind, str) or not resolver_kind:
    raise SystemExit("AccessPlan has no typed resolver kind")
status = subprocess.run(
    [sys.executable, str(repo / "scripts/eventctl.py"), "status", "--run-dir", str(run), "--json"],
    cwd=repo, text=True, capture_output=True, check=True,
    env={**__import__('os').environ, "EARSHOT_REPO": str(repo)},
)
state = json.loads(status.stdout)["state"]
reached = {r["transition"] for r in state["reached"]}
if "preflight_passed" not in reached:
    raise SystemExit("RunPlan has not reached preflight_passed")
if not state["active"]:
    raise SystemExit("RunPlan is terminal or archived")
values = {
    "PLAN_EVENT_ID": plan["event_id"],
    "PLAN_RUN_ID": plan["run_id"],
    "PLAN_SHA256": plan["plan_sha256"],
    "PLAN_HOST": plan["execution_host_id"],
    "RESOLVER_KIND": resolver_kind,
}
for key, value in values.items():
    print(f"{key}={shlex.quote(str(value))}")
PY
) || { echo "REFUSED: --run-dir is not a schedulable box webcast plan" >&2; exit 1; }
eval "$PLAN_EXPORTS"

REMOTE_REPO=$("${SSH[@]}" "cd ${BOX_REPO} && pwd -P") || {
  echo "REFUSED: cannot resolve the box repository" >&2
  exit 1
}
[[ "$REMOTE_REPO" == /* && "$REMOTE_REPO" != *[$'\t\r\n ']* ]] || {
  echo "REFUSED: box returned an unsafe repository path" >&2
  exit 1
}

PINNED_BIN="$REMOTE_REPO/state/$PLAN_EVENT_ID/earshot-pinned"
"${SSH[@]}" "test -f '$PINNED_BIN' && test -x '$PINNED_BIN' && test ! -L '$PINNED_BIN'" || {
  echo "REFUSED: box preflight pin is missing, non-executable, or symlinked: $PINNED_BIN" >&2
  exit 1
}

# ── Transfer the exact RunPlan + receipt prefix ───────────────────────────
# A normal source deploy explicitly excludes var/runs. This is the only path
# that may publish a run on the execution host. rsync lands in a private
# namespace first; a checksum dry-run proves the source did not change during
# transfer, then the box's eventctl and run-host-boundary helper verify/publish.
REMOTE_RUN="$REMOTE_REPO/var/runs/$PLAN_EVENT_ID/$PLAN_RUN_ID"
if [[ "$VERIFY_ONLY" != 1 ]]; then
  STAGING=$("${SSH[@]}" "mkdir -p '$REMOTE_REPO/var/host-boundary-staging' && mktemp -d '$REMOTE_REPO/var/host-boundary-staging/dispatch.XXXXXX'") || {
    echo "REFUSED: cannot allocate private box run staging" >&2; exit 1;
  }
  [[ "$STAGING" == "$REMOTE_REPO"/var/host-boundary-staging/dispatch.* ]] || {
    echo "REFUSED: box returned an unsafe staging path '$STAGING'" >&2; exit 1;
  }
  STAGED_RUN="$STAGING/$PLAN_EVENT_ID/$PLAN_RUN_ID"
  cleanup_staging() {
    "${SSH[@]}" "case '$STAGING' in '$REMOTE_REPO'/var/host-boundary-staging/dispatch.*) rm -rf -- '$STAGING';; esac" >/dev/null 2>&1 || true
  }
  trap cleanup_staging EXIT
  "${SSH[@]}" "mkdir -p '$STAGING/$PLAN_EVENT_ID'" || {
    echo "REFUSED: cannot create staged event directory" >&2; exit 1;
  }
  RSYNC_RSH="$SSH_BIN -o ConnectTimeout=15 -o BatchMode=yes -i $KEY"
  "$RSYNC_BIN" -a --delete -e "$RSYNC_RSH" "$RUN_DIR/" "$BOX:$STAGED_RUN/" || {
    echo "REFUSED: RunPlan prefix transfer failed" >&2; exit 1;
  }
  STABILITY=$("$RSYNC_BIN" -ani --checksum --delete -e "$RSYNC_RSH" \
    "$RUN_DIR/" "$BOX:$STAGED_RUN/") || {
      echo "REFUSED: RunPlan prefix stability check failed" >&2; exit 1;
    }
  [[ -z "$STABILITY" ]] || {
    printf '%s\n' "$STABILITY" >&2
    echo "REFUSED: RunPlan prefix changed during transfer" >&2
    exit 1
  }
  "${SSH[@]}" "cd '$REMOTE_REPO' && EARSHOT_REPO='$REMOTE_REPO' python3 scripts/eventctl.py verify --run-dir '$STAGED_RUN' && EARSHOT_REPO='$REMOTE_REPO' python3 scripts/run-host-boundary.py reconcile --repo '$REMOTE_REPO' --incoming '$STAGED_RUN' --destination '$REMOTE_RUN' --expected-host '$PLAN_HOST'" || {
    echo "REFUSED: box rejected the staged RunPlan prefix" >&2; exit 1;
  }
  cleanup_staging
  trap - EXIT
else
  "${SSH[@]}" "cd '$REMOTE_REPO' && EARSHOT_REPO='$REMOTE_REPO' python3 scripts/eventctl.py verify --run-dir '$REMOTE_RUN'" >/dev/null || {
    echo "REFUSED: installed box RunPlan/ledger is missing or invalid" >&2; exit 1;
  }
fi

TIMER_ARGS=(--run-dir "$REMOTE_RUN" --expected-host "$PLAN_HOST")
[[ -n "$FIRE_AT_UTC" ]] && TIMER_ARGS+=(--fire-at-utc "$FIRE_AT_UTC")
[[ "$VERIFY_ONLY" == 1 ]] && TIMER_ARGS+=(--verify-only)
REMOTE_TIMER_ARGS=$(printf ' %q' "${TIMER_ARGS[@]}")
"${SSH[@]}" "cd '$REMOTE_REPO' && EARSHOT_REPO='$REMOTE_REPO' scripts/install-event-timer.sh$REMOTE_TIMER_ARGS" || {
  echo "REFUSED: box systemd install/loaded-readback proof failed" >&2
  exit 1
}

# A green installer exit is insufficient until the box-owned ledger re-verifies
# with the durable scheduled receipt written by schedule-proof.py.
"${SSH[@]}" "cd '$REMOTE_REPO' && EARSHOT_REPO='$REMOTE_REPO' python3 scripts/eventctl.py verify --run-dir '$REMOTE_RUN' && EARSHOT_REPO='$REMOTE_REPO' python3 scripts/eventctl.py status --run-dir '$REMOTE_RUN' --json" >/dev/null || {
  echo "REFUSED: box ledger failed post-install verification" >&2
  exit 1
}

echo "SCHEDULED: $PLAN_EVENT_ID/$PLAN_RUN_ID via systemd-user on $BOX (host=$PLAN_HOST resolver=$RESOLVER_KIND)"
echo "  loaded readback: scripts/install-event-timer.sh --verify-only"
