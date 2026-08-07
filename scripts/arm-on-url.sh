#!/usr/bin/env bash
# arm-on-url.sh — wait for a resolver to land a stream URL, PROVE it decodes on
# THIS host, then launch the webcast arm. Fail-closed at every step.
#
# WHY NOT arm-when-url.sh: that script's readiness gate is HTTP 200 + #EXTM3U.
# On YouTube that gate is affirmatively misleading — HOOD 2026-07-29 passed it
# and still captured nothing, because the bot-wall refuses SEGMENTS while the
# manifest 200s (scripts/youtube-resolve.sh:26-31). This script gates on
# scripts/pcm-gate.sh — actual decoded PCM bytes — which is the only check that
# can distinguish "reachable" from "capturable".
#
#   scripts/arm-on-url.sh --slug pltr --kind youtube \
#       --config configs/webcast-pltr-26aug03-arm-wc.toml \
#       --account $HOME/.earshot/ms.env --deadline-utc 21:20 [--i-am-live]
#
# Chain: wait for $HOME/<slug>-stream.url  →  pcm-gate on this host  →
#        launch-webcast.sh (which runs its own --wait-live poll and guards).
# Any step failing pages via notify.sh and exits nonzero. There is no silent
# path out: a missed event is an acceptable outcome, a deaf arm is not.
set -uo pipefail

REPO="${EARSHOT_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$REPO"

SLUG="" KIND="" CONFIG="" ACCOUNT="" DEADLINE_HHMM="" LIVE="" FILLABLE="" SECS=6
while [[ $# -gt 0 ]]; do
  case "$1" in
    --slug)         SLUG="${2:?}"; shift 2 ;;
    --kind)         KIND="${2:?}"; shift 2 ;;
    --config)       CONFIG="${2:?}"; shift 2 ;;
    --account)      ACCOUNT="${2:?}"; shift 2 ;;
    --deadline-utc) DEADLINE_HHMM="${2:?}"; shift 2 ;;
    --secs)         SECS="${2:?}"; shift 2 ;;
    --i-am-live)    LIVE="--i-am-live"; shift ;;
    # Forwarded to launch-webcast.sh, which owns the W7 ruling (lifted
    # 2026-08-05 for this slate). Requires --i-am-live; checked below so a
    # misconfigured timer fails at install time, not at T-0.
    --fillable)     FILLABLE="--fillable"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$SLUG" && -n "$KIND" && -n "$CONFIG" && -n "$ACCOUNT" && -n "$DEADLINE_HHMM" ]] || {
  echo "usage: arm-on-url.sh --slug S --kind youtube|hls --config C --account A --deadline-utc HH:MM [--i-am-live] [--fillable]" >&2
  exit 2; }
[[ -z "${FILLABLE:-}" || -n "$LIVE" ]] || {
  echo "--fillable requires --i-am-live" >&2; exit 2; }

URLF="$HOME/$SLUG-stream.url"
LOG="$HOME/$SLUG-armonurl-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG") 2>&1
page() { "$REPO/scripts/notify.sh" "$1" "$2" || true; }
EVENT_ID=$(sed -n '/^\[event\]/,/^\[/s/^[[:space:]]*id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
jnl() { "$REPO/scripts/journal.sh" --event "${EVENT_ID:-UNKNOWN}" --actor arm-on-url.sh --action "$1" --outcome "$2" --detail "${3:-}" --log "$LOG" || true; }

# ── Green gate (2026-08-04, marker-activated) ───────────────────────────────
# Enforces ONLY when a preflight verdict exists for this event — legacy and
# rehearsal invocations are untouched. EARSHOT_GREEN_OVERRIDE="<reason>"
# proceeds but pages: an override that pages is a decision, not a bypass.
# Runs at watcher START, hours before T-0, so a refusal pages with time to fix.
if [[ -n "$EVENT_ID" && -f "state/${EVENT_ID}/preflight-result.json" ]]; then
  if GREEN_OUT=$("$REPO/scripts/assert-ready.sh" "$CONFIG" 2>&1); then
    echo "$GREEN_OUT"
  elif [[ -n "${EARSHOT_GREEN_OVERRIDE:-}" ]]; then
    echo "GREEN GATE OVERRIDDEN: $GREEN_OUT (reason: $EARSHOT_GREEN_OVERRIDE)"
    page "GREEN GATE OVERRIDDEN: $EVENT_ID" "$GREEN_OUT — override reason: $EARSHOT_GREEN_OVERRIDE"
    jnl note warn "green gate overridden: $EARSHOT_GREEN_OVERRIDE"
  else
    echo "REFUSED by green gate: $GREEN_OUT"
    page "$SLUG REFUSED by green gate" "$GREEN_OUT — fix and restart the watcher, or set EARSHOT_GREEN_OVERRIDE=\"<reason>\" (pages)."
    jnl watch-start refused "$GREEN_OUT"
    exit 7
  fi
fi
jnl watch-start info "deadline=$DEADLINE_HHMM kind=$KIND posture=${LIVE:---dry-run}"

# Resolve the deadline to an absolute epoch the same way url-escalate.sh does:
# the NEXT occurrence of that UTC wall time.
NOW=$(date -u +%s)
TODAY=$(date -u +%Y-%m-%d)
DEADLINE=$(python3 -c "
import calendar,datetime,sys
h,m=sys.argv[1].split(':')
d=datetime.datetime.strptime(sys.argv[2],'%Y-%m-%d').replace(hour=int(h),minute=int(m))
e=calendar.timegm(d.timetuple())
if e < int(sys.argv[3]): e += 86400
print(e)" "$DEADLINE_HHMM" "$TODAY" "$NOW")
echo "=== arm-on-url $SLUG kind=$KIND $(date -u +%FT%TZ) ==="
echo "DEADLINE-RESOLVED: $(python3 -c "import datetime;print(datetime.datetime.fromtimestamp($DEADLINE,datetime.timezone.utc).isoformat())")"
echo "url-file : $URLF"
echo "posture  : ${LIVE:---dry-run}"

# ── step 1: wait for the resolver ──────────────────────────────────────────
while [[ ! -s "$URLF" ]]; do
  if (( $(date -u +%s) > DEADLINE )); then
    echo "URL-MISSED: $SLUG — no $URLF by deadline. NOT ARMED."
    page "$SLUG NOT ARMED — no URL" "Resolver never produced $URLF before $DEADLINE_HHMM UTC. See $LOG"
    jnl url fail "URL-MISSED at deadline $DEADLINE_HHMM — NOT ARMED"
    exit 3
  fi
  sleep 5
done
echo "URL-READY: $SLUG $(head -c 120 "$URLF")"
jnl url ok "$(head -c 120 "$URLF")"

# ── step 2: PCM gate — the check that HTTP 200 cannot make ─────────────────
# Deliberately gates on the URL FILE, so we test exactly what the arm will read.
if ! "$REPO/scripts/pcm-gate.sh" --kind "$KIND" --url-file "$URLF" --secs "$SECS" --label "$SLUG"; then
  echo "PCM-GATE FAILED for $SLUG on $(hostname -s) — REFUSING to arm a deaf leg."
  page "$SLUG NOT ARMED — no decodable audio" \
       "pcm-gate got no PCM from $URLF on $(hostname -s). This vantage is dead (HOOD 07-29 class). See $LOG"
  jnl pcm-gate fail "no decodable PCM on $(hostname -s) — NOT ARMED"
  exit 4
fi
jnl pcm-gate ok "decoded PCM on $(hostname -s)"

# ── step 3: launch ─────────────────────────────────────────────────────────
# NO --wait-live here, deliberately. That gate polls the target for liveness,
# but the value in $URLF is a MASTER MANIFEST, not a watch page — with
# --wait-kind youtube it would run `yt-dlp --print is_live` against an .m3u8
# and never see True, hanging forever 20 s before the call. It is also
# redundant: step 2 just decoded real PCM off this exact URL, which is a
# strictly stronger liveness proof than an is_live poll.
# WHY THIS IS A LOOP AND NOT AN `exec` (2026-08-07). It used to end with
# `exec launch-webcast.sh`, which replaces the process image — so there was no
# child, no `wait`, no trap, and NOTHING could observe the arm dying. AAOI
# 26AUG06 armed at 19:58:00Z and lost every leg at 19:58:36Z with not one
# journal line recording it; the stream was live again at 21:04 and a human
# re-armed at 21:06, 36 minutes into the call. `$DEADLINE` was already an
# absolute epoch here, read only by the URL-wait loop above.
#
# Bounded by the SAME deadline, so this cannot outlive the call window.
REARMS=0
MAX_REARMS="${MAX_REARMS:-6}"
# An attempt that dies faster than this never really started — re-gating
# immediately would just spin. redial-supervisor.sh:183 uses the same bar for
# the phone lane; AAOI died at 32 s, right on it.
MEDIA_CONFIRM_SECS="${MEDIA_CONFIRM_SECS:-30}"

while :; do
  echo "ARMING $SLUG at $(date -u +%FT%TZ) (attempt $((REARMS + 1)))"
  jnl arm-handoff info "launch-webcast.sh posture=${LIVE:---dry-run}${FILLABLE:+ fillable} attempt=$((REARMS + 1))"

  T0=$(date -u +%s)
  "$REPO/scripts/launch-webcast.sh" --config "$CONFIG" --account "$ACCOUNT" $LIVE $FILLABLE &
  ARM_PID=$!
  # Forward a stop to the child so a SIGTERM'd watcher does not orphan an arm.
  trap 'kill "$ARM_PID" 2>/dev/null; exit 143' TERM INT
  wait "$ARM_PID"; RC=$?
  trap - TERM INT
  DUR=$(( $(date -u +%s) - T0 ))
  REARMS=$((REARMS + 1))

  jnl arm-exit info "attempt=$REARMS rc=$RC after ${DUR}s"
  echo "ARM ENDED: $SLUG rc=$RC after ${DUR}s (attempt $REARMS)"

  # A deliberate disarm wins over any re-arm.
  if [[ -f "$REPO/state/$EVENT_ID/STOP" ]]; then
    echo "STOP file present — not re-arming."
    jnl arm-exit ok "STOP file present after attempt $REARMS — done"
    exit "$RC"
  fi

  NOW=$(date -u +%s)
  if (( NOW >= DEADLINE )); then
    echo "past arm deadline — not re-arming."
    jnl arm-exit info "past deadline after attempt $REARMS (rc=$RC) — done"
    exit "$RC"
  fi
  if (( REARMS >= MAX_REARMS )); then
    page "$SLUG re-arm budget exhausted" \
         "$MAX_REARMS attempts, last rc=$RC after ${DUR}s, deadline not yet reached. See $LOG"
    jnl arm-exit fail "re-arm budget $MAX_REARMS exhausted (last rc=$RC)"
    exit "$RC"
  fi
  # rc=0 before the deadline means the stream ended early, which for a webcast
  # is usually the call ending — but it is also what a dead leg looks like, so
  # re-gate rather than guess. The PCM gate below decides.
  if (( DUR <= MEDIA_CONFIRM_SECS )); then
    page "$SLUG ARM-NO-MEDIA" \
         "attempt $REARMS died after only ${DUR}s (rc=$RC) — the stream is not holdable. See $LOG"
    jnl arm-exit fail "ARM-NO-MEDIA: attempt $REARMS died in ${DUR}s"
  fi

  sleep 20
  # Re-gate before re-arming: the URL may have rotated, and re-arming into a
  # stream that is still dead is what burns the budget.
  if [[ ! -s "$URLF" ]]; then
    echo "url file vanished — not re-arming."
    jnl arm-exit fail "url file $URLF empty/absent at re-arm"
    exit "$RC"
  fi
  if ! "$REPO/scripts/pcm-gate.sh" --kind "$KIND" --url-file "$URLF" --secs "$SECS" --label "$SLUG"; then
    echo "re-gate FAILED — stream still dead, waiting."
    jnl pcm-gate fail "re-gate failed before attempt $((REARMS + 1))"
    continue
  fi
  jnl pcm-gate ok "re-gate passed before attempt $((REARMS + 1))"
done
