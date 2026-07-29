#!/usr/bin/env bash
# Unattended WING arm — the whole 2026-07-29 morning chain in one unit.
#
# Context: WING's call is 14:00 UTC = 08:00 CDMX, inside the human-absent
# window that silently dropped PYPL and BA on 07-28. Its published dial-in is
# NOT usable (operator-screened — VERIFICATIONS Q-OPS), so the webcast is the
# only path, and the webcast needs a URL that until now only a browser could
# produce. choruscall-resolve.py replaces the browser.
#
# Chain, with the failure of each step made LOUD rather than silent:
#   1. resolve  — register (Actinver identity, from EARSHOT_REG_*) and poll the
#                 player until it carries an m3u8; writes $HOME/wing-stream.url
#   2. arm      — scripts/run-arm.sh wing, which re-checks its own 7 guards
#                 (fresh URL, master-not-variant, pinned binary, ORT dylib,
#                 detector-smoke, starts_at) and REFUSES rather than half-arm
#   3. stop     — hard SIGTERM at the deadline. SIGTERM is the validated kill
#                 path (tests/kill_sigterm.rs, 2-3 ms); nobody is awake to send
#                 it by hand, and RUNBOOK shutdown bugs 2-3 are still open.
#
# Fail-closed by construction: if step 1 finds no m3u8 the arm NEVER starts and
# notify.sh pages. That is the intended outcome of the one unpinned risk here —
# no live Chorus Call event has ever been observed, so "the live player serves
# HLS" is inference (Q-CC). A wrong arm is not a possible outcome; a missed
# event is.
set -uo pipefail

REPO="${EARSHOT_REPO:-$HOME/ear}"
cd "$REPO"
SLUG=wing
WEBCASTID="${WING_WEBCASTID:-Cv4NzoL0}"
RESOLVE_DEADLINE_SECS="${WING_RESOLVE_SECS:-2700}"   # ~45 min: fires T-35, covers a late start
ARM_MAX_SECS="${WING_ARM_SECS:-5400}"                # 90 min: 60 min call + slack
LOG="$HOME/wing-autorun-$(date -u +%Y%m%dT%H%M%SZ).log"

exec > >(tee -a "$LOG") 2>&1
echo "=== wing-autorun $(date -u +%FT%TZ) ==="

note() { echo "[$(date -u +%T)] $*"; }
page() { "$REPO/scripts/notify.sh" "$1" "$2" --quiet || true; }

# The identity must come from the environment; it is never committed. The
# systemd unit sources ~/.earshot/registration.env.
for v in EARSHOT_REG_FIRST EARSHOT_REG_LAST EARSHOT_REG_EMAIL EARSHOT_REG_COMPANY; do
  if [[ -z "${!v:-}" ]]; then
    page "WING autorun: missing $v" "Registration identity incomplete; cannot resolve the stream. Event NOT armed."
    note "FATAL: $v unset"; exit 2
  fi
done

# --- step 1: resolve ---------------------------------------------------------
note "resolving $SLUG (webcastid=$WEBCASTID, deadline ${RESOLVE_DEADLINE_SECS}s)"
# A stale URL is the loaded gun run-arm guard 2 exists for; the .kind must go
# with it or we could arm the fallback leg against a fresh HLS URL.
rm -f "$HOME/$SLUG-stream.url" "$HOME/$SLUG-stream.kind"
# --allow-progressive: accept .mp4/.m4a as well as .m3u8. The venue serves all
# three off one field, ffmpeg reads all three, and a captured call on a
# non-ideal transport beats a missed one. The kind file tells us which we got.
if ! python3 scripts/choruscall-resolve.py \
      --webcastid "$WEBCASTID" --slug "$SLUG" \
      --register --allow-progressive \
      --wait-secs "$RESOLVE_DEADLINE_SECS" --poll-secs 10; then
  page "WING NOT ARMED — stream unresolved" \
       "choruscall-resolve produced no usable stream for WING before the deadline (neither HLS nor progressive). The call ran uncaptured. See $LOG"
  note "FATAL: resolve failed; NOT arming"; exit 3
fi
KIND="$(cat "$HOME/$SLUG-stream.kind" 2>/dev/null || echo hls)"
note "resolved [$KIND]: $(cat "$HOME/$SLUG-stream.url")"

# --- step 2: arm -------------------------------------------------------------
# run-arm.sh defaults to LIVE; posture stays 1c non-crossing observe, enforced
# in `arm` itself, and the $1 ledger clamps real spend regardless.
if [[ "$KIND" == "progressive" ]]; then
  # hls-edge-fetch.py speaks manifests, not progressive bodies — both primary
  # legs would die instantly. Swap to the single generic ffmpeg leg.
  export EARSHOT_ARM_CONFIG="configs/wing-progressive-arm.toml"
  export EARSHOT_ALLOW_NON_HLS=1
  note "PROGRESSIVE stream — arming fallback config $EARSHOT_ARM_CONFIG"
  page "WING on the progressive fallback" \
       "hlsSource was not HLS; arming configs/wing-progressive-arm.toml with a plain ffmpeg leg. Capture proceeds; latency is NOT comparable with HLS venues."
fi
note "arming (hard stop in ${ARM_MAX_SECS}s via SIGTERM)"
timeout --signal=TERM --kill-after=30 "$ARM_MAX_SECS" scripts/run-arm.sh "$SLUG"
rc=$?
case "$rc" in
  0)   note "arm exited cleanly"; page "WING run complete" "Clean exit. Log: $LOG" ;;
  124) note "arm stopped at the deadline (expected)"; page "WING run complete (deadline stop)" "SIGTERM at ${ARM_MAX_SECS}s. Log: $LOG" ;;
  *)   note "arm exited rc=$rc"; page "WING arm exited rc=$rc" "Investigate $LOG" ;;
esac
exit 0
