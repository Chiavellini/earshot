#!/usr/bin/env bash
# xvenue-autorun.sh — record BOTH venues for one event, then normalize + report.
#
# WHY THIS EXISTS (2026-08-04). The cross-venue lane was launched by hand every
# time, which is why the AMD 26AUG04 capture sat on disk for a day with 28,967
# rows and no analysis. This is the unattended version.
#
# ⚠ DELIBERATELY NOT WIRED INTO event-autorun.sh. That crossing would put this
# lane inside `control.supervise` and, more importantly, inside the arm's
# failure domain. It runs on its OWN timer so it can die without costing a
# trade. It places no orders, touches no OrderRegistry, reads no STOP file, and
# never passes --i-am-live. Both recorders are read-only market-data clients.
#
# Both feeds MUST run on this one host: recording Kalshi from one vantage and
# Polymarket from another measures our own network asymmetry, not the venues.
#
# Usage:
#   scripts/xvenue-autorun.sh --event KXEARNINGSMENTIONAMD-26AUG04 \
#       --map configs/pm-map-26aug03.json --secs 5400
#   scripts/xvenue-autorun.sh --check          # offline self-test
#
# Exit: 0 recorded+reported / 1 recorded but nothing comparable / 2 setup failure.
set -uo pipefail
# $HOME/.cargo/bin is NOT on launchd's minimal PATH and the Kalshi recorder is
# a cargo example — omitting it silently loses the whole Kalshi side of every
# unattended run. Caught by --check before it could happen live.
export PATH="$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

cd "$(dirname "$0")/.." || { echo "cannot reach repo root" >&2; exit 2; }

EVENT=""; MAP=""; SECS=5400; OUTROOT="state/xvenue"; CHECK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --event) EVENT="$2"; shift 2 ;;
    --map)   MAP="$2";   shift 2 ;;
    --secs)  SECS="$2";  shift 2 ;;
    --out)   OUTROOT="$2"; shift 2 ;;
    --check) CHECK=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if (( CHECK )); then
  echo "xvenue-autorun self-test:"
  rc=0
  for t in "scripts/pm-tape.py --self-test" \
           "scripts/xvenue-normalize.py --self-test" \
           "scripts/xvenue-report.py --self-test"; do
    # shellcheck disable=SC2086
    if python3 $t >/dev/null 2>&1; then echo "  PASS  $t"; else echo "  FAIL  $t"; rc=1; fi
  done
  for b in cargo python3; do
    if command -v "$b" >/dev/null 2>&1; then echo "  PASS  tool present: $b"
    else echo "  FAIL  tool MISSING: $b"; rc=1; fi
  done
  (( rc )) && { echo "SELFTEST FAIL"; exit 2; }
  echo "SELFTEST PASS"; exit 0
fi

[[ -n "$EVENT" && -n "$MAP" ]] || { echo "usage: $0 --event E --map M [--secs N] | --check" >&2; exit 2; }
[[ -f "$MAP" ]] || { echo "FATAL: no map at $MAP" >&2; exit 2; }

DIR="$OUTROOT/$EVENT"
mkdir -p "$DIR"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
KTAPE="$DIR/kalshi-$STAMP.jsonl"
PTAPE="$DIR/pm-$STAMP.jsonl"

# Credentials are needed for the Kalshi WS handshake only (market data). If they
# are absent, record Polymarket anyway rather than losing the whole session —
# half a tape is evidence, no tape is not.
if [[ -f "$HOME/.kalshi/earshot_env" ]]; then
  # shellcheck disable=SC1091
  source "$HOME/.kalshi/earshot_env"
fi

echo "xvenue-autorun: $EVENT for ${SECS}s → $DIR"

KPID=""
if [[ -n "${KALSHI_API_KEY_ID:-}" ]]; then
  cargo run --release --quiet --example book-tape -- \
      --event "$EVENT" --out "$KTAPE" --secs "$SECS" --label "$EVENT" &
  KPID=$!
else
  echo "WARN: no KALSHI_API_KEY_ID — Kalshi side will NOT be recorded" >&2
fi

python3 scripts/pm-tape.py --map "$MAP" --event "$EVENT" \
    --out "$PTAPE" --secs "$SECS" --label "$EVENT" &
PPID_=$!

# Forward the real signal to both children: launchd and the event timer send
# SIGTERM, and each recorder writes its close row on receipt. A drill on any
# other mechanism validates the wrong path.
trap 'echo "xvenue-autorun: SIGTERM → children"; [[ -n "$KPID" ]] && kill -TERM "$KPID" 2>/dev/null; kill -TERM "$PPID_" 2>/dev/null' TERM INT

[[ -n "$KPID" ]] && wait "$KPID"
wait "$PPID_"

TAPES=()
[[ -s "$KTAPE" ]] && TAPES+=("$KTAPE")
[[ -s "$PTAPE" ]] && TAPES+=("$PTAPE")
if [[ ${#TAPES[@]} -eq 0 ]]; then
  echo "FATAL: no tape produced" >&2
  exit 2
fi

NORM="$DIR/norm-$STAMP.jsonl"
python3 scripts/xvenue-normalize.py --pair "$EVENT" --map "$MAP" --event "$EVENT" \
    --out "$NORM" "${TAPES[@]}" || { echo "normalize failed" >&2; exit 2; }

python3 scripts/xvenue-report.py --norm "$NORM" --pair "$EVENT" \
    --out "$DIR/report-$STAMP.json"
RC=$?

# Compress the finished tapes. They are written uncompressed so a hard kill
# leaves them readable; once closed, they are archival.
gzip -f "${TAPES[@]}" 2>/dev/null || true

echo "xvenue-autorun: done (report rc=$RC)"
exit $RC
