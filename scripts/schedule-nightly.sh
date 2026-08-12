#!/usr/bin/env bash
# Schedule every preflight-passed schema-3 RunPlan for one UTC event date.
#
# This is the sole nightly preparation -> scheduler join. It enumerates
# immutable RunPlans (never config filename shapes), refuses any active run
# that has not reached preflight_passed, dispatches phone runs through the
# verified box boundary, and installs webcast runs locally. The lane installer
# may append `scheduled` only after reading back a loaded job bound to the exact
# run directory and plan hash.
#
#   scripts/schedule-nightly.sh --date YYYY-MM-DD
#   scripts/schedule-nightly.sh --date YYYY-MM-DD --dry-run
#   scripts/schedule-nightly.sh --check
set -uo pipefail

REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO" || exit 2
INSTALL_T0="${_EARSHOT_SCHEDULE_INSTALL_T0:-$REPO/scripts/install-t0-agents.sh}"
BOX_INSTALL="${_EARSHOT_SCHEDULE_BOX_INSTALL:-$REPO/scripts/box-event-install.sh}"
NOTIFY="${_EARSHOT_SCHEDULE_NOTIFY:-$REPO/scripts/notify.sh}"
ALERTCTL="${_EARSHOT_SCHEDULE_ALERTCTL:-$REPO/scripts/alertctl.py}"

DATE="" DRY_RUN=0 CHECK_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --date) DATE="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) sed -n '1,16p' "$0"; exit 0 ;;
    *) echo "USAGE: unknown argument: $1" >&2; exit 2 ;;
  esac
done

legacy_page() {
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "DRY-RUN WOULD PAGE (no trustworthy RunPlan): $1"
  else
    "$NOTIFY" "$1" "$2" --quiet >/dev/null 2>&1 || true
  fi
}

# One invocation delegates durable request/delivery facts, bounded retry, and
# ambiguous-call suppression to platform.notify. This helper always returns
# success so notification trouble cannot replace the schedule failure verdict.
critical_alert() {  # <run-dir> <event-id> <code> <dedupe-key> <body>
  local run_dir="$1" event_id="$2" code="$3" dedupe="$4" body="$5"
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "DRY-RUN WOULD REQUIRE CRITICAL ALERT $event_id code=$code dedupe=$dedupe"
    return 0
  fi
  if [[ ! -x "$ALERTCTL" ]]; then
    echo "ALERT REFUSED $event_id: alertctl adapter is unavailable at $ALERTCTL" >&2
    return 0
  fi
  EARSHOT_REPO="$REPO" "$ALERTCTL" raise \
    --run-dir "$run_dir" --severity critical --code "$code" \
    --dedupe-key "$dedupe" --subject "Nightly schedule failed: $event_id" \
    --body "$body" >/dev/null 2>&1 || {
      echo "ALERT DELIVERY FAILED $event_id: original schedule failure remains nonzero" >&2
      return 0
    }
}

run_self_check() {
  local root repo date event_phone event_web run transition
  root=$(mktemp -d /tmp/schedule-nightly-check.XXXXXX)
  trap 'rm -rf -- "$root"' RETURN
  repo="$root/repo"
  mkdir -p "$repo/scripts" "$repo/configs" "$repo/calls"
  cp "$REPO/scripts/eventctl.py" "$repo/scripts/eventctl.py"
  cp "$REPO/scripts/schedule-nightly.sh" "$repo/scripts/schedule-nightly.sh"
  date="2026-08-14"
  event_phone="KXEARNINGSMENTIONSCHEDPHONE-26AUG14-PHONE"
  event_web="KXEARNINGSMENTIONSCHEDWEB-26AUG14-WEBCAST"
  cat > "$repo/configs/phone.toml" <<EOF
[event]
id = "$event_phone"
starts_at = "2026-08-14T21:00:00Z"
[[sources]]
kind = "twilio_stream"
bind = "127.0.0.1:8081"
[[keywords]]
market_ticker = "KXEARNINGSMENTIONSCHEDPHONE-26AUG14-FOO"
phrase = "foo"
EOF
  cat > "$repo/configs/web.toml" <<EOF
[event]
id = "$event_web"
starts_at = "2026-08-14T22:00:00Z"
[[sources]]
kind = "pipe"
cmd = ["ffmpeg", "-i", "\$HOME/web-stream.url"]
[[keywords]]
market_ticker = "KXEARNINGSMENTIONSCHEDWEB-26AUG14-FOO"
phrase = "foo"
EOF
  cat > "$repo/configs/phone.env" <<EOF
CONFIG_PATH="configs/phone.toml"
EVENT_DATE="$date"
EOF
  cat > "$repo/configs/web.env" <<EOF
CONFIG_PATH="configs/web.toml"
EVENT_DATE="$date"
EOF
  for lane in phone web; do
    EARSHOT_REPO="$repo" python3 "$repo/scripts/eventctl.py" init \
      --config "configs/$lane.toml" --event-env "configs/$lane.env" \
      --host-id "earshot-test-host" \
      --account-fingerprint "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
      >/dev/null
    run=$(find "$repo/var/runs" -path "*/control/run-plan.json" \
      -exec dirname {} \; | sed 's#/control$##' | while read -r candidate; do
        python3 -c 'import json,sys; p=json.load(open(sys.argv[1]+"/control/run-plan.json")); print(sys.argv[1]) if p["config_path"]==sys.argv[2] else None' \
          "$candidate" "configs/$lane.toml"
      done)
    for transition in prepared \
      $( [[ "$lane" == phone ]] && echo admission_resolved || echo access_acquired ) \
      study_completed preflight_passed
    do
      EARSHOT_REPO="$repo" python3 "$repo/scripts/eventctl.py" record \
        --run-dir "$run" --transition "$transition" >/dev/null
    done
  done
  cat > "$repo/scripts/install-t0-stub.sh" <<'EOF'
#!/usr/bin/env bash
printf 'web %s\n' "$*" >> "$EARSHOT_CHECK_CALLS"
EOF
  cat > "$repo/scripts/box-install-stub.sh" <<'EOF'
#!/usr/bin/env bash
printf 'phone %s\n' "$*" >> "$EARSHOT_CHECK_CALLS"
EOF
  cat > "$repo/scripts/notify-stub.sh" <<'EOF'
#!/usr/bin/env bash
printf 'legacy-page %s\n' "$*" >> "$EARSHOT_CHECK_NOTIFICATIONS"
exit 97
EOF
  cat > "$repo/scripts/alert-stub.sh" <<'EOF'
#!/usr/bin/env bash
printf 'run-alert %s\n' "$*" >> "$EARSHOT_CHECK_NOTIFICATIONS"
exit 98
EOF
  : > "$repo/calls/notifications"
  chmod +x "$repo/scripts/"*-stub.sh
  EARSHOT_REPO="$repo" \
    EARSHOT_LOCAL_EXECUTION_HOST="earshot-test-host" \
    EARSHOT_CHECK_CALLS="$repo/calls/invocations" \
    EARSHOT_CHECK_NOTIFICATIONS="$repo/calls/notifications" \
    _EARSHOT_SCHEDULE_INSTALL_T0="$repo/scripts/install-t0-stub.sh" \
    _EARSHOT_SCHEDULE_BOX_INSTALL="$repo/scripts/box-install-stub.sh" \
    _EARSHOT_SCHEDULE_NOTIFY="$repo/scripts/notify-stub.sh" \
    _EARSHOT_SCHEDULE_ALERTCTL="$repo/scripts/alert-stub.sh" \
    bash "$repo/scripts/schedule-nightly.sh" --date "$date" >/dev/null
  grep -q "^web --run-dir .* --expected-host earshot-test-host$" \
    "$repo/calls/invocations"
  grep -q "^phone --event-env configs/phone.env --run-dir " \
    "$repo/calls/invocations"
  [[ $(wc -l < "$repo/calls/invocations" | tr -d ' ') == 2 ]]
  [[ ! -s "$repo/calls/notifications" ]]

  # A definite installer failure raises exactly one RunPlan-bound request,
  # preserves the nonzero schedule verdict even when the injected alert
  # adapter fails, and does not prevent the webcast sibling from running.
  cat > "$repo/scripts/box-install-stub.sh" <<'EOF'
#!/usr/bin/env bash
printf 'phone %s\n' "$*" >> "$EARSHOT_CHECK_CALLS"
exit 31
EOF
  chmod +x "$repo/scripts/box-install-stub.sh"
  : > "$repo/calls/invocations"
  : > "$repo/calls/notifications"
  EARSHOT_REPO="$repo" \
    EARSHOT_LOCAL_EXECUTION_HOST="earshot-test-host" \
    EARSHOT_CHECK_CALLS="$repo/calls/invocations" \
    EARSHOT_CHECK_NOTIFICATIONS="$repo/calls/notifications" \
    _EARSHOT_SCHEDULE_INSTALL_T0="$repo/scripts/install-t0-stub.sh" \
    _EARSHOT_SCHEDULE_BOX_INSTALL="$repo/scripts/box-install-stub.sh" \
    _EARSHOT_SCHEDULE_NOTIFY="$repo/scripts/notify-stub.sh" \
    _EARSHOT_SCHEDULE_ALERTCTL="$repo/scripts/alert-stub.sh" \
    bash "$repo/scripts/schedule-nightly.sh" --date "$date" >/dev/null 2>&1
  [[ $? == 1 ]]
  grep -q '^phone ' "$repo/calls/invocations"
  grep -q '^web ' "$repo/calls/invocations"
  [[ $(wc -l < "$repo/calls/invocations" | tr -d ' ') == 2 ]]
  grep -q 'run-alert raise .*--code schedule_dispatch_failed .*--dedupe-key nightly-schedule-dispatch' \
    "$repo/calls/notifications"
  [[ $(grep -c '^run-alert ' "$repo/calls/notifications") == 1 ]]
  ! grep -q '^legacy-page ' "$repo/calls/notifications"

  # Dry-run neither installs nor calls either notification adapter.
  : > "$repo/calls/invocations"
  : > "$repo/calls/notifications"
  EARSHOT_REPO="$repo" \
    EARSHOT_LOCAL_EXECUTION_HOST="earshot-test-host" \
    EARSHOT_CHECK_CALLS="$repo/calls/invocations" \
    EARSHOT_CHECK_NOTIFICATIONS="$repo/calls/notifications" \
    _EARSHOT_SCHEDULE_INSTALL_T0="$repo/scripts/install-t0-stub.sh" \
    _EARSHOT_SCHEDULE_BOX_INSTALL="$repo/scripts/box-install-stub.sh" \
    _EARSHOT_SCHEDULE_NOTIFY="$repo/scripts/notify-stub.sh" \
    _EARSHOT_SCHEDULE_ALERTCTL="$repo/scripts/alert-stub.sh" \
    bash "$repo/scripts/schedule-nightly.sh" --date "$date" --dry-run >/dev/null
  [[ ! -s "$repo/calls/invocations" ]]
  [[ ! -s "$repo/calls/notifications" ]]
  echo "schedule-nightly --check: PASS"
}

if [[ "$CHECK_ONLY" == 1 ]]; then
  [[ -z "$DATE" && "$DRY_RUN" == 0 ]] || {
    echo "USAGE: --check cannot be combined with other arguments" >&2; exit 2;
  }
  run_self_check
  exit 0
fi

[[ -n "$DATE" ]] || { echo "USAGE: --date YYYY-MM-DD is required" >&2; exit 2; }
python3 -c 'import datetime,sys; datetime.date.fromisoformat(sys.argv[1])' "$DATE" \
  2>/dev/null || { echo "USAGE: invalid date '$DATE'" >&2; exit 2; }

MANIFEST=$(mktemp /tmp/earshot-schedule-manifest.XXXXXX)
MANIFEST_ERRORS=$(mktemp /tmp/earshot-schedule-errors.XXXXXX)
trap 'rm -f -- "$MANIFEST" "$MANIFEST_ERRORS"' EXIT
python3 - "$REPO" "$DATE" > "$MANIFEST" 2> "$MANIFEST_ERRORS" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

repo, wanted = Path(sys.argv[1]), dt.date.fromisoformat(sys.argv[2])
errors = []
for path in sorted((repo / "var" / "runs").glob("*/*/control/run-plan.json")):
    try:
        plan = json.loads(path.read_text())
        starts = dt.datetime.fromisoformat(plan["starts_at_utc"].replace("Z", "+00:00"))
        if starts.tzinfo is None:
            raise ValueError("starts_at_utc is timezone-naive")
        starts = starts.astimezone(dt.timezone.utc)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"invalid RunPlan candidate {path}: {exc}")
        continue
    if starts.date() != wanted:
        continue
    if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
        errors.append(f"unsupported RunPlan candidate {path}: schema/kind is not current")
        continue
    try:
        run = path.parent.parent
        values = [
            str(run), plan["event_id"], plan["run_id"], plan["lane"],
            plan.get("event_env_path") or "", plan["execution_host_id"],
            plan["plan_sha256"],
        ]
    except KeyError as exc:
        errors.append(f"incomplete RunPlan candidate {path}: missing {exc}")
        continue
    if not all(isinstance(value, str) for value in values):
        errors.append(f"non-string RunPlan field in {path}")
        continue
    if any("\x1f" in value or "\n" in value for value in values):
        errors.append(f"unsafe RunPlan field in {path}")
        continue
    print("\x1f".join(values))
for error in errors:
    print(f"REFUSED: {error}", file=sys.stderr)
raise SystemExit(1 if errors else 0)
PY
MANIFEST_RC=$?

TOTAL=0 SCHEDULED=0 VERIFIED=0 SKIPPED=0 PROBLEMS=0 UNTRUSTED=0
if [[ "$MANIFEST_RC" -ne 0 ]]; then
  cat "$MANIFEST_ERRORS" >&2
  UNTRUSTED=$(grep -c '^REFUSED:' "$MANIFEST_ERRORS" || true)
  [[ "$UNTRUSTED" -gt 0 ]] || UNTRUSTED=1
  PROBLEMS=$((PROBLEMS + UNTRUSTED))
fi
while IFS=$'\x1f' read -r run_dir event_id run_id lane event_env host plan_sha; do
  [[ -n "$run_dir" ]] || continue
  TOTAL=$((TOTAL + 1))
  if ! EARSHOT_REPO="$REPO" python3 scripts/eventctl.py verify \
      --run-dir "$run_dir" >/dev/null; then
    echo "REFUSED $event_id/$run_id: RunPlan/ledger verification failed" >&2
    PROBLEMS=$((PROBLEMS + 1)); UNTRUSTED=$((UNTRUSTED + 1)); continue
  fi
  STATUS=$(EARSHOT_REPO="$REPO" python3 scripts/eventctl.py status \
    --run-dir "$run_dir" --json) || {
      echo "REFUSED $event_id/$run_id: status failed" >&2
      critical_alert "$run_dir" "$event_id" "schedule_status_failed" \
        "nightly-schedule-status" \
        "The verified RunPlan's lifecycle status could not be read. Scheduling was refused and no successful schedule was recorded."
      PROBLEMS=$((PROBLEMS + 1)); continue
    }
  if ! STATE=$(python3 -c '
import json,sys
p=json.load(sys.stdin); reached={x["transition"] for x in p["state"]["reached"]}
if not p["state"]["active"]: print("terminal")
elif "preflight_passed" not in reached: print("unready")
elif "scheduled" in reached: print("scheduled")
else: print("ready")
' <<< "$STATUS"); then
    echo "REFUSED $event_id/$run_id: status payload is invalid" >&2
    critical_alert "$run_dir" "$event_id" "schedule_status_invalid" \
      "nightly-schedule-status-invalid" \
      "The verified RunPlan produced an invalid lifecycle status payload. Scheduling was refused and no successful schedule was recorded."
    PROBLEMS=$((PROBLEMS + 1)); continue
  fi
  case "$STATE" in
    terminal)
      echo "SKIP $event_id/$run_id: terminal or archived"
      SKIPPED=$((SKIPPED + 1)); continue ;;
    unready)
      echo "REFUSED $event_id/$run_id: active run has no preflight_passed receipt" >&2
      critical_alert "$run_dir" "$event_id" "schedule_preflight_missing" \
        "nightly-schedule-preflight" \
        "The active RunPlan has no proof-bearing preflight_passed receipt. Scheduling was refused; complete the generic preparation chain."
      PROBLEMS=$((PROBLEMS + 1)); continue ;;
  esac

  VERIFY_FLAG=()
  [[ "$STATE" == scheduled ]] && VERIFY_FLAG=(--verify-only)
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "DRY-RUN $lane $event_id/$run_id host=$host state=$STATE plan=$plan_sha"
    continue
  fi
  case "$lane" in
    webcast)
      local_host="${EARSHOT_LOCAL_EXECUTION_HOST:-}"
      if [[ -z "$local_host" ]]; then
        echo "REFUSED $event_id/$run_id: EARSHOT_LOCAL_EXECUTION_HOST is not configured" >&2
        critical_alert "$run_dir" "$event_id" "schedule_host_missing" \
          "nightly-schedule-host" \
          "The local execution-host identity is not configured. Webcast scheduling was refused before installation."
        PROBLEMS=$((PROBLEMS + 1)); continue
      fi
      if ! "$INSTALL_T0" --run-dir "$run_dir" --expected-host "$local_host" \
          ${VERIFY_FLAG[@]+"${VERIFY_FLAG[@]}"}; then
        echo "REFUSED $event_id/$run_id: launchd install/readback failed" >&2
        critical_alert "$run_dir" "$event_id" "schedule_install_failed" \
          "nightly-schedule-install" \
          "The webcast scheduler install/readback proof failed for this RunPlan. No successful schedule may be inferred from an on-disk job file."
        PROBLEMS=$((PROBLEMS + 1)); continue
      fi
      ;;
    phone)
      if [[ -z "$event_env" ]]; then
        echo "REFUSED $event_id/$run_id: phone RunPlan has no event env" >&2
        critical_alert "$run_dir" "$event_id" "schedule_event_env_missing" \
          "nightly-schedule-event-env" \
          "The phone RunPlan has no pinned event environment. Box dispatch and scheduler installation were refused."
        PROBLEMS=$((PROBLEMS + 1)); continue
      fi
      if ! "$BOX_INSTALL" --event-env "$event_env" --run-dir "$run_dir" \
          ${VERIFY_FLAG[@]+"${VERIFY_FLAG[@]}"}; then
        echo "REFUSED $event_id/$run_id: box dispatch/install/readback failed" >&2
        critical_alert "$run_dir" "$event_id" "schedule_dispatch_failed" \
          "nightly-schedule-dispatch" \
          "The phone RunPlan's box dispatch or scheduler readback proof failed. No successful schedule was recorded."
        PROBLEMS=$((PROBLEMS + 1)); continue
      fi
      ;;
    *)
      echo "REFUSED $event_id/$run_id: unsupported lane '$lane'" >&2
      critical_alert "$run_dir" "$event_id" "schedule_lane_unsupported" \
        "nightly-schedule-lane" \
        "The RunPlan names an unsupported ingress lane. Scheduling was refused before installation."
      PROBLEMS=$((PROBLEMS + 1)); continue ;;
  esac
  if [[ "$STATE" == scheduled ]]; then
    VERIFIED=$((VERIFIED + 1))
  else
    SCHEDULED=$((SCHEDULED + 1))
  fi
done < "$MANIFEST"

# A slate date with no RunPlan candidates is not an all-clear. Either there is
# genuinely nothing to run (which must be represented by an explicit descope)
# or the stage/plan arrow failed. Silent zero-work success recreates the
# 2026-07-28/08-06 failure class this wrapper exists to remove.
if [[ "$TOTAL" -eq 0 ]]; then
  echo "REFUSED: no schema-3 RunPlans exist for $DATE" >&2
  PROBLEMS=$((PROBLEMS + 1))
  UNTRUSTED=$((UNTRUSTED + 1))
fi

echo "schedule-nightly: date=$DATE runs=$TOTAL scheduled=$SCHEDULED verified=$VERIFIED terminal_skip=$SKIPPED problems=$PROBLEMS dry_run=$DRY_RUN"
if [[ "$PROBLEMS" -ne 0 ]]; then
  if [[ "$UNTRUSTED" -ne 0 ]]; then
    legacy_page "TYPED SCHEDULE UNBOUND FAILURES for $DATE" \
      "$UNTRUSTED failure(s) could not be bound to a trustworthy RunPlan. Run-bound failures, if any, have their own durable critical AlertRequests."
  fi
  exit 1
fi
