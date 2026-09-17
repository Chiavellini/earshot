# Kalshi EARNINGS MENTION Markets: Discovery Pipeline Investigation

**Date:** 2026-09-17  
**Branch:** webcast-lane  
**Investigator:** Cloud Agent  
**Status:** ⚠️ CRITICAL GAPS IDENTIFIED

---

## Executive Summary

**Can earshot reliably capture mention markets posted only a few hours before begin time?**

**Answer: NO — Multiple critical timing gaps exist in the current pipeline.**

The system has a **41-day stale slate** (last updated 2026-08-07, today is 2026-09-17), **no automated intraday discovery**, and **manual-only same-day staging**. A market posted 3-6 hours before call time will miss all automated discovery and staging steps.

### Critical Finding: Minimum Lead Time Requirements

| Pipeline Stage | Current Cadence | Minimum Lead Time | Gap for N-hour listings |
|---|---|---|---|
| **Discovery** (slate-daily) | Once daily, 20:00 local (~02:00Z next day) | ~22 hours before call | ❌ Miss if listed <22h before |
| **Census** (mentions-census) | Not scheduled | Manual only | ❌ No protection against new series |
| **Intraday watch** (slate-intraday) | NOT INSTALLED | Every 2h (designed, not deployed) | ❌ No same-day safety net |
| **Staging** (stage-nightly) | Once daily, 20:50 local | ~21 hours before call | ❌ Miss if listed <21h before |
| **Number hunt** (hunt-daily) | Once daily, 20:30 local | ~21.5 hours before call | ❌ Phone venue unknown |
| **Preflight** (preflight-nightly) | Once daily, 01:00Z | ~7 hours before early calls | ⚠️ Tight for 08:00Z calls |

**Worst case:** Market listed at 14:00Z for 21:00Z call (7h lead) = **complete miss** (discovery, staging, hunt all skip).

---

## 1. Discovery Pipeline Architecture

### 1.1 The Core Discovery Chain

```
┌─────────────────────────────────────────────────────────────────────────┐
│  KALSHI API (events + series endpoints)                                  │
└────────────────┬────────────────────────────────────────────────────────┘
                 │
                 ├─► PER-SERIES SWEEP (discover-events.py)
                 │   • Queries each KXEARNINGSMENTION* series individually
                 │   • BA-safe: catches misdated tickers (BA-26JUL21 → Jul 28)
                 │   • Detects stale dates (PGR open with past date)
                 │   • Cross-checks IR pages for call times
                 │   • CADENCE: Daily at 20:00 local (~02:00Z next day)
                 │   • SCHEDULED: com.earshot.slate-daily.plist
                 │   • OUTPUT: slate/latest.json (21-day horizon)
                 │
                 └─► CENSUS SWEEP (mentions-census.py)
                     • Full category scan (no prefix filter)
                     • Detects new series (BA, KXFEDMENTION, KXWORLDNEWSMENTION precedent)
                     • CADENCE: ❌ NOT SCHEDULED (manual only)
                     • OUTPUT: slate/census.json (committed, 398 series as of 2026-07-30)
                     • ALERT: NEW-FAMILY / OUT-OF-PREFIX-OPEN lines

┌─────────────────────────────────────────────────────────────────────────┐
│  slate/latest.json (committed artifact, 21-day horizon)                  │
│  • Generated: 2026-08-07T02:02:08Z                                       │
│  • TODAY: 2026-09-17 → ⚠️ 40 DAYS STALE                                 │
│  • 30 events, 162 series, sweep delta: [] (no Boeing-class misses)      │
└────────────────┬────────────────────────────────────────────────────────┘
                 │
                 ├─► INTRADAY WATCH (slate-intraday.sh)
                 │   • Cheap 3-day sweep every 2 hours
                 │   • Detects SAMEDAY-ADDED + LIVE-NOT-ARMED drift
                 │   • CADENCE: ❌ NOT INSTALLED (script exists, no launchd plist)
                 │   • DESIGNED: 10,12,14,16,18,20,22Z (10/2:00:00 UTC systemd)
                 │   • Would catch: Markets listed after slate-daily, before call
                 │
                 ├─► STAGING (stage-nightly.sh → slate-stage.sh)
                 │   • Merges verified call times (slate-time-merge.py)
                 │   • Stages configs for events ≤3 days out
                 │   • CADENCE: Daily at 20:50 local
                 │   • OUTPUT: configs/{webcast,twilio}-*-arm*.toml + autorun.env
                 │   • GATE: NEEDS-TIME if no verified call_time_utc
                 │
                 ├─► NUMBER HUNT (hunt-daily.sh → hunt-dial-in.py)
                 │   • Scrapes SEC filings for dial-in bridges
                 │   • CADENCE: Daily at 20:30 local
                 │   • OUTPUT: state/hunt/*.json (gitignored), slate/hunt-status.json
                 │   • ALERT: HUNT-NEW when bridge found
                 │
                 └─► PREFLIGHT (preflight-nightly.sh)
                     • T-1 readiness sweep (green gate, timer verification)
                     • CADENCE: Daily at 01:00Z (after staging, before morning calls)
                     • Per-event: preflight-event.sh (build, probes, dial-in gates)
```

### 1.2 Key Temporal Invariants

1. **Daily cycle starts 20:00 local (CDMX = UTC-6):**
   - 20:00 = slate-daily (discovery)
   - 20:30 = hunt-daily (number search)
   - 20:50 = stage-nightly (staging + timer install)

2. **Preflight fires 01:00Z next day** (19:00 local, 5h after staging)

3. **Event timelines from slate data (measured 2026-07-30):**
   - Earnings events list **4-7 days ahead** (per hunt-daily.sh comment)
   - Horizon: 21 days (discover-events.py default)
   - Staging window: ≤3 days (slate-stage.sh --horizon-days 3)

---

## 2. Specific Vulnerability: Short-Lead Markets

### 2.1 The "INDI" and "FED-FAMI" Precedent

From `slate-intraday.sh` comments (lines 5-9):

> "That window has already cost two races — **WING-INDI** (2026-07-29: the India market was listed late and never armed; its first-mention pump ran with no possible fire) and **FED-FAMI** (event listed after staging; nobody was watching)."

**Root cause:** Events listed **after slate-daily (02:00Z) and before call time** are invisible until the next daily sweep.

### 2.2 Current Protection: None

| Protection Mechanism | Status | Coverage |
|---|---|---|
| **Intraday discovery** | ❌ Script exists, NOT SCHEDULED | Would run 10-22Z every 2h |
| **Manual checks** | Human-dependent | Unreliable (INDI/FED-FAMI proved) |
| **Kalshi notifications** | Not implemented | N/A |

### 2.3 Timing Analysis: Can We Catch a 6-Hour Market?

**Scenario:** Kalshi posts KXEARNINGSMENTIONXYZ at 14:00Z for 20:00Z call (6-hour lead).

```
Timeline (UTC):
14:00  Kalshi lists market (sub_title: "On Sep 17, 2026")
       ↓
14:05  ❌ slate-daily last ran yesterday 02:00Z → slate is stale
       ❌ slate-intraday NOT INSTALLED → no 14:00Z check
       ↓
20:00  Call begins
       ↓
02:00  slate-daily runs, discovers market (10h AFTER call ended)
       (next day)
```

**Result:** Complete miss. Zero detection before call time.

### 2.4 Even With Intraday: Gaps Remain

**If slate-intraday were installed** (10,12,14,16,18,20,22Z):

```
14:00  Market listed
       ↓
14:05  ❌ No intraday sweep at 14:00 (runs at :00 only)
       ↓
16:00  ✅ slate-intraday DETECTS (SAMEDAY-ADDED alert)
       ❌ But NO AUTOMATED STAGING
       ❌ Manual intervention required:
          • earshot prepare --event-ticker XYZ
          • slate-stage.sh --issuer XYZ (same-day restage)
          • scripts/box-event-install.sh (timer install)
       ↓
20:00  Call begins (4h after detection, 2h for manual staging?)
```

**Minimum viable lead with intraday + human on call:** ~4-6 hours.

---

## 3. Gap Analysis: Where Markets Fall Through

### 3.1 Discovery Gaps

| Gap | Impact | Precedent | Mitigation Status |
|---|---|---|---|
| **Census not scheduled** | New series invisible until manual scan | BA, KXFEDMENTION, KXWORLDNEWSMENTION | ❌ Not automated |
| **Daily-only sweep** | 22h blind window after 02:00Z | INDI, FED-FAMI | ❌ Intraday not installed |
| **21-day horizon** | Markets >21d out not discovered | None observed | ✅ Sufficient (events list 4-7d ahead) |
| **Stale slate (40d)** | All markets missed during gap | **ACTIVE NOW (2026-08-07 → 2026-09-17)** | 🚨 OPERATIONAL FAILURE |

### 3.2 Staging Gaps

| Gap | Impact | Current Guard | Adequacy |
|---|---|---|---|
| **Same-day listings** | Listed after 20:50, call next day | ❌ None | Manual restage only |
| **Call time verification** | Misdated events (BA, PGR, HOOD) | ✅ IR page cross-check | Strong |
| **Number hunt lag** | Phone venue unknown at staging | ⚠️ Staged as webcast-only | Webcast fallback OK |
| **Horizon = 3 days** | Events >3d out not staged | ✅ Intentional (readiness window) | Appropriate |

### 3.3 Race Conditions

1. **Pagination vs per-series delta** (discover-events.py sweep_delta):
   - Both sweeps query `status=open`
   - Boeing precedent: paginated sweep missed BA on 2026-07-27 (did not reproduce)
   - **Current status:** `sweep_delta: {"per_series_only": [], "paginated_only": []}` (slate is 40d stale)
   - **Risk:** If pagination is intermittently incomplete, sweep_delta catches it (logged, alerted)

2. **Midnight UTC boundary** (slate-stage.sh WINDOW-WRAP):
   - Call window crosses midnight → HHMM windows left TBD
   - **Guard:** ENV-STALE state, manual COMPLETE_EARLIEST override required

3. **IR page time extraction** (discover-events.py parse_clock):
   - 08-06/08-07 lost 9 of 10 events: page times not extracted, staged at 00:00Z → launchd job dated previous day
   - **Fix:** parse_clock committed 2026-08-07 (lines 170-250, self-test lines 625-689)
   - **Status:** Fixed in code, but slate is stale

---

## 4. Operational State (2026-09-17)

### 4.1 Scheduled Jobs (launchd)

From `configs/launchd/*.plist`:

| Job | Schedule | Status | Last Known Good |
|---|---|---|---|
| com.earshot.slate-daily | 20:00 local daily | ⚠️ Unknown (40d stale slate) | 2026-08-07 02:02Z |
| com.earshot.hunt-daily | 20:30 local daily | ⚠️ Unknown | Unknown |
| com.earshot.stage-nightly | 20:50 local daily (inferred) | ⚠️ Unknown | Unknown |
| com.earshot.preflight-nightly | 01:00Z daily (intended, not confirmed installed) | ⚠️ Unknown | Unknown |
| **slate-intraday** | **NOT SCHEDULED** | ❌ Script exists, no plist | N/A |
| **mentions-census** | **NOT SCHEDULED** | ❌ Manual only | 2026-07-30 15:56Z |

### 4.2 Critical Findings

🚨 **STALE SLATE:** `slate/latest.json` generated 2026-08-07, **40 days ago**. Either:
   - Discovery job is not running, OR
   - Discovery job is failing silently, OR
   - Job runs but does not commit (timestamp-only change checkout in slate-daily.sh:135)

🚨 **NO INTRADAY PROTECTION:** `slate-intraday.sh` documented and tested, but **no launchd plist exists**.

🚨 **NO CENSUS SCHEDULE:** `mentions-census.py` catches new series (BA/FED/WNT precedent), but runs **manually only**.

### 4.3 Evidence of Recent Activity

- `slate/census.json`: 2026-07-30 15:56Z (48 days old)
- `slate/latest.json`: 2026-08-07 02:02Z (40 days old, 30 events, 162 series)
- No `configs/*-autorun.env` on disk (staging not run recently or gitignored)
- Hunt status unknown (state/hunt/ is gitignored)

---

## 5. Verification Plan for the Operator

### 5.1 Immediate Checks (Run Now)

```bash
# 1. Check if discovery job is actually running
launchctl list | grep earshot

# 2. Check discovery job logs
tail -100 ~/Library/Logs/earshot/slate-daily.log

# 3. Force a discovery run manually to see if it works
cd /path/to/earshot
scripts/slate-daily.sh --dry-run

# 4. Compare live Kalshi board vs current slate
python3 scripts/board-open.py --issuer AAPL  # pick any active issuer
# Then check if it's in slate/latest.json

# 5. Check for new series not in census
python3 scripts/mentions-census.py --baseline slate/census.json --horizon-days 21 > /tmp/census-check.txt
grep -E '^(NEW-FAMILY|OUT-OF-PREFIX-OPEN):' /tmp/census-check.txt

# 6. Verify intraday watcher is not installed
ls configs/launchd/ | grep intraday  # Should be empty

# 7. Check slate age vs staging age
ls -lt configs/webcast-*-arm*.toml 2>/dev/null | head -5  # When was staging last run?
```

### 5.2 Coverage Verification (Ongoing)

Create a daily verification script (`scripts/coverage-check.sh`):

```bash
#!/usr/bin/env bash
# Daily coverage check: do we have ALL open mention markets?

set -uo pipefail
cd "$(dirname "$0")/.."

echo "=== Mention Market Coverage Check $(date -u +%FT%TZ) ==="

# 1. Live board snapshot
python3 scripts/board-open.py --category Mentions > /tmp/live-board.json 2>&1 || {
  echo "FAIL: Could not query live board"; exit 1
}

# 2. Compare vs committed slate
python3 - <<'PY'
import json, sys, datetime

live = json.load(open("/tmp/live-board.json"))
committed = json.load(open("slate/latest.json"))

live_tickers = {e.get("event_ticker") for e in live.get("events", []) if e.get("status") in ("active", "open")}
slate_tickers = {e.get("event_ticker") for e in committed.get("events", [])}

missing = live_tickers - slate_tickers
if missing:
    print(f"MISSING-FROM-SLATE: {len(missing)} live events not in committed slate:")
    for t in sorted(missing):
        print(f"  {t}")
    sys.exit(1)

slate_age_h = (datetime.datetime.now(datetime.timezone.utc) - 
               datetime.datetime.fromisoformat(committed["generated_utc"].replace("Z", "+00:00"))
              ).total_seconds() / 3600
if slate_age_h > 30:
    print(f"STALE-SLATE: {slate_age_h:.1f}h old (bar 30h)")
    sys.exit(1)

print(f"OK: All {len(live_tickers)} live markets in slate (age {slate_age_h:.1f}h)")
PY
```

Run daily after `slate-daily` (add to `com.earshot.slate-daily.plist` or separate job).

### 5.3 Short-Lead Detection

To detect markets listed <12h before call:

```bash
# Add to slate-intraday.sh or run separately every 2h:
python3 - slate/latest.json <<'PY'
import json, sys, datetime
slate = json.load(open(sys.argv[1]))
now = datetime.datetime.now(datetime.timezone.utc)

for e in slate.get("events", []):
    ct = e.get("call_time_utc")
    if not ct: continue
    call_dt = datetime.datetime.fromisoformat(ct.replace("Z", "+00:00"))
    lead_h = (call_dt - now).total_seconds() / 3600
    if 0 < lead_h < 12:
        print(f"SHORT-LEAD: {e['event_ticker']} {e['issuer']} in {lead_h:.1f}h ({ct})")
PY
```

---

## 6. Recommended Fixes (Priority Order)

### 6.1 CRITICAL (Fix Immediately)

1. **Diagnose and fix stale slate** (2026-08-07 → 2026-09-17, 40d gap)
   ```bash
   # Check if job is running
   launchctl print gui/$(id -u)/com.earshot.slate-daily
   
   # If not loaded, install:
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.earshot.slate-daily.plist
   
   # Force a run now:
   scripts/slate-daily.sh
   ```

2. **Install intraday watcher** (`slate-intraday.sh` exists, needs schedule)
   ```bash
   # Create configs/launchd/com.earshot.slate-intraday.plist:
   # StartCalendarInterval: ARRAY of hours 10,12,14,16,18,20,22 UTC
   # (Convert to local time: CDMX = UTC-6 → 04,06,08,10,12,14,16)
   
   # Install:
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.earshot.slate-intraday.plist
   ```

### 6.2 HIGH (Fix This Week)

3. **Schedule census sweep** (weekly, not daily - rate limit guard)
   ```bash
   # Create configs/launchd/com.earshot.census-weekly.plist
   # StartCalendarInterval: Weekday=0 (Sunday), Hour=21
   # Runs: scripts/mentions-census.py --baseline slate/census.json --out slate/census.json
   ```

4. **Add coverage verification** to daily cycle
   - Runs after slate-daily
   - Compares live board vs slate
   - Pages on delta (not just logged)

### 6.3 MEDIUM (Next Sprint)

5. **Automate same-day restaging** (currently manual `slate-stage.sh --issuer X`)
   - Option A: Let slate-intraday trigger restaging on SAMEDAY-ADDED
   - Option B: Run slate-stage.sh every 4h (idempotent, skips ALREADY-STAGED)

6. **Add slate age guard** to all downstream consumers
   - stage-nightly.sh already checks (hunt-daily.sh:60-77)
   - Add to: preflight-nightly, install-t0-agents, launch-event

7. **Monitor sweep_delta** (Boeing-class pagination failures)
   - Currently logged, not alerted (slate-daily.sh only pages on content changes)
   - Page on: `len(sweep_delta["per_series_only"]) > 0`

### 6.4 DESIGN NEEDED (Future)

8. **Kalshi webhook integration** (eliminate polling delay)
   - Push notifications for new series/events in Mentions category
   - Requires Kalshi API feature (may not exist)

9. **Readiness hash for discovery** (variant-study.sh pattern)
   - Census/slate runs emit hash of discovered series set
   - Downstream checks refuse to proceed on hash mismatch

---

## 7. Minimum Viable Lead Times (Post-Fix)

**After implementing Critical + High fixes:**

| Listing Time | Discovery Lag | Staging Lag | Total | Catchable? |
|---|---|---|---|---|
| 24h before call | 0-2h (intraday) | 0h (already in staging window) | 2h | ✅ Yes |
| 12h before call | 0-2h (intraday) | 0h (restage if <3d, else already staged) | 2h | ✅ Yes |
| 6h before call | 0-2h (intraday) | **Manual restage required** | 2h+human | ⚠️ Tight |
| 3h before call | 0-2h (intraday) | **Manual restage required** | 2h+human | ❌ Likely miss |

**With automated same-day restaging (Medium priority):**

| Listing Time | Total Lag | Catchable? |
|---|---|---|
| 6h before call | ~2h (detect + stage) | ✅ Yes |
| 4h before call | ~2h | ⚠️ Tight (2h buffer for preflight + arm) |
| 3h before call | ~2h | ❌ Likely miss (1h buffer insufficient) |

**Minimum safe lead time:** **4-6 hours** (with all fixes, human on call for <6h).

**Current minimum:** **22 hours** (daily sweep only, no intraday protection).

---

## 8. Code Paths Reference

### 8.1 Discovery
- **scripts/discover-events.py** (lines 1-933): Per-series + paginated sweep, IR cross-check, stale date detection
- **scripts/mentions-census.py** (lines 1-291): Full category census, new series alerts
- **scripts/slate-daily.sh** (lines 1-137): Discovery job wrapper, commit + alert logic
- **scripts/slate-intraday.sh** (lines 1-194): Same-day watcher (NOT INSTALLED)

### 8.2 Staging
- **scripts/slate-stage.sh** (lines 1-632): Staging logic, webcast/phone lane derivation
- **scripts/stage-nightly.sh** (lines 1-75): Staging job wrapper, timer install
- **scripts/slate-time-merge.py** (referenced, not read): Merges verified call times into slate

### 8.3 Auxiliary
- **scripts/hunt-daily.sh** (lines 1-166): SEC dial-in number sweep
- **scripts/preflight-nightly.sh** (lines 1-335): T-1 readiness gate
- **scripts/night-before-sweep.sh** (lines 1-112): Timer verification (called by stage-nightly)

### 8.4 Data Artifacts
- **slate/latest.json**: 21-day horizon, committed, consumed by staging/hunt/preflight
- **slate/census.json**: Full Mentions category, 398 series, committed
- **slate/hunt-status.json**: Digit-free hunt status, committed (digits in gitignored state/hunt/)
- **configs/*-arm*.toml**: Staged event configs (webcast/twilio lanes)
- **configs/*-autorun.env**: Event timer contracts (gitignored or not present on disk)

---

## 9. Conclusions

### 9.1 Direct Answer to User's Question

**"Does the system capture mention markets posted only a few hours before begin time?"**

**NO.** The current system:
- Has a **40-day stale slate** (operational failure)
- Runs discovery **once daily** (22h blind window)
- Has **no intraday watcher installed** (script exists, not scheduled)
- Requires **manual restaging** for same-day listings

**Minimum lead time with current system:** ~22 hours (if slate is fresh and daily jobs run).

**Minimum lead time after fixes:** 4-6 hours (with intraday watcher + automated restaging).

### 9.2 Robustness Holes

1. **Stale slate (40d) is undetected** → all downstream jobs trust stale data
2. **No same-day safety net** → INDI/FED-FAMI precedent, will repeat
3. **Census not scheduled** → new series invisible (BA/FED/WNT precedent)
4. **Pagination reliability unknown** → sweep_delta logged but not alerted
5. **Same-day restaging is manual** → human bottleneck for <22h listings

### 9.3 What Needs to Happen Next

**IMMEDIATE (Today):**
1. Fix stale slate (diagnose why slate-daily hasn't run in 40 days)
2. Verify all launchd jobs are loaded and firing

**THIS WEEK:**
3. Install intraday watcher (2-hour polling, 10-22Z)
4. Schedule weekly census (new series detection)
5. Add daily coverage check (live board vs slate delta)

**NEXT SPRINT:**
6. Automate same-day restaging (remove human bottleneck)
7. Add slate age guards to all consumers
8. Alert on sweep_delta (pagination reliability)

**PROOF OF COVERAGE:**
- Run `scripts/coverage-check.sh` (from §5.2) daily after slate-daily
- Monitor for MISSING-FROM-SLATE / SHORT-LEAD alerts
- Test response time: simulate a 6h-lead market listing, measure time to staged config

---

## Appendix A: Test Plan for 6-Hour Market

**Objective:** Prove the system can catch a market listed 6h before call time.

**Prerequisites:**
- All CRITICAL + HIGH fixes deployed (intraday watcher, coverage check)
- Slate is fresh (<2h old)
- All launchd jobs verified running

**Test Procedure:**

1. **T-6h (14:00Z):** Manually create a synthetic event in a test account
   ```bash
   # Or wait for a real listing and timestamp it
   echo "$(date -u +%FT%TZ) TEST-START: KXEARNINGSMENTIONTEST-26SEP17" >> test-log.txt
   ```

2. **T-4h (16:00Z):** Verify intraday detection
   ```bash
   # Check slate-intraday logs for SAMEDAY-ADDED line
   grep "SAMEDAY-ADDED.*TEST" ~/Library/Logs/earshot/slate-intraday.log
   ```

3. **T-3.5h (16:30Z):** Verify operator paged
   ```bash
   # Check email/SMS for slate-intraday alert
   ```

4. **T-3h (17:00Z):** Manual restaging (current process)
   ```bash
   scripts/slate-stage.sh --issuer TEST
   ```

5. **T-2h (18:00Z):** Verify config staged
   ```bash
   ls configs/*test*-arm*.toml
   ```

6. **T-1h (19:00Z):** Preflight should pass
   ```bash
   scripts/preflight-event.sh --config configs/webcast-test-*-arm-wc.toml --account ~/.kalshi/test.env
   ```

7. **T-0 (20:00Z):** Event should arm automatically

**Success criteria:** Event staged + armed with <3h of human intervention time.

**Current status:** Test cannot pass (intraday not installed, slate is stale).

---

## Appendix B: Slate Metadata

```json
{
  "generated_utc": "2026-08-07T02:02:08.819074+00:00",
  "horizon_days": 21,
  "series_count": 162,
  "counts": {
    "paginated": 30,
    "per_series": 30,
    "union": 30
  },
  "sweep_delta": {
    "per_series_only": [],
    "paginated_only": []
  },
  "events": 30
}
```

**Age as of 2026-09-17:** 40 days, 22 hours (critical staleness).

---

**END OF INVESTIGATION**
