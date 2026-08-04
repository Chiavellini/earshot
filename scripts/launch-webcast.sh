#!/usr/bin/env bash
# Webcast-lane launcher: arm the pipeline on a config whose [[sources]] is
# `hls` or `pipe` (source::pipe decodes via ffmpeg/yt-dlp/streamlink). No
# tunnel, no Twilio, no dial — the subprocess pulls the stream itself.
#
#   scripts/launch-webcast.sh --config configs/<event>.toml --account <env> \
#       [--bin <pinned>] [--wait-live <url-or-url-file> --wait-kind youtube|hls]
#
# POSTURE. Default is DRY RUN (no orders reach Kalshi). `--i-am-live` opts into a
# real arm; `--fillable` is still refused here and stays gated on the Phase-4
# precision verdict (docs/BAKEOFF.md G4) — see W7 in docs/RACE-CHECKLIST.md.
#
# W7 LIFTED 2026-07-27 (operator ruling, for PayPal 07-28). The prior text read
# "this script deliberately accepts no --fillable/--i-am-live", which meant the
# RUNBOOK's own step-5 command produced a DRY RUN — so the 07-26 fix that makes
# `arm` refuse a partial allowlist (config::allowlist_covers_keywords) could never
# fire on the path the runbook told you to use, because dry-runs do not reach the
# risk gate. An event billed as "the live Δrace measurement" was going to place no
# orders at all.
#
# Live safety is enforced in `arm`, NOT duplicated here — one place, per CLAUDE.md:
#   - --i-am-live with an empty [risk].allowlist_tickers is REFUSED (main.rs)
#   - a non-empty allowlist that misses a keyword is REFUSED (allowlist_covers_keywords)
#   - --allow-mention alone is the mention-PROBE posture: cap must be ≤ 1¢ and
#     orders are non-crossing, so a live arm here still CANNOT fill.
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

CONFIG="" ACCOUNT="" BIN="${EARSHOT_BIN:-}" WAIT_LIVE="" WAIT_KIND="" LIVE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)    CONFIG="$2"; shift 2 ;;
    --account)   ACCOUNT="$2"; shift 2 ;;
    --bin)       BIN="$2"; shift 2 ;;
    --wait-live) WAIT_LIVE="$2"; shift 2 ;;
    --wait-kind) WAIT_KIND="$2"; shift 2 ;;
    --i-am-live) LIVE=1; shift ;;
    # Refused HERE rather than passed through, so the message names the gate
    # instead of surfacing as a generic arm error 8 minutes before a call.
    --fillable|--fill-all)
      echo "REFUSING $1: the webcast fill posture is gated on the Phase-4 precision" >&2
      echo "verdict (docs/BAKEOFF.md, G4 fill-tier gate — NOT yet scored). W7 lifted" >&2
      echo "--i-am-live only. To arm fillable you must do it deliberately by hand." >&2
      exit 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$CONFIG" && -n "$ACCOUNT" ]] || {
  echo "usage: $0 --config <cfg.toml> --account <env-file> [--bin <pinned>] [--i-am-live] [--wait-live <url-or-url-file> --wait-kind youtube|hls]" >&2; exit 2
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
    # Two bugs fixed 2026-08-03, both of which made this resolver a no-op on a
    # Mac: it searched ONLY '*.so*' (there is no .so on Darwin, so the find
    # always came up empty and the REFUSE below fired on a healthy tree), and it
    # rooted at $HOME/ear, which is the BOX layout — the laptop repo is
    # $HOME/Code/ear. Resolve per-platform, and search the real repo root.
    # -not -path '*dSYM*': the debug bundle holds a same-named file that dlopen
    # HANGS on (see run-arm.sh) — that decoy hung preflight's detector-smoke.
    _vend="${EARSHOT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}/vendor"
    case "$(uname -s)" in
      Darwin) _pat='libonnxruntime*.dylib' ;;
      *)      _pat='libonnxruntime*.so*'   ;;
    esac
    _ort=$(find "$_vend" -name "$_pat" -not -path '*dSYM*' 2>/dev/null | head -1 || true)
    if [[ -n "$_ort" ]]; then
      export ORT_DYLIB_PATH="$_ort"
    fi
  fi
  case "${ORT_DYLIB_PATH:-}" in
    *dSYM*) echo "REFUSING: ORT_DYLIB_PATH is a dSYM debug bundle, not a loadable library: $ORT_DYLIB_PATH" >&2; exit 2 ;;
  esac
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

# The posture line must state what this run ACTUALLY is. It previously printed
# "(observe posture)" unconditionally, which was true only because the flag could
# not be passed — the same class of defect as V6's health line that went silent
# exactly when the leg broke (docs/LATENCY-PROOF.md §V6).
LIVE_ARGS=()
if (( LIVE )); then
  LIVE_ARGS+=(--i-am-live)
  POSTURE="LIVE mention-probe (orders WILL be sent; cap ≤ 1¢, non-crossing, allowlist enforced by arm)"
else
  POSTURE="DRY RUN (no orders are sent; the risk gate is NOT exercised)"
fi
echo "webcast lane: event=${EVENT_ID}  config=${CONFIG}  state=${STATE_DIR}"
echo "posture: ${POSTURE}"
if [[ -n "$BIN" ]]; then
  [[ -x "$BIN" ]] || { echo "pinned binary not executable: ${BIN}" >&2; exit 2; }
  exec "$BIN" arm --config "$CONFIG" --state-dir "$STATE_DIR" --report "$REPORT" --allow-mention ${LIVE_ARGS[@]+"${LIVE_ARGS[@]}"}
else
  exec cargo run --release -- arm --config "$CONFIG" --state-dir "$STATE_DIR" --report "$REPORT" --allow-mention ${LIVE_ARGS[@]+"${LIVE_ARGS[@]}"}
fi
