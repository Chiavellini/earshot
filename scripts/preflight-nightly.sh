#!/usr/bin/env bash
# T-1 NIGHTLY preflight over every immutable schema-3 RunPlan for one date.
#
#   scripts/preflight-nightly.sh [--date YYYY-MM-DD] [--no-mail]
#
# WHY THIS EXISTS: scripts/preflight-event.sh is REQUIRED the night before
# every scheduled event (docs/RUNBOOK.md; born from the 2026-07-15 audit) —
# but today it runs only if a human remembers to run it. On 2026-07-28 the
# PYPL and BA events were missed SILENTLY with no human at the keyboard: the
# gap was not one broken check, it was that nothing was scheduled to check.
# This script is the scheduled rememberer: every night it walks every staged
# schema-3 RunPlan whose starts_at_utc is today (UTC), proves its required
# preflight inputs exist, and raises one run-bound critical alert for every
# verified plan that fails. The aggregate remains the fallback for
# corrupt/unverifiable plans and date-scope failures.
#
# WHAT IT DOES:
#   1. Enumerates var/runs/*/*/control/run-plan.json by immutable UTC start
#      date. Zero matching plans fails; an orphan env is not an event.
#   2. Verifies each plan/ledger and skips only typed terminal runs. An active
#      run with no pinned env or a webcast run with no AccessPlan is a per-run
#      FAIL; a broken run never prevents its siblings being checked.
#   3. Sources each pinned env in a SUBSHELL only, so values cannot leak into
#      another run, then runs preflight-event.sh with the plan's config,
#      exact --run-dir, account env, event env, and webcast AccessPlan.
#      It ALWAYS passes the env file itself as
#      --event-env (preflight treats it as optional context: the dial-in
#      go/no-go gate when it names a number, AND the LABEL for the Linux
#      systemd-timer check). --fire-hhmm is passed when the env defines
#      FIRE_HHMM; when it does not, preflight-event.sh silently skips its
#      fire-mechanism (timer/wake) check, so THIS script counts a WARN and
#      says so — the all-clear mail must never hide that the timer check did
#      not run. --result-json is FEATURE-DETECTED (grep on the script): it
#      does not exist yet (checked 2026-07-30); when it lands, this passes
#      --result-json state/<event>/preflight-result.json with no edit here.
#   4. A crash or hang of one preflight must not cost the others: each run is
#      exit-code-isolated and capped at 45 min (cargo --release build + live
#      probes fit well inside that; a wedged build must not eat the night).
#      On timeout the preflight's WHOLE process group is killed — a bare
#      `kill $pid` would orphan cargo's children and the hang would survive.
#   5. For each failed plan whose identity and ledger verified, records and
#      delivers a proof-bearing critical AlertRequest via alertctl.py. The
#      request is deterministic; alert failure never replaces or suppresses
#      the originating preflight failure. --no-mail neither delivers nor
#      mutates a run ledger with alert facts.
#   6. Aggregates ONE summary mail via scripts/notify.sh:
#        <N> PASS, <M> FAIL(<run>: <first FAIL line>), <W> WARN, <K> TERMINAL-SKIP
#      Every selected run lands in exactly one PASS/FAIL/TERMINAL-SKIP bucket;
#      WARN is an extra count. Machine-readable lines
#      "PREFLIGHT-NIGHTLY: <event>/<run> PASS|FAIL|SKIP" print per run, plus
#      "PREFLIGHT-NIGHTLY: <event>/<run>
#      WARN <reason>" when a check was skipped.
#      (Each preflight-event.sh run ALSO mails its own full checklist — the
#      aggregate is the index, the per-event mails are the detail.)
#   7. Exits 1 if any preflight FAILED, no RunPlan exists for the date, or the
#      summary mail could not be delivered — on ANY night, all-clear included (an alert chain that
#      cannot alert is itself a failed preflight; a silent all-clear is
#      indistinguishable from the 2026-07-28 miss).
#
# SCHEDULE (intended, NOT installed here): box systemd user timer, daily
# 01:00Z — after slate-daily has staged the day's envs, hours before the
# earliest US-morning fire. This script deliberately INSTALLS NOTHING;
# arming the timer is a separate, deliberate act (same posture as
# scripts/install-event-timer.sh). Runs unchanged on the CDMX laptop for
# manual T-1 sweeps.
set -euo pipefail

REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ALERTCTL="${EARSHOT_ALERTCTL:-$REPO/scripts/alertctl.py}"

# If this script itself dies mid-sweep, that must not be silent — the whole
# point is that silence is indistinguishable from a healthy idle box. The
# trap is installed BEFORE the first failable command (cd/mktemp) so even
# the earliest death is tagged and mailed.
DATE=""
NO_MAIL=0
TMPD=""
finished=0
on_exit() {
  [[ -n "$TMPD" ]] && rm -rf "$TMPD"
  [[ "$finished" == 1 ]] && return
  echo "PREFLIGHT-NIGHTLY-CRASH: script died before the summary (see output above)" >&2
  [[ "$NO_MAIL" == 1 ]] || bash "$REPO/scripts/notify.sh" \
    "nightly preflight ${DATE:-(date unset)}: SCRIPT CRASHED" \
    "scripts/preflight-nightly.sh died before producing its summary at $(date -u '+%Y-%m-%d %H:%M:%SZ'). The T-1 sweep is INCOMPLETE — run it by hand." || true
}
trap on_exit EXIT

cd "$REPO"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --date)
      if [[ $# -lt 2 ]]; then
        echo "PREFLIGHT-NIGHTLY-USAGE: --date needs a value (YYYY-MM-DD)" >&2
        echo "usage: $0 [--date YYYY-MM-DD] [--no-mail]" >&2
        exit 2
      fi
      DATE="$2"; shift 2 ;;
    --no-mail) NO_MAIL=1; shift ;;  # dev only: print the summary, skip notify.sh
    *)
      echo "PREFLIGHT-NIGHTLY-USAGE: unknown arg '$1'" >&2
      echo "usage: $0 [--date YYYY-MM-DD] [--no-mail]" >&2
      exit 2 ;;
  esac
done

# Today UTC by default. `date -u +%F`-style is portable; date -d is NOT
# (GNU-only — macOS date has no -d), so the override is validated in python3.
if [[ -z "$DATE" ]]; then
  DATE=$(date -u +%Y-%m-%d)
elif ! python3 -c "import datetime,sys; datetime.datetime.strptime(sys.argv[1], '%Y-%m-%d')" "$DATE" 2>/dev/null; then
  echo "PREFLIGHT-NIGHTLY-USAGE: --date must be YYYY-MM-DD (got '$DATE')" >&2
  exit 2
fi

# Feature-detect --result-json so older checked-out adapters fail for their own
# evidence gaps instead of receiving an argument they do not understand.
HAS_RESULT_JSON=0
grep -q 'result-json' "$REPO/scripts/preflight-event.sh" && HAS_RESULT_JSON=1

# Run "$@" as the LEADER of a fresh process group (invoke with &, then the
# job's pid == the new pgid). setsid exists on the Linux box; macOS ships no
# setsid, so perl setpgrp+exec is the portable equivalent (pid preserved by
# exec in both branches — the background subshell is never a group leader in
# a non-interactive shell, so util-linux setsid does not fork).
in_own_pgroup() {
  if command -v setsid >/dev/null 2>&1; then exec setsid "$@"; fi
  exec perl -e 'setpgrp(0,0); exec @ARGV or die "exec failed: $!"' -- "$@"
}

# GNU timeout exists on the box; the CDMX laptop has neither timeout nor
# gtimeout — same fallback shape as preflight-event.sh's run_with_timeout,
# hardened: the child runs in its OWN process group and a timeout kills the
# whole group (a bare `kill $pid` orphans cargo's children and the hang
# survives). Returns 124 ONLY when the watchdog actually fired (flag file);
# a child that dies on its own signal keeps its 128+n status so the caller
# can tell "timed out" from "killed from outside".
run_with_timeout() {  # <secs> <cmd…>
  if command -v timeout >/dev/null 2>&1; then timeout "$@"; return $?; fi
  if command -v gtimeout >/dev/null 2>&1; then gtimeout "$@"; return $?; fi
  local secs="$1"; shift
  local fired="$TMPD/watchdog-fired.$$.$RANDOM"
  in_own_pgroup "$@" & local pid=$!
  # Watchdog in its OWN group too (so killing it kills its sleeper), and it
  # POLLS the child instead of taking one long sleep: if the group-kill below
  # races the watchdog's setpgrp and misses, the watchdog still self-exits
  # ≤2s after the child dies — never a 45-min orphan whose deadline kill
  # could land on a recycled pgid.
  in_own_pgroup bash -c '
    end=$(( $(date +%s) + $1 ))
    while kill -0 "$3" 2>/dev/null; do
      if [ "$(date +%s)" -ge "$end" ]; then
        touch "$2"
        kill -TERM -- "-$3" 2>/dev/null
        sleep 5
        kill -9 -- "-$3" 2>/dev/null
        exit 0
      fi
      sleep 2
    done
  ' watchdog "$secs" "$fired" "$pid" & local watcher=$!
  local rc=0; wait "$pid" || rc=$?
  kill -9 -- "-$watcher" 2>/dev/null || kill -9 "$watcher" 2>/dev/null || true
  wait "$watcher" 2>/dev/null || true  # reap; if the kill raced setpgrp, self-exit is ≤2s
  if [[ -e "$fired" ]]; then
    rm -f "$fired"
    kill -9 -- "-$pid" 2>/dev/null || true  # sweep TERM-ignoring survivors
    return 124
  fi
  return $rc
}

TMPD=$(mktemp -d "${TMPDIR:-/tmp}/preflight-nightly.XXXXXX")
BODY_FILE="$TMPD/body.txt"
: > "$BODY_FILE"

n_pass=0; n_fail=0; n_skip=0; n_warn=0
fail_detail=""   # "EVENT/RUN: [FAIL] …" for the one-line summary
pass_names=""; fail_names=""; skip_names=""; warn_names=""
n_runs=0

raise_run_alert() { # <verified-run-dir> <event/run>
  local verified_run_dir="$1" failed_run="$2" alert_out="" alert_last=""
  # --no-mail is the offline notification posture. Do not append a durable
  # requirement while deliberately suppressing every delivery attempt.
  [[ "$NO_MAIL" == 0 ]] || return 0
  if ! alert_out=$(python3 "$ALERTCTL" raise \
      --run-dir "$verified_run_dir" \
      --severity critical \
      --code preflight_failed \
      --dedupe-key nightly_preflight \
      --subject "preflight failed: $failed_run" \
      --body "Nightly preflight did not pass for $failed_run on UTC date $DATE. Inspect the run ledger and rerun scripts/opsctl.sh preflight --date $DATE." \
      2>&1); then
    alert_last=$(tail -n 1 <<<"$alert_out")
    echo "PREFLIGHT-NIGHTLY-RUN-ALERT-FAILED: $failed_run — ${alert_last:-alertctl exited without detail}; original preflight failure preserved" >&2
    return 0
  fi
  alert_last=$(tail -n 1 <<<"$alert_out")
  echo "PREFLIGHT-NIGHTLY-RUN-ALERT: $failed_run — ${alert_last:-proof-bearing alert recorded}"
}

mark_fail() { # <event/run> <message> [verified-run-dir]
  local failed_run="$1" failed_message="$2" verified_run_dir="${3:-}"
  n_fail=$((n_fail+1)); fail_names="$fail_names $failed_run"
  [[ -n "$fail_detail" ]] && fail_detail="$fail_detail; "
  fail_detail="${fail_detail}${failed_run}: ${failed_message}"
  echo "PREFLIGHT-NIGHTLY: $failed_run FAIL"
  echo "FAIL  $failed_run — $failed_message" >> "$BODY_FILE"
  [[ -z "$verified_run_dir" ]] || raise_run_alert "$verified_run_dir" "$failed_run"
}

# RunPlan is the event-set authority. Beginning from env filenames made an
# active plan with a missing env invisible. The unit separator preserves an
# empty event_env_path (unlike tab, which bash treats as collapsible IFS
# whitespace).
MANIFEST="$TMPD/runplans.usv"
if ! python3 - "$REPO" "$DATE" > "$MANIFEST" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

repo, wanted = Path(sys.argv[1]), dt.date.fromisoformat(sys.argv[2])
for path in sorted((repo / "var" / "runs").glob("*/*/control/run-plan.json")):
    try:
        plan = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid RunPlan candidate {path}: {exc}")
    if plan.get("schema") != 3 or plan.get("kind") != "run-plan":
        continue
    try:
        starts = dt.datetime.fromisoformat(
            str(plan["starts_at_utc"]).replace("Z", "+00:00")
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise SystemExit(f"invalid schema-3 RunPlan candidate {path}: {exc}")
    if starts.tzinfo is None:
        raise SystemExit(
            f"invalid RunPlan candidate {path}: starts_at_utc is timezone-naive"
        )
    if starts.astimezone(dt.timezone.utc).date() != wanted:
        continue
    run_dir = path.parent.parent
    fields = [
        str(run_dir), str(plan.get("event_id", run_dir.parent.name)),
        str(plan.get("run_id", run_dir.name)), str(plan.get("lane", "")),
        str(plan.get("config_path", "")),
        str(plan.get("event_env_path") or ""),
        str(plan.get("plan_sha256", "")),
    ]
    if any("\n" in value or "\x1f" in value for value in fields):
        raise SystemExit(f"unsafe RunPlan field in {path}")
    print("\x1f".join(fields))
PY
then
  echo "PREFLIGHT-NIGHTLY-MANIFEST-FAILED: could not enumerate RunPlans for $DATE" >&2
  exit 1
fi

while IFS=$'\x1f' read -r run_dir event_id run_id lane plan_cfg env_rel plan_sha; do
  [[ -n "$run_dir" ]] || continue
  n_runs=$((n_runs+1))
  run_name="$event_id/$run_id"

  # Refuse mutated plans/envs and torn ledgers before trusting any field. A
  # corrupt sibling is reported and does not prevent later siblings running.
  verify_out=""
  if ! verify_out=$(EARSHOT_REPO="$REPO" python3 "$REPO/scripts/eventctl.py" verify \
      --run-dir "$run_dir" 2>&1); then
    verify_detail=$(tail -n 1 <<< "$verify_out")
    mark_fail "$run_name" \
      "RunPlan/ledger verification failed: ${verify_detail:-$run_dir}"
    continue
  fi
  status=""
  if ! status=$(EARSHOT_REPO="$REPO" python3 "$REPO/scripts/eventctl.py" status \
      --run-dir "$run_dir" --json 2>/dev/null); then
    mark_fail "$run_name" "typed status failed for verified RunPlan $run_dir" "$run_dir"
    continue
  fi
  active=$(python3 -c 'import json,sys; print("1" if json.load(sys.stdin)["state"]["active"] else "0")' <<< "$status" 2>/dev/null || true)
  if [[ "$active" == 0 ]]; then
    n_skip=$((n_skip+1)); skip_names="$skip_names $run_name"
    echo "PREFLIGHT-NIGHTLY: $run_name SKIP terminal"
    echo "SKIP  $run_name — typed run is terminal; no preflight may advance it" >> "$BODY_FILE"
    continue
  elif [[ "$active" != 1 ]]; then
    mark_fail "$run_name" "typed status payload was unreadable" "$run_dir"
    continue
  fi

  # This is the failure the old env-driven enumeration could not observe.
  if [[ -z "$env_rel" ]]; then
    mark_fail "$run_name" \
      "active RunPlan has no event_env_path; stage must publish and pin the event env" \
      "$run_dir"
    continue
  fi
  envf="$REPO/$env_rel"
  if [[ ! -f "$envf" ]]; then
    mark_fail "$run_name" "pinned event env $env_rel is missing" "$run_dir"
    continue
  fi

  # Webcast acquisition is a parallel prerequisite produced by
  # control.acquire. Preflight consumes it; this node never creates or repairs
  # it. The full preflight validates schema/hash after this visibility check.
  access_plan=""
  if [[ "$lane" == webcast ]]; then
    access_plan="$run_dir/control/access-plan.json"
    if [[ ! -f "$access_plan" ]]; then
      mark_fail "$run_name" \
        "required webcast AccessPlan missing at $access_plan; run control.acquire, then retry preflight" \
        "$run_dir"
      continue
    fi
  elif [[ "$lane" != phone ]]; then
    mark_fail "$run_name" "unsupported RunPlan lane '$lane'" "$run_dir"
    continue
  fi

  # Source in a SUBSHELL and extract only the keys this sweep needs
  # (event-autorun.sh's contract: EVENT_NAME/CONFIG_PATH/ACCOUNT_ENV/
  # EVENT_DATE, plus FIRE_HHMM once slate-stage.sh writes it).
  # \x1f-delimited so values with spaces survive; nothing the env sets
  # survives past the substitution.
  if ! rec=$(
    set -eu
    # shellcheck disable=SC1090
    source "$envf" >/dev/null 2>&1
    printf '%s\x1f%s\x1f%s\x1f%s\x1f%s' \
      "${EVENT_NAME:-}" "${CONFIG_PATH:-}" "${ACCOUNT_ENV:-}" \
      "${EVENT_DATE:-}" "${FIRE_HHMM:-}"
  ); then
    mark_fail "$run_name" "pinned event env could not be sourced: $env_rel" "$run_dir"
    continue
  fi
  ev_name=""; env_cfg=""; ev_acct=""; ev_date=""; ev_fire=""
  IFS=$'\x1f' read -r ev_name env_cfg ev_acct ev_date ev_fire <<<"$rec" || true

  # Pinned env metadata is evidence, never a second event selector.
  if [[ -z "$env_cfg" || -z "$ev_date" ]]; then
    mark_fail "$run_name" \
      "pinned env incomplete (CONFIG_PATH='${env_cfg:-}' EVENT_DATE='${ev_date:-}') in $env_rel" \
      "$run_dir"
    continue
  fi
  if [[ "$env_cfg" != "$plan_cfg" ]]; then
    mark_fail "$run_name" \
      "pinned env CONFIG_PATH=$env_cfg contradicts RunPlan config_path=$plan_cfg" \
      "$run_dir"
    continue
  fi
  if [[ "$ev_date" != "$DATE" ]]; then
    mark_fail "$run_name" \
      "pinned env EVENT_DATE=$ev_date contradicts RunPlan UTC date $DATE" \
      "$run_dir"
    continue
  fi
  # Today's event but no account is a preparation failure, not an ignorable
  # non-date skip. Calling this SKIP let a date-level facade return success and
  # continue toward scheduling with an event it could never authenticate.
  if [[ -z "$ev_acct" ]]; then
    mark_fail "$run_name" \
      "pinned env has no ACCOUNT_ENV in $env_rel; cannot preflight or schedule" \
      "$run_dir"
    continue
  fi

  # ── Build the preflight invocation ───────────────────────────────────────
  args=(--config "$plan_cfg" --account "$ev_acct" --run-dir "$run_dir")
  # ALWAYS pass the env file — preflight-event.sh treats it as optional
  # context: the dial-in go/no-go gate when it names a number (a TBD number
  # FAIL-STOPs tonight, not at 06:05 — the DPZ 2026-07-20 promise, kept at
  # T-1) AND the LABEL its Linux systemd-timer check needs. Conditioning on
  # TO_NUMBER/PASSCODE would lose the LABEL for webcast-lane envs.
  args+=(--event-env "$envf")
  [[ -n "$access_plan" ]] && args+=(--access-plan "$access_plan")
  if [[ -n "$ev_fire" ]]; then
    args+=(--fire-hhmm "$ev_fire")
  else
    # Without --fire-hhmm, preflight-event.sh SILENTLY skips its whole
    # fire-mechanism (pmset wake / systemd timer) check — nothing verifies
    # that anything will actually fire this event. That gap must be visible
    # in the aggregate, WARN-counted, never hidden by an all-clear subject.
    n_warn=$((n_warn+1)); warn_names="$warn_names $run_name"
    echo "PREFLIGHT-NIGHTLY: $run_name WARN fire-mechanism check SKIPPED (no FIRE_HHMM in env)"
    echo "WARN  $run_name — fire-mechanism check SKIPPED (no FIRE_HHMM in $envf); nothing verified a timer/wake will fire this event" >> "$BODY_FILE"
  fi
  if [[ "$HAS_RESULT_JSON" == 1 ]]; then
    mkdir -p "$REPO/state/$event_id"
    args+=(--result-json "state/$event_id/preflight-result.json")
  fi

  out="$TMPD/run-$n_runs.out"
  rc=0
  run_with_timeout 2700 bash "$REPO/scripts/preflight-event.sh" "${args[@]}" >"$out" 2>&1 || rc=$?

  if [[ "$rc" -eq 0 ]]; then
    n_pass=$((n_pass+1)); pass_names="$pass_names $run_name"
    echo "PREFLIGHT-NIGHTLY: $run_name PASS"
    echo "PASS  $run_name — $plan_cfg (exit 0)" >> "$BODY_FILE"
  else
    if [[ "$rc" -eq 124 ]]; then
      first_fail="preflight TIMED OUT (>45 min) — wedged build or hung probe"
    elif [[ "$rc" -ge 128 ]]; then
      first_fail="preflight died on signal $((rc-128)) (exit $rc) — killed from outside, NOT a timeout"
    else
      first_fail=$(grep -m1 '^\[FAIL\]' "$out" || true)
      first_fail="${first_fail:-exit $rc with no [FAIL] line — preflight itself crashed}"
    fi
    mark_fail "$run_name" "$first_fail" "$run_dir"
    {
      echo "      config: $plan_cfg (exit $rc)"
      echo "      last 30 lines of preflight output:"
      tail -n 30 "$out" | sed 's/^/      | /'
    } >> "$BODY_FILE"
  fi
done < "$MANIFEST"

# ── Aggregate summary — every selected run in exactly one bucket ─────────────
if [[ "$n_runs" -eq 0 ]]; then
  n_fail=$((n_fail+1))
  first_fail="no schema-3 RunPlans exist for $DATE"
  fail_detail="$first_fail"
  fail_names=" date-scope"
  echo "PREFLIGHT-NIGHTLY-EMPTY: $first_fail" >&2
  echo "FAIL  date-scope — $first_fail; staging/RunPlan compilation did not establish an event set" >> "$BODY_FILE"
fi
if [[ -n "$fail_detail" ]]; then
  SUMMARY="$n_pass PASS, $n_fail FAIL($fail_detail), $n_warn WARN, $n_skip TERMINAL-SKIP"
else
  SUMMARY="$n_pass PASS, 0 FAIL, $n_warn WARN, $n_skip TERMINAL-SKIP"
fi
echo "PREFLIGHT-NIGHTLY-SUMMARY: $SUMMARY"
BODY="$SUMMARY

date checked (UTC): $DATE
RunPlans selected: $n_runs   (pass:${pass_names:- none}  fail:${fail_names:- none}  warn:${warn_names:- none}  terminal-skip:${skip_names:- none})

$(cat "$BODY_FILE")

Per-event preflight emails (full checklists) were sent by preflight-event.sh for each PASS/FAIL above.
Run by scripts/preflight-nightly.sh on $(hostname) at $(date -u '+%Y-%m-%d %H:%M:%SZ') — intended schedule: box systemd user timer, daily 01:00Z (not installed by this script)."

if [[ "$n_fail" -gt 0 ]]; then
  SUBJECT="nightly preflight $DATE: $n_fail FAIL ($n_pass pass, $n_warn warn, $n_skip skip)"
elif [[ "$n_warn" -gt 0 ]]; then
  SUBJECT="nightly preflight $DATE: all clear* ($n_pass pass, $n_warn WARN fire-check skipped, $n_skip skip)"
else
  SUBJECT="nightly preflight $DATE: all clear ($n_pass pass, $n_skip skip)"
fi

echo
echo "$BODY"

if [[ "$NO_MAIL" == 1 ]]; then
  echo "PREFLIGHT-NIGHTLY-NOMAIL: --no-mail set — summary printed only, notify.sh skipped"
else
  # notify.sh: <subject> <body> [--quiet]. Quiet on an all-clear night; a
  # T-1 FAIL is allowed to speak on a Darwin host — someone should hear it.
  QUIET_FLAG="--quiet"
  [[ "$n_fail" -gt 0 ]] && QUIET_FLAG=""
  if ! bash "$REPO/scripts/notify.sh" "$SUBJECT" "$BODY" $QUIET_FLAG; then
    echo "PREFLIGHT-NIGHTLY-ALERT-FAILED: every notify channel failed — summary above is the only copy" >&2
    finished=1
    exit 1
  fi
fi

finished=1
[[ "$n_fail" -eq 0 ]] || exit 1
exit 0
