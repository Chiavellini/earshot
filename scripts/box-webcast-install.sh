#!/usr/bin/env bash
# Transfer one exact schema-3 webcast RunPlan to its execution box and install
# the box's systemd-user timer. This is a dispatch boundary only: it does not
# resolve media, build a binary, create a pin, dial, open ports, or move phone
# credentials.
#
#   scripts/box-webcast-install.sh --run-dir var/runs/<EVENT_ID>/<RUN_ID>
#       [--fire-at-utc UTC] [--verify-only]
#   scripts/box-webcast-install.sh --run-dir <run> --dispatch-only
#
# A successful return proves the canonical schema-2 AccessPlan is non-static,
# the plan is bound to EARSHOT_BOX_EXECUTION_HOST, the box already has its
# executable preflight pin, and the LOADED systemd unit passed schedule-proof.
#
# --dispatch-only stops after the transfer: it publishes the RunPlan prefix on
# the box and installs NOTHING. It exists because box webcast preflight runs on
# the box (seam S-C) and hard-refuses without the laptop-bound AccessPlan, while
# deploy-box.sh excludes var/runs/ and the full path above will not dispatch
# until preflight_passed — a circle that was previously broken only by hand
# rsync. So --dispatch-only drops exactly two gates, both of which are
# SCHEDULING preconditions rather than transport ones: the preflight_passed
# requirement and the box pin check. Every transport safety property is kept —
# private staging namespace, checksum stability re-check, remote eventctl
# verify, and the run-host-boundary prefix reconcile that refuses divergence.
#
# It is bounded BEFORE launched, the same bound (and the same reasoning) as
# pull-box-state.sh --prelaunch: until launched a run has no capture/, book/ or
# orders/ content, only atomically written control proofs, so there is no second
# growing authority for a push to race. It records no receipt — inventing a
# lifecycle transition for a transport step would be a schema change.
#
# What every successful transfer DOES record (E11): this copy's ledger write
# authority now belongs to the box (`eventctl authority --transfer-to`, a
# host-private sidecar, not a receipt, never transported). The box mints the
# next receipts (`scheduled`, ...); a laptop-side append before they are pulled
# back would fork the chain permanently, so eventctl refuses it until
# `pull-box-state.sh --prelaunch` returns authority. A CLOSED attempt is not
# transferred: it will never be extended by the box, and its `archived` tail is
# laptop bookkeeping.
#
# Every ssh and rsync leg rides ONE multiplexed master per invocation (E13):
# ControlMaster=auto, socket under /tmp, ControlPersist=120, torn down on exit.
set -euo pipefail

REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPO=$(cd "$REPO" && pwd -P)
cd "$REPO"

RUN_DIR_RAW="" VERIFY_ONLY=0 FIRE_AT_UTC="" DISPATCH_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir)       RUN_DIR_RAW="${2:?}"; shift 2 ;;
    --fire-at-utc)   FIRE_AT_UTC="${2:?}"; shift 2 ;;
    --verify-only)   VERIFY_ONLY=1; shift ;;
    --dispatch-only) DISPATCH_ONLY=1; shift ;;
    -h|--help)       sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$RUN_DIR_RAW" ]] || {
  echo "usage: $0 --run-dir var/runs/<EVENT_ID>/<RUN_ID> [--fire-at-utc UTC] [--verify-only]" >&2
  echo "       $0 --run-dir var/runs/<EVENT_ID>/<RUN_ID> --dispatch-only" >&2
  exit 2
}
# --dispatch-only installs nothing, so pairing it with a scheduling argument is
# always a mistake about what the caller is doing. Refuse rather than silently
# ignore one of them.
if [[ "$DISPATCH_ONLY" == 1 ]]; then
  [[ "$VERIFY_ONLY" != 1 ]] || {
    echo "REFUSED: --dispatch-only and --verify-only are mutually exclusive" >&2; exit 2; }
  [[ -z "$FIRE_AT_UTC" ]] || {
    echo "REFUSED: --dispatch-only installs no timer, so --fire-at-utc is meaningless" >&2; exit 2; }
fi

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
SSH_OPTS=(-o ConnectTimeout=15 -o BatchMode=yes -i "$KEY")

# One multiplexed master per invocation (E13). A full dispatch opens ten
# ssh/rsync legs in a few seconds; sshd's MaxStartups drops bursts of fresh
# connections AT RANDOM, which surfaced as "rsync failed for run <a different
# run each retry>" and cascaded into E12. Mirror of scripts/pull-box-state.sh
# start_ssh_mux. The socket MUST live under /tmp, not TMPDIR: unix sockaddr
# paths cap near 104 bytes and macOS's TMPDIR alone is ~50.
SSH_MUX_DIR=""
start_ssh_mux() {
  SSH_MUX_DIR=$(mktemp -d /tmp/ear-dispatch-mux.XXXXXX) || return 1
  SSH_OPTS+=(-o ControlMaster=auto -o "ControlPath=$SSH_MUX_DIR/mux" -o ControlPersist=120)
}
stop_ssh_mux() {
  [[ -n "$SSH_MUX_DIR" ]] || return 0
  # Only a real master owns a socket. A stub, or a refusal before the first
  # connection, leaves none — and then no teardown call may reach the box.
  if [[ -S "$SSH_MUX_DIR/mux" ]]; then
    "$SSH_BIN" "${SSH_OPTS[@]}" -O exit "$BOX" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$SSH_MUX_DIR"; SSH_MUX_DIR=""
}
# ONE exit trap, set once. A later `trap … EXIT` would silently replace it and
# leave the master behind; staging cleanup is a flag inside it instead.
STAGING_LIVE=0
on_exit() {
  if (( STAGING_LIVE )); then cleanup_staging; fi
  stop_ssh_mux
}
start_ssh_mux || { echo "REFUSED: could not allocate the SSH multiplex directory under /tmp" >&2; exit 1; }
trap on_exit EXIT
SSH=("$SSH_BIN" "${SSH_OPTS[@]}" "$BOX")

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
# A closed attempt is dispatched for one reason only: to tell the box it is
# dead. It will never acquire media again, so demanding a canonical AccessPlan
# would block the very propagation that clears "ambiguous: N active runs".
RUN_IS_ACTIVE=$(EARSHOT_REPO="$REPO" python3 scripts/eventctl.py status \
  --run-dir "$RUN_DIR" --json | python3 -c \
  'import json,sys; print("1" if json.load(sys.stdin)["state"]["active"] else "0")') || {
    echo "REFUSED: cannot read local run state" >&2
    exit 1
  }
if [[ "$DISPATCH_ONLY" == 1 && "$RUN_IS_ACTIVE" == 0 ]]; then
  echo "note: dispatching a CLOSED attempt — propagating supersession only"
else
  # Say why a closed run is refused, here, in one line. Falling through to the
  # AccessPlan validator instead reports "no canonical schema-2 AccessPlan",
  # which sends the operator to look at a perfectly good access plan.
  [[ "$RUN_IS_ACTIVE" == 1 ]] || {
    echo "REFUSED: RunPlan is terminal or archived" >&2
    exit 1
  }
  EARSHOT_REPO="$REPO" python3 scripts/access-plan.py validate \
    --run-dir "$RUN_DIR" --canonical >/dev/null || {
      echo "REFUSED: webcast RunPlan has no canonical schema-2 AccessPlan" >&2
      exit 1
    }
fi

PLAN_EXPORTS=$(python3 - "$REPO" "$RUN_DIR" "$EXPECTED_HOST" "$DISPATCH_ONLY" <<'PY'
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

repo, run, expected_host = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
dispatch_only = sys.argv[4] == "1"
plan = json.loads((run / "control/run-plan.json").read_text())
access_path = run / "control/access-plan.json"
# Only a supersession dispatch may proceed without one; see the bash guard above.
if dispatch_only and not access_path.exists():
    access = {}
else:
    access = json.loads(access_path.read_text())
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
    if not dispatch_only:
        raise SystemExit("AccessPlan has no typed resolver kind")
    resolver_kind = "n/a-supersession"
status = subprocess.run(
    [sys.executable, str(repo / "scripts/eventctl.py"), "status", "--run-dir", str(run), "--json"],
    cwd=repo, text=True, capture_output=True, check=True,
    env={**__import__('os').environ, "EARSHOT_REPO": str(repo)},
)
state = json.loads(status.stdout)["state"]
reached = {r["transition"] for r in state["reached"]}
if dispatch_only:
    # Transport, not scheduling. preflight_passed is precisely what this mode
    # exists to make reachable, so requiring it here would restore the circle.
    # The bound instead is pre-launched: past that point the box owns a growing
    # run and a push from the laptop could race it.
    if "launched" in reached:
        raise SystemExit(
            "RunPlan has reached launched; the box owns it now — pull terminal "
            "evidence with pull-box-state.sh instead of pushing over it"
        )
elif "preflight_passed" not in reached:
    raise SystemExit("RunPlan has not reached preflight_passed")
# Scheduling a dead run is always wrong. Propagating the news that a run is
# dead is the opposite: leaving a superseded attempt "active" on the box is what
# makes the next on-box preflight refuse with "ambiguous: N active runs". So
# --dispatch-only deliberately carries closed ledgers too; the prefix reconcile
# still refuses anything that is not a clean extension of the box's chain.
if not state["active"] and not dispatch_only:
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

# E11: authority follows the canonical copy of a run the box will still
# extend. Called only after the box has verified+published this prefix (or,
# under --verify-only, re-verified the copy it already holds). Fail-closed: a
# box that holds the run while this copy still thinks it is writable is the
# exact fork the door exists to prevent, so a failed marker is a failed
# dispatch — the nightly re-run reconciles `already-equal` and retries here.
transfer_authority() {
  [[ "$RUN_IS_ACTIVE" == 1 ]] || return 0   # closed attempt: laptop keeps its archived tail
  EARSHOT_REPO="$REPO" python3 scripts/eventctl.py authority --run-dir "$RUN_DIR" \
    --transfer-to "$PLAN_HOST" --by box-webcast-install >/dev/null || {
      echo "REFUSED: box holds $PLAN_EVENT_ID/$PLAN_RUN_ID but this copy's ledger authority marker could not be written;" >&2
      echo "         re-run, or: scripts/eventctl.py authority --run-dir $RUN_DIR --transfer-to $PLAN_HOST --by operator" >&2
      exit 1
    }
}

REMOTE_REPO=$("${SSH[@]}" "cd ${BOX_REPO} && pwd -P") || {
  echo "REFUSED: cannot resolve the box repository" >&2
  exit 1
}
[[ "$REMOTE_REPO" == /* && "$REMOTE_REPO" != *[$'\t\r\n ']* ]] || {
  echo "REFUSED: box returned an unsafe repository path" >&2
  exit 1
}

# The pin is produced BY the box preflight this mode exists to unblock, so it
# cannot exist yet under --dispatch-only. It remains mandatory for scheduling.
if [[ "$DISPATCH_ONLY" != 1 ]]; then
  PINNED_BIN="$REMOTE_REPO/state/$PLAN_EVENT_ID/earshot-pinned"
  "${SSH[@]}" "test -f '$PINNED_BIN' && test -x '$PINNED_BIN' && test ! -L '$PINNED_BIN'" || {
    echo "REFUSED: box preflight pin is missing, non-executable, or symlinked: $PINNED_BIN" >&2
    exit 1
  }
fi

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
  STAGING_LIVE=1   # on_exit (set once above) now cleans staging too
  "${SSH[@]}" "mkdir -p '$STAGING/$PLAN_EVENT_ID'" || {
    echo "REFUSED: cannot create staged event directory" >&2; exit 1;
  }
  RSYNC_RSH="$SSH_BIN ${SSH_OPTS[*]}"   # same options — same multiplexed master — for rsync
  # control/authority.json is THIS host's private authority marker (E11); it
  # never travels. Both passes must carry the same exclude or the stability
  # check itemises it as a change. run-host-boundary.py ignores it too.
  "$RSYNC_BIN" -a --delete --exclude=/control/authority.json -e "$RSYNC_RSH" "$RUN_DIR/" "$BOX:$STAGED_RUN/" || {
    echo "REFUSED: RunPlan prefix transfer failed" >&2; exit 1;
  }
  STABILITY=$("$RSYNC_BIN" -ani --checksum --delete --exclude=/control/authority.json -e "$RSYNC_RSH" \
    "$RUN_DIR/" "$BOX:$STAGED_RUN/") || {
      echo "REFUSED: RunPlan prefix stability check failed" >&2; exit 1;
    }
  [[ -z "$STABILITY" ]] || {
    printf '%s\n' "$STABILITY" >&2
    echo "REFUSED: RunPlan prefix changed during transfer" >&2
    exit 1
  }
  "${SSH[@]}" "cd '$REMOTE_REPO' && EARSHOT_REPO='$REMOTE_REPO' python3 scripts/eventctl.py verify --run-dir '$STAGED_RUN' && EARSHOT_REPO='$REMOTE_REPO' python3 scripts/run-host-boundary.py reconcile --repo '$REMOTE_REPO' --incoming '$STAGED_RUN' --destination '$REMOTE_RUN' --expected-host '$PLAN_HOST'" || {
    echo "REFUSED: box rejected the staged RunPlan prefix" >&2
    # Divergence here has one overwhelmingly common cause, and the message from
    # the boundary primitive ("neither is an exact byte prefix") does not name
    # it: a box run's `scheduled` receipt is minted on the BOX, so recording
    # anything on the laptop copy before pulling that receipt back FORKS the
    # chain, permanently — both sides then hold a different receipt at the same
    # index and no prefix relation can exist again.
    if [[ "$RUN_IS_ACTIVE" == 0 ]]; then
      echo "  hint: was this attempt closed on the laptop AFTER it was scheduled?" >&2
      echo "        the 'scheduled' receipt is minted on the box; pull it back first:" >&2
      echo "          scripts/pull-box-state.sh --prelaunch --event $PLAN_EVENT_ID" >&2
      echo "        then close, then dispatch. A chain already forked this way cannot" >&2
      echo "        be rejoined — close the box copy directly over SSH instead." >&2
      echo "        (since 2026-08-17 eventctl refuses that laptop-side write outright — E11)" >&2
    fi
    exit 1
  }
  cleanup_staging
  STAGING_LIVE=0
else
  "${SSH[@]}" "cd '$REMOTE_REPO' && EARSHOT_REPO='$REMOTE_REPO' python3 scripts/eventctl.py verify --run-dir '$REMOTE_RUN'" >/dev/null || {
    echo "REFUSED: installed box RunPlan/ledger is missing or invalid" >&2; exit 1;
  }
fi
# The box now holds (or has re-proven it holds) this run's chain: mark this
# copy accordingly before anything else can append to it. --verify-only lands
# here too — schedule-nightly sends it once the laptop ledger already shows
# `scheduled` (i.e. after a pull), and a green re-verify proves the box still
# holds the armed job, so authority converges back to remote.
transfer_authority

if [[ "$DISPATCH_ONLY" == 1 ]]; then
  echo "DISPATCHED: $PLAN_EVENT_ID/$PLAN_RUN_ID → $BOX:$REMOTE_RUN (host=$PLAN_HOST resolver=$RESOLVER_KIND)"
  if [[ "$RUN_IS_ACTIVE" == 1 ]]; then
    echo "  no timer installed, no receipt recorded; ledger authority now held by the box —"
    echo "  pull (scripts/pull-box-state.sh --prelaunch --event $PLAN_EVENT_ID) before recording anything on this copy (E11)"
  else
    echo "  no timer installed, no receipt recorded (closed attempt: supersession propagated)"
  fi
  echo "  next: preflight ON the box, then pull-box-state.sh --prelaunch --event $PLAN_EVENT_ID"
  exit 0
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
