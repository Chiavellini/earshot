#!/usr/bin/env bash
# UNATTENDED webcast-lane supervisor (RUNBOOK "per-event unattended launch
# (webcast lane) is the next unit"; autonomy plan W2.3, approved 2026-07-28,
# unbuilt until now). Generalized from scripts/wing-autorun.sh the same way
# event-autorun.sh generalized ms-autorun.sh: every event-specific value comes
# from a per-event env file named by EARSHOT_EVENT_ENV.
#
# Why this exists: PYPL and BA were silently missed on 2026-07-28 because the
# human-absent morning window had no owner — nothing launched, nothing paged,
# and the day looked identical to a healthy idle box. wing-autorun.sh closed
# that hole for exactly one event by hardcoding it. This script is the generic
# unit: launchd/systemd fires it, and from that moment every step either
# advances the chain or pages. There is no silent exit path.
#
# Chain (each failure made LOUD rather than silent):
#   1. resolve — produce the stream URL:
#        RESOLVER=choruscall → scripts/choruscall-resolve.py polls the venue
#          (browser-free; Actinver identity from EARSHOT_REG_*)
#        RESOLVER=url-file   → scripts/url-escalate.sh owns getting a URL into
#          URL_FILE by URL_DEADLINE_HHMM (its exit 1 = give up, fail-closed)
#      Either way, no URL by the deadline ⇒ "<SLUG> NOT ARMED — no URL" page
#      and exit 1. A missed event is a possible outcome; a wrong arm is not.
#   2. validate — independent reachability probe before the URL reaches the
#      arm, exactly as arm-when-url.sh does (Ford PATH A/B post-mortem:
#      HTTP 200 AND #EXTM3U, from THIS box — session-bound tokens fail here,
#      before the event, not 0 s into it).
#   3. launch — scripts/launch-webcast.sh --wait-live: the live gate runs
#      OUTSIDE the arm (RUNBOOK 07-22 ops post-mortems #1/#2 — a liveness
#      gate inside a pipe-source cmd starves the 30 s idle guard). NOTE:
#      launch-webcast's live poll has no timeout by design; if the stream
#      never flips live, step 4's deadline TERM is the backstop and the
#      report will show 0 detections loudly.
#   4. stop — at END_HHMM_LIVE, SIGTERM to the launch PROCESS GROUP (30 s
#      grace, then an unconditional group SIGKILL sweep — a TERM-ignoring
#      ffmpeg/streamlink child cannot outlive the run). SIGTERM is the
#      validated kill path (tests/kill_sigterm.rs; CLAUDE.md: the STOP file
#      is NOT the production kill mechanism). Then the event-autorun style
#      report + mail: detections, orders, ledger, report.json.
#
# ENV CONTRACT — EARSHOT_EVENT_ENV names a sourceable file (absolute or
# repo-relative). ALL clock values are UTC HHMM: one env file must mean the
# same instant on the CDMX laptop and the us-east box (url-escalate.sh's flag
# is --deadline-utc for the same reason). Same-UTC-day events only.
#   LANE=webcast              guard — any other lane is REFUSED
#   SLUG=<slug>               names URL file, alert subjects, logs
#   CONFIG=configs/<ev>.toml  its [[sources]] cmd must read URL_FILE
#   ACCOUNT_ENV=<env file>    Kalshi creds for launch-webcast
#   EVENT_DATE=YYYY-MM-DD     UTC date guard (skipped in --test)
#   FIRE_HHMM=HHMM            optional: sleep until then if fired early
#   END_HHMM_LIVE=HHMM        SIGTERM the launch group at this time
#   RESOLVER=choruscall|url-file
#   WEBCASTID=<id>            choruscall only
#   URL_FILE=<path>           default $HOME/<slug>-stream.url
#   URL_DEADLINE_HHMM=HHMM    resolver give-up time → NOT ARMED page
#   ARM_LIVE=1                optional: pass --i-am-live (default DRY RUN;
#                             --fillable is refused by launch-webcast itself)
#   WAIT_KIND=hls|youtube     optional --wait-live kind, default hls
#
# Test mode (exercises env parsing + the URL_FILE handoff + validation +
# report + mail, never a real launch; every mail subject carries [TEST] — the
# 2026-07-15 untagged rehearsal alert was read as a real failure):
#   EARSHOT_EVENT_ENV=... scripts/webcast-autorun.sh --test <known-good-m3u8-url>
# KNOWN GAP — what --test does NOT cover: resolver dispatch never runs (the
# caller-supplied URL substitutes for choruscall-resolve.py / url-escalate.sh
# entirely), the EARSHOT_REG_* registration-identity check is SKIPPED, and no
# launch/arm happens. Consequence for rehearsal: a green --test proves nothing
# about the resolver, the registration identity, or the launch path — those
# fail for the first time at fire time unless drilled separately (resolver
# probe + the launchd-fired dress rehearsal per CLAUDE.md).
#
# Abort before fire:  boot out the launchd/systemd unit.
# Abort mid-run:      touch state/<EVENT_ID>/STOP — honored every 5 s during
#                     the FIRE_HHMM wait and during the live window, but NOT
#                     during the resolver wait (step 1 blocks inside the
#                     resolver; only SIGTERM interrupts that phase). SIGTERM
#                     to this script works in EVERY phase — both mechanisms
#                     end with the launch group TERMed.
set -uo pipefail  # NOT -e: unattended — every failure must reach a page or
                  # the report, not kill the supervisor mid-sentence
                  # (event-autorun.sh:15, wing-autorun.sh:25 — same call)

# launchd/systemd run with a minimal environment — set everything explicitly.
export PATH="/opt/homebrew/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="${HOME:-/Users/bernardodelrio}"
# Repo root from this script's location; EARSHOT_REPO overrides (the hardcoded
# CDMX path died in 6 ms on the first Linux-fired rehearsal, 2026-07-17).
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

ts()   { date -u "+%Y-%m-%dT%H:%M:%SZ"; }
log()  { echo "[$(ts)] $*"; }
# Every page goes through notify.sh (mail + macOS notification; --quiet skips
# `say`) with the mode tag on the subject. notify.sh already warns loudly on a
# missing mailer; `|| true` because a dead alert channel must not kill the run.
SUBJ_TAG=""
page() { "$REPO/scripts/notify.sh" "${SUBJ_TAG}$1" "$2" --quiet || true; }
# Greppable failure exit: FATAL: line + page + exit. Usable before LOG exists.
die()  { local code="$1" subj="$2" body="$3"
         echo "FATAL: $subj" >&2; log "FATAL: $subj — $body"
         page "$subj" "$body"; finished=1; exit "$code"; }
# Guard skip: intentional no-op (wrong day, duplicate, closed window). Still
# pages — a silent no-op on event day looks identical to a healthy idle box —
# but tags NOT-RUN: so a FATAL grep stays a real-failure grep.
bail() { local subj="$1" body="$2"
         log "NOT-RUN: $subj — $body"
         page "$subj" "$body"; finished=1; exit 0; }

# date math via python3 — `date -d` is GNU-only, `date -v` is BSD-only.
secs_until_utc() {  # secs_until_utc <HHMM> → seconds until that UTC time today (0 if passed)
  python3 - "$1" <<'PY'
import sys, datetime as dt
hhmm = sys.argv[1].zfill(4)
now = dt.datetime.now(dt.timezone.utc)
tgt = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]), second=0, microsecond=0)
print(max(0, int((tgt - now).total_seconds())))
PY
}

# ── Args ──────────────────────────────────────────────────────────────────
TEST=0 TEST_URL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --test)
      TEST=1; TEST_URL="${2:-}"
      [[ -n "$TEST_URL" ]] || { echo "FATAL: --test requires a caller-supplied URL" >&2; exit 2; }
      shift 2 ;;
    *) echo "FATAL: unknown arg: $1 (usage: EARSHOT_EVENT_ENV=<file> $0 [--test <url>])" >&2; exit 2 ;;
  esac
done
finished=0
[[ "$TEST" == 1 ]] && SUBJ_TAG="[TEST] "

# ── Per-event parameters ──────────────────────────────────────────────────
EVENT_ENV="${EARSHOT_EVENT_ENV:-}"
if [[ -z "$EVENT_ENV" ]]; then
  die 1 "webcast-autorun: NOT RUN (no event env)" \
    "Fired without EARSHOT_EVENT_ENV at $(ts). No launch attempted."
fi
[[ -f "$EVENT_ENV" ]] || EVENT_ENV="$REPO/$EVENT_ENV"
if [[ ! -f "$EVENT_ENV" ]]; then
  die 1 "webcast-autorun: NOT RUN (event env unreadable)" \
    "EARSHOT_EVENT_ENV '$EARSHOT_EVENT_ENV' is not a readable file (also tried under $REPO). No launch attempted."
fi
# shellcheck disable=SC1090
source "$EVENT_ENV"

missing=""
for v in LANE SLUG CONFIG ACCOUNT_ENV EVENT_DATE END_HHMM_LIVE RESOLVER URL_DEADLINE_HHMM; do
  [[ -n "${!v:-}" ]] || missing="$missing $v"
done
[[ -n "$missing" ]] && die 1 "webcast-autorun: NOT RUN (event env incomplete)" \
  "$EVENT_ENV lacks:$missing — exiting without launching."

SLUG_UC=$(printf '%s' "$SLUG" | tr '[:lower:]' '[:upper:]')
URL_FILE="${URL_FILE:-$HOME/$SLUG-stream.url}"
WAIT_KIND="${WAIT_KIND:-hls}"
FIRE_HHMM="${FIRE_HHMM:-}"
ARM_LIVE="${ARM_LIVE:-0}"

# This supervisor owns exactly one lane. A phone-lane env pointed here would
# "succeed" into a webcast launch with no dial — refuse instead.
[[ "$LANE" == "webcast" ]] || die 1 "$SLUG_UC NOT ARMED — wrong lane" \
  "LANE='$LANE' in $EVENT_ENV; this supervisor handles LANE=webcast only (phone lane: event-autorun.sh)."
case "$RESOLVER" in choruscall|url-file) ;; *)
  die 1 "$SLUG_UC NOT ARMED — bad RESOLVER" \
    "RESOLVER='$RESOLVER' in $EVENT_ENV; must be choruscall or url-file." ;;
esac
[[ "$RESOLVER" == "choruscall" && -z "${WEBCASTID:-}" ]] && \
  die 1 "$SLUG_UC NOT ARMED — no WEBCASTID" \
    "RESOLVER=choruscall but WEBCASTID is unset in $EVENT_ENV."
for k in END_HHMM_LIVE URL_DEADLINE_HHMM ${FIRE_HHMM:+FIRE_HHMM}; do
  t="${!k}"
  [[ "$t" =~ ^[0-9]{3,4}$ ]] || die 1 "$SLUG_UC NOT ARMED — bad HHMM ($k)" \
    "$k='$t' is not a UTC HHMM value (env file: $EVENT_ENV)."
  t=$(printf '%04d' "$((10#$t))")   # zero-pad: 930 → 0930, same as secs_until_utc
  if (( 10#${t:0:2} > 23 || 10#${t:2:2} > 59 )); then
    die 1 "$SLUG_UC NOT ARMED — bad HHMM ($k)" \
      "$k='${!k}' is out of range: HH must be 00-23, MM 00-59 (env file: $EVENT_ENV). Refusing now rather than surfacing later as a bogus 'deadline already passed'."
  fi
done
[[ -f "$REPO/$CONFIG" || -f "$CONFIG" ]] || die 1 "$SLUG_UC NOT ARMED — config missing" \
  "CONFIG '$CONFIG' not found (cwd-relative or under $REPO)."
cd "$REPO" || die 1 "$SLUG_UC NOT ARMED — repo missing" "cd $REPO failed."
[[ -f "$ACCOUNT_ENV" ]] || die 1 "$SLUG_UC NOT ARMED — account env missing" \
  "ACCOUNT_ENV '$ACCOUNT_ENV' not found."

# choruscall --register needs the Actinver identity (never committed; the
# launchd plist / systemd unit sources ~/.earshot/registration.env).
if [[ "$RESOLVER" == "choruscall" && "$TEST" != 1 ]]; then
  for v in EARSHOT_REG_FIRST EARSHOT_REG_LAST EARSHOT_REG_EMAIL EARSHOT_REG_COMPANY; do
    [[ -n "${!v:-}" ]] || die 2 "$SLUG_UC NOT ARMED — missing $v" \
      "Registration identity incomplete; choruscall-resolve cannot register. Event NOT armed."
  done
fi

# ── State dir + log (need [event].id first; sed matches launch-webcast.sh) ─
EVENT_ID=$(sed -n '/^\[event\]/,/^\[/s/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" 2>/dev/null | head -1)
[[ -n "$EVENT_ID" ]] || die 1 "$SLUG_UC NOT ARMED — no event id" \
  "Could not parse [event].id from $CONFIG."
STATE_DIR="$REPO/state/$EVENT_ID"
mkdir -p "$STATE_DIR"
# Per-run stamped log: DET_COUNT and the summary greps below must only ever
# see THIS run — an append-shared webcast-autorun.log would let a rerun count
# a prior run's detections. Unsuffixed symlink kept for humans/tails.
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ).$$"
LOG="$STATE_DIR/webcast-autorun.$RUN_STAMP.log"
SUMMARY="$STATE_DIR/webcast-autorun-summary.txt"
PIDFILE="$STATE_DIR/webcast-autorun.pid"
exec > >(tee -a "$LOG") 2>&1
log "=== webcast-autorun start ($SLUG/$EVENT_ID, resolver=$RESOLVER, test=$TEST) ==="

# ── Guards (every early exit pages — a silent no-op on event day looks
#    identical to a healthy idle box; event-autorun audit F6/F8) ───────────
if [[ "$TEST" != 1 && "$(date -u +%Y-%m-%d)" != "$EVENT_DATE" ]]; then
  bail "$SLUG_UC run: NOT RUN (date guard)" \
    "Fired on $(date -u +%Y-%m-%d)Z, not event date $EVENT_DATE. If today WAS event day, the schedule or env file is wrong."
fi
# Pidfile guard — PID-alive alone is not enough after a crash + PID reuse
# (audit F13): the live PID must actually BE a webcast-autorun.
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null \
   && ps -p "$(cat "$PIDFILE")" -o command= 2>/dev/null | grep -q "webcast-autorun"; then
  bail "$SLUG_UC run: SKIPPED (already running)" \
    "Found a live instance (pid $(cat "$PIDFILE")) at $(ts) and exited. If no run is active, investigate $PIDFILE."
fi
rm -f "$PIDFILE"; echo $$ > "$PIDFILE"
# Symlink repointed only AFTER the duplicate-instance/date guards, so a bailed
# no-op run cannot hijack the human-facing log name away from the live run.
ln -sf "webcast-autorun.$RUN_STAMP.log" "$STATE_DIR/webcast-autorun.log"
rm -f "$STATE_DIR/STOP"
if [[ "$TEST" != 1 && $((10#$(date -u +%H%M))) -ge $((10#$END_HHMM_LIVE)) ]]; then
  bail "$SLUG_UC run: NOT RUN (window already closed)" \
    "Started at $(ts) but END_HHMM_LIVE=$END_HHMM_LIVE UTC has passed — ZERO attempts made. The job likely fired late (machine asleep?)."
fi

# ── Launch-group teardown + traps ─────────────────────────────────────────
LAUNCH_PID="" LAUNCH_PGID=""
stop_launch() {  # stop_launch <why> — SIGTERM the whole group, then ALWAYS KILL-sweep it
  local why="$1"
  [[ -n "$LAUNCH_PGID" ]] || return 0   # nothing was ever launched
  if [[ -n "$LAUNCH_PID" ]] && kill -0 "$LAUNCH_PID" 2>/dev/null; then
    log "SIGTERM → launch process group $LAUNCH_PGID ($why) — arm's validated kill path (tests/kill_sigterm.rs)"
    kill -TERM -- "-$LAUNCH_PGID" 2>/dev/null || kill -TERM "$LAUNCH_PID" 2>/dev/null
    for _ in $(seq 1 30); do
      kill -0 "$LAUNCH_PID" 2>/dev/null || break
      sleep 1
    done
    kill -0 "$LAUNCH_PID" 2>/dev/null && \
      log "WARN: launch survived SIGTERM for 30 s — SIGKILL to the group (report may be incomplete)"
  fi
  # ALWAYS sweep the whole group with SIGKILL, even when the leader already
  # exited: a TERM-ignoring ffmpeg/streamlink child must not outlive the run.
  # No-op (ESRCH) on an empty group — idempotent, safe to call repeatedly.
  kill -KILL -- "-$LAUNCH_PGID" 2>/dev/null || true
}
on_exit() {  # FAILED page if we die before the clean-finish mark (best-effort)
  [[ "$finished" == 1 ]] && return
  log "EXIT trap: supervisor terminated unexpectedly"
  stop_launch "supervisor exit trap"
  page "$SLUG_UC run: FAILED (supervisor died)" \
    "webcast-autorun.sh exited unexpectedly at $(ts). Launch group was TERMed. Check $LOG"
}
on_term() {
  log "SIGTERM/SIGINT received — stopping launch group and exiting"
  stop_launch "external signal"
  page "$SLUG_UC run: STOPPED by signal" "webcast-autorun received TERM/INT at $(ts). Launch group TERMed. Log: $LOG"
  finished=1; rm -f "$PIDFILE"; exit 143
}
trap on_exit EXIT
trap on_term TERM INT

# ── Optional wait until FIRE_HHMM (lets the unit fire early, e.g. on wake) ─
if [[ "$TEST" != 1 && -n "$FIRE_HHMM" ]]; then
  wait_s=$(secs_until_utc "$FIRE_HHMM")
  if [[ "$wait_s" -gt 0 ]]; then
    log "fired early — waiting ${wait_s}s until FIRE_HHMM=$FIRE_HHMM UTC (STOP file polled every 5 s)"
    # Chunked sleep, recomputed from the wall clock each pass: honors STOP
    # during the wait (header contract) and survives a machine sleep/wake
    # mid-wait (one big `sleep N` would oversleep by the nap length).
    while wait_s=$(secs_until_utc "$FIRE_HHMM"); [[ "$wait_s" -gt 0 ]]; do
      if [[ -f "$STATE_DIR/STOP" ]]; then
        rm -f "$PIDFILE"
        bail "$SLUG_UC run: STOPPED (STOP file before launch)" \
          "STOP file appeared during the FIRE_HHMM wait at $(ts). Nothing was launched or armed."
      fi
      sleep $(( wait_s < 5 ? wait_s : 5 ))
    done
  fi
fi

# ── Step 1: resolve the stream URL ────────────────────────────────────────
# Stale URL first: a leftover file from a prior event is the loaded gun the
# fresh-URL guard exists for (wing-autorun.sh:53). The .kind file travels
# with it.
rm -f "$URL_FILE" "${URL_FILE%.url}.kind"
if [[ "$TEST" == 1 ]]; then
  # Test substitutes the resolver's OUTPUT, not the dispatch: a caller-supplied
  # URL lands in URL_FILE atomically (tmp+mv, the resolvers' own convention)
  # and everything downstream — validation, report, mail — runs for real.
  log "TEST: resolver output substituted with caller-supplied URL"
  printf '%s\n' "$TEST_URL" > "$URL_FILE.tmp" && mv "$URL_FILE.tmp" "$URL_FILE"
else
  resolve_secs=$(secs_until_utc "$URL_DEADLINE_HHMM")
  if [[ "$resolve_secs" -le 0 ]]; then
    die 1 "$SLUG_UC NOT ARMED — no URL" \
      "URL_DEADLINE_HHMM=$URL_DEADLINE_HHMM UTC already passed at start ($(ts)); resolver never ran. Fail-closed: NOT arming."
  fi
  case "$RESOLVER" in
    choruscall)
      log "resolving via choruscall-resolve.py (webcastid=$WEBCASTID, deadline ${resolve_secs}s)"
      # No --allow-progressive here: step 2 requires #EXTM3U, and the generic
      # lane arms HLS/pipe configs only. A progressive-fallback event keeps
      # its own script + fallback config (wing-autorun.sh:72-80 precedent).
      if ! python3 scripts/choruscall-resolve.py \
            --webcastid "$WEBCASTID" --slug "$SLUG" --out "$URL_FILE" \
            --register --wait-secs "$resolve_secs" --poll-secs 10; then
        die 1 "$SLUG_UC NOT ARMED — no URL" \
          "choruscall-resolve produced no usable stream before URL_DEADLINE_HHMM=$URL_DEADLINE_HHMM UTC. The call runs uncaptured. See $LOG"
      fi
      ;;
    url-file)
      log "resolving via url-escalate.sh (url-file=$URL_FILE, deadline $URL_DEADLINE_HHMM UTC)"
      # url-escalate.sh owns landing a URL in URL_FILE by the deadline
      # (documented interface: --slug --url-file --deadline-utc, UTC HHMM).
      # Exit 1 is its give-up signal — fail-closed and loud, per spec.
      if ! scripts/url-escalate.sh --slug "$SLUG" --url-file "$URL_FILE" \
            --deadline-utc "$URL_DEADLINE_HHMM"; then
        die 1 "$SLUG_UC NOT ARMED — no URL" \
          "url-escalate.sh gave up before URL_DEADLINE_HHMM=$URL_DEADLINE_HHMM UTC without producing $URL_FILE. Fail-closed: NOT arming. See $LOG"
      fi
      ;;
  esac
fi
[[ -s "$URL_FILE" ]] || die 1 "$SLUG_UC NOT ARMED — no URL" \
  "Resolver exited 0 but $URL_FILE is missing/empty — resolver contract broken. NOT arming."
URL="$(cat "$URL_FILE")"
log "url landed: $URL"

# ── Step 2: validate the URL before it reaches the arm (arm-when-url.sh) ──
# Independent reachability from THIS box (Ford PATH A/B): a session-bound
# token resolves fine where the session lives and 403s here.
PROBE="$STATE_DIR/url-probe.m3u8"
CODE=$(curl -sS -o "$PROBE" -w "%{http_code}" -m 20 "$URL" || echo 000)
log "probe: HTTP $CODE  $(head -c 120 "$PROBE" 2>/dev/null | tr '\n' ' ')"
if [[ "$CODE" != 200 ]]; then
  die 4 "$SLUG_UC URL NOT FETCHABLE" \
    "REFUSING to arm: HTTP $CODE for $URL — session-bound token? Capture must run where the session lives. Log: $LOG"
fi
if ! head -c 64 "$PROBE" | grep -q '#EXTM3U'; then
  die 5 "$SLUG_UC URL NOT HLS" \
    "REFUSING to arm: $URL returned 200 but no #EXTM3U. Log: $LOG"
fi
log "url validated: HTTP 200 + #EXTM3U"

# ── Step 3: launch ────────────────────────────────────────────────────────
# Pinned binary from the night-before preflight (scripts/preflight-event.sh):
# run EXACTLY what was drilled, no fire-time compile (audit F7). Missing pin
# on a live run falls back to cargo — loudly; it should never happen.
BIN_ARGS=()
PINNED_BIN="$STATE_DIR/earshot-pinned"
if [[ -x "$PINNED_BIN" ]]; then
  BIN_ARGS=(--bin "$PINNED_BIN")
  log "using pinned binary: $PINNED_BIN"
elif [[ "$TEST" != 1 ]]; then
  log "WARN: no pinned binary at $PINNED_BIN — launch-webcast will fall back to cargo run"
  page "$SLUG_UC run: WARNING (no pinned binary)" \
    "Falling back to 'cargo run' from the working tree — scripts/preflight-event.sh was not run. Launch continues, but this is the unpinned path the runbook forbids."
fi
LIVE_ARGS=()
if [[ "$ARM_LIVE" == 1 && "$TEST" != 1 ]]; then
  LIVE_ARGS=(--i-am-live)   # mention-probe posture; enforcement lives in arm
  POSTURE="LIVE mention-probe"
else
  POSTURE="DRY RUN (no orders sent)"
fi
# --wait-live gets the URL FILE (not the literal URL) to match launch-webcast's
# interface — but launch-webcast reads the file exactly ONCE at startup
# (launch-webcast.sh:131) and polls that captured URL. Consequence: a resolver
# that refreshes the file after this point is NOT picked up; the URL validated
# in step 2 is the URL that gets armed, full stop.
LAUNCH_CMD=(scripts/launch-webcast.sh
  --config "$CONFIG" --account "$ACCOUNT_ENV"
  --wait-live "$URL_FILE" --wait-kind "$WAIT_KIND")
LAUNCH_RC=""
if [[ "$TEST" == 1 ]]; then
  log "TEST: skipping real launch; would run: ${LAUNCH_CMD[*]}${BIN_ARGS[0]:+ ${BIN_ARGS[*]}}${LIVE_ARGS[0]:+ ${LIVE_ARGS[*]}}"
  STOP_REASON="test-mode (no launch)"
  LAUNCH_RC=0
else
  end_secs=$(secs_until_utc "$END_HHMM_LIVE")
  log "launching (posture: $POSTURE; hard SIGTERM stop at $END_HHMM_LIVE UTC, ~${end_secs}s)"
  # set -m: give the launch its OWN process group so the deadline TERM reaches
  # launch-webcast's exec'd arm AND its ffmpeg/pipe children, portably on
  # macOS bash 3.2 and Linux (no setsid binary on stock macOS, no GNU timeout).
  set -m
  "${LAUNCH_CMD[@]}" ${BIN_ARGS[@]+"${BIN_ARGS[@]}"} ${LIVE_ARGS[@]+"${LIVE_ARGS[@]}"} &
  LAUNCH_PID=$!
  LAUNCH_PGID=$LAUNCH_PID   # job-control background job = its own group leader
  set +m
  page "$SLUG_UC run: LAUNCHED ($POSTURE)" \
    "webcast-autorun launched $SLUG/$EVENT_ID at $(ts).
Posture: $POSTURE
Stream: $URL
Stop: SIGTERM at $END_HHMM_LIVE UTC (or touch $STATE_DIR/STOP)
Log: $LOG"

  # ── Step 4: deadline stop + STOP watcher ────────────────────────────────
  STOP_REASON=""
  while :; do
    if ! kill -0 "$LAUNCH_PID" 2>/dev/null; then
      STOP_REASON="self-exit"; break
    fi
    if [[ -f "$STATE_DIR/STOP" ]]; then
      STOP_REASON="STOP file"; stop_launch "STOP file"; break
    fi
    if [[ $((10#$(date -u +%H%M))) -ge $((10#$END_HHMM_LIVE)) ]]; then
      STOP_REASON="deadline $END_HHMM_LIVE UTC"; stop_launch "deadline"; break
    fi
    sleep 5
  done
  wait "$LAUNCH_PID" 2>/dev/null; LAUNCH_RC=$?
  log "launch ended: rc=$LAUNCH_RC (stop reason: $STOP_REASON)"
  if [[ "$STOP_REASON" == "self-exit" && "$LAUNCH_RC" -ne 0 ]]; then
    # No redial loop in this lane (v1): a webcast that dies stays dead until a
    # human looks — so this page must go out WHILE the window is open, not be
    # discovered in the post-run summary (audit F6).
    page "$SLUG_UC run: launch EXITED EARLY (rc=$LAUNCH_RC)" \
      "launch-webcast exited rc=$LAUNCH_RC before the $END_HHMM_LIVE UTC deadline at $(ts). NOT relaunching (v1 is single-shot). Recent log:
$(tail -15 "$LOG")"
  fi
fi

# ── Report + mail (modeled on event-autorun.sh's report block) ────────────
log "building report"
DET_COUNT=$(grep -c "decision intent" "$LOG" 2>/dev/null || true)
DET_COUNT="${DET_COUNT:-0}"
{
  echo "EARSHOT $EVENT_ID webcast autonomous run — $(ts)  (posture: ${POSTURE:-n/a}, test=$TEST)"
  echo
  echo "resolver: $RESOLVER   url: $URL"
  echo "stop reason: ${STOP_REASON:-none}   launch rc: ${LAUNCH_RC:-n/a}"
  echo
  echo "── detections (decision intents): $DET_COUNT ──"
  grep "decision intent" "$LOG" 2>/dev/null | tail -30 || echo "(none)"
  echo
  echo "── orders / executor lines ──"
  grep -iE "order (acked|sent|placed)|LOST_RACE|refus" "$LOG" 2>/dev/null | tail -30 || echo "(none)"
  echo
  echo "── ledger ──"
  cat "$STATE_DIR/probe_ledger.json" 2>/dev/null || echo "(no ledger)"
  echo
  echo "── report.json (tail) ──"
  tail -40 "$STATE_DIR/report.json" 2>/dev/null || echo "(no report.json)"
  echo
  echo "full log: $LOG"
} > "$SUMMARY"
page "$SLUG_UC run: SUMMARY ($DET_COUNT detections, stop: ${STOP_REASON:-none})" "$(cat "$SUMMARY")"
log "summary mailed; written to $SUMMARY"

rm -f "$PIDFILE"
finished=1
log "=== webcast-autorun done ==="
exit 0
