#!/usr/bin/env bash
# Per-event lifecycle journal — the single index that answers "why did event X fail".
#   journal.sh --event <EVENT_ID> --actor <name> --action <verb> --outcome <o> \
#              [--detail <text>] [--log <path>]
#
# Appends ONE JSON line to state/<EVENT_ID>/journal.jsonl. Every actor that
# touches an event calls this as a subprocess (the notify.sh pattern): works
# identically from bash 3.2 (laptop), bash 5 (box), launchd, systemd, python.
#
# Action vocabulary (v1): stage study readiness preflight timer-install
#   watch-start url pcm-gate launch-start arm-start listener-ready dial
#   supervisor-start redial stream live arm-exit arm-handoff autorun-start
#   grade descope note
# Outcome: ok | fail | warn | info | refused | skipped
#
# INVARIANT: this script ALWAYS exits 0. A journaling failure must never kill
# a live event. Callers still append `|| true` as belt-and-braces.
#
# Box copies are pulled post-event to state/<E>/box/journal.jsonl and merged
# by the reader at render time — journals are per-host, never merged on disk.
# Known limitation: actors that fail before an EVENT_ID exists cannot journal
# per-event; they page via notify.sh instead.

REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)}"
[ -n "$REPO" ] || exit 0

EVENT="" ACTOR="" ACTION="" OUTCOME="" DETAIL="" LOGPATH=""
while [ $# -gt 0 ]; do
  case "$1" in
    --event)   EVENT="${2:-}"; shift 2 ;;
    --actor)   ACTOR="${2:-}"; shift 2 ;;
    --action)  ACTION="${2:-}"; shift 2 ;;
    --outcome) OUTCOME="${2:-}"; shift 2 ;;
    --detail)  DETAIL="${2:-}"; shift 2 ;;
    --log)     LOGPATH="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "$EVENT" ] && [ -n "$ACTOR" ] && [ -n "$ACTION" ] && [ -n "$OUTCOME" ] || exit 0

DIR="$REPO/state/$EVENT"
mkdir -p "$DIR" 2>/dev/null || exit 0

TS="$(date -u +%FT%TZ)"
HOST="$(hostname -s 2>/dev/null || echo unknown)"

J_TS="$TS" J_EVENT="$EVENT" J_ACTOR="$ACTOR" J_ACTION="$ACTION" \
J_OUTCOME="$OUTCOME" J_DETAIL="$DETAIL" J_LOG="$LOGPATH" J_HOST="$HOST" J_PID="$$" \
python3 - >> "$DIR/journal.jsonl" 2>/dev/null <<'PY' || true
import json, os
print(json.dumps({
    "ts": os.environ["J_TS"],
    "event": os.environ["J_EVENT"],
    "actor": os.environ["J_ACTOR"],
    "action": os.environ["J_ACTION"],
    "outcome": os.environ["J_OUTCOME"],
    "detail": os.environ.get("J_DETAIL", ""),
    "log": os.environ.get("J_LOG", ""),
    "host": os.environ.get("J_HOST", ""),
    "pid": int(os.environ.get("J_PID", "0")),
}, separators=(",", ":")))
PY

# Typed-ledger bridge (control.run.receipts): mirror this line into the
# active run's receipts.jsonl as a `compat` OBSERVATION — it can never
# advance or kill typed state, and a bridge failure can never reach the
# caller. The dir test is a cheap pre-filter; eventctl itself resolves the
# active run (0 active = clean no-op, ambiguity = suppressed error here).
if [ -d "$REPO/var/runs/$EVENT" ]; then
  python3 "$REPO/scripts/eventctl.py" compat --event "$EVENT" --actor "$ACTOR" \
    --action "$ACTION" --outcome "$OUTCOME" \
    ${DETAIL:+--detail "$DETAIL"} ${LOGPATH:+--log "$LOGPATH"} \
    >/dev/null 2>&1 || true
fi
exit 0
