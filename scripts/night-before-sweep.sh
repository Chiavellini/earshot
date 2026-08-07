#!/usr/bin/env bash
# night-before-sweep.sh — ONE read-only answer to "is the slate actually going
# to fire tomorrow, or does it just look like it will?"
#
#   scripts/night-before-sweep.sh 2026-08-06
#
# WHY: on 2026-08-05 every job for the next day was loaded, listed by
# `launchctl list`, and dead — each one's StartCalendarInterval was a time that
# had already passed, so runs=0 forever and not one would have fired. Nothing in
# the chain compared the SCHEDULED INSTANT to now. `launchctl list` showing a
# label is not evidence; only the loaded descriptor being in the FUTURE is.
#
# Checks, per webcast config for the date:
#   1. green gate (assert-ready.sh: preflight verdict + readiness hash/freshness)
#   2. the LOADED launchd descriptor (not the plist file) is in the future
#   3. the loaded argv carries the intended posture flags
#   4. the pinned binary exists
#   5. no STALE $HOME/<slug>-stream.url that would make the watcher arm a corpse
# Plus host-level: sleep assertion, disk, Kalshi reachability, mailer config.
#
# Exit 0 = every check passed. Exit 1 = at least one problem. Read-only always.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"
DATE="${1:?usage: night-before-sweep.sh YYYY-MM-DD}"
UID_N=$(id -u)
DP=$(python3 -c "
from datetime import date
d=date.fromisoformat('$DATE'); print(f\"{d.strftime('%y')}{d.strftime('%b').lower()}{d.strftime('%d')}\")")
NOW=$(date +%s)
PROBLEMS=0
say() { printf '%-8s %s\n' "$1" "$2"; [[ "$1" == "FAIL" ]] && PROBLEMS=$((PROBLEMS+1)); return 0; }

echo "═══ night-before sweep for $DATE ($DP) — $(date '+%F %T %Z')"
for cfg in configs/webcast-*-"$DP"-arm-wc.toml; do
  [[ -f "$cfg" ]] || continue
  slug=$(basename "$cfg" .toml); slug="${slug#webcast-}"; slug="${slug%-$DP-arm-wc}"
  echo "── $slug"

  if out=$(scripts/assert-ready.sh "$cfg" 2>&1); then say PASS "green gate: ${out#PASS: }"
  else say FAIL "green gate: $out"; fi

  unit="gui/$UID_N/com.earshot.${slug}-${DP}-watch"
  info=$(launchctl print "$unit" 2>/dev/null)
  if [[ -z "$info" ]]; then
    say FAIL "watch unit NOT LOADED"
  else
    desc=$(sed -n '/descriptor = {/,/}/p' <<<"$info")
    mo=$(sed -n 's/.*"Month" => \([0-9]*\).*/\1/p' <<<"$desc"); dy=$(sed -n 's/.*"Day" => \([0-9]*\).*/\1/p' <<<"$desc")
    hh=$(sed -n 's/.*"Hour" => \([0-9]*\).*/\1/p' <<<"$desc"); mi=$(sed -n 's/.*"Minute" => \([0-9]*\).*/\1/p' <<<"$desc")
    if [[ -n "$mo$dy$hh$mi" ]]; then
      yr=$(date +%Y)
      fire=$(python3 -c "
import datetime,calendar,time
t=datetime.datetime($yr,$mo,$dy,$hh,$mi)
print(int(time.mktime(t.timetuple())))" 2>/dev/null)
      nruns=$(sed -n 's/.*runs = \([0-9]*\).*/\1/p' <<<"$info" | head -1)
      when=$(printf '%02d-%02d %02d:%02d' "$mo" "$dy" "$hh" "$mi")
      if [[ -n "$fire" && "$fire" -gt "$NOW" ]]; then
        say PASS "fires $when local — in $(( (fire-NOW)/60 )) min"
      elif [[ "${nruns:-0}" -gt 0 ]]; then
        # Already fired. Distinguishing this from "never fired" is the whole
        # point — both have a past calendar time, but only runs=0 is a miss.
        say PASS "fired $when local, runs=$nruns — ALREADY RAN (this sweep is a night-before tool; past+runs>0 is success, not failure)"
      else
        say FAIL "fires $when local — IN THE PAST and runs=0, this job will NEVER run"
      fi
    else
      say FAIL "watch unit has NO calendar descriptor"
    fi
    posture=$(grep -o '\-\-i-am-live\( --fillable\)\?' <<<"$info" | head -1)
    say PASS "posture flags: ${posture:-none (dry-run)}"
  fi

  ev=$(sed -n '/^\[event\]/,/^\[/s/^[[:space:]]*id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$cfg" | head -1)
  [[ -x "state/$ev/earshot-pinned" ]] && say PASS "pinned binary present" || say FAIL "no pinned binary at state/$ev/earshot-pinned"

  u="$HOME/$slug-stream.url"
  if [[ -f "$u" ]]; then
    age=$(( (NOW - $(stat -f %m "$u" 2>/dev/null || echo "$NOW")) / 3600 ))
    say FAIL "STALE $u exists (${age}h old) — watcher would arm it immediately; rm it"
  else
    say PASS "no stale stream url (resolver will write it)"
  fi
done

echo "── host"
# NB: do NOT pipe a producer into `grep -q` under `set -o pipefail`. grep -q
# exits at the FIRST match, the producer takes SIGPIPE, and pipefail then makes
# the whole pipeline nonzero — so a HELD assertion reads as absent. This check
# reported a false FAIL on a demonstrably caffeinated laptop before it was
# rewritten to capture first and match second. Same trap family as the pipefail
# notes in docs/RUNBOOK.md.
ASSERTIONS=$(pmset -g assertions 2>/dev/null || true)
USER_WAKE=$(pmset -g sched 2>/dev/null | grep -i "wake at" | grep -v "by 'com.apple" | head -1 || true)
if [[ "$ASSERTIONS" == *PreventSystemSleep*caffeinate* ]]; then
  say PASS "PreventSystemSleep assertion held (machine cannot sleep)"
elif [[ -n "$USER_WAKE" ]]; then
  say PASS "user-scheduled pmset wake: $USER_WAKE"
else
  say FAIL "no caffeinate assertion and no user pmset wake — a sleeping laptop defers launchd"
fi
GFREE=$(df -g . 2>/dev/null | awk 'NR==2{print $4}')
if [[ "${GFREE:-0}" -ge 5 ]]; then say PASS "disk ${GFREE} GiB free"; else say FAIL "disk only ${GFREE:-?} GiB free"; fi
HTTP=$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' https://api.elections.kalshi.com/trade-api/v2/exchange/status 2>/dev/null || true)
if [[ "$HTTP" == "200" ]]; then say PASS "Kalshi API 200"; else say FAIL "Kalshi API returned ${HTTP:-no-response}"; fi
[[ -f "$HOME/.kalshi/mail_env" ]] && say PASS "mailer configured (pages can reach the operator)" || say FAIL "no ~/.kalshi/mail_env — failures would be SILENT"

echo
if [[ "$PROBLEMS" -eq 0 ]]; then echo "SWEEP CLEAN — every check passed."; exit 0; fi
echo "SWEEP: $PROBLEMS PROBLEM(S) — fix before the morning."; exit 1
