#!/usr/bin/env bash
# Webcast-lane launcher: arm the pipeline on a config whose [[sources]] is
# `hls` or `pipe` (source::pipe decodes via ffmpeg/yt-dlp/streamlink). No
# tunnel, no Twilio, no dial — the subprocess pulls the stream itself.
#
#   scripts/launch-webcast.sh --config configs/<event>.toml --account <env> \
#       [--bin <pinned>] [--wait-live <url-or-url-file> --wait-kind youtube|hls]
#
# POSTURE: capture/observe only — this script deliberately accepts no
# --fillable/--i-am-live. Lift only per the fill-posture gates in
# docs/RACE-STRATEGY.md.
#
# LIVE GATE (07-22 ops post-mortems #1/#2, docs/RUNBOOK.md): the wait for the
# stream to go live happens HERE, before arm ever starts:
#   #1 `yt-dlp --wait-for-video` stalls at the pre-live→live flip — so no
#      armed-leg cmd may carry it; this script polls `is_live` and launches
#      on the flip instead.
#   #2 a liveness gate INSIDE a pipe-source cmd emits no bytes and trips the
#      pipeline's 30 s idle guard — so the gate must run OUTSIDE the arm.
# Post-mortem #3 (sandboxed arm hard-killed, no signal handler ran): run this
# script from a PLAIN shell / nohup — never inside a sandboxed supervisor.
# arm's own SIGTERM path is the validated kill mechanism.
#
# Abort: Ctrl-C (arm's kill watcher cancels session orders + writes the
# report), or `touch state/<event>/STOP`.
set -euo pipefail

CONFIG="" ACCOUNT="" BIN="${EARSHOT_BIN:-}" WAIT_LIVE="" WAIT_KIND=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)    CONFIG="$2"; shift 2 ;;
    --account)   ACCOUNT="$2"; shift 2 ;;
    --bin)       BIN="$2"; shift 2 ;;
    --wait-live) WAIT_LIVE="$2"; shift 2 ;;
    --wait-kind) WAIT_KIND="$2"; shift 2 ;;
    *) echo "unknown arg: $1 (this launcher takes no live/fillable flags by design)" >&2; exit 2 ;;
  esac
done
[[ -n "$CONFIG" && -n "$ACCOUNT" ]] || {
  echo "usage: $0 --config <cfg.toml> --account <env-file> [--bin <pinned>] [--wait-live <url-or-url-file> --wait-kind youtube|hls]" >&2; exit 2
}
[[ -f "$CONFIG" ]] || { echo "config not found: $CONFIG" >&2; exit 2; }
[[ -f "$ACCOUNT" ]] || { echo "account env not found: $ACCOUNT" >&2; exit 2; }

for tool in ffmpeg; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing tool: $tool" >&2; exit 1; }
done
# yt-dlp/streamlink only matter for configs that use them; warn, don't fail.
command -v yt-dlp >/dev/null 2>&1 || echo "note: yt-dlp not found — YouTube pipe configs will fail" >&2
command -v streamlink >/dev/null 2>&1 || echo "note: streamlink not found — streamlink legs will fail" >&2

# streaming_ctc loads onnxruntime DYNAMICALLY. With ORT_DYLIB_PATH unset the
# detector fails to build and the arm dies at startup — and a launchd-fired run
# has a minimal env, which is exactly the class of failure CLAUDE.md warns about
# (an interactively-validated path is not a validated path). Resolve it HERE so
# the launcher works identically from a shell and from launchd, and REFUSE
# before the call rather than failing 0 s into it.
if grep -qE '^[[:space:]]*engine[[:space:]]*=[[:space:]]*"streaming_ctc"' "$CONFIG"; then
  if [[ -z "${ORT_DYLIB_PATH:-}" ]]; then
    # NOT `[[ -n "$_ort" ]] && export ...` — under `set -e` that compound
    # returns 1 when the find comes up empty and kills the script SILENTLY,
    # before the REFUSING message below can explain why. Same family as the
    # pipefail traps in docs/RUNBOOK.md. Use an if.
    _ort=$(find "${EARSHOT_ROOT:-$HOME/ear}/vendor" -name 'libonnxruntime*.so*' 2>/dev/null | head -1 || true)
    if [[ -n "$_ort" ]]; then
      export ORT_DYLIB_PATH="$_ort"
    fi
  fi
  [[ -n "${ORT_DYLIB_PATH:-}" && -f "${ORT_DYLIB_PATH}" ]] || {
    echo "REFUSING: config uses engine=streaming_ctc but ORT_DYLIB_PATH is unset/invalid." >&2
    echo "  run scripts/fetch-models.sh (it fetches the dylib and prints the export), or" >&2
    echo "  export ORT_DYLIB_PATH=/path/to/libonnxruntime.so before launching." >&2
    exit 2
  }
  echo "streaming_ctc: ORT_DYLIB_PATH=${ORT_DYLIB_PATH}"
fi

# Armed-leg cmds must not gate on liveness themselves (post-mortems #1/#2).
if grep -q "wait-for-video" "$CONFIG"; then
  echo "REFUSING: $CONFIG contains --wait-for-video in a source cmd — the live gate" >&2
  echo "belongs to this script (--wait-live), never inside the arm (RUNBOOK 07-22 #1/#2)" >&2
  exit 2
fi

EVENT_ID=$(sed -n '/^\[event\]/,/^\[/s/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
[[ -n "$EVENT_ID" ]] || { echo "no [event].id in $CONFIG" >&2; exit 2; }
STATE_DIR="state/${EVENT_ID}"
mkdir -p "$STATE_DIR"
REPORT="${STATE_DIR}/report.json"

# Self-check for post-mortem #3: prove this shell can trap SIGTERM and write
# the state dir — a sandboxed parent that swallows signals or blocks writes
# fails HERE, before the event, not 60 s into the call.
trap 'true' TERM
touch "${STATE_DIR}/.launch-selfcheck" 2>/dev/null || {
  echo "REFUSING: cannot write ${STATE_DIR} — launch from a plain shell (post-mortem #3)" >&2; exit 2
}
rm -f "${STATE_DIR}/.launch-selfcheck"
trap - TERM

# --wait-live: poll until the stream is actually live, THEN arm (launch on
# the flip; arm bring-up is ~8 s, cheap next to a pre-live stall).
if [[ -n "$WAIT_LIVE" ]]; then
  [[ "$WAIT_KIND" == "youtube" || "$WAIT_KIND" == "hls" ]] || {
    echo "--wait-live requires --wait-kind youtube|hls" >&2; exit 2
  }
  # Accept a URL file (the run-<ev> pattern) or a literal URL.
  URL="$WAIT_LIVE"
  [[ -f "$WAIT_LIVE" ]] && URL="$(cat "$WAIT_LIVE")"
  [[ -n "$URL" ]] || { echo "REFUSING: --wait-live target is empty" >&2; exit 2; }
  echo "live gate: polling ($WAIT_KIND) every 15 s — arm launches on the flip"
  while :; do
    case "$WAIT_KIND" in
      youtube)
        # RUNBOOK 07-22 fix #1: poll is_live; --wait-for-video is unvalidated
        # at the pre-live→live transition.
        LIVE=$(yt-dlp --js-runtimes node --print "%(is_live)s" "$URL" 2>/dev/null || true)
        [[ "$LIVE" == "True" ]] && break
        ;;
      hls)
        # Live = manifest resolves and lists at least one media segment.
        BODY=$(curl -fsS --max-time 10 "$URL" 2>/dev/null || true)
        if [[ -n "$BODY" ]]; then
          if grep -q "EXTINF" <<<"$BODY"; then
            break
          fi
          # Master playlist: follow the first variant. tr strips CRLF —
          # real-world manifests (DW/Akamai) end lines with \r\n and a
          # trailing \r makes curl reject the URL as malformed.
          VARIANT=$(grep -v '^#' <<<"$BODY" | head -1 | tr -d '\r')
          if [[ -n "$VARIANT" ]]; then
            [[ "$VARIANT" != http* ]] && VARIANT="${URL%/*}/$VARIANT"
            # Capture, then grep: `curl | grep -q` SIGPIPEs curl on first
            # match, and under pipefail the pipeline rc goes nonzero — the
            # break silently never fires (found in the 07-23 dress rehearsal).
            VBODY=$(curl -fsS --max-time 10 "$VARIANT" 2>/dev/null || true)
            if grep -q "EXTINF" <<<"$VBODY"; then
              break
            fi
          fi
        fi
        ;;
    esac
    sleep 15
  done
  echo "live gate: stream is LIVE ($(date -u +%FT%TZ)) — arming now"
fi

# shellcheck disable=SC1090
source "$ACCOUNT"

echo "webcast lane: event=${EVENT_ID}  config=${CONFIG}  state=${STATE_DIR}  (observe posture)"
if [[ -n "$BIN" ]]; then
  [[ -x "$BIN" ]] || { echo "pinned binary not executable: ${BIN}" >&2; exit 2; }
  exec "$BIN" arm --config "$CONFIG" --state-dir "$STATE_DIR" --report "$REPORT" --allow-mention
else
  exec cargo run --release -- arm --config "$CONFIG" --state-dir "$STATE_DIR" --report "$REPORT" --allow-mention
fi
