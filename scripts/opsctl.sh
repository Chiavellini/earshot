#!/usr/bin/env bash
# The public event-operations facade. Run `opsctl.sh surface` to see the only
# supported operator commands; everything else is a node-owned internal adapter
# or historical evidence.
set -euo pipefail

REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
cd "$REPO"

usage() {
  cat <<'EOF'
usage:
  scripts/opsctl.sh surface
  scripts/opsctl.sh prepare-day --date YYYY-MM-DD [--dry-run] [--no-mail]
  scripts/opsctl.sh stage --date YYYY-MM-DD
  scripts/opsctl.sh access --date YYYY-MM-DD
  scripts/opsctl.sh bind-access --run-dir PATH --access-mode MODE --resolver-kind KIND [resolver/session options]
  scripts/opsctl.sh study --date YYYY-MM-DD [--dry-run]
  scripts/opsctl.sh preflight --date YYYY-MM-DD [--no-mail]
  scripts/opsctl.sh schedule --date YYYY-MM-DD [--dry-run]
  scripts/opsctl.sh status <EVENT_ID | YYYY-MM-DD | today>
  scripts/opsctl.sh pull (--date YYYY-MM-DD | --event EVENT_ID) [--dry-run]
  scripts/opsctl.sh audit --run-dir var/runs/EVENT_ID/RUN_ID
  scripts/opsctl.sh audit-day --date YYYY-MM-DD
  scripts/opsctl.sh fills --date YYYY-MM-DD
  scripts/opsctl.sh alerts --run-dir var/runs/EVENT_ID/RUN_ID
  scripts/opsctl.sh ack-alert --run-dir PATH --alert-id 64HEX --by ID [--note text]
  scripts/opsctl.sh descope EVENT_TICKER "reason" [--fallbacks "..."]

prepare-day means exactly: stage -> access -> study -> preflight -> schedule.
Each stage continues across sibling events, and the command returns nonzero if
any stage reports a problem. It accepts one UTC date and no event-specific
switches. bind-access is the explicit, RunPlan-bound declaration of public or
pre-established session access; it never registers, signs in, or stores a
credential.
EOF
}

refuse() { echo "REFUSED: $*" >&2; exit 2; }

valid_date() {
  [[ "$1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || return 1
  python3 - "$1" <<'PY' >/dev/null 2>&1
import datetime, sys
datetime.date.fromisoformat(sys.argv[1])
PY
}

date_args() {
  local date="" dry=0 no_mail=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --date) [[ $# -ge 2 ]] || refuse "--date needs YYYY-MM-DD"; date="$2"; shift 2 ;;
      --dry-run) dry=1; shift ;;
      --no-mail) no_mail=1; shift ;;
      *) refuse "unknown argument: $1" ;;
    esac
  done
  [[ -n "$date" ]] || refuse "--date YYYY-MM-DD is required"
  valid_date "$date" || refuse "invalid UTC date: $date"
  OPS_DATE="$date" OPS_DRY="$dry" OPS_NO_MAIL="$no_mail"
}

command="${1:-}"
[[ -n "$command" ]] || { usage >&2; exit 2; }
shift

case "$command" in
  surface)
    [[ $# -eq 0 ]] || refuse "surface takes no arguments"
    exec python3 scripts/ops-surface-check.py --list
    ;;
  prepare-day)
    date_args "$@"
    if [[ "$OPS_DRY" == 1 ]]; then
      # stage-nightly has no honest dry-run; never mutate a real slate merely
      # to preview the chain.
      echo "DRY-RUN prepare-day $OPS_DATE"
      echo "  scripts/stage-nightly.sh --date $OPS_DATE"
      echo "  python3 scripts/access-plan.py check-day --date $OPS_DATE"
      echo "  scripts/study-nightly.sh --date $OPS_DATE --dry-run"
      echo "  scripts/preflight-nightly.sh --date $OPS_DATE --no-mail"
      echo "  scripts/schedule-nightly.sh --date $OPS_DATE --dry-run"
      exit 0
    fi
    stage_rc=0 access_rc=0 study_rc=0 preflight_rc=0 schedule_rc=0
    scripts/stage-nightly.sh --date "$OPS_DATE" || stage_rc=$?
    python3 scripts/access-plan.py check-day --date "$OPS_DATE" || access_rc=$?
    scripts/study-nightly.sh --date "$OPS_DATE" || study_rc=$?
    preflight_args=(--date "$OPS_DATE")
    [[ "$OPS_NO_MAIL" == 1 ]] && preflight_args+=(--no-mail)
    scripts/preflight-nightly.sh "${preflight_args[@]}" || preflight_rc=$?
    scripts/schedule-nightly.sh --date "$OPS_DATE" || schedule_rc=$?
    echo "prepare-day: date=$OPS_DATE stage=$stage_rc access=$access_rc study=$study_rc preflight=$preflight_rc schedule=$schedule_rc"
    if (( stage_rc || access_rc || study_rc || preflight_rc || schedule_rc )); then
      echo "prepare-day: PROBLEMS — ready siblings were processed; failed runs were not marked scheduled" >&2
      exit 1
    fi
    ;;
  stage)
    date_args "$@"; [[ "$OPS_DRY" == 0 && "$OPS_NO_MAIL" == 0 ]] || refuse "stage accepts only --date"
    exec scripts/stage-nightly.sh --date "$OPS_DATE"
    ;;
  access)
    date_args "$@"; [[ "$OPS_DRY" == 0 && "$OPS_NO_MAIL" == 0 ]] || refuse "access accepts only --date"
    exec python3 scripts/access-plan.py check-day --date "$OPS_DATE"
    ;;
  bind-access)
    [[ $# -gt 0 ]] || refuse "bind-access needs one RunPlan and explicit access/resolver options"
    exec python3 scripts/access-plan.py bind "$@"
    ;;
  study)
    date_args "$@"; [[ "$OPS_NO_MAIL" == 0 ]] || refuse "study does not accept --no-mail"
    args=(--date "$OPS_DATE"); [[ "$OPS_DRY" == 1 ]] && args+=(--dry-run)
    exec scripts/study-nightly.sh "${args[@]}"
    ;;
  preflight)
    date_args "$@"; [[ "$OPS_DRY" == 0 ]] || refuse "preflight uses --no-mail, not --dry-run"
    args=(--date "$OPS_DATE"); [[ "$OPS_NO_MAIL" == 1 ]] && args+=(--no-mail)
    exec scripts/preflight-nightly.sh "${args[@]}"
    ;;
  schedule)
    date_args "$@"; [[ "$OPS_NO_MAIL" == 0 ]] || refuse "schedule does not accept --no-mail"
    args=(--date "$OPS_DATE"); [[ "$OPS_DRY" == 1 ]] && args+=(--dry-run)
    exec scripts/schedule-nightly.sh "${args[@]}"
    ;;
  status)
    [[ $# -eq 1 ]] || refuse "status needs EVENT_ID, YYYY-MM-DD, or today"
    exec scripts/event-status.sh "$1"
    ;;
  pull)
    [[ $# -gt 0 ]] || refuse "pull needs --date or --event"
    exec scripts/pull-box-state.sh "$@"
    ;;
  audit)
    [[ "${1:-}" == "--run-dir" && $# -eq 2 ]] || refuse "audit needs --run-dir PATH"
    exec python3 scripts/run-audit.py --run-dir "$2"
    ;;
  audit-day)
    date_args "$@"; [[ "$OPS_DRY" == 0 && "$OPS_NO_MAIL" == 0 ]] || refuse "audit-day accepts only --date"
    exec python3 scripts/audit-day.py --date "$OPS_DATE"
    ;;
  fills)
    # E15: the account's side of the ledger. Read-only, signed GETs only.
    date_args "$@"; [[ "$OPS_DRY" == 0 && "$OPS_NO_MAIL" == 0 ]] || refuse "fills accepts only --date"
    exec python3 scripts/fill-reconcile.py --date "$OPS_DATE" \
      --json "var/audits/fills-$OPS_DATE.json"
    ;;
  alerts)
    [[ "${1:-}" == "--run-dir" && $# -eq 2 ]] || refuse "alerts needs --run-dir PATH"
    exec python3 scripts/alertctl.py status --run-dir "$2"
    ;;
  ack-alert)
    exec python3 scripts/alertctl.py ack "$@"
    ;;
  descope)
    [[ $# -ge 2 ]] || refuse "descope needs EVENT_TICKER and reason"
    exec scripts/descope-event.sh "$@"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    refuse "unknown operation: $command"
    ;;
esac
