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

case "$SLUG" in
  pypl) CONFIG=configs/webcast-pypl-26jul28-arm.toml; URLF=$HOME/pypl-stream.url; EVENT=KXEARNINGSMENTIONPYPL-26JUL28 ;;
  ba)   CONFIG=configs/webcast-ba-26jul28-arm.toml;   URLF=$HOME/ba-stream.url;   EVENT=KXEARNINGSMENTIONBA-26JUL21 ;;
  f)    CONFIG=configs/webcast-f-26jul28-arm.toml;    URLF=$HOME/f-stream.url;    EVENT=KXEARNINGSMENTIONF-26JUL28 ;;
  *) echo "usage: $0 {pypl|ba|f} [--dry] [--url-ok]" >&2; exit 2 ;;
esac

cd "$(dirname "$0")/.."
REPO="$PWD"
fail() { echo "REFUSING: $*" >&2; exit 1; }

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
if grep -qE '^\s*engine\s*=\s*"streaming_ctc"' "$CONFIG"; then
  DYLIB="$(find "$REPO/vendor" -name 'libonnxruntime*.so*' -o -name 'libonnxruntime*.dylib' 2>/dev/null | head -1)"
  [[ -n "$DYLIB" ]] || fail "config runs streaming_ctc but no libonnxruntime found under vendor/ —
         the arm would HANG SILENTLY at 'book snapshots live'."
  export ORT_DYLIB_PATH="$DYLIB"
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
echo "armed (pid $!) — tail -f $LOG"
echo "READ BACK the 'posture:' line in the log before the call starts."
echo "abort: touch $REPO/state/$EVENT/STOP   (or SIGTERM the arm)"
