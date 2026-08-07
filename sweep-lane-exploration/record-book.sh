#!/usr/bin/env bash
# record-book.sh — bank a Kalshi ORDER BOOK tape into this lane.
#
# WHY. Every fill in this study is credited because somebody else took that
# offer. The public trades tape cannot tell "an offer rested there and we could
# have had it" from "we would have lost the race for it", and that distinction
# is the single assumption the whole intra-sweep arm rests on
# (PRE-REGISTRATION-R2.md §R8). Only a book tape settles it. None exists: both
# banked cross-venue events backfilled their Kalshi side from the trades
# archive (`state/xvenue/*/norm.jsonl.meta.json`, feed_kind "archive"), so
# `examples/book-tape.rs` has never actually recorded a live session.
#
# WHAT IT DOES NOT DO. `book-tape` references no order endpoint, no
# OrderRegistry and no STOP file; it subscribes to `orderbook_delta` and writes
# bytes. This is NOT the recorder the repo's "never point a recorder at a
# tradeable call" rule is about — that rule governs AUDIO capture displacing an
# arm (bridge-probe / chain-capture bank corpus with no detector and no
# orders). This runs ALONGSIDE an arm, never instead of one, which is what
# book-tape.rs's own header says it is for.
#
# ISOLATION. Writes only under this directory. `--out` is validated, not
# trusted: state/ must stay untouched (the lane's isolation guarantee).
#
# ⚠ Per chiavellini.md §7.5, three parallel investigation agents once drove the
#   box to load 100 during a live arm and cost 9,395 dropped audio frames. This
#   is one extra WebSocket subscription and one writer — but nothing else of
#   this lane's may run on the box during a call.
#
# Usage:
#   ./record-book.sh --event KXEARNINGSMENTIONXYZ-26AUG12 --secs 5400
#   ./record-book.sh --tickers T1,T2 --secs 120 --label smoke
set -euo pipefail

LANE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$LANE/.." && pwd)"
DEST="$LANE/book-tapes"

EVENT=""; TICKERS=""; SECS=300; LABEL=""; STALE_OK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --event)   EVENT="$2"; shift 2 ;;
    --tickers) TICKERS="$2"; shift 2 ;;
    --secs)    SECS="$2"; shift 2 ;;
    --label)   LABEL="$2"; shift 2 ;;
    # Run an EXISTING binary when src/ will not compile. Never for an event:
    # a stale box binary found at T-60 is what cost the 26AUG05 race. The tape
    # records which binary produced it via book-tape's own `git_sha`.
    --allow-stale-binary) STALE_OK=1; shift ;;
    *) echo "record-book: unknown argument $1" >&2; exit 2 ;;
  esac
done
[[ -n "$EVENT" || -n "$TICKERS" ]] || { echo "record-book: need --event or --tickers" >&2; exit 2; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="${EVENT:-${LABEL:-tickers}}"
OUT="$DEST/${NAME}-${STAMP}.jsonl"

# The isolation guarantee, enforced rather than trusted.
case "$(cd "$DEST" 2>/dev/null && pwd || echo "$DEST")" in
  "$LANE"/*) ;;
  *) echo "record-book: REFUSING to write outside the lane: $DEST" >&2; exit 1 ;;
esac
mkdir -p "$DEST"

if [[ ! -f "$HOME/.kalshi/earshot_env" ]]; then
  echo "record-book: no ~/.kalshi/earshot_env — the WS handshake needs credentials" >&2
  exit 1
fi
# shellcheck disable=SC1091
source "$HOME/.kalshi/earshot_env"
[[ -n "${KALSHI_API_KEY_ID:-}" ]] || { echo "record-book: KALSHI_API_KEY_ID unset" >&2; exit 1; }

cd "$REPO"
BIN="./target/release/examples/book-tape"
# Build first and separately: a compile that starts after the board opens costs
# the recording, and cargo's own output would otherwise land inside the tape's
# start window.
echo "record-book: building book-tape…"
if ! cargo build --release --quiet --example book-tape; then
  if [[ "$STALE_OK" == "1" && -x "$BIN" ]]; then
    echo "record-book: ⚠ BUILD FAILED — running the EXISTING binary because" >&2
    echo "record-book: ⚠ --allow-stale-binary was passed. This validates the" >&2
    echo "record-book: ⚠ RECORDER MECHANISM ONLY, not the production path." >&2
    echo "record-book: ⚠ binary mtime: $(date -r "$BIN" -u +%Y-%m-%dT%H:%M:%SZ)" >&2
  else
    echo "record-book: build failed and no --allow-stale-binary. Refusing." >&2
    exit 1
  fi
fi
[[ -x "$BIN" ]] || { echo "record-book: no book-tape binary at $BIN" >&2; exit 1; }

ARGS=(--out "$OUT" --secs "$SECS")
[[ -n "$EVENT"   ]] && ARGS+=(--event "$EVENT")
[[ -n "$TICKERS" ]] && ARGS+=(--tickers "$TICKERS")
[[ -n "$LABEL"   ]] && ARGS+=(--label "$LABEL")

echo "record-book: ${NAME} for ${SECS}s → $OUT"
"$BIN" "${ARGS[@]}" &
PID=$!
# SIGTERM is what launchd, the event timer and `kill` actually send; book-tape
# writes its `close` row on receipt. Forwarding anything else validates the
# wrong path (docs/RUNBOOK.md hardening notes).
trap 'echo "record-book: SIGTERM → book-tape"; kill -TERM "$PID" 2>/dev/null || true' TERM INT
wait "$PID" || true

# A tape without a `close` row is truncated, and a truncated tape read as a
# complete one is how "no resting size" becomes a false finding.
if [[ ! -s "$OUT" ]]; then
  echo "record-book: FAIL — empty tape at $OUT" >&2; exit 1
fi
python3 - "$OUT" <<'PY'
import json, sys
path = sys.argv[1]
rows = [json.loads(l) for l in open(path) if l.strip()]
kinds = {}
for r in rows:
    kinds[r.get("t", "?")] = kinds.get(r.get("t", "?"), 0) + 1
close = next((r for r in rows if r.get("t") == "close"), None)
sess = next((r for r in rows if r.get("t") == "session"), None)
print(f"record-book: rows={len(rows)} {kinds}")
if sess:
    print(f"  markets={len(sess.get('tickers') or [])} schema={sess.get('schema_version')} "
          f"clock_offset_s={sess.get('clock_offset_s')}")
if close is None:
    print("record-book: FAIL — no close row; tape is TRUNCATED", file=sys.stderr); sys.exit(1)
print(f"  close: reason={close.get('reason')} frames={close.get('rows')} "
      f"dropped={close.get('dropped')}")
if close.get("dropped"):
    print("record-book: WARNING — frames dropped, tape is INCOMPLETE", file=sys.stderr)
if not close.get("rows"):
    print("record-book: FAIL — zero frames recorded", file=sys.stderr); sys.exit(1)
PY
echo "record-book: OK → $OUT"
