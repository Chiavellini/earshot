#!/usr/bin/env bash
# Night-before preflight for an unattended event run (docs/RUNBOOK.md —
# REQUIRED before every scheduled event; born from the 2026-07-15 audit).
#
#   scripts/preflight-event.sh --config configs/<event>.toml --account ~/.earshot/<acct>.env \
#       [--fire-hhmm 0605]   # scheduled fire time, for the pmset wake check
#
# What it does:
#   1. Builds --release from the CURRENT checkout and PINS the binary into
#      the event state dir (earshot-pinned + pinned.info with commit+sha256).
#      ms-autorun/launch-event run the pin — never a fire-time compile.
#   2. Verifies every environment assumption the 06:05 launchd context makes:
#      disk headroom, credentials (Kalshi + Twilio live probes), ngrok agent,
#      required tools, timezone, scheduled wake, mailer.
#   3. Enforces the BLOCKING keyword-readiness gate (2026-07-29): refuses to
#      pass unless configs/.readiness/<basename>.json exists, hash-matches the
#      config's [[keywords]], is ≤7 days old, and confirms every keyword ≥1.
#      `--only-readiness` runs just that gate (dev/fixture testing; no build,
#      no probes, no mail, no state dir).
# Exit nonzero on any FAIL; a PREFLIGHT email reports the full checklist.
set -uo pipefail

CONFIG="" ACCOUNT="" FIRE_HHMM="" EVENT_ENV="" ONLY_READINESS=0 RESULT_JSON=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)    CONFIG="$2"; shift 2 ;;
    --account)   ACCOUNT="$2"; shift 2 ;;
    --fire-hhmm) FIRE_HHMM="$2"; shift 2 ;;
    --event-env) EVENT_ENV="$2"; shift 2 ;;
    --result-json) RESULT_JSON="$2"; shift 2 ;;  # structured verdict artifact (Phase D door); default state/<EVENT_ID>/preflight-result.json
    --only-readiness) ONLY_READINESS=1; shift ;;  # dev: run ONLY the keyword-readiness gate (no build/probes/mail)
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
if [[ "$ONLY_READINESS" == 1 ]]; then
  [[ -n "$CONFIG" ]] || { echo "usage: $0 --config <cfg.toml> --only-readiness" >&2; exit 2; }
else
  [[ -n "$CONFIG" && -n "$ACCOUNT" ]] || {
    echo "usage: $0 --config <cfg.toml> --account <env-file> [--fire-hhmm 0605]" >&2
    echo "          [--event-env configs/<event>-autorun.env]   # dial-in go/no-go gate" >&2
    echo "          [--only-readiness]   # dev: keyword-readiness gate only, nothing else" >&2
    exit 2
  }
fi

FAILS=0
WARNS=0
REPORT_LINES=()

# GNU timeout exists on the box; the CDMX laptop has neither timeout nor
# gtimeout — fall back to a background-killer that reports 124 like timeout.
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
check() {  # check <PASS|FAIL|WARN> <message>
  local verdict="$1" msg="$2"
  REPORT_LINES+=("[$verdict] $msg")
  echo "[$verdict] $msg"
  [[ "$verdict" == FAIL ]] && FAILS=$((FAILS+1))
  [[ "$verdict" == WARN ]] && WARNS=$((WARNS+1))
  return 0
}

# ── Keyword readiness gate (STANDING policy 2026-07-29, made BLOCKING here) ─
# WING lost two live races (CITR, AI) to keyword gaps that were FLAGGED in the
# config ("corpus-UNVALIDATED", "⚠ review variants") — and nothing refused to
# arm. The study already existed (scripts/variant-study.sh: TTS through the
# REAL detector); this gate makes it load-bearing: no arm without a fresh,
# hash-matching, all-confirmed study artifact in configs/.readiness/.
# keywords_sha256 is computed by scripts/keywords-extract.py on BOTH sides
# (artifact writer and this gate), so the two extractions can never drift.
# Defined as a function so `--only-readiness` can exercise it in isolation.
readiness_gate() {
  local ready n_kw kw_sha
  ready="configs/.readiness/$(basename "$CONFIG" .toml).json"
  kw_sha=$(python3 "$(dirname "$0")/keywords-extract.py" "$CONFIG" --sha256 2>/dev/null)
  n_kw=$(python3 "$(dirname "$0")/keywords-extract.py" "$CONFIG" 2>/dev/null | grep -c . || true)
  if [[ -z "$kw_sha" ]]; then
    check FAIL "keyword-readiness gate could not extract [[keywords]] from $CONFIG (scripts/keywords-extract.py failed)"
    return
  fi
  if [[ "${n_kw:-0}" -eq 0 ]]; then
    check WARN "no [[keywords]] in config — keyword-readiness gate not applicable (capture-only config?)"
    return
  fi
  if [[ ! -f "$ready" ]]; then
    check FAIL "keyword readiness study MISSING: $ready — every armed word must pass the TTS→detector study (WING 2026-07-29 lost CITR+AI to flagged-but-unenforced gaps). Fix: scripts/variant-study.sh $CONFIG   (needs macOS 'say' — run it on the laptop, NOT the box)"
    return
  fi
  local verdict
  verdict=$(READY="$ready" KW_SHA="$kw_sha" CFG="$CONFIG" python3 - <<'PY'
import json, os, sys
from datetime import date, datetime

path, want_sha, cfg = os.environ["READY"], os.environ["KW_SHA"], os.environ["CFG"]
fix = f"re-run: scripts/variant-study.sh {cfg} (macOS 'say' — laptop, not the box)"
try:
    a = json.load(open(path))
except Exception as e:
    print(f"FAIL readiness artifact unreadable ({e}) — {fix}")
    sys.exit(0)
got_sha = str(a.get("keywords_sha256", ""))
if got_sha != want_sha:
    print(f"FAIL keywords CHANGED since the study (config sha {want_sha[:12]}… != studied {got_sha[:12]}…) — the study proves nothing about the new set; {fix}")
    sys.exit(0)
try:
    age = (date.today() - datetime.strptime(a.get("date", ""), "%Y-%m-%d").date()).days
except ValueError:
    age = 9999
if age > 7:
    print(f"FAIL readiness study STALE (dated {a.get('date', '?')}, {age}d ago > 7d) — {fix}")
    sys.exit(0)
dead = [k.get("ticker", "?").split("-")[-1] for k in a.get("keywords", []) if not k.get("confirmed")]
if dead:
    # FAIL-unless-ACKED, mirroring the short-token gate above (2026-08-05).
    # Rationale for the escape hatch: dropping a dead word forfeits ONE market,
    # but refusing the whole event forfeits ALL of them — and on 2026-08-05 a
    # single stubborn word (`govtech`, still 0 after its variants were widened)
    # would have cost an entire 13-market board while the operator was away.
    # The WING lesson (never arm an UNSTUDIED word) is untouched: the study
    # still must exist, hash-match, and be fresh. What is ackable is the
    # narrower, evidenced claim "I know these specific words are dead and I
    # accept their markets are unserved". The ack must NAME every dead token,
    # so a new dead word cannot hide behind an old acknowledgement.
    ack_path = f"configs/.acks/{os.path.basename(cfg)[:-5]}-dead-word.ack"
    try:
        ack_text = open(ack_path).read()
        ack_age = (date.today()
                   - date.fromtimestamp(os.path.getmtime(ack_path))).days
    except OSError:
        ack_text, ack_age = "", 9999
    unacked = [d for d in dead if d not in ack_text]
    if unacked or ack_age > 7:
        why = (f"ack is {ack_age}d old (>7d)" if not unacked
               else f"not named in the ack: {' '.join(unacked)}")
        print(f"FAIL {len(dead)} keyword(s) scored ZERO Confirmed in the study: {' '.join(dead)} — a word that fails clean studio TTS will NOT fire live; diagnose (scripts/dead-word-autopsy.sh), fix variants, then {fix}. To arm the REST of the board knowingly, name every dead token in {ack_path} ({why}).")
        sys.exit(0)
    live = len(a.get("keywords", [])) - len(dead)
    print(f"PASS keyword readiness with {len(dead)} ACKED dead word(s) ({' '.join(dead)}, ack {ack_age}d old) — {live} live keywords; those markets are knowingly UNSERVED, do not read their silence as detector success")
    sys.exit(0)
n = len(a.get("keywords", []))
print(f"PASS keyword readiness: {n} keywords, all confirmed (study {a.get('date')}, engine {a.get('engine', '?')}, voices {'+'.join(a.get('voices', []))})")

# WEAK-WORD WARNING (PLTR 2026-08-03). `nvidia` was the event's ONE true detection
# miss — 37c pre-call, the only place real edge existed — and it passed this gate
# silently. confirmed==0 is a FAIL above; a low-but-nonzero score is NOT a fail,
# because dropping a keyword forfeits its market and that is a trading decision,
# not a preflight one. But it must not be invisible.
#
# The metric is confirmed/clips, NOT raw confirmed. Raw counts are not comparable
# across studies: the 2026-08-04 carrier-sentence change tripled clips per variant
# and moved absolute counts by ~3x while the underlying detectability was
# unchanged. The ratio is stable under that change. Reference point: NVID scored
# 0.33 (2 of 6). Anything at or below that band deserves eyes before arming.
WEAK_RATIO = 0.5
weak = []
for k in a.get("keywords", []):
    c, n = int(k.get("confirmed") or 0), int(k.get("clips") or 0)
    if c > 0 and n > 0 and c / n <= WEAK_RATIO:
        weak.append((k.get("ticker", "?").split("-")[-1], k.get("phrase", "?"), c, n, c / n))
weak.sort(key=lambda t: t[4])
if weak:
    detail = ", ".join(f"{t} \"{p}\" {c}/{n}={r:.2f}" for t, p, c, n, r in weak)
    print(f"WARN {len(weak)} WEAK keyword(s) — confirmed/clips <= {WEAK_RATIO} in the study, "
          f"materially likelier to be missed live (PLTR NVID scored 0.33 and was that event's "
          f"only real edge, lost): {detail} — add variants and re-run the study, or accept the risk knowingly")

# OVER-FIRING (added 2026-08-04). The same ratio read from ABOVE. A keyword that
# fires many times on clips holding ONE utterance each is assembling from pieces
# rather than hearing the word — the mechanism behind GRAB `commission` firing on
# audio that said "moats ... competitive advantage".
#
# HONEST LIMITS, measured before shipping this — do not over-read a clean run:
#   * It separates `commission` (2.67, GRAB cohort top) and nothing else.
#   * It MISSES `robot` (1.42, at the GRAB median of 1.38) and `game` (1.50,
#     BELOW four clean SNAP words: storage/china 2.00, live stream 1.83,
#     family center 1.67). Both were live false positives.
#   * `headwind` (1.50) ranks 5th highest and was a RECALL failure, so the ratio
#     also conflates over-firing with variant count (confirmed sums per variant).
#   * It is METHOD-DEPENDENT: carrier-sentence studies (2026-08-04 onward) read
#     0.11-4.78 with CLEAN words at 3.7-4.8, so the legacy constant is nonsense
#     there. Hence the split below and no absolute carrier-era number yet.
# The durable discriminator is max Confirmed fires on a SINGLE clip (one
# utterance per clip, so >=3 fires is the assembly window seen directly). That
# field does not exist in the artifact yet; when variant-study.sh emits
# max_fires_per_clip, gate on it and retire the ratio read.
OVERFIRE_RATIO_LEGACY = 2.0   # PRE-REGISTERED 2026-08-04. Pre-carrier studies only.
# Branch on the artifact's own method marker, never on its date: the first
# carrier study ran 2026-08-03 UTC, so a date cutoff mis-classifies it and
# flags every clean word at the legacy bar (observed on AMD before this fix).
# Artifacts written before the marker existed have no key -> legacy, correctly.
carriers_n = int(a.get("carriers") or 0)
ratios = [(int(x.get("confirmed") or 0) / int(x.get("clips") or 1))
          for x in a.get("keywords", []) if int(x.get("clips") or 0) > 0]
over = []
if carriers_n == 0:
    bar, basis = OVERFIRE_RATIO_LEGACY, f"confirmed/clips > {OVERFIRE_RATIO_LEGACY} (legacy bare-clip study)"
elif len(ratios) >= 10:
    # Top decile OF THIS STUDY. Relative, because no absolute carrier-era bar is
    # defensible yet: clean AMD words read 3.7-4.8. Calibration data, not a verdict.
    bar = sorted(ratios)[int(len(ratios) * 0.9)]
    basis = (f"top decile of THIS carrier study (n={len(ratios)}, cut {bar:.2f}) — no absolute "
             f"carrier-era bar is defensible yet; this is calibration data, not a verdict")
else:
    bar, basis = None, ""
if bar is not None:
    for k in a.get("keywords", []):
        c, n = int(k.get("confirmed") or 0), int(k.get("clips") or 0)
        if n > 0 and c / n > bar:
            over.append((k.get("ticker", "?").split("-")[-1], k.get("phrase", "?"), c, n, c / n))
over.sort(key=lambda t: -t[4])
if over:
    detail = ", ".join(f"{t} \"{p}\" {c}/{n}={r:.2f}" for t, p, c, n, r in over)
    print(f"WARN {len(over)} OVER-FIRING candidate(s) — {basis}: {detail} — "
          f"NOTE this ratio caught GRAB \"commission\" (2.67) but does NOT separate "
          f"\"robot\"/\"game\", which were also live false positives; treat as a prompt to "
          f"inspect, not as coverage")
PY
  )
  # The gate may emit a verdict line plus optional WARN lines; handle each.
  local vline
  vline=$(printf '%s\n' "$verdict" | head -1)
  case "$vline" in
    PASS*) check PASS "${vline#PASS }" ;;
    FAIL*) check FAIL "${vline#FAIL }" ;;
    *)     check FAIL "keyword-readiness gate could not run: ${vline:-empty verdict}" ;;
  esac
  while IFS= read -r extra; do
    [[ "$extra" == WARN* ]] && check WARN "${extra#WARN }"
  done < <(printf '%s\n' "$verdict" | tail -n +2)
}

# Dev short-circuit: gate-only run for fixture testing / quick iteration.
# Deliberately BEFORE the event-identity block so it creates no state dirs,
# builds nothing, probes nothing, and mails nothing.
if [[ "$ONLY_READINESS" == 1 ]]; then
  if [[ ! -f "$CONFIG" ]]; then
    check FAIL "config not found: $CONFIG"
  else
    readiness_gate
  fi
  [[ "$FAILS" -eq 0 ]] || exit 1
  exit 0
fi

# ── Event identity ─────────────────────────────────────────────────────────
if [[ -f "$CONFIG" ]]; then
  EVENT_ID=$(sed -n '/^\[event\]/,/^\[/s/^id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
  if [[ -n "$EVENT_ID" ]]; then
    check PASS "config $CONFIG → event $EVENT_ID"
  else
    check FAIL "config $CONFIG has no parseable [event].id"
  fi
else
  check FAIL "config not found: $CONFIG"
fi
STATE_DIR="state/${EVENT_ID:-preflight-unknown}"
mkdir -p "$STATE_DIR"

# ⚠ PORTABILITY (fixed 2026-07-27). This script was written for the CDMX macOS
# laptop. Run unchanged on the us-east Linux box it produced FIVE failures, of
# which FOUR were false — ngrok (not used on Linux, as this script itself says
# 130 lines below), `df -g` (a macOS flag), `cargo` (not on a non-interactive
# PATH), and Twilio creds (irrelevant to a webcast-only event). A gate that
# cries wolf four times out of five trains the operator to ignore the fifth.

# ── Tools the launch chain shells out to ───────────────────────────────────
TOOLS=(curl nc lsof python3 git shasum)
# ngrok is the CDMX tunnel path only; on Linux the WS endpoint is direct.
[[ "$(uname -s)" == "Darwin" ]] && TOOLS+=(ngrok)
for tool in "${TOOLS[@]}"; do
  if command -v "$tool" >/dev/null 2>&1; then
    check PASS "tool: $tool ($(command -v "$tool"))"
  else
    check FAIL "tool MISSING: $tool"
  fi
done

# ── Disk ───────────────────────────────────────────────────────────────────
# `df -g` is macOS. `df -k` is POSIX and works on both.
avail_gb=$(df -k / | awk 'NR==2 {printf "%d", $4/1048576}')
if [[ "${avail_gb:-0}" -ge 5 ]]; then
  check PASS "disk: ${avail_gb} GiB free (>= 5)"
else
  check FAIL "disk: only ${avail_gb:-?} GiB free (< 5) — free space before event day (target/debug is safe to delete; NEVER target/release)"
fi

# ── Build + pin ────────────────────────────────────────────────────────────
# The box is rsync-deployed, so it is NOT a git checkout. That must be a loud
# failure, not a silent pass: the whole point of the pin is "this binary is
# THAT commit", and the previous code printed `git tree clean at ` with an
# EMPTY commit and scored it PASS — a check passing vacuously, the same defect
# class as the placeholder allowlist caught earlier today.
if git rev-parse --git-dir >/dev/null 2>&1; then
  HEAD_SHA=$(git rev-parse HEAD)
  dirty=$(git status --porcelain | wc -l | tr -d ' ')
  [[ "$dirty" == 0 ]] && check PASS "git tree clean at $(git rev-parse --short HEAD)" \
                      || check WARN "git tree has $dirty dirty paths — the pin records what is BUILT, review before trusting"
else
  HEAD_SHA="UNKNOWN-NOT-A-GIT-CHECKOUT"
  dirty="n/a"
  check WARN "NOT a git checkout (rsync-deployed host) — the pin CANNOT record a commit. Provenance is the rsync source, so verify the laptop tree is committed and clean before trusting this pin."
fi
# cargo lives in ~/.cargo/bin, which a NON-INTERACTIVE shell does not get: the
# profile that adds it is skipped. This is the same minimal-env class as the
# ORT_DYLIB_PATH trap, and it made the build look broken when it was fine.
[[ -d "$HOME/.cargo/bin" ]] && export PATH="$HOME/.cargo/bin:$PATH"
if ! command -v cargo >/dev/null 2>&1; then
  check FAIL "cargo not on PATH (looked in ~/.cargo/bin) — cannot build or pin"
fi
if cargo build --release 2>&1 | tail -1 | grep -q "Finished"; then
  cp target/release/earshot "$STATE_DIR/earshot-pinned"
  {
    echo "commit=$HEAD_SHA"
    echo "dirty_paths=$dirty"
    echo "sha256=$(shasum -a 256 "$STATE_DIR/earshot-pinned" | awk '{print $1}')"
    echo "built=$(date "+%Y-%m-%d %H:%M:%S %z")"
  } > "$STATE_DIR/pinned.info"
  check PASS "pinned binary: $STATE_DIR/earshot-pinned ($(grep sha256 "$STATE_DIR/pinned.info" | cut -c1-23)…)"
  # Follow-up 6 gate: arm refuses the WHOLE config over one detector-
  # unencodable variant. Exercise the exact encode path with the pin (offline).
  #
  # ⚠ --model-dir is LOAD-BEARING, not tidiness. compile-keywords resolves the
  # FIRST detector block's model_dir, and the armed configs deliberately keep a
  # [detector.sherpa_kws] block so the engine can be reverted with a one-line
  # edit. Without this, a streaming_ctc config is validated against the
  # GIGASPEECH vocab — a keyword outside the CTC vocab would never fire and this
  # check would report PASS. Hit by hand on 2026-07-25; pinned as a RUNBOOK trap.
  ENGINE=$(sed -n '/^\[detector\]/,/^\[/s/^engine[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)
  MODEL_DIR=$(sed -n "/^\[detector\.${ENGINE}\]/,/^\[/s/^model_dir[[:space:]]*=[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$CONFIG" | head -1)
  if [[ -n "$ENGINE" && -n "$MODEL_DIR" ]]; then
    check PASS "detector engine=$ENGINE → model_dir=$MODEL_DIR"
    CK_OUT=$("$STATE_DIR/earshot-pinned" compile-keywords --config "$CONFIG" --model-dir "$MODEL_DIR" 2>&1)
    CK_RC=$?
  else
    check FAIL "could not resolve [detector].engine / its model_dir from $CONFIG — cannot validate the RIGHT vocab"
    CK_OUT=$("$STATE_DIR/earshot-pinned" compile-keywords --config "$CONFIG" 2>&1)
    CK_RC=$?
  fi
  # Detector HEARTBEAT with the pin (Ford 2026-07-28 bug 1): compile-keywords
  # proves the vocab, not the runtime — a wrong-platform onnxruntime dylib
  # still hangs at arm. detector-smoke builds the engine and decodes 3 s of
  # audio under the SAME env the arm will use; run it the night before so a
  # dead runtime is a tonight problem, not a T-20 problem.
  if [[ "$ENGINE" == "streaming_ctc" && -z "${ORT_DYLIB_PATH:-}" ]]; then
    case "$(uname -s)" in
      # -not -path '*dSYM*' — see run-arm.sh: the .dSYM bundle holds a file with
      # the identical basename that dlopen HANGS on. Without this exclusion the
      # find is order-dependent, and on 2026-08-03 it picked the decoy and this
      # very check reported "detector-smoke HUNG (>90 s)" for a healthy tree.
      Linux)  ORT_DYLIB_PATH="$(find vendor -name 'libonnxruntime*.so*'   -not -path '*dSYM*' 2>/dev/null | head -1)" ;;
      Darwin) ORT_DYLIB_PATH="$(find vendor -name 'libonnxruntime*.dylib' -not -path '*dSYM*' 2>/dev/null | head -1)" ;;
    esac
    export ORT_DYLIB_PATH
  fi
  SMOKE_OUT=$(run_with_timeout 90 "$STATE_DIR/earshot-pinned" detector-smoke --config "$CONFIG" 2>&1 | tail -1)
  SMOKE_RC=$?
  if [[ $SMOKE_RC -eq 0 && "$SMOKE_OUT" == *"detector-smoke OK"* ]]; then
    check PASS "detector heartbeat: $SMOKE_OUT"
  elif [[ $SMOKE_RC -eq 124 ]]; then
    check FAIL "detector-smoke HUNG (>90 s) — dead/wrong-platform engine runtime (Ford deaf-gap class). ORT_DYLIB_PATH=${ORT_DYLIB_PATH:-unset}"
  else
    check FAIL "detector-smoke failed (exit $SMOKE_RC): $SMOKE_OUT"
  fi
  if [[ $CK_RC -eq 0 ]]; then
    check PASS "keywords compile (BPE-encodable) on the ARMED engine's vocab"
    # Short token targets are the acoustic analogue of the 2-character variant
    # problem that produced 38 false alarms in one event set.
    #
    # ESCALATED 2026-08-04 from WARN to FAIL-unless-acked. SNAP `game` (▁GA ME,
    # k=2) fired live and settled NO — and the WARN below had fired that same
    # night into the void. The measured confidences are why no softer gate works:
    # 0.587 (traded, spec_edge) / 0.932 (pipe_tuned) / 0.953 (streamlink). A k=2
    # target assembles at NEAR-CEILING confidence, so a decision-layer
    # min_confidence gate cannot reach it — raise the gate past 0.587 and the
    # 0.953 leg fires instead, because fanout ORs at the decision.
    #
    # Dropping the keyword still forfeits its market, which is a TRADING
    # decision — so this does not refuse the word, it refuses to proceed
    # SILENTLY. The ack file is the human taking the decision the WARN used to
    # swallow. Compile-time refusal (kws_compile/config::validate) is
    # deliberately NOT taken, for the same forfeits-a-market reason.
    SHORT=$(printf '%s\n' "$CK_OUT" | grep -oE "confirmed \[[^]]+\]: .* \(([12]) tokens\)" | sed -E 's/confirmed \[([^]]+)\].*\(([12]) tokens\)/\1 (\2 tokens)/' || true)
    if [[ -n "$SHORT" ]]; then
      ACK_FILE="configs/.acks/$(basename "$CONFIG" .toml)-short-tokens.ack"
      ACK_MAX_AGE_DAYS=7
      if [[ ! -f "$ACK_FILE" ]]; then
        check FAIL "SHORT token targets (k<=2) present and UNACKED — live evidence: SNAP \"game\" fired 0.587/0.932/0.953 and settled NO; no confidence gate reaches a k=2 target. Acknowledge the trading decision:  mkdir -p configs/.acks && printf '%s\n' \"acked $(date -u +%F) by <operator>\" $(printf '%s\n' "$SHORT" | sed 's/^/           /' | tr '\n' ' ') > $ACK_FILE"
        printf '%s\n' "$SHORT" | sed 's/^/        /'
      else
        # Portability (fixed 2026-08-04, found running this on the us-east box):
        # BSD `stat -f %m` and GNU `stat -f` are DIFFERENT FLAGS — on GNU, -f is
        # --file-system, which SUCCEEDS and prints "  File: ..." instead of an
        # mtime. So the `||` fallback never fired on Linux, the arithmetic got
        # that text, and preflight died with `File: unbound variable` — taking
        # every later gate (admission, TLS, readiness, mailer) with it. Detect
        # the OS instead of relying on failure, matching the guard at :468.
        if [[ "$(uname -s)" == Darwin ]]; then
          ACK_MTIME=$(stat -f %m "$ACK_FILE")
        else
          ACK_MTIME=$(stat -c %Y "$ACK_FILE")
        fi
        ACK_AGE_D=$(( ( $(date -u +%s) - ACK_MTIME ) / 86400 ))
        if (( ACK_AGE_D > ACK_MAX_AGE_DAYS )); then
          check FAIL "short-token ack STALE (${ACK_AGE_D}d > ${ACK_MAX_AGE_DAYS}d): $ACK_FILE — re-ack for this event"
        else
          MISSING_ACK=""
          while IFS= read -r s; do
            [[ -z "$s" ]] && continue
            w="${s%% (*}"
            grep -qF -- "$w" "$ACK_FILE" || MISSING_ACK+="$w "
          done <<< "$SHORT"
          if [[ -n "$MISSING_ACK" ]]; then
            check FAIL "short-token ack does not name: $MISSING_ACK— every k<=2 target must be listed in $ACK_FILE"
          else
            check WARN "SHORT token targets ACKED (${ACK_AGE_D}d old, $ACK_FILE) — highest false-alarm risk, accepted knowingly:"
            printf '%s\n' "$SHORT" | sed 's/^/        /'
          fi
        fi
      fi
    else
      check PASS "no 1-2 token keyword targets"
    fi
  else
    check FAIL "compile-keywords FAILED — config has detector-unencodable phrases; arm will refuse at fire time"
  fi
else
  check FAIL "cargo build --release FAILED — fix before event day"
fi

# ── Allowlist coverage ─────────────────────────────────────────────────────
# `arm` refuses a partial allowlist at start-up, but that fires at 8 AM on the
# call. Catch it the night before. The shape: `prepare`'s placeholder allowlist
# entry survives while [[keywords]] are filled in real — non-empty, so
# --i-am-live's emptiness check passes, the arm comes up clean, and then EVERY
# order is refused NotAllowlisted at fire time. All three configs staged for
# 2026-07-28/29 shipped exactly that way.
AL_OUT=$(python3 - "$CONFIG" <<'PY'
import re, sys
s = open(sys.argv[1]).read()
m = re.search(r'allowlist_tickers\s*=\s*\[(.*?)\]', s, re.S)
allow = set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()
kws = set(re.findall(r'market_ticker\s*=\s*"([^"]+)"', s))
if not kws:
    print("SKIP no keywords")
elif not allow:
    print(f"EMPTY {len(kws)}")
else:
    miss = sorted(kws - allow)
    print(f"MISS {len(miss)} {len(kws)} " + " ".join(miss[:5]) if miss else f"OK {len(kws)}")
PY
) || AL_OUT="ERR"
case "$AL_OUT" in
  OK*)    check PASS "[risk].allowlist_tickers covers all ${AL_OUT#OK } keyword markets" ;;
  EMPTY*) check FAIL "[risk].allowlist_tickers is EMPTY with ${AL_OUT#EMPTY } keywords — every order refused NotAllowlisted; paste prepare's [risk] block" ;;
  MISS*)  check FAIL "[risk].allowlist_tickers MISSES keyword markets: $AL_OUT — those orders are refused NotAllowlisted at fire time" ;;
  SKIP*)  check WARN "no [[keywords]] in config — allowlist coverage not checked" ;;
  *)      check FAIL "allowlist coverage check could not run ($AL_OUT)" ;;
esac

# ── Allowlist vs LIVE board (INDI class) ──────────────────────────────────
# The inverse direction of the check above: markets Kalshi lists for the event
# that the config never arms. WING 2026-07-29 shipped 13/14 (India missing);
# FED 2026-07-29 shipped 43/44 (FAMI opened after staging). Public endpoint,
# no creds; network failure is a WARN, a live-not-armed market is a FAIL.
if DIFF_OUT=$(bash "$(dirname "$0")/allowlist-diff.sh" "$CONFIG" 2>&1); then
  check PASS "allowlist matches the live Kalshi board ($(head -1 <<<"$DIFF_OUT"))"
else
  case $? in
    1) check FAIL "live board has markets this config does not arm: $(grep LIVE-NOT-ARMED <<<"$DIFF_OUT" | tr '\n' ' ')" ;;
    *) check WARN "allowlist-vs-live diff could not run: $(head -1 <<<"$DIFF_OUT")" ;;
  esac
fi

# ── Armed variants vs the market's OWN resolution phrase ───────────────────
# The readiness study proves the detector can HEAR our variants. It cannot
# notice they are the WRONG WORDS — a perfectly-heard wrong word scores a
# clean 16/16. AAPL 2026-07-30 armed "iphone" against the market "iPhone 18"
# and fired a Confirmed intent at 21:02:20Z while the market sat at 6c all
# night, because `prepare`'s sanitize_variant() alphabet is letters+spaces and
# silently STRIPS DIGITS. The same generator shipped "windows" for
# "Windows 11" (MSFT 07-29, live), "ultra" for "Ultra 3", "formula" for
# "Formula 1". prepare flags these (`compound = ...is_ascii_digit()...`) and
# only writes a review comment — a comment is not a gate, and nobody reviews
# at T-20. This re-derives the requirement from the LIVE board, so it holds
# however prepare evolves.
if PHRASE_OUT=$(python3 "$(dirname "$0")/market-phrase-screen.py" "$CONFIG" 2>&1); then
  check PASS "armed variants can resolve their markets ($(head -1 <<<"$PHRASE_OUT"))"
else
  case $? in
    1) check FAIL "variants cannot resolve their market: $(grep 'PHRASE-MISMATCH' <<<"$PHRASE_OUT" | sed 's/ *PHRASE-MISMATCH: //' | tr '\n' ' ')" ;;
    *) check WARN "market-phrase screen could not run: $(head -1 <<<"$PHRASE_OUT")" ;;
  esac
fi

# ── Credentials (live probes, read-only) ───────────────────────────────────
if [[ -f "$ACCOUNT" ]]; then
  if [[ "$(uname -s)" == Darwin ]]; then
    perms=$(stat -f '%Lp' "$ACCOUNT")
  else
    perms=$(stat -c '%a' "$ACCOUNT")
  fi
  check PASS "account env exists: $ACCOUNT ($perms perms)"
  # shellcheck disable=SC1090
  source "$ACCOUNT"
  # Twilio is the PHONE lane. A webcast-only event has no phone leg, so absent
  # Twilio creds are correct there, not a failure — demand them only when the
  # config actually declares a twilio source.
  if grep -qE '^[[:space:]]*kind[[:space:]]*=[[:space:]]*"twilio' "$CONFIG" 2>/dev/null; then
    if [[ -n "${TWILIO_ACCOUNT_SID:-}" && -n "${TWILIO_AUTH_TOKEN:-}" ]]; then
      code=$(curl -s -o /dev/null -w '%{http_code}' \
        "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}.json" \
        -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}")
      [[ "$code" == 200 ]] && check PASS "Twilio auth probe: 200" \
                           || check FAIL "Twilio auth probe: HTTP $code"
    else
      check FAIL "config declares a twilio source but the account env lacks TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN"
    fi
  else
    check PASS "webcast-only config (no twilio source) — Twilio creds not required"
  fi
else
  check FAIL "account env missing: $ACCOUNT"
fi
kalshi_code=$(curl -s -o /dev/null -w '%{http_code}' "https://api.elections.kalshi.com/trade-api/v2/exchange/status")
[[ "$kalshi_code" == 200 ]] && check PASS "Kalshi API reachable (exchange/status 200)" \
                            || check FAIL "Kalshi API probe: HTTP $kalshi_code"

# ── ngrok agent (Darwin/CDMX only) ─────────────────────────────────────────
# Linux/VPS production is tunnel-free by design (docs/INGRESS-BAKEOFF.md §5:
# "No tunnels in production") — the WS endpoint terminates on the box.
if [[ "$(uname -s)" == Darwin ]]; then
  if ngrok config check >/dev/null 2>&1; then
    check PASS "ngrok agent config valid"
  else
    check FAIL "ngrok config check failed"
  fi
else
  check PASS "tunnel-free host (Linux) — direct WS endpoint; ngrok not used"
fi

# ── TLS ingress (Q-TLS): the phone lane's PRODUCTION front door ─────────────
# Preflight used to say "tunnel-free host (Linux)" and PASS unconditionally,
# which was true and useless: since Q-TLS the media path is Caddy terminating
# TLS on the box at wss://<host>/e/<port>, and NOTHING in preflight ever touched
# it. A dead Caddy, an expired cert, or a bind port with no Caddyfile route
# reads exactly like a healthy night until Twilio cannot connect and the event
# is gone.
#
# Runs on any OS, not just the box: if an ingress is named, it gets probed. Only
# meaningful for a phone arm — a webcast arm dials nothing.
if grep -qE '^[[:space:]]*kind[[:space:]]*=[[:space:]]*"twilio' "$CONFIG" 2>/dev/null; then
    # The ingress is a LAUNCH argument, not a config field, so preflight has to
    # be told. Accept it the same ways the launcher can receive it.
    TLS_URL="${EARSHOT_STREAM_URL:-}"
    if [[ -z "$TLS_URL" && -n "${EARSHOT_TLS_HOST:-}" ]]; then
      TLS_PORT=$(sed -n 's/^[[:space:]]*bind[[:space:]]*=[[:space:]]*"[^:]*:\([0-9][0-9]*\)".*/\1/p' "$CONFIG" | head -1)
      [[ -n "$TLS_PORT" ]] && TLS_URL="wss://${EARSHOT_TLS_HOST}/e/${TLS_PORT}"
    fi
    if [[ -z "$TLS_URL" ]]; then
      # FAIL, not WARN (2026-08-04, Gap P): a phone arm whose ingress preflight
      # never ran is exactly the "healthy-looking night, dead front door" class
      # (6fe1e30). Laptop/ngrok rehearsals have no TLS host at preflight time —
      # they say so explicitly with EARSHOT_TUNNEL_REHEARSAL=1, which keeps the
      # historical WARN.
      if [[ -n "${EARSHOT_TUNNEL_REHEARSAL:-}" ]]; then
        check WARN "TLS ingress UNVERIFIED (EARSHOT_TUNNEL_REHEARSAL=1 — ngrok/laptop rehearsal posture; production events must preflight their real wss endpoint)"
      else
        check FAIL "TLS ingress UNVERIFIED — set EARSHOT_STREAM_URL (or EARSHOT_TLS_HOST) so preflight can probe the wss endpoint this event will actually use; Q-TLS is the phone lane's front door and nothing else checks it. For a deliberate ngrok/laptop rehearsal, set EARSHOT_TUNNEL_REHEARSAL=1."
      fi
    else
      TLS_HOSTNAME="${TLS_URL#*://}"; TLS_HOSTNAME="${TLS_HOSTNAME%%/*}"
      TLS_PATH="/${TLS_URL#*://*/}"
      # 1. Cert: valid, and not expiring inside the event horizon. Let's Encrypt
      #    renews at 30 d; under 10 d left means renewal is already failing.
      CERT_END=$(echo | openssl s_client -servername "$TLS_HOSTNAME" -connect "${TLS_HOSTNAME}:443" 2>/dev/null \
                 | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
      if [[ -z "$CERT_END" ]]; then
        check FAIL "TLS handshake to ${TLS_HOSTNAME}:443 FAILED — Twilio cannot reach this event's media endpoint. Check caddy is running and the A record still points at this box."
      else
        CERT_DAYS=$(CERT_END="$CERT_END" python3 - <<'PY' 2>/dev/null || echo -1
import datetime, os
end = datetime.datetime.strptime(os.environ["CERT_END"].strip(), "%b %d %H:%M:%S %Y %Z")
print(int((end.replace(tzinfo=datetime.timezone.utc) - datetime.datetime.now(datetime.timezone.utc)).days))
PY
)
        if [[ "$CERT_DAYS" -lt 0 ]]; then
          check FAIL "TLS cert for $TLS_HOSTNAME expired or unparsable (enddate '$CERT_END')"
        elif [[ "$CERT_DAYS" -lt 10 ]]; then
          check WARN "TLS cert for $TLS_HOSTNAME expires in ${CERT_DAYS}d — Let's Encrypt renews at 30d, so under 10 means renewal is already failing"
        else
          check PASS "TLS ingress cert valid for $TLS_HOSTNAME (${CERT_DAYS}d left)"
        fi
        # 2. Route: the per-event path must be MATCHED by the Caddyfile. An
        #    unrouted port falls through to `respond 404` — indistinguishable
        #    from a healthy host until the call. A matched WS path answers a
        #    plain GET with 400/426/502 (upgrade required / upstream not yet
        #    listening), never 404.
        ING_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://${TLS_HOSTNAME}${TLS_PATH}" 2>/dev/null || echo 000)
        case "$ING_CODE" in
          404) check FAIL "TLS ingress ${TLS_PATH} returns 404 — this bind port has NO route in deploy/caddy/Caddyfile. Twilio would be refused. Add the @e<port> block and reload caddy." ;;
          000) check FAIL "TLS ingress ${TLS_HOSTNAME}${TLS_PATH} unreachable (no HTTP response)" ;;
          *)   check PASS "TLS ingress route ${TLS_PATH} live (HTTP $ING_CODE — a matched WS path, not the 404 catch-all)" ;;
        esac
      fi
    fi
fi

# ── Mid-call persistence (Q-REDIAL): no phone arm without a reconnect budget ─
# The Rust default is max_reconnects = 0 (one-shot): a first drop ends the
# run. Only slate-stage-derived configs carry the pinned production values;
# the hand-written 26jul29 configs went to real events bare. launch-event.sh
# refuses at T-0 (unless --one-shot-ok); this is the night-before signal.
if grep -qE '^[[:space:]]*kind[[:space:]]*=[[:space:]]*"twilio' "$CONFIG" 2>/dev/null; then
  MAXR=$(sed -n 's/^[[:space:]]*max_reconnects[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$CONFIG" | head -1)
  if [[ -n "$MAXR" && "$MAXR" -gt 0 ]]; then
    check PASS "mid-call persistence: max_reconnects = $MAXR"
  else
    check FAIL "phone config has NO mid-call persistence (max_reconnects unset/0) — a first drop ends the run. Add \"max_reconnects = 2\" + \"idle_timeout_secs = 8\" (Q-REDIAL pinned values; slate-stage.sh stages them), or launch with --one-shot-ok to name a deliberate one-shot."
  fi
fi

# ── Caddyfile no-rewrite invariant (Q-M3 signature base) ───────────────────
# Twilio signs the EXACT stream URL. A `uri strip_prefix` or `header_up Host`
# in the media site block changes what the box sees, so every handshake 403s
# with nothing in the Twilio console explaining why. deploy/caddy/Caddyfile
# warns about this in a comment; a comment is not a check.
if [[ -f deploy/caddy/Caddyfile ]]; then
  if grep -nE '^[^#]*(uri[[:space:]]+strip_prefix|header_up[[:space:]]+Host)' deploy/caddy/Caddyfile >/dev/null; then
    check FAIL "deploy/caddy/Caddyfile rewrites the path or Host — that changes the Q-M3 signature base and EVERY Twilio handshake will 403. Remove the strip_prefix/header_up directive."
  else
    check PASS "Caddyfile preserves path and Host (Q-M3 signature base intact)"
  fi
fi

# ── Timezone + scheduled wake ──────────────────────────────────────────────
tz=$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')
[[ "$tz" == "America/Mexico_City" ]] && check PASS "timezone: $tz" \
  || check WARN "timezone is '$tz', not America/Mexico_City — ALL window times (launchd fire, END_HHMM) shift with local time; re-verify the schedule"
# 2026-08-04: the fire-mechanism gate used to run only when the caller
# remembered --fire-hhmm — a silent skip. Default it from the event env so a
# slate-staged event always gets the gate; FAIL loudly on a contradiction.
if [[ -z "$FIRE_HHMM" && -n "$EVENT_ENV" && -f "$EVENT_ENV" ]]; then
  ENV_FIRE=$(source "$EVENT_ENV" 2>/dev/null; echo "${FIRE_HHMM:-}")
  if [[ -n "$ENV_FIRE" ]]; then
    FIRE_HHMM="$ENV_FIRE"
    check PASS "fire time defaulted from $EVENT_ENV: FIRE_HHMM=$FIRE_HHMM (gate cannot silently skip)"
  fi
elif [[ -n "$FIRE_HHMM" && -n "$EVENT_ENV" && -f "$EVENT_ENV" ]]; then
  ENV_FIRE=$(source "$EVENT_ENV" 2>/dev/null; echo "${FIRE_HHMM:-}")
  if [[ -n "$ENV_FIRE" && "$ENV_FIRE" != "$FIRE_HHMM" ]]; then
    check FAIL "fire-time CONTRADICTION: --fire-hhmm $FIRE_HHMM but $EVENT_ENV says FIRE_HHMM=$ENV_FIRE — one of them is wrong; the timer and the wake check must agree"
  fi
fi
if [[ -n "$FIRE_HHMM" ]]; then
  if [[ "$(uname -s)" == Darwin ]]; then
    # Only USER-scheduled wakes count — macOS system alarms (by 'com.apple…')
    # come and go and must not satisfy this check.
    user_wake=$(pmset -g sched 2>/dev/null | grep -i "wake at" | grep -v "by 'com.apple" | head -1 | tr -s ' ')
    # What this gate actually needs to know is "will this machine be awake at
    # fire time". A scheduled wake is one way to guarantee it; a HELD
    # PreventSystemSleep assertion (caffeinate) is direct evidence of the same
    # thing for a machine that is already awake and on AC — arguably stronger,
    # since it needs no wake-from-sleep transition at all. Accepting only the
    # pmset form made the gate cry wolf on an awake, caffeinated laptop
    # (2026-08-05: it FAILed all four morning events at T-8min, which is how a
    # gate teaches operators to override it). Both forms are verified, never
    # assumed; neither is satisfiable by a comment.
    caff=$(pmset -g assertions 2>/dev/null | grep -i 'PreventSystemSleep.*caffeinate' | head -1 | tr -s ' ')
    if [[ -n "$user_wake" ]]; then
      check PASS "pmset user-scheduled wake: $user_wake — VERIFY it is before fire time $FIRE_HHMM"
    elif [[ -n "$caff" ]]; then
      check PASS "no scheduled wake, but a PreventSystemSleep assertion is HELD (${caff:0:80}…) — the machine cannot sleep before fire time $FIRE_HHMM. Keep it on AC with the process alive; a scheduled wake is still preferable for an unattended overnight gap."
    else
      check FAIL "NO user-scheduled pmset wake and NO held PreventSystemSleep assertion — a closed lid at fire time defers launchd past the window. Set one: sudo pmset schedule wake \"MM/DD/YY HH:MM:SS\" (or hold the machine awake: caffeinate -s &)"
    fi
  else
    # Linux server: no sleep/wake concept — the fire mechanism is a systemd
    # user timer (scripts/install-event-timer.sh). Verify it is armed.
    ev_label=""
    if [[ -n "$EVENT_ENV" && -f "$EVENT_ENV" ]]; then
      ev_label=$(source "$EVENT_ENV" 2>/dev/null; echo "${LABEL:-}")
    fi
    if [[ -n "$ev_label" ]]; then
      next=$(systemctl --user list-timers --all 2>/dev/null | grep -F "$ev_label.timer" | head -1 | tr -s ' ')
      if [[ -n "$next" ]]; then
        check PASS "systemd timer armed: $next — VERIFY NEXT is the event fire time ($FIRE_HHMM box-local)"
      else
        check FAIL "NO systemd user timer '$ev_label.timer' — nothing will fire. Arm it: scripts/install-event-timer.sh --event-env $EVENT_ENV --fire-hhmm $FIRE_HHMM --date <YYYY-MM-DD>"
      fi
    else
      check WARN "fire-time check skipped: no --event-env (or no LABEL in it) — cannot name the systemd timer to verify"
    fi
  fi
fi
if [[ "$(uname -s)" == Darwin ]]; then
  check WARN "power: $(pmset -g batt | tr '\n' ' ' | tr -s ' ') — must be AC + lid open at fire time"
fi

# ── CPU load ───────────────────────────────────────────────────────────────
# 2026-07-17: unrelated batch jobs pushed this box to load 82 — network egress
# failed outright and every process crawled. The pipeline's latency budgets
# (chunk→detect p90 ~25 ms) were measured on a quiet box; a saturated one is a
# different machine than the one that rehearsed.
if [[ "$(uname -s)" == Darwin ]]; then
  ncpu=$(sysctl -n hw.ncpu)
  load1=$(sysctl -n vm.loadavg | awk '{print $2}')
else
  ncpu=$(nproc)
  load1=$(awk '{print $1}' /proc/loadavg)
fi
if python3 -c "exit(0 if float('$load1') > $ncpu*2 else 1)"; then
  check FAIL "load average $load1 with $ncpu cores — box is saturated; whatever is running must be done or stopped before the event"
elif python3 -c "exit(0 if float('$load1') > $ncpu*0.75 else 1)"; then
  check WARN "load average $load1 with $ncpu cores — something heavy is running; confirm it finishes before fire time"
else
  check PASS "load average $load1 ($ncpu cores)"
fi

# ── Registration identity round-trip (2026-07-29) ──────────────────────────
# ~/.earshot/registration.env feeds the browser-free Chorus Call resolver
# (choruscall-resolve.py --register, driven by wing-autorun.sh). It held
#     EARSHOT_REG_LAST=Del Rio
# UNQUOTED, so `set -a; . registration.env` split on the space: it exported
# EARSHOT_REG_LAST=Del and then tried to RUN `Rio` ("Rio: command not found").
#
# For THAT specific shape, wing-autorun.sh's emptiness check is in fact enough:
# measured 2026-07-29, sourcing `EARSHOT_REG_LAST=Del Rio` leaves the variable
# EMPTY (bash reads it as a temporary assignment prefix to the command `Rio`, so
# nothing persists), and emptiness is caught. An earlier draft of this comment
# claimed it yields "Del" and slips through — that was wrong.
#
# The reason to check ROUND-TRIP EQUALITY anyway is the corruption that is NOT
# empty and therefore invisible to every existing guard. All three measured
# 2026-07-29 by sourcing the file and printing the result:
#   EARSHOT_REG_LAST=Del Rio   → ""   (empty — emptiness catches it)
#   EARSHOT_REG_COMPANY=A&B    → ""   (empty — emptiness catches it)
#   EARSHOT_REG_LAST=O$Brien   → "O"  ← NON-EMPTY AND WRONG, invisible to
#                                       every check we have. `$Brien` is an
#                                       unset variable, so it expands to
#                                       nothing and leaves a plausible name.
# That last class is the gap: we would register as "O" and never know.
# Comparing what the file SAYS against what sourcing YIELDS catches all three.
REG_ENV="${EARSHOT_REG_ENV:-$HOME/.earshot/registration.env}"
if [[ -f "$REG_ENV" ]]; then
  REG_OUT=$(
    REG_ENV="$REG_ENV" python3 - <<'PY'
import os, re, shlex, subprocess, sys

path = os.environ["REG_ENV"]
want = {}
for ln in open(path):
    ln = ln.strip()
    if not ln or ln.startswith("#"):
        continue
    ln = re.sub(r"^export\s+", "", ln)
    if "=" not in ln:
        continue
    k, v = ln.split("=", 1)
    k = k.strip()
    if not k.startswith("EARSHOT_REG_"):
        continue
    # The INTENDED value: what a human reading the file would say it is.
    want[k] = v.strip().strip('"').strip("'")

if not want:
    print("SKIP no EARSHOT_REG_* keys")
    sys.exit(0)

# The ACTUAL value a shell produces by sourcing it, the way the autoruns do.
probe = "set -a; . " + shlex.quote(path) + " >/dev/null 2>&1; set +a; " + \
        "; ".join(f'printf "%s\\037%s\\036" {k} "${{{k}-}}"' for k in want)
r = subprocess.run(["bash", "-c", probe], capture_output=True, text=True)
got = {}
for rec in r.stdout.split("\036"):
    if "\037" in rec:
        k, v = rec.split("\037", 1)
        got[k] = v

bad = [f"{k}: file says {want[k]!r} but sourcing yields {got.get(k, '')!r}"
       for k in want if got.get(k, "") != want[k]]
if bad:
    print("FAIL " + "; ".join(bad))
else:
    print(f"OK {len(want)} keys round-trip exactly")
PY
  )
  case "$REG_OUT" in
    OK*)   check PASS "registration identity round-trips (${REG_OUT#OK }) — $REG_ENV" ;;
    SKIP*) check WARN "registration identity: ${REG_OUT#SKIP } in $REG_ENV" ;;
    FAIL*) check FAIL "registration identity CORRUPTED BY SOURCING — ${REG_OUT#FAIL }.
       Quote the values (EARSHOT_REG_LAST=\"Del Rio\"). An unquoted value with a
       space truncates silently and is NOT caught by an emptiness check." ;;
    *)     check WARN "registration identity check could not run: $REG_OUT" ;;
  esac
else
  # Only some hosts resolve Chorus Call; absence is not a failure.
  check WARN "no $REG_ENV — browser-free Chorus Call registration unavailable on this host"
fi

# ── Dial-in go/no-go gate ──────────────────────────────────────────────────
# The night-before FAIL-STOP promised by configs/<event>-autorun.env ("the
# preflight must FAIL-STOP if these are still TBD"). Until 2026-07-16 that gate
# did not exist: the ONLY placeholder check lived in event-autorun.sh, and it
# fires at 06:05 on call morning — hours after the go/no-go decision, with no
# time left to hunt a number. The promise read as covered until the morning it
# wasn't. This is that gate, at the hour it was meant to run.
if [[ -n "$EVENT_ENV" ]]; then
  if [[ -f "$EVENT_ENV" ]]; then
    # Subshell: never leak event vars into the rest of the preflight.
    DIAL_STATUS=$(
      # shellcheck disable=SC1090
      source "$EVENT_ENV" 2>/dev/null
      if [[ "${TO_NUMBER:-TBD}" == *TBD* || "${PASSCODE:-}" == *TBD* ]]; then
        echo "TBD:${TO_NUMBER:-unset}/${PASSCODE:-unset}"
      else
        echo "OK:${TO_NUMBER}"
      fi
    )
    if [[ "$DIAL_STATUS" == TBD:* ]]; then
      # Lane-aware (2026-08-04): TO_NUMBER=TBD is slate-stage's DELIBERATE
      # posture for a webcast-only event — nothing dials, so TBD digits are
      # correct there, not a NO-GO. FAILing them trains override behavior.
      if grep -qE '^[[:space:]]*kind[[:space:]]*=[[:space:]]*"twilio' "$CONFIG" 2>/dev/null; then
        check FAIL "dial-in still TBD in $EVENT_ENV (${DIAL_STATUS#TBD:}) — NO-GO. The number hunt has not completed; event-autorun would abort at fire time. See docs/RUNBOOK.md Track A."
      else
        check PASS "dial-in TBD in $EVENT_ENV is the deliberate webcast-only posture (no twilio source in $CONFIG — nothing dials)"
      fi
    else
      check PASS "dial-in resolved in $EVENT_ENV (${DIAL_STATUS#OK:})"
    fi
  else
    check FAIL "event env not found: $EVENT_ENV"
  fi
else
  # Not passing --event-env is itself a risk: it silently skips the gate.
  check WARN "no --event-env given — dial-in go/no-go gate SKIPPED. Pass --event-env configs/<event>-autorun.env to enforce it."
fi

# ── Bridge admission gate (VERIFICATIONS Q-ADMIT, 2026-07-29) ──────────────
# A number that resolves is NOT a bridge that admits us: MSFT's registry entry
# said listen_access="yes" ("no password required") and the live bridge is an
# operator screen that dropped the silent caller in 42 s. Every one of the
# 2026-07-29 phone failures was knowable the night before with one ~$0.02
# probe call. So: a phone arm requires a probe-observed `admission = "held"`
# in configs/dial-ins.toml for this venue (scripts/bridge-probe.sh sets it).
# Twilio-source configs only — a webcast arm dials nothing (don't cry wolf).
if grep -qE '^[[:space:]]*kind[[:space:]]*=[[:space:]]*"twilio' "$CONFIG" 2>/dev/null; then
  ADMIT_OUT=$(EVENT_ID="$EVENT_ID" CONFIG="$CONFIG" python3 - <<'PY'
import os, re, sys, tomllib

event_id = os.environ.get("EVENT_ID", "")
m = re.match(r"KXEARNINGSMENTION([A-Z0-9]+)", event_id)
if not m:
    print(f"SKIP cannot derive series key from event id {event_id!r}")
    sys.exit(0)
key = m.group(1)
try:
    with open("configs/dial-ins.toml", "rb") as f:
        reg = tomllib.load(f)
except Exception as e:
    print(f"FAIL configs/dial-ins.toml unreadable: {e}")
    sys.exit(0)
entry = reg.get(key)
if entry is None:
    print(f"FAIL venue {key} not in configs/dial-ins.toml — probe it: scripts/bridge-probe.sh --to <number> --label {key.lower()}-<date>")
    sys.exit(0)
admission = entry.get("admission", "untested")
probed = entry.get("admission_probed", "never")
detail = entry.get("admission_detail", "")
if admission == "held":
    from datetime import date, datetime
    try:
        age = (date.today() - datetime.strptime(probed, "%Y-%m-%d").date()).days
    except ValueError:
        age = 9999
    if age <= 30:
        print(f"PASS {key} admission=held (probed {probed}: {detail})")
    else:
        print(f"WARN {key} admission=held but STALE (probed {probed}, {age}d ago) — re-probe: scripts/bridge-probe.sh (~$0.02)")
elif admission == "operator_screened":
    # Default: NO-GO. Downgrade to PASS-with-note ONLY when BOTH sides of the
    # gated operator path are in place (docs/OPERATOR-HANDLER.md §Registry
    # interaction): (a) THIS config opts in via [operator_handler] enabled=true,
    # and (b) the venue's registry entry carries operator_handler =
    # "rehearsed-<date>" — a field written ONLY after a successful end-to-end
    # rehearsal against the fake operator (never pre-filled by hand).
    cfg_text = ""
    try:
        cfg_text = open(os.environ.get("CONFIG", "")).read()
    except OSError:
        pass
    oh_block = re.search(r'^\[operator_handler\]\s*$(.*?)(?=^\[|\Z)',
                         cfg_text, re.S | re.M)
    handler_enabled = bool(oh_block and re.search(
        r'^\s*enabled\s*=\s*true\b', oh_block.group(1), re.M))
    rehearsed = re.fullmatch(r"rehearsed-(\d{4}-\d{2}-\d{2})",
                             str(entry.get("operator_handler", "")))
    if handler_enabled and rehearsed:
        print(f"PASS {key} operator-screened venue armed via gated human-first handler (rehearsed {rehearsed.group(1)}) — {detail}")
    else:
        missing = []
        if not handler_enabled:
            missing.append("config lacks [operator_handler] enabled=true")
        if not rehearsed:
            missing.append(f'venue lacks operator_handler="rehearsed-<date>" in configs/dial-ins.toml (set only by a passed rehearsal)')
        print(f"FAIL {key} admission=operator_screened ({detail}) — a live human drops silent callers; unattended-infeasible on the pure listen-only posture. NO-GO for a phone arm. Gated operator path not ready: {'; '.join(missing)}.")
elif admission == "dropped":
    print(f"FAIL {key} admission=dropped ({detail}) — the bridge hangs up on us. NO-GO until the drop cause is understood.")
elif entry.get("reservation_window"):
    # Q-ADMIT addendum: this venue's bridge does not EXIST outside its event
    # window, so "untested" here does not mean nobody bothered — it means the
    # only probe that could decide it has not become possible yet. A bare FAIL
    # tells the operator to do something they cannot do, which is how a gate
    # gets ignored. Name the window and the command instead.
    import datetime as _dt
    starts = ""
    try:
        with open(os.environ.get("CONFIG", ""), "rb") as f:
            starts = str(tomllib.load(f).get("event", {}).get("starts_at", ""))
    except Exception:
        pass
    window = ""
    try:
        call = _dt.datetime.fromisoformat(starts.replace("Z", "+00:00"))
        if call.tzinfo is None:
            call = call.replace(tzinfo=_dt.timezone.utc)
        fmt = lambda d: d.strftime("%H:%MZ")
        window = (f" Window {fmt(call - _dt.timedelta(minutes=90))}.."
                  f"{fmt(call - _dt.timedelta(minutes=30))} on "
                  f"{(call - _dt.timedelta(minutes=90)).date()}.")
    except ValueError:
        pass
    print(f"WARN {key} admission untested and the bridge is reservation-window "
          f"only — an off-hours probe returns a no-answer that classifies "
          f"nothing (Q-ADMIT addendum).{window} Probe in-window: "
          f"scripts/admission-window-probe.sh --config {os.environ.get('CONFIG','<cfg>')} --run")
else:
    print(f"FAIL {key} admission untested — one ~$0.02 call decides this: scripts/bridge-probe.sh --to <number> --label {key.lower()}-<date>")
PY
  )
  case "$ADMIT_OUT" in
    PASS*) check PASS "${ADMIT_OUT#PASS }" ;;
    WARN*) check WARN "${ADMIT_OUT#WARN }" ;;
    SKIP*) check WARN "bridge admission gate: ${ADMIT_OUT#SKIP }" ;;
    *)     check FAIL "${ADMIT_OUT#FAIL }" ;;
  esac
fi

# ── Keyword readiness gate ─────────────────────────────────────────────────
# Every config being preflighted, every lane (phone AND webcast — WING's lost
# words would have lost on either). Function defined above with the rationale.
readiness_gate

# ── Mailer live test ───────────────────────────────────────────────────────
BODY=$(printf '%s\n' "${REPORT_LINES[@]}")
if [[ -f "$HOME/.kalshi/mail_env" ]]; then
  if (
    # shellcheck disable=SC1090
    source "$HOME/.kalshi/mail_env"
    export MAIL_USER MAIL_APP_PASSWORD MAIL_TO
    MAIL_SUBJECT="EARSHOT preflight ${EVENT_ID:-?}: $([[ $FAILS -eq 0 ]] && echo PASS || echo "$FAILS FAIL")" \
    MAIL_BODY="$BODY" python3 - <<'PY'
import os, smtplib, ssl
from email.message import EmailMessage
msg = EmailMessage()
msg["From"] = os.environ["MAIL_USER"]; msg["To"] = os.environ["MAIL_TO"]
msg["Subject"] = os.environ["MAIL_SUBJECT"]
msg.set_content(os.environ["MAIL_BODY"])
with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
    s.login(os.environ["MAIL_USER"], os.environ["MAIL_APP_PASSWORD"])
    s.send_message(msg)
PY
  ); then
    check PASS "mailer live test: preflight email sent"
  else
    check FAIL "mailer live test FAILED — the unattended run cannot alert"
  fi
else
  check FAIL "mail env missing: ~/.kalshi/mail_env"
fi

echo
# ── Structured verdict artifact (Phase D door; consumed by assert-ready.sh
# and preflight-nightly.sh's feature-detect). Written on PASS AND FAIL, as the
# last act, via tmp+mv; a write failure must never convert a PASS into a
# crash-exit — hence the || WARN, never a FAIL increment after the verdict.
if [[ -z "$RESULT_JSON" && -n "${EVENT_ID:-}" ]]; then
  RESULT_JSON="state/${EVENT_ID}/preflight-result.json"
fi
if [[ -n "$RESULT_JSON" ]]; then
  SKIPPED=""
  [[ -z "$EVENT_ENV" ]] && SKIPPED="dial-in"
  [[ -z "$FIRE_HHMM" ]] && SKIPPED="${SKIPPED:+$SKIPPED,}fire-mechanism"
  if mkdir -p "$(dirname "$RESULT_JSON")" 2>/dev/null && \
     PF_LINES=$(printf '%s\n' "${REPORT_LINES[@]}") \
     PF_EVENT="${EVENT_ID:-}" PF_CONFIG="$CONFIG" PF_FAILS="$FAILS" PF_WARNS="$WARNS" \
     PF_FIRE="$FIRE_HHMM" PF_ENV="$EVENT_ENV" PF_SKIPPED="$SKIPPED" \
     PF_CFG_SHA="$(shasum -a 256 "$CONFIG" 2>/dev/null | cut -d' ' -f1)" \
     PF_KW_SHA="$(python3 "$(dirname "$0")/keywords-extract.py" "$CONFIG" --sha256 2>/dev/null || true)" \
     python3 - > "${RESULT_JSON}.tmp" <<'PY' && mv "${RESULT_JSON}.tmp" "$RESULT_JSON"
import json, os, socket
from datetime import datetime, timezone
gates = []
for line in os.environ.get("PF_LINES", "").splitlines():
    if line.startswith("[") and "]" in line:
        v, m = line[1:].split("]", 1)
        gates.append({"verdict": v, "msg": m.strip()})
print(json.dumps({
    "schema": 1,
    "event_id": os.environ.get("PF_EVENT", ""),
    "config": os.environ.get("PF_CONFIG", ""),
    "config_sha256": os.environ.get("PF_CFG_SHA", ""),
    "keywords_sha256": os.environ.get("PF_KW_SHA", ""),
    "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "host": socket.gethostname().split(".")[0],
    "pass": int(os.environ.get("PF_FAILS", "1")) == 0,
    "fails": int(os.environ.get("PF_FAILS", "0")),
    "warns": int(os.environ.get("PF_WARNS", "0")),
    "fire_hhmm": os.environ.get("PF_FIRE", ""),
    "event_env": os.environ.get("PF_ENV", ""),
    "gates": gates,
    "gates_skipped": [s for s in os.environ.get("PF_SKIPPED", "").split(",") if s],
}, indent=1))
PY
  then
    echo "preflight-result: $RESULT_JSON"
  else
    echo "[WARN] could not write $RESULT_JSON — assert-ready.sh will refuse until a preflight writes it" >&2
    rm -f "${RESULT_JSON}.tmp" 2>/dev/null || true
  fi
fi
if [[ -n "${EVENT_ID:-}" ]]; then
  "$(dirname "$0")/journal.sh" --event "$EVENT_ID" --actor preflight-event.sh --action preflight \
    --outcome "$([[ $FAILS -eq 0 ]] && echo ok || echo fail)" \
    --detail "$FAILS fails, $WARNS warns" --log "$RESULT_JSON" || true
fi
if [[ "$FAILS" -eq 0 ]]; then
  echo "PREFLIGHT PASS — pinned binary + environment verified for ${EVENT_ID:-?}"
else
  echo "PREFLIGHT: $FAILS FAILURE(S) — fix before the event" >&2
  exit 1
fi
