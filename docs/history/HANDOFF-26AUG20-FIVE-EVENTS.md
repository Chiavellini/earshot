# HANDOFF — 26AUG20 is a FIVE-event day (SUPPLEMENTS HANDOFF-26AUG20.md, read that first)

Written 2026-08-19 ~21:30Z by the operator-session agent (session "operator",
killed by safeguards flags — the SAME failure class as 26AUG19 05:38Z; assume
this session is dead and nothing pending on it will run). Repo HEAD `9b6796f`,
branch `webcast-lane`. Working tree: only untracked readiness JSONs (FUTU x2,
WMT) — do not commit files you didn't create.

## 0. THE HEADLINE

The operator says 5 events tomorrow. The handoff you already have covers only
3 (FUTU/WMT/DE). A LIVE Kalshi sweep run 21:08Z (discover-events.py --days 3,
output at the path in §1) confirms FIVE open boards for 2026-08-20:

| event | call UTC | venue | class | prep state |
|---|---|---|---|---|
| FUTU 11:30Z | edge.media-server.com/mmc/p/ex2zp2eh | Notified MMC, reg wall (profile `futu` past wall) | staged+box twin, study DONE+healthy | per HANDOFF-26AUG20.md |
| WMT 12:00Z | youtube.com/watch?v=hOdyv3MKhPE | YouTube Live, LAPTOP ONLY | staged, study DONE 14/14 healthy (validated ratios, no contention) |
| **AAP 12:00Z** | **events.q4inc.com/attendee/400875169** | **Q4 attendee, NO wall — same class as DE** | **NOT STAGED, not in slate/latest.json** |
| DE 14:00Z | events.q4inc.com/attendee/531842209 | Q4 attendee, no wall | staged+box twin; laptop study DONE (tailwind widen WORKED? NO — see §3); box-twin study was RUNNING CONTENDED — INVALID, re-run |
| **ROST 20:15Z** | **edge.media-server.com/mmc/p/6rfh9au9** | **Notified MMC, REG WALL — same class as FUTU; NO profile exists yet** | **NOT STAGED** |

WHY AAP/ROST were invisible: `com.earshot.slate-daily` FAILED on Aug 18 20:46
local — DNS was down at fire time, discover-events URLError, and the failure
mail ALSO died on DNS (log: ~/Library/Logs/earshot/slate-daily.log). So
slate/latest.json is frozen at 2026-08-18T02:02Z with 3 events for the day.
The discovery layer was dark for ~25 h and nothing paged. (Separate defect to
ledger later: slate-daily needs a retry/offline-queue for its own failure mail.)

## 1. Evidence gathered this session (paths, so you don't re-measure)

- Fresh sweep JSON (24 events, incl. AAP/ROST): `/private/tmp/claude-501/-Users-bernardodelrio-Code-ear/3ed3dc4d-4d64-4fa1-a6d0-76ab2d610520/scratchpad/slate-now.json`
  — but it's in a session scratchpad that may be GC'd; if gone, re-run
  `python3 scripts/discover-events.py --json <tmp> --days 3` (took ~2 min).
- **AAP call time 12:00Z**: IR press release "Thursday, August 20, 2026 at 8:00 a.m. ET"
  (ir.advanceautoparts.com events page links Webcast -> events.q4inc.com/attendee/400875169)
  AND Q4 attendee API `eventStart 2026-08-20T12:00:00.000Z` (curl'd 21:12Z,
  status NOT_STARTED, broadcastUrl '' — mints at call, exactly DE's shape;
  registrationFields=[], guest.enabled=true ⇒ NO wall; box can self-resolve).
  Two independent sources — record both in call-times.json.
- **ROST call time 20:15Z**: IR event page "Aug 20, 2026 at 4:15 PM EDT"
  (investors.rossstores.com, fetched in Chrome — the site 403/hangs curl from
  both laptop and box; use the browser) AND the MMC player header itself
  shows "8/20/2026 2:15 PM" (venue-local). Player = registration form
  (First/Last/Email/Company + "Keep me logged in") — classic Notified MMC wall.
- Box (ubuntu@3.212.174.94, key ~/.ssh/earshot-useast1.pem): 16 vCPU, load
  0.08, 6.7G disk free, DEPLOYED-FROM=9b6796f clean, NO 26AUG20 timers loaded yet.
- Laptop: NOW ON AC + charging (operator ask #1 from the old handoff is DONE),
  caffeinate held, but **load avg hit 223** at 21:26Z (Codex + 4 claude
  sessions + studies). This is the WMT-capture threat (§7 of old handoff) and
  it's what invalidated the DE studies (§3).
- alert_env still ABSENT (`~/.kalshi/alert_env`) — pages still have no phone
  receiver. Operator ask #2 still OPEN.

## 2. Staging AAP + ROST — the exact chain (traced through code this session)

DO NOT run `opsctl.sh stage --date 2026-08-20` / `eventctl init-date`: init-date
globs every config for the date and refuses the FUTU/DE box twins with
"a DIFFERENT plan already exists (differs in: ['execution_host_id'])" —
red exit + page, though it does NOT corrupt them (eventctl.py:1521-1531).
Do NOT export EARSHOT_EXECUTION_HOST to dodge it (all-or-nothing refusal).
Use the per-issuer path, which skips init-date entirely:

```sh
cd ~/Code/ear && source ~/.kalshi/earshot_env
# 1. call times (both dates match ticker suffix -> no date_correction needed)
$EDITOR slate/call-times.json    # add:
#  "KXEARNINGSMENTIONAAP-26AUG20": {"call_time_utc":"2026-08-20T12:00:00Z",
#    "source":"IR PR 'Thursday, August 20, 2026 8:00 a.m. ET' + Q4 attendee API 400875169 eventStart=2026-08-20T12:00:00Z (read 2026-08-19T21:12Z)",
#    "recorded_utc":"2026-08-19T21:30:00Z"}
#  "KXEARNINGSMENTIONROST-26AUG20": {"call_time_utc":"2026-08-20T20:15:00Z",
#    "source":"IR event page 'Aug 20, 2026 4:15 PM EDT' + MMC player 6rfh9au9 header '8/20/2026 2:15 PM' (read 2026-08-19T21:15Z)",
#    "recorded_utc":"2026-08-19T21:30:00Z"}
# 2. timed slate from a FRESH sweep (latest.json is stale; --slate/--out are first-class)
python3 scripts/discover-events.py --json /tmp/slate-fresh.json --days 3
python3 scripts/slate-time-merge.py --slate /tmp/slate-fresh.json --out slate/latest-timed.json --require-date 2026-08-20
#    expect zero UNTIMED/REFUSED for our 5 (AAP+ROST get timed from step 1; the other
#    date-unconfirmed tickers on the sweep, e.g. BJ 26AUG21, are outside --require-date)
# 3. stage ONLY the new issuers (target/release/earshot exists so prepare uses it)
scripts/slate-stage.sh --slate slate/latest-timed.json --issuer AAP
scripts/slate-stage.sh --slate slate/latest-timed.json --issuer ROST
#    expect STAGED + NO-PHONE-LANE + WEBCAST-TIMER + PORT (ports land ~8090/8091)
#    verify: grep -c '__' configs/webcast-aap-26aug20-arm-wc.toml == 0, starts_at has no TODO
# 4. laptop RunPlans, ONE AT A TIME (never init-date)
python3 scripts/eventctl.py init --config configs/webcast-aap-26aug20-arm-wc.toml  --event-env configs/aap-26aug20-autorun.env  --earshot-bin target/release/earshot
python3 scripts/eventctl.py init --config configs/webcast-rost-26aug20-arm-wc.toml --event-env configs/rost-26aug20-autorun.env --earshot-bin target/release/earshot
# 5. box twins (NO --force — --force is the flag that can orphan a pinned plan)
scripts/webcast-box-twin.py --from configs/webcast-aap-26aug20-arm-wc.toml  --init
scripts/webcast-box-twin.py --from configs/webcast-rost-26aug20-arm-wc.toml --init
```

### Bind shapes (mirror DE for AAP, FUTU for ROST — read theirs from
var/runs/<E>/<R>/control/access-plan.json if in doubt):

AAP both hosts, resolver `q4-api`, NO push job (box self-resolves like DE):
```sh
scripts/opsctl.sh bind-access --run-dir var/runs/KXEARNINGSMENTIONAAP-26AUG20-WEBCAST/<R> \
  --access-mode public --vantage laptop/aap --resolver-kind q4-api \
  --resolver-argv-json '["scripts/q4-resolve.sh","--event-id","400875169","--slug","aap","--deadline-utc","12:15"]' \
  --url-file '$HOME/aap-stream.url'
# box twin: same argv, --vantage us-east1/aap
```

ROST: FIRST register a profile (headed, standing identity bdelriom@actinver.com.mx,
tick "Keep me logged in", verify player loads PAST the form):
```sh
python3 scripts/browser-resolve.py --slug rost --url https://edge.media-server.com/mmc/p/6rfh9au9/ \
  --profile ~/.earshot/profiles/rost --headed --wait-secs 120 --no-notify
```
Then bind BOTH legs `preresolved` (bind synthesizes --deadline-utc/--expect-plan-sha256/--fresh-secs itself; $HOME/ spelling literal):
```sh
scripts/opsctl.sh bind-access --run-dir var/runs/KXEARNINGSMENTIONROST-26AUG20-WEBCAST/<R> \
  --access-mode public --vantage laptop/rost --resolver-kind preresolved \
  --resolver-argv-json '["scripts/urlfile-wait.sh","$HOME/rost-stream.url"]' \
  --preresolve-kind browser-network \
  --preresolve-argv-json '["python3","scripts/browser-resolve.py","--slug","rost","--url","https://edge.media-server.com/mmc/p/6rfh9au9/","--profile","/Users/bernardodelrio/.earshot/profiles/rost","--out","$HOME/rost-stream.url","--click-play","--linger-secs","20","--reject-endlist","--wait-secs","6600"]' \
  --url-file '$HOME/rost-stream.url'
# box twin: same, --vantage us-east1/rost
```
ROST DOES need a url-push-job (Notified mints at/after start):
`url-push-job.sh --install --slug rost --at-utc 20:16 --until-utc 20:29 --run-dir <ROST-BOX run> --run-dir <ROST laptop run> --host local`
(after posture/re-init/preflight/schedule — same ordering as §5 of the main handoff).

## 3. STUDY STATE — the DE numbers are POISONED, re-run them

- FUTU laptop+box: DONE, healthy (see main handoff §4). VALID.
- WMT laptop: DONE 14/14, ratios 0.9-3.4, finished BEFORE the load spike
  (21:05Z, load then ~15). I judge it VALID, but eyeball the log for `TTS failed`
  near its window if paranoid.
- **DE laptop (webcast-de-26aug20-arm-wc.json, written 19:51Z): says
  "tailwind 0/18 FAIL" and study-nightly AUTO-WROTE
  `configs/.acks/webcast-de-26aug20-arm-wc-dead-word.ack` naming TAIL.
  The main handoff §4 predicted the 4-variant widen should score ~1.83 like
  FUTU. It scored 0/18 — under a laptop whose `say` was measured at 8s
  (hardcoded alarm is 20s but the log shows 17 `TTS failed` lines across the
  run). This is EXACTLY `contended-study-invents-dead-words`. DO NOT TRUST
  the DE dead-word verdict; DELETE the ack only after a clean re-run proves
  tailwind either way.**
- **DE box twin study: was running at load 99->223. Its partial rows already
  show `efficiency 38/16` (16 clips where FUTU's equivalents got 18 = clips
  dropped). INVALID regardless of what it prints. Kill it if still running.**
- Re-run, SERIALIZED, on a quiet laptop (kill Codex/other sessions or wait for
  load<8 AND measured `say -o /dev/null test` < 3.5s — gate on measured say,
  not load avg):
```sh
scripts/variant-study.sh configs/webcast-de-26aug20-arm-wc.toml      # then compare tailwind vs FUTU 1.83
scripts/webcast-box-twin.py --from configs/webcast-de-26aug20-arm-wc.toml   # re-clone readiness ONLY if keywords unchanged (no --force; if it refuses on byte-drift STOP and read §2 gotcha 12)
# AAP + ROST after staging:
scripts/variant-study.sh configs/webcast-aap-26aug20-arm-wc.toml
scripts/variant-study.sh configs/webcast-rost-26aug20-arm-wc.toml
```
  (variant-study per-config is the surgical form; `opsctl study --date` would
  redo everything including healthy FUTU/WMT — acceptable but slower; if you
  use it, it must run SERIALIZED and quiet.)

## 4. REMAINING CHAIN — unchanged from HANDOFF-26AUG20.md §5, now x5 events

Follow the main handoff §5.1-5.4 exactly, extended:
- Posture (operator ruling: `[risk].max_order_notional_cents = 1940`, then
  posture-from-study `--size-tiers 3.0:20,2.0:10,1.0:4 --low-band-budget 2`)
  for ALL FIVE laptop configs; box twins re-derived AFTER posture.
- Re-init/re-bind all (posture changes bytes => E16); bind argv shapes: main
  handoff §6 for FUTU/WMT/DE + this file §2 for AAP/ROST.
- Preflight: git status clean FIRST (readiness JSONs untracked is fine).
- Box dispatch for FOUR box twins now: FUTU, DE, AAP, ROST
  (schedule -> dispatch-only -> preflight ON box -> pull --prelaunch -> install -> read timers).
- Push jobs: FUTU (11:15-11:44, per main handoff §5.4) and ROST (20:16-20:29).
  AAP/DE self-resolve on box: NO push. WMT laptop: NO push.
- Failovers (laptop wraps AFTER opsctl schedule): FUTU (--start-utc 11:30
  --grace-min 4), AAP (--start-utc 12:00 --grace-min 3), DE (14:00, 3),
  ROST (20:15, 3). WMT: none (laptop IS primary).
- Watchdogs x2 per event (start+5, start+35) — 10 installs total; FUTU/AAP/DE/ROST
  get both legs (--leg E-BOX:R:ubuntu@3.212.174.94 --leg E:R:local), WMT one local leg.
- Rehearse the production mechanism per CLAUDE.md (launchd-fired rehearsal
  push, main handoff §5.4 last bullet).

## 5. COLLISION WATCH — new with 5 events

- 11:30 FUTU + 12:00 WMT + 12:00 AAP overlap. Box carries FUTU+AAP+(DE later)
  — 16 idle vCPUs, twins run num_threads=2, fine. The LAPTOP at 12:00 carries:
  WMT primary capture + FUTU standby failover watch + AAP standby failover
  watch + the FUTU url-push browser leg (11:15-11:44). That is acceptable ONLY
  if the laptop is quiet. **The single biggest capture risk for tomorrow is
  laptop load: at 21:26Z it was 223. The operator MUST kill the Codex/agent
  swarm before 11:00Z or WMT dies exactly like the 26AUG18 laptop stalls
  (laptop-is-not-a-capture-host).** Consider asking the operator whether WMT
  should also get a box cookie attempt — no: memory says box is bot-walled at
  the segment layer; laptop it is. So: quiet laptop = non-negotiable ask #3.
- One Kalshi account, 5 events: fine (per-word caps + $200/event ledger),
  but worst-case notional x5 — READ each posture's printed worst case and
  record in the journals.
- Port map: new events take lowest free >= 8080 (~8090/8091, box twins ~8102/8103).
  slate-stage prints PORT lines — verify no collision with the staged 3.

## 6. OPERATOR ASKS (restated, updated)

1. ~~AC power~~ DONE (on AC + charging, caffeinate held, 21:20Z).
2. **Page receiver still missing** — `~/.kalshi/alert_env` absent
   (ALERT_TO_NUMBER/TWILIO_* per scripts/alert-phone.sh header). 10 pages, 0
   readers on 26AUG19. Needs a human decision.
3. **Quiet the laptop 10:45Z-14:30Z and 19:45Z-21:00Z** (WMT primary + push/
   failover legs + ROST push). Load 223 today would kill it.
4. Study re-runs (§3) need a quiet laptop TONIGHT too — same ask, earlier.

## 7. WHAT THIS SESSION DID NOT DO (so you don't assume)

- No file in the repo was modified by this session except THIS handoff file.
  (Plan file lives outside the repo. Chrome visited ROST/AAP IR + MMC pages,
  read-only; typed nothing into the ROST registration form.)
- AAP/ROST: NOT staged, NOT in call-times.json, NOT studied, no profile for rost.
- DE box-twin study: possibly still running/contended — check
  `pgrep -fl variant-study` and the log at
  `/private/tmp/claude-501/-Users-bernardodelrio-Code-ear/834c0382-*/scratchpad/study-26aug20.log`.
- slate/latest.json still stale; slate-daily's 20:00 local firing tonight may
  fix it (DNS permitting) and will COMMIT slate/ — expect an autonomous commit
  mid-evening; don't be surprised by it.
- Nothing scheduled/installed for AAP/ROST anywhere (laptop launchd or box systemd).

## ADDENDUM 21:55Z — stage-nightly fired at ~20:50 local and MOVED THE STATE

Read this before acting on §2 or on any run-dir id in either handoff:

1. **A parallel lane postured the three laptop arm configs** (uncommitted M:
   webcast-{de,futu,wmt}-26aug20-arm-wc.toml — cap 100→1940 + comment, DE
   dropped the IRAN market/keyword). Config bytes changed ⇒ stage-nightly's
   init-date **superseded (blocked+archived) ALL prior laptop run-dirs** and
   initialized fresh ones:
   - DE   laptop: `20260820T140000Z-p3-09c8a9552b07`
   - FUTU laptop: `20260820T113000Z-p3-c745a23a0898`
   - WMT  laptop: `20260820T120000Z-p3-5b9aeb6dfdc5`
   These have **NO AccessPlans** — re-bind all three (shapes: main handoff §6),
   including WMT whose old PASS bind is now archived.
2. **Box twins survived untouched** (init-date refused them on
   execution_host_id, as predicted — that refusal is benign). But their configs
   still carry the OLD cap (100) while the laptop configs now say 1940: the
   box twins must be RE-DERIVED from the postured laptop configs
   (webcast-box-twin.py, per main handoff §5.2) — do NOT bind or dispatch the
   current box run-dirs until then. Check whether the parallel lane already ran
   posture-from-study (the 1940 edit looks like its prologue) before redoing it.
3. **AAP + ROST are now STAGED** (configs/webcast-{aap,rost}-26aug20-arm-wc.toml
   exist, plus bj-26aug21) **but NEEDS-TIME**: starts_at carries TODO, no
   autorun env, init REFUSED both. §2 steps 3-5 collapse to: add the two
   call-times.json entries (§2 step 1, evidence in §1), re-run
   `scripts/slate-stage.sh --slate <timed> --issuer AAP` (RECONCILED path fills
   the time + writes the env), then eventctl init one-at-a-time, then box twins.
   BJ 26AUG21 is a real next-day event — needs a call time too (not tonight's
   problem, don't lose it).
4. **Coordinate before editing**: an unknown session is actively editing the
   arm configs in this worktree. `git status` before every step; the readiness
   JSONs + postured configs are uncommitted and belong to that lane, not you.
