#!/usr/bin/env bash
# Day-of arm launcher for a staged webcast event. One command per event, with
# the guards that have actually bitten us wired in as REFUSALS rather than as
# runbook prose you have to remember at 05:50 local.
#
#   scripts/run-arm.sh pypl        # PayPal  Tue 07-28 08:00 ET
#   scripts/run-arm.sh ba          # Boeing  Tue 07-28 10:30 ET
#   scripts/run-arm.sh f           # Ford    Tue 07-28 17:00 ET
#   scripts/run-arm.sh <slug> --dry    # dry run (no --i-am-live)
#
# Why this exists: before 2026-07-27 there was no arm helper at all — every
# ~/run-*.sh on the box is a `capture-bakeoff`, not an `arm` — so the three
# Tuesday events would each have been hand-assembled from docs/RUNBOOK.md under
# time pressure. Every guard below is a real incident:
#
#   1. STALE URL FILE. On 2026-07-27 $HOME/pypl-stream.url was left pointing at
#      an AWS IVS *demo channel* after a dry run. An arm started with it would
#      have captured the demo channel while looking entirely normal
#      (docs/RUNBOOK.md "T−20 min"). We refuse a URL file older than the config
#      unless --url-ok is passed.
#   2. VARIANT INSTEAD OF MASTER. A media/variant playlist can die mid-call while
#      the channel stays live — measured 10,860 consecutive 404s over 45 min
#      (LATENCY-PROOF §V6). hls-edge-fetch.py re-resolves ONLY if handed a
#      master; given a variant it prints "CANNOT RE-RESOLVE" and goes deaf, then
#      the leg dies on the 30 s idle guard and forfeits the rest of the call.
#   3. MISSING ORT_DYLIB_PATH. streaming_ctc dlopens onnxruntime; unset, the arm
#      HANGS SILENTLY at "book snapshots live" — no error, no threads, looks
#      healthy. launch-webcast.sh sets it, but we verify the dylib is findable
#      before we get that far so the failure is loud and early.
#   4. UNPINNED BINARY. preflight pins per event; a fire-time compile is exactly
#      what the pin exists to prevent. We refuse if the pin is absent.
#   5. DRY RUN BY ACCIDENT. --i-am-live is REQUIRED to place orders (W7 lifted
#      2026-07-27). Default here is LIVE, and --dry is the explicit opt-out, so
#      forgetting a flag cannot silently produce a no-order run.
#
# Posture is enforced in `arm`, not here: --allow-mention is the mention-PROBE
# gate (cap must be ≤1¢, orders non-crossing), so a live arm on these configs
# still CANNOT fill. --fillable stays refused by launch-webcast.sh.
set -euo pipefail

SLUG="${1:-}"; shift || true
LIVE=1; URL_OK=0
for a in "$@"; do
  case "$a" in
    --dry)    LIVE=0 ;;
    --url-ok) URL_OK=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."
REPO="$PWD"
fail() { echo "REFUSING: $*" >&2; exit 1; }

# GNU timeout exists on the box; the CDMX laptop has neither timeout nor
# gtimeout, so fall back to a background-killer that reports 124 like timeout.
run_with_timeout() {  # <secs> <cmd…>
  if command -v timeout >/dev/null 2>&1; then timeout "$@"; return $?; fi
  if command -v gtimeout >/dev/null 2>&1; then gtimeout "$@"; return $?; fi
  local secs="$1"; shift
  "$@" & local pid=$!
  ( sleep "$secs" && kill -9 "$pid" 2>/dev/null ) & local watcher=$!
  local rc=0; wait "$pid" || rc=$?
  kill "$watcher" 2>/dev/null || true
  [[ $rc -ge 128 ]] && return 124
  return $rc
}

case "$SLUG" in
  pypl) CONFIG=configs/webcast-pypl-26jul28-arm.toml; URLF=$HOME/pypl-stream.url; EVENT=KXEARNINGSMENTIONPYPL-26JUL28 ;;
  ba)   CONFIG=configs/webcast-ba-26jul28-arm.toml;   URLF=$HOME/ba-stream.url;   EVENT=KXEARNINGSMENTIONBA-26JUL21 ;;
  # ⚠ URLF is `ford-stream.url`, NOT `f-stream.url`. The SLUG is `f` but the
  # config reads $HOME/ford-stream.url (and ford-arrival.log / ford_spec_edge.err),
  # and docs/RUNBOOK.md says to substitute `ford`. Mismatched, guard 2 would
  # validate a file the arm never reads — every check green, wrong stream.
  f)    CONFIG=configs/webcast-f-26jul28-arm.toml;    URLF=$HOME/ford-stream.url; EVENT=KXEARNINGSMENTIONF-26JUL28 ;;
  *)
    # Generic path (2026-07-29 onward): every staged config is
    # configs/webcast-<slug>-<date>-arm.toml and reads $HOME/<slug>-stream.url —
    # the slug/urlfile mismatch was a Ford-only legacy. Refuse ambiguity: more
    # than one matching config means the operator must clean up, not us guess.
    MATCHES=(configs/webcast-"$SLUG"-*-arm.toml)
    [[ -f "${MATCHES[0]}" ]] || { echo "usage: $0 <slug> [--dry] [--url-ok] — no configs/webcast-$SLUG-*-arm.toml" >&2; exit 2; }
    [[ ${#MATCHES[@]} -eq 1 ]] || fail "ambiguous slug $SLUG: ${MATCHES[*]} — delete or rename the stale one"
    CONFIG="${MATCHES[0]}"
    URLF="$HOME/$SLUG-stream.url"
    EVENT="$(sed -n 's/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)"
    [[ -n "$EVENT" ]] || fail "no event id parsed from $CONFIG"
    ;;
esac

# --- guard 1: config present -------------------------------------------------
[[ -f "$CONFIG" ]] || fail "config $CONFIG not found"

# --- guard 2: stream URL present, non-empty, and not stale -------------------
[[ -s "$URLF" ]] || fail "$URLF missing or empty — surface the m3u8 first (docs/RUNBOOK.md T−20)"
URL="$(head -1 "$URLF" | tr -d '[:space:]')"
[[ "$URL" == http*://*.m3u8* ]] || fail "$URLF does not look like an m3u8 URL: ${URL:0:60}"
if [[ "$URL_OK" -eq 0 && "$URLF" -ot "$CONFIG" ]]; then
  fail "$URLF is OLDER than $CONFIG — probably a stale URL from a previous run.
         Re-surface it (rm -f $URLF first), or pass --url-ok if you are certain."
fi

# --- guard 3: master, not a variant playlist (LATENCY-PROOF §V6) ------------
if [[ "$URL" == *"/v1/playlist/"* ]]; then
  fail "that is a VARIANT playlist (…/v1/playlist/<token>.m3u8).
         Take the CHANNEL/MASTER from the network tab instead — on IVS it looks like
         …playback.live-video.net/api/video/v1/….m3u8
         A variant can expire mid-call and the fetcher CANNOT re-resolve it (§V6)."
fi

# --- guard 4: pinned binary ---------------------------------------------------
PIN="$REPO/state/$EVENT/earshot-pinned"
[[ -x "$PIN" ]] || fail "no pinned binary at $PIN — run scripts/preflight-event.sh first"

# --- guard 5: onnxruntime dylib findable (streaming_ctc) ----------------------
# ⚠ PLATFORM-AWARE, learned live 2026-07-28 (Ford): `find … | head -1` picked
# the rsync'd macOS dylib on the Linux box, and dlopen of a Mach-O hangs the
# arm at exactly the same place as a missing lib — 'book snapshots live',
# silently, in live AND dry postures. Cost: the first ~9 min of the Ford call.
# The lib format MUST match the host: Linux ⇒ .so, Darwin ⇒ .dylib.
if grep -qE '^\s*engine\s*=\s*"streaming_ctc"' "$CONFIG"; then
  case "$(uname -s)" in
    Linux)  DYLIB="$(find "$REPO/vendor" -name 'libonnxruntime*.so*' 2>/dev/null | head -1)" ;;
    Darwin) DYLIB="$(find "$REPO/vendor" -name 'libonnxruntime*.dylib' 2>/dev/null | head -1)" ;;
    *)      DYLIB="" ;;
  esac
  [[ -n "$DYLIB" ]] || fail "config runs streaming_ctc but no HOST-PLATFORM libonnxruntime under vendor/
         ($(uname -s) needs $( [[ "$(uname -s)" == Darwin ]] && echo .dylib || echo .so );
         a wrong-platform lib HANGS SILENTLY at 'book snapshots live' — Ford 2026-07-28)."
  export ORT_DYLIB_PATH="$DYLIB"
fi

# --- guard 6: detector HEARTBEAT — build + run the real engine before arming --
# Path guards (5) prove a file exists; only actually decoding audio proves the
# engine works. `detector-smoke` builds the config's detector with the exact
# env exported above and pushes 3 s of audio through it. A wrong-platform
# dylib HANGS in dlopen (Ford 2026-07-28: guard 5's find|head-1 passed and the
# arm still went deaf for 9 min) — `timeout` turns that hang into exit 124.
SMOKE_OUT="$(run_with_timeout 90 "$PIN" detector-smoke --config "$CONFIG" 2>&1 | tail -1)" || {
  rc=$?
  [[ $rc -eq 124 ]] && fail "detector-smoke HUNG (>90 s) — dead/wrong-platform engine runtime. DO NOT ARM.
         (this is the Ford 2026-07-28 deaf-gap signature; check ORT_DYLIB_PATH=${ORT_DYLIB_PATH:-unset})"
  fail "detector-smoke failed (exit $rc): $SMOKE_OUT"
}
[[ "$SMOKE_OUT" == *"detector-smoke OK"* ]] || fail "detector-smoke did not report OK: $SMOKE_OUT"

# --- guard 7: launch time vs call start (arm T-20, never after the bell) -----
STARTS_AT="$(sed -n 's/^starts_at[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)"
if [[ -n "$STARTS_AT" ]]; then
  NOW_EPOCH="$(date -u +%s)"
  START_EPOCH="$(date -u -d "$STARTS_AT" +%s 2>/dev/null || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$STARTS_AT" +%s 2>/dev/null || echo 0)"
  if [[ "$START_EPOCH" -gt 0 ]]; then
    DELTA=$(( START_EPOCH - NOW_EPOCH ))
    if (( DELTA < 0 )); then
      echo "⚠⚠ LATE LAUNCH: call started $(( -DELTA / 60 )) min ago ($STARTS_AT) — arming anyway, every second counts" >&2
      bash scripts/notify.sh "ARM LATE: $EVENT" "run-arm.sh launched $(( -DELTA / 60 )) min after starts_at $STARTS_AT" --quiet || true
    elif (( DELTA < 1200 )); then
      echo "note: T-$(( DELTA / 60 ))min — inside the T-20 window, OK" >&2
    else
      echo "note: T-$(( DELTA / 60 ))min — early is fine; the arm idles until the stream goes live" >&2
    fi
  fi
fi

LOG="$HOME/${SLUG}-arm-$(date -u +%Y%m%dT%H%M%SZ).log"
POSTURE=$([[ "$LIVE" -eq 1 ]] && echo "LIVE (--i-am-live)" || echo "DRY RUN")

cat <<EOF
── arm ${SLUG} ──────────────────────────────────────────────
  event    : $EVENT
  config   : $CONFIG
  stream   : ${URL:0:78}
  pin      : $(sed -n 's/^sha256=//p' "$REPO/state/$EVENT/pinned.info" 2>/dev/null | cut -c1-16)…
  ort      : ${ORT_DYLIB_PATH:-n/a}
  posture  : $POSTURE
  log      : $LOG
─────────────────────────────────────────────────────────────
EOF

ARGS=(--config "$CONFIG" --account "$HOME/.kalshi/earshot_env" --bin "$PIN")
[[ "$LIVE" -eq 1 ]] && ARGS+=(--i-am-live)

# Plain shell / nohup — never a sandboxed supervisor (07-22 post-mortem #3: a
# sandboxed arm was hard-killed and no signal handler ran, so no report was
# written). arm's own SIGTERM path is the validated kill mechanism.
nohup bash scripts/launch-webcast.sh "${ARGS[@]}" > "$LOG" 2>&1 &
ARM_PID=$!
echo "armed (pid $ARM_PID) — tail -f $LOG"
echo "READ BACK the 'posture:' line in the log before the call starts."
echo "abort: touch $REPO/state/$EVENT/STOP   (or SIGTERM the arm)"

# --- deaf-watchdog (Ford 2026-07-28 bugs 1+5): alert if the pipeline never ---
# starts or a fanout leg dies at spawn. Detached; alerts via notify.sh, never
# kills — the operator decides. Three checks:
#   t+150s  pipeline started?  ('fanout: per-leg detect chains' or 'dry-run:'/
#           'ARMED' followed by detector init). Absence = the silent-hang class.
#   t+150s  any leg already dead? ('leg source finished' within the first 150 s
#           = Ford bug 5: pipe_tuned died 0.68 s in with frames=0, no alert).
#   every 60s until the call ends: arrival log stalled >90 s while the arm is
#           still up = fetcher deaf mid-call (LATENCY-PROOF §V6 class).
ARRIVAL_LOG="$HOME/${SLUG}-arrival.log"
[[ "$SLUG" == "f" ]] && ARRIVAL_LOG="$HOME/ford-arrival.log"
nohup bash -c '
  LOG="$1"; EVENT="$2"; ARM_PID="$3"; ARRIVAL="$4"; REPO="$5"
  sleep 150
  kill -0 "$ARM_PID" 2>/dev/null || { bash "$REPO/scripts/notify.sh" "ARM DIED EARLY: $EVENT" "arm pid $ARM_PID gone within 150 s — read $LOG" --quiet; exit 0; }
  if ! grep -qaE "fanout: per-leg detect chains|Loaded ONNX Runtime dylib|dry-run: orders" "$LOG"; then
    bash "$REPO/scripts/notify.sh" "ARM DEAF: $EVENT" "150 s after launch: no pipeline/detector-init line in $LOG — Ford deaf-gap signature. Kill and relaunch." --quiet
  fi
  if grep -qa "leg source finished" "$LOG"; then
    bash "$REPO/scripts/notify.sh" "ARM LEG DEAD: $EVENT" "a fanout leg exited within 150 s of launch (Ford bug 5: single-legged run). grep \"leg source finished\" $LOG" --quiet
  fi
  while kill -0 "$ARM_PID" 2>/dev/null; do
    sleep 60
    if [[ -f "$ARRIVAL" ]]; then
      AGE=$(( $(date +%s) - $(stat -c %Y "$ARRIVAL" 2>/dev/null || stat -f %m "$ARRIVAL") ))
      if (( AGE > 90 )); then
        bash "$REPO/scripts/notify.sh" "FETCHER STALLED: $EVENT" "$ARRIVAL silent for ${AGE}s while arm is up" --quiet
        sleep 240   # do not re-page every minute for the same stall
      fi
    fi
  done
' watchdog "$LOG" "$EVENT" "$ARM_PID" "$ARRIVAL_LOG" "$REPO" > "$HOME/${SLUG}-watchdog.log" 2>&1 &
echo "watchdog (pid $!) — alerts via notify.sh; log $HOME/${SLUG}-watchdog.log"
