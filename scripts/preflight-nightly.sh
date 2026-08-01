#!/usr/bin/env bash
# T-1 NIGHTLY preflight over every staged autorun env (W2.2).
#
#   scripts/preflight-nightly.sh [--date YYYY-MM-DD] [--no-mail]
#
# WHY THIS EXISTS: scripts/preflight-event.sh is REQUIRED the night before
# every scheduled event (docs/RUNBOOK.md; born from the 2026-07-15 audit) —
# but today it runs only if a human remembers to run it. On 2026-07-28 the
# PYPL and BA events were missed SILENTLY with no human at the keyboard: the
# gap was not one broken check, it was that nothing was scheduled to check.
# This script is the scheduled rememberer: every night it walks every staged
# configs/*-autorun.env, preflights the ones whose EVENT_DATE is today (UTC),
# and mails ONE aggregate verdict — so a T-1 gap is an email the night
# before, not a post-mortem the day after.
#
# WHAT IT DOES:
#   1. For each configs/*-autorun.env: sources it in a SUBSHELL only — the
#      sourcing-corruption trap (unquoted `Del Rio`, `O$Brien`) that
#      preflight-event.sh's registration round-trip guards against must never
#      pollute THIS process's environment or leak between envs.
#   2. Skips any env whose EVENT_DATE != today UTC (--date overrides "today";
#      the intended 01:00Z fire time is early on the event's OWN UTC day, so
#      today-UTC == event day at run time). Envs missing CONFIG_PATH,
#      EVENT_DATE, or ACCOUNT_ENV are counted in the same SKIP bucket,
#      loudly.
#   3. For each match: runs scripts/preflight-event.sh with the env's
#      CONFIG_PATH/ACCOUNT_ENV, ALWAYS passing the env file itself as
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
#   5. Aggregates ONE summary mail via scripts/notify.sh:
#        <N> PASS, <M> FAIL(<event>: <first FAIL line>), <W> WARN, <K> NO-ENV/SKIP
#      Every processed env lands in exactly one PASS/FAIL/SKIP bucket; WARN
#      is an extra count (a PASSing env can still carry the fire-mechanism
#      WARN). Machine-readable lines "PREFLIGHT-NIGHTLY: <event>
#      PASS|FAIL|SKIP" print per env, plus "PREFLIGHT-NIGHTLY: <event>
#      WARN <reason>" when a check was skipped.
#      (Each preflight-event.sh run ALSO mails its own full checklist — the
#      aggregate is the index, the per-event mails are the detail.)
#   6. Exits 1 if any preflight FAILED, or if the summary mail could not be
#      delivered — on ANY night, all-clear included (an alert chain that
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

# Feature-detect --result-json in preflight-event.sh (it does NOT support it
# yet — passing an unknown arg would exit 2 and every env would read as FAIL
# for a reason that is this script's bug, not the event's).
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
fail_detail=""   # "DPZ: [FAIL] …; GEV: [FAIL] …" for the one-line summary
pass_names=""; fail_names=""; skip_names=""; warn_names=""
n_envs=0

shopt -s nullglob
for envf in "$REPO"/configs/*-autorun.env; do
  n_envs=$((n_envs+1))
  stem=$(basename "$envf" | sed 's/-autorun\.env$//')

  # Source in a SUBSHELL and extract only the keys this sweep needs
  # (event-autorun.sh's contract: EVENT_NAME/CONFIG_PATH/ACCOUNT_ENV/
  # EVENT_DATE, plus FIRE_HHMM once slate-stage.sh writes it).
  # \x1f-delimited so values with spaces survive; nothing the env sets
  # survives past the substitution.
  rec=$(
    set +e +u
    # shellcheck disable=SC1090
    source "$envf" >/dev/null 2>&1
    printf '%s\x1f%s\x1f%s\x1f%s\x1f%s' \
      "${EVENT_NAME:-}" "${CONFIG_PATH:-}" "${ACCOUNT_ENV:-}" \
      "${EVENT_DATE:-}" "${FIRE_HHMM:-}"
  ) || true
  ev_name=""; ev_cfg=""; ev_acct=""; ev_date=""; ev_fire=""
  IFS=$'\x1f' read -r ev_name ev_cfg ev_acct ev_date ev_fire <<<"$rec" || true
  name="${ev_name:-$stem}"

  # ── SKIP bucket: not today, or env too broken to know ────────────────────
  if [[ -z "$ev_cfg" || -z "$ev_date" ]]; then
    n_skip=$((n_skip+1)); skip_names="$skip_names $name"
    echo "PREFLIGHT-NIGHTLY: $name SKIP"
    echo "SKIP  $name — env incomplete (CONFIG_PATH='${ev_cfg:-}' EVENT_DATE='${ev_date:-}') in $envf; event-autorun would refuse it too" >> "$BODY_FILE"
    continue
  fi
  if [[ "$ev_date" != "$DATE" ]]; then
    n_skip=$((n_skip+1)); skip_names="$skip_names $name"
    echo "PREFLIGHT-NIGHTLY: $name SKIP"
    echo "SKIP  $name — EVENT_DATE=$ev_date != $DATE ($envf)" >> "$BODY_FILE"
    continue
  fi
  # Today's event but no account: preflight-event.sh would FAIL on the empty
  # --account for a reason that is the ENV's incompleteness, not the event's
  # health — keep the diagnosis honest and bucket it as a loud SKIP.
  if [[ -z "$ev_acct" ]]; then
    n_skip=$((n_skip+1)); skip_names="$skip_names $name"
    echo "PREFLIGHT-NIGHTLY: $name SKIP"
    echo "SKIP  $name — incomplete env (no ACCOUNT_ENV) in $envf; cannot preflight without an account env" >> "$BODY_FILE"
    continue
  fi

  # ── Build the preflight invocation ───────────────────────────────────────
  args=(--config "$ev_cfg" --account "$ev_acct")
  # ALWAYS pass the env file — preflight-event.sh treats it as optional
  # context: the dial-in go/no-go gate when it names a number (a TBD number
  # FAIL-STOPs tonight, not at 06:05 — the DPZ 2026-07-20 promise, kept at
  # T-1) AND the LABEL its Linux systemd-timer check needs. Conditioning on
  # TO_NUMBER/PASSCODE would lose the LABEL for webcast-lane envs.
  args+=(--event-env "$envf")
  if [[ -n "$ev_fire" ]]; then
    args+=(--fire-hhmm "$ev_fire")
  else
    # Without --fire-hhmm, preflight-event.sh SILENTLY skips its whole
    # fire-mechanism (pmset wake / systemd timer) check — nothing verifies
    # that anything will actually fire this event. That gap must be visible
    # in the aggregate, WARN-counted, never hidden by an all-clear subject.
    n_warn=$((n_warn+1)); warn_names="$warn_names $name"
    echo "PREFLIGHT-NIGHTLY: $name WARN fire-mechanism check SKIPPED (no FIRE_HHMM in env)"
    echo "WARN  $name — fire-mechanism check SKIPPED (no FIRE_HHMM in $envf); nothing verified a timer/wake will fire this event" >> "$BODY_FILE"
  fi
  if [[ "$HAS_RESULT_JSON" == 1 ]]; then
    # `|| true`: a staged env can name a config that does not exist yet —
    # preflight-event.sh will FAIL that loudly itself; the sweep must survive.
    eid=$(sed -n '/^\[event\]/,/^\[/s/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$ev_cfg" 2>/dev/null | head -1 || true)
    eid="${eid:-$stem}"
    mkdir -p "$REPO/state/$eid"
    args+=(--result-json "state/$eid/preflight-result.json")
  fi

  out="$TMPD/$stem.out"
  rc=0
  run_with_timeout 2700 bash "$REPO/scripts/preflight-event.sh" "${args[@]}" >"$out" 2>&1 || rc=$?

  if [[ "$rc" -eq 0 ]]; then
    n_pass=$((n_pass+1)); pass_names="$pass_names $name"
    echo "PREFLIGHT-NIGHTLY: $name PASS"
    echo "PASS  $name — $ev_cfg (exit 0)" >> "$BODY_FILE"
  else
    n_fail=$((n_fail+1)); fail_names="$fail_names $name"
    echo "PREFLIGHT-NIGHTLY: $name FAIL"
    if [[ "$rc" -eq 124 ]]; then
      first_fail="preflight TIMED OUT (>45 min) — wedged build or hung probe"
    elif [[ "$rc" -ge 128 ]]; then
      first_fail="preflight died on signal $((rc-128)) (exit $rc) — killed from outside, NOT a timeout"
    else
      first_fail=$(grep -m1 '^\[FAIL\]' "$out" || true)
      first_fail="${first_fail:-exit $rc with no [FAIL] line — preflight itself crashed}"
    fi
    [[ -n "$fail_detail" ]] && fail_detail="$fail_detail; "
    fail_detail="${fail_detail}${name}: ${first_fail}"
    {
      echo "FAIL  $name — $ev_cfg (exit $rc)"
      echo "      first FAIL: $first_fail"
      echo "      last 30 lines of preflight output:"
      tail -n 30 "$out" | sed 's/^/      | /'
    } >> "$BODY_FILE"
  fi
done
shopt -u nullglob

# ── Aggregate summary — every env in exactly one bucket ────────────────────
if [[ -n "$fail_detail" ]]; then
  SUMMARY="$n_pass PASS, $n_fail FAIL($fail_detail), $n_warn WARN, $n_skip NO-ENV/SKIP"
else
  SUMMARY="$n_pass PASS, 0 FAIL, $n_warn WARN, $n_skip NO-ENV/SKIP"
fi
echo "PREFLIGHT-NIGHTLY-SUMMARY: $SUMMARY"
if [[ "$n_envs" -eq 0 ]]; then
  # Zero staged envs is information, not silence: either nothing is
  # scheduled (fine) or slate-daily never staged today's events (the
  # 2026-07-28 failure shape). The human decides which; we make sure they see it.
  echo "PREFLIGHT-NIGHTLY-EMPTY: no configs/*-autorun.env staged at all"
  echo "(no configs/*-autorun.env files exist — nothing staged; if events are expected for $DATE, slate/staging never ran)" >> "$BODY_FILE"
fi

BODY="$SUMMARY

date checked (UTC): $DATE
envs scanned: $n_envs   (pass:${pass_names:- none}  fail:${fail_names:- none}  warn:${warn_names:- none}  skip:${skip_names:- none})

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
