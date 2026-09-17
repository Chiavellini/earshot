# Mention Market Coverage: Immediate Remediation Steps

**Date:** 2026-09-17  
**Operator:** Bernardo Del Rio  
**System:** Earshot mention market discovery pipeline

---

## 🚨 CRITICAL ISSUES (Fix Now)

### Issue 1: Stale Slate (40 Days)

**Problem:** `slate/latest.json` generated 2026-08-07, hasn't updated in 40 days.

**Impact:** All downstream jobs (staging, hunt, preflight) trust stale data. New markets invisible.

**Diagnosis:**
```bash
cd ~/Code/ear

# Check if slate-daily job is loaded
launchctl print gui/$(id -u)/com.earshot.slate-daily

# Check recent logs
tail -100 ~/Library/Logs/earshot/slate-daily.log

# Check if job is even installed
launchctl list | grep slate-daily
```

**Fix Options:**

**Option A: Job not loaded** (most likely)
```bash
# Install the job
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.earshot.slate-daily.plist

# Force immediate run
launchctl kickstart -k gui/$(id -u)/com.earshot.slate-daily

# Or run manually
scripts/slate-daily.sh
```

**Option B: Job failing silently**
```bash
# Check the log for errors
cat ~/Library/Logs/earshot/slate-daily.log

# Common failures:
# - Python dependencies missing: pip3 install -r requirements.txt (if exists)
# - Kalshi API timeout: retry, check network
# - Git issues: check repo is clean, git status

# Manual run to see errors
scripts/slate-daily.sh --dry-run
```

**Option C: Job running but not committing**
```bash
# slate-daily.sh line 135: checks if content changed before committing
# If only generated_utc changed, it does: git checkout slate/latest.json
# This is CORRECT behavior if no events changed

# Force a content change (will trigger commit):
python3 scripts/discover-events.py --json slate/latest.json --days 21

# Check if there's actually new content
git diff slate/latest.json
```

**Verification:**
```bash
# After fix, verify:
# 1. Slate age <2h
python3 -c "import json, datetime; d=json.load(open('slate/latest.json')); gen=datetime.datetime.fromisoformat(d['generated_utc'].replace('Z','+00:00')); print(f'Age: {(datetime.datetime.now(datetime.timezone.utc)-gen).total_seconds()/3600:.1f}h')"

# 2. Job will fire tonight
launchctl print gui/$(id -u)/com.earshot.slate-daily | grep -A5 "next fire"
```

---

### Issue 2: No Intraday Protection (INDI/FED-FAMI Class)

**Problem:** `slate-intraday.sh` exists but no launchd job installed. Markets listed after 02:00Z (slate-daily) and before call time are invisible until next day.

**Impact:** 22-hour blind window. Two confirmed misses (INDI, FED-FAMI 2026-07-29).

**Fix:**
```bash
cd ~/Code/ear

# Install the NEW plist (just created in this repo)
cp configs/launchd/com.earshot.slate-intraday.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.earshot.slate-intraday.plist

# Verify loaded
launchctl list | grep slate-intraday

# Check schedule (should fire at 04,06,08,10,12,14,16 local = 10,12,14,16,18,20,22Z)
launchctl print gui/$(id -u)/com.earshot.slate-intraday | grep -A10 "CalendarInterval"

# Force test run
launchctl kickstart -k gui/$(id -u)/com.earshot.slate-intraday

# Or manual test
scripts/slate-intraday.sh --dry-run
```

**Verification:**
```bash
# 1. Check logs after next scheduled fire (wait for next even hour 04-16 local)
tail -50 ~/Library/Logs/earshot/slate-intraday.log

# 2. Look for "OK: slate-intraday ... clean" or "SAMEDAY-ADDED" alerts
```

---

### Issue 3: No Census Schedule (New Series Detection)

**Problem:** `mentions-census.py` detects new series (BA, KXFEDMENTION, KXWORLDNEWSMENTION precedent) but only runs manually. Census is 48 days stale.

**Impact:** If Kalshi adds a new mention family (e.g., KXCBCMENTION), we're blind until someone manually runs census.

**Fix:**
```bash
cd ~/Code/ear

# Install the NEW weekly census job
cp configs/launchd/com.earshot.census-weekly.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.earshot.census-weekly.plist

# Verify loaded
launchctl list | grep census-weekly

# Check schedule (should fire Sunday 21:00 local)
launchctl print gui/$(id -u)/com.earshot.census-weekly

# Run manually NOW to refresh stale census
python3 scripts/mentions-census.py \
  --baseline slate/census.json \
  --out slate/census.json \
  --horizon-days 21 \
  --classify

# Commit the refreshed census
git add slate/census.json
git commit -m "census: manual refresh 2026-09-17 — 48d stale"
```

**Verification:**
```bash
# Check census age
python3 -c "import json, datetime; d=json.load(open('slate/census.json')); print(f\"Generated: {d['generated_utc']}\")"

# Look for NEW-FAMILY alerts in next weekly run
tail -50 ~/Library/Logs/earshot/census-weekly.log
```

---

## ⚠️ HIGH PRIORITY (Fix This Week)

### Issue 4: No Coverage Verification

**Problem:** No automated check compares live board vs committed slate. The 40-day stale slate was undetected.

**Fix:**
```bash
cd ~/Code/ear

# Test the new coverage check (just created)
scripts/coverage-check.sh --dry-run

# If it works, add to daily cycle (two options):

# Option A: Standalone job (runs 03:00Z, after slate-daily 02:00Z)
cat > ~/Library/LaunchAgents/com.earshot.coverage-check.plist <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.earshot.coverage-check</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/bernardodelrio/Code/ear/scripts/coverage-check.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>21</integer>
    <key>Minute</key>
    <integer>10</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/bernardodelrio/Library/Logs/earshot/coverage-check.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/bernardodelrio/Library/Logs/earshot/coverage-check.log</string>
</dict>
</plist>
XML

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.earshot.coverage-check.plist

# Option B: Append to slate-daily.sh (easier, same alert stream)
# Edit scripts/slate-daily.sh, add after line 136 (before exit):
#   scripts/coverage-check.sh || true
```

---

## 📋 Verification Checklist (Run After Fixes)

```bash
cd ~/Code/ear

echo "=== POST-FIX VERIFICATION ==="

# 1. All discovery jobs loaded
echo "1. Loaded jobs:"
launchctl list | grep earshot

# 2. Slate is fresh
echo "2. Slate age:"
python3 -c "import json, datetime; d=json.load(open('slate/latest.json')); gen=datetime.datetime.fromisoformat(d['generated_utc'].replace('Z','+00:00')); print(f'  {(datetime.datetime.now(datetime.timezone.utc)-gen).total_seconds()/3600:.1f}h old')"

# 3. Intraday watcher will fire
echo "3. Next intraday fire:"
launchctl print gui/$(id -u)/com.earshot.slate-intraday 2>/dev/null | grep -A2 "next fire date" || echo "  (not installed)"

# 4. Census will run weekly
echo "4. Next census fire:"
launchctl print gui/$(id -u)/com.earshot.census-weekly 2>/dev/null | grep -A2 "next fire date" || echo "  (not installed)"

# 5. Coverage check runs daily
echo "5. Coverage check:"
scripts/coverage-check.sh --dry-run && echo "  PASS" || echo "  FAIL (see output above)"

# 6. All events in slate are on live board (or closed recently)
echo "6. Live board comparison:"
scripts/coverage-check.sh 2>&1 | head -1
```

**Expected output:**
```
1. Loaded jobs:
  - com.earshot.slate-daily
  - com.earshot.slate-intraday
  - com.earshot.census-weekly
  - com.earshot.hunt-daily
  - ... (others)

2. Slate age:
  <2h old

3. Next intraday fire:
  Today 04:00 (or next scheduled hour)

4. Next census fire:
  Sunday 21:00

5. Coverage check:
  PASS

6. Live board comparison:
  OK: All <N> live markets in slate (age <2h)
```

---

## 🧪 Test: Can We Catch a 6-Hour Market?

**After all fixes are deployed:**

```bash
# Simulate a market listed 6h before call time
# (Or wait for a real one and timestamp it)

# At T-6h (14:00Z):
echo "$(date -u +%FT%TZ) TEST: Monitoring for short-lead market" >> /tmp/test-log.txt

# At T-4h (16:00Z, next intraday sweep):
# Check for SAMEDAY-ADDED alert
grep "SAMEDAY-ADDED" ~/Library/Logs/earshot/slate-intraday.log | tail -5

# Manual restaging (currently required):
# scripts/slate-stage.sh --issuer <TICKER>

# At T-2h: Verify config exists
# ls configs/*<ticker>*-arm*.toml

# At T-1h: Preflight should pass
# scripts/preflight-event.sh --config configs/... --account ~/.kalshi/...

# At T-0: Event should arm
```

**Current minimum lead time (after fixes):** 4-6 hours (with human intervention for restaging).

**Without human:** 22 hours (next daily slate-daily + stage-nightly cycle).

---

## 📞 Who to Page When Things Break

**Discovery failures:**
- Stale slate (>30h): `scripts/coverage-check.sh` alerts
- Census degraded: `mentions-census.py` prints CENSUS-DEGRADED line
- Sweep delta (Boeing class): slate-daily.sh alerts on per_series_only events

**Staging failures:**
- NEEDS-TIME: `stage-nightly.sh` pages with slate-time-merge.py output
- UNSTAGED (webcast/phone): `slate-intraday.sh` UNSTAGED() lines
- Timer not loaded: `night-before-sweep.sh` (part of stage-nightly)

**Runtime failures:**
- Preflight fails: `preflight-nightly.sh` mails per-event + aggregate
- Missed arm: `scripts/missed-arm-check.py --date YYYY-MM-DD` (run after event)

All alerts go to: `scripts/notify.sh` (mail + optional SMS via config).

---

## 📚 Additional Reading

- **Full investigation:** `/workspace/MENTION_MARKET_INVESTIGATION.md`
- **Discovery contract:** `scripts/discover-events.py` docstring (lines 1-60)
- **Intraday watcher:** `scripts/slate-intraday.sh` docstring (lines 1-55)
- **Staging pipeline:** `scripts/slate-stage.sh` docstring (lines 1-100)
- **System architecture:** `docs/ARCHITECTURE.md` (control node pipeline, lines 10-20)

---

**Status:** Fixes committed to branch `webcast-lane`. Install launchd jobs manually (no automation for bootstrapping).

**Next review:** 2026-09-24 (one week) — verify all jobs fired, slate is fresh, no alerts.
