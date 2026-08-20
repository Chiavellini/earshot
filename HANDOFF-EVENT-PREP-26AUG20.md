# Handoff — event-prep reconstruction and ROST continuation

Date: 2026-08-20  
Branch: `webcast-lane`  
Starting commit: `e7f5656`  
Implementation commit: `ad58316`  
Claim-release commit: `0387026`

## Initial project state

The session began as a continuation of
`docs/history/HANDOFF-26AUG20-FIVE-EVENTS.md`, initially focused on making sure
DE captured. The task then changed: stop direct monitoring, reconstruct why the
system was unreliable without the operator on 2026-08-19 and 2026-08-20, and
repair both the code path and the live `/event-prep` skill so it could be used
for ROST later on 2026-08-20.

The important failures present at the start were:

- The Aug 18 daily discovery sweep died on one DNS failure. Its failure email
  died on the same outage, so AAP and ROST were absent from the slate until a
  human found them.
- Nightly automation was fragmented across discovery, stage, study, and
  preflight jobs. No loaded daily arrow reached scheduling and no component
  checked whether the complete event would actually fire.
- `/event-prep` described a strong READY contract, but most of its critical
  steps were prose/manual. `opsctl prepare-day` only ran five phases and could
  not enforce push, failover, watchdog, or whole-slate coverage.
- The macOS TTS study accepted a successful process exit without validating
  audio payload. A sandboxed `say` invocation produced a 78-byte/header-only
  WAV that could be counted as a requested clip.
- TGT and TOL on Aug 19 had zero audio: URL delivery named only one host, TOL's
  selected Notified master yielded zero PCM, webinar.net was WebRTC rather than
  HLS, and session wakeups were mistaken for durable monitoring.
- Posture edits created new immutable AAP/ROST attempts, but their preparation
  and access proof remained on superseded attempts. The active ROST runs had
  only `plan_compiled`.
- Q4 participant phone entry had not been treated as the first acquisition
  option. Presenter metadata such as `dialIn.speaker=true` was also easy to
  confuse with an actual attendee dial-in.

## Work completed

Commit `ad58316` reconstructed the event-preparation path around one executable,
fail-closed definition of READY.

1. `opsctl prepare-day` now executes:
   `discover -> stage -> access -> study -> preflight -> schedule -> readiness audit`.
2. Discovery retries transient DNS, timeout, HTTP 429, and HTTP 5xx failures,
   then atomically publishes the slate. Exhaustion remains nonzero.
3. `event-prep-audit.py` joins the refreshed slate back to active schema-3
   RunPlans and checks access, readiness evidence, measured posture, preflight,
   schedule proof, two watchdogs, required multi-host PCM-gated URL push, and
   required box-primary/laptop failover.
4. `variant-study.sh` now rejects empty, header-only, wrong-rate, or
   wrong-format clips and validates the concatenated replay payload.
5. The live `/event-prep` skill was rewritten around the audit blocker states
   and exact next actions. It explicitly prioritizes a real Q4 participant
   dial-in while distinguishing presenter-bridge metadata.
6. The legacy `com.earshot.preflight-nightly` label now runs the complete
   current-UTC-date facade rather than preflight alone.
7. The missing LaunchAgent was installed from committed bytes and loaded. Its
   readback currently shows:

   ```text
   cd /Users/bernardodelrio/Code/ear && exec scripts/opsctl.sh prepare-day --date "$(date -u +%F)"
   StartCalendarInterval: 23:30 local
   log: ~/Library/Logs/earshot/event-prep-nightly.log
   ```

No live arm, order, or monitoring session was started by this reconstruction.

## Files changed

- `.claude/skills/event-prep/SKILL.md` — replaced the long manual procedure
  with the audit-driven blocker workflow, Q4 phone priority, immutable-attempt
  rules, venue access rules, and exact ROST continuation contract.
- `.claude/skills/event-prep/references/TRAPS.md` — recorded the Aug 19/20
  failures, corrected the posture automation description, and documented the
  new discovery/study/audit guards.
- `.claude/skills/event-prep/references/BOX-CHECKS.md` — clarified that box GO
  is necessary but insufficient without whole-event support-job proof.
- `architecture/claims.toml` — temporarily claimed reconstruction paths, then
  released those claims in `0387026`; the existing five-event claims remain for
  still-dirty per-event artifacts.
- `architecture/nodes.toml` — registered the live event-prep skill and the new
  audit/test under `control.operate`; registered the WAV regression under
  `evidence.benchmark`.
- `architecture/operations.toml` — updated the public prepare-day description
  and classified the new audit as a `control.operate` internal adapter.
- `configs/launchd/com.earshot.preflight-nightly.plist` — changed the loaded
  command from preflight-only to full current-UTC-day preparation and moved its
  log to `event-prep-nightly.log`.
- `docs/STATUS.md` — updated current state with the seven-stage facade, audit,
  discovery retry, WAV gate, and live ROST verdict.
- `docs/nodes/control-operate.md` — documented the expanded facade and the
  whole-event audit contract.
- `docs/nodes/platform-runtime.md` — documented the retained legacy label and
  its new full-chain loaded command.
- `docs/runbooks/EVENT-LIFECYCLE.md` — replaced the five-stage procedure with
  the seven-stage, fail-closed lifecycle.
- `scripts/discover-events.py` — added bounded transient-network retries,
  offline retry regression coverage, and atomic JSON publication.
- `scripts/opsctl.sh` — added fresh discovery and final readiness audit to
  real and dry-run preparation.
- `scripts/variant-study.sh` — added exact WAV payload/format validation before
  clips affect expected counts or readiness output.
- `tests/ops_surface.py` — updated the canonical dry-run contract.
- `tests/opsctl.py` — updated facade sequencing fixtures and assertions.

## Files created

- `scripts/event-prep-audit.py` — read-only, date-level authority for the word
  READY; emits a blocker and next action per event.
- `tests/event_prep_audit.py` — isolated regressions for missing staging,
  complete single-host readiness, and missing measured posture.
- `tests/variant_study_wav.sh` — proves header-only and wrong-rate WAVs fail and
  a real 16-kHz mono PCM clip passes.

## Files intentionally left untouched

- The dirty per-event DE/FUTU/WMT configs, untracked AAP/ROST configs/envs,
  generated `.readiness`/`.acks`, `slate/call-times.json`, and runtime `var/`
  trees were not included in `ad58316`. They belong to the five-event live lane
  or generated evidence and must not be swept into a commit.
- `configs/bj-26aug21-raw.toml`, `configs/webcast-bj-26aug21-arm-wc.toml`, and
  `configs/twilio-live-aap.toml` were left untouched because they appear to be
  another worker's unclaimed work.
- Risk caps, budgets, detector thresholds, `src/risk.rs`, execution policy, and
  live arming behavior were not changed.
- Archived skill copies under `docs/history/` were left frozen as historical
  evidence; only `.claude/skills/event-prep/` is authoritative.
- Existing `url-push-job.sh`, `laptop-failover.sh`, and `event-watchdog.sh`
  implementations were not rewritten. The new audit makes their absence
  blocking instead of relying on memory.

## Source-of-truth documents

- `AGENTS.md` — node ownership, exact-file claims, dirty-tree safety, and done
  criteria.
- `docs/STATUS.md` and `docs/NODES.md` — current project state and node map.
- `docs/nodes/control-operate.md` — facade/lifecycle ownership.
- `docs/nodes/evidence-benchmark.md` — keyword-study ownership and evidence
  contract.
- `docs/nodes/platform-runtime.md` — loaded host scheduler contract.
- `docs/runbooks/EVENT-LIFECYCLE.md` — current generic event procedure.
- `docs/history/HANDOFF-26AUG20-FIVE-EVENTS.md` — initial five-event facts,
  venue URLs, timings, and old ROST binding recipe. Note that its ROST
  `access-mode public` example is superseded by the refined skill: a Notified
  registration wall must be bound as `registered-session` with attestation.
- `.claude/skills/event-prep/SKILL.md` — authoritative agent procedure after
  `ad58316`.

## Current project state

The reconstruction code and skill are committed and the full nightly
LaunchAgent is loaded. Focused verification passed:

- `python3 tests/ops_surface.py` — 8 tests
- `python3 tests/opsctl.py -q` — 16 tests
- `python3 tests/event_prep_audit.py` — 3 tests
- `tests/variant_study_wav.sh`
- `python3 scripts/discover-events.py --self-test`
- `scripts/schedule-nightly.sh --check`
- shell syntax, Python compilation, and plist validation

`scripts/doc-lint.sh` remains red for pre-existing unrelated state: two old
bakeoff configs are claimed by the benchmark claim but owned by
`control.prepare`, and unclaimed BJ/Twilio work is dirty. Do not “fix” those by
claiming or committing another worker's files without first resolving
ownership.

The live date audit currently reports:

- AAP — `NEEDS-ACCESS` on laptop and box.
- DE — `NEEDS-SCHEDULE` for the box attempt; the live DE event already ran.
- FUTU — `NEEDS-SCHEDULE` for the box attempt; the live event already ran.
- ROST — `NEEDS-ACCESS` on both active post-posture attempts.
- WMT — `NEEDS-SUPPORT` because no two loaded watchdogs are visible.

Active ROST attempts:

- Laptop:
  `var/runs/KXEARNINGSMENTIONROST-26AUG20-WEBCAST/20260820T201500Z-p3-e6e7dd16df6e`
- Box:
  `var/runs/KXEARNINGSMENTIONROST-26AUG20-BOX-WEBCAST/20260820T201500Z-p3-1e18c786a6eb`

Both currently have only `plan_compiled`. ROST starts at `20:15Z` and uses the
Notified MMC player `https://edge.media-server.com/mmc/p/6rfh9au9/`.

## Immediate next steps

1. Invoke the refined skill for ROST and read its current machine verdict:

   ```bash
   python3 scripts/event-prep-audit.py --date 2026-08-20
   ```

2. Register/verify the persistent Notified player session in a headed browser
   using the standing identity and profile `~/.earshot/profiles/rost`. Confirm
   the player is past the form; registration success alone is insufficient.

3. Bind both exact active ROST attempts as `registered-session` plus
   `preresolved`, using:

   - `--attested-by <operator-id>`
   - `--session-ref browser-profile:rost` (or another value accepted by the
     current access-plan schema)
   - `--session-expires-at-utc` later than the resolver deadline
   - `urlfile-wait.sh` on `$HOME/rost-stream.url`
   - browser preresolve argv with the persistent profile, `--linger-secs 20`,
     `--reject-endlist`, `--click-play`, and the 6600-second wait window.

   Do not copy the old handoff's `access-mode public`; the venue has a
   registration prerequisite.

4. Run access, preflight, and scheduling against the active runs:

   ```bash
   scripts/opsctl.sh access --date 2026-08-20
   scripts/opsctl.sh preflight --date 2026-08-20
   scripts/opsctl.sh schedule --date 2026-08-20
   ```

5. After scheduling, install and read back:

   - one PCM-gated multi-host ROST URL push, covering both exact runs;
   - the laptop failover wrapper pointing at the exact box run;
   - two watchdogs, at start+5 and start+35, naming both legs.

6. Finish only when this returns zero and prints READY:

   ```bash
   python3 scripts/event-prep-audit.py --date 2026-08-20
   ```

Do not resume ad-hoc monitoring in this continuation. Durable push, failover,
watchdog, schedule, and audit proof are the objective.
