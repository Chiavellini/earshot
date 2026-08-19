#!/usr/bin/env bash
# event-watchdog.sh — the live-event supervisor nobody had on 2026-08-19 (TGT and TOL were both
# lost and no one knew until 17:46Z). A launchd/systemd one-shot at start+N checks every leg of
# an event on every host and PAGES when a leg is not capturing; a second check near the end
# reports detection/intent counts. It fixes nothing — it makes the silence loud.
#
#   scripts/event-watchdog.sh --install --slug tol --at-utc 12:35 \
#       --leg KXEARNINGSMENTIONTOL-26AUG18-BOX-WEBCAST:20260819T123000Z-p3-816e…:ubuntu@box \
#       --leg KXEARNINGSMENTIONTOL-26AUG18-WEBCAST:20260819T123000Z-p3-e3c3…:local
#   scripts/event-watchdog.sh --run --slug tol --leg … [--leg …]
# Log: state/_day/watchdog-<slug>.log. Node: control.launch.
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; REPO=$(cd "$REPO" && pwd -P); cd "$REPO"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
KEY="${EARSHOT_BOX_KEY:-$HOME/.ssh/earshot-useast1.pem}"
MODE="" SLUG="" AT="" LEGS=()
while [[ $# -gt 0 ]]; do case "$1" in
  --install) MODE=install; shift;; --run) MODE=run; shift;; --slug) SLUG="$2"; shift 2;; --at-utc) AT="$2"; shift 2;;
  --leg) LEGS+=("$2"); shift 2;; *) echo "unknown arg $1" >&2; exit 2;; esac; done
[[ -n "$SLUG" && ${#LEGS[@]} -gt 0 ]] || { echo "USAGE-ERROR: need --slug and >=1 --leg E:RUN:host" >&2; exit 2; }
if [[ "$MODE" == install ]]; then
  [[ -n "$AT" ]] || { echo "USAGE-ERROR: --install needs --at-utc" >&2; exit 2; }
  read -r H M D MO < <(python3 -c 'import sys,datetime
h,m=map(int,sys.argv[1].split(":")); now=datetime.datetime.now(datetime.timezone.utc)
d=datetime.datetime(now.year,now.month,now.day,h,m,tzinfo=datetime.timezone.utc)
if d < now: d+=datetime.timedelta(days=1)
l=d.astimezone(); print(l.hour,l.minute,l.day,l.month)' "$AT")
  LABEL="com.earshot.watchdog.$SLUG.${AT/:/}"; PL="$HOME/Library/LaunchAgents/$LABEL.plist"
  ARGS=""; for l in "${LEGS[@]}"; do ARGS+="<string>--leg</string><string>$l</string>"; done
  cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$REPO/scripts/event-watchdog.sh</string><string>--run</string><string>--slug</string><string>$SLUG</string>$ARGS</array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string><key>HOME</key><string>$HOME</string></dict>
  <key>WorkingDirectory</key><string>$REPO</string><key>RunAtLoad</key><false/>
  <key>StartCalendarInterval</key><dict><key>Month</key><integer>$MO</integer><key>Day</key><integer>$D</integer><key>Hour</key><integer>$H</integer><key>Minute</key><integer>$M</integer></dict>
  <key>StandardOutPath</key><string>$REPO/state/_day/launchd-$LABEL.log</string><key>StandardErrorPath</key><string>$REPO/state/_day/launchd-$LABEL.log</string>
</dict></plist>
PLIST
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; launchctl bootstrap "gui/$(id -u)" "$PL" || { echo "REFUSED: bootstrap $LABEL" >&2; exit 1; }
  RB=$(launchctl print "gui/$(id -u)/$LABEL" | grep -o '"Hour" => [0-9]*\|"Minute" => [0-9]*\|"Day" => [0-9]*' | sort -u | tr '\n' ' '); [[ -n "$RB" ]] || { echo "REFUSED: readback empty" >&2; exit 1; }
  echo "INSTALLED $LABEL at ${AT}Z — loaded readback: $RB"; exit 0
fi
LOG="state/_day/watchdog-$SLUG.log"; mkdir -p state/_day; exec >>"$LOG" 2>&1
echo "=== watchdog $SLUG $(date -u +%FT%TZ)"
# A box-primary / laptop-failover pair has, by design, exactly ONE capturing leg
# (memory laptop-is-not-a-capture-host / the 2026-08-19 one-trader ruling). So
# the alarm is "NO leg is capturing", or "a leg reached capture_ready and its run
# has since gone terminal/its unit died" — not "some leg is idle". Counts use
# `|| true` (grep -c exits 1 on zero matches under set -e is not in play here,
# but the old `|| echo 0` emitted a SECOND line that shifted the remote parse and
# made box detection counts meaningless).
ANY_READY=0; DEADAFTERREADY=0; SUMMARY=""; DETAIL=""
for l in "${LEGS[@]}"; do
  IFS=: read -r E R HOST <<<"$l"
  if [[ "$HOST" == local ]]; then
    RC=$(grep -c '"transition":"capture_ready"' "var/runs/$E/$R/control/receipts.jsonl" 2>/dev/null); RC=${RC:-0}
    TERM=$(grep -cE '"transition":"(failed|aborted)"' "var/runs/$E/$R/control/receipts.jsonl" 2>/dev/null); TERM=${TERM:-0}
    UA=local; DET=$(grep -c "decision intent\|detection ignored" "state/$E/launch.log" 2>/dev/null); DET=${DET:-0}
    ALOG=$(ls -t "state/$E"/arm-on-url-*.log 2>/dev/null | head -1)
  else
    OUT=$(ssh -n -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 "$HOST" '
      R=~/ear/var/runs/'"$E"'/'"$R"'/control/receipts.jsonl
      P=~/ear/var/runs/'"$E"'/'"$R"'/control/run-plan.json
      sha=$(python3 -c "import json;print(json.load(open(\"$P\"))[\"plan_sha256\"][:20])" 2>/dev/null)
      rc=$(grep -c "\"transition\":\"capture_ready\"" "$R" 2>/dev/null); echo "${rc:-0}"
      tm=$(grep -cE "\"transition\":\"(failed|aborted)\"" "$R" 2>/dev/null); echo "${tm:-0}"
      systemctl --user is-active "com.earshot.run.$sha.service" 2>/dev/null || echo unknown
      dt=$(grep -c "decision intent\|detection ignored" ~/ear/state/'"$E"'/launch.log 2>/dev/null); echo "${dt:-0}"' 2>/dev/null)
    RC=$(sed -n 1p <<<"$OUT"); TERM=$(sed -n 2p <<<"$OUT"); UA=$(sed -n 3p <<<"$OUT"); DET=$(sed -n 4p <<<"$OUT")
    [[ -z "$RC" ]] && { RC=U; TERM=0; UA=UNREACHABLE; DET=0; }
    ALOG=""
  fi
  [[ "$RC" == "1" ]] && ANY_READY=1
  [[ "$RC" == "1" && ( "$TERM" -ge 1 || "$UA" == failed || "$UA" == inactive ) ]] && DEADAFTERREADY=1
  echo "$E @ $HOST: capture_ready=$RC terminal=$TERM unit=$UA detections=$DET"
  SUMMARY+="$E@$HOST ready=$RC term=$TERM unit=$UA det=$DET; "
  if [[ "$RC" != "1" || "$TERM" -ge 1 || "$UA" == failed ]]; then
    tail=""; [[ -n "$ALOG" && -f "$ALOG" ]] && tail=$(tail -3 "$ALOG" 2>/dev/null | tr '\n' ' ')
    DETAIL+="[$E@$HOST] ${tail:-no arm log}
"
  fi
done
if (( ! ANY_READY )); then
  scripts/notify.sh "WATCHDOG $SLUG: NO leg is capturing at $(date -u +%T)Z" "$SUMMARY
$DETAIL" >/dev/null 2>&1 || true
elif (( DEADAFTERREADY )); then
  scripts/notify.sh "WATCHDOG $SLUG: a leg WAS capturing and has since DIED at $(date -u +%T)Z" "$SUMMARY
$DETAIL" >/dev/null 2>&1 || true
fi
echo "summary: $SUMMARY"
