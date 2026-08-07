#!/usr/bin/env bash
# post-event-report.sh — mail ONE verdict after an event, from state/ alone.
#
#   scripts/post-event-report.sh --event KXEARNINGSMENTIONCELH-26AUG06-WEBCAST [--date 2026-08-06]
#
# WHY: an unattended run currently pages only on FAILURE paths (URL-MISSED,
# refusals, timeouts). Silence is therefore ambiguous — it reads identically to
# "it worked" and to "the watcher never fired at all" (the 2026-08-05 class,
# where every 08-06 job was scheduled into the past with runs=0 and would have
# emitted nothing whatsoever). This job always speaks: it reports the outcome
# whether or not anything went wrong, so an absent email is itself a signal.
#
# Deliberately depends on NOTHING but state/ and the repo scripts — no live
# session, no interactive agent. Read-only. Always exits 0 and always attempts
# to mail; a reporting job that can fail closed would reintroduce the silence.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

EVENT="" DATE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --event) EVENT="${2:?}"; shift 2 ;;
    --date)  DATE="${2:?}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 0 ;;
  esac
done
[[ -n "$EVENT" ]] || { echo "usage: post-event-report.sh --event <EVENT_ID> [--date YYYY-MM-DD]" >&2; exit 0; }
DATE="${DATE:-$(date -u +%F)}"
SD="state/$EVENT"

# ── did the watcher even run? (the silence-is-ambiguous case) ───────────────
SLUG=$(printf '%s' "$EVENT" | sed -E 's/^KXEARNINGSMENTION([A-Z0-9]+)-.*/\1/' | tr 'A-Z' 'a-z')
DP=$(printf '%s' "$EVENT" | sed -E 's/^[^-]*-([0-9]{2}[A-Z]{3}[0-9]{2}).*/\1/' | tr 'A-Z' 'a-z')
WATCH_LOG="$HOME/Library/Logs/earshot/com.earshot.${SLUG}-${DP}-watch.log"
# An EMPTY log counts as "never fired", not as "fired quietly" — the file can
# exist because something touched it (a log tail, a monitor) without launchd
# ever having run the job. Only bytes prove a run.
WLINES=0
[[ -f "$WATCH_LOG" ]] && WLINES=$(wc -l <"$WATCH_LOG" | tr -d ' ')
RUNS=$(launchctl print "gui/$(id -u)/com.earshot.${SLUG}-${DP}-watch" 2>/dev/null | sed -n 's/.*runs = \([0-9]*\).*/\1/p' | head -1)
if [[ "$WLINES" -gt 0 ]]; then
  RAN="watcher RAN — $WLINES log lines; launchd runs=${RUNS:-?}"
else
  RAN="*** WATCHER PRODUCED NO OUTPUT (log lines=$WLINES, launchd runs=${RUNS:-?}) — treat as DID NOT FIRE.
    This is the 2026-08-05 class: a job whose StartCalendarInterval is already in
    the past loads fine and shows in launchctl list, but runs=0 forever.
    Check: launchctl print gui/$(id -u)/com.earshot.${SLUG}-${DP}-watch | grep -A6 descriptor ***"
fi

# ── orders: count send_ts, NEVER .orders|length (26AUG05 miscount) ──────────
ORDERS=$(python3 - "$SD/report.json" <<'PY' 2>/dev/null || echo "report.json unreadable or absent"
import json, sys
try:
    j = json.load(open(sys.argv[1]))
except Exception:
    print("report.json unreadable or absent"); raise SystemExit
o = j.get("orders") or []
sent = [x for x in o if x.get("send_ts") is not None]
det = j.get("detections") or j.get("decisions") or []
print(f"detections/decisions : {len(det)}")
print(f"orders decided       : {len(o)}")
print(f"orders ACTUALLY SENT : {len(sent)}   <- the only number that means a real order")
for x in sent:
    print(f"   SENT {x.get('market_ticker','?')} @ {x.get('price_cents','?')}c x{x.get('count','?')} send_ts={x.get('send_ts')}")
if o and not sent:
    print("   (decided but none reached the wire — 1c non-crossing orders never send;")
    print("    a 97c order can also refuse on the 100c event-notional cap after the first)")
PY
)

# ── did it actually HEAR the call? ─────────────────────────────────────────
# The watcher check above is job liveness, and job liveness is not run
# liveness. AAOI-26AUG06 armed twice, lost every leg within ~32 s both times,
# and its watcher log was full — so this report mailed "sent=0", which reads
# identically to a healthy call where nobody said the keyword.
#
# run-verdict.sh already IS the audio assertion (FAIL "ARMED BUT DEAF" on
# frames_in=0, WARN on a lost leg or a drop rate above threshold, PASS
# otherwise). It was wired only into the PHONE lane (run-arm.sh); the webcast
# lane never called it. Its verdict now drives the SUBJECT LINE, so an
# ambiguous silence cannot survive to the inbox.
VERDICT_OUT=$(scripts/run-verdict.sh "$EVENT" 2>&1); VERDICT_RC=$?
case "$VERDICT_RC" in
  0) VERDICT_TAG=$(printf '%s' "$VERDICT_OUT" | sed -n 's/.*\(PASS\|WARN\) — \([^\t]*\).*/\1/p' | head -1) ;;
  3) VERDICT_TAG="FAIL" ;;
  4) VERDICT_TAG="NO-REPORT" ;;
  *) VERDICT_TAG="UNKNOWN" ;;
esac
[[ -n "$VERDICT_TAG" ]] || VERDICT_TAG="PASS"

# The call window. A run can have frames and still have heard none of the CALL:
# AAOI's first arm died 32 min BEFORE T-0, which a bare frames_in>0 would pass
# on a longer run. The config path is recorded in the journal by
# launch-webcast.sh, so the event's own evidence resolves it — no new argument.
CFG=$(sed -n 's/.*"detail":"config=\([^"]*\)".*/\1/p' "$SD/journal.jsonl" 2>/dev/null | tail -1)
WINDOW="call window: unknown (no config= line in journal.jsonl)"
if [[ -n "$CFG" && -f "$CFG" ]]; then
  WINDOW=$(CFG="$CFG" SD="$SD" python3 <<'PY' 2>/dev/null || echo "call window: unresolved"
import json, os, tomllib
from datetime import datetime, timezone
cfg = tomllib.load(open(os.environ["CFG"], "rb"))
starts = (cfg.get("event") or {}).get("starts_at")
if not starts:
    print("call window: config has no [event].starts_at"); raise SystemExit
t0 = datetime.fromisoformat(starts.replace("Z", "+00:00"))
armed = []
try:
    for ln in open(os.path.join(os.environ["SD"], "journal.jsonl")):
        j = json.loads(ln)
        if j.get("action") == "arm-start":
            armed.append(datetime.fromisoformat(j["ts"].replace("Z", "+00:00")))
except (OSError, ValueError, KeyError):
    pass
if not armed:
    print(f"call window: T-0 {t0:%H:%M}Z — NO arm-start in the journal"); raise SystemExit
# An arm that both started and ended before T-0 never heard the call at all.
last = max(armed)
delta = (last - t0).total_seconds() / 60
when = f"{abs(delta):.0f} min {'after' if delta > 0 else 'before'} T-0"
print(f"call window: T-0 {t0:%Y-%m-%dT%H:%M}Z; last arm-start {last:%H:%M}Z ({when}); "
      f"{len(armed)} arm-start(s)")
PY
)
fi

STATUS=$(scripts/event-status.sh "$EVENT" 2>&1 | head -40 || echo "event-status.sh unavailable")
MISSED=$(python3 scripts/missed-arm-check.py --date "$DATE" 2>&1 | head -15 || echo "missed-arm-check unavailable")

SENT_N=$(printf '%s' "$ORDERS" | sed -n 's/^orders ACTUALLY SENT : \([0-9]*\).*/\1/p' | head -1)
# Audio verdict FIRST in the subject: "sent=0" alone was the ambiguity this
# report was built to kill, and it did not kill it.
SUBJ="$EVENT POST-EVENT — $VERDICT_TAG, sent=${SENT_N:-?}"
BODY="$(printf '%s\n\n== audio verdict ==\n%s\n%s\n\n== fire evidence ==\n%s\n\n== orders ==\n%s\n\n== lifecycle ==\n%s\n\n== missed-arm (%s) ==\n%s\n' \
  "$EVENT" "$VERDICT_OUT" "$WINDOW" "$RAN" "$ORDERS" "$STATUS" "$DATE" "$MISSED")"

printf '%s\n' "$BODY"
# Overridable (see run-verdict.sh): grading an archived run must not mail.
"${EARSHOT_NOTIFY:-scripts/notify.sh}" "$SUBJ" "$BODY" --quiet || true
exit 0
