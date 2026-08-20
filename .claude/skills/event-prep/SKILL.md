---
name: event-prep
description: Prepare every earnings-mention event on a UTC date end-to-end. Refreshes discovery, stages immutable runs, acquires real phone/webcast access, studies and postures keywords, preflights, schedules, installs URL-push/failover/watchdog coverage, and refuses READY until the whole-event audit passes. It prepares only; it never manually arms a live event or changes operator risk policy.
---

# Event Prep

## Outcome

Turn a target UTC date into one auditable row per discovered event:

`READY | NEEDS-STAGE | NEEDS-ACCESS | NEEDS-STUDY | NEEDS-POSTURE |
NEEDS-PREFLIGHT | NEEDS-SCHEDULE | NEEDS-SUPPORT | DESCOPED(reason)`.

The code—not the agent’s confidence—decides READY:

```bash
python3 scripts/event-prep-audit.py --date YYYY-MM-DD
```

READY requires the refreshed discovery census, active immutable RunPlans,
actual access, a valid study, measured posture, preflight, loaded schedule
proof, two watchdogs, and any required PCM-gated URL push and laptop failover.
Never report READY from a successful individual command.

## Safety boundary

- Prepare only. Never invoke an arm by hand and never point a recorder or probe
  at a tradeable call.
- Never change risk caps, bands, budgets, ledger scope, or short-token policy by
  judgment. Posture may only come from `posture-from-study.py` under an already
  recorded operator policy.
- Never use `EARSHOT_GREEN_OVERRIDE` unless the operator personally supplies
  the reason.
- Digits, PINs, cookies, and credentials stay outside git. Phone digits belong
  in `state/hunt/numbers.env` mode 0600; browser identity stays in the external
  browser profile.
- Preserve immutable RunPlans. A config edit creates a new attempt; never
  rewrite a pinned plan or copy proof from different bytes.
- Follow `AGENTS.md`: read status/node contracts, inspect claims and the dirty
  tree, and claim exact governed files before editing.

## Canonical start

Read the current state, preview, then run the full facade:

```bash
sed -n '1,220p' docs/STATUS.md
sed -n '1,220p' docs/NODES.md
git status --short
scripts/opsctl.sh prepare-day --date YYYY-MM-DD --dry-run
scripts/opsctl.sh prepare-day --date YYYY-MM-DD
python3 scripts/event-prep-audit.py --date YYYY-MM-DD
```

`prepare-day` is now exactly:

`discover → stage → access → study → preflight → schedule → support → readiness audit`.

Discovery is a fresh broad plus per-series sweep, not yesterday’s slate. It
retries transient DNS/5xx/429 failures and atomically publishes the result. The
facade continues ready siblings after one failure but returns nonzero. Work the
audit blockers below, rerun the necessary phase, and finish by rerunning the
audit. Fix, explicitly descope, or leave the date red—silence is not an
outcome.

## Blocker playbook

### NEEDS-STAGE

First prove that the event exists and its time is real:

1. Read its row in `slate/latest.json`. Ticker dates are not call dates.
2. Confirm date/time from two independent sources, one preferably the issuer.
3. Add the exact UTC time and provenance to `slate/call-times.json`.
4. Stage and re-audit:

```bash
scripts/opsctl.sh stage --date YYYY-MM-DD
python3 scripts/event-prep-audit.py --date YYYY-MM-DD
```

Zero events is not a quiet day; it is `NEEDS-DISCOVERY`. A missing active
RunPlan, ambiguous active attempts, placeholder time, or config drift remains
`NEEDS-STAGE`. The proof-bearing `prepared` receipt is published during
preflight, so its absence on an otherwise staged run is `NEEDS-PREFLIGHT`.

### NEEDS-ACCESS

Access means a real acquisition path, not a URL or registration receipt.

For every active webcast RunPlan:

1. Open the actual event/player page in the persistent browser profile.
2. Classify access as `public`, `registered-session`, or `signed-in-session`.
3. Identify the resolver from observed player traffic/API behavior.
4. Prove the same player class can yield host-ingestible PCM. A past replay of
   that player is sufficient for the ingest mechanism; current dynamic media
   is proven by the scheduled PCM gate at fire time.
5. Bind the exact active RunPlan and rerun the date gate:

```bash
scripts/opsctl.sh bind-access --run-dir var/runs/<EVENT>/<RUN> \
  --access-mode <public|registered-session|signed-in-session> \
  --resolver-kind <q4-api|youtube-live|browser-network|choruscall|preresolved|static-public> \
  --resolver-argv-json '<exact non-secret argv>' <session/resolver options>
scripts/opsctl.sh access --date YYYY-MM-DD
```

Registration is not admission. Confirm the player is in-room, the session will
survive unattended, and its expiry exceeds the resolver deadline.

#### Q4 priority: inspect participant dial-in first

On every Q4 event, inspect the event page/player for a participant dial-in
number and PIN before settling on browser audio. A visible participant “Dial
in” option is the preferred acquisition lane because it avoids player/session
fragility. Qualify and stage that phone lane first, with webcast retained as
redundancy when possible.

Do not confuse Q4’s presenter bridge metadata with participant entry:
`configuration.dialIn.speaker=true` or a `third_party` conference intake says
presenters can dial; it does not publish a listener number. A participant lane
requires the actual visible number/PIN or equivalent attendee instructions.

#### Venue rules that cost events

- Notified MMC (`edge.media-server.com/mmc/p/...`): registration wall plus a
  holding VOD. The top-level `metafile` master may decode to zero frames while
  a player-requested media playlist works. Use browser-network resolution with
  `--reject-endlist`, reject the holding hash, linger for observed manifests,
  and let `url-push-job.sh` choose only a candidate that passes `pcm-gate.sh`.
- webinar.net: WebRTC may have no HLS path. Do not invent a manifest; use a
  supported browser/phone acquisition lane or descope.
- Q4: API resolver is preferred for public webcast media; participant phone is
  preferred when the event actually publishes it.
- YouTube: use the existing YouTube resolver path; do not replace it with an
  unmeasured generic HLS recipe.

### NEEDS-STUDY

Run on the supported macOS study host:

```bash
scripts/opsctl.sh study --date YYYY-MM-DD
```

`variant-study.sh` now validates real mono/16-kHz/16-bit sample payload in every
TTS clip and the concatenated replay. A successful `say`/ffmpeg exit with a
header-only WAV is a study failure, never evidence.

Dead-word resolution has only three outcomes:

- adopt a widening proposal, creating a new config hash and RunPlan;
- acknowledge only measured, non-crossing residue within the existing guards;
- descope the event/market on the record.

Never delete a hard word merely to make the board green.

### NEEDS-POSTURE

The audit requires a measured `# posture:` marker on every keyword cap. Apply
only the operator’s already recorded policy:

```bash
python3 scripts/posture-from-study.py configs/<event>.toml \
  --compile-with target/release/earshot \
  --model-dir <model_dir from that config> \
  <recorded size-tier/low-band arguments>
python3 scripts/eventctl.py init-date --date YYYY-MM-DD \
  --earshot-bin target/release/earshot
```

Posture changes config bytes. `init-date` must close/archive the superseded
attempt and compile a new immutable attempt. Rebind access to the new run, then
rerun study/access/preflight. Do not pretend the old AccessPlan or preflight
belongs to new bytes. If no size/low-band policy is recorded, stop at
`NEEDS-POSTURE`; do not invent one.

### NEEDS-PREFLIGHT / NEEDS-SCHEDULE

```bash
scripts/opsctl.sh preflight --date YYYY-MM-DD
export EARSHOT_LOCAL_EXECUTION_HOST=<stable laptop id>
export EARSHOT_BOX_EXECUTION_HOST=earshot-useast1
scripts/opsctl.sh schedule --date YYYY-MM-DD
```

Scheduling is host-directed from immutable `execution_host_id`. It must publish
`control/schedule-proof.json`, read the loaded descriptor back, and append the
`scheduled` receipt. A plist/unit file on disk is not coverage.

### NEEDS-SUPPORT

Run the typed support installer only after scheduling; it derives exact host
RunPlans and readbacks instead of relying on hand-transcribed event/run strings:

```bash
scripts/opsctl.sh support --date YYYY-MM-DD
```

The commands below document the adapters it drives and remain recovery tools,
not the normal preparation path.

For any `preresolved` event, use one multi-host push job. Start at T−2; the job
may retry through the AccessPlan deadline and publishes only after host-local
PCM succeeds:

```bash
scripts/url-push-job.sh --install --slug <issuer-lc> \
  --at-utc <start-2m> --until-utc <deadline> \
  --run-dir var/runs/<BOX_EVENT>/<BOX_RUN> --host <box> \
  --run-dir var/runs/<LOCAL_EVENT>/<LOCAL_RUN> --host local
```

For a box-primary plus laptop lane, wrap the already loaded laptop job:

```bash
scripts/laptop-failover.sh --install \
  --run-dir var/runs/<LOCAL_EVENT>/<LOCAL_RUN> \
  --box-event <BOX_EVENT> --box-run <BOX_RUN> \
  --start-utc <HH:MM> --deadline-utc <HH:MM>
```

Install two watchdogs (start+5 and start+35), each naming every host leg:

```bash
scripts/event-watchdog.sh --install --slug <issuer-lc> --at-utc <HH:MM> \
  --leg <BOX_EVENT>:<BOX_RUN>:<box> \
  --leg <LOCAL_EVENT>:<LOCAL_RUN>:local
```

Session wakeups, terminal tails, and an agent promising to watch do not count.
Only loaded jobs and the audit count.

## Final verification and handoff

```bash
python3 scripts/event-prep-audit.py --date YYYY-MM-DD
scripts/opsctl.sh status YYYY-MM-DD
launchctl print gui/$(id -u)/com.earshot.preflight-nightly \
  | rg 'opsctl.sh|prepare-day|event-prep-nightly'
```

The scheduled `com.earshot.preflight-nightly` label is intentionally retained
for migration compatibility, but its loaded command must be the full
`opsctl prepare-day --date $(date -u +%F)` completion job. Earlier slate,
stage, and study jobs are head starts; this final job is the scheduled arrow
that reaches schedule plus the whole-event audit.

Report one line per event and include the audit’s exact blocker/next action.
For ROST specifically, do not reuse the superseded pre-posture attempts: bind
and advance only the active post-posture laptop and box RunPlans, register the
Notified session, install the PCM-gated multi-host push, failover, and both
watchdogs, then require the audit to say READY.

## References

- `references/TRAPS.md` — dated failures and why each guard exists.
- `references/BOX-CHECKS.md` — box dispatch, identity, transport, and readback.
- `docs/runbooks/EVENT-LIFECYCLE.md` — lifecycle and evidence contract.
- `docs/nodes/control-operate.md` — facade and final readiness join.
