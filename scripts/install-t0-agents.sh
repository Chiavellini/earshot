#!/usr/bin/env bash
# Install the per-event T-0 launchd agents for one slate date (laptop).
#
#   scripts/install-t0-agents.sh --date 2026-08-05 [--account-map <file>] [--kind-map <file>]
#   scripts/install-t0-agents.sh --rehearsal --config configs/rehearsal-<x>.toml
#
# Per webcast config configs/webcast-<lc>-<dp>-arm-wc.toml, writes + loads:
#   com.earshot.<slug>-<dp>-watch    fire call−75min: assert-ready && arm-on-url
#                                    (deadline call+20, --i-am-live: standing
#                                    arm-all posture; caps enforced inside arm)
#   com.earshot.<slug>-<dp>-escalate fire call−75min: url-escalate.sh ladder
#                                    T-60/30/15 — the component whose absence
#                                    killed SPCX/ZETA/TOST on 2026-08-04
# Per twilio config whose venue is NOT admission=held(≤30d):
#   com.earshot.<slug>-<dp>-probepage fire call−35min: PAGE-ONLY reminder with
#                                    the exact admission-window-probe command.
#                                    (Never an unattended dialer — a probe into
#                                    a live arm is the double-leg class.)
# Plus two watcher-liveness page jobs (04:30 + 13:00 local).
#
# All StartCalendarInterval entries carry Month+Day so each fires exactly once;
# bootout the com.earshot.*-<dp>-* set after the event day.
# account-map/kind-map: lines "slug=value" (account env path / hls|youtube).
set -uo pipefail
REPO="${EARSHOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"
LA="$HOME/Library/LaunchAgents"
LOGDIR="$HOME/Library/Logs/earshot"
mkdir -p "$LA" "$LOGDIR"
UID_N=$(id -u)

DATE="" ACCOUNT_MAP="" KIND_MAP="" REHEARSAL=0 RCONFIG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --date)        DATE="${2:?}"; shift 2 ;;
    --account-map) ACCOUNT_MAP="${2:?}"; shift 2 ;;
    --kind-map)    KIND_MAP="${2:?}"; shift 2 ;;
    --rehearsal)   REHEARSAL=1; shift ;;
    --config)      RCONFIG="${2:?}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

map_lookup() { # <file> <slug> <default>
  local v=""
  [[ -n "$1" && -f "$1" ]] && v=$(sed -n "s/^$2=//p" "$1" | head -1)
  echo "${v:-$3}"
}

write_plist() { # <label> <hour> <min> <month> <day> <cmd...>
  local label="$1" hour="$2" min="$3" month="$4" day="$5"; shift 5
  local plist="$LA/${label}.plist"
  local args=""
  for a in "$@"; do args="${args}    <string>$(python3 -c "import html,sys;print(html.escape(sys.argv[1]))" "$a")</string>
"; done
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${label}</string>
  <key>ProgramArguments</key>
  <array>
${args}  </array>
  <key>WorkingDirectory</key><string>${REPO}</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${HOME}/.cargo/bin</string></dict>
  <key>StartCalendarInterval</key>
  <dict><key>Month</key><integer>${month}</integer><key>Day</key><integer>${day}</integer><key>Hour</key><integer>${hour}</integer><key>Minute</key><integer>${min}</integer></dict>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>${LOGDIR}/${label}.log</string>
  <key>StandardErrorPath</key><string>${LOGDIR}/${label}.log</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/${UID_N}" "$plist" 2>/dev/null || true
  if launchctl bootstrap "gui/${UID_N}" "$plist"; then
    echo "LOADED: ${label} → $(printf '%02d-%02d %02d:%02d local' "$month" "$day" "$hour" "$min")"
  else
    echo "LOAD-FAILED: ${label} — install by hand: launchctl bootstrap gui/${UID_N} $plist" >&2
    return 1
  fi
}

# local (hour min month day) for "UTC starts_at + offset minutes"
local_hm() { # <starts_at_iso_z> <offset_min>
  python3 - "$1" "$2" <<'PY'
import sys
from datetime import datetime, timedelta, timezone
t = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00")) + timedelta(minutes=int(sys.argv[2]))
lt = t.astimezone()  # laptop local (CDMX, UTC-6, no DST)
print(lt.hour, lt.minute, lt.month, lt.day)
PY
}
utc_hm() { # <starts_at_iso_z> <offset_min> → HH:MM
  python3 - "$1" "$2" <<'PY'
import sys
from datetime import datetime, timedelta
t = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00")) + timedelta(minutes=int(sys.argv[2]))
print(f"{t.hour:02d}:{t.minute:02d}")
PY
}

if [[ "$REHEARSAL" -eq 1 ]]; then
  [[ -n "$RCONFIG" && -f "$RCONFIG" ]] || { echo "--rehearsal needs --config <rehearsal-cfg>" >&2; exit 2; }
  NOW_PLUS_2=$(python3 -c "
from datetime import datetime, timedelta
t = datetime.now() + timedelta(minutes=2)
print(t.hour, t.minute, t.month, t.day)")
  read -r H M MO D <<<"$NOW_PLUS_2"
  DEADLINE=$(python3 -c "
from datetime import datetime, timedelta, timezone
t = datetime.now(timezone.utc) + timedelta(minutes=17)
print(f'{t.hour:02d}:{t.minute:02d}')")
  write_plist "com.earshot.rehearsal-watch" "$H" "$M" "$MO" "$D" \
    /bin/bash -c "cd $REPO && scripts/assert-ready.sh $RCONFIG && exec scripts/arm-on-url.sh --slug rehearsal --kind hls --config $RCONFIG --account $HOME/.earshot/ms.env --deadline-utc $DEADLINE"
  echo "REHEARSAL: watcher fires in ~2 min; URL file: \$HOME/rehearsal-stream.url; deadline $DEADLINE UTC (~17 min)"
  exit 0
fi

[[ -n "$DATE" ]] || { echo "usage: install-t0-agents.sh --date YYYY-MM-DD [--account-map f] [--kind-map f]" >&2; exit 2; }
DP=$(python3 -c "
from datetime import date
d = date.fromisoformat('$DATE')
print(f\"{d.strftime('%y')}{d.strftime('%b').lower()}{d.strftime('%d')}\")")

INSTALLED=0 PROBLEMS=0

# ── webcast events: watch + escalate ────────────────────────────────────────
for cfg in configs/webcast-*-"$DP"-arm-wc.toml; do
  [[ -f "$cfg" ]] || continue
  slug=$(basename "$cfg" .toml); slug="${slug#webcast-}"; slug="${slug%-$DP-arm-wc}"
  # Phone-lane configs can hide behind webcast- filenames (AMD 26AUG04) — the
  # discriminator is the source kind, never the filename.
  if grep -qE '^[[:space:]]*kind[[:space:]]*=[[:space:]]*"twilio' "$cfg" && \
     ! grep -qE '^[[:space:]]*kind[[:space:]]*=[[:space:]]*"pipe' "$cfg"; then
    echo "SKIP $slug: $cfg declares only twilio sources — phone lane, not a watcher target"
    continue
  fi
  starts=$(sed -n '/^\[event\]/,/^\[/s/^[[:space:]]*starts_at[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$cfg" | head -1)
  ev=$(sed -n '/^\[event\]/,/^\[/s/^[[:space:]]*id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$cfg" | head -1)
  [[ -n "$starts" ]] || { echo "SKIP $slug: no [event].starts_at in $cfg" >&2; PROBLEMS=$((PROBLEMS+1)); continue; }
  acct=$(map_lookup "$ACCOUNT_MAP" "$slug" "$HOME/.earshot/ms.env")
  kind=$(map_lookup "$KIND_MAP" "$slug" "hls")
  read -r WH WM WMO WD <<<"$(local_hm "$starts" -75)"
  ARM_DEADLINE=$(utc_hm "$starts" 20)
  URL_DEADLINE=$(utc_hm "$starts" 0)
  write_plist "com.earshot.${slug}-${DP}-watch" "$WH" "$WM" "$WMO" "$WD" \
    /bin/bash -c "cd $REPO && scripts/assert-ready.sh $cfg && exec scripts/arm-on-url.sh --slug $slug --kind $kind --config $cfg --account $acct --deadline-utc $ARM_DEADLINE --i-am-live" \
    || PROBLEMS=$((PROBLEMS+1))
  write_plist "com.earshot.${slug}-${DP}-escalate" "$WH" "$WM" "$WMO" "$WD" \
    /bin/bash -c "cd $REPO && exec scripts/url-escalate.sh --slug $slug --url-file $HOME/$slug-stream.url --deadline-utc $URL_DEADLINE --ladder 60,30,15 --instructions 'Open the $slug IR/webcast page in a LOGGED-IN browser, copy the master .m3u8 (DevTools Network, filter m3u8), then: printf \"%s\" \"<URL>\" > \$HOME/$slug-stream.url'" \
    || PROBLEMS=$((PROBLEMS+1))
  INSTALLED=$((INSTALLED+2))
  [[ -n "$ev" ]] && scripts/journal.sh --event "$ev" --actor install-t0-agents.sh --action timer-install \
    --outcome ok --detail "watch+escalate fire $(printf '%02d:%02d' "$WH" "$WM") local; arm deadline $ARM_DEADLINE Z; account $acct; kind $kind" || true
done

# ── phone events without fresh held admission: T−35 probe-reminder page ────
for cfg in configs/twilio-*-"$DP"-arm.toml; do
  [[ -f "$cfg" ]] || continue
  slug=$(basename "$cfg" .toml); slug="${slug#twilio-}"; slug="${slug%-$DP-arm}"
  starts=$(sed -n '/^\[event\]/,/^\[/s/^[[:space:]]*starts_at[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$cfg" | head -1)
  ev=$(sed -n '/^\[event\]/,/^\[/s/^[[:space:]]*id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$cfg" | head -1)
  [[ -n "$starts" ]] || continue
  key=$(python3 -c "
import re
m = re.match(r'KXEARNINGSMENTION([A-Z0-9]+)', '$ev')
print(m.group(1) if m else '')")
  FRESH_HELD=$(python3 -c "
import tomllib
from datetime import date, datetime
reg = tomllib.load(open('configs/dial-ins.toml','rb'))
e = reg.get('$key') or {}
ok = False
if e.get('admission') == 'held':
    try:
        ok = (date.today() - datetime.strptime(e.get('admission_probed',''), '%Y-%m-%d').date()).days <= 30
    except ValueError:
        pass
print('yes' if ok else 'no')" 2>/dev/null || echo no)
  if [[ "$FRESH_HELD" == "yes" ]]; then
    echo "OK $slug: admission=held fresh — no probe-reminder needed"
    continue
  fi
  read -r PH PM PMO PD <<<"$(local_hm "$starts" -35)"
  write_plist "com.earshot.${slug}-${DP}-probepage" "$PH" "$PM" "$PMO" "$PD" \
    /bin/bash -c "cd $REPO && exec scripts/notify.sh 'T-35 PROBE $slug' 'Admission for $key is NOT fresh-held. Probe NOW (in-window): scripts/admission-window-probe.sh --config $cfg --run — then launch or descope-event.sh with the verdict.'" \
    || PROBLEMS=$((PROBLEMS+1))
  INSTALLED=$((INSTALLED+1))
  [[ -n "$ev" ]] && scripts/journal.sh --event "$ev" --actor install-t0-agents.sh --action timer-install \
    --outcome warn --detail "probepage at T-35 (admission not fresh-held)" || true
done

# ── watcher-liveness page jobs (04:30 + 13:00 local) ────────────────────────
read -r MO D <<<"$(python3 -c "
from datetime import date
d = date.fromisoformat('$DATE')
print(d.month, d.day)")"
write_plist "com.earshot.liveness-am-${DP}" 4 30 "$MO" "$D" \
  /bin/bash -c "cd $REPO && exec scripts/watcher-liveness.sh --date $DATE" || PROBLEMS=$((PROBLEMS+1))
write_plist "com.earshot.liveness-pm-${DP}" 13 0 "$MO" "$D" \
  /bin/bash -c "cd $REPO && exec scripts/watcher-liveness.sh --date $DATE" || PROBLEMS=$((PROBLEMS+1))

echo "install-t0-agents: $INSTALLED event agents + 2 liveness jobs for $DATE; problems: $PROBLEMS"
[[ "$PROBLEMS" -eq 0 ]] || exit 1
