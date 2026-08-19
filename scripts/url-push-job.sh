#!/usr/bin/env bash
# url-push-job.sh — the SCHEDULED, PCM-PROVEN, MULTI-HOST url push for preresolved plans.
# Closes S5 ("the push is MANUAL") after 2026-08-19, when TGT and TOL were both missed:
# the box legs waited 75 min on a url-file that had only ever been published on the
# laptop, and the laptop leg of TOL armed on a "master" that yielded ZERO frames.
#
#   scripts/url-push-job.sh --install --slug tol --at-utc 12:31 --until-utc 12:44 \
#       --run-dir var/runs/<E-BOX>/<R> [--host ubuntu@box] \
#       --run-dir var/runs/<E>/<R>     --host local                # any number of (run-dir, host) pairs
#   scripts/url-push-job.sh --run     --slug tol --until-utc 12:44 --run-dir … --host … [...]
#   scripts/url-push-job.sh --check                                # offline self-test
#
# --install writes and bootstraps a launchd one-shot (com.earshot.push.<slug>) that runs
# `--run` at --at-utc (converted to this host's local time) and READS THE LOADED JOB BACK.
# --run loops until --until-utc: (1) execute the FIRST run's recorded preresolve argv
# (all listed plans must share the same url-file basename — they are the legs of ONE
# event on several hosts); (2) PROVE the minted URL yields decodable audio on this host
# with scripts/pcm-gate.sh (6 s, ≥80 % bytes) — a holding VOD, an empty "metafile"
# master or a bot-walled manifest all FAIL here and trigger a re-resolve; (3) publish to
# EVERY listed host with scripts/push-url-file.sh --from-file (ssh for remote hosts,
# native for `local`); (4) page on success and on final failure. Log:
# state/<E>/push.log for every listed event. Never prints the URL.
# Node: control.acquire.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; REPO=$(cd "$REPO" && pwd -P); cd "$REPO"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
MODE="" SLUG="" AT="" UNTIL="" PCM_SECS=6
RUNS=() HOSTS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --install) MODE=install; shift ;;
    --run) MODE=run; shift ;;
    --check) MODE=check; shift ;;
    --slug) SLUG="${2:?}"; shift 2 ;;
    --at-utc) AT="${2:?}"; shift 2 ;;
    --until-utc) UNTIL="${2:?}"; shift 2 ;;
    --pcm-secs) PCM_SECS="${2:?}"; shift 2 ;;
    --run-dir) RUNS+=("${2:?}"); HOSTS+=("__PENDING__"); shift 2 ;;
    --host) [[ ${#HOSTS[@]} -gt 0 ]] || { echo "USAGE-ERROR: --host must follow a --run-dir" >&2; exit 2; }
            HOSTS[$((${#HOSTS[@]}-1))]="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
for i in "${!HOSTS[@]}"; do [[ "${HOSTS[$i]}" == "__PENDING__" ]] && HOSTS[$i]="${EARSHOT_BOX:-ubuntu@3.212.174.94}"; done

plan_field() {  # plan_field <run-dir> <python expr on access plan dict `a` and run plan `p`>
  python3 - "$1" "$2" <<'PY'
import json,sys,pathlib
run=pathlib.Path(sys.argv[1]); a=json.load(open(run/"control"/"access-plan.json")); p=json.load(open(run/"control"/"run-plan.json"))
print(eval(sys.argv[2]))
PY
}

if [[ "$MODE" == check ]]; then
  P=0; F=0; chk() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; P=$((P+1)); else echo "  FAIL  $1 (expected $2, got $3)"; F=$((F+1)); fi; }
  # local-time conversion is the only pure piece worth pinning: 12:31Z on a UTC-6 host = 06:31
  got=$(TZ=America/Mexico_City python3 -c 'import datetime;from zoneinfo import ZoneInfo;d=datetime.datetime(2026,8,19,12,31,tzinfo=datetime.timezone.utc).astimezone(ZoneInfo("America/Mexico_City"));print(d.strftime("%H:%M"))')
  chk "utc->local conversion" "06:31" "$got"
  # hosts default to the box unless --host local follows the run-dir
  chk "host default" "${EARSHOT_BOX:-ubuntu@3.212.174.94}" "$( RUNS=(); HOSTS=(); HOSTS+=("__PENDING__"); for i in "${!HOSTS[@]}"; do [[ "${HOSTS[$i]}" == "__PENDING__" ]] && HOSTS[$i]="${EARSHOT_BOX:-ubuntu@3.212.174.94}"; done; echo "${HOSTS[0]}")"
  # a bounded leg is killed at the bound and reports 124 (never outlives --until-utc)
  UNTIL_EPOCH=$(( $(date +%s) + 7 )); tmpl="$(mktemp "${TMPDIR:-/tmp}/url-push-check.XXXXXX")"
  run_bounded_leg() { local logf="$1"; shift; local budget=$(( UNTIL_EPOCH - $(date +%s) )); (( budget > 5 )) || budget=5
    "$@" > "$logf" 2>&1 & local pid=$!; local waited=0
    while kill -0 "$pid" 2>/dev/null; do if (( waited >= budget )); then kill -TERM "$pid" 2>/dev/null; sleep 2; kill -KILL "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; echo "LEG-BOUND" >> "$logf"; return 124; fi; sleep 1; waited=$((waited+1)); done; wait "$pid"; }
  run_bounded_leg "$tmpl" sleep 60; chk "bounded leg killed at the bound (exit 124)" "124" "$?"
  run_bounded_leg "$tmpl" true; chk "bounded leg passes a fast exit through" "0" "$?"; rm -f "$tmpl"
  (( F )) && { echo "SELFTEST FAIL ($F failed, $P passed)"; exit 1; }; echo "SELFTEST PASS ($P checks)"; exit 0
fi
[[ -n "$SLUG" && ${#RUNS[@]} -gt 0 && -n "$UNTIL" ]] || { echo "USAGE-ERROR: need --slug, --until-utc and >=1 --run-dir" >&2; exit 2; }
for r in "${RUNS[@]}"; do [[ -f "$r/control/access-plan.json" ]] || { echo "REFUSED: $r has no AccessPlan" >&2; exit 1; }; done
BASES=(); for r in "${RUNS[@]}"; do BASES+=("$(plan_field "$r" 'pathlib.Path(a["endpoint"]["url_files"][0]).name')"); done
for b in "${BASES[@]}"; do [[ "$b" == "${BASES[0]}" ]] || { echo "REFUSED: listed plans mint different url-files (${BASES[*]}) — one event per job" >&2; exit 1; }; done
BASE="${BASES[0]}"; EVENTS=(); for r in "${RUNS[@]}"; do EVENTS+=("$(plan_field "$r" 'p["event_id"]')"); done

if [[ "$MODE" == install ]]; then
  [[ -n "$AT" ]] || { echo "USAGE-ERROR: --install needs --at-utc HH:MM" >&2; exit 2; }
  read -r H M D MO < <(python3 -c 'import sys,datetime
h,m=map(int,sys.argv[1].split(":")); now=datetime.datetime.now(datetime.timezone.utc)
d=datetime.datetime(now.year,now.month,now.day,h,m,tzinfo=datetime.timezone.utc)
if d < now: d+=datetime.timedelta(days=1)
l=d.astimezone(); print(l.hour,l.minute,l.day,l.month)' "$AT")
  LABEL="com.earshot.push.$SLUG"; PL="$HOME/Library/LaunchAgents/$LABEL.plist"
  ARGS=""; for i in "${!RUNS[@]}"; do ARGS+="<string>--run-dir</string><string>$REPO/${RUNS[$i]#$REPO/}</string><string>--host</string><string>${HOSTS[$i]}</string>"; done
  cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$REPO/scripts/url-push-job.sh</string><string>--run</string><string>--slug</string><string>$SLUG</string><string>--until-utc</string><string>$UNTIL</string><string>--pcm-secs</string><string>$PCM_SECS</string>$ARGS</array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string><key>HOME</key><string>$HOME</string></dict>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><false/>
  <key>StartCalendarInterval</key><dict><key>Month</key><integer>$MO</integer><key>Day</key><integer>$D</integer><key>Hour</key><integer>$H</integer><key>Minute</key><integer>$M</integer></dict>
  <key>StandardOutPath</key><string>$REPO/state/_day/launchd-$LABEL.log</string>
  <key>StandardErrorPath</key><string>$REPO/state/_day/launchd-$LABEL.log</string>
</dict></plist>
PLIST
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
  launchctl bootstrap "gui/$(id -u)" "$PL" || { echo "REFUSED: launchctl bootstrap failed for $LABEL" >&2; exit 1; }
  RB=$(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -o '"Hour" => [0-9]*\|"Minute" => [0-9]*\|"Day" => [0-9]*' | sort -u | tr '\n' ' ')
  [[ -n "$RB" ]] || { echo "REFUSED: loaded job readback empty for $LABEL" >&2; exit 1; }
  echo "INSTALLED $LABEL fires ${AT}Z (local $H:$(printf %02d "$M") day $D) until ${UNTIL}Z — loaded readback: $RB"
  echo "  targets: $(for i in "${!RUNS[@]}"; do printf '%s@%s ' "${EVENTS[$i]}" "${HOSTS[$i]}"; done)"
  echo "  disarm: launchctl bootout gui/$(id -u)/$LABEL"
  exit 0
fi

# ── run ────────────────────────────────────────────────────────────────────
for e in "${EVENTS[@]}"; do mkdir -p "state/$e"; done
log() { for e in "${EVENTS[@]}"; do echo "$(date -u +%FT%TZ) $*" >> "state/$e/push.log"; done; echo "$*"; }
page() { scripts/notify.sh "$1" "$2" ${3:+--quiet} >/dev/null 2>&1 || true; }
RESOLVE_ARGV=(); while IFS= read -r part; do RESOLVE_ARGV+=("$part"); done < <(
  plan_field "${RUNS[0]}" '"\n".join(x.replace("$HOME/", __import__("os").path.expanduser("~")+"/") for x in a["resolver"]["preresolve"]["argv"])')
[[ ${#RESOLVE_ARGV[@]} -gt 0 ]] || { log "REFUSED: ${RUNS[0]} records no preresolve argv"; exit 1; }
log "=== url-push-job slug=$SLUG until=${UNTIL}Z targets=$(for i in "${!RUNS[@]}"; do printf '%s@%s ' "${EVENTS[$i]}" "${HOSTS[$i]}"; done) leg=${RESOLVE_ARGV[0]} ${RESOLVE_ARGV[1]:-}"
# Every resolve leg is BOUNDED to --until-utc. The loop condition is only
# evaluated between iterations, and a browser-resolve leg carries its own
# --wait-secs (6600 on the FUTU plan) — unbounded, one leg could outlive the
# window and this job would never reach its NOT ARMED page.
UNTIL_EPOCH=$(python3 -c 'import sys,datetime as d
h,m=map(int,sys.argv[1].split(":")); n=d.datetime.now(d.timezone.utc)
t=n.replace(hour=h,minute=m,second=0,microsecond=0)
if t<n-d.timedelta(hours=12): t+=d.timedelta(days=1)
print(int(t.timestamp()))' "$UNTIL")
run_bounded_leg() {  # run_bounded_leg <logfile> <argv...> -> exit code (124 = bound hit)
  local logf="$1"; shift
  local budget=$(( UNTIL_EPOCH - $(date +%s) )); (( budget > 5 )) || budget=5
  # own process group, so the bound kills the headless browser too, not just python
  python3 -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@" > "$logf" 2>&1 & local pid=$!
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if (( waited >= budget )); then kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null; sleep 2; kill -KILL -- "-$pid" 2>/dev/null; wait "$pid" 2>/dev/null; echo "LEG-BOUND: killed after ${waited}s (until ${UNTIL}Z)" >> "$logf"; return 124; fi
    sleep 1; waited=$((waited+1))
  done
  wait "$pid"
}
until [[ "$(date -u +%H:%M)" > "$UNTIL" ]]; do
  rm -f "$HOME/$BASE"
  LEGLOG="$(mktemp "${TMPDIR:-/tmp}/url-push-leg.XXXXXX")"
  run_bounded_leg "$LEGLOG" "${RESOLVE_ARGV[@]}"; RC=$?
  cat "$LEGLOG" >> "state/${EVENTS[0]}/push.log"
  # Candidates: the minted url-file first, then every other manifest the leg SAW
  # (MANIFEST-SEEN lines), masters before variants. TOL 2026-08-19 minted a
  # "metafile" master that yielded zero frames while sibling media playlists
  # existed — the PCM gate, not the resolver's preference, decides what ships.
  CANDS=()
  [[ $RC -eq 0 && -s "$HOME/$BASE" ]] && CANDS+=("$(cat "$HOME/$BASE")")
  while IFS= read -r u; do [[ -n "$u" ]] && CANDS+=("$u"); done < <(grep -o 'MANIFEST-SEEN master \[200\] [^ ]*' "$LEGLOG" | awk '{print $4}')
  while IFS= read -r u; do [[ -n "$u" ]] && CANDS+=("$u"); done < <(grep -o 'MANIFEST-SEEN variant \[200\] [^ ]*' "$LEGLOG" | awk '{print $4}')
  rm -f "$LEGLOG"
  if [[ ${#CANDS[@]} -eq 0 ]]; then log "resolve leg exit=$RC, no manifest candidates — retrying"; sleep 10; continue; fi
  PASSED=""
  for u in "${CANDS[@]}"; do
    printf '%s' "$u" > "$HOME/.${BASE}.cand"
    if scripts/pcm-gate.sh --kind hls --url-file "$HOME/.${BASE}.cand" --secs "$PCM_SECS" --label "$SLUG-push" >> "state/${EVENTS[0]}/push.log" 2>&1; then PASSED="$u"; break; fi
    log "PCM-GATE FAIL on candidate ${u:0:60}… — next"
  done
  rm -f "$HOME/.${BASE}.cand"
  if [[ -z "$PASSED" ]]; then log "no candidate passed the PCM gate (holding VOD / empty master / walled media) — re-resolving"; sleep 15; continue; fi
  printf '%s' "$PASSED" > "$HOME/$BASE"
  log "PCM-GATE PASS — publishing"
  OK=1; for i in "${!RUNS[@]}"; do
    if scripts/push-url-file.sh --run-dir "${RUNS[$i]}" --from-file "$HOME/$BASE" --host "${HOSTS[$i]}" >> "state/${EVENTS[$i]}/push.log" 2>&1; then log "PUSHED ${EVENTS[$i]} @ ${HOSTS[$i]}"; else log "PUSH FAILED ${EVENTS[$i]} @ ${HOSTS[$i]}"; OK=0; fi
  done
  if (( OK )); then page "$SLUG url pushed (pcm-proven)" "published to ${#RUNS[@]} host(s) at $(date -u +%T)Z" quiet; exit 0; fi
  sleep 10
done
log "FAILED: no pcm-proven URL published by ${UNTIL}Z"
page "$SLUG NOT ARMED — no pcm-proven stream URL by ${UNTIL}Z" "every resolve either found no manifest or failed the PCM gate; see state/${EVENTS[0]}/push.log"
exit 3
