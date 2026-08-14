#!/usr/bin/env bash
# urlfile-wait — the box side of the resolver split: block until THIS attempt's
# url-file has been pushed here, then exit 0 so arm-on-url proceeds to PCM proof.
#
#   scripts/urlfile-wait.sh $HOME/<slug>-stream.url \
#     --deadline-utc <ISO> --expect-plan-sha256 <hex64> \
#     [--fresh-secs N] [--poll-secs S]
#   scripts/urlfile-wait.sh --check          # offline self-test, no arguments
#
# WHY THIS EXISTS (2026-08-13, the box-webcast migration): the us-east box has
# no playwright and no operator to pass a registration wall, so browser-class
# venues are resolved on the laptop and PUSHED here (scripts/push-url-file.sh).
# This is the AccessPlan resolver leg for those events — it mints nothing, it
# waits.
#
# WHY IT KEYS ON THE PLAN HASH, NOT ON mtime: seam S-A requires that a url-file
# left by a PRIOR attempt be refused. The path is derived from the pinned
# config, so every attempt of one event reuses it — AMAT 2026-08-13 has two
# attempts 11 minutes apart, same $HOME/amat-stream.url, same 20:45:00Z
# deadline. No mtime window wide enough to cover the T-75 fire window can tell
# them apart. The push writes a sidecar <url-file>.meta naming the exact
# run_plan_sha256 it was minted for; that is the predicate. Freshness stays as a
# sanity bound only.
#
# WHY IT FAILS CLOSED ON A MISSING DEADLINE: arm-on-url runs the resolver with
# no timeout and only tests the deadline AFTER the child returns, so a blocking
# resolver that invents its own bound would sail straight through the call. The
# deadline is bound into the hash-covered argv; without one this refuses to run.
#
# Log tags: URLFILE-WAIT URLFILE-FRESH URLFILE-STALE URLFILE-WRONG-ATTEMPT
#           URLFILE-DEADLINE. The URL itself is NEVER printed — everything here
#           lands in state/<EVENT>/arm-on-url-<RUN>.log, which pull-box-state.sh
#           copies back to the laptop; url-sha is the durable identifier
#           (pcm-gate.sh sets that precedent).
# Exit 0 a fresh url-file for this attempt is present / 3 deadline reached /
# 2 usage. Never silent.
# Node: control.acquire (compatibility surface: $HOME/<slug>-stream.url).
set -uo pipefail

URLFILE="" DEADLINE_UTC="" EXPECT_SHA="" FRESH_SECS="" POLL_SECS=2 CHECK=0
FUTURE_SKEW_SECS=300
HEARTBEAT_SECS=30

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deadline-utc) DEADLINE_UTC="${2:?}"; shift 2 ;;
    --expect-plan-sha256) EXPECT_SHA="${2:?}"; shift 2 ;;
    --fresh-secs) FRESH_SECS="${2:?}"; shift 2 ;;
    --poll-secs) POLL_SECS="${2:?}"; shift 2 ;;
    --check) CHECK=1; shift ;;
    -h|--help) sed -n '2,44p' "$0"; exit 0 ;;
    -*) echo "unknown arg: $1" >&2; exit 2 ;;
    *)
      [[ -z "$URLFILE" ]] || { echo "USAGE-ERROR: exactly one url-file" >&2; exit 2; }
      URLFILE="$1"; shift ;;
  esac
done

# probe <url-file> <expect-sha> <min-mtime-epoch> <max-mtime-epoch>
# Prints one verdict token plus a non-secret detail. Every filesystem and hash
# decision lives here so the shell never has to reach for `stat -c`/`stat -f`
# (they disagree between the macOS laptop and the Linux box).
probe() {
  URLFILE="$1" EXPECT_SHA="$2" MIN_MTIME="$3" MAX_MTIME="$4" python3 - <<'PY'
import hashlib, json, os, sys
from pathlib import Path

target = Path(os.environ["URLFILE"])
expect = os.environ["EXPECT_SHA"]
lo, hi = int(os.environ["MIN_MTIME"]), int(os.environ["MAX_MTIME"])


def out(verdict, detail=""):
    print(f"{verdict} {detail}".rstrip())
    raise SystemExit(0)


if target.is_symlink():
    out("NOT-REGULAR", "url-file is a symlink")
if not target.exists():
    out("ABSENT")
if not target.is_file():
    out("NOT-REGULAR", "url-file is not a regular file")
raw = target.read_bytes()
if not raw.strip():
    out("EMPTY")
first = raw.decode("utf-8", "replace").splitlines()[0].strip()
if not (first.startswith("http://") or first.startswith("https://")):
    out("NOT-URL", "first line is not an http(s) endpoint")
url_sha = hashlib.sha256(raw).hexdigest()

sidecar = Path(str(target) + ".meta")
if not sidecar.is_file() or sidecar.is_symlink():
    out("NO-SIDECAR", f"{sidecar.name} is missing")
try:
    meta = json.loads(sidecar.read_text())
except (OSError, json.JSONDecodeError) as exc:
    out("BAD-SIDECAR", f"unreadable: {exc}")
if not isinstance(meta, dict):
    out("BAD-SIDECAR", "not a JSON object")
if meta.get("run_plan_sha256") != expect:
    # The exact prior-attempt case: same path, same deadline, different plan.
    out("WRONG-ATTEMPT", f"sidecar names run_plan_sha256={str(meta.get('run_plan_sha256'))[:12]}")
if meta.get("url_sha256") != url_sha:
    out("SHA-MISMATCH", "sidecar and url-file were not published together")

mtime = int(target.stat().st_mtime)
if mtime > hi:
    out("FUTURE", f"mtime is {mtime - hi}s beyond tolerated clock skew")
if mtime < lo:
    out("STALE", f"mtime is {lo - mtime}s older than the freshness bound")
out("FRESH", f"url-sha={url_sha[:12]} pushed={meta.get('pushed_utc')}")
PY
}

epoch_of() {  # ISO-8601 UTC -> epoch seconds, or empty on refusal
  python3 - "$1" <<'PY'
import datetime as dt, sys
raw = sys.argv[1]
try:
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValueError("not UTC")
    print(int(parsed.timestamp()))
except ValueError:
    pass
PY
}

if (( CHECK )); then
  # Offline self-test: no network, no clock dependence beyond `now`.
  P=0; F=0
  chk() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; P=$((P+1));
          else echo "  FAIL  $1 (expected $2, got $3)"; F=$((F+1)); fi; }
  T="$(mktemp -d "${TMPDIR:-/tmp}/urlfile-wait-check.XXXXXX")"
  trap 'rm -rf -- "$T"' EXIT
  echo "urlfile-wait self-test:"
  U="$T/x-stream.url"
  SHA_A=$(printf 'a%.0s' $(seq 1 64))
  SHA_B=$(printf 'b%.0s' $(seq 1 64))
  NOW=$(python3 -c 'import time;print(int(time.time()))')
  LO=$((NOW - 5400)); HI=$((NOW + 300))
  meta_for() {  # meta_for <url-file> <plan-sha>
    URLF="$1" PLAN="$2" python3 - <<'PY'
import hashlib, json, os
from pathlib import Path
p = Path(os.environ["URLF"])
Path(str(p) + ".meta").write_text(json.dumps({
    "schema": 1, "kind": "url-file-push", "event_id": "E", "run_id": "R",
    "run_plan_sha256": os.environ["PLAN"], "pushed_utc": "2026-08-13T20:00:00Z",
    "url_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
}, sort_keys=True) + "\n")
PY
  }

  chk "absent url-file"        ABSENT        "$(probe "$U" "$SHA_A" "$LO" "$HI" | cut -d' ' -f1)"
  printf 'https://example.invalid/a.m3u8\n' > "$U"
  chk "no sidecar"             NO-SIDECAR    "$(probe "$U" "$SHA_A" "$LO" "$HI" | cut -d' ' -f1)"
  meta_for "$U" "$SHA_B"
  chk "prior attempt refused"  WRONG-ATTEMPT "$(probe "$U" "$SHA_A" "$LO" "$HI" | cut -d' ' -f1)"
  meta_for "$U" "$SHA_A"
  chk "this attempt accepted"  FRESH         "$(probe "$U" "$SHA_A" "$LO" "$HI" | cut -d' ' -f1)"
  printf 'https://example.invalid/b.m3u8\n' > "$U"
  chk "url edited after push"  SHA-MISMATCH  "$(probe "$U" "$SHA_A" "$LO" "$HI" | cut -d' ' -f1)"
  meta_for "$U" "$SHA_A"
  python3 -c 'import os,sys;os.utime(sys.argv[1],(0,0))' "$U"
  chk "stale by mtime"         STALE         "$(probe "$U" "$SHA_A" "$LO" "$HI" | cut -d' ' -f1)"
  python3 -c 'import os,sys,time;t=int(time.time())+9999;os.utime(sys.argv[1],(t,t))' "$U"
  chk "mtime beyond skew"      FUTURE        "$(probe "$U" "$SHA_A" "$LO" "$HI" | cut -d' ' -f1)"
  printf 'not-a-url\n' > "$U"; meta_for "$U" "$SHA_A"
  chk "non-http body"          NOT-URL       "$(probe "$U" "$SHA_A" "$LO" "$HI" | cut -d' ' -f1)"
  : > "$U"; chk "empty url-file" EMPTY       "$(probe "$U" "$SHA_A" "$LO" "$HI" | cut -d' ' -f1)"
  rm -f "$U"; ln -s /nonexistent "$U"
  chk "symlink refused"        NOT-REGULAR   "$(probe "$U" "$SHA_A" "$LO" "$HI" | cut -d' ' -f1)"
  chk "epoch parse"            1755115200    "$(epoch_of 2025-08-13T20:00:00Z)"
  chk "epoch refuses naive"    ""            "$(epoch_of 2025-08-13T20:00:00)"
  echo
  (( F )) && { echo "SELFTEST FAIL ($F failed, $P passed)"; exit 1; }
  echo "SELFTEST PASS ($P checks)"; exit 0
fi

[[ -n "$URLFILE" ]] || { echo "USAGE-ERROR: a url-file path is required" >&2; exit 2; }
[[ -n "$DEADLINE_UTC" ]] || {
  echo "USAGE-ERROR: --deadline-utc is required; this wait never invents its own bound" >&2
  exit 2
}
[[ "$EXPECT_SHA" =~ ^[0-9a-f]{64}$ ]] || {
  echo "USAGE-ERROR: --expect-plan-sha256 must be the RunPlan's hex64 plan_sha256" >&2
  exit 2
}
[[ "$POLL_SECS" =~ ^[1-9][0-9]*$ ]] || { echo "USAGE-ERROR: --poll-secs must be a positive integer" >&2; exit 2; }
FRESH_SECS="${FRESH_SECS:-${EARSHOT_URLFILE_FRESH_SECS:-5400}}"
[[ "$FRESH_SECS" =~ ^[1-9][0-9]*$ ]] || { echo "USAGE-ERROR: --fresh-secs must be a positive integer" >&2; exit 2; }
# The env var may only NARROW the bound the AccessPlan bound into argv: a
# fire-time variable must not be able to widen what the immutable plan fixed.
if [[ -n "${EARSHOT_URLFILE_FRESH_SECS:-}" ]] \
   && [[ "$EARSHOT_URLFILE_FRESH_SECS" =~ ^[1-9][0-9]*$ ]] \
   && (( EARSHOT_URLFILE_FRESH_SECS < FRESH_SECS )); then
  FRESH_SECS="$EARSHOT_URLFILE_FRESH_SECS"
fi

DEADLINE=$(epoch_of "$DEADLINE_UTC")
[[ -n "$DEADLINE" ]] || { echo "USAGE-ERROR: --deadline-utc must be UTC ISO-8601 (…Z)" >&2; exit 2; }
# Same rule for the deadline: arm-on-url's own bound may narrow, never widen.
if [[ -n "${RESOLVER_DEADLINE_EPOCH:-}" ]] \
   && [[ "$RESOLVER_DEADLINE_EPOCH" =~ ^[0-9]+$ ]] \
   && (( RESOLVER_DEADLINE_EPOCH < DEADLINE )); then
  DEADLINE="$RESOLVER_DEADLINE_EPOCH"
fi

START=$(python3 -c 'import time;print(int(time.time()))')
echo "=== urlfile-wait $(date -u +%FT%TZ) file=$URLFILE attempt=${EXPECT_SHA:0:12} deadline=$DEADLINE_UTC fresh=${FRESH_SECS}s"
LAST_VERDICT="" LAST_BEAT="$START"
while :; do
  NOW=$(python3 -c 'import time;print(int(time.time()))')
  VERDICT_LINE=$(probe "$URLFILE" "$EXPECT_SHA" "$((NOW - FRESH_SECS))" "$((NOW + FUTURE_SKEW_SECS))")
  VERDICT="${VERDICT_LINE%% *}"
  DETAIL="${VERDICT_LINE#"$VERDICT"}"; DETAIL="${DETAIL# }"
  if [[ "$VERDICT" == FRESH ]]; then
    echo "URLFILE-FRESH $URLFILE $DETAIL after $((NOW - START))s"
    exit 0
  fi
  # One line per state CHANGE, plus a heartbeat, so a long wait is visible in
  # the fire-time log without drowning it (the C9 lesson: 110 silent minutes).
  if [[ "$VERDICT" != "$LAST_VERDICT" ]]; then
    case "$VERDICT" in
      WRONG-ATTEMPT) echo "URLFILE-WRONG-ATTEMPT $DETAIL — this run expects ${EXPECT_SHA:0:12}; refusing a prior attempt's file" ;;
      STALE|FUTURE)  echo "URLFILE-STALE $VERDICT $DETAIL" ;;
      *)             echo "URLFILE-WAIT $VERDICT $DETAIL" ;;
    esac
    LAST_VERDICT="$VERDICT"
  elif (( NOW - LAST_BEAT >= HEARTBEAT_SECS )); then
    echo "URLFILE-WAIT $VERDICT — $((NOW - START))s waited, $((DEADLINE - NOW))s to deadline"
    LAST_BEAT="$NOW"
  fi
  if (( NOW >= DEADLINE )); then
    echo "URLFILE-DEADLINE no fresh url-file for attempt ${EXPECT_SHA:0:12} by $DEADLINE_UTC (last verdict: $VERDICT $DETAIL)" >&2
    exit 3
  fi
  sleep "$POLL_SECS"
done
