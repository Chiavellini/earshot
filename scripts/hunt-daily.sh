#!/usr/bin/env bash
# Daily bridge hunt — the scheduled half of hunt-dial-in.py.
#
# WHY THIS IS A SCHEDULE AND NOT A COMMAND. On 2026-07-30 we could phone-arm
# ZERO of 47 open earnings events: 39 issuers had no registry entry, and the
# recon worklist listed them with an EMPTY "start from" column — flagged for
# weeks, actioned never, because the hunt was a human errand nobody ran. The
# fix is not a better errand. Earnings events list 4-7 days ahead (measured
# 2026-07-30), so a daily sweep has real lead time to find a number, leaving
# the probe — the only thing that can set `admission` — days rather than
# minutes before the call.
#
# Fired by launchd (configs/launchd/com.earshot.hunt-daily.plist, 20:30 local,
# i.e. after slate-daily has refreshed the slate this reads). Manual:
# --dry-run prints every decision, commits nothing, alerts nothing.
#
# Steps:
#   1. hunt-dial-in.py --self-test (frozen CMG/YUM/Ford/replay fixtures) — a
#      classifier regression must never reach a live sweep, because its
#      failure mode is handing a REPLAY line to an auto-prober.
#   2. Live sweep over the slate horizon → state/hunt/*.json (digits,
#      gitignored) + slate/hunt-status.json (digits-free, committed).
#   3. Alert on newly-found bridges, on issuers still unresolved inside the
#      escalation window, and on ANY step failing.
#   4. Commit slate/hunt-status.json ONLY when its content changed.
#
# NO DIGITS EVER REACH GIT. Step 4 commits one file and that file is asserted
# digit-free before the commit (docs/DIAL-INS.md:135). NO CALLS ARE PLACED —
# this script only reads SEC.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
TODAY="$(date -u +%F)"
STATUS="slate/hunt-status.json"

alert() {
  if [ "$DRY" = 1 ]; then
    echo "[dry-run] ALERT: $1"; echo "$2" | sed 's/^/[dry-run]   /'
  else
    scripts/notify.sh "$1" "$2" --quiet || echo "warn: notify.sh failed" >&2
  fi
}
fail() { alert "hunt-daily FAILED" "$1"; echo "FAIL: $1" >&2; exit 1; }

echo "=== hunt-daily $(date -u +%FT%TZ) dry_run=$DRY ==="

# 1. Regression gate BEFORE any live work.
python3 scripts/hunt-dial-in.py --self-test \
  || fail "hunt-dial-in.py --self-test FAILED — classifier regression; refusing to sweep"

# 1b. The slate is this sweep's horizon. A stale one silently narrows what we
#     hunt to whatever was listed days ago, and the failure is invisible: the
#     sweep succeeds, reports issuers, and simply never mentions the events it
#     could not see. This is not hypothetical — slate-daily failed on a Kalshi
#     read timeout on 2026-07-31 and the machine was off on 07-30, leaving
#     slate/latest.json two days behind while looking perfectly well-formed.
SLATE="slate/latest.json"
[ -f "$SLATE" ] || fail "$SLATE missing — run scripts/slate-daily.sh first"
SLATE_AGE_H="$(SLATE="$SLATE" python3 - <<'PY'
import datetime, json, os, sys
try:
    stamp = json.load(open(os.environ["SLATE"]))["generated_utc"]
    gen = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
except Exception:
    print(9999); sys.exit(0)
now = datetime.datetime.now(datetime.timezone.utc)
print(int((now - gen).total_seconds() // 3600))
PY
)"
if [ "$SLATE_AGE_H" -gt "${MAX_SLATE_AGE_H:-30}" ]; then
  fail "$SLATE is ${SLATE_AGE_H}h old (bar ${MAX_SLATE_AGE_H:-30}h) — refusing to \
sweep a stale horizon. Run scripts/slate-daily.sh, then re-run. Override with \
MAX_SLATE_AGE_H=<hours> only if you know why the slate is old."
fi
echo "slate age: ${SLATE_AGE_H}h (bar ${MAX_SLATE_AGE_H:-30}h)"

# 2. Baseline from git, not from disk, so a hand-edited file cannot fake a
#    no-change day (the slate-daily pattern).
PREV="$(mktemp)"; trap 'rm -f "$PREV"' EXIT
git show "HEAD:$STATUS" > "$PREV" 2>/dev/null || echo '{"issuers":{}}' > "$PREV"

OUT="$(python3 scripts/hunt-dial-in.py --from-slate "$SLATE" --days 12 \
        --state-dir state/hunt --out "$STATUS" 2>&1)" \
  || fail "hunt sweep exited nonzero — see the launchd log"
echo "$OUT"

# 3. Alerts. A newly-found bridge is the event worth waking someone for: it
#    converts a webcast-only event into a probe-able phone lane.
NEWFOUND="$(python3 - "$PREV" "$STATUS" <<'PY'
import json, sys
old = json.load(open(sys.argv[1])).get("issuers", {})
new = json.load(open(sys.argv[2])).get("issuers", {})
for k, v in sorted(new.items()):
    if v.get("found") and not old.get(k, {}).get("found"):
        print(f"HUNT-NEW: {k} — {v.get('evidence')} ({v.get('number_src')})")
PY
)"
[ -n "$NEWFOUND" ] && echo "$NEWFOUND"

# Digit-leak assertion: the committed artifact must never carry a phone number
# or a passcode. This is the enforcement the digits policy never had.
# Walk the structure rather than regexing the file. A blind scan flags the
# microsecond field of an ISO timestamp and the CIK inside a sec.gov URL, and
# a guard that fires on every clean run is a guard everyone learns to bypass.
LEAK="$(python3 - "$STATUS" <<'PY'
import json, re, sys
SAFE_KEYS = {"generated_utc", "hunted_utc", "last_hunted", "number_src", "filing_url"}
# 6..15, not 6..10: an E.164 number is 11 digits (+1 plus ten), so a {6,10}
# bound lets the most dialable form of all through. Kept in step with the
# registry guard in scripts/doc-lint.py.
PHONEY = re.compile(r"\b\d{3}[-.\s/]\d{3}[-.\s/]\d{4}\b|\b\d{6,15}\b")
bad = set()
def walk(o, key=None):
    if isinstance(o, dict):
        for k, v in o.items():
            walk(v, k)
    elif isinstance(o, list):
        for x in o:
            walk(x, key)
    elif isinstance(o, str) and key not in SAFE_KEYS:
        for m in PHONEY.finditer(re.sub(r"https?://\S+", " ", o)):
            bad.add(m.group(0))
walk(json.load(open(sys.argv[1])))
print(" ".join(sorted(bad)))
PY
)"
if [ -n "$LEAK" ]; then
  fail "DIGITS LEAKED into $STATUS: $LEAK — refusing to commit (docs/DIAL-INS.md:135)"
fi

if [ -n "$NEWFOUND" ]; then
  alert "hunt: new dial-in bridge(s) found" "$NEWFOUND

Next: probe before trusting (THE MSFT RULE) —
  scripts/bridge-probe.sh --to <number> --label <key>-$TODAY --account ~/.earshot/<acct>.env
Digits are in state/hunt/ (gitignored)."
fi

# 4. Commit the digits-free half only, and only on content change.
if [ "$DRY" = 1 ]; then
  echo "[dry-run] skipping commit; $STATUS left as-written"
  exit 0
fi
if ! git diff --quiet -- "$STATUS" 2>/dev/null || [ -n "$(git status --porcelain -- "$STATUS")" ]; then
  # generated_utc alone must not count as a change (slate-daily's rule)
  if python3 - "$PREV" "$STATUS" <<'PY'
import json, sys
a = json.load(open(sys.argv[1])).get("issuers", {})
b = json.load(open(sys.argv[2])).get("issuers", {})
sys.exit(0 if a == b else 1)
PY
  then
    git checkout -q -- "$STATUS" 2>/dev/null || true
    echo "content unchanged — no commit"
  else
    n_found="$(python3 -c "import json;print(sum(1 for v in json.load(open('$STATUS'))['issuers'].values() if v.get('found')))")"
    git add "$STATUS" && git commit -q -m "hunt: auto $TODAY — $n_found issuer(s) with a published dial-in" -- "$STATUS" \
      || fail "git commit of $STATUS failed"
    echo "committed $STATUS ($TODAY)"
  fi
else
  echo "no change to $STATUS"
fi
