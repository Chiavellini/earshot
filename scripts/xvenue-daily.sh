#!/usr/bin/env bash
# xvenue-daily.sh — standing health + coverage check for the cross-venue lane.
#
# WHY THIS EXISTS (2026-08-04). The lane's failure mode is silence: a recorder
# that never ran, a map that went stale, a tape nobody normalized. The AMD
# 26AUG04 capture sat unanalysed for a day precisely because nothing asked.
# This asks, every day, and says so out loud.
#
# Checks, in order:
#   1. every tool's offline self-test still passes (a broken toolchain is the
#      only failure that can silently corrupt an archive)
#   2. Polymarket's push endpoint still handshakes (Cloudflare rules change
#      without notice — Q-PM's own re-verify trigger)
#   3. tapes on disk that have no report next to them
#   4. ledger coverage against the node's >=3-event gate
#
# Exit: 0 clean / 1 real finding / 2 undetermined (fail loud, never silent).
#
# SCHEDULING — the plist is NOT checked in. `configs/launchd/**` is glob-owned
# by `platform.runtime`, whose one open claim is held by another lane, so
# shipping the file there would need a claim this change cannot take. Recipe:
#
#   cat > ~/Library/LaunchAgents/com.earshot.xvenue-daily.plist <<'PLIST'
#   <?xml version="1.0" encoding="UTF-8"?>
#   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
#     "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
#   <plist version="1.0"><dict>
#     <key>Label</key><string>com.earshot.xvenue-daily</string>
#     <key>ProgramArguments</key><array>
#       <string>/bin/bash</string>
#       <string>/Users/bernardodelrio/Code/ear/scripts/xvenue-daily.sh</string>
#     </array>
#     <key>StartCalendarInterval</key>
#     <dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>45</integer></dict>
#     <key>StandardOutPath</key>
#     <string>/Users/bernardodelrio/Library/Logs/earshot/xvenue-daily.log</string>
#     <key>StandardErrorPath</key>
#     <string>/Users/bernardodelrio/Library/Logs/earshot/xvenue-daily.log</string>
#   </dict></plist>
#   PLIST
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.earshot.xvenue-daily.plist
#
# 20:45 local is deliberately AFTER slate-daily (20:00) and hunt-daily (20:30):
# this job reports on what those staged, so it must not race them.
set -uo pipefail
export PATH="$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

cd "$(dirname "$0")/.." || { echo "cannot reach repo root" >&2; exit 2; }

DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

RC=0
note() { echo "xvenue-daily: $*"; }
finding() { echo "xvenue-daily: FINDING: $*"; RC=1; }

note "=== 1. tool self-tests ==="
for t in scripts/pm-tape.py scripts/xvenue-normalize.py scripts/xvenue-report.py; do
  if python3 "$t" --self-test >/dev/null 2>&1; then
    note "  ok   $t"
  else
    finding "$t self-test FAILS — the lane cannot be trusted until this is green"
  fi
done

note "=== 2. Polymarket push endpoint reachable ==="
# A handshake only; no subscribe, no data. This is Q-PM's re-verify trigger:
# the transport is behind Cloudflare and has changed shape before.
if python3 - <<'PY'
import socket, ssl, base64, os, sys
H = "ws-subscriptions-clob.polymarket.com"
try:
    raw = socket.create_connection((H, 443), timeout=12)
    s = ssl.create_default_context().wrap_socket(raw, server_hostname=H)
    k = base64.b64encode(os.urandom(16)).decode()
    s.sendall(f"GET /ws/market HTTP/1.1\r\nHost: {H}\r\nUpgrade: websocket\r\n"
              f"Connection: Upgrade\r\nSec-WebSocket-Key: {k}\r\n"
              f"Sec-WebSocket-Version: 13\r\n\r\n".encode())
    line = s.recv(256).split(b"\r\n", 1)[0].decode("latin-1")
    s.close()
    sys.exit(0 if "101" in line else 1)
except Exception:
    sys.exit(1)
PY
then note "  ok   wss://ws-subscriptions-clob.polymarket.com/ws/market → 101"
else finding "Polymarket push endpoint did NOT upgrade — re-verify Q-XVENUE-WS before the next event"
fi

note "=== 3. tapes without a report ==="
shopt -s nullglob
orphans=0
for d in state/xvenue/*/; do
  ev="$(basename "$d")"
  tapes=("$d"*.jsonl "$d"*.jsonl.gz)
  reports=("$d"report*.json)
  if (( ${#tapes[@]} > 0 && ${#reports[@]} == 0 )); then
    finding "$ev has ${#tapes[@]} tape(s) and NO report — run scripts/xvenue-report.py"
    orphans=$((orphans+1))
  fi
done
(( orphans == 0 )) && note "  ok   every tape directory has a report"

note "=== 4. ledger coverage ==="
if [[ -f slate/xvenue-ledger.json ]]; then
  python3 - <<'PY'
import json
d = json.load(open("slate/xvenue-ledger.json"))
n = d.get("events", 0)
print(f"xvenue-daily:   {n} event(s) banked; 3-event gate "
      f"{'MET' if d.get('gate_3_events_met') else 'NOT met'}")
for r in d.get("rows", []):
    print(f"xvenue-daily:     {r['pair_id']:<22} {r['verdict']:<16} "
          f"K/PM notional ratio {r.get('notional_ratio')}")
PY
else
  note "  no ledger yet (no event has been reported)"
fi

if (( DRY )); then
  note "dry-run: no notification sent"
elif (( RC )); then
  [[ -x scripts/notify.sh ]] && scripts/notify.sh "xvenue-daily found $RC issue class(es)" 2>/dev/null || true
fi

note "done (rc=$RC)"
exit $RC
