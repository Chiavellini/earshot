#!/usr/bin/env bash
# Webcast-lane launcher: arm the pipeline on a config whose [[sources]] is
# `hls` or `pipe` (source::pipe decodes via ffmpeg/yt-dlp/streamlink). No
# tunnel, no Twilio, no dial — the subprocess pulls the stream itself.
#
#   scripts/launch-webcast.sh --config configs/<event>.toml --account <env> \
#       [--bin <pinned>] [--wait-live <url-or-url-file> --wait-kind youtube|hls]
#
# POSTURE. Default is DRY RUN (no orders reach Kalshi). `--i-am-live` opts into a
# real arm; `--fillable` is ACCEPTED (see the W7 FILL-HALF note at the flag
# itself) and requires `--i-am-live`.
#
# CORRECTED 2026-08-11: this line used to read "`--fillable` is still refused
# here", which the flag parser below has contradicted since 2026-08-05. The
# stale text is worth calling out rather than just deleting, because it fails in
# the expensive direction — read literally it says the webcast lane cannot fill,
# so an operator pricing a FILL posture would conclude the whole config is inert
# and go looking for a bug that is not there. The precision caveat W7 was built
# for DOES still stand and is UNRESOLVED (docs/BAKEOFF.md G4 is not scored, and
# the .readiness studies measure RECALL, not precision).
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

CONFIG="" ACCOUNT="" BIN="${EARSHOT_BIN:-}" WAIT_LIVE="" WAIT_KIND="" LIVE=0 FILLABLE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)    CONFIG="$2"; shift 2 ;;
    --account)   ACCOUNT="$2"; shift 2 ;;
    --bin)       BIN="$2"; shift 2 ;;
    --wait-live) WAIT_LIVE="$2"; shift 2 ;;
    --wait-kind) WAIT_KIND="$2"; shift 2 ;;
    --i-am-live) LIVE=1; shift ;;
    # W7 FILL-HALF LIFTED 2026-08-05 (operator ruling, for the 26AUG05 afternoon
    # slate). This arm previously refused --fillable outright, which — combined
    # with prepare writing max_price_cents=1 — meant the webcast lane could not
    # place an order at all: every intent short-circuited at executor.rs's
    # non-crossing branch and returned BEFORE send_order(). Four armed events on
    # 26AUG05 reported "23 orders" that never reached the wire.
    # The precision caveat W7 was built for still stands and is UNRESOLVED:
    # docs/BAKEOFF.md G4 is not scored, and the .readiness studies measure
    # RECALL, not precision. Money is bounded elsewhere — MENTION_FILL_CAP_CENTS
    # (a per-ORDER price bound), [risk].max_event_notional_cents, and the
    # per-event probe ledger.
    #
    # THE NUMBERS IN THIS NOTE WENT STALE TWICE; do not read them from here.
    # It said "50¢/order" (raised to 97 on 2026-08-05) and "the per-event $1
    # probe ledger — so a false fire costs at most that event's $1" (the ledger
    # went 100 → 2000 on 2026-08-10, then → 7000 on 2026-08-11 for tiered
    # sizing). "At most $1" understated the 26AUG11 exposure by ~70×. Read the
    # live values from src/risk.rs and the event's own [risk] block — a comment
    # is not a cap.
    --fillable)  FILLABLE="--fillable"; shift ;;
    # --fill-all stays refused: it is the tier override, a separate decision.
    --fill-all)
      echo "REFUSING $1: --fill-all overrides the vetted-word tier and is not part" >&2
      echo "of the 26AUG05 W7 ruling. Arm it deliberately by hand if you mean it." >&2
      exit 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
if [[ -n "$FILLABLE" && $LIVE -eq 0 ]]; then
  echo "--fillable requires --i-am-live (arm refuses it otherwise; fail here, not 8 min before a call)" >&2
  exit 2
fi
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
# 2026-08-05 rehearsal catch: a launchd-fired watcher reached the exec with no
# --bin and DIED on `exec: cargo: not found` (launchd's PATH has no ~/.cargo).
# When preflight pinned a binary for this event, run the pin — which is also
# what the runbook demands. Explicit --bin still wins.
if [[ -z "$BIN" && -x "${STATE_DIR}/earshot-pinned" ]]; then
  BIN="${STATE_DIR}/earshot-pinned"
  echo "pinned binary auto-selected: $BIN (preflight wrote it; cargo fallback never fires under launchd)"
fi
# Per-event log (2026-08-04): the final exec below inherits this redirect, so
# the arm's own stdout is captured per-event for the first time.
exec > >(tee -a "${STATE_DIR}/launch.log") 2>&1
jnl() { scripts/journal.sh --event "$EVENT_ID" --actor launch-webcast.sh --action "$1" --outcome "$2" --detail "${3:-}" --log "${STATE_DIR}/launch.log" || true; }

# ── Green gate (2026-08-04, marker-activated) ───────────────────────────────
# Enforces ONLY when a preflight verdict exists for this event. Override
# EARSHOT_GREEN_OVERRIDE="<reason>" proceeds but pages.
if [[ -f "${STATE_DIR}/preflight-result.json" ]]; then
  if GREEN_OUT=$(scripts/assert-ready.sh "$CONFIG" 2>&1); then
    echo "$GREEN_OUT"
  elif [[ -n "${EARSHOT_GREEN_OVERRIDE:-}" ]]; then
    echo "GREEN GATE OVERRIDDEN: $GREEN_OUT (reason: $EARSHOT_GREEN_OVERRIDE)"
    scripts/notify.sh "GREEN GATE OVERRIDDEN: $EVENT_ID" "$GREEN_OUT — override reason: $EARSHOT_GREEN_OVERRIDE" --quiet || true
    jnl note warn "green gate overridden: $EARSHOT_GREEN_OVERRIDE"
  else
    echo "REFUSED by green gate: $GREEN_OUT" >&2
    scripts/notify.sh "$EVENT_ID REFUSED by green gate" "$GREEN_OUT — fix and relaunch, or EARSHOT_GREEN_OVERRIDE=\"<reason>\" (pages)." --quiet || true
    jnl launch-start refused "$GREEN_OUT"
    exit 7
  fi
fi
jnl launch-start info "config=$CONFIG"

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
  if [[ -n "$FILLABLE" ]]; then
    LIVE_ARGS+=("$FILLABLE")
    # State the truth: this is the posture in which an order can actually FILL.
    # The old line claimed "orders WILL be sent" for the 1¢ probe, where in fact
    # nothing was ever sent — never describe a posture by its flag, only by what
    # it does at the wire.
    # Derive the bound from THIS config rather than repeating a literal that
    # can drift out of sync with risk.rs — a posture line that misstates the
    # cap is the exact defect class found on 26AUG05 (see the D2 note above).
    CFG_MAX_CAP=$(grep -oE '^max_price_cents[[:space:]]*=[[:space:]]*[0-9]+' "$CONFIG" \
                  | grep -oE '[0-9]+$' | sort -n | tail -1)
    CFG_MAX_SIZE=$(grep -oE '^budget_contracts[[:space:]]*=[[:space:]]*[0-9]+' "$CONFIG" \
                   | grep -oE '[0-9]+$' | sort -n | tail -1)
    POSTURE="LIVE mention-FILL (orders MAY cross the ask and FILL; max cap ${CFG_MAX_CAP:-?}¢ × ${CFG_MAX_SIZE:-?} contract(s) per order, spend bound by notional caps + per-event \$1 probe ledger)"
  else
    POSTURE="LIVE mention-probe (cap ≤ 1¢ non-crossing — an order reaches the wire ONLY if the ask is ≤ 1¢; otherwise it short-circuits unsent)"
  fi
else
  POSTURE="DRY RUN (no orders are sent; the risk gate is NOT exercised)"
fi
echo "webcast lane: event=${EVENT_ID}  config=${CONFIG}  state=${STATE_DIR}"
echo "posture: ${POSTURE}"

# ── AUDIO SIDE-CAR: always, not opt-in (2026-08-11) ────────────────────────
# `arm` banks NO audio and NO transcript, so PRECISION — the false-positive
# rate that gates position size — cannot be measured after the fact. The
# readiness study measures RECALL only, on clean studio TTS.
#
# 26AUG11 is why this is automatic now: CAVA+CRWV filled 5 orders / $48.50 at
# tranche 10 and banked no audio at all, so there is NO WAY to establish
# whether "catering"/"same store"/"acceleration"/"dining"/"government" were
# actually said. We bought positions we cannot audit. HIMS 26AUG10 WAS
# auditable (scored 9/9 true-positive) purely because a sidecar happened to be
# running that night.
#
# FAIL-OPEN BY CONSTRUCTION: every step below is `|| true`-guarded and the arm
# is exec'd regardless. A recorder must never be able to block a trade path.
# capture-bakeoff has no detector, no risk gate and no Kalshi connection, so it
# cannot place, cancel or influence an order — and this ADDS a recording to an
# armed run, which is not the "recorder instead of an arm" case CLAUDE.md bans.
if [[ "${EARSHOT_NO_SIDECAR:-0}" != "1" ]]; then
  SIDECAR_CFG="${STATE_DIR}/capture-sidecar.toml"
  # Copy a leg BYTE-FOR-BYTE out of the arm's own config, so the banked audio
  # is what the detector actually heard — not a second, differently-tuned pull.
  SIDECAR_SLUG=$(python3 - "$CONFIG" "$EVENT_ID" "$SIDECAR_CFG" <<'PYEOF' 2>/dev/null || true
import re, sys, tomllib
cfg_path, event_id, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = tomllib.load(open(cfg_path, "rb"))
srcs = [s for s in (cfg.get("sources") or []) if s.get("kind") == "pipe" and s.get("cmd")]
if not srcs:
    sys.exit(1)
src = next((s for s in srcs if s.get("label") == "pipe_tuned"), srcs[-1])
cmd = src["cmd"]
m = re.search(r'\$HOME/([A-Za-z0-9_-]+)-stream\.url', " ".join(cmd))
if not m:
    sys.exit(1)
starts = str((cfg.get("event") or {}).get("starts_at", ""))
body = "".join(f'  {json_cmd},\n' for json_cmd in (__import__("json").dumps(c) for c in cmd))
open(out_path, "w").write(
    "# AUTO-GENERATED by launch-webcast.sh — audio side-car for the run above.\n"
    "# One leg, copied byte-for-byte from the arm config's pipe leg, so the\n"
    "# banked audio represents what the detector actually heard.\n"
    f'[event]\nid        = "{event_id}-CAPTURE"\nstarts_at = "{starts}"\n\n'
    "[pipeline]\nchunk_ms = 80\n\n"
    '[[sources]]\nkind = "pipe"\nlabel = "sidecar"\nsample_rate_hz = 16000\ncmd = [\n'
    + body + "]\n")
print(m.group(1))
PYEOF
)
  if [[ -n "$SIDECAR_SLUG" && -f "$SIDECAR_CFG" ]]; then
    nohup scripts/capture-sidecar.sh --slug "$SIDECAR_SLUG" --config "$SIDECAR_CFG" \
        --record-dir "${STATE_DIR}/capture" --event "$EVENT_ID" \
        >> "${STATE_DIR}/capture-sidecar.log" 2>&1 &
    echo "audio side-car: recording to ${STATE_DIR}/capture (slug ${SIDECAR_SLUG}, pid $!)"
    jnl sidecar-start info "audio side-car recording to ${STATE_DIR}/capture" || true
  else
    # LOUD, because a silent skip is how tonight happened.
    echo "⚠ audio side-car NOT started (no pipe leg with a \$HOME/<slug>-stream.url in $CONFIG)" >&2
    echo "  → this run will be UNAUDITABLE: fills cannot be scored true/false afterwards." >&2
    jnl sidecar-start warn "side-car not started — run will be unauditable" || true
  fi
fi
if [[ -n "$BIN" ]]; then
  [[ -x "$BIN" ]] || { echo "pinned binary not executable: ${BIN}" >&2; exit 2; }
  jnl arm-start info "pinned $BIN — $POSTURE"
  exec "$BIN" arm --config "$CONFIG" --state-dir "$STATE_DIR" --report "$REPORT" --allow-mention ${LIVE_ARGS[@]+"${LIVE_ARGS[@]}"}
else
  jnl arm-start warn "cargo run (UNPINNED — the runbook forbids this for real events) — $POSTURE"
  exec cargo run --release -- arm --config "$CONFIG" --state-dir "$STATE_DIR" --report "$REPORT" --allow-mention ${LIVE_ARGS[@]+"${LIVE_ARGS[@]}"}
fi
