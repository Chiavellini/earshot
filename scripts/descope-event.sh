#!/usr/bin/env bash
# Descope one market event ON THE TYPED RECORD.
#
#   descope-event.sh <EVENT_TICKER> "<reason>" [--fallbacks "<tried1>; <tried2>"]
#
# EVENT_TICKER is the exact RunPlan `market_event_ticker`, not a lane event ID
# or filename prefix. Every related schema-3 RunPlan is validated before any
# transition is appended. All active pre-launch lane attempts are then closed
# with typed `descope` receipts before the legacy projection is published.
#
# A genuinely legacy event (no exact or related typed RunPlan) retains the old
# DESCOPED.json + journal behavior. A malformed, ambiguous, launched, or other-
# terminal typed selection is a refusal, never an excuse to fall back to legacy
# state and leave a schedulable RunPlan active.
set -uo pipefail

REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
cd "$REPO" || exit 2

usage() {
  echo 'usage: descope-event.sh <EVENT_TICKER> "<reason>" [--fallbacks "<tried1>; <tried2>"]' >&2
  exit 2
}
refuse() { echo "REFUSED: $*" >&2; exit 1; }

[[ $# -ge 2 ]] || usage
EV="$1"
REASON="$2"
shift 2
[[ "$EV" =~ ^[A-Z0-9]+(-[A-Z0-9]+)*$ && ${#EV} -le 64 ]] || \
  refuse "EVENT_TICKER must be an exact uppercase market-event ticker"
[[ -n "$REASON" ]] || refuse "a descope without a reason is a miss with paperwork"

FALLBACKS=""
if [[ $# -gt 0 ]]; then
  [[ "$1" == "--fallbacks" && $# -eq 2 ]] || usage
  FALLBACKS="$2"
fi

MANIFEST=$(mktemp "${TMPDIR:-/tmp}/earshot-descope.XXXXXX") || \
  refuse "could not allocate typed-selection manifest"
TARGETS=$(mktemp "${TMPDIR:-/tmp}/earshot-descope-targets.XXXXXX") || {
  rm -f -- "$MANIFEST"
  refuse "could not allocate typed-selection target list"
}
cleanup() { rm -f -- "$MANIFEST" "$TARGETS"; }
trap cleanup EXIT

# Resolve and prevalidate the complete typed selection before any lifecycle or
# legacy write. Only directories related to this ticker can prevent the legacy
# fallback; unrelated historical runs do not make an old event undescopeable.
if ! EARSHOT_REPO="$REPO" python3 - "$REPO" "$EV" > "$MANIFEST" <<'PY'
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path


repo = Path(sys.argv[1]).resolve()
ticker = sys.argv[2]
run_root = repo / "var" / "runs"
eventctl = repo / "scripts" / "eventctl.py"


def fail(message: str) -> None:
    raise SystemExit(f"REFUSED: {message}")


def related_name(value: object) -> bool:
    return isinstance(value, str) and (
        value == ticker or value.startswith(ticker + "-")
    )


if run_root.is_symlink():
    fail(f"typed run root is a symlink: {run_root}")
if run_root.exists() and not run_root.is_dir():
    fail(f"typed run root is not a directory: {run_root}")

matches = []
related_mismatches = []
if run_root.is_dir():
    for event_dir in sorted(run_root.iterdir()):
        dir_related = related_name(event_dir.name)
        if event_dir.is_symlink() or not event_dir.is_dir():
            if dir_related:
                fail(f"related event candidate is not a regular directory: {event_dir}")
            continue
        for run_dir in sorted(event_dir.iterdir()):
            if run_dir.is_symlink() or not run_dir.is_dir():
                if dir_related:
                    fail(f"related run candidate is not a regular directory: {run_dir}")
                continue
            plan_path = run_dir / "control" / "run-plan.json"
            if plan_path.is_symlink() or not plan_path.is_file():
                if dir_related:
                    fail(f"related run candidate has no regular RunPlan: {run_dir}")
                continue
            try:
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                if dir_related:
                    fail(f"related RunPlan is unreadable: {plan_path}: {exc}")
                continue
            event_id = plan.get("event_id")
            exact = plan.get("market_event_ticker") == ticker
            related = dir_related or related_name(event_id)
            if not exact:
                if related:
                    related_mismatches.append(
                        {
                            "run_dir": str(run_dir),
                            "event_id": event_id,
                            "market_event_ticker": plan.get("market_event_ticker"),
                        }
                    )
                continue
            if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
                fail(f"selected run is not an exact schema-3 RunPlan: {run_dir}")
            if event_dir.name != event_id or run_dir.name != plan.get("run_id"):
                fail(f"selected RunPlan identity differs from its directory: {run_dir}")
            matches.append((run_dir.resolve(), plan))

if related_mismatches:
    detail = ", ".join(
        f"{row['event_id']}/{Path(row['run_dir']).name}"
        f"=>{row['market_event_ticker']!r}"
        for row in related_mismatches
    )
    fail(
        "related typed runs are outside the exact market-ticker selection; "
        f"refusing a partial/legacy descope: {detail}"
    )

if not matches:
    print(json.dumps({"schema": 1, "mode": "legacy-only", "ticker": ticker}))
    raise SystemExit(0)

targets = []
already = []
for run_dir, plan in matches:
    env = {**os.environ, "EARSHOT_REPO": str(repo)}
    verify = subprocess.run(
        [sys.executable, str(eventctl), "verify", "--run-dir", str(run_dir)],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if verify.returncode:
        fail(
            f"selected RunPlan failed verification: {run_dir}: "
            f"{(verify.stderr or verify.stdout).strip()}"
        )
    status = subprocess.run(
        [
            sys.executable,
            str(eventctl),
            "status",
            "--run-dir",
            str(run_dir),
            "--json",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if status.returncode:
        fail(
            f"selected RunPlan status failed: {run_dir}: "
            f"{(status.stderr or status.stdout).strip()}"
        )
    payload = json.loads(status.stdout)
    state = payload.get("state") or {}
    reached = {
        row.get("transition")
        for row in state.get("reached", [])
        if isinstance(row, dict)
    }
    row = {
        "run_dir": str(run_dir),
        "event_id": plan["event_id"],
        "run_id": plan["run_id"],
        "plan_sha256": plan["plan_sha256"],
        "lane": plan["lane"],
    }
    if state.get("active") is True:
        if "launched" in reached:
            fail(
                f"selected run {plan['event_id']}/{plan['run_id']} has launched; "
                "use aborted/failed, not descope"
            )
        targets.append(row)
    elif "descope" in reached:
        already.append(row)
    else:
        terminal = (state.get("terminal") or {}).get("transition")
        fail(
            f"selected run {plan['event_id']}/{plan['run_id']} is already "
            f"terminal as {terminal or 'unknown'}, not descope"
        )

counts = Counter(row["event_id"] for row in targets)
ambiguous = sorted(event_id for event_id, count in counts.items() if count > 1)
if ambiguous:
    fail(
        "multiple active attempts exist for the same lane event; select/close "
        f"the collision explicitly before descoping: {ambiguous}"
    )
if not targets and not already:
    fail("typed selection unexpectedly produced no active or descoped run")

print(
    json.dumps(
        {
            "schema": 1,
            "mode": "typed",
            "ticker": ticker,
            "targets": sorted(targets, key=lambda row: row["run_dir"]),
            "already_descoped": sorted(already, key=lambda row: row["run_dir"]),
        },
        sort_keys=True,
    )
)
PY
then
  exit 1
fi

MODE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["mode"])' "$MANIFEST") || \
  refuse "typed-selection manifest is unreadable"

if [[ "$MODE" == "typed" ]]; then
  python3 - "$MANIFEST" > "$TARGETS" <<'PY' || refuse "could not read typed targets"
import json
import sys
for row in json.load(open(sys.argv[1], encoding="utf-8"))["targets"]:
    path = row["run_dir"]
    if "\n" in path or "\r" in path:
        raise SystemExit("unsafe newline in run path")
    print(path)
PY

  # Prevalidation above prevents known partial selection. If an external actor
  # races this loop, a failed append stops immediately and no legacy projection
  # is published; rerunning safely completes any already-recorded prefix.
  while IFS= read -r run_dir; do
    [[ -n "$run_dir" ]] || continue
    if ! EARSHOT_REPO="$REPO" python3 scripts/eventctl.py record \
      --run-dir "$run_dir" \
      --transition descope \
      --actor descope-event \
      --detail "$REASON${FALLBACKS:+; fallbacks=$FALLBACKS}"
    then
      echo "REFUSED: typed descope stopped before legacy publication; rerun after reconciling $run_dir" >&2
      exit 1
    fi
  done < "$TARGETS"

  # Re-read every selected ledger. The legacy projection may exist only after
  # all exact runs durably show the terminal transition.
  if ! EARSHOT_REPO="$REPO" python3 - "$REPO" "$MANIFEST" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
manifest = json.load(open(sys.argv[2], encoding="utf-8"))
rows = manifest["targets"] + manifest["already_descoped"]
for row in rows:
    result = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "eventctl.py"),
            "status",
            "--run-dir",
            row["run_dir"],
            "--json",
        ],
        cwd=repo,
        env={**os.environ, "EARSHOT_REPO": str(repo)},
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise SystemExit(f"REFUSED: post-descope status failed for {row['run_dir']}")
    reached = {
        item.get("transition")
        for item in json.loads(result.stdout)["state"]["reached"]
    }
    if "descope" not in reached:
        raise SystemExit(f"REFUSED: descope is not durable for {row['run_dir']}")
PY
  then
    exit 1
  fi
elif [[ "$MODE" != "legacy-only" ]]; then
  refuse "unknown typed-selection mode $MODE"
fi

DIR="state/${EV}"
mkdir -p "$DIR" || refuse "could not create legacy projection directory $DIR"
OUT="$DIR/DESCOPED.json"

# Publish the compatibility projection only after typed authority is terminal.
# It records which typed runs it projects so a grader never has to infer scope.
EV="$EV" REASON="$REASON" FALLBACKS="$FALLBACKS" MODE="$MODE" \
  MANIFEST="$MANIFEST" OUT="$OUT" python3 - <<'PY' || \
  refuse "could not durably publish $OUT"
import json
import os
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path

out = Path(os.environ["OUT"])
manifest = json.load(open(os.environ["MANIFEST"], encoding="utf-8"))
typed = manifest.get("targets", []) + manifest.get("already_descoped", [])
payload = {
    "event_ticker": os.environ["EV"],
    "reason": os.environ["REASON"],
    "fallbacks_exhausted": [
        value.strip()
        for value in os.environ.get("FALLBACKS", "").split(";")
        if value.strip()
    ],
    "decided_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "decided_by": os.environ.get("USER", "unknown"),
    "host": socket.gethostname().split(".")[0],
    "authority": (
        "typed-run-ledgers" if os.environ["MODE"] == "typed" else "legacy-only"
    ),
    "typed_runs": [
        {
            key: row[key]
            for key in ("event_id", "run_id", "plan_sha256", "lane")
        }
        for row in sorted(typed, key=lambda value: value["run_dir"])
    ],
}
fd, temporary = tempfile.mkstemp(prefix=".DESCOPED.", dir=out.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, out)
    directory = os.open(out.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
except Exception:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY

echo "DESCOPED: $EV — $REASON ($MODE)"
echo "record: $OUT"

# Historical consumers retain their projection. With typed runs already
# terminal, journal.sh cannot mistake this compatibility observation for the
# authoritative transition or advance another attempt.
scripts/journal.sh --event "$EV" --actor descope-event.sh --action descope --outcome info \
  --detail "$REASON" --log "$OUT" || true
scripts/notify.sh "DESCOPED $EV" \
  "$REASON${FALLBACKS:+ — fallbacks tried: $FALLBACKS} (record: $OUT)" \
  --quiet || true
